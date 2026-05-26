# iPad Day 3 — `ask_user` disambiguation

Instructions for the Claude agent working in [`tp/Louie`](https://github.com/tp/Louie) on the `hey-louie` branch (PR #5). Continues from `IPAD_DAY_2.md`.

## Context — what just shipped on the backend

Day 3 added one client-mediated tool to the iPad's contract: **`ask_user`**. The backend's reference impl is in `backend/evals/fake_louie.py` (search for `ask_user`); the new eval case `thriller_disambig` exercises it end-to-end against both Anthropic and OpenAI.

Why this matters for the iPad: the backend agent now sometimes emits a `tool_call` with `name: "ask_user"` mid-turn. The iPad must (a) render a tap popover with the choices, (b) wait for the user to pick one, (c) reply with a `tool_result` carrying the picked `{id, label}`. The agent loop then continues with whatever follow-up it had planned (typically `play_music` with the picked id).

This is **client-mediated**, not voice. The user disambiguates by tapping, not by re-recording — that decision is documented in `Hey-Louie/DECISIONS.md` ("WebSocket protocol: per-turn..."). Speaking the question aloud via TTS while the popover is up is a nice-to-have but not required for the Day-3 milestone.

**No new tools.** The five Day-2 tools (`search_music`, `play_music`, `control_lights`, `set_climate`, `query_state`) plus `ask_user` is the full Day-3 catalog. Resist the urge to add anything; that constraint is the whole point of the sprint discipline.

## 1. Schema — `Louie/HeyLouieSchemas.swift`

Append this entry to the `LOUIE_TOOL_SCHEMAS` array. Description text matters — the model picks tools off the description, and the language here is tuned (in concert with the backend's system prompt) to prevent over-asking. Copy verbatim:

```swift
LouieToolSchema(
    name: "ask_user",
    description: """
        Ask the user to disambiguate between concrete options when their request is \
        genuinely ambiguous AND picking the wrong default would noticeably annoy them. \
        The user sees a tap popover (not a re-record) with the choices you provide; the \
        tool_result is the picked {id, label}. USE SPARINGLY — prefer confident action \
        with a one-sentence narration over asking. Never ask about which room or what \
        temperature; pick a sensible default and say what you did. Only call this when \
        (a) two or more plausible interpretations exist (e.g. 'play Thriller' → song or \
        album?) AND (b) no prior tool result already resolves the ambiguity. The `id` \
        strings you supply MUST be tokens that make sense for your follow-up action — \
        typically values returned from a previous tool call (e.g. search_music hit ids), \
        not free-form strings.
        """,
    inputSchema: [
        "type": "object",
        "properties": [
            "question": [
                "type": "string",
                "description": "Short, spoken-aloud-friendly question. No markdown, no preamble like 'sure!'. Examples: 'The song or the album?', 'Which Coldplay album?'."
            ],
            "choices": [
                "type": "array",
                "minItems": 2,
                "maxItems": 5,
                "description": "2-5 distinct options the user can tap.",
                "items": [
                    "type": "object",
                    "properties": [
                        "id": [
                            "type": "string",
                            "description": "Opaque token to act on after the tap (e.g. a search_music id)."
                        ],
                        "label": [
                            "type": "string",
                            "description": "Short human-facing label, 1-4 words."
                        ]
                    ],
                    "required": ["id", "label"]
                ]
            ]
        ],
        "required": ["question", "choices"]
    ]
)
```

Source of truth: `Hey-Louie/backend/evals/fake_louie.py`, search for `name="ask_user"`. If they ever drift, **the iPad copy wins in production** (the backend trusts whatever the client sent in `hello.tools`) but eval results would silently degrade — keep them in sync.

## 2. Dispatcher — `Louie/HeyLouieToolDispatcher.swift`

Add a handler that turns an incoming `tool_call` (`name: "ask_user"`) into a popover and waits for the user's tap. Sketch:

```swift
func dispatchAskUser(args: [String: Any]) async throws -> String {
    guard let question = args["question"] as? String,
          let rawChoices = args["choices"] as? [[String: Any]] else {
        throw HeyLouieError.badArgs("ask_user requires question + choices")
    }
    let choices: [AskUserChoice] = try rawChoices.map { raw in
        guard let id = raw["id"] as? String, let label = raw["label"] as? String else {
            throw HeyLouieError.badArgs("each choice needs id + label")
        }
        return AskUserChoice(id: id, label: label)
    }

    // Hand off to the overlay; suspend until the user taps.
    let picked = await voiceAgentState.presentAskUser(question: question, choices: choices)

    // tool_result content is JSON: {"id": "...", "label": "..."}
    let payload = ["id": picked.id, "label": picked.label]
    return try String(data: JSONSerialization.data(withJSONObject: payload), encoding: .utf8) ?? "{}"
}
```

The returned string is the `content` field of the outgoing `tool_result` frame (see `IPAD_DAY_2.md §1`). It's a JSON object with `id` and `label` — the model uses `id` to act and may narrate `label` (`"Playing the Thriller album."`).

**Error path:** if `presentAskUser` is dismissed without a pick (user taps outside, cancels the turn, app backgrounds), reply with `tool_result(is_error: true, content: "user dismissed the popover")`. The model can then narrate "Okay, never mind." or similar. **Do not** invent a default pick — that defeats the purpose.

## 3. UI — wire `VoiceAgentOverlay`'s popover

`Louie/VoiceAgent.swift` already has the popover UI from Day 2 (mentioned in IPAD_DAY_2.md §0 — `ask-user popover`). What's left:

1. Add a `presentAskUser(question:choices:) async -> AskUserChoice` method on the `VoiceAgentState` (or wherever the popover state lives). It sets the popover's visible state, suspends via a continuation, resumes when the user taps.
2. The popover renders choices as buttons; tapping one resolves the continuation with the picked choice and dismisses.
3. If the user dismisses without picking, resume with `nil` (and have the dispatcher convert that into a `tool_result(is_error: true)` per §2).

**Optional polish, can skip for Day 3:** TTS-speak `question` while the popover is up via the existing `VoiceCapture` TTS path. Useful for the demo video; not required for the milestone.

## 4. Cancel interaction

Backend already supports cancel mid-turn (Day 3 added `cancel_token` to `run_turn`; the WebSocket layer will wire that to the `cancel` message in production). For the iPad:

- If the user presses push-to-talk while the popover is up, treat that as **dismiss the popover + send `cancel` on the WebSocket**. The agent loop will raise `TurnCancelled`; the server sends back `cancelled` and closes.
- The popover should not survive a turn boundary — when the WebSocket closes (success or cancel), any open popover dismisses.

## 5. Test plan

Manual, on a physical iPad:

1. **Happy path — disambiguation works.** Press push-to-talk, say "Play Thriller." Expect: popover appears with at least two choices ("the song" / "the album"). Tap "the album". Expect: TTS says something like "Playing Thriller." and the fake-state debug view shows `nowPlayingId == "$id:album:thriller"`.
2. **Happy path — clear request doesn't ask.** Say "Play some jazz." Expect: no popover; jazz starts playing; `nowPlayingId == "$id:genre:jazz"`.
3. **Dismiss.** Trigger disambiguation (step 1), then tap outside the popover. Expect: TTS narrates a graceful "never mind" or similar; no playback change; WebSocket closes cleanly.
4. **Cancel via push-to-talk.** Trigger disambiguation, then push-to-talk while popover is up. Expect: popover dismisses, agent loop cancels, no playback change.

Cross-check against the backend eval `thriller_disambig` — that case asserts the same end-to-end behavior with a scripted picker. If the iPad path diverges from the eval's outcome, the schemas or dispatcher contract are out of sync.

## 6. What NOT to do

- Don't add a sixth client-mediated tool. The catalog is locked for the sprint.
- Don't render the popover as a re-record (voice answer) — the decision in `DECISIONS.md` is taps-not-voice; voice for follow-up has latency + STT-accuracy issues that are out of scope.
- Don't try to be clever about "the model probably wants X" and auto-pick. The whole point is letting the user decide.
- Don't widen the contract by sending `null` ids, free-form strings outside the catalog, etc. The dispatcher's follow-up `play_music` would reject non-catalog ids anyway (the backend's fake handler errors on unknown ids; the real iPad music player would too).

## 7. Definition of done

- `HeyLouieSchemas.swift` contains the `ask_user` entry; the iPad sends it in `hello.tools` on every WebSocket connect.
- `HeyLouieToolDispatcher.swift` handles `ask_user` calls and routes to `VoiceAgentState.presentAskUser`.
- The popover from Day 2 is wired and dismisses on tap with the picked choice round-tripped as `tool_result`.
- Test plan §5.1 and §5.2 pass on a real iPad.
- Steps 5.3 and 5.4 (dismiss + cancel) pass at least once; minor wording quibbles are fine.

Anything else — TTS-speaking the question, animations, multi-choice ergonomics, accessibility polish — goes in `FOLLOWUPS.md` on the iPad side and is fair game for a future sprint.
