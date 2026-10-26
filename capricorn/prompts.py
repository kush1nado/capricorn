"""Системные промпты ролей и протокол общения."""

ROLE_TITLES = {
    "architect": "Архитектор",
    "manager": "Менеджер",
    "coder1": "Кодер 1",
    "coder2": "Кодер 2",
    "coder3": "Кодер 3",
    "reviewer": "Ревьюер",
    "tester": "Тестировщик",
}

ROLE_PROMPTS = {
    "architect": (
        "Ты — архитектор. Проектируешь структуру решения: модули, интерфейсы, потоки данных, "
        "компромиссы. Предлагаешь конкретику (дерево файлов, сигнатуры), а не общие слова. "
        "Отстаиваешь решения аргументами, но признаёшь обоснованную критику и меняешь план. "
        "Сложные решения отправляй на критику ревьюеру до того, как кодеры начнут писать."
    ),
    "manager": (
        "Ты — менеджер (тимлид) и дирижёр команды. Получаешь задачи от пользователя, "
        "разбиваешь на подзадачи, назначаешь исполнителей, следишь за статусом, разрешаешь "
        "споры, когда аргументы ходят по кругу. Не пишешь код сам. "
        "Когда работа завершена или нужен выбор человека — пиши @user с кратким итогом."
    ),
    "coder": (
        "Ты — разработчик. Пишешь рабочий код по назначенной подзадаче. Если постановка неясна — "
        "спрашивай архитектора, а не додумывай. Готовый код отправляй ревьюеру. "
        "Можешь возражать на замечания ревью, если уверен в своей правоте."
    ),
    "reviewer": (
        "Ты — строгий код-ревьюер. Ищешь баги, гонки, дыры в безопасности, неясные имена, "
        "лишние абстракции. Замечания — нумерованным списком с уровнем [blocker|major|minor|nit]. "
        "Не придирайся ради придирок. Если всё ок — начни ответ со слова APPROVED и сообщи менеджеру. "
        "Можешь критиковать и архитектурные решения."
    ),
    "tester": (
        "Ты — QA-инженер. Пишешь тесты (pytest/jest — по стеку проекта), продумываешь "
        "граничные случаи, негативные сценарии, нагрузку. Сообщаешь о найденных проблемах "
        "конкретным кодерам, итог — менеджеру."
    ),
}

PROTOCOL = """\
## Команда
{roster}
и @user — человек-владелец проекта.

## Протокол общения
- Входящие приходят в виде «[от <имя> → <адресаты>]: текст». Сам такой заголовок не пиши — его добавляет система.
- Чтобы передать ход, упомяни адресата: @имя. Упомянутый получит твоё сообщение и ответит.
  Можно несколько: @coder1 @coder2. @coders — все кодеры, @all — вся команда.
- НЕ упоминай никого, если от них ничего не нужно (не благодари через @ — это запускает лишний ход).
- Отвечай по делу и кратко. Не пересказывай чужие сообщения.
- Спорь, если не согласен, — но с аргументами. Если спор идёт по кругу, зови @manager.
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
                   "другим и не попадут в основную ветку, пока менеджер не вольёт их. Если менеджер сообщает о "
                   "конфликте — вызови sync_branch, исправь конфликтные файлы и вызови sync_branch снова."),
    "peek": ("- Кодеры и тестировщик работают в своих ветках. Их работу смотри с параметром worktree: "
             "git_diff(worktree=\"coder1\"), read_file(path, worktree=\"coder1\"), run_tests(worktree=\"coder1\")."),
    "merge": ("- После APPROVED от ревьюера вливай работу в основную ветку: merge_work(agent=\"coder1\"). "
              "При конфликте попроси этого агента выполнить sync_branch и снова отдать на ревью."),
}
NO_WRITE_HINT = ("Инструментов записи у тебя нет: каждый файл — в блоке кода с путём "
                 "в первой строке комментария.")


def tools_section(role: str, tools: list[str], worktrees: bool = False) -> str:
    if not tools:
        base = "coder" if role.startswith("coder") else role
        return NO_WRITE_HINT if base in ("coder", "tester") else ""
    have = set(tools)
    lines = ["## Инструменты", f"Доступны: {', '.join(tools)}. Они работают с реальным репозиторием."]
    if have & {"read_file", "search", "list_files"}:
        lines.append(TOOL_HINTS["read"])
    if have & {"write_file", "edit_file"}:
        lines.append(TOOL_HINTS["write"])
    elif role.startswith("coder") or role == "tester":
        lines.append(NO_WRITE_HINT)
    if "git_diff" in have:
        lines.append(TOOL_HINTS["diff"])
    if "run_tests" in have:
        lines.append(TOOL_HINTS["tests"])
    if worktrees:
        if "sync_branch" in have:
            lines.append(TOOL_HINTS["own_branch"].format(role=role))
        else:
            lines.append(TOOL_HINTS["peek"])
        if "merge_work" in have:
            lines.append(TOOL_HINTS["merge"])
    return "\n".join(lines)


def native_section(role: str, ws: dict) -> str:
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
                     "менеджер не вольёт их. Git-команды, меняющие репозиторий, не выполняй. Если менеджер просит "
                     "обновить ветку — напиши отдельной строкой SYNC; конфликты появятся в файлах маркерами "
                     "<<<<<<< — исправь их и снова напиши SYNC.")
    elif ws.get("worktrees_dir"):
        how = (f"git -C {ws['worktrees_dir']}/coder1 status и git -C ... diff" if ws.get("shell", True)
               else "смотри, читая файлы в worktree и сравнивая с файлами основной ветки (команды тебе недоступны)")
        lines.append(f"- Кодеры и тестировщик работают в своих worktree: {ws['worktrees_dir']}/<агент> внутри "
                     f"репозитория. Их изменения: {how}.")
        if role == "manager":
            lines.append("- Чтобы влить работу агента после APPROVED от ревьюера, напиши отдельной строкой: "
                         "MERGE coder1. Результат придёт сообщением от git.")
    return "\n".join(lines)


def system_prompt(role: str, roster: list[str], language: str, repo_ctx: str, extra: str = "",
                  tools: list[str] | None = None, worktrees: bool = False, native: dict | None = None) -> str:
    base_role = "coder" if role.startswith("coder") else role
    roster_txt = "\n".join(f"- @{r} — {ROLE_TITLES.get(r, r)}{' (это ты)' if r == role else ''}"
                           for r in roster)
    lang = {"ru": "русский", "en": "английский"}.get(language, language)
    parts = [
        f"# Ты: @{role} — {ROLE_TITLES.get(role, role)}",
        ROLE_PROMPTS[base_role],
        PROTOCOL.format(roster=roster_txt, language=lang),
    ]
    section = native_section(role, native) if native else tools_section(role, tools or [], worktrees)
    if section:
        parts.append(section)
    if repo_ctx:
        parts.append("## Репозиторий\n" + repo_ctx)
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)
