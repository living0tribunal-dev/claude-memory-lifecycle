#!/usr/bin/env python3
"""3-GATE PreToolUse Hook — Python Version
Injiziert 3-GATE Reminder nur bei Mutations-Tools (Write/Edit/Bash/NotebookEdit).
Read/Glob/Grep/Agent bleiben ohne Reminder — spart ~3-5KB Context/Session.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

LOG_FILE = Path.home() / ".claude" / "hooks" / "hook-debug.log"
MUTATION_TOOLS = {"Write", "Edit", "NotebookEdit"}

def main():
    # stdin parsen
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except Exception:
        hook_input = {}

    tool_name = hook_input.get("tool_name", "")

    # Log
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"PreToolUse Hook (py): {timestamp} tool={tool_name}\n")
    except Exception:
        pass

    # Nur bei Mutations-Tools den Reminder ausgeben
    if tool_name in MUTATION_TOOLS:
        reminder = "[3-GATE CHECK] Vor dieser Aktion: SCOPE? SIMPEL? VALIDIERT?"
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": reminder}}))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse"}}))

if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Fallback: Reminder ausgeben (sicherer als schweigen)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "[3-GATE CHECK] Vor dieser Aktion: SCOPE? SIMPEL? VALIDIERT?"}}))
