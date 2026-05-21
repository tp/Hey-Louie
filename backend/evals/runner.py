"""Pytest entry point for the eval suite.

Parametrized over (adapter_factory, eval_case). Day 1 registers only the
Anthropic adapter; Day 3 adds OpenAI and emits a CSV with cross-provider
latency / token / cost columns. For now we just assert pass/fail.

Run: `pytest backend/evals/` (asyncio_mode=auto is set in pyproject.toml).
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from backend.adapters.anthropic import AnthropicAdapter
from backend.adapters.base import LLMAdapter
from backend.agent.loop import run_turn
from backend.agent.tools import default_registry
from backend.evals.cases import CASES, EvalCase
from backend.evals.fake_louie import default_state

# Each entry is (label, factory). Day 3 appends ("openai", lambda: OpenAIAdapter()).
# A factory (rather than a singleton) means a fresh client per test, which keeps
# any future connection-pool state from leaking between cases.
AdapterFactory = Callable[[], LLMAdapter]

ADAPTERS: list[tuple[str, AdapterFactory]] = [
    ("anthropic", lambda: AnthropicAdapter()),
]


_REQUIRES_API_KEY = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — eval suite hits real provider APIs",
)


@_REQUIRES_API_KEY
@pytest.mark.parametrize(
    ("adapter_label", "adapter_factory"),
    ADAPTERS,
    ids=[label for label, _ in ADAPTERS],
)
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
async def test_eval_case(
    adapter_label: str,
    adapter_factory: AdapterFactory,
    case: EvalCase,
) -> None:
    adapter = adapter_factory()
    registry = default_registry()
    state = default_state()

    result = await run_turn(adapter, registry, state, case.utterance)

    called = result.tool_names
    assert sorted(called) == sorted(case.expected_tools), (
        f"[{adapter_label}/{case.name}] tool mismatch\n"
        f"  expected: {sorted(case.expected_tools)}\n"
        f"  actual:   {sorted(called)}\n"
        f"  final_text: {result.final_text!r}"
    )

    for record in result.tool_calls:
        assert not record.is_error, (
            f"[{adapter_label}/{case.name}] tool {record.name} errored: input={record.input}"
        )

    if case.check is not None:
        case.check(state, result.final_text)
