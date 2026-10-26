"""capricorn [--repo PATH] [--config FILE] [--mock] [--complex] [--web [--port N] [--no-open] | --desktop]"""
from __future__ import annotations

import argparse
import os
import sys

from .config import load_config
from .terminal import prepare_terminal


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="capricorn", description="Мультиагентная среда разработки")
    ap.add_argument("--repo", default=os.getcwd(), help="путь к git-репозиторию (по умолч. текущая папка)")
    ap.add_argument("--config", help="путь к YAML-конфигу")
    ap.add_argument("--mock", action="store_true", help="все агенты на офлайн-заглушке (проверка UI)")
    ap.add_argument("--complex", action="store_true", help="сразу включить третьего кодера")
    ap.add_argument("--resume", action="store_true", help="продолжить последнюю сессию проекта")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--web", action="store_true", help="открыть интерфейс в браузере")
    mode.add_argument("--desktop", action="store_true", help="открыть интерфейс в отдельном окне")
    ap.add_argument("--port", type=int, default=0, help="порт для --web (по умолч. свободный)")
    ap.add_argument("--no-open", action="store_true", help="--web: не открывать браузер")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config, force_mock=args.mock)   # ошибки конфига — сразу, до запуска сервера

    if args.web or args.desktop:
        from . import web
        child = web.app_argv(args.repo, args.config, args.mock, args.complex, args.resume)
        try:
            if args.web:
                web.serve_web(web.child_command(child), args.port or web.free_port(), open_browser=not args.no_open)
            else:
                web.run_desktop(child)
        except web.ModeUnavailable as e:
            print(f"capricorn: {e}", file=sys.stderr)
            return 2
        return 0

    if args.complex:
        cfg.complex_mode = True
    prepare_terminal()
    from .tui import CapricornApp
    CapricornApp(cfg, os.path.abspath(args.repo), resume=args.resume).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
