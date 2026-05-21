"""The agent loop.

`run_turn` drives one user utterance to a final spoken response. Each step
calls the adapter once; if the model emitted tool_use blocks, every tool in
that message is dispatched in parallel (`asyncio.gather`), the results are
appended as a single user message of `ToolResultBlock`s, and we loop. The
loop terminates on any stop reason other than `tool_use`.

Day 1 omits: streaming, cancellation, `ask_user`. Cancellation lands Day 2
once a real Session holds the cancel flag; `ask_user` lands Day 3.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from backend.adapters.base import (
    LLMAdapter,
    Message,
    StopReason,
    TextBlock,
    ToolUseBlock,
)
from backend.agent.tools import ToolRegistry
from backend.evals.fake_louie import FakeLouie

# One paragraph. Each clause earns its place; the eval suite is the regression
# net if a tweak helps one case and breaks another. Day-3 stretch is to A/B
# this against 2-3 variants on the full eval set.
SYSTEM_PROMPT = (
    "You are Louie, a voice agent for a home. The user speaks to you via push-to-talk "
    "and your reply is spoken aloud, so keep narration to one short sentence — no lists, "
    "no markdown, no preamble like 'sure, I'll do that'. Prefer confident action with a "
    "brief confirmation ('Playing jazz.') over asking clarifying questions; only ask when "
    "a request is genuinely ambiguous and you can't pick a reasonable default. For music, "
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


async def run_turn(
    adapter: LLMAdapter,
    registry: ToolRegistry,
    state: FakeLouie,
    utterance: str,
    *,
    history: list[Message] | None = None,
    system: str = SYSTEM_PROMPT,
    max_steps: int = 8,
) -> TurnResult:
    """Drive one user utterance to a final assistant response.

    `history` is the prior conversation; pass None for a fresh turn. The full
    updated history (including this turn) comes back on `TurnResult.messages`
    so the caller can feed it back in for multi-turn dialogue.
    """
    messages: list[Message] = list(history or [])
    messages.append(Message(role="user", content=[TextBlock(text=utterance)]))

    tool_calls: list[ToolCallRecord] = []
    input_tokens = 0
    output_tokens = 0
    last_model = adapter.model
    stop_reason: StopReason = "end_turn"

    schemas = registry.schemas()

    for step in range(1, max_steps + 1):
        result = await adapter.complete(system=system, messages=messages, tools=schemas)
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
            *[registry.call(tu.name, tu.input, state, tu.id) for tu in tool_uses]
        )
        for tu, res in zip(tool_uses, results, strict=True):
            tool_calls.append(ToolCallRecord(name=tu.name, input=tu.input, is_error=res.is_error))

        messages.append(Message(role="user", content=list(results)))

    raise AgentLoopError(
        f"agent loop exceeded max_steps={max_steps} (last stop_reason={stop_reason})"
    )


def _join_text(message: Message) -> str:
    return "".join(b.text for b in message.content if isinstance(b, TextBlock)).strip()
