"""Логика стартового экрана: проект, недавние, доступные модели, пресеты, настройки проекта."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .config import ROLES, Config
from .sessions import project_dir

RECENT_LIMIT = 10
WRITERS = {"coder1", "coder2", "coder3", "tester"}


def default_project_dir() -> Path:
    """Текущая папка; но не корень диска и не внутренность .app (так стартует .app на macOS) — тогда домашняя."""
    cwd = Path(os.getcwd()).resolve()
    if cwd == Path(cwd.anchor) or any(p.suffix == ".app" for p in (cwd, *cwd.parents)):
        return Path.home()
    return cwd


# --------------------------------------------------------------- недавние

def _recent_file() -> Path:
    return Path.home() / ".config" / "capricorn" / "recent.json"


def recent_projects() -> list[str]:
    try:
        items = json.loads(_recent_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [p for p in items if isinstance(p, str) and Path(p).is_dir()][:RECENT_LIMIT]


def remember_project(path: str | Path) -> None:
    path = str(Path(path).resolve())
    items = [path, *[p for p in recent_projects() if p != path]][:RECENT_LIMIT]
    f = _recent_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


# ----------------------------------------------------------------- модели

def cli_available() -> dict[str, bool]:
    from .cli_providers import CodexProvider
    return {"claude-code": shutil.which("claude") is not None,
            "codex": CodexProvider("codex", {}).discover() is not None}


def available_models(cfg: Config, cli: dict[str, bool] | None = None) -> list[tuple[str, str]]:
    """(подпись, модель) — только то, что реально запустится на этой машине."""
    cli = cli_available() if cli is None else cli
    out: list[tuple[str, str]] = []
    if cli.get("claude-code"):
        out += [("Claude Sonnet (подписка)", "claude-code/sonnet"), ("Claude Opus (подписка)", "claude-code/opus")]
    if cli.get("codex"):
        out.append(("Codex (подписка ChatGPT)", "codex/default"))
    for model in dict.fromkeys(a.model for a in cfg.agents.values()):     # из конфига проекта, по порядку
        provider = model.split("/", 1)[0]
        if provider in cfg.providers and provider != "mock" and model not in {m for _, m in out}:
            out.append((f"{model} (конфиг)", model))
    out.append(("Заглушка (без модели)", "mock/mock"))
    return out


def presets(cfg: Config, available: list[tuple[str, str]]) -> dict[str, dict[str, str]]:
    have = {m for _, m in available}
    out: dict[str, dict[str, str]] = {}
    if "claude-code/sonnet" in have:
        out["Claude"] = {r: "claude-code/sonnet" for r in ROLES}
    if "codex/default" in have:
        out["Codex"] = {r: "codex/default" for r in ROLES}
    if {"claude-code/sonnet", "codex/default"} <= have:
        out["Смешанно"] = {r: "codex/default" if r in WRITERS else "claude-code/sonnet" for r in ROLES}
    out["Из конфига"] = {r: a.model for r, a in cfg.agents.items()}
    out["Заглушка"] = {r: "mock/mock" for r in ROLES}
    return out


def apply_models(cfg: Config, models: dict[str, str]) -> None:
    """Назначить модели и добавить нужные CLI-провайдеры, если в конфиге их нет."""
    for role, model in models.items():
        if role not in cfg.agents:
            continue
        provider = model.split("/", 1)[0]
        if provider in ("codex", "claude-code"):
            cfg.providers.setdefault(provider, {"type": provider})
        if provider in cfg.providers:
            cfg.agents[role].model = model


# ------------------------------------------------------- настройки проекта

def _settings_file(repo: str | Path) -> Path:
    return project_dir(repo) / "settings.json"


def load_project_settings(repo: str | Path) -> dict:
    try:
        data = json.loads(_settings_file(repo).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_project_settings(repo: str | Path, settings: dict) -> None:
    f = _settings_file(repo)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
