"""FakeLouie — the reference iPad-side model, used as both eval fixture and
the canonical specification of what the real iPad must implement.

`FakeLouie` is a `Session` implementation: it owns the state (music, lights,
climate), the music catalog, the five tool handlers, and the schemas the
model sees for them. Tests construct one and pass it straight to
`run_turn(adapter, fake, utterance)`; they can also inspect state directly
afterwards (`fake.music.is_playing`, `fake.lights["kitchen"].on`) without
going through `query_state`.

═══════════════════════════════════════════════════════════════════════════
   CONTRACT — keep in sync with the iPad
═══════════════════════════════════════════════════════════════════════════
This module is the reference implementation of the iPad-side "Louie" model.
The schemas and tool_result shapes here MUST match what the iPad ships:

  - Schemas (`LOUIE_TOOL_SCHEMAS` below) ↔ `Louie/HeyLouieSchemas.swift`
    (PR tp/Louie#5, branch `hey-louie`). The iPad sends these in `hello.tools`;
    the backend trusts whatever arrives. Drift = the model sees different
    descriptions in eval vs production.

  - Handler return shapes (`_h_*` below) ↔ `HeyLouieToolDispatcher.swift`.
    Both must produce byte-identical `tool_result.content` for the same args.
    Drift = the model's narration silently degrades against one runtime.

The eval suite is the regression net: any handler change that breaks a
canonical shape will surface as a failed case. When editing here, also update
IPAD_DAY_2.md.
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, override

from backend.adapters.base import ToolResultBlock, ToolSchema
from backend.agent.session import Session

ROOMS: tuple[str, ...] = ("living_room", "kitchen", "bedroom")
_MUSIC_TYPES: tuple[str, ...] = ("artist", "album", "genre", "playlist", "track")
_SUBSYSTEMS: tuple[str, ...] = ("music", "lights", "climate", "all")


# --- state ----------------------------------------------------------------


@dataclass
class MusicState:
    # Opaque catalog token (e.g. "$id:genre:jazz") set by play_music. Tests
    # assert against this rather than the title because the id is stable
    # across model wording, narration choices, and STT noise.
    now_playing_id: str | None = None
    # Human-facing title for `query_state` to read back ("Thriller is playing").
    # Always set together with `now_playing_id`; never store one without
    # the other.
    now_playing_title: str | None = None
    is_playing: bool = False
    volume: int = 50  # 0-100


@dataclass
class Light:
    on: bool = False
    brightness: int = 100  # 0-100, meaningful when on=True


@dataclass
class Climate:
    target_c: float = 20.0


# --- catalog --------------------------------------------------------------
#
# Tiny in-memory music catalog. The dual `thriller` mapping is what powers the
# Day-3 disambiguation eval ("play Thriller" → song or album?). Adding entries
# is fine; renaming ids breaks eval assertions.

_ENTRIES: list[dict[str, Any]] = [
    {"id": "$id:genre:jazz", "type": "genre", "title": "Jazz", "tokens": ["jazz"]},
    {"id": "$id:genre:classical", "type": "genre", "title": "Classical", "tokens": ["classical"]},
    {"id": "$id:genre:rock", "type": "genre", "title": "Rock", "tokens": ["rock"]},
    {"id": "$id:genre:ambient", "type": "genre", "title": "Ambient", "tokens": ["ambient"]},
    {"id": "$id:song:thriller", "type": "track", "title": "Thriller", "tokens": ["thriller"]},
    {"id": "$id:album:thriller", "type": "album", "title": "Thriller", "tokens": ["thriller"]},
    {"id": "$id:artist:queen", "type": "artist", "title": "Queen", "tokens": ["queen"]},
    {
        "id": "$id:track:bohemian-rhapsody",
        "type": "track",
        "title": "Bohemian Rhapsody",
        "tokens": ["bohemian"],
    },
    {"id": "$id:artist:miles-davis", "type": "artist", "title": "Miles Davis", "tokens": ["miles"]},
]

_BY_ID: dict[str, dict[str, Any]] = {e["id"]: e for e in _ENTRIES}


def _search_catalog(query: str, type_filter: str | None) -> list[dict[str, str]]:
    """Return hits whose tokens appear in the query. Lowercase, naive substring match."""
    q = query.lower()
    hits: list[dict[str, str]] = []
    for entry in _ENTRIES:
        if type_filter and entry["type"] != type_filter:
            continue
        if any(token in q for token in entry["tokens"]):
            hits.append({"id": entry["id"], "type": entry["type"], "title": entry["title"]})
    return hits


# --- schemas --------------------------------------------------------------
#
# Descriptions are load-bearing: the model picks tools off the description, not
# the name. Each one below is written assuming the model sees the tool list cold.
# These are the schemas the real iPad will send in its `hello` message — keep
# them and the iPad's hardcoded copy in sync.

LOUIE_TOOL_SCHEMAS: list[ToolSchema] = [
    ToolSchema(
        name="search_music",
        description=(
            "Find a playable music id for a user's request before calling play_music. "
            "Use this for any phrase that names a genre, artist, album, song, or playlist "
            "(e.g. 'jazz', 'Queen', 'Thriller', 'something ambient'). Returns a JSON array "
            "of hits, each shaped {id, type, title}. The `id` is opaque — pass it verbatim "
            "to play_music. If the array is empty, tell the user you couldn't find it; do "
            "not invent ids. If multiple hits come back: pick the one whose `type` and "
            "`title` clearly match what the user said (e.g. 'play the Thriller album' → "
            "the type='album' hit; 'play Queen' → the type='artist' hit). Only ask for "
            "clarification (via ask_user) when the request is genuinely ambiguous and no "
            "hit is a confident match — never silently pick the first hit as a fallback."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The user's phrasing, lightly normalized. E.g. 'jazz', 'Thriller', 'Queen'."
                    ),
                },
                "type": {
                    "type": "string",
                    "enum": list(_MUSIC_TYPES),
                    "description": (
                        "Optional filter when the user was explicit (e.g. 'the Thriller album' "
                        "→ type='album'). Omit when the user was vague."
                    ),
                },
            },
            "required": ["query"],
        },
    ),
    ToolSchema(
        name="play_music",
        description=(
            "Start playback of a specific item. The `id` argument MUST be a value returned "
            "from a prior search_music call in this turn — do not synthesize ids, do not pass "
            "raw queries like 'jazz'. If you don't have an id yet, call search_music first."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "An opaque id from search_music, shaped like '$id:<type>:<slug>'."
                    ),
                },
            },
            "required": ["id"],
        },
    ),
    ToolSchema(
        name="control_lights",
        description=(
            "Turn a room's lights on or off, set brightness, or both. At least one of `on` "
            "or `brightness` is required. Passing brightness > 0 without `on` is treated as "
            "'turn it on at that level'. To change brightness without turning the light on, "
            "pass on=false explicitly (the brightness value is stored for the next time it's "
            "turned on). Available rooms: living_room, kitchen, bedroom."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "room": {
                    "type": "string",
                    "enum": list(ROOMS),
                    "description": "The room whose lights to control.",
                },
                "on": {
                    "type": "boolean",
                    "description": "True to turn on, false to turn off. Optional.",
                },
                "brightness": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Brightness percentage, 0-100. Optional.",
                },
            },
            "required": ["room"],
        },
    ),
    ToolSchema(
        name="set_climate",
        description=(
            "Set a room's target temperature in degrees Celsius. The user may say the unit "
            "or not; assume Celsius unless they explicitly say Fahrenheit (in which case "
            "convert before calling). Reasonable range is 5-35°C."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "room": {
                    "type": "string",
                    "enum": list(ROOMS),
                    "description": "The room whose climate to set.",
                },
                "target_c": {
                    "type": "number",
                    "minimum": 5.0,
                    "maximum": 35.0,
                    "description": "Target temperature in Celsius.",
                },
            },
            "required": ["room", "target_c"],
        },
    ),
    ToolSchema(
        name="query_state",
        description=(
            "Read the current state of the house. Use this before answering questions like "
            "'what's playing?', 'is the kitchen light on?', 'what's the bedroom set to?'. "
            "Returns a JSON snapshot. Prefer the narrowest `subsystem` for the question; "
            "use 'all' only when the user asked for a broad status."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "subsystem": {
                    "type": "string",
                    "enum": list(_SUBSYSTEMS),
                    "description": "Which subsystem to read. Default 'all'.",
                },
            },
        },
    ),
]


# --- the Session implementation -------------------------------------------


@dataclass
class FakeLouie(Session):
    """In-memory reference implementation of the iPad-side Louie model.

    Inherits from `Session` explicitly (not just structurally) so type
    checkers verify the method signatures match the protocol at this site.
    """

    music: MusicState = field(default_factory=MusicState)
    lights: dict[str, Light] = field(default_factory=lambda: {r: Light() for r in ROOMS})
    climate: dict[str, Climate] = field(default_factory=lambda: {r: Climate() for r in ROOMS})

    # --- Session protocol -------------------------------------------------

    @override
    def schemas(self) -> list[ToolSchema]:
        return LOUIE_TOOL_SCHEMAS

    @override
    async def dispatch_tool(
        self, name: str, args: dict[str, Any], tool_use_id: str
    ) -> ToolResultBlock:
        handler = _HANDLERS.get(name)
        if handler is None:
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=f"unknown tool: {name!r}",
                is_error=True,
            )
        try:
            content = handler(self, args)
        except Exception as exc:  # noqa: BLE001 — model recovers from handler errors
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=f"{type(exc).__name__}: {exc}",
                is_error=True,
            )
        return ToolResultBlock(tool_use_id=tool_use_id, content=content)


# --- handlers -------------------------------------------------------------
#
# Pure functions over `(FakeLouie, args) -> json-string`. Validation throws;
# `dispatch_tool` wraps thrown errors into ToolResultBlock(is_error=True).
# Synchronous because there's no I/O — these are the reference impl for what
# the iPad executes when it receives a `tool_call`.


def _h_search_music(_state: FakeLouie, args: dict[str, Any]) -> str:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("`query` is required and must be a non-empty string")
    type_filter = args.get("type")
    if type_filter is not None and type_filter not in _MUSIC_TYPES:
        raise ValueError(f"unknown type: {type_filter!r}")
    return json.dumps(_search_catalog(query, type_filter))


def _h_play_music(state: FakeLouie, args: dict[str, Any]) -> str:
    music_id = args.get("id")
    if not isinstance(music_id, str):
        raise ValueError("`id` is required and must be a string")
    entry = _BY_ID.get(music_id)
    if entry is None:
        raise ValueError(
            "`id` must be a token returned by search_music "
            f"(e.g. '$id:genre:jazz'), got {music_id!r}"
        )
    title = entry["title"]
    state.music.now_playing_id = music_id
    state.music.now_playing_title = title
    state.music.is_playing = True
    return json.dumps({"ok": True, "id": music_id, "now_playing": title})


def _require_room(args: dict[str, Any]) -> str:
    room = args.get("room")
    if not isinstance(room, str) or room not in ROOMS:
        raise ValueError(f"`room` must be one of {list(ROOMS)}, got {room!r}")
    return room


def _h_control_lights(state: FakeLouie, args: dict[str, Any]) -> str:
    room = _require_room(args)
    light = state.lights[room]
    on = args.get("on")
    brightness = args.get("brightness")
    if on is None and brightness is None:
        raise ValueError("provide at least one of `on` or `brightness`")
    if on is not None:
        if not isinstance(on, bool):
            raise ValueError(f"`on` must be a boolean, got {on!r}")
        light.on = on
    if brightness is not None:
        if (
            not isinstance(brightness, int)
            or isinstance(brightness, bool)
            or not 0 <= brightness <= 100
        ):
            raise ValueError(f"`brightness` must be an int 0-100, got {brightness!r}")
        light.brightness = brightness
        # Dimming to >0 implies the user wants the light on; explicit on=False wins.
        if on is None and brightness > 0:
            light.on = True
    return json.dumps({"ok": True})


def _h_set_climate(state: FakeLouie, args: dict[str, Any]) -> str:
    room = _require_room(args)
    target = args.get("target_c")
    if not isinstance(target, int | float) or isinstance(target, bool):
        raise ValueError(f"`target_c` must be a number, got {target!r}")
    target_f = float(target)
    if not 5.0 <= target_f <= 35.0:
        raise ValueError(f"`target_c` must be between 5.0 and 35.0, got {target_f}")
    state.climate[room].target_c = target_f
    return json.dumps({"ok": True, "room": room, "target_c": target_f})


def _snapshot_music(state: FakeLouie) -> dict[str, Any]:
    return {
        "id": state.music.now_playing_id,
        "now_playing": state.music.now_playing_title,
        "is_playing": state.music.is_playing,
        "volume": state.music.volume,
    }


def _snapshot_lights(state: FakeLouie) -> dict[str, Any]:
    return {
        room: {"on": light.on, "brightness": light.brightness}
        for room, light in state.lights.items()
    }


def _snapshot_climate(state: FakeLouie) -> dict[str, Any]:
    return {r: {"target_c": c.target_c} for r, c in state.climate.items()}


def _h_query_state(state: FakeLouie, args: dict[str, Any]) -> str:
    subsystem = args.get("subsystem", "all")
    if subsystem == "music":
        return json.dumps({"music": _snapshot_music(state)})
    if subsystem == "lights":
        return json.dumps({"lights": _snapshot_lights(state)})
    if subsystem == "climate":
        return json.dumps({"climate": _snapshot_climate(state)})
    if subsystem == "all":
        return json.dumps(
            {
                "music": _snapshot_music(state),
                "lights": _snapshot_lights(state),
                "climate": _snapshot_climate(state),
            }
        )
    raise ValueError(f"`subsystem` must be one of {list(_SUBSYSTEMS)}, got {subsystem!r}")


_HANDLERS = {
    "search_music": _h_search_music,
    "play_music": _h_play_music,
    "control_lights": _h_control_lights,
    "set_climate": _h_set_climate,
    "query_state": _h_query_state,
}
