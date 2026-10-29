"""Сжатие контекста для агентов, которым не нужен код целиком (по умолчанию — менеджер).

Без вызовов модели: блоки кода сворачиваются в одну строку, подслушанное ужимается до начала сообщения,
старые записи истории — до короткого конспекта. Последние ходы остаются как есть.
"""
from __future__ import annotations

import re

CODE_RE = re.compile(r"```([\w+.#-]*)[^\n]*\n(.*?)(?:```|\Z)", re.S)
BLANKS_RE = re.compile(r"\n{3,}")
KEEP_FULL = 6                   # последние записи истории не трогаем


def collapse_code(text: str) -> str:
    def repl(m: re.Match) -> str:
        n = len(m.group(2).splitlines())
        lang = f" {m.group(1)}" if m.group(1) else ""
        return f"[код{lang}, строк: {n} — свёрнут]"
    return CODE_RE.sub(repl, text)


def digest(text: str, limit: int) -> str:
    """Код свёрнут, пустые строки схлопнуты, длина не больше limit (с пометкой, сколько отрезано)."""
    out = BLANKS_RE.sub("\n\n", collapse_code(text)).strip()
    if len(out) <= limit:
        return out
    cut = out[:limit].rstrip()
    return f"{cut}… [ещё {len(out) - len(cut)} симв.]"


def compact_context(items: list[str], chars: int, count: int) -> list[str]:
    """Подслушанное: только последние count сообщений, каждое — не длиннее chars."""
    skipped = max(0, len(items) - count)
    kept = [digest(t, chars) for t in items[skipped:]]
    return ([f"… ранее ещё сообщений: {skipped}"] if skipped else []) + kept


def compact_history(history: list[dict], chars: int) -> None:
    """Старые записи (кроме последних KEEP_FULL) — в конспект. Структура и id вызовов инструментов не меняются."""
    for entry in history[:-KEEP_FULL]:
        if entry.get("compact"):
            continue
        if entry["role"] in ("user", "assistant") and len(entry.get("content") or "") > chars:
            entry["content"] = digest(entry["content"], chars)
        for r in entry.get("results") or []:
            if len(r.get("content") or "") > chars:
                r["content"] = digest(r["content"], chars)
        entry["compact"] = True
