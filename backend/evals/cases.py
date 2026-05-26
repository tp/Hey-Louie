"""Eval cases — what "working" means for the agent harness.

Cases are written before the loop they exercise. Each case is a single user
utterance with a small assertion bundle: which tools the agent must (and must
not) call, an optional state seed, and an optional post-turn check.

The case mix on Day 3 has a deliberate shape:

- Most cases (~17) are CLEAR requests with `forbidden_tools=("ask_user",)`.
  Without this signal, models over-ask; the asymmetry "many cases that
  forbid asking, one case that demands it" is what surfaces ask_user prompt
  sensitivity in the CSV. Removing the asymmetry collapses the eval to
  unit-tests-with-extra-steps.
- ONE disambiguation case (`thriller_disambig`) demands ask_user. The
  picker is wired in `setup` so the eval can assert the agent then acted on
  the chosen id, not the other one.
- Parallel cases verify the loop's gather() — the model issues multiple
  tool_uses in one assistant message; we assert all ran.
- ONE cancellation case verifies the cancel-token primitive that will later
  drive the WebSocket `cancel` message in production.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backend.evals.fake_louie import FakeLouie

InitialState = Callable[[FakeLouie], None]


@dataclass(frozen=True)
class EvalCase:
    name: str
    utterance: str
    # Exact set of tool names the agent must call (compared as a set, so
    # order is irrelevant but extras and omissions both fail the case).
    expected_tools: list[str]
    # Tools the agent must NOT call. Primary use: assert the model doesn't
    # reach for ask_user on clear requests it should just act on.
    forbidden_tools: tuple[str, ...] = ()
    # Optional state seed run before the turn (e.g. prime now_playing,
    # install a custom ask_user_picker).
    setup: InitialState | None = None
    # Optional post-turn assertion over (final FakeLouie state, final agent text).
    # Should raise AssertionError on mismatch.
    check: Callable[[FakeLouie, str], None] | None = None
    # When True the runner fires `cancel_token` immediately after the first
    # tool result returns; the loop must raise TurnCancelled cleanly. Skips
    # `expected_tools` / `check` assertions — cancellation cases verify
    # control-flow, not behavior.
    cancel_after_first_tool: bool = False


# Shorthand: "ask_user is forbidden on this case." Most cases use this — the
# common shape is "clear request, just act."
NO_ASK: tuple[str, ...] = ("ask_user",)


# --- setup helpers --------------------------------------------------------


def _seed_thriller_playing(state: FakeLouie) -> None:
    """Prime so 'what's playing?' has something to narrate."""
    state.music.now_playing_id = "$id:album:thriller"
    state.music.now_playing_title = "Thriller"
    state.music.is_playing = True


def _seed_kitchen_on(state: FakeLouie) -> None:
    state.lights["kitchen"].on = True
    state.lights["kitchen"].brightness = 100


def _seed_bedroom_target(state: FakeLouie) -> None:
    state.climate["bedroom"].target_c = 18.0


def _pick_thriller_song(_question: str, choices: list[dict[str, str]]) -> str:
    """User picks 'the song' in the Thriller disambiguation case."""
    for c in choices:
        if c["id"] == "$id:song:thriller":
            return c["id"]
    # Fall through if model named the choices differently — fail loudly so the
    # case turns red rather than silently passing on a wrong assumption.
    raise AssertionError(
        f"ask_user choices did not include $id:song:thriller; got {[c['id'] for c in choices]}"
    )


def _setup_thriller_disambig(state: FakeLouie) -> None:
    state.ask_user_picker = _pick_thriller_song


# --- checks ---------------------------------------------------------------


def _check_now_playing(expected_id: str) -> Callable[[FakeLouie, str], None]:
    def check(state: FakeLouie, _final_text: str) -> None:
        assert state.music.is_playing, "expected playback to be active"
        assert state.music.now_playing_id == expected_id, (
            f"expected now_playing_id={expected_id!r}, got {state.music.now_playing_id!r}"
        )

    return check


