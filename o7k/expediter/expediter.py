"""o7k Expediter — deterministic pipeline orchestrator.

Loads resources/projects.yaml and runs the packaging manager for each
project. Runs up to MAX_WORKERS projects in parallel — each project gets
its own workpackage, its own LXD build container, and makes independent
LLM calls, so there is no shared state between runs.

The expediter itself never calls an LLM (AGENTS.md invariant 1). It only
reads projects.yaml, dispatches managers, and collects their outcomes.

Usage:
    python -m o7k.expediter                     # all projects
    python -m o7k.expediter cinder              # one project
    python -m o7k.expediter cinder nova glance  # subset
"""

from __future__ import annotations

import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Max parallel pipeline runs. Keeps LXD resource usage and OpenRouter
# rate-limit pressure manageable.
MAX_WORKERS = 4

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PROJECTS_PATH = _REPO_ROOT / "resources" / "projects.yaml"


@dataclass
class RunResult:
    package: str
    success: bool
    error: str = ""
    traceback: str = ""


def _load_projects(names: list[str] | None = None) -> list[dict]:
    """Load projects from projects.yaml, optionally filtered by name."""
    data = yaml.safe_load(_PROJECTS_PATH.read_text())
    projects = data.get("projects", [])
    if names:
        projects = [p for p in projects if p["package"] in names]
        missing = set(names) - {p["package"] for p in projects}
        if missing:
            print(f"[expediter] WARNING: unknown packages: {', '.join(sorted(missing))}")
    return projects


def _run_one(project: dict) -> RunResult:
    """Run the full packaging pipeline for a single project."""
    package = project["package"]
    # Import here so each thread gets a clean call — manager is stateless
    from managers.packaging import run as manager_run  # noqa: PLC0415
    try:
        manager_run(package_name=package)
        return RunResult(package=package, success=True)
    except SystemExit as e:
        # managers.packaging calls sys.exit(1) on fatal errors
        return RunResult(
            package=package,
            success=False,
            error=f"manager exited with code {e.code}",
        )
    except Exception as e:  # noqa: BLE001
        return RunResult(
            package=package,
            success=False,
            error=str(e),
            traceback=traceback.format_exc(),
        )


def run(package_names: list[str] | None = None) -> int:
    """Run the expediter. Returns 0 if all projects succeeded, 1 otherwise.

    Args:
        package_names: optional list of package names to run. If None or
                       empty, runs all projects from projects.yaml.
    """
    projects = _load_projects(package_names or [])
    if not projects:
        print("[expediter] No projects to run.")
        return 1

    count = len(projects)
    workers = min(MAX_WORKERS, count)
    print(f"[expediter] Starting {count} project(s) with up to {workers} parallel workers")
    print(f"[expediter] Projects: {', '.join(p['package'] for p in projects)}\n")

    results: list[RunResult] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_one, p): p["package"] for p in projects}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            status = "✓" if result.success else "✗"
            print(f"[expediter] {status} {result.package} {'done' if result.success else 'FAILED: ' + result.error}")

    # Summary
    passed = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    print(f"\n[expediter] ── Summary ──────────────────────────")
    print(f"[expediter] Passed : {len(passed)}/{count}")
    print(f"[expediter] Failed : {len(failed)}/{count}")
    for r in failed:
        print(f"[expediter]   ✗ {r.package}: {r.error}")
        if r.traceback:
            print(f"[expediter]     {r.traceback.splitlines()[-1]}")

    return 0 if not failed else 1
