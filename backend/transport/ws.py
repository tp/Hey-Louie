"""FastAPI WebSocket endpoint for the voice agent.

One WebSocket = one push-to-talk turn. The handshake is:

  client → {"type": "hello", "session_id": "<uuid>", "tools": [<schema>, ...]}
  client → {"type": "utterance", "text": "..."}
  server ↔ client  tool_call / tool_result loop
  server → {"type": "final_text", "text": "..."}     (or "error")
  close

The `tools` field carries the iPad's full tool schemas (name, description,
input_schema) — the backend doesn't store these; whatever the client sends is
what the model sees. This keeps tool discovery client-driven so future
iPad-side additions don't need a coordinated backend change.

See DECISIONS.md "WebSocket protocol" for the rationale.

Concurrency model: the recv loop runs in the request handler task. Once
`utterance` arrives, the agent loop runs in a separate task so the recv loop
can keep dispatching incoming `tool_result` messages to the Session's
pending-futures map. A per-Session asyncio.Lock serializes outbound sends
because parallel tool dispatch can write concurrently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast, override

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.adapters.anthropic import AnthropicAdapter
from backend.adapters.base import LLMAdapter, ToolResultBlock, ToolSchema
from backend.adapters.openai import OpenAIAdapter
from backend.agent.loop import AgentLoopError, run_turn
from backend.agent.session import Session

log = logging.getLogger(__name__)

router = APIRouter()


# Module-level adapter factory so tests can swap it via `set_adapter_factory`.
# Read inside the endpoint at call time (not captured as a default argument) so
# the override is picked up even though FastAPI registered the endpoint earlier.
AdapterFactory = Callable[[], LLMAdapter]


def _default_adapter_factory() -> LLMAdapter:
    # Randomize provider per session so both adapters get exercised in normal use.
    # Haiku is excluded — it misfires on the ask_user disambiguation rule
    # (verbalizes the clarifying question instead of calling the tool). Still
    # in the eval matrix for tracking; not in production rotation.
    return random.choice([AnthropicAdapter, OpenAIAdapter])()


_adapter_factory: AdapterFactory = _default_adapter_factory


def set_adapter_factory(factory: AdapterFactory | None) -> None:
    """Swap the adapter factory used by new sessions. None resets to default."""
    global _adapter_factory
    _adapter_factory = factory or _default_adapter_factory


@dataclass
class WebSocketSession(Session):
    """Per-turn Session backed by a WebSocket to the iPad.

    `_schemas` is whatever the client sent in `hello.tools`; we don't validate
    or merge with anything backend-side. `pending` correlates outbound
    `tool_call` messages with incoming `tool_result` messages via tool_use_id.

    Inherits from `Session` explicitly so signature drift between this impl
    and the protocol is a type error at definition time, not a silent
    structural mismatch caught only when something calls it the wrong way.
    """

    session_id: str
    ws: WebSocket
    adapter: LLMAdapter
    _schemas: list[ToolSchema] = field(default_factory=list)
    pending: dict[str, asyncio.Future[ToolResultBlock]] = field(default_factory=dict)
    # Serializes concurrent `send_json` calls from parallel tool dispatches.
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @override
    def schemas(self) -> list[ToolSchema]:
        return self._schemas

    async def send(self, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.ws.send_json(payload)

    @override
    async def dispatch_tool(
        self, name: str, args: dict[str, Any], tool_use_id: str
    ) -> ToolResultBlock:
        """Send `tool_call` to the iPad and await the matching `tool_result`.

        Wire/cancel errors come back as `ToolResultBlock(is_error=True)` so the
        model gets a chance to recover instead of seeing the turn die mid-step.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ToolResultBlock] = loop.create_future()
        self.pending[tool_use_id] = future
        try:
            await self.send(
                {
                    "type": "tool_call",
                    "tool_use_id": tool_use_id,
                    "name": name,
                    "input": args,
                }
            )
            return await future
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — WS death, etc.
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=f"{type(exc).__name__}: {exc}",
                is_error=True,
            )
        finally:
            # Clear the entry even on error/cancel so a stale Future can't be
            # resolved by a late `tool_result` from a previous turn.
            self.pending.pop(tool_use_id, None)

    def resolve_tool(self, tool_use_id: str, content: str, is_error: bool) -> bool:
        """Complete the Future waiting on `tool_use_id`. Returns True if matched."""
        future = self.pending.get(tool_use_id)
        if future is None or future.done():
            return False
        future.set_result(
            ToolResultBlock(tool_use_id=tool_use_id, content=content, is_error=is_error)
        )
        return True


