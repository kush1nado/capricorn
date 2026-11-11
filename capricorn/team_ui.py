"""Окно /team: состав команды, редактор роли (тип, название, описание, промпт, скиллы, инструменты)
и редактор скилла. Изменения копятся в окне и применяются по s: пишется .capricorn/team.yaml,
оркестратор перестраивает команду, интерфейс — панели. Выключение на задачу (o) действует сразу."""
from __future__ import annotations

import copy

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Select, SelectionList, Static, TextArea

from .prompts import ROLE_PROMPTS
from .team import (KIND_HELP, KINDS, RoleDef, TeamError, check_name, list_skills, read_skill, roles_of, save_team,
                   skill_path, title_of, write_skill)
from .tools import ROLE_TOOLS, TOOLS

RED, STEEL, BONE, GREEN = "#ff1a3c", "#6e7681", "#e9e4dc", "#39ff14"

BOX_CSS = """
{cls} {{ align: center middle; background: rgba(5, 5, 6, 0.85); }}
{cls} > Vertical {{ width: 96%; height: 94%; border: heavy #ff1a3c; background: #08080a; }}
{cls} .title {{ height: 1; background: #ff1a3c; color: #050506; text-style: bold; padding: 0 1; }}
{cls} .keys {{ height: auto; color: #6e7681; padding: 0 1; }}
{cls} .msg {{ height: auto; padding: 0 1; }}
{cls} Label {{ color: #ff1a3c; text-style: bold; margin-top: 1; }}
{cls} Input, {cls} Select, {cls} SelectionList, {cls} TextArea {{ border: tall #5c0a16; background: #08080a; }}
{cls} Button {{ background: #2a0309; color: #e9e4dc; border: tall #5c0a16; margin-right: 1; }}
{cls} Button:hover, {cls} Button:focus {{ background: #ff1a3c; color: #050506; }}
"""


def role_line(role: RoleDef, cfg, off: set[str]) -> Text:
    t = Text()
    t.append("▸ " if role.enabled and role.name not in off else "· ", style=RED if role.enabled else STEEL)
    t.append(f"{role.name:12}", style=f"bold {BONE}" if role.enabled else STEEL)
    t.append(f"{role.kind:10}", style=STEEL)
    title = role.title or (title_of(cfg, role.name) if role.name in cfg.agents else KINDS[role.kind])
    t.append(title, style=BONE if role.enabled else STEEL)
    state = "не в составе" if not role.enabled else ("выкл. на задаче" if role.name in off else "")
    if state:
        t.append(f"  [{state}]", style=STEEL)
    return t


def role_info(role: RoleDef) -> Text:
    t = Text()
    t.append(f"@{role.name}", style=f"bold {RED}")
    t.append(f" — {KINDS[role.kind]}: {KIND_HELP[role.kind]}\n\n", style=BONE)
    rows = [("Описание", role.description or "—"),
            ("Скиллы", ", ".join(role.skills) or "—"),
            ("Инструменты", ", ".join(role.tools) if role.tools is not None else "по типу роли"),
            ("Промпт", (role.prompt.strip().splitlines() or ["—"])[0][:200] if role.prompt else "стандартный для типа")]
    for k, v in rows:
        t.append(f"{k}: ", style=STEEL)
        t.append(v + "\n", style=BONE)
    return t


