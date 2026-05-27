"""Canonical message + adapter types.

Load-bearing: every adapter consumes and produces these types. Provider-native
shapes (Anthropic's `MessageParam`, OpenAI's `ChatCompletionMessage`) never
escape an adapter — they're converted at the boundary in both directions.

Day 1 keeps this minimal: no streaming, no extended-thinking blocks, no images.
Tool result content is restricted to `str` (the tool stringifies its output).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

Role = Literal["user", "assistant"]
StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"]


# --- content blocks -------------------------------------------------------


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolUseBlock:
    id: str  # provider-issued; we round-trip it back as tool_use_id
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: str  # tools stringify their output (JSON or plain text)
    is_error: bool = False


Block = TextBlock | ToolUseBlock | ToolResultBlock


# --- messages -------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    role: Role
    content: list[Block]


# --- adapter I/O ----------------------------------------------------------


@dataclass(frozen=True)
class ToolSchema:
    """Provider-agnostic tool definition. Adapters rename keys at the boundary."""

    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema for the arguments object


@dataclass(frozen=True)
class Usage:
    # `input_tokens` is the FULL prompt size (uncached + cache_read + cache_write).
    # Both adapters normalize to this so the eval CSV and Sentry view compare
    # apples to apples. Anthropic's API reports uncached-only by default; we add
    # the cache portions back in. OpenAI already reports a total.
    input_tokens: int
    output_tokens: int
    model: str
    cache_read_input_tokens: int = 0
    # Anthropic only — OpenAI auto-caches and doesn't bill writes separately.
    cache_write_input_tokens: int = 0


@dataclass(frozen=True)
class CompletionResult:
    message: Message  # role="assistant", content is the raw block sequence
    stop_reason: StopReason
    usage: Usage


# --- the adapter contract -------------------------------------------------


class LLMAdapter(Protocol):
    """One model call. The loop owns the conversation; the adapter is stateless."""

    name: str  # short provider label, e.g. "anthropic" — used in eval CSV
    model: str  # concrete model id, e.g. "claude-sonnet-4-6"

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
    ) -> CompletionResult: ...
