"""Браузерный и десктопный режимы.

--web      textual-serve поднимает локальный сервер; на каждую вкладку браузера он запускает
           отдельный процесс Capricorn с веб-драйвером Textual (команда — child_command()).
--desktop  тот же сервер в фоновом процессе + окно pywebview. Окно закрыли — сервер и все
           его процессы гасятся.
"""
from __future__ import annotations

import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
TITLE = "Capricorn"
ASSETS = Path(__file__).resolve().parent / "web_assets"     # шаблон заставки для --web / --desktop


class ModeUnavailable(RuntimeError):
    """Режим нельзя запустить: не установлен нужный пакет."""


def app_argv(repo: str, config: str | None, mock: bool, complex_mode: bool, resume: bool = False) -> list[str]:
    """Аргументы терминального режима для дочернего процесса (без --web/--desktop)."""
    args = ["--repo", os.path.abspath(repo)]
    if config:
        args += ["--config", os.path.abspath(config)]
    if mock:
        args.append("--mock")
    if complex_mode:
        args.append("--complex")
    if resume:
        args.append("--resume")
    return args


CLI_EXE = "capricorn-cli.exe" if os.name == "nt" else "capricorn-cli"


def self_command(args: list[str]) -> list[str]:
    """Как запустить Capricorn ещё раз: из сборки PyInstaller или через python -m.

    В сборке оконный Capricorn(.exe) без консоли, а процессу интерфейса нужен stdout —
    поэтому дочерние процессы идут через консольный capricorn-cli из той же папки."""
    if getattr(sys, "frozen", False):
        cli = os.path.join(os.path.dirname(sys.executable), CLI_EXE)
        return [cli if os.path.exists(cli) else sys.executable, *args]
    return [sys.executable, "-m", "capricorn", *args]


def shell_join(argv: list[str]) -> str:
    """textual-serve запускает команду через shell: cmd.exe на Windows, sh на Unix."""
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def child_command(args: list[str]) -> str:
    return shell_join(self_command(args))


def free_port(host: str = HOST) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def wait_for_port(port: int, host: str = HOST, timeout: float = 20.0, proc: subprocess.Popen | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False                     # сервер умер, не дождавшись
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def kill_tree(proc: subprocess.Popen) -> None:
    """Сервер textual-serve плодит процессы приложения — гасим всё дерево."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        proc.kill()
        proc.wait(timeout=5)


# ------------------------------------------------------------------ web

def serve_web(command: str, port: int, open_browser: bool = True, host: str = HOST) -> None:
    try:
        from textual_serve.server import Server
    except ImportError:
        raise ModeUnavailable("для --web нужен пакет textual-serve: pip install 'capricorn[web]'") from None
    url = f"http://{host}:{port}"
    if open_browser:
        def opener():
            if wait_for_port(port, host):
                webbrowser.open(url)
        threading.Thread(target=opener, daemon=True).start()
    # своя заставка (app_index.html) вместо стандартной textual-serve; static (textual.js, шрифты) — её
    Server(command, host=host, port=port, title=TITLE, templates_path=str(ASSETS)).serve()


# -------------------------------------------------------------- desktop

def spawn_server(args: list[str], port: int) -> subprocess.Popen:
    """Сервер — отдельным процессом: pywebview обязан жить в главном потоке (macOS),
    а aiohttp-серверу нужен свой event loop и обработчики сигналов."""
    argv = self_command([*args, "--web", "--no-open", "--port", str(port)])
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True   # своя группа процессов — для kill_tree
    return subprocess.Popen(argv, **kw)


def run_desktop(args: list[str], webview=None, port: int | None = None) -> None:
    if webview is None:
        try:
            import webview  # type: ignore[import-not-found,no-redef]
        except ImportError:
            raise ModeUnavailable("для --desktop нужен пакет pywebview: pip install 'capricorn[desktop]'") from None
    port = port or free_port()
    server = spawn_server(args, port)
    try:
        if not wait_for_port(port, proc=server):
            raise RuntimeError("веб-сервер Capricorn не запустился")
        webview.create_window(TITLE, f"http://{HOST}:{port}", width=1480, height=920,
                              min_size=(900, 600), background_color="#050506")
        webview.start()
    finally:
        kill_tree(server)
