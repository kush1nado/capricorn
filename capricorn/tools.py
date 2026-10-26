"""Инструменты агентов: работа с файлами репозитория, git diff, запуск тестов.

Все пути — относительно корня репозитория; выход за его пределы и доступ к .git запрещены.
Модель не может запустить произвольную команду: run_tests исполняет только test_command из конфига.
"""
from __future__ import annotations

import asyncio
import fnmatch
import locale
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .config import Config
from .gitinfo import run as git_run
from .worktrees import Worktrees, is_writer

READ_MAX_LINES = 2000
READ_MAX_CHARS = 60_000
OUTPUT_MAX_CHARS = 20_000
LIST_LIMIT = 300
SEARCH_LIMIT = 100
SEARCH_MAX_FILE = 1_000_000
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", "dist", "build"}

WRITE_TOOLS = {"write_file", "edit_file", "sync_branch", "merge_work"}
WORKTREE_TOOLS = {"sync_branch", "merge_work"}               # только когда включены worktree
PEEK_TOOLS = {"list_files", "read_file", "search", "git_diff", "run_tests"}   # умеют смотреть чужой worktree
READ_ONLY = ["list_files", "read_file", "search", "git_diff"]
ROLE_TOOLS = {
    "architect": READ_ONLY,
    "manager": ["list_files", "read_file", "git_diff", "merge_work"],
    "coder": [*READ_ONLY, "write_file", "edit_file", "run_tests", "sync_branch"],
    "reviewer": [*READ_ONLY, "run_tests"],
    "tester": [*READ_ONLY, "write_file", "edit_file", "run_tests", "sync_branch"],
}


class ToolError(Exception):
    """Ошибка, которую стоит показать модели как есть."""


@dataclass
class ToolResult:
    content: str                 # уходит модели
    is_error: bool = False
    summary: str = ""            # коротко — для панели агента


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[["Toolbox", dict], Awaitable[ToolResult]]

    def as_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _clip(text: str, limit: int = OUTPUT_MAX_CHARS, tail: bool = False) -> str:
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    return (f"… [обрезано {cut} символов]\n" + text[-limit:]) if tail else (text[:limit] + f"\n… [обрезано {cut} символов]")


class Workspace:
    """Где работает инструмент: основной репозиторий или worktree агента."""

    def __init__(self, root: str | Path, cfg: Config, *, agent: str = "", toolbox: "Toolbox | None" = None,
                 base: str | None = None, label: str = ""):
        self.root = Path(root).resolve()
        self.cfg = cfg
        self.agent = agent
        self.toolbox = toolbox
        self.base = base            # для worktree: от какого коммита считать diff работы агента
        self.label = label          # «coder1» — чей это worktree (для сообщений)

    # ------------------------------------------------------------ пути
    def path(self, rel, *, must_exist: bool = True) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise ToolError("пустой путь")
        p = Path(rel)
        full = (p if p.is_absolute() else self.root / p).resolve()
        try:
            relp = full.relative_to(self.root)
        except ValueError:
            raise ToolError(f"путь вне репозитория: {rel}") from None
        if relp.parts and relp.parts[0] == ".git":
            raise ToolError("доступ к .git запрещён")
        if must_exist and not full.exists():
            raise ToolError(f"нет такого файла: {rel}")
        return full

    def rel(self, full: Path) -> str:
        return full.relative_to(self.root).as_posix() or "."

    async def files(self) -> list[str]:
        """Файлы репозитория: из git (с учётом .gitignore) или обходом каталога."""
        code, out = await git_run("git", "ls-files", "-co", "--exclude-standard", cwd=str(self.root))
        if code == 0:
            return sorted({line for line in out.splitlines() if line and (self.root / line).is_file()})
        found = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                found.append(Path(dirpath, fn).relative_to(self.root).as_posix())
        return sorted(found)

    async def scoped_files(self, path: str | None, glob: str | None) -> list[str]:
        prefix = ""
        if path not in (None, "", "."):
            full = self.path(path)
            prefix = self.rel(full)
            if full.is_file():
                return [prefix]
            prefix += "/"
        out = []
        for f in await self.files():
            if prefix and not f.startswith(prefix):
                continue
            if glob and not (fnmatch.fnmatch(f, glob) or fnmatch.fnmatch(f.rsplit("/", 1)[-1], glob)):
                continue
            out.append(f)
        return out


