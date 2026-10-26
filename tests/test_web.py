"""Браузерный и десктопный режимы."""
import asyncio
import os
import subprocess
import sys
import time

import aiohttp
import pytest

from capricorn import __main__ as cli
from capricorn import web

PY = sys.executable


# --------------------------------------------------------------- команды

def test_app_argv_is_absolute_and_drops_mode_flags(tmp_path):
    args = web.app_argv("rel/repo", "conf.yaml", mock=True, complex_mode=True)
    assert args == ["--repo", os.path.abspath("rel/repo"), "--config", os.path.abspath("conf.yaml"),
                    "--mock", "--complex"]
    assert web.app_argv(str(tmp_path), None, False, False) == ["--repo", str(tmp_path)]


def test_self_command_dev_and_frozen(monkeypatch, tmp_path):
    assert web.self_command(["--mock"]) == [PY, "-m", "capricorn", "--mock"]
    exe = tmp_path / ("Capricorn.exe" if os.name == "nt" else "Capricorn")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert web.self_command(["--mock"]) == [str(exe), "--mock"]          # cli рядом нет
    cli_exe = tmp_path / web.CLI_EXE
    cli_exe.write_bytes(b"")
    assert web.self_command(["--mock"]) == [str(cli_exe), "--mock"]


def test_child_command_quotes_spaces():
    cmd = web.shell_join(["C:/Program Files/py.exe", "--repo", "my repo", "--mock"])
    assert ('"C:/Program Files/py.exe"' if os.name == "nt" else "'C:/Program Files/py.exe'") in cmd
    assert ('"my repo"' if os.name == "nt" else "'my repo'") in cmd


def test_free_port_and_wait_for_port():
    port = web.free_port()
    assert 1024 < port < 65536
    assert web.wait_for_port(port, timeout=0.3) is False


def test_wait_for_port_stops_when_process_dies():
    proc = subprocess.Popen([PY, "-c", "pass"])
    proc.wait()
    t = time.monotonic()
    assert web.wait_for_port(web.free_port(), timeout=10, proc=proc) is False
    assert time.monotonic() - t < 2


def test_kill_tree_kills_grandchildren(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    code = ("import subprocess, sys, time;"
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
            f"open(r'{pidfile}', 'w').write(str(p.pid)); time.sleep(60)")
    kw = {"start_new_session": True} if os.name != "nt" else {}
    proc = subprocess.Popen([PY, "-c", code], **kw)
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.05)
    grandchild = int(pidfile.read_text())
    web.kill_tree(proc)
    assert proc.poll() is not None
    time.sleep(0.3)
    assert not _alive(grandchild)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:                                   # зомби у init тоже считаем мёртвым
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return True


# ------------------------------------------------------------------- CLI

