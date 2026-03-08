#!/usr/bin/env python3
"""Write-Gate: Verhindert grosse Writes ohne vorheriges Lesen.

Gate-3 Mechanismus (Stufe 1 — Hook, nicht umgehbar).
Zaehlt Read-Aufrufe pro User-Prompt. Blockt grosse Writes wenn zu wenig gelesen wurde.

Modes:
  reset       → UserPromptSubmit: Read-Counter auf 0 setzen
  track-read  → PostToolUse:Read: Gelesene Datei registrieren
  check-write → PreToolUse:Write: Grosse Writes ohne genug Reads blocken
"""

import sys
import json
import os
import time

# Schwellen
LARGE_WRITE_THRESHOLD = 5000   # Zeichen — darunter kein Check
MIN_READS_FOR_LARGE_WRITE = 3  # Unique Dateien

# State
STATE_DIR = os.path.join(os.environ.get("TEMP", "/tmp"), "claude-write-gate")
STATE_FILE = os.path.join(STATE_DIR, "state.json")


def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {"reads": [], "last_reset": ""}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)


def get_hook_input():
    try:
        return json.loads(sys.stdin.read())
    except Exception:
        return {}


def handle_reset():
    """UserPromptSubmit: Reset read counter."""
    get_hook_input()  # consume stdin
    state = {"reads": [], "last_reset": time.strftime("%Y-%m-%dT%H:%M:%S")}
    save_state(state)
    sys.exit(0)


def handle_track_read(hook_input):
    """PostToolUse:Read: Track file path."""
    tool_input = hook_input.get("tool_input", {})
    file_path = tool_input.get("file_path", "")

    if not file_path:
        sys.exit(0)

    state = load_state()
    if file_path not in state["reads"]:
        state["reads"].append(file_path)
        save_state(state)

    sys.exit(0)


def handle_check_write(hook_input):
    """PreToolUse:Write: Block large writes without enough reads."""
    tool_input = hook_input.get("tool_input", {})
    content = tool_input.get("content", "")

    content_len = len(content)

    if content_len < LARGE_WRITE_THRESHOLD:
        sys.exit(0)

    state = load_state()
    read_count = len(state.get("reads", []))

    if read_count < MIN_READS_FOR_LARGE_WRITE:
        output = {
            "hookSpecificOutput": {
                "message": (
                    f"GATE-3 MECHANISMUS: Du schreibst {content_len} Zeichen "
                    f"aber hast nur {read_count} Datei(en) in diesem Prompt gelesen. "
                    f"Minimum: {MIN_READS_FOR_LARGE_WRITE} Reads vor einem grossen Write. "
                    f"Lies zuerst die Quellen."
                )
            }
        }
        print(json.dumps(output))
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    hook_input = get_hook_input()

    if mode == "reset":
        handle_reset()
    elif mode == "track-read":
        handle_track_read(hook_input)
    elif mode == "check-write":
        handle_check_write(hook_input)
    else:
        sys.exit(0)
