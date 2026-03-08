#!/usr/bin/env python3
"""
Loop-Detector Hook - Pattern-based Loop Detection
PreToolUse Hook für stuck pattern detection

USAGE: Wird vor jedem Tool-Aufruf getriggert
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import deque
from dataclasses import dataclass, asdict

# Pfade
HOME = Path.home()
CONFIG_FILE = HOME / ".claude" / "config" / "feature-toggles.json"
STATE_FILE = HOME / ".claude" / "state" / "loop-detector-state.json"
LOG_FILE = HOME / ".claude" / "hooks" / "hook-debug.log"

# Action history (in-memory + persisted)
MAX_HISTORY = 100


@dataclass
class Action:
    """Represents a single tool action"""
    type: str  # "search", "edit", "api_call", "file_read", etc.
    target: str  # File path, query string, MCP tool name, etc.
    result: str  # "success", "error", etc.
    timestamp: str  # ISO format

    @classmethod
    def from_tool_call(cls, tool_name: str, tool_args: Dict, result: str = "pending") -> 'Action':
        """Create Action from tool call"""

        # Classify type
        if tool_name in ["Grep", "Glob", "WebSearch", "WebFetch"]:
            action_type = "search"
        elif tool_name in ["Edit", "Write", "NotebookEdit"]:
            action_type = "edit"
        elif tool_name == "Read":
            action_type = "file_read"
        elif tool_name.startswith("mcp__"):
            action_type = "api_call"
        elif tool_name == "Task":
            action_type = "agent_call"
        else:
            action_type = "other"

        # Extract target
        target = ""
        if action_type == "search":
            target = tool_args.get("query", tool_args.get("pattern", ""))
        elif action_type in ["edit", "file_read"]:
            target = tool_args.get("file_path", "")
        elif action_type == "api_call":
            target = tool_name  # MCP tool name
        elif action_type == "agent_call":
            target = tool_args.get("subagent_type", "")
        else:
            target = tool_name

        return cls(
            type=action_type,
            target=target,
            result=result,
            timestamp=datetime.now().isoformat()
        )


def log(message: str):
    """Log to debug file"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] LoopDetector: {message}\n")


