"""Маршрутизация по @упоминаниям и синхронные операции оркестратора (без запуска агентов)."""
import json

import pytest

from capricorn.orchestrator import USER
from helpers import build_orch, messages


@pytest.fixture
def orch(tmp_path):
    return build_orch(tmp_path)[0]


@pytest.mark.parametrize("sender, text, expected", [
    ("manager", "@architect глянь", ["architect"]),
    (USER, "@arch @PM @qa", ["architect", "manager", "tester"]),
    (USER, "@архитектор и @Менеджер", ["architect", "manager"]),
    (USER, "@review, потом @ревьюер", ["reviewer"]),             # дубликаты схлопываются
    ("architect", "@architect @reviewer", ["reviewer"]),         # себя не адресуем
    ("manager", "@coders за работу", ["coder1", "coder2"]),
    ("manager", "@coder3 тоже", []),                             # coder3 вне состава
    ("manager", "@user готово", [USER]),
    ("manager", "@me", [USER]),
    (USER, "@bob @", []),                                        # неизвестные — мимо
    (USER, "пиши на mail@coder1.com", []),                       # не упоминание
    (USER, "@@coder1", []),
])
def test_parse_targets(orch, sender, text, expected):
    assert orch.parse_targets(sender, text) == expected


def test_all_excludes_sender_and_user(orch):
    assert orch.parse_targets("manager", "@all") == ["architect", "coder1", "coder2", "reviewer", "tester"]


def test_complex_mode_adds_coder3(orch):
    orch.set_complex(True)
    assert orch.parse_targets("manager", "@coders") == ["coder1", "coder2", "coder3"]
    assert orch.parse_targets("manager", "@coder3") == ["coder3"]
    assert "coder3" in orch.roster


@pytest.mark.parametrize("text", [
    "```python\n# @coder1\nx = 1\n```",
    "вызови `@coder2` декоратор",
    "```\n@reviewer незакрытый блок",
])
def test_mentions_in_code_are_ignored(orch, text):
    assert orch.parse_targets("manager", text) == []


def test_mention_outside_code_still_counts(orch):
    assert orch.parse_targets("manager", "```\n@coder1\n```\n@coder2 сделай") == ["coder2"]


def test_user_without_mentions_goes_to_default_target(tmp_path):
    orch, events = build_orch(tmp_path, default_target="architect")
    msg = orch.route(USER, "сделай сервис")
    assert msg.to == ["architect"]
    assert orch.agents["architect"].pending == [msg]
    assert orch.agents["architect"].wake.is_set()
    assert messages(events) == [msg]


def test_agent_without_mentions_routes_nowhere(orch):
    msg = orch.route("coder1", "просто мысль вслух")
    assert msg.to == []
    assert not any(a.pending for a in orch.agents.values())


def test_observe_all_gets_context_not_turn(orch):
    msg = orch.route("architect", "@reviewer оцени")
    assert orch.agents["reviewer"].pending == [msg]
    assert orch.agents["manager"].context == [msg]
    assert orch.agents["manager"].pending == []
    assert not orch.agents["manager"].wake.is_set()
    assert orch.agents["tester"].context == []

    direct = orch.route("architect", "@manager готово")
    assert orch.agents["manager"].pending == [direct]
    assert orch.agents["manager"].context == [msg]           # адресованное не дублируется


def test_messages_are_logged_as_jsonl(orch, isolated_home):
    orch.route(USER, "привет @architect")
    lines = orch.log_path.read_text(encoding="utf-8").splitlines()
    # папка без git — данные проекта в домашнем каталоге, лог — в каталоге сессии
    assert orch.log_path.parent.parent.parent.parent == isolated_home / ".local" / "share" / "capricorn" / "projects"
    assert orch.log_path.parent.name == orch.session_id
    rec = json.loads(lines[0])
    assert (rec["sender"], rec["to"], rec["content"]) == (USER, ["architect"], "привет @architect")


def test_message_ids_are_unique_and_increasing(orch):
    ids = [orch.route(USER, "a").id, orch.route(USER, "b").id]
    assert ids[0] < ids[1]


def test_user_says_appends_attachments_once_and_restores_budget(orch, tmp_path):
    (tmp_path / "app.py").write_text("print('ок')\n", encoding="utf-8")
    orch.attach("app.py")
    orch.budget = 2
    msg = orch.user_says("посмотри")
    assert msg.content.startswith("посмотри\n\nФайл `app.py`:\n```\nprint('ок')\n")
    assert orch.attachments == []
    assert orch.budget == orch.cfg.budget
    assert orch.user_says("ещё").content == "ещё"


def test_user_says_keeps_bigger_budget(orch):
    orch.budget = 100
    orch.user_says("задача")
    assert orch.budget == 100


def test_attach_absolute_and_truncated(orch, tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 70_000, encoding="utf-8")
    assert orch.attach(str(big)) == str(big)
    assert orch.attachments[0].endswith("… [обрезано]\n```")
    assert len(orch.attachments[0]) < 61_000


def test_attach_missing_file_raises(orch):
    with pytest.raises(FileNotFoundError):
        orch.attach("nope.py")
    assert orch.attachments == []


def test_set_model_validates_provider(orch):
    with pytest.raises(ValueError, match="нет провайдера"):
        orch.set_model("coder1", "ghost/model")
    orch.set_model("coder1", "mock/x")
    assert orch.cfg.agents["coder1"].model == "mock/x"


def test_reset_one_and_all(orch):
    for a in orch.agents.values():
        a.history.append({"role": "user", "content": "x"})
    orch.route("architect", "@reviewer @tester")
    orch.reset("reviewer")
    assert orch.agents["reviewer"].history == [] and orch.agents["reviewer"].pending == []
    assert orch.agents["tester"].pending
    orch.reset()
    assert not any(a.history or a.pending or a.context for a in orch.agents.values())
