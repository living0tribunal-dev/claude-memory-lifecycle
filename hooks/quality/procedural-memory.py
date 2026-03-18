#!/usr/bin/env python3
"""Procedural Memory Executor — Generischer Hook fuer task-spezifische Prozeduren.

Event: UserPromptSubmit (sync)
Laedt Prozedur-Definitionen aus ~/.claude/procedures/*.md,
matcht Trigger-Keywords gegen User-Prompt, fuehrt Checks aus,
injiziert Ergebnisse als Fakten via additionalContext.

Immunsystem-Analogie:
- Executor (dieser Code) = T-Zell-Rezeptor-System (fix)
- Prozedur-Bibliothek (~/.claude/procedures/) = Antikoerper-Repertoire (waechst)
- Trigger-Match = Antigen-Erkennung
- Check-Execution = Immunantwort
- Injection = Zytokin-Signal
"""
import json
import sys
import os
import re
import glob as glob_module
from datetime import datetime
from pathlib import Path

# Config
PROCEDURES_DIR = Path.home() / ".claude" / "procedures"
STATE_DIR = Path.home() / ".claude" / "state"
LOG_DIR = Path.home() / ".claude" / "logs"
CACHE_FILE = STATE_DIR / "procedural-memory-cache.json"
MAX_INJECTIONS = 2  # Prevent dilution (S40 finding)
MIN_KEYWORD_MATCHES = 1  # At least N keywords must match

# Cache
_procedures_cache = None
_procedures_mtime = 0


def log(message):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_DIR / "procedural-memory.log", "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def parse_procedure(filepath):
    """Parse a procedure markdown file with YAML frontmatter."""
    try:
        text = Path(filepath).read_text(encoding="utf-8")
    except Exception as e:
        log(f"Cannot read {filepath}: {e}")
        return None

    # Split frontmatter from body
    if not text.startswith("---"):
        log(f"No frontmatter in {filepath}")
        return None

    parts = text.split("---", 2)
    if len(parts) < 3:
        log(f"Invalid frontmatter in {filepath}")
        return None

    frontmatter_text = parts[1].strip()
    body = parts[2].strip()

    # Simple YAML parser (avoid external dependency)
    procedure = {"body": body, "file": str(filepath)}
    procedure.update(parse_simple_yaml(frontmatter_text))

    # Validate required fields
    trigger = procedure.get("trigger", {})
    if not trigger.get("keywords"):
        log(f"No trigger keywords in {filepath}")
        return None
    if not procedure.get("check"):
        log(f"No check definition in {filepath}")
        return None

    return procedure


def parse_simple_yaml(text):
    """Minimal YAML parser for procedure frontmatter.
    Handles: scalars, lists (inline [...] and indented -), nested objects (2 levels)."""
    result = {}
    current_key = None
    current_obj = None

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())

        if indent == 0 and ":" in stripped:
            # Top-level key
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()

            if value:
                # Inline value
                result[key] = parse_yaml_value(value)
                current_key = None
                current_obj = None
            else:
                # Nested object starts
                current_key = key
                current_obj = {}
                result[key] = current_obj

        elif indent > 0 and current_key and current_obj is not None:
            if stripped.startswith("- "):
                # List item under current nested key — find which sub-key
                # This handles indented list items
                item_value = stripped[2:].strip()
                # Find the last sub-key that was set
                if "_last_list_key" in current_obj:
                    list_key = current_obj["_last_list_key"]
                    if isinstance(current_obj.get(list_key), list):
                        current_obj[list_key].append(parse_yaml_value(item_value))
            elif ":" in stripped:
                sub_key, _, sub_value = stripped.partition(":")
                sub_key = sub_key.strip()
                sub_value = sub_value.strip()

                if sub_value:
                    parsed = parse_yaml_value(sub_value)
                    current_obj[sub_key] = parsed
                    if isinstance(parsed, list):
                        current_obj["_last_list_key"] = sub_key
                else:
                    # Sub-key with no value — start a list
                    current_obj[sub_key] = []
                    current_obj["_last_list_key"] = sub_key

    # Clean up internal markers
    for key in result:
        if isinstance(result[key], dict):
            result[key].pop("_last_list_key", None)

    return result


def parse_yaml_value(value):
    """Parse a YAML value: string, number, bool, inline list."""
    if not value:
        return ""

    # Remove quotes
    if (value.startswith('"') and value.endswith('"')) or \
       (value.startswith("'") and value.endswith("'")):
        return value[1:-1]

    # Inline list: ["a", "b", "c"]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1]
        items = []
        for item in inner.split(","):
            item = item.strip().strip('"').strip("'")
            if item:
                items.append(item)
        return items

    # Boolean
    if value.lower() in ("true", "yes"):
        return True
    if value.lower() in ("false", "no"):
        return False

    # Number
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass

    return value


