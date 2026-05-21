"""Day-1 eval cases. Five unambiguous turns, one per tool (lights gets two).

Eval cases are written before the loop they exercise — they define what
"working" means. Day 3 expands this file to 25–30 cases with disambiguation,
parallel tool calls, and cancellation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backend.evals.fake_louie import FakeLouie


@dataclass(frozen=True)
class EvalCase:
    name: str
    utterance: str
    # Exact set of tool names the agent must call (compared as a set, so
    # order is irrelevant but extras and omissions both fail the case).
    expected_tools: list[str]
    # Optional post-turn assertion over (final FakeLouie state, final agent text).
    # Should raise AssertionError on mismatch.
    check: Callable[[FakeLouie, str], None] | None = None


# --- checks ---------------------------------------------------------------


def _check_jazz_playing(state: FakeLouie, _final_text: str) -> None:
    # Assert the stable catalog id, not a narration title: the search hit for
    # "jazz" is unambiguous, so the agent has no excuse for picking anything
    # else. Day 3 will add a Thriller case where multiple ids are valid.
    # Bare `==` asserts get pytest's rich both-sides diff on failure.
    assert state.music.is_playing
    assert state.music.now_playing_id == "$id:genre:jazz"


def _check_kitchen_on(state: FakeLouie, _final_text: str) -> None:
    assert state.lights["kitchen"].on


def _check_living_room_dim(state: FakeLouie, _final_text: str) -> None:
    # "Dim to 20%" implies the light is on; if a model leaves it off we accept,
    # but brightness must reflect the request.
    assert state.lights["living_room"].brightness == 20


def _check_bedroom_climate(state: FakeLouie, _final_text: str) -> None:
    assert state.climate["bedroom"].target_c == 19.0


def _check_query_music(_state: FakeLouie, final_text: str) -> None:
    # Read-only tool; just assert the agent produced narration after the call.
    # Day 3 tightens this with substring assertions once we seed initial state.
    assert final_text.strip()


# --- cases ---------------------------------------------------------------


CASES: list[EvalCase] = [
    EvalCase(
        name="play_jazz",
        utterance="Play some jazz.",
        # Two-step: agent must search first, then play the returned id.
        # play_music rejects anything that isn't a $id:... token.
        expected_tools=["search_music", "play_music"],
        check=_check_jazz_playing,
    ),
    EvalCase(
        name="lights_on_kitchen",
        utterance="Turn on the kitchen lights.",
        expected_tools=["control_lights"],
        check=_check_kitchen_on,
    ),
    EvalCase(
        name="dim_living_room",
        utterance="Dim the living room lights to 20 percent.",
        expected_tools=["control_lights"],
        check=_check_living_room_dim,
    ),
    EvalCase(
        name="climate_bedroom",
        utterance="Set the bedroom to 19 degrees.",
        expected_tools=["set_climate"],
        check=_check_bedroom_climate,
    ),
    EvalCase(
        name="query_music",
        utterance="What's currently playing?",
        expected_tools=["query_state"],
        check=_check_query_music,
    ),
]
