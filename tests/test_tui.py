"""Smoke-тесты интерфейса через Textual Pilot (headless)."""
import asyncio

import pytest

from capricorn.config import load_config
from capricorn.tui import CapricornApp, GitPanel, Item


def feed_texts(app) -> list[str]:
    return [str(i.content) for i in app.feed.query(Item)]


def panel_texts(app, key) -> list[str]:
    return [str(i.content) for i in app.query_one(f"#p-{key}").query(Item)]


async def wait_for(pilot, cond, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("условие не выполнилось")
        await pilot.pause(0.02)


async def submit(pilot, text):
    pilot.app.query_one("#prompt").value = text
    await pilot.press("enter")
    await pilot.pause()


@pytest.fixture
async def ui(tmp_path, fast_mock):
    app = CapricornApp(load_config(force_mock=True), str(tmp_path))
    async with app.run_test(size=(180, 50)) as pilot:
        yield app, pilot
        app.orch.stop_all()
        for t in app.orch._tasks:
            t.cancel()


async def test_boot(ui):
    app, pilot = ui
    await wait_for(pilot, lambda: "не git" in str(app.query_one(GitPanel).content))
    assert any("CAPRICORN ONLINE" in t for t in feed_texts(app))
    assert app.panels["coder3"].display is False
    assert "mock/architect" in app.panels["architect"].border_subtitle
    assert app.focused is app.query_one("#prompt")


async def test_task_flows_through_team(ui):
    app, pilot = ui
    await submit(pilot, "сделай сервис")
    await wait_for(pilot, lambda: any("Готово" in t for t in feed_texts(app)))
    assert panel_texts(app, "manager") and panel_texts(app, "architect")
    assert app.query_one("#prompt").value == ""
    assert app.query_one("#prompt").history == ["сделай сервис"]


async def test_commands(ui):
    app, pilot = ui
    o = app.orch
    await submit(pilot, "/help")
    assert any("/continue" in t for t in feed_texts(app))

    await submit(pilot, "/budget 5")
    assert o.budget == 5
    await submit(pilot, "/continue")
    assert o.budget == 20

    await submit(pilot, "/complex on")
    assert app.panels["coder3"].display is True and o.cfg.complex_mode
    await submit(pilot, "/complex off")
    assert app.panels["coder3"].display is False

    await submit(pilot, "/model coder1 mock/other")
    assert o.cfg.agents["coder1"].model == "mock/other"
    assert "mock/other" in app.panels["coder1"].border_subtitle

    await submit(pilot, "/pause")
    assert not o.running.is_set()
    await pilot.press("f5")
    assert o.running.is_set()

    await submit(pilot, "/zoom architect")
    assert app.screen.maximized is app.query_one("#p-architect")
    await pilot.press("escape")
    assert app.screen.maximized is None


@pytest.mark.parametrize("line, needle", [
    ("/nope", "неизвестная команда"),
    ("/model ghost mock/x", "нет агента"),
    ("/model coder1 ghost/x", "нет провайдера"),
    ("/attach missing.py", "missing.py"),
    ("/model coder1", "не хватает аргументов"),
    ("/", "не хватает аргументов"),
    ('/attach "незакрытая', "No closing quotation"),
])
async def test_command_errors_are_shown_not_raised(ui, line, needle):
    app, pilot = ui
    await submit(pilot, line)
    assert any(needle in t and t.startswith("▲") for t in feed_texts(app)), feed_texts(app)[-1]


async def test_tools_command(ui):
    app, pilot = ui
    await submit(pilot, "/tools")
    out = feed_texts(app)[-1]
    assert "coder1" in out and "write_file" in out
    arch = [l for l in out.splitlines() if l.startswith("architect")][0]
    assert "write_file" not in arch
    assert "тесты: python -m pytest -q" in out
    app.cfg.tools.enabled = False
    await submit(pilot, "/tools")
    assert "выключены" in feed_texts(app)[-1]


async def test_working_status_counts_as_active(ui):
    app, pilot = ui
    app.panels["coder1"].set_meta(status="working")
    assert app.panels["coder1"].has_class("active")


async def test_timers_survive_teardown(ui):
    """Интервалы могут сработать, когда виджеты уже сняты (выход из приложения)."""
    app, pilot = ui
    await app.query_one("#status").remove()
    await app.query_one(GitPanel).remove()
    app.update_status()
    await app.refresh_git().wait()


@pytest.fixture
async def ui_git(tmp_path, fast_mock):
    from helpers import git, init_repo
    repo = tmp_path / "gitrepo"
    init_repo(repo)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    app = CapricornApp(load_config(force_mock=True), str(repo))
    async with app.run_test(size=(180, 50)) as pilot:
        yield app, pilot
        app.orch.stop_all()
        for t in app.orch._tasks:
            t.cancel()


async def test_worktrees_command(ui_git):
    app, pilot = ui_git
    await submit(pilot, "/worktrees")
    await wait_for(pilot, lambda: any("ещё не создан" in t for t in feed_texts(app)))
    out = [t for t in feed_texts(app) if "ещё не создан" in t][-1]
    assert out.splitlines()[0].startswith("coder1") and "tester" in out and "reviewer" not in out
    await submit(pilot, "/worktrees prune")
    await wait_for(pilot, lambda: any("убирать нечего" in t for t in feed_texts(app)))


async def test_worktrees_command_without_git(ui):
    app, pilot = ui
    await submit(pilot, "/worktrees")
    await wait_for(pilot, lambda: any("worktree выключены" in t for t in feed_texts(app)))


def test_help_markup_renders():
    """Регрессия: «[prune]» в HELP Rich принял за стиль, и /help падал. Скобки со строчной буквой экранируй."""
    from rich.text import Text
    from capricorn.tui import HELP
    plain = Text.from_markup(HELP).plain
    assert "/worktrees [prune]" in plain and "/continue [N]" in plain


async def test_merge_command(ui_git):
    app, pilot = ui_git
    await app.orch.toolbox.run("coder1", "write_file", {"path": "m.py", "content": "m"})
    await submit(pilot, "/merge coder1")
    await wait_for(pilot, lambda: any("влита" in t for t in feed_texts(app)))
    assert (app.orch.toolbox.root / "m.py").exists()
    await submit(pilot, "/merge nobody")
    await wait_for(pilot, lambda: any("нет агента" in t for t in feed_texts(app)))
