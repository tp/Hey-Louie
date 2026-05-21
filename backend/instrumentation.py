"""Sentry initialization.

Errors + request traces + LLM call instrumentation (Anthropic, OpenAI).
The manual `gen_ai.invoke_agent` / `gen_ai.execute_tool` spans wrapping the
agent loop land on Day 4 per PLAN.md — those come on top of this.
"""

from __future__ import annotations

import os

import sentry_sdk
from sentry_sdk.integrations.anthropic import AnthropicIntegration
from sentry_sdk.integrations.openai import OpenAIIntegration


def init_sentry() -> None:
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT", "development"),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "1.0")),
        send_default_pii=True,
        integrations=[
            AnthropicIntegration(include_prompts=True),
            OpenAIIntegration(include_prompts=True),
        ],
    )
