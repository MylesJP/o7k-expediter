"""Scenarios driving the detection stage with canned skill responses.

Each test feeds a starting workpackage + a canned detector response through
the simulated runner and manager (see `tests/helpers.py`), and asserts the
resulting workpackage YAML state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.helpers import (
    apply_detector_stamp_via_runner,
    detection_manager_decide,
    load_wp,
)


def _git_log(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%s"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def test_new_release_advances_to_packaging(workpackage_repo, detector_response):
    repo = workpackage_repo("fresh")

    apply_detector_stamp_via_runner(repo, detector_response("new_release"))
    action = detection_manager_decide(repo)

    wp = load_wp(repo)
    assert action == "advance"
    assert wp["current_stage"] == "packaging"
    assert wp["current_owner"] == "packager"
    assert wp["target"]["upstream_version"] == "31.0.1"
    assert wp["target"]["trigger"]["source"].endswith("/hibiscus/cinder.yaml")
    assert wp["stamps"][-1]["state"] == "NEW_RELEASE"
    assert wp["stamps"][-1]["result"] == "verified"
    # Two signed commits: runner stamp, then manager advance.
    assert len(_git_log(repo)) == 3  # init + runner + manager


def test_up_to_date_terminates_without_advancing(workpackage_repo, detector_response):
    repo = workpackage_repo("fresh")

    apply_detector_stamp_via_runner(repo, detector_response("up_to_date"))
    action = detection_manager_decide(repo)

    wp = load_wp(repo)
    assert action == "terminate"
    assert wp["status"] == "terminated"
    assert wp["current_stage"] == "detection"  # never advanced
    assert wp["current_owner"] is None
    assert wp["stamps"][-1]["state"] == "UP_TO_DATE"


def test_in_flight_terminates(workpackage_repo, detector_response):
    repo = workpackage_repo("fresh")

    apply_detector_stamp_via_runner(repo, detector_response("in_flight"))
    action = detection_manager_decide(repo)

    wp = load_wp(repo)
    assert action == "terminate"
    assert wp["status"] == "terminated"
    assert wp["stamps"][-1]["state"] == "IN_FLIGHT"
    assert wp["stamps"][-1]["in_flight_version"] == "31.0.1"


def test_new_prerelease_terminates_under_default_policy(workpackage_repo, detector_response):
    """Default policy: don't package betas. Flip when policy changes."""
    repo = workpackage_repo("fresh")

    apply_detector_stamp_via_runner(repo, detector_response("new_prerelease"))
    action = detection_manager_decide(repo)

    wp = load_wp(repo)
    assert action == "terminate"
    assert wp["status"] == "terminated"
    assert wp["stamps"][-1]["state"] == "NEW_PRERELEASE"


def test_uncertain_escalates_with_intervention(workpackage_repo, detector_response):
    repo = workpackage_repo("fresh")

    apply_detector_stamp_via_runner(repo, detector_response("uncertain"))
    action = detection_manager_decide(repo)

    wp = load_wp(repo)
    assert action == "escalate"
    assert wp["status"] == "escalated"
    assert wp["intervention"] is not None
    assert wp["intervention"]["reason"] == "detector reported UNCERTAIN"
    assert wp["stamps"][-1]["state"] == "UNCERTAIN"
    assert wp["stamps"][-1]["result"] == "needs_human_review"
    # Upstream version was "unknown" — runner should not have overwritten target.
    assert wp["target"]["upstream_version"] == ""


def test_runner_does_not_touch_current_stage(workpackage_repo, detector_response):
    """Invariant 8: only managers update current_stage and current_owner."""
    repo = workpackage_repo("fresh")
    before = load_wp(repo)

    apply_detector_stamp_via_runner(repo, detector_response("new_release"))

    after = load_wp(repo)
    assert after["current_stage"] == before["current_stage"] == "detection"
    assert after["current_owner"] == before["current_owner"]
