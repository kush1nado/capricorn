"""Сбор информации о git-репозитории (асинхронно, через CLI)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass, field


async def run(*args: str, cwd: str, timeout: float = 8, strip: bool = True,
              env: dict[str, str] | None = None, stderr: bool = True) -> tuple[int, str]:
    """Команда с таймаутом. По таймауту или отмене процесс убивается — иначе он живёт дальше
    и держит пайпы (например, git-команда при выходе из приложения).
    strip=False — для вывода, где важны ведущие пробелы (git status --porcelain).
    stderr=False — только stdout: в diff не должны попадать предупреждения git (например, про CRLF)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,       # не наследуем stdin интерфейса
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT if stderr else asyncio.subprocess.DEVNULL,
            env={**os.environ, **env} if env else None)
    except (FileNotFoundError, NotADirectoryError) as e:
        return 1, str(e)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        await _reap(proc)
        return 1, f"таймаут {timeout} с: {' '.join(args[:3])}"
    except asyncio.CancelledError:
        await _reap(proc)
        raise
    text = out.decode(errors="replace")
    return proc.returncode or 0, text.strip() if strip else text


async def _reap(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0"}     # push/pull без «введите пароль» — сразу ошибка, а не зависание


@dataclass
class FileChange:
    """Строка git status: x — индекс, y — рабочая копия."""
    path: str
    x: str
    y: str

    @property
    def untracked(self) -> bool:
        return self.x == "?"

    @property
    def staged(self) -> bool:
        return self.x not in (" ", "?")

    @property
    def unstaged(self) -> bool:
        return self.y not in (" ",) and not self.untracked

    @property
    def conflict(self) -> bool:
        return "U" in self.x + self.y or self.x + self.y in ("AA", "DD")


@dataclass
class GitSnapshot:
    ok: bool = False
    error: str = ""
    branch: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    staged: int = 0
    modified: int = 0
    untracked: int = 0
    commits: list[tuple[str, str, str, str]] = field(default_factory=list)  # hash, when, author, subject
    branches: list[str] = field(default_factory=list)
    mrs: list[str] = field(default_factory=list)
    mr_source: str = ""


class GitWatcher:
    def __init__(self, repo: str):
        self.repo = repo
        self._mr_cache: tuple[float, list[str], str] = (0, [], "")

    async def snapshot(self) -> GitSnapshot:
        s = GitSnapshot()
        code, out = await run("git", "rev-parse", "--is-inside-work-tree", cwd=self.repo)
        if code != 0:
            s.error = "не git-репозиторий"
            return s
        s.ok = True
        _, s.branch = await run("git", "branch", "--show-current", cwd=self.repo)
        if not s.branch:
            _, h = await run("git", "rev-parse", "--short", "HEAD", cwd=self.repo)
            s.branch = f"(detached {h})"

        code, st = await run("git", "status", "--porcelain=v1", "--branch", cwd=self.repo)
        for line in st.splitlines():
            if line.startswith("##"):
                if "..." in line:
                    s.upstream = line.split("...", 1)[1].split(" ", 1)[0]
                if "ahead " in line:
                    s.ahead = int(line.split("ahead ", 1)[1].split("]")[0].split(",")[0])
                if "behind " in line:
                    s.behind = int(line.split("behind ", 1)[1].split("]")[0])
            elif line.startswith("??"):
                s.untracked += 1
            else:
                if line[0] != " ":
                    s.staged += 1
                if len(line) > 1 and line[1] != " ":
                    s.modified += 1

        _, log = await run("git", "log", "-n", "12", "--pretty=format:%h\x1f%cr\x1f%an\x1f%s",
                           cwd=self.repo)
        for line in log.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                s.commits.append(tuple(parts))  # type: ignore[arg-type]

        _, br = await run("git", "for-each-ref", "--sort=-committerdate",
                          "--format=%(refname:short)", "refs/heads", cwd=self.repo)
        s.branches = [b for b in br.splitlines() if b]

        s.mrs, s.mr_source = await self._mrs()
        return s

    async def _mrs(self) -> tuple[list[str], str]:
        ts, cached, src = self._mr_cache
        if time.time() - ts < 60:
            return cached, src
        _, remote = await run("git", "remote", "get-url", "origin", cwd=self.repo)
        mrs: list[str] = []
        src = ""
        if "github" in remote and shutil.which("gh"):
            code, out = await run("gh", "pr", "list", "--limit", "6", "--json",
                                  "number,title,headRefName,isDraft", cwd=self.repo, timeout=10)
            if code == 0:
                try:
                    for pr in json.loads(out):
                        d = " [draft]" if pr.get("isDraft") else ""
                        mrs.append(f"#{pr['number']} {pr['headRefName']}{d} — {pr['title']}")
                    src = "gh"
                except json.JSONDecodeError:
                    pass
        elif "gitlab" in remote and shutil.which("glab"):
            code, out = await run("glab", "mr", "list", "--per-page", "6", cwd=self.repo, timeout=10)
            if code == 0:
                mrs = [l.strip() for l in out.splitlines() if l.strip().startswith("!")]
                src = "glab"
        self._mr_cache = (time.time(), mrs, src)
        return mrs, src

    async def commits(self, n: int = 60, ref: str = "HEAD") -> list[tuple[str, str, str, str]]:
        """(полный хэш, когда, автор, тема) — для полноэкранного Git."""
        code, out = await run("git", "log", "-n", str(n), "--pretty=format:%H\x1f%cr\x1f%an\x1f%s", ref,
                              cwd=self.repo)
        if code != 0:
            return []
        return [tuple(line.split("\x1f")) for line in out.splitlines() if line.count("\x1f") == 3]  # type: ignore[misc]

    async def show(self, commit: str, limit: int = 200_000) -> str:
        """Содержимое коммита: заголовок, статистика и diff."""
        if not commit or not all(c in "0123456789abcdefABCDEF" for c in commit):
            return f"некорректный хэш: {commit!r}"
        code, out = await run("git", "show", "--stat", "--patch", "--no-color", "--format=fuller", commit,
                              cwd=self.repo, timeout=20, stderr=False)
        if code != 0:
            return out[:500]
        return out if len(out) <= limit else out[:limit] + "\n… [обрезано]"

    # ------------------------------------------------- lazygit-операции
    async def changes(self) -> list[FileChange]:
        code, out = await run("git", "status", "--porcelain=v1", "-z", "-uall", cwd=self.repo, strip=False)
        if code != 0:
            return []
        parts = out.split("\0")
        result, i = [], 0
        while i < len(parts):
            entry = parts[i]
            i += 1
            if len(entry) < 4:
                continue
            x, y, path = entry[0], entry[1], entry[3:]
            if x in "RC":                   # переименование: следом идёт старый путь — пропускаем
                i += 1
            result.append(FileChange(path, x, y))
        return result

    async def _git(self, *args: str, timeout: float = 30, env: dict | None = None) -> tuple[bool, str]:
        code, out = await run("git", *args, cwd=self.repo, timeout=timeout, env=env)
        return code == 0, out

    async def stage(self, path: str) -> tuple[bool, str]:
        return await self._git("add", "--", path)

    async def unstage(self, path: str) -> tuple[bool, str]:
        ok, out = await self._git("restore", "--staged", "--", path)
        if not ok:                          # в репозитории ещё нет коммитов — restore не умеет
            ok, out = await self._git("rm", "--cached", "-q", "--", path)
        return ok, out

    async def stage_all(self) -> tuple[bool, str]:
        return await self._git("add", "-A")

    async def unstage_all(self) -> tuple[bool, str]:
        return await self._git("reset", "-q")

    async def discard(self, change: FileChange) -> tuple[bool, str]:
        """Откатить изменения файла. Новый (неотслеживаемый или только добавленный) файл удаляется."""
        if change.untracked:
            return await self._git("clean", "-f", "-q", "--", change.path)
        if change.x == "A":
            return await self._git("rm", "-f", "-q", "--", change.path)
        return await self._git("restore", "--source=HEAD", "--staged", "--worktree", "--", change.path)

    async def file_diff(self, change: FileChange, limit: int = 200_000) -> str:
        if change.untracked:
            try:
                with open(os.path.join(self.repo, change.path), encoding="utf-8", errors="replace") as f:
                    body = f.read(limit)
            except OSError as e:
                return str(e)
            return f"новый файл {change.path}\n" + "".join(f"+{line}\n" for line in body.splitlines())
        parts = []
        if change.staged:
            _, staged = await run("git", "diff", "--cached", "--no-color", "--", change.path, cwd=self.repo,
                                  timeout=20, stderr=False)
            parts.append(staged)
        if change.unstaged:
            _, unstaged = await run("git", "diff", "--no-color", "--", change.path, cwd=self.repo,
                                    timeout=20, stderr=False)
            parts.append(unstaged)
        text = "\n".join(p for p in parts if p)
        return text[:limit] if text else "(нет текстовых изменений)"

    async def commit(self, message: str) -> tuple[bool, str]:
        if not message.strip():
            return False, "пустое сообщение коммита"
        return await self._git("commit", "-m", message, timeout=600)     # с хуками репозитория — могут быть долгими

    async def create_branch(self, name: str) -> tuple[bool, str]:
        if not name.strip() or name.startswith("-"):
            return False, f"некорректное имя ветки: {name!r}"
        return await self._git("switch", "-c", name.strip())

    async def push(self) -> tuple[bool, str]:
        return await self._git("push", timeout=180, env=NO_PROMPT)

    async def pull(self) -> tuple[bool, str]:
        return await self._git("pull", "--ff-only", timeout=180, env=NO_PROMPT)

    async def fetch(self) -> tuple[bool, str]:
        return await self._git("fetch", "--prune", timeout=180, env=NO_PROMPT)

    async def switch(self, branch: str) -> tuple[bool, str]:
        code, out = await run("git", "switch", branch, cwd=self.repo)
        return code == 0, out

    async def context_for_agents(self) -> str:
        s = await self.snapshot()
        if not s.ok:
            return ""
        _, files = await run("git", "ls-files", cwd=self.repo)
        flist = files.splitlines()
        tree = "\n".join(flist[:80]) + (f"\n… и ещё {len(flist) - 80}" if len(flist) > 80 else "")
        commits = "\n".join(f"{h} {subj}" for h, _, _, subj in s.commits[:5])
        return (f"Ветка: {s.branch}. Изменено: {s.modified}, в индексе: {s.staged}, "
                f"новых: {s.untracked}.\nПоследние коммиты:\n{commits}\n\nФайлы:\n{tree}")
