"""Загрузка конфигурации: YAML + подстановка ${ENV}."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROLES = ["architect", "manager", "coder1", "coder2", "coder3", "reviewer", "tester"]

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(value):
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


@dataclass
class AgentConfig:
    name: str
    model: str = "mock/mock"
    temperature: float = 0.5
    max_tokens: int = 4096
    observe_all: bool = False
    compact: bool = False               # код свёрнут, подслушанное и старая история ужаты
    extra_prompt: str = ""
    tools: list[str] | None = None      # None — набор по роли (tools.ROLE_TOOLS)

    @property
    def provider(self) -> str:
        return self.model.split("/", 1)[0]

    @property
    def model_id(self) -> str:
        return self.model.split("/", 1)[1] if "/" in self.model else self.model


@dataclass
class ToolsConfig:
    enabled: bool = True
    write: bool = True                  # write_file / edit_file
    run_tests: bool = True
    test_command: str | list[str] = "python -m pytest -q"
    test_timeout: int = 300
    max_rounds: int = 12                # раундов «модель → инструменты» за один ход
    worktrees: bool = True              # свой git worktree у каждого кодера и тестировщика


@dataclass
class Config:
    language: str = "ru"
    default_target: str = "manager"
    budget: int = 30
    max_history: int = 40
    context_chars: int = 400            # сжатый контекст: длина одного подслушанного сообщения
    context_messages: int = 12          # и сколько последних подслушанных сообщений видно
    complex_mode: bool = False
    providers: dict = field(default_factory=lambda: {"mock": {"type": "mock"}})
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    source: str = "defaults"


CANDIDATES = [
    Path("capricorn.yaml"),
    Path(".capricorn/config.yaml"),
    Path.home() / ".config" / "capricorn" / "config.yaml",
]


def load_config(path: str | None = None, force_mock: bool = False, base: str | Path | None = None) -> Config:
    """base — папка проекта: относительные кандидаты (capricorn.yaml, .capricorn/config.yaml) ищутся в ней."""
    raw: dict = {}
    source = "defaults (mock)"
    if path:
        paths = [Path(path)]
    elif base is not None:
        paths = [p if p.is_absolute() else Path(base) / p for p in CANDIDATES]
    else:
        paths = CANDIDATES
    for p in paths:
        if p.exists():
            raw = _expand(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
            source = str(p)
            break
    else:
        if path:
            raise FileNotFoundError(path)

    cfg = Config(
        language=raw.get("language", "ru"),
        default_target=raw.get("default_target", "manager"),
        budget=int(raw.get("budget", 30)),
        max_history=int(raw.get("max_history", 40)),
        context_chars=int(raw.get("context_chars", 400)),
        context_messages=int(raw.get("context_messages", 12)),
        complex_mode=bool(raw.get("complex_mode", False)),
        providers=raw.get("providers") or {"mock": {"type": "mock"}},
        source=source,
    )
    cfg.providers.setdefault("mock", {"type": "mock"})

    t = raw.get("tools") or {}
    cfg.tools = ToolsConfig(
        enabled=bool(t.get("enabled", True)),
        write=bool(t.get("write", True)),
        run_tests=bool(t.get("run_tests", True)),
        test_command=t.get("test_command", ToolsConfig.test_command),
        test_timeout=int(t.get("test_timeout", 300)),
        max_rounds=int(t.get("max_rounds", 12)),
        worktrees=bool(t.get("worktrees", True)),
    )

    agents_raw = raw.get("agents") or {}
    for role in ROLES:
        a = agents_raw.get(role) or {}
        ac = AgentConfig(
            name=role,
            model=a.get("model", "mock/mock"),
            temperature=float(a.get("temperature", 0.5)),
            max_tokens=int(a.get("max_tokens", 4096)),
            observe_all=bool(a.get("observe_all", role == "manager")),
            compact=bool(a.get("compact", role == "manager")),
            extra_prompt=a.get("extra_prompt", ""),
            tools=list(a["tools"]) if a.get("tools") is not None else None,
        )
        if force_mock:
            ac.model = f"mock/{role}"
        cfg.agents[role] = ac
    return cfg
