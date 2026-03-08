# Installation

## Prerequisites

- Python 3.10+
- Claude Code CLI ([install guide](https://docs.anthropic.com/en/docs/claude-code))
- ~500MB free disk space (for the ONNX embedding model)
- LLM API key for consolidation and quality checking (default: Gemini free tier)

## Step 1: Clone the Repository

```bash
git clone https://github.com/living0tribunal-dev/claude-memory-lifecycle.git
cd claude-memory-lifecycle
```

## Step 2: Install Python Dependencies

```bash
pip install onnxruntime tokenizers transformers google-genai
```

## Step 3: Set Up the Embedding Model

```bash
python memory/memory-buffer.py setup-model
```

This downloads Qwen3-0.6B ONNX INT8 (~500MB) to `~/.claude-mem/models/`.

## Step 4: Copy Files to Claude Code Directories

```bash
# Core memory scripts
cp memory/memory-buffer.py ~/.claude/scripts/
cp memory/claude-mem.py ~/.claude/scripts/

# Hook scripts (adjust paths as needed)
cp hooks/integration/*.py ~/.claude/hooks/
cp hooks/quality/*.py ~/.claude/hooks/
cp hooks/safety/*.py ~/.claude/hooks/
```

## Step 5: Configure Hooks

Copy `examples/settings.json.example` to `~/.claude/settings.json`, or merge the `hooks` section into your existing settings.

The example file contains all hook event mappings with the correct matchers and timeouts.

## Step 6: Set Environment Variables

For the default Gemini setup:

```bash
# Add to your shell profile (.bashrc, .zshrc, etc.)
export GEMINI_API_KEY="your-gemini-api-key-here"
```

Get a free Gemini API key at [AI Studio](https://aistudio.google.com/).

Alternatively, add the key to `~/.claude/settings.json`:

```json
{
  "env": {
    "GEMINI_API_KEY": "your-gemini-api-key-here"
  }
}
```

## Step 7: Verify Installation

```bash
# Check the buffer system
python ~/.claude/scripts/memory-buffer.py status

# Test embedding
python ~/.claude/scripts/memory-buffer.py add "Test entry for installation verification"
python ~/.claude/scripts/memory-buffer.py embed-pending
python ~/.claude/scripts/memory-buffer.py search "test installation"

# Verify the test entry appears in search results
python ~/.claude/scripts/memory-buffer.py status
```

## Step 8: Start Claude Code

```bash
claude
```

The hooks activate automatically. On the first `UserPromptSubmit`, you should see system reminders from the focus-nudge and context-watchdog hooks.

## Using a Different LLM

The system defaults to Gemini but any LLM with JSON mode can be used. To swap:

1. Open `memory/memory-buffer.py`
2. Find the `gemini_generate()` function
3. Replace the API call with your preferred LLM's SDK
4. Ensure the function returns parsed JSON (the consolidation and routing pipelines depend on structured output)

## Troubleshooting

**"No module named onnxruntime"** — Run `pip install onnxruntime` (not `onnxruntime-gpu` unless you have CUDA).

**Embedding takes too long** — First run downloads and caches the tokenizer. Subsequent runs are ~1s for 10 entries.

**Gemini rate limit errors** — The free tier allows ~500 calls/day per key. Use two keys with round-robin (see the `gemini_generate()` wrapper) for ~1000 calls/day.

**Hooks not triggering** — Check `~/.claude/settings.json` for correct hook paths. Paths must be absolute. Check `~/.claude/hooks/hook-debug.log` for error output.
