"""Packaging Manager.

Orchestrates the full packaging pipeline for one project:
  1. Run the detector skill → parse STATE
  2. If NEW_RELEASE: create a Work Package, advance to packaging
  3. Run packastack-build skill (real build + LLM diagnosis) → stamp
  4. Advance to QA → run run-autopkgtest skill → stamp
  5. Mark WP done

This is the only component that updates current_stage / current_owner.
Skills never do that. The expediter never does that.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

# Make sure the repo root is importable when run as a script
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from o7k import skills, workpackage  # noqa: E402

PROJECTS_PATH = REPO_ROOT / "resources" / "projects.yaml"
LOG_WIDTH = 78

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _field(text: str, key: str) -> str:
    """Extract a single-line field from structured skill output."""
    m = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _log(message: str = "") -> None:
    print(message, flush=True)


def _section(title: str, detail: str = "") -> None:
    _log()
    _log("=" * LOG_WIDTH)
    line = f"[manager] {title.upper()}"
    if detail:
        line += f" | {detail}"
    _log(line)
    _log("-" * LOG_WIDTH)


def _info(label: str, value: str | Path) -> None:
    _log(f"  {label:<14} {value}")


def _ok(message: str) -> None:
    _log(f"[manager] OK    {message}")


def _warn(message: str) -> None:
    _log(f"[manager] WARN  {message}")


def _error(message: str) -> None:
    _log(f"[manager] ERROR {message}")


def _response(title: str, text: str) -> None:
    _log()
    _log(f"[{title}]")
    body = text.strip()
    if not body:
        _log("  (empty response)")
        return
    for line in body.splitlines():
        _log(f"  {line}" if line else "")


# ---------------------------------------------------------------------------
# Stage handlers
# ---------------------------------------------------------------------------

def _run_detection(project: dict) -> tuple[str, str, str, str]:
    """Run the detector skill. Returns (state, upstream_version, source_url, explanation)."""
    package = project["package"]
    openstack_series = project.get("openstack_series", "hibiscus")
    ubuntu_series = project.get("ubuntu_series", "noble")

    _section("detection", f"{package} ({openstack_series}/{ubuntu_series})")
    env = {
        "PACKAGE": package,
        "OPENSTACK_SERIES": openstack_series,
        "UBUNTU_SERIES": ubuntu_series,
    }

    response = skills.run("detector", env)
    _response("detector response", response)

    state = _field(response, "STATE")
    upstream_version = _field(response, "UPSTREAM_VERSION")
    source_url = _field(response, "SOURCE_URL")
    explanation = _field(response, "EXPLANATION")
    return state, upstream_version, source_url, explanation


def _build_env(wp: dict, wp_path: Path) -> dict[str, str]:
    """Build the env dict from the workpackage target block."""
    target = wp["target"]
    return {
        "PACKAGE": target["upstream_project"],
        "OPENSTACK_SERIES": target.get("openstack_series", "hibiscus"),
        "UBUNTU_SERIES": target.get("ubuntu_release", "noble"),
        "WORKPACKAGE_DIR": str(wp_path.parent),
    }


def _run_packaging(wp: dict, wp_path: Path) -> bool:
    """Run the packastack-build skill. Returns True on success."""
    package = wp["target"]["upstream_project"]
    version = wp["target"]["upstream_version"]

    _section("packaging", f"{package} {version}")

    env = _build_env(wp, wp_path)
    _info("workpackage", wp_path.parent)
    _info("series", f"{env['OPENSTACK_SERIES']} / {env['UBUNTU_SERIES']}")
    response = skills.run("packastack-build", env)
    _response("packastack-build response", response)

    status = _field(response, "STATUS")
    explanation = _field(response, "EXPLANATION")

    if status == "SUCCESS":
        deb_paths = _field(response, "DEB_PATHS")
        apt_repo = _field(response, "APT_REPO")
        artifacts = deb_paths.split() if deb_paths else []
        workpackage.add_stamp(
            wp, wp_path,
            stage="packaging",
            result="verified",
            detail=f"packastack build succeeded: {explanation}",
            artifacts=artifacts,
        )
        # Stash build outputs in the wp for the QA stage
        wp["_build_output"] = {
            "deb_paths": deb_paths,
            "apt_repo": apt_repo,
        }
        _ok(f"packastack build verified ({len(artifacts)} artifact(s))")
        return True

    # Build failed — record the failure details
    failure_class = _field(response, "FAILURE_CLASS")
    diagnosis = _field(response, "DIAGNOSIS")
    patch_filename = _field(response, "PATCH_FILENAME")

    detail = (
        f"build failed: failure_class={failure_class}, "
        f"diagnosis={diagnosis}, patch={patch_filename}"
    )

    if patch_filename and patch_filename != "NO_PATCH":
        # Skill proposed a patch — for now we log it, future: apply + retry
        _warn(f"skill proposed patch: {patch_filename}")
        workpackage.add_stamp(
            wp, wp_path,
            stage="packaging",
            result="rejected",
            detail=detail,
        )
    else:
        workpackage.escalate(
            wp, wp_path,
            summary=detail,
            stage="packaging",
            stderr_excerpt=diagnosis,
        )

    _error(f"packaging did not pass ({failure_class or 'unknown failure'})")
    return False


def _run_qa(wp: dict, wp_path: Path) -> bool:
    """Run the run-autopkgtest skill. Returns True on pass."""
    _section("qa")

    env = _build_env(wp, wp_path)
    # Pass DEB_DIR if we know it from the build stage
    build_output = wp.get("_build_output", {})
    if build_output.get("apt_repo"):
        env["DEB_DIR"] = build_output["apt_repo"]
        _info("apt repo", env["DEB_DIR"])

    response = skills.run("run-autopkgtest", env)
    _response("run-autopkgtest response", response)

    result = _field(response, "AUTOPKGTEST_RESULT")
    explanation = _field(response, "EXPLANATION")
    tests_total = _field(response, "TESTS_TOTAL")
    tests_passed = _field(response, "TESTS_PASSED")
    tests_failed = _field(response, "TESTS_FAILED")

    detail = (
        f"autopkgtest {result}: {tests_passed}/{tests_total} passed, "
        f"{tests_failed} failed. {explanation}"
    )

    if result == "PASS":
        workpackage.add_stamp(
            wp, wp_path,
            stage="qa",
            result="verified",
            detail=detail,
        )
        _ok(f"autopkgtest passed ({tests_passed}/{tests_total})")
        return True
    elif result == "SKIP":
        workpackage.add_stamp(
            wp, wp_path,
            stage="qa",
            result="verified_with_warnings",
            detail=detail,
        )
        _warn(f"autopkgtest skipped ({tests_skipped}/{tests_total})")
        return True
    else:
        workpackage.escalate(
            wp, wp_path,
            summary=f"QA failed: {detail}",
            stage="qa",
        )
        _error(f"autopkgtest did not pass ({result or 'unknown result'})")
        return False


# ---------------------------------------------------------------------------
# Advance stage helper
# ---------------------------------------------------------------------------

def _advance(wp: dict, wp_path: Path, new_stage: str, owner: str = "manager") -> dict:
    wp["current_stage"] = new_stage
    wp["current_owner"] = owner
    workpackage.save(wp, wp_path, f"advance(stage): {new_stage}")
    _ok(f"advanced to stage={new_stage}")
    return workpackage.load(wp_path)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(package_name: str | None = None) -> None:
    """Run the full packaging pipeline for one project from projects.yaml."""
    projects_data = yaml.safe_load(PROJECTS_PATH.read_text())
    projects = projects_data.get("projects", [])

    if package_name:
        projects = [p for p in projects if p["package"] == package_name]
    if not projects:
        _error(f"no project found for {package_name!r}")
        sys.exit(1)

    project = projects[0]
    package = project["package"]

    # ── Stage 1: Detection ──────────────────────────────────────────────────
    state, upstream_version, source_url, explanation = _run_detection(project)

    if state not in ("NEW_RELEASE", "NEW_PRERELEASE"):
        if state == "NO_RELEASES":
            _warn(f"STATE=NO_RELEASES; no upstream releases yet. {explanation}")
        else:
            _warn(f"STATE={state}; nothing to do. {explanation}")
        return

    _ok(f"{state} detected: {package} {upstream_version}")

    # Create Work Package
    wp_path = workpackage.create_wp(
        package,
        upstream_version,
        source_url,
        openstack_series=project.get("openstack_series", "hibiscus"),
        ubuntu_release=project.get("ubuntu_series", "noble"),
        uca_pocket=project.get("uca_pocket", project.get("openstack_series", "hibiscus")),
    )
    _info("workpackage", wp_path)
    wp = workpackage.load(wp_path)

    # Write detection stamp
    workpackage.add_stamp(
        wp, wp_path,
        stage="detection",
        result="verified",
        detail=f"detector state={state}, upstream={upstream_version}",
    )
    wp = workpackage.load(wp_path)

    # Advance to packaging
    wp = _advance(wp, wp_path, "packaging")

    # ── Stage 2: Packaging (packastack-build skill) ─────────────────────────
    if not _run_packaging(wp, wp_path):
        _error("packaging failed; workpackage escalated")
        return

    wp = workpackage.load(wp_path)
    wp = _advance(wp, wp_path, "qa")

    # ── Stage 3: QA (run-autopkgtest skill) ─────────────────────────────────
    if not _run_qa(wp, wp_path):
        _error("QA failed; workpackage escalated")
        return

    wp = workpackage.load(wp_path)

    # ── Done ─────────────────────────────────────────────────────────────────
    wp["status"] = "done"
    wp["current_stage"] = "done"
    wp["current_owner"] = "none"
    workpackage.save(wp, wp_path, "done: pipeline completed successfully")
    _section("complete", package)
    _ok("pipeline complete; status=done")
    _info("workpackage", wp_path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="o7k packaging manager")
    parser.add_argument("package", nargs="?", default="cinder",
                        help="Package name from resources/projects.yaml (default: cinder)")
    args = parser.parse_args()

    run(package_name=args.package)
