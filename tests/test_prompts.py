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


def test_tools_section_for_coder_with_write():
    sp = system_prompt("coder1", ROSTER, "ru", "", tools=["read_file", "write_file", "edit_file", "run_tests"])
    assert "## Инструменты\nДоступны: read_file, write_file, edit_file, run_tests." in sp
    assert "write_file для новых" in sp and "run_tests" in sp
    assert "git_diff" not in sp
    assert "Инструментов записи у тебя нет" not in sp


def test_coder_without_tools_gets_code_block_hint():
    assert "Инструментов записи у тебя нет" in system_prompt("coder1", ROSTER, "ru", "")
    assert "Инструментов записи у тебя нет" in system_prompt("tester", ROSTER, "ru", "", tools=["read_file"])
    assert "Инструментов" not in system_prompt("architect", ROSTER, "ru", "")


def test_reviewer_read_only_tools():
    sp = system_prompt("reviewer", ROSTER, "ru", "", tools=["read_file", "git_diff"])
    assert "git_diff" in sp and "write_file" not in sp and "Инструментов записи" not in sp


def test_worktree_hints_per_role():
    coder = system_prompt("coder2", ROSTER, "ru", "", tools=["write_file", "sync_branch"], worktrees=True)
    assert "своей ветке capricorn/coder2" in coder and "merge_work" not in coder
    rev = system_prompt("reviewer", ROSTER, "ru", "", tools=["git_diff"], worktrees=True)
    assert 'git_diff(worktree="coder1")' in rev and "своей ветке" not in rev
    mgr = system_prompt("manager", ROSTER, "ru", "", tools=["git_diff", "merge_work"], worktrees=True)
    assert "merge_work(agent=" in mgr and 'worktree="coder1"' in mgr
    plain = system_prompt("reviewer", ROSTER, "ru", "", tools=["git_diff"], worktrees=False)
    assert "worktree" not in plain
