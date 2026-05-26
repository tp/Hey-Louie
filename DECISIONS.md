# Decisions

## Tools over code execution

For now we focus only on providing tools to the agent, even though that obviously limits it capabilites.

## Add `search_music` tool

Split music into search_music + play_music. Reason: returning a query string from the agent leaves the lookup work undefined; a real client (Qobuz, Sonos) exposes search and play as separate calls. Bumps tool count from 5 to 6. Worth it: forces multi-step tool use into the eval suite from Day 1.

## `search_music` uses a tiny in-memory catalog, not synthesized IDs

`search_music` returns hits from a 3–5 entry catalog instead of synthesizing an ID from the query string. Synthesis is one line of code, but a catalog (a) lets Day 3 stage the "play Thriller — song or album?" disambiguation case cleanly without special-casing, and (b) makes the eventual swap to a real provider (Qobuz, Apple Music) a like-for-like replacement of the lookup table rather than a tool-shape change. Cost is ~10 lines of dict literals.

## `search_music` dispatches to the iPad too — not local

Claude originally assumed `search_music` would stay server-local because a real backend "would call a catalog API." That's wrong for this project: the music library only exists on the iPad (no server-side catalog API is in scope), so on Day 2 `search_music` flips to `local=False` alongside the four house tools. Every tool round-trips. This is actually a useful constraint to write about — it mirrors how Claude Code and similar coding agents work: the model runs on a server but every interesting tool (read file, run command, inspect state) executes on a physically-local machine that holds the real data. The agent harness pays a latency cost for that round-trip and earns the right to never see the data directly. Worth a paragraph in the blog post.

## Session is the agent ↔ tool-executor seam

The agent layer (`agent/loop.py`, `agent/session.py`) knows nothing about iPads, WebSockets, in-memory state, or catalogs. It interacts with one thing: a `Session` Protocol with `schemas()` and `dispatch_tool()`. Two implementations exist — `FakeLouie` (in `evals/fake_louie.py`, the in-memory reference impl used for evals) and `WebSocketSession` (in `transport/ws.py`, the production wire). Both inherit from `Session` explicitly with `@override` decorators so signature drift is a type error at definition time.

This was a real shift from the Day-1 design, which had a `ToolRegistry` with dual-mode `call()` branching on `if session is not None and not tool.local`. That mixed two concerns into one object and made the test-only catalog leak into production imports. The split: schemas + handlers + catalog all belong to "the executor" (whoever implements `Session`), not to the agent layer. The agent layer just routes calls.

Why this matters for the iPad seam: it lets tool discovery be client-driven. `WebSocketSession.schemas()` returns whatever the client sent in `hello.tools` — the backend has no static knowledge of the iPad's tool list. Adding a tool to the iPad is a client-only change. `FakeLouie` happens to hardcode its own copy (it IS a fake iPad), and the CONTRACT block in its docstring spells out that the iPad's `HeyLouieSchemas.swift` and `HeyLouieToolDispatcher.swift` must match.

## Server-side tools: deferred to a future sprint

The current loop only knows about client-mediated tools (those `session.dispatch_tool` routes to the iPad). Server-side tools — things the backend would execute itself, e.g. a code-exec sandbox, web fetch, or file ops — are out of scope for this sprint. There's a placeholder slot: `run_turn(..., extra_tools=())` joins server-side schemas into the model's tool list. When the first server-side tool lands, `run_turn` grows a `server_dispatch: Callable[[name, args, id], Awaitable[ToolResultBlock]]` parameter and the `gather()` over tool_uses tries it for any name in `extra_tools` before falling back to `session.dispatch_tool`. This is intentionally vapor for Day 2.

Worth flagging: `ask_user` is **not** a server-side tool — that was a mistake in early notes. It lives in the iPad's tool catalog and reaches the loop through `session.schemas()` like any other iPad tool. Its shape is `{question, choices: [{id, label}, ...]}` — the iPad displays a tap popover (not a re-record), the user picks one, the picked choice id comes back as `tool_result`. So disambiguation is "tap to clarify", not "re-record to clarify"; that's a meaningful UX choice worth mentioning in the blog post. The blog post should also not lump `ask_user` in with future server-side tools (sandbox/code-exec/web-fetch).

## WebSocket protocol: per-turn, message-typed, futures-by-tool-use-id

The iPad opens a fresh WebSocket per push-to-talk → response cycle (not held open across turns). On connect the client sends `hello` with its full `tools` schema list, then `utterance`. The backend kicks off `run_turn`, and every tool call goes out as a `tool_call` message; the iPad answers with `tool_result` (matched by `tool_use_id`); the loop terminates with `final_text`, after which the server closes. `cancel` from either side aborts the in-flight turn.

Per-turn rather than per-session because (a) Modal autoscales per second — a persistent socket pins one container per user — and (b) bidirectional traffic only matters _during_ a turn; between turns there's nothing to push. The trade-off is cold-start latency on follow-ups ("turn it up" 3s after "play Coldplay"); the answer is session-scoped server state (Modal Dict or similar) keyed by session id, hydrated on each new WS — parked in [[followups]] for week two.

