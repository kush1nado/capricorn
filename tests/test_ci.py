"""CI проверяет все заявленные платформы и минимальную версию Python."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_matrix() -> dict:
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
    return wf["jobs"]["test"]["strategy"]["matrix"]


def test_ci_covers_all_platforms():
    assert set(load_matrix()["os"]) == {"ubuntu-latest", "windows-latest", "macos-latest"}


def test_ci_tests_minimum_supported_python():
    m = re.search(r'requires-python\s*=\s*">=(\d+\.\d+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert m, "requires-python не найден"
    versions = load_matrix()["python"]
    assert all(isinstance(v, str) for v in versions), "версии в кавычках, иначе YAML сделает из 3.10 число 3.1"
    assert m.group(1) in versions


def load_jobs() -> dict:
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
    return wf["jobs"]


def test_build_runs_for_release_tags():
    assert "refs/tags/v" in load_jobs()["build"]["if"]


def test_release_job_publishes_builds_for_tags():
    rel = load_jobs()["release"]
    assert rel["needs"] == "build"
    assert "refs/tags/v" in rel["if"]
    assert rel["permissions"]["contents"] == "write"
    steps = " ".join(str(s.get("run", "")) for s in rel["steps"])
    assert "version.py check-tag" in steps, "тег должен совпадать с версией в capricorn/__init__.py"
    assert "gh release create" in steps
