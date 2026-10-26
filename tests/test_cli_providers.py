"""Провайдеры Codex CLI и Claude Code CLI — на поддельном CLI (tests/fake_cli.py), без сети и подписок."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from capricorn.cli_providers import ClaudeCodeProvider, CodexProvider
from capricorn.providers import ProviderError, build_providers
from capricorn.tools import Toolbox
from helpers import ScriptedProvider, build_orch, git, init_repo, kinds, messages, settle

FAKE = str(Path(__file__).resolve().parent / "fake_cli.py")
MSGS = [{"role": "user", "content": "[от manager → coder1]: сделай"}]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """fake(runs) -> команда поддельного CLI; fake.log() — что ему передавали."""
    log = tmp_path / "cli.log"
    script = tmp_path / "cli.json"
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    monkeypatch.setenv("FAKE_CLI_SCRIPT", str(script))

    class Fake:
        def __call__(self, runs):
            script.write_text(json.dumps(runs, ensure_ascii=False), encoding="utf-8")
            return [sys.executable, FAKE]

        def log(self):
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return Fake()


async def collect(provider, agent="coder1", workspace=None, messages=MSGS, model="default"):
    out = []
    async for chunk in provider.stream(model=model, system="СИСТЕМА", messages=messages, temperature=0,
                                       max_tokens=10, agent=agent, workspace=workspace):
        out.append(chunk)
    return out


def ws(path, write=False, **kw):
    return {"path": str(path), "write": write, "test_command": "python -m pytest -q", **kw}


# ----------------------------------------------------------------- codex

CODEX_TURN = [
    {"type": "thread.started", "thread_id": "th-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Смотрю.\n"}},
    {"type": "item.started", "item": {"type": "command_execution", "command": "rg foo", "status": "in_progress"}},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "rg foo", "exit_code": 0}},
    {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py", "kind": "update"}]}},
    {"type": "item.completed", "item": {"type": "reasoning", "text": "думаю"}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Готово. @reviewer"}},
    {"type": "turn.completed", "usage": {"input_tokens": 120, "cached_input_tokens": 100, "output_tokens": 7}},
]


async def test_codex_first_turn_then_resume(fake, tmp_path):
    p = CodexProvider("codex", {"command": fake([{"events": CODEX_TURN}, {"events": CODEX_TURN[1:]}])})
    out = await collect(p, workspace=ws(tmp_path), model="gpt-x")
    assert out == ["Смотрю.", {"note": "shell: rg foo → 0"}, {"note": "правка: a.py"},
                   "\n\nГотово. @reviewer", {"usage": {"in": 20, "out": 7, "cached": 100}}]
    assert p.sessions == {"coder1": "th-1"}

    out2 = await collect(p, workspace=ws(tmp_path, write=True),
                         messages=[*MSGS, {"role": "assistant", "content": "x"}, {"role": "user", "content": "ещё"}])
    first, second = fake.log()
    assert first["argv"] == ["exec", "--json", "--skip-git-repo-check", "-C", str(tmp_path),
                             "-s", "read-only", "-m", "gpt-x", "-"]
    assert first["stdin"] == "СИСТЕМА\n\n---\n\n[от manager → coder1]: сделай"
    assert Path(first["cwd"]) == tmp_path
    assert second["argv"][-3:] == ["resume", "th-1", "-"] and "workspace-write" in second["argv"]
    assert "-m" not in second["argv"]                       # model default — без -m
    assert second["stdin"] == "ещё"                          # память — у CLI, шлём только новое
    assert out2[-2] == "\n\nГотово. @reviewer"


async def test_codex_sessions_per_agent_and_reset(fake, tmp_path):
    p = CodexProvider("codex", {"command": fake([{"events": CODEX_TURN}])})
    await collect(p, agent="coder1", workspace=ws(tmp_path))
    await collect(p, agent="coder2", workspace=ws(tmp_path))
    assert "resume" not in fake.log()[1]["argv"]             # у coder2 своя сессия
    p.reset("coder1")
    assert "coder1" not in p.sessions and "coder2" in p.sessions
    p.reset()
    assert p.sessions == {}


async def test_codex_turn_failed(fake, tmp_path):
    p = CodexProvider("codex", {"command": fake([{"events": [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.failed", "error": {"message": "usage limit reached"}}], "exit": 1}])})
    with pytest.raises(ProviderError, match="usage limit reached"):
        await collect(p, workspace=ws(tmp_path))


async def test_codex_crash_reports_stderr(fake, tmp_path):
    p = CodexProvider("codex", {"command": fake([{"events": [], "exit": 3, "stderr": "not logged in"}])})
    with pytest.raises(ProviderError, match="код выхода 3.*not logged in"):
        await collect(p, workspace=ws(tmp_path))


async def test_missing_cli(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with pytest.raises(ProviderError, match="не найден.*providers.codex.command"):
        await collect(CodexProvider("codex", {}), workspace=ws(tmp_path))
    with pytest.raises(ProviderError, match="Claude Code CLI не найден"):
        await collect(ClaudeCodeProvider("claude-code", {}), workspace=ws(tmp_path))


@pytest.mark.skipif(os.name != "nt", reason="поиск в установке десктопного Codex — только Windows")
def test_codex_found_in_desktop_install(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    lonely = tmp_path / "OpenAI" / "Codex" / "bin" / "aaa"
    lonely.mkdir(parents=True)
    (lonely / "codex.exe").write_bytes(b"")                # без code-mode host — не годится
    assert CodexProvider("codex", {}).discover() is None
    full = tmp_path / "OpenAI" / "Codex" / "bin" / "bbb"
    full.mkdir(parents=True)
    (full / "codex.exe").write_bytes(b"")
    (full / "codex-code-mode-host.exe").write_bytes(b"")
    assert CodexProvider("codex", {}).discover() == str(full / "codex.exe")


async def test_cancel_kills_cli_process_tree(fake, tmp_path):
    from test_web import _alive
    pidfile = tmp_path / "pids"
    p = CodexProvider("codex", {"command": fake([{"events": [{"type": "thread.started", "thread_id": "t"}],
                                                  "sleep": 60, "pidfile": str(pidfile)}])})
    task = asyncio.create_task(collect(p, workspace=ws(tmp_path)))
    for _ in range(300):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.02)
    cli_pid, child_pid = map(int, pidfile.read_text().split())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)
    assert not _alive(cli_pid) and not _alive(child_pid)


# ------------------------------------------------------------ claude code

CLAUDE_TURN = [
    {"type": "system", "subtype": "init", "session_id": "s-1"},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Читаю."},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "src/app.py"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "..."}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Всё ок. @manager"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "Всё ок. @manager",
     "session_id": "s-1", "usage": {"input_tokens": 50, "cache_read_input_tokens": 30, "output_tokens": 9}},
]


async def test_claude_read_only_then_resume(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")                   # Capricorn запущен внутри Claude Code
    p = ClaudeCodeProvider("claude-code", {"command": fake([{"events": CLAUDE_TURN}])})
    out = await collect(p, agent="reviewer", workspace=ws(tmp_path))
    assert out == ["Читаю.", {"note": "Read: src/app.py"}, "\n\nВсё ок. @manager", {"usage": {"in": 50, "out": 9, "cached": 30}}]
    assert p.sessions["reviewer"] == "s-1"
    await collect(p, agent="reviewer", workspace=ws(tmp_path))
    first, second = fake.log()
    a = first["argv"]
    assert a[:5] == ["-p", "--output-format", "stream-json", "--verbose", "--allowedTools"]
    assert a[5] == "Read,Glob,Grep,LS"
    assert a[a.index("--permission-mode") + 1] == "default"
    assert "Bash" in a[a.index("--disallowedTools") + 1]
    assert first["claudecode"] is None                     # иначе вложенный claude откажется работать
    assert first["stdin"].startswith("СИСТЕМА\n\n---\n\n")
    assert second["argv"][-2:] == ["--resume", "s-1"]


async def test_claude_writer_gets_edit_tools_and_only_test_shell(fake, tmp_path):
    p = ClaudeCodeProvider("claude-code", {"command": fake([{"events": CLAUDE_TURN}])})
    await collect(p, workspace=ws(tmp_path, write=True), model="sonnet")
    a = fake.log()[0]["argv"]
    allowed = a[a.index("--allowedTools") + 1].split(",")
    assert {"Edit", "Write", "MultiEdit"} <= set(allowed)
    assert "Bash(python -m pytest -q:*)" in allowed and "Bash" not in allowed
    assert a[a.index("--permission-mode") + 1] == "acceptEdits"
    assert "--disallowedTools" not in a
    assert a[a.index("--model") + 1] == "sonnet"


async def test_claude_error_result(fake, tmp_path):
    p = ClaudeCodeProvider("claude-code", {"command": fake([{"events": [
        {"type": "result", "subtype": "error", "is_error": True,
         "result": "API Error: 401 OAuth access token is invalid · Please run /login"}], "exit": 1}])})
    with pytest.raises(ProviderError, match="Please run /login"):
        await collect(p, workspace=ws(tmp_path))


def test_build_cli_providers():
    ps = build_providers({"c": {"type": "codex"}, "cc": {"type": "claude-code"}})
    assert type(ps["c"]) is CodexProvider and type(ps["cc"]) is ClaudeCodeProvider
    assert ps["c"].native_tools and ps["cc"].native_tools


# ------------------------------------------------------- в оркестраторе

@pytest.fixture
async def git_orch(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    created = []

    def factory(replies=None, **kw):
        orch, events = build_orch(repo, replies, **kw)
        orch.start()
        created.append(orch)
        return orch, events, repo

    yield factory
    for orch in created:
        for t in orch._tasks:
            t.cancel()
        await asyncio.gather(*orch._tasks, return_exceptions=True)


async def test_cli_agents_get_workspace_and_no_tools(git_orch, fake):
    orch, events, repo = git_orch()
    codex = CodexProvider("codex", {"command": fake([{"events": CODEX_TURN}])})
    orch.providers["codex"] = codex
    orch.cfg.agents["coder1"].model = "codex/default"
    orch.cfg.agents["reviewer"].model = "codex/default"
    orch.route("manager", "@coder1 @reviewer начинайте")
    await settle(orch, timeout=20)

    by_agent = {rec["stdin"].split("\n", 1)[0]: rec for rec in fake.log()}   # «# Ты: @coder1 — Кодер 1»
    c1 = next(r for k, r in by_agent.items() if k.startswith("# Ты: @coder1"))
    rev = next(r for k, r in by_agent.items() if k.startswith("# Ты: @reviewer"))
    wt = await orch.toolbox.worktrees.path("coder1")
    assert Path(c1["cwd"]) == wt and "workspace-write" in c1["argv"]
    assert Path(rev["cwd"]) == repo.resolve() and "read-only" in rev["argv"]
    assert "Рабочая папка" in c1["stdin"] and "capricorn/coder1" in c1["stdin"]
    assert "read_file" not in c1["stdin"]                   # наши инструменты CLI-агенту не описываем
    assert "git -C .git" in rev["stdin"].replace("\\", "/")
    end = [d for d in kinds(events, "stream_end") if d["agent"] == "coder1"][0]
    assert "» shell: rg foo → 0" in end["text"] and "» правка: a.py" in end["text"]


async def test_merge_directive_from_manager(git_orch):
    orch, events, repo = git_orch({"manager": ["Ревью пройдено.\nMERGE coder1\n```\nMERGE coder2\n```", "Принято."]})
    await orch.toolbox.run("coder1", "write_file", {"path": "feature.py", "content": "ok\n"})
    orch.route("reviewer", "@manager APPROVED")
    await settle(orch)
    assert (repo / "feature.py").read_text(encoding="utf-8") == "ok\n"
    git_msgs = [m for m in messages(events) if m.sender == "git"]
    assert len(git_msgs) == 1 and git_msgs[0].to == ["manager"]   # MERGE в блоке кода не выполняется
    assert "влита" in git_msgs[0].content
    assert any("MERGE: capricorn/coder1 влита" in d["text"] for d in kinds(events, "system"))


async def test_directives_respect_roles(git_orch):
    # ответ на сообщение от git — уже без директивы, иначе агент зациклится (его остановит только бюджет)
    orch, events, repo = git_orch({"coder1": ["MERGE coder1", "ок"], "coder2": ["SYNC", "ок"],
                                   "reviewer": ["SYNC", "ок"]})
    await orch.toolbox.run("coder1", "write_file", {"path": "c1.py", "content": "1"})
    orch.route("manager", "@coder1 @coder2 @reviewer")
    await settle(orch)
    assert not (repo / "c1.py").exists()                     # MERGE — только менеджеру
    git_msgs = [m for m in messages(events) if m.sender == "git"]
    assert [m.to for m in git_msgs] == [["coder2"]]          # SYNC — только пишущим
    assert "обновлена" in git_msgs[0].content


async def test_directive_errors_do_not_kill_agent(git_orch, monkeypatch):
    orch, events, repo = git_orch({"manager": ["MERGE coder1", "ок"]})

    async def boom(agent):
        raise RuntimeError("git сломался")
    monkeypatch.setattr(orch.toolbox.worktrees, "merge", boom)
    await orch.toolbox.run("coder1", "write_file", {"path": "z.py", "content": "z"})
    orch.route("reviewer", "@manager APPROVED")
    await settle(orch)
    assert any("git сломался" in d["text"] and d["level"] == "error" for d in kinds(events, "system"))
    assert all(not t.done() for t in orch._tasks)
    assert orch.agents["manager"].turns == 2                 # получил сообщение от git и ответил


async def test_reset_forgets_cli_sessions(git_orch):
    orch, _, _ = git_orch()
    codex = CodexProvider("codex", {})
    codex.sessions = {"coder1": "a", "coder2": "b"}
    orch.providers["codex"] = codex
    orch.reset("coder1")
    assert codex.sessions == {"coder2": "b"}


@pytest.mark.parametrize("raw, shown", [
    ('"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command \'python -m unittest -q\'',
     "python -m unittest -q"),
    ("/bin/bash -lc 'pytest -q tests'", "pytest -q tests"),
    ('bash -lc "git diff"', "git diff"),
    ("rg foo", "rg foo"),
])
def test_codex_note_shows_inner_command(raw, shown):
    from capricorn.cli_providers import command_text
    assert command_text(raw) == shown


async def test_claude_hides_todowrite_notes(fake, tmp_path):
    p = ClaudeCodeProvider("claude-code", {"command": fake([{"events": [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "TodoWrite", "input": {"todos": []}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}]}},
        {"type": "result", "is_error": False, "usage": {}}]}])})
    out = await collect(p, workspace=ws(tmp_path, write=True))
    assert {"note": "Edit: a.py"} in out and not any("TodoWrite" in str(c) for c in out)


def test_readonly_shell_capability():
    assert CodexProvider.readonly_shell is True          # песочница ОС: команды можно, запись нельзя
    assert ClaudeCodeProvider.readonly_shell is False    # Bash в режиме чтения запрещён целиком


async def test_native_workspace_shell_flag_depends_on_provider(git_orch):
    orch, _, _ = git_orch()
    orch.providers["codex"] = CodexProvider("codex", {})
    orch.providers["claude-code"] = ClaudeCodeProvider("claude-code", {})
    orch.cfg.agents["reviewer"].model = "claude-code/default"
    orch.cfg.agents["architect"].model = "codex/default"
    orch.cfg.agents["coder1"].model = "claude-code/default"
    assert (await orch.native_workspace("reviewer"))["shell"] is False
    assert (await orch.native_workspace("architect"))["shell"] is True
    assert (await orch.native_workspace("coder1"))["shell"] is True       # пишущему — тесты в Bash


async def test_cli_parallel_runs_are_limited(fake, tmp_path):
    """Не больше max_parallel одновременных запусков CLI — иначе параллельные ходы упираются в лимиты подписки."""
    slow = [{"events": [{"type": "thread.started", "thread_id": "t"},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": "ок"}},
                        {"type": "turn.completed", "usage": {}}], "sleep": 0.6}]
    p = CodexProvider("codex", {"command": fake(slow), "max_parallel": 1})
    loop = asyncio.get_running_loop()
    spans = {}

    async def run(agent):
        start = loop.time()
        out = await collect(p, agent=agent, workspace=ws(tmp_path))
        spans[agent] = (start, loop.time(), out)

    await asyncio.gather(run("coder1"), run("coder2"))
    (s1, e1, o1), (s2, e2, o2) = sorted(spans.values())
    assert max(e1, e2) - min(s1, s2) >= 1.1                 # шли по очереди, а не вместе
    queued = [o for o in (o1, o2) if {"status": "queued"} in o]
    assert len(queued) == 1                                   # второй ждал слот и сообщил об этом
    assert {"status": "thinking"} in queued[0]


def test_cli_max_parallel_default_and_validation():
    assert CodexProvider("codex", {}).max_parallel == 2
    assert ClaudeCodeProvider("cc", {"max_parallel": 4}).max_parallel == 4
    assert CodexProvider("codex", {"max_parallel": 0}).max_parallel == 1
