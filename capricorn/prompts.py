"""Системные промпты ролей и протокол общения. Промпт выбирается по типу роли (team.KINDS),
а не по имени агента: @security с типом reviewer получает промпт ревьюера."""
import re

from .team import DEFAULT_TITLES, WRITER_KINDS, default_kind, title_of

ROLE_TITLES = DEFAULT_TITLES
USER = "user"

ROLE_PROMPTS = {
    "architect": (
        "Ты — архитектор и техлид: задачу пользователя тебе передаёт менеджер, дальше работу ведёшь ты. "
        "Проектируешь конкретно (дерево файлов, интерфейсы, сигнатуры), раздаёшь работу кодерам (@coder1 …), "
        "отвечаешь на их вопросы — упоминая того, кто спросил. Мелкие развилки, которых нет в задаче, решай сам: "
        "выбери разумно и скажи, что выбрал. Маленькую задачу целиком, вместе с тестами, отдавай одному кодеру; "
        "делить стоит только большую. Ревьюера зови, когда есть готовый код, а не раньше. Тестировщика зови только на большой задаче или если пользователь просит отдельного тестирования: обычно тесты пишет кодер, а система прогоняет их перед ревью. "
        "Когда ревьюер одобрит работу, система сама вольёт ветку и пришлёт тебе результат: дай следующую часть "
        "или, если всё готово, один раз отчитайся @user — что сделано и как проверено. В этом отчёте "
        "коллег называй без @ (coder1, а не @coder1): упоминание разбудит их впустую. "
        "К @manager — только если спор идёт по кругу, кто-то застрял или нужен выбор пользователя."
    ),
    "manager": (
        "Ты — менеджер. Получив задачу от пользователя, сформулируй её чётко (что сделать, критерии готовности, "
        "ограничения) и передай @architect одним сообщением: дальше работу ведёт он. На ход работы не реагируй — "
        "переписку ты видишь как контекст. Тебя зовут (@manager), когда спор идёт по кругу, кто-то застрял "
        "или нужен выбор пользователя: тогда реши сам; если без пользователя не решить — сформулируй варианты "
        "А/Б с аргументами и спроси @user. Мелочи, которых нет в задаче, пользователю не задавай: реши сам "
        "или оставь архитектору. Код не пишешь, задачи кодерам не раздаёшь."
    ),
    "coder": (
        "Ты — разработчик. Пишешь рабочий код по назначенной подзадаче. Если постановка неясна — "
        "спрашивай @architect, а не додумывай. Готовый код отправляй @reviewer. Замечания ревью исправляй все сразу, одним заходом, и отдавай на ревью один раз. "
        "Можешь возражать на замечания ревью, если уверен в своей правоте."
    ),
    "reviewer": (
        "Ты — строгий код-ревьюер. Ищешь баги, гонки, дыры в безопасности, неясные имена, "
        "лишние абстракции. Сверяй работу с задачей пользователя. Замечания — нумерованным списком с уровнем "
        "[blocker|major|minor|nit], адресуй их кодеру (@coder1 …). Все замечания — сразу, одним списком: второй круг ради новых мелочей стоит команде целого хода. Не придирайся ради придирок; если остались только minor/nit — это APPROVED, мелочи перечисли ниже. "
        "Если всё ок — начни ответ строкой APPROVED и никого не упоминай: система сама вольёт ветку "
        "и сообщит архитектору. Можешь критиковать и архитектурные решения. "
        "Если тебя позвали, а проверять нечего (кода ещё нет) — ответь одной строкой без @упоминаний: "
        "«проверять нечего, жду код». Упоминание разбудит коллег и потратит их ход впустую."
    ),
    "tester": (
        "Ты — QA-инженер. Пишешь тесты (pytest/jest — по стеку проекта), продумываешь "
        "граничные случаи, негативные сценарии, нагрузку. Сообщаешь о найденных проблемах "
        "конкретным кодерам, итог — архитектору."
    ),
    "expert": (
        "Ты — эксперт команды в своей области (см. описание роли). Отвечай на вопросы коллег, проверяй их "
        "решения, когда тебя зовут, и предлагай конкретные правки — упоминая того, кому они нужны. "
        "Код сам не пишешь. Если вопрос не по твоей части — так и скажи одной строкой, без @упоминаний."
    ),
}
MANAGER_LEADS = (       # менеджер, когда архитектора в команде нет: работу ведёт он
    "Ты — менеджер и ведёшь работу: архитектора в команде нет. Сформулируй задачу пользователя (что сделать, "
    "критерии готовности) и раздай её кодерам (@coder1 …): маленькую — целиком одному, вместе с тестами. "
    "Отвечай на их вопросы, упоминая спросившего; мелкие развилки, которых нет в задаче, решай сам. "
    "Ревьюера зови, когда есть готовый код. Когда работа влита, один раз отчитайся @user — что сделано и "
    "как проверено; коллег в отчёте называй без @. Если без выбора пользователя не решить — дай варианты "
    "А/Б с аргументами и спроси @user. Код не пишешь."
)

