# Louie Voice Agent — 5-Day Implementation Plan

## Goal

Build a credible **agent harness** in 5 days. Voice in (push-to-talk on iPad), voice out, agent loop running on a Python backend on Modal, tools dispatched back to the iPad over WebSocket, evals across two frontier models, observability via Sentry, and a blog post with concrete data.

The harness is the protagonist. [Louie](https://github.com/tp/Louie) is the testbed.

## Why this scope

Five days is a budget, not an aspiration. The point is not to build a polished home-control app — it is to produce a small but real piece of agent infrastructure with eval data and an honest writeup. The signal lives in trade-off articulation, not in feature count.

## Discipline (read this every morning)

- **The agent harness is the protagonist.** Louie is the testbed mentioned in two sentences of the blog post.
- **Five tools, locked on Day 1.** No additions during the sprint. If a sixth tool would be cool, write it in `FOLLOWUPS.md` and move on.
- **Evals are written before the feature they test.** Always.
- **Blog post outline is written before any code.** If you can't write the section, you don't yet know what you're building.
- **Decisions go in `DECISIONS.md` as you make them.** One paragraph each. This is the raw material for the blog post.
- **Stretch goals do not enter the sprint.** They go in `FOLLOWUPS.md` and become future blog posts.

## Stack

- **Backend:** Python 3.12, FastAPI, deployed to Modal. WebSocket via `@modal.asgi_app()`.
- **LLM:** Anthropic and OpenAI native Python SDKs. Hand-rolled adapter behind a canonical message format. No LiteLLM, no Pydantic AI, no LangGraph — the point is to understand the loop.
- **iPad client:** SwiftUI. `SFSpeechRecognizer` for STT (on-device, push-to-talk). `AVSpeechSynthesizer` for TTS. `URLSessionWebSocketTask` for transport.
- **Tools:** Five total. `play_music`, `control_lights`, `set_climate`, `query_state`, plus the system tool `ask_user`. All dispatched to iPad except `ask_user`.
- **Eval harness:** pytest with custom assertions. Fake-Louie fixture for tool handlers. CSV output for cross-model comparison.
- **Observability:** Sentry with `AnthropicIntegration` and `OpenAIIntegration` auto-instrumentation. Trace IDs propagated iPad ↔ backend.
- **Local dev:** Claude Code, Codex, or Cursor as primary driver. The repo includes an `AGENTS.md` (see bottom of this doc).

## Architecture (one paragraph)

iPad opens WebSocket for interaction only (not held open; to let the server go to sleep) and sends `tool_manifest`. User holds push-to-talk button; on release, `SFSpeechRecognizer` returns final transcript; iPad sends `utterance` over WebSocket. Backend runs agent loop: model call → if `stop_reason == tool_use`, dispatch tool calls in parallel (some local like `ask_user`, most remote via WebSocket → iPad) → collect results → next model call → repeat until `end_turn`. Final text streamed back to iPad, spoken via `AVSpeechSynthesizer`. Sentry instruments the whole trace, end to end.

## Directory layout

```
louie-agent/
├── PLAN.md                  (this file)
├── AGENTS.md                (instructions for agentic coding tools)
├── DECISIONS.md             (running log of design decisions)
├── FOLLOWUPS.md             (ideas that didn't make the sprint)
├── BLOG_OUTLINE.md          (section headings, written Day 0)
├── backend/
│   ├── adapters/
│   │   ├── base.py          (canonical Message + LLMAdapter protocol)
│   │   ├── anthropic.py
│   │   └── openai.py
│   ├── agent/
│   │   ├── loop.py
│   │   ├── session.py
│   │   └── tools.py
│   ├── transport/
│   │   └── ws.py
│   ├── evals/
│   │   ├── cases.py
│   │   ├── fake_louie.py
│   │   └── runner.py
│   ├── instrumentation.py   (Sentry setup)
│   └── main.py              (Modal app entry)
└── ipad/                    (SwiftUI Xcode project, lives in the Louie repo, we might want to publish a Swift package here to consume/implement)
    └── ...
```

---

## Day 0 (evening before): Setup

Stop reading docs. Set up the repo and write the blog outline.

**Tasks:**

- Create repo, initialize Python project (`uv` or `poetry`).
- Install Modal CLI, log in. Confirm a "hello world" deploys.
- Anthropic and OpenAI API keys in a `.env` file. Test each with a one-line script.
- Sentry project created. DSN in `.env`. SDK installed.
- `BLOG_OUTLINE.md` written: five section headings minimum. _Don't write content yet — just the headings, so the work has shape._
- `DECISIONS.md` created. First entry: "Backend orchestrates, iPad executes tools — chose because of model/tool-ecosystem maturity in Python and because real production agents work this way."

**Done when:** You can deploy a "hello world" to Modal and call both LLM APIs from your laptop. No code yet for the agent.

---

## Day 1: Adapter + loop + first evals (no transport, no iPad)

**Goal:** A function `run_turn(adapter, session, utterance) -> str` that runs the full agent loop against the fake Louie. Both adapters work. Five eval cases pass.

**Deliverables:**

1. `adapters/base.py` — `Message`, `TextBlock`, `ToolUseBlock`, `ToolResultBlock`, `CompletionResult`, `LLMAdapter` protocol.
2. `adapters/anthropic.py` — implements `LLMAdapter` against `anthropic.AsyncAnthropic`.
3. `agent/tools.py` — `Tool` dataclass, `ToolRegistry`, four mocked tools (`play_music`, `control_lights`, `set_climate`, `query_state`). All "local" for Day 1 — they mutate the fake Louie state directly.
4. `agent/loop.py` — the loop. Parallel tool execution via `asyncio.gather`. No streaming yet, no `ask_user`, no cancellation.
5. `evals/fake_louie.py` — in-memory state object with `music`, `lights`, `climate` fields.
6. `evals/cases.py` — five eval cases, all unambiguous, covering each tool.
7. `evals/runner.py` — pytest-based runner that parametrizes over adapters. For Day 1, only Anthropic registered.

**Discipline:**

- The first thing you write today is the eval cases, _before_ the loop. They define what "working" means.
- Tool descriptions get disproportionate attention. Bad descriptions = wrong tool choice. Read them out loud.
- The system prompt is one carefully-written paragraph. Save it as a constant in `agent/loop.py`.

**Stretch (do not start until everything above is solid):**

- Add a third adapter using **Pydantic AI**, run the same evals through it, compare lines of code and observed behavior. This becomes a "build it three ways" sub-section in the blog post.
- Iterate the system prompt with at least 3 variants tested against the eval suite. The blog post benefits from showing how much prompt sensitivity there is.

**Done when:** `pytest backend/evals/` passes all 5 cases against the Anthropic adapter. You can `print(asyncio.run(run_turn(...)))` and see sensible output.

---

## Day 2: Transport + iPad client

**Goal:** End-to-end voice on iPad. Press button, speak, hear response, with all tools executing on the iPad against the (still mocked) Louie state.

**Deliverables:**

Backend:

1. `transport/ws.py` — FastAPI WebSocket handler. Dispatches messages by `type`. Maintains a `pending_tool_results: dict[str, Future]` per session.
2. Refactor tools: `play_music`, `control_lights`, `set_climate`, `query_state` flip from `local=True` to `local=False`. They now dispatch over the WebSocket and await results.
3. `agent/session.py` — `Session` dataclass holds adapter, registry, messages, futures, the WebSocket reference, a `cancelled` flag.
4. `main.py` — Modal app with `@modal.asgi_app()` exposing the WebSocket.

iPad:

1. SwiftUI app with a single push-to-talk button. `SFSpeechRecognizer` with `requestOnDeviceRecognition = true` and `shouldReportPartialResults = false`.
2. WebSocket client (`URLSessionWebSocketTask`). On connect, send `tool_manifest`. On `tool_call` message, execute against a local fake-Louie object, send back `tool_result`. On `final_text`, speak with `AVSpeechSynthesizer`.
3. A simple state view that shows "what Louie thinks is happening" so you can verify tool calls landed.

**Discipline:**

- Wire `cancel` from day one. Pressing the button mid-response cancels the in-flight turn. The eval suite gets a cancel case on Day 3.
- WebSocket protocol exactly as specified in `DECISIONS.md`. Resist the temptation to add fields.
- iPad is dumb. No business logic. Tool execution against fake Louie state — connecting to the real Louie is a follow-up, not a sprint goal.

**Stretch:**

- Replace push-to-talk with **Silero VAD** (CoreML port). Tune silence threshold. Compare UX in the blog post.
- Implement **streaming text** from backend (model.stream → SSE-over-WebSocket → iPad starts speaking before model finishes). Real latency win.
- Compare a **voice-to-voice path** (OpenAI Realtime API) to the text-route baseline. This is its own blog post — measure latency, cost, and the loss of cross-provider portability.
- Trace ID propagation iPad → backend so Sentry shows the full button-press-to-TTS trace.

**Done when:** You speak into the iPad, the agent calls a tool, the iPad updates its fake state, and you hear a response. End-to-end loop on real voice.

---

## Day 3: Second adapter + cross-frontier evals + `ask_user`

**Goal:** Both Anthropic and OpenAI adapters work. Eval CSV with comparison data exists. Disambiguation via `ask_user` works.

**Deliverables:**

1. `adapters/openai.py` — implements `LLMAdapter` against `openai.AsyncOpenAI`. Same canonical types in/out. About 100 lines.
2. `agent/tools.py` — `ASK_USER` tool definition. Marked `local=True`. In the loop, it sends `ask_user` event to iPad, awaits the next utterance, returns it as the tool result.
3. iPad: handle `ask_user` event by speaking the question via TTS and treating the next button-press utterance as the answer.
4. `evals/cases.py` — expand to 25–30 cases. New categories:
   - Parallel tool calls ("dim the lights and play jazz").
   - Disambiguation that _should_ trigger `ask_user` ("play Thriller" → song or album?).
   - Disambiguation that should _not_ trigger `ask_user` (clear requests). `forbidden_tools=["ask_user"]`.
   - Cancellation (utterance, then immediate cancel — agent loop should exit cleanly).
5. `evals/runner.py` outputs a CSV: `case_name, provider, model, success, tool_calls, tokens_in, tokens_out, latency_ms, cost_usd`.
6. Run the full suite across at least two models per provider (e.g., Claude Sonnet + Opus, GPT-5-mini + GPT-5). Commit the CSV.

**Discipline:**

- Resist adding more tools. The eval count grows, not the tool count.
- The `ask_user` system prompt language matters: explicit "use sparingly, prefer confident action with narration over asking." Without this, models over-ask.
- For each failure in the CSV, write one sentence in `DECISIONS.md` explaining what failed and why. Future-you needs this for the blog post.

**Stretch:**

- Add a **reasoning model** (e.g., o4, Claude with extended thinking) and compare tool-choice accuracy vs latency vs cost. This is interesting enough to be its own blog post.
- Implement **tool retry**: when a tool returns `is_error=True`, instrument whether the model recovers gracefully. Score the retry behavior in evals.
- Add a **third provider** (Gemini, via LiteLLM this time as a contrast point) and discuss the experience of using a library vs the hand-rolled adapters.

**Done when:** CSV exists with at least 25 cases × 4 model configurations. The disambiguation cases pass on at least one provider.

---

## Day 4: Observability + polish + edge cases

**Goal:** Sentry traces visible end to end. The hardest eval cases identified and addressed. The "interesting" data for the blog post is captured.

**Deliverables:**

1. `instrumentation.py` — Sentry init with `AnthropicIntegration` and `OpenAIIntegration`, `traces_sample_rate=1.0`, `send_default_pii=True`.
2. Manual instrumentation: wrap `run_turn` in a `gen_ai.invoke_agent` span; each tool execution in a `gen_ai.execute_tool` span with `gen_ai.tool.name`, `gen_ai.tool.input`, `gen_ai.tool.output`.
3. iPad Sentry SDK installed; trace ID propagation in WebSocket connect handshake. Verify in Sentry that you can see button-press → STT → backend agent → tools → TTS as one trace.
4. **Screenshot of the Sentry trace** — this is the single most important image in the blog post. Save it.
5. Add an STT-mangling test: simulate `SFSpeechRecognizer` mistranscribing "Coldplay" as "coal play" by injecting noisy utterances directly into eval cases. Measure whether the agent recovers via fuzzy lookup in the music tool. This is the "what failed and how I designed around it" section that interview committees actually care about.

**Discipline:**

- Sentry's PII setting is a real product decision. Write a paragraph in `DECISIONS.md` about when capturing prompts is acceptable and when it isn't.
- Resist optimizing latency. Measure it, report it. Optimizing comes after the blog post.

**Stretch:**

- Compare **Sentry vs Langfuse vs Laminar** on the same workload. The OpenTelemetry `gen_ai.*` conventions make this a side-by-side comparison rather than a re-instrumentation. Could be its own writeup.
- **Custom vocabulary biasing** via `SFSpeechRecognitionRequest.contextualStrings` populated with your actual music library. Measure improvement on the STT-mangling cases.
- **LLM-side STT correction**: pre-process the utterance through a cheap model that does fuzzy correction against known entities before the main agent loop sees it. Two-stage retrieval pattern.

**Done when:** A single Sentry trace shows the full iPad → backend → tools → response journey with tokens and costs annotated. The eval CSV has data points for at least one "agent recovers from bad STT" case.

---

## Day 5: Blog post + cleanup

**Goal:** Publishable writeup. Clean repo. Honest documentation of what didn't work.

**Deliverables:**

1. Blog post draft, following the outline written on Day 0. Sections likely include:
   - The architecture choice: backend agent, iPad tools, and why.
   - The hand-rolled adapter (and why I didn't reach for LiteLLM).
   - The `ask_user` pattern: disambiguation as just another tool.
   - Eval design and what the CSV revealed.
   - Cross-model comparison: concrete numbers.
   - What broke: STT mangling, ambiguous intent, latency surprises.
   - What I'd do differently / what's next.
2. README in the repo: setup instructions, how to run evals, the Sentry screenshot.
3. `FOLLOWUPS.md` cleaned up. Each item is a credible future post or sprint.
4. `DECISIONS.md` reviewed — anything embarrassing rewritten, but **leave the honest "this didn't work" entries in**. Those are the most valuable.

**Discipline:**

- Ship the blog post even if the code feels unfinished. A rough harness with a real eval and an honest writeup beats a polished harness with no writeup.
- Don't gold-plate the iPad UI. Two extra hours on a waveform animation is two hours not writing.

**Stretch:**

- **TTS comparison**: ElevenLabs vs OpenAI TTS vs Apple's `AVSpeechSynthesizer` on the same agent responses. Latency, voice quality, cost. Could carry a standalone post.
- **The "audio streaming API vs text route" fork** — implement the OpenAI Realtime API path in a branch, compare on the same eval suite. This is a substantial separate project; lock it in `FOLLOWUPS.md` for a future sprint.
- **Wake-word**: Porcupine integration for hands-free invocation. Mostly a UX thing, not architectural.
- **Persistent state**: For faster resumes on subsequent calls (e.g. volume up after changing the song).

**Done when:** The blog post is published. The repo is at a state another engineer could fork and run.

---

## Open questions / deferred decisions

These are explicitly _not_ answered in the sprint and should be parked in `FOLLOWUPS.md`:

- Persistent session storage (currently in-memory).
- Multi-user / auth.
- Connecting to the real Louie API (instead of fake state on the iPad).
- Tool versioning / capability discovery from iPad to backend dynamically.
- Cost optimization (prompt caching, model routing).
- MCP-compatible tool manifest format (worth converging on for portability — but not in the sprint).

---

## Local development notes

- **Modal hot-reload:** `modal serve main.py` for development. Iterates fast.
- **Sentry locally:** set `traces_sample_rate=1.0` during development, dial down later.
- **API costs:** running the full 30-case eval across 4 model configs probably costs €2–5. Don't optimize prematurely; do watch the dashboards.
- **WebSocket testing:** `websocat` is invaluable. Test the backend with a CLI client before touching the iPad.
- **iPad simulator works for everything except real microphone STT** — bring a physical iPad in early.
