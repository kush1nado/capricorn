"""Сборка приложения (PyInstaller) и проверка результата.

    python scripts/build.py          → dist/Capricorn/ (Windows: Capricorn.exe, macOS: Capricorn.app)

Нужны: pip install -e ".[desktop,build]"
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    cmd = [sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "capricorn.spec"), "--noconfirm",
           "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build")]
    if subprocess.run(cmd, cwd=ROOT).returncode != 0:
        return 1
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke_build.py"),
                           str(ROOT / "dist" / "Capricorn")], cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
