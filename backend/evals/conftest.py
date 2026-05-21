"""Pytest config for the eval suite.

Loads `.env` so the runner picks up `ANTHROPIC_API_KEY` (and Day-3
`OPENAI_API_KEY`) the same way `backend/main.py` does at app startup.
Eval tests hit real provider APIs by design (see AGENTS.md), so the keys
must be available — if `.env` is missing or doesn't have them, the
`_REQUIRES_API_KEY` skipif in `runner.py` skips the relevant cases.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()
