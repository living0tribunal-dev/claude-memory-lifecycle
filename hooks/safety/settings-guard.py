#!/usr/bin/env python3
"""Settings-Guard — PreToolUse Hook fuer Edit/Write auf settings.json.

MECHANISMUS (Stufe 1): Verhindert permanentes Eintragen von MCP-Servern
in settings.json. On-demand Server gehoeren in mcp-proxy-config.json
und werden via /mcp-loader geladen/entladen.

Feuert auf Edit und Write Tools.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

LOG_FILE = Path.home() / ".claude" / "hooks" / "hook-debug.log"
SETTINGS_PATH = str(Path.home() / ".claude" / "settings.json").replace("\\", "/").lower()

# Patterns die auf MCP-Server-Aenderungen hindeuten
MCP_PATTERNS = [
    "mcpservers",
    "mcp-server",
    "mcp_server",
    '"command"',
    "lazy_load",
    "npx",
    "uvx",
]


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] SettingsGuard: {msg}\n")
    except Exception:
        pass


def is_settings_file(file_path: str) -> bool:
    """Prueft ob die Datei settings.json ist."""
    normalized = file_path.replace("\\", "/").lower()
    return normalized == SETTINGS_PATH or normalized.endswith(".claude/settings.json")


def has_mcp_changes(text: str) -> bool:
    """Prueft ob der Text MCP-Server-Aenderungen enthaelt."""
    text_lower = text.lower().replace(" ", "")
    return any(p.replace(" ", "") in text_lower for p in MCP_PATTERNS)


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

    # Nur settings.json schuetzen
    if not is_settings_file(file_path):
        print(json.dumps({"approved": True}))
        return

    # Pruefen ob MCP-Server geaendert werden
    new_string = tool_input.get("new_string", "")
    content = tool_input.get("content", "")
    change_text = new_string or content

    if has_mcp_changes(change_text):
        log(f"BLOCKED: MCP server change in settings.json detected")
        print(json.dumps({
            "approved": False,
            "reason": (
                "SETTINGS-GUARD: MCP-Server NICHT in settings.json eintragen!\n"
                "On-demand Server gehoeren in mcp-proxy-config.json und werden "
                "via /mcp-loader geladen/entladen.\n"
                "settings.json enthaelt nur PERMANENTE Core-Server "
                "(claude-mem, dynamic-proxy, claudeus-wp-mcp).\n"
                "Aktion: Nutze /mcp-loader zum Laden des gewuenschten Servers."
            )
        }))
        return

    # Nicht-MCP Aenderungen an settings.json: warnen aber durchlassen
    log(f"WARN: settings.json edit (non-MCP)")
    print(json.dumps({
        "approved": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": (
                "SETTINGS-GUARD: Du aenderst settings.json. "
                "Stelle sicher dass keine MCP-Server permanent eingetragen werden. "
                "On-demand Server via /mcp-loader laden."
            )
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        # Fail-open
        print(json.dumps({"approved": True}))