def load_procedures():
    """Load all procedures from PROCEDURES_DIR with caching."""
    global _procedures_cache, _procedures_mtime

    if not PROCEDURES_DIR.exists():
        return []

    # Check directory mtime for cache invalidation
    try:
        current_mtime = PROCEDURES_DIR.stat().st_mtime
        # Also check individual file mtimes
        for f in PROCEDURES_DIR.glob("*.md"):
            fmt = f.stat().st_mtime
            if fmt > current_mtime:
                current_mtime = fmt
    except Exception:
        current_mtime = 0

    if _procedures_cache is not None and current_mtime <= _procedures_mtime:
        return _procedures_cache

    procedures = []
    for filepath in sorted(PROCEDURES_DIR.glob("*.md")):
        proc = parse_procedure(filepath)
        if proc:
            procedures.append(proc)

    _procedures_cache = procedures
    _procedures_mtime = current_mtime
    log(f"Loaded {len(procedures)} procedures from {PROCEDURES_DIR}")
    return procedures


def match_keywords(procedure, user_prompt):
    """Check if procedure keywords match the user prompt."""
    trigger = procedure.get("trigger", {})
    keywords = trigger.get("keywords", [])
    if not keywords:
        return 0

    prompt_lower = user_prompt.lower()
    matches = sum(1 for kw in keywords if kw.lower() in prompt_lower)
    return matches


def check_project_scope(procedure, cwd):
    """Check if procedure's project scope matches current context."""
    trigger = procedure.get("trigger", {})
    project = trigger.get("project")
    if not project:
        return True  # No project scope = matches everywhere

    # Check if project name is in CWD path
    return project.lower() in cwd.lower()


def expand_path(path_pattern, cwd):
    """Expand a path pattern to actual file paths."""
    path_pattern = str(path_pattern)

    # Home directory expansion
    if path_pattern.startswith("~"):
        path_pattern = str(Path.home()) + path_pattern[1:]

    # If absolute, use as-is
    if os.path.isabs(path_pattern):
        if "*" in path_pattern:
            return glob_module.glob(path_pattern, recursive=True)
        return [path_pattern] if os.path.exists(path_pattern) else []

    # Relative = relative to CWD
    full_pattern = os.path.join(cwd, path_pattern)
    if "*" in full_pattern:
        return glob_module.glob(full_pattern, recursive=True)
    return [full_pattern] if os.path.exists(full_pattern) else []


def execute_check(procedure, cwd):
    """Execute a procedure's check. Returns (passed, detail_message)."""
    check = procedure.get("check", {})
    check_type = check.get("type", "")
    path_pattern = check.get("path", "")
    expect = check.get("expect", "present")

    if not path_pattern:
        return True, ""

    files = expand_path(path_pattern, cwd)

    if check_type == "file_exists":
        exists = len(files) > 0
        if expect == "present" and not exists:
            return False, f"Expected file matching '{path_pattern}' not found"
        if expect == "absent" and exists:
            return False, f"File matching '{path_pattern}' exists but should not"
        return True, ""

    elif check_type == "grep":
        pattern = check.get("pattern", "")
        if not pattern:
            return True, ""

        found_in = []
        for filepath in files:
            try:
                content = Path(filepath).read_text(encoding="utf-8", errors="ignore")
                if re.search(pattern, content):
                    found_in.append(filepath)
            except Exception:
                continue

        if expect == "present" and not found_in:
            return False, f"Pattern '{pattern}' not found in any file matching '{path_pattern}'"
        if expect == "absent" and found_in:
            short_paths = [os.path.basename(f) for f in found_in[:3]]
            return False, f"Pattern '{pattern}' found in: {', '.join(short_paths)}"
        return True, ""

    else:
        log(f"Unknown check type: {check_type}")
        return True, ""


def main():
    # Read stdin
    try:
        input_data = json.loads(sys.stdin.read())
    except Exception:
        input_data = {}

    user_message = input_data.get("user_message", input_data.get("message", ""))
    cwd = input_data.get("cwd", os.getcwd())

    if not user_message:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}))
        return

    # Load procedures
    procedures = load_procedures()
    if not procedures:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}))
        return

    # Match and execute
    injections = []

    for proc in procedures:
        # Project scope check
        if not check_project_scope(proc, cwd):
            continue

        # Keyword matching
        keyword_hits = match_keywords(proc, user_message)
        if keyword_hits < MIN_KEYWORD_MATCHES:
            continue

        # Execute check
        passed, detail = execute_check(proc, cwd)

        if not passed:
            name = proc.get("name", "Unknown")
            body = proc.get("body", "")
            injection = f"PROCEDURAL MEMORY [{name}]: {body}"
            if detail:
                injection += f"\n  Check-Detail: {detail}"
            injections.append((keyword_hits, injection))
            log(f"TRIGGERED: {name} ({keyword_hits} keywords, check failed: {detail})")
        else:
            log(f"PASSED: {proc.get('name', '?')} ({keyword_hits} keywords, check OK)")

    # Sort by keyword match count (most relevant first), limit
    injections.sort(key=lambda x: -x[0])
    injection_texts = [text for _, text in injections[:MAX_INJECTIONS]]

    if injection_texts:
        context = "\n".join(injection_texts)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}))
