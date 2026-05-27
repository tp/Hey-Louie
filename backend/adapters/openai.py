"""OpenAI adapter — canonical Message ↔ OpenAI Responses API.

The Responses API is the right surface for tool-using agents: it returns rich
output items (text, function_call, reasoning), supports parallel tool calls,
and is where OpenAI is moving reasoning models. Chat Completions would also
work; Responses keeps the door open for `extended thinking` style features
without re-writing the adapter.

Shape mapping vs Anthropic:
- `system` → `instructions=` (string, not a message).
- Canonical `Message`s are flattened into a list of input items: user/assistant
  text become `{role, content:[{type:"input_text"|"output_text", text}]}`,
  `ToolUseBlock` becomes a `function_call` item (carrying `call_id`),
  `ToolResultBlock` becomes a `function_call_output` item.
- We round-trip `call_id` as `ToolUseBlock.id`. The Responses API also
  attaches an item `id` to each function_call; we don't use it — `call_id` is
  the cross-API stable handle and what `function_call_output` matches on.
- Stop reason: any `function_call` in output → "tool_use"; else
  `incomplete_details.reason == "max_output_tokens"` → "max_tokens"; else
  "end_turn" (refusals, content filter, etc. collapse to terminal).

Day 1 omissions carried forward: no streaming, no reasoning output, no
images. Day-3-stretch reasoning support means setting `reasoning={"effort":...}`
in __init__ and threading the reasoning output items back through — not in
the sprint.
"""

from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

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


def _tool_to_openai(tool: ToolSchema) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema,
        # strict=False — our schemas use JSON Schema features (enum on optional
        # fields, no additionalProperties:false) that strict mode rejects. The
        # eval suite is the regression net for argument quality.
        "strict": False,
    }


def _message_to_input_items(message: Message) -> list[dict[str, Any]]:
    """One canonical Message → 1+ Responses-API input items.

    Text and tool-use blocks in the same assistant message become separate
    items, in order. Tool-result blocks (always carried on a user-role
    message in our model) become standalone `function_call_output` items —
    the `role` field on the parent is irrelevant for those.
    """
    items: list[dict[str, Any]] = []
    text_buf: list[str] = []

    def flush_text() -> None:
        if not text_buf:
            return
        joined = "".join(text_buf)
        text_buf.clear()
        if message.role == "assistant":
            items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": joined}],
                }
            )
        else:
            # Plain-string content (not the `[{"type":"input_text",...}]` array
            # form) so Sentry's Conversations view actually renders the user
            # utterance — the renderer only knows the string form. The Responses
            # API accepts both shapes; semantically equivalent for the model.
            # See FOLLOWUPS.md "Sentry Conversations view: incomplete rendering".
            items.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": joined,
                }
            )

    for block in message.content:
        if isinstance(block, TextBlock):
            text_buf.append(block.text)
        elif isinstance(block, ToolUseBlock):
            flush_text()
            items.append(
                {
                    "type": "function_call",
                    "call_id": block.id,
                    "name": block.name,
                    "arguments": json.dumps(block.input),
                }
            )
        elif isinstance(block, ToolResultBlock):
            flush_text()
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": block.tool_use_id,
                    "output": block.content,
                }
            )
        else:
            raise TypeError(f"unexpected block type: {type(block).__name__}")

    flush_text()
    return items


def _resolve_stop_reason(
    has_tool_use: bool, status: str | None, incomplete_reason: str | None
) -> StopReason:
    if has_tool_use:
        return "tool_use"
    if status == "incomplete" and incomplete_reason == "max_output_tokens":
        return "max_tokens"
    return "end_turn"


class OpenAIAdapter:
    """Implements the `LLMAdapter` protocol against `openai.AsyncOpenAI`.

    `max_tokens` maps to `max_output_tokens`. Default of 1024 matches the
    Anthropic adapter — bump for reasoning models or verbose narration.
    """

    name = "openai"

    def __init__(
        self,
        *,
        model: str = "gpt-5-mini",
        max_tokens: int = 1024,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.model = model
        self._max_tokens = max_tokens
        self._client = client or AsyncOpenAI()

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
    ) -> CompletionResult:
        input_items: list[dict[str, Any]] = []
        for m in messages:
            input_items.extend(_message_to_input_items(m))

        # Sort tools by name. OpenAI's auto prompt cache also keys on the
        # serialized prefix, so any reorder from the iPad's tool catalog would
        # break cache hits on prompts ≥1024 tokens. Same rationale as the
        # Anthropic adapter.
        sorted_tools = [_tool_to_openai(t) for t in sorted(tools, key=lambda t: t.name)]

        resp = await self._client.responses.create(
            model=self.model,
            instructions=system,
            input=input_items,
            tools=sorted_tools,
            max_output_tokens=self._max_tokens,
            # Don't persist server-side state; we own the conversation history.
            store=False,
            parallel_tool_calls=True,
        )

        blocks: list[TextBlock | ToolUseBlock | ToolResultBlock] = []
        for item in resp.output:
            item_type = getattr(item, "type", None)
            if item_type == "message":
                for c in item.content:
                    c_type = getattr(c, "type", None)
                    if c_type == "output_text":
                        blocks.append(TextBlock(text=c.text))
                    # refusal / annotation content intentionally ignored on Day 3.
            elif item_type == "function_call":
                try:
                    parsed_input = json.loads(item.arguments) if item.arguments else {}
                except json.JSONDecodeError:
                    parsed_input = {"_raw_arguments": item.arguments}
                if not isinstance(parsed_input, dict):
                    parsed_input = {"_value": parsed_input}
                blocks.append(
                    ToolUseBlock(
                        id=item.call_id,
                        name=item.name,
                        input=parsed_input,
                    )
                )
            # reasoning / web_search_call / etc. ignored on Day 3.

        has_tool_use = any(isinstance(b, ToolUseBlock) for b in blocks)
        incomplete_reason = (
            resp.incomplete_details.reason if resp.incomplete_details is not None else None
        )
        stop_reason = _resolve_stop_reason(has_tool_use, resp.status, incomplete_reason)

        usage = resp.usage
        cached_tokens = 0
        if usage is not None:
            details = getattr(usage, "input_tokens_details", None)
            if details is not None:
                cached_tokens = getattr(details, "cached_tokens", 0) or 0
        return CompletionResult(
            message=Message(role="assistant", content=blocks),
            stop_reason=stop_reason,
            usage=Usage(
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
                model=resp.model,
                cache_read_input_tokens=cached_tokens,
            ),
        )