def _check_light(
    room: str, *, on: bool | None = None, brightness: int | None = None
) -> Callable[[FakeLouie, str], None]:
    def check(state: FakeLouie, _final_text: str) -> None:
        light = state.lights[room]
        if on is not None:
            assert light.on is on, f"{room}: expected on={on}, got on={light.on}"
        if brightness is not None:
            assert light.brightness == brightness, (
                f"{room}: expected brightness={brightness}, got {light.brightness}"
            )

    return check


def _check_climate(
    room: str, target_c: float, *, tolerance: float = 0.6
) -> Callable[[FakeLouie, str], None]:
    def check(state: FakeLouie, _final_text: str) -> None:
        actual = state.climate[room].target_c
        assert abs(actual - target_c) <= tolerance, (
            f"{room}: expected target_c≈{target_c} (±{tolerance}), got {actual}"
        )

    return check


def _check_text_contains(needle: str) -> Callable[[FakeLouie, str], None]:
    def check(_state: FakeLouie, final_text: str) -> None:
        assert needle.lower() in final_text.lower(), (
            f"expected {needle!r} in narration, got: {final_text!r}"
        )

    return check


def _check_thriller_disambig(state: FakeLouie, _final_text: str) -> None:
    # Picker chose the song; play_music must have used that id, not the album.
    assert state.music.now_playing_id == "$id:song:thriller", (
        f"expected now_playing_id=$id:song:thriller, got {state.music.now_playing_id!r}"
    )
    assert len(state.ask_user_log) == 1, (
        f"expected exactly one ask_user call, got {len(state.ask_user_log)}: {state.ask_user_log}"
    )


def _check_lights_off(*rooms: str) -> Callable[[FakeLouie, str], None]:
    def check(state: FakeLouie, _final_text: str) -> None:
        for room in rooms:
            assert state.lights[room].on is False, f"{room} should be off"

    return check


def _check_no_search_hits(_state: FakeLouie, final_text: str) -> None:
    # The agent should narrate that it couldn't find anything; we accept any of
    # several plausible phrasings since model wording varies.
    lowered = final_text.lower()
    assert any(
        phrase in lowered
        for phrase in ("couldn't find", "could not find", "can't find", "didn't find", "no match")
    ), f"expected a 'not found' narration, got: {final_text!r}"


def _check_outside_scope(_state: FakeLouie, final_text: str) -> None:
    lowered = final_text.lower()
    assert any(
        phrase in lowered
        for phrase in ("can't", "cannot", "don't", "do not", "unable", "no access", "sorry")
    ), f"expected a 'cannot help' narration, got: {final_text!r}"


def _all(*checks: Callable[[FakeLouie, str], None]) -> Callable[[FakeLouie, str], None]:
    """Run several checks in order — first failure aborts."""

    def combined(state: FakeLouie, final_text: str) -> None:
        for c in checks:
            c(state, final_text)

    return combined


def _check_query_affirms_on(state: FakeLouie, final_text: str) -> None:
    assert any(w in final_text.lower() for w in ("yes", "on", "lit", "kitchen is")), (
        f"expected affirmation, got: {final_text!r}"
    )


def _seed_lights_on(*rooms: str) -> InitialState:
    def seed(state: FakeLouie) -> None:
        for room in rooms:
            state.lights[room].on = True

    return seed


# --- cases ---------------------------------------------------------------


