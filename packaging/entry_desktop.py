"""Точка входа оконного Capricorn(.exe / .app): по умолчанию — десктопный режим."""
import os
import sys
from pathlib import Path

from capricorn.__main__ import main


def default_repo() -> str | None:
    """Двойной клик: Windows/Linux стартуют в папке приложения — её и берём.
    macOS запускает .app с текущей папкой «/» — тогда (и внутри бандла) берём домашний каталог,
    а не корень диска: агентам с инструментами там делать нечего."""
    cwd = Path(os.getcwd()).resolve()
    if cwd == Path(cwd.anchor) or any(p.suffix == ".app" for p in (cwd, *cwd.parents)):
        return str(Path.home())
    return None


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--repo" not in args and (repo := default_repo()):
        args = ["--repo", repo, *args]
    if not {"--web", "--desktop"} & set(args):
        args.append("--desktop")
    sys.exit(main(args))
