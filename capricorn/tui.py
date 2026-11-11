"""Терминальный интерфейс на Textual: сетка панелей агентов + git + общая лента + ввод."""
from __future__ import annotations

import copy
import shlex
import time
from pathlib import Path

from rich.console import Group
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, DirectoryTree, Input, Label, OptionList, RadioButton, RadioSet, Select, Static

from . import conflicts as cx
from . import start
from .config import Config, load_config
from .gitinfo import GitSnapshot
from .orchestrator import USER, Message, Orchestrator
from .sessions import SessionStore, to_markdown
from .terminal import install_key_fixes, install_web_input_fix
from .team import TeamError, apply_team, load_team, title_of
from .usage import k, usage_report

install_key_fixes()       # alt+цифры и alt+space иначе не доходят (см. terminal.py)
install_web_input_fix()   # Windows: иначе Esc в окне/браузере срабатывает только со следующей клавишей

# Палитра: корпоративный киберпанк — чёрный, кроваво-красный, сталь, неоновые акценты
RED = "#ff1a3c"        # основной
RED_DIM = "#5c0a16"    # рамки
RED_DEEP = "#2a0309"   # подложки
STEEL = "#6e7681"      # служебный текст
BONE = "#e9e4dc"       # основной текст
COLORS = {
    "architect": RED, "manager": BONE, "coder1": "#00e5ff", "coder2": "#ff2e88",
    "coder3": "#b26bff", "reviewer": "#ff7a00", "tester": "#f5d300", USER: f"bold {BONE}",
    "system": STEEL,
}
DEFAULT_COLOR_NAMES = set(COLORS)
EXTRA_COLORS = ["#39ff14", "#40e0d0", "#ff69b4", "#c8a96e", "#87cefa", "#dda0dd", "#ffa07a", "#9acd32"]
CODE_THEME = "fruity"
SPIN = "░▒▓█▓▒"
PANEL_ORDER = ["git", "architect", "manager", "coder1", "coder2", "coder3", "reviewer", "tester", "feed"]
MAX_ITEMS = 150
PANEL_NAMES = {"git": "Git", "feed": "Лента"}


def color_for(name: str) -> str:
    """Цвет агента: у стандартных — свой, новым ролям — по очереди из запасной палитры."""
    if name not in COLORS:
        used = len([n for n in COLORS if n not in DEFAULT_COLOR_NAMES])
        COLORS[name] = EXTRA_COLORS[used % len(EXTRA_COLORS)]
    return COLORS[name].replace("bold ", "")
ACTIVE = ("thinking", "typing", "working", "testing")  # working — инструмент; testing — тесты для ревью

HELP = """\
[b]Ввод[/b]: текст → менеджеру; [#00e5ff]@architect @reviewer текст[/] → адресно; @coders, @all.
[b]Команды[/b]:
  /pause /resume /stop          — пауза, продолжить, прервать всех
  /continue [N]                 — добавить N ходов (по умолч. 15)
  /complex on|off               — третий кодер
  /team  (alt+t)                — окно состава: добавить, убрать, изменить роль, скиллы;  /team list — текстом
  /off <агент>  /on <агент>     — выключить роль на текущую задачу (до /new) и вернуть
  /model <агент> <пров/модель>  — сменить модель на лету;  /models — список
  /retry <агент>  /reset [агент] — повтор после ошибки, очистка памяти
  /attach <путь>                — приложить файл к следующему сообщению
  /branch <имя>                 — git switch (или ctrl+b)
  /zoom <агент|git|feed>        — развернуть панель (esc — назад)
  /tools                        — инструменты агентов (чтение/запись файлов, diff, тесты)
  /worktrees \\[prune]           — ветки кодеров; prune — убрать уже влитые
  /merge <агент>                — влить ветку агента в текущую (то же, что merge_work у менеджера)
  /undo-merge <агент>           — откатить последнее слияние ветки (revert)
  /diff <агент>                 — что агент наменял в своей ветке, до слияния (или alt+d на его панели)
  /usage                        — ходы, токены и стоимость по агентам за сессию
  /sessions  /session <id|last>  /new  /export [файл] — сессии проекта
  /budget N  /help  /quit
[b]Клавиши[/b] (все через alt):
  alt+1..9, alt+стрелки — выбрать панель     alt+space (или alt+o) — открыть её окно
  в окне агента — личный разговор; esc закрывает окно, итог уходит команде
  alt+g — полноэкранный Git (ветки, коммиты, конфликты)   alt+d — diff ветки агента   alt+z — развернуть панель, esc — свернуть
  alt+t команда · alt+p пауза · alt+s стоп · alt+b ветки · alt+w worktree · alt+n новая сессия · alt+e экспорт · alt+h справка
  (F5/F8/ctrl+b тоже работают; если терминал перехватывает alt+space — жми alt+o)"""


