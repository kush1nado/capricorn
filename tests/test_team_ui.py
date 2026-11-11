"""Окно /team: состав команды, редактор роли и скиллов (Textual Pilot, headless)."""
import pytest

from capricorn.config import load_config
from capricorn.team import load_team, read_skill, write_skill
from capricorn.tui import CapricornApp
from test_tui import drain, feed_texts, submit, wait_for

TEAM = """\
roles:
  architect: {}
  manager:   {}
  coder1:    {}
  reviewer:  {}
  tester:    {enabled: false}
"""


@pytest.fixture
async def ui(tmp_path, fast_mock):
    (tmp_path / ".capricorn").mkdir()
    (tmp_path / ".capricorn" / "team.yaml").write_text(TEAM, encoding="utf-8")
    app = CapricornApp(load_config(force_mock=True), str(tmp_path))
    async with app.run_test(size=(180, 60)) as pilot:
        yield app, pilot, tmp_path
        await drain(app)
        app.orch.stop_all()
        for t in app.orch._tasks:
            t.cancel()


async def open_team(pilot):
    from capricorn.team_ui import TeamScreen
    await submit(pilot, "/team")
    await wait_for(pilot, lambda: isinstance(pilot.app.screen, TeamScreen))
    return pilot.app.screen


async def highlight(pilot, scr, name):
    scr.query_one("#team-list").index = [r.name for r in scr.roles].index(name)
    await pilot.pause()


async def test_window_lists_roles_and_alt_t_opens_it(ui):
    app, pilot, _ = ui
    from capricorn.team_ui import TeamScreen
    await pilot.press("alt+t")
    await wait_for(pilot, lambda: isinstance(app.screen, TeamScreen))
    scr = app.screen
    assert [r.name for r in scr.roles] == ["architect", "manager", "coder1", "reviewer", "tester"]
    await highlight(pilot, scr, "reviewer")
    assert "APPROVED" in str(scr.query_one("#team-info").content), "описание типа видно справа"
    await pilot.press("escape")
    await wait_for(pilot, lambda: not isinstance(app.screen, TeamScreen))


async def test_toggle_membership_and_save(ui):
    app, pilot, repo = ui
    scr = await open_team(pilot)
    await highlight(pilot, scr, "tester")
    await pilot.press("space")
    assert scr.roles[-1].enabled and scr.dirty
    await pilot.press("s")
    await wait_for(pilot, lambda: not scr.dirty)
    assert "tester" in app.orch.roster and app.panels["tester"].display
    assert load_team(repo)[-1].enabled is True


async def test_off_for_task_from_window(ui):
    app, pilot, _ = ui
    scr = await open_team(pilot)
    await highlight(pilot, scr, "reviewer")
    await pilot.press("o")
    assert "reviewer" in app.orch.off and app.panels["reviewer"].has_class("off")
    assert "выкл. на задаче" in str(scr.query_one("#team-list").highlighted_child.query_one("Static").content)
    await pilot.press("o")
    assert "reviewer" not in app.orch.off


async def test_add_role_with_editor_and_skill(ui):
    app, pilot, repo = ui
    from capricorn.team_ui import RoleEditor, SkillEditor
    write_skill(repo, "sql", "Индексы — по запросам.")
    scr = await open_team(pilot)
    await pilot.press("a")
    await wait_for(pilot, lambda: isinstance(app.screen, RoleEditor))
    ed = app.screen
    ed.query_one("#role-name").value = "dba"
    ed.query_one("#role-kind").value = "expert"
    ed.query_one("#role-title").value = "Эксперт по БД"
    ed.query_one("#role-desc").value = "схемы и индексы"
    ed.query_one("#role-skills").select("sql")
    # новый скилл прямо из редактора роли
    ed.new_skill_named("owasp")
    await wait_for(pilot, lambda: isinstance(app.screen, SkillEditor) and app.screen.query("#skill-text"))
    app.screen.query_one("#skill-text").text = "Проверяй инъекции."
    await pilot.press("ctrl+s")
    await wait_for(pilot, lambda: app.screen is ed)
    assert read_skill(repo, "owasp") == "Проверяй инъекции."
    assert "owasp" in ed.query_one("#role-skills").selected, "новый скилл сразу отмечен"
    await pilot.press("ctrl+s")
    await wait_for(pilot, lambda: app.screen is scr)
    assert scr.roles[-1].name == "dba" and sorted(scr.roles[-1].skills) == ["owasp", "sql"]
    await pilot.press("s")
    await wait_for(pilot, lambda: "dba" in app.panels)
    assert app.orch.cfg.agents["dba"].kind == "expert"
    assert [r.name for r in load_team(repo)][-1] == "dba"
    assert any("состав сохранён" in t for t in feed_texts(app))


