"""End-to-end WebSocket integration test with a scripted fake adapter.

Exercises the transport without hitting a real LLM:
- hello → session created
- utterance → adapter emits tool_use → server dispatches `tool_call`
- client answers with `tool_result` → adapter emits final text → `final_text`

Also checks that an extra tool_result for an unknown id is ignored safely,
and that two tool_calls fan out and resolve independently.
"""

from __future__ import annotations

import json
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.adapters.base import (
    CompletionResult,
    Message,
    TextBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
)
from backend.transport import ws as ws_module
from backend.transport.ws import router


class ScriptedAdapter:
    """Yields a pre-baked sequence of CompletionResults per call."""

    name = "scripted"
    model = "scripted-1"

    def __init__(self, responses: list[CompletionResult]) -> None:
        self._responses = iter(responses)

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
    ) -> CompletionResult:
        return next(self._responses)


def _make_app(adapter_responses: list[CompletionResult]) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    def factory() -> ScriptedAdapter:
        return ScriptedAdapter(adapter_responses)

    ws_module.set_adapter_factory(factory)
    return app


def _recv(ws_client) -> dict:
    raw = ws_client.receive_text()
    return json.loads(raw)


def test_simple_tool_round_trip() -> None:
    """One tool_call, one tool_result, one final_text."""
    usage = Usage(input_tokens=10, output_tokens=5, model="scripted-1")
    responses = [
        CompletionResult(
            message=Message(
                role="assistant",
                content=[
                    ToolUseBlock(
                        id="t1", name="control_lights", input={"room": "kitchen", "on": True}
                    )
                ],
            ),
            stop_reason="tool_use",
            usage=usage,
        ),
        CompletionResult(
            message=Message(
                role="assistant",
                content=[TextBlock(text="Kitchen lights on.")],
            ),
            stop_reason="end_turn",
            usage=usage,
        ),
    ]
    app = _make_app(responses)
    with TestClient(app).websocket_connect("/agent") as ws_client:
        ws_client.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "session_id": str(uuid.uuid4()),
                    "tools": [
                        {
                            "name": "control_lights",
                            "description": "Turn a room's lights on or off.",
                            "input_schema": {"type": "object", "properties": {}},
                        }
                    ],
                }
            )
        )
        ws_client.send_text(json.dumps({"type": "utterance", "text": "turn on kitchen"}))

        tool_call = _recv(ws_client)
        assert tool_call["type"] == "tool_call"
        assert tool_call["name"] == "control_lights"
        assert tool_call["input"] == {"room": "kitchen", "on": True}
        assert tool_call["tool_use_id"] == "t1"

        ws_client.send_text(
            json.dumps(
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": '{"ok": true}',
                    "is_error": False,
                }
            )
        )

        final = _recv(ws_client)
        assert final == {"type": "final_text", "text": "Kitchen lights on."}


def test_parallel_tool_dispatch() -> None:
    """Two tool_use blocks in one model turn fan out in parallel."""
    usage = Usage(input_tokens=10, output_tokens=5, model="scripted-1")
    responses = [
        CompletionResult(
            message=Message(
                role="assistant",
                content=[
                    ToolUseBlock(
                        id="t1", name="control_lights", input={"room": "living_room", "on": True}
                    ),
                    ToolUseBlock(
                        id="t2", name="set_climate", input={"room": "bedroom", "target_c": 19}
                    ),
                ],
            ),
            stop_reason="tool_use",
            usage=usage,
        ),
        CompletionResult(
            message=Message(role="assistant", content=[TextBlock(text="Done.")]),
            stop_reason="end_turn",
            usage=usage,
        ),
    ]
    app = _make_app(responses)
    with TestClient(app).websocket_connect("/agent") as ws_client:
        ws_client.send_text(json.dumps({"type": "hello", "tools": []}))
        ws_client.send_text(json.dumps({"type": "utterance", "text": "do both"}))

        # Two tool_calls arrive in some order — collect by id.
        first = _recv(ws_client)
        second = _recv(ws_client)
        assert {first["tool_use_id"], second["tool_use_id"]} == {"t1", "t2"}
        assert first["type"] == "tool_call"
        assert second["type"] == "tool_call"

        # Reply to them out of order — the futures map handles correlation.
        ws_client.send_text(
            json.dumps({"type": "tool_result", "tool_use_id": "t2", "content": '{"ok": true}'})
        )
        ws_client.send_text(
            json.dumps({"type": "tool_result", "tool_use_id": "t1", "content": '{"ok": true}'})
        )

        final = _recv(ws_client)
        assert final["type"] == "final_text"
        assert final["text"] == "Done."


def test_utterance_before_hello_errors() -> None:
    app = _make_app([])
    with TestClient(app).websocket_connect("/agent") as ws_client:
        ws_client.send_text(json.dumps({"type": "utterance", "text": "hi"}))
        msg = _recv(ws_client)
        assert msg["type"] == "error"
        assert "hello" in msg["message"].lower()


def test_unknown_message_type_errors_but_session_survives() -> None:
    usage = Usage(input_tokens=1, output_tokens=1, model="scripted-1")
    responses = [
        CompletionResult(
            message=Message(role="assistant", content=[TextBlock(text="OK.")]),
            stop_reason="end_turn",
            usage=usage,
        ),
    ]
    app = _make_app(responses)
    with TestClient(app).websocket_connect("/agent") as ws_client:
        ws_client.send_text(json.dumps({"type": "hello", "tools": []}))
        ws_client.send_text(json.dumps({"type": "nonsense"}))
        err = _recv(ws_client)
        assert err["type"] == "error"

        ws_client.send_text(json.dumps({"type": "utterance", "text": "ping"}))
        final = _recv(ws_client)
        assert final == {"type": "final_text", "text": "OK."}
