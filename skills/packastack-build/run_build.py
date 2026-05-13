#!/usr/bin/env python3
"""Run packastack build in an ephemeral LXD container and emit structured context.

Pre-context script for the `packastack-build` skill. Spins up a fresh LXD
container per build, runs packastack inside it, pulls .deb artifacts to the
host at WORKPACKAGE_DIR/build-output/apt-repo/, destroys the container, then
emits two context blocks for the LLM:

  === package_identity ===   — who we're building
  === build_result ===       — status, stage reached, log tail, deb paths

Using a container per build means:
  - No schroot collisions between parallel package builds
  - No shared ~/.config/packastack between concurrent runs
  - Failed builds leave no residue on the host

Inputs (env):
  PACKAGE           e.g. "cinder"
  UBUNTU_SERIES     e.g. "noble"
  OPENSTACK_SERIES  e.g. "hibiscus"
  WORKPACKAGE_DIR   path to work-package repo; artifacts land under here

Output (stdout): key/value blocks consumed by the skill runner.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

PACKASTACK_REPO = "https://github.com/MylesJP/packastack.git"


def emit(block: str, **fields: str) -> None:
    print(f"=== {block} ===")
    for k, v in fields.items():
        # Escape newlines so each field stays on one line; receiver decodes \\n
        print(f"{k}: {str(v).replace(chr(92), chr(92)*2).replace(chr(10), r'\n')}")


def tail_text(text: str, n: int = 80) -> str:
    return "\n".join(text.splitlines()[-n:])


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


# ---------------------------------------------------------------------------
# LXD helpers
# ---------------------------------------------------------------------------

def _lxc(container: str, *cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a single command inside the container."""
    return subprocess.run(
        ["lxc", "exec", container, "--", *cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def _lxc_script(container: str, script: str, timeout: int = 3600) -> subprocess.CompletedProcess:
    """Run a multi-line bash script inside the container via stdin."""
    return subprocess.run(
        ["lxc", "exec", container, "--", "bash", "-s"],
        input=script, capture_output=True, text=True, timeout=timeout,
    )


def _launch_container(container: str, ubuntu_series: str) -> None:
    result = subprocess.run(
        ["lxc", "launch", f"ubuntu:{ubuntu_series}", container],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"lxc launch failed: {result.stderr.strip()}")
    print(f"[run_build] Container {container} launched")


def _setup_container(container: str, ubuntu_series: str) -> None:
    """Install build deps and pre-create the sbuild chroot inside the container."""
    print(f"[run_build] Setting up container {container} ...")
    script = f"""
set -e
cloud-init status --wait 2>/dev/null || true
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \\
    git git-buildpackage sbuild schroot debootstrap \\
    debhelper dh-python dh-apache2 \\
    openstack-pkg-tools python3-sphinx python3-pbr \\
    ubuntu-dev-tools devscripts build-essential

snap install astral-uv --classic

git config --global user.email "packastack@build.local"
git config --global user.name "Packastack Build"

# subuid/subgid needed by sbuild for user namespace chroot sessions
grep -q '^root:' /etc/subuid || echo "root:100000:65536" >> /etc/subuid
grep -q '^root:' /etc/subgid || echo "root:100000:65536" >> /etc/subgid

# pyversions stub — removed in noble, still referenced by some packages
cat > /usr/local/bin/pyversions << 'STUB'
#!/bin/sh
echo python3
STUB
chmod +x /usr/local/bin/pyversions

# Pre-create the sbuild schroot using plain debootstrap while DNS is known good.
# sbuild-createchroot (called later by packastack) runs debootstrap inside a
# new namespace where DNS fails inside LXD containers — so we do it here first.
CHROOT_DIR="/var/lib/schroot/chroots/packastack-{ubuntu_series}-amd64"
SCHROOT_CFG="/etc/schroot/chroot.d/{ubuntu_series}-amd64-packastack"
if [ ! -d "$CHROOT_DIR" ]; then
    echo "[setup] Pre-creating sbuild chroot for {ubuntu_series} ..."
    mkdir -p "$CHROOT_DIR"
    debootstrap --arch=amd64 --variant=buildd \\
        --include=fakeroot,build-essential \\
        --components=main,universe \\
        {ubuntu_series} "$CHROOT_DIR" \\
        http://archive.ubuntu.com/ubuntu
    # Ensure DNS works inside the sbuild chroot session.
    # Noble's /etc/resolv.conf is a symlink to 127.0.0.53 (stub) — remove it.
    # Noble's nsswitch.conf uses 'resolve' (systemd-resolved socket) which is
    # not mounted inside the sbuild session — override to plain 'dns'.
    GW=$(ip route show default | awk '/via/{{print $3; exit}}')
    rm -f "$CHROOT_DIR/etc/resolv.conf"
    echo "nameserver ${{GW:-8.8.8.8}}" > "$CHROOT_DIR/etc/resolv.conf"
    sed -i 's/^hosts:.*/hosts: files dns/' "$CHROOT_DIR/etc/nsswitch.conf" 2>/dev/null || \
        echo "hosts: files dns" >> "$CHROOT_DIR/etc/nsswitch.conf"
    # Prevent the sbuild schroot profile from overwriting our resolv.conf.
    sed -i '/resolv/d' /etc/schroot/sbuild/copyfiles 2>/dev/null || true
    cat > "$SCHROOT_CFG" << CFG
[{ubuntu_series}-amd64-packastack]
description=Sbuild chroot {ubuntu_series}/amd64 (pre-created)
aliases=packastack-{ubuntu_series}-amd64
type=directory
directory=$CHROOT_DIR
groups=root,sbuild
root-groups=root,sbuild
profile=sbuild
union-type=overlay
CFG
    echo "[setup] Schroot {ubuntu_series}-amd64-packastack ready."
fi
"""
    result = _lxc_script(container, script, timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"Container setup failed:\n{result.stderr[-2000:]}")
    print(f"[run_build] Container setup complete")


def _run_packastack(container: str, package: str, ubuntu_series: str, openstack_series: str = "") -> tuple[int, str]:
    """Clone packastack and run the build. Returns (exit_code, combined_output)."""
    target_flag = f"--target {openstack_series}" if openstack_series else ""
    script = f"""
set -e
export UV_PROJECT_ENVIRONMENT=/root/.venv-packastack

if [ ! -d /root/packastack ]; then
    git clone {PACKASTACK_REPO} /root/packastack
fi

# Redirect all packastack output paths into /build-output inside the container
mkdir -p /root/.config/packastack
cat > /root/.config/packastack/config.yaml << 'CFG'
paths:
  cache_root: /build-output/cache
  openstack_releases_repo: /build-output/cache/openstack-releases
  openstack_project_config: /build-output/cache/openstack-project-config
  ubuntu_archive_cache: /build-output/cache/ubuntu-archive
  local_apt_repo: /build-output/apt-repo
  upstream_tarballs: /build-output/cache/upstream-tarballs
  build_root: /build-output/build
CFG
mkdir -p /build-output/apt-repo

cd /root/packastack
uv run packastack init
uv run packastack build {package} --ubuntu-series {ubuntu_series} {target_flag} --archive-deps
"""
    result = _lxc_script(container, script, timeout=3600)
    combined = result.stdout + result.stderr
    return result.returncode, combined


def _pull_debs(container: str, host_apt_repo: Path) -> list[str]:
    """Pull all .deb files from /build-output/apt-repo in the container to host."""
    host_apt_repo.mkdir(parents=True, exist_ok=True)
    r = _lxc(container, "find", "/build-output/apt-repo", "-name", "*.deb", "-type", "f")
    container_paths = [p.strip() for p in r.stdout.splitlines() if p.strip()]
    pulled = []
    for cp in container_paths:
        filename = Path(cp).name
        host_path = host_apt_repo / filename
        subprocess.run(
            ["lxc", "file", "pull", f"{container}{cp}", str(host_path)],
            check=True, capture_output=True,
        )
        pulled.append(str(host_path))
    return pulled


def _pull_sbuild_log(container: str, package: str) -> str:
    """Pull the last 80 lines of the most recent sbuild log from the container."""
    r = _lxc(container, "find", f"/build-output/build/{package}",
              "-name", "sbuild.stdout.log", "-type", "f")
    logs = sorted(p.strip() for p in r.stdout.splitlines() if p.strip())
    if not logs:
        return "(no sbuild log found)"
    r = _lxc(container, "tail", "-n", "80", logs[-1])
    return r.stdout


def _destroy_container(container: str) -> None:
    subprocess.run(["lxc", "delete", "--force", container],
                   capture_output=True, check=False)
    print(f"[run_build] Container {container} destroyed")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    package = os.environ.get("PACKAGE", "").strip()
    ubuntu_series = os.environ.get("UBUNTU_SERIES", "noble").strip()
    openstack_series = os.environ.get("OPENSTACK_SERIES", "").strip()
    workpackage_dir = os.environ.get("WORKPACKAGE_DIR", "").strip()

    if not package:
        emit("build_result", status="error", error="PACKAGE env var not set")
        return 2

    # Artifacts land at WORKPACKAGE_DIR/build-output/apt-repo/ on the host
    if workpackage_dir:
        apt_repo = Path(workpackage_dir) / "build-output" / "apt-repo"
    else:
        apt_repo = Path.home() / "o7k-build-output" / "apt-repo"

    emit(
        "package_identity",
        package=package,
        ubuntu_series=ubuntu_series,
        openstack_series=openstack_series or "unknown",
        apt_repo=str(apt_repo),
    )

    container = f"o7k-build-{package}-{uuid.uuid4().hex[:8]}"
    try:
        _launch_container(container, ubuntu_series)

        try:
            _setup_container(container, ubuntu_series)
        except RuntimeError as e:
            emit("build_result", status="error", stage="setup", error=str(e))
            return 1

        exit_code, combined = _run_packastack(container, package, ubuntu_series, openstack_series)
        sbuild_log_tail = _pull_sbuild_log(container, package)

        if exit_code == 0:
            debs = _pull_debs(container, apt_repo)
            emit(
                "build_result",
                status="success",
                stage="complete",
                exit_code="0",
                deb_paths=" ".join(debs) if debs else "none found",
                apt_repo=str(apt_repo),
                sbuild_log_tail=sbuild_log_tail,
            )
            return 0

        if "Source package built successfully" in combined:
            stage = "sbuild"
        elif "Source build failed" in combined:
            stage = "source-build"
        else:
            stage = "unknown"

        failure_class = classify_failure(sbuild_log_tail + combined)
        emit(
            "build_result",
            status="failed",
            stage=stage,
            exit_code=str(exit_code),
            failure_class=failure_class,
            sbuild_log_tail=sbuild_log_tail,
            packastack_output_tail=tail_text(combined, 30),
        )
        return 1

    finally:
        _destroy_container(container)


if __name__ == "__main__":
    sys.exit(main())
