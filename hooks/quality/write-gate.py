#!/usr/bin/env python3
"""Write-Gate: Verhindert Writes/Edits ohne vorheriges Lesen der Zieldatei.

Gate-3 Mechanismus (Stufe 1 — Hook, nicht umgehbar).
Trackt gelesene und geschriebene Dateien pro Prompt.
Blockt Mutations an Dateien die in diesem Prompt nicht gelesen/geschrieben wurden.

Modes:
  reset       → UserPromptSubmit: Tracking auf 0 setzen
  track-read  → PostToolUse:Read/Write: Datei-Pfad registrieren
  check-write → PreToolUse:Write/Edit: Mutation ohne Lesen blocken
"""

import sys
import json
import os
import time
import hashlib

# Schwellen (nur fuer neue Dateien via Write)
LARGE_WRITE_THRESHOLD = 5000   # Zeichen — darunter kein Check
MIN_READS_FOR_LARGE_WRITE = 3  # Unique Dateien

# State — CWD-basierte Isolation (verschiedene Projekte = verschiedene State-Files)
STATE_DIR = os.path.join(os.environ.get("TEMP", "/tmp"), "claude-write-gate")


def get_state_file():
    """Session-spezifisches State-File basierend auf CWD."""
    cwd = os.getcwd()
    cwd_hash = hashlib.md5(cwd.encode()).hexdigest()[:8]
    return os.path.join(STATE_DIR, f"state-{cwd_hash}.json")


def normalize_path(p):
    """Normalize path for consistent comparison across path formats."""
    if not p:
        return ""
    return os.path.normpath(p).lower()


def load_state():
    try:
        with open(get_state_file(), 'r') as f:
            return json.load(f)
    except Exception:
        return {"reads": [], "last_reset": ""}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(get_state_file(), 'w') as f:
        json.dump(state, f)


def get_hook_input():
    try:
        return json.loads(sys.stdin.read())
    except Exception:
        return {}


def handle_reset():
    """UserPromptSubmit: Reset tracking."""
    get_hook_input()  # consume stdin
    state = {"reads": [], "last_reset": time.strftime("%Y-%m-%dT%H:%M:%S")}
    save_state(state)
    sys.exit(0)


def handle_track_read(hook_input):
    """PostToolUse:Read/Write/Grep/Glob: Track file path or research marker."""
    tool_input = hook_input.get("tool_input", {})
    file_path = tool_input.get("file_path", "") or tool_input.get("path", "")

    if not file_path:
        # Grep/Glob ohne expliziten Pfad -> generischer Research-Marker
        file_path = "__tool_research__"

    norm = normalize_path(file_path)
    state = load_state()
    if norm not in state["reads"]:
        state["reads"].append(norm)
        save_state(state)

    sys.exit(0)


def handle_check_write(hook_input):
    """PreToolUse:Write/Edit: Block mutations without reading the target file."""
    tool_input = hook_input.get("tool_input", {})
    tool_name = hook_input.get("tool_name", "Write")
    file_path = tool_input.get("file_path", "")

    if not file_path:
        sys.exit(0)

    norm = normalize_path(file_path)
    state = load_state()
    tracked = [normalize_path(p) for p in state.get("reads", [])]

    # Identity check: is the target file in tracked files?
    if norm in tracked:
        sys.exit(0)

    # File not tracked — distinguish new file creation from existing file mutation
    is_new_file = (tool_name == "Write" and not os.path.exists(file_path))

    if is_new_file:
        # New file: apply quantity check (need enough research before large writes)
        content = tool_input.get("content", "")
        if len(content) < LARGE_WRITE_THRESHOLD:
            sys.exit(0)  # Small new files pass
        if len(tracked) >= MIN_READS_FOR_LARGE_WRITE:
            sys.exit(0)  # Enough research done
        output = {
            "hookSpecificOutput": {
                "message": (
                    f"GATE-3: Neue Datei mit {len(content)} Zeichen, "
                    f"aber nur {len(tracked)} Datei(en) gelesen. "
                    f"Minimum: {MIN_READS_FOR_LARGE_WRITE} Reads vor einem grossen Write. "
                    f"Lies zuerst die relevanten Quellen."
                )
            }
        }
        print(json.dumps(output))
        sys.exit(2)
    else:
        # Existing file: MUST have been read/written this prompt
        output = {
            "hookSpecificOutput": {
                "message": (
                    f"GATE-3: Du editierst/ueberschreibst '{os.path.basename(file_path)}' "
                    f"aber hast diese Datei in diesem Prompt nicht gelesen. "
                    f"Erst Read('{file_path}'), dann {tool_name}."
                )
            }
        }
        print(json.dumps(output))
        sys.exit(2)


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
