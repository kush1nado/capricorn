"""Подготовка терминала до импорта Textual."""
from __future__ import annotations

import os
import queue
import sys
import threading
from typing import MutableMapping


class TickingInputReader:
    """Замена InputReader веб-драйвера Textual для Windows.

    Родной читает stdin блокирующим os.read без таймаута: пока нет ввода, цикл драйвера стоит и
    не зовёт tick() парсера — одиночный Esc (и всё, что ждёт таймера) срабатывает только со следующей
    клавишей. Здесь чтение — в фоновом потоке, а итератор раз в timeout отдаёт b"" как пульс.
    """

    def __init__(self, timeout: float = 0.1, fileno: int | None = None):
        self._fileno = sys.__stdin__.fileno() if fileno is None else fileno
        self.timeout = timeout
        self._exit = threading.Event()
        self._queue: queue.Queue[bytes] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True, name="capricorn-stdin").start()

    def _pump(self):
        while not self._exit.is_set():
            try:
                data = os.read(self._fileno, 1024)
            except OSError:
                data = b""
            self._queue.put(data)
            if not data:
                return

    def close(self) -> None:
        self._exit.set()

    def __iter__(self):
        while not self._exit.is_set():
            try:
                data = self._queue.get(timeout=self.timeout)
            except queue.Empty:
                yield b""                # пульс: драйвер позовёт parser.tick()
                continue
            if not data:
                return
            yield data


def install_web_input_fix() -> None:
    if sys.platform != "win32":          # на Linux/macOS у Textual уже есть таймаут в select()
        return
    import textual.drivers.web_driver as web_driver
    web_driver.InputReader = TickingInputReader


class _AltKey:
    """Как элемент enum Keys в таблице Textual: парсеру нужен только .value — имя клавиши."""

    def __init__(self, name: str):
        self.value = name


def install_key_fixes() -> None:
    """Чтобы Alt-клавиши Capricorn доходили до приложения сразу (проверено настоящим парсером в тестах).

    Терминал шлёт Alt+X как «ESC X». У Textual с этим три беды:
    * «ESC+цифра» отображается в символы Option-клавиш macOS (ради WezTerm): Alt+2 → знак торговой марки;
    * «ESC+пробел» разбирается как обычный пробел — префикс alt теряется;
    * остальные «ESC+буква» распознаются только по таймеру tick(), а веб-драйвер зовёт его лишь при
      следующем вводе — в браузере и окне клавиша «залипает» до следующего нажатия.
    Регистрируем «ESC+цифра/буква/пробел» как полные последовательности: alt+… приходит мгновенно везде.
    Правка внутренностей Textual: если их переименуют, упадут тесты test_terminal.py.
    """
    from textual._ansi_sequences import ANSI_SEQUENCES_KEYS

    for ch in "0123456789abcdefghijklmnopqrstuvwxyz":
        ANSI_SEQUENCES_KEYS[f"\x1b{ch}"] = (_AltKey(f"alt+{ch}"),)
    ANSI_SEQUENCES_KEYS["\x1b "] = (_AltKey("alt+space"),)


def prepare_terminal(environ: MutableMapping[str, str] | None = None, platform: str | None = None) -> None:
    """Textual определяет глубину цвета до того, как включит VT-режим консоли Windows,
    и в классическом conhost скатывается к 8-битной палитре — тема теряет цвета.
    conhost в Windows 10+ умеет truecolor, поэтому задаём его явно (если пользователь
    не выбрал сам). Вызывать до импорта textual: константы читаются при импорте."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        environ.setdefault("TEXTUAL_COLOR_SYSTEM", "truecolor")