@router.websocket("/agent")
async def agent_ws(ws: WebSocket) -> None:
    await ws.accept()
    session: WebSocketSession | None = None
    turn_task: asyncio.Task[None] | None = None

    async def send(payload: dict[str, Any]) -> None:
        # Once a session exists, ALL writes must go through its send lock —
        # a running turn_task can be mid-`session.send(tool_call)` while the
        # recv loop wants to emit an error response, and starlette's
        # WebSocket.send isn't concurrency-safe.
        if session is not None:
            await session.send(payload)
        else:
            await ws.send_json(payload)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await send({"type": "error", "message": "invalid json"})
                continue
            if not isinstance(msg, dict):
                await send({"type": "error", "message": "expected json object"})
                continue

            mtype = msg.get("type")

            if mtype == "hello":
                if session is not None:
                    await send({"type": "error", "message": "duplicate hello"})
                    continue
                try:
                    session = _open_session(ws, msg, _adapter_factory)
                except ValueError as exc:
                    await send({"type": "error", "message": f"bad hello: {exc}"})
                    continue
                log.info(
                    "session %s: hello, adapter=%s model=%s tools=%s",
                    session.session_id,
                    session.adapter.name,
                    getattr(session.adapter, "model", "?"),
                    [s.name for s in session.schemas()],
                )

            elif mtype == "utterance":
                if session is None:
                    await send({"type": "error", "message": "hello required before utterance"})
                    continue
                if turn_task is not None and not turn_task.done():
                    await send({"type": "error", "message": "another turn is already running"})
                    continue
                text = msg.get("text")
                if not isinstance(text, str) or not text.strip():
                    await send(
                        {"type": "error", "message": "utterance.text must be a non-empty string"}
                    )
                    continue
                turn_task = asyncio.create_task(_run_one_turn(session, text))

            elif mtype == "tool_result":
                if session is None:
                    continue
                tool_use_id = msg.get("tool_use_id")
                if not isinstance(tool_use_id, str):
                    continue
                session.resolve_tool(
                    tool_use_id=tool_use_id,
                    content=str(msg.get("content", "")),
                    is_error=bool(msg.get("is_error", False)),
                )

            elif mtype == "cancel":
                if turn_task is not None and not turn_task.done():
                    turn_task.cancel()

            else:
                await send({"type": "error", "message": f"unknown type: {mtype!r}"})

    except WebSocketDisconnect:
        log.info(
            "session %s: disconnected",
            session.session_id if session else "<no-session>",
        )
    finally:
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
            try:
                await turn_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — cleanup path, swallow any error
                log.exception("error draining turn_task on disconnect")


def _open_session(
    ws: WebSocket, hello: dict[str, Any], adapter_factory: AdapterFactory
) -> WebSocketSession:
    session_id = hello.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = str(uuid.uuid4())
    schemas = _parse_tools(hello.get("tools"))
    return WebSocketSession(
        session_id=session_id,
        ws=ws,
        adapter=adapter_factory(),
        _schemas=schemas,
    )


def _parse_tools(raw: Any) -> list[ToolSchema]:
    """Coerce hello.tools into a list of ToolSchema. Raises ValueError on shape errors."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("`tools` must be a list of {name, description, input_schema} objects")
    out: list[ToolSchema] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"tools[{i}] must be an object")
        entry_d = cast(dict[str, Any], entry)
        name = entry_d.get("name")
        description = entry_d.get("description")
        input_schema = entry_d.get("input_schema")
        if not isinstance(name, str) or not name:
            raise ValueError(f"tools[{i}].name must be a non-empty string")
        if not isinstance(description, str):
            raise ValueError(f"tools[{i}].description must be a string")
        if not isinstance(input_schema, dict):
            raise ValueError(f"tools[{i}].input_schema must be an object")
        out.append(ToolSchema(name=name, description=description, input_schema=input_schema))
    return out


async def _run_one_turn(session: WebSocketSession, utterance: str) -> None:
    """Drive the agent loop for one utterance, then send `final_text` and close."""
    try:
        result = await run_turn(
            session.adapter, session, utterance, conversation_id=session.session_id
        )
        await session.send({"type": "final_text", "text": result.final_text})
    except asyncio.CancelledError:
        log.info("session %s: turn cancelled", session.session_id)
        # Best-effort notify; the WS may already be closing.
        try:
            await session.send({"type": "cancelled"})
        except Exception:  # noqa: BLE001
            pass
        raise
    except AgentLoopError as exc:
        log.warning("session %s: agent loop error: %s", session.session_id, exc)
        await session.send({"type": "error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 — surface any backend bug to the iPad
        log.exception("session %s: turn errored", session.session_id)
        await session.send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        # Close from the server side so the iPad sees a clean disconnect.
        try:
            await session.ws.close()
        except Exception:  # noqa: BLE001 — already closed is fine
            pass
