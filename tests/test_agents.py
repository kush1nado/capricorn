"""Живые ходы агентов на скриптованном провайдере."""
import asyncio

import pytest

from capricorn.orchestrator import USER
from helpers import ScriptedProvider, kinds, messages, settle


def assert_alternates(history):
    roles = [m["role"] for m in history]
    assert roles[:1] in ([], ["user"])
    assert all(a != b for a, b in zip(roles, roles[1:])), roles


async def test_handoff_chain(make_orch):
    orch, events = make_orch({
        "manager": ["@architect набросай план", "Принято."],
        "architect": "План готов. @manager",
    })
    orch.user_says("сделай сервис")
    await settle(orch)

    msgs = messages(events)
    assert [(m.sender, m.to) for m in msgs] == [
        (USER, ["manager"]),
        ("manager", ["architect"]),
        ("architect", ["manager"]),
        ("manager", []),
    ]
    assert orch.budget == orch.cfg.budget - 3
    arch = orch.agents["architect"]
    assert arch.history[0] == {"role": "user", "content": "[от manager → architect]: @architect набросай план"}
    assert arch.history[1]["role"] == "assistant"
    assert arch.turns == 1 and arch.status == "idle"
    assert arch.tokens_in == 10 and arch.tokens_out == len("План готов. @manager")
    for a in orch.agents.values():
        assert_alternates(a.history)


async def test_stream_events_are_consistent(make_orch):
    orch, events = make_orch({"manager": "ответ @user"})
    orch.user_says("привет")
    await settle(orch)
    start, = kinds(events, "stream_start")
    chunks = kinds(events, "stream_chunk")
    end, = kinds(events, "stream_end")
    assert start["msg_id"] == end["msg_id"] == chunks[0]["msg_id"]
    assert "".join(c["chunk"] for c in chunks) == end["text"] == "ответ @user"
    assert end["to"] == [USER]
    statuses = [d["status"] for d in kinds(events, "status") if d["agent"] == "manager"]
    assert statuses == ["thinking", "typing", "idle"]
    assert messages(events)[-1].id == end["msg_id"]


async def test_system_prompt_has_role_and_roster(make_orch):
    orch, _ = make_orch({"reviewer": "ок"})
    orch.route("architect", "@reviewer оцени")
    await settle(orch)
    call, = orch.providers["script"].calls_for("reviewer")
    assert "# Ты: @reviewer" in call.system
    assert "@coder3" not in call.system
    assert call.model == "test"


async def test_messages_arriving_during_pause_are_batched(make_orch):
    orch, _ = make_orch({"manager": "ок"})
    orch.pause()
    orch.user_says("первое")
    orch.user_says("второе")
    await settle(orch)
    assert orch.providers["script"].calls == []

    orch.resume()
    await settle(orch)
    call, = orch.providers["script"].calls_for("manager")
    last = call.messages[-1]["content"]
    assert "первое" in last and "второе" in last
    assert orch.agents["manager"].turns == 1


async def test_observed_context_is_prepended_once(make_orch):
    orch, _ = make_orch({"manager": ["ок", "ок"]})
    orch.route("architect", "@reviewer глянь")       # reviewer молчит (пустой ответ)
    await settle(orch)
    orch.user_says("статус?")
    await settle(orch)
    content = orch.agents["manager"].history[0]["content"]
    assert content.startswith("(для контекста, отвечать не нужно)\n[от architect → reviewer]: @reviewer глянь")
    assert content.endswith("[от user → manager]: статус?")
    assert orch.agents["manager"].context == []
    orch.user_says("ещё")
    await settle(orch)
    assert "для контекста" not in orch.agents["manager"].history[2]["content"]


async def test_think_blocks_do_not_route(make_orch):
    orch, events = make_orch({"manager": "<think>может @coder1?</think>Решил: @reviewer"})
    orch.user_says("x")
    await settle(orch)
    msg, = [m for m in messages(events) if m.sender == "manager"]
    assert msg.to == ["reviewer"]
    assert msg.content == "Решил: @reviewer"
    assert "<think>" in orch.agents["manager"].history[-1]["content"]   # сырой текст в истории


