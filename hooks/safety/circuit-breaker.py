#!/usr/bin/env python3
"""
Circuit-Breaker Hook - Objective-based (NOT just frequency)
PreToolUse Hook für intelligente Loop-Prevention

USAGE: Wird vor jedem Tool-Aufruf getriggert
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict, deque
from typing import Dict, List, Optional

# Pfade
HOME = Path.home()
CONFIG_FILE = HOME / ".claude" / "config" / "feature-toggles.json"
STATE_FILE = HOME / ".claude" / "state" / "circuit-breaker-state.json"
LOG_FILE = HOME / ".claude" / "hooks" / "hook-debug.log"

# State persistence (in-memory for MVP, could use sqlite for production)
OBJECTIVE_COUNTERS = defaultdict(lambda: deque(maxlen=100))
LAST_RESET = {}

def log(message: str):
    """Log to debug file"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] CircuitBreaker: {message}\n")

def load_config() -> Dict:
    """Load circuit breaker config from feature-toggles.json"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
            return config.get("circuit_breaker_config", {}).get("objectives", {})
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log(f"ERROR loading config: {e}")
        return {}

def load_state() -> Dict:
    """Load circuit breaker state"""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}
    except Exception as e:
        log(f"ERROR loading state: {e}")
        return {}

def save_state(state: Dict):
    """Save circuit breaker state"""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"ERROR saving state: {e}")

def classify_objective(tool_name: str, tool_args: Dict) -> str:
    """
    Classify tool call into objective category
    (This is what makes it "objective-based" not just frequency-based)
    """

    # Search objectives
    if tool_name in ["Grep", "Glob", "WebSearch", "WebFetch"]:
        return "search"

    # Edit objectives
    if tool_name in ["Edit", "Write", "NotebookEdit"]:
        return "edit"

    # File read objectives
    if tool_name == "Read":
        return "file_read"

    # API call objectives (MCP tools)
    if tool_name.startswith("mcp__"):
        return "api_call"

    # LLM call objectives (Task tool with agents)
    if tool_name == "Task":
        return "llm_call"

    # Default
    return "other"

def check_circuit_breaker(objective: str, config: Dict, state: Dict) -> Optional[str]:
    """
    Check if circuit breaker should trip

    Returns:
        None if OK, error message if circuit breaker trips
    """

    if objective not in config:
        return None

    obj_config = config[objective]
    now = datetime.now()

    # Initialize state for this objective
    if objective not in state:
        state[objective] = {
            "consecutive_count": 0,
            "last_call": None,
            "hourly_count": 0,
            "hourly_reset": now.isoformat()
        }

    obj_state = state[objective]

    # Parse last_call timestamp
    last_call = None
    if obj_state["last_call"]:
        try:
            last_call = datetime.fromisoformat(obj_state["last_call"])
        except ValueError:
            pass

    # Check hourly limit (if configured)
    if "max_per_hour" in obj_config:
        hourly_reset = datetime.fromisoformat(obj_state["hourly_reset"])

        # Reset hourly counter if needed
        if now - hourly_reset > timedelta(hours=1):
            obj_state["hourly_count"] = 0
            obj_state["hourly_reset"] = now.isoformat()

        # Check hourly limit
        if obj_state["hourly_count"] >= obj_config["max_per_hour"]:
            return f"❌ Circuit Breaker TRIPPED: {objective} - Exceeded {obj_config['max_per_hour']} calls/hour. Cooldown required."

        obj_state["hourly_count"] += 1

    # Check consecutive limit (if configured)
    if "max_consecutive" in obj_config:
        # Reset consecutive counter if cooldown expired
        cooldown = obj_config.get("cooldown_seconds", 60)

        if last_call and (now - last_call).total_seconds() > cooldown:
            obj_state["consecutive_count"] = 0

        # Increment consecutive counter
        obj_state["consecutive_count"] += 1

        # Check consecutive limit
        if obj_state["consecutive_count"] > obj_config["max_consecutive"]:
            return f"❌ Circuit Breaker TRIPPED: {objective} - Exceeded {obj_config['max_consecutive']} consecutive calls. Cooldown for {cooldown}s required."

    # Update last call timestamp
    obj_state["last_call"] = now.isoformat()

    return None

def main():
    """Main hook logic"""

    log("CircuitBreaker Hook started")

    # Read hook input from stdin (Claude Code hook protocol)
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        hook_input = {}

    tool_name = hook_input.get("tool_name", "unknown")
    tool_args = hook_input.get("tool_input", {})

    # Load config and state
    config = load_config()
    if not config:
        log("Circuit breaker config not found - allowing all calls")
        print(json.dumps({"approved": True}))
        return

    state = load_state()

    # Classify objective
    objective = classify_objective(tool_name, tool_args)
    log(f"Tool: {tool_name}, Objective: {objective}")

    # Check circuit breaker
    error_message = check_circuit_breaker(objective, config, state)

    if error_message:
        log(f"Circuit breaker TRIPPED: {error_message}")
        # Block the tool call
        output = {
            "approved": False,
            "reason": error_message,
            "hookSpecificOutput": {
                "error": error_message,
                "suggestion": "Wait for cooldown period or use different approach"
            }
        }
        print(json.dumps(output))
    else:
        log(f"Circuit breaker OK - allowing {objective} call")
        # Allow the tool call
        output = {"approved": True}
        print(json.dumps(output))

    # Save updated state
    save_state(state)
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
