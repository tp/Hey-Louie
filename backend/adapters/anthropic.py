"""Anthropic adapter — canonical Message ↔ Anthropic Messages API.

The loop owns conversation history; this module is a thin shim around one
`messages.create` call. Provider-native dicts never leak past `complete()`.

Day 1 omitted on purpose: streaming, extended thinking, image content.
All of those can be added without changing the canonical types in `base.py`.
Prompt caching landed later — see `complete()` for the cache_control placement.
"""

from __future__ import annotations

from typing import Any, cast

from anthropic import AsyncAnthropic

from backend.adapters.base import (
    CompletionResult,
    Message,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
)

# Anthropic stop reasons we don't model in StopReason (pause_turn, refusal) get
# coerced to "end_turn" — the loop treats them as terminal, which matches what
# the user-facing behavior should be in those cases.
_STOP_REASON_MAP: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
}


def _block_to_anthropic(block: TextBlock | ToolUseBlock | ToolResultBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    raise TypeError(f"unexpected block type: {type(block).__name__}")


def _message_to_anthropic(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": [_block_to_anthropic(b) for b in message.content],
    }


def _tool_to_anthropic(tool: ToolSchema) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


class AnthropicAdapter:
    """Implements the `LLMAdapter` protocol against `anthropic.AsyncAnthropic`.

    `max_tokens` is fixed at construction. Day 1 default of 1024 is enough for
    tool calls + a sentence or two of narration; bump for cases that need it.
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 1024,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self.model = model
        self._max_tokens = max_tokens
        self._client = client or AsyncAnthropic()

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
    ) -> CompletionResult:
        # Sort tools by name before serializing. Prompt cache hits require a
        # byte-exact prefix; whatever iteration order the iPad happens to use
        # for its tool catalog should not leak into the wire format. Sorted at
        # the adapter (not the loop) so other adapters / future call sites are
        # equally protected.
        tools_payload = [_tool_to_anthropic(t) for t in sorted(tools, key=lambda t: t.name)]
        # Cache breakpoint at the last tool entry. The Anthropic cache is a
        # prefix cache — everything up to and including this breakpoint is
        # cached as one entry. That covers `system` + all tool schemas, which
        # is the stable bulk of every call. Hits across the multi-step tool
        # loop within a turn and across turns within the 5-min TTL.
        if tools_payload:
            tools_payload[-1] = {
                **tools_payload[-1],
                "cache_control": {"type": "ephemeral"},
            }

        resp = await self._client.messages.create(
            model=self.model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[_message_to_anthropic(m) for m in messages],
            tools=tools_payload,
        )

        blocks: list[TextBlock | ToolUseBlock | ToolResultBlock] = []
        for block in resp.content:
            if block.type == "text":
                blocks.append(TextBlock(text=block.text))
            elif block.type == "tool_use":
                blocks.append(
                    ToolUseBlock(
                        id=block.id,
                        name=block.name,
                        input=cast(dict[str, Any], block.input),
                    )
                )
            # Other block types (e.g. thinking) intentionally ignored on Day 1.

        stop_reason = _STOP_REASON_MAP.get(resp.stop_reason or "end_turn", "end_turn")

        cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0

        return CompletionResult(
            message=Message(role="assistant", content=blocks),
            stop_reason=stop_reason,
            usage=Usage(
                # Anthropic's `input_tokens` excludes cached portions; add them
                # back so `Usage.input_tokens` is a full prompt-size number,
                # matching OpenAI's semantic.
                input_tokens=resp.usage.input_tokens + cache_read + cache_write,
                output_tokens=resp.usage.output_tokens,
                model=resp.model,
                cache_read_input_tokens=cache_read,
                cache_write_input_tokens=cache_write,
            ),
        )
