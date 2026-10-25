from capricorn.prompts import ROLE_PROMPTS, system_prompt

ROSTER = ["architect", "manager", "coder1", "coder2", "reviewer", "tester"]


def test_marks_self_and_lists_roster():
    sp = system_prompt("reviewer", ROSTER, "ru", "")
    assert sp.startswith("# Ты: @reviewer — Ревьюер")
    assert "- @reviewer — Ревьюер (это ты)" in sp
    assert "- @architect — Архитектор\n" in sp
    assert sp.count("(это ты)") == 1
    assert "Язык общения: русский" in sp
    assert "## Репозиторий" not in sp


def test_coders_share_base_prompt():
    sp = system_prompt("coder2", ROSTER, "en", "")
    assert ROLE_PROMPTS["coder"] in sp
    assert "Язык общения: английский" in sp


def test_repo_context_and_extra_appended_last():
    sp = system_prompt("manager", ROSTER, "de", "Ветка: main", extra="EXTRA")
    assert "## Репозиторий\nВетка: main" in sp
    assert sp.endswith("EXTRA")
    assert "Язык общения: de" in sp       # неизвестный код языка — как есть


def test_every_role_has_prompt():
    for role in ROSTER + ["coder3"]:
        system_prompt(role, ROSTER, "ru", "")
