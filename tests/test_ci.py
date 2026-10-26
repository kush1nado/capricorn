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
