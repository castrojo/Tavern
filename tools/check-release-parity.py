#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify a Tavern release shipped on every supported channel.

Release-parity gate for #104 and #227. A tag is only a stable release when the
same promotion produced an artifact on every channel: the GitHub Release itself
(with its AppImage, Flatpak bundle, and macOS assets), the Flatpak OCI image on
ghcr.io, and the Homebrew cask in tuna-os/homebrew-tap.

This tool reports which channels are missing for a tag and exits non-zero so
the release workflow can fail the gate. The channel decision is pure and unit
tested; the ``collect`` step only gathers live state from GitHub.

Each channel resolves to one of three states:

- ``True``  -- the artifact is present on that channel.
- ``False`` -- the channel was readable and the artifact is absent. This fails
  the gate: a tag without an artifact on a channel is not a stable release.
- ``None``  -- the channel could not be read (scope, network, registry down).
  This is surfaced as a warning but does not fail the gate, so a transient
  read failure never blocks a release path on a false positive.
"""

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Canonical channel order. A stable release must reach every one of them.
REQUIRED_CHANNELS = (
    "github_release",
    "appimage",
    "flatpak",
    "macos",
    "homebrew_cask",
    "flatpak_oci",
)

REPO = "tuna-os/Tavern"
TAP_REPO = "tuna-os/homebrew-tap"
TAP_CASK = "Casks/tavern.rb"
FLATPAK_PACKAGE = "tavern"

# Asset names the GitHub Release must carry.
RELEASE_ASSETS = ("Tavern-Linux.AppImage", "Tavern-Linux.flatpak", "Tavern-macOS.zip")

PRESENT = True
ABSENT = False
UNKNOWN = None


def evaluate_parity(version, present):
    """Return ``(ok, missing, unknown)`` for a release.

    ``present`` maps each channel name to ``True``/``False``/``None``.
    ``missing`` lists definitively absent channels (these fail the gate);
    ``unknown`` lists channels that could not be read.
    """
    missing = [channel for channel in REQUIRED_CHANNELS if present.get(channel) is ABSENT]
    unknown = [channel for channel in REQUIRED_CHANNELS if present.get(channel) is UNKNOWN]
    return (len(missing) == 0, missing, unknown)


def _run(argv, check=True):
    return subprocess.run(argv, capture_output=True, text=True, check=check)


def release_exists(tag, repo=REPO):
    result = _run(["gh", "release", "view", tag, "--repo", repo, "--json", "id"], check=False)
    return result.returncode == 0 and "id" in result.stdout


def release_asset_names(tag, repo=REPO):
    result = _run(["gh", "release", "view", tag, "--repo", repo, "--json", "assets"], check=False)
    if result.returncode != 0:
        return set()
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return set()
    return {asset["name"] for asset in data.get("assets", [])}


def tap_default_branch(repo=TAP_REPO):
    result = _run(["gh", "api", f"repos/{repo}", "--jq", ".default_branch"], check=False)
    if result.returncode != 0:
        return "main"
    return (result.stdout or "main").strip() or "main"


def _fetch_raw(url):
    result = _run(["curl", "--fail", "--location", "--silent", url], check=False)
    return result.stdout if result.returncode == 0 else ""


def cask_version(repo=TAP_REPO, path=TAP_CASK):
    """Read the cask version from the maintained tap, or None if unreadable.

    Reads the authenticated contents API first (works with the release token in
    CI). If that is unavailable, fall back to the public raw URL so the check
    still runs against a public tap without extra credentials.
    """
    branch = tap_default_branch(repo)
    content_result = _run(
        [
            "gh",
            "api",
            f"repos/{repo}/contents/{path}?ref={branch}",
            "--jq",
            ".content",
        ],
        check=False,
    )
    if content_result.returncode == 0 and content_result.stdout:
        content = base64.b64decode(content_result.stdout).decode("utf-8", errors="replace")
    else:
        content = _fetch_raw(f"https://raw.githubusercontent.com/{repo}/{branch}/{path}")
        if not content:
            return None
    match = re.search(r'version\s+"([^"]+)"', content)
    return match.group(1) if match else None


def flatpak_oci_tags(org=REPO.split("/")[0], package=FLATPAK_PACKAGE):
    """Return the tags published to ghcr.io/<org>/<package>, or None if unreadable.

    A 200 with no matching tag means the artifact is absent; a non-2xx response
    (scope, registry down) is inconclusive and returns None.
    """
    result = _run(
        [
            "gh",
            "api",
            f"orgs/{org}/packages/container/{package}/versions?per_page=100",
            "--jq",
            ".[].metadata.container.tags[]",
        ],
        check=False,
    )
    if result.returncode != 0:
        return None
    tags = []
    for line in result.stdout.splitlines():
        line = line.strip().strip('"')
        if line:
            tags.append(line)
    return tags


def _oci_has_release(oci_tags, tag, version):
    """True if an OCI tag carries this release.

    The shared publisher pushes per-arch tags (``v0.1.64-x86_64``), so accept
    the bare tag/version or either with an ``-<arch>`` suffix.
    """
    names = (tag, version)
    return any(t in names or any(t.startswith(f"{n}-") for n in names) for t in oci_tags)


def collect(tag, version):
    """Gather live channel state for a tag. Pure keys, network-backed values."""
    asset_names = release_asset_names(tag)
    cask = cask_version()
    oci_tags = flatpak_oci_tags()

    cask_status = cask == version if cask is not None else UNKNOWN
    oci_status = _oci_has_release(oci_tags, tag, version) if oci_tags is not None else UNKNOWN

    return {
        "github_release": release_exists(tag),
        "appimage": "Tavern-Linux.AppImage" in asset_names,
        "flatpak": "Tavern-Linux.flatpak" in asset_names,
        "macos": "Tavern-macOS.zip" in asset_names,
        "homebrew_cask": cask_status,
        "flatpak_oci": oci_status,
    }


def _status_label(value):
    if value is PRESENT:
        return "ok"
    if value is ABSENT:
        return "MISSING"
    return "UNKNOWN"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify a Tavern release reached every channel.")
    parser.add_argument("--version", help="release version without the v prefix (for example 0.1.64)")
    parser.add_argument("--tag", help="release tag, for example v0.1.64")
    parser.add_argument("--repo", default=REPO, help="Tavern repository (owner/name)")
    args = parser.parse_args(argv)

    if args.tag and not args.version:
        version = args.tag[1:] if args.tag.startswith("v") else args.tag
    elif args.version:
        version = args.version
    else:
        parser.error("provide --version or a v-prefixed --tag")

    present = collect(args.tag or f"v{version}", version)
    ok, missing, unknown = evaluate_parity(version, present)

    for channel in REQUIRED_CHANNELS:
        print(f"{channel}: {_status_label(present.get(channel))}")

    if unknown:
        print(f"warning: could not verify {', '.join(unknown)}", file=sys.stderr)

    if ok:
        print(f"Release parity verified for {version}: all {len(REQUIRED_CHANNELS)} channels present")
        return 0

    print(
        f"Release parity FAILED for {version}: missing {', '.join(missing)}. "
        "A tag is not a stable release until every channel carries it.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
