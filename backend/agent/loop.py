"""The agent loop.

`run_turn` drives one user utterance to a final spoken response. Each step
calls the adapter once; if the model emitted tool_use blocks, every tool in
that message is dispatched in parallel via `session.dispatch_tool`, the
results are appended as a single user message of `ToolResultBlock`s, and we
loop. The loop terminates on any stop reason other than `tool_use`.

The agent layer knows nothing about iPads, WebSockets, or in-memory state —
that all lives behind the `Session` interface. Two Sessions exist today:
`FakeLouie` (eval suite) and `WebSocketSession` (production wire).

`extra_tools` is the slot for server-side tools — things the backend executes
directly without round-tripping to the iPad (future: code-exec sandbox, web
fetch, file ops). Currently empty: no server-side tools exist in this sprint.
See `DECISIONS.md "Server-side tools: deferred to a future sprint"` for the
future-routing plan. (Note: `ask_user` is NOT a server-side tool — it lives
in the iPad's tool catalog and reaches the loop through `session.schemas()`.
Its shape is question + tap-able choices, the iPad shows a popover, and the
picked choice id comes back as `tool_result`.)
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from backend.adapters.base import (
    LLMAdapter,
    Message,
    StopReason,
    TextBlock,
    ToolSchema,
    ToolUseBlock,
)
from backend.agent.session import Session

# One paragraph. Each clause earns its place; the eval suite is the regression
# net if a tweak helps one case and breaks another. Day-3 stretch is to A/B
# this against 2-3 variants on the full eval set.
SYSTEM_PROMPT = (
    "You are Louie, a voice agent for a home. The user speaks to you via push-to-talk "
    "and your reply is spoken aloud, so keep narration to one short sentence — no lists, "
    "no markdown, no preamble like 'sure, I'll do that'. Prefer confident action with a "
    "brief confirmation ('Playing jazz.') over asking clarifying questions. Use ask_user "
    "sparingly: only when (a) two or more plausible interpretations exist AND (b) picking "
    "the wrong default would noticeably annoy the user (e.g. 'play Thriller' → song vs "
    "album). Never ask about which room, what temperature, or which light — pick a sensible "
    "default and say what you did. When a search returns hits where one type/title clearly "
    "matches the user's phrasing, take that hit silently rather than asking. For music, "
    "always call search_music first to get a real id, then play_music with that id — never "
    "synthesize or pass raw queries to play_music. For state questions ('what's playing?', "
    "'is the kitchen on?'), call query_state with the narrowest subsystem before answering. "
    "Trust the user's request literally; don't volunteer changes they didn't ask for."
)


@dataclass(frozen=True)
class ToolCallRecord:
    """One tool invocation made during a turn — for eval assertions and the CSV."""

    name: str
    input: dict[str, Any]
    is_error: bool


@dataclass
class TurnResult:
    final_text: str
    stop_reason: StopReason
    tool_calls: list[ToolCallRecord]
    messages: list[Message]  # full history including this turn
    input_tokens: int = 0
    output_tokens: int = 0
    steps: int = 0
    model: str = ""

    @property
    def tool_names(self) -> list[str]:
        return [c.name for c in self.tool_calls]


class AgentLoopError(RuntimeError):
    """Raised when the loop can't terminate (e.g. exceeds max_steps)."""


class TurnCancelled(RuntimeError):
    """Raised when `cancel_token` was set mid-turn.

    Carries the partial messages, tool_calls, and per-provider token counts
    accumulated before the cancel so the WebSocket layer can drain/log them
    (in production), eval cases can assert that at least one tool dispatched
    before bail-out, and the CSV row still reflects what was actually spent.
    """

    def __init__(
        self,
        messages: list[Message],
        tool_calls: list[ToolCallRecord],
        steps: int,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = "",
    ) -> None:
        super().__init__(f"turn cancelled after {steps} step(s), {len(tool_calls)} tool call(s)")
        self.messages = messages
        self.tool_calls = tool_calls
        self.steps = steps
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.model = model


async def run_turn(
    adapter: LLMAdapter,
    session: Session,
    utterance: str,
    *,
    history: list[Message] | None = None,
    system: str = SYSTEM_PROMPT,
    max_steps: int = 8,
    extra_tools: Sequence[ToolSchema] = (),
    cancel_token: asyncio.Event | None = None,
) -> TurnResult:
    """Drive one user utterance to a final assistant response.

    `history` is the prior conversation; pass None for a fresh turn. The full
    updated history (including this turn) comes back on `TurnResult.messages`
    so the caller can feed it back in for multi-turn dialogue.

    `extra_tools` joins server-side tool schemas into the list the model sees.
    Their dispatch is not yet routed — when the first server-side tool lands,
    this signature grows a `server_dispatch: Callable[[name, args, id], Awaitable[ToolResultBlock]]`
    callable, and the gather() below tries it for any name that's in
    `extra_tools` before falling back to `session.dispatch_tool`. For Day 2
    `extra_tools` is empty and the routing question hasn't bitten us.
    """
    messages: list[Message] = list(history or [])
    messages.append(Message(role="user", content=[TextBlock(text=utterance)]))

    tool_calls: list[ToolCallRecord] = []
    input_tokens = 0
    output_tokens = 0
    last_model = adapter.model
    stop_reason: StopReason = "end_turn"

    tools: list[ToolSchema] = [*session.schemas(), *extra_tools]

    for step in range(1, max_steps + 1):
        # Check before paying for the model call. In production the WebSocket
        # `cancel` message sets this; in evals the runner wires it to fire
        # after the first tool result.
        if cancel_token is not None and cancel_token.is_set():
            raise TurnCancelled(
                messages,
                tool_calls,
                step - 1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=last_model,
            )

        result = await adapter.complete(system=system, messages=messages, tools=tools)
        messages.append(result.message)
        input_tokens += result.usage.input_tokens
        output_tokens += result.usage.output_tokens
        last_model = result.usage.model
        stop_reason = result.stop_reason

        if result.stop_reason != "tool_use":
            return TurnResult(
                final_text=_join_text(result.message),
                stop_reason=stop_reason,
                tool_calls=tool_calls,
                messages=messages,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                steps=step,
                model=last_model,
            )

        tool_uses = [b for b in result.message.content if isinstance(b, ToolUseBlock)]

        # gather() preserves input order, so results line up with tool_uses by
        # index — needed to pair tool_use_ids back to their results.
        results = await asyncio.gather(
            *[session.dispatch_tool(tu.name, tu.input, tu.id) for tu in tool_uses]
        )
        for tu, res in zip(tool_uses, results, strict=True):
            tool_calls.append(ToolCallRecord(name=tu.name, input=tu.input, is_error=res.is_error))

        messages.append(Message(role="user", content=list(results)))

        # Second check: tool results are in but we haven't started the next
        # model call yet. Cheapest place to honor a cancel that arrived while
        # tools were running.
        if cancel_token is not None and cancel_token.is_set():
            raise TurnCancelled(
                messages,
                tool_calls,
                step,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=last_model,
            )

    raise AgentLoopError(
        f"agent loop exceeded max_steps={max_steps} (last stop_reason={stop_reason})"
    )


def _join_text(message: Message) -> str:
    return "".join(b.text for b in message.content if isinstance(b, TextBlock)).strip()
