"""Уроки живого прогона: ревьюер видит, где код кодера; пустые ходы не съедают бюджет;
промпты ведут к порядку «кодер → ревью → MERGE → SYNC»."""
import asyncio

import pytest

from capricorn.prompts import system_prompt
from helpers import build_orch, git, init_repo, messages, settle


@pytest.fixture
async def team(tmp_path):
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


async def test_writer_message_carries_branch_and_changed_files(team):
    orch, events, repo = team({"coder1": ["Готово, @reviewer посмотри"], "reviewer": ["APPROVED"]})
    await orch.toolbox.run("coder1", "write_file", {"path": "feature.py", "content": "ok\n"})
    await orch.toolbox.run("coder1", "edit_file", {"path": "app.py", "old": "x = 1", "new": "x = 2"})
    orch.route("manager", "@coder1 сделай фичу")
    await settle(orch)
    msg = next(m for m in messages(events) if m.sender == "coder1")
    assert msg.content.startswith("Готово, @reviewer посмотри")
    note = msg.content.split("посмотри", 1)[1]
    assert "capricorn/coder1" in note and ".git/capricorn/worktrees/coder1" in note.replace("\\", "/")
    assert "feature.py" in note and "app.py" in note
    to_reviewer = orch.provider_for("reviewer").calls_for("reviewer")[0].messages[-1]["content"]
    assert "feature.py" in to_reviewer


async def test_no_branch_note_without_changes_or_from_non_writers(team):
    orch, events, _ = team({"coder1": ["Вопрос, @architect: какой API?"], "architect": ["@coder1 вот такой"]})
    orch.route("manager", "@coder1 сделай")
    await settle(orch)
    by = {m.sender: m.content for m in messages(events)}
    assert "capricorn/coder1" not in by["coder1"], "изменений нет — пометка не нужна"
    assert "capricorn/" not in by["architect"]


async def test_reply_without_addressees_is_refunded(team):
    orch, _, _ = team({"manager": ["@coder1 делай"], "coder1": ["жду решения архитектора"]}, budget=5)
    orch.user_says("задача")
    await settle(orch)
    assert orch.agents["coder1"].turns == 1
    assert orch.budget == 4, "ход менеджера списан, а ответ coder1 без адресатов — нет"


def prompt(role, native=None):
    return system_prompt(role, ["architect", "manager", "coder1", "coder2", "reviewer", "tester"], "ru", "",
                         tools=[] if native else ["read_file", "write_file", "sync_branch", "merge_work", "git_diff"],
                         worktrees=True, native=native)


def test_prompts_teach_order_of_work():
    mgr = prompt("manager")
    assert "ревьюера" in mgr and "готов" in mgr                     # ревью — только с готовым кодом
    assert "зависим" in mgr and ("SYNC" in mgr or "sync_branch" in mgr)
    rev = prompt("reviewer")
    assert "проверять нечего" in rev                                 # и отвечает без @, не будя кодеров
    coder = prompt("coder1")
    assert "чужой работы" in coder
    native = prompt("coder1", native={"path": "/w", "write": True, "test_command": "pytest", "branch": "capricorn/coder1"})
    assert "чужой работы" in native and "SYNC" in native


async def test_every_agent_sees_user_task_once(team):
    """Живой прогон 2: ревьюер не видел задачу и одобрил код, сверив его с отчётом кодера."""
    orch, _, _ = team({"manager": ["@coder1 делай CLI"], "coder1": ["Готово, @reviewer", "Поправил, @reviewer"],
                       "reviewer": ["@coder1 нет сортировки", "APPROVED"]})
    orch.user_says("CLI: регистр не важен, при равенстве — по алфавиту")
    await settle(orch)
    prov = orch.provider_for("reviewer")
    first, second = (c.messages[-1]["content"] for c in prov.calls_for("reviewer"))
    assert "регистр не важен" in first and "задача пользователя" in first
    assert "регистр не важен" not in second, "та же задача второй раз не повторяется"
    c1 = prov.calls_for("coder1")[0].messages[-1]["content"]
    assert "регистр не важен" in c1
    mgr = prov.calls_for("manager")[0].messages[-1]["content"]
    assert mgr.count("регистр не важен") == 1, "менеджер получил задачу напрямую — без дубля"
    assert orch.agents["reviewer"].task_seen == orch.task_msg.id
    assert orch.agents["architect"].task_seen == 0, "кто не ходил — тот задачу ещё не видел"


def test_only_manager_assigns_coders():
    arch = prompt("architect")
    assert "только менеджер" in arch
    assert "только ты" in prompt("manager")


async def test_late_agent_sees_task_and_followups(team):
    orch, _, _ = team({"manager": ["понял", "@reviewer проверь"], "reviewer": ["ок"]})
    orch.user_says("сделай CLI с сортировкой по алфавиту")
    await settle(orch)
    orch.user_says("вариант А")
    await settle(orch)
    to_reviewer = orch.provider_for("reviewer").calls_for("reviewer")[0].messages[-1]["content"]
    assert "сортировкой по алфавиту" in to_reviewer and "вариант А" in to_reviewer
