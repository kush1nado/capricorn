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
- Входящие приходят в виде «[от <имя> → <адресаты>]: текст».
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
}
NO_WRITE_HINT = ("Инструментов записи у тебя нет: каждый файл — в блоке кода с путём "
                 "в первой строке комментария.")


def tools_section(role: str, tools: list[str]) -> str:
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
    return "\n".join(lines)


def system_prompt(role: str, roster: list[str], language: str, repo_ctx: str, extra: str = "",
                  tools: list[str] | None = None) -> str:
    base_role = "coder" if role.startswith("coder") else role
    roster_txt = "\n".join(f"- @{r} — {ROLE_TITLES.get(r, r)}{' (это ты)' if r == role else ''}"
                           for r in roster)
    lang = {"ru": "русский", "en": "английский"}.get(language, language)
    parts = [
        f"# Ты: @{role} — {ROLE_TITLES.get(role, role)}",
        ROLE_PROMPTS[base_role],
        PROTOCOL.format(roster=roster_txt, language=lang),
    ]
    section = tools_section(role, tools or [])
    if section:
        parts.append(section)
    if repo_ctx:
        parts.append("## Репозиторий\n" + repo_ctx)
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)
