"""Состав команды: роли, их типы, описания, промпты, скиллы и инструменты.

Имя агента (как к нему обращаются: @security) отделено от типа роли (как он себя ведёт: reviewer).
Состав проекта хранится в .capricorn/team.yaml, скиллы — в .capricorn/skills/<имя>.md.
Модели и ключи — в capricorn.yaml (agents.<имя>.model), а не здесь: подписки у каждого свои.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

KINDS = {
    "architect": "Архитектор",
    "manager": "Менеджер",
    "coder": "Кодер",
    "reviewer": "Ревьюер",
    "tester": "Тестировщик",
    "expert": "Эксперт",
}
KIND_HELP = {
    "architect": "ведёт работу: проектирует, раздаёт кодерам, отчитывается пользователю",
    "manager": "принимает задачу от пользователя и передаёт ведущему; вмешивается при споре",
    "coder": "пишет код в своей ветке и отдаёт на ревью",
    "reviewer": "проверяет работу; его APPROVED вливает ветку",
    "tester": "пишет тесты в своей ветке",
    "expert": "консультант: отвечает на вопросы по своей области, код не пишет",
}
WRITER_KINDS = {"coder", "tester"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,19}$")
RESERVED = {"user", "me", "human", "all", "team", "coders", "coder", "git", "feed", "system",
            "все", "кодеры"}
TEAM_FILE = Path(".capricorn") / "team.yaml"
SKILLS_DIR = Path(".capricorn") / "skills"
SKILL_LIMIT = 20_000                 # символов одного скилла в промпте
DEFAULT_TITLES = {
    "architect": "Архитектор", "manager": "Менеджер", "coder1": "Кодер 1", "coder2": "Кодер 2",
    "coder3": "Кодер 3", "reviewer": "Ревьюер", "tester": "Тестировщик",
}
DEFAULT_TEAM = ["architect", "manager", "coder1", "coder2", "coder3", "reviewer", "tester"]


class TeamError(ValueError):
    """Ошибка в описании команды — показывается пользователю как есть."""


def default_kind(name: str) -> str:
    if re.fullmatch(r"coder\d*", name):
        return "coder"
    return name if name in KINDS else "expert"


def check_name(name: str, taken: list[str] | tuple = ()) -> str:
    if not NAME_RE.match(name or ""):
        raise TeamError(f"имя «{name}»: латиница в нижнем регистре, цифры и _, от 2 до 20 символов, с буквы")
    if name in RESERVED:
        raise TeamError(f"имя «{name}» занято системой")
    if name in taken:
        raise TeamError(f"роль «{name}» уже есть")
    return name


@dataclass
class RoleDef:
    name: str
    kind: str
    title: str = ""
    description: str = ""            # коротко: видно команде в списке состава
    prompt: str = ""                 # заменяет стандартный промпт типа роли
    skills: list[str] = field(default_factory=list)
    tools: list[str] | None = None   # None — набор по типу
    enabled: bool = True             # в составе команды

    def as_yaml(self) -> dict:
        out: dict = {"kind": self.kind}
        for key in ("title", "description", "prompt"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.skills:
            out["skills"] = list(self.skills)
        if self.tools is not None:
            out["tools"] = list(self.tools)
        if not self.enabled:
            out["enabled"] = False
        return out


def parse_role(name: str, raw: dict | None) -> RoleDef:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise TeamError(f"роль {name}: ожидается словарь полей")
    kind = raw.get("kind") or default_kind(name)
    if kind not in KINDS:
        raise TeamError(f"роль {name}: тип «{kind}» неизвестен. Есть: {', '.join(KINDS)}")
    skills = raw.get("skills") or []
    tools = raw.get("tools")
    if not isinstance(skills, list) or (tools is not None and not isinstance(tools, list)):
        raise TeamError(f"роль {name}: skills и tools — списки")
    return RoleDef(name=name, kind=kind, title=str(raw.get("title") or ""),
                   description=str(raw.get("description") or ""), prompt=str(raw.get("prompt") or ""),
                   skills=[str(s) for s in skills], tools=[str(t) for t in tools] if tools is not None else None,
                   enabled=bool(raw.get("enabled", True)))


def team_path(repo: str | Path) -> Path:
    return Path(repo) / TEAM_FILE


def load_team(repo: str | Path) -> list[RoleDef] | None:
    """Состав из .capricorn/team.yaml или None, если файла нет (тогда команда по умолчанию)."""
    p = team_path(repo)
    if not p.is_file():
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise TeamError(f"{TEAM_FILE}: не разобрать YAML — {e}") from e
    roles = data.get("roles") if isinstance(data, dict) else None
    if not isinstance(roles, dict) or not roles:
        raise TeamError(f"{TEAM_FILE}: нужен непустой раздел roles:")
    out: list[RoleDef] = []
    for name, raw in roles.items():
        check_name(str(name), [r.name for r in out])
        out.append(parse_role(str(name), raw))
    return out


def save_team(repo: str | Path, roles: list[RoleDef]) -> Path:
    p = team_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump({"roles": {r.name: r.as_yaml() for r in roles}}, allow_unicode=True, sort_keys=False)
    p.write_text("# Состав команды Capricorn. Редактируется окном /team или вручную.\n" + body, encoding="utf-8")
    return p


def roles_of(cfg) -> list[RoleDef]:
    """Текущий состав из конфига — чтобы показать и сохранить его."""
    return [RoleDef(name=n, kind=a.kind, title=a.title, description=a.description, prompt=a.prompt,
                    skills=list(a.skills), tools=list(a.tools) if a.tools is not None else None,
                    enabled=a.enabled)
            for n, a in cfg.agents.items()]


def apply_team(cfg, roles: list[RoleDef] | None) -> None:
    """Перестроить cfg.agents по составу: порядок и поля ролей — из состава, модель — из конфига.
    Новой роли без модели в конфиге достаётся модель первого агента того же типа (иначе непишущего, иначе любого):
    заглушка mock/mock в реальном прогоне молча ничего бы не делала."""
    if roles is None:
        return
    from .config import AgentConfig
    old = cfg.agents
    new: dict = {}
    for r in roles:
        base = old.get(r.name)
        if base is None:
            same = (next((a for a in old.values() if a.kind == r.kind), None)          # тот же тип,
                    or next((a for a in old.values() if a.kind not in WRITER_KINDS), None)  # иначе непишущий,
                    or next(iter(old.values()), None))                               # иначе любой

            base = AgentConfig(name=r.name, observe_all=r.kind == "manager", compact=r.kind == "manager")
            if same is not None:
                base.model, base.temperature, base.max_tokens = same.model, same.temperature, same.max_tokens
        base.kind, base.title, base.description, base.prompt = r.kind, r.title, r.description, r.prompt
        base.skills = list(r.skills)
        if r.tools is not None:
            base.tools = list(r.tools)
        base.enabled = r.enabled
        new[r.name] = base
    cfg.agents = new


def title_of(cfg, name: str) -> str:
    a = cfg.agents.get(name) if cfg is not None else None
    if a is not None and a.title:
        return a.title
    if name in DEFAULT_TITLES:
        return DEFAULT_TITLES[name]
    return KINDS.get(a.kind if a is not None else default_kind(name), name)


# --------------------------------------------------------------- скиллы

def skills_dir(repo: str | Path) -> Path:
    return Path(repo) / SKILLS_DIR


def skill_path(repo: str | Path, skill: str) -> Path:
    if not NAME_RE.match(skill.replace("-", "_")):
        raise TeamError(f"имя скилла «{skill}»: латиница, цифры, - и _, от 2 до 20 символов")
    return skills_dir(repo) / f"{skill}.md"


def list_skills(repo: str | Path) -> list[str]:
    d = skills_dir(repo)
    return sorted(p.stem for p in d.glob("*.md")) if d.is_dir() else []


def read_skill(repo: str | Path, skill: str) -> str:
    try:
        return skill_path(repo, skill).read_text(encoding="utf-8")
    except (OSError, TeamError):
        return ""


def write_skill(repo: str | Path, skill: str, text: str) -> Path:
    p = skill_path(repo, skill)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def skills_section(repo: str | Path, skills: list[str]) -> str:
    """Текст скиллов роли для промпта. Нет файла — скилл пропускается."""
    parts = []
    for s in skills:
        text = read_skill(repo, s).strip()
        if not text:
            continue
        if len(text) > SKILL_LIMIT:
            text = text[:SKILL_LIMIT] + "\n… [обрезано]"
        parts.append(f"### {s}\n{text}")
    return ("## Скиллы роли\n" + "\n\n".join(parts)) if parts else ""
