# iPad Day 4 — Sentry SDK and end-to-end trace propagation

Instructions for the Claude agent working in [`tp/Louie`](https://github.com/tp/Louie) on the `hey-louie` branch (PR #5). Continues from `IPAD_DAY_3.md`.

## Context — what just shipped on the backend

Day 4 added manual Sentry spans to the agent loop (`backend/agent/loop.py`):

- A `gen_ai.invoke_agent` span wraps each `run_turn` call, attributing the model, agent name, prompt, response text, token totals, and `stop_reason`.
- A `gen_ai.execute_tool` span wraps each `session.dispatch_tool` call, attributing `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.input`, `gen_ai.tool.output`, and `is_error`.

The Anthropic + OpenAI integrations already auto-instrument every model call as a `gen_ai.chat` span (with the LLM call's full HTTP request as a `http.client` grandchild), so the backend tree looks like:

```
websocket.server /agent          (auto, FastAPI/Starlette integration)
└── gen_ai.invoke_agent louie    (manual, this sprint)
    ├── gen_ai.chat              (auto, AnthropicIntegration)
    │   └── http.client          (auto, urllib3 / httpx)
    ├── gen_ai.execute_tool ...  (manual, this sprint)
    └── gen_ai.chat              (auto)
        └── http.client
```

Your job for Day 4 is to extend that trace **upward** — into the iPad — so a single Sentry trace covers the full press-to-talk → TTS cycle. By the end, pressing the push-to-talk button produces one trace whose root lives on the iPad and whose descendants include every LLM call and tool dispatch above.

**Do not** touch the production Linn code or any of the Day-2/3 Hey-Louie wiring beyond the additions below. The voice agent's behavior must be unchanged; only observability is added.

## 1. Sentry SDK install — Swift Package Manager

Add the [Sentry Cocoa SDK](https://github.com/getsentry/sentry-cocoa) via SPM:

- Repository: `https://github.com/getsentry/sentry-cocoa`
- Version rule: "Up to Next Major" from `8.49.0` (or whatever the latest 8.x is at the time of work — pin the major).
- Target: add `Sentry` to the iPad app target only (not to any Swift package targets — keep the dependency at the app boundary).

After installing, `import Sentry` must compile in `Louie/`.

## 2. Initialize at app launch — `LouieApp.swift` (or wherever `@main` lives)

In the app entry point's `init()`, before any voice-agent code runs:

```swift
import Sentry

@main
struct LouieApp: App {
    init() {
        SentrySDK.start { options in
            options.dsn = ProcessInfo.processInfo.environment["SENTRY_DSN"]
                ?? Bundle.main.object(forInfoDictionaryKey: "SentryDSN") as? String
                ?? ""
            options.environment = "development"  // flip to "production" for Modal-deployed backend
            options.tracesSampleRate = 1.0       // dial down later; during the sprint we want every trace
            options.sendDefaultPii = true        // matches the backend; see Hey-Louie/DECISIONS.md
            // Auto-instrument URLSession HTTP traffic. The WS upgrade goes through URLSession,
            // so this will set sentry-trace + baggage on the upgrade request automatically
            // IF the URL matches tracePropagationTargets below.
            options.enableNetworkTracking = true
            // Match localhost + Modal deployment. Without this, headers are not propagated
            // (default policy is conservative).
            options.tracePropagationTargets = [
                "localhost",
                "127.0.0.1",
                "modal.run",  // adjust to the deployed host if different
            ]
        }
    }
    // ...existing body...
}
```

**DSN handling.** The DSN is project-specific — get it from the Sentry project the user just created. Read precedence: `SENTRY_DSN` env var first (useful for sim builds via scheme env), then a `SentryDSN` key in `Info.plist`, then empty (which disables the SDK gracefully). **Do not** hardcode the DSN in source. Put it in `Info.plist` for device builds, scheme env for sim, and add the `Info.plist` line to the local-only override list if there's an `.gitignore`/sample-config pattern in the project.

**Why `sendDefaultPii = true`.** Matches the backend (`backend/instrumentation.py`). It captures the user utterance, tool inputs, and tool outputs in the trace, which is what makes the screenshot meaningful. Hey-Louie/DECISIONS.md "Sentry PII: prompts + responses captured during the sprint" documents the boundary for production deployment — read that before flipping `environment` to production.

## 3. Wrap each push-to-talk turn in a transaction

The agent invocation is owned by the backend (`gen_ai.invoke_agent` is its op). The iPad's transaction represents a superset: STT + WS round-trip (which includes the agent invocation) + TTS. Use a distinct op so the Sentry AI Agents dashboard doesn't double-count.

Find the controller that owns one turn — the WebSocket-driven `VoiceAgent` controller introduced in Day 2 (replaces `VoiceAgentDummy`). Wherever a turn begins (push-to-talk released → STT final transcript available → just before opening the WebSocket), start the transaction. End it after TTS finishes speaking the final response (or on cancel/error).

```swift
import Sentry

final class VoiceAgentController {
    // Held for the lifetime of one turn. nil between turns.
    private var turnTransaction: (any Span)?

    func startTurn(utterance: String) {
        // op identifies the kind of work; name is per-turn human-readable.
        let tx = SentrySDK.startTransaction(
            name: "voice turn",
            operation: "app.voice_turn",
            bindToScope: true  // <- critical: makes URLSession auto-instrumentation see this as the active span
        )
        tx.setData(value: "louie", key: "gen_ai.agent.name")
        tx.setData(value: utterance, key: "gen_ai.prompt")
        turnTransaction = tx
        // ...kick off the WS connect from here, see §4...
    }

    func finishTurn(finalText: String?, error: Error? = nil) {
        guard let tx = turnTransaction else { return }
        if let finalText { tx.setData(value: finalText, key: "gen_ai.response.text") }
        if let error {
            tx.setData(value: String(describing: error), key: "error")
            tx.finish(status: .internalError)
        } else {
            tx.finish()
        }
        turnTransaction = nil
    }
}
```

**`bindToScope: true` is load-bearing.** Without it, the transaction exists but is not the "active" span on the current scope, which means the URLSession auto-instrumentation won't pick it up as the parent when injecting `sentry-trace` headers. The result would be: WS upgrade gets headers, but they describe an orphan trace, not this transaction. Test this assumption first — if it doesn't work, swap to a manual `serializeTraceContext()` approach (see §4 fallback).

**Lifecycle.** `finishTurn` must be called on every exit path: `final_text` received and TTS done, `cancelled` received, WS error, `TurnCancelled` raised on the backend, app backgrounded mid-turn. A turn transaction that never finishes leaks memory and never lands in Sentry. Use `defer` around the whole `startTurn` body if that's idiomatic for the controller.

## 4. Propagate to the backend via WebSocket upgrade headers

Trace propagation works by setting two headers on the outgoing HTTP request: `sentry-trace` (the trace context) and `baggage` (sampling + extra context). The backend's FastAPI/Starlette integration reads these on the incoming request and continues the trace.

`URLSessionWebSocketTask` accepts a custom `URLRequest`, and URLSession's Sentry instrumentation will inject the two headers automatically when `tracePropagationTargets` matches the URL and there's an active scope-bound transaction. So the path is:

```swift
// In the controller, after startTurn() (so the transaction is active on scope):
let backendURL = URL(string: "ws://localhost:8000/agent")!  // or the deployed Modal URL
let request = URLRequest(url: backendURL)
// Do NOT manually inject sentry-trace / baggage here — the URLSession integration does it
// as long as: (a) the URL host matches tracePropagationTargets, and (b) §3 used bindToScope: true.

let task = urlSession.webSocketTask(with: request)
task.resume()
```

**Verify propagation worked.** Run the backend locally with `LOG_LEVEL=debug` or temporarily print `request.headers.get("sentry-trace")` in `transport/ws.py`'s `agent_ws` handler. You should see a value like `<32-hex>-<16-hex>-1`. If empty, see fallback below.

**Fallback if auto-injection doesn't work.** Some SDK versions / URLSession configurations skip WebSocket upgrade requests when injecting headers. Manual fallback — extract the headers from the active span and set them yourself:

```swift
// Before creating the URLRequest:
var request = URLRequest(url: backendURL)
if let span = SentrySDK.span {  // the active turn transaction (because bindToScope: true)
    let traceHeader = span.toTraceHeader().value()
    request.setValue(traceHeader, forHTTPHeaderField: "sentry-trace")
    if let baggage = PrivateSentrySDKOnly.getBaggageHttpHeader(span: span) {
        request.setValue(baggage, forHTTPHeaderField: "baggage")
    }
}
```

`PrivateSentrySDKOnly` is documented Sentry-internal but stable API used by their own integrations. If reluctant to use it, omit the `baggage` header — `sentry-trace` alone is enough for trace continuity (you lose sampling decisions across services, but the trace tree connects).

## 5. Optional — child spans for STT and TTS

Worth doing for the blog-post screenshot: explicit STT and TTS spans surface where the human-perceived latency actually lives (often >50% of a turn is STT + TTS, not the LLM). Add as children of the turn transaction:

```swift
// In VoiceCapture or wherever SFSpeechRecognizer's final transcript callback fires:
let sttSpan = SentrySDK.span?.startChild(operation: "stt", description: "SFSpeechRecognizer")
// ...start recognition, await final transcript...
sttSpan?.setData(value: transcript, key: "stt.transcript")
sttSpan?.setData(value: SFSpeechRecognizer.supportsOnDeviceRecognition ? "on-device" : "server", key: "stt.mode")
sttSpan?.finish()

// In TTS, around the AVSpeechSynthesizer call:
let ttsSpan = SentrySDK.span?.startChild(operation: "tts", description: "AVSpeechSynthesizer")
// ...speak, await delegate finish...
ttsSpan?.setData(value: text, key: "tts.text")
ttsSpan?.finish()
```

These render as siblings of the `websocket.client` span on the iPad and give the blog post a much sharper "where the time goes" story.

## 6. Tool-call attribution (skip for Day 4 — note for later)

When the backend dispatches a tool over the WebSocket, the iPad's tool execution time is currently subsumed into the backend's `gen_ai.execute_tool` span (which spans the `await` on the future). For the Day-4 screenshot, that's fine.

If you ever want each iPad tool execution as its own span — making local catalog lookups visible — the protocol would need to carry trace context on `tool_call` and `tool_result` frames. Out of scope for this sprint; mention to the user only if they ask.

## 7. Verify end-to-end

1. Backend running locally (`uv run fastapi dev backend/main.py` in the Hey-Louie repo) with `SENTRY_DSN` set in `.env`.
2. iPad app built with the `SENTRY_DSN` matching the **same Sentry project** as the backend (separate project = separate trace storage; the link won't show even if propagation works).
3. Press push-to-talk, say "play some jazz", wait for the spoken response.
4. Open Sentry → Traces, find the most recent trace. Expected shape:
   - Root: `app.voice_turn` "voice turn" with `gen_ai.prompt = "Play some jazz."`
   - Children include: `stt`, `websocket.client` (if §5 done) — and a `websocket.server /agent` from the backend appears nested under the WS client span by virtue of the propagated trace.
   - Under `websocket.server`: `gen_ai.invoke_agent` → 2-3× `gen_ai.chat` and 2× `gen_ai.execute_tool`.
   - Total root duration ≈ STT + WS round-trip + TTS — visibly larger than the backend's `gen_ai.invoke_agent` span.

If the iPad transaction and backend transaction show as **separate traces** (different `trace_id`s), the headers didn't propagate. Debug order:
1. Add a `print(ws.headers.get("sentry-trace"))` at the top of `agent_ws` in `backend/transport/ws.py` and reproduce. Empty = iPad side problem (§4 fallback).
2. If the header is present but backend still creates a fresh trace, the Starlette integration isn't continuing on WS upgrades. Fix on the backend by manually wrapping `agent_ws` with `sentry_sdk.continue_trace(...)` — coordinate with the Hey-Louie repo.

## 8. Test plan

Manual, on a physical iPad with backend on the same network:

- [ ] App launches without Sentry SDK crashes; `SentrySDK.isEnabled` returns true.
- [ ] Backend `/sentry-debug` endpoint hit from the iPad triggers a Sentry error event in the backend project (sanity check the DSN config).
- [ ] One push-to-talk turn ("play some jazz") produces a single trace in Sentry with iPad + backend spans connected.
- [ ] The `gen_ai.prompt` attribute on the root span matches the spoken transcript.
- [ ] A turn that uses `ask_user` (e.g. "play Thriller") still produces one connected trace; the popover tap latency is visible inside the backend's `gen_ai.execute_tool ask_user` span.
- [ ] A cancelled turn (press button mid-response) produces a trace where the root span is finished with status `cancelled` and the backend's `gen_ai.invoke_agent` shows partial token totals.
- [ ] STT and TTS spans (if §5 implemented) bookend the WS work and account for the visible latency gap.

## Acceptance for Day 4

A Sentry trace screenshot exists showing the full press-to-talk → TTS cycle as one tree with both iPad and backend spans. Save the screenshot to `Hey-Louie/blog/img/` (or wherever the blog post assets land) — it's the centerpiece image for the Day-5 writeup.

What's deliberately deferred:

- Per-tool spans on the iPad (§6).
- Sampling / PII redaction for production (`include_prompts`, `sendDefaultPii`) — covered in `Hey-Louie/DECISIONS.md` and parked.
- Replacing `app.voice_turn` with a more conventional op name — the Sentry AI Agents dashboard currently buckets on `gen_ai.invoke_agent`, and the backend already owns that op, so we deliberately differ.
