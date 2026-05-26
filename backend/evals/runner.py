"""Pytest entry point for the eval suite.

Parametrized over (adapter_factory, eval_case). The full sweep (every case ×
every provider) writes one row per test to `backend/evals/results.csv`:
latency, tokens, cost, success, and the actual tool list. That CSV is the
raw material for the Day-5 blog post's cross-model comparison.

Each test constructs a fresh `FakeLouie` — which is both the in-memory state
and the `Session` the loop executes tools against — runs an optional `setup`,
drives `run_turn`, and asserts on tool names + the per-case `check`. Whether
the assertions pass or fail, the row is written in a `finally` block so even
red runs leave behind comparable data.

Run: `pytest backend/evals/` (asyncio_mode=auto is set in pyproject.toml).
"""

from __future__ import annotations

import asyncio
import csv
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from backend.adapters.anthropic import AnthropicAdapter
from backend.adapters.base import LLMAdapter, ToolResultBlock
from backend.adapters.openai import OpenAIAdapter
from backend.agent.loop import TurnCancelled, run_turn
from backend.evals.cases import CASES, EvalCase
from backend.evals.fake_louie import FakeLouie

# Approximate USD per million tokens, keyed on the model id we send to the
# adapter. Numbers shift; this is for relative comparison in the blog post,
# not invoicing. Update when providers move and note the change in
# DECISIONS.md. As of 2026-05.
_PRICING: dict[str, tuple[float, float]] = {
    # (input $/Mtok, output $/Mtok)
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-7": (15.0, 75.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5": (1.25, 10.0),
}


def _estimate_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    # Returned `resp.model` may include version suffixes; match by prefix
    # against our pricing table. Unknown models log 0.0 — the CSV is still
    # useful for latency + tokens.
    for key, (pin, pout) in _PRICING.items():
        if model.startswith(key):
            return (tokens_in / 1_000_000.0) * pin + (tokens_out / 1_000_000.0) * pout
    return 0.0


_CSV_PATH = Path(__file__).parent / "results.csv"
_CSV_HEADER = [
    "case_name",
    "provider",
    "model",
    "success",
    "tools_called",
    "tokens_in",
    "tokens_out",
    "latency_ms",
    "cost_usd",
    "error",
]


@pytest.fixture(scope="session")
def csv_writer() -> Any:
    """Truncate-and-write CSV per pytest session.

    One session = one snapshot of the suite. Multiple test runs overwrite
    rather than append; copy results.csv between runs to preserve history.
    """
    f = _CSV_PATH.open("w", newline="")
    writer = csv.writer(f)
    writer.writerow(_CSV_HEADER)
    yield writer
    f.close()


# Each entry is (label, factory). A factory (rather than a singleton) means a
# fresh client per test, which keeps any future connection-pool state from
# leaking between cases. Each entry also carries the env-var that gates it, so
# Anthropic-only or OpenAI-only environments skip cleanly per-provider rather
# than disabling the entire suite.
AdapterFactory = Callable[[], LLMAdapter]


def _param(label: str, factory: AdapterFactory, env_key: str) -> pytest.param:  # type: ignore[name-defined]
    return pytest.param(
        label,
        factory,
        marks=pytest.mark.skipif(
            not os.environ.get(env_key),
            reason=f"{env_key} not set — eval suite hits real provider APIs",
        ),
        id=label,
    )


ADAPTERS = [
    _param("anthropic", lambda: AnthropicAdapter(model="claude-sonnet-4-6"), "ANTHROPIC_API_KEY"),
    _param("openai", lambda: OpenAIAdapter(model="gpt-5-mini"), "OPENAI_API_KEY"),
]


