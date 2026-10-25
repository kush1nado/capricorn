"""Кроссмодельный слой. Два настоящих адаптера покрывают почти всё:

* anthropic — нативный Messages API (Claude)
* openai    — любой OpenAI-совместимый endpoint: OpenAI, DeepSeek, Qwen/DashScope,
              MiniMax, OpenRouter, Groq, Together, Ollama, LM Studio, vLLM, llama.cpp
* mock      — офлайн-заглушка, разыгрывает диалог команды без сети

Каждый stream() — асинхронный генератор: отдаёт str-чанки текста, затем (если модель
вызвала инструменты) {"tool_calls": [{"id", "name", "args"}]} и в конце {"usage": {"in", "out"}}.

История агента хранится в нейтральном формате и переводится под каждый API:
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "args": dict}]}  (tool_calls — необязательно)
  {"role": "tool", "results": [{"id", "name", "content": str, "is_error": bool}]}
Инструменты: [{"name", "description", "parameters": JSON Schema}].
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from typing import AsyncIterator

import httpx

Chunk = str | dict


class ProviderError(RuntimeError):
    pass


class Provider:
    def __init__(self, name: str, conf: dict):
        self.name = name
        self.conf = conf

    async def stream(self, *, model: str, system: str, messages: list[dict],
                     temperature: float, max_tokens: int, agent: str,
                     tools: list[dict] | None = None, tool_choice: str = "auto") -> AsyncIterator[Chunk]:
        raise NotImplementedError
        yield  # pragma: no cover


async def _sse_lines(resp: httpx.Response) -> AsyncIterator[str]:
    async for line in resp.aiter_lines():
        if line.startswith("data:"):
            yield line[5:].strip()


def _parse_args(raw: str) -> dict | str:
    """JSON-аргументы инструмента; при ошибке — исходная строка (Toolbox вернёт модели ошибку)."""
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


# ------------------------------------------------------------ anthropic ----

def to_anthropic_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            text = m.get("content") or ""
            if not calls:
                # Anthropic не принимает пустой текст ассистента
                out.append({"role": "assistant", "content": text if text.strip() else "…"})
                continue
            blocks = [{"type": "text", "text": text}] if text.strip() else []
            blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"],
                        "input": c["args"] if isinstance(c["args"], dict) else {}} for c in calls]
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                 "is_error": bool(r.get("is_error"))} for r in m["results"]]})
    return out


class AnthropicProvider(Provider):
    async def stream(self, *, model, system, messages, temperature, max_tokens, agent,
                     tools=None, tool_choice="auto"):
        key = self.conf.get("api_key") or ""
        if not key:
            raise ProviderError(f"{self.name}: нет api_key")
        url = (self.conf.get("base_url") or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        body = {"model": model, "system": system, "messages": to_anthropic_messages(messages),
                "max_tokens": max_tokens, "temperature": temperature, "stream": True}
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"],
                              "input_schema": t["parameters"]} for t in tools]
            body["tool_choice"] = {"type": tool_choice}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        usage = {"in": 0, "out": 0}
        blocks: dict[int, dict] = {}            # index → накапливаемый tool_use
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20)) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    raise ProviderError(f"{resp.status_code}: {(await resp.aread()).decode(errors='replace')[:400]}")
                async for data in _sse_lines(resp):
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    t = ev.get("type")
                    if t == "content_block_delta":
                        d = ev["delta"]
                        if d.get("type") == "text_delta":
                            yield d["text"]
                        elif d.get("type") == "input_json_delta" and ev.get("index") in blocks:
                            blocks[ev["index"]]["json"] += d.get("partial_json", "")
                    elif t == "content_block_start" and ev.get("content_block", {}).get("type") == "tool_use":
                        cb = ev["content_block"]
                        blocks[ev.get("index", len(blocks))] = {"id": cb["id"], "name": cb["name"], "json": ""}
                    elif t == "message_start":
                        usage["in"] = ev["message"].get("usage", {}).get("input_tokens", 0)
                    elif t == "message_delta":
                        usage["out"] = ev.get("usage", {}).get("output_tokens", usage["out"])
                    elif t == "error":
                        raise ProviderError(str(ev.get("error")))
        if blocks:
            yield {"tool_calls": [{"id": b["id"], "name": b["name"], "args": _parse_args(b["json"])}
                                  for _, b in sorted(blocks.items())]}
        yield {"usage": usage}


# --------------------------------------------------------------- openai ----

def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            if not calls:
                out.append({"role": "assistant", "content": m.get("content") or ""})
                continue
            out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"],
                              "arguments": json.dumps(c["args"], ensure_ascii=False)
                              if isinstance(c["args"], dict) else str(c["args"])}}
                for c in calls]})
        elif role == "tool":
            out += [{"role": "tool", "tool_call_id": r["id"], "content": r["content"]} for r in m["results"]]
    return out


class OpenAICompatProvider(Provider):
    """Сам подстраивается под капризы провайдеров: если endpoint ругается на
    temperature / max_tokens / stream_options / tools — повторяет запрос без них
    (без инструментов агент просто работает текстом)."""

    async def stream(self, *, model, system, messages, temperature, max_tokens, agent,
                     tools=None, tool_choice="auto"):
        base = (self.conf.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        headers = {"Authorization": f"Bearer {self.conf.get('api_key') or 'none'}",
                   "Content-Type": "application/json"}
        headers.update(self.conf.get("headers") or {})
        params: dict = {"temperature": temperature, "max_tokens": max_tokens,
                        "stream_options": {"include_usage": True}}
        # Reasoning-модели OpenAI не принимают temperature и max_tokens
        if re.match(r"^(o\d|gpt-5)", model):
            params.pop("temperature")
            params["max_completion_tokens"] = params.pop("max_tokens")
        if tools:
            params["tools"] = [{"type": "function", "function": t} for t in tools]
            params["tool_choice"] = tool_choice
        wire = to_openai_messages(system, messages)

        for _attempt in range(6):
            body = {"model": model, "stream": True, "messages": wire, **params}
            usage = {"in": 0, "out": 0}
            calls: dict[int, dict] = {}
            async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20)) as client:
                async with client.stream("POST", base + "/chat/completions",
                                         json=body, headers=headers) as resp:
                    if resp.status_code == 400:
                        err = (await resp.aread()).decode(errors="replace")
                        fixed = False
                        for p in ("stream_options", "temperature"):
                            if p in err and p in params:
                                params.pop(p); fixed = True
                        if "max_tokens" in err and "max_tokens" in params:
                            params["max_completion_tokens"] = params.pop("max_tokens"); fixed = True
                        if "tool" in err.lower() and "tools" in params:
                            params.pop("tools"); params.pop("tool_choice", None); fixed = True
                        if fixed:
                            continue
                        raise ProviderError(f"400: {err[:400]}")
                    if resp.status_code >= 400:
                        raise ProviderError(f"{resp.status_code}: {(await resp.aread()).decode(errors='replace')[:400]}")
                    async for data in _sse_lines(resp):
                        if data == "[DONE]":
                            break
                        try:
                            ev = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if ev.get("usage"):
                            usage = {"in": ev["usage"].get("prompt_tokens", 0),
                                     "out": ev["usage"].get("completion_tokens", 0)}
                        for ch in ev.get("choices") or []:
                            delta = ch.get("delta") or {}
                            if delta.get("content"):
                                yield delta["content"]
                            for i, tc in enumerate(delta.get("tool_calls") or []):
                                slot = calls.setdefault(tc.get("index", i), {"id": "", "name": "", "args": ""})
                                slot["id"] = tc.get("id") or slot["id"]
                                fn = tc.get("function") or {}
                                slot["name"] = fn.get("name") or slot["name"]
                                slot["args"] += fn.get("arguments") or ""
            if calls:
                yield {"tool_calls": [{"id": c["id"] or f"call_{i}", "name": c["name"],
                                       "args": _parse_args(c["args"])}
                                      for i, c in sorted(calls.items())]}
            yield {"usage": usage}
            return
        raise ProviderError("не удалось подобрать параметры запроса")


# ---------------------------------------------------------------- mock ----

_FROM_RE = re.compile(r"^\[от (\w+)", re.M)
_SPLIT_RE = re.compile(r"^(?=\[от \w+)", re.M)


class MockProvider(Provider):
    """Разыгрывает правдоподобный спор команды, чтобы проверить UI без ключей.
    Инструменты не вызывает: офлайн-демо не должно трогать файлы репозитория."""

    def __init__(self, name, conf):
        super().__init__(name, conf)
        self.seen: dict[str, int] = {}

    def _bump(self, key: str) -> int:
        self.seen[key] = self.seen.get(key, 0) + 1
        return self.seen[key]

    def _reply(self, agent: str, last: str) -> str:
        senders = _FROM_RE.findall(last)
        frm = senders[-1] if senders else "user"
        role = re.sub(r"\d", "", agent)

        if role == "manager":
            if frm == "user":
                self.seen.clear()
                return ("Принял задачу. Сначала нужна архитектура, потом раздам кодерам.\n\n"
                        "@architect набросай структуру модулей и интерфейсы.")
            if frm == "architect":
                return ("План утверждён. Распределение:\n"
                        "- coder1 — слой хранения (`storage.py`)\n"
                        "- coder2 — HTTP-обработчики (`api.py`)\n"
                        "- tester — тесты на граничные случаи\n\n"
                        "@coder1 @coder2 @tester за работу.")
            if "APPROVED" in last:
                self.seen["approved"] = self.seen.get("approved", 0) + last.count("APPROVED")
                n = self.seen["approved"]
                if n >= 2 and self.seen.get("tested"):
                    return "@user Готово: код прошёл ревью, тесты написаны. Можно мержить."
                return f"Фиксирую: одобрено ревью ({n}/2)."
            if frm == "tester":
                self._bump("tested")
                if self.seen.get("approved", 0) >= 2:
                    return "@user Готово: код прошёл ревью, тесты написаны. Можно мержить."
                return "Тесты принял, жду завершения ревью."
            return "Принято."

        if role == "architect":
            if frm == "reviewer":
                return ("Справедливо насчёт кэша — убираю его из v1. С репозиторием-интерфейсом "
                        "не согласен: без него не протестировать хранилище без БД. Оставляю.\n\n"
                        "@manager архитектура финальная, можно раздавать.")
            return ("Предлагаю три слоя:\n\n```\napi.py      → обработчики, валидация\n"
                    "service.py  → бизнес-логика\nstorage.py  → Repository + SQLite-реализация\n```\n"
                    "Плюс LRU-кэш поверх storage.\n\n@reviewer оцени, пока не начали писать код.")

        if role == "reviewer":
            if frm == "architect":
                return ("Две претензии:\n1. [major] LRU-кэш в v1 — преждевременная оптимизация, "
                        "плюс инвалидация при записи не продумана.\n2. [minor] Repository-интерфейс "
                        "ради одной реализации — лишняя абстракция?\n\n@architect обоснуй или упрости.")
            if frm.startswith("coder"):
                if self._bump("rev_" + frm) == 1:
                    return (f"1. [major] нет обработки пустого ввода — упадёт с KeyError.\n"
                            f"2. [nit] имя `data` ничего не говорит.\n\n@{frm} исправь п.1.")
                return f"APPROVED — {frm} закрыл замечания.\n\n@manager"
            return "Ок."

        if role == "coder":
            fname = "storage.py" if agent == "coder1" else "api.py"
            if frm == "reviewer":
                return (f"Исправил:\n```python\ndef handle(payload: dict) -> dict:\n"
                        f"    if not payload:\n        raise ValueError(\"empty payload\")\n"
                        f"    record = payload[\"record\"]\n    ...\n```\n@reviewer глянь ещё раз.")
            return (f"`{fname}`:\n```python\ndef handle(data):\n    record = data[\"record\"]\n"
                    f"    return save(record)\n```\n@reviewer на ревью.")

        if role == "tester":
            return ("Набросал тесты:\n```python\ndef test_empty_payload():\n"
                    "    with pytest.raises(ValueError):\n        handle({})\n\n"
                    "def test_unicode_keys():\n    assert handle({\"record\": {\"ключ\": 1}})\n```\n"
                    "Нашёл кейс: конкурентная запись в SQLite без блокировки.\n\n@manager тесты готовы.")
        return "..."

    def _replies(self, agent: str, last: str) -> str:
        """Пачка входящих = один ход: отвечаем на каждое сообщение, а не только на последнее."""
        batch = last.split("\n\n---\n\n")[-1]          # без подслушанного контекста
        parts = [p for p in _SPLIT_RE.split(batch) if p.strip()] or [last]
        out: list[str] = []
        for part in parts:
            reply = self._reply(agent, part)
            if reply not in out:
                out.append(reply)
        return "\n\n".join(out)

    async def stream(self, *, model, system, messages, temperature, max_tokens, agent,
                     tools=None, tool_choice="auto"):
        last = (messages[-1].get("content") or "") if messages else ""
        text = self._replies(agent, last)
        await asyncio.sleep(random.uniform(0.3, 1.0))
        for i in range(0, len(text), 4):
            yield text[i:i + 4]
            await asyncio.sleep(0.012)
        yield {"usage": {"in": len(system + last) // 4, "out": len(text) // 4}}


def build_providers(conf: dict) -> dict[str, Provider]:
    kinds = {"anthropic": AnthropicProvider, "openai": OpenAICompatProvider, "mock": MockProvider}
    out = {}
    for name, pc in conf.items():
        cls = kinds.get((pc or {}).get("type", "openai"))
        if cls is None:
            raise ValueError(f"Неизвестный тип провайдера {name}: {pc.get('type')}")
        out[name] = cls(name, pc or {})
    return out