CASES: list[EvalCase] = [
    # === single-tool, clear actions ===
    EvalCase(
        name="play_jazz",
        utterance="Play some jazz.",
        expected_tools=["search_music", "play_music"],
        forbidden_tools=NO_ASK,
        check=_check_now_playing("$id:genre:jazz"),
    ),
    EvalCase(
        name="play_classical",
        utterance="Put on some classical music.",
        expected_tools=["search_music", "play_music"],
        forbidden_tools=NO_ASK,
        check=_check_now_playing("$id:genre:classical"),
    ),
    EvalCase(
        name="play_ambient",
        utterance="Something ambient, please.",
        expected_tools=["search_music", "play_music"],
        forbidden_tools=NO_ASK,
        check=_check_now_playing("$id:genre:ambient"),
    ),
    EvalCase(
        name="play_album_thriller_explicit",
        utterance="Play the Thriller album.",
        # Phrasing is explicit ("the album") — search hits both, but the
        # type=album hit is the clear pick. ask_user is wrong here.
        expected_tools=["search_music", "play_music"],
        forbidden_tools=NO_ASK,
        check=_check_now_playing("$id:album:thriller"),
    ),
    EvalCase(
        name="play_artist_queen",
        utterance="Play Queen.",
        expected_tools=["search_music", "play_music"],
        forbidden_tools=NO_ASK,
        check=_check_now_playing("$id:artist:queen"),
    ),
    EvalCase(
        name="play_bohemian_rhapsody",
        utterance="Play Bohemian Rhapsody.",
        expected_tools=["search_music", "play_music"],
        forbidden_tools=NO_ASK,
        check=_check_now_playing("$id:track:bohemian-rhapsody"),
    ),
    # === lights ===
    EvalCase(
        name="lights_on_kitchen",
        utterance="Turn on the kitchen lights.",
        expected_tools=["control_lights"],
        forbidden_tools=NO_ASK,
        check=_check_light("kitchen", on=True),
    ),
    EvalCase(
        name="lights_off_bedroom",
        utterance="Turn off the bedroom light.",
        expected_tools=["control_lights"],
        forbidden_tools=NO_ASK,
        check=_check_light("bedroom", on=False),
    ),
    EvalCase(
        name="dim_living_room",
        utterance="Dim the living room lights to 20 percent.",
        expected_tools=["control_lights"],
        forbidden_tools=NO_ASK,
        check=_check_light("living_room", brightness=20),
    ),
    EvalCase(
        name="brightness_only_kitchen",
        utterance="Set the kitchen lights to 60 percent.",
        expected_tools=["control_lights"],
        forbidden_tools=NO_ASK,
        check=_check_light("kitchen", brightness=60),
    ),
    # === climate ===
    EvalCase(
        name="climate_bedroom",
        utterance="Set the bedroom to 19 degrees.",
        expected_tools=["set_climate"],
        forbidden_tools=NO_ASK,
        check=_check_climate("bedroom", 19.0),
    ),
    EvalCase(
        name="climate_living_room_warm",
        utterance="Make the living room 22 degrees.",
        expected_tools=["set_climate"],
        forbidden_tools=NO_ASK,
        check=_check_climate("living_room", 22.0),
    ),
    EvalCase(
        name="climate_fahrenheit",
        # 68°F ≈ 20°C — the system prompt tells the model to convert.
        utterance="Set the bedroom to 68 Fahrenheit.",
        expected_tools=["set_climate"],
        forbidden_tools=NO_ASK,
        check=_check_climate("bedroom", 20.0, tolerance=0.6),
    ),
    # === query_state ===
    EvalCase(
        name="query_music",
        utterance="What's currently playing?",
        expected_tools=["query_state"],
        forbidden_tools=NO_ASK,
        setup=_seed_thriller_playing,
        check=_check_text_contains("thriller"),
    ),
    EvalCase(
        name="query_kitchen_light",
        utterance="Is the kitchen light on?",
        expected_tools=["query_state"],
        forbidden_tools=NO_ASK,
        setup=_seed_kitchen_on,
        check=_check_query_affirms_on,
    ),
    EvalCase(
        name="query_bedroom_climate",
        utterance="What's the bedroom set to?",
        expected_tools=["query_state"],
        forbidden_tools=NO_ASK,
        setup=_seed_bedroom_target,
        check=_check_text_contains("18"),
    ),
    # === parallel tool calls (one assistant message dispatches multiple) ===
    EvalCase(
        name="parallel_dim_and_jazz",
        utterance="Dim the living room lights to 30 percent and play some jazz.",
        # Three tools total: control_lights once, plus search_music + play_music.
        # The loop runs whatever the model emits in parallel via gather().
        expected_tools=["control_lights", "search_music", "play_music"],
        forbidden_tools=NO_ASK,
        check=_all(
            _check_light("living_room", brightness=30),
            _check_now_playing("$id:genre:jazz"),
        ),
    ),
    EvalCase(
        name="parallel_kitchen_off_living_off",
        utterance="Turn off the kitchen and living room lights.",
        expected_tools=["control_lights"],  # called twice, same name
        forbidden_tools=NO_ASK,
        setup=_seed_lights_on("kitchen", "living_room"),
        check=_check_lights_off("kitchen", "living_room"),
    ),
    EvalCase(
        name="parallel_climate_and_lights",
        utterance="Set the bedroom to 18 and turn off the kitchen light.",
        expected_tools=["set_climate", "control_lights"],
        forbidden_tools=NO_ASK,
        setup=_seed_lights_on("kitchen"),
        check=_all(
            _check_climate("bedroom", 18.0),
            _check_light("kitchen", on=False),
        ),
    ),
    # === disambiguation: ask_user IS required ===
    EvalCase(
        name="thriller_disambig",
        utterance="Play Thriller.",
        # search_music returns BOTH the song and the album. Without an explicit
        # "the album" / "the song" cue, the model should ask. Picker (seeded
        # in setup) returns the song id.
        expected_tools=["search_music", "ask_user", "play_music"],
        setup=_setup_thriller_disambig,
        check=_check_thriller_disambig,
    ),
    # === STT-mangled utterances (recovery via model world knowledge) ===
    #
    # Real `SFSpeechRecognizer` output is phonetically plausible but lexically
    # wrong. Our catalog token match is exact-substring, so all three queries
    # below return [] on first search. The recovery path is *the model* —
    # which already knows 'kween' isn't a band but 'Queen' is — retrying
    # search_music with a corrected spelling. No alt-tokens or fuzzy match
    # in the tool itself: extending the catalog can't anticipate every
    # mishearing in a 100M-track world. See DECISIONS.md "STT mangling".
    #
    # set(tools_called) == set(expected_tools) means multiple retries are
    # allowed silently — the assertion that matters is `_check_now_playing`,
    # which fails if the model gave up or landed on the wrong id.
    EvalCase(
        name="stt_mangle_queen",
        utterance="Put on some kween.",
        expected_tools=["search_music", "play_music"],
        forbidden_tools=NO_ASK,
        check=_check_now_playing("$id:artist:queen"),
    ),
    EvalCase(
        name="stt_mangle_thriller_song",
        # "the song" disambiguates after the retry surfaces both hits, so this
        # case isolates the mangling-recovery axis from the ask_user axis.
        utterance="Play thrill her, the song.",
        expected_tools=["search_music", "play_music"],
        forbidden_tools=NO_ASK,
        check=_check_now_playing("$id:song:thriller"),
    ),
    EvalCase(
        name="stt_mangle_miles_davis",
        utterance="Play some myles davis.",
        expected_tools=["search_music", "play_music"],
        forbidden_tools=NO_ASK,
        check=_check_now_playing("$id:artist:miles-davis"),
    ),
    # === graceful no-match ===
    EvalCase(
        name="no_match_lady_gaga",
        utterance="Play some Lady Gaga.",
        # Catalog doesn't have it. search_music returns []; agent should not
        # fabricate an id or call play_music — it should narrate the miss.
        # ask_user is the wrong move too (no choices to offer).
        expected_tools=["search_music"],
        forbidden_tools=("play_music", "ask_user"),
        check=_check_no_search_hits,
    ),
    # === outside scope ===
    EvalCase(
        name="outside_scope_weather",
        utterance="What's the weather like outside?",
        # No tool covers weather. Agent should narrate that it can't help
        # rather than reaching for query_state or fabricating data.
        expected_tools=[],
        forbidden_tools=NO_ASK,
        check=_check_outside_scope,
    ),
    # === cancellation ===
    EvalCase(
        name="cancel_during_play_jazz",
        utterance="Play some jazz.",
        # Runner fires cancel_token after first tool result (search_music).
        # Expectation: loop raises TurnCancelled cleanly without calling
        # play_music or the model a second time.
        expected_tools=[],  # ignored when cancel_after_first_tool=True
        cancel_after_first_tool=True,
    ),
]
