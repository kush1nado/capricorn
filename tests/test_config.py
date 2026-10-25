from pathlib import Path

import pytest

from capricorn.config import ROLES, AgentConfig, load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


def test_defaults_without_file():
    cfg = load_config()
    assert cfg.source == "defaults (mock)"
    assert list(cfg.agents) == ROLES
    assert all(a.model == "mock/mock" for a in cfg.agents.values())
    assert cfg.agents["manager"].observe_all is True
    assert not any(a.observe_all for n, a in cfg.agents.items() if n != "manager")
    assert cfg.providers == {"mock": {"type": "mock"}}
    assert (cfg.budget, cfg.max_history, cfg.complex_mode) == (30, 40, False)


def test_yaml_in_cwd_with_env_expansion(monkeypatch):
    monkeypatch.setenv("CAPRI_TEST_KEY", "sk-123")
    monkeypatch.delenv("CAPRI_MISSING", raising=False)
    Path("capricorn.yaml").write_text(
        "budget: 7\ncomplex_mode: true\n"
        "providers:\n  ds:\n    type: openai\n    api_key: ${CAPRI_TEST_KEY}\n"
        "    headers: {X-Extra: '${CAPRI_MISSING}'}\n"
        "agents:\n  coder1:\n    model: ds/deepseek-chat\n    temperature: 0.1\n"
        "    extra_prompt: пиши тесты\n  manager:\n    observe_all: false\n",
        encoding="utf-8")
    cfg = load_config()
    assert cfg.source == "capricorn.yaml"
    assert cfg.budget == 7 and cfg.complex_mode is True
    assert cfg.providers["ds"]["api_key"] == "sk-123"
    assert cfg.providers["ds"]["headers"] == {"X-Extra": ""}
    assert "mock" in cfg.providers                       # заглушка доступна всегда
    c1 = cfg.agents["coder1"]
    assert (c1.model, c1.temperature, c1.extra_prompt) == ("ds/deepseek-chat", 0.1, "пиши тесты")
    assert cfg.agents["manager"].observe_all is False
    assert cfg.agents["tester"].model == "mock/mock"     # не описан — дефолт


def test_explicit_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(str(tmp_path / "nope.yaml"))


def test_empty_yaml_gives_defaults(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.source == str(p)
    assert cfg.budget == 30


def test_force_mock_overrides_models(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("agents:\n  architect:\n    model: anthropic/claude\n", encoding="utf-8")
    cfg = load_config(str(p), force_mock=True)
    assert {n: a.model for n, a in cfg.agents.items()} == {r: f"mock/{r}" for r in ROLES}


@pytest.mark.parametrize("model, provider, model_id", [
    ("ollama/qwen2.5-coder:14b", "ollama", "qwen2.5-coder:14b"),
    ("openrouter/anthropic/claude-x", "openrouter", "anthropic/claude-x"),
    ("bare", "bare", "bare"),
])
def test_agent_model_split(model, provider, model_id):
    a = AgentConfig(name="x", model=model)
    assert (a.provider, a.model_id) == (provider, model_id)


def test_example_config_is_consistent():
    cfg = load_config(str(EXAMPLE))
    assert set(cfg.agents) == set(ROLES)
    for name, a in cfg.agents.items():
        assert a.provider in cfg.providers, f"{name}: провайдер {a.provider} не описан"
    for name, p in cfg.providers.items():
        assert p.get("type") in ("openai", "anthropic", "mock"), name
