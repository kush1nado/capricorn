import json

import httpx
import pytest

from capricorn.orchestrator import USER
from capricorn.providers import (AnthropicProvider, MockProvider, OpenAICompatProvider,
                                 ProviderError, build_providers)
from helpers import messages, settle, sse

MSGS = [{"role": "user", "content": "привет"}]


async def collect(provider, model="m", **kw):
    params = dict(model=model, system="SYS", messages=MSGS, temperature=0.3, max_tokens=100, agent="coder1")
    params.update(kw)
    text, usage = "", None
    async for chunk in provider.stream(**params):
        if isinstance(chunk, dict):
            usage = chunk["usage"]
        else:
            text += chunk
    return text, usage


# ------------------------------------------------------------ anthropic

async def test_anthropic_streams_text_and_usage(http_mock):
    body = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 12}}},
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "хм"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "При"}},
        "не json",
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "вет"}},
        {"type": "message_delta", "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    )
    reqs = http_mock(lambda r: httpx.Response(200, content=body))
    p = AnthropicProvider("anthropic", {"api_key": "k", "base_url": "https://proxy.local/"})
    assert await collect(p, model="claude-x") == ("Привет", {"in": 12, "out": 5})

    req, = reqs
    assert str(req.url) == "https://proxy.local/v1/messages"
    assert req.headers["x-api-key"] == "k" and req.headers["anthropic-version"] == "2023-06-01"
    sent = json.loads(req.content)
    assert sent == {"model": "claude-x", "system": "SYS", "messages": MSGS,
                    "max_tokens": 100, "temperature": 0.3, "stream": True}


async def test_anthropic_requires_key(http_mock):
    reqs = http_mock(lambda r: httpx.Response(200))
    with pytest.raises(ProviderError, match="api_key"):
        await collect(AnthropicProvider("anthropic", {"api_key": ""}))
    assert reqs == []


async def test_anthropic_http_error(http_mock):
    http_mock(lambda r: httpx.Response(401, text='{"error":"invalid x-api-key"}'))
    with pytest.raises(ProviderError, match="401.*invalid x-api-key"):
        await collect(AnthropicProvider("anthropic", {"api_key": "k"}))


async def test_anthropic_error_event(http_mock):
    http_mock(lambda r: httpx.Response(200, content=sse(
        {"type": "error", "error": {"type": "overloaded_error"}})))
    with pytest.raises(ProviderError, match="overloaded"):
        await collect(AnthropicProvider("anthropic", {"api_key": "k"}))


# --------------------------------------------------------------- openai

def oa_chunk(text=None, usage=None):
    ev = {"choices": [{"delta": {"content": text} if text is not None else {}}]}
    if usage:
        ev = {"choices": [], "usage": usage}
    return ev


async def test_openai_streams_and_sends_system_first(http_mock):
    body = sse(oa_chunk("Hel"), oa_chunk(), oa_chunk("lo"),
               oa_chunk(usage={"prompt_tokens": 7, "completion_tokens": 2}), "[DONE]", oa_chunk("after"))
    reqs = http_mock(lambda r: httpx.Response(200, content=body))
    p = OpenAICompatProvider("ds", {"base_url": "https://api.ds.local/v1/", "api_key": "sk",
                                    "headers": {"X-Title": "capricorn"}})
    assert await collect(p, model="deepseek-chat") == ("Hello", {"in": 7, "out": 2})

    req, = reqs
    assert str(req.url) == "https://api.ds.local/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk" and req.headers["x-title"] == "capricorn"
    sent = json.loads(req.content)
    assert sent["messages"] == [{"role": "system", "content": "SYS"}, *MSGS]
    assert (sent["temperature"], sent["max_tokens"], sent["stream"]) == (0.3, 100, True)
    assert sent["stream_options"] == {"include_usage": True}


async def test_openai_without_key_sends_placeholder(http_mock):
    reqs = http_mock(lambda r: httpx.Response(200, content=sse("[DONE]")))
    assert await collect(OpenAICompatProvider("ollama", {})) == ("", {"in": 0, "out": 0})
    assert reqs[0].headers["authorization"] == "Bearer none"
    assert str(reqs[0].url) == "https://api.openai.com/v1/chat/completions"


@pytest.mark.parametrize("model", ["o3-mini", "gpt-5-mini"])
async def test_openai_reasoning_models_use_completion_tokens(http_mock, model):
    reqs = http_mock(lambda r: httpx.Response(200, content=sse("[DONE]")))
    await collect(OpenAICompatProvider("openai", {"api_key": "k"}), model=model)
    sent = json.loads(reqs[0].content)
    assert "temperature" not in sent and "max_tokens" not in sent
    assert sent["max_completion_tokens"] == 100


async def test_openai_drops_rejected_params_and_retries(http_mock):
    def handler(req):
        sent = json.loads(req.content)
        if "temperature" in sent:
            return httpx.Response(400, text="Unsupported parameter: 'temperature'")
        if "max_tokens" in sent:
            return httpx.Response(400, text="'max_tokens' is not supported, use max_completion_tokens")
        return httpx.Response(200, content=sse(oa_chunk("ok"), "[DONE]"))

    reqs = http_mock(handler)
    assert (await collect(OpenAICompatProvider("x", {})))[0] == "ok"
    assert len(reqs) == 3
    final = json.loads(reqs[-1].content)
    assert final["max_completion_tokens"] == 100 and "temperature" not in final


async def test_openai_unrelated_400_is_not_retried(http_mock):
    reqs = http_mock(lambda r: httpx.Response(400, text="model not found"))
    with pytest.raises(ProviderError, match="400: model not found"):
        await collect(OpenAICompatProvider("x", {}))
    assert len(reqs) == 1


async def test_openai_server_error(http_mock):
    http_mock(lambda r: httpx.Response(503, text="overloaded"))
    with pytest.raises(ProviderError, match="503"):
        await collect(OpenAICompatProvider("x", {}))


# ----------------------------------------------------------- factory

def test_build_providers():
    ps = build_providers({"a": {"type": "anthropic"}, "o": {"base_url": "u"}, "m": {"type": "mock"}, "n": None})
    assert type(ps["a"]) is AnthropicProvider
    assert type(ps["o"]) is OpenAICompatProvider          # тип по умолчанию — openai
    assert type(ps["m"]) is MockProvider
    assert type(ps["n"]) is OpenAICompatProvider and ps["n"].conf == {}


def test_build_providers_unknown_type():
    with pytest.raises(ValueError, match="grpc"):
        build_providers({"x": {"type": "grpc"}})


# -------------------------------------------------------------- mock

async def test_mock_team_finishes_task(make_orch, fast_mock):
    orch, events = make_orch(provider=MockProvider("mock", {}))
    orch.user_says("сделай сервис заметок")
    await settle(orch, timeout=10)
    final = [m for m in messages(events) if USER in m.to]
    assert final and final[-1].sender == "manager" and "Готово" in final[-1].content
    assert orch.budget > 0
    assert all(a.status == "idle" for n, a in orch.agents.items() if n in orch.roster)


async def test_mock_answers_every_message_in_batch(fast_mock):
    p = MockProvider("mock", {})
    batch = ("[от coder2 → reviewer]: `api.py`\n\n```python\nx\n```\n\n"
             "[от coder1 → reviewer]: `storage.py`")
    text, _ = await collect(p, agent="reviewer", messages=[{"role": "user", "content": batch}])
    assert "@coder1" in text and "@coder2" in text


async def test_mock_manager_counts_batched_approvals(fast_mock):
    p = MockProvider("mock", {})
    p.seen["tested"] = 1
    batch = "[от reviewer → manager]: APPROVED — coder1\n\nAPPROVED — coder2\n\n@manager"
    text, _ = await collect(p, agent="manager", messages=[{"role": "user", "content": batch}])
    assert text.startswith("@user Готово")
