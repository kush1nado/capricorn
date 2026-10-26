"""Все подпроцессы явно задают stdin.

Иначе они наследуют stdin интерфейса: в веб/десктоп-режиме это канал от textual-serve, который в
соседнем потоке читает драйвер Textual — на Windows запуск git с таким наследством подвисал
(Git-экран и git-панель оставались пустыми). Да и git не должен уметь «спросить пароль» у интерфейса.
"""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "capricorn"
CALLS = {"create_subprocess_exec", "create_subprocess_shell", "run", "Popen"}


def test_every_subprocess_call_sets_stdin():
    missing = []
    for f in PKG.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            owner = getattr(node.func.value, "id", "") if isinstance(node.func, ast.Attribute) else ""
            if name not in CALLS or (name == "run" and owner not in ("subprocess",)):
                continue
            kws = {k.arg for k in node.keywords}
            if not ({"stdin", "input"} & kws or None in kws):       # None — это **kwargs
                missing.append(f"{f.name}:{node.lineno} {owner}.{name}")
    assert not missing, f"подпроцессы без stdin=: {missing}"
