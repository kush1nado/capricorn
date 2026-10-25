"""Общие тестовые утилиты: скриптованный провайдер, сборка оркестратора, ожидание тишины."""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass

from capricorn.config import load_config
from capricorn.orchestrator import Orchestrator
from capricorn.providers import Provider


@dataclass
class Call:
    agent: str
    model: str
    system: str
    messages: list[dict]
    tools: list[dict] | None = None
    tool_choice: str = "auto"


def tool(name: str, **args) -> dict:
    """Ответ модели, вызывающий инструмент: tool("read_file", path="a.py")."""
    return {"tool_calls": [{"name": name, "args": args}]}


class ScriptedProvider(Provider):
    """replies: {agent: ответ | [ ...по одному на вызов модели ]}.

    Ответ: str; callable(agent, last_incoming) -> ответ или awaitable; исключение — бросается;
    dict {"text": str, "tool_calls": [{"name", "args", "id"?}]} — вызов инструментов.
    Когда список кончился, агент отвечает пустой строкой.
    """

    def __init__(self, replies: dict | None = None):
        super().__init__("script", {})
        self.replies = {k: list(v) if isinstance(v, list) else v for k, v in (replies or {}).items()}
        self.calls: list[Call] = []

    def calls_for(self, agent: str) -> list[Call]:
        return [c for c in self.calls if c.agent == agent]

    async def stream(self, *, model, system, messages, temperature, max_tokens, agent,
                     tools=None, tool_choice="auto"):
        self.calls.append(Call(agent, model, system, [dict(m) for m in messages], tools, tool_choice))
        item = self.replies.get(agent, "")
        if isinstance(item, list):
            item = item.pop(0) if item else ""
        if isinstance(item, BaseException) or (isinstance(item, type) and issubclass(item, BaseException)):
            raise item
        if callable(item):
            item = item(agent, messages[-1].get("content", "") if messages else "")
            if inspect.isawaitable(item):
                item = await item
        calls = []
        if isinstance(item, dict):
            calls = [{"id": c.get("id", f"t{len(self.calls)}_{i}"), "name": c["name"], "args": c.get("args", {})}
                     for i, c in enumerate(item.get("tool_calls") or [])]
            item = item.get("text", "")
        text = str(item)
        for i in range(0, len(text), 5):
            yield text[i:i + 5]
            await asyncio.sleep(0)
        if calls:
            yield {"tool_calls": calls}
        yield {"usage": {"in": 10, "out": len(text)}}


def build_orch(repo, replies=None, *, provider: Provider | None = None, **overrides):
    """Оркестратор, где все агенты на провайдере `script`. Не запускает задачи агентов."""
    cfg = load_config()
    cfg.providers["script"] = {"type": "mock"}
    for a in cfg.agents.values():
        a.model = "script/test"
    for key, value in overrides.items():
        if not hasattr(cfg, key):
            raise AttributeError(f"в Config нет поля {key}")
        setattr(cfg, key, value)
    events: list[tuple[str, dict]] = []
    orch = Orchestrator(cfg, str(repo), lambda kind, **d: events.append((kind, d)))
    orch.providers["script"] = provider or ScriptedProvider(replies)
    return orch, events


def kinds(events, kind: str) -> list[dict]:
    return [d for k, d in events if k == kind]


def messages(events) -> list:
    return [d["msg"] for k, d in events if k == "message"]


async def settle(orch: Orchestrator, timeout: float = 5.0):
    """Ждёт, пока ни один агент не генерирует и не разбужен (две проверки подряд)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    quiet = 0
    while quiet < 2:
        await asyncio.sleep(0.005)
        busy = any(a.gen_task is not None or a.wake.is_set() for a in orch.agents.values())
        quiet = 0 if busy else quiet + 1
        if loop.time() > deadline:
            state = {n: (a.status, len(a.pending), a.gen_task is not None) for n, a in orch.agents.items()}
            raise AssertionError(f"команда не успокоилась за {timeout}s: {state}")


def sse(*events) -> bytes:
    """Тело SSE-ответа: dict → data: <json>, str → data: <str>."""
    out = []
    for e in events:
        out.append("data: " + (e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)))
        out.append("")
    return ("\n".join(out) + "\n").encode("utf-8")


def git(cwd, *args: str) -> str:
    """Синхронный git для подготовки репозиториев в тестах."""
    import subprocess
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr or r.stdout}")
    return r.stdout.strip()


def init_repo(path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "core.autocrlf", "false")
