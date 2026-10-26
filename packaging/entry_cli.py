"""Точка входа консольного capricorn-cli(.exe): терминал, --web, --desktop; им же сервер запускает интерфейс."""
import sys

from capricorn.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
