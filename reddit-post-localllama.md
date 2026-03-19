# Reddit Post — r/LocalLLaMA

## Titel
Local Qwen3-0.6B INT8 as embedding backbone for an AI memory system

## Flair
Discussion

## Body

Most AI coding assistants solve the memory problem by calling an embedding API on every store and retrieve. This does not scale. 15-25 sessions per day means hundreds of API calls, latency on every write, and a dependency on a service that can change pricing at any time.

I needed embeddings for a memory lifecycle system that runs inside Claude Code. The system processes knowledge through 5 phases: buffer, connect, consolidate, route, age. Embeddings drive phases 2 through 4 (connection tracking, cluster detection, similarity routing).

Requirements: 1024-dimensional vectors, cosine similarity above 0.75 must mean genuine semantic relatedness, batch processing for 20+ entries, zero API calls.

I tested several models and landed on Qwen3-0.6B quantized to INT8 via ONNX Runtime. Not the obvious first pick. Sentence-transformers models seemed like the default choice, but Qwen3-0.6B at 1024d gave better separation between genuinely related entries and structural noise (session logs that share format but not topic).

The cold start problem: ONNX model loading takes ~3 seconds. For a hook-based system where every tool call can trigger an embedding check, that is not usable. Solution: a persistent embedding server on localhost:52525 that loads the model once at system boot. Warm inference: ~12ms per batch, roughly 250x faster than cold start.

The server starts automatically via a startup hook. If it goes down, the system falls back to direct ONNX loading. Nothing breaks, it just gets slower.

What the embeddings enable:
- **Connection graph**: new entries get linked to existing entries above 0.75 cosine similarity. Isolated entries fade over time. Connected entries survive. Expiry based on isolation, not time.
- **Cluster detection**: groups of 3+ connected entries get merged into proven knowledge by an LLM (Gemini Flash free tier for consolidation).
- **Similarity routing**: proven knowledge gets routed to the right config file based on embedding distance to existing content.

All CPU, no GPU needed. The 0.6B model runs on any modern machine. Single Python script, ~2,900 lines, SQLite + ONNX.

Open source: [github.com/living0tribunal-dev/claude-memory-lifecycle](https://github.com/living0tribunal-dev/claude-memory-lifecycle)

Full engineering story with threshold decisions and failure modes: [After 3,874 Memories, My AI Coding Assistant Couldn't Find Anything Useful](https://dev.to/living0tribunal/after-3874-memories-my-ai-coding-assistant-couldnt-find-anything-useful-1cc)

Anyone else using small local models for infrastructure rather than generation? Embeddings feel like the right use case for sub-1B parameters.