class TeamScreen(ModalScreen[None]):
    """Состав команды. Изменения — в копии; s применяет и сохраняет, esc закрывает (спросит, если не сохранено)."""
    BINDINGS = [
        Binding("escape", "close", "Закрыть", priority=True),
        Binding("a", "add", "Добавить"),
        Binding("e", "edit", "Изменить"),
        Binding("d", "delete", "Удалить"),
        Binding("space", "toggle", "В составе"),
        Binding("o", "off", "Выкл. на задаче"),
        Binding("s", "save", "Сохранить"),
    ]
    DEFAULT_CSS = BOX_CSS.format(cls="TeamScreen") + """
    TeamScreen #team-body { height: 1fr; }
    TeamScreen #team-list { width: 1fr; border: solid #5c0a16; background: #08080a; }
    TeamScreen #team-info { width: 1fr; border: solid #5c0a16; padding: 0 1; }
    TeamScreen ListItem.--highlight { background: #2a0309; }
    """

    def __init__(self):
        super().__init__()
        self.roles: list[RoleDef] = []
        self.dirty = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("▓ КОМАНДА // СОСТАВ ПРОЕКТА ┃ .capricorn/team.yaml", classes="title")
            with Horizontal(id="team-body"):
                yield ListView(id="team-list")
                yield Static("", id="team-info")
            yield Static("", id="team-msg", classes="msg")
            yield Static("a добавить · enter/e изменить · d удалить · space в составе · o выкл. на задаче (сразу) · "
                         "s сохранить и применить · esc закрыть", classes="keys")

    def on_mount(self):
        self.roles = copy.deepcopy(roles_of(self.app.cfg))
        self.refresh_list()
        self.query_one("#team-list").focus()

    # ------------------------------------------------------------ вид
    @property
    def current(self) -> RoleDef | None:
        i = self.query_one("#team-list", ListView).index
        return self.roles[i] if i is not None and 0 <= i < len(self.roles) else None

    def refresh_list(self, keep: str | None = None):
        lv = self.query_one("#team-list", ListView)
        old = keep or (self.current.name if self.current else None)
        lv.clear()
        off = self.app.orch.off
        for r in self.roles:
            lv.append(ListItem(Static(role_line(r, self.app.cfg, off))))
        names = [r.name for r in self.roles]
        lv.index = names.index(old) if old in names else (0 if names else None)
        self.show_info()

    def show_info(self):
        r = self.current
        self.query_one("#team-info", Static).update(role_info(r) if r else "")

    @on(ListView.Highlighted, "#team-list")
    def highlighted(self, _ev):
        self.show_info()

    @on(ListView.Selected, "#team-list")
    def selected(self, _ev):
        self.action_edit()

    def message(self, text: str, ok: bool = True):
        self.query_one("#team-msg", Static).update(Text(text, style=GREEN if ok else RED))

    def changed(self, keep: str | None = None):
        self.dirty = True
        self.refresh_list(keep)
        self.message("есть несохранённые изменения — s сохранит и применит")

    # ---------------------------------------------------------- действия
    def action_add(self):
        self.app.push_screen(RoleEditor(None, [r.name for r in self.roles]), self._added)

    def _added(self, role: RoleDef | None):
        if role is not None:
            self.roles.append(role)
            self.changed(role.name)

    def action_edit(self):
        r = self.current
        if r is not None:
            self.app.push_screen(RoleEditor(copy.deepcopy(r), [x.name for x in self.roles]), self._edited)

    def _edited(self, role: RoleDef | None):
        if role is not None:
            i = next(i for i, r in enumerate(self.roles) if r.name == role.name)
            self.roles[i] = role
            self.changed(role.name)

    def action_delete(self):
        r = self.current
        if r is None:
            return
        from .tui import ConfirmModal

        def done(yes: bool):
            if yes:
                self.roles.remove(r)
                self.changed()
        self.app.push_screen(ConfirmModal(f"Убрать роль {r.name} из команды? Её история в сессии останется."), done)

    def action_toggle(self):
        r = self.current
        if r is not None:
            r.enabled = not r.enabled
            self.changed(r.name)

    def action_off(self):
        """Выключить на текущую задачу — сразу, без сохранения: это не состав, а настройка задачи."""
        r = self.current
        if r is None:
            return
        o = self.app.orch
        try:
            o.set_off(r.name, r.name not in o.off)
        except ValueError as e:
            self.message(str(e), ok=False)
            return
        self.app.apply_layout()
        self.refresh_list(r.name)
        self.message(f"{r.name}: " + ("выключен на эту задачу" if r.name in o.off else "снова работает"))

    async def action_save(self):
        if not any(r.enabled for r in self.roles):
            self.message("в составе должна остаться хотя бы одна роль", ok=False)
            return
        app = self.app
        try:
            app.orch.set_team(copy.deepcopy(self.roles))
            path = save_team(app.repo, self.roles)
        except (ValueError, OSError) as e:
            self.message(str(e), ok=False)
            return
        await app.rebuild_panels()
        app.sync_meta()
        self.dirty = False
        self.refresh_list()
        self.message(f"сохранено: {path}")
        app.feed.system(f"► состав сохранён ({len(app.orch.roster)} в работе): {path}")

    def action_close(self):
        if not self.dirty:
            self.dismiss(None)
            return
        from .tui import ConfirmModal
        self.app.push_screen(ConfirmModal("Изменения состава не сохранены. Закрыть без сохранения?"),
                             lambda yes: self.dismiss(None) if yes else None)


