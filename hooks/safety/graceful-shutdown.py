#!/usr/bin/env python3
"""
Graceful-Shutdown Hook - STATE Backup bei Timeout/Error/Token-Limit
Continuous Monitoring Hook (kann via Stop Hook oder long-running agents getriggert werden)

USAGE: Check shutdown triggers und perform backup actions
"""

import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional

# Pfade
HOME = Path.home()
CONFIG_FILE = HOME / ".claude" / "config" / "feature-toggles.json"
STATE_FILE = HOME / ".claude" / "state" / "graceful-shutdown-state.json"
LOG_FILE = HOME / ".claude" / "hooks" / "hook-debug.log"

def log(message: str):
    """Log to debug file"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] GracefulShutdown: {message}\n")


def load_config() -> Dict:
    """Load graceful shutdown config from feature-toggles.json"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
            return config.get("graceful_shutdown_config", {})
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log(f"ERROR loading config: {e}")
        return {}


def load_state() -> Dict:
    """Load graceful shutdown state (runtime tracking)"""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "session_start": datetime.now().isoformat(),
            "consecutive_errors": 0,
            "last_error": None,
            "token_count_estimate": 0
        }
    except Exception as e:
        log(f"ERROR loading state: {e}")
        return {
            "session_start": datetime.now().isoformat(),
            "consecutive_errors": 0,
            "last_error": None,
            "token_count_estimate": 0
        }


def save_state(state: Dict):
    """Save graceful shutdown state"""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state["last_updated"] = datetime.now().isoformat()
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"ERROR saving state: {e}")


def calculate_runtime_minutes(state: Dict) -> int:
    """Calculate session runtime in minutes"""
    try:
        session_start = datetime.fromisoformat(state["session_start"])
        now = datetime.now()
        return int((now - session_start).total_seconds() / 60)
    except (KeyError, ValueError):
        return 0


