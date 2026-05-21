# iPad Day 2 — Voice agent over WebSocket

Instructions for the Claude agent working in [`tp/Louie`](https://github.com/tp/Louie) on the `hey-louie` branch (PR #5).

## Context

That branch already added:

- `Louie/VoiceAgent.swift` — `VoiceAgentState`, `VoiceAgentEvent`, the overlay UI (`VoiceAgentOverlay`, push-to-talk button, ask-user popover), and a placeholder controller `VoiceAgentDummy` that echoes the transcript via TTS.
- `Louie/VoiceCapture.swift` — push-to-talk STT (`SFSpeechRecognizer`, on-device when supported) + TTS (`AVSpeechSynthesizer`).
- `Louie/ContentView.swift` — integrates `VoiceAgentOverlay(state: voiceAgent.state, onEvent: voiceAgent.handle)` next to the existing `PlayerBar`.
- `Louie/Info.plist` — microphone, speech-recognition, and local-network usage descriptions.

Your job for Day 2 is to **replace `VoiceAgentDummy` with a real WebSocket-driven controller** that talks to the Hey-Louie backend, executes tool calls against a local fake state, and speaks the final response.

The backend is the protagonist (per Hey-Louie `PLAN.md`). The iPad here is intentionally dumb — no real Linn integration, no music library lookups against Linn. A fake state holds the five subsystems the backend's tools touch.

**Do not touch the production Linn code** (`Packages/Linn`, `HomeView`, `LibraryBrowser`, `PlayerBar`, etc.). Day 2 is additive on top of the voice-agent files.

## 1. WebSocket protocol (must match exactly)

One WebSocket per push-to-talk turn. Opens, exchanges messages, closes. URL: `ws://<backend-host>:8000/agent` for local dev (backend run with `uv run fastapi dev backend/main.py`); the deployed Modal URL for prod.

All frames are JSON text. Message types:

**Client → Server**

```json
{"type": "hello", "session_id": "<uuid-string>", "tools": [{"name": "search_music", "description": "...", "input_schema": {...}}, ...]}
{"type": "utterance", "text": "play some jazz"}
{"type": "tool_result", "tool_use_id": "<id from tool_call>", "content": "<stringified JSON>", "is_error": false}
{"type": "cancel"}
```

The `tools` array is the iPad's full tool catalog — name, description, and a JSON Schema for the arguments. The backend treats it as the source of truth; whatever you send is what the model sees. This means **adding a new iPad-side tool is a client-only change** (no backend deploy needed) as long as your handler matches the schema you declared.

**Server → Client**

```json
{"type": "tool_call", "tool_use_id": "<opaque>", "name": "search_music", "input": {"query": "jazz"}}
{"type": "final_text", "text": "Playing jazz."}
{"type": "cancelled"}
{"type": "error", "message": "..."}
```

Server closes the socket after `final_text` / `error` / `cancelled`. Client may close any time; server treats that as cancel.

## 2. Fake state model — `Louie/HeyLouieFakeState.swift` (new file)

Holds music, lights, climate. Lives only on the iPad. Use `@Observable` so the debug view re-renders.

```swift
import Foundation
import Observation

@MainActor
@Observable
final class HeyLouieFakeState {
    enum Room: String, CaseIterable, Sendable {
        case livingRoom = "living_room"
        case kitchen
        case bedroom
    }

    // Music
    var nowPlayingId: String? = nil
    var nowPlayingTitle: String? = nil
    var isPlaying: Bool = false
    var volume: Int = 50

    // Lights & climate, keyed by Room
    var lightOn: [Room: Bool] = Dictionary(uniqueKeysWithValues: Room.allCases.map { ($0, false) })
    var lightBrightness: [Room: Int] = Dictionary(uniqueKeysWithValues: Room.allCases.map { ($0, 100) })
    var targetC: [Room: Double] = Dictionary(uniqueKeysWithValues: Room.allCases.map { ($0, 20.0) })
}
```

## 3. Music catalog — `Louie/HeyLouieCatalog.swift` (new file)

The music library only exists on the iPad. Ship this exact catalog so `search_music` / `play_music` behavior matches the backend's reference implementation. The id format is `$id:<type>:<slug>` — opaque to the model, validated by `play_music`.

```swift
import Foundation

enum HeyLouieCatalog {
    struct Entry: Sendable {
        let id: String
        let type: String   // "artist" | "album" | "genre" | "playlist" | "track"
        let title: String
        let tokens: [String]
    }

    static let entries: [Entry] = [
        .init(id: "$id:genre:jazz",              type: "genre",  title: "Jazz",              tokens: ["jazz"]),
        .init(id: "$id:genre:classical",         type: "genre",  title: "Classical",         tokens: ["classical"]),
        .init(id: "$id:genre:rock",              type: "genre",  title: "Rock",              tokens: ["rock"]),
        .init(id: "$id:genre:ambient",           type: "genre",  title: "Ambient",           tokens: ["ambient"]),
        .init(id: "$id:song:thriller",           type: "track",  title: "Thriller",          tokens: ["thriller"]),
        .init(id: "$id:album:thriller",          type: "album",  title: "Thriller",          tokens: ["thriller"]),
        .init(id: "$id:artist:queen",            type: "artist", title: "Queen",             tokens: ["queen"]),
        .init(id: "$id:track:bohemian-rhapsody", type: "track",  title: "Bohemian Rhapsody", tokens: ["bohemian"]),
        .init(id: "$id:artist:miles-davis",      type: "artist", title: "Miles Davis",       tokens: ["miles"]),
    ]

    static let byId: [String: Entry] = Dictionary(uniqueKeysWithValues: entries.map { ($0.id, $0) })

    /// Lowercase, naive substring match against tokens. Optional type filter.
    static func search(query: String, type: String? = nil) -> [Entry] {
        let q = query.lowercased()
        return entries.filter { entry in
            (type == nil || entry.type == type) && entry.tokens.contains { q.contains($0) }
        }
    }
}
```

## 4. Tool handlers — `Louie/HeyLouieToolDispatcher.swift` (new file)

Each handler returns the JSON string that the backend agent will see as `tool_result.content`. The **exact shapes below are the contract** — they match the backend's in-process reference handlers in `backend/agent/tools.py`. If your shape drifts, the agent's narration will go subtly wrong without obvious tool errors.

Errors throw — the dispatcher turns thrown errors into `tool_result` with `is_error=true` and the error message as content. Argument validation is conservative: bad room → error, missing required field → error. The model recovers from errors fine; silent wrong behavior is worse than a loud error.

```swift
import Foundation

enum HeyLouieToolError: LocalizedError {
    case unknownTool(String)
    case badArgument(String)
    var errorDescription: String? {
        switch self {
        case .unknownTool(let name): "unknown tool: \(name)"
        case .badArgument(let detail): detail
        }
    }
}

@MainActor
struct HeyLouieToolDispatcher {
    let state: HeyLouieFakeState

    /// Returns the JSON content string for tool_result, or throws.
    func call(name: String, args: [String: Any]) throws -> String {
        switch name {
        case "search_music":    return try searchMusic(args)
        case "play_music":      return try playMusic(args)
        case "control_lights":  return try controlLights(args)
        case "set_climate":     return try setClimate(args)
        case "query_state":     return try queryState(args)
        default: throw HeyLouieToolError.unknownTool(name)
        }
    }

    // MARK: search_music
    // Returns a JSON array: [{"id": "$id:...", "type": "genre"|..., "title": "..."}]
    private func searchMusic(_ args: [String: Any]) throws -> String {
        guard let query = args["query"] as? String, !query.trimmingCharacters(in: .whitespaces).isEmpty else {
            throw HeyLouieToolError.badArgument("`query` is required and must be a non-empty string")
        }
        let typeFilter = args["type"] as? String
        let allowed: Set<String> = ["artist", "album", "genre", "playlist", "track"]
        if let t = typeFilter, !allowed.contains(t) {
            throw HeyLouieToolError.badArgument("unknown type: \(t)")
        }
        let hits = HeyLouieCatalog.search(query: query, type: typeFilter)
        let payload = hits.map { ["id": $0.id, "type": $0.type, "title": $0.title] }
        return try jsonString(payload)
    }

    // MARK: play_music
    // Returns {"ok": true, "id": "...", "now_playing": "Title"}
    private func playMusic(_ args: [String: Any]) throws -> String {
        guard let id = args["id"] as? String else {
            throw HeyLouieToolError.badArgument("`id` is required and must be a string")
        }
        guard let entry = HeyLouieCatalog.byId[id] else {
            throw HeyLouieToolError.badArgument("`id` must be a token returned by search_music (e.g. '$id:genre:jazz'), got \(id)")
        }
        state.nowPlayingId = id
        state.nowPlayingTitle = entry.title
        state.isPlaying = true
        return try jsonString(["ok": true, "id": id, "now_playing": entry.title])
    }

    // MARK: control_lights
    // Returns {"ok": true}
    private func controlLights(_ args: [String: Any]) throws -> String {
        let room = try requireRoom(args)
        let on = args["on"] as? Bool
        let brightness = args["brightness"] as? Int
        if on == nil && brightness == nil {
            throw HeyLouieToolError.badArgument("provide at least one of `on` or `brightness`")
        }
        if let b = brightness, !(0...100).contains(b) {
            throw HeyLouieToolError.badArgument("`brightness` must be 0-100, got \(b)")
        }
        if let on { state.lightOn[room] = on }
        if let brightness {
            state.lightBrightness[room] = brightness
            // Dimming to >0 without explicit on=false implies turn on.
            if on == nil && brightness > 0 {
                state.lightOn[room] = true
            }
        }
        return try jsonString(["ok": true])
    }

    // MARK: set_climate
    // Returns {"ok": true, "room": "kitchen", "target_c": 21.0}
    private func setClimate(_ args: [String: Any]) throws -> String {
        let room = try requireRoom(args)
        let target: Double
        if let d = args["target_c"] as? Double { target = d }
        else if let i = args["target_c"] as? Int { target = Double(i) }
        else { throw HeyLouieToolError.badArgument("`target_c` must be a number") }
        guard (5.0...35.0).contains(target) else {
            throw HeyLouieToolError.badArgument("`target_c` must be between 5.0 and 35.0, got \(target)")
        }
        state.targetC[room] = target
        return try jsonString(["ok": true, "room": room.rawValue, "target_c": target])
    }

    // MARK: query_state
    // Returns {"music": {...}, "lights": {...}, "climate": {...}} or a single subsystem.
    private func queryState(_ args: [String: Any]) throws -> String {
        let subsystem = (args["subsystem"] as? String) ?? "all"
        switch subsystem {
        case "music":   return try jsonString(["music": musicSnapshot()])
        case "lights":  return try jsonString(["lights": lightsSnapshot()])
        case "climate": return try jsonString(["climate": climateSnapshot()])
        case "all":     return try jsonString([
                               "music": musicSnapshot(),
                               "lights": lightsSnapshot(),
                               "climate": climateSnapshot(),
                           ])
        default: throw HeyLouieToolError.badArgument("unknown subsystem: \(subsystem)")
        }
    }

    // MARK: helpers
    private func requireRoom(_ args: [String: Any]) throws -> HeyLouieFakeState.Room {
        guard let raw = args["room"] as? String, let room = HeyLouieFakeState.Room(rawValue: raw) else {
            throw HeyLouieToolError.badArgument("`room` must be one of living_room, kitchen, bedroom")
        }
        return room
    }
    private func musicSnapshot() -> [String: Any] {
        [
            "id": state.nowPlayingId as Any,
            "now_playing": state.nowPlayingTitle as Any,
            "is_playing": state.isPlaying,
            "volume": state.volume,
        ]
    }
    private func lightsSnapshot() -> [String: [String: Any]] {
        Dictionary(uniqueKeysWithValues: HeyLouieFakeState.Room.allCases.map { room in
            (room.rawValue, ["on": state.lightOn[room] ?? false, "brightness": state.lightBrightness[room] ?? 100])
        })
    }
    private func climateSnapshot() -> [String: [String: Any]] {
        Dictionary(uniqueKeysWithValues: HeyLouieFakeState.Room.allCases.map { room in
            (room.rawValue, ["target_c": state.targetC[room] ?? 20.0])
        })
    }
    private func jsonString(_ value: Any) throws -> String {
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        return String(data: data, encoding: .utf8) ?? "{}"
    }
}
```

## 4a. Tool schemas — `Louie/HeyLouieSchemas.swift` (new file)

The backend doesn't hardcode tool definitions — it uses whatever you send in `hello.tools`. Ship the same five schemas the backend's reference impl uses (in `backend/evals/fake_louie.py:LOUIE_TOOL_SCHEMAS`). The descriptions are load-bearing — the model picks tools off them — so copy them verbatim rather than paraphrasing.

```swift
import Foundation

enum HeyLouieSchemas {
    /// Encoded as `[String: Any]` so they go straight into the JSON `tools`
    /// array without an intermediate Codable layer.
    static let all: [[String: Any]] = [
        [
            "name": "search_music",
            "description": """
                Find a playable music id for a user's request before calling play_music. \
                Use this for any phrase that names a genre, artist, album, song, or playlist \
                (e.g. 'jazz', 'Queen', 'Thriller', 'something ambient'). Returns a JSON array \
                of hits, each shaped {id, type, title}. The `id` is opaque — pass it verbatim \
                to play_music. If the array is empty, tell the user you couldn't find it; do \
                not invent ids. If multiple hits come back: pick the one whose `type` and \
                `title` clearly match what the user said (e.g. 'play the Thriller album' → \
                the type='album' hit; 'play Queen' → the type='artist' hit). Only ask for \
                clarification (via ask_user) when the request is genuinely ambiguous and no \
                hit is a confident match — never silently pick the first hit as a fallback.
                """,
            "input_schema": [
                "type": "object",
                "properties": [
                    "query": [
                        "type": "string",
                        "description": "The user's phrasing, lightly normalized. E.g. 'jazz', 'Thriller', 'Queen'.",
                    ],
                    "type": [
                        "type": "string",
                        "enum": ["artist", "album", "genre", "playlist", "track"],
                        "description": "Optional filter when the user was explicit (e.g. 'the Thriller album' → type='album'). Omit when the user was vague.",
                    ],
                ],
                "required": ["query"],
            ],
        ],
        [
            "name": "play_music",
            "description": """
                Start playback of a specific item. The `id` argument MUST be a value returned \
                from a prior search_music call in this turn — do not synthesize ids, do not pass \
                raw queries like 'jazz'. If you don't have an id yet, call search_music first.
                """,
            "input_schema": [
                "type": "object",
                "properties": [
                    "id": [
                        "type": "string",
                        "description": "An opaque id from search_music, shaped like '$id:<type>:<slug>'.",
                    ],
                ],
                "required": ["id"],
            ],
        ],
        [
            "name": "control_lights",
            "description": """
                Turn a room's lights on or off, set brightness, or both. At least one of `on` \
                or `brightness` is required. Passing brightness > 0 without `on` is treated as \
                'turn it on at that level'. To change brightness without turning the light on, \
                pass on=false explicitly (the brightness value is stored for the next time it's \
                turned on). Available rooms: living_room, kitchen, bedroom.
                """,
            "input_schema": [
                "type": "object",
                "properties": [
                    "room": [
                        "type": "string",
                        "enum": ["living_room", "kitchen", "bedroom"],
                        "description": "The room whose lights to control.",
                    ],
                    "on": ["type": "boolean", "description": "True to turn on, false to turn off. Optional."],
                    "brightness": [
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Brightness percentage, 0-100. Optional.",
                    ],
                ],
                "required": ["room"],
            ],
        ],
        [
            "name": "set_climate",
            "description": """
                Set a room's target temperature in degrees Celsius. The user may say the unit \
                or not; assume Celsius unless they explicitly say Fahrenheit (in which case \
                convert before calling). Reasonable range is 5-35°C.
                """,
            "input_schema": [
                "type": "object",
                "properties": [
                    "room": [
                        "type": "string",
                        "enum": ["living_room", "kitchen", "bedroom"],
                        "description": "The room whose climate to set.",
                    ],
                    "target_c": [
                        "type": "number",
                        "minimum": 5.0,
                        "maximum": 35.0,
                        "description": "Target temperature in Celsius.",
                    ],
                ],
                "required": ["room", "target_c"],
            ],
        ],
        [
            "name": "query_state",
            "description": """
                Read the current state of the house. Use this before answering questions like \
                'what's playing?', 'is the kitchen light on?', 'what's the bedroom set to?'. \
                Returns a JSON snapshot. Prefer the narrowest `subsystem` for the question; \
                use 'all' only when the user asked for a broad status.
                """,
            "input_schema": [
                "type": "object",
                "properties": [
                    "subsystem": [
                        "type": "string",
                        "enum": ["music", "lights", "climate", "all"],
                        "description": "Which subsystem to read. Default 'all'.",
                    ],
                ],
            ],
        ],
    ]
}
```

## 5. WebSocket client — `Louie/HeyLouieClient.swift` (new file)

One client = one turn. Uses `URLSessionWebSocketTask`. Exposes an `AsyncThrowingStream<HeyLouieInbound, Error>` of inbound messages so the controller can drive the state machine off it.

```swift
import Foundation

enum HeyLouieInbound: Sendable {
    case toolCall(id: String, name: String, input: [String: Any])
    case finalText(String)
    case cancelled
    case error(String)
}

enum HeyLouieClientError: LocalizedError {
    case decode(String)
    case transport(String)
    var errorDescription: String? {
        switch self {
        case .decode(let d): "decode: \(d)"
        case .transport(let d): "transport: \(d)"
        }
    }
}

@MainActor
final class HeyLouieClient {
    /// Defaults to local dev. Override via UserDefaults key `HeyLouieBackendURL`
    /// or by passing a value here. For Modal deploys, use the wss:// URL.
    static let defaultURL: URL = {
        if let raw = UserDefaults.standard.string(forKey: "HeyLouieBackendURL"),
           let url = URL(string: raw) { return url }
        return URL(string: "ws://localhost:8000/agent")!
    }()

    private let task: URLSessionWebSocketTask

    init(url: URL = HeyLouieClient.defaultURL) {
        let config = URLSessionConfiguration.default
        self.task = URLSession(configuration: config).webSocketTask(with: url)
    }

    func open() { task.resume() }
    func close() { task.cancel(with: .normalClosure, reason: nil) }

    func send(_ payload: [String: Any]) async throws {
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        guard let text = String(data: data, encoding: .utf8) else {
            throw HeyLouieClientError.transport("non-utf8 payload")
        }
        try await task.send(.string(text))
    }

    func sendHello(sessionId: String, tools: [[String: Any]]) async throws {
        try await send(["type": "hello", "session_id": sessionId, "tools": tools])
    }
    func sendUtterance(_ text: String) async throws {
        try await send(["type": "utterance", "text": text])
    }
    func sendToolResult(id: String, content: String, isError: Bool) async throws {
        try await send(["type": "tool_result", "tool_use_id": id, "content": content, "is_error": isError])
    }
    func sendCancel() async throws {
        try await send(["type": "cancel"])
    }

    /// Yields decoded inbound messages until the socket closes or errors.
    func messages() -> AsyncThrowingStream<HeyLouieInbound, Error> {
        AsyncThrowingStream { continuation in
            let pump = Task { [task] in
                do {
                    while !Task.isCancelled {
                        let msg = try await task.receive()
                        let raw: String
                        switch msg {
                        case .string(let s): raw = s
                        case .data(let d):   raw = String(data: d, encoding: .utf8) ?? ""
                        @unknown default:    continue
                        }
                        guard
                            let data = raw.data(using: .utf8),
                            let obj  = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                            let type = obj["type"] as? String
                        else {
                            continuation.finish(throwing: HeyLouieClientError.decode("bad json: \(raw)"))
                            return
                        }
                        switch type {
                        case "tool_call":
                            guard
                                let id = obj["tool_use_id"] as? String,
                                let name = obj["name"] as? String,
                                let input = obj["input"] as? [String: Any]
                            else {
                                continuation.finish(throwing: HeyLouieClientError.decode("bad tool_call: \(obj)"))
                                return
                            }
                            continuation.yield(.toolCall(id: id, name: name, input: input))
                        case "final_text":
                            continuation.yield(.finalText((obj["text"] as? String) ?? ""))
                            continuation.finish()
                            return
                        case "cancelled":
                            continuation.yield(.cancelled)
                            continuation.finish()
                            return
                        case "error":
                            continuation.yield(.error((obj["message"] as? String) ?? "unknown"))
                            continuation.finish()
                            return
                        default:
                            continue
                        }
                    }
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in pump.cancel() }
        }
    }
}
```

## 6. Replace `VoiceAgentDummy` with a real `VoiceAgent`

Keep the `VoiceAgentState` / `VoiceAgentEvent` / overlay code in `Louie/VoiceAgent.swift` untouched — those are stable. Add a new controller class `VoiceAgent` with the same `state` / `handle` surface so `ContentView` doesn't need to change much. Keep `VoiceAgentDummy` as-is for previews and the no-backend demo path.

The new `VoiceAgent`:

1. On `startRecording`: same as dummy — start `VoiceCapture`.
2. On `stopRecording`: get transcript, transition to `.thinking`, open WebSocket, send `hello` + `utterance`.
3. For each inbound message:
   - `toolCall(id, name, input)` → set `state = .runningLocalTool(...)`, run `HeyLouieToolDispatcher.call`, send `tool_result` back.
   - `finalText(text)` → `state = .showingResponse(text)`; `capture.speak(text)`; close client.
   - `error(msg)` → `state = .failed(msg)`; close client.
   - `cancelled` → `state = .idle`.
4. On `cancel` event from the user: send `{"type": "cancel"}`, close client, return to `.idle`.

Sketch (drop alongside `VoiceAgentDummy` in `VoiceAgent.swift`):

```swift
@MainActor
@Observable
final class VoiceAgent {
    private(set) var state: VoiceAgentState = .idle
    let fake = HeyLouieFakeState()      // exposed for the debug view

    private let capture = VoiceCapture()
    private var recordingTask: Task<Void, Never>?
    private var client: HeyLouieClient?
    private var releaseContinuation: CheckedContinuation<Void, Never>?
    private var stopRequested = false

    func handle(_ event: VoiceAgentEvent) {
        switch event {
        case .startRecording:
            if case .idle = state { beginRecording() }
            else if case .showingResponse = state { beginRecording() }
            else if case .failed = state { beginRecording() }
        case .stopRecording:
            if case .recording = state {
                stopRequested = true
                releaseContinuation?.resume(); releaseContinuation = nil
            }
        case .cancel:
            cancelEverything()
            state = .idle
        case .answerPrimary, .answerSecondary:
            break  // Day 3 ask_user
        }
    }

    private func beginRecording() {
        stopRequested = false
        state = .recording
        recordingTask?.cancel()
        recordingTask = Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                try await capture.startRecording()
            } catch {
                if !Task.isCancelled { state = .failed(error.localizedDescription) }
                return
            }
            if !stopRequested {
                await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
                    releaseContinuation = c
                }
            }
            if Task.isCancelled { capture.cancel(); return }
            state = .thinking
            let transcript: String
            do { transcript = try await capture.stopRecording() }
            catch {
                if !Task.isCancelled { state = .failed(error.localizedDescription) }
                return
            }
            if Task.isCancelled { return }
            await runBackendTurn(transcript: transcript)
        }
    }

    private func runBackendTurn(transcript: String) async {
        let client = HeyLouieClient()
        self.client = client
        client.open()
        let dispatcher = HeyLouieToolDispatcher(state: fake)
        let sessionId = UUID().uuidString
        do {
            try await client.sendHello(
                sessionId: sessionId,
                tools: HeyLouieSchemas.all,
            )
            try await client.sendUtterance(transcript)
            for try await msg in client.messages() {
                if Task.isCancelled { break }
                switch msg {
                case .toolCall(let id, let name, let input):
                    state = .runningLocalTool(LocalToolActivity(name: name, summary: summary(for: name, input: input)))
                    let (content, isError): (String, Bool) = {
                        do { return (try dispatcher.call(name: name, args: input), false) }
                        catch { return ("\(error.localizedDescription)", true) }
                    }()
                    try await client.sendToolResult(id: id, content: content, isError: isError)
                case .finalText(let text):
                    state = .showingResponse(text)
                    capture.speak(text)
                case .cancelled:
                    state = .idle
                case .error(let msg):
                    state = .failed(msg)
                }
            }
        } catch {
            state = .failed(error.localizedDescription)
        }
        client.close()
        self.client = nil
    }

    private func cancelEverything() {
        recordingTask?.cancel(); recordingTask = nil
        releaseContinuation?.resume(); releaseContinuation = nil
        Task { try? await client?.sendCancel() }
        client?.close(); client = nil
        capture.cancel()
    }

    private func summary(for tool: String, input: [String: Any]) -> String {
        switch tool {
        case "search_music":   return "Searching for '\(input["query"] ?? "")'"
        case "play_music":     return "Playing \(input["id"] ?? "")"
        case "control_lights": return "Lights in \(input["room"] ?? "?")"
        case "set_climate":    return "Climate in \(input["room"] ?? "?") → \(input["target_c"] ?? "?")°C"
        case "query_state":    return "Reading state"
        default: return tool
        }
    }
}
```

## 7. Wire it in — `Louie/ContentView.swift`

One-line swap. Change:

```swift
@State private var voiceAgent = VoiceAgentDummy()
```

to:

```swift
@State private var voiceAgent = VoiceAgent()
```

The overlay's existing `VoiceAgentOverlay(state: voiceAgent.state, onEvent: voiceAgent.handle)` keeps working — `state` and `handle` are the same shape.

## 8. Debug state view (optional but recommended)

Per Hey-Louie `PLAN.md` Day 2 step 3: "A simple state view that shows 'what Louie thinks is happening' so you can verify tool calls landed." Add a `HeyLouieDebugView` accessible via a debug-only sidebar entry, showing `voiceAgent.fake` (now playing, lights per room, climate per room). Use `@Bindable` on the observable.

## 9. Backend URL configuration

Default: `ws://localhost:8000/agent`. For testing on a physical iPad against a Mac running `uv run fastapi dev backend/main.py`, set `HeyLouieBackendURL` in UserDefaults to `ws://<mac-lan-ip>:8000/agent`. Make sure the Mac's firewall allows incoming on 8000 and the iPad is on the same Wi-Fi. (The existing `NSLocalNetworkUsageDescription` in Info.plist covers this.) For Modal deploys, use the `wss://` URL Modal prints.

## 10. Test plan

- **Simulator:** STT does not work in iOS Simulator (no real mic). Test on a physical iPad.
- Run backend locally: `cd hey-louie-agent && uv run fastapi dev backend/main.py`. Confirm `/health` returns OK and `/agent` accepts a WebSocket (try `websocat ws://localhost:8000/agent`).
- **Smoke tests on device:**
  1. "Play some jazz" → state goes recording → thinking → search_music tool call (debug view shows search) → play_music → final_text "Playing jazz." spoken; fake state shows `nowPlayingId = "$id:genre:jazz"`.
  2. "Turn on the kitchen lights" → one control_lights call; debug view shows kitchen on.
  3. "What's playing?" → query_state call; agent speaks back current state.
  4. Cancel: speak "play jazz", before TTS finishes press the button — verify cancel reaches backend (logs show "turn cancelled"), iPad returns to `.idle`.

## 11. Out of scope (do NOT add)

- Don't add a sixth tool. If you think one is needed, append a line to Hey-Louie's `FOLLOWUPS.md` on the backend repo and stop.
- Don't connect any of these tools to the real `Linn` package. Day 2 is fake state only.
- Don't stream `final_text` token-by-token — that's a Day 2 stretch goal not in the base sprint.
- Don't implement `ask_user` — that's Day 3.

Once the simple smoke tests pass on device, push and update PR #5.