class RoleEditor(ModalScreen[RoleDef | None]):
    """Роль: имя (только у новой), тип, название, описание, свой промпт, скиллы, инструменты."""
    BINDINGS = [Binding("escape", "cancel", "Отмена", priority=True),
                Binding("ctrl+s", "save", "Сохранить", priority=True)]
    DEFAULT_CSS = BOX_CSS.format(cls="RoleEditor") + """
    RoleEditor #role-scroll { padding: 0 1; }
    RoleEditor #role-prompt { height: 10; }
    RoleEditor #role-skills, RoleEditor #role-tools { height: auto; max-height: 10; }
    RoleEditor .row { height: 3; }
    """

    def __init__(self, role: RoleDef | None, taken: list[str]):
        super().__init__()
        self.new = role is None
        self.role = role or RoleDef(name="", kind="coder")
        self.taken = [n for n in taken if n != self.role.name]
        self.tools_kind = self.role.kind       # для какого типа сейчас отмечены инструменты «по умолчанию»

    def compose(self) -> ComposeResult:
        r = self.role
        with Vertical():
            yield Static(f"▓ РОЛЬ // {'НОВАЯ' if self.new else r.name.upper()} ┃ ctrl+s — сохранить · esc — отмена",
                         classes="title")
            with VerticalScroll(id="role-scroll"):
                yield Label("ИМЯ — как звать: @имя (латиница, цифры, _)")
                yield Input(r.name, id="role-name", disabled=not self.new, select_on_focus=False)
                yield Label("ТИП — определяет поведение")
                yield Select([(f"{k} — {KIND_HELP[k]}", k) for k in KINDS], id="role-kind",
                             allow_blank=False, value=r.kind)
                yield Label("НАЗВАНИЕ")
                yield Input(r.title, id="role-title", placeholder=KINDS[r.kind], select_on_focus=False)
                yield Label("ОПИСАНИЕ — видно всей команде в списке состава")
                yield Input(r.description, id="role-desc", placeholder="например: ищет дыры в безопасности",
                            select_on_focus=False)
                yield Label("ПРОМПТ — пусто: стандартный для типа")
                yield TextArea(r.prompt, id="role-prompt")
                with Horizontal(classes="row"):
                    yield Button("ВЗЯТЬ СТАНДАРТНЫЙ", id="role-std")
                yield Label("СКИЛЛЫ — .capricorn/skills/*.md, дописываются в промпт")
                yield SelectionList(*self.skill_options(r.skills), id="role-skills")
                with Horizontal(classes="row"):
                    yield Button("НОВЫЙ СКИЛЛ", id="skill-new")
                    yield Button("ИЗМЕНИТЬ ОТМЕЧЕННЫЙ", id="skill-edit")
                yield Label("ИНСТРУМЕНТЫ — для моделей по API; Codex и Claude Code работают своими")
                yield SelectionList(*self.tool_options(r.tools if r.tools is not None else ROLE_TOOLS[r.kind]),
                                    id="role-tools")
            yield Static("", id="role-msg", classes="msg")
            with Horizontal(classes="row"):
                yield Button("СОХРАНИТЬ", id="role-save")
                yield Button("ОТМЕНА", id="role-cancel")

    def on_mount(self):
        self.query_one("#role-name" if self.new else "#role-title").focus()

    def skill_options(self, chosen: list[str]) -> list[tuple[str, str, bool]]:
        names = list(dict.fromkeys([*list_skills(self.app.repo), *chosen]))
        return [(s + ("" if read_skill(self.app.repo, s) else "  (нет файла)"), s, s in chosen) for s in names]

    @staticmethod
    def tool_options(chosen: list[str]) -> list[tuple[str, str, bool]]:
        return [(f"{n} — {TOOLS[n].description.split('. ')[0].rstrip('.')[:70]}", n, n in chosen) for n in TOOLS]

    def message(self, text: str, ok: bool = False):
        self.query_one("#role-msg", Static).update(Text(text, style=GREEN if ok else RED))

    # ----------------------------------------------------------- события
    @on(Select.Changed, "#role-kind")
    def kind_changed(self, ev: Select.Changed):
        kind = str(ev.value)
        self.query_one("#role-title", Input).placeholder = KINDS[kind]
        tools = self.query_one("#role-tools", SelectionList)
        if set(tools.selected) == set(ROLE_TOOLS[self.tools_kind]):   # не трогали — набор по новому типу
            tools.deselect_all()
            for n in ROLE_TOOLS[kind]:
                tools.select(n)
        self.tools_kind = kind

    @on(Button.Pressed)
    def pressed(self, ev: Button.Pressed):
        bid = ev.button.id
        if bid == "role-std":
            self.use_standard_prompt()
        elif bid == "skill-new":
            from .tui import InputModal
            self.app.push_screen(InputModal("Имя нового скилла", "например: owasp"),
                                 lambda name: self.new_skill_named(name) if name else None)
        elif bid == "skill-edit":
            chosen = self.query_one("#role-skills", SelectionList).selected
            if chosen:
                self.edit_skill(chosen[-1])
            else:
                self.message("отметь скилл, который хочешь изменить")
        elif bid == "role-save":
            self.action_save()
        elif bid == "role-cancel":
            self.action_cancel()

    def use_standard_prompt(self):
        kind = str(self.query_one("#role-kind", Select).value)
        self.query_one("#role-prompt", TextArea).text = ROLE_PROMPTS[kind]

    def new_skill_named(self, name: str):
        name = name.strip()
        try:
            skill_path(self.app.repo, name)              # проверка имени до открытия редактора
        except TeamError as e:
            self.message(str(e))
            return
        self.edit_skill(name)

    def edit_skill(self, name: str):
        def done(saved: bool):
            if not saved:
                return
            sl = self.query_one("#role-skills", SelectionList)
            chosen = [*sl.selected, name] if name not in sl.selected else list(sl.selected)
            sl.clear_options()
            sl.add_options(self.skill_options(chosen))
        self.app.push_screen(SkillEditor(name), done)

    # ---------------------------------------------------------- итог
    def action_save(self):
        name = self.query_one("#role-name", Input).value.strip()
        try:
            if self.new:
                check_name(name, self.taken)
        except TeamError as e:
            self.message(str(e))
            return
        kind = str(self.query_one("#role-kind", Select).value)
        tools = list(self.query_one("#role-tools", SelectionList).selected)
        self.dismiss(RoleDef(
            name=name, kind=kind,
            title=self.query_one("#role-title", Input).value.strip(),
            description=self.query_one("#role-desc", Input).value.strip(),
            prompt=self.query_one("#role-prompt", TextArea).text.strip(),
            skills=list(self.query_one("#role-skills", SelectionList).selected),
            tools=None if set(tools) == set(ROLE_TOOLS[kind]) else [n for n in TOOLS if n in tools],
            enabled=self.role.enabled,
        ))

    def action_cancel(self):
        self.dismiss(None)


class SkillEditor(ModalScreen[bool]):
    """Текст скилла: .capricorn/skills/<имя>.md. Общий для всех ролей, которые его отметили."""
    BINDINGS = [Binding("escape", "cancel", "Отмена", priority=True),
                Binding("ctrl+s", "save", "Сохранить", priority=True)]
    DEFAULT_CSS = BOX_CSS.format(cls="SkillEditor") + """
    SkillEditor #skill-text { height: 1fr; }
    """

    def __init__(self, name: str):
        super().__init__()
        self.skill = name

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"▓ СКИЛЛ // {self.skill} ┃ .capricorn/skills/{self.skill}.md ┃ ctrl+s — сохранить · "
                         "esc — отмена", classes="title")
            yield TextArea(read_skill(self.app.repo, self.skill), id="skill-text")
            yield Static("Markdown: правила, сценарии, ловушки. Попадает в промпт всех ролей с этим скиллом.",
                         classes="keys")

    def on_mount(self):
        self.query_one("#skill-text").focus()

    def action_save(self):
        try:
            write_skill(self.app.repo, self.skill, self.query_one("#skill-text", TextArea).text)
        except (TeamError, OSError) as e:
            self.query_one(".keys", Static).update(Text(str(e), style=RED))
            return
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)
