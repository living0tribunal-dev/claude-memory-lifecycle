#!/usr/bin/env python3
"""3-GATE PreToolUse Hook — Python Version
Injiziert 3-GATE Reminder bei jedem Tool-Aufruf.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

LOG_FILE = Path.home() / ".claude" / "hooks" / "hook-debug.log"

def main():
    # stdin konsumieren
    try:
        sys.stdin.read()
    except Exception:
        pass

    # Log
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"PreToolUse Hook (py): {timestamp}\n")
    except Exception:
        pass

    # 3-GATE Reminder ausgeben
    reminder = "[3-GATE CHECK] Vor dieser Aktion: SCOPE? SIMPEL? VALIDIERT?"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": reminder}}))

if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse"}}))
