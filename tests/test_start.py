"""Логика стартового экрана."""
import os
from pathlib import Path

from capricorn import start
from capricorn.config import ROLES, load_config
from helpers import init_repo


def test_default_project_dir(monkeypatch, tmp_path, isolated_home):
    monkeypatch.chdir(tmp_path)
    assert start.default_project_dir() == tmp_path.resolve()
    monkeypatch.chdir(os.path.abspath(os.sep))
    assert start.default_project_dir() == isolated_home
    bundle = tmp_path / "Capricorn.app" / "Contents" / "MacOS"
    bundle.mkdir(parents=True)
    monkeypatch.chdir(bundle)
    assert start.default_project_dir() == isolated_home


def test_recent_projects(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    assert start.recent_projects() == []
    start.remember_project(a)
    start.remember_project(b)
    start.remember_project(a)                                 # наверх, без дублей
    assert start.recent_projects() == [str(a.resolve()), str(b.resolve())]
    b.rmdir()
    assert start.recent_projects() == [str(a.resolve())]       # исчезнувшие папки не показываем
    for i in range(15):
        d = tmp_path / f"p{i}"
        d.mkdir()
        start.remember_project(d)
    assert len(start.recent_projects()) == start.RECENT_LIMIT


def test_available_models_and_presets(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("providers:\n  anthropic: {type: anthropic}\nagents:\n  reviewer: {model: anthropic/claude-sonnet-5-5}\n",
                 encoding="utf-8")
    cfg = load_config(str(p))
    both = start.available_models(cfg, {"claude-code": True, "codex": True})
    models = [m for _, m in both]
    assert models[:4] == ["claude-code/sonnet", "claude-code/opus", "claude-code/haiku", "codex/default"]
    assert "anthropic/claude-sonnet-5-5" in models and models[-1] == "mock/mock"
    ps = start.presets(cfg, both)
    assert list(ps) == ["Claude", "Codex", "Смешанно", "Экономно", "Из конфига", "Заглушка"]
    assert ps["Смешанно"]["coder1"] == "codex/default" and ps["Смешанно"]["reviewer"] == "claude-code/sonnet"
    assert ps["Из конфига"]["reviewer"] == "anthropic/claude-sonnet-5-5"

    none = start.available_models(cfg, {"claude-code": False, "codex": False})
    assert "codex/default" not in [m for _, m in none]
    assert list(start.presets(cfg, none)) == ["Из конфига", "Заглушка"]


def test_apply_models_adds_cli_providers():
    cfg = load_config()
    start.apply_models(cfg, {"coder1": "codex/default", "reviewer": "claude-code/sonnet", "tester": "ghost/x",
                             "nobody": "mock/mock"})
    assert cfg.providers["codex"] == {"type": "codex"} and cfg.providers["claude-code"] == {"type": "claude-code"}
    assert cfg.agents["coder1"].model == "codex/default"
    assert cfg.agents["tester"].model == "mock/mock"          # неизвестный провайдер — не трогаем


def test_project_settings_roundtrip(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    assert start.load_project_settings(repo) == {}
    start.save_project_settings(repo, {"models": {"coder1": "codex/default"}})
    assert start.load_project_settings(repo)["models"]["coder1"] == "codex/default"
    assert (repo / ".git" / "capricorn" / "settings.json").exists()


def test_load_config_from_project_dir(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "capricorn.yaml").write_text("budget: 7\n", encoding="utf-8")
    assert load_config(base=proj).budget == 7
    assert load_config(base=proj).source == str(proj / "capricorn.yaml")
    assert load_config().budget == 30                          # текущая папка — без конфига
    assert set(load_config(base=proj).agents) == set(ROLES)


def _codex_cache(home, models):
    import json
    d = home / ".codex"
    d.mkdir(parents=True, exist_ok=True)
    (d / "models_cache.json").write_text(json.dumps({"models": models}), encoding="utf-8")


def test_codex_models_from_cache(isolated_home):
    assert start.codex_models() == []
    _codex_cache(isolated_home, [
        {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list"},
        {"slug": "gpt-reserve", "display_name": "GPT-Reserve", "visibility": "hide"},
        {"slug": "gpt-6-luna", "display_name": "GPT-6-Luna", "visibility": "list"},
    ])
    assert start.codex_models() == [("GPT-6.1-Sol", "gpt-6.1-sol"), ("GPT-6-Luna", "gpt-6-luna")]


def test_light_models_and_economy_preset(isolated_home):
    _codex_cache(isolated_home, [{"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list"},
                                 {"slug": "gpt-6-luna", "display_name": "GPT-6-Luna", "visibility": "list"}])
    cfg = load_config()
    av = start.available_models(cfg, {"claude-code": True, "codex": True})
    models = [m for _, m in av]
    assert "claude-code/haiku" in models and "codex/gpt-6-luna" in models and "codex/gpt-6.1-sol" in models
    ps = start.presets(cfg, av)
    eco = ps["Экономно"]
    assert eco["coder1"] == "codex/gpt-6-luna" and eco["tester"] == "codex/gpt-6-luna"
    assert eco["manager"] == "claude-code/haiku" and eco["reviewer"] == "claude-code/haiku"

    only_claude = start.available_models(cfg, {"claude-code": True, "codex": False})
    assert set(start.presets(cfg, only_claude)["Экономно"].values()) == {"claude-code/haiku"}
    only_codex = start.available_models(cfg, {"claude-code": False, "codex": True})
    assert set(start.presets(cfg, only_codex)["Экономно"].values()) == {"codex/gpt-6-luna"}
    none = start.available_models(cfg, {"claude-code": False, "codex": False})
    assert "Экономно" not in start.presets(cfg, none)


def test_codex_home_env(monkeypatch, tmp_path):
    custom = tmp_path / "ch"
    monkeypatch.setenv("CODEX_HOME", str(custom))
    custom.mkdir()
    (custom / "models_cache.json").write_text('{"models": [{"slug": "x", "display_name": "X", "visibility": "list"}]}',
                                               encoding="utf-8")
    assert start.codex_models() == [("X", "x")]
