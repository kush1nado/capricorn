"""Разрешение конфликтов: разбор маркеров, сборка результата, git-операции и вопрос агенту."""
import pytest

from capricorn import conflicts as cx
from capricorn.gitinfo import GitWatcher
from helpers import conflicted_repo, git, init_repo

TEXT = ("a = 1\n"
        "<<<<<<< HEAD\nb = 200\n=======\nb = 20\n>>>>>>> other\n"
        "c = 3\n"
        "<<<<<<< HEAD\nd = 4\n=======\nd = 40\ne = 5\n>>>>>>> other\n")


def test_parse_splits_text_and_hunks():
    cf = cx.parse(TEXT)
    assert len(cf.hunks) == 2 and cf.unresolved == 2
    h = cf.hunks[0]
    assert (h.ours, h.theirs, h.ours_label, h.theirs_label) == (["b = 200\n"], ["b = 20\n"], "HEAD", "other")
    assert cf.render() == TEXT, "без выбора файл собирается обратно как был"


def test_choices_build_result():
    cf = cx.parse(TEXT)
    cf.hunks[0].choice = cx.THEIRS
    cf.hunks[1].choice = cx.BOTH
    assert cf.unresolved == 0
    assert cf.render() == "a = 1\nb = 20\nc = 3\nd = 4\nd = 40\ne = 5\n"
    cf.hunks[1].choice = cx.OURS
    assert cf.render().endswith("c = 3\nd = 4\n")


def test_agent_choice_needs_proposal():
    cf = cx.parse(TEXT)
    h = cf.hunks[0]
    h.choice = cx.AGENT
    assert not h.resolved and "<<<<<<<" in cf.render()
    h.proposal = ["b = 220\n"]
    assert h.resolved and cf.render().startswith("a = 1\nb = 220\nc = 3\n")


def test_diff3_and_crlf():
    text = "x\r\n<<<<<<< ours\r\nA\r\n||||||| base\r\nO\r\n=======\r\nB\r\n>>>>>>> theirs\r\ny\r\n"
    cf = cx.parse(text)
    h = cf.hunks[0]
    assert h.base == ["O\r\n"] and h.theirs == ["B\r\n"]
    assert cf.render() == text
    h.choice = cx.BOTH
    assert cf.render() == "x\r\nA\r\nB\r\ny\r\n"


def test_separator_line_inside_theirs_is_content():
    cf = cx.parse("<<<<<<< a\nX\n=======\nY\n=======\n>>>>>>> b\n")
    assert cf.hunks[0].theirs == ["Y\n", "=======\n"]


@pytest.mark.parametrize("bad", ["<<<<<<< a\nX\n=======\nY\n", "<<<<<<< a\nX\n<<<<<<< b\n"])
def test_unclosed_block_is_error(bad):
    with pytest.raises(cx.ConflictParseError):
        cx.parse(bad)


def test_context_around_hunk():
    cf = cx.parse(TEXT)
    before, after = cf.context(cf.hunks[1], 2)
    assert before == ["c = 3\n"] and after == []


def test_proposal_takes_last_code_block_and_file_eol():
    ans = "Вот так:\n```python\nb = 220\nb2 = 1\n```\nобъединил оба изменения"
    assert cx.proposal_lines(ans, ["x\r\n"]) == ["b = 220\r\n", "b2 = 1\r\n"]
    assert cx.proposal_lines("plain\n", ["x\n"]) == ["plain\n"]


def test_agent_prompt_has_both_sides():
    cf = cx.parse(TEXT)
    p = cx.agent_prompt("calc.py", cf, cf.hunks[0])
    assert "calc.py" in p and "b = 200" in p and "b = 20\n" in p and "a = 1" in p


# ------------------------------------------------------------------ git
@pytest.fixture
def repo(tmp_path):
    return conflicted_repo(tmp_path / "r")


async def test_merge_state_and_conflicts(repo, tmp_path):
    w = GitWatcher(str(repo))
    assert await w.merge_state() == "merge"
    assert {c.path for c in await w.changes() if c.conflict} == {"calc.py", "gone.txt"}
    clean = tmp_path / "clean"
    init_repo(clean)
    assert await GitWatcher(str(clean)).merge_state() == ""


async def test_save_resolved_then_continue(repo):
    w = GitWatcher(str(repo))
    cf = cx.parse(w.read_text("calc.py"))
    cf.hunks[0].choice = cx.THEIRS
    assert (await w.save_resolved("calc.py", cf.render()))[0]
    ok, out = await w.continue_op()
    assert not ok and "gone.txt" in out, "пока остались конфликты, merge не продолжаем"
    assert (await w.take_side("gone.txt", "ours"))[0]          # у нас файл удалён — принимаем удаление
    assert not (repo / "gone.txt").exists()
    ok, out = await w.continue_op()
    assert ok, out
    assert await w.merge_state() == ""
    assert (repo / "calc.py").read_text(encoding="utf-8") == "a = 1\nb = 20\nc = 3\n"
    assert git(repo, "log", "-1", "--format=%p").count(" ") == 1, "получился merge-коммит"


async def test_take_side_theirs_and_abort(repo):
    w = GitWatcher(str(repo))
    assert (await w.take_side("calc.py", "theirs"))[0]
    assert (repo / "calc.py").read_text(encoding="utf-8") == "a = 1\nb = 20\nc = 3\n"
    assert not any(c.conflict for c in await w.changes() if c.path == "calc.py")
    assert not (await w.take_side("calc.py", "mine"))[0]
    assert (await w.abort_op())[0]
    assert await w.merge_state() == ""
    assert (repo / "calc.py").read_text(encoding="utf-8") == "a = 1\nb = 200\nc = 3\n"
    assert not (await w.abort_op())[0]


# ------------------------------------------------------------------ агент
async def test_ask_once_does_not_touch_team_history(make_orch):
    orch, _ = make_orch({"architect#once": "```\nb = 220\n```\nсложил"})
    agent = orch.agents["architect"]
    before = list(agent.history)
    answer = await orch.ask_once("architect", cx.RESOLVER_SYSTEM, "реши")
    assert "b = 220" in answer
    assert agent.history == before, "разовый вопрос не попадает в историю команды"
    call = orch.provider_for("architect").calls_for("architect#once")[0]
    assert call.system == cx.RESOLVER_SYSTEM and call.messages == [{"role": "user", "content": "реши"}]
