"""Точка входа оконного Capricorn(.exe / .app): по умолчанию — десктопный режим со стартовым экраном."""
import sys

from capricorn.__main__ import main

if __name__ == "__main__":
    args = sys.argv[1:]
    if not {"--web", "--desktop"} & set(args):
        args.append("--desktop")
    sys.exit(main(args))
