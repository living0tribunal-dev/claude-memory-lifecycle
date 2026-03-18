# Reddit Post — r/ClaudeCode

## Titel
I built a memory lifecycle system that lets Claude Code forget

## Body

After a few months of daily use, my claude-mem had 3,874 entries. Most of them were noise: session logs, duplicates, outdated facts. Semantic search technically worked, but the results were useless because everything competed for attention.

The core problem: Claude Code stores everything and forgets nothing.

I built a lifecycle system that runs entirely through hooks (zero modifications to Claude Code):

- **Buffer** — everything goes in without judgment
- **Connect** — local embeddings (Qwen3-0.6B ONNX, no API) build a connection graph
- **Consolidate** — clusters of related entries get merged into proven knowledge by an LLM
- **Age** — isolated entries face a substance check. Connected knowledge survives. Unconnected noise fades.

The key idea: expiry is based on isolation, not time. A months-old API endpoint that's still referenced by other knowledge stays. Yesterday's typo fix that connects to nothing fades.

28 sessions of real use. 93% cluster density. 5.6% promotion rate. Single Python script, ~2,300 lines, SQLite + ONNX.

Open source: [github.com/living0tribunal-dev/claude-memory-lifecycle](https://github.com/living0tribunal-dev/claude-memory-lifecycle)

Detailed blog post with engineering decisions: [After 3,874 Memories, My AI Coding Assistant Couldn't Find Anything Useful](https://dev.to/living0tribunal/after-3874-memories-my-ai-coding-assistant-couldnt-find-anything-useful-1cc)

Curious if anyone else has run into the same memory noise problem. What's your current approach?