class Toolbox(Workspace):
    """Права агентов и диспетчер вызовов. Сам — рабочее пространство основного репозитория."""

    def __init__(self, root: str | Path, cfg: Config):
        super().__init__(root, cfg, toolbox=self)
        self.worktrees = Worktrees(self.root, cfg)

    # ------------------------------------------------------------ доступ
    def allowed(self, agent: str) -> list[str]:
        tc = self.cfg.tools
        if not tc.enabled:
            return []
        ac = self.cfg.agents[agent]
        base = "coder" if agent.startswith("coder") else agent
        names = ac.tools if ac.tools is not None else ROLE_TOOLS.get(base, [])
        out = []
        for n in names:
            if n not in TOOLS or n in out:
                continue
            if n in WRITE_TOOLS and not tc.write:
                continue
            if n == "run_tests" and not tc.run_tests:
                continue
            if n in WORKTREE_TOOLS and not self.worktrees.active:
                continue
            out.append(n)
        return out

    def writers(self) -> list[str]:
        return [a for a in self.cfg.agents if is_writer(a) and (a != "coder3" or self.cfg.complex_mode)]

    def peeks_worktrees(self, agent: str) -> bool:
        """Может ли агент заглядывать в чужие worktree (параметр worktree)."""
        return self.worktrees.active and not is_writer(agent)

    def spec(self, agent: str, name: str) -> dict:
        s = TOOLS[name].as_dict()
        if name in PEEK_TOOLS and self.peeks_worktrees(agent):
            params = dict(s["parameters"])
            params["properties"] = {**params["properties"], "worktree": {
                "type": "string", "enum": self.writers(),
                "description": "чей worktree смотреть (по умолчанию — основная ветка)"}}
            s["parameters"] = params
        return s

    def specs_for(self, agent: str) -> list[dict]:
        return [self.spec(agent, n) for n in self.allowed(agent)]

    async def workspace_for(self, agent: str, peek: str | None) -> Workspace:
        if peek:
            if peek not in self.writers():
                raise ToolError(f"нет worktree «{peek}». Есть: {', '.join(self.writers())}")
            if not await self.worktrees.exists(peek):
                raise ToolError(f"{peek} ещё ничего не делал — его worktree не создан")
            return Workspace(await self.worktrees.path(peek), self.cfg, agent=agent, toolbox=self,
                             base=await self.worktrees.base(peek), label=peek)
        if self.worktrees.active and is_writer(agent):
            wt = await self.worktrees.ensure(agent)
            return Workspace(wt, self.cfg, agent=agent, toolbox=self,
                             base=await self.worktrees.base(agent), label=agent)
        return Workspace(self.root, self.cfg, agent=agent, toolbox=self)

    async def run(self, agent: str, name: str, args) -> ToolResult:
        try:
            if name not in self.allowed(agent):
                raise ToolError(f"инструмент «{name}» тебе недоступен. Есть: {', '.join(self.allowed(agent)) or 'нет'}")
            if not isinstance(args, dict):
                raise ToolError("аргументы должны быть JSON-объектом")
            params = self.spec(agent, name)["parameters"]
            missing = [k for k in params["required"] if k not in args]
            if missing:
                raise ToolError(f"не хватает аргументов: {', '.join(missing)}")
            unknown = [k for k in args if k not in params["properties"]]
            if unknown:
                raise ToolError(f"лишние аргументы: {', '.join(unknown)}")
            args = dict(args)
            ws = await self.workspace_for(agent, args.pop("worktree", None))
            return await TOOLS[name].handler(ws, args)
        except ToolError as e:
            return ToolResult(f"Ошибка: {e}", True, f"ошибка: {e}")
        except Exception as e:  # noqa: BLE001 — модели нужен текст ошибки, а не падение хода
            return ToolResult(f"Ошибка: {type(e).__name__}: {e}", True, f"ошибка: {type(e).__name__}")


# ------------------------------------------------------------------ handlers