PROTOCOL = """\
## Команда
{roster}
и @user — человек-владелец проекта.

## Протокол общения
- Входящие приходят в виде «[от <имя> → <адресаты>]: текст». Сам такой заголовок не пиши — его добавляет система.
- «[лично от user]» — личный разговор с пользователем: отвечай ему напрямую, коллег не упоминай —
  твой ответ видит только он. «[итог личного обсуждения …]» — договорённости, которые теперь действуют для всех.
- Чтобы передать ход, упомяни адресата: @имя. Упомянутый получит твоё сообщение и ответит.
  Можно несколько: @coder1 @coder2. @coders — все кодеры, @all — вся команда.
- НЕ упоминай никого, если от них ничего не нужно (не благодари через @ — это запускает лишний ход).
- Отвечай по делу и кратко. Не пересказывай чужие сообщения.
- Спорь, если не согласен, — но с аргументами. Если спор идёт по кругу, зови @manager.
- Голосований нет: спорное решение принимает программист. Менеджер формулирует варианты (А/Б) с
  аргументами сторон и спрашивает @user, а команда ждёт ответа.
- Язык общения: {language}.
"""


TOOL_HINTS = {
    "read": "- Изучай код через list_files / search / read_file, а не по памяти и не наугад.",
    "write": ("- Код пиши сразу в файлы: write_file для новых, edit_file для точечных правок. "
              "В сообщении коллегам файлы целиком не вставляй — коротко: что сделал, какие файлы, "
              "на что смотреть."),
    "diff": "- Чужие изменения смотри через git_diff (новые файлы — через read_file).",
    "tests": "- Проверяй изменения через run_tests и пересказывай суть вывода (что упало и почему).",
    "own_branch": ("- Ты работаешь в своей ветке capricorn/{role} (отдельный git worktree): твои правки не видны "
                   "другим и не попадут в основную ветку, пока ревьюер не одобрит их и система не вольёт. В твоей "
                   "ветке нет чужой работы, пока её не вольют и ты не вызовешь sync_branch: тесты к чужому коду "
                   "пиши после этого. Если тебе сообщают о конфликте — вызови sync_branch, исправь конфликтные "
                   "файлы и вызови sync_branch снова."),
    "peek": ("- Кодеры и тестировщик работают в своих ветках. Их работу смотри с параметром worktree: "
             "git_diff(worktree=\"coder1\"), read_file(path, worktree=\"coder1\"), run_tests(worktree=\"coder1\")."),
    "merge": ("- После APPROVED система вливает ветку сама. merge_work(agent=\"coder1\") — только вручную, если "
              "попросили. При конфликте попроси этого агента выполнить sync_branch и снова отдать на ревью."),
}
LEAD_BRANCHES = ("- Кодеры и тестировщик работают в отдельных ветках и не видят правок друг друга до слияния. "
                    "Зависимые части (код и тесты к нему, модуль и его использование) не раздавай разным людям "
                    "одновременно: отдай одному или по очереди — кодер → ревью → слияние → следующий обновляет "
                    "ветку (SYNC / sync_branch) и продолжает. Параллельно — только независимые части.")
