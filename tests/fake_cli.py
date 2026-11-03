"""Поддельный Codex/Claude CLI для тестов.

Пишет в FAKE_CLI_LOG строку JSON: argv, stdin, cwd, CLAUDECODE, TEMP/TMP/TMPDIR. Выдаёт события из FAKE_CLI_SCRIPT —
JSON-списка запусков: [{"events": [...], "exit": 0, "stderr": "", "sleep": 0, "pidfile": ""}, ...];
n-й запуск берёт n-й элемент (последний повторяется).
"""
import json
import os
import subprocess
import sys
import time

log_path = os.environ["FAKE_CLI_LOG"]
with open(os.environ["FAKE_CLI_SCRIPT"], encoding="utf-8") as f:
    runs = json.load(f)
n = 0
if os.path.exists(log_path):
    with open(log_path, encoding="utf-8") as f:
        n = sum(1 for _ in f)
stdin = sys.stdin.buffer.read().decode("utf-8")
with open(log_path, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd(),
                        "claudecode": os.environ.get("CLAUDECODE"),
                        "temp": {k: os.environ.get(k) for k in ("TEMP", "TMP", "TMPDIR", "PYTEST_ADDOPTS")}},
                       ensure_ascii=False) + "\n")

run = runs[min(n, len(runs) - 1)]
if run.get("pidfile"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(run["pidfile"], "w", encoding="utf-8") as f:
        f.write(f"{os.getpid()} {child.pid}")
for ev in run.get("events", []):
    sys.stdout.buffer.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
if run.get("sleep"):
    time.sleep(run["sleep"])
sys.stderr.buffer.write(run.get("stderr", "").encode("utf-8"))
sys.exit(run.get("exit", 0))
