#!/usr/bin/env python3
"""Stop Self-Check Hook — Python Version
Fordert Self-Check bei langen Antworten (>THRESHOLD Zeichen).
Event: Stop

LOOP-SCHUTZ:
1. stop_hook_active Flag pruefen
2. Eigener Zaehler (max MAX_BLOCKS Blocks pro Session)
3. Schwellwert fuer lange Antworten
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# Config
MAX_BLOCKS = 2
LENGTH_THRESHOLD = 2500
STATE_DIR = Path.home() / ".claude" / "state"
LOG_DIR = Path.home() / ".claude" / "logs"
COUNTER_FILE = STATE_DIR / "stop_check_count"
PLAN_GATE_COUNTER_FILE = STATE_DIR / "plan_gate_count"
PLAN_GATE_MAX_BLOCKS = 1
WORKAROUND_COUNTER_FILE = STATE_DIR / "workaround_count"
WORKAROUND_MAX_BLOCKS = 1
TRACKER_FILE = os.path.join(os.environ.get("TEMP", "/tmp"), "claude-research-tracker.json")

def log(message):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_DIR / "hook-execution.log", "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] stop-self-check: {message}\n")
    except Exception:
        pass

def get_counter():
    """Zaehler lesen und inkrementieren."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = str(COUNTER_FILE) + ".lock"

    # Einfaches File-Locking via increment_counter.py Muster
    try:
        count = int(COUNTER_FILE.read_text().strip())
    except Exception:
        count = 0
    count += 1
    COUNTER_FILE.write_text(str(count))
    return count

def reset_counter():
    try:
        COUNTER_FILE.write_text("0")
    except Exception:
        pass

def get_plan_gate_counter():
    """Eigener Zaehler fuer Plan-Gate (getrennt von Length-Check)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        count = int(PLAN_GATE_COUNTER_FILE.read_text().strip())
    except Exception:
        count = 0
    count += 1
    PLAN_GATE_COUNTER_FILE.write_text(str(count))
    return count


def reset_plan_gate_counter():
    try:
        PLAN_GATE_COUNTER_FILE.write_text("0")
    except Exception:
        pass


def get_workaround_counter():
    """Eigener Zaehler fuer Workaround-Check."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        count = int(WORKAROUND_COUNTER_FILE.read_text().strip())
    except Exception:
        count = 0
    count += 1
    WORKAROUND_COUNTER_FILE.write_text(str(count))
    return count


def reset_workaround_counter():
    try:
        WORKAROUND_COUNTER_FILE.write_text("0")
    except Exception:
        pass


def check_workaround(response):
    """Scannt Response auf Fix-ohne-Analyse Muster (NO-WORKAROUND)."""
    fix_rush = len(re.findall(
        r'(?i)\b(ich fix|ich beheb|ich korrigier'
        r'|lass mich.*(?:fix|beheb|korrigier)'
        r'|schnell.*(?:fix|beheb|änder|korrigier)'
        r'|let me (?:quickly |just )?fix'
        r'|quick fix'
        r'|das kann ich.*(?:fix|änder|beheb))',
        response
    ))
    if fix_rush < 1:
        return None

    has_analysis = bool(re.search(
        r'(?i)(hypothese|ursache|root.?cause'
        r'|weil.*(?:fehler|error|problem)'
        r'|warum.*(?:passiert|fehl|schlägt)'
        r'|vermutlich|das problem ist|die ursache'
        r'|the (?:cause|issue|problem) is)',
        response
    ))
    if has_analysis:
        return None

    return (
        "NO-WORKAROUND: Fix-Sprache ohne vorherige Analyse erkannt. "
        "STOPP → DENKEN → Hypothese formulieren (was ist die Ursache?) → DANN fixen."
    )


def check_plan_gate(response):
    """Scannt Response auf Reihenfolge/Prioritaet ohne Methodik-Mapping."""
    ordering = len(re.findall(
        r'(?i)\b(zuerst|als erstes|dann|danach|anschliessend'
        r'|phase [a-d]|schritt \d|priorit[äa]t|reihenfolge'
        r'|bevor|nachdem)\b',
        response
    ))
    if ordering < 2:
        return None

    has_mapping = bool(re.search(
        r'(?i)(weil .{0,40}(prinzip|methodik|mechanism|regel)'
        r'|WEIL\b'
        r'|begr[üu]nd.{0,30}(reihenfolge|priorit|ordnung))',
        response
    ))
    if has_mapping:
        return None

    return (
        "PLAN-GATE (Stufe 3): Deine Antwort enthaelt Reihenfolge-/Prioritaets-Woerter "
        "ohne Methodik-Mapping. Stufe 4 anwenden: Welches PRINZIP bestimmt die Reihenfolge? "
        "Format: '[A] vor [B] WEIL [Prinzip] sagt [Begruendung]'"
    )


