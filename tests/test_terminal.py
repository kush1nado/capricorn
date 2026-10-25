"""Совместимость с консолью Windows: цвета и глифы."""
from pathlib import Path

from capricorn.terminal import prepare_terminal

PKG = Path(__file__).resolve().parent.parent / "capricorn"

# Символы вне ASCII и кириллицы, наличие которых проверено в Consolas — шрифте
# классической консоли Windows (conhost). Остальные там рисуются квадратами.
# Новый символ — сначала проверь его в C:\Windows\Fonts\consola.ttf, потом добавляй сюда.
CONSOLAS_SAFE = set(
    "«·»×±—‖…←↑→↓≡─┃═║▀▄█▌▐░▒▓■□▪▫▬▲►▼◄▸◂●○◊"
)


def test_windows_forces_truecolor():
    env = {}
    prepare_terminal(env, platform="win32")
    assert env["TEXTUAL_COLOR_SYSTEM"] == "truecolor"


def test_user_choice_is_respected():
    env = {"TEXTUAL_COLOR_SYSTEM": "256"}
    prepare_terminal(env, platform="win32")
    assert env["TEXTUAL_COLOR_SYSTEM"] == "256"


def test_other_platforms_untouched():
    env = {}
    prepare_terminal(env, platform="linux")
    assert env == {}


def test_only_consolas_safe_glyphs_in_sources():
    bad = {}
    for f in PKG.glob("*.py"):
        for ch in set(f.read_text(encoding="utf-8")):
            if ord(ch) > 127 and not ("\u0400" <= ch <= "\u04ff") and ch not in CONSOLAS_SAFE:
                bad.setdefault(f.name, set()).add(f"{ch} U+{ord(ch):04X}")
    assert not bad, f"глифы, которых нет в Consolas (в conhost будут квадратами): {bad}"


def test_main_prepares_terminal_before_textual_import():
    src = (PKG / "__main__.py").read_text(encoding="utf-8")
    assert src.index("prepare_terminal()") < src.index("from .tui import")