Tool calls fan out in parallel via `asyncio.gather`, so the WebSocket can have multiple concurrent sends. A per-session `asyncio.Lock` around the send call serializes them; `tool_use_id` lets the iPad and backend correlate without ordering guarantees. Pending tool dispatches live in `Session.pending: dict[str, asyncio.Future[ToolResultBlock]]`.

Message types — client → server: `hello`, `utterance`, `tool_result`, `cancel`. Server → client: `tool_call`, `final_text`, `error`. Everything is JSON text frames. The shape is locked here so the iPad agent and backend agent don't drift.

## Day 3 sweep: Sonnet 4.6 vs GPT-5-mini, 23 cases, all pass

First full cross-provider sweep landed at 46/46 green (Anthropic Sonnet 4.6 + OpenAI GPT-5-mini, see `evals/results.csv`). The all-pass result was suspicious enough to re-verify the assertions — they're load-bearing: forbid `ask_user` on 17 cases, demand it on 1 (`thriller_disambig`), demand the picked id be acted on, demand no `play_music` after an empty search, demand graceful "can't help" narration on `outside_scope_weather`. The system prompt's "use ask_user sparingly + pick a default and narrate" clause is doing real work — earlier prompt variants over-asked on `play_album_thriller_explicit` and `play_artist_queen`. Worth a paragraph in the blog post about how much prompt sensitivity disambiguation cases have.

Cross-provider observations from the CSV (these are the bits the blog post can cite without picking winners):

- **Cost gap is ~10-25×.** Sonnet 4.6 ran ~$0.015-0.033 per case; GPT-5-mini ~$0.001-0.003. Driven by input pricing (Sonnet $3/Mtok vs GPT-5-mini $0.25/Mtok) and the fact that even simple cases burn ~5k input tokens once the tool catalog + system prompt are loaded.
- **GPT-5-mini emits 2-6× more output tokens on cases with non-obvious reasoning.** The starkest is `climate_fahrenheit` (68°F → 20°C conversion): 547 output tokens vs Sonnet's 89. Looks like chain-of-thought leaking into the response, even though our adapter doesn't enable a `reasoning=` mode. Worth investigating Day 4 whether `reasoning={"effort":"minimal"}` brings it in line; the cost impact today is ~10× the smallest cases.
- **Empty-search recovery differs.** On `no_match_lady_gaga` (catalog has no Lady Gaga), Sonnet 4.6 called `search_music` twice (likely with a varied query); GPT-5-mini called it once and gave up. Both narrated "couldn't find" correctly, but Sonnet's retry is the kind of behavior worth a real Day-4 STT-mangling case — does the retry surface a fuzzy-corrected query that succeeds?
- **Latency is roughly comparable** (~3-6s for simple cases, 6-10s for parallel/disambig), with GPT-5-mini occasionally slower on long-output cases. Network noise dominates the variance.

What this sweep doesn't measure yet (Day 4 territory): whether the parallel cases actually dispatched tools in one assistant message vs serialized into multiple turns. The `tools_called` column is order-preserving but doesn't expose the message boundary. Adding `steps` to the CSV would catch this — parked.

## Sentry PII: prompts + responses captured during the sprint

Both LLM integrations are initialized with `include_prompts=True` and the SDK with `send_default_pii=True` (see `backend/instrumentation.py`). That means every captured event — `gen_ai.chat` spans, `gen_ai.invoke_agent` spans, errors — carries the full system prompt, user utterance, assistant text, and tool inputs/outputs. The manual spans we added in Day 4 also set `gen_ai.prompt` and `gen_ai.response.text` directly.

This is acceptable now because the project is a solo build, the data is the developer's own home commands ("turn on the kitchen lights"), and `traces_sample_rate=1.0` means a small number of events go to a private Sentry project. It is **not** acceptable for any deployment serving other users without first deciding what to do about: (a) household members other than the developer being recorded; (b) accidental capture of sensitive utterances ("set an appointment with Dr. Smith…" if the tool catalog ever grows); (c) cross-organization Sentry views where engineers from unrelated projects can read traces.

The right production answer has three layers, in order of cost:
- **`before_send` / `before_send_transaction` scrubbing** to redact `gen_ai.prompt` and `gen_ai.response.text` before egress — keep the structural data (tokens, latencies, tool names) without the payloads.
- **Per-environment gating**: `include_prompts=True` only when `SENTRY_ENVIRONMENT == "development"`.
- **Sentry's PII data scrubbing rules** applied server-side as a defense in depth.

Parked as a follow-up; the screenshot the Day-4 blog post wants is more valuable with prompts visible, and the production hardening is well-understood enough to write up later without re-discovering it.

## STT mangling: model retry beats tool-side fuzziness

Day 4 added three STT-mangled eval cases (`stt_mangle_queen`, `stt_mangle_thriller_song`, `stt_mangle_miles_davis`) — utterances like "put on some kween" that the catalog's exact-substring search returns `[]` for on first try. The cases ask: does the agent recover?

