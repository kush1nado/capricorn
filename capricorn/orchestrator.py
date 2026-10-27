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
from .sessions import VERSION as SESSION_VERSION
from .sessions import SessionStore
from .worktrees import Outcome, is_writer

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
# модели копируют формат входящих «[от X → Y]:» (иногда в **жирном**) в начало своего ответа
ECHO_RE = re.compile(r"\A\s*(?:\*\*)?\[от [^\]\n]*\]:?(?:\*\*)?:?\s*")
DIRECTIVE_RE = re.compile(r"^[ \t]*(MERGE[ \t]+(\w+)|SYNC)[ \t]*$", re.M)


@dataclass
class Message:
    id: int
    sender: str
    to: list[str]
    content: str
    ts: float = field(default_factory=time.time)
    # team — общая шина; private — личный диалог с user; handoff — просьба подвести итог диалога;
    # summary — этот итог, разосланный команде
    kind: str = "team"

    def as_incoming(self) -> str:
        if self.kind == "private":
            return f"[лично от {self.sender}]: {self.content}"
        if self.kind == "summary":
            return f"[итог личного обсуждения {self.sender} с user]: {self.content}"
        return f"[от {self.sender} → {', '.join(self.to) or '—'}]: {self.content}"


PERSONAL = ("private", "handoff")     # такие сообщения агент обрабатывает отдельным ходом и раньше командных
HANDOFF_PROMPT = ("Личный диалог с пользователем закончен. Сформулируй итог для команды: какие решения "
                  "приняты, о чём договорились, что и кому теперь делать. Итог разошлют всем автоматически; "
                  "кому нужно действовать — упомяни через @ (он получит итог как задачу). Без приветствий.")


