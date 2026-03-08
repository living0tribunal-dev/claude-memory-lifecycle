# Architecture

## The Problem

Analysis of 3,874 existing memory entries revealed 3 root problems:

1. **No lifecycle** — No process between storing and retrieving. Everything accumulates forever.
2. **No schema** — Flat text blobs with no structured metadata. "Tags" exist only as text in the document.
3. **Everything manual** — No automatic maintenance, no quality signal, no proactive retrieval.

These 11 symptoms converged on a single insight: memory needs a **lifecycle**, not a bigger database.

## Core Insight: Meta-Layer, Not a 6th Store

Claude Code already has 5 memory systems:

| System | Memory Type | Function |
|--------|------------|----------|
| Rules files | Procedural | Control behavior |
| CLAUDE.md | Working memory | Per-project context |
| _RESEARCH/ | Declarative/Semantic | Research knowledge |
| Git history | Version memory | Change history |
| claude-mem | Episodic | Long-term learnings |

The solution is not a 6th store — it's a **meta-layer** (knowledge orchestrator) that manages the lifecycle across all 5 existing systems.

## 7 Design Decisions

1. **Staging over Gate** — Value is unknown at write time. Everything enters a buffer; value reveals itself through connections and usage.
2. **Connection-based expiry over TTL** — Isolation determines decay, not time. Connected knowledge survives; isolated knowledge fades.
3. **Two promotion modes** — Convergent (clusters of 3+ connected entries consolidate) and divergent (isolated entries get a substance check — diamond protection).
4. **Token-overlap for noise** — Template clusters (session-saves with >80% word overlap) are detected mechanically. No LLM needed.
5. **Unified consolidation** — One LLM call handles merging, routing, deduplication, and consistency checking.
6. **Embedded anticipation** — Proactive briefing uses existing embeddings and RESUME_PROMPT.md content. No additional API call.
7. **Lazy migration** — The old system becomes a read-only archive. Useful entries migrate through recall, not batch import.

## 5-Phase Lifecycle

```
Phase 1: Buffer         Add → hash → store (fast, no model)
Phase 2: Connect        Embed (ONNX) → link similar entries (cosine ≥ 0.75)
Phase 3: Consolidate    Clusters of 3+ → LLM merge → proven → route to target system
Phase 4: Anticipate     Session start / topic switch → embedding search → briefing
Phase 5: Age            Isolated entries → substance check → reprieve or expire
```

## Consolidation Pipeline

```
buffer entries
    │
    ├── embed-pending (ONNX, local)
    │       │
    │       ├── connections (cosine ≥ 0.75)
    │       │       │
    │       │       ├── clusters (BFS, ≥ 3 connected, all pairs ≥ 0.80)
    │       │       │       │
    │       │       │       ├── noise filter (token overlap > 80%) → expire
    │       │       │       │
    │       │       │       └── knowledge cluster → consolidate (LLM)
    │       │       │               │
    │       │       │               ├── route (LLM → target system)
    │       │       │               │
    │       │       │               ├── conflict-check (LLM → action)
    │       │       │               │
    │       │       │               └── write-target → target system
    │       │       │
    │       │       └── isolated entries → age / diamond-check
    │       │               │
    │       │               ├── substance check (LLM) → reprieve (up to 3x)
    │       │               │
    │       │               └── no substance → expire
    │       │
    │       └── user-gedanke entries → never auto-expire
    │
    └── empty auto-session-saves → immediate expire
```

## Routing Matrix

| | Global | Project-specific |
|--|--------|-----------------|
| Imperative (enforceable) | Hook script | CLAUDE.md |
| Imperative (guideline) | Rules file | CLAUDE.md |
| Declarative | claude-mem | _RESEARCH/ |

## Technology Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Buffer store | SQLite (WAL mode) | Clean schema for lifecycle states + connection graph |
| Embeddings | Qwen3-0.6B ONNX INT8, 1024d | Best MMTEB score in class (64.33), fits in 500MB RAM |
| Consolidation LLM | Any with JSON mode (default: Gemini Flash) | Free tier sufficient for ~20 consolidations/day |
| Quality checker LLM | Any with chat API (default: Gemini Flash-Lite) | Free tier sufficient for ~1000 checks/day |
| Integration | Claude Code hooks API | Zero modifications to Claude Code |

## Database Schema

```sql
buffer_entries (id, text, text_hash UNIQUE, state, entry_type, project, recall_count, reprieve_count)
entry_embeddings (entry_id, embedding BLOB, model, dimensions)
connections (entry_a, entry_b, similarity, CHECK a < b)
routing_decisions (entry_id, target_system, action, resolution_text)
system_meta (key, value)
```

## Known Tradeoffs

- **No formal benchmarks** — Validated through real use, not LoCoMo/LongMemEval (different optimization target).
- **Single user** — No multi-user, no concurrency handling.
- **Embedding model size** — Qwen3-0.6B chosen for laptop RAM constraints. Larger models would improve connection quality.
- **Free tier LLM** — Rate-limited to ~1000 quality checks and ~20 consolidations per day.

## Theoretical Foundation

Complementary Learning Systems theory (McClelland et al., 1995): fast hippocampal learning (buffer) complemented by slow neocortical integration (consolidation into persistent storage), connected by replay (the consolidation pipeline). Entries that form connections survive consolidation; isolated entries face decay — mirroring how connected memories survive hippocampal replay while isolated ones don't.
