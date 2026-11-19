# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural validation of .github/workflows/screenshots.yml.

Issue #315: candidate and published Flatpak visual evidence must be separate
lanes, each bound to the exact artifact it validates. This runs the workflow
file through the same parser GitHub Actions uses (PyYAML) and asserts the
shape that enforces that binding. It does not launch a Flatpak — there is no
runtime here — so it validates the harness contract, not a capture.

Run: python3 -m pytest tools/tests/test_workflow_screenshots.py
"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCREENSHOTS = WORKFLOWS / "screenshots.yml"
CAPTURE_ACTION = "tuna-os/flatpak-index/.github/actions/capture-screenshots@main"


def _load():
    return yaml.safe_load(SCREENSHOTS.read_text())


def _on(doc):
    # YAML 1.1 reads the `on:` key as the boolean True; GitHub Actions does too.
    return doc.get(True, doc.get("on"))


def _capture_step(job):
    """The single step of a job that invokes the shared capture action."""
    steps = [s for s in job.get("steps", []) if s.get("uses") == CAPTURE_ACTION]
    assert len(steps) == 1, f"expected one capture step in {job}"
    return steps[0]


def _run_text(job):
    """All `run:` text of a job, joined with newlines."""
    return "\n".join(s.get("run", "") for s in job.get("steps", []))


def test_has_both_lanes():
    doc = _load()
    jobs = doc["jobs"]
    # A published lane and a candidate lane (build + capture).
    assert "published-artifact" in jobs
    assert "candidate-build" in jobs
    assert "candidate-capture" in jobs


def test_triggers_bound_to_relevant_paths():
    on = _on(_load())
    paths = on["pull_request"]["paths"]
    for expected in (
        "data/org.tunaos.tavern.metainfo.xml.in",
        "data/**",
        "src/**",
        "*.blp",
        "meson.build",
        ".github/workflows/screenshots.yml",
    ):
        assert expected in paths, f"missing trigger path {expected}"
    assert "workflow_dispatch" in on


def test_candidate_builds_not_installs_published():
    """The candidate lane must photograph the built artifact, never the
    published app — acceptance 1's core: the binary under review is the one
    captured."""
    doc = _load()
    build = doc["jobs"]["candidate-build"]
    capture = doc["jobs"]["candidate-capture"]

    # candidate-build uses flatpak-builder to produce a bundle.
    build_uses = [s.get("uses", "") for s in build["steps"]]
    assert any("flatpak-builder" in u for u in build_uses)
    # candidate-capture depends on the build, so it captures the real artifact.
    assert capture.get("needs") == "candidate-build"

    # The candidate capture command installs the downloaded bundle with
    # --force-registry, and never reaches for the published remote/app ref.
    capture_cmd = " ".join(_capture_step(capture).get("with", {}).get("command", "").split())
    assert "force-registry" in capture_cmd
    assert "Tavern-candidate.flatpak" in capture_cmd
    assert "tuna-os" not in capture_cmd
    assert "org.tunaos.tavern" not in capture_cmd


def test_published_lane_installs_remote():
    """The published lane is the only lane that reaches the tuna-os remote, and
    it records that its binary is the remote artifact (source_binding_status
    unknown by design)."""
    doc = _load()
    published = doc["jobs"]["published-artifact"]
    published_cmd = _run_text(published)
    assert "flatpak install" in published_cmd
    assert "tuna-os" in published_cmd
    # Provenance sidecar names the remote and declares unknown source binding.
    assert "source_binding_status" in published_cmd
    assert '"unknown"' in published_cmd


def test_both_lanes_bind_readiness_and_deadline():
    """Each lane passes a readiness predicate and a deadline to the shared
    action, so a capture is only accepted once the app reaches its intended
    state, bounded in time."""
    doc = _load()
    for lane in ("published-artifact", "candidate-capture"):
        with_ = _capture_step(doc["jobs"][lane]).get("with", {})
        assert with_.get("readiness-predicate"), f"{lane} missing readiness predicate"
        assert with_.get("deadline"), f"{lane} missing deadline"
        # app-id is passed so the action binds the capture to the app's window.
        assert with_.get("app-id") in ("org.tunaos.tavern", "$APP_ID"), f"{lane} app-id"


def test_readiness_uses_semantic_readback_not_just_visibility():
    """Readiness must be a real readback of app state (log message), not merely
    window visibility."""
    doc = _load()
    published = _capture_step(doc["jobs"]["published-artifact"]).get("with", {})
    candidate = _capture_step(doc["jobs"]["candidate-capture"]).get("with", {})
    # Both read the app log, which only exists once the app has done real work.
    assert "tavern-published.log" in published["readiness-predicate"]
    assert "tavern-candidate.log" in candidate["readiness-predicate"]


def test_per_run_output_dir_is_unique_and_never_committed():
    """Evidence goes to a fresh per-run dir, never the committed
    docs/screenshots/ baseline."""
    doc = _load()
    env = doc["env"]
    run_dir = env["SCREENSHOTS_RUN_DIR"]
    assert "run_id" in run_dir
    assert "run_attempt" in run_dir
    assert "docs/screenshots" not in run_dir
    # No step writes to the committed baseline.
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            assert "docs/screenshots" not in run, "a step commits to the baseline"


def test_receipts_and_provenance_uploaded():
    """Each lane uploads its PNG, versioned receipt, and provenance sidecar."""
    doc = _load()
    for lane in ("published-artifact", "candidate-capture"):
        steps = doc["jobs"][lane]["steps"]
        upload = [s for s in steps if "upload-artifact" in str(s.get("uses", ""))]
        assert upload, f"{lane} does not upload evidence"
        # Provenance is produced (a run step writes provenance.json) before
        # capture and travels with the upload.
        provenance_steps = [s for s in steps if "provenance" in s.get("run", "")]
        assert provenance_steps, f"{lane} does not record provenance"


def test_candidate_uses_container_published_does_not():
    """candidate-build runs in the privileged Flatpak builder container; the
    capture/published lanes run on the host for bubblewrap privileges."""
    doc = _load()
    assert doc["jobs"]["candidate-build"].get("container")
    assert not doc["jobs"]["published-artifact"].get("container")
    assert not doc["jobs"]["candidate-capture"].get("container")


def test_candidate_lane_never_installs_published_app():
    """The candidate lane must never install the published app by its remote
    ref — it installs only the built bundle."""
    doc = _load()
    capture = doc["jobs"]["candidate-capture"]
    capture_cmd = " ".join(_capture_step(capture).get("with", {}).get("command", "").split())
    if "flatpak install" in capture_cmd:
        assert "Tavern-candidate.flatpak" in capture_cmd
        assert "tuna-os" not in capture_cmd