def check_research_coverage():
    """Prueft ob ein Research-Tracker aktiv ist und gibt Coverage-Warnung zurueck."""
    if not os.path.exists(TRACKER_FILE):
        return None

    try:
        with open(TRACKER_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return None

    planned = state.get("planned_sources", [])
    used = state.get("used", {})
    skipped = state.get("skipped", {})
    covered = set(used.keys()) | set(skipped.keys())
    not_covered = [s for s in planned if s not in covered]

    total = len(planned)
    if total == 0:
        return None

    coverage_pct = int((len(covered) / total) * 100)

    if not not_covered:
        return None  # 100% — kein Alarm noetig

    lines = [f"RESEARCH-COVERAGE [{state.get('topic', '?')}]: {len(covered)}/{total} ({coverage_pct}%)"]
    lines.append(f"NICHT ABGEDECKT ({len(not_covered)}):")
    for src in not_covered:
        lines.append(f"  ! {src}")
    lines.append("Diese Quellen wurden geplant aber NICHT genutzt/uebersprungen.")
    return "\n".join(lines)


def main():
    # stdin lesen (Stop-Event JSON)
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        data = {}

    log("Hook triggered")

    # LOOP-SCHUTZ #1: stop_hook_active Flag
    if data.get("stop_hook_active", False):
        log("stop_hook_active=true -> allowing (loop protection)")
        sys.exit(0)

    # LOOP-SCHUTZ #2: Eigener Zaehler
    count = get_counter()
    log(f"Block count: {count} / {MAX_BLOCKS}")

    if count > MAX_BLOCKS:
        log("Max blocks reached -> allowing and resetting counter")
        reset_counter()
        sys.exit(0)

    # Antwort-Laenge pruefen
    response = data.get("last_assistant_message", "")
    response_length = len(response)
    log(f"Response length: {response_length} (threshold: {LENGTH_THRESHOLD})")

    # WORKAROUND Check (eigener Zaehler, vor Plan-Gate)
    workaround_msg = check_workaround(response)
    if workaround_msg:
        wa_count = get_workaround_counter()
        if wa_count <= WORKAROUND_MAX_BLOCKS:
            log(f"BLOCKING - workaround (block {wa_count}/{WORKAROUND_MAX_BLOCKS})")
            print(workaround_msg, file=sys.stderr)
            sys.exit(2)
        else:
            log("Workaround triggered but max blocks reached, resetting")
            reset_workaround_counter()

    # PLAN-GATE Check (eigener Zaehler, vor Length-Check)
    plan_gate_msg = check_plan_gate(response)
    if plan_gate_msg:
        pg_count = get_plan_gate_counter()
        if pg_count <= PLAN_GATE_MAX_BLOCKS:
            log(f"BLOCKING - plan-gate (block {pg_count}/{PLAN_GATE_MAX_BLOCKS})")
            print(plan_gate_msg, file=sys.stderr)
            sys.exit(2)
        else:
            log("Plan-gate triggered but max blocks reached, resetting")
            reset_plan_gate_counter()

    # Research-Coverage Check (unabhaengig von Antwort-Laenge)
    coverage_warning = check_research_coverage()
    if coverage_warning:
        log(f"BLOCKING - research coverage incomplete")
        msg = (
            f"STOP COVERAGE-CHECK\n\n"
            f"{coverage_warning}\n\n"
            "BEVOR du stoppst: Fehlende Quellen abarbeiten oder mit 'skip' + Grund markieren."
        )
        print(msg, file=sys.stderr)
        sys.exit(2)

    if response_length > LENGTH_THRESHOLD:
        log(f"BLOCKING - requesting self-check (block {count}/{MAX_BLOCKS})")
        # Exit 2 = Block, stderr = Feedback
        msg = (
            f"STOP SELF-CHECK ({count}/{MAX_BLOCKS})\n\n"
            f"Deine Antwort ist >{LENGTH_THRESHOLD} Zeichen lang.\n\n"
            "BEVOR du stoppst, beantworte kurz:\n"
            "1. Habe ich alternative Ansaetze geprueft?\n"
            "2. Was koennte ich uebersehen haben?\n"
            "3. Gibt es Gegenbeweise zu meiner Hauptthese?\n\n"
            "(Antworte kurz, dann kannst du stoppen)"
        )
        print(msg, file=sys.stderr)
        sys.exit(2)
    else:
        log(f"ALLOWING - short response ({response_length} chars)")
        sys.exit(0)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(0)  # fail-open
