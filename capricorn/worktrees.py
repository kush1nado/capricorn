"""Отдельный git worktree на каждого пишущего агента (кодеры, тестировщик).

Агент работает в своей ветке capricorn/<агент>, не мешая другим. Worktree лежат в
<git-dir>/capricorn/worktrees/<агент> — внутри .git, поэтому не видны в `git status`.
Менеджер вливает готовую работу в текущую ветку (merge), агент подтягивает её к себе (sync).
Служебные коммиты агентов делаются от имени «Capricorn <агент>» и без хуков репозитория.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config
from .gitinfo import run

BRANCH_PREFIX = "capricorn/"
MARKERS = ("<<<<<<< ", ">>>>>>> ")
TEMP_DIR = ".capricorn-tmp"      # временные файлы пишущего CLI-агента (TEMP), вне git


def is_writer(agent: str) -> bool:
    return agent.startswith("coder") or agent == "tester"


def identity(agent: str) -> list[str]:
    return ["-c", f"user.name=Capricorn {agent}", "-c", f"user.email={agent}@capricorn.local"]


@dataclass
class Outcome:
    ok: bool
    text: str
    conflicts: list[str] = field(default_factory=list)
    empty: bool = False          # слияние: новых коммитов нет, вливать было нечего


class Worktrees:
    def __init__(self, repo: str | Path, cfg: Config):
        self.repo = Path(repo).resolve()
        self.cfg = cfg
        self._active: bool | None = None
        # кто разрешает конфликт слияния в основной ветке: задан (TUI) — слияние остаётся открытым
        # и уходит человеку в окно Git; не задан — слияние отменяется, а кодера просят сделать sync
        self.on_conflict: Callable[[str, list[str]], None] | None = None

    # ---------------------------------------------------------- состояние
    @property
    def active(self) -> bool:
        """Включено в конфиге, это git-репозиторий и в нём есть хотя бы один коммит."""
        if not (self.cfg.tools.enabled and self.cfg.tools.write and self.cfg.tools.worktrees):
            return False
        if self._active is None:
            r = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=self.repo, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True)
            self._active = r.returncode == 0
        return self._active

    def branch(self, agent: str) -> str:
        return BRANCH_PREFIX + agent

    async def git_dir(self) -> Path:
        _, out = await run("git", "rev-parse", "--git-common-dir", cwd=str(self.repo))
        p = Path(out.strip())
        return (p if p.is_absolute() else self.repo / p).resolve()

    async def path(self, agent: str) -> Path:
        return await self.git_dir() / "capricorn" / "worktrees" / agent

    async def main_branch(self) -> str:
        code, out = await run("git", "symbolic-ref", "--short", "-q", "HEAD", cwd=str(self.repo))
        if code != 0 or not out:
            raise RuntimeError("основной репозиторий в detached HEAD — переключись на ветку")
        return out.strip()

    async def temp_dir(self, root: Path) -> Path:
        """Временная папка внутри рабочей папки агента: песочница Codex не пускает в системный TEMP,
        и pytest с tmp_path падает. Папка исключена через info/exclude — общий для всех worktree."""
        tmp = root / TEMP_DIR
        tmp.mkdir(exist_ok=True)
        exclude = await self.git_dir() / "info" / "exclude"
        line = f"/{TEMP_DIR}/"
        text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if line not in text.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a", encoding="utf-8", newline="") as f:
                f.write(("" if not text or text.endswith("\n") else "\n") + line + "\n")
        return tmp

    async def exists(self, agent: str) -> bool:
        return (await self.path(agent) / ".git").exists()

    # ----------------------------------------------------------- создание
    async def ensure(self, agent: str) -> Path:
        wt = await self.path(agent)
        if (wt / ".git").exists():
            return wt
        await run("git", "worktree", "prune", cwd=str(self.repo))
        code, _ = await run("git", "rev-parse", "--verify", "-q", self.branch(agent), cwd=str(self.repo))
        args = (["git", "worktree", "add", str(wt), self.branch(agent)] if code == 0
                else ["git", "worktree", "add", "-b", self.branch(agent), str(wt), "HEAD"])
        code, out = await run(*args, cwd=str(self.repo), timeout=60)
        if code != 0:
            raise RuntimeError(f"не удалось создать worktree для {agent}: {out[:400]}")
        return wt

    # ------------------------------------------------------------- работа
    async def _git(self, cwd: Path, *args: str, timeout: float = 60) -> tuple[int, str]:
        return await run("git", *args, cwd=str(cwd), timeout=timeout)

    async def commit_all(self, agent: str, message: str) -> bool:
        """Закоммитить всё незакоммиченное в worktree агента. True — если было что коммитить."""
        wt = await self.ensure(agent)
        await self._git(wt, "add", "-A")
        code, _ = await self._git(wt, "diff", "--cached", "--quiet")
        if code == 0:
            return False
        code, out = await self._git(wt, *identity(agent), "commit", "--no-verify", "-q", "-m", message)
        if code != 0:
            raise RuntimeError(f"коммит в {self.branch(agent)} не удался: {out[:400]}")
        return True

    async def base(self, agent: str) -> str:
        """Общий предок ветки агента и основной ветки — от него считается diff работы агента."""
        wt = await self.ensure(agent)
        code, out = await self._git(wt, "merge-base", "HEAD", await self.main_branch())
        if code != 0:
            raise RuntimeError(out[:300])
        return out.strip()

    async def changes(self, agent: str) -> list[tuple[str, str]]:
        """Что агент наменял относительно общего предка с основной веткой, включая незакоммиченное:
        [(статус A/M/D/?, путь)]. Ничего не коммитит."""
        if not await self.exists(agent):
            return []
        wt, base = await self.path(agent), await self.base(agent)
        _, out = await run("git", "diff", "--name-status", "--no-renames", base, cwd=str(wt), stderr=False)
        items = [(s[0], p) for s, p in (line.split("\t", 1) for line in out.splitlines() if "\t" in line)]
        _, new = await run("git", "ls-files", "--others", "--exclude-standard", cwd=str(wt), stderr=False)
        return items + [("?", p) for p in new.splitlines() if p]

    async def file_diff(self, agent: str, status: str, path: str, limit: int = 200_000) -> str:
        wt = await self.path(agent)
        if status == "?":
            try:
                body = (wt / path).read_text(encoding="utf-8", errors="replace")[:limit]
            except OSError as e:
                return str(e)
            return f"новый файл {path}\n" + "".join(f"+{line}\n" for line in body.splitlines())
        _, out = await run("git", "diff", "--no-color", await self.base(agent), "--", path,
                           cwd=str(wt), timeout=20, stderr=False)
        return out[:limit] if out else "(нет текстовых изменений)"

    async def merge(self, agent: str) -> Outcome:
        """Влить работу агента в текущую ветку основного репозитория."""
        if not await self.exists(agent):
            return Outcome(False, f"у {agent} ещё нет рабочей ветки — он ничего не делал")
        main = await self.main_branch()
        code, _ = await self._git(self.repo, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        if code == 0:
            return Outcome(False, f"в {main} не завершено слияние — конфликт разрешает пользователь в окне Git. "
                                  "Подожди, пока он закончит, и повтори слияние.")
        await self.commit_all(agent, f"{agent}: работа для слияния")
        code, ahead = await self._git(self.repo, "rev-list", "--count", f"{main}..{self.branch(agent)}")
        if code == 0 and ahead.strip() == "0":
            return Outcome(True, f"в {self.branch(agent)} нет новых коммитов относительно {main} — сливать нечего",
                           empty=True)
        code, out = await self._git(self.repo, *identity("manager"), "merge", "--no-ff", "--no-verify",
                                    "-m", f"Merge {self.branch(agent)}: работа {agent} (Capricorn)",
                                    self.branch(agent))
        if code == 0:
            return Outcome(True, f"{self.branch(agent)} влита в {main}")
        _, conflicts = await self._git(self.repo, "diff", "--name-only", "--diff-filter=U")
        files = [f for f in conflicts.splitlines() if f]
        if files and self.on_conflict is not None:
            self.on_conflict(agent, files)
            return Outcome(False, f"конфликт при слиянии {self.branch(agent)} в {main}: {', '.join(files)}. "
                                  "Слияние оставлено открытым — пользователь разрешает его в окне Git. "
                                  "Другие ветки пока не вливай, о завершении придёт сообщение.", files)
        await self._git(self.repo, "merge", "--abort")
        if files:
            return Outcome(False, f"конфликт при слиянии в {main}, слияние отменено. Файлы: {', '.join(files)}. "
                                  f"Попроси @{agent} выполнить sync_branch и разрешить конфликты у себя.")
        return Outcome(False, f"слияние не удалось: {out[:400]}")

    async def sync(self, agent: str) -> Outcome:
        """Подтянуть основную ветку в ветку агента. Повторный вызов завершает разрешение конфликтов."""
        wt = await self.ensure(agent)
        code, merge_head = await self._git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        if code == 0 and merge_head:
            left = [f for f in await self._unresolved(wt)]
            if left:
                return Outcome(False, "в файлах остались маркеры конфликта: " + ", ".join(left))
            await self._git(wt, "add", "-A")
            code, out = await self._git(wt, *identity(agent), "commit", "--no-verify", "-q", "--no-edit")
            return Outcome(code == 0, "конфликты разрешены, слияние завершено" if code == 0 else out[:400])
        main = await self.main_branch()
        await self.commit_all(agent, f"{agent}: промежуточный коммит перед sync")
        code, out = await self._git(wt, *identity(agent), "merge", "--no-edit", "--no-verify", main)
        if code == 0:
            return Outcome(True, f"ветка обновлена из {main}")
        _, conflicts = await self._git(wt, "diff", "--name-only", "--diff-filter=U")
        files = [f for f in conflicts.splitlines() if f]
        if not files:
            await self._git(wt, "merge", "--abort")
            return Outcome(False, f"не удалось подтянуть {main}: {out[:400]}")
        return Outcome(False, f"конфликты в файлах: {', '.join(files)}. Исправь их (убери маркеры <<<<<<< ======= >>>>>>>) "
                              f"и снова вызови sync_branch.")

    async def undo_merge(self, agent: str) -> Outcome:
        """Откатить последнее слияние агента отдельным revert-коммитом."""
        await self.main_branch()
        code, out = await self._git(self.repo, "log", f"--grep=^Merge {self.branch(agent)}:",
                                    "--merges", "--format=%H", "-1")
        if code != 0:
            return Outcome(False, f"не удалось найти слияние: {out[:400]}")
        commit = out.strip()
        if not commit:
            return Outcome(False, f"нет слияния {self.branch(agent)} в текущей ветке")
        code, out = await run("git", "status", "--porcelain", cwd=str(self.repo), stderr=False)
        if code != 0 or out.strip():
            return Outcome(False, "перед откатом сохрани или убери незакоммиченные изменения")
        for ref in ("MERGE_HEAD", "REVERT_HEAD", "CHERRY_PICK_HEAD"):
            code, _ = await self._git(self.repo, "rev-parse", "-q", "--verify", ref)
            if code == 0:
                return Outcome(False, "перед откатом заверши текущую операцию Git")
        code, out = await self._git(self.repo, *identity("manager"), "revert", "-m", "1",
                                    "--no-edit", "--no-commit", commit)
        if code != 0:
            _, conflicts = await self._git(self.repo, "diff", "--name-only", "--diff-filter=U")
            abort_code, abort_out = await self._git(self.repo, "revert", "--abort")
            if abort_code != 0:
                return Outcome(False, f"откат не удался: {out[:400]}. Отмена отката: {abort_out[:400]}")
            if conflicts.strip():
                return Outcome(False, "откат дал конфликт, откат отменён. Файлы: "
                               + ", ".join(conflicts.splitlines()))
            return Outcome(False, f"откат не удался: {out[:400]}")
        code, out = await self._git(self.repo, *identity("manager"), "commit", "--no-verify",
                                    "--no-edit")
        if code != 0:
            cleanup_code, cleanup_out = await self._git(self.repo, "reset", "--merge", "HEAD")
            if cleanup_code != 0:
                return Outcome(False, f"не удалось создать коммит отката: {out[:400]}. "
                               f"Не удалось убрать изменения отката: {cleanup_out[:400]}")
        return Outcome(code == 0, f"слияние {self.branch(agent)} откачено" if code == 0
                       else f"не удалось создать коммит отката: {out[:400]}")

    async def _unresolved(self, wt: Path) -> list[str]:
        _, out = await self._git(wt, "diff", "--name-only", "--diff-filter=U")
        left = []
        for f in (x for x in out.splitlines() if x):
            try:
                text = (wt / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(line.startswith(MARKERS) for line in text.splitlines()):
                left.append(f)
        return left

    # ------------------------------------------------------------ обзор
    async def status(self, agents: list[str]) -> list[str]:
        if not self.active:
            return []
        main = await self.main_branch()
        lines = []
        for a in agents:
            if not is_writer(a):
                continue
            if not await self.exists(a):
                lines.append(f"{a:8} — ещё не создан")
                continue
            wt = await self.path(a)
            _, ahead = await self._git(self.repo, "rev-list", "--count", f"{main}..{self.branch(a)}")
            _, dirty = await self._git(wt, "status", "--porcelain")
            n = len([x for x in dirty.splitlines() if x])
            lines.append(f"{a:8} {self.branch(a)}: коммитов не в {main}: {ahead.strip() or 0}, "
                         f"незакоммиченных файлов: {n}")
        return lines

    async def prune(self, agents: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
        """Убрать worktree и ветки, полностью влитые в основную ветку и без незакоммиченных изменений.
        Возвращает (убранные, [(агент, почему не вышло)]): файлы из песочницы Codex бывают недоступны."""
        main = await self.main_branch()
        removed, failed = [], []
        for a in agents:
            if not is_writer(a) or not await self.exists(a):
                continue
            wt = await self.path(a)
            _, dirty = await self._git(wt, "status", "--porcelain")
            _, ahead = await self._git(self.repo, "rev-list", "--count", f"{main}..{self.branch(a)}")
            if dirty.strip() or ahead.strip() != "0":
                continue
            code, out = await self._git(self.repo, "worktree", "remove", str(wt))
            if code == 0:
                await self._git(self.repo, "branch", "-d", self.branch(a))
                removed.append(a)
            else:
                failed.append((a, (out.strip().splitlines() or ["git worktree remove не удался"])[-1][:300]))
        return removed, failed
