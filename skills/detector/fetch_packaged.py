#!/usr/bin/env python3
"""Fetch the currently packaged version of an OpenStack package on Launchpad.

Pre-context script for the `detector` skill. Reads PACKAGE from the
environment, fetches the top entry of `debian/changelog` from the master branch
of `~ubuntu-openstack-dev/+git/<package>`, and prints a structured block for
inclusion in the LLM prompt as the `packaged_release` context block.

The upstream version reported here strips any Debian revision suffix
(`-0ubuntu1`, `~cloud0`, etc.) so the LLM can compare apples-to-apples with
the upstream value.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

CHANGELOG_URL = (
    "https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/{package}/"
    "plain/debian/changelog"
)
GIT_URL = "https://git.launchpad.net/~ubuntu-openstack-dev/ubuntu/+source/{package}"

# Standard Debian changelog top line: "<pkg> (<version>) <suite>; urgency=<u>"
TOP_LINE = re.compile(r"^([^\s]+)\s+\(([^)]+)\)\s+(\S+);")


def emit(**fields: str) -> None:
    print("=== packaged_release ===")
    for k, v in fields.items():
        print(f"{k}: {v}")


def strip_revision(version: str) -> str:
    """Remove Debian revision and Ubuntu Cloud Archive suffixes."""
    v = version.split("-", 1)[0]
    v = v.split("~", 1)[0]
    if ":" in v:
        v = v.split(":", 1)[1]
    return v


def fetch_changelog_from_git(package: str) -> str | None:
    with tempfile.TemporaryDirectory(prefix=f"o7k-{package}-") as tmp:
        repo = Path(tmp) / "repo"
        result = subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                GIT_URL.format(package=package),
                str(repo),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return None
        try:
            return (repo / "debian" / "changelog").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return None


def main() -> int:
    package = os.environ.get("PACKAGE", "").strip()
    if not package:
        emit(error="missing PACKAGE env var")
        return 2

    url = CHANGELOG_URL.format(package=package)
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        text = fetch_changelog_from_git(package)
        if text is None:
            emit(
                package=package,
                source_url=url,
                version="unknown",
                error=f"http {e.code}",
            )
            return 0
    except (urllib.error.URLError, TimeoutError) as e:
        emit(package=package, source_url=url, version="unknown", error=f"network: {e}")
        return 0

    for line in text.splitlines():
        m = TOP_LINE.match(line)
        if m:
            full_version = m.group(2)
            emit(
                package=package,
                source_url=url,
                version=strip_revision(full_version),
                full_version=full_version,
                suite=m.group(3),
            )
            return 0

    emit(
        package=package,
        source_url=url,
        version="unknown",
        error="no top changelog entry found",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
