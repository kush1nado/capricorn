"""Smoke-тесты интерфейса через Textual Pilot (headless)."""
import asyncio

import pytest

from capricorn.config import load_config
from capricorn.tui import PANEL_ORDER, CapricornApp, GitPanel, Item


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


async def test_session_commands(ui, tmp_path):
    app, pilot = ui
    o = app.orch
    await submit(pilot, "сделай сервис")
    await wait_for(pilot, lambda: any("Готово" in t for t in feed_texts(app)))
    first = o.session_id

    await submit(pilot, "/export notes/session.md")
    out = tmp_path / "notes" / "session.md"
    assert out.exists() and "сделай сервис" in out.read_text(encoding="utf-8")

    await submit(pilot, "/new")
    assert o.session_id != first and o.messages == []
    assert not any("сделай сервис" in t for t in feed_texts(app))
    assert all(not a.history for a in o.agents.values())

    await submit(pilot, "/sessions")
    assert first in feed_texts(app)[-1] and "сделай сервис" in feed_texts(app)[-1]

    await submit(pilot, "/session last")
    assert o.session_id == first
    assert any("сделай сервис" in t for t in feed_texts(app))                   # лента перерисована
    assert panel_texts(app, "architect")                                           # и панели агентов

    await submit(pilot, "/session nope")
    assert "нет сессии" in feed_texts(app)[-1]


async def test_resume_flag_restores_last_session(tmp_path, fast_mock):
    repo = str(tmp_path)
    app = CapricornApp(load_config(force_mock=True), repo)
    async with app.run_test(size=(180, 50)) as pilot:
        await submit(pilot, "первая задача")
        await wait_for(pilot, lambda: any("Готово" in t for t in feed_texts(app)))
        sid = app.orch.session_id
        app.orch.stop_all()
    again = CapricornApp(load_config(force_mock=True), repo, resume=True)
    async with again.run_test(size=(180, 50)) as pilot:
        await pilot.pause()
        assert again.orch.session_id == sid
        assert any("первая задача" in t for t in feed_texts(again))
        assert any("продолжаем сессию" in t for t in feed_texts(again))
        again.orch.stop_all()
    fresh = CapricornApp(load_config(force_mock=True), repo)
    async with fresh.run_test(size=(180, 50)) as pilot:
        await pilot.pause()
        assert fresh.orch.session_id != sid
        assert any("есть прошлая сессия" in t for t in feed_texts(fresh))
        fresh.orch.stop_all()


def test_help_mentions_session_commands():
    from rich.text import Text
    from capricorn.tui import HELP
    plain = Text.from_markup(HELP).plain
    assert "/export [файл]" in plain and "/session <id|last>" in plain


# ------------------------------------------------------- окна и alt-клавиши

def selected(app):
    return [k for k in PANEL_ORDER if app.query_one(f"#p-{k}").has_class("selected")]


async def test_alt_selection_and_movement(ui):
    app, pilot = ui
    assert selected(app) == ["manager"]
    await pilot.press("alt+2")
    assert selected(app) == ["architect"]
    await pilot.press("alt+right")
    assert app.selected == "manager"
    await pilot.press("alt+down")
    assert app.selected == "reviewer"
    await pilot.press("alt+left")
    assert app.selected == "coder2"
    await pilot.press("alt+6")                       # coder3 скрыт — выбор не меняется
    assert app.selected == "coder2"
    await pilot.press("alt+down")                    # под coder2 — лента
    assert app.selected == "feed"


async def test_alt_space_opens_private_window_and_hands_off(ui):
    from capricorn.tui import FocusScreen
    app, pilot = ui
    app.query_one("#prompt").focus()
    await pilot.press("alt+2", "alt+space")          # срабатывает даже из строки ввода
    assert isinstance(app.screen, FocusScreen) and app.screen.agent == "architect"
    app.screen.query_one("#focus-input").value = "как делим модули?"
    await pilot.press("enter")
    await wait_for(pilot, lambda: app.orch.agents["architect"].turns == 1)
    await pilot.pause(0.2)
    texts = [str(i.content) for i in app.screen.panel.query(Item)]
    assert any("как делим модули?" in t for t in texts) and len(texts) >= 2
    assert app.orch.agents["reviewer"].turns == 0               # @упоминания в личном не ушли

    await pilot.press("escape")
    assert not isinstance(app.screen, FocusScreen)
    await wait_for(pilot, lambda: any(m.kind == "summary" for m in app.orch.messages))
    assert any("итог личного обсуждения" in t for t in feed_texts(app))


