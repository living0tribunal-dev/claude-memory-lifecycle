# Claude Memory System

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.18926259.svg)](https://doi.org/10.5281/zenodo.18926259)

**A cognitive layer for Claude Code — memory lifecycle, quality assurance, and safety — built entirely with hooks.**

Most LLM memory systems store everything and forget nothing. The result: after a few weeks, your memory files are full of duplicates, outdated facts, and noise. The real problem isn't storage — it's *forgetting*.

Human memory works because the hippocampus consolidates connected memories and lets isolated ones fade ([McClelland et al., 1995](https://pubmed.ncbi.nlm.nih.gov/7624455/)). This system implements the same principle for Claude Code: entries that connect to other knowledge survive. Entries that remain isolated fade away.

```
                        ┌──────────────────────────────────┐
                        │          Claude Code             │
                        ├──────────────────────────────────┤
                        │         Hooks Layer              │
                        │  ┌────────┐┌────────┐┌────────┐ │
                        │  │Quality ││Memory  ││Safety  │ │
                        │  │Checker ││Integr. ││Guards  │ │
                        │  └────────┘└────────┘└────────┘ │
                        ├──────────────────────────────────┤
                        │     Memory Buffer (SQLite)       │
                        │                                  │
                        │  Add → Embed → Connect →         │
                        │    Consolidate → Route → Age     │
                        │                                  │
                        ├──────────────────────────────────┤
                        │       Target Systems             │
                        │ CLAUDE.md │ Rules │ Research     │
                        └──────────────────────────────────┘
```

## How It Works

Every piece of information passes through a 5-phase lifecycle:

**1. Buffer** — Everything goes in. No gate, no judgment. Fast writes, no model needed.

**2. Connect** — New entries get embedded (Qwen3-0.6B, ONNX, local) and linked to similar entries above a cosine similarity threshold (0.75).

**3. Consolidate** — Clusters of 3+ connected entries get merged into proven knowledge by an LLM (any LLM with JSON mode; default: Gemini Flash free tier). The originals expire.

**4. Route** — Proven knowledge gets classified by target system (CLAUDE.md, rules files, research docs) and written to the right place. Conflicts with existing content are detected before writing.

**5. Age** — Isolated entries face a substance check. Valuable loners get reprieved (up to 3 times). Entries that remain isolated and unsubstantial fade away. User thoughts (`#user-gedanke`) are protected and never auto-expire.

## What Makes This Different

### Connection-Based Expiry

Most memory systems use time-based expiry — TTL or decay functions like Ebbinghaus curves. An entry fades after N epochs regardless of its connections. We use **isolation**: an entry expires because nothing connects to it. A months-old API endpoint that's still referenced by other knowledge stays. Yesterday's typo fix that connects to nothing fades.

### Diamond Protection

Some entries are valuable *because* they're unique — they don't cluster with anything. Before expiring an isolated entry, a substance check evaluates whether it contains genuine, standalone knowledge. Valuable loners get reprieved (up to 3 times). Unlike static permanent-memory flags (where the user decides upfront what's important), diamond protection is automatic — the system discovers valuable loners during the aging process.

### Memory Type Classification

Entries are automatically classified from content at write time:

| Type | Detection | Behavior |
|------|-----------|----------|
| `decision` | `#decision` or `#entscheidung` tag | Never auto-expires (decisions are foundational — format: WHAT + BECAUSE + CONSEQUENCE) |
| `user-gedanke` | `#user-gedanke` tag | Never auto-expires (user's explicit thoughts are sacred) |
| `session-save` | `#session-save` tag | Normal lifecycle — consolidation and aging |
| `auto-session-save` | `AUTO-SESSION-SAVE` prefix | Immediately expired if empty (0 user messages) |
| `insight` | None of the above | Normal lifecycle |

### Proactive Briefing

On session start or topic switch, relevant buffer entries are surfaced automatically. The system embeds the current `RESUME_PROMPT.md` content and finds similar entries — no API call needed. A secondary consistency check flags contradictions between loaded entries.

### LLM-as-Checker

A separate LLM reviews Claude's output after every response, checking against methodology rules. This is a **mechanism**, not a prompt — Claude can't ignore it because it runs outside Claude's context. The default is Gemini Flash-Lite (free tier, 1000 calls/day), but any LLM with a chat API works.

### Hook-Based Architecture

Zero modifications to Claude Code. Everything runs through the [official hooks API](https://docs.anthropic.com/en/docs/claude-code/hooks):

| Hook Event | What Runs |
|------------|-----------|
| `UserPromptSubmit` | Context injection, focus checks, Gemini violation feedback, write-gate reset |
| `PreToolUse` | 3-gate checks, write-gate enforcement |
| `PostToolUse` | Read tracking, agent result persistence |
| `Stop` | Self-checks, Gemini quality checker |
| `PreCompact` | Session save, embedding updates, aging pipeline, graceful shutdown |

## Architecture

The system has three layers that work independently:

### Memory Layer
- `memory-buffer.py` — Core: buffer, embeddings, connections, consolidation, routing, aging, briefing, diamond protection, migration
- `auto-session-save.py` — Captures session context on PreCompact
- `subtopic-awareness.py` — Detects topic switches, triggers re-briefing
- `context-watchdog.py` — Warns before auto-compact erases context
- `agent-results-persist.py` — Saves agent results to `ARTIFACTS/` directory

### Quality Layer
- `gemini-checker.py` — Gemini reviews Claude's responses against rules (async, two-mode: check + inject)
- `stop-self-check.py` — Pattern-based self-check (length, workaround detection)
- `write-gate.py` — Blocks writes if Claude hasn't read enough files first (Gate-3 mechanism)
- `research-gate.py` — Enforces research workflow (inventory before research)
- `focus-nudge.py` — Periodic focus checks

### Safety Layer
- `block-secrets.py` — Prevents committing secrets
- `settings-guard.py` — Protects settings.json from corruption
- `claudemd-guard.py` — Protects CLAUDE.md from accidental overwrites
- `pretool-3gate.py` — Injects 3-gate reminder before tool use
- `circuit-breaker.py` — Stops runaway loops
- `loop-detector.py` — Detects repetitive tool call patterns
- `graceful-shutdown.py` — Clean shutdown on context limit

## Real-World Numbers

| Metric | Value |
|--------|-------|
| Total entries processed | ~5,400 (claude-mem) + 71 (buffer) |
| Development sessions | 25 |
| Active buffer entries | 43 |
| Noise removed | 24 entries expired, 491 connections pruned (expired + cross-project) |
| Connection threshold | 0.75 cosine similarity |
| Embedding model | Qwen3-0.6B ONNX INT8 — local, no API, ~1s for 10 entries, ~500MB RAM (chosen for laptop compatibility; larger models improve quality but require more RAM) |
| Consolidation model | Any LLM with JSON mode (default: Gemini 2.5 Flash, free tier) |
| Quality checker model | Any LLM with chat API (default: Gemini 3.1 Flash-Lite, free tier, 1000 RPD) |
| Memory types | 5 (auto-detected from content) |

## Empirical Evaluation

The system is validated by [`eval.py`](eval.py), which runs 9 automated checks against the live database:

| Metric | Result | What It Checks |
|--------|--------|----------------|
| Cross-project connections | 0 | Write-time filter prevents false links between unrelated projects |
| Type detection accuracy | 71/71 (100%) | Deterministic classifier matches expected type for every entry |
| Auto-expire precision | 0 FP, 0 FN | Empty auto-session-saves expire; non-empty ones survive |
| User-gedanke protection | 5/5 protected | User's explicit thoughts never auto-expire |
| Connection discrimination | 276 intra, 0 cross | Connections form within projects, not across them |
| Cluster density | 93% connected | Most buffer entries link to at least one other entry |
| Promotion rate | 5.6% | Selective: only well-connected, consolidated knowledge advances |
| Aging audit | 24 expired | Expired entries are empty saves (8), old session-saves (10), unlinked insights (5) |
| Recall tracking | 5 entries recalled | Search-driven recall counts feed the promotion pathway |

No academic benchmarks (LoCoMo, LongMemEval) — those measure retrieval accuracy on synthetic datasets. This system optimizes for a different goal: memory lifecycle quality in real daily use across 25+ sessions.

## Design Philosophy

**Bottom-Up.** We started by analyzing 3,874 existing memory entries and found 3 root problems: no forgetting mechanism, no quality signal, no proactive retrieval. The architecture emerged from the problems, not from a framework.

**Mechanism over Prompt.** A prompt is a request. A mechanism is a fact. The write-gate doesn't *ask* Claude to read before writing — it *blocks the write* if Claude hasn't read. The Gemini checker doesn't *suggest* rule compliance — it *reports violations* into the next prompt.

**Connection over Time.** Knowledge doesn't have an expiration date. It has a relevance signal: its connections to other knowledge. This mirrors hippocampal consolidation — connected memories survive replay, isolated ones don't.

**No Framework.** SQLite, ONNX, and the Claude Code hooks API. No LangChain, no vector database, no infrastructure. The entire system is a single Python script (~2,300 lines) plus hook scripts.

## Comparison

| System | Memory Model | Expiry | Quality Check | Integration |
|--------|-------------|--------|---------------|-------------|
| **This system** | Connection-based lifecycle | Isolation + substance check | LLM cross-check | Hooks (no fork) |
| [claude-mem](https://github.com/thedotmack/claude-mem) | Observation capture + compress | None | LLM compression | Claude Code hooks |
| [engram-rs](https://github.com/kael-bit/engram-rs) | Atkinson-Shiffrin 3-layer | Ebbinghaus decay (3 half-lives) | LLM quality gate | MCP + CLI (Rust) |
| [engram-ai-memory](https://github.com/foramoment/engram-ai-memory) | 5-type knowledge graph | Ebbinghaus + permanent exemptions | Noise gate | MCP server |
| Claude auto-memory | Flat files, append-only | None | None | Built-in |
| [Copilot Memory](https://docs.github.com/en/copilot) | Citation-verification | Self-healing | Runtime citation check | Built-in |
| [SimpleMem](https://arxiv.org/abs/2601.02553) | CLS-theory | Decay function | Benchmarked | Research prototype |
| [MemOS](https://github.com/MemOS) | Governance + TTL | Time-based + policy | Conflict detection | Framework |

## Prerequisites

- Python 3.10+
- Claude Code CLI
- ~500MB disk space (ONNX model)
- LLM API key for consolidation and quality checking (default: Gemini free tier — [AI Studio](https://aistudio.google.com/))

## Installation

See [docs/installation.md](docs/installation.md) for detailed setup instructions.

Quick overview:
1. Clone this repository
2. Run model setup: `python memory/memory-buffer.py setup-model`
3. Configure hooks in `~/.claude/settings.json` (see [examples/settings.json.example](examples/settings.json.example))
4. Set `GEMINI_API_KEY` environment variable (for the default Gemini setup)
5. Start Claude Code — the system activates automatically

## Limitations

- **Single user.** Designed for one person's workflow. No multi-user support.
- **LLM dependency.** Consolidation and quality checking require an LLM API. Gemini is the default (free tier, ~1000 calls/day), but any LLM with JSON mode can be substituted by changing the `gemini_generate()` wrapper.
- **No formal benchmarks.** Validated through 25 sessions of real use, not LoCoMo or LongMemEval.
- **Claude Code specific.** The hooks API is specific to Claude Code. Adapting to other tools requires reimplementing the integration layer.
- **English/German.** Prompts and rules are partially in German (the developer's language). Internationalization is not implemented.

## Background

This system was developed over 25 iterative sessions using a bottom-up methodology: analyze real data, identify real problems, build minimal solutions, verify empirically, then iterate. The full design process — from analyzing 3,874 legacy entries to the current 5-phase architecture — is documented in the [research notes](docs/architecture.md).

The theoretical foundation draws from Complementary Learning Systems theory (McClelland et al., 1995): fast hippocampal learning (buffer) complemented by slow neocortical integration (consolidation into persistent storage), connected by replay (the consolidation pipeline).

## License

MIT