def test_cli_dispatches_web(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(web, "serve_web", lambda cmd, port, open_browser: seen.update(cmd=cmd, port=port, open=open_browser))
    assert cli.main(["--repo", str(tmp_path), "--mock", "--web", "--port", "8765", "--no-open"]) == 0
    assert seen["port"] == 8765 and seen["open"] is False
    assert "--web" not in seen["cmd"] and "--mock" in seen["cmd"] and str(tmp_path) in seen["cmd"]


def test_cli_dispatches_desktop(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(web, "run_desktop", lambda args: seen.update(args=args))
    assert cli.main(["--repo", str(tmp_path), "--desktop", "--complex"]) == 0
    assert seen["args"] == ["--repo", str(tmp_path), "--complex"]


def test_cli_reports_missing_package(monkeypatch, capsys):
    def boom(args):
        raise web.ModeUnavailable("для --desktop нужен пакет pywebview")
    monkeypatch.setattr(web, "run_desktop", boom)
    assert cli.main(["--desktop"]) == 2
    assert "pywebview" in capsys.readouterr().err


def test_web_and_desktop_are_exclusive(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--web", "--desktop"])


def test_missing_webview_raises_mode_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", None)       # import webview -> ImportError
    with pytest.raises(web.ModeUnavailable, match="pywebview"):
        web.run_desktop(["--mock"])


# --------------------------------------------------------------- desktop

class FakeWebview:
    def __init__(self):
        self.window = None
        self.started = False

    def create_window(self, title, url, **kw):
        self.window = (title, url, kw)

    def start(self):
        self.started = True


def test_run_desktop_opens_window_and_kills_server(monkeypatch):
    spawned = {}

    class FakeProc:
        pid = 1
        def poll(self):
            return None

    monkeypatch.setattr(web, "spawn_server", lambda args, port: spawned.setdefault("proc", FakeProc()))
    monkeypatch.setattr(web, "wait_for_port", lambda port, proc=None: True)
    killed = []
    monkeypatch.setattr(web, "kill_tree", lambda p: killed.append(p))
    wv = FakeWebview()
    web.run_desktop(["--mock"], webview=wv, port=5555)
    title, url, kw = wv.window
    assert (title, url) == ("Capricorn", "http://127.0.0.1:5555") and wv.started
    assert kw["background_color"] == "#050506"
    assert killed == [spawned["proc"]]


def test_run_desktop_server_failure_still_cleans_up(monkeypatch):
    monkeypatch.setattr(web, "spawn_server", lambda args, port: object())
    monkeypatch.setattr(web, "wait_for_port", lambda port, proc=None: False)
    killed = []
    monkeypatch.setattr(web, "kill_tree", lambda p: killed.append(p))
    wv = FakeWebview()
    with pytest.raises(RuntimeError, match="не запустился"):
        web.run_desktop([], webview=wv, port=5556)
    assert wv.window is None and len(killed) == 1


# ------------------------------------------------------- сквозной: --web

async def test_web_server_serves_live_app(tmp_path, isolated_home, monkeypatch):
    """Настоящий сервер + настоящий процесс Capricorn с веб-драйвером: в websocket идёт интерфейс."""
    monkeypatch.setenv("HOME", str(isolated_home))          # дочерние процессы — тоже не в настоящий $HOME
    monkeypatch.setenv("USERPROFILE", str(isolated_home))
    port = web.free_port()
    server = web.spawn_server(web.app_argv(str(tmp_path), None, mock=True, complex_mode=False), port)
    try:
        assert await asyncio.to_thread(web.wait_for_port, port, proc=server), "сервер не поднялся"
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{port}/") as r:
                assert r.status == 200
                page = await r.text()
                assert 'data-capricorn-loader="1"' in page          # наша заставка, не стандартная
                assert "fonts/RobotoMono-VariableFont_wght.ttf" in page
            async with http.get(f"http://127.0.0.1:{port}/static/fonts/RobotoMono-VariableFont_wght.ttf") as r:
                assert r.status == 200                               # шрифт отдаётся локально
            received = ""
            async with http.ws_connect(f"http://127.0.0.1:{port}/ws?width=160&height=48") as ws:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 30
                while "CAPRICORN" not in received and loop.time() < deadline:
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=deadline - loop.time())
                    except asyncio.TimeoutError:
                        break
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        received += msg.data.decode("utf-8", errors="ignore")
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        received += msg.data
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            assert "CAPRICORN" in received, received[-500:]
    finally:
        await asyncio.to_thread(web.kill_tree, server)
    assert server.poll() is not None


# --------------------------------------------------------------- сборка

def _run_entry(name, monkeypatch, argv):
    import runpy
    from pathlib import Path
    import capricorn.__main__ as m
    seen = {}
    monkeypatch.setattr(m, "main", lambda args=None: seen.setdefault("args", args) and 0)
    monkeypatch.setattr(sys, "argv", ["prog", *argv])
    path = Path(__file__).resolve().parent.parent / "packaging" / name
    with pytest.raises(SystemExit):
        runpy.run_path(str(path), run_name="__main__")
    return seen.get("args")


def test_desktop_entry_defaults_to_desktop(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert _run_entry("entry_desktop.py", monkeypatch, ["--mock"]) == ["--mock", "--desktop"]
    assert _run_entry("entry_desktop.py", monkeypatch, ["--web"]) == ["--web"]


def test_desktop_entry_avoids_filesystem_root_and_app_bundle(monkeypatch, tmp_path, isolated_home):
    root = os.path.abspath(os.sep)
    monkeypatch.chdir(root)                    # так стартует .app на macOS по двойному клику
    assert _run_entry("entry_desktop.py", monkeypatch, []) == ["--repo", str(isolated_home), "--desktop"]
    bundle = tmp_path / "Capricorn.app" / "Contents" / "MacOS"
    bundle.mkdir(parents=True)
    monkeypatch.chdir(bundle)
    assert _run_entry("entry_desktop.py", monkeypatch, []) == ["--repo", str(isolated_home), "--desktop"]
    assert _run_entry("entry_desktop.py", monkeypatch, ["--repo", "x"]) == ["--repo", "x", "--desktop"]


def test_cli_entry_passes_args_through(monkeypatch):
    assert _run_entry("entry_cli.py", monkeypatch, ["--mock"]) is None    # main() читает sys.argv сам


def test_smoke_finds_cli_in_dist_and_app_bundle(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("smoke", Path(__file__).resolve().parent.parent / "scripts" / "smoke_build.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    dist = tmp_path / "Capricorn"
    dist.mkdir()
    with pytest.raises(SystemExit):
        smoke.find_cli(dist)
    mac = tmp_path / "Capricorn.app" / "Contents" / "MacOS"
    mac.mkdir(parents=True)
    (mac / web.CLI_EXE).write_bytes(b"")
    assert smoke.find_cli(dist) == mac / web.CLI_EXE
    (dist / web.CLI_EXE).write_bytes(b"")
    assert smoke.find_cli(dist) == dist / web.CLI_EXE


# --------------------------------------------------------------- заставка

def test_loader_template_keeps_textual_hooks_and_is_offline():
    html = (web.ASSETS / "app_index.html").read_text(encoding="utf-8")
    for hook in ('js/textual.js', 'class="textual-terminal"', 'data-session-websocket-url="{{ app_websocket_url }}"',
                 'data-font-size="{{ font_size }}"', 'id="start"', 'intro-dialog', 'closed-dialog',
                 '-first-byte', '-closed', 'function refresh()'):
        assert hook in html, hook
    import re
    external = re.findall(r'(?:href|src|url\()\s*=?\s*["\']?(https?://[^"\')\s]+)', html)
    assert external == [], f"заставка не должна ходить в интернет: {external}"
    assert "CAPRICORN" in html and "ИНИЦИАЛИЗАЦИЯ" in html


def test_serve_web_uses_our_template(monkeypatch):
    seen = {}

    class FakeServer:
        def __init__(self, command, **kw):
            seen.update(command=command, **kw)

        def serve(self):
            seen["served"] = True

    import textual_serve.server
    monkeypatch.setattr(textual_serve.server, "Server", FakeServer)
    web.serve_web("cmd", 1234, open_browser=False)
    assert seen["templates_path"] == str(web.ASSETS) and seen["served"]
    assert (web.ASSETS / "app_index.html").exists()


def test_loader_is_packaged():
    """Шаблон должен попадать и в wheel (package-data), и в сборку PyInstaller."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert "web_assets/*.html" in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "web_assets" in (root / "packaging" / "capricorn.spec").read_text(encoding="utf-8")
