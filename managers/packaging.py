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

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _field(text: str, key: str) -> str:
    """Extract a single-line field from structured skill output."""
    m = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Stage handlers
# ---------------------------------------------------------------------------

def _run_detection(project: dict) -> tuple[str, str, str, str]:
    """Run the detector skill. Returns (state, upstream_version, source_url, explanation)."""
    package = project["package"]
    openstack_series = project.get("openstack_series", "hibiscus")
    ubuntu_series = project.get("ubuntu_series", "noble")

    print(f"\n[manager] ── DETECTION ── {package} ({openstack_series}/{ubuntu_series})")
    env = {
        "PACKAGE": package,
        "OPENSTACK_SERIES": openstack_series,
        "UBUNTU_SERIES": ubuntu_series,
    }

    response = skills.run("detector", env)
    print("[detector response]\n" + response)

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

    print(f"\n[manager] ── PACKAGING ── {package} {version}")

    env = _build_env(wp, wp_path)
    response = skills.run("packastack-build", env)
    print("[packastack-build response]\n" + response)

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
        print(f"[manager] Skill proposed patch: {patch_filename}")
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

    return False


def _run_qa(wp: dict, wp_path: Path) -> bool:
    """Run the run-autopkgtest skill. Returns True on pass."""
    print(f"\n[manager] ── QA ──")

    env = _build_env(wp, wp_path)
    # Pass DEB_DIR if we know it from the build stage
    build_output = wp.get("_build_output", {})
    if build_output.get("apt_repo"):
        env["DEB_DIR"] = build_output["apt_repo"]

    response = skills.run("run-autopkgtest", env)
    print("[run-autopkgtest response]\n" + response)

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
        return True
    elif result == "SKIP":
        workpackage.add_stamp(
            wp, wp_path,
            stage="qa",
            result="verified_with_warnings",
            detail=detail,
        )
        return True
    else:
        workpackage.escalate(
            wp, wp_path,
            summary=f"QA failed: {detail}",
            stage="qa",
        )
        return False


# ---------------------------------------------------------------------------
# Advance stage helper
# ---------------------------------------------------------------------------

def _advance(wp: dict, wp_path: Path, new_stage: str, owner: str = "manager") -> dict:
    wp["current_stage"] = new_stage
    wp["current_owner"] = owner
    workpackage.save(wp, wp_path, f"advance(stage): {new_stage}")
    print(f"[manager] ✓ advanced → current_stage={new_stage}")
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
        print(f"[manager] No project found for {package_name!r}")
        sys.exit(1)

    project = projects[0]
    package = project["package"]

    # ── Stage 1: Detection ──────────────────────────────────────────────────
    state, upstream_version, source_url, explanation = _run_detection(project)

    if state not in ("NEW_RELEASE", "NEW_PRERELEASE"):
        if state == "NO_RELEASES":
            print(f"\n[manager] STATE=NO_RELEASES — no upstream releases yet for this series. {explanation}")
        else:
            print(f"\n[manager] STATE={state} — nothing to do. {explanation}")
        return

    print(f"\n[manager] ✓ {state} detected: {package} {upstream_version}")

    # Create Work Package
    wp_path = workpackage.create_wp(
        package,
        upstream_version,
        source_url,
        openstack_series=project.get("openstack_series", "hibiscus"),
        ubuntu_release=project.get("ubuntu_series", "noble"),
        uca_pocket=project.get("uca_pocket", project.get("openstack_series", "hibiscus")),
    )
    print(f"[manager] Work Package created: {wp_path.name}")
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
        print("\n[manager] ✗ Packaging failed — escalated")
        return

    wp = workpackage.load(wp_path)
    wp = _advance(wp, wp_path, "qa")

    # ── Stage 3: QA (run-autopkgtest skill) ─────────────────────────────────
    if not _run_qa(wp, wp_path):
        print("\n[manager] ✗ QA failed — escalated")
        return

    wp = workpackage.load(wp_path)

    # ── Done ─────────────────────────────────────────────────────────────────
    wp["status"] = "done"
    wp["current_stage"] = "done"
    wp["current_owner"] = "none"
    workpackage.save(wp, wp_path, "done: pipeline completed successfully")
    print(f"\n[manager] ✓ Pipeline complete — status=done")
    print(f"[manager] Work Package: {wp_path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="o7k packaging manager")
    parser.add_argument("package", nargs="?", default="cinder",
                        help="Package name from resources/projects.yaml (default: cinder)")
    args = parser.parse_args()

    run(package_name=args.package)
