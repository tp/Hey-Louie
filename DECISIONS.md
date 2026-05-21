# Decisions

## Tools over code execution

For now we focus only on providing tools to the agent, even though that obviously limits it capabilites.

## Add `search_music` tool

Split music into search_music + play_music. Reason: returning a query string from the agent leaves the lookup work undefined; a real client (Qobuz, Sonos) exposes search and play as separate calls. Bumps tool count from 5 to 6. Worth it: forces multi-step tool use into the eval suite from Day 1.

## `search_music` uses a tiny in-memory catalog, not synthesized IDs

`search_music` returns hits from a 3–5 entry catalog instead of synthesizing an ID from the query string. Synthesis is one line of code, but a catalog (a) lets Day 3 stage the "play Thriller — song or album?" disambiguation case cleanly without special-casing, and (b) makes the eventual swap to a real provider (Qobuz, Apple Music) a like-for-like replacement of the lookup table rather than a tool-shape change. Cost is ~10 lines of dict literals.

## `search_music` dispatches to the iPad too — not local

Claude originally assumed `search_music` would stay server-local because a real backend "would call a catalog API." That's wrong for this project: the music library only exists on the iPad (no server-side catalog API is in scope), so on Day 2 `search_music` flips to `local=False` alongside the four house tools. Every tool round-trips. This is actually a useful constraint to write about — it mirrors how Claude Code and similar coding agents work: the model runs on a server but every interesting tool (read file, run command, inspect state) executes on a physically-local machine that holds the real data. The agent harness pays a latency cost for that round-trip and earns the right to never see the data directly. Worth a paragraph in the blog post.

## `play_music` looks up the title in the catalog, not a session registry

The ID returned by `search_music` is shaped `$id:<type>:<slug>` (e.g. `$id:genre:jazz`). `play_music` validates the id against the catalog and uses the canonical title stored there — it does NOT carry a session-scoped registry of prior `search_music` results, and it does NOT parse the slug to reconstruct the title (an earlier attempt that gave approximate results like `bohemian-rhapsody` → `Bohemian Rhapsody`). Reason: matches how the real iPad will behave — the device holds the library and can resolve any valid id to its display title regardless of which queries preceded it, so the backend's mock should behave the same. `FakeLouie.music` stores both `now_playing_id` (stable, asserted in evals) and `now_playing_title` (human-facing, read back by `query_state`). The pair is always set together.