def _read_text(p: Path) -> str:
    with open(p, encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


async def _list_files(tb: Toolbox, a: dict) -> ToolResult:
    files = await tb.scoped_files(a.get("path"), a.get("pattern"))
    shown = files[:LIST_LIMIT]
    more = f"\n… и ещё {len(files) - LIST_LIMIT}" if len(files) > LIST_LIMIT else ""
    body = "\n".join(shown) + more if files else "(ничего не найдено)"
    return ToolResult(body, summary=f"{len(files)} файлов")


async def _read_file(tb: Toolbox, a: dict) -> ToolResult:
    p = tb.path(a["path"])
    if p.is_dir():
        raise ToolError(f"{a['path']} — каталог, используй list_files")
    lines = _read_text(p).replace("\r\n", "\n").splitlines(keepends=True)   # модели — единый \n
    start = max(1, int(a.get("start_line") or 1))
    count = min(int(a.get("max_lines") or READ_MAX_LINES), READ_MAX_LINES)
    chunk = "".join(lines[start - 1:start - 1 + count])
    end = min(len(lines), start - 1 + count)
    clipped = _clip(chunk, READ_MAX_CHARS)
    head = f"{tb.rel(p)} — строк: {len(lines)}"
    if start > 1 or end < len(lines):
        head += f", показаны {start}-{end}"
    return ToolResult(f"{head}\n{clipped}", summary=f"`{tb.rel(p)}` ({len(lines)} стр.)")


async def _write_file(tb: Toolbox, a: dict) -> ToolResult:
    p = tb.path(a["path"], must_exist=False)
    if p.is_dir():
        raise ToolError(f"{a['path']} — каталог")
    content = a["content"]
    if not isinstance(content, str):
        raise ToolError("content должен быть строкой")
    existed = p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    n = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    verb = "перезаписан" if existed else "создан"
    return ToolResult(f"{tb.rel(p)} {verb}, строк: {n}", summary=f"`{tb.rel(p)}` {verb}, {n} стр.")


async def _edit_file(tb: Toolbox, a: dict) -> ToolResult:
    p = tb.path(a["path"])
    old, new = a["old"], a["new"]
    if not isinstance(old, str) or not isinstance(new, str) or not old:
        raise ToolError("old и new — строки, old не пустой")
    text = _read_text(p)
    if old not in text and "\r\n" in text:          # модель обычно присылает \n, а файл с CRLF
        old, new = old.replace("\r\n", "\n").replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
    count = text.count(old)
    if count == 0:
        raise ToolError("фрагмент old не найден — перечитай файл через read_file")
    if count > 1:
        raise ToolError(f"фрагмент old встречается {count} раз — добавь контекста, чтобы он был уникален")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text.replace(old, new, 1))
    return ToolResult(f"{tb.rel(p)}: заменён 1 фрагмент", summary=f"`{tb.rel(p)}` изменён")


async def _search(tb: Toolbox, a: dict) -> ToolResult:
    try:
        rx = re.compile(a["pattern"])
    except re.error as e:
        raise ToolError(f"некорректное регулярное выражение: {e}") from None
    hits: list[str] = []
    total = 0
    for f in await tb.scoped_files(a.get("path"), a.get("glob")):
        full = tb.root / f
        try:
            if full.stat().st_size > SEARCH_MAX_FILE:
                continue
            text = _read_text(full)
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                total += 1
                if len(hits) < SEARCH_LIMIT:
                    hits.append(f"{f}:{i}: {line.strip()[:200]}")
    if not hits:
        return ToolResult("(совпадений нет)", summary="0 совпадений")
    more = f"\n… и ещё {total - len(hits)}" if total > len(hits) else ""
    return ToolResult("\n".join(hits) + more, summary=f"{total} совпадений")


async def _git_diff(tb: Toolbox, a: dict) -> ToolResult:
    code, _ = await git_run("git", "rev-parse", "--is-inside-work-tree", cwd=str(tb.root))
    if code != 0:
        raise ToolError("это не git-репозиторий")
    args = ["git", "diff", "--no-color"]
    if tb.base:                                   # worktree агента: вся его работа от точки ответвления
        args.append(tb.base)
    elif a.get("staged"):
        args.append("--cached")
    if a.get("path"):
        args += ["--", tb.rel(tb.path(a["path"], must_exist=False))]
    code, diff = await git_run(*args, cwd=str(tb.root), timeout=30)
    if code != 0:
        raise ToolError(diff[:500])
    parts = [diff or "(изменений в отслеживаемых файлах нет)"]
    if tb.label:
        parts.insert(0, f"Работа {tb.label} относительно основной ветки:")
    if tb.base or not a.get("staged"):
        _, untracked = await git_run("git", "ls-files", "--others", "--exclude-standard", cwd=str(tb.root))
        names = [u for u in untracked.splitlines() if u]
        if a.get("path"):
            prefix = tb.rel(tb.path(a["path"], must_exist=False))
            names = [u for u in names if u == prefix or u.startswith(prefix.rstrip("/") + "/")]
        if names:
            parts.append("Новые неотслеживаемые файлы (содержимое — через read_file):\n" + "\n".join(names))
    changed = diff.count("\ndiff --git") + diff.startswith("diff --git")
    return ToolResult(_clip("\n\n".join(parts)), summary=f"{changed} файлов в diff")