async def test_alt_space_in_window_closes_it(ui):
    from capricorn.tui import FocusScreen
    app, pilot = ui
    await pilot.press("alt+space")
    assert isinstance(app.screen, FocusScreen)
    await pilot.press("alt+space")
    assert not isinstance(app.screen, FocusScreen)
    assert not any(m.kind == "handoff" for m in app.orch.messages)   # не говорили — итога нет


async def test_git_screen_lists_and_shows_commits(ui_git):
    from capricorn.tui import GitScreen
    app, pilot = ui_git
    await pilot.press("alt+1", "alt+space")
    assert isinstance(app.screen, GitScreen)
    await wait_for(pilot, lambda: app.screen.commits)
    await wait_for(pilot, lambda: "a.txt" in app.screen.shown)
    assert app.screen.branches == ["main"]
    await pilot.press("alt+g")
    assert not isinstance(app.screen, GitScreen)


async def test_alt_shortcuts_for_commands(ui):
    app, pilot = ui
    await pilot.press("alt+p")
    assert not app.orch.running.is_set()
    await pilot.press("alt+p")
    assert app.orch.running.is_set()
    await pilot.press("alt+h")
    assert any("alt+space" in t for t in feed_texts(app))


async def test_movement_with_coder3_visible(ui):
    app, pilot = ui
    await submit(pilot, "/complex on")
    await pilot.press("alt+5", "alt+right")
    assert app.selected == "coder3"
    await pilot.press("alt+down")
    assert app.selected == "feed"
    await submit(pilot, "/complex off")
    await pilot.press("alt+6")
    assert app.selected == "feed"                    # coder3 снова скрыт


async def test_focus_window_input_is_visible(ui):
    """Регрессия: .panel { height: 100% } из CSS приложения вытеснял строку ввода личного окна."""
    from capricorn.tui import FocusScreen
    app, pilot = ui
    await pilot.press("alt+space")
    await pilot.pause()
    assert isinstance(app.screen, FocusScreen)
    box = app.screen.query_one("#focus-input").region
    screen = app.screen.region
    assert box.height > 0 and box.bottom <= screen.bottom and box.y > 2


# ------------------------------------------------------------ стартовый экран

async def test_start_screen_picks_project_models_and_starts(tmp_path, fast_mock, monkeypatch):
    from capricorn import start
    from capricorn.tui import StartScreen
    monkeypatch.setattr(start, "cli_available", lambda: {"claude-code": True, "codex": True})
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "capricorn.yaml").write_text("budget: 9\n", encoding="utf-8")
    app = CapricornApp(load_config(), str(tmp_path / "work"), setup=True)
    async with app.run_test(size=(140, 50)) as pilot:
        assert isinstance(app.screen, StartScreen) and app.orch is None
        await pilot.press("alt+space")                          # команды ещё нет — клавиши не ломают
        assert app.orch is None
        app.screen.query_one("#project").value = str(proj)
        await pilot.pause()
        names = [str(b.label) for b in app.screen.query("#presets Button")]
        assert names == ["Claude", "Codex", "Смешанно", "Экономно", "Из конфига", "Заглушка"]
        await pilot.click("#preset-2")                          # «Смешанно»
        await pilot.pause()
        assert app.screen.chosen_models()["coder1"] == "codex/default"
        assert app.screen.chosen_models()["reviewer"] == "claude-code/sonnet"
        await pilot.click("#go")
        await pilot.pause()
        assert not isinstance(app.screen, StartScreen)
        assert app.orch is not None and app.repo == str(proj.resolve())
        assert app.cfg.budget == 9                               # конфиг из папки проекта
        assert app.cfg.agents["coder1"].model == "codex/default" and "codex" in app.orch.providers
        app.orch.stop_all()
    assert start.recent_projects()[0] == str(proj.resolve())
    assert start.load_project_settings(proj)["models"]["reviewer"] == "claude-code/sonnet"


