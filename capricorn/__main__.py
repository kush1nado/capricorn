"""capricorn [--repo PATH] [--config FILE] [--mock] [--complex]"""
from __future__ import annotations

import argparse
import os

from .config import load_config


def main():
    ap = argparse.ArgumentParser(prog="capricorn", description="Мультиагентная среда разработки в терминале")
    ap.add_argument("--repo", default=os.getcwd(), help="путь к git-репозиторию (по умолч. текущая папка)")
    ap.add_argument("--config", help="путь к YAML-конфигу")
    ap.add_argument("--mock", action="store_true", help="все агенты на офлайн-заглушке (проверка UI)")
    ap.add_argument("--complex", action="store_true", help="сразу включить третьего кодера")
    args = ap.parse_args()

    cfg = load_config(args.config, force_mock=args.mock)
    if args.complex:
        cfg.complex_mode = True

    from .tui import CapricornApp
    CapricornApp(cfg, os.path.abspath(args.repo)).run()


if __name__ == "__main__":
    main()
