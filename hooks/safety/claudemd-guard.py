#!/usr/bin/env python3
"""CLAUDE.md Guard — PreToolUse Hook fuer Edit/Write auf CLAUDE.md.

MECHANISMUS (Stufe 1): Verhindert unkontrollierte Edits an CLAUDE.md Dateien,
insbesondere durch Task-Agents mit bypassPermissions.

Hintergrund: claude-mem #3168/#3170 — Batch-Edits via Task-Agents + Regex
haben CLAUDE.md Dateien beschaedigt. Dieser Hook blockiert ALLE programmatischen
CLAUDE.md Edits. Manuelle Edits durch den User bleiben moeglich.

Feuert auf Edit und Write Tools.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

LOG_FILE = Path.home() / ".claude" / "hooks" / "hook-debug.log"


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] CLAUDEmdGuard: {msg}\n")
    except Exception:
        pass


def is_claudemd(file_path: str) -> bool:
    """Prueft ob die Datei eine CLAUDE.md ist."""
    normalized = file_path.replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized
    return basename == "CLAUDE.md"


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        hook_input = {}

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    # Nur auf Edit/Write reagieren
    if tool_name not in ("Edit", "Write"):
        print(json.dumps({"approved": True}))
        return

    file_path = tool_input.get("file_path", "")

    # Nur CLAUDE.md schuetzen
    if not is_claudemd(file_path):
        print(json.dumps({"approved": True}))
        return

    log(f"BLOCKED: {tool_name} on {file_path}")
    print(json.dumps({
        "approved": False,
        "reason": (
            "CLAUDEMD-GUARD: CLAUDE.md Edits sind BLOCKIERT.\n"
            "Hintergrund: Task-Agents haben CLAUDE.md Dateien beschaedigt (S42).\n"
            "CLAUDE.md nur manuell editieren (User oeffnet Datei selbst).\n"
            "Falls dieser Edit beabsichtigt ist: User muss manuell editieren."
        )
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        # Fail-open
        print(json.dumps({"approved": True}))