async def test_budget_exhaustion_pauses_and_continue_resumes(make_orch):
    orch, events = make_orch({"architect": "@reviewer", "reviewer": "@architect"}, budget=3)
    orch.user_says("@architect спорьте")
    await settle(orch)
    assert orch.budget == 0
    assert orch.agents["architect"].turns + orch.agents["reviewer"].turns == 3
    warns = [d for d in kinds(events, "system") if d["level"] == "warn"]
    assert len(warns) == 1 and "Бюджет" in warns[0]["text"]
    waiting = [n for n, a in orch.agents.items() if a.status == "waiting budget"]
    assert waiting == ["reviewer"]           # architect → reviewer → architect, дальше стоп

    orch.add_budget(2)
    await settle(orch)
    assert orch.agents["architect"].turns + orch.agents["reviewer"].turns == 5
    assert len([d for d in kinds(events, "system") if d["level"] == "warn"]) == 2


async def test_new_user_task_restores_budget(make_orch):
    orch, _ = make_orch({"manager": "ок"}, budget=1)
    orch.user_says("раз")
    await settle(orch)
    assert orch.budget == 0
    orch.user_says("два")
    await settle(orch)
    assert orch.agents["manager"].turns == 2


async def test_provider_error_is_reported_and_not_retried_automatically(make_orch):
    orch, events = make_orch({"manager": [RuntimeError("401 bad key"), "теперь ок"]})
    orch.user_says("задача")
    await settle(orch)

    assert len(orch.providers["script"].calls_for("manager")) == 1, "повтор должен быть только по /retry"
    errors = [d for d in kinds(events, "system") if d["level"] == "error"]
    assert len(errors) == 1 and "401 bad key" in errors[0]["text"]
    mgr = orch.agents["manager"]
    assert mgr.status == "error"
    assert mgr.history == []
    assert [m.content for m in mgr.pending] == ["задача"]
    assert orch.budget == orch.cfg.budget - 1
    assert kinds(events, "stream_end")[-1]["text"] == ""

    orch.retry("manager")
    await settle(orch)
    assert mgr.status == "idle" and mgr.turns == 1 and mgr.pending == []
    assert [m["role"] for m in mgr.history] == ["user", "assistant"]


async def test_unknown_provider_does_not_kill_agent(make_orch):
    orch, events = make_orch({"manager": "ок"})
    orch.cfg.agents["manager"].model = "ghost/model"
    orch.user_says("задача")
    await settle(orch)

    errors = [d for d in kinds(events, "system") if d["level"] == "error"]
    assert len(errors) == 1 and "ghost" in errors[0]["text"]
    mgr = orch.agents["manager"]
    assert mgr.status == "error" and mgr.history == [] and len(mgr.pending) == 1
    assert all(not t.done() for t in orch._tasks), "цикл агента не должен умирать"

    orch.set_model("manager", "script/test")
    orch.retry("manager")
    await settle(orch)
    assert mgr.turns == 1 and mgr.status == "idle"


async def test_stop_all_interrupts_stream_and_team_recovers(make_orch):
    started = asyncio.Event()

    async def hang(agent, last):
        started.set()
        await asyncio.Event().wait()

    orch, events = make_orch({"manager": [hang, "снова в строю"]})
    orch.user_says("долгая задача")
    await asyncio.wait_for(started.wait(), 2)
    orch.stop_all()
    await settle(orch)

    mgr = orch.agents["manager"]
    assert mgr.status == "stopped"
    assert mgr.history[-1] == {"role": "assistant", "content": "\n[прервано]"}
    assert orch.budget == 0

    orch.user_says("новая задача")
    await settle(orch)
    assert mgr.status == "idle" and mgr.history[-1]["content"] == "снова в строю"
    assert_alternates(mgr.history)


async def test_history_is_trimmed_and_starts_with_user(make_orch):
    orch, _ = make_orch({"manager": lambda a, last: "ок"}, max_history=5)
    for i in range(6):
        orch.user_says(f"задача {i}")
        await settle(orch)
    calls = orch.providers["script"].calls_for("manager")
    assert max(len(c.messages) for c in calls) == 5       # в модель уходит не больше max_history
    for c in calls:
        assert c.messages[0]["role"] == "user"
    hist = orch.agents["manager"].history
    assert_alternates(hist)
    assert hist[-1]["role"] == "assistant"
    assert "задача 5" in hist[-2]["content"]


async def test_coder3_joins_only_in_complex_mode(make_orch):
    orch, events = make_orch({"manager": ["@coders", "@coders"], "coder1": "a", "coder2": "b", "coder3": "c"})
    orch.user_says("раз")
    await settle(orch)
    assert orch.agents["coder3"].turns == 0
    orch.set_complex(True)
    orch.user_says("два")
    await settle(orch)
    assert orch.agents["coder3"].turns == 1


