"""Терминальный интерфейс на Textual: сетка панелей агентов + git + общая лента + ввод."""
from __future__ import annotations

import shlex
import time

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Container, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Input, Label, OptionList, Static

from .config import Config
from .gitinfo import GitSnapshot
from .orchestrator import USER, Message, Orchestrator
from .prompts import ROLE_TITLES

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
CODE_THEME = "fruity"
SPIN = "░▒▓█▓▒"
PANEL_ORDER = ["git", "architect", "manager", "coder1", "coder2", "coder3", "reviewer", "tester", "feed"]
MAX_ITEMS = 150
ACTIVE = ("thinking", "typing", "working")     # working — агент выполняет инструмент

HELP = """\
[b]Ввод[/b]: текст → менеджеру; [#00e5ff]@architect @reviewer текст[/] → адресно; @coders, @all.
[b]Команды[/b]:
  /pause /resume /stop          — пауза, продолжить, прервать всех
  /continue [N]                 — добавить N ходов (по умолч. 15)
  /complex on|off               — третий кодер
  /model <агент> <пров/модель>  — сменить модель на лету;  /models — список
  /retry <агент>  /reset [агент] — повтор после ошибки, очистка памяти
  /attach <путь>                — приложить файл к следующему сообщению
  /branch <имя>                 — git switch (или ctrl+b)
  /zoom <агент|git|feed>        — развернуть панель (esc — назад)
  /tools                        — инструменты агентов (чтение/запись файлов, diff, тесты)
  /worktrees \\[prune]           — ветки кодеров; prune — убрать уже влитые
  /budget N  /help  /quit
[b]Клавиши[/b]: F5 пауза · F8 стоп · ctrl+b ветки · alt+1..9 развернуть панель · esc свернуть"""


