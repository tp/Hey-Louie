"""In-memory Louie state used by eval cases and (Day 1) the tool implementations.

On Day 1 the five tools mutate this object directly. On Day 2 the same state
lives on the iPad and the backend tools dispatch over WebSocket — but the
shape is the contract, so we lock it down here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ROOMS: tuple[str, ...] = ("living_room", "kitchen", "bedroom")


@dataclass
class MusicState:
    # Opaque catalog token (e.g. "$id:genre:jazz") set by play_music. Tests
    # assert against this rather than the title because the id is stable
    # across model wording, narration choices, and STT noise.
    now_playing_id: str | None = None
    # Human-facing title for `query_state` to read back ("Bono is playing").
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


@dataclass
class FakeLouie:
    music: MusicState = field(default_factory=MusicState)
    lights: dict[str, Light] = field(default_factory=lambda: {r: Light() for r in ROOMS})
    climate: dict[str, Climate] = field(default_factory=lambda: {r: Climate() for r in ROOMS})


def default_state() -> FakeLouie:
    return FakeLouie()
