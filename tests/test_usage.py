"""/usage: токены, ходы и стоимость по агентам за сессию, сколько срезало сжатие."""
import pytest

from capricorn.config import load_config
from capricorn.usage import cost, price_for, usage_report
from helpers import settle


def test_price_lookup_and_cost():
    prices = {"openai/big": (2.0, 0.5, 8.0), "small": (1.0, 0.1, 4.0)}
    assert price_for(prices, "openai/big") == (2.0, 0.5, 8.0)
    assert price_for(prices, "codex/small") == (1.0, 0.1, 4.0), "без провайдера — по имени модели"
    assert price_for(prices, "mock/echo") is None
    assert cost((2.0, 0.5, 8.0), 1_000_000, 2_000_000, 500_000) == pytest.approx(2 + 1 + 4)


def test_config_prices(tmp_path):
    assert load_config().prices == {}
    (tmp_path / "c.yaml").write_text("prices:\n  openai/big: [2, 0.5, 8]\n", encoding="utf-8")
    assert load_config(tmp_path / "c.yaml").prices == {"openai/big": (2.0, 0.5, 8.0)}
    (tmp_path / "c.yaml").write_text("prices:\n  openai/big: [2, 8]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="prices.openai/big"):
        load_config(tmp_path / "c.yaml")


async def test_report_counts_tokens_cost_and_compaction(make_orch):
    orch, _ = make_orch({"coder1": ["готово @manager\n```python\n" + "x = 1\n" * 40 + "```"], "manager": ["ок"]})
    orch.cfg.prices = {orch.cfg.agents["coder1"].model: (1.0, 0.0, 2.0)}
    orch.agents["coder1"].tokens_in, orch.agents["coder1"].tokens_out = 1_000_000, 500_000
    orch.agents["coder1"].turns = 3
    orch.user_says("@coder1 сделай")
    await settle(orch)
    assert orch.agents["manager"].compact_saved > 100
    orch.cfg.agents["manager"].model = "mock/без-цены"

    out = usage_report(orch)
    lines = out.splitlines()
    head = lines[0].split()
    assert head[:5] == ["АГЕНТ", "ХОДЫ", "ВХОД", "КЭШ", "ВЫХОД"] and head[-1] == "$"
    row = next(x for x in lines if x.startswith("coder1"))
    assert "1000.0k" in row and "$2.00" in row
    assert next(x for x in lines if x.startswith("manager")).rstrip().endswith("—"), "цена неизвестна"
    total = next(x for x in lines if x.startswith("итого"))
    assert "$2.00+" in total, "часть цен неизвестна — сумма снизу"
    assert "сжатие" in out and "manager" in out.split("сжатие", 1)[1]


async def test_report_without_prices_hints_config(make_orch):
    orch, _ = make_orch({})
    out = usage_report(orch)
    assert "$" not in out.splitlines()[0] and "prices" in out
