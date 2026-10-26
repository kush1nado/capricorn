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


def _parse(seq: str) -> list[str]:
    """Только feed(), без tick(): веб-драйвер Textual зовёт tick() лишь при следующем вводе,
    так что клавиша обязана распознаваться сразу — иначе в браузере/окне она «залипает»."""
    from textual import events
    from textual._xterm_parser import XTermParser
    p = XTermParser(debug=False)
    return [e.key for e in p.feed(seq) if isinstance(e, events.Key)]


def test_alt_keys_reach_the_app_through_real_parser():
    """Регрессия: Textual превращал Alt+цифру в «™» (совместимость с WezTerm/macOS), а Alt+Space — в пробел.
    Pilot.press обходит парсер, поэтому проверяем именно разбор последовательностей терминала."""
    from capricorn.terminal import install_key_fixes
    install_key_fixes()
    assert _parse("\x1b ") == ["alt+space"]
    assert [_parse(f"\x1b{d}") for d in "1290"] == [["alt+1"], ["alt+2"], ["alt+9"], ["alt+0"]]
    assert [_parse(f"\x1b{c}") for c in "ogpzh"] == [[f"alt+{c}"] for c in "ogpzh"]
    assert _parse("\x1b[1;3D") == ["alt+left"]
    assert _parse(" ") == ["space"] and _parse("2") == ["2"] and _parse("o") == ["o"]   # обычные не тронуты
    install_key_fixes()                                              # повторный вызов безопасен
    assert _parse("\x1b ") == ["alt+space"]


def test_tui_installs_key_fixes_on_import():
    import capricorn.tui  # noqa: F401
    assert _parse("\x1b3") == ["alt+3"]


def test_ticking_input_reader_yields_heartbeats_and_data():
    """Регрессия: на Windows веб-драйвер Textual читал stdin блокирующе, tick() парсера не звался без
    нового ввода — одиночный Esc в окне/браузере срабатывал только со следующей клавишей."""
    import os
    import time

    from capricorn.terminal import TickingInputReader
    r_fd, w_fd = os.pipe()
    reader = TickingInputReader(timeout=0.05, fileno=r_fd)
    it = iter(reader)
    t = time.monotonic()
    assert next(it) == b""                                   # пульс без ввода — tick() сработает
    assert time.monotonic() - t < 1
    os.write(w_fd, b"\x1b")
    got = next(it)
    while got == b"":
        got = next(it)
    assert got == b"\x1b"
    os.close(w_fd)                                           # конец ввода — итерация заканчивается
    rest = list(it)
    assert all(chunk == b"" for chunk in rest)
    reader.close()
    os.close(r_fd)


def test_web_driver_uses_ticking_reader_on_windows(monkeypatch):
    import textual.drivers.web_driver as wd

    from capricorn import terminal
    monkeypatch.setattr(wd, "InputReader", wd.InputReader)
    monkeypatch.setattr(terminal.sys, "platform", "win32")
    terminal.install_web_input_fix()
    assert wd.InputReader is terminal.TickingInputReader
