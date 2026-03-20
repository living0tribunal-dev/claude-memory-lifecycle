#!/usr/bin/env python3
"""3-GATE PreToolUse Hook — Python Version
Injiziert 3-GATE Reminder nur bei Mutations-Tools (Write/Edit/NotebookEdit).
Read/Glob/Grep/Agent bleiben ohne Reminder — spart ~3-5KB Context/Session.
"""
import json
from datetime import datetime
from pathlib import Path

from platform_adapter import HookContext

LOG_FILE = Path.home() / ".claude" / "hooks" / "hook-debug.log"
MUTATION_TOOLS = {"Write", "Edit", "NotebookEdit"}
REMINDER = "[3-GATE CHECK] Vor dieser Aktion: SCOPE? SIMPEL? VALIDIERT?"

def main():
    ctx = HookContext("PreToolUse")

    # Log
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"PreToolUse Hook (py): {datetime.now():%Y-%m-%d %H:%M:%S} tool={ctx.tool_name}\n")
    except Exception:
        pass

    if ctx.tool_name in MUTATION_TOOLS:
        ctx.inject(REMINDER)
    else:
        ctx.empty_output()

if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Fallback: Reminder ausgeben (sicherer als schweigen)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": REMINDER}}))
