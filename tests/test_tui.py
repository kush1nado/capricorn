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
    assert any(needle in t and t.startswith("⚠") for t in feed_texts(app)), feed_texts(app)[-1]