async def test_start_screen_remembers_models_and_offers_resume(tmp_path, fast_mock, monkeypatch):
    from capricorn import start
    from capricorn.tui import StartScreen
    monkeypatch.setattr(start, "cli_available", lambda: {"claude-code": False, "codex": True})
    proj = tmp_path / "proj"
    proj.mkdir()
    start.save_project_settings(proj, {"models": {"coder2": "codex/default"}})
    first = CapricornApp(load_config(force_mock=True), str(proj))
    async with first.run_test(size=(140, 50)) as pilot:
        await submit(pilot, "прошлая задача")
        await wait_for(pilot, lambda: any("Готово" in t for t in feed_texts(first)))
        first.orch.stop_all()

    app = CapricornApp(load_config(), str(proj), setup=True)
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, StartScreen)
        assert scr.chosen_models()["coder2"] == "codex/default"   # запомнено для проекта
        assert scr.chosen_models()["reviewer"] == "mock/mock"
        resume = scr.query_one("#resume-session")
        assert not resume.disabled and "прошлая задача" in str(resume.label)
        resume.value = True
        await pilot.pause()
        await pilot.click("#go")
        await pilot.pause()
        assert any("прошлая задача" in t for t in feed_texts(app))
        app.orch.stop_all()


async def test_start_screen_rejects_missing_folder(tmp_path, fast_mock):
    from capricorn.tui import StartScreen
    app = CapricornApp(load_config(), str(tmp_path), setup=True)
    async with app.run_test(size=(140, 50)) as pilot:
        app.screen.query_one("#project").value = str(tmp_path / "nope")
        await pilot.pause()
        await pilot.click("#go")
        await pilot.pause()
        assert isinstance(app.screen, StartScreen) and app.orch is None
        assert "папк" in str(app.screen.query_one("#warn").content)


async def test_click_on_panel_selects_it_for_alt_space(ui):
    """Регрессия: в окне приложения alt+цифра может не дойти — личное окно открывалось только с менеджером.
    Клик по панели выбирает её."""
    from capricorn.tui import FocusScreen
    app, pilot = ui
    await pilot.click("#p-reviewer")
    assert app.selected == "reviewer" and selected(app) == ["reviewer"]
    assert "Ревьюер" in str(app.query_one("#status").content)
    await pilot.press("alt+space")
    assert isinstance(app.screen, FocusScreen) and app.screen.agent == "reviewer"


async def test_alt_digit_inside_focus_window_does_not_crash(ui):
    from capricorn.tui import FocusScreen
    app, pilot = ui
    await pilot.press("alt+space")
    assert isinstance(app.screen, FocusScreen)
    await pilot.press("alt+4")                       # панели — на главном экране, не в окне
    assert app.selected == "coder1" and isinstance(app.screen, FocusScreen)


async def test_click_then_alt_digit_leaves_single_highlight(ui):
    """Регрессия: клик ставил фокус на панель (жирная рамка :focus), alt+цифра переносила выбор,
    а фокус оставался — выделенными выглядели обе панели."""
    app, pilot = ui
    await pilot.click("#p-reviewer")
    assert app.focused is app.query_one("#p-reviewer")
    await pilot.press("alt+2")
    assert app.selected == "architect" and selected(app) == ["architect"]
    focused = app.focused
    assert focused is None or getattr(focused, "key", None) in (None, "architect")


# ------------------------------------------------------- lazygit в Git-окне

async def open_git(app, pilot):
    from capricorn.tui import GitScreen
    await pilot.press("alt+g")
    assert isinstance(app.screen, GitScreen)
    await wait_for(pilot, lambda: app.screen.commits)
    return app.screen


