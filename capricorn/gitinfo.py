"""Сбор информации о git-репозитории (асинхронно, через CLI)."""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import dataclass, field


async def run(*args: str, cwd: str, timeout: float = 8) -> tuple[int, str]:
    """Команда с таймаутом. По таймауту или отмене процесс убивается — иначе он живёт дальше
    и держит пайпы (например, git-команда при выходе из приложения)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
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
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def _reap(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


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
