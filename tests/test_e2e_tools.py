"""Сквозной сценарий: настоящий адаптер провайдера (HTTP подменён) + Toolbox + цикл агента."""
import json

import httpx
import pytest

from capricorn.providers import AnthropicProvider, OpenAICompatProvider
from helpers import messages, settle, sse


def anthropic_turns():
    """1-й запрос: модель читает файл; 2-й: правит; 3-й: отвечает текстом."""
    def tool_use(i, name, args):
        return sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)}},
            {"type": "message_delta", "usage": {"output_tokens": 7}})
    return [
        tool_use(1, "read_file", {"path": "app.py"}),
        tool_use(2, "edit_file", {"path": "app.py", "old": "return 1", "new": "return 2"}),
        sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Исправил. @reviewer"}}),
    ]


def openai_turns():
    def call(i, name, args):
        return sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": f"call_{i}", "function": {"name": name, "arguments": json.dumps(args)}}]}}]},
            "[DONE]")
    return [
        call(1, "read_file", {"path": "app.py"}),
        call(2, "edit_file", {"path": "app.py", "old": "return 1", "new": "return 2"}),
        sse({"choices": [{"delta": {"content": "Исправил. @reviewer"}}]}, "[DONE]"),
    ]


@pytest.mark.parametrize("kind", ["anthropic", "openai"])
async def test_agent_reads_and_edits_through_real_adapter(make_orch, http_mock, tmp_path, kind):
    (tmp_path / "app.py").write_bytes(b"def f():\n    return 1\n")
    turns = anthropic_turns() if kind == "anthropic" else openai_turns()
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        system = body.get("system") or body["messages"][0]["content"]
        if not system.startswith("# Ты: @coder1"):          # остальные агенты просто молчат
            done = (sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ок"}})
                    if kind == "anthropic" else sse({"choices": [{"delta": {"content": "ок"}}]}, "[DONE]"))
            return httpx.Response(200, content=done)
        bodies.append(body)
        return httpx.Response(200, content=turns[len(bodies) - 1])

    http_mock(handler)
    orch, events = make_orch()
    orch.providers["script"] = (AnthropicProvider("script", {"api_key": "k"}) if kind == "anthropic"
                                else OpenAICompatProvider("script", {}))
    orch.route("manager", "@coder1 почини f()")
    await settle(orch)

    assert (tmp_path / "app.py").read_bytes() == b"def f():\n    return 2\n"
    assert len(bodies) == 3
    if kind == "anthropic":
        second = bodies[1]["messages"]
        assert second[1]["content"][0] == {"type": "tool_use", "id": "toolu_1", "name": "read_file",
                                           "input": {"path": "app.py"}}
        result = second[2]["content"][0]
        assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_1"
        assert "return 1" in result["content"] and result["is_error"] is False
        assert {t["name"] for t in bodies[0]["tools"]} >= {"read_file", "edit_file", "run_tests"}
    else:
        second = bodies[1]["messages"]
        assert second[2]["tool_calls"][0]["function"]["name"] == "read_file"
        assert second[3]["role"] == "tool" and second[3]["tool_call_id"] == "call_1"
        assert "return 1" in second[3]["content"]
    msg, = [m for m in messages(events) if m.sender == "coder1"]
    assert msg.to == ["reviewer"] and msg.content == "Исправил. @reviewer"
    assert orch.agents["coder1"].tokens_in > 0 or kind == "openai"
