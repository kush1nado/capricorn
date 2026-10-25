import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("commit_msg", ROOT / "scripts" / "commit_msg.py")
commit_msg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(commit_msg)


def test_clean_message_passes():
    assert commit_msg.problems("Fix routing\n\nDetails.\n") == []


@pytest.mark.parametrize("msg", [
    "Fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n",
    "Fix\n\n  co-authored-by: someone <a@b.c>",
])
def test_co_author_rejected(msg):
    assert any("Co-Authored-By" in p for p in commit_msg.problems(msg))


def test_git_comments_ignored():
    assert commit_msg.problems("# Co-Authored-By: в комментарии\nFix\n") == []
    assert commit_msg.problems("# только комментарий\n\n") == ["пустое сообщение"]


def test_main_reads_file(tmp_path):
    f = tmp_path / "COMMIT_EDITMSG"
    f.write_text("Fix\n\nCo-Authored-By: x <y@z>\n", encoding="utf-8")
    assert commit_msg.main([str(f)]) == 1
    f.write_text("Fix\n", encoding="utf-8")
    assert commit_msg.main([str(f)]) == 0


def test_claude_session_rejected():
    msg = "Fix\n\nClaude-Session: https://claude.ai/code/session_x\n"
    assert any("Claude-Session" in p for p in commit_msg.problems(msg))