# ------------------------------------------------------------- инструменты

from helpers import tool  # noqa: E402


async def test_coder_writes_file_then_reports(make_orch, tmp_path):
    orch, events = make_orch({
        "coder1": [tool("write_file", path="calc.py", content="def add(a, b):\n    return a + b\n"),
                   tool("read_file", path="calc.py"),
                   "Готово, `calc.py`. @reviewer"],
    })
    orch.route("manager", "@coder1 напиши add()")
    await settle(orch)

    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    hist = orch.agents["coder1"].history
    assert [m["role"] for m in hist] == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert hist[1]["tool_calls"][0]["name"] == "write_file"
    assert hist[2]["results"][0]["content"] == "calc.py создан, строк: 2"
    assert "return a + b" in hist[4]["results"][0]["content"]
    assert hist[-1] == {"role": "assistant", "content": "Готово, `calc.py`. @reviewer"}

    msg, = [m for m in messages(events) if m.sender == "coder1"]
    assert msg.to == ["reviewer"] and msg.content == "Готово, `calc.py`. @reviewer"
    assert orch.agents["coder1"].turns == 1 and orch.budget == orch.cfg.budget - 2   # coder1 + reviewer

    end = [d for d in kinds(events, "stream_end") if d["agent"] == "coder1"][0]
    assert "» `write_file` `calc.py` создан, 2 стр." in end["text"]
    assert end["text"].endswith("Готово, `calc.py`. @reviewer")
    tools_ev = [d for d in kinds(events, "tool") if d["agent"] == "coder1"]
    assert [(t["name"], t["ok"]) for t in tools_ev] == [("write_file", True), ("read_file", True)]
    statuses = [d["status"] for d in kinds(events, "status") if d["agent"] == "coder1"]
    assert "working" in statuses and statuses[-1] == "idle"


async def test_tool_specs_follow_role(make_orch):
    orch, _ = make_orch({"architect": "ок", "coder1": "ок"})
    orch.route("manager", "@architect @coder1 привет")
    await settle(orch)
    p = orch.providers["script"]
    arch_tools = {t["name"] for t in p.calls_for("architect")[0].tools}
    coder_tools = {t["name"] for t in p.calls_for("coder1")[0].tools}
    assert "write_file" not in arch_tools and "write_file" in coder_tools
    assert "## Инструменты" in p.calls_for("coder1")[0].system


async def test_tools_disabled_sends_none(make_orch):
    orch, _ = make_orch({"coder1": "ок"})
    orch.cfg.tools.enabled = False
    orch.route("manager", "@coder1 x")
    await settle(orch)
    call, = orch.providers["script"].calls_for("coder1")
    assert call.tools is None
    assert "Инструментов записи у тебя нет" in call.system


async def test_tool_errors_go_back_to_model(make_orch):
    orch, events = make_orch({
        "architect": [tool("write_file", path="x.py", content="x"), "понял, писать не могу"],
    })
    orch.route("manager", "@architect запиши файл")
    await settle(orch)
    hist = orch.agents["architect"].history
    res = hist[2]["results"][0]
    assert res["is_error"] and "недоступен" in res["content"]
    assert [d["ok"] for d in kinds(events, "tool")] == [False]
    assert orch.agents["architect"].status == "idle"


