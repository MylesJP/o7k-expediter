"""Work-package YAML load / save / stamp primitives.

A work package is a YAML file created from resources/wp_template.yaml.
This module handles creation, loading, saving, and appending stamps.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE_PATH = _REPO_ROOT / "resources" / "wp_template.yaml"
_WORK_PACKAGES_DIR = _REPO_ROOT / "work_packages"

WP_FILENAME = "workpackage.yaml"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Core I/O
# ---------------------------------------------------------------------------

def load(wp_path: Path) -> dict:
    """Load a workpackage.yaml and return as dict."""
    return yaml.safe_load(wp_path.read_text())


def save(wp: dict, wp_path: Path, message: str = "") -> None:
    """Write *wp* dict back to *wp_path*."""
    wp_path.write_text(yaml.dump(wp, default_flow_style=False, sort_keys=False))
    if message:
        print(f"[workpackage] saved: {message}")


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def create_wp(
    package: str,
    upstream_version: str,
    source_url: str,
    *,
    openstack_series: str | None = None,
    ubuntu_release: str | None = None,
    uca_pocket: str | None = None,
    base_dir: Path | None = None,
) -> Path:
    """Create a new workpackage.yaml from the template. Returns path to the file."""
    base = base_dir or _WORK_PACKAGES_DIR
    base.mkdir(parents=True, exist_ok=True)

    wp_id = f"{package}-{upstream_version}-{uuid.uuid4().hex[:8]}"
    wp_dir = base / wp_id
    wp_dir.mkdir()

    template = _TEMPLATE_PATH.read_text()
    rendered = (
        template
        .replace("{{WP_ID}}", wp_id)
        .replace("{{PROJECT}}", package)
        .replace("{{TIMESTAMP}}", _now_iso())
        .replace("{{SOURCE_URL}}", source_url)
    )

    wp_data = yaml.safe_load(rendered)
    wp_data["target"]["upstream_version"] = upstream_version
    if openstack_series:
        wp_data["target"]["openstack_series"] = openstack_series
    if ubuntu_release:
        wp_data["target"]["ubuntu_release"] = ubuntu_release
    if uca_pocket:
        wp_data["target"]["uca_pocket"] = uca_pocket

    wp_path = wp_dir / WP_FILENAME
    wp_path.write_text(yaml.dump(wp_data, default_flow_style=False, sort_keys=False))
    print(f"[workpackage] created: {wp_path}")
    return wp_path


# ---------------------------------------------------------------------------
# Stamps
# ---------------------------------------------------------------------------

def add_stamp(
    wp: dict,
    wp_path: Path,
    *,
    stage: str,
    result: str,
    detail: str,
    artifacts: list[str] | None = None,
) -> None:
    """Append a stamp and save."""
    stamp = {
        "stage": stage,
        "result": result,
        "detail": detail,
        "timestamp": _now_iso(),
    }
    if artifacts:
        stamp["artifacts"] = artifacts

    wp.setdefault("stamps", []).append(stamp)
    wp.setdefault("history", []).append({
        "event": "stamp",
        "stage": stage,
        "result": result,
        "timestamp": stamp["timestamp"],
    })
    save(wp, wp_path, f"stamp({stage}): {result}")


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

def escalate(
    wp: dict,
    wp_path: Path,
    *,
    summary: str,
    stage: str,
    stderr_excerpt: str = "",
) -> None:
    """Mark the work package as escalated."""
    wp["status"] = "escalated"
    wp["intervention"] = {
        "stage": stage,
        "summary": summary,
        "stderr_excerpt": stderr_excerpt,
        "timestamp": _now_iso(),
    }
    wp.setdefault("history", []).append({
        "event": "escalate",
        "stage": stage,
        "summary": summary,
        "timestamp": _now_iso(),
    })
    save(wp, wp_path, f"escalate({stage}): {summary[:72]}")