def load_config() -> Dict:
    """Load loop detection config from feature-toggles.json"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
            return config.get("loop_detection_config", {}).get("patterns", {})
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log(f"ERROR loading config: {e}")
        return {}


def load_state() -> List[Action]:
    """Load action history from state"""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return [Action(**a) for a in data.get("history", [])]
        return []
    except Exception as e:
        log(f"ERROR loading state: {e}")
        return []


def save_state(history: List[Action]):
    """Save action history to state"""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "history": [asdict(a) for a in history],
                "last_updated": datetime.now().isoformat()
            }, f, indent=2)
    except Exception as e:
        log(f"ERROR saving state: {e}")


def detect_pattern_same_search_3x(action: Action, history: List[Action]) -> bool:
    """Detect if same search query repeated 3+ times"""
    if action.type != "search":
        return False

    # Count recent searches with same target
    recent_searches = [
        a for a in history
        if a.type == "search" and a.target == action.target
    ]

    return len(recent_searches) >= 3


def detect_pattern_same_file_edit_5x(action: Action, history: List[Action]) -> bool:
    """Detect if same file edited 5+ times"""
    if action.type != "edit":
        return False

    # Count recent edits to same file
    recent_edits = [
        a for a in history
        if a.type == "edit" and a.target == action.target
    ]

    return len(recent_edits) >= 5


def detect_pattern_no_progress_10_turns(action: Action, history: List[Action]) -> bool:
    """Detect if no progress in last 10 turns (all errors or all same action)"""
    if len(history) < 10:
        return False

    recent_10 = list(history)[-10:]

    # Check 1: All errors
    all_errors = all(a.result == "error" for a in recent_10)
    if all_errors:
        return True

    # Check 2: All same action (e.g., same search 10x)
    all_same_type = all(a.type == recent_10[0].type for a in recent_10)
    all_same_target = all(a.target == recent_10[0].target for a in recent_10)
    if all_same_type and all_same_target:
        return True

    return False


def detect_pattern_api_error_repeat_3x(action: Action, history: List[Action]) -> bool:
    """Detect if API errors repeated 3+ times"""
    if action.result != "error":
        return False

    if action.type != "api_call":
        return False

    # Count recent API errors
    recent_api_errors = [
        a for a in history
        if a.type == "api_call" and a.result == "error"
    ]

    return len(recent_api_errors) >= 3


def detect_patterns(action: Action, history: List[Action], config: Dict) -> List[str]:
    """
    Detect all matching patterns

    Returns:
        List of pattern names that matched
    """
    matched_patterns = []

    # Pattern 1: Same search 3x
    if "same_search_3x" in config and detect_pattern_same_search_3x(action, history):
        matched_patterns.append("same_search_3x")

    # Pattern 2: Same file edit 5x
    if "same_file_edit_5x" in config and detect_pattern_same_file_edit_5x(action, history):
        matched_patterns.append("same_file_edit_5x")

    # Pattern 3: No progress 10 turns
    if "no_progress_10_turns" in config and detect_pattern_no_progress_10_turns(action, history):
        matched_patterns.append("no_progress_10_turns")

    # Pattern 4: API error repeat 3x
    if "api_error_repeat_3x" in config and detect_pattern_api_error_repeat_3x(action, history):
        matched_patterns.append("api_error_repeat_3x")

    return matched_patterns


def handle_pattern(pattern_name: str, config: Dict, action: Action) -> Dict:
    """
    Handle detected pattern according to config

    Returns:
        Dict with "approved" (bool), "reason" (str), "hookSpecificOutput" (dict)
    """
    pattern_config = config.get(pattern_name, {})
    action_type = pattern_config.get("action", "WARN")
    message = pattern_config.get("message", f"Pattern '{pattern_name}' detected")

    log(f"Pattern detected: {pattern_name} - Action: {action_type}")

    if action_type == "WARN":
        # Allow but warn
        return {
            "approved": True,
            "hookSpecificOutput": {
                "warning": f"⚠️ WARNING: {message}",
                "pattern": pattern_name,
                "action_type": action.type,
                "action_target": action.target,
                "suggestion": "Consider changing approach to avoid loop"
            }
        }

    elif action_type == "STOP":
        # Block the call
        return {
            "approved": False,
            "reason": f"❌ STOPPED: {message}",
            "hookSpecificOutput": {
                "error": message,
                "pattern": pattern_name,
                "action_type": action.type,
                "action_target": action.target,
                "suggestion": "Break the loop by using different approach or tool"
            }
        }

    elif action_type == "ESCALATE":
        # Block and escalate to user
        return {
            "approved": False,
            "reason": f"🚨 ESCALATED: {message} - User intervention required",
            "hookSpecificOutput": {
                "error": message,
                "pattern": pattern_name,
                "action_type": action.type,
                "action_target": action.target,
                "escalation": True,
                "suggestion": "Manual review needed - loop detected over 10 turns"
            }
        }

    elif action_type == "PAUSE":
        # Block and request manual intervention
        return {
            "approved": False,
            "reason": f"⏸️ PAUSED: {message} - Manual intervention required",
            "hookSpecificOutput": {
                "error": message,
                "pattern": pattern_name,
                "action_type": action.type,
                "action_target": action.target,
                "pause": True,
                "suggestion": "Review recent actions and clear error before continuing"
            }
        }

    else:
        # Unknown action type - default to WARN
        return {
            "approved": True,
            "hookSpecificOutput": {
                "warning": f"⚠️ {message} (unknown action type: {action_type})"
            }
        }


def main():
    """Main hook logic"""

    log("LoopDetector Hook started")

    # Read hook input from stdin (Claude Code hook protocol)
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        hook_input = {}

    tool_name = hook_input.get("tool_name", "unknown")
    tool_args = hook_input.get("tool_input", {})
    tool_result = "pending"
    session_id = hook_input.get("session_id", "unknown")

    log(f"Tool: {tool_name}, Args: {json.dumps(tool_args)[:100]}...")

    # Load config
    config = load_config()
    if not config:
        log("Loop detection config not found - allowing all calls")
        print(json.dumps({"approved": True}))
        return

    # Load state (action history)
    history = load_state()

    # SESSION RESET: Clear stale history from different sessions
    state_data = {}
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state_data = json.load(f)
    except Exception:
        pass

    stored_session = state_data.get("session_id", "")
    if stored_session and stored_session != session_id:
        log(f"Session changed ({stored_session[:8]}... -> {session_id[:8]}...) - resetting history")
        history = []

    # Also check staleness: if last entry is >2h old, reset
    if history:
        try:
            last_ts = datetime.fromisoformat(history[-1].timestamp)
            if (datetime.now() - last_ts).total_seconds() > 7200:  # 2 hours
                log(f"History stale (last entry {history[-1].timestamp}) - resetting")
                history = []
        except (ValueError, AttributeError):
            pass

    # Create action from current tool call
    action = Action.from_tool_call(tool_name, tool_args, result=tool_result)

    # Detect patterns
    matched_patterns = detect_patterns(action, history, config)

    if matched_patterns:
        log(f"Patterns detected: {matched_patterns}")

        # Handle first (highest priority) pattern
        pattern_name = matched_patterns[0]
        output = handle_pattern(pattern_name, config, action)

        # If multiple patterns, add info
        if len(matched_patterns) > 1:
            if "hookSpecificOutput" not in output:
                output["hookSpecificOutput"] = {}
            output["hookSpecificOutput"]["additional_patterns"] = matched_patterns[1:]

        print(json.dumps(output))

    else:
        log(f"No patterns detected - allowing call")

        # Allow the call
        output = {"approved": True}
        print(json.dumps(output))

    # ALWAYS add action to history (fixes self-healing bug where
    # approved=False prevented state updates, causing permanent loops)
    history.append(action)
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]

    # Save state with session_id for cross-session detection
    try:
        save_data = {
            "history": [asdict(a) for a in history],
            "last_updated": datetime.now().isoformat(),
            "session_id": session_id
        }
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(save_data, f, indent=2)
    except Exception as e:
        log(f"ERROR saving state: {e}")

    log("Hook completed")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        import traceback
        log(traceback.format_exc())
        # Allow tool call on error (fail-open for safety)
        print(json.dumps({"approved": True}))
