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


def codex_models() -> list[tuple[str, str]]:
    """(название, slug) моделей Codex из его кэша (~/.codex/models_cache.json) — без хардкода:
    новые модели OpenAI появляются сами. Скрытые (visibility != list) не показываем."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        data = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for m in data.get("models") or [] if isinstance(data, dict) else []:
        if isinstance(m, dict) and m.get("slug") and m.get("visibility", "list") == "list":
            out.append((str(m.get("display_name") or m["slug"]), str(m["slug"])))
    return out


def available_models(cfg: Config, cli: dict[str, bool] | None = None) -> list[tuple[str, str]]:
    """(подпись, модель) — только то, что реально запустится на этой машине."""
    cli = cli_available() if cli is None else cli
    out: list[tuple[str, str]] = []
    if cli.get("claude-code"):
        out += [("Claude Sonnet (подписка)", "claude-code/sonnet"), ("Claude Opus (подписка)", "claude-code/opus"),
                ("Claude Haiku — лёгкая (подписка)", "claude-code/haiku")]
    if cli.get("codex"):
        out.append(("Codex по умолчанию (подписка ChatGPT)", "codex/default"))
        out += [(f"Codex {name} (подписка)", f"codex/{slug}") for name, slug in codex_models()]
    for model in dict.fromkeys(a.model for a in cfg.agents.values()):     # из конфига проекта, по порядку
        provider = model.split("/", 1)[0]
        if provider in cfg.providers and provider != "mock" and model not in {m for _, m in out}:
            out.append((f"{model} (конфиг)", model))
    out.append(("Заглушка (без модели)", "mock/mock"))
    return out


def presets(cfg: Config, available: list[tuple[str, str]]) -> dict[str, dict[str, str]]:
    have = {m for _, m in available}
    have_order = [m for _, m in available]
    out: dict[str, dict[str, str]] = {}
    if "claude-code/sonnet" in have:
        out["Claude"] = {r: "claude-code/sonnet" for r in ROLES}
    if "codex/default" in have:
        out["Codex"] = {r: "codex/default" for r in ROLES}
    if {"claude-code/sonnet", "codex/default"} <= have:
        out["Смешанно"] = {r: "codex/default" if r in WRITERS else "claude-code/sonnet" for r in ROLES}
    # «Экономно»: лёгкие модели — кодерам Codex Luna, остальным Claude Haiku (что есть)
    light_claude = "claude-code/haiku" if "claude-code/haiku" in have else None
    light_codex = next((m for m in have_order if m.startswith("codex/") and "luna" in m), None)
    if light_claude or light_codex:
        out["Экономно"] = {r: (light_codex or light_claude) if r in WRITERS else (light_claude or light_codex)
                          for r in ROLES}
    out["Из конфига"] = {r: a.model for r, a in cfg.agents.items()}
    out["Заглушка"] = {r: "mock/mock" for r in ROLES}
    return out


KIND_SAMPLE = {"architect": "architect", "manager": "manager", "coder": "coder1", "reviewer": "reviewer",
               "tester": "tester", "expert": "reviewer"}


def preset_key(role: str, kind: str | None = None) -> str:
    """Пресеты описаны для стандартных ролей; новой роли достаётся модель стандартной того же типа."""
    from .team import default_kind
    return KIND_SAMPLE.get(kind or default_kind(role), "reviewer")


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
