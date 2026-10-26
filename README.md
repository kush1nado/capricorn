# Capricorn — мультиагентная среда разработки в терминале

[![tests](https://github.com/kush1nado/capricorn/actions/workflows/tests.yml/badge.svg)](https://github.com/kush1nado/capricorn/actions/workflows/tests.yml)

Киберпанк-терминал в красно-чёрной корпоративной палитре. Команда агентов на одном экране: архитектор, менеджер, 2–3 кодера, ревьюер, тестировщик,
плюс git-панель и общая лента. Агенты передают друг другу ход через `@упоминания`, спорят
и критикуют — а ты в любой момент вмешиваешься из строки ввода.
Каждый агент может работать на своей модели: Claude, OpenAI, DeepSeek, Qwen, MiniMax, OpenRouter, Ollama, LM Studio.

```
┌ 1 Git ─────────┬ 2 Архитектор ──┬ 3 Менеджер ────┐
├ 4 Кодер 1 ─────┼ 5 Кодер 2 ─────┼ 6 Кодер 3* / 7 Ревьюер
├ 8 Тестировщик ─┴ 9 Лента команды (вся переписка) ┘
 статус: работают / пауза · ходов осталось · токены
 > ввод
```

## Запуск

Работает на Windows, Linux и macOS (Python ≥ 3.10); тесты гоняются на всех трёх в CI.

**Установка** (один раз, в корне проекта):

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .           # Windows: .venv\Scripts\python -m pip install -e .
```

**Запуск:**

| ОС | Как |
|---|---|
| Windows | двойной клик по `Capricorn.cmd` или `.venv\Scripts\capricorn` в консоли |
| Linux, macOS | `.venv/bin/capricorn` или `source .venv/bin/activate`, затем `capricorn` |

`Capricorn.cmd` — только для Windows (переключает консоль в UTF-8). На Linux и macOS он не нужен.
На macOS стандартный Terminal.app не умеет truecolor — тема будет в упрощённой 256-цветной палитре;
в iTerm2, Ghostty, WezTerm, Kitty всё как задумано.

**Параметры:**

```bash
capricorn --mock                 # без ключей: заглушка разыгрывает работу команды
cp config.example.yaml capricorn.yaml   # в корне своего репозитория; ключи — в env
capricorn --repo ~/work/project  # реальные модели
capricorn --complex              # сразу с третьим кодером
```

Конфиг ищется в `./capricorn.yaml`, `./.capricorn/config.yaml`, `~/.config/capricorn/config.yaml`.

## Как это устроено

| Файл | Что делает |
|---|---|
| `providers.py` | 2 адаптера: нативный Anthropic и любой OpenAI-совместимый endpoint, оба со стримингом и tool calling (сами убирают `temperature`/`max_tokens`/`tools`, если провайдер их не принимает). `mock` — для офлайна, инструменты не вызывает. |
| `tools.py` | Инструменты агентов: чтение/запись файлов, поиск, `git diff`, запуск тестов. Песочница — корень репозитория. |
| `orchestrator.py` | Шина сообщений. У каждого агента своя очередь и своя история. `@имя` в ответе = передать ход; `@coders`, `@all`, `@user`. Упоминания внутри кода игнорируются. Пачка входящих = один ход. |
| `prompts.py` | Роли и протокол общения (когда звать, когда молчать, как спорить). |
| `gitinfo.py` | Ветка, ahead/behind, статус, коммиты, ветки, PR/MR (через `gh` / `glab`), `git switch`. |
| `tui.py` | Textual-интерфейс: стриминг ответов, спиннеры статусов, зум панели, модалка веток. |

**Защита от бесконечных споров**: бюджет ходов на задачу (`budget`, по умолчанию 30).
Кончился — команда встаёт на паузу, `/continue 20` даёт ещё.

**Инструменты.** Агенты работают с реальным репозиторием через tool calling:

| Инструмент | Кто | Что |
|---|---|---|
| `list_files`, `read_file`, `search` | все (менеджер — без `search`) | файлы с учётом `.gitignore`, чтение, поиск по regex |
| `git_diff` | все | незакоммиченные изменения + список новых файлов |
| `write_file`, `edit_file` | кодеры, тестировщик | создать/перезаписать файл, заменить уникальный фрагмент |
| `run_tests` | кодеры, ревьюер, тестировщик | `tools.test_command` из конфига (произвольный shell модели недоступен) |

Пути за пределы репозитория (в т.ч. через symlink/junction) и в `.git` запрещены. За один ход —
не больше `tools.max_rounds` раундов «модель → инструменты», в последнем инструменты отключаются.
Вызовы видны в панели агента строками `» инструмент …` и пишутся в лог сессии. `/tools` — кто что может.
Выключить: `tools.enabled: false`, только чтение: `tools.write: false`.

**Менеджер видит всё** (`observe_all`): чужая переписка подмешивается ему как контекст, не тратя ход.

Переписка каждой сессии пишется в `~/.local/share/capricorn/sessions/*.jsonl`.

## Команды

| | |
|---|---|
| `текст` | менеджеру (настраивается `default_target`) |
| `@architect @reviewer текст` | адресно; `@coders`, `@all` |
| `/pause` `/resume` `/stop` | F5 — пауза, F8 — прервать всех |
| `/continue [N]`, `/budget N` | управление бюджетом ходов |
| `/complex on\|off` | третий кодер |
| `/model coder1 deepseek/deepseek-chat` | сменить модель на лету; `/models` — список |
| `/retry <агент>`, `/reset [агент]` | повтор после ошибки, очистка памяти |
| `/attach src/app.py` | приложить файл к следующему сообщению |
| `/tools` | инструменты агентов и команда тестов |
| `/branch <имя>` или ctrl+b | переключить ветку |
| `/zoom <агент>` или alt+1..9 | развернуть панель, esc — назад |

## Разработка

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e .[dev]   # Linux/macOS: .venv/bin/python
git config core.hooksPath .githooks             # pre-commit: версия + тесты; commit-msg: без соавторов
.venv/Scripts/python -m pytest -q
.venv/Scripts/python scripts/version.py bump    # каждый коммит поднимает версию (patch)
```

Правила работы — в [CLAUDE.md](CLAUDE.md), типовые сценарии — в [.claude/SKILLS.md](.claude/SKILLS.md).
Тесты не ходят в сеть: HTTP-провайдеры проверяются через `httpx.MockTransport`, агенты — через
скриптованный провайдер, интерфейс — через Textual Pilot.

## Дальше (v0.2+)

1. ~~Инструменты агентов~~ — готово (v0.1.5).
2. Отдельный git worktree на каждого кодера, чтобы параллельная запись не конфликтовала.
3. Учёт стоимости по прайсу моделей, лимит в $ на задачу.
4. Сохранение/загрузка сессии, экспорт спора в Markdown.
5. Голосование при споре (architect vs reviewer → решает manager или ты).