async def test_git_screen_stage_commit_branch(ui_git):
    from capricorn.tui import InputModal
    app, pilot = ui_git
    repo = app.orch.toolbox.root
    (repo / "a.txt").write_text("changed", encoding="utf-8")
    (repo / "new.txt").write_text("fresh", encoding="utf-8")
    scr = await open_git(app, pilot)
    await wait_for(pilot, lambda: len(scr.files) == 2)
    await wait_for(pilot, lambda: "+changed" in scr.shown or "+fresh" in scr.shown)   # diff файла справа

    await pilot.press("c")                                           # в индексе пусто — подсказка, без окна
    assert not isinstance(app.screen, InputModal) and "индексе пусто" in str(scr.query_one("#git-msg").content)

    await pilot.press("a")
    await wait_for(pilot, lambda: scr.files and all(c.staged for c in scr.files))
    await pilot.press("c")
    assert isinstance(app.screen, InputModal)
    app.screen.query_one("#modal-input").value = "коммит из Capricorn"
    await pilot.press("enter")
    await wait_for(pilot, lambda: scr.commits and scr.commits[0][3] == "коммит из Capricorn")
    assert scr.files == []

    await pilot.press("n")
    app.screen.query_one("#modal-input").value = "feature/ui"
    await pilot.press("enter")
    await wait_for(pilot, lambda: scr.current_branch == "feature/ui")


async def test_git_screen_toggle_and_discard(ui_git):
    from capricorn.tui import ConfirmModal
    app, pilot = ui_git
    repo = app.orch.toolbox.root
    (repo / "a.txt").write_text("oops", encoding="utf-8")
    scr = await open_git(app, pilot)
    await wait_for(pilot, lambda: len(scr.files) == 1)
    await pilot.press("space")
    await wait_for(pilot, lambda: scr.files and scr.files[0].staged)
    await pilot.press("space")
    await wait_for(pilot, lambda: scr.files and not scr.files[0].staged)
    await pilot.press("d")
    assert isinstance(app.screen, ConfirmModal)
    await pilot.press("n")                                           # отказ — файл не трогаем
    assert (repo / "a.txt").read_text(encoding="utf-8") == "oops"
    await pilot.press("d", "y")
    await wait_for(pilot, lambda: scr.files == [])
    assert (repo / "a.txt").read_text(encoding="utf-8") == "a"


def test_render_diff_colors():
    from capricorn.tui import GREEN, RED, render_diff
    t = render_diff("diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n ctx")
    styles = {span.style for span in t.spans}
    assert GREEN in styles and RED in styles
    assert t.plain.splitlines() == ["diff --git a/x b/x", "@@ -1 +1 @@", "-old", "+new", " ctx"]


def test_git_message_drops_warnings_and_hints():
    from capricorn.tui import git_message
    out = "warning: in the working copy of 'calc.py', LF will be replaced by CRLF\nhint: use --force\n"
    assert git_message(out, ok=True) == "готово"
    assert git_message(out + "error: failed to push some refs\n", ok=False) == "error: failed to push some refs"
    assert git_message("[main 1a2b3c] коммит\n 1 file changed", ok=True) == " 1 file changed"
    assert git_message("", ok=False) == "ошибка"


# ------------------------------------------------------- конфликты в Git-окне

@pytest.fixture
async def ui_conflict(tmp_path, fast_mock):
    from helpers import conflicted_repo
    repo = conflicted_repo(tmp_path / "conflict")
    app = CapricornApp(load_config(force_mock=True), str(repo))
    async with app.run_test(size=(180, 50)) as pilot:
        yield app, pilot, repo
        app.orch.stop_all()
        for t in app.orch._tasks:
            t.cancel()


async def open_conflict(app, pilot):
    from capricorn.tui import ConflictScreen
    scr = await open_git(app, pilot)
    await wait_for(pilot, lambda: scr.state == "merge" and len(scr.files) == 2)
    assert "MERGE" in str(scr.query_one("#git-state").content)
    await pilot.press("enter")                                       # calc.py — первый в списке
    await wait_for(pilot, lambda: isinstance(app.screen, ConflictScreen))
    return scr, app.screen