async def _sync_branch(ws: Workspace, a: dict) -> ToolResult:
    res = await ws.toolbox.worktrees.sync(ws.agent)
    if not res.ok:
        raise ToolError(res.text)
    return ToolResult(res.text, summary=res.text)


async def _merge_work(ws: Workspace, a: dict) -> ToolResult:
    target = a["agent"]
    if target not in ws.toolbox.writers():
        raise ToolError(f"нет такой ветки. Можно: {', '.join(ws.toolbox.writers())}")
    res = await ws.toolbox.worktrees.merge(target)
    if not res.ok:
        raise ToolError(res.text)
    return ToolResult(res.text, summary=res.text)


def _decode_output(raw: bytes) -> str:
    """UTF-8, а если не вышло — кодировка системы (не-Python раннеры на Windows). Переводы строк — LF."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode(locale.getpreferredencoding(False), errors="replace")
    return text.replace("\r\n", "\n")


def _test_argv(cmd: str | list[str]) -> list[str]:
    return list(cmd) if isinstance(cmd, list) else shlex.split(cmd)


async def _run_tests(tb: Toolbox, a: dict) -> ToolResult:
    tc = tb.cfg.tools
    argv = _test_argv(tc.test_command)
    if a.get("target"):
        argv.append(tb.rel(tb.path(a["target"])))
    # Python-инструменты (pytest и т.п.) — в UTF-8; иначе на Windows вывод идёт в cp1251/cp866
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(tb.root), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as e:
        raise ToolError(f"не удалось запустить {argv[0]}: {e}") from None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), tc.test_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"тесты не уложились в {tc.test_timeout} с и были остановлены") from None
    except asyncio.CancelledError:
        proc.kill()
        raise
    text = _decode_output(out)
    rc = proc.returncode
    return ToolResult(f"$ {shlex.join(argv)}\nкод выхода: {rc}\n{_clip(text, tail=True)}",
                      summary=f"код выхода {rc}")


TOOLS: dict[str, ToolSpec] = {s.name: s for s in [
    ToolSpec("list_files", "Список файлов репозитория (учитывает .gitignore).", _schema({
        "path": {"type": "string", "description": "каталог относительно корня, по умолчанию весь репозиторий"},
        "pattern": {"type": "string", "description": "glob по имени или пути, например *.py"},
    }, []), _list_files),
    ToolSpec("read_file", "Прочитать текстовый файл.", _schema({
        "path": {"type": "string"},
        "start_line": {"type": "integer", "description": "с какой строки (с 1)"},
        "max_lines": {"type": "integer", "description": f"сколько строк, максимум {READ_MAX_LINES}"},
    }, ["path"]), _read_file),
    ToolSpec("search", "Поиск по регулярному выражению в файлах репозитория.", _schema({
        "pattern": {"type": "string", "description": "регулярное выражение Python"},
        "path": {"type": "string", "description": "каталог или файл"},
        "glob": {"type": "string", "description": "фильтр файлов, например *.py"},
    }, ["pattern"]), _search),
    ToolSpec("git_diff", "Незакоммиченные изменения (git diff) и список новых файлов.", _schema({
        "path": {"type": "string"},
        "staged": {"type": "boolean", "description": "показать индекс (--cached)"},
    }, []), _git_diff),
    ToolSpec("write_file", "Создать или полностью перезаписать файл.", _schema({
        "path": {"type": "string"},
        "content": {"type": "string", "description": "полное содержимое файла"},
    }, ["path", "content"]), _write_file),
    ToolSpec("edit_file", "Заменить в файле один уникальный фрагмент текста.", _schema({
        "path": {"type": "string"},
        "old": {"type": "string", "description": "точный текст, встречающийся в файле ровно один раз"},
        "new": {"type": "string", "description": "чем заменить"},
    }, ["path", "old", "new"]), _edit_file),
    ToolSpec("run_tests", "Запустить тесты проекта (команда задана в конфиге).", _schema({
        "target": {"type": "string", "description": "файл или каталог с тестами, необязательно"},
    }, []), _run_tests),
    ToolSpec("sync_branch", "Подтянуть основную ветку в свою. При конфликтах — исправь файлы и вызови снова.",
             _schema({}, []), _sync_branch),
    ToolSpec("merge_work", "Влить работу агента (его ветку capricorn/<агент>) в основную ветку. "
                           "Вызывай после APPROVED от ревьюера.", _schema({
        "agent": {"type": "string", "description": "чью работу вливать: coder1, coder2, coder3, tester"},
    }, ["agent"]), _merge_work),
]}
