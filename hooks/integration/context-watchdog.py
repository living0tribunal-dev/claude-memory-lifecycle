#!/usr/bin/env python3
"""
Context Watchdog - UserPromptSubmit Hook
Warns when session is getting long to trigger /session-save before auto-compact.

Tracks user message count per session. At threshold, injects a warning
as additionalContext so Claude can run /session-save before auto-compact fires.
"""

import sys
import json
import os
import time
from pathlib import Path

# Config
WARN_THRESHOLD = 25         # First warning after N user messages
WARN_INTERVAL = 10          # Re-warn every N messages after first warning
SESSION_GAP_MINUTES = 30    # Reset counter if no message for this long

STATE_DIR = Path(os.path.expanduser('~')) / '.claude' / 'state'
STATE_FILE = STATE_DIR / 'context-watchdog.json'


def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {}


def save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)


def main():
    # Try to read hook input (may contain session_id)
    input_data = {}
    try:
        raw = sys.stdin.read().strip()
        if raw:
            input_data = json.loads(raw)
    except:
        pass

    session_id = input_data.get('session_id', '')

    state = load_state()
    now = time.time()

    # Detect new session: by session_id or time gap
    is_new_session = False
    if session_id:
        if state.get('session_id') != session_id:
            is_new_session = True
    else:
        last_time = state.get('last_time', 0)
        if now - last_time > SESSION_GAP_MINUTES * 60:
            is_new_session = True

    if is_new_session:
        state = {
            'session_id': session_id,
            'message_count': 0,
            'warned_at': 0,
            'compact_count': 0,
        }

    state['message_count'] = state.get('message_count', 0) + 1
    state['last_time'] = now
    count = state['message_count']
    warned_at = state.get('warned_at', 0)

    # Check if warning needed
    should_warn = False
    if count >= WARN_THRESHOLD:
        if warned_at == 0:
            should_warn = True
        elif count >= warned_at + WARN_INTERVAL:
            should_warn = True

    if should_warn:
        state['warned_at'] = count
        save_state(state)

        compact_count = state.get('compact_count', 0)
        compact_info = f" (bereits {compact_count}x compacted)" if compact_count > 0 else ""

        output = {
            "hookSpecificOutput": {
                "additionalContext": f"CONTEXT WATCHDOG: {count} User-Messages in dieser Session{compact_info}. Bei 1M Context laeuft die Session lange ohne Compact. /session-save ausfuehren um semantischen Kontext zu sichern."
            }
        }
        print(json.dumps(output))
    else:
        save_state(state)
        print(json.dumps({}))


if __name__ == '__main__':
    main()