async def test_conflict_blocks_whole_file_and_continue(ui_conflict):
    app, pilot, repo = ui_conflict
    from helpers import git
    git_scr, cx_scr = await open_conflict(app, pilot)
    assert cx_scr.cf.unresolved == 1 and "b = 200" in str(cx_scr.query_one("#cx-ours-body").content)
    await pilot.press("s")                                           # не всё решено — не сохраняем
    assert app.screen is cx_scr and "не решено" in str(cx_scr.query_one("#cx-msg").content)
    await pilot.press("right")
    assert cx_scr.cf.unresolved == 0 and cx_scr.query_one("#cx-theirs").has_class("chosen")
    await pilot.press("s")
    await wait_for(pilot, lambda: app.screen is git_scr)
    assert (repo / "calc.py").read_text(encoding="utf-8") == "a = 1\nb = 20\nc = 3\n"
    await wait_for(pilot, lambda: [c.path for c in git_scr.files if c.conflict] == ["gone.txt"])

    git_scr.query_one("#files").highlighted = [c.path for c in git_scr.files].index("gone.txt")
    await pilot.press("o")                                           # у нас файл удалён — принимаем удаление
    await wait_for(pilot, lambda: not any(c.conflict for c in git_scr.files))
    assert "m — завершить" in str(git_scr.query_one("#git-state").content)
    await pilot.press("m")
    await wait_for(pilot, lambda: git_scr.state == "")
    assert not git_scr.query_one("#git-state").has_class("on")
    assert git(repo, "log", "-1", "--format=%p").count(" ") == 1


async def test_conflict_agent_proposal(ui_conflict):
    app, pilot, repo = ui_conflict
    asked = []

    async def fake_ask(agent, system, prompt):
        asked.append((agent, prompt))
        return "```\nb = 220\n```\nсложил оба"
    app.orch.ask_once = fake_ask
    _, cx_scr = await open_conflict(app, pilot)
    await pilot.press("enter")                                       # варианта ещё нет
    assert "alt+r" in str(cx_scr.query_one("#cx-msg").content) and cx_scr.cf.unresolved == 1
    await pilot.press("w")
    assert cx_scr.agent == "coder1"
    await pilot.press("alt+r")
    await wait_for(pilot, lambda: cx_scr.hunk.proposal is not None)
    assert asked[0][0] == "coder1" and "calc.py" in asked[0][1]
    assert "b = 220" in str(cx_scr.query_one("#cx-result-body").content)
    await pilot.press("enter", "s")
    await wait_for(pilot, lambda: (repo / "calc.py").read_text(encoding="utf-8") == "a = 1\nb = 220\nc = 3\n")


async def test_conflict_agent_error_keeps_window(ui_conflict):
    app, pilot, _ = ui_conflict

    async def broken(agent, system, prompt):
        raise RuntimeError("нет сети")
    app.orch.ask_once = broken
    _, cx_scr = await open_conflict(app, pilot)
    await pilot.press("alt+r")
    await wait_for(pilot, lambda: "нет сети" in str(cx_scr.query_one("#cx-msg").content))
    assert app.screen is cx_scr and not cx_scr.asking


async def test_conflict_escape_confirms_and_abort(ui_conflict):
    from capricorn.tui import ConfirmModal
    app, pilot, repo = ui_conflict
    git_scr, cx_scr = await open_conflict(app, pilot)
    await pilot.press("left", "escape")                              # выбор сделан — спрашиваем
    assert isinstance(app.screen, ConfirmModal)
    await pilot.press("y")
    await wait_for(pilot, lambda: app.screen is git_scr)
    assert "<<<<<<<" in (repo / "calc.py").read_text(encoding="utf-8"), "без сохранения файл не тронут"
    await pilot.press("A")
    assert isinstance(app.screen, ConfirmModal)
    await pilot.press("y")
    await wait_for(pilot, lambda: git_scr.state == "" and not git_scr.files)
    assert (repo / "calc.py").read_text(encoding="utf-8") == "a = 1\nb = 200\nc = 3\n"
