"""Оркестратор: агенты, шина сообщений, маршрутизация по @упоминаниям, бюджет ходов.

UI-независим: всё, что происходит, отдаётся через emit(kind, **data).
События: message, stream_start, stream_chunk, stream_end, status, system, tool.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config
from .gitinfo import GitWatcher
from .prompts import system_prompt
from .providers import Provider, ProviderError, build_providers
from .tools import Toolbox

USER = "user"

ALIASES = {
    "architect": "architect", "arch": "architect", "архитектор": "architect",
    "manager": "manager", "pm": "manager", "менеджер": "manager",
    "reviewer": "reviewer", "review": "reviewer", "ревьюер": "reviewer",
    "tester": "tester", "qa": "tester", "тестировщик": "tester",
    "coder1": "coder1", "coder2": "coder2", "coder3": "coder3",
    "coders": "*coders", "coder": "*coders", "кодеры": "*coders",
    "all": "*all", "все": "*all", "team": "*all",
    "user": USER, "me": USER, "human": USER,
}
MENTION_RE = re.compile(r"(?<![\w@])@([\wа-яё]+)", re.I)
CODE_RE = re.compile(r"```.*?(```|$)|`[^`\n]*`", re.S)
THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.S)


@dataclass
class Message:
    id: int
    sender: str
    to: list[str]
    content: str
    ts: float = field(default_factory=time.time)

    def as_incoming(self) -> str:
        return f"[от {self.sender} → {', '.join(self.to) or '—'}]: {self.content}"


class Agent:
    def __init__(self, orch: "Orchestrator", name: str):
        self.orch = orch
        self.name = name
        self.history: list[dict] = []
        self.pending: list[Message] = []    # сообщения, требующие ответа
        self.context: list[Message] = []    # подслушанное (observe_all), без хода
        self.wake = asyncio.Event()
        self.gen_task: asyncio.Task | None = None
        self.status = "idle"
        self.tokens_in = 0
        self.tokens_out = 0
        self.turns = 0

    @property
    def cfg(self):
        return self.orch.cfg.agents[self.name]

    def set_status(self, status: str):
        self.status = status
        self.orch.emit("status", agent=self.name, status=status,
                       tokens_in=self.tokens_in, tokens_out=self.tokens_out)

    async def loop(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            await self.orch.running.wait()
            if not self.pending or self.name not in self.orch.roster:
                continue
            if self.orch.budget <= 0:
                self.set_status("waiting budget")
                self.orch.budget_exhausted()
                continue
            self.orch.budget -= 1
            batch, self.pending = self.pending, []
            ctx, self.context = self.context, []
            ok = False
            self.gen_task = asyncio.create_task(self._turn(batch, ctx))
            try:
                ok = await self.gen_task
            except asyncio.CancelledError:
                pass
            finally:
                self.gen_task = None
            if ok and self.pending:           # после ошибки повтор — только /retry или новое сообщение
                self.wake.set()

    async def _turn(self, batch: list[Message], ctx: list[Message]) -> bool:
        parts = []
        if ctx:
            parts.append("(для контекста, отвечать не нужно)\n" +
                         "\n\n".join(m.as_incoming() for m in ctx))
        parts.append("\n\n".join(m.as_incoming() for m in batch))
        self.history.append({"role": "user", "content": "\n\n---\n\n".join(parts)})
        self._trim()
        base = len(self.history) - 1              # с этого индекса — всё, что добавит этот ход

        msg_id = next(self.orch.ids)
        self.orch.emit("stream_start", agent=self.name, msg_id=msg_id)
        self.set_status("thinking")
        text = ""         # текст текущего раунда; последний уходит команде
        display = ""      # всё, что видно в панели: текст всех раундов + строки инструментов

        def show(piece: str):
            nonlocal display
            display += piece
            self.orch.emit("stream_chunk", agent=self.name, msg_id=msg_id, chunk=piece)

        try:
            provider = self.orch.provider_for(self.name)
            tools = self.orch.toolbox.specs_for(self.name)
            system = system_prompt(self.name, self.orch.roster, self.orch.cfg.language,
                                   await self.orch.repo_context(), self.cfg.extra_prompt,
                                   tools=[t["name"] for t in tools],
                                   worktrees=self.orch.toolbox.worktrees.active)
            rounds = 0
            while True:
                last_round = rounds >= self.orch.cfg.tools.max_rounds
                text, calls = "", []
                async for chunk in provider.stream(
                        model=self.cfg.model_id, system=system, messages=self.history,
                        temperature=self.cfg.temperature, max_tokens=self.cfg.max_tokens,
                        agent=self.name, tools=tools or None,
                        tool_choice="none" if last_round else "auto"):
                    if isinstance(chunk, dict):
                        u = chunk.get("usage") or {}
                        self.tokens_in += u.get("in", 0)
                        self.tokens_out += u.get("out", 0)
                        calls += chunk.get("tool_calls") or []
                        continue
                    if self.status != "typing":
                        self.set_status("typing")
                    text += chunk
                    show(chunk)
                if not calls or last_round:
                    break
                self.history.append({"role": "assistant", "content": text, "tool_calls": calls})
                results = []
                for call in calls:
                    self.set_status("working")
                    res = await self.orch.toolbox.run(self.name, call["name"], call["args"])
                    results.append({"id": call["id"], "name": call["name"],
                                    "content": res.content, "is_error": res.is_error})
                    sep = "\n" if display and not display.endswith("\n") else ""
                    show(f"{sep}» `{call['name']}` {res.summary}\n")
                    self.orch.emit("tool", agent=self.name, name=call["name"], args=call["args"],
                                   ok=not res.is_error, summary=res.summary)
                    self.orch._log_event({"type": "tool", "agent": self.name, "name": call["name"],
                                          "args": call["args"], "ok": not res.is_error,
                                          "summary": res.summary})
                self.history.append({"role": "tool", "results": results})
                if text or display:
                    show("\n")
                rounds += 1
        except asyncio.CancelledError:
            del self.history[base + 1:]           # не оставляем tool_calls без результатов
            text += "\n[прервано]"
            self.history.append({"role": "assistant", "content": text})
            self.orch.emit("stream_end", agent=self.name, msg_id=msg_id, text=display + "\n[прервано]", to=[])
            self.set_status("stopped")
            raise
        except Exception as e:  # noqa: BLE001 — любая ошибка провайдера/конфига идёт в UI, цикл живёт
            del self.history[base:]               # сохраняем чередование user/assistant
            self.pending = batch + self.pending   # можно повторить через /retry
            self.context = ctx + self.context
            self.orch.emit("stream_end", agent=self.name, msg_id=msg_id, text="", to=[])
            self.orch.emit("system", text=f"▲ {self.name}: {type(e).__name__}: {e}", level="error")
            self.set_status("error")
            return False

        self.turns += 1
        self.history.append({"role": "assistant", "content": text})
        clean = THINK_RE.sub("", text).strip()
        msg = self.orch.route(self.name, clean, msg_id=msg_id, emit_message=False)
        self.orch.emit("stream_end", agent=self.name, msg_id=msg_id, text=display, to=msg.to)
        self.orch.emit("message", msg=msg)
        self.set_status("idle")
        return True

    def _trim(self):
        limit = self.orch.cfg.max_history
        if len(self.history) > limit:
            self.history = self.history[-limit:]
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)

    def cancel(self):
        if self.gen_task and not self.gen_task.done():
            self.gen_task.cancel()


class Orchestrator:
    def __init__(self, cfg: Config, repo: str, emit: Callable[..., None]):
        self.cfg = cfg
        self.repo = repo
        self.emit = emit
        self.providers: dict[str, Provider] = build_providers(cfg.providers)
        self.agents = {name: Agent(self, name) for name in cfg.agents}
        self.git = GitWatcher(repo)
        self.toolbox = Toolbox(repo, cfg)
        self.ids = itertools.count(1)
        self.budget = cfg.budget
        self.running = asyncio.Event()
        self.running.set()
        self.attachments: list[str] = []
        self._repo_ctx: tuple[float, str] = (0, "")
        self._budget_warned = False
        self._tasks: list[asyncio.Task] = []
        log_dir = Path.home() / ".local" / "share" / "capricorn" / "sessions"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / time.strftime("%Y%m%d-%H%M%S.jsonl")

    # --------------------------------------------------------------- setup
    def start(self):
        self._tasks = [asyncio.create_task(a.loop()) for a in self.agents.values()]

    @property
    def roster(self) -> list[str]:
        return [n for n in self.agents if n != "coder3" or self.cfg.complex_mode]

    @property
    def coders(self) -> list[str]:
        return [n for n in self.roster if n.startswith("coder")]

    def provider_for(self, agent: str) -> Provider:
        p = self.cfg.agents[agent].provider
        if p not in self.providers:
            raise ProviderError(f"провайдер «{p}» не описан в конфиге")
        return self.providers[p]

    async def repo_context(self) -> str:
        ts, ctx = self._repo_ctx
        if time.time() - ts > 15:
            ctx = await self.git.context_for_agents()
            self._repo_ctx = (time.time(), ctx)
        return ctx

    # ------------------------------------------------------------- routing
    def parse_targets(self, sender: str, text: str) -> list[str]:
        stripped = CODE_RE.sub("", text)
        out: list[str] = []
        for raw in MENTION_RE.findall(stripped):
            key = ALIASES.get(raw.lower())
            if key is None:
                continue
            names = (self.coders if key == "*coders"
                     else [n for n in self.roster] if key == "*all"
                     else [key])
            for n in names:
                if n != sender and n not in out and (n == USER or n in self.roster):
                    out.append(n)
        return out

    def route(self, sender: str, text: str, msg_id: int | None = None,
              emit_message: bool = True) -> Message:
        to = self.parse_targets(sender, text)
        if sender == USER and not to:
            to = [self.cfg.default_target]
        msg = Message(id=msg_id or next(self.ids), sender=sender, to=to, content=text)
        self._log(msg)
        if emit_message:
            self.emit("message", msg=msg)
        for name in to:
            if name in self.agents:
                self.agents[name].pending.append(msg)
                self.agents[name].wake.set()
        for name, agent in self.agents.items():
            if name not in to and name != sender and agent.cfg.observe_all and name in self.roster:
                agent.context.append(msg)
        return msg

    def user_says(self, text: str) -> Message:
        if self.attachments:
            text += "\n\n" + "\n\n".join(self.attachments)
            self.attachments = []
        self.budget = max(self.budget, self.cfg.budget)
        self._budget_warned = False
        return self.route(USER, text)

    def budget_exhausted(self):
        if not self._budget_warned:
            self._budget_warned = True
            self.emit("system", level="warn",
                      text="‖ Бюджет ходов исчерпан. /continue [N] — продолжить, или напиши новую задачу.")

    def add_budget(self, n: int):
        self.budget += n
        self._budget_warned = False
        for a in self.agents.values():
            if a.pending:
                a.wake.set()

    # ------------------------------------------------------------ controls
    def pause(self):
        self.running.clear()

    def resume(self):
        self.running.set()
        for a in self.agents.values():
            if a.pending:
                a.wake.set()

    def stop_all(self):
        for a in self.agents.values():
            a.pending.clear()
            a.cancel()
        self.budget = 0

    def retry(self, name: str):
        a = self.agents[name]
        if a.pending:
            a.wake.set()

    def set_model(self, agent: str, model: str):
        provider = model.split("/", 1)[0]
        if provider not in self.providers:
            raise ValueError(f"нет провайдера «{provider}». Есть: {', '.join(self.providers)}")
        self.cfg.agents[agent].model = model

    def set_complex(self, on: bool):
        self.cfg.complex_mode = on

    def reset(self, name: str | None = None):
        for a in self.agents.values():
            if name in (None, a.name):
                a.history.clear(); a.pending.clear(); a.context.clear()

    def attach(self, path: str) -> str:
        p = Path(self.repo, path) if not Path(path).is_absolute() else Path(path)
        content = p.read_text(encoding="utf-8", errors="replace")
        if len(content) > 60_000:
            content = content[:60_000] + "\n… [обрезано]"
        self.attachments.append(f"Файл `{path}`:\n```\n{content}\n```")
        return str(p)

    def _log(self, msg: Message):
        self._log_event(asdict(msg))

    def _log_event(self, record: dict):
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
