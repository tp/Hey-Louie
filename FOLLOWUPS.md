# Follow-Ups

## Add code execution

- See if we can give the model access to APIs to be executed by the agent, so only local commands would need to round-trip through the local app.
  - E.g. it would be cool to say "play new Lady Gaga" album, and then it could figure that out via the Qobuz API and just send the final ID back to the client.
- Also for tasks which LLMs can not handle, e.g. find the most listened-to song from last month. Even with a tool for the play history available, it'd need a bit of code to reliably count the top playing one.

## Eval case refinements

- More detailed tool use assertions (e.g. parameters)
- More specific final checks; unique/inline per test case, for improved readability?

## Latency: prompt caching on both adapters

The Day-3 sweep showed 3-10s eval-only latency before any network / STT / TTS / WS overhead is added — too slow for conversational voice. Biggest single lever: prompt caching. Every turn re-sends ~5k input tokens of tool catalog + system prompt; with Anthropic `cache_control` blocks and OpenAI's automatic caching, the prefill cost (and most of the latency on those tokens) drops dramatically after the first turn. Add to `adapters/anthropic.py` and `adapters/openai.py`; re-run the sweep and compare CSVs.

## Latency: smaller / faster models for this scope

The agent's "world knowledge" footprint is intentionally tiny — a fixed tool catalog, a ~10-entry music library, three rooms. Frontier models are over-spec'd for picking between 6 tools and resolving a Thriller-song-vs-album disambiguation. Worth running the eval suite against:

- Claude Haiku 4.5 (the obvious fair fight against GPT-5-mini)
- gpt-5-nano (if it exists by then) or `gpt-4.1-mini`
- An open model on Modal w/ vLLM — Qwen-3, Llama, Mistral at ~3B-8B. The blog post already wants to mention this; this would put real numbers behind it.

The eval suite is exactly the right shape for this — same cases, swap adapter, compare CSVs. Recognize artist/song names is the main capability load; if a small model handles "Bohemian Rhapsody" → `$id:track:bohemian-rhapsody` it's plenty.
