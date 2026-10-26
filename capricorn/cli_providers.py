"""Провайдеры-агенты: Codex CLI и Claude Code CLI — работают под подпиской пользователя.

В отличие от API-провайдеров, у них свои инструменты (чтение/правка файлов, shell), поэтому
инструменты Capricorn им не передаются (native_tools = True). Оркестратор сообщает рабочую папку
(worktree агента или основной репозиторий) и можно ли писать. У каждого агента Capricorn — своя
сессия CLI: первый ход открывает её (с системным промптом), дальше — resume только с новым
сообщением; память разговора хранит сам CLI.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
from pathlib import Path
from typing import AsyncIterator

from .providers import Provider, ProviderError

STDERR_TAIL = 600


_WRAPPED_RE = re.compile(r"""(?:powershell(?:\.exe)?"?\s+-Command|(?:^|[\\/\s])(?:ba)?sh\s+-l?c)\s+(['"])(.*)\1\s*$""",
                         re.I | re.S)


def command_text(cmd: str) -> str:
    """Codex оборачивает команды в powershell -Command '…' / bash -lc '…' — показываем суть."""
    m = _WRAPPED_RE.search(cmd or "")
    return m.group(2) if m else (cmd or "")


def _short(text: str, n: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F", stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


class AgentCLIProvider(Provider):
    native_tools = True
    label = "CLI"
    readonly_shell = False      # может ли агент в режиме «только чтение» выполнять команды (git diff и т.п.)

    def __init__(self, name: str, conf: dict):
        super().__init__(name, conf)
        self.sessions: dict[str, str] = {}
        # параллельные запуски CLI упираются в лимиты подписки — ограничиваем
        self.max_parallel = max(1, int(conf.get("max_parallel", 2)))
        self._sem: asyncio.Semaphore | None = None

    async def stream(self, *, model, system, messages, temperature, max_tokens, agent,
                     tools=None, tool_choice="auto", workspace: dict | None = None):
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_parallel)
        queued = self._sem.locked()
        if queued:
            yield {"status": "queued"}          # ждём свободный слот — видно в панели агента
        async with self._sem:
            if queued:
                yield {"status": "thinking"}
            inner = self._stream(model=model, system=system, messages=messages, temperature=temperature,
                                 max_tokens=max_tokens, agent=agent, workspace=workspace)
            async with contextlib.aclosing(inner):
                async for chunk in inner:
                    yield chunk

    async def _stream(self, *, model, system, messages, temperature, max_tokens, agent,
                      workspace: dict | None = None):
        raise NotImplementedError
        yield  # pragma: no cover

    # ----------------------------------------------------------- настройка
    def reset(self, agent: str | None = None) -> None:
        if agent is None:
            self.sessions.clear()
        else:
            self.sessions.pop(agent, None)

    def discover(self) -> str | None:
        raise NotImplementedError

    def executable(self) -> list[str]:
        cmd = self.conf.get("command")
        if cmd:
            return list(cmd) if isinstance(cmd, list) else shlex.split(cmd, posix=os.name != "nt")
        found = self.discover()
        if not found:
            raise ProviderError(f"{self.label} не найден. Укажи путь: providers.{self.name}.command")
        return [found]

    @staticmethod
    def prompt(system: str, messages: list[dict], resumed: bool) -> str:
        last = messages[-1].get("content", "") if messages else ""
        return last if resumed else f"{system}\n\n---\n\n{last}"

    # -------------------------------------------------------------- запуск
    async def _events(self, argv: list[str], stdin_text: str, cwd: str) -> AsyncIterator[dict]:
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
        kw = {"start_new_session": True} if os.name != "nt" else {}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024, **kw)
        except OSError as e:
            raise ProviderError(f"{self.label}: не удалось запустить {argv[0]}: {e}") from None
        stderr_chunks: list[bytes] = []

        async def drain_stderr():
            assert proc.stderr is not None
            async for line in proc.stderr:
                stderr_chunks.append(line)

        err_task = asyncio.create_task(drain_stderr())
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(stdin_text.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
            await proc.wait()
            await err_task
        except BaseException:
            await _kill_tree(proc)
            err_task.cancel()
            raise
        if proc.returncode:
            tail = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()[-STDERR_TAIL:]
            raise ProviderError(f"{self.label}: код выхода {proc.returncode}. {tail}")


# ----------------------------------------------------------------- codex ----

class CodexProvider(AgentCLIProvider):
    """codex exec --json: события thread.started / item.* / turn.completed|failed."""
    label = "Codex CLI"
    readonly_shell = True       # -s read-only: команды выполняются, запись запрещена песочницей ОС

    def discover(self) -> str | None:
        found = shutil.which("codex")
        if found:
            return found
        if os.name == "nt":
            # десктопное приложение Codex кладёт CLI сюда; рядом нужен code-mode host
            base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
            cands = [p for p in base.glob("*/codex.exe") if (p.parent / "codex-code-mode-host.exe").exists()]
            if cands:
                return str(max(cands, key=lambda p: p.stat().st_mtime))
        return None

    def argv(self, *, model: str, workdir: str, write: bool, session: str | None) -> list[str]:
        args = [*self.executable(), "exec", "--json", "--skip-git-repo-check", "-C", workdir,
                "-s", "workspace-write" if write else "read-only"]
        if model and model != "default":
            args += ["-m", model]
        if session:
            args += ["resume", session]
        return [*args, "-"]

    async def _stream(self, *, model, system, messages, temperature, max_tokens, agent,
                     tools=None, tool_choice="auto", workspace: dict | None = None):
        ws = workspace or {"path": os.getcwd(), "write": False}
        session = self.sessions.get(agent)
        argv = self.argv(model=model, workdir=ws["path"], write=ws["write"], session=session)
        said = False
        events = self._events(argv, self.prompt(system, messages, bool(session)), ws["path"])
        async with contextlib.aclosing(events):
            async for ev in events:
                t = ev.get("type")
                if t == "thread.started" and ev.get("thread_id"):
                    self.sessions[agent] = ev["thread_id"]
                elif t == "item.completed":
                    item = ev.get("item") or {}
                    kind = item.get("type")
                    if kind == "agent_message" and item.get("text"):
                        yield ("\n\n" if said else "") + item["text"].strip()
                        said = True
                    elif kind == "command_execution":
                        yield {"note": f"shell: {_short(command_text(item.get('command', '')))} → {item.get('exit_code')}"}
                    elif kind == "file_change":
                        paths = [c.get("path", "?") for c in item.get("changes") or []]
                        yield {"note": "правка: " + _short(", ".join(paths) or "файлы")}
                    elif kind == "error" and item.get("message"):
                        yield {"note": "ошибка: " + _short(item["message"], 140)}
                elif t == "turn.completed":
                    u = ev.get("usage") or {}
                    yield {"usage": {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0)}}
                elif t in ("turn.failed", "error"):
                    msg = (ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict) else ev.get("message")
                    raise ProviderError(f"Codex: {msg or ev}")


# ----------------------------------------------------------- claude code ----

READ_TOOLS = ["Read", "Glob", "Grep", "LS"]
WRITE_TOOLS = ["Edit", "MultiEdit", "Write"]
HIDDEN_TOOLS = {"TodoWrite"}            # внутреннее планирование Claude — в панели это шум


class ClaudeCodeProvider(AgentCLIProvider):
    """claude -p --output-format stream-json: события system/init, assistant, result."""
    label = "Claude Code CLI"

    def discover(self) -> str | None:
        return shutil.which("claude")

    def argv(self, *, model: str, write: bool, session: str | None, test_command) -> list[str]:
        allowed = list(READ_TOOLS)
        if write:
            allowed += WRITE_TOOLS
            cmd = test_command if isinstance(test_command, str) else " ".join(test_command)
            if cmd:
                allowed.append(f"Bash({cmd}:*)")          # shell — только команда тестов
        args = [*self.executable(), "-p", "--output-format", "stream-json", "--verbose",
                "--allowedTools", ",".join(allowed),
                "--permission-mode", "acceptEdits" if write else "default"]
        if not write:
            args += ["--disallowedTools", ",".join([*WRITE_TOOLS, "Bash", "NotebookEdit"])]
        if model and model != "default":
            args += ["--model", model]
        if session:
            args += ["--resume", session]
        return args

    async def _stream(self, *, model, system, messages, temperature, max_tokens, agent,
                     tools=None, tool_choice="auto", workspace: dict | None = None):
        ws = workspace or {"path": os.getcwd(), "write": False}
        session = self.sessions.get(agent)
        argv = self.argv(model=model, write=ws["write"], session=session,
                         test_command=ws.get("test_command", ""))
        said = False
        events = self._events(argv, self.prompt(system, messages, bool(session)), ws["path"])
        async with contextlib.aclosing(events):
            async for ev in events:
                t = ev.get("type")
                if ev.get("session_id"):
                    self.sessions[agent] = ev["session_id"]
                if t == "assistant":
                    for block in (ev.get("message") or {}).get("content") or []:
                        if block.get("type") == "text" and block.get("text", "").strip():
                            yield ("\n\n" if said else "") + block["text"].strip()
                            said = True
                        elif block.get("type") == "tool_use" and block.get("name") not in HIDDEN_TOOLS:
                            inp = block.get("input") or {}
                            target = inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("command") or ""
                            yield {"note": f"{block.get('name')}: {_short(target)}"}
                elif t == "result":
                    if ev.get("is_error"):
                        raise ProviderError(f"Claude Code: {_short(ev.get('result') or ev.get('subtype'), 300)}")
                    u = ev.get("usage") or {}
                    yield {"usage": {"in": u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0),
                                     "out": u.get("output_tokens", 0)}}

