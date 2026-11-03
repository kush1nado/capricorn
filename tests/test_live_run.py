"""scripts/live_run.py: ответы пользователя во время прогона — строки, дописанные в файл --answers."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("live_run", ROOT / "scripts" / "live_run.py")
live_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live_run)


def test_new_answers_reads_only_appended_lines(tmp_path):
    f = tmp_path / "answers.txt"
    assert live_run.new_answers(f, 0) == ([], 0), "файла ещё нет"
    f.write_text("вариант А\n\n", encoding="utf-8")
    lines, pos = live_run.new_answers(f, 0)
    assert lines == ["вариант А"]
    with f.open("a", encoding="utf-8") as fh:
        fh.write("@coder1 продолжай\nнедописан")
    lines, pos = live_run.new_answers(f, pos)
    assert lines == ["@coder1 продолжай"], "строку без перевода строки ещё пишут"
    with f.open("a", encoding="utf-8") as fh:
        fh.write("ная\n")
    assert live_run.new_answers(f, pos)[0] == ["недописанная"]


def test_waiting_for_user():
    from capricorn.orchestrator import Message
    ask = Message(id=1, sender="manager", to=["user"], content="А или Б?")
    reply = Message(id=2, sender="user", to=["manager"], content="А")
    assert live_run.waiting_for_user([ask])
    assert not live_run.waiting_for_user([ask, reply])
    assert not live_run.waiting_for_user([])
