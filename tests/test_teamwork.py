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
    arch = prompt("architect")                                       # работу ведёт архитектор
    assert "зависим" in arch and ("SYNC" in arch or "sync_branch" in arch)
    assert "одному кодеру" in arch, "маленькую задачу не дробим (замер: 8 запусков вместо одного)"
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
    assert orch.agents["tester"].task_seen == 0, "кто не ходил — тот задачу ещё не видел"


def test_manager_hands_task_to_architect_and_steps_in_only_on_trouble():
    """Владелец: менеджер ставит задачу и отдаёт архитектору, а не реагирует на каждое сообщение."""
    mgr = prompt("manager").lower()
    assert "@architect" in mgr and "спор" in mgr and "застрял" in mgr
    assert "мелочи" in mgr, "мелочи, которых нет в задаче, пользователю не задаём"
    arch = prompt("architect").lower()
    assert "раздаёшь работу кодерам" in arch and "@user" in arch


async def test_late_agent_sees_task_and_followups(team):
    orch, _, _ = team({"manager": ["понял", "@reviewer проверь"], "reviewer": ["ок"]})
    orch.user_says("сделай CLI с сортировкой по алфавиту")
    await settle(orch)
    orch.user_says("вариант А")
    await settle(orch)
    to_reviewer = orch.provider_for("reviewer").calls_for("reviewer")[0].messages[-1]["content"]
    assert "сортировкой по алфавиту" in to_reviewer and "вариант А" in to_reviewer


async def test_approved_merges_writer_branch_without_manager(team):
    """APPROVED — система сама вливает ветку того, чью работу одобрили, и сообщает архитектору."""
    orch, events, repo = team({"coder1": ["Готово, @reviewer"],
                               "reviewer": ["Проверяю ветку.\n\n**APPROVED**\n\nВсё хорошо."],
                               "architect": ["Принято, всё готово. @user итог: фича влита."]})
    await orch.toolbox.run("coder1", "write_file", {"path": "feature.py", "content": "ok\n"})
    orch.route("architect", "@coder1 сделай фичу")
    await settle(orch)
    assert (repo / "feature.py").read_text(encoding="utf-8") == "ok\n"
    git_msgs = [m for m in messages(events) if m.sender == "git"]
    assert [m.to for m in git_msgs] == [["architect"]] and "влита" in git_msgs[0].content
    assert orch.agents["manager"].turns == 0, "менеджер в обычной работе не участвует"
    assert orch.agents["coder1"].turns == 1, "APPROVED не будит кодера ответом на его «готово?»"


async def test_approved_without_worktrees_goes_to_architect(tmp_path):
    from helpers import build_orch
    orch, events = build_orch(tmp_path, {"reviewer": ["APPROVED"], "architect": ["ок"]})
    orch.start()
    try:
        orch.route("coder1", "@reviewer готово")
        await settle(orch)
        assert next(m for m in messages(events) if m.sender == "reviewer").to == ["architect"]
    finally:
        for t in orch._tasks:
            t.cancel()


async def test_answer_without_mention_goes_back_to_who_asked(team):
    """Замер: архитектор ответил на вопрос кодера без @coder1 — ответ ушёл в никуда, команда встала."""
    orch, events, _ = team({"coder1": ["@architect номер — это id или позиция?", "понял, делаю"],
                            "architect": ["Номер — постоянный id, не переиспользуется."]})
    orch.route("architect", "@coder1 сделай todo")
    await settle(orch)
    answer = next(m for m in messages(events) if m.sender == "architect" and "постоянный id" in m.content)
    assert answer.to == ["coder1"] and orch.agents["coder1"].turns == 2


async def test_plain_reply_without_question_is_not_redirected(team):
    orch, events, _ = team({"coder1": ["@architect сделал часть 1", "ок"], "architect": ["Принято."]})
    orch.route("architect", "@coder1 сделай")
    await settle(orch)
    assert next(m for m in messages(events) if m.content == "Принято.").to == []


def test_approved_detection():
    from capricorn.orchestrator import APPROVED_RE
    assert APPROVED_RE.search("Проверяю.\n\n**APPROVED**")
    assert APPROVED_RE.search("APPROVED, замечаний нет")
    assert not APPROVED_RE.search("Пока не APPROVED: нет тестов")
    assert not APPROVED_RE.search("NOT APPROVED")


async def test_review_request_carries_test_result_run_on_host(team):
    """Ревьюер на CLI не может запускать тесты: их прогоняет оркестратор в ветке кодера, без токенов."""
    import sys
    orch, events, _ = team({"coder1": ["Готово, @reviewer", "Вопрос, @architect"], "reviewer": ["ок"],
                            "architect": ["ок"]})
    orch.cfg.tools.test_command = [sys.executable, "-c", "print('collected 3 items'); print('3 passed in 0.1s')"]
    await orch.toolbox.run("coder1", "write_file", {"path": "f.py", "content": "x = 1\n"})
    orch.route("manager", "@coder1 сделай")
    await settle(orch)
    review = next(m for m in messages(events) if m.sender == "coder1")
    assert "тесты: 3 passed in 0.1s (код 0)" in review.content
    orch.route("manager", "@coder1 спроси архитектора")
    await settle(orch)
    question = [m for m in messages(events) if m.sender == "coder1"][-1]
    assert "тесты:" not in question.content, "тесты гоняем только для ревью"


async def test_no_test_run_when_tests_disabled(team):
    orch, events, _ = team({"coder1": ["Готово, @reviewer"], "reviewer": ["ок"]})
    orch.cfg.tools.run_tests = False
    await orch.toolbox.run("coder1", "write_file", {"path": "f.py", "content": "x = 1\n"})
    orch.route("manager", "@coder1 сделай")
    await settle(orch)
    assert "тесты:" not in next(m for m in messages(events) if m.sender == "coder1").content


async def test_manager_message_crossing_a_reply_does_not_wake_again(team):
    """Живые прогоны: менеджер пишет агенту, пока тот уже отвечает ему же, — и агент делает лишний ход."""
    orch_box = {}

    async def reviewing(agent, incoming):
        orch_box["o"].route("manager", "@reviewer проверь, пожалуйста")     # пришло, пока ревьюер занят
        return "APPROVED @manager"

    orch, events, _ = team({"reviewer": [reviewing, "лишний ход"], "manager": ["ок"]})
    orch_box["o"] = orch
    orch.route("coder1", "@reviewer готово")
    await settle(orch)
    rev = orch.agents["reviewer"]
    assert rev.turns == 1 and not rev.pending
    assert any("проверь, пожалуйста" in m.content for m in rev.context), "не потеряно — видно как контекст"


async def test_crossing_message_from_others_still_wakes(team):
    box = {}

    async def coding(agent, incoming):
        box["o"].route("reviewer", "@coder1 ещё замечание")
        return "сделал @manager"

    orch, _, _ = team({"coder1": [coding, "учёл замечание"], "manager": ["ок"]})
    box["o"] = orch
    orch.route("manager", "@coder1 сделай")
    await settle(orch)
    assert orch.agents["coder1"].turns == 2, "замечание ревьюера — новое дело, его не глотаем"