**Baseline (no prompt change), both models recovered 2/3 — but on different cases.** Sonnet failed on Thriller by *narrating* a confirmation question ("did you mean Thriller by Michael Jackson?") — the model knew the answer but chose to ask via text instead of acting. GPT-5-mini failed on Queen by giving up after one empty search. Both 2/3 but the failure modes are completely different — that's the cross-model story the blog post wants.

Considered three tool-side responses and rejected all of them:

1. **Explicit `alt_tokens` per catalog entry.** Hand-code "thrill her" → Thriller. Doesn't scale: real catalogs are 10M+ entries and STT errors are open-ended.
2. **Phonetic / Levenshtein matching in the tool.** Adds a dep and introduces false-positive risk on no-match cases like `no_match_lady_gaga`.
3. **LLM-side correction pre-pass.** A cheap second model rewrites the utterance against the catalog. Powerful, but a separate model call per turn — parked for a future post.

The real layers for a production system are **STT-side biasing** (`SFSpeechRecognitionRequest.contextualStrings` with the user's top-N artists, capped at the OS's ~50K limit) and **model-side world knowledge** (the model already knows "Coldplay" is a band — let it correct).

**What Day 4 shipped is the model-side fix as a system-prompt clause** (`agent/loop.py`):

> If search_music returns no hits and the request looks like an entity name, retry once with a phonetically similar query before giving up. Never offer alternatives in narration — act on your best interpretation, use ask_user, or say you couldn't find it.

This is the third version of the clause. The CSVs in `backend/evals/` (`results_stt_baseline.csv`, `results_stt_with_examples.csv`, `results_stt_minimal.csv`, plus the live `results.csv`) trace the ablation:

1. **First draft (with leaked examples).** Included literal `'kween' for 'Queen', 'thrill her' for 'Thriller'` inside the prompt. 6/6 passed — but the test was circular. The model wasn't generalizing; it was looking up the answer in its own context window. The eval cases used exactly those phrases. Caught by the user, not by the suite.
2. **Stripped version.** Removed all specific examples, the leading "real artist/song/album" categorization, and the explicit "did you mean X?" prohibition. Just "retry once with a corrected spelling." Still 6/6 passed — examples were decorative for correctness — but **GPT-5-mini's output tokens jumped ~50%** (351-664 vs 211-371) and **latency roughly doubled** (8-17s vs 5-10s). Sonnet was unchanged. The few-shot examples were an inference-time optimization for the smaller model, not a correctness aid. Worth its own paragraph in the blog post: when an ablation looks like "no regression," check the cost columns.
3. **Final version (current).** Added the failure-mode guidance back, abstracted: "Never offer alternatives in narration — act on your best interpretation, use ask_user, or say you couldn't find it." Three legitimate exits, no fourth. Also reframed "corrected spelling" → "phonetically similar query" — STT errors are phonetic, not orthographic, and the model needs to think about sound, not letters. 6/6 still pass; the test is no longer self-fulfilling.

**Bonus the clause didn't directly ask for.** Sonnet started preemptively searching with the phonetically corrected spelling on the first try, skipping the empty-result round-trip entirely (`search → play`, 2 tools). GPT-5-mini does the literal retry pattern (`search → search → play`, 3 tools) on most cases. The clause taught a broader generalization than its literal text, but only to Sonnet.

**Cost summary across the six cases on the final prompt.** Sonnet: ~$0.023/case, ~135-140 output tokens, mostly 6s with one 12s outlier. GPT-5-mini: ~$0.002/case, 221-555 output tokens, 6-12s. The output-token range on GPT-5-mini is much wider than Sonnet's — reasoning leakage on the cases requiring real correction.

**Risk we did not regression-test:** `no_match_lady_gaga`. Catalog truly has no Lady Gaga; the new clause might tempt the model into a phantom retry instead of graceful narration. Worth a full-suite re-run before publishing the blog post.

**Blog-post angles** from this ablation alone:

- The "tool fix" was prompt engineering, not code. The model already had the knowledge; the prompt just had to give it permission to retry.
- Few-shot examples in a system prompt are sometimes inference-time optimizations, not correctness aids. Stripping them passed all tests but spiked cost on the smaller model. The CSV table is the artifact: "no regression on green, but the latency column tells the real story."
- Models have different self-corrective defaults. Sonnet preempts the bad query; GPT-5-mini retries literally. The cost and latency differences are visible in the tool-call patterns, not just the totals.
- An eval where the prompt names the test inputs is theatre. Worth saying out loud — easy to fall into, easy to miss without a careful reader.

## `play_music` looks up the title in the catalog, not a session registry

The ID returned by `search_music` is shaped `$id:<type>:<slug>` (e.g. `$id:genre:jazz`). `play_music` validates the id against the catalog and uses the canonical title stored there — it does NOT carry a session-scoped registry of prior `search_music` results, and it does NOT parse the slug to reconstruct the title (an earlier attempt that gave approximate results like `bohemian-rhapsody` → `Bohemian Rhapsody`). Reason: matches how the real iPad will behave — the device holds the library and can resolve any valid id to its display title regardless of which queries preceded it, so the backend's mock should behave the same. `FakeLouie.music` stores both `now_playing_id` (stable, asserted in evals) and `now_playing_title` (human-facing, read back by `query_state`). The pair is always set together.
