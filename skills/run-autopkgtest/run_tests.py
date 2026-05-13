#!/usr/bin/env python3
"""Run autopkgtest against locally built debs and emit structured context.

Pre-context script for the `run-autopkgtest` skill. Reads PACKAGE,
UBUNTU_SERIES, and WORKPACKAGE_DIR from the environment, locates the
.deb files from the preceding build stamp, and runs autopkgtest.

Inputs (env):
  PACKAGE           e.g. "cinder"
  UBUNTU_SERIES     e.g. "noble"
  WORKPACKAGE_DIR   path to work-package repo (locates artifact dirs)
  DEB_DIR           optional override: explicit path to dir containing debs

Output (stdout):
  === package_identity ===
  === autopkgtest_result ===
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def emit(block: str, **fields: str) -> None:
    print(f"=== {block} ===")
    for k, v in fields.items():
        # Escape newlines so each field stays on one line; receiver decodes \\n
        print(f"{k}: {str(v).replace(chr(92), chr(92)*2).replace(chr(10), r'\n')}")


def tail(path: Path, n: int = 60) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return "(log not found)"


def parse_autopkgtest_output(output: str) -> dict:
    """Parse autopkgtest stdout into per-test results."""
    results: dict[str, str] = {}
    for line in output.splitlines():
        # autopkgtest lines: "nova/nova-tests PASS", "nova/nova-tests FAIL"
        m = re.match(r"^(\S+)\s+(PASS|FAIL|SKIP|ERROR)\s*$", line.strip())
        if m:
            results[m.group(1)] = m.group(2)
    return results


def newest(path: Path, pattern: str) -> Path | None:
    matches = sorted(path.rglob(pattern), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


def main() -> int:
    package = os.environ.get("PACKAGE", "").strip()
    ubuntu_series = os.environ.get("UBUNTU_SERIES", "noble").strip()
    workpackage_dir = os.environ.get("WORKPACKAGE_DIR", "").strip()
    deb_dir_override = os.environ.get("DEB_DIR", "").strip()
    testsrc_override = os.environ.get("TESTSRC", "").strip()

    if not package:
        emit("autopkgtest_result", status="error", error="PACKAGE env var not set")
        return 2
    if not workpackage_dir:
        emit("autopkgtest_result", status="error", error="WORKPACKAGE_DIR env var not set")
        return 2

    emit(
        "package_identity",
        package=package,
        ubuntu_series=ubuntu_series,
    )

    workpackage = Path(workpackage_dir)
    packastack_artifacts = workpackage / "artifacts" / "packastack"

    # Locate debs
    if deb_dir_override:
        deb_dir = Path(deb_dir_override)
    else:
        deb_dir = packastack_artifacts / "apt-repo"

    debs = list(deb_dir.rglob("*.deb")) if deb_dir.exists() else []
    if not debs:
        emit(
            "autopkgtest_result",
            status="error",
            error=f"no .deb files found in {deb_dir}",
        )
        return 1

    if testsrc_override:
        testsrc = Path(testsrc_override)
    else:
        # Prefer Packastack's complete build-output bundle. The apt repo pool
        # may omit companion files referenced by the .dsc, such as .asc files.
        testsrc = newest(packastack_artifacts / "build", f"{package}_*.dsc")
        if testsrc is None:
            testsrc = newest(deb_dir, f"{package}_*.dsc") if deb_dir.exists() else None
    if not testsrc:
        emit(
            "autopkgtest_result",
            status="error",
            error=f"no source package found in {deb_dir}; set TESTSRC to a source tree or .dsc",
        )
        return 1

    if not shutil.which("autopkgtest"):
        emit(
            "autopkgtest_result",
            status="error",
            error="autopkgtest not installed; run: sudo apt-get install autopkgtest",
        )
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    results_dir = workpackage / "artifacts" / "autopkgtest" / stamp
    results_dir.mkdir(parents=True, exist_ok=True)

    deb_args = [str(d) for d in sorted(debs)]
    cmd = deb_args + [str(testsrc), "-o", str(results_dir), "--", "lxd", f"ubuntu:{ubuntu_series}"]

    try:
        result = subprocess.run(
            ["autopkgtest"] + cmd,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        emit("autopkgtest_result", status="error", error="autopkgtest timed out after 1800s")
        return 1
    except FileNotFoundError:
        emit("autopkgtest_result", status="error", error="autopkgtest binary not found")
        return 1

    combined = result.stdout + result.stderr
    test_results = parse_autopkgtest_output(combined)

    passed = [t for t, r in test_results.items() if r == "PASS"]
    failed = [t for t, r in test_results.items() if r == "FAIL"]
    skipped = [t for t, r in test_results.items() if r == "SKIP"]

    overall = "PASS" if result.returncode == 0 else ("ERROR" if not test_results else "FAIL")

    fields: dict[str, str] = dict(
        status=overall,
        exit_code=str(result.returncode),
        tests_total=str(len(test_results)),
        tests_passed=str(len(passed)),
        tests_failed=str(len(failed)),
        tests_skipped=str(len(skipped)),
        results_dir=str(results_dir),
        debs_used=" ".join(deb_args),
    )

    # Attach log tails for each failed test
    for test_name in failed:
        safe = re.sub(r"[^\w-]", "_", test_name)
        log_path = results_dir / test_name / "testout"
        fields[f"failed_test_{safe}_log"] = tail(log_path, 60)

    # If overall ERROR, attach combined output
    if overall == "ERROR":
        fields["autopkgtest_stderr"] = "\n".join(combined.splitlines()[-40:])

    emit("autopkgtest_result", **fields)
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
