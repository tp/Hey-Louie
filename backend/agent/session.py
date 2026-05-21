"""The Session interface — how the agent loop executes a tool.

A Session is the abstract "thing that can run tools and tell us which ones it
has". The agent loop knows nothing about iPads, WebSockets, or in-memory
state; it asks the session for schemas, hands the model whatever the session
returns, then routes any `tool_use` block back to `session.dispatch_tool`.

Two implementations exist:

- `evals/fake_louie.py:FakeLouie` — synchronous, in-memory. Used by the eval
  suite. The catalog and handlers that simulate the iPad live there.
- `transport/ws.py:WebSocketSession` — WebSocket-backed. Schemas come from the
  client's `hello` message; dispatch round-trips a `tool_call` over the wire.

Server-side tools — things the backend executes itself, never round-tripping
through the iPad (future: code-exec sandbox, web fetch, file ops) — slot in
via `extra_tools` on `run_turn`. None exist today; the join point is the
parameter signature in `agent/loop.py:run_turn`.

`ask_user` is NOT a server-side tool: it's client-mediated, lives in the
iPad's tool catalog, and reaches the loop through `session.schemas()`. The
shape is `{question, choices: [{id, label}, ...]}` — the iPad shows a tap
popover (not voice), the user picks one, the iPad sends the picked id back
as `tool_result`. Disambiguation by tap, not by re-recording.
"""

from __future__ import annotations

from typing import Any, Protocol

from backend.adapters.base import ToolResultBlock, ToolSchema


class Session(Protocol):
    """What `run_turn` needs from whoever is executing tools."""

    def schemas(self) -> list[ToolSchema]:
        """The tools the agent should expose to the model for this session."""
        ...

    async def dispatch_tool(
        self, name: str, args: dict[str, Any], tool_use_id: str
    ) -> ToolResultBlock:
        """Run one tool call and return the result block.

        Implementations own the error boundary: handler exceptions should come
        back as `ToolResultBlock(is_error=True, content="...")` so the model
        can recover, not as raised exceptions that abort the turn.
        """
        ...
