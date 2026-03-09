---
title: "After 3,874 Memories, My AI Coding Assistant Couldn't Find Anything Useful"
published: false
description: "Why AI memory systems need to forget, and how a connection-based lifecycle solves this for Claude Code."
tags: ai, opensource, python, productivity
---

# After 3,874 Memories, My AI Coding Assistant Couldn't Find Anything Useful

After several months of daily Claude Code use, my memory system contained 3,874 entries. Semantic search worked fine technically. But when I actually needed a specific piece of knowledge, what came back was noise: session logs, outdated facts, duplicate entries, auto-saves with zero content.

I analyzed the data. 80% of entries had no tags. 81% came from a single month. Only 32% had embeddings, which meant semantic search was blind to two-thirds of everything stored. The system stored everything and forgot nothing.


## Every AI Memory System Has the Same Blind Spot

I surveyed 17 memory systems for LLM coding assistants. They all do roughly the same thing: store entries, embed them, retrieve by similarity. Some add categories, some add importance scores. Fewer than half have a mechanism for forgetting. And those that do almost always use time-based expiry.

Human memory works because the hippocampus consolidates connected memories and lets isolated ones fade. That is not a metaphor; it is Complementary Learning Systems theory (McClelland et al., 1995). Your brain runs a two-speed system: fast learning in the hippocampus, slow integration into the neocortex, connected by replay during sleep. Memories that get replayed survive. Memories that don't connect to anything fade.

AI memory systems skip this entirely. They are all hippocampus, no neocortex.


## The Design Insight: Connection, Not Time

So forgetting is necessary. But how should an AI system decide what to forget?

Most systems that implement forgetting use time-based expiry: TTL, Ebbinghaus decay curves, epoch counters. An entry fades after N days regardless of whether anything references it.

I considered Ebbinghaus decay and explicitly rejected it. The problem I observed in my data was not "old entries are stale." It was "unconnected entries are noise." A months-old API endpoint that is still referenced by other entries is valuable. Yesterday's typo fix that connects to nothing is noise. Time does not determine value; connections do.

The design insight that drives the entire architecture: an entry should expire because nothing connects to it, not because it is old. Isolation, not age.


## A Five-Phase Memory Lifecycle

I built a lifecycle system for Claude Code over 28 iterative sessions. Everything runs through the official hooks API, zero modifications to Claude Code itself. Hooks were a deliberate constraint: any system that requires forking the host tool becomes unmaintainable across updates.

**1. Buffer.** Every piece of information goes in without judgment. No gate, no quality check at write time, because value reveals itself through connections later, not through signal words at entry time. This was confirmed during red-teaming: the most valuable insights often lack obvious markers. Writes stay under 50ms.

**2. Connect.** New entries get embedded locally and linked to similar entries above a cosine similarity threshold of 0.75. That threshold was calibrated empirically; starting at 0.65 produced spurious cross-topic connections that polluted the graph.

**3. Consolidate.** When 3 or more connected entries form a cluster, an LLM merges them into a single proven entry. The originals expire. The minimum of 3 prevents premature merging; with 2, any pair of loosely similar entries would consolidate before enough evidence accumulates. A deterministic noise filter (Jaccard similarity on word sets, threshold 80%) catches template clusters like session logs before they reach the LLM, avoiding unnecessary API calls for a signal that does not need human-level judgment.

**4. Anticipate.** On session start or topic switch, the system surfaces relevant entries automatically by embedding the current context and matching against the buffer. No extra API call needed; the system reuses existing embeddings.

**5. Age.** Entries that stay isolated face a substance check. Valuable loners get reprieved, up to 3 times (the limit prevents limbo: without it, an entry could receive infinite reprieves and never fade). Entries that fail the check expire. User thoughts and decisions are protected by a hard rule and never auto-expire.

There is a subtlety in Phase 5. Some knowledge is valuable precisely because it is unique: a rare insight that does not cluster with anything. Pure connection-based expiry would destroy it. That is why the substance check exists. Before expiring an isolated entry, the system asks: does this contain specific, actionable knowledge? Entries that pass receive protection. Unlike permanent-memory flags where you decide upfront what is important, this "diamond protection" discovers valuable loners automatically during the aging process.


