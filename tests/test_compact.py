"""Сжатие контекста менеджера: код свёрнут, подслушанное ужато, старая история — конспект."""
from capricorn.compact import KEEP_FULL, collapse_code, compact_context, compact_history, digest
from capricorn.config import load_config
from helpers import settle

CODE = "Готово:\n```python\n" + "\n".join(f"line_{i} = {i}" for i in range(50)) + "\n```\n@manager проверь"


def test_collapse_code_keeps_text_and_counts_lines():
    out = collapse_code(CODE)
    assert out == "Готово:\n[код python, строк: 50 — свёрнут]\n@manager проверь"
    assert collapse_code("```\nx\n") == "[код, строк: 1 — свёрнут]", "незакрытый блок тоже сворачивается"
    assert collapse_code("без кода") == "без кода"


def test_digest_limits_length():
    assert digest("коротко", 100) == "коротко"
    out = digest("а" * 500, 100)
    assert out.startswith("а" * 100) and "ещё 400 симв." in out
    assert digest("a\n\n\n\n\nb", 100) == "a\n\nb"


def test_compact_context_keeps_last_messages():
    items = [f"msg {i} " + "x" * 1000 for i in range(20)]
    out = compact_context(items, 50, 5)
    assert out[0] == "… ранее ещё сообщений: 15"
    assert out[1].startswith("msg 15") and len(out) == 6
    assert all(len(t) < 120 for t in out[1:])
    assert compact_context(["a"], 50, 5) == ["a"]


def test_compact_history_spares_recent_and_keeps_structure():
    long = "y" * 2000
    history = [{"role": "user", "content": long},
               {"role": "assistant", "content": long, "tool_calls": [{"id": "t1", "name": "read_file", "args": {}}]},
               {"role": "tool", "results": [{"id": "t1", "name": "read_file", "content": long}]}]
    history += [{"role": "user" if i % 2 == 0 else "assistant", "content": long} for i in range(KEEP_FULL)]
    compact_history(history, 300)
    assert len(history[0]["content"]) < 400 and len(history[2]["results"][0]["content"]) < 400
    assert history[1]["tool_calls"][0]["id"] == "t1" and history[2]["results"][0]["id"] == "t1"
    assert all(h["content"] == long for h in history[-KEEP_FULL:]), "последние ходы не трогаем"


def test_config_compact_defaults_to_manager(tmp_path):
    cfg = load_config()
    assert cfg.agents["manager"].compact and not cfg.agents["reviewer"].compact
    (tmp_path / "c.yaml").write_text("context_chars: 99\nagents:\n  manager:\n    compact: false\n", encoding="utf-8")
    cfg = load_config(tmp_path / "c.yaml")
    assert cfg.context_chars == 99 and not cfg.agents["manager"].compact


async def test_manager_gets_collapsed_code_reviewer_gets_full(make_orch):
    orch, _ = make_orch({"coder1": [CODE + " и @reviewer"], "manager": ["ок"], "reviewer": ["норм"]})
    prov = orch.provider_for("coder1")
    orch.user_says("@coder1 сделай")
    await settle(orch)
    to_manager = prov.calls_for("manager")[0].messages[-1]["content"]
    to_reviewer = prov.calls_for("reviewer")[0].messages[-1]["content"]
    assert "line_49" not in to_manager and "[код python, строк: 50" in to_manager
    assert "line_49 = 49" in to_reviewer


async def test_manager_overheard_context_is_short(make_orch):
    long = "@coder2 " + "подробности " * 300
    orch, _ = make_orch({"coder1": [long], "manager": ["ок"]})
    prov = orch.provider_for("coder1")
    orch.user_says("@coder1 сделай")
    await settle(orch)
    orch.user_says("@manager как дела?")
    await settle(orch)
    to_manager = prov.calls_for("manager")[-1].messages[-1]["content"]
    assert "для контекста" in to_manager and "симв.]" in to_manager
    assert len(to_manager) < 1000