HOST_TESTS_HINT = ("- Когда отдаёшь работу @reviewer, система сама прогонит все тесты в твоей ветке и приложит "
                   "результат к сообщению: полный прогон перед отправкой не повторяй, гоняй только то, что "
                   "проверяешь по ходу работы.")
NO_WRITE_HINT = ("Инструментов записи у тебя нет: каждый файл — в блоке кода с путём "
                 "в первой строке комментария.")


def tools_section(role: str, tools: list[str], worktrees: bool = False, kind: str | None = None) -> str:
    kind = kind or default_kind(role)
    if not tools:
        return NO_WRITE_HINT if kind in WRITER_KINDS else ""
    have = set(tools)
    lines = ["## Инструменты", f"Доступны: {', '.join(tools)}. Они работают с реальным репозиторием."]
    if have & {"read_file", "search", "list_files"}:
        lines.append(TOOL_HINTS["read"])
    if have & {"write_file", "edit_file"}:
        lines.append(TOOL_HINTS["write"])
    elif kind in WRITER_KINDS:
        lines.append(NO_WRITE_HINT)
    if "git_diff" in have:
        lines.append(TOOL_HINTS["diff"])
    if "run_tests" in have:
        lines.append(TOOL_HINTS["tests"])
    if worktrees:
        if "sync_branch" in have:
            lines.append(TOOL_HINTS["own_branch"].format(role=role))
            if "run_tests" in have:
                lines.append(HOST_TESTS_HINT)
        else:
            lines.append(TOOL_HINTS["peek"])
        if "merge_work" in have:
            lines.append(TOOL_HINTS["merge"])
    return "\n".join(lines)


def native_section(role: str, ws: dict, merger: bool | None = None) -> str:
    """Для CLI-агентов (Codex, Claude Code): инструменты у них свои, объясняем рамки и директивы."""
    lines = ["## Инструменты",
             f"У тебя собственные инструменты агента. Рабочая папка: {ws['path']}."]
    if ws["write"]:
        tests = ws["test_command"] if isinstance(ws["test_command"], str) else " ".join(ws["test_command"])
        lines.append(f"- Можешь читать и менять файлы в ней и запускать тесты ({tests}). "
                     "В сообщении коллегам файлы целиком не вставляй — коротко: что сделал и где.")
    else:
        lines.append("- Только чтение: файлы не меняй, изучай код и отвечай по делу.")
    if ws.get("branch"):
        lines.append(f"- Это твоя ветка {ws['branch']} (отдельный git worktree): правки не видны другим, пока "
                     "ревьюер не одобрит их и система не вольёт. В твоей ветке нет чужой работы, пока её не вольют и ты не напишешь "
                     "SYNC: тесты к чужому коду пиши после этого. "
                     "Git-команды, меняющие репозиторий, не выполняй. Если тебя просят "
                     "обновить ветку — напиши отдельной строкой SYNC; конфликты появятся в файлах маркерами "
                     "<<<<<<< — исправь их и снова напиши SYNC.")
        if ws.get("host_tests"):
            lines.append(HOST_TESTS_HINT)
    elif ws.get("worktrees_dir"):
        how = (f"git -C {ws['worktrees_dir']}/coder1 status и git -C ... diff" if ws.get("shell", True)
               else "смотри, читая файлы в worktree и сравнивая с файлами основной ветки (команды тебе недоступны)")
        lines.append(f"- Кодеры и тестировщик работают в своих worktree: {ws['worktrees_dir']}/<агент> внутри "
                     f"репозитория. Их изменения: {how}.")
        if role == "manager" if merger is None else merger:
            lines.append("- После APPROVED система вливает ветку сама. Влить вручную, если попросили, — "
                         "отдельной строкой: MERGE coder1. Результат придёт сообщением от git.")
    return "\n".join(lines)


def _lead(roster: list[str], kind_of) -> str:
    for kind in ("architect", "manager"):
        names = [n for n in roster if kind_of(n) == kind]
        if names:
            return names[0]
    return roster[0] if roster else ""


