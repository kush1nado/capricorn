from __future__ import annotations

import asyncio
import os
import types
from pathlib import Path

import httpx
import pytest

from capricorn import config as config_mod
from capricorn import providers
from helpers import build_orch


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Никаких записей в настоящий $HOME, чужих capricorn.yaml и GIT_* из окружения.

    Из pre-commit-хука git передаёт GIT_INDEX_FILE, GIT_AUTHOR_* и пр. — без очистки
    git-команды тестов могли бы трогать индекс настоящего репозитория.
    """
    for var in [v for v in os.environ if v.startswith("GIT_")]:
        monkeypatch.delenv(var)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(config_mod, "CANDIDATES", [
        Path("capricorn.yaml"),
        Path(".capricorn/config.yaml"),
        home / ".config" / "capricorn" / "config.yaml",
    ])
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return home


@pytest.fixture
async def make_orch(tmp_path):
    """make_orch(replies, **cfg) -> (orch, events); задачи агентов запущены и гасятся после теста."""
    created = []

    def factory(replies=None, **kw):
        orch, events = build_orch(tmp_path, replies, **kw)
        orch.start()
        created.append(orch)
        return orch, events

    yield factory
    for orch in created:
        for t in orch._tasks:
            t.cancel()
        await asyncio.gather(*orch._tasks, return_exceptions=True)


@pytest.fixture
def fast_mock(monkeypatch):
    """MockProvider без пауз между чанками."""
    real_sleep = asyncio.sleep

    async def no_wait(_delay=0):
        await real_sleep(0)

    monkeypatch.setattr(providers, "asyncio", types.SimpleNamespace(sleep=no_wait))


@pytest.fixture
def http_mock(monkeypatch):
    """http_mock(handler) — провайдеры ходят в MockTransport; возвращает список запросов."""
    def install(handler):
        seen: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        real = httpx.AsyncClient
        monkeypatch.setattr(providers.httpx, "AsyncClient",
                            lambda *a, **kw: real(*a, transport=httpx.MockTransport(wrapped), **kw))
        return seen

    return install
