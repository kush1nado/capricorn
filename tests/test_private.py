"""Личный канал с агентом: диалог только с пользователем, итог — команде."""
import pytest

from capricorn.orchestrator import HANDOFF_PROMPT, USER
from helpers import kinds, messages, settle


async def test_private_dialog_stays_between_user_and_agent(make_orch):
    orch, events = make_orch({"architect": "Предлагаю три слоя. @coder1 @reviewer гляньте"})
    orch.open_private("architect")
    orch.private_says("architect", "как разложим модули?")
    await settle(orch)

    q, a = [m for m in messages(events) if m.kind == "private"]
    assert (q.sender, q.to) == (USER, ["architect"])
    assert (a.sender, a.to) == ("architect", [USER])                  # @упоминания в личном — не маршрутизируются
    assert orch.agents["coder1"].turns == 0 and orch.agents["reviewer"].turns == 0
    assert orch.agents["manager"].context == []                      # менеджер личное не подслушивает
    sent = orch.providers["script"].calls_for("architect")[0].messages[-1]["content"]
    assert sent == "[лично от user]: как разложим модули?"


async def test_closing_window_shares_summary_with_team(make_orch):
    orch, events = make_orch({"architect": ["Давай так: api, service, storage.", "Итог: три слоя, coder1 — storage."],
                              "manager": "Принял, @coder1 начинай со storage", "coder1": "ок"})
    orch.open_private("architect")
    orch.private_says("architect", "архитектура?")
    await settle(orch)
    assert orch.end_private("architect") is True
    await settle(orch)

    handoff = orch.providers["script"].calls_for("architect")[1].messages[-1]["content"]
    assert HANDOFF_PROMPT in handoff
    summary, = [m for m in messages(events) if m.kind == "summary"]
    assert summary.sender == "architect" and summary.content == "Итог: три слоя, coder1 — storage."
    assert set(summary.to) == {"manager", "coder1", "coder2", "reviewer", "tester"}
    # менеджер получил итог задачей и раздал работу; остальные — контекстом, без лишних ходов
    assert orch.agents["manager"].turns == 1
    assert orch.agents["reviewer"].turns == 0
    assert any(m.kind == "summary" for m in orch.agents["reviewer"].context)
    mgr_in = orch.providers["script"].calls_for("manager")[0].messages[-1]["content"]
    assert mgr_in.startswith("[итог личного обсуждения architect с user]: Итог: три слоя")
    assert orch.agents["coder1"].turns == 1                           # пришло от менеджера


async def test_closing_without_talk_sends_nothing(make_orch):
    orch, events = make_orch({"architect": "x"})
    orch.open_private("architect")
    assert orch.end_private("architect") is False
    await settle(orch)
    assert orch.providers["script"].calls == []


async def test_private_messages_jump_the_queue(make_orch):
    orch, _ = make_orch({"architect": ["ответ лично", "ответ команде"]})
    orch.pause()
    orch.route("manager", "@architect командный вопрос")
    orch.private_says("architect", "личный вопрос")
    orch.resume()
    await settle(orch)
    first, second = orch.providers["script"].calls_for("architect")
    assert "личный вопрос" in first.messages[-1]["content"] and "командный" not in first.messages[-1]["content"]
    assert "командный вопрос" in second.messages[-1]["content"]


async def test_private_says_unknown_agent(make_orch):
    orch, _ = make_orch()
    with pytest.raises(ValueError):
        orch.private_says("ghost", "x")


async def test_summary_from_manager_wakes_nobody(make_orch):
    orch, events = make_orch({"manager": ["обсудили", "итог"]})
    orch.open_private("manager")
    orch.private_says("manager", "план?")
    await settle(orch)
    orch.end_private("manager")
    await settle(orch)
    assert sum(a.turns for n, a in orch.agents.items() if n != "manager") == 0
    assert all(any(m.kind == "summary" for m in orch.agents[n].context) for n in ("architect", "coder1"))
