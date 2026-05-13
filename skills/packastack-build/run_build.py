#!/usr/bin/env python3
"""Run packastack build on the host and emit structured context for the LLM.

Pre-context script for the `packastack-build` skill. Reads PACKAGE,
UBUNTU_SERIES, and WORKPACKAGE_DIR from the environment, runs
`packastack build`, and emits two context blocks:

  === package_identity ===   — who we're building
  === build_result ===       — status, stage reached, log tail, deb paths

Inputs (env):
  PACKAGE           e.g. "cinder"
  UBUNTU_SERIES     e.g. "noble"
  OPENSTACK_SERIES  e.g. "hibiscus"
  WORKPACKAGE_DIR   path to work-package repo (artifacts stored under here)

Output (stdout): key/value blocks consumed by the skill runner.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PACKASTACK_DIR = Path.home() / "packastack"


def emit(block: str, **fields: str) -> None:
    print(f"=== {block} ===")
    for k, v in fields.items():
        # Escape newlines so each field stays on one line; receiver decodes \\n
        print(f"{k}: {str(v).replace(chr(92), chr(92)*2).replace(chr(10), r'\n')}")


def tail(path: Path, n: int = 80) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return "(log not found)"


def find_debs(apt_repo: Path, package: str) -> list[str]:
    if not apt_repo.exists():
        return []
    python_binary = f"python3-{package.removeprefix('python-')}"
    return [
        str(p)
        for p in apt_repo.rglob("*.deb")
        if package in p.name or f"python3-{package}" in p.name or python_binary in p.name
    ]


def classify_failure(log_tail: str) -> str:
    checks = [
        (r"dpkg-checkbuilddeps.*Unmet build dependencies", "missing-build-dep"),
        (r"(gbp pq.*(fail|error)|quilt push.*(fail|error)|Hunk FAILED|\bpatch\b.*failed)", "patch-apply-fail"),
        (r"pybuild --clean.*returned exit code", "pybuild-clean-fail"),
        (r"Error creating chroot session", "sbuild-chroot-error"),
        (r"Failed to execute chroot-setup-commands", "sbuild-chroot-error"),
        (r"(make\[.*\].*Error|gcc.*error:|g\+\+.*error:)", "compile-error"),
    ]
    for pattern, cls in checks:
        if re.search(pattern, log_tail, re.IGNORECASE):
            return cls
    return "unknown"


def main() -> int:
    package = os.environ.get("PACKAGE", "").strip()
    ubuntu_series = os.environ.get("UBUNTU_SERIES", "noble").strip()
    openstack_series = os.environ.get("OPENSTACK_SERIES", "").strip()
    workpackage_dir = os.environ.get("WORKPACKAGE_DIR", "").strip()

    if not package:
        emit("build_result", status="error", error="PACKAGE env var not set")
        return 2

    if not workpackage_dir:
        emit("build_result", status="error", error="WORKPACKAGE_DIR env var not set")
        return 2

    workpackage = Path(workpackage_dir)
    output_dir = workpackage / "artifacts" / "packastack"
    tool_home = output_dir / "home"
    venv = output_dir / "venv"
    apt_repo = output_dir / "apt-repo"
    build_root = output_dir / "build"

    # Write packastack config pointing to our output dir
    cfg_dir = tool_home / ".config" / "packastack"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        f"""paths:
  cache_root: {output_dir}/cache
  openstack_releases_repo: {output_dir}/cache/openstack-releases
  openstack_project_config: {output_dir}/cache/openstack-project-config
  ubuntu_archive_cache: {output_dir}/cache/ubuntu-archive
  local_apt_repo: {apt_repo}
  upstream_tarballs: {output_dir}/cache/upstream-tarballs
  build_root: {build_root}
"""
    )

    emit(
        "package_identity",
        package=package,
        ubuntu_series=ubuntu_series,
        openstack_series=openstack_series or "unknown",
        output_dir=str(output_dir),
        apt_repo=str(apt_repo),
    )

    if not PACKASTACK_DIR.exists():
        emit(
            "build_result",
            status="error",
            stage="setup",
            error=f"packastack not found at {PACKASTACK_DIR}; clone it first",
        )
        return 1

    env = {
        **os.environ,
        "HOME": str(tool_home),
        "UV_PROJECT_ENVIRONMENT": str(venv),
        "GIT_AUTHOR_NAME": "Packastack Build",
        "GIT_AUTHOR_EMAIL": "packastack@build.local",
        "GIT_COMMITTER_NAME": "Packastack Build",
        "GIT_COMMITTER_EMAIL": "packastack@build.local",
    }

    try:
        init_result = subprocess.run(
            ["uv", "run", "packastack", "init"],
            cwd=str(PACKASTACK_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        emit("build_result", status="error", stage="setup",
             error="packastack init timed out after 1800s")
        return 1
    except FileNotFoundError:
        emit("build_result", status="error", stage="setup",
             error="uv not found; install via: snap install astral-uv --classic")
        return 1

    if init_result.returncode != 0:
        init_output = init_result.stdout + init_result.stderr
        emit(
            "build_result",
            status="error",
            stage="setup",
            error="packastack init failed",
            packastack_init_output_tail="\n".join(init_output.splitlines()[-30:]),
        )
        return 1

    cmd = [
        "uv", "run", "packastack", "build", package,
        "--ubuntu-series", ubuntu_series,
    ]
    if openstack_series:
        cmd.extend(["--target", openstack_series])
    cmd.append("--archive-deps")

    try:
        result = subprocess.run(
            cmd,
            cwd=str(PACKASTACK_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except subprocess.TimeoutExpired:
        emit("build_result", status="error", stage="timeout",
             error="build timed out after 3600s")
        return 1
    except FileNotFoundError:
        emit("build_result", status="error", stage="setup",
             error="uv not found; install via: snap install astral-uv --classic")
        return 1

    combined_output = result.stdout + result.stderr

    # Find the most recent sbuild log
    sbuild_log_tail = "(no sbuild log found)"
    if build_root.exists():
        logs = sorted(build_root.glob(f"{package}/*/logs/sbuild.stdout.log"))
        if logs:
            sbuild_log_tail = tail(logs[-1], 80)

    if result.returncode == 0:
        debs = find_debs(apt_repo, package)
        emit(
            "build_result",
            status="success",
            stage="complete",
            exit_code=str(result.returncode),
            deb_paths=" ".join(debs) if debs else "none found",
            apt_repo=str(apt_repo),
            sbuild_log_tail=sbuild_log_tail,
        )
        return 0

    # Determine which stage failed
    if "Source package built successfully" in combined_output:
        stage = "sbuild"
    elif "Source build failed" in combined_output:
        stage = "source-build"
    else:
        stage = "unknown"

    failure_class = classify_failure(sbuild_log_tail + combined_output)

    emit(
        "build_result",
        status="failed",
        stage=stage,
        exit_code=str(result.returncode),
        failure_class=failure_class,
        sbuild_log_tail=sbuild_log_tail,
        packastack_output_tail="\n".join(combined_output.splitlines()[-30:]),
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
