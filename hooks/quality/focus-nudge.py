#!/usr/bin/env python3
"""Focus-Nudge Hook — Python Version
Fokus-Reminder alle FOCUS_INTERVAL Prompts.
Event: UserPromptSubmit
"""
import json
import sys
import os
from datetime import datetime
from pathlib import Path

# Config
FOCUS_INTERVAL = 5
STATE_DIR = Path.home() / ".claude" / "state"
LOG_DIR = Path.home() / ".claude" / "logs"
COUNTER_FILE = STATE_DIR / "focus_counter"

def log(message):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_DIR / "hook-execution.log", "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] focus-nudge: {message}\n")
    except Exception:
        pass

def atomic_increment():
    """Atomarer Zaehler via increment_counter.py"""
    script_dir = Path(__file__).parent
    counter_script = script_dir / "increment_counter.py"
    if counter_script.exists():
        try:
            # increment_counter.py direkt importieren
            sys.path.insert(0, str(script_dir))
            from increment_counter import atomic_increment as inc
            return inc(str(COUNTER_FILE))
        except Exception:
            pass
    # Fallback: einfacher Zaehler
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        count = int(COUNTER_FILE.read_text().strip())
    except Exception:
        count = 0
    count += 1
    COUNTER_FILE.write_text(str(count))
    return count

def main():
    # stdin konsumieren
    try:
        sys.stdin.read()
    except Exception:
        pass

    count = atomic_increment()
    log(f"Prompt #{count}")

    # Alle FOCUS_INTERVAL Prompts: Reminder
    if count % FOCUS_INTERVAL == 0:
        log(f"Triggering focus reminder at prompt #{count}")
        context = (
            f"FOKUS-CHECK (Prompt #{count}) - Pruefe: "
            "1) Urspruengliches Ziel? "
            "2) Neue Themen statt alte abschliessen? "
            "3) CLAUDES LAW: Muss automatisiert sein "
            "4) PLAN-GATE: Reihenfolge/Plan? UEBERSETZT? KONSISTENT? GEGENGEPRUEFT?"
        )
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}))

if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}))