def ago(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


# ------------------------------------------------------------------ widgets

def select_panel(panel):
    """Клик (фокус) по панели главного экрана делает её выбранной — alt+space откроет её окно.
    Мышь работает везде, а alt+цифра в окне приложения может не дойти."""
    app = panel.app
    if getattr(app, "orch", None) is not None and panel.id == f"p-{panel.key}":
        app.select(panel.key)


class Item(Static):
    """Одно сообщение в панели."""


class LogPanel(VerticalScroll):
    can_focus = True
    ALLOW_MAXIMIZE = True

    def __init__(self, key: str, title: str, index: int, wid: str | None = None):
        super().__init__(id=wid or f"p-{key}", classes="panel")
        self.key = key
        self.index = index
        self.border_title = f"▓ {index:02d} // {title.upper()}"

    def _at_bottom(self) -> bool:
        return self.scroll_y >= self.max_scroll_y - 3

    def add(self, renderable, classes: str = "") -> Item:
        follow = self._at_bottom()
        item = Item(renderable, classes=classes)
        self.mount(item)
        items = self.query(Item)
        if len(items) > MAX_ITEMS:
            items.first().remove()
        if follow:
            self.call_after_refresh(self.scroll_end, animate=False)
        return item

    def refresh_follow(self, item: Item, renderable):
        follow = self._at_bottom()
        item.update(renderable)
        if follow:
            self.call_after_refresh(self.scroll_end, animate=False)

    def clear(self):
        for item in list(self.query(Item)):
            item.remove()

    def on_focus(self):
        select_panel(self)


class AgentPanel(LogPanel):
    def __init__(self, key: str, index: int, wid: str | None = None, title: str | None = None):
        super().__init__(key, title or title_of(None, key), index, wid)
        self.styles.border_title_color = color_for(key)
        self.streams: dict[int, tuple[Item, str]] = {}
        self.dirty: set[int] = set()
        self.status = "idle"
        self.model = ""
        self.tok = (0, 0, 0)            # новые ↑, ответ ↓, кэш
        self._spin = 0

    def on_mount(self):
        self.set_interval(0.1, self._tick)

    def set_meta(self, model: str | None = None, status: str | None = None, tok=None):
        if model is not None:
            self.model = model
        if status is not None:
            self.status = status
        if tok is not None:
            self.tok = tok
        self._render_subtitle()

    def _render_subtitle(self):
        icon = {"idle": "▫", "error": "×", "stopped": "■", "waiting budget": "‖", "queued": "…"}.get(
            self.status, SPIN[self._spin % len(SPIN)])
        self.set_class(self.status in ACTIVE, "active")
        self.set_class(self.status == "error", "failed")
        self.border_subtitle = (f"{icon} {self.status.upper()} ┃ {self.model} ┃ "
                                f"{k(self.tok[0])}↑ {k(self.tok[1])}↓" + (f" кэш {k(self.tok[2])}" if self.tok[2] else ""))

    def _tick(self):
        if self.status in ACTIVE:
            self._spin += 1
            self._render_subtitle()
        for mid in list(self.dirty):
            item, text = self.streams[mid]
            self.refresh_follow(item, self._body(text, streaming=True))
        self.dirty.clear()

    def _body(self, text: str, streaming=False, to: list[str] | None = None):
        parts = [Markdown(text + (" █" if streaming else ""), code_theme=CODE_THEME)]
        if to:
            t = Text("▸▸ ", style=RED_DIM)
            for i, n in enumerate(to):
                t.append(("" if i == 0 else ", ") + n, style=COLORS.get(n, "white"))
            parts.append(t)
        return Group(*parts)

    def incoming(self, msg: Message):
        first = msg.content.strip().splitlines()[0] if msg.content.strip() else ""
        t = Text("◂◂ ", style=RED_DIM)
        t.append(msg.sender, style=COLORS.get(msg.sender, "white"))
        t.append(f": {first[:160]}", style=f"italic {STEEL}")
        self.add(t, classes="incoming")

    def user_line(self, text: str):
        """Реплика пользователя целиком — в личном окне агента."""
        t = Text("▸ ты: ", style=f"bold {RED}")
        t.append(text, style=f"bold {BONE}")
        self.add(t, classes="incoming")

    def own(self, text: str, to: list[str]):
        """Готовое сообщение агента (при восстановлении сессии — без стриминга)."""
        self.add(self._body(text, to=to), classes="own")

    def clear(self):
        super().clear()
        self.streams.clear()
        self.dirty.clear()

    def stream_start(self, mid: int):
        self.streams[mid] = (self.add(Text("▒▒▒", style=RED_DIM), classes="own"), "")

    def stream_chunk(self, mid: int, chunk: str):
        item, text = self.streams[mid]
        self.streams[mid] = (item, text + chunk)
        self.dirty.add(mid)

    def stream_end(self, mid: int, text: str, to: list[str]):
        item, _ = self.streams.pop(mid)
        self.dirty.discard(mid)
        if not text:
            item.remove()
            return
        self.refresh_follow(item, self._body(text, to=to))


class FeedPanel(LogPanel):
    def message(self, msg: Message):
        if msg.kind == "handoff":           # служебная просьба подвести итог — в ленте не нужна
            return
        t = Text(f"{ago(msg.ts)} ", style="#4a4f57")
        t.append(msg.sender, style=COLORS.get(msg.sender, "white"))
        t.append(" ▸ " if msg.to else " · ", style=STEEL)
        if msg.kind == "summary":
            t.append("все", style=BONE)
            t.append("  ◂ итог личного обсуждения\n", style=RED)
            t.append(msg.content.strip(), style=BONE)
            self.add(t, classes="hl")
            return
        for i, n in enumerate(msg.to):
            t.append(("" if i == 0 else ", ") + n, style=COLORS.get(n, "white"))
        body = msg.content.strip()
        if msg.kind == "private":           # сам разговор — в окне агента, здесь только след
            first = body.splitlines()[0] if body else ""
            t.append(f"  · лично · {first[:100]}", style="#4a4f57")
            self.add(t)
            return
        if msg.sender == USER or USER in msg.to:
            t.append("\n" + body, style=f"bold {BONE}" if USER in msg.to else BONE)
        else:
            first = body.splitlines()[0] if body else ""
            t.append(f"  {first[:120]}", style="#a8a29a")
        self.add(t, classes="hl" if USER in msg.to else "")

    def system(self, text: str, level: str = "info", markup: bool = False):
        style = {"error": f"bold {RED}", "warn": "#f5d300"}.get(level, STEEL)
        self.add(Text.from_markup(text, style=style) if markup else Text(text, style=style))


class GitPanel(Static, can_focus=True):
    ALLOW_MAXIMIZE = True

    def on_focus(self):
        select_panel(self)

    def __init__(self, index: int):
        super().__init__(id="p-git", classes="panel")
        self.key = "git"
        self.border_title = f"▓ {index:02d} // GIT"

    @staticmethod
    def _section(t: Text, title: str):
        t.append(f"\n▒ {title} ", style=f"bold {RED}")
        t.append("─" * 24 + "\n", style=RED_DIM)

    def show(self, s: GitSnapshot):
        if not s.ok:
            self.update(Text(f"× {s.error}", style=RED))
            return
        self.border_title = f"▓ 01 // GIT ┃ {s.branch}"
        t = Text()
        t.append(f"» {s.branch}", style=f"bold {BONE} on {RED_DEEP}")
        if s.upstream:
            t.append(f"  → {s.upstream}", style=STEEL)
            t.append(f"  ↑{s.ahead} ↓{s.behind}",
                     style=COLORS["tester"] if s.ahead or s.behind else STEEL)
        t.append("\n")
        t.append(f"●{s.staged} индекс  ", style=GREEN)
        t.append(f"±{s.modified} изм.  ", style=COLORS["reviewer"])
        t.append(f"+{s.untracked} новых", style=COLORS["coder2"])
        self._section(t, "КОММИТЫ")
        for h, when, author, subj in s.commits[:10]:
            t.append(h, style=RED)
            t.append(f" {subj[:60]}", style=BONE)
            t.append(f"  {when}, {author}\n", style=STEEL)
        self._section(t, f"MR/PR{(' · ' + s.mr_source) if s.mr_source else ''}")
        if s.mrs:
            for m in s.mrs:
                t.append(m[:90] + "\n", style=COLORS["coder3"])
        else:
            t.append("нет открытых / gh|glab не настроен\n", style=STEEL)
        self._section(t, "ВЕТКИ · CTRL+B")
        for b in s.branches[:12]:
            t.append(("► " + b) if b == s.branch else b,
                     style=f"bold {RED}" if b == s.branch else COLORS["coder1"])
            t.append("   ")
        self.update(t)


class BranchPicker(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Отмена")]
    DEFAULT_CSS = """
    BranchPicker { align: center middle; }
    BranchPicker > Vertical { width: 60; height: 24; border: heavy $primary; background: $surface; }
    BranchPicker Label { background: $primary; color: $background; width: 100%; text-style: bold; }
    BranchPicker OptionList { border: none; background: $surface; }
    """

    def __init__(self, branches: list[str], current: str):
        super().__init__()
        self.branches = branches
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(" ▓ ПЕРЕКЛЮЧИТЬ ВЕТКУ ┃ enter — выбрать · esc — отмена")
            yield OptionList(*[("● " if b == self.current else "  ") + b for b in self.branches])

    @on(OptionList.OptionSelected)
    def picked(self, ev: OptionList.OptionSelected):
        self.dismiss(self.branches[ev.option_index])


class DirsOnly(DirectoryTree):
    def filter_paths(self, paths):
        return [p for p in paths if p.is_dir() and not p.name.startswith(".")]


class StartScreen(ModalScreen[dict]):
    """Стартовый экран: проект, модели агентов, новая или прошлая сессия."""
    BINDINGS = [Binding("escape", "app.quit", "Выход")]
    DEFAULT_CSS = """
    StartScreen { align: center middle; background: #050506; }
    StartScreen #start-box { width: 110; max-width: 98%; height: 96%; border: heavy #ff1a3c; background: #08080a; }
    StartScreen #start-title { height: 1; background: #ff1a3c; color: #050506; text-style: bold; padding: 0 1; }
    StartScreen .caption { color: #ff1a3c; text-style: bold; margin-top: 1; }
    StartScreen #start-scroll { padding: 0 2; }
    StartScreen #project-row { height: 3; }
    StartScreen #project { width: 1fr; }
    StartScreen #tree { height: 12; border: solid #3d0710; display: none; }
    StartScreen #tree.open { display: block; }
    StartScreen #recent { height: auto; max-height: 6; border: solid #3d0710; }
    StartScreen #presets { height: 3; }
    StartScreen #presets Button { min-width: 12; margin-right: 1; }
    StartScreen #roles { layout: grid; grid-size: 2; grid-columns: 18 1fr; grid-rows: 3; height: auto; }
    StartScreen #roles Label { padding: 1 0 0 0; }
    StartScreen #warn { color: #f5d300; height: auto; }
    StartScreen #actions { height: 3; margin-top: 1; }
    StartScreen #go { min-width: 20; }
    StartScreen Button { background: #2a0309; color: #e9e4dc; border: tall #5c0a16; }
    StartScreen Button:hover, StartScreen Button:focus { background: #ff1a3c; color: #050506; }
    """

    def __init__(self, cfg: Config, default_dir: str, config_path: str | None = None):
        super().__init__()
        self.cfg = cfg
        self.config_path = config_path
        self.default_dir = default_dir
        self.available = start.available_models(cfg)
        self.preset_map: dict[str, dict[str, str]] = {}
        self.latest_session: str | None = None
        self.role_names: list[str] = []
        self.kinds: dict[str, str] = {}
        self.ready = False              # Input.Changed приходит ещё при монтировании — до готовности не реагируем

    def compose(self) -> ComposeResult:
        with Vertical(id="start-box"):
            yield Static("▓ CAPRICORN // НОВЫЙ ЗАПУСК ┃ enter — запуск · esc — выход", id="start-title")
            with VerticalScroll(id="start-scroll"):
                yield Label("ПРОЕКТ — папка, с которой работает команда", classes="caption")
                with Horizontal(id="project-row"):
                    yield Input(self.default_dir, id="project", placeholder="путь к папке проекта")
                    yield Button("ОБЗОР", id="browse")
                yield DirsOnly(start.default_project_dir(), id="tree")
                recent = start.recent_projects()
                if recent:
                    yield Label("НЕДАВНИЕ", classes="caption")
                    yield OptionList(*recent, id="recent")
                yield Label("МОДЕЛИ АГЕНТОВ", classes="caption")
                yield Horizontal(id="presets")
                yield Container(id="roles")
                yield Label("СЕССИЯ", classes="caption")
                with RadioSet(id="session"):
                    yield RadioButton("новая", value=True, id="new-session")
                    yield RadioButton("продолжить последнюю", id="resume-session", disabled=True)
                yield Static("", id="warn")
                with Horizontal(id="actions"):
                    yield Button("► ЗАПУСК", id="go")
                    yield Button("ВЫХОД", id="quit")

    async def on_mount(self):
        await self.refresh_project()
        self.ready = True
        self.query_one("#go").focus()

    # --------------------------------------------------------- проект
    def project_path(self) -> Path:
        return Path(self.query_one("#project", Input).value.strip().strip('"')).expanduser()

    async def refresh_project(self):
        path = self.project_path()
        warn = self.query_one("#warn", Static)
        if not path.is_dir():
            warn.update("▲ такой папки нет")
            return
        warn.update("")
        cfg = copy.deepcopy(self.cfg) if self.config_path else load_config(base=path)
        try:
            apply_team(cfg, load_team(path))
        except TeamError as e:
            warn.update(f"▲ {e}")
        self.available = start.available_models(cfg)
        self.preset_map = start.presets(cfg, self.available)
        bar = self.query_one("#presets", Horizontal)
        await bar.remove_children()             # асинхронно: иначе новые кнопки столкнутся id со старыми
        await bar.mount_all([Button(name, id=f"preset-{i}") for i, name in enumerate(self.preset_map)])
        saved = start.load_project_settings(path).get("models") or {}
        options = [(label, model) for label, model in self.available]
        have = {m for _, m in options}
        roles = self.query_one("#roles", Container)
        await roles.remove_children()
        self.role_names = list(cfg.agents)
        self.kinds = {n: a.kind for n, a in cfg.agents.items()}
        widgets = []
        for role, agent in cfg.agents.items():
            want = saved.get(role) or agent.model
            widgets += [Label(title_of(cfg, role) + ("" if agent.enabled else " (не в составе)")),
                        Select(options, id=f"model-{role}", allow_blank=False,
                               value=want if want in have else "mock/mock")]
        await roles.mount_all(widgets)
        self.latest_session = SessionStore(path).latest()
        resume = self.query_one("#resume-session", RadioButton)
        if self.latest_session:
            info = next(s for s in SessionStore(path).list() if s.id == self.latest_session)
            when = time.strftime("%d.%m %H:%M", time.localtime(info.updated))
            resume.label = f"продолжить последнюю — {when}, {info.messages} сообщ. «{info.title[:40]}»"
            resume.disabled = False
        else:
            resume.label = "продолжить последнюю (в этом проекте сессий нет)"
            resume.value = False
            resume.disabled = True
            self.query_one("#new-session", RadioButton).value = True

    @on(Input.Changed, "#project")
    async def project_changed(self, _ev):
        if self.ready:
            await self.refresh_project()

    @on(Input.Submitted, "#project")
    def project_submitted(self, _ev):
        self.go()

    @on(DirectoryTree.DirectorySelected, "#tree")
    def picked_dir(self, ev: DirectoryTree.DirectorySelected):
        self.query_one("#project", Input).value = str(ev.path)

    @on(OptionList.OptionSelected, "#recent")
    def picked_recent(self, ev: OptionList.OptionSelected):
        self.query_one("#project", Input).value = str(ev.option.prompt)

    # --------------------------------------------------------- кнопки
    @on(Button.Pressed)
    def pressed(self, ev: Button.Pressed):
        bid = ev.button.id or ""
        if bid == "browse":
            self.query_one("#tree").toggle_class("open")
        elif bid == "quit":
            self.app.exit()
        elif bid == "go":
            self.go()
        elif bid.startswith("preset-"):
            models = list(self.preset_map.values())[int(bid.split("-", 1)[1])]
            have = {m for _, m in self.available}
            for role in self.role_names:
                model = models.get(role) or models.get(start.preset_key(role, self.kinds.get(role)))
                if model in have:
                    self.query_one(f"#model-{role}", Select).value = model

    def chosen_models(self) -> dict[str, str]:
        return {role: self.query_one(f"#model-{role}", Select).value for role in self.role_names}

    def go(self):
        path = self.project_path()
        if not path.is_dir():
            self.query_one("#warn", Static).update("▲ выбери существующую папку проекта")
            return
        resume = bool(self.latest_session) and self.query_one("#resume-session", RadioButton).value
        self.dismiss({"repo": str(path.resolve()), "models": self.chosen_models(), "resume": resume})


class FocusScreen(ModalScreen[None]):
    """Личное окно агента: разговор только с ним. Закрыли — агент подводит итог для команды."""
    BINDINGS = [
        Binding("escape", "close", "Закрыть"),
        Binding("alt+space", "close", show=False, priority=True),
        Binding("alt+o", "close", show=False, priority=True),
    ]
    DEFAULT_CSS = """
    FocusScreen { align: center middle; background: rgba(5, 5, 6, 0.85); }
    FocusScreen > Vertical { width: 96%; height: 94%; border: heavy #ff1a3c; background: #08080a; }
    FocusScreen #focus-title { height: 1; background: #ff1a3c; color: #050506; text-style: bold; padding: 0 1; }
    FocusScreen .panel { border: none; height: 1fr; }
    FocusScreen #focus-input { border: heavy #ff1a3c; background: #08080a; color: #e9e4dc; }
    """

    def __init__(self, agent: str):
        super().__init__()
        self.agent = agent
        self.panel = AgentPanel(agent, 0, wid=f"f-{agent}")

    def compose(self) -> ComposeResult:
        name = title_of(self.app.cfg, self.agent)
        self.panel.border_title = f"▓ {self.app.panel_order.index(self.agent) + 1:02d} // {name.upper()}"
        title = name.upper()
        with Vertical():
            yield Static(f"▓ {title} // ЛИЧНЫЙ КАНАЛ ┃ видишь только ты ┃ esc — закрыть, итог уйдёт команде",
                         id="focus-title")
            yield self.panel
            yield PromptInput(id="focus-input", placeholder=f"▸ лично для: {name}…")

    def on_mount(self):
        a = self.app.orch.agents[self.agent]
        self.panel.set_meta(model=self.app.cfg.agents[self.agent].model, status=a.status,
                            tok=(a.tokens_in, a.tokens_out, a.tokens_cached))
        for msg in self.app.orch.messages:          # прошлые личные разговоры с этим агентом
            if msg.kind != "private":
                continue
            if msg.sender == USER and msg.to == [self.agent]:
                self.panel.user_line(msg.content)
            elif msg.sender == self.agent:
                self.panel.own(msg.content, [])
        self.query_one("#focus-input").focus()

    @on(Input.Submitted, "#focus-input")
    def said(self, ev: Input.Submitted):
        text = ev.value.strip()
        ev.input.value = ""
        if text:
            ev.input.remember(text)
            self.app.orch.private_says(self.agent, text)

    def action_close(self):
        self.dismiss(None)


GREEN = "#39ff14"      # неоновый зелёный: добавленное, индекс
CYAN = "#00e5ff"


def render_diff(text: str) -> Text:
    """Diff в палитре Capricorn: + зелёный, - красный, @@ голубой, служебное — сталь."""
    out = Text()
    for line in text.splitlines():
        if line.startswith(("+++", "---", "diff --git", "index ", "new file", "deleted file", "similarity", "rename ")):
            style = f"bold {BONE}"
        elif line.startswith("+"):
            style = GREEN
        elif line.startswith("-"):
            style = RED
        elif line.startswith("@@"):
            style = CYAN
        elif line.startswith(("commit ", "Author", "AuthorDate", "Commit", "CommitDate", "Merge:")):
            style = STEEL
        else:
            style = "#a8a29a"
        out.append(line + "\n", style=style)
    return out


def git_message(out: str, ok: bool) -> str:
    """Итог git-операции для строки сообщений: без служебных warning:/hint: (например, про CRLF)."""
    lines = [line for line in out.splitlines() if line.strip() and not line.startswith(("warning:", "hint:"))]
    return lines[-1] if lines else ("готово" if ok else "ошибка")


def change_label(c) -> Text:
    """Строка списка файлов: индекс зелёным, изменения красным, новое голубым, конфликт жирным красным."""
    t = Text()
    if c.conflict:
        t.append("× ", style=f"bold {RED}")
    else:
        t.append("●" if c.staged else " ", style=GREEN)
        t.append("±" if c.unstaged else ("+" if c.untracked else " "), style=CYAN if c.untracked else RED)
        t.append(" ")
    style = GREEN if c.staged and not c.unstaged else (CYAN if c.untracked else BONE)
    t.append(c.path, style=style)
    return t


class InputModal(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Отмена")]
    DEFAULT_CSS = """
    InputModal { align: center middle; background: rgba(5, 5, 6, 0.7); }
    InputModal > Vertical { width: 80; height: auto; border: heavy #ff1a3c; background: #08080a; padding: 0 1; }
    InputModal Label { color: #ff1a3c; text-style: bold; margin: 1 0 0 0; }
    InputModal Input { border: tall #5c0a16; background: #08080a; }
    InputModal .hint { color: #6e7681; margin-bottom: 1; }
    """

    def __init__(self, title: str, placeholder: str = ""):
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text)
            yield Input(placeholder=self.placeholder, id="modal-input")
            yield Static("enter — ок · esc — отмена", classes="hint")

    def on_mount(self):
        self.query_one("#modal-input").focus()

    @on(Input.Submitted, "#modal-input")
    def done(self, ev: Input.Submitted):
        self.dismiss(ev.value.strip() or None)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [Binding("y", "dismiss(True)", "Да"), Binding("n", "dismiss(False)", "Нет"),
                Binding("escape", "dismiss(False)", "Нет")]
    DEFAULT_CSS = """
    ConfirmModal { align: center middle; background: rgba(5, 5, 6, 0.7); }
    ConfirmModal > Vertical { width: 70; height: auto; border: heavy #ff1a3c; background: #08080a; padding: 1 2; }
    ConfirmModal .hint { color: #6e7681; margin-top: 1; }
    """

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(self.text, style=f"bold {BONE}"))
            yield Static("y — да · n / esc — нет", classes="hint")


def lines_text(lines: list[str], style: str) -> Text:
    return Text("".join(lines).replace("\r\n", "\n"), style=style) if lines else Text("(пусто)", style=STEEL)


class ConflictScreen(ModalScreen[bool]):
    """Конфликты одного файла по блокам: наше, их, оба или вариант агента. Возвращает True, если файл решён."""
    BINDINGS = [
        Binding("escape", "close", "Закрыть"),
        Binding("alt+space", "close", show=False, priority=True),
        Binding("alt+g", "close", show=False, priority=True),
        Binding("alt+o", "close", show=False, priority=True),
        Binding("left,h", f"pick('{cx.OURS}')", show=False),
        Binding("right,l", f"pick('{cx.THEIRS}')", show=False),
        Binding("b", f"pick('{cx.BOTH}')", show=False),
        Binding("enter", f"pick('{cx.AGENT}')", show=False),
        Binding("u", "pick('')", show=False),
        Binding("up,k", "go(-1)", show=False),
        Binding("down,j", "go(1)", show=False),
        Binding("alt+r", "ask", show=False, priority=True),
        Binding("R", "ask_all", show=False),
        Binding("w", "next_agent", show=False),
        Binding("o", "whole('ours')", show=False),
        Binding("t", "whole('theirs')", show=False),
        Binding("s", "save", show=False),
    ]
    DEFAULT_CSS = """
    ConflictScreen { align: center middle; background: rgba(5, 5, 6, 0.85); }
    ConflictScreen > Vertical { width: 96%; height: 94%; border: heavy #ff1a3c; background: #08080a; }
    ConflictScreen #cx-title { height: 1; background: #ff1a3c; color: #050506; text-style: bold; padding: 0 1; }
    ConflictScreen #cx-keys { height: auto; background: #140205; color: #a8a29a; padding: 0 1; }
    ConflictScreen .ctx { height: auto; color: #6e7681; padding: 0 1; }
    ConflictScreen .side { width: 1fr; border: solid #5c0a16; padding: 0 1;
                           border-title-color: #ff1a3c; border-title-style: bold; }
    ConflictScreen .side.chosen { border: heavy #39ff14; border-title-color: #39ff14; }
    ConflictScreen #cx-msg { height: 1; padding: 0 1; }
    """
    KIND_ORDER = ("architect", "coder", "reviewer", "manager", "tester", "expert")   # кого спрашивать первым

    def agents(self) -> list[str]:
        o = self.app.orch
        return sorted(o.roster, key=lambda n: self.KIND_ORDER.index(o.cfg.kind_of(n)))

    def __init__(self, path: str):
        super().__init__()
        self.path = path
        self.cf: cx.ConflictFile | None = None
        self.index = 0
        self.asking: set[int] = set()           # блоки, по которым ждём ответ агента
        self.agent = ""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("▓ КОНФЛИКТ", id="cx-title")
            yield Static("", id="cx-before", classes="ctx")
            with Horizontal():
                for key in ("ours", "theirs", "result"):
                    pane = VerticalScroll(Static("", id=f"cx-{key}-body"), id=f"cx-{key}", classes="side")
                    pane.can_focus = False      # стрелки — навигация по блокам, а не прокрутка
                    yield pane
            yield Static("", id="cx-after", classes="ctx")
            yield Static("", id="cx-msg")
            yield Static("← наше · → их · b оба · alt+r спросить агента · R агента по всем · enter принять вариант "
                         "агента · w другой агент · u сбросить · ↑↓ блоки · o/t файл целиком · s сохранить · esc",
                         id="cx-keys")

    def on_mount(self):
        names = self.agents() or list(self.app.orch.agents)
        self.agent = names[0] if names else ""
        try:
            self.cf = cx.parse(self.git.read_text(self.path))
        except (OSError, UnicodeDecodeError, cx.ConflictParseError) as e:
            self.cf = None
            self.message(f"по блокам не разобрать ({e.__class__.__name__}) — o/t: взять файл целиком", ok=False)
        if self.cf is not None and not self.cf.hunks:
            self.cf = None
            self.message("маркеров конфликта нет — o/t: взять файл целиком", ok=False)
        self.render_hunk()

    @property
    def git(self):
        return self.app.orch.git

    @property
    def hunk(self) -> cx.Hunk | None:
        return self.cf.hunks[self.index] if self.cf and self.cf.hunks else None

    def message(self, text: str, ok: bool = True):
        self.query_one("#cx-msg", Static).update(Text(text, style=GREEN if ok else f"bold {RED}"))

    def render_hunk(self):
        h = self.hunk
        total = len(self.cf.hunks) if self.cf else 0
        left = self.cf.unresolved if self.cf else 0
        self.query_one("#cx-title", Static).update(
            f"▓ КОНФЛИКТ ┃ {self.path} ┃ блок {self.index + 1 if h else 0}/{total} ┃ осталось {left} ┃ агент: {self.agent}")
        if h is None:
            for key in ("ours", "theirs", "result"):
                self.query_one(f"#cx-{key}-body", Static).update("")
            return
        before, after = self.cf.context(h)
        self.query_one("#cx-before", Static).update(lines_text(before, STEEL) if before else "")
        self.query_one("#cx-after", Static).update(lines_text(after, STEEL) if after else "")
        self.query_one("#cx-ours").border_title = f"НАШЕ · {h.ours_label or 'ours'}"
        self.query_one("#cx-theirs").border_title = f"ИХ · {h.theirs_label or 'theirs'}"
        self.query_one("#cx-ours-body", Static).update(lines_text(h.ours, GREEN))
        self.query_one("#cx-theirs-body", Static).update(lines_text(h.theirs, CYAN))
        result = self.query_one("#cx-result")
        if h.resolved:
            result.border_title = {cx.OURS: "ИТОГ · наше", cx.THEIRS: "ИТОГ · их", cx.BOTH: "ИТОГ · оба",
                                   cx.AGENT: "ИТОГ · вариант агента"}[h.choice]
            body = lines_text(h.result(), BONE)
        elif h.proposal is not None:
            result.border_title = "ВАРИАНТ АГЕНТА · enter — принять"
            body = lines_text(h.proposal, CYAN)
        elif self.index in self.asking:
            result.border_title = "ВАРИАНТ АГЕНТА"
            body = Text(f"{self.agent} думает…", style=STEEL)
        else:
            result.border_title = "ИТОГ"
            body = Text("не выбрано: ← наше · → их · b оба · alt+r агент", style=STEEL)
        self.query_one("#cx-result-body", Static).update(body)
        for key, sides in (("ours", (cx.OURS, cx.BOTH)), ("theirs", (cx.THEIRS, cx.BOTH)), ("result", (cx.AGENT,))):
            self.query_one(f"#cx-{key}").set_class(h.choice in sides, "chosen")

    # ------------------------------------------------------------ действия
    def action_go(self, step: int):
        if self.cf and self.cf.hunks:
            self.index = (self.index + step) % len(self.cf.hunks)
            self.render_hunk()

    def action_pick(self, side: str):
        h = self.hunk
        if h is None:
            return
        if side == cx.AGENT and h.proposal is None:
            self.message("варианта агента ещё нет — alt+r", ok=False)
            return
        h.choice = side or None
        if h.choice and self.cf.unresolved:
            hunks = self.cf.hunks
            self.index = next(i % len(hunks) for i in range(self.index + 1, self.index + 1 + len(hunks))
                              if not hunks[i % len(hunks)].resolved)
        elif h.choice:
            self.message("все блоки решены — s сохранить")
        self.render_hunk()

    def action_next_agent(self):
        names = self.agents()
        if names:
            self.agent = names[(names.index(self.agent) + 1) % len(names)] if self.agent in names else names[0]
            self.render_hunk()

    def action_ask(self):
        if self.hunk is not None and self.agent:
            self.ask(self.index)

    def action_ask_all(self):
        if self.cf and self.agent:
            for i, h in enumerate(self.cf.hunks):
                if not h.resolved and h.proposal is None:
                    self.ask(i)

    @work(group="cx-agent")
    async def ask(self, index: int):
        if index in self.asking:
            return
        h, agent = self.cf.hunks[index], self.agent
        self.asking.add(index)
        self.render_hunk()
        try:
            answer = await self.app.orch.ask_once(agent, cx.RESOLVER_SYSTEM, cx.agent_prompt(self.path, self.cf, h))
        except Exception as e:                       # noqa: BLE001 — ошибка провайдера показывается, окно живёт
            self.message(f"{agent}: {e}", ok=False)
            return
        finally:
            self.asking.discard(index)
        h.proposal = cx.proposal_lines(answer, h.ours + h.theirs) if answer.strip() else None
        if h.proposal is None:
            self.message(f"{agent} ничего не предложил", ok=False)
        elif index == self.index:
            self.message(f"{agent} предложил вариант — enter принять")
        self.render_hunk()

    def action_save(self):
        if self.cf is None:
            self.message("по блокам не разобрать — o/t: взять файл целиком", ok=False)
        elif self.cf.unresolved:
            self.message(f"не решено блоков: {self.cf.unresolved}", ok=False)
        else:
            self.finish(self.git.save_resolved, self.path, self.cf.render())

    def action_whole(self, side: str):
        self.finish(self.git.take_side, self.path, side)

    @work(group="cx-op")
    async def finish(self, op, *args):
        ok, out = await op(*args)
        if ok:
            self.dismiss(True)
        else:
            self.message(git_message(out, ok), ok=False)

    def action_close(self):
        if self.cf and any(h.choice for h in self.cf.hunks):
            self.app.push_screen(ConfirmModal("выйти без сохранения?\nвыбранное в этом файле потеряется"),
                                 lambda yes: yes and self.dismiss(False))
        else:
            self.dismiss(False)


STATUS_STYLE = {"A": ("+", GREEN), "?": ("+", CYAN), "D": ("-", RED), "M": ("±", BONE)}


class AgentDiffScreen(ModalScreen[None]):
    """Что агент наменял в своей ветке до слияния: файлы слева, diff справа, m — влить."""
    BINDINGS = [
        Binding("escape", "close", "Закрыть"),
        Binding("alt+space", "close", show=False, priority=True),
        Binding("alt+g", "close", show=False, priority=True),
        Binding("alt+o", "close", show=False, priority=True),
        Binding("alt+d", "close", show=False, priority=True),
        Binding("m", "merge", show=False),
        Binding("r", "refresh", show=False),
    ]
    DEFAULT_CSS = """
    AgentDiffScreen { align: center middle; background: rgba(5, 5, 6, 0.85); }
    AgentDiffScreen > Vertical { width: 96%; height: 94%; border: heavy #ff1a3c; background: #08080a; }
    AgentDiffScreen #ad-title { height: 1; background: #ff1a3c; color: #050506; text-style: bold; padding: 0 1; }
    AgentDiffScreen #ad-keys { height: 1; background: #140205; color: #a8a29a; padding: 0 1; }
    AgentDiffScreen #ad-files { width: 36%; height: 1fr; border: heavy #39ff14; background: #08080a; }
    AgentDiffScreen #ad-view { border: solid #3d0710; padding: 0 1; }
    AgentDiffScreen #ad-msg { height: 1; padding: 0 1; }
    """

    def __init__(self, agent: str):
        super().__init__()
        self.agent = agent
        self.files: list[tuple[str, str]] = []
        self.shown = ""
        self.loaded = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"▓ DIFF ┃ {self.agent}", id="ad-title")
            with Horizontal():
                yield OptionList(id="ad-files")
                with VerticalScroll(id="ad-view"):
                    yield Static("", id="ad-diff")
            yield Static("", id="ad-msg")
            yield Static("стрелки — файлы · m — влить в текущую ветку · r — обновить · esc", id="ad-keys")

    def on_mount(self):
        self.query_one("#ad-files").focus()
        self.reload()

    @property
    def wts(self):
        return self.app.orch.toolbox.worktrees

    def message(self, text: str, ok: bool = True):
        if not self.is_attached:
            return
        self.query_one("#ad-msg", Static).update(Text(text, style=GREEN if ok else f"bold {RED}"))

    @work(exclusive=True, group="agent-diff")
    async def reload(self):
        try:
            files = await self.wts.changes(self.agent)
            base = (await self.wts.base(self.agent))[:7] if files else ""
        except Exception as e:                      # noqa: BLE001 — например, detached HEAD в основном репозитории
            self.message(str(e), ok=False)
            files, base = [], ""
        self.files = files
        self.loaded = True
        if not self.is_attached:            # окно закрыли, пока git отвечал
            return
        since = f" от {base}" if base else ""
        self.query_one("#ad-title", Static).update(
            f"▓ DIFF ┃ {self.agent} ┃ {self.wts.branch(self.agent)}{since} ┃ файлов: {len(files)}")
        ol = self.query_one("#ad-files", OptionList)
        ol.clear_options()
        if not files:
            ol.add_option(Text(f"{self.agent} ничего не менял", style=STEEL))
            self.query_one("#ad-diff", Static).update("")
            return
        for status, path in files:
            mark, style = STATUS_STYLE.get(status, ("±", BONE))
            ol.add_option(Text.assemble((f"{mark} ", style), (path, style)))
        ol.highlighted = 0
        self.preview(0)

    @on(OptionList.OptionHighlighted, "#ad-files")
    def on_pick(self, ev: OptionList.OptionHighlighted):
        if 0 <= ev.option_index < len(self.files):
            self.preview(ev.option_index)

    @work(exclusive=True, group="agent-diff-file")
    async def preview(self, index: int):
        status, path = self.files[index]
        self.shown = await self.wts.file_diff(self.agent, status, path)
        if not self.is_attached:
            return
        self.query_one("#ad-diff", Static).update(render_diff(self.shown))

    @work(group="agent-diff-op")
    async def action_merge(self):
        if not self.files:
            self.message("сливать нечего", ok=False)
            return
        try:
            res = await self.wts.merge(self.agent)
        except Exception as e:                      # noqa: BLE001
            self.message(str(e), ok=False)
            return
        self.message(res.text, res.ok)
        self.app.refresh_git()
        if res.ok:
            self.reload()

    def action_refresh(self):
        self.reload()

    def action_close(self):
        self.dismiss(None)


