#!/usr/bin/env python3
"""Gemini Bottom-Up Checker — Two-Mode Hook
Mode 'check': Stop-Hook (async) — Claudes Antwort via Gemini pruefen, Ergebnis speichern.
Mode 'inject': UserPromptSubmit-Hook — Gespeichertes Ergebnis als additionalContext injizieren.
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Config
LENGTH_THRESHOLD = 500
STATE_DIR = Path.home() / ".claude" / "state"
LOG_DIR = Path.home() / ".claude" / "logs"
RESULT_FILE = STATE_DIR / "gemini-checker-result.json"
COUNTER_FILE = STATE_DIR / "gemini-checker-daily.json"
API_KEYS = ["GEMINI_API_KEY", "GEMINI_API_KEY_ROUTING"]

CHECKER_PROMPT = """Du bist ein Quality-Checker. Pruefe ob die folgende Claude-Antwort Top-Down-Violations enthaelt.

REGELN die geprueft werden muessen:
1. VALIDIERT: Hat Claude Code/Architektur vorgeschlagen OHNE vorher die relevanten Files gelesen zu haben? (Gate 3)
2. SCOPE: Hat Claude mehr als 5 Files auf einmal aendern wollen ohne nachzufragen? (Gate 1)
3. PLAN-GATE: Hat Claude eine Reihenfolge/Plan vorgeschlagen OHNE zu begruenden welches Prinzip die Reihenfolge bestimmt? (Regel 17)
4. EXECUTION-LOCK: Hat Claude Files geschrieben/geaendert OHNE auf explizites User-OK zu warten? (Regel)
5. PLAN-CHAIN: Hat Claude eine Reihenfolge begruendet, aber die Kausalkette Prinzip→Konsequenz→Ordering ist logisch UNGUELTIG? (Form erfuellt, Substanz falsch — z.B. "A vor B WEIL Mechanism-First" aber Mechanism-First verursacht diese Ordering gar nicht)

Antworte NUR als JSON (kein Markdown, keine Codeblocks):
{"violation": true/false, "rules_violated": ["REGEL_NAME"], "reason": "kurze Begruendung"}

Wenn KEINE Violation: {"violation": false, "rules_violated": [], "reason": "OK"}

CLAUDE-ANTWORT:
"""


def log(message):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_DIR / "hook-execution.log", "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] gemini-checker: {message}\n")
    except Exception:
        pass


def save_result(violation, rules_violated, reason):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    result = {
        "violation": violation,
        "rules_violated": rules_violated,
        "reason": reason,
        "timestamp": time.time(),
        "checked_at": datetime.now().isoformat()
    }
    RESULT_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    log(f"Result saved: violation={violation}, rules={rules_violated}")


def get_daily_counter():
    """Read and increment daily API call counter. Returns (count, key_index)."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        data = json.loads(COUNTER_FILE.read_text(encoding="utf-8"))
        if data.get("date") != today:
            data = {"date": today, "count": 0}
    except Exception:
        data = {"date": today, "count": 0}
    data["count"] += 1
    COUNTER_FILE.write_text(json.dumps(data), encoding="utf-8")
    # Round-robin: alternate between keys
    key_index = data["count"] % len(API_KEYS)
    return data["count"], key_index


def get_api_key():
    """Get API key via round-robin. Returns (key, key_name) or (None, None)."""
    count, key_index = get_daily_counter()
    key_name = API_KEYS[key_index]
    key = os.environ.get(key_name)
    if key:
        log(f"Call #{count}, using {key_name}")
        return key, key_name
    # Fallback to other key
    other = API_KEYS[1 - key_index]
    key = os.environ.get(other)
    if key:
        log(f"Call #{count}, fallback to {other}")
        return key, other
    return None, None


def mode_check():
    """Stop-Hook: Claudes Antwort pruefen via Gemini."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        data = {}

    # Loop-Schutz
    if data.get("stop_hook_active", False):
        log("stop_hook_active=true -> skip")
        sys.exit(0)

    response = data.get("last_assistant_message", "")

    # Pre-Filter: nur lange Antworten pruefen
    if len(response) < LENGTH_THRESHOLD:
        log(f"Pre-filter: {len(response)} < {LENGTH_THRESHOLD} -> skip")
        save_result(False, [], "Pre-filter: zu kurz")
        sys.exit(0)

    # API Key (round-robin)
    api_key, key_name = get_api_key()
    if not api_key:
        log("Kein API Key verfuegbar -> skip")
        sys.exit(0)

    # Gemini Call
    try:
        import google.genai as genai
        client = genai.Client(api_key=api_key)
        gemini_response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=CHECKER_PROMPT + response[:3000]  # Max 3000 Zeichen senden
        )
        answer = gemini_response.text.strip()
        log(f"Gemini raw: {answer[:200]}")

        # JSON parsen (Gemini gibt manchmal Markdown-Codeblocks zurueck)
        clean = answer
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[-1]  # Erste Zeile (```json) entfernen
            clean = clean.rsplit("```", 1)[0]  # Letzte ``` entfernen
        clean = clean.strip()

        result = json.loads(clean)
        save_result(
            result.get("violation", False),
            result.get("rules_violated", []),
            result.get("reason", "?")
        )
    except Exception as e:
        log(f"Gemini error (fail-open): {e}")
        save_result(False, [], f"Error: {e}")

    sys.exit(0)


def mode_inject():
    """UserPromptSubmit-Hook: Gespeichertes Ergebnis injizieren."""
    # stdin konsumieren
    try:
        sys.stdin.read()
    except Exception:
        pass

    # Ergebnis-File lesen
    try:
        result = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except Exception:
        # Kein Ergebnis -> nichts injizieren
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}))
        sys.exit(0)

    if result.get("violation", False):
        rules = ", ".join(result.get("rules_violated", []))
        reason = result.get("reason", "?")
        context = (
            f"GEMINI-CHECKER VIOLATION: [{rules}] — {reason}. "
            "Korrigiere deine naechste Antwort entsprechend."
        )
        log(f"Injecting violation: {rules}")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}))

        # Ergebnis loeschen damit es nicht nochmal injiziert wird
        try:
            RESULT_FILE.unlink()
        except Exception:
            pass
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}))

    sys.exit(0)


if __name__ == "__main__":
    try:
        mode = sys.argv[1] if len(sys.argv) > 1 else "inject"
        if mode == "check":
            mode_check()
        else:
            mode_inject()
    except Exception as e:
        log(f"FATAL: {e}")
        # fail-open
        if len(sys.argv) > 1 and sys.argv[1] == "check":
            sys.exit(0)
        else:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}))
            sys.exit(0)