def ago(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


# ------------------------------------------------------------------ widgets

class Item(Static):
    """Одно сообщение в панели."""


class LogPanel(VerticalScroll):
    can_focus = True
    ALLOW_MAXIMIZE = True

    def __init__(self, key: str, title: str, index: int):
        super().__init__(id=f"p-{key}", classes="panel")
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


class AgentPanel(LogPanel):
    def __init__(self, key: str, index: int):
        super().__init__(key, ROLE_TITLES[key], index)
        self.styles.border_title_color = COLORS[key].replace("bold ", "")
        self.streams: dict[int, tuple[Item, str]] = {}
        self.dirty: set[int] = set()
        self.status = "idle"
        self.model = ""
        self.tok = (0, 0)
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
        icon = {"idle": "▫", "error": "×", "stopped": "■", "waiting budget": "‖"}.get(
            self.status, SPIN[self._spin % len(SPIN)])
        self.set_class(self.status in ACTIVE, "active")
        self.set_class(self.status == "error", "failed")
        self.border_subtitle = (f"{icon} {self.status.upper()} ┃ {self.model} ┃ "
                                f"{k(self.tok[0])}↑ {k(self.tok[1])}↓")

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
        t = Text(f"{ago(msg.ts)} ", style="#4a4f57")
        t.append(msg.sender, style=COLORS.get(msg.sender, "white"))
        t.append(" ▸ " if msg.to else " · ", style=STEEL)
        for i, n in enumerate(msg.to):
            t.append(("" if i == 0 else ", ") + n, style=COLORS.get(n, "white"))
        body = msg.content.strip()
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
        t.append(f"●{s.staged} индекс  ", style=COLORS["coder1"])
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
    .panel:focus, .panel:focus-within { border: heavy #ff1a3c; background: #0c0607; }
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
        Binding("f5", "toggle_pause", "Пауза"),
        Binding("f8", "stop", "Стоп"),
        Binding("ctrl+b", "branches", "Ветки"),
        Binding("escape", "unzoom", "Свернуть", show=False),
        Binding("ctrl+q", "quit", "Выход"),
        *[Binding(f"alt+{i}", f"zoom_index({i})", show=False) for i in range(1, 10)],
    ]

    def __init__(self, cfg: Config, repo: str):
        super().__init__()
        self.cfg = cfg
        self.repo = repo
        self.orch: Orchestrator | None = None
        self.snapshot = GitSnapshot()
        self.panels: dict[str, AgentPanel] = {}

    def compose(self) -> ComposeResult:
        yield Static(BANNER, id="banner")
        with Container(id="grid"):
            for i, key in enumerate(PANEL_ORDER, 1):
                if key == "git":
                    yield GitPanel(i)
                elif key == "feed":
                    yield FeedPanel("feed", "Лента команды", i)
                else:
                    p = AgentPanel(key, i)
                    self.panels[key] = p
                    yield p
        yield Static(id="status")
        yield PromptInput(id="prompt", placeholder="▸ задача для команды… @agent — адресно · /help — команды")

    # ---------------------------------------------------------- lifecycle
    def on_mount(self):
        self.register_theme(CAPRICORN_THEME)
        self.theme = "capricorn"
        self.orch = Orchestrator(self.cfg, self.repo, self.handle_orch_event)
        for name, p in self.panels.items():
            p.set_meta(model=self.cfg.agents[name].model)
        self.apply_layout()
        self.orch.start()
        self.feed.system(f"▓ CAPRICORN ONLINE ┃ конфиг: {self.cfg.source} ┃ репо: {self.repo}")
        self.feed.system("/help — команды. Напиши задачу — менеджер раздаст её команде.")
        self.refresh_git()
        self.set_interval(5, self.refresh_git)
        self.set_interval(0.5, self.update_status)
        self.query_one("#prompt").focus()

    @property
    def feed(self) -> FeedPanel:
        return self.query_one("#p-feed", FeedPanel)

    def apply_layout(self):
        on = self.cfg.complex_mode
        self.panels["coder3"].display = on
        self.feed.styles.column_span = 1 if on else 2

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
        if not o.running.is_set():
            state = f"[bold #050506 on #f5d300] ‖ ПАУЗА [/]"
        elif busy:
            state = f"[bold #050506 on {RED}] ► LIVE [/] [{RED}]{' '.join(busy)}[/]"
        else:
            state = f"[{STEEL}]▫ STANDBY[/]"
        att = f" ┃ ФАЙЛЫ {len(o.attachments)}" if o.attachments else ""
        mode = "COMPLEX ×3" if self.cfg.complex_mode else "STANDARD"
        sep = f" [{RED_DIM}]┃[/] "
        status.update(
            f"{state}{sep}ХОДЫ [{BONE}]{o.budget}[/]{sep}РЕЖИМ [{BONE}]{mode}[/]{sep}"
            f"ТОКЕНЫ [{BONE}]{k(tin)}↑ {k(tout)}↓[/]{att}{sep}"
            f"[{STEEL}]F5 пауза · F8 стоп · ctrl+b ветки · /help[/]")

    # ------------------------------------------------------ orchestrator
    def handle_orch_event(self, kind: str, **d):
        if kind == "message":
            msg: Message = d["msg"]
            self.feed.message(msg)
            for n in msg.to:
                if n in self.panels:
                    self.panels[n].incoming(msg)
            if USER in msg.to:
                self.bell()
        elif kind == "stream_start":
            self.panels[d["agent"]].stream_start(d["msg_id"])
        elif kind == "stream_chunk":
            self.panels[d["agent"]].stream_chunk(d["msg_id"], d["chunk"])
        elif kind == "stream_end":
            self.panels[d["agent"]].stream_end(d["msg_id"], d["text"], d["to"])
        elif kind == "status":
            self.panels[d["agent"]].set_meta(status=d["status"], tok=(d["tokens_in"], d["tokens_out"]))
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
        elif cmd == "models":
            self.feed.system("\n".join(f"{n:9} {a.model}" for n, a in self.cfg.agents.items())
                             + "\nпровайдеры: " + ", ".join(o.providers))
        elif cmd == "retry":
            o.retry(self._agent(args[0]))
        elif cmd == "reset":
            o.reset(self._agent(args[0]) if args else None)
            self.feed.system(f"память очищена: {args[0] if args else 'все'}")
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
                removed = await wts.prune(self.orch.roster)
                self.feed.system(f"убраны влитые worktree: {', '.join(removed)}" if removed
                                 else "убирать нечего: у всех есть невлитая работа или worktree не созданы")
            else:
                self.feed.system("\n".join(await wts.status(self.orch.roster)))
        except Exception as e:  # noqa: BLE001
            self.feed.system(f"▲ {e}", "error")
        self.refresh_git()

    @work(group="git")
    async def switch_branch(self, branch: str):
        ok, out = await self.orch.git.switch(branch)
        self.feed.system(("► " if ok else "▲ ") + out, "info" if ok else "error")
        self.refresh_git()

    def zoom(self, key: str):
        w = self.query_one(f"#p-{key}")
        self.screen.maximize(w)

    def action_zoom_index(self, i: int):
        self.zoom(PANEL_ORDER[i - 1])

    def action_unzoom(self):
        self.screen.minimize()
        self.query_one("#prompt").focus()
