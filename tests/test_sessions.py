"""Сессии по проектам: хранение, продолжение, экспорт."""
import json

import pytest

from capricorn.orchestrator import USER, Message
from capricorn.sessions import SessionStore, project_dir, to_markdown
from helpers import build_orch, git, init_repo, settle, tool


def test_project_dir_inside_git_or_home(tmp_path, isolated_home):
    repo = tmp_path / "repo"
    init_repo(repo)
    assert project_dir(repo) == (repo / ".git" / "capricorn").resolve()
    plain = tmp_path / "my project"
    plain.mkdir()
    d = project_dir(plain)
    assert d.parent == isolated_home / ".local" / "share" / "capricorn" / "projects"
    assert d.name.startswith("my_project-") and project_dir(plain) == d       # стабильно


def test_store_roundtrip_list_and_latest(tmp_path):
    store = SessionStore(tmp_path)
    assert store.list() == [] and store.latest() is None
    store.save("a", {"id": "a", "updated": 1, "messages": [{"sender": USER, "content": "первая\nзадача"}]})
    store.save("b", {"id": "b", "updated": 2, "messages": []})
    (store.root / "broken").mkdir()
    (store.root / "broken" / "state.json").write_text("{не json", encoding="utf-8")
    infos = store.list()
    assert [i.id for i in infos] == ["b", "a"] and store.latest() == "b"
    assert infos[1].title == "первая задача" and infos[1].messages == 1
    assert store.load("a")["id"] == "a"
    with pytest.raises(FileNotFoundError):
        store.load("nope")
    with pytest.raises(ValueError):
        store.dir("../evil")
    assert not list(store.root.glob("*/*.tmp"))


def test_markdown_export():
    md = to_markdown({"id": "s1", "repo": "/r", "created": 0, "models": {"coder1": "codex/default"},
                      "messages": [{"sender": USER, "to": ["manager"], "content": "сделай", "ts": 0},
                                   {"sender": "architect", "to": ["user"], "content": "итог", "ts": 0,
                                    "kind": "handoff"}]})
    assert md.startswith("# Capricorn — сессия s1")
    assert "| coder1 | `codex/default` |" in md
    assert "· user → manager" in md and "сделай" in md and "итог для команды" in md


async def test_session_saved_after_turn_and_restored(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    orch, _ = build_orch(repo, {"manager": "Ок, @coder1 займись", "coder1": "сделал"})
    orch.providers["script"].sessions = {"manager": "cli-123"}      # как у CLI-провайдера
    orch.start()
    try:
        orch.user_says("задача")
        await settle(orch)
    finally:
        for t in orch._tasks:
            t.cancel()
    path = orch.store.dir(orch.session_id) / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    assert [m["sender"] for m in state["messages"]] == [USER, "manager", "coder1"]
    assert state["agents"]["coder1"]["turns"] == 1 and state["cli_sessions"]["script"] == {"manager": "cli-123"}
    assert path.is_relative_to((repo / ".git" / "capricorn").resolve())
    assert (orch.store.dir(orch.session_id) / "log.jsonl").exists()

    fresh, _ = build_orch(repo)
    fresh.providers["script"].sessions = {}
    assert fresh.store.latest() == orch.session_id
    warnings = fresh.restore(fresh.store.load(orch.session_id))
    assert warnings == []
    assert fresh.session_id == orch.session_id and len(fresh.messages) == 3
    assert fresh.agents["coder1"].history == orch.agents["coder1"].history
    assert fresh.agents["coder1"].turns == 1 and fresh.budget == orch.budget
    assert fresh.providers["script"].sessions == {"manager": "cli-123"}
    assert fresh.route(USER, "ещё").id > max(m.id for m in orch.messages)       # id не повторяются


async def test_restore_keeps_pending_without_waking(tmp_path):
    orch, _ = build_orch(tmp_path)
    orch.agents["manager"].pending.append(Message(1, USER, ["manager"], "ждёт"))
    state = orch.snapshot()
    fresh, _ = build_orch(tmp_path)
    fresh.restore(state)
    assert [m.content for m in fresh.agents["manager"].pending] == ["ждёт"]
    assert not fresh.agents["manager"].wake.is_set()                      # ход — только по /continue


def test_restore_warns_about_unavailable_provider(tmp_path):
    orch, _ = build_orch(tmp_path)
    state = orch.snapshot()
    state["models"]["coder1"] = "ghost/model"
    fresh, _ = build_orch(tmp_path)
    warnings = fresh.restore(state)
    assert len(warnings) == 1 and "ghost/model" in warnings[0]
    assert fresh.cfg.agents["coder1"].model == "script/test"


async def test_tool_turn_history_survives_roundtrip(tmp_path):
    orch, _ = build_orch(tmp_path, {"coder1": [tool("list_files"), "ок"]})
    orch.start()
    try:
        orch.route("manager", "@coder1 x")
        await settle(orch)
    finally:
        for t in orch._tasks:
            t.cancel()
    fresh, _ = build_orch(tmp_path)
    fresh.restore(json.loads(json.dumps(orch.snapshot())))
    assert [m["role"] for m in fresh.agents["coder1"].history] == ["user", "assistant", "tool", "assistant"]


def test_new_ids_unique_within_one_second(tmp_path):
    """Регрессия: id из секунды и PID совпадал, и /new перезаписывал прошлую сессию."""
    store = SessionStore(tmp_path)
    ids = {store.new_id() for _ in range(50)}
    assert len(ids) == 50


def test_empty_session_is_not_saved(tmp_path):
    """Регрессия: каждое открытие окна/вкладки сохраняло пустую сессию — /sessions зарастал мусором."""
    orch, _ = build_orch(tmp_path)
    orch.save()
    assert orch.store.list() == []
    orch.route(USER, "привет")
    orch.save()
    assert [s.id for s in orch.store.list()] == [orch.session_id]
