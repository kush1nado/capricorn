"""Живой прогон команды без интерфейса: печатает ход работы и итог.

    python scripts/live_run.py --repo D:\\demo --config D:\\demo\\capricorn.yaml --budget 10 "@coder1 сделай …"

Нужен для проверки настоящих провайдеров (API или CLI): то, что тесты на подделках не покажут.
Останавливается, когда команда затихла, кончился бюджет или вышел --timeout (секунды).

--answers ФАЙЛ — ответы пользователя во время прогона: каждая дописанная в файл строка уходит команде,
как если бы её набрали в интерфейсе («А», «@coder1 продолжай»). «/continue N» добавляет ходов.
Пока ждут ответа (вопрос @user или кончился бюджет), прогон не завершается ещё --wait секунд.
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from capricorn.config import load_config  # noqa: E402
from capricorn.orchestrator import USER, Orchestrator  # noqa: E402
from capricorn.usage import usage_report  # noqa: E402


def make_printer(started: float):
    def emit(kind: str, **d):
        t = f"{time.monotonic() - started:6.1f}s"
        if kind == "message":
            m = d["msg"]
            body = m.content.strip().replace("\n", "\n" + " " * 10)
            print(f"{t} [{m.sender} → {', '.join(m.to) or '—'}]\n          {body}\n", flush=True)
        elif kind == "stream_chunk" and d["chunk"].lstrip().startswith("»"):
            print(f"{t}   {d['agent']}: {d['chunk'].strip()}", flush=True)
        elif kind == "system":
            print(f"{t} !! {d['text']}", flush=True)
    return emit


def new_answers(path: Path, pos: int) -> tuple[list[str], int]:
    """Строки, дописанные в файл после позиции pos (в байтах). Недописанную последнюю строку не берём."""
    try:
        data = path.read_bytes()[pos:]
    except FileNotFoundError:
        return [], pos
    end = data.rfind(b"\n") + 1
    lines = [s.strip() for s in data[:end].decode("utf-8", errors="replace").splitlines()]
    return [s for s in lines if s], pos + end


def waiting_for_user(messages) -> bool:
    """Последнее обращение к пользователю — вопрос (есть «?»), и он ещё не ответил.
    Итоговый отчёт («задача выполнена») — не вопрос: ждать ответа на него незачем."""
    for m in reversed(messages):
        if m.sender == USER:
            return False
        if USER in m.to:
            return "?" in m.content
    return False


def answer(orch: Orchestrator, line: str):
    if line.startswith("/continue"):
        parts = line.split()
        orch.add_budget(int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 15)
    else:
        orch.user_says(line)


async def run(args) -> int:
    cfg = load_config(args.config)
    cfg.budget = args.budget
    started = time.monotonic()
    orch = Orchestrator(cfg, str(Path(args.repo).resolve()), make_printer(started))
    orch.start()
    orch.user_says(args.task)
    answers = Path(args.answers) if args.answers else None
    pos = answers.stat().st_size if answers and answers.exists() else 0    # старые строки — не ответы
    quiet = idle_since = 0
    while time.monotonic() - started < args.timeout:
        await asyncio.sleep(1)
        if answers:
            lines, pos = new_answers(answers, pos)
            for line in lines:
                print(f"{time.monotonic() - started:6.1f}s >> ответ пользователя: {line}", flush=True)
                answer(orch, line)
        busy = any(a.gen_task is not None or a.wake.is_set() for a in orch.agents.values())
        quiet = 0 if busy else quiet + 1
        if quiet < 3:
            continue
        stuck = waiting_for_user(orch.messages) or orch.budget <= 0
        if not (answers and stuck):
            break
        if quiet == 3:
            idle_since = time.monotonic()
            why = "вопрос пользователю" if waiting_for_user(orch.messages) else "кончился бюджет (/continue N)"
            print(f"{idle_since - started:6.1f}s ?? ждёт ответа: {why} — допиши строку в {answers}", flush=True)
        elif time.monotonic() - idle_since > args.wait:
            print(f"\n!! ответа нет {args.wait}s — завершаю")
            break
    else:
        print(f"\n!! таймаут {args.timeout}s — останавливаю команду")
        orch.stop_all()
        await asyncio.sleep(2)
    for t in orch._tasks:
        t.cancel()
    await asyncio.gather(*orch._tasks, return_exceptions=True)

    print("\n================ итог ================")
    print(f"время: {time.monotonic() - started:.0f}s, ходов осталось: {orch.budget}")
    print(usage_report(orch))
    errors = [name for name, a in orch.agents.items() if a.status == "error"]
    if errors:
        print(f"с ошибкой: {', '.join(errors)}")
    repo = Path(args.repo)
    for cmd in (["git", "log", "--oneline", "--graph", "-8", "--all"], ["git", "status", "--short"]):
        print(f"\n$ {' '.join(cmd)}")
        print(subprocess.run(cmd, cwd=repo, capture_output=True, text=True, encoding="utf-8").stdout)
    branches = await orch.toolbox.worktrees.status(orch.roster)
    if branches:
        print("ветки агентов:\n" + "\n".join(branches))
    print(f"лог сессии: {orch.log_path}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # в файл/пайп на Windows иначе cp1251
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--config")
    ap.add_argument("--budget", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--answers", help="файл, строки из которого уходят команде как ответы пользователя")
    ap.add_argument("--wait", type=int, default=600, help="сколько ждать ответа, когда команда остановилась")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
