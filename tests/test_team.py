"""Настраиваемая команда: роли с типами в .capricorn/team.yaml, скиллы, свой промпт, /off и /on на задачу."""
import asyncio

import pytest

from capricorn import start
from capricorn.config import load_config
from capricorn.prompts import system_prompt
from capricorn.team import (TeamError, apply_team, load_team, parse_role, read_skill, roles_of, save_team,
                            skills_section, write_skill)
from helpers import build_orch, git, init_repo, messages, settle

TEAM = """\
roles:
  lead:     {kind: architect, title: Техлид}
  dev:      {kind: coder}
  security:
    kind: reviewer
    title: Безопасник
    description: ищет дыры в безопасности
    skills: [owasp]
  reviewer: {}
  qa:       {kind: tester, enabled: false}
"""


def write_team(repo, text=TEAM):
    (repo / ".capricorn").mkdir(parents=True, exist_ok=True)
    (repo / ".capricorn" / "team.yaml").write_text(text, encoding="utf-8")


@pytest.fixture
async def team_orch(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    created = []

    def factory(replies=None, team=TEAM, **kw):
        if team is not None:
            write_team(repo, team)
        orch, events = build_orch(repo, replies, **kw)
        orch.start()
        created.append(orch)
        return orch, events, repo

    yield factory
    for orch in created:
        for t in orch._tasks:
            t.cancel()
        await asyncio.gather(*orch._tasks, return_exceptions=True)


# ------------------------------------------------------------- team.yaml

def test_no_team_file_means_default_team(tmp_path):
    assert load_team(tmp_path) is None


def test_load_team_keeps_order_and_kinds(tmp_path):
    write_team(tmp_path)
    roles = load_team(tmp_path)
    assert [r.name for r in roles] == ["lead", "dev", "security", "reviewer", "qa"]
    assert [r.kind for r in roles] == ["architect", "coder", "reviewer", "reviewer", "tester"]
    sec = roles[2]
    assert (sec.title, sec.description, sec.skills) == ("Безопасник", "ищет дыры в безопасности", ["owasp"])
    assert roles[4].enabled is False


@pytest.mark.parametrize("text, err", [
    ("roles:\n  x1: {kind: boss}\n", "тип «boss»"),
    ("roles:\n  Bad: {kind: coder}\n", "латиница"),
    ("roles:\n  all: {kind: coder}\n", "занято"),
    ("roles: {}\n", "непустой"),
    ("roles: [\n", "YAML"),
])
def test_bad_team_file_is_reported(tmp_path, text, err):
    write_team(tmp_path, text)
    with pytest.raises(TeamError, match=err):
        load_team(tmp_path)


def test_save_and_load_roundtrip(tmp_path):
    write_team(tmp_path)
    roles = load_team(tmp_path)
    roles[1].prompt = "Пиши на Go."
    save_team(tmp_path, roles)
    again = load_team(tmp_path)
    assert [(r.name, r.kind, r.prompt, r.enabled) for r in again] == [
        (r.name, r.kind, r.prompt, r.enabled) for r in roles]


def test_new_role_takes_model_of_same_kind():
    cfg = load_config()
    cfg.agents["reviewer"].model = "claude-code/sonnet"
    cfg.agents["coder1"].model = "codex/default"
    write = parse_role
    apply_team(cfg, [write("security", {"kind": "reviewer"}), write("dev", {"kind": "coder"}),
                     write("coder1", {})])
    assert list(cfg.agents) == ["security", "dev", "coder1"]
    assert cfg.agents["security"].model == "claude-code/sonnet"
    assert cfg.agents["dev"].model == "codex/default"
    assert [r.name for r in roles_of(cfg)] == ["security", "dev", "coder1"]


def test_extra_agent_in_capricorn_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("agents:\n  security: {model: mock/x, kind: reviewer, title: Безопасник}\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.agents["security"].kind == "reviewer" and cfg.agents["security"].model == "mock/x"
    assert cfg.is_writer("coder1") and not cfg.is_writer("security")


def test_complex_mode_is_coder3_in_team():
    cfg = load_config()
    assert cfg.complex_mode is False and cfg.agents["coder3"].enabled is False
    cfg.complex_mode = True
    assert cfg.agents["coder3"].enabled is True


def test_preset_for_custom_role_uses_kind():
    assert start.preset_key("security", "reviewer") == "reviewer"
    assert start.preset_key("dev", "coder") == "coder1"
    assert start.preset_key("guru", "expert") == "reviewer"


# --------------------------------------------------------------- скиллы

def test_skills_go_into_prompt_section(tmp_path):
    write_skill(tmp_path, "owasp", "Проверяй инъекции.")
    assert read_skill(tmp_path, "owasp") == "Проверяй инъекции."
    sec = skills_section(tmp_path, ["owasp", "missing"])
    assert sec.startswith("## Скиллы роли") and "### owasp\nПроверяй инъекции." in sec
    assert "missing" not in sec
    assert skills_section(tmp_path, ["missing"]) == ""
    with pytest.raises(TeamError):
        write_skill(tmp_path, "../evil", "x")


async def test_agent_prompt_has_skills_title_and_description(team_orch):
    orch, _, repo = team_orch({"security": ["ок"]})
    write_skill(repo, "owasp", "Проверяй SQL-инъекции.")
    orch.route("dev", "@security глянь")
    await settle(orch)
    system = orch.provider_for("security").calls_for("security")[0].system
    assert system.startswith("# Ты: @security — Безопасник")
    assert "ищет дыры в безопасности" in system and "Проверяй SQL-инъекции." in system
    assert "- @lead — Техлид" in system and "qa" not in system, "не в составе — не в списке"


def test_custom_prompt_replaces_default():
    cfg = load_config()
    cfg.agents["coder1"].prompt = "Ты пишешь только на Rust."
    sp = system_prompt("coder1", ["architect", "coder1"], "ru", "", team=cfg)
    assert "Ты пишешь только на Rust." in sp and "Ты — разработчик" not in sp


# --------------------------------------------------- маршрутизация по типам

async def test_roster_routing_and_kinds(team_orch):
    orch, _, _ = team_orch({})
    assert orch.roster == ["lead", "dev", "security", "reviewer"]
    assert orch.lead == "lead" and orch.default_target == "lead", "менеджера нет — задача ведущему"
    assert orch.parse_targets("dev", "@security @qa") == ["security"], "qa не в составе"
    assert orch.parse_targets("dev", "@ревьюер") == ["security", "reviewer"], "тип без имени — все этого типа"
    assert orch.parse_targets("dev", "@reviewer") == ["reviewer"], "точное имя сильнее типа"
    assert orch.parse_targets("lead", "@coders") == ["dev"]
    assert orch.approvers == ["security", "reviewer"]
    assert orch.toolbox.writers() == ["dev"]


async def test_user_task_goes_to_lead_without_manager(team_orch):
    orch, events, _ = team_orch({"lead": ["@dev сделай"], "dev": ["ок"]})
    orch.user_says("задача")
    await settle(orch)
    assert messages(events)[0].to == ["lead"]


async def test_merge_waits_for_all_addressed_reviewers(team_orch):
    orch, events, repo = team_orch({"dev": ["Готово, @security @reviewer"],
                                    "security": ["APPROVED"], "reviewer": ["APPROVED"], "lead": ["ок"]})
    await orch.toolbox.run("dev", "write_file", {"path": "f.py", "content": "ok\n"})
    orch.route("lead", "@dev сделай")
    await settle(orch)
    git_msgs = [m for m in messages(events) if m.sender == "git"]
    assert len(git_msgs) == 1 and "влита" in git_msgs[0].content and git_msgs[0].to == ["lead"]
    assert (repo / "f.py").exists()


async def test_merge_does_not_happen_until_second_reviewer_agrees(team_orch):
    orch, events, repo = team_orch({"dev": ["Готово, @security @reviewer"],
                                    "security": ["APPROVED"], "reviewer": ["@dev нет тестов"],
                                    "lead": ["ок"]})
    await orch.toolbox.run("dev", "write_file", {"path": "f.py", "content": "ok\n"})
    orch.route("lead", "@dev сделай")
    await settle(orch)
    assert not [m for m in messages(events) if m.sender == "git"]
    assert not (repo / "f.py").exists()


TEAM_NO_REVIEW = "roles:\n  architect: {}\n  coder1: {}\n"


async def test_without_reviewer_lead_approves_and_merges(team_orch):
    orch, events, repo = team_orch({"coder1": ["Готово, @architect"], "architect": ["APPROVED", "Итог: готово."]},
                                   team=TEAM_NO_REVIEW)
    await orch.toolbox.run("coder1", "write_file", {"path": "f.py", "content": "ok\n"})
    orch.route("user", "@coder1 сделай")
    await settle(orch)
    assert (repo / "f.py").exists()
    assert orch.approvers == ["architect"]
    coder = orch.provider_for("coder1").calls_for("coder1")[0].system
    assert "Ревьюера в команде нет" in coder and "@architect" in coder
    lead = orch.provider_for("architect").calls_for("architect")[0].system
    assert "проверяешь ты" in lead and "Менеджера в команде нет" in lead


def test_manager_leads_without_architect():
    cfg = load_config()
    sp = system_prompt("manager", ["manager", "coder1", "reviewer"], "ru", "", team=cfg)
    assert "архитектора в команде нет" in sp and "@architect" not in sp
    coder = system_prompt("coder1", ["manager", "coder1", "reviewer"], "ru", "", team=cfg)
    assert "@architect" not in coder and "спрашивай @manager" in coder


# ---------------------------------------------------------- /off и /on

async def test_off_removes_role_for_this_task(team_orch):
    orch, events, _ = team_orch({"dev": ["Готово, @security @reviewer"], "reviewer": ["APPROVED"],
                                 "security": ["не должен"], "lead": ["ок"]})
    orch.set_off("security", True)
    assert "security" not in orch.roster and orch.approvers == ["reviewer"]
    orch.route("lead", "@dev сделай")
    await settle(orch)
    assert orch.agents["security"].turns == 0
    assert orch.snapshot()["off"] == ["security"]
    orch.set_off("security", False)
    assert "security" in orch.roster


async def test_off_is_cleared_by_new_session_and_restored(team_orch):
    orch, _, _ = team_orch({})
    orch.set_off("reviewer", True)
    state = orch.snapshot()
    orch.new_session()
    assert orch.off == set()
    orch.restore(state)
    assert orch.off == {"reviewer"}


async def test_off_errors(team_orch):
    orch, _, _ = team_orch({}, team="roles:\n  architect: {}\n  coder1: {}\n  qa: {kind: tester, enabled: false}\n")
    with pytest.raises(ValueError, match="нет агента"):
        orch.set_off("ghost", True)
    with pytest.raises(ValueError, match="не в составе"):
        orch.set_off("qa", True)
    orch.set_off("coder1", True)
    with pytest.raises(ValueError, match="последнего"):
        orch.set_off("architect", True)


# ------------------------------------------------------ ошибки и смена состава

async def test_broken_team_file_falls_back_to_default(team_orch):
    orch, _, _ = team_orch({}, team="roles:\n  x1: {kind: boss}\n")
    assert "boss" in orch.team_error
    assert "architect" in orch.roster and "x1" not in orch.agents


async def test_set_team_adds_and_removes_agents(team_orch):
    orch, _, repo = team_orch({"guru": ["знаю"]})
    roles = load_team(repo)
    roles = [r for r in roles if r.name != "reviewer"]
    roles.append(parse_role("guru", {"kind": "expert", "description": "про базы данных"}))
    orch.set_team(roles)
    assert "reviewer" not in orch.agents and list(orch.agents)[-1] == "guru"
    assert orch.agents["guru"].loop_task is not None
    orch.route("dev", "@guru вопрос?")
    await settle(orch)
    assert orch.agents["guru"].turns == 1
    assert orch.cfg.agents["guru"].model == "script/test", "модель — как у первого агента того же типа или по умолчанию"


async def test_set_team_refuses_while_someone_works(team_orch):
    gate = asyncio.Event()

    async def slow(agent, incoming):
        await gate.wait()
        return "ок"

    orch, _, repo = team_orch({"dev": [slow]})
    orch.route("lead", "@dev сделай")
    await asyncio.sleep(0.05)
    with pytest.raises(ValueError, match="работают: dev"):
        orch.set_team(load_team(repo))
    gate.set()
    await settle(orch)


async def test_mock_plays_role_by_model_for_custom_names(fast_mock):
    """--mock: новая роль lead с моделью mock/architect отвечает как архитектор, а не «...»."""
    from capricorn.providers import MockProvider
    p = MockProvider("mock", {})
    chunks = [c async for c in p.stream(model="architect", system="", agent="lead", temperature=0, max_tokens=10,
                                        messages=[{"role": "user", "content": "[от manager → lead]: набросай"}])]
    text = "".join(c for c in chunks if isinstance(c, str))
    assert "слоя" in text
