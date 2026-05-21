"""Hey Louie agent — FastAPI app for local development.

Run with: `fastapi dev backend/main.py`

This module is provider-agnostic: no Modal imports. The Modal wrapper lives in
`backend/deploy.py` and imports `app` from here.
"""

from __future__ import annotations

import asyncio

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from openai import AsyncOpenAI

from backend.instrumentation import init_sentry

load_dotenv()
init_sentry()

app = FastAPI(title="Hey Louie agent")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/sentry-debug")
def sentry_debug() -> None:
    raise RuntimeError("Sentry debug: this should appear in your Sentry project")


_PROMPT = "Capital of France? Answer in 1 word."


async def _ask_anthropic() -> str:
    client = AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=20,
        messages=[{"role": "user", "content": _PROMPT}],
    )
    return resp.content[0].text.strip()


async def _ask_openai() -> str:
    client = AsyncOpenAI()
    resp = await client.chat.completions.create(
        model="gpt-5-mini",
        max_completion_tokens=1000,
        messages=[{"role": "user", "content": _PROMPT}],
    )
    return (resp.choices[0].message.content or "").strip()


def _normalize(s: str) -> str:
    return s.lower().strip(".,!?\"' ")


@app.get("/llm-smoke")
async def llm_smoke() -> dict[str, object]:
    results = await asyncio.gather(
        _ask_anthropic(), _ask_openai(), return_exceptions=True
    )
    anthropic_ans = results[0] if isinstance(results[0], str) else f"error: {results[0]!r}"
    openai_ans = results[1] if isinstance(results[1], str) else f"error: {results[1]!r}"
    agree = (
        isinstance(results[0], str)
        and isinstance(results[1], str)
        and _normalize(anthropic_ans) == _normalize(openai_ans)
    )
    return {"prompt": _PROMPT, "anthropic": anthropic_ans, "openai": openai_ans, "agree": agree}