## Engineering Decisions

The connection graph that drives the entire lifecycle depends on embedding quality. If similarities are wrong, entries cluster incorrectly or expire when they should not. That made the embedding model choice critical.

### Embedding Model

The system runs on a 6GB RAM laptop (Intel i5-8250U, no GPU), which rules out most high-quality embedding models. I evaluated four candidates:

| Model | MMTEB Score | RAM | Issue |
|-------|------------|-----|-------|
| all-MiniLM-L6-v2 | ~56 | ~100MB | Low quality for multilingual content |
| BGE-M3 | 59.56 | ~2.2GB | Exceeds hardware constraint |
| EmbeddingGemma-300M | 61.15 | ~1.2GB | Silent wrong scores with certain library versions; 164x slowdown on whitespace |
| **Qwen3-0.6B (INT8)** | **64.33** | **~560MB** | Highest quality within constraint |

Qwen3-0.6B scored highest on MMTEB Multilingual and, with ONNX INT8 quantization, fit within the RAM budget. The EmbeddingGemma issues were found through structured red-teaming and confirmed via upstream bug reports.

I use the full 1024-dimensional embeddings despite the model's Matryoshka support for reduced dimensions. Empirical testing (Sack, 2025) showed that 256-dimensional Matryoshka embeddings retain only 57% of top-10 retrieval overlap. For connection tracking, where borderline similarities determine cluster membership versus expiry, that loss is unacceptable.

### The External Checker

A separate LLM reviews Claude's output after every response, checking against methodology rules. Why a separate LLM? Because self-checking does not work. A prompt that tells Claude "check your own output" runs inside the same context window; Claude can rationalize violations away. The checker runs outside Claude's process entirely. Violations get injected into the next prompt as system context. This is the difference between asking someone to grade their own homework and having an external reviewer.

The default checker model is Gemini 3.1 Flash-Lite on the free tier, providing approximately 1,000 checks per day at zero cost.


## Real Numbers

28 sessions of daily use. 9 automated checks against the live database:

| Metric | Result |
|--------|--------|
| Type detection accuracy | 100% (71/71) |
| Auto-expire precision | 0 false positives, 0 false negatives |
| Cross-project connections | 0 (write-time filter) |
| Cluster density | 93% connected |
| Promotion rate | 5.6% |
| Entries expired | 24 (empty saves, old logs, unlinked noise) |

No academic benchmarks. Those measure retrieval accuracy on synthetic datasets. This system optimizes for lifecycle quality in daily practice.


## Stack and Source

| Component | Choice | Why |
|-----------|--------|-----|
| Buffer store | SQLite (WAL mode) | Single file, no server, WAL allows concurrent reads from parallel hooks |
| Embeddings | Qwen3-0.6B ONNX INT8 | Highest MMTEB score within 6GB RAM constraint |
| Consolidation | Any LLM with JSON mode | Default: Gemini Flash free tier, zero cost, ~1000 calls/day |
| Quality checker | Gemini 3.1 Flash-Lite | External process, free tier, ~1000 checks/day |
| Integration | Claude Code hooks API | Survives version upgrades without changes |

The entire system is a single Python script (~2,300 lines) plus hook scripts. No LangChain, no vector database, no infrastructure beyond SQLite and ONNX.

The system is open source: [github.com/living0tribunal-dev/claude-memory-lifecycle](https://github.com/living0tribunal-dev/claude-memory-lifecycle)

A detailed technical report is available in the repository.

One observation stayed with me after building this. I started from practical problems: noise, no lifecycle, no forgetting. I worked bottom-up, from data to patterns to architecture. The resulting system turned out to mirror Complementary Learning Systems theory from neuroscience: fast buffer learning, slow consolidation into persistent storage, connected by replay. Whether that reflects a deep structural constraint on how knowledge management must work, or simply the obviousness of buffer-plus-consolidation as a pattern, is a question I cannot answer. But the convergence is there.
