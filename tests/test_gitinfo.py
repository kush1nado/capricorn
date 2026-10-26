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


async def test_commits_and_show(tmp_path):
    repo = tmp_path / "r"
    init_repo(repo)
    commit(repo, "a.py", "x = 1\n", "первый")
    commit(repo, "a.py", "x = 2\n", "второй")
    w = GitWatcher(str(repo))
    cs = await w.commits()
    assert [c[3] for c in cs] == ["второй", "первый"] and len(cs[0][0]) == 40
    shown = await w.show(cs[0][0])
    assert "второй" in shown and "-x = 1" in shown and "+x = 2" in shown and "a.py" in shown
    assert "некорректный" in await w.show("HEAD; rm -rf /")
    assert await GitWatcher(str(tmp_path)).commits() == []


# ------------------------------------------------------- lazygit-операции

async def test_changes_stage_unstage_discard(tmp_path):
    repo = tmp_path / "r"
    init_repo(repo)
    commit(repo, "a.py", "x = 1\n", "init")
    commit(repo, "keep.py", "k\n", "keep")
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "new.py").write_text("n = 1\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "deep.txt").write_text("d", encoding="utf-8")
    w = GitWatcher(str(repo))

    ch = {c.path: c for c in await w.changes()}
    assert set(ch) == {"a.py", "new.py", "sub/deep.txt"}           # -uall: файлы в новых папках по одному
    assert ch["a.py"].unstaged and not ch["a.py"].staged
    assert ch["new.py"].untracked and not ch["new.py"].staged

    assert (await w.stage("a.py"))[0]
    a = next(c for c in await w.changes() if c.path == "a.py")
    assert a.staged and not a.unstaged
    assert "+x = 2" in await w.file_diff(a)
    assert (await w.unstage("a.py"))[0]
    assert not next(c for c in await w.changes() if c.path == "a.py").staged

    assert "+n = 1" in await w.file_diff(next(c for c in await w.changes() if c.path == "new.py"))
    assert (await w.stage_all())[0] and all(c.staged for c in await w.changes())
    assert (await w.unstage_all())[0] and not any(c.staged for c in await w.changes())

    for c in await w.changes():
        assert (await w.discard(c))[0], c.path
    assert await w.changes() == []
    assert (repo / "a.py").read_text(encoding="utf-8") == "x = 1\n" and not (repo / "new.py").exists()


async def test_discard_staged_new_file_and_rename(tmp_path):
    repo = tmp_path / "r"
    init_repo(repo)
    commit(repo, "old.py", "o\n", "init")
    (repo / "added.py").write_text("a", encoding="utf-8")
    git(repo, "add", "added.py")
    git(repo, "mv", "old.py", "renamed.py")
    w = GitWatcher(str(repo))
    paths = {c.path: c for c in await w.changes()}
    assert set(paths) == {"added.py", "renamed.py"}                 # старый путь переименования не дублируется
    assert (await w.discard(paths["added.py"]))[0]
    assert not (repo / "added.py").exists()


async def test_commit_and_branch(tmp_path):
    repo = tmp_path / "r"
    init_repo(repo)
    commit(repo, "a.py", "1", "init")
    w = GitWatcher(str(repo))
    assert (await w.commit("  "))[0] is False
    (repo / "a.py").write_text("2", encoding="utf-8")
    await w.stage("a.py")
    ok, _ = await w.commit("правка через Capricorn")
    assert ok and (await w.commits())[0][3] == "правка через Capricorn"
    assert (await w.create_branch("-rf"))[0] is False
    assert (await w.create_branch("feature/x"))[0]
    assert (await w.snapshot()).branch == "feature/x"


async def test_push_pull_fetch_with_local_remote(tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    a, b = tmp_path / "a", tmp_path / "b"
    init_repo(a)
    commit(a, "f.txt", "1", "init")
    git(a, "remote", "add", "origin", str(origin))
    git(a, "push", "-q", "-u", "origin", "main")
    git(tmp_path, "clone", "-q", str(origin), str(b))
    git(b, "config", "user.name", "B")
    git(b, "config", "user.email", "b@example.com")
    wa, wb = GitWatcher(str(a)), GitWatcher(str(b))
    commit(a, "f.txt", "2", "from a")
    assert (await wa.push())[0]
    assert (await wb.fetch())[0]
    assert (await wb.pull())[0]
    assert (b / "f.txt").read_text(encoding="utf-8") == "2"


async def test_push_without_remote_fails_fast(tmp_path):
    repo = tmp_path / "r"
    init_repo(repo)
    commit(repo, "a", "1", "init")
    ok, out = await GitWatcher(str(repo)).push()
    assert not ok and out


async def test_diff_has_no_git_warnings(tmp_path):
    """Регрессия: при core.autocrlf=true git пишет в stderr «LF will be replaced by CRLF» —
    это попадало в diff на экране (stderr склеивался с выводом)."""
    repo = tmp_path / "r"
    init_repo(repo)
    git(repo, "config", "core.autocrlf", "true")
    (repo / "c.py").write_bytes(b"a = 1\n")
    git(repo, "add", "c.py")
    git(repo, "commit", "-qm", "init")
    (repo / "c.py").write_bytes(b"a = 2\n")
    w = GitWatcher(str(repo))
    ch = (await w.changes())[0]
    diff = await w.file_diff(ch)
    assert "warning:" not in diff and "+a = 2" in diff
    assert "warning:" not in await w.show((await w.commits())[0][0])
