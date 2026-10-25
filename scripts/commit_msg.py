"""Проверка сообщения коммита (вызывается из .githooks/commit-msg).

    python scripts/commit_msg.py <файл-сообщения>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

FORBIDDEN = {
    "Co-Authored-By": re.compile(r"^\s*co-authored-by:", re.I | re.M),
    "Claude-Session": re.compile(r"^\s*claude-session:", re.I | re.M),
}


def problems(message: str) -> list[str]:
    body = "\n".join(l for l in message.splitlines() if not l.startswith("#"))
    out = []
    if not body.strip():
        out.append("пустое сообщение")
    for name, rx in FORBIDDEN.items():
        if rx.search(body):
            out.append(f"строки {name} в коммитах не пишем — убери её")
    return out


def main(argv: list[str]) -> int:
    errs = problems(Path(argv[0]).read_text(encoding="utf-8"))
    for e in errs:
        print(f"commit-msg: {e}", file=sys.stderr)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
