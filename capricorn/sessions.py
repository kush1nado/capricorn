"""Сессии команды по проектам: сохранение, список, продолжение, экспорт в Markdown.

Данные проекта лежат в <git-common-dir>/capricorn (внутри .git — не видно в git status),
для папок без git — в ~/.local/share/capricorn/projects/<имя>-<хэш>. Сессия — каталог
sessions/<id>/ со state.json (полный снимок команды) и log.jsonl (поток событий).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

STATE = "state.json"
VERSION = 1


def project_dir(repo: str | Path) -> Path:
    repo = Path(repo).resolve()
    try:
        r = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=repo, capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        r = None
    if r is not None and r.returncode == 0 and r.stdout.strip():
        gd = Path(r.stdout.strip())
        return ((gd if gd.is_absolute() else repo / gd).resolve()) / "capricorn"
    slug = re.sub(r"[^\w.-]+", "_", repo.name or "root")[:40]
    digest = hashlib.sha1(str(repo).lower().encode("utf-8")).hexdigest()[:8]
    return Path.home() / ".local" / "share" / "capricorn" / "projects" / f"{slug}-{digest}"


@dataclass
class SessionInfo:
    id: str
    updated: float
    messages: int
    title: str


class SessionStore:
    def __init__(self, repo: str | Path):
        self.root = project_dir(repo) / "sessions"

    def new_id(self) -> str:
        """Уникален даже для двух сессий в одну секунду (иначе /new перезаписал бы прошлую)."""
        while True:
            sid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
            if not (self.root / sid).exists():
                return sid

    def dir(self, sid: str) -> Path:
        if not re.fullmatch(r"[\w.-]+", sid):
            raise ValueError(f"некорректный id сессии: {sid!r}")
        return self.root / sid

    def save(self, sid: str, state: dict) -> Path:
        d = self.dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (STATE + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, d / STATE)          # атомарно: обрыв посреди записи не портит сессию
        return d / STATE

    def load(self, sid: str) -> dict:
        path = self.dir(sid) / STATE
        if not path.exists():
            raise FileNotFoundError(f"нет сессии {sid}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[SessionInfo]:
        out = []
        if not self.root.exists():
            return out
        for d in self.root.iterdir():
            path = d / STATE
            if not path.is_file():
                continue
            try:
                st = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue                    # битый файл не роняет список
            msgs = st.get("messages") or []
            first = next((m["content"] for m in msgs if m.get("sender") == "user"), "")
            out.append(SessionInfo(d.name, float(st.get("updated") or path.stat().st_mtime), len(msgs),
                                   " ".join(first.split())[:80]))
        return sorted(out, key=lambda s: s.updated, reverse=True)

    def latest(self) -> str | None:
        sessions = self.list()
        return sessions[0].id if sessions else None


def to_markdown(state: dict) -> str:
    """Переписка команды — для чтения человеком (без истории моделей и вызовов инструментов)."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(state.get("created") or time.time()))
    lines = [f"# Capricorn — сессия {state.get('id', '')}", "",
             f"- Проект: `{state.get('repo', '')}`", f"- Начата: {when}"]
    models = state.get("models") or {}
    if models:
        lines += ["", "| Агент | Модель |", "|---|---|", *[f"| {a} | `{m}` |" for a, m in models.items()]]
    lines.append("")
    for m in state.get("messages") or []:
        t = time.strftime("%H:%M", time.localtime(m.get("ts") or 0))
        to = ", ".join(m.get("to") or []) or "—"
        mark = {"private": " · лично", "handoff": " · итог для команды"}.get(m.get("kind", "team"), "")
        lines += [f"### {t} · {m.get('sender')} → {to}{mark}", "", (m.get("content") or "").strip(), ""]
    return "\n".join(lines).rstrip() + "\n"
