"""Подготовка терминала до импорта Textual."""
from __future__ import annotations

import os
import sys
from typing import MutableMapping


def prepare_terminal(environ: MutableMapping[str, str] | None = None, platform: str | None = None) -> None:
    """Textual определяет глубину цвета до того, как включит VT-режим консоли Windows,
    и в классическом conhost скатывается к 8-битной палитре — тема теряет цвета.
    conhost в Windows 10+ умеет truecolor, поэтому задаём его явно (если пользователь
    не выбрал сам). Вызывать до импорта textual: константы читаются при импорте."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        environ.setdefault("TEXTUAL_COLOR_SYSTEM", "truecolor")