class Agent:
    def __init__(self, orch: "Orchestrator", name: str):
        self.orch = orch
        self.name = name
        self.history: list[dict] = []
        self.pending: list[Message] = []    # сообщения, требующие ответа
        self.context: list[Message] = []    # подслушанное (observe_all), без хода
        self.wake = asyncio.Event()
        self.gen_task: asyncio.Task | None = None
        self._stop_requested = False        # отмену хода запросил /stop, а не отмена самого цикла
        self.status = "idle"
        self.tokens_in = 0                  # новые входные токены
        self.tokens_out = 0
        self.tokens_cached = 0              # входные из кэша (повтор системного промпта и истории)
        self.turns = 0

    @property
    def cfg(self):
        return self.orch.cfg.agents[self.name]

    def set_status(self, status: str):
        self.status = status
        self.orch.emit("status", agent=self.name, status=status,
                       tokens_in=self.tokens_in, tokens_out=self.tokens_out, tokens_cached=self.tokens_cached)

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
            personal = [m for m in self.pending if m.kind in PERSONAL]
            if personal:                       # личный диалог — отдельным ходом, командное подождёт
                batch = personal
                self.pending = [m for m in self.pending if m.kind not in PERSONAL]
                ctx = []
            else:
                batch, self.pending = self.pending, []
                ctx, self.context = self.context, []
            ok = False
            self.gen_task = asyncio.create_task(self._turn(batch, ctx))
            try:
                ok = await self.gen_task
            except asyncio.CancelledError:
                if not self._stop_requested:  # отменяют сам цикл (выход из приложения) — не глотаем
                    self.gen_task.cancel()
                    raise
            finally:
                self._stop_requested = False
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
            native = getattr(provider, "native_tools", False)    # CLI-агент со своими инструментами
            tools = [] if native else self.orch.toolbox.specs_for(self.name)
            extra = {"workspace": await self.orch.native_workspace(self.name)} if native else {}
            system = system_prompt(self.name, self.orch.roster, self.orch.cfg.language,
                                   await self.orch.repo_context(), self.cfg.extra_prompt,
                                   tools=[t["name"] for t in tools],
                                   worktrees=self.orch.toolbox.worktrees.active,
                                   native=extra.get("workspace"))
            rounds = 0
            while True:
                last_round = rounds >= self.orch.cfg.tools.max_rounds
                text, calls = "", []
                async for chunk in provider.stream(
                        model=self.cfg.model_id, system=system, messages=self.history,
                        temperature=self.cfg.temperature, max_tokens=self.cfg.max_tokens,
                        agent=self.name, tools=tools or None,
                        tool_choice="none" if last_round else "auto", **extra):
                    if isinstance(chunk, dict):
                        u = chunk.get("usage") or {}
                        self.tokens_in += u.get("in", 0)
                        self.tokens_out += u.get("out", 0)
                        self.tokens_cached += u.get("cached", 0)
                        calls += chunk.get("tool_calls") or []
                        if chunk.get("status"):          # queued — ждёт слот CLI; thinking — дождался
                            self.set_status(chunk["status"])
                        if chunk.get("note"):            # действие CLI-агента его же инструментом
                            self.set_status("working")
                            sep = "\n" if display and not display.endswith("\n") else ""
                            show(f"{sep}» {chunk['note']}\n")
                            self.orch._log_event({"type": "tool", "agent": self.name, "note": chunk["note"]})
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
            self.orch.save()
            raise
        except Exception as e:  # noqa: BLE001 — любая ошибка провайдера/конфига идёт в UI, цикл живёт
            del self.history[base:]               # сохраняем чередование user/assistant
            self.pending = batch + self.pending   # можно повторить через /retry
            self.context = ctx + self.context
            self.orch.emit("stream_end", agent=self.name, msg_id=msg_id, text="", to=[])
            self.orch.emit("system", text=f"▲ {self.name}: {type(e).__name__}: {e}", level="error")
            self.set_status("error")
            self.orch.save()
            return False

        self.turns += 1
        self.history.append({"role": "assistant", "content": text})
        clean = ECHO_RE.sub("", THINK_RE.sub("", text).strip())
        kinds = {m.kind for m in batch}
        if "handoff" in kinds:                 # итог личного диалога — всей команде
            msg = self.orch.share_summary(self.name, clean, msg_id)
        elif "private" in kinds:               # ответ в личном канале — только пользователю
            msg = self.orch.deliver(Message(id=msg_id, sender=self.name, to=[USER], content=clean, kind="private"),
                                    emit_message=False)
        else:
            msg = self.orch.route(self.name, clean, msg_id=msg_id, emit_message=False)
        self.orch.emit("stream_end", agent=self.name, msg_id=msg_id, text=display, to=msg.to)
        self.orch.emit("message", msg=msg)
        self.set_status("idle")
        if msg.kind == "team":
            await self.orch.run_directives(self.name, clean)
        self.orch.save()
        return True

    def _trim(self):
        limit = self.orch.cfg.max_history
        if len(self.history) > limit:
            self.history = self.history[-limit:]
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)

    def cancel(self):
        if self.gen_task and not self.gen_task.done():
            self._stop_requested = True
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
        self.messages: list[Message] = []          # вся лента сессии — для сохранения и экспорта
        self._private_marks: dict[str, int] = {}   # где в ленте открыли личный канал с агентом
        self.store = SessionStore(repo)
        self._save_warned = False
        self.new_session()

    # ------------------------------------------------------------ сессии
    def new_session(self):
        self.session_id = self.store.new_id()
        self.created = time.time()
        self.messages = []
        self.log_path = self.store.dir(self.session_id) / "log.jsonl"

    def snapshot(self) -> dict:
        return {
            "version": SESSION_VERSION, "id": self.session_id, "repo": str(self.repo),
            "created": self.created, "updated": time.time(),
            "budget": self.budget, "complex_mode": self.cfg.complex_mode,
            "models": {n: a.model for n, a in self.cfg.agents.items()},
            "messages": [asdict(m) for m in self.messages],
            "agents": {n: {"history": a.history, "pending": [asdict(m) for m in a.pending],
                           "context": [asdict(m) for m in a.context], "tokens_in": a.tokens_in,
                           "tokens_cached": a.tokens_cached,
                           "tokens_out": a.tokens_out, "turns": a.turns}
                       for n, a in self.agents.items()},
            "cli_sessions": {name: dict(p.sessions) for name, p in self.providers.items() if hasattr(p, "sessions")},
        }

    def save(self):
        if not self.messages and not any(a.history or a.pending for a in self.agents.values()):
            return                      # пустую сессию (открыли и закрыли) не храним
        try:
            self.store.save(self.session_id, self.snapshot())
        except (OSError, TypeError, ValueError) as e:
            if not self._save_warned:
                self._save_warned = True
                self.emit("system", level="warn", text=f"▲ сессия не сохранилась: {e}")

    def restore(self, state: dict) -> list[str]:
        """Поднять сохранённую сессию. Очереди восстанавливаются без запуска ходов (/continue).
        Возвращает предупреждения (например, модель недоступна — оставлена текущая)."""
        warnings = []
        self.session_id = state["id"]
        self.created = state.get("created") or time.time()
        self.log_path = self.store.dir(self.session_id) / "log.jsonl"
        self.messages = [Message(**m) for m in state.get("messages") or []]
        self.budget = int(state.get("budget", self.cfg.budget))
        self.cfg.complex_mode = bool(state.get("complex_mode", self.cfg.complex_mode))
        for name, model in (state.get("models") or {}).items():
            if name not in self.cfg.agents:
                continue
            if model.split("/", 1)[0] in self.providers:
                self.cfg.agents[name].model = model
            else:
                warnings.append(f"{name}: провайдер модели {model} не настроен — оставлена {self.cfg.agents[name].model}")
        for name, st in (state.get("agents") or {}).items():
            a = self.agents.get(name)
            if a is None:
                continue
            a.history = list(st.get("history") or [])
            a.pending = [Message(**m) for m in st.get("pending") or []]
            a.context = [Message(**m) for m in st.get("context") or []]
            a.tokens_in, a.tokens_out, a.turns = st.get("tokens_in", 0), st.get("tokens_out", 0), st.get("turns", 0)
            a.tokens_cached = st.get("tokens_cached", 0)
            a.status = "idle"
        for pname, sessions in (state.get("cli_sessions") or {}).items():
            p = self.providers.get(pname)
            if p is not None and hasattr(p, "sessions"):
                p.sessions = dict(sessions)
        top = max([m.id for m in self.messages], default=0)
        self.ids = itertools.count(top + 1)
        return warnings

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
        self.messages.append(msg)
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
        msg = self.route(USER, text)
        self.save()
        return msg

    # ----------------------------------------------- личный канал с агентом
    def deliver(self, msg: Message, emit_message: bool = True, wake: bool = True) -> Message:
        """Сообщение с заранее известными адресатами — без разбора @упоминаний и подслушивания."""
        self.messages.append(msg)
        self._log(msg)
        if emit_message:
            self.emit("message", msg=msg)
        for name in msg.to:
            if name in self.agents:
                self.agents[name].pending.append(msg)
                if wake:
                    self.agents[name].wake.set()
        return msg

    def open_private(self, agent: str):
        self._private_marks[agent] = len(self.messages)

    def private_says(self, agent: str, text: str) -> Message:
        if agent not in self.agents:
            raise ValueError(f"нет агента «{agent}»")
        self.budget = max(self.budget, self.cfg.budget)
        self._budget_warned = False
        msg = self.deliver(Message(id=next(self.ids), sender=USER, to=[agent], content=text, kind="private"))
        self.save()
        return msg

    def end_private(self, agent: str) -> bool:
        """Окно агента закрыли. Если поговорили — агент подводит итог, и он уходит команде."""
        mark = self._private_marks.pop(agent, len(self.messages))
        talked = any(m.kind == "private" and m.sender == USER and m.to == [agent] for m in self.messages[mark:])
        if talked:
            self.deliver(Message(id=next(self.ids), sender=USER, to=[agent], content=HANDOFF_PROMPT, kind="handoff"))
            self.save()
        return talked

    def share_summary(self, agent: str, text: str, msg_id: int) -> Message:
        """Итог разосылается всем как контекст (без лишних ходов); менеджер получает его как задачу."""
        others = [n for n in self.roster if n != agent]
        msg = Message(id=msg_id, sender=agent, to=others, content=text, kind="summary")
        self.messages.append(msg)
        self._log(msg)
        # задачей — менеджеру и тем, кого упомянули (итог менеджера — обычно его поручения)
        actors = {n for n in self.parse_targets(agent, text) if n in self.agents}
        actors.add(self.cfg.default_target)
        for name in others:
            target = self.agents[name]
            if name in actors:
                target.pending.append(msg)
                target.wake.set()
            else:
                target.context.append(msg)
        return msg

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
        self.save()

    def set_complex(self, on: bool):
        self.cfg.complex_mode = on
        self.save()

    def reset(self, name: str | None = None):
        for a in self.agents.values():
            if name in (None, a.name):
                a.history.clear(); a.pending.clear(); a.context.clear()
        for p in self.providers.values():       # CLI-провайдеры забывают сессии
            p.reset(name)
        self.save()

    # ------------------------------------------------- CLI-агенты и git
    async def native_workspace(self, agent: str) -> dict:
        """Где работает CLI-агент и можно ли ему писать (как у наших инструментов: пишут кодеры и тестировщик)."""
        tc = self.cfg.tools
        write = tc.enabled and tc.write and is_writer(agent)
        ws = await self.toolbox.workspace_for(agent, None) if tc.enabled else self.toolbox
        wts = self.toolbox.worktrees
        provider = self.provider_for(agent)
        return {"path": str(ws.root), "write": write, "test_command": tc.test_command,
                "shell": write or getattr(provider, "readonly_shell", False),
                "branch": wts.branch(agent) if ws.label else "",
                "worktrees_dir": str(Path(".git") / "capricorn" / "worktrees") if wts.active else ""}

    async def ask_once(self, agent: str, system: str, prompt: str) -> str:
        """Разовый вопрос агенту вне диалога команды: его история и CLI-сессия не трогаются,
        токены идут в его счётчик. CLI-агент работает только на чтение."""
        provider = self.provider_for(agent)
        a, cfg = self.agents[agent], self.cfg.agents[agent]
        key = f"{agent}#once"                   # отдельная сессия у CLI-провайдера
        extra = ({"workspace": {"path": str(self.repo), "write": False, "shell": False, "test_command": ""}}
                 if getattr(provider, "native_tools", False) else {})
        text = ""
        try:
            async for chunk in provider.stream(model=cfg.model_id, system=system,
                                               messages=[{"role": "user", "content": prompt}],
                                               temperature=cfg.temperature, max_tokens=cfg.max_tokens,
                                               agent=key, **extra):
                if isinstance(chunk, str):
                    text += chunk
                else:
                    u = chunk.get("usage") or {}
                    a.tokens_in += u.get("in", 0)
                    a.tokens_out += u.get("out", 0)
                    a.tokens_cached += u.get("cached", 0)
        finally:
            provider.reset(key)
        return text

    def notify(self, agent: str, text: str, sender: str = "git"):
        """Служебное сообщение агенту (результат слияния и т.п.) — оно же будит его на ход."""
        msg = Message(id=next(self.ids), sender=sender, to=[agent], content=text)
        self.messages.append(msg)
        self._log(msg)
        self.emit("message", msg=msg)
        if agent in self.agents:
            self.agents[agent].pending.append(msg)
            self.agents[agent].wake.set()
        self.save()

    async def run_directives(self, agent: str, text: str):
        """MERGE <агент> (менеджер) и SYNC (кодер/тестировщик) — git-операции для CLI-агентов,
        которым песочница не даёт трогать .git. Вне блоков кода, отдельной строкой."""
        wts = self.toolbox.worktrees
        if not wts.active:
            return
        for m in DIRECTIVE_RE.finditer(CODE_RE.sub("", text)):
            target = m.group(2)
            try:
                if target is not None:
                    if agent != "manager":
                        continue
                    if target not in self.toolbox.writers():
                        res = Outcome(False, f"нет ветки агента «{target}». Есть: {', '.join(self.toolbox.writers())}")
                    else:
                        res = await wts.merge(target)
                else:
                    if not is_writer(agent):
                        continue
                    res = await wts.sync(agent)
            except Exception as e:  # noqa: BLE001 — git-ошибка не должна ронять цикл агента
                res = Outcome(False, f"{type(e).__name__}: {e}")
            self.emit("system", text=("» " if res.ok else "▲ ") + f"{m.group(1).split()[0]}: {res.text}",
                      level="info" if res.ok else "error")
            self.notify(agent, res.text)

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
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