async def test_editor_validates_name_and_tools_default_by_kind(ui):
    app, pilot, _ = ui
    from capricorn.team_ui import RoleEditor
    scr = await open_team(pilot)
    await pilot.press("a")
    await wait_for(pilot, lambda: isinstance(app.screen, RoleEditor))
    ed = app.screen
    ed.query_one("#role-name").value = "coder1"
    await pilot.press("ctrl+s")
    assert app.screen is ed and "уже есть" in str(ed.query_one("#role-msg").content)
    ed.query_one("#role-name").value = "Плохо"
    await pilot.press("ctrl+s")
    assert "латиница" in str(ed.query_one("#role-msg").content)
    ed.query_one("#role-name").value = "dev"
    ed.query_one("#role-kind").value = "coder"
    await pilot.pause()
    assert "write_file" in ed.query_one("#role-tools").selected, "инструменты по типу"
    await pilot.press("ctrl+s")
    await wait_for(pilot, lambda: app.screen is scr)
    assert scr.roles[-1].tools is None, "набор по типу не записываем в файл"


async def test_edit_existing_role_prompt_and_standard_text(ui):
    app, pilot, _ = ui
    from capricorn.team_ui import RoleEditor
    scr = await open_team(pilot)
    await highlight(pilot, scr, "coder1")
    await pilot.press("enter")
    await wait_for(pilot, lambda: isinstance(app.screen, RoleEditor))
    ed = app.screen
    assert ed.query_one("#role-name").disabled, "имя существующей роли не меняем"
    ed.use_standard_prompt()
    assert "разработчик" in ed.query_one("#role-prompt").text
    ed.query_one("#role-prompt").text = "Пиши на Go."
    await pilot.press("ctrl+s")
    await wait_for(pilot, lambda: app.screen is scr)
    assert scr.roles[2].prompt == "Пиши на Go." and scr.dirty


async def test_delete_role_and_close_without_saving(ui):
    app, pilot, repo = ui
    from capricorn.team_ui import TeamScreen
    from capricorn.tui import ConfirmModal
    scr = await open_team(pilot)
    await highlight(pilot, scr, "reviewer")
    await pilot.press("d")
    await wait_for(pilot, lambda: isinstance(app.screen, ConfirmModal))
    await pilot.press("y")
    await wait_for(pilot, lambda: app.screen is scr)
    assert "reviewer" not in [r.name for r in scr.roles]
    await pilot.press("escape")
    await wait_for(pilot, lambda: isinstance(app.screen, ConfirmModal))
    await pilot.press("y")
    await wait_for(pilot, lambda: not isinstance(app.screen, (TeamScreen, ConfirmModal)))
    assert "reviewer" in app.orch.agents and "reviewer" in [r.name for r in load_team(repo)], "не сохраняли"


async def test_save_refused_while_team_works(ui):
    app, pilot, _ = ui
    scr = await open_team(pilot)
    await highlight(pilot, scr, "tester")
    await pilot.press("space")
    import asyncio
    app.orch.agents["coder1"].gen_task = asyncio.get_running_loop().create_future()     # будто идёт ход
    await pilot.press("s")
    assert scr.dirty and "работают: coder1" in str(scr.query_one("#team-msg").content)
    app.orch.agents["coder1"].gen_task = None


async def test_cannot_leave_team_empty(ui):
    app, pilot, _ = ui
    scr = await open_team(pilot)
    for r in scr.roles:
        r.enabled = False
    scr.dirty = True
    await pilot.press("s")
    assert scr.dirty and "хотя бы одна" in str(scr.query_one("#team-msg").content)


async def test_team_list_subcommand_still_prints(ui):
    app, pilot, _ = ui
    await submit(pilot, "/team list")
    assert any("team.yaml" in t for t in feed_texts(app))


async def test_typing_in_editor_does_not_wipe_field(ui):
    """Живая проверка: фокус выделял название целиком, и первая же клавиша стирала его."""
    app, pilot, _ = ui
    from capricorn.team_ui import RoleEditor
    scr = await open_team(pilot)
    await highlight(pilot, scr, "reviewer")
    await pilot.press("enter")
    await wait_for(pilot, lambda: isinstance(app.screen, RoleEditor) and app.screen.query("#role-title"))
    app.screen.query_one("#role-title").value = "Ревьюер"
    app.screen.query_one("#role-desc").focus()
    app.screen.query_one("#role-title").focus()
    await pilot.pause()
    await pilot.press("x")
    assert app.screen.query_one("#role-title").value == "Ревьюерx"