class GitScreen(ModalScreen[None]):
    """Полноэкранный Git в духе lazygit: файлы (индекс, откат, коммит), ветки, коммиты, push/pull."""
    BINDINGS = [
        Binding("escape", "close", "Закрыть"),
        Binding("alt+space", "close", show=False, priority=True),
        Binding("alt+g", "close", show=False, priority=True),
        Binding("alt+o", "close", show=False, priority=True),
        Binding("space", "toggle_stage", show=False),
        Binding("a", "stage_all", show=False),
        Binding("d", "discard", show=False),
        Binding("c", "commit", show=False),
        Binding("n", "new_branch", show=False),
        Binding("p", "remote('pull')", show=False),
        Binding("P", "remote('push')", show=False),
        Binding("f", "remote('fetch')", show=False),
        Binding("r", "refresh", show=False),
        Binding("o", "take_side('ours')", show=False),
        Binding("t", "take_side('theirs')", show=False),
        Binding("m", "continue_op", show=False),
        Binding("A", "abort_op", show=False),
    ]
    DEFAULT_CSS = """
    GitScreen { align: center middle; background: rgba(5, 5, 6, 0.85); }
    GitScreen > Vertical { width: 96%; height: 94%; border: heavy #ff1a3c; background: #08080a; }
    GitScreen #git-title { height: 1; background: #ff1a3c; color: #050506; text-style: bold; padding: 0 1; }
    GitScreen #git-keys { height: 1; background: #140205; color: #a8a29a; padding: 0 1; }
    GitScreen #git-left { width: 42%; }
    GitScreen .caption { color: #ff1a3c; text-style: bold; padding: 0 1; }
    GitScreen OptionList { border: solid #3d0710; background: #08080a; }
    GitScreen OptionList:focus { border: heavy #39ff14; }
    GitScreen #files { height: 40%; }
    GitScreen #branches { height: 20%; }
    GitScreen #commits { height: 1fr; }
    GitScreen #git-view { border: solid #3d0710; padding: 0 1; }
    GitScreen #git-msg { height: 1; padding: 0 1; }
    GitScreen #git-state { height: auto; display: none; background: #2a0510; color: #ff1a3c; text-style: bold; padding: 0 1; }
    GitScreen #git-state.on { display: block; }
    """

    def __init__(self):
        super().__init__()
        self.files: list = []
        self.branches: list[str] = []
        self.commits: list[tuple[str, str, str, str]] = []
        self.shown = ""                 # текст справа (diff файла или коммита)
        self.current_branch = ""
        self.state = ""                 # merge / rebase / cherry-pick / revert, прерванные конфликтом

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("▓ GIT", id="git-title")
            yield Static("", id="git-state")
            with Horizontal():
                with Vertical(id="git-left"):
                    yield Label("ФАЙЛЫ", classes="caption")
                    yield OptionList(id="files")
                    yield Label("ВЕТКИ", classes="caption")
                    yield OptionList(id="branches")
                    yield Label("КОММИТЫ", classes="caption")
                    yield OptionList(id="commits")
                with VerticalScroll(id="git-view"):
                    yield Static("", id="commit-view")
            yield Static("", id="git-msg")
            yield Static("пробел индекс · a всё · d откатить · c коммит · n ветка · p pull · P push · f fetch · "
                         "r обновить · enter конфликт · m/A merge · tab — списки · esc", id="git-keys")

    def on_mount(self):
        self.reload()
        self.query_one("#files").focus()

    @property
    def git(self):
        return self.app.orch.git

    def message(self, text: str, ok: bool = True):
        if not self.is_attached:
            return
        self.query_one("#git-msg", Static).update(Text(text, style=GREEN if ok else f"bold {RED}"))

    def show(self, text: str):
        self.shown = text
        if not self.is_attached:            # git ответил, когда окно уже закрыли
            return
        self.query_one("#commit-view", Static).update(render_diff(text) if text else Text("нет изменений", style=STEEL))

    @work(exclusive=True, group="git-screen")
    async def reload(self):
        snap = await self.git.snapshot()
        if not self.is_attached:
            return
        self.current_branch = snap.branch
        track = f" → {snap.upstream} ↑{snap.ahead} ↓{snap.behind}" if snap.upstream else ""
        self.query_one("#git-title", Static).update(f"▓ GIT ┃ {snap.branch}{track}")
        files = await self.git.changes()
        self.state = await self.git.merge_state()
        self.files = files                  # вместе с баннером: между ними нет await
        self.show_state()
        self.branches = snap.branches
        self.commits = await self.git.commits()
        if not self.is_attached:
            return
        fl = self.query_one("#files", OptionList)
        keep = fl.highlighted
        fl.clear_options()
        fl.add_options([change_label(c) for c in self.files] or [Text("чисто — нечего коммитить", style=STEEL)])
        bl = self.query_one("#branches", OptionList)
        bl.clear_options()
        bl.add_options([Text(("► " if b == snap.branch else "  ") + b, style=f"bold {GREEN}" if b == snap.branch else BONE)
                        for b in self.branches])
        cl = self.query_one("#commits", OptionList)
        cl.clear_options()
        cl.add_options([Text.assemble((h[:7], RED), "  ", (subj[:70], BONE), (f"  · {when}", STEEL))
                        for h, when, _, subj in self.commits])
        if self.files:
            fl.highlighted = min(keep or 0, len(self.files) - 1)
            self.preview_file(self.files[fl.highlighted])
        elif self.commits:
            self.show_commit(self.commits[0][0])

    def show_state(self):
        if not self.is_attached:
            return
        banner = self.query_one("#git-state", Static)
        banner.set_class(bool(self.state), "on")
        if not self.state:
            return
        n = sum(c.conflict for c in self.files)
        left = (f"в конфликте: {n} ┃ enter — разрешить по блокам · o/t — файл целиком наш/их · "
                if n else "конфликты решены ┃ m — завершить · ")
        swap = " (при rebase «наше» — ветка, на которую переносишь)" if self.state == "rebase" else ""
        banner.update(f"▲ {self.state.upper()} ┃ {left}A — прервать{swap}")

    @work(exclusive=True, group="git-show")
    async def show_commit(self, sha: str):
        self.show(await self.git.show(sha))

    @work(exclusive=True, group="git-show")
    async def preview_file(self, change):
        self.show(await self.git.file_diff(change))

    @on(OptionList.OptionHighlighted, "#commits")
    def preview_commit(self, ev: OptionList.OptionHighlighted):
        if 0 <= ev.option_index < len(self.commits):
            self.show_commit(self.commits[ev.option_index][0])

    @on(OptionList.OptionHighlighted, "#files")
    def preview_change(self, ev: OptionList.OptionHighlighted):
        if 0 <= ev.option_index < len(self.files):
            self.preview_file(self.files[ev.option_index])

    @on(OptionList.OptionSelected, "#files")
    def resolve(self, ev: OptionList.OptionSelected):
        if 0 <= ev.option_index < len(self.files) and self.files[ev.option_index].conflict:
            self.app.push_screen(ConflictScreen(self.files[ev.option_index].path), self.resolved)

    def resolved(self, done: bool | None):
        if done:
            self.message("файл решён и добавлен в индекс")
        self.reload()
        self.app.refresh_git()

    @on(OptionList.OptionSelected, "#branches")
    def switch(self, ev: OptionList.OptionSelected):
        self.run_git(self.git.switch, self.branches[ev.option_index])

    # ------------------------------------------------------------ действия
    def selected_change(self):
        fl = self.query_one("#files", OptionList)
        if self.focused is fl and fl.highlighted is not None and fl.highlighted < len(self.files):
            return self.files[fl.highlighted]
        return None

    @work(group="git-op")
    async def run_git(self, op, *args):
        ok, out = await op(*args)
        self.message(git_message(out, ok), ok)
        if ok and op in (self.git.continue_op, self.git.abort_op):
            self.app.merge_done(op == self.git.continue_op)
        self.reload()
        self.app.refresh_git()

    def action_toggle_stage(self):
        c = self.selected_change()
        if c is not None:
            self.run_git(self.git.unstage if c.staged and not c.unstaged else self.git.stage, c.path)

    def action_stage_all(self):
        if self.files:
            all_staged = all(c.staged and not c.unstaged for c in self.files)
            self.run_git(self.git.unstage_all if all_staged else self.git.stage_all)

    def action_discard(self):
        c = self.selected_change()
        if c is None:
            return
        what = "удалить новый файл" if c.untracked or c.x == "A" else "откатить изменения"
        self.app.push_screen(ConfirmModal(f"{what}: {c.path}?\nЭто нельзя отменить."),
                             lambda yes: yes and self.run_git(self.git.discard, c))

    def action_commit(self):
        if not any(c.staged for c in self.files):
            self.message("в индексе пусто — сначала пробел (файл) или a (всё)", ok=False)
            return
        self.app.push_screen(InputModal("СООБЩЕНИЕ КОММИТА", "что сделано"),
                             lambda msg: msg and self.run_git(self.git.commit, msg))

    def action_new_branch(self):
        self.app.push_screen(InputModal(f"НОВАЯ ВЕТКА от {self.current_branch}", "feature/имя"),
                             lambda name: name and self.run_git(self.git.create_branch, name))

    def action_remote(self, op: str):
        self.message(f"{op}…")
        self.run_git(getattr(self.git, op))

    def action_refresh(self):
        self.reload()

    def action_take_side(self, side: str):
        c = self.selected_change()
        if c is None or not c.conflict:
            self.message("o/t — для файла в конфликте", ok=False)
            return
        self.run_git(self.git.take_side, c.path, side)

    def action_continue_op(self):
        if not self.state:
            self.message("нет прерванного merge/rebase", ok=False)
            return
        self.message(f"{self.state} --continue…")
        self.run_git(self.git.continue_op)

    def action_abort_op(self):
        if not self.state:
            self.message("нет прерванного merge/rebase", ok=False)
            return
        self.app.push_screen(ConfirmModal(f"прервать {self.state}?\nрешённые конфликты пропадут, "
                                          "ветка вернётся как была"),
                             lambda yes: yes and self.run_git(self.git.abort_op))

    def action_close(self):
        self.dismiss(None)


