"""Отдельный worktree у каждого пишущего агента: изоляция, просмотр, слияние, конфликты."""
import sys

import pytest

from capricorn.config import load_config
from capricorn.tools import Toolbox
from capricorn.worktrees import Worktrees
from helpers import git, init_repo


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    init_repo(r)
    (r / "app.py").write_bytes(b"def f():\n    return 1\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def box(repo):
    return Toolbox(repo, load_config())


async def ok(box, who, name, /, **args):
    res = await box.run(who, name, args)
    assert not res.is_error, res.content
    return res


async def err(box, who, name, /, **args):
    res = await box.run(who, name, args)
    assert res.is_error, res.content
    return res.content


def status(repo) -> str:
    return git(repo, "status", "--porcelain")


# ---------------------------------------------------------------- включение

def test_active_only_for_git_repo_with_commits(tmp_path, repo):
    assert Worktrees(repo, load_config()).active
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not Worktrees(plain, load_config()).active
    empty = tmp_path / "empty"
    init_repo(empty)
    assert not Worktrees(empty, load_config()).active          # без коммитов worktree не создать
    cfg = load_config()
    cfg.tools.worktrees = False
    assert not Worktrees(repo, cfg).active
    cfg = load_config()
    cfg.tools.write = False
    assert not Worktrees(repo, cfg).active


def test_tools_and_specs_depend_on_role(box):
    assert "sync_branch" in box.allowed("coder1") and "merge_work" not in box.allowed("coder1")
    assert "merge_work" in box.allowed("manager") and "sync_branch" not in box.allowed("manager")
    rev = {s["name"]: s for s in box.specs_for("reviewer")}
    assert rev["git_diff"]["parameters"]["properties"]["worktree"]["enum"] == ["coder1", "coder2", "tester"]
    assert "worktree" not in rev["search"]["parameters"]["required"]
    coder = {s["name"]: s for s in box.specs_for("coder1")}
    assert "worktree" not in coder["read_file"]["parameters"]["properties"]
    box.cfg.complex_mode = True
    assert "coder3" in box.specs_for("reviewer")[0]["parameters"]["properties"]["worktree"]["enum"]


# ----------------------------------------------------------------- изоляция

async def test_writer_works_in_own_worktree(box, repo):
    await ok(box, "coder1", "write_file", path="new.py", content="x = 1\n")
    await ok(box, "coder1", "edit_file", path="app.py", old="return 1", new="return 10")
    wt = await box.worktrees.path("coder1")
    assert (wt / "new.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (repo / "new.py").exists()
    assert (repo / "app.py").read_bytes() == b"def f():\n    return 1\n"
    assert status(repo) == ""                                  # worktree внутри .git — не виден
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "capricorn/coder1"
    assert "нет такого файла" in await err(box, "coder2", "read_file", path="new.py")


async def test_writer_cannot_escape_worktree(box):
    await ok(box, "coder1", "write_file", path="a.txt", content="a")
    assert "вне репозитория" in await err(box, "coder2", "write_file", path="../coder1/a.txt", content="pwn")
    assert "запрещён" in await err(box, "coder1", "read_file", path=".git")
    assert "вне репозитория" in await err(box, "reviewer", "read_file", path="../../app.py")


async def test_reviewer_peeks_into_worktree(box, repo):
    await ok(box, "coder1", "edit_file", path="app.py", old="return 1", new="return 2")
    await ok(box, "coder1", "write_file", path="extra.py", content="y = 2\n")
    diff = (await ok(box, "reviewer", "git_diff", worktree="coder1")).content
    assert diff.startswith("Работа coder1 относительно основной ветки:")
    assert "+    return 2" in diff and "extra.py" in diff
    main_diff = (await ok(box, "reviewer", "git_diff")).content
    assert "return 2" not in main_diff
    assert "y = 2" in (await ok(box, "reviewer", "read_file", path="extra.py", worktree="coder1")).content
    assert "нет такого файла" in await err(box, "reviewer", "read_file", path="extra.py")
    assert "extra.py" in (await ok(box, "reviewer", "list_files", worktree="coder1")).content


async def test_peek_errors(box):
    assert "ещё ничего не делал" in await err(box, "reviewer", "git_diff", worktree="coder2")
    assert "лишние аргументы: worktree" in await err(box, "coder1", "read_file", path="app.py", worktree="coder2")
    res = await box.run("reviewer", "git_diff", {"worktree": "manager"})
    assert res.is_error and "нет worktree" in res.content


async def test_run_tests_in_worktree(box, repo):
    box.cfg.tools.test_command = [sys.executable, "-c", "import os; print('CWD', os.getcwd())"]
    await ok(box, "coder1", "write_file", path="t.py", content="")
    wt = await box.worktrees.path("coder1")
    assert str(wt) in (await ok(box, "coder1", "run_tests")).content
    assert str(wt) in (await ok(box, "reviewer", "run_tests", worktree="coder1")).content
    assert f"CWD {repo}" in (await ok(box, "reviewer", "run_tests")).content


# ------------------------------------------------------------------ слияние

async def test_merge_work_brings_changes_to_main(box, repo):
    await ok(box, "coder1", "write_file", path="feature.py", content="def feature():\n    return 42\n")
    res = await ok(box, "manager", "merge_work", agent="coder1")
    assert "влита в main" in res.content
    assert (repo / "feature.py").read_text(encoding="utf-8") == "def feature():\n    return 42\n"
    assert status(repo) == ""
    log = git(repo, "log", "--format=%an|%s", "-3")
    assert log.splitlines()[0].startswith("Capricorn manager|Merge capricorn/coder1")
    assert "Capricorn coder1|coder1: работа для слияния" in log
    again = await ok(box, "manager", "merge_work", agent="coder1")
    assert "сливать нечего" in again.content


async def test_merge_errors(box, repo):
    assert "ничего не делал" in await err(box, "manager", "merge_work", agent="coder2")
    assert "нет такой ветки" in await err(box, "manager", "merge_work", agent="reviewer")
    await ok(box, "coder1", "write_file", path="x.py", content="x")
    git(repo, "checkout", "-q", "--detach")
    assert "detached HEAD" in await err(box, "manager", "merge_work", agent="coder1")


async def test_conflict_merge_then_sync_and_resolve(box, repo):
    await ok(box, "coder1", "edit_file", path="app.py", old="return 1", new="return 'один'")
    await ok(box, "coder2", "edit_file", path="app.py", old="return 1", new="return 'два'")
    await ok(box, "manager", "merge_work", agent="coder1")

    msg = await err(box, "manager", "merge_work", agent="coder2")
    assert "конфликт" in msg and "app.py" in msg and "sync_branch" in msg
    assert status(repo) == ""                                   # слияние отменено, main чистый
    assert "один" in (repo / "app.py").read_text(encoding="utf-8")

    msg = await err(box, "coder2", "sync_branch")
    assert "конфликты в файлах: app.py" in msg
    wt2 = await box.worktrees.path("coder2")
    assert "<<<<<<< " in (wt2 / "app.py").read_text(encoding="utf-8")
    assert "маркеры конфликта: app.py" in await err(box, "coder2", "sync_branch")

    await ok(box, "coder2", "write_file", path="app.py", content="def f():\n    return 'один и два'\n")
    assert "конфликты разрешены" in (await ok(box, "coder2", "sync_branch")).content
    await ok(box, "manager", "merge_work", agent="coder2")
    assert (repo / "app.py").read_text(encoding="utf-8") == "def f():\n    return 'один и два'\n"
    assert status(repo) == ""


async def test_sync_brings_main_into_branch(box, repo):
    await ok(box, "coder1", "write_file", path="a.py", content="a")
    await ok(box, "coder2", "write_file", path="b.py", content="b")
    await ok(box, "manager", "merge_work", agent="coder1")
    assert "обновлена из main" in (await ok(box, "coder2", "sync_branch")).content
    wt2 = await box.worktrees.path("coder2")
    assert (wt2 / "a.py").exists() and (wt2 / "b.py").exists()


async def test_agent_commits_skip_repo_hooks(box, repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    await ok(box, "coder1", "write_file", path="h.py", content="h")
    await ok(box, "manager", "merge_work", agent="coder1")
    assert (repo / "h.py").exists()


# ---------------------------------------------------------------- обзор

async def test_status_and_prune(box, repo):
    wts = box.worktrees
    agents = ["coder1", "coder2", "tester", "reviewer"]
    assert (await wts.status(agents))[0].startswith("coder1   — ещё не создан")
    await ok(box, "coder1", "write_file", path="p.py", content="p")
    await ok(box, "coder2", "write_file", path="q.py", content="q")
    lines = await wts.status(agents)
    assert "незакоммиченных файлов: 1" in lines[0] and len(lines) == 3
    await ok(box, "manager", "merge_work", agent="coder1")
    assert await wts.prune(agents) == ["coder1"]                # coder2 не влит — его не трогаем
    assert not await wts.exists("coder1")
    assert "capricorn/coder1" not in git(repo, "branch", "--list", "capricorn/*")
    assert await wts.exists("coder2")
    await ok(box, "coder1", "write_file", path="again.py", content="1")   # пересоздаётся при надобности
    assert await wts.exists("coder1")
