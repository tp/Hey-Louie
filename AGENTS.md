# Working in this repo

## What this is

A 5-day sprint to build a working agent harness. The harness is the product,
not the home control app it happens to run on. Read PLAN.md before doing anything.

## What you should and shouldn't do

DO:

- Write the eval case before the feature it tests.
- Keep tool descriptions excellent. Bad descriptions = wrong tool choice.
  When generating or editing a tool, also review its description.
- Use the canonical Message/Block types from adapters/base.py everywhere.
  Never store provider-native message dicts in the conversation history.
- Use `asyncio.gather` for parallel tool execution. Never sequentialize.
- Log decisions in DECISIONS.md as you make them. One paragraph each.
- Run `uv run pytest backend/` after any change to agent/, adapters/, or transport/.

DON'T:

- Add tools beyond the five in evals/fake_louie.py (LOUIE_TOOL_SCHEMAS).
  If you think one is needed, add a line to FOLLOWUPS.md and stop.
- Add features to the Louie testbed. The harness is the product.
- Reach for LangChain, LangGraph, LiteLLM, or Pydantic AI in the main path.
  These are explicitly excluded — the hand-rolled adapter is the point.
- Add persistence (Redis, Postgres). In-memory session dict is correct for
  this sprint.
- Polish the iPad UI beyond functional. Waveform animations, splash screens,
  themes — all out of scope.

## Architecture invariants

- Backend agent loop dispatches every tool to the iPad via `Session.dispatch_tool`; the iPad executes them (including `ask_user`, which shows a tap popover and returns the picked choice id).
- WebSocket protocol exactly as documented in PLAN.md "Architecture summary".
- Sentry instrumentation uses OpenTelemetry `gen_ai.*` semantic conventions.

## Style

- Python: type-annotated, dataclasses over dicts, async-first.
- Swift: SwiftUI, no UIKit unless unavoidable, no third-party deps for
  the sprint (push-to-talk is built-in).
- Tests: pytest with parametrize over adapters. No mocking of LLM responses
  in evals — those are integration tests against real APIs.

## When to ask before doing

- Anything that would touch the WebSocket protocol shape.
- Anything that adds a tool.
- Anything that adds a dependency to the backend or iPad.
- Anything in adapters/base.py — the canonical types are load-bearing.

## When to just do it

- Implementing eval cases.
- Editing tool descriptions.
- Refactoring within a single module that doesn't change its interface.
- Adding instrumentation spans.
- Fixing failing tests.
