from capricorn.gitinfo import GitWatcher
from helpers import git, init_repo


def commit(repo, name, text, msg):
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-qm", msg)


async def test_not_a_repo(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    s = await GitWatcher(str(d)).snapshot()
    assert not s.ok and s.error == "не git-репозиторий"
    assert await GitWatcher(str(d)).context_for_agents() == ""


async def test_snapshot_counts_and_log(tmp_path):
    repo = tmp_path / "r"
    init_repo(repo)
    commit(repo, "a.py", "a\n", "first")
    commit(repo, "b.py", "b\n", "второй коммит")
    (repo / "a.py").write_text("changed\n", encoding="utf-8")            # изменён, не в индексе
    (repo / "c.py").write_text("c\n", encoding="utf-8")
    git(repo, "add", "c.py")                                              # в индексе
    (repo / "b.py").write_text("staged\n", encoding="utf-8")
    git(repo, "add", "b.py")
    (repo / "b.py").write_text("staged and changed\n", encoding="utf-8")  # MM
    (repo / "new.txt").write_text("?", encoding="utf-8")
    git(repo, "branch", "feature")

    s = await GitWatcher(str(repo)).snapshot()
    assert s.ok and s.branch == "main" and s.upstream == ""
    assert (s.staged, s.modified, s.untracked) == (2, 2, 1)
    assert [c[3] for c in s.commits] == ["второй коммит", "first"]
    assert s.commits[0][2] == "Test"
    assert set(s.branches) == {"main", "feature"}
    assert s.mrs == [] and s.mr_source == ""


async def test_ahead_behind_against_upstream(tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo, other = tmp_path / "r", tmp_path / "other"
    init_repo(repo)
    commit(repo, "a", "1", "base")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "-u", "origin", "main")
    git(tmp_path, "clone", "-q", str(origin), str(other))
    git(other, "config", "user.name", "O")
    git(other, "config", "user.email", "o@example.com")
    commit(other, "b", "2", "theirs")
    git(other, "push", "-q")
    commit(repo, "c", "3", "ours")
    git(repo, "fetch", "-q")

    s = await GitWatcher(str(repo)).snapshot()
    assert s.upstream == "origin/main"
    assert (s.ahead, s.behind) == (1, 1)


async def test_switch_and_detached(tmp_path):
    repo = tmp_path / "r"
    init_repo(repo)
    commit(repo, "a", "1", "one")
    commit(repo, "a", "2", "two")
    w = GitWatcher(str(repo))
    git(repo, "branch", "dev")
    ok, _ = await w.switch("dev")
    assert ok and (await w.snapshot()).branch == "dev"
    ok, out = await w.switch("missing")
    assert not ok and out
    git(repo, "checkout", "-q", "HEAD~1")
    assert (await w.snapshot()).branch.startswith("(detached ")


async def test_context_for_agents_lists_files(tmp_path):
    repo = tmp_path / "r"
    init_repo(repo)
    for i in range(85):
        (repo / f"f{i:02d}.py").write_text("", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "many files")
    ctx = await GitWatcher(str(repo)).context_for_agents()
    assert ctx.startswith("Ветка: main.")
    assert "many files" in ctx and "f00.py" in ctx
    assert "… и ещё 5" in ctx and "f84.py" not in ctx


async def _child_pid(pidfile):
    import asyncio
    for _ in range(200):
        if pidfile.exists() and pidfile.read_text():
            return int(pidfile.read_text())
        await asyncio.sleep(0.02)
    raise AssertionError("дочерний процесс не стартовал")


def _sleeper(pidfile):
    import sys
    return (sys.executable, "-c", f"import os, time; open(r'{pidfile}', 'w').write(str(os.getpid())); time.sleep(30)")


async def test_run_kills_process_on_timeout(tmp_path):
    """Регрессия: по таймауту процесс оставался жить (и держал пайпы)."""
    import asyncio
    from capricorn.gitinfo import run
    from test_web import _alive
    pidfile = tmp_path / "pid"
    code, out = await run(*_sleeper(pidfile), cwd=str(tmp_path), timeout=1)
    assert code == 1
    pid = await _child_pid(pidfile)
    await asyncio.sleep(0.3)
    assert not _alive(pid)


async def test_run_kills_process_on_cancel(tmp_path):
    import asyncio
    from capricorn.gitinfo import run
    from test_web import _alive
    pidfile = tmp_path / "pid"
    task = asyncio.create_task(run(*_sleeper(pidfile), cwd=str(tmp_path), timeout=30))
    pid = await _child_pid(pidfile)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.3)
    assert not _alive(pid)
