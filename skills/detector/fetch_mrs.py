#!/usr/bin/env python3
"""Fetch open merge proposals against the Ubuntu OpenStack packaging repo.

Pre-context script for the `detector` skill. Reads PACKAGE from the
environment, queries the Launchpad API for open merge proposals against
`~ubuntu-openstack-dev/+git/<package>`, and for each one tries to read the
top `debian/changelog` version from the source branch.

The Launchpad API returns JSON without auth for public projects. We avoid the
`launchpadlib` dependency and just use urllib.

Output (stdout): one block per proposal under `=== in_flight_proposals ===`,
or `count: 0` if none are open.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

LP_API_BASE = "https://api.launchpad.net/devel"
TARGET_REPO_PATH = "/~ubuntu-openstack-dev/+git/{package}"

# Pattern shared with fetch_packaged.py — kept inline rather than imported to
# keep each pre-context script independently runnable.
TOP_LINE = re.compile(r"^([^\s]+)\s+\(([^)]+)\)\s+(\S+);")


def strip_revision(version: str) -> str:
    v = version.split("-", 1)[0]
    v = v.split("~", 1)[0]
    if ":" in v:
        v = v.split(":", 1)[1]
    return v


def http_json(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def http_text(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError):
        return None


def changelog_version_from_branch(repo_url: str, branch: str) -> str | None:
    # Launchpad git web exposes branches as `/plain/<file>?h=<ref>`.
    url = f"{repo_url}/plain/debian/changelog?h={urllib.parse.quote(branch)}"
    text = http_text(url)
    if not text:
        return None
    for line in text.splitlines():
        m = TOP_LINE.match(line)
        if m:
            return strip_revision(m.group(2))
    return None


def main() -> int:
    package = os.environ.get("PACKAGE", "").strip()
    if not package:
        print("=== in_flight_proposals ===")
        print("error: missing PACKAGE env var")
        return 2

    target = TARGET_REPO_PATH.format(package=package)
    list_url = (
        f"{LP_API_BASE}{target}?ws.op=getMergeProposals&status=Needs+review"
    )
    data = http_json(list_url)

    print("=== in_flight_proposals ===")
    print(f"package: {package}")
    print(f"query_url: {list_url}")

    if data is None:
        print("error: launchpad api unreachable")
        print("count: unknown")
        return 0

    entries = data.get("entries") or []
    print(f"count: {len(entries)}")

    repo_git_url = f"https://git.launchpad.net{target}"
    for i, mp in enumerate(entries, 1):
        source_branch = mp.get("source_git_path") or ""
        # source_git_path looks like "refs/heads/<branch>"; strip the prefix.
        branch = source_branch.rsplit("/", 1)[-1] if source_branch else ""
        version = (
            changelog_version_from_branch(repo_git_url, branch)
            if branch
            else None
        )
        print(f"--- proposal {i} ---")
        print(f"web_link: {mp.get('web_link', 'unknown')}")
        print(f"status: {mp.get('queue_status', 'unknown')}")
        print(f"source_branch: {branch or 'unknown'}")
        print(f"source_version: {version or 'unknown'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
