"""Pytest fixtures for scenario tests.

These build the inputs (a temp git repo with a chosen workpackage, a canned
skill response string). The actual state-transition logic lives in
`tests/helpers.py` — it's a stand-in for the real runner and managers, which
do not exist yet. See `tests/helpers.py` for the contract those helpers
encode.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_RESOURCES = REPO_ROOT / "test_resources"


def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, env=env, capture_output=True
    )


@pytest.fixture
def workpackage_repo(tmp_path):
    """Return a callable that materializes a workpackage fixture into a temp git repo."""

    def _make(fixture: str) -> Path:
        src = TEST_RESOURCES / "workpackages" / f"{fixture}.yaml"
        if not src.exists():
            raise FileNotFoundError(f"unknown workpackage fixture: {fixture}")
        repo = tmp_path / fixture
        repo.mkdir()
        shutil.copy(src, repo / "workpackage.yaml")
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", "workpackage.yaml")
        _git(repo, "commit", "-q", "-m", "init workpackage")
        return repo

    return _make


@pytest.fixture
def detector_response():
    """Return a callable that loads a canned detector response by scenario name."""

    def _load(scenario: str) -> str:
        path = TEST_RESOURCES / "skill_responses" / "detector" / f"{scenario}.txt"
        if not path.exists():
            raise FileNotFoundError(f"unknown detector scenario: {scenario}")
        return path.read_text()

    return _load
