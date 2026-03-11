#!/usr/bin/env python3
"""Violation Enforcer — Deferred Gemini violation enforcement.

PreToolUse hook for Write/Edit. Reads gemini-checker result file.
If unresolved violation exists, blocks the tool call (exit code 2).

Closes the enforcement gap: Gemini detects violations (async Stop),
this hook enforces them (sync PreToolUse).
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

RESULT_FILE = Path.home() / ".claude" / "state" / "gemini-checker-result.json"
LOG_DIR = Path.home() / ".claude" / "logs"
MAX_BLOCKS = 2
EXPIRY_SECONDS = 120
# PLAN-GATE already has mechanical blocker in stop-self-check.py (55+ blocks)
SKIP_RULES = {"PLAN-GATE"}


def log(message):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_DIR / "hook-execution.log", "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] violation-enforcer: {message}\n")
    except Exception:
        pass


def main():
    # Consume stdin (required by hook protocol)
    try:
        sys.stdin.read()
    except Exception:
        pass

    # Read Gemini checker result
    try:
        result = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except Exception:
        sys.exit(0)

    if not result.get("violation"):
        sys.exit(0)

    # Check expiry
    age = time.time() - result.get("timestamp", 0)
    if age > EXPIRY_SECONDS:
        log(f"Expired ({age:.0f}s > {EXPIRY_SECONDS}s) -> allow")
        sys.exit(0)

    # Filter out already-handled rules
    rules = [r for r in result.get("rules_violated", []) if r not in SKIP_RULES]
    if not rules:
        log("Only PLAN-GATE (already handled) -> allow")
        sys.exit(0)

    # Check enforcement counter
    enforced_count = result.get("enforced_count", 0)
    if enforced_count >= MAX_BLOCKS:
        log(f"Max blocks reached ({enforced_count}/{MAX_BLOCKS}) -> allow")
        sys.exit(0)

    # Block: increment counter and save
    result["enforced_count"] = enforced_count + 1
    try:
        RESULT_FILE.write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass

    reason = result.get("reason", "?")
    rules_str = ", ".join(rules)
    log(f"BLOCKING ({enforced_count + 1}/{MAX_BLOCKS}): [{rules_str}] — {reason}")

    output = {
        "hookSpecificOutput": {
            "message": (
                f"VIOLATION-ENFORCER ({enforced_count + 1}/{MAX_BLOCKS}): "
                f"[{rules_str}] — {reason}"
            )
        }
    }
    print(json.dumps(output))
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(0)  # fail-open
