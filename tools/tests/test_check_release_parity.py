# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/check-release-parity.py — the release-parity gate (#104, #227).

The channel decision is the gate: it decides whether a tag has reached every
supported channel. ``collect`` only gathers live state, so tests patch it and
exercise the decision and CLI without touching the network.

Each channel resolves to one of three states: PRESENT (True, present), ABSENT
(False, readable but missing — fails the gate), or UNKNOWN (None, unreadable —
surfaced as a warning, does not fail).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loader import load_module

check_release_parity = load_module("tools/check-release-parity.py", "check_release_parity")

PRESENT = check_release_parity.PRESENT
ABSENT = check_release_parity.ABSENT
UNKNOWN = check_release_parity.UNKNOWN


def all_present():
    return {channel: PRESENT for channel in check_release_parity.REQUIRED_CHANNELS}


def test_evaluate_parity_passes_when_all_channels_present():
    ok, missing, unknown = check_release_parity.evaluate_parity("0.1.64", all_present())
    assert ok is True
    assert missing == []
    assert unknown == []


def test_evaluate_parity_fails_lists_missing_canonically():
    present = all_present()
    present["homebrew_cask"] = ABSENT
    present["flatpak_oci"] = ABSENT
    ok, missing, unknown = check_release_parity.evaluate_parity("0.1.64", present)
    assert ok is False
    # Missing channels are reported in canonical order, not insertion order.
    assert missing == ["homebrew_cask", "flatpak_oci"]
    assert unknown == []


def test_evaluate_parity_reports_single_missing_channel():
    present = all_present()
    present["appimage"] = ABSENT
    ok, missing, unknown = check_release_parity.evaluate_parity("0.1.64", present)
    assert ok is False
    assert missing == ["appimage"]


def test_unknown_channel_does_not_fail_gate():
    present = all_present()
    present["flatpak_oci"] = UNKNOWN
    ok, missing, unknown = check_release_parity.evaluate_parity("0.1.64", present)
    assert ok is True
    assert missing == []
    assert unknown == ["flatpak_oci"]


def test_unknown_channels_reported_separately():
    present = all_present()
    present["homebrew_cask"] = UNKNOWN
    present["flatpak_oci"] = UNKNOWN
    ok, missing, unknown = check_release_parity.evaluate_parity("0.1.64", present)
    assert ok is True
    assert unknown == ["homebrew_cask", "flatpak_oci"]


def _patch_collect(monkeypatch, present):
    monkeypatch.setattr(check_release_parity, "collect", lambda tag, version: present)


def test_main_reports_ok_and_exits_zero(monkeypatch, capsys):
    _patch_collect(monkeypatch, all_present())
    code = check_release_parity.main(["--version", "0.1.64"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Release parity verified for 0.1.64" in out
    assert "github_release: ok" in out


def test_main_fails_and_exits_nonzero(monkeypatch, capsys):
    present = all_present()
    present["flatpak_oci"] = ABSENT
    _patch_collect(monkeypatch, present)
    code = check_release_parity.main(["--tag", "v0.1.64"])
    assert code == 1
    captured = capsys.readouterr()
    assert "Release parity FAILED for 0.1.64" in captured.err
    assert "flatpak_oci" in captured.err


def test_main_warns_on_unknown_channel_but_passes(monkeypatch, capsys):
    present = all_present()
    present["flatpak_oci"] = UNKNOWN
    _patch_collect(monkeypatch, present)
    code = check_release_parity.main(["--version", "0.1.64"])
    assert code == 0
    captured = capsys.readouterr()
    assert "warning" in captured.err
    assert "flatpak_oci" in captured.err


def test_main_strips_v_from_tag(monkeypatch, capsys):
    _patch_collect(monkeypatch, all_present())
    code = check_release_parity.main(["--tag", "v0.2.0"])
    assert code == 0
    assert "Release parity verified for 0.2.0" in capsys.readouterr().out


def test_main_requires_version_or_tag(monkeypatch, capsys):
    _patch_collect(monkeypatch, all_present())
    import pytest

    with pytest.raises(SystemExit) as exc:
        check_release_parity.main([])
    assert exc.value.code == 2


def test_collect_reads_github_assets_and_tap(monkeypatch):
    monkeypatch.setattr(check_release_parity, "release_exists", lambda tag: True)
    monkeypatch.setattr(
        check_release_parity,
        "release_asset_names",
        lambda tag: set(check_release_parity.RELEASE_ASSETS),
    )
    monkeypatch.setattr(check_release_parity, "cask_version", lambda: "0.1.64")
    monkeypatch.setattr(check_release_parity, "flatpak_oci_tags", lambda: ["latest", "v0.1.64"])

    present = check_release_parity.collect("v0.1.64", "0.1.64")
    assert present["github_release"] is PRESENT
    assert present["appimage"] is PRESENT
    assert present["flatpak"] is PRESENT
    assert present["macos"] is PRESENT
    assert present["homebrew_cask"] is PRESENT
    assert present["flatpak_oci"] is PRESENT


def test_collect_fails_homebrew_when_cask_version_mismatched(monkeypatch):
    monkeypatch.setattr(check_release_parity, "release_exists", lambda tag: True)
    monkeypatch.setattr(
        check_release_parity,
        "release_asset_names",
        lambda tag: set(check_release_parity.RELEASE_ASSETS),
    )
    monkeypatch.setattr(check_release_parity, "cask_version", lambda: "0.1.63")
    monkeypatch.setattr(check_release_parity, "flatpak_oci_tags", lambda: ["v0.1.64"])

    present = check_release_parity.collect("v0.1.64", "0.1.64")
    assert present["homebrew_cask"] is ABSENT


def test_collect_reports_oci_unknown_when_query_errors(monkeypatch):
    monkeypatch.setattr(check_release_parity, "release_exists", lambda tag: True)
    monkeypatch.setattr(
        check_release_parity,
        "release_asset_names",
        lambda tag: set(check_release_parity.RELEASE_ASSETS),
    )
    monkeypatch.setattr(check_release_parity, "cask_version", lambda: "0.1.64")
    monkeypatch.setattr(check_release_parity, "flatpak_oci_tags", lambda: None)

    present = check_release_parity.collect("v0.1.64", "0.1.64")
    assert present["flatpak_oci"] is UNKNOWN


@pytest.mark.parametrize(
    "tags, expected",
    [
        (["latest", "latest-x86_64", "v0.1.64-x86_64"], True),
        (["0.1.64"], True),
        (["latest", "v0.1.63-x86_64", "v0.1.640-x86_64"], False),
    ],
)
def test_oci_matches_per_arch_release_tags(tags, expected):
    assert check_release_parity._oci_has_release(tags, "v0.1.64", "0.1.64") is expected