async def test_rounds_are_limited_and_last_round_forbids_tools(make_orch, tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    orch, _ = make_orch({"coder1": lambda agent, last: tool("read_file", path="a.txt")})
    orch.cfg.tools.max_rounds = 2
    orch.route("manager", "@coder1 читай бесконечно")
    await settle(orch)
    calls = orch.providers["script"].calls_for("coder1")
    assert [c.tool_choice for c in calls] == ["auto", "auto", "none"]
    hist = orch.agents["coder1"].history
    assert [m["role"] for m in hist] == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert "tool_calls" not in hist[-1]


async def test_error_mid_tool_loop_rolls_back_turn(make_orch, tmp_path):
    orch, events = make_orch({"coder1": [tool("write_file", path="w.py", content="1"),
                                         RuntimeError("503 overloaded"), "дописал"]})
    orch.route("manager", "@coder1 пиши")
    await settle(orch)
    c1 = orch.agents["coder1"]
    assert c1.status == "error" and c1.history == [] and len(c1.pending) == 1
    assert (tmp_path / "w.py").exists()                  # действие инструмента уже случилось
    orch.retry("coder1")
    await settle(orch)
    assert [m["role"] for m in c1.history] == ["user", "assistant"]
    assert c1.history[-1]["content"] == "дописал"


async def test_stop_during_tool_loop_keeps_history_valid(make_orch):
    started = asyncio.Event()

    async def hang(agent, last):
        started.set()
        await asyncio.Event().wait()

    orch, _ = make_orch({"coder1": [tool("list_files"), hang]})
    orch.route("manager", "@coder1 x")
    await asyncio.wait_for(started.wait(), 5)
    orch.stop_all()
    await settle(orch)
    hist = orch.agents["coder1"].history
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert "tool_calls" not in hist[-1] and hist[-1]["content"].endswith("[прервано]")


async def test_trim_never_starts_with_tool_entries(make_orch, tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    orch, _ = make_orch({"coder1": [tool("read_file", path="a.txt"), "ок"] * 4}, max_history=4)
    for i in range(4):
        orch.route("manager", f"@coder1 раз {i}")
        await settle(orch)
    for c in orch.providers["script"].calls_for("coder1"):
        assert c.messages[0]["role"] == "user"
        assert len(c.messages) <= 6


async def test_tool_calls_are_logged(make_orch, isolated_home):
    orch, _ = make_orch({"coder1": [tool("list_files"), "ок"]})
    orch.route("manager", "@coder1 x")
    await settle(orch)
    import json
    recs = [json.loads(line) for line in orch.log_path.read_text(encoding="utf-8").splitlines()]
    assert {"type": "tool", "agent": "coder1", "name": "list_files", "args": {}, "ok": True,
            "summary": recs[1]["summary"]} == recs[1]


async def test_cancelling_agent_loop_mid_turn_really_stops_it(make_orch):
    """Регрессия: цикл агента глотал собственную отмену, если она приходила посреди хода
    (except CancelledError предназначался для /stop) — задача не завершалась никогда."""
    started = asyncio.Event()

    async def hang(agent, last):
        started.set()
        await asyncio.Event().wait()

    orch, _ = make_orch({"manager": hang})
    orch.user_says("x")
    await asyncio.wait_for(started.wait(), 5)
    loop_task = orch._tasks[list(orch.agents).index("manager")]
    loop_task.cancel()
    done, _ = await asyncio.wait({loop_task}, timeout=3)
    assert loop_task in done, "цикл агента не завершился после отмены"


async def test_stop_still_keeps_loop_alive_after_fix(make_orch):
    started = asyncio.Event()

    async def hang(agent, last):
        started.set()
        await asyncio.Event().wait()

    orch, _ = make_orch({"manager": [hang, "снова"]})
    orch.user_says("x")
    await asyncio.wait_for(started.wait(), 5)
    orch.stop_all()
    await settle(orch)
    assert all(not t.done() for t in orch._tasks)
    orch.user_says("y")
    await settle(orch)
    assert orch.agents["manager"].history[-1]["content"] == "снова"


@pytest.mark.parametrize("reply", [
    "**[от manager → user]:**\n\nГотово. @user",
    "[от manager → user]: Готово. @user",
    "  **[от manager → user]**: Готово. @user",
])
async def test_echoed_protocol_header_is_stripped(make_orch, reply):
    """Модели копируют формат входящих «[от X → Y]:» в начало ответа — коллегам это не нужно."""
    orch, events = make_orch({"manager": reply})
    orch.user_says("x")
    await settle(orch)
    msg, = [m for m in messages(events) if m.sender == "manager"]
    assert msg.content == "Готово. @user" and msg.to == ["user"]


async def test_provider_status_chunks_update_agent_status(make_orch):
    class Queued(ScriptedProvider):
        async def stream(self, **kw):
            yield {"status": "queued"}
            yield {"status": "thinking"}
            async for c in super().stream(**kw):
                yield c

    orch, events = make_orch()
    orch.providers["script"] = Queued({"manager": "ок"})
    orch.user_says("x")
    await settle(orch)
    statuses = [d["status"] for d in kinds(events, "status") if d["agent"] == "manager"]
    assert statuses[:3] == ["thinking", "queued", "thinking"] and statuses[-1] == "idle"
