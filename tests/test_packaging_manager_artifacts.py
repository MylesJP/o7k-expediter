from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import yaml

from managers import packaging


def _load_run_build_module():
    path = Path(__file__).resolve().parent.parent / "skills" / "packastack-build" / "run_build.py"
    spec = importlib.util.spec_from_file_location("packastack_build_run_build", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workpackage(tmp_path: Path) -> tuple[dict, Path]:
    wp_dir = tmp_path / "wp"
    wp_dir.mkdir()
    wp = {
        "id": "wp-test",
        "status": "in_progress",
        "current_stage": "packaging",
        "current_owner": "packager",
        "target": {
            "upstream_project": "python-novaclient",
            "upstream_version": "18.12.0",
            "openstack_series": "gazpacho",
            "ubuntu_release": "resolute",
            "uca_pocket": "gazpacho",
            "trigger": {"type": "upstream_release", "detected": "", "source": ""},
        },
        "stamps": [],
        "history": [],
        "open_issues": [],
        "intervention": None,
    }
    wp_path = wp_dir / "workpackage.yaml"
    wp_path.write_text(yaml.safe_dump(wp, sort_keys=False))
    return wp, wp_path


def test_packaging_manager_threads_workpackage_artifacts_into_qa(tmp_path, monkeypatch):
    wp, wp_path = _workpackage(tmp_path)
    artifact_root = wp_path.parent / "artifacts" / "packastack"
    apt_repo = artifact_root / "apt-repo"
    deb = apt_repo / "pool" / "main" / "python3-novaclient_18.12.0-0ubuntu1_all.deb"
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_run(skill_name: str, env: dict[str, str]) -> str:
        calls.append((skill_name, env.copy()))
        assert env["WORKPACKAGE_DIR"] == str(wp_path.parent)

        if skill_name == "packastack-build":
            return "\n".join(
                [
                    "STATUS: SUCCESS",
                    "PACKAGE: python-novaclient",
                    "UBUNTU_SERIES: resolute",
                    f"DEB_PATHS: {deb}",
                    f"APT_REPO: {apt_repo}",
                    "EXPLANATION: build succeeded",
                    "CONFIDENCE: 1.0",
                ]
            )

        if skill_name == "run-autopkgtest":
            assert env["DEB_DIR"] == str(apt_repo)
            return "\n".join(
                [
                    "AUTOPKGTEST_RESULT: PASS",
                    "PACKAGE: python-novaclient",
                    "UBUNTU_SERIES: resolute",
                    "TESTS_TOTAL: 1",
                    "TESTS_PASSED: 1",
                    "TESTS_FAILED: 0",
                    "TESTS_SKIPPED: 0",
                    "EXPLANATION: tests passed",
                    "CONFIDENCE: 1.0",
                ]
            )

        raise AssertionError(f"unexpected skill: {skill_name}")

    monkeypatch.setattr(packaging.skills, "run", fake_run)

    assert packaging._run_packaging(wp, wp_path) is True
    assert wp["_build_output"]["apt_repo"] == str(apt_repo)
    assert wp["stamps"][-1]["artifacts"] == [str(deb)]
    assert str(wp_path.parent / "artifacts") in wp["stamps"][-1]["artifacts"][0]

    assert packaging._run_qa(wp, wp_path) is True
    assert [name for name, _env in calls] == ["packastack-build", "run-autopkgtest"]
    assert wp["stamps"][-1]["stage"] == "qa"
    assert wp["stamps"][-1]["result"] == "verified"


def test_packastack_build_script_uses_workpackage_local_home_and_output(tmp_path, monkeypatch):
    module = _load_run_build_module()

    wp_dir = tmp_path / "wp"
    wp_dir.mkdir()
    fake_packastack = tmp_path / "packastack"
    fake_packastack.mkdir()

    monkeypatch.setattr(module, "PACKASTACK_DIR", fake_packastack)
    monkeypatch.setenv("PACKAGE", "python-novaclient")
    monkeypatch.setenv("OPENSTACK_SERIES", "gazpacho")
    monkeypatch.setenv("UBUNTU_SERIES", "resolute")
    monkeypatch.setenv("WORKPACKAGE_DIR", str(wp_dir))

    commands: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, cwd=None, env=None, **_kwargs):
        commands.append(list(cmd))
        assert cwd == str(fake_packastack)
        assert env["HOME"] == str(wp_dir / "artifacts" / "packastack" / "home")
        assert env["UV_PROJECT_ENVIRONMENT"] == str(wp_dir / "artifacts" / "packastack" / "venv")

        if cmd[:4] == ["uv", "run", "packastack", "build"]:
            apt_repo = wp_dir / "artifacts" / "packastack" / "apt-repo" / "pool" / "main"
            apt_repo.mkdir(parents=True)
            (apt_repo / "python3-novaclient_18.12.0-0ubuntu1_all.deb").write_text("deb")
            return Result(stdout="Source package built successfully\n")

        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 0

    config = wp_dir / "artifacts" / "packastack" / "home" / ".config" / "packastack" / "config.yaml"
    assert config.exists()
    config_text = config.read_text()
    assert f"cache_root: {wp_dir}/artifacts/packastack/cache" in config_text
    assert f"local_apt_repo: {wp_dir}/artifacts/packastack/apt-repo" in config_text
    assert ["uv", "run", "packastack", "init"] in commands
    assert [
        "uv",
        "run",
        "packastack",
        "build",
        "python-novaclient",
        "--ubuntu-series",
        "resolute",
        "--target",
        "gazpacho",
        "--archive-deps",
    ] in commands
