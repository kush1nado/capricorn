import importlib.util
import re
from pathlib import Path

import pytest

import capricorn

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("version_script", ROOT / "scripts" / "version.py")
version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(version)


def test_package_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", capricorn.__version__)
    assert version.parse((ROOT / "capricorn" / "__init__.py").read_text(encoding="utf-8")) == \
        tuple(map(int, capricorn.__version__.split(".")))


def test_pyproject_takes_version_from_package():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text
    assert 'version = { attr = "capricorn.__version__" }' in text
    assert not re.search(r'^version\s*=\s*"', text, re.M), "версия должна жить только в __init__.py"


@pytest.mark.parametrize("part, expected", [
    ("patch", (1, 2, 4)), ("minor", (1, 3, 0)), ("major", (2, 0, 0)),
])
def test_bumped(part, expected):
    assert version.bumped((1, 2, 3), part) == expected


def test_bumped_rejects_unknown_part():
    with pytest.raises(ValueError):
        version.bumped((1, 2, 3), "build")


def test_bump_rewrites_only_version_line(tmp_path, monkeypatch):
    init = tmp_path / "__init__.py"
    init.write_text('"""doc"""\n__version__ = "0.9.9"\nOTHER = "1.2.3"\n', encoding="utf-8")
    monkeypatch.setattr(version, "INIT", init)
    assert version.main(["bump"]) == 0
    assert init.read_text(encoding="utf-8") == '"""doc"""\n__version__ = "0.9.10"\nOTHER = "1.2.3"\n'
    assert version.main(["bump", "minor"]) == 0
    assert '__version__ = "0.10.0"' in init.read_text(encoding="utf-8")


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        version.parse("__version__ = 'abc'")


def test_check_requires_increase_against_head(tmp_path, monkeypatch):
    from helpers import git, init_repo
    init_repo(tmp_path)
    pkg = tmp_path / "capricorn"
    pkg.mkdir()
    init = pkg / "__init__.py"
    init.write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    monkeypatch.setattr(version, "ROOT", tmp_path)
    monkeypatch.setattr(version, "INIT", init)

    assert version.main(["check"]) == 0                  # нет HEAD — первый коммит
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "init")

    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    git(tmp_path, "add", "x.txt")
    assert version.main(["check"]) == 1                  # версия не поднята

    version.main(["bump"])
    assert version.main(["check"]) == 1                  # поднята, но не в индексе
    git(tmp_path, "add", "capricorn/__init__.py")
    assert version.main(["check"]) == 0

    init.write_text('__version__ = "0.0.9"\n', encoding="utf-8")
    git(tmp_path, "add", "capricorn/__init__.py")
    assert version.main(["check"]) == 1                  # понижение тоже нельзя