def _install_cancel_after_first_tool(fake: FakeLouie, cancel_token: asyncio.Event) -> None:
    """Wrap `dispatch_tool` so the cancel event fires once the first call returns.

    This is the eval-side analogue of the iPad sending a `cancel` message
    while a tool is still being processed: the model has issued a tool_use,
    the iPad started executing it, then the user pressed the button mid-flight.
    """
    original = fake.dispatch_tool
    first = True

    async def wrapper(name: str, args: dict[str, Any], tool_use_id: str) -> ToolResultBlock:
        nonlocal first
        result = await original(name, args, tool_use_id)
        if first:
            first = False
            cancel_token.set()
        return result

    fake.dispatch_tool = wrapper  # type: ignore[method-assign]


@pytest.mark.parametrize(("adapter_label", "adapter_factory"), ADAPTERS)
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
async def test_eval_case(
    adapter_label: str,
    adapter_factory: AdapterFactory,
    case: EvalCase,
    csv_writer: Any,
) -> None:
    adapter = adapter_factory()
    fake = FakeLouie()
    if case.setup is not None:
        case.setup(fake)

    cancel_token: asyncio.Event | None = None
    if case.cancel_after_first_tool:
        cancel_token = asyncio.Event()
        _install_cancel_after_first_tool(fake, cancel_token)

    tokens_in = 0
    tokens_out = 0
    model_id = ""
    tools_called: list[str] = []
    error: str | None = None
    start = time.perf_counter()
    try:
        if case.cancel_after_first_tool:
            assert cancel_token is not None
            try:
                await run_turn(adapter, fake, case.utterance, cancel_token=cancel_token)
            except TurnCancelled as cancelled:
                tools_called = [c.name for c in cancelled.tool_calls]
                tokens_in = cancelled.input_tokens
                tokens_out = cancelled.output_tokens
                model_id = cancelled.model
                assert len(cancelled.tool_calls) >= 1, (
                    f"[{adapter_label}/{case.name}] expected at least one tool call before "
                    f"cancel; got {len(cancelled.tool_calls)}"
                )
                # Cancellation cases skip expected_tools/check — they verify
                # control-flow, not behavior.
                return
            else:
                raise AssertionError(
                    f"[{adapter_label}/{case.name}] expected TurnCancelled, run_turn returned"
                )

        result = await run_turn(adapter, fake, case.utterance, cancel_token=cancel_token)
        tokens_in = result.input_tokens
        tokens_out = result.output_tokens
        model_id = result.model
        tools_called = result.tool_names

        # Set comparison: same unique tools, regardless of how many times each
        # was called (e.g. parallel "turn off kitchen and living room" →
        # control_lights twice, but expected_tools=["control_lights"]). Extras
        # still fail.
        assert set(tools_called) == set(case.expected_tools), (
            f"[{adapter_label}/{case.name}] tool mismatch\n"
            f"  expected: {sorted(set(case.expected_tools))}\n"
            f"  actual:   {sorted(tools_called)}\n"
            f"  final_text: {result.final_text!r}"
        )

        for forbidden in case.forbidden_tools:
            assert forbidden not in tools_called, (
                f"[{adapter_label}/{case.name}] forbidden tool {forbidden!r} was called; "
                f"actual tools={tools_called}, final_text={result.final_text!r}"
            )

        for record in result.tool_calls:
            assert not record.is_error, (
                f"[{adapter_label}/{case.name}] tool {record.name} errored: input={record.input}"
            )

        if case.check is not None:
            case.check(fake, result.final_text)
    except AssertionError as exc:
        error = f"AssertionError: {exc}".splitlines()[0][:300]
        raise
    except Exception as exc:  # noqa: BLE001 — surface infra/API errors in CSV
        error = f"{type(exc).__name__}: {exc}"[:300]
        raise
    finally:
        latency_ms = int((time.perf_counter() - start) * 1000)
        csv_writer.writerow(
            [
                case.name,
                adapter_label,
                model_id,
                "true" if error is None else "false",
                "|".join(tools_called),
                tokens_in,
                tokens_out,
                latency_ms,
                f"{_estimate_cost_usd(model_id, tokens_in, tokens_out):.5f}",
                error or "",
            ]
        )