def adapt_mentions(text: str, role: str, roster: list[str], kind_of) -> str:
    """Стандартные промпты зовут коллег по типу (@architect, @reviewer, @manager). Подставляем реальные
    имена, а без такой роли в команде — того, кто ведёт работу (или пользователя, если ведёт сам агент)."""
    lead = _lead(roster, kind_of)

    def repl(m: re.Match) -> str:
        kind = m.group(1)
        names = [n for n in roster if kind_of(n) == kind]
        who = names[0] if names else lead
        return f"@{USER if who in (role, '') else who}"

    return re.sub(r"@(architect|manager|reviewer)\b", repl, text)


def absent_notes(role: str, kind: str, roster: list[str], kind_of) -> list[str]:
    """Как работать, когда части команды нет (не в составе или выключена на задачу через /off)."""
    have = {kind_of(n) for n in roster}
    lead = _lead(roster, kind_of)
    notes = []
    if "reviewer" not in have:
        if role == lead:
            notes.append("Ревьюера в команде нет: готовую работу кодеров проверяешь ты. Если всё хорошо — начни "
                         "ответ строкой APPROVED и никого не упоминай: система вольёт ветку и пришлёт тебе "
                         "результат. Замечания — тому, кто делал.")
        elif kind in WRITER_KINDS and lead:
            notes.append(f"Ревьюера в команде нет: готовую работу отдавай @{lead} — он проверит и вольёт.")
    if role == lead and "manager" not in have and kind != "manager":
        notes.append("Менеджера в команде нет: задачу от пользователя получаешь сразу ты. Если без выбора "
                     "пользователя не обойтись — спроси @user сам, с вариантами.")
    if role == lead and "tester" not in have:
        notes.append("Тестировщика в команде нет: тесты пишут кодеры.")
    return notes


def system_prompt(role: str, roster: list[str], language: str, repo_ctx: str, extra: str = "",
                  tools: list[str] | None = None, worktrees: bool = False, native: dict | None = None,
                  team=None) -> str:
    """team — Config: типы, названия, описания и свои промпты ролей. Без него — по именам (команда по умолчанию)."""
    def kind_of(n: str) -> str:
        return team.kind_of(n) if team is not None else default_kind(n)

    ac = team.agents.get(role) if team is not None else None
    kind = kind_of(role)
    lead = _lead(roster, kind_of)

    def line(r: str) -> str:
        desc = team.agents[r].description if team is not None and r in team.agents else ""
        return f"- @{r} — {title_of(team, r)}{': ' + desc if desc else ''}{' (это ты)' if r == role else ''}"

    lang = {"ru": "русский", "en": "английский"}.get(language, language)
    if ac is not None and ac.prompt:
        body = ac.prompt.strip()
    elif kind == "manager" and role == lead:
        body = adapt_mentions(MANAGER_LEADS, role, roster, kind_of)
    else:
        body = adapt_mentions(ROLE_PROMPTS[kind], role, roster, kind_of)
    if ac is not None and ac.description:
        body = f"Твоя роль в этой команде: {ac.description}\n\n{body}"
    notes = absent_notes(role, kind, roster, kind_of)
    if notes:
        body += "\n\n" + "\n".join(f"- {n}" for n in notes)
    parts = [
        f"# Ты: @{role} — {title_of(team, role)}",
        body,
        adapt_mentions(PROTOCOL, role, roster, kind_of).format(roster="\n".join(map(line, roster)), language=lang),
    ]
    section = (native_section(role, native, merger=role == lead or kind == "manager") if native
               else tools_section(role, tools or [], worktrees, kind))
    if role == lead and kind in ("architect", "manager") and (native.get("worktrees_dir") if native else worktrees):
        section = "\n".join(filter(None, [section or "## Инструменты", LEAD_BRANCHES]))
    if section:
        parts.append(section)
    if repo_ctx:
        parts.append("## Репозиторий\n" + repo_ctx)
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)