def estimate_token_count() -> int:
    """
    Estimate current token count using multiple methods

    Strategy:
    1. Try to read from transcript file (most accurate)
    2. Fall back to heuristic based on session duration

    Heuristic:
    - Average session: ~500 tokens/message
    - Heavy research: ~2000 tokens/message
    - Average: ~20-30 messages/hour
    - Conservative estimate: 30,000 tokens/hour
    """
    try:
        # Method 1: Try to read latest transcript and estimate
        transcripts_dir = HOME / ".claude" / "projects" / "C--Users-livin"

        if transcripts_dir.exists():
            jsonl_files = list(transcripts_dir.glob('*.jsonl'))
            if jsonl_files:
                latest_transcript = max(jsonl_files, key=lambda p: p.stat().st_mtime)

                # Count lines (each line is roughly one turn)
                with open(latest_transcript, 'r', encoding='utf-8') as f:
                    line_count = sum(1 for _ in f)

                # Estimate: 1500 tokens per turn (input + output)
                estimated_tokens = line_count * 1500

                log(f"Token estimate from transcript: {estimated_tokens} ({line_count} turns)")
                return estimated_tokens
    except Exception as e:
        log(f"Transcript-based token counting failed: {e}")

    # Method 2: Heuristic fallback based on session duration
    try:
        state_file = HOME / ".claude" / "state" / "graceful-shutdown-state.json"

        if state_file.exists():
            with open(state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
                session_start = datetime.fromisoformat(state["session_start"])
                runtime_hours = (datetime.now() - session_start).total_seconds() / 3600

                # Conservative estimate: 30,000 tokens per hour
                estimated_tokens = int(runtime_hours * 30000)

                log(f"Token estimate from runtime: {estimated_tokens} ({runtime_hours:.2f} hours)")
                return estimated_tokens
    except Exception as e:
        log(f"Heuristic token counting failed: {e}")

    # Fallback: Return 0 if all methods fail
    return 0


def check_shutdown_triggers(runtime_minutes: int, token_count: int, consecutive_errors: int, config: Dict) -> Optional[str]:
    """
    Check if any shutdown trigger is met

    Returns:
        Trigger name if shutdown should occur, None otherwise
    """
    triggers = config.get("triggers", {})

    # Trigger 1: Timeout
    if triggers.get("timeout", {}).get("enabled", False):
        max_runtime = triggers["timeout"].get("max_runtime_minutes", 120)
        if runtime_minutes >= max_runtime:
            return f"timeout ({runtime_minutes}/{max_runtime} minutes)"

    # Trigger 2: Token limit
    if triggers.get("token_limit", {}).get("enabled", False):
        max_tokens = triggers["token_limit"].get("max_tokens", 180000)
        warning_threshold = triggers["token_limit"].get("warning_threshold", 150000)

        if token_count >= max_tokens:
            return f"token_limit ({token_count}/{max_tokens} tokens)"
        elif token_count >= warning_threshold:
            # WARNING: Auto-Compact imminent - trigger session-save NOW
            log(f"WARNING: Approaching token limit ({token_count}/{max_tokens}) - triggering auto-session-save")
            try:
                auto_session_save_script = HOME / ".claude" / "hooks" / "auto-session-save.py"
                if auto_session_save_script.exists():
                    result = subprocess.run(
                        ["python", str(auto_session_save_script)],
                        capture_output=True,
                        text=True,
                        timeout=30
                    )
                    if result.returncode == 0:
                        log("auto-session-save completed successfully")
                    else:
                        log(f"ERROR: auto-session-save failed: {result.stderr}")
                else:
                    log(f"ERROR: auto-session-save.py not found at {auto_session_save_script}")
            except Exception as e:
                log(f"ERROR during auto-session-save trigger: {e}")

    # Trigger 3: Error threshold
    if triggers.get("error_threshold", {}).get("enabled", False):
        max_errors = triggers["error_threshold"].get("max_consecutive_errors", 5)
        if consecutive_errors >= max_errors:
            return f"error_threshold ({consecutive_errors}/{max_errors} consecutive errors)"

    return None


def perform_backup_claude_mem(trigger: str, config: Dict) -> bool:
    """Backup critical state to claude-mem"""
    if not config.get("backup_actions", {}).get("claude_mem_backup", False):
        return True  # Skip if disabled

    try:
        backup_text = f"RESEARCH-BACKUP: Graceful shutdown at {datetime.now().isoformat()} - Trigger: {trigger}"

        claude_mem_script = HOME / ".claude" / "scripts" / "claude-mem.py"
        if not claude_mem_script.exists():
            log(f"WARN: claude-mem.py not found at {claude_mem_script}")
            return False

        result = subprocess.run(
            ["python", str(claude_mem_script), "add", backup_text],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            log(f"claude-mem backup completed: {backup_text}")
            return True
        else:
            log(f"ERROR: claude-mem backup failed: {result.stderr}")
            return False

    except Exception as e:
        log(f"ERROR during claude-mem backup: {e}")
        return False


def perform_backup_git_commit(trigger: str, config: Dict) -> bool:
    """Git auto-commit for _RESEARCH/ changes"""
    if not config.get("backup_actions", {}).get("git_auto_commit", False):
        return True  # Skip if disabled

    try:
        research_dir = Path.cwd() / "_RESEARCH"
        if not research_dir.exists():
            log("No _RESEARCH/ directory found - skipping git backup")
            return True

        # Check if git repo
        if not (research_dir / ".git").exists():
            log("_RESEARCH/ is not a git repo - skipping git backup")
            return True

        # Git add
        subprocess.run(
            ["git", "add", "."],
            cwd=research_dir,
            capture_output=True,
            timeout=10
        )

        # Git commit
        commit_message = f"Auto-backup before graceful shutdown ({trigger})"
        result = subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=research_dir,
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            log(f"Git auto-commit completed: {commit_message}")
            return True
        elif "nothing to commit" in result.stdout:
            log("Git: nothing to commit (working tree clean)")
            return True
        else:
            log(f"Git commit failed: {result.stderr}")
            return False

    except Exception as e:
        log(f"ERROR during git backup: {e}")
        return False


def perform_backup_state_snapshot(trigger: str, config: Dict) -> bool:
    """Create STATE.md snapshots for all research projects"""
    if not config.get("backup_actions", {}).get("state_snapshot", False):
        return True  # Skip if disabled

    try:
        research_dir = Path.cwd() / "_RESEARCH"
        if not research_dir.exists():
            log("No _RESEARCH/ directory found - skipping STATE snapshots")
            return True

        # Find all STATE.md files
        state_files = list(research_dir.glob("*/STATE.md"))

        if not state_files:
            log("No STATE.md files found")
            return True

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        for state_file in state_files:
            backup_file = state_file.parent / f"STATE_backup_{timestamp}.md"

            # Copy file
            import shutil
            shutil.copy2(state_file, backup_file)

            log(f"STATE snapshot created: {backup_file}")

        log(f"Created {len(state_files)} STATE snapshots")
        return True

    except Exception as e:
        log(f"ERROR during STATE snapshot: {e}")
        return False


def perform_graceful_shutdown(trigger: str, config: Dict) -> Dict:
    """
    Perform all backup actions and prepare shutdown

    Returns:
        Dict with shutdown info and notification
    """
    log(f"!!! GRACEFUL SHUTDOWN TRIGGERED: {trigger} !!!")

    backup_results = {
        "claude_mem": False,
        "git_commit": False,
        "state_snapshot": False
    }

    # 1. claude-mem backup
    backup_results["claude_mem"] = perform_backup_claude_mem(trigger, config)

    # 2. Git auto-commit
    backup_results["git_commit"] = perform_backup_git_commit(trigger, config)

    # 3. STATE snapshot
    backup_results["state_snapshot"] = perform_backup_state_snapshot(trigger, config)

    # 4. Notification
    notification = config.get("backup_actions", {}).get(
        "notification",
        "Research paused - state saved. Resume with /project-load [PROJECT_ID]"
    )

    success_count = sum(1 for v in backup_results.values() if v)
    total_count = len(backup_results)

    log(f"Backup completed: {success_count}/{total_count} successful")
    log(f"Notification: {notification}")

    return {
        "shutdown": True,
        "trigger": trigger,
        "backup_results": backup_results,
        "notification": notification,
        "timestamp": datetime.now().isoformat()
    }


def main():
    """Main hook logic"""

    log("GracefulShutdown Hook started")

    # Load config
    config = load_config()
    if not config:
        log("Graceful shutdown config not found - skipping")
        print(json.dumps({"shutdown": False}))
        return

    # Load state
    state = load_state()

    # FIX: Reset session_start if stale (>4 hours old = definitely a new session)
    try:
        session_start = datetime.fromisoformat(state["session_start"])
        hours_old = (datetime.now() - session_start).total_seconds() / 3600
        if hours_old > 4:
            log(f"Session start stale ({hours_old:.0f}h old) - resetting to now")
            state["session_start"] = datetime.now().isoformat()
            state["consecutive_errors"] = 0
            save_state(state)
    except (KeyError, ValueError):
        state["session_start"] = datetime.now().isoformat()
        save_state(state)

    # Calculate metrics
    runtime_minutes = calculate_runtime_minutes(state)
    token_count = estimate_token_count()  # Placeholder for MVP
    consecutive_errors = state.get("consecutive_errors", 0)

    log(f"Metrics - Runtime: {runtime_minutes}min, Tokens: {token_count}, Errors: {consecutive_errors}")

    # Check shutdown triggers
    trigger = check_shutdown_triggers(runtime_minutes, token_count, consecutive_errors, config)

    if trigger:
        # Perform graceful shutdown
        shutdown_info = perform_graceful_shutdown(trigger, config)

        output = {
            "shutdown": True,
            "trigger": trigger,
            "backup_results": shutdown_info["backup_results"],
            "notification": shutdown_info["notification"],
            "hookSpecificOutput": {
                "message": f"🛑 Graceful Shutdown: {trigger}",
                "details": f"Backups completed: {shutdown_info['backup_results']}",
                "instruction": shutdown_info["notification"]
            }
        }
        print(json.dumps(output))

        log("Graceful shutdown completed")

    else:
        log("No shutdown triggers met - continuing")

        # Save updated state
        save_state(state)

        output = {"shutdown": False}
        print(json.dumps(output))

    log("Hook completed")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        import traceback
        log(traceback.format_exc())
        # Don't shutdown on error (fail-safe)
        print(json.dumps({"shutdown": False}))
