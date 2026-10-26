"""Проверка собранного приложения: capricorn-cli поднимает веб-сервер, а в websocket приходит живой интерфейс.

    python scripts/smoke_build.py [dist/Capricorn]

На macOS ищет capricorn-cli и внутри Capricorn.app. Код выхода 0 — сборка рабочая.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from capricorn import web  # noqa: E402


def find_cli(dist: Path) -> Path:
    for candidate in (dist / web.CLI_EXE, dist / "Capricorn.app" / "Contents" / "MacOS" / web.CLI_EXE,
                      dist.parent / "Capricorn.app" / "Contents" / "MacOS" / web.CLI_EXE):
        if candidate.exists():
            return candidate
    raise SystemExit(f"не найден {web.CLI_EXE} в {dist}")


async def check(cli: Path) -> bool:
    import aiohttp

    out = subprocess.run([str(cli), "--help"], capture_output=True, timeout=60)
    ok = out.returncode == 0 and b"--desktop" in out.stdout
    print("--help:", "ok" if ok else f"FAIL {out.returncode}")
    if not ok:                              # в CI иначе не видно причины
        print(out.stderr.decode("utf-8", errors="replace")[-2000:])
        return False

    repo = tempfile.mkdtemp(prefix="capricorn-smoke-")
    port = web.free_port()
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
    server = subprocess.Popen([str(cli), "--mock", "--repo", repo, "--web", "--no-open", "--port", str(port)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    try:
        if not await asyncio.to_thread(web.wait_for_port, port, proc=server, timeout=60):
            print("server: FAIL (не поднялся)")
            return False
        received = ""
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{port}/") as r:
                loader = r.status == 200 and 'data-capricorn-loader="1"' in await r.text()
            print("loader:", "ok" if loader else "FAIL (стандартная заставка textual-serve — шаблон не попал в сборку)")
            if not loader:
                return False
            async with http.ws_connect(f"http://127.0.0.1:{port}/ws?width=160&height=48") as ws:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 60
                while "CAPRICORN" not in received and loop.time() < deadline:
                    try:
                        msg = await asyncio.wait_for(ws.receive(), deadline - loop.time())
                    except asyncio.TimeoutError:
                        break
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        received += msg.data.decode("utf-8", "ignore")
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        received += msg.data
                    else:
                        break
        ok = "CAPRICORN" in received
        print("web UI:", "ok" if ok else f"FAIL, получено {len(received)} символов: {received[-300:]!r}")
        return ok
    finally:
        await asyncio.to_thread(web.kill_tree, server)


def main(argv: list[str]) -> int:
    dist = Path(argv[0]) if argv else ROOT / "dist" / "Capricorn"
    return 0 if asyncio.run(check(find_cli(dist))) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
