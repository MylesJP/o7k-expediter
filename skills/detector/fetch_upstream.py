#!/usr/bin/env python3
"""Fetch the latest upstream release for a tracked OpenStack package.

Pre-context script for the `detector` skill. Reads PACKAGE and OPENSTACK_SERIES
from the environment, fetches the deliverable YAML from the OpenStack releases
repo, and prints a small structured block to stdout for inclusion in the LLM
prompt as the `upstream_release` context block.

Inputs (env):
  PACKAGE             e.g. "nova"
  OPENSTACK_SERIES    e.g. "2024.1", "caracal"

Output (stdout): a key/value block; missing fields are emitted as `unknown`.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

import yaml

DELIVERABLE_URL = (
    "https://opendev.org/openstack/releases/raw/branch/master/"
    "deliverables/{series}/{package}.yaml"
)


def emit(**fields: str) -> None:
    print("=== upstream_release ===")
    for k, v in fields.items():
        print(f"{k}: {v}")


def main() -> int:
    package = os.environ.get("PACKAGE", "").strip()
    series = os.environ.get("OPENSTACK_SERIES", "").strip()
    if not package or not series:
        emit(error="missing PACKAGE or OPENSTACK_SERIES env var")
        return 2

    url = DELIVERABLE_URL.format(series=series, package=package)
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = yaml.safe_load(resp.read())
    except urllib.error.HTTPError as e:
        emit(
            package=package,
            openstack_series=series,
            source_url=url,
            version="unknown",
            error=f"http {e.code}",
        )
        return 0
    except (urllib.error.URLError, TimeoutError) as e:
        emit(
            package=package,
            openstack_series=series,
            source_url=url,
            version="unknown",
            error=f"network: {e}",
        )
        return 0

    releases = data.get("releases") or []
    if not releases:
        emit(
            package=package,
            openstack_series=series,
            source_url=url,
            version="unknown",
            error="no releases in deliverable",
        )
        return 0

    latest = releases[-1]
    version = str(latest.get("version", "unknown"))
    projects = latest.get("projects") or []
    repo = projects[0].get("repo") if projects else ""
    sha = projects[0].get("hash") if projects else ""

    emit(
        package=package,
        openstack_series=series,
        source_url=url,
        version=version,
        upstream_repo=repo or "unknown",
        upstream_sha=sha or "unknown",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
