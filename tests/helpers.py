"""Simulated runner and detection-stage manager.

These are **not** the production runner/manager — that code doesn't exist
yet. They are a reference implementation of the contracts described in
AGENTS.md, used by scenario tests to assert manager behaviour today.

When the real code lands, tests can either:
  - switch to calling the real runner/manager, or
  - keep these helpers as a contract baseline the real code is checked
    against.

What each helper encodes:

`apply_detector_stamp_via_runner`
    Mirrors what the runner does with a `routing`-contract skill response.
    Parses the canned detector output, persists `UPSTREAM_VERSION` and
    `SOURCE_URL` into `target.*`, appends a detection stamp, appends a
    history entry, and commits — all in one signed commit. Does **not**
    touch `current_stage` or `current_owner` (invariant 8).

`detection_manager_decide`
    Mirrors what the detection-stage manager does after the runner's stamp
    lands. Reads the latest stamp, applies the current gate policy, and
    writes `current_stage` / `current_owner` / `status` / `intervention` in
    a second signed commit. Current policy (revisit when gates.yaml exists):

      NEW_RELEASE      → advance current_stage to "packaging"
      UP_TO_DATE       → status: terminated
      IN_FLIGHT        → status: terminated
      NEW_PRERELEASE   → status: terminated (no beta packaging today)
      UNCERTAIN        → status: escalated, intervention populated
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

_LINE = re.compile(r"^([A-Z_]+):\s*(.*)$")
_SCALAR_FIELDS = {
    "STATE",
    "PACKAGE",
    "OPENSTACK_SERIES",
    "UBUNTU_SERIES",
    "UPSTREAM_VERSION",
    "PACKAGED_VERSION",
    "IN_FLIGHT_VERSION",
    "SOURCE_URL",
    "CONFIDENCE",
}


def parse_detector_response(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw in text.splitlines():
        m = _LINE.match(raw.strip())
        if m and m.group(1) in _SCALAR_FIELDS:
            fields[m.group(1)] = m.group(2).strip()
    return fields


def load_wp(repo: Path) -> dict:
    return yaml.safe_load((repo / "workpackage.yaml").read_text())


def save_wp(repo: Path, data: dict) -> None:
    (repo / "workpackage.yaml").write_text(yaml.safe_dump(data, sort_keys=False))


def _git_commit(repo: Path, message: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(
        ["git", "-C", str(repo), "add", "workpackage.yaml"],
        check=True, env=env, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-C", str(repo), "commit", "-q", "-m", message],
        check=True, env=env, capture_output=True,
    )


def apply_detector_stamp_via_runner(repo: Path, response_text: str) -> None:
    fields = parse_detector_response(response_text)
    wp = load_wp(repo)

    upstream = fields.get("UPSTREAM_VERSION", "")
    if upstream and upstream != "unknown":
        wp["target"]["upstream_version"] = upstream

    source = fields.get("SOURCE_URL", "")
    if source and source != "none":
        wp["target"]["trigger"]["source"] = source

    state = fields.get("STATE", "UNCERTAIN")
    try:
        confidence = float(fields.get("CONFIDENCE", "0") or 0)
    except ValueError:
        confidence = 0.0

    stamp = {
        "stage": "detection",
        "skill": "detector",
        "state": state,
        "upstream_version": fields.get("UPSTREAM_VERSION"),
        "packaged_version": fields.get("PACKAGED_VERSION"),
        "in_flight_version": fields.get("IN_FLIGHT_VERSION"),
        "confidence": confidence,
        "result": "needs_human_review" if state == "UNCERTAIN" else "verified",
    }
    wp["stamps"].append(stamp)
    wp["history"].append(
        {"event": "detection_stamp_written", "state": state, "by": "runner"}
    )
    save_wp(repo, wp)
    _git_commit(repo, f"runner: detection stamp ({state})")


def detection_manager_decide(repo: Path) -> str:
    """Apply current gate policy. Returns the action taken."""
    wp = load_wp(repo)
    if not wp["stamps"]:
        raise AssertionError("no stamps to decide on")
    stamp = wp["stamps"][-1]
    if stamp["stage"] != "detection":
        raise AssertionError(f"latest stamp is for stage {stamp['stage']}, not detection")

    state = stamp["state"]
    if state == "NEW_RELEASE":
        wp["current_stage"] = "packaging"
        wp["current_owner"] = "packager"
        action = "advance"
    elif state in ("UP_TO_DATE", "IN_FLIGHT", "NEW_PRERELEASE"):
        wp["status"] = "terminated"
        wp["current_owner"] = None
        action = "terminate"
    elif state == "UNCERTAIN":
        wp["status"] = "escalated"
        wp["current_owner"] = None
        wp["intervention"] = {
            "reason": "detector reported UNCERTAIN",
            "stamp_index": len(wp["stamps"]) - 1,
        }
        action = "escalate"
    else:
        raise AssertionError(f"unknown detector state: {state}")

    wp["history"].append(
        {"event": "manager_decision", "state": state, "action": action, "by": "manager"}
    )
    save_wp(repo, wp)
    _git_commit(repo, f"manager: {action} after detection {state}")
    return action
