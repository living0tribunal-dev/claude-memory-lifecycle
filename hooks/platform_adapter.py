#!/usr/bin/env python3
"""Platform Adapter — Abstracts Claude Code hook interaction into 3 cables.

All hooks interact with Claude Code through exactly 3 patterns:
1. INPUT  — stdin JSON (event context from Claude Code)
2. OUTPUT — stdout JSON (additionalContext injection back to Claude)
3. BLOCK  — exit code 2 (prevent the action)

This module handles all platform-specific parsing so hooks focus on logic.
Switching platforms = changing this one file.

Usage:
    from platform_adapter import HookContext
    ctx = HookContext("PreToolUse")

    # INPUT: read data
    ctx.tool_name       # "Write", "Edit", "Bash", ...
    ctx.tool_input      # {"file_path": "...", "command": "..."}
    ctx.response        # last assistant message (Stop hooks)
    ctx.user_message    # user prompt (UserPromptSubmit hooks)
    ctx.session_id      # session identifier
    ctx.cwd             # working directory (from stdin or os.getcwd())
    ctx.agent_type      # subagent type (SubagentStart hooks)
    ctx.get("field")    # raw access to any field

    # OUTPUT: inject context
    ctx.inject("Warning text")

    # BLOCK: prevent action
    ctx.block("Reason shown on stderr")
"""
import json
import os
import sys


class HookContext:
    """Parses Claude Code hook input and provides output helpers."""

    def __init__(self, event_name=None):
        """Parse stdin JSON. Safe: returns empty dict on any error."""
        self._event_name = event_name
        try:
            raw = sys.stdin.read()
            self._data = json.loads(raw) if raw.strip() else {}
        except Exception:
            self._data = {}

    # ── INPUT cable ──

    @property
    def tool_name(self):
        """Tool name (handles both snake_case and camelCase variants)."""
        return self._data.get("tool_name") or self._data.get("toolName", "")

    @property
    def tool_input(self):
        """Tool input parameters (handles both snake_case and camelCase)."""
        return self._data.get("tool_input") or self._data.get("toolInput", {})

    @property
    def response(self):
        """Last assistant message (Stop hooks). Field: last_assistant_message."""
        return self._data.get("last_assistant_message", "")

    @property
    def user_message(self):
        return self._data.get("user_message", "")

    @property
    def session_id(self):
        return self._data.get("session_id", "")

    @property
    def cwd(self):
        return self._data.get("cwd", "") or os.getcwd()

    @property
    def agent_type(self):
        return self._data.get("agent_type", "")

    def get(self, key, default=None):
        """Raw access to any stdin field."""
        return self._data.get(key, default)

    @property
    def raw(self):
        """Full parsed dict for custom field access."""
        return self._data

    # ── OUTPUT cable ──

    def inject(self, text, event_name=None):
        """Output additionalContext to Claude."""
        name = event_name or self._event_name
        output = {"additionalContext": text}
        if name:
            output["hookEventName"] = name
        print(json.dumps({"hookSpecificOutput": output}))

    def empty_output(self, event_name=None):
        """Acknowledge event without injecting content."""
        name = event_name or self._event_name
        output = {}
        if name:
            output["hookEventName"] = name
        print(json.dumps({"hookSpecificOutput": output}))

    # ── BLOCK cable ──

    def block(self, reason=None):
        """Block the action (exit code 2). Reason goes to stderr."""
        if reason:
            print(reason, file=sys.stderr)
        sys.exit(2)