# окна поверх команды: alt+space в них закрывает окно, а не открывает новое
OVERLAYS = (FocusScreen, GitScreen, ConflictScreen, AgentDiffScreen)


class PromptInput(Input):
    BINDINGS = [Binding("up", "hist(-1)", show=False), Binding("down", "hist(1)", show=False)]

    def __init__(self, **kw):
        super().__init__(**kw)
        self.history: list[str] = []
        self.pos = 0

    def remember(self, text: str):
        self.history.append(text)
        self.pos = len(self.history)

    def action_hist(self, d: int):
        if not self.history:
            return
        self.pos = max(0, min(len(self.history), self.pos + d))
        self.value = self.history[self.pos] if self.pos < len(self.history) else ""
        self.cursor_position = len(self.value)


# ---------------------------------------------------------------------- app

BANNER = "▓▒░ CAPRICORN ┃ MULTI-AGENT DEVELOPMENT ENVIRONMENT ┃ ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒"

CAPRICORN_THEME = Theme(
    name="capricorn",
    primary=RED, secondary="#b3122e", accent=RED, warning="#f5d300", error=RED,
    success="#00e5ff", foreground=BONE, background="#050506", surface="#0d0d10",
    panel="#140205", dark=True,
)


class CapricornApp(App):
    TITLE = "Capricorn"
    CSS = """
    Screen { layout: vertical; background: #050506; }
    #banner { height: 1; background: $primary; color: #050506; text-style: bold; padding: 0 1; }
    #grid { layout: grid; grid-size: 3 3; grid-gutter: 0; height: 1fr; }
    .panel {
        border: solid #3d0710; border-title-color: #ff1a3c; border-title-style: bold;
        border-subtitle-color: #6e7681; padding: 0 1; height: 100%; background: #08080a;
        scrollbar-size-vertical: 1; scrollbar-color: #5c0a16; scrollbar-background: #08080a;
        scrollbar-color-hover: #ff1a3c; scrollbar-color-active: #ff1a3c;
    }
    .panel.active { border: solid #ff1a3c; border-subtitle-color: #ff1a3c; }
    .panel.failed { border: double #ff1a3c; }
    .panel.off { border: dashed #3d0710; opacity: 50%; }
    .panel:focus, .panel:focus-within { border: heavy #ff1a3c; background: #0c0607; }
    .panel.selected { border: heavy #ff1a3c; border-title-background: #2a0309; }
    /* в окнах: CSS приложения сильнее DEFAULT_CSS экранов, поэтому правила для .panel — здесь */
    FocusScreen .panel { height: 1fr; border: none; }
    #p-git { overflow-y: auto; }
    Item { margin-bottom: 1; }
    Item.incoming { margin-bottom: 0; }
    #p-feed Item { margin-bottom: 0; }
    Item.hl { background: #2a0309; border-left: thick #ff1a3c; padding-left: 1; }
    #status { height: 1; padding: 0 1; background: #140205; color: #a8a29a; }
    #prompt { border: heavy #ff1a3c; background: #08080a; color: #e9e4dc; }
    #prompt:focus { border: heavy #ff1a3c; background-tint: #ff1a3c 4%; }
    #prompt > .input--placeholder { color: #6e7681; }
    #prompt > .input--cursor { background: #ff1a3c; color: #050506; }
    """
    COMMAND_PALETTE_BINDING = "ctrl+backslash"
    BINDINGS = [
        # всё основное — через alt; priority: срабатывает, даже когда курсор в строке ввода
        Binding("alt+space", "open_selected", "Окно панели", priority=True),
        Binding("alt+o", "open_selected", show=False, priority=True),
        *[Binding(f"alt+{i}", f"select_index({i})", show=False, priority=True) for i in range(1, 10)],
        Binding("alt+left", "move(-1, 0)", show=False, priority=True),
        Binding("alt+right", "move(1, 0)", show=False, priority=True),
        Binding("alt+up", "move(0, -1)", show=False, priority=True),
        Binding("alt+down", "move(0, 1)", show=False, priority=True),
        Binding("alt+g", "open_git", "Git", priority=True),
        Binding("alt+d", "open_diff", show=False, priority=True),
        Binding("alt+z", "zoom_selected", show=False, priority=True),
        Binding("alt+p", "toggle_pause", show=False, priority=True),
        Binding("alt+s", "stop", show=False, priority=True),
        Binding("alt+b", "branches", show=False, priority=True),
        Binding("alt+w", "slash('/worktrees')", show=False, priority=True),
        Binding("alt+n", "slash('/new')", show=False, priority=True),
        Binding("alt+e", "slash('/export')", show=False, priority=True),
        Binding("alt+h", "slash('/help')", show=False, priority=True),
        Binding("alt+t", "open_team", show=False, priority=True),
        Binding("f5", "toggle_pause", "Пауза"),
        Binding("f8", "stop", "Стоп"),
        Binding("ctrl+b", "branches", "Ветки"),
        Binding("escape", "unzoom", "Свернуть", show=False),
        Binding("ctrl+q", "quit", "Выход"),
    ]

    def __init__(self, cfg: Config, repo: str, resume: bool = False, setup: bool = False,
                 config_path: str | None = None):
        super().__init__()
        self.cfg = cfg
        self.repo = repo
        self.setup = setup              # показать стартовый экран (проект, модели, сессия)
        self.config_path = config_path  # явный --config: не подменять конфигом проекта
        self.resume = resume            # продолжить последнюю сессию проекта
        self.orch: Orchestrator | None = None
        self.selected = "manager"       # панель, которую откроет alt+space
        self.merging = ""               # чья ветка вливается с конфликтом, открытым в окне Git
        self.focus_agent: str | None = None
        self.focus_screen: FocusScreen | None = None
        self.snapshot = GitSnapshot()
        self.panels: dict[str, AgentPanel] = {}

    def compose(self) -> ComposeResult:
        yield Static(BANNER, id="banner")
        with Container(id="grid"):
            yield GitPanel(1)
            yield from self.make_panels()
            yield FeedPanel("feed", "Лента команды", len(self.cfg.agents) + 2)
        yield Static(id="status")
        yield PromptInput(id="prompt", placeholder="▸ задача для команды… @agent — адресно · /help — команды")

    # ---------------------------------------------------------- lifecycle
    async def on_mount(self):
        self.register_theme(CAPRICORN_THEME)
        self.theme = "capricorn"
        if self.setup:
            self.push_screen(StartScreen(self.cfg, self.repo, self.config_path), self._setup_done)
        else:
            await self.start_team()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Пока открыт стартовый экран, команды нет — alt-клавиши приложения не срабатывают."""
        if self.orch is None and action not in ("quit", "command_palette"):
            return False
        return True

    async def _setup_done(self, choice: dict | None):
        if not choice:
            return
        self.repo = choice["repo"]
        if not self.config_path:
            self.cfg = load_config(base=self.repo)
        start.apply_models(self.cfg, choice["models"])
        start.save_project_settings(self.repo, {"models": choice["models"]})
        start.remember_project(self.repo)
        self.resume = choice["resume"]
        await self.start_team()

    @property
    def panel_order(self) -> list[str]:
        return ["git", *self.panels, "feed"]

    def make_panels(self) -> list[AgentPanel]:
        self.panels = {key: AgentPanel(key, i, title=title_of(self.cfg, key))
                       for i, key in enumerate(self.cfg.agents, 2)}
        return list(self.panels.values())

    async def rebuild_panels(self):
        """Состав команды поменялся (team.yaml, окно /team): панели агентов собираются заново."""
        for p in list(self.panels.values()):
            await p.remove()
        await self.query_one("#grid").mount_all(self.make_panels(), before=self.feed)
        self.feed.index = len(self.panels) + 2
        self.feed.border_title = f"▓ {self.feed.index:02d} // ЛЕНТА КОМАНДЫ"
        if self.selected not in self.panel_order:
            self.selected = self.orch.default_target if self.orch else "feed"

    async def start_team(self):
        self.orch = Orchestrator(self.cfg, self.repo, self.handle_orch_event)
        if list(self.panels) != list(self.cfg.agents):
            await self.rebuild_panels()
        previous = self.orch.store.latest()
        restored = None
        if self.resume and previous:
            restored = self.orch.store.load(previous)
            warnings = self.orch.restore(restored)
        self.orch.toolbox.worktrees.on_conflict = self.merge_conflict
        self.orch.start()
        self.feed.system(f"▓ CAPRICORN ONLINE ┃ конфиг: {self.cfg.source} ┃ репо: {self.repo}")
        if self.orch.team_error:
            self.feed.system(f"▲ {self.orch.team_error}. Работает команда по умолчанию", "error")
        if restored:
            self.replay()
            for w in warnings:
                self.feed.system(f"▲ {w}", "warn")
            self.feed.system(f"► продолжаем сессию {self.orch.session_id}. /continue — доделать очередь")
        else:
            self.feed.system("/help — команды. Напиши задачу — менеджер раздаст её команде.")
            if previous:
                self.feed.system(f"есть прошлая сессия {previous} — /session last, список — /sessions")
        self.sync_meta()
        self.refresh_git()
        self.set_interval(5, self.refresh_git)
        self.set_interval(0.5, self.update_status)
        self.query_one("#prompt").focus()

    def sync_meta(self):
        for name, p in self.panels.items():
            a = self.orch.agents.get(name)
            if a is None:
                continue
            p.set_meta(model=self.cfg.agents[name].model, status=a.status, tok=(a.tokens_in, a.tokens_out, a.tokens_cached))
        self.apply_layout()
        self.select(self.selected)

    def replay(self):
        """Перерисовать ленту и панели из сохранённой переписки."""
        self.feed.clear()
        for p in self.panels.values():
            p.clear()
        for msg in self.orch.messages:
            self.show_message(msg, live=False)

    def on_unmount(self):
        if self.orch is not None:
            self.orch.save()

    @property
    def feed(self) -> FeedPanel:
        return self.query_one("#p-feed", FeedPanel)

    def apply_layout(self):
        """Панели тех, кто в составе; выключенные на задачу (/off) — приглушены. Лента добирает строку сетки."""
        for name, p in self.panels.items():
            p.display = self.cfg.agents[name].enabled if name in self.cfg.agents else False
            off = self.orch is not None and name in self.orch.off
            p.set_class(off, "off")
        visible = len(self.visible_panels())
        rows = max(1, -(-visible // 3))
        self.query_one("#grid").styles.grid_size_rows = rows
        self.feed.styles.column_span = 3 * rows - visible + 1
        if self.selected not in self.visible_panels():
            self.select(self.orch.default_target if self.orch else "feed")

    @work(exclusive=True, group="git")
    async def refresh_git(self):
        self.snapshot = await self.orch.git.snapshot()
        try:
            panel = self.query_one(GitPanel)
        except NoMatches:            # экран уже разбирается (выход)
            return
        panel.show(self.snapshot)

    def update_status(self):
        try:
            status = self.query_one("#status", Static)
        except NoMatches:            # интервал сработал во время выхода
            return
        o = self.orch
        busy = [a.name for a in o.agents.values() if a.status in ACTIVE]
        tin = sum(a.tokens_in for a in o.agents.values())
        tout = sum(a.tokens_out for a in o.agents.values())
        tcache = sum(a.tokens_cached for a in o.agents.values())
        if not o.running.is_set():
            state = f"[bold #050506 on #f5d300] ‖ ПАУЗА [/]"
        elif busy:
            state = f"[bold #050506 on {RED}] ► LIVE [/] [{RED}]{' '.join(busy)}[/]"
        else:
            state = f"[{STEEL}]▫ STANDBY[/]"
        att = f" ┃ ФАЙЛЫ {len(o.attachments)}" if o.attachments else ""
        mode = f"{len(o.roster)} АГЕНТОВ" + (f" [{STEEL}]выкл: {', '.join(sorted(o.off))}[/]" if o.off else "")
        sep = f" [{RED_DIM}]┃[/] "
        status.update(
            f"{state}{sep}ХОДЫ [{BONE}]{o.budget}[/]{sep}СОСТАВ [{BONE}]{mode}[/]{sep}"
            f"ТОКЕНЫ [{BONE}]{k(tin)}↑ {k(tout)}↓[/]" + (f" [{STEEL}]кэш {k(tcache)}[/]" if tcache else "")
            + f"{att}{sep}"
            f"[{STEEL}]alt+space → [{BONE}]{PANEL_NAMES.get(self.selected) or title_of(self.cfg, self.selected)}[/] · клик/alt+1..9 — выбор · alt+g git · alt+h справка[/]")

    # ------------------------------------------------------ orchestrator
    def show_message(self, msg: Message, live: bool = True):
        self.feed.message(msg)
        for n in msg.to:
            if n in self.panels:
                self.panels[n].incoming(msg)
        if not live and msg.sender in self.panels and msg.content.strip():
            self.panels[msg.sender].own(msg.content, msg.to)     # вживую своё сообщение пришло стримом
        if live and USER in msg.to and msg.kind != "private":     # личные ответы видны в окне агента
            self.bell()

    def handle_orch_event(self, kind: str, **d):
        focus = self.focus_screen if d.get("agent") == self.focus_agent and self.focus_agent else None
        if kind == "message":
            msg: Message = d["msg"]
            self.show_message(msg)
            if (self.focus_screen and msg.kind == "private" and msg.sender == USER
                    and msg.to == [self.focus_agent]):
                self.focus_screen.panel.user_line(msg.content)
        elif kind == "stream_start":
            self.panels[d["agent"]].stream_start(d["msg_id"])
            if focus:
                focus.panel.stream_start(d["msg_id"])
        elif kind == "stream_chunk":
            self.panels[d["agent"]].stream_chunk(d["msg_id"], d["chunk"])
            if focus and d["msg_id"] in focus.panel.streams:
                focus.panel.stream_chunk(d["msg_id"], d["chunk"])
        elif kind == "stream_end":
            self.panels[d["agent"]].stream_end(d["msg_id"], d["text"], d["to"])
            if focus and d["msg_id"] in focus.panel.streams:
                focus.panel.stream_end(d["msg_id"], d["text"], d["to"])
        elif kind == "status":
            self.panels[d["agent"]].set_meta(status=d["status"], tok=(d["tokens_in"], d["tokens_out"], d.get("tokens_cached", 0)))
            if focus:
                focus.panel.set_meta(status=d["status"], tok=(d["tokens_in"], d["tokens_out"], d.get("tokens_cached", 0)))
        elif kind == "system":
            self.feed.system(d["text"], d.get("level", "info"))

    # -------------------------------------------------------------- input
    @on(Input.Submitted, "#prompt")
    def submitted(self, ev: Input.Submitted):
        text = ev.value.strip()
        ev.input.value = ""
        if not text:
            return
        ev.input.remember(text)
        if text.startswith("/"):
            try:
                self.command(text)
            except IndexError:
                self.feed.system(f"▲ не хватает аргументов: {text}. /help", "error")
            except Exception as e:  # noqa: BLE001
                self.feed.system(f"▲ {e}", "error")
        else:
            self.orch.user_says(text)

    def open_session(self, sid: str):
        o = self.orch
        if sid == "last":
            others = [s.id for s in o.store.list() if s.id != o.session_id]
            if not others:
                raise ValueError("других сессий в этом проекте нет")
            sid = others[0]
        state = o.store.load(sid)               # FileNotFoundError уйдёт в ленту как ошибка
        o.save()
        o.stop_all()
        warnings = o.restore(state)
        self.replay()
        self.sync_meta()
        for w in warnings:
            self.feed.system(f"▲ {w}", "warn")
        self.feed.system(f"► открыта сессия {sid}. /continue — доделать очередь")

    def team_summary(self) -> str:
        lines = []
        for n, a in self.cfg.agents.items():
            state = "не в составе" if not a.enabled else ("выкл. на задаче" if n in self.orch.off else "")
            mark = "▸" if n in self.orch.roster else "·"
            extra = [x for x in (a.description, f"скиллы: {', '.join(a.skills)}" if a.skills else "",
                                 "свой промпт" if a.prompt else "", state) if x]
            lines.append(f"{mark} {n:10} {a.kind:9} {title_of(self.cfg, n)}" + (f" — {'; '.join(extra)}" if extra else ""))
        lines.append("состав проекта: .capricorn/team.yaml, скиллы: .capricorn/skills/*.md")
        return "\n".join(lines)

    def _agent(self, name: str) -> str:
        if name not in self.orch.agents:
            raise ValueError(f"нет агента «{name}». Есть: {', '.join(self.orch.agents)}")
        return name

    def command(self, line: str):
        parts = shlex.split(line[1:])
        cmd, args = parts[0].lower(), parts[1:]
        o = self.orch
        if cmd in ("help", "h", "?"):
            self.feed.system(HELP, markup=True)
        elif cmd == "pause":
            o.pause(); self.feed.system("‖ пауза — текущие ответы допишутся, новые ходы не начнутся", "warn")
        elif cmd == "resume":
            o.resume(); self.feed.system("► продолжаем")
        elif cmd == "stop":
            self.action_stop()
        elif cmd == "continue":
            n = int(args[0]) if args else 15
            o.add_budget(n); self.feed.system(f"+{n} ходов")
        elif cmd == "budget":
            o.budget = int(args[0]); self.feed.system(f"бюджет: {o.budget}")
        elif cmd == "complex":
            on = (args[0].lower() in ("on", "1", "да", "yes")) if args else not self.cfg.complex_mode
            o.set_complex(on); self.apply_layout()
            self.feed.system(f"режим: {'сложный — подключён coder3' if on else 'обычный'}")
        elif cmd in ("off", "on"):
            name = self._agent(args[0])
            o.set_off(name, cmd == "off")
            self.apply_layout()
            self.feed.system(f"{title_of(self.cfg, name)} ({name}) " +
                             ("выключен на эту задачу — /on вернёт" if cmd == "off" else "снова в команде"))
        elif cmd == "team":
            if args and args[0].lower() == "list":
                self.feed.system(self.team_summary())
            else:
                self.action_open_team()
        elif cmd == "model":
            name = self._agent(args[0]); o.set_model(name, args[1])
            self.panels[name].set_meta(model=args[1]); self.feed.system(f"{name} → {args[1]}")
        elif cmd == "tools":
            tc = self.cfg.tools
            if not tc.enabled:
                self.feed.system("инструменты выключены (tools.enabled: false)")
            else:
                cmdline = tc.test_command if isinstance(tc.test_command, str) else " ".join(tc.test_command)
                lines = [f"{n:9} {', '.join(o.toolbox.allowed(n)) or '—'}" for n in o.roster]
                lines.append(f"тесты: {cmdline if tc.run_tests else 'выключены'} · "
                             f"запись: {'да' if tc.write else 'нет'} · раундов за ход: {tc.max_rounds}")
                self.feed.system("\n".join(lines))
        elif cmd == "worktrees":
            self.show_worktrees(args[0].lower() if args else "")
        elif cmd == "merge":
            self.merge_work(self._agent(args[0]))
        elif cmd == "undo-merge":
            self.undo_merge_work(self._agent(args[0]))
        elif cmd == "usage":
            self.feed.system(usage_report(self.orch))
        elif cmd == "diff":
            self.open_diff(self._agent(args[0]))
        elif cmd == "models":
            self.feed.system("\n".join(f"{n:9} {a.model}" for n, a in self.cfg.agents.items())
                             + "\nпровайдеры: " + ", ".join(o.providers))
        elif cmd == "retry":
            o.retry(self._agent(args[0]))
        elif cmd == "reset":
            o.reset(self._agent(args[0]) if args else None)
            self.feed.system(f"память очищена: {args[0] if args else 'все'}")
        elif cmd == "sessions":
            infos = o.store.list()
            if not infos:
                self.feed.system("сохранённых сессий в этом проекте нет")
            else:
                self.feed.system("\n".join(
                    f"{'▸' if s.id == o.session_id else ' '} {s.id}  {time.strftime('%d.%m %H:%M', time.localtime(s.updated))}"
                    f"  {s.messages:3} сообщ.  {s.title or '—'}" for s in infos[:15])
                    + "\n/session <id> или /session last — продолжить")
        elif cmd == "session":
            self.open_session(args[0])
        elif cmd == "new":
            o.save()
            o.stop_all()
            o.new_session()
            o.reset()
            o.budget = self.cfg.budget
            self.replay()
            self.sync_meta()
            self.feed.system(f"► новая сессия {o.session_id}. Прошлая сохранена — /sessions")
        elif cmd == "export":
            o.save()
            path = (Path(self.repo, args[0]) if args else o.store.dir(o.session_id) / f"capricorn-{o.session_id}.md")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(to_markdown(o.snapshot()), encoding="utf-8")
            self.feed.system(f"► переписка сохранена: {path}")
        elif cmd == "attach":
            p = o.attach(args[0]); self.feed.system(f"+ приложу к следующему сообщению: {p}")
        elif cmd == "branch":
            self.switch_branch(args[0])
        elif cmd == "zoom":
            self.zoom(args[0])
        elif cmd in ("quit", "exit", "q"):
            self.exit()
        else:
            raise ValueError(f"неизвестная команда /{cmd}. /help")

    # ------------------------------------------------------------ actions
    def action_toggle_pause(self):
        if self.orch.running.is_set():
            self.command("/pause")
        else:
            self.command("/resume")

    def action_stop(self):
        self.orch.stop_all()
        self.feed.system("■ все агенты остановлены. Новая задача снова запустит команду.", "warn")

    def action_branches(self):
        if not self.snapshot.branches:
            return
        self.push_screen(BranchPicker(self.snapshot.branches, self.snapshot.branch),
                         lambda b: b and self.switch_branch(b))

    @work(group="git")
    async def show_worktrees(self, sub: str = ""):
        wts = self.orch.toolbox.worktrees
        if not wts.active:
            self.feed.system("worktree выключены: нужен git-репозиторий с коммитами и tools.worktrees: true")
            return
        try:
            if sub == "prune":
                removed, failed = await wts.prune(self.orch.roster)
                if removed or not failed:
                    self.feed.system(f"убраны влитые worktree: {', '.join(removed)}" if removed
                                     else "убирать нечего: у всех есть невлитая работа или worktree не созданы")
                for agent, why in failed:
                    self.feed.system(f"▲ {agent}: worktree не удалён — {why}. Файлы, созданные в песочнице Codex, "
                                     "бывают доступны только ей (см. README)", "warn")
            else:
                self.feed.system("\n".join(await wts.status(self.orch.roster)))
        except Exception as e:  # noqa: BLE001
            self.feed.system(f"▲ {e}", "error")
        self.refresh_git()

    @work(group="git")
    async def merge_work(self, agent: str):
        wts = self.orch.toolbox.worktrees
        if not wts.active:
            self.feed.system("worktree выключены — сливать нечего")
            return
        try:
            res = await wts.merge(agent)
            self.feed.system(("► " if res.ok else "▲ ") + res.text, "info" if res.ok else "error")
        except Exception as e:  # noqa: BLE001
            self.feed.system(f"▲ {e}", "error")
        self.refresh_git()

    def undo_merge_work(self, agent: str):
        if not self.orch.toolbox.worktrees.active:
            self.feed.system("worktree выключены — откатывать нечего")
            return
        self.push_screen(ConfirmModal(f"Откатить последнее слияние capricorn/{agent}?"),
                         lambda confirmed: self.perform_undo_merge(agent) if confirmed else None)

    @work(group="git")
    async def perform_undo_merge(self, agent: str):
        try:
            res = await self.orch.toolbox.worktrees.undo_merge(agent)
            self.feed.system(("► " if res.ok else "▲ ") + res.text, "info" if res.ok else "error")
        except Exception as e:  # noqa: BLE001
            self.feed.system(f"▲ {e}", "error")
        self.refresh_git()

    def merge_conflict(self, agent: str, files: list[str]):
        """Слияние ветки агента упёрлось в конфликт: оно оставлено открытым, разбирает человек в окне Git."""
        self.merging = agent
        self.feed.system(f"▲ конфликт при слиянии capricorn/{agent}: {', '.join(files)}. "
                         "Окно Git: enter на файле — разобрать, m — завершить слияние, A — отменить", "warn")
        self.call_later(self.show_merge_conflict)

    def show_merge_conflict(self):
        git_open = next((s for s in self.screen_stack if isinstance(s, GitScreen)), None)
        if git_open is not None:
            git_open.reload()
        elif not isinstance(self.screen, OVERLAYS):
            self.push_screen(GitScreen(), lambda _: self.refresh_git())
        self.refresh_git()

    def merge_done(self, finished: bool):
        """Пользователь завершил или отменил слияние в окне Git — сказать тому, кто ведёт работу (техлиду)."""
        agent, self.merging = self.merging, ""
        if not agent:
            return
        if finished:
            self.orch.notify(self.orch.lead, f"пользователь завершил слияние capricorn/{agent}: конфликты "
                                             "разрешены, работа влита. Можно продолжать.")
        else:
            self.orch.notify(self.orch.lead, f"пользователь отменил слияние capricorn/{agent}. Попроси @{agent} "
                                             "выполнить sync_branch, разрешить конфликты у себя и снова отдать "
                                             "на ревью.")

    @work(group="git")
    async def switch_branch(self, branch: str):
        ok, out = await self.orch.git.switch(branch)
        self.feed.system(("► " if ok else "▲ ") + out, "info" if ok else "error")
        self.refresh_git()

    def zoom(self, key: str):
        w = self.query_one(f"#p-{key}")
        self.screen.maximize(w)

    def action_unzoom(self):
        self.screen.minimize()
        self.query_one("#prompt").focus()

    # ------------------------------------------------- выбор панели и окна
    def visible_panels(self) -> list[str]:
        return [k for k in self.panel_order if k not in self.cfg.agents or self.cfg.agents[k].enabled]

    def select(self, key: str):
        if key not in self.visible_panels():
            return
        self.selected = key
        for k in self.panel_order:
            self.query_one(f"#p-{k}").set_class(k == key, "selected")
        focused = self.focused
        if getattr(focused, "key", key) != key and focused in self.screen_stack[0].query(".panel"):
            self.query_one("#prompt").focus()   # иначе рамка :focus остаётся на прежней панели — выделены две
        if self.orch is not None:
            self.update_status()            # подсказка «alt+space → …» меняется сразу

    def action_select_index(self, i: int):
        order = self.panel_order
        if i <= len(order):
            self.select(order[i - 1])

    def grid_cells(self) -> dict[tuple[int, int], str]:
        """Клетки сетки, как они видны на экране: скрытые панели сдвигают остальные, лента добирает строку."""
        cells, pos = {}, 0
        visible = self.visible_panels()
        for key in visible:
            span = int(self.feed.styles.column_span or 1) if key == "feed" else 1
            for s in range(span):
                cells[divmod(pos + s, 3)] = key
            pos += span
        return cells

    def action_move(self, dx: int, dy: int):
        cells = self.grid_cells()
        mine = [rc for rc, k in cells.items() if k == self.selected]
        if not mine:
            return
        row, col = mine[0] if dx <= 0 else mine[-1]
        r, c = row + dy, col + dx
        while (r, c) in cells:
            if cells[(r, c)] != self.selected:
                self.select(cells[(r, c)])
                return
            r, c = r + dy, c + dx

    def action_zoom_selected(self):
        self.zoom(self.selected)

    def action_open_selected(self):
        if isinstance(self.screen, OVERLAYS):                              # alt+space в окне — закрыть его
            self.screen.action_close()
            return
        if self.selected == "git":
            self.action_open_git()
        elif self.selected == "feed":
            self.zoom("feed")
        else:
            self.open_focus(self.selected)

    def action_open_team(self):
        from .team_ui import TeamScreen
        if isinstance(self.screen, TeamScreen):
            self.screen.action_close()
        elif not isinstance(self.screen, OVERLAYS) and self.screen is self.screen_stack[0]:
            self.push_screen(TeamScreen())

    def action_open_git(self):
        if isinstance(self.screen, (GitScreen, ConflictScreen, AgentDiffScreen)):
            self.screen.action_close()
            return
        if isinstance(self.screen, FocusScreen):
            return
        self.push_screen(GitScreen(), lambda _: self.refresh_git())

    def action_open_diff(self):
        if isinstance(self.screen, AgentDiffScreen):
            self.screen.action_close()
        elif not isinstance(self.screen, OVERLAYS):
            self.open_diff(self.selected)

    def open_diff(self, agent: str):
        if not self.cfg.is_writer(agent):
            self.feed.system("diff есть у кодеров и тестировщика: выбери их панель (alt+цифра) или /diff coder1")
        elif not self.orch.toolbox.worktrees.active:
            self.feed.system("worktree выключены — у агентов нет своих веток")
        else:
            self.push_screen(AgentDiffScreen(agent), lambda _: self.refresh_git())

    def open_focus(self, agent: str):
        self.focus_agent = agent
        self.orch.open_private(agent)
        self.focus_screen = FocusScreen(agent)
        self.push_screen(self.focus_screen, self._focus_closed)

    def _focus_closed(self, _result=None):
        agent, self.focus_agent, self.focus_screen = self.focus_agent, None, None
        if agent and self.orch.end_private(agent):
            self.feed.system(f"► {title_of(self.cfg, agent)} подводит итог разговора — он уйдёт команде")
        self.query_one("#prompt").focus()

    def action_slash(self, line: str):
        try:
            self.command(line)
        except Exception as e:  # noqa: BLE001
            self.feed.system(f"▲ {e}", "error")
