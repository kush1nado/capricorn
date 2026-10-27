"""Версия проекта — единственный источник: capricorn/__init__.py.

    python scripts/version.py bump [patch|minor|major]   поднять версию
    python scripts/version.py check                      staged-версия > версии в HEAD? (для pre-commit)
    python scripts/version.py show                       текущая версия
    python scripts/version.py check-tag v1.2.3           тег релиза совпадает с версией? (для CI)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "capricorn" / "__init__.py"
INIT_GIT = "capricorn/__init__.py"
VERSION_RE = re.compile(r'^__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', re.M)


def parse(text: str) -> tuple[int, int, int]:
    m = VERSION_RE.search(text)
    if not m:
        raise ValueError("не найдено __version__ = \"X.Y.Z\"")
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def fmt(v: tuple[int, int, int]) -> str:
    return ".".join(map(str, v))


def bumped(v: tuple[int, int, int], part: str) -> tuple[int, int, int]:
    major, minor, patch = v
    if part == "major":
        return major + 1, 0, 0
    if part == "minor":
        return major, minor + 1, 0
    if part == "patch":
        return major, minor, patch + 1
    raise ValueError(f"неизвестная часть версии: {part}")


def git_show(rev_path: str) -> str | None:
    r = subprocess.run(["git", "show", rev_path], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8")
    return r.stdout if r.returncode == 0 else None


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "show"
    if cmd == "show":
        print(fmt(parse(INIT.read_text(encoding="utf-8"))))
        return 0
    if cmd == "bump":
        with open(INIT, encoding="utf-8", newline="") as f:      # newline="" — переводы строк как есть
            text = f.read()
        old = parse(text)
        new = bumped(old, argv[1] if len(argv) > 1 else "patch")
        with open(INIT, "w", encoding="utf-8", newline="") as f:
            f.write(VERSION_RE.sub(f'__version__ = "{fmt(new)}"', text, count=1))
        print(f"{fmt(old)} -> {fmt(new)}")
        return 0
    if cmd == "check":
        head = git_show(f"HEAD:{INIT_GIT}")
        if head is None:          # первый коммит — сравнивать не с чем
            return 0
        staged = git_show(f":{INIT_GIT}")
        if staged is None:
            print(f"version: {INIT_GIT} не в индексе", file=sys.stderr)
            return 1
        old, new = parse(head), parse(staged)
        if new <= old:
            print(f"version: каждый коммит поднимает версию (сейчас {fmt(old)} -> {fmt(new)}).\n"
                  f"  python scripts/version.py bump && git add {INIT_GIT}", file=sys.stderr)
            return 1
        return 0
    if cmd == "check-tag":
        if len(argv) < 2:
            print(__doc__, file=sys.stderr)
            return 2
        expected = "v" + fmt(parse(INIT.read_text(encoding="utf-8")))
        if argv[1] != expected:
            print(f"version: тег {argv[1]} не совпадает с версией пакета ({expected})", file=sys.stderr)
            return 1
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
