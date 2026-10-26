"""Живой прогон команды без интерфейса: печатает ход работы и итог.

    python scripts/live_run.py --repo D:\\demo --config D:\\demo\\capricorn.yaml --budget 10 "@coder1 сделай …"

Нужен для проверки настоящих провайдеров (API или CLI): то, что тесты на подделках не покажут.
Останавливается, когда команда затихла, кончился бюджет или вышел --timeout (секунды).
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
from capricorn.orchestrator import Orchestrator  # noqa: E402


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


async def run(args) -> int:
    cfg = load_config(args.config)
    cfg.budget = args.budget
    started = time.monotonic()
    orch = Orchestrator(cfg, str(Path(args.repo).resolve()), make_printer(started))
    orch.start()
    orch.user_says(args.task)
    quiet = 0
    while time.monotonic() - started < args.timeout:
        await asyncio.sleep(1)
        busy = any(a.gen_task is not None or a.wake.is_set() for a in orch.agents.values())
        quiet = 0 if busy else quiet + 1
        if quiet >= 3:
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
    for name, a in orch.agents.items():
        if a.turns or a.status == "error":
            print(f"  {name:9} ходов {a.turns:2}  токены {a.tokens_in}↑ {a.tokens_out}↓  статус {a.status}")
    repo = Path(args.repo)
    for cmd in (["git", "log", "--oneline", "--graph", "-8", "--all"], ["git", "status", "--short"]):
        print(f"\n$ {' '.join(cmd)}")
        print(subprocess.run(cmd, cwd=repo, capture_output=True, text=True, encoding="utf-8").stdout)
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
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
