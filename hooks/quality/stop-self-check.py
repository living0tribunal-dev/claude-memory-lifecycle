#!/usr/bin/env python3
"""Stop Self-Check Hook — Python Version
Fordert Self-Check bei langen Antworten (>THRESHOLD Zeichen).
Event: Stop

LOOP-SCHUTZ:
1. stop_hook_active Flag pruefen
2. Eigener Zaehler (max MAX_BLOCKS Blocks pro Session)
3. Schwellwert fuer lange Antworten

SESSION-ISOLATION:
Counter-Dateien sind session-spezifisch (via session_id aus Hook-Input).
Parallele Claude-Code-Sessions teilen keine Counter mehr.
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Config
MAX_BLOCKS = 2
LENGTH_THRESHOLD = 5000
STATE_DIR = Path.home() / ".claude" / "state"
LOG_DIR = Path.home() / ".claude" / "logs"
PLAN_GATE_MAX_BLOCKS = 1
WORKAROUND_MAX_BLOCKS = 1
GATE3_MAX_BLOCKS = 2
GATE3_MIN_LENGTH = 500
TRACKER_FILE = os.path.join(os.environ.get("TEMP", "/tmp"), "claude-research-tracker.json")
COUNTER_PREFIXES = ("stop_check_count_", "plan_gate_count_", "workaround_count_", "gate3_read_count_")

def log(message):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_DIR / "hook-execution.log", "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] stop-self-check: {message}\n")
    except Exception:
        pass


def get_counter_value(counter_file):
    """Read counter, increment, write back."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        count = int(counter_file.read_text().strip())
    except Exception:
        count = 0
    count += 1
    counter_file.write_text(str(count))
    return count


def reset_counter_value(counter_file):
    """Reset counter to 0."""
    try:
        counter_file.write_text("0")
    except Exception:
        pass


def cleanup_stale_counters(max_age_hours=24):
    """Remove session-specific counter files older than max_age_hours."""
    try:
        now = time.time()
        for f in STATE_DIR.iterdir():
            if any(f.name.startswith(p) for p in COUNTER_PREFIXES):
                if now - f.stat().st_mtime > max_age_hours * 3600:
                    f.unlink()
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


def check_gate3_reads(response):
    """Prueft ob Claude Dateien gelesen hat bevor er technisch antwortet (Gate-3)."""
    # Kurze Antworten = Chat, nicht technisch
    if len(response) < GATE3_MIN_LENGTH:
        return None

    # Write-Gate State lesen (trackt Read/Write/Grep/Glob)
    try:
        cwd = os.getcwd()
        cwd_hash = hashlib.md5(cwd.encode()).hexdigest()[:8]
        state_dir = os.path.join(os.environ.get("TEMP", "/tmp"), "claude-write-gate")
        state_file = os.path.join(state_dir, f"state-{cwd_hash}.json")
        with open(state_file, 'r') as f:
            state = json.load(f)
        reads = state.get("reads", [])
    except Exception:
        return None  # State nicht lesbar -> fail-open

    # Wenn gelesen wurde -> OK
    if len(reads) > 0:
        return None

    # Agent-Marker (Agent-Reads passieren im Subprozess, nicht getrackt)
    if re.search(r'(?i)\b(agent|subagent)\b.*\b(tool|spawn|launch|start)', response):
        return None

    # Technische Signale zaehlen
    tech_signals = len(re.findall(
        r'(```'
        r'|\.(?:py|js|ts|md|json|yaml|sh|rs|go)\b'
        r'|(?:def |class |function |import |from \S+ import)'
        r'|(?:[Ll]ine \d+|Zeile \d+)'
        r'|(?:[A-Z]:\\|/home/|/usr/|~/\.)'
        r')',
        response
    ))

    if tech_signals < 2:
        return None  # Nicht genug technische Signale

    return (
        "GATE-3 READ-CHECK: Deine Antwort enthaelt technische Inhalte "
        f"({tech_signals} Code-Signale), aber du hast keine Dateien gelesen (0 Reads). "
        "Lies zuerst die relevanten Dateien mit Read/Grep/Glob, dann antworte."
    )


def main():
    # stdin lesen (Stop-Event JSON)
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        data = {}

    # Session-Isolation: counter files per session
    session_id = data.get('session_id', '')
    if session_id:
        sid_hash = hashlib.md5(session_id.encode()).hexdigest()[:8]
    else:
        sid_hash = hashlib.md5(os.getcwd().encode()).hexdigest()[:8]

    counter_file = STATE_DIR / f"stop_check_count_{sid_hash}"
    plan_gate_counter_file = STATE_DIR / f"plan_gate_count_{sid_hash}"
    workaround_counter_file = STATE_DIR / f"workaround_count_{sid_hash}"
    gate3_counter_file = STATE_DIR / f"gate3_read_count_{sid_hash}"

    # Cleanup stale counter files from old sessions
    cleanup_stale_counters()

    log(f"Hook triggered (session={sid_hash})")

    # LOOP-SCHUTZ #1: stop_hook_active Flag
    if data.get("stop_hook_active", False):
        log("stop_hook_active=true -> allowing (loop protection)")
        sys.exit(0)

    # LOOP-SCHUTZ #2: Eigener Zaehler
    count = get_counter_value(counter_file)
    log(f"Block count: {count} / {MAX_BLOCKS}")

    if count > MAX_BLOCKS:
        log("Max blocks reached -> allowing and resetting counter")
        reset_counter_value(counter_file)
        sys.exit(0)

    # Antwort-Laenge pruefen
    response = data.get("last_assistant_message", "")
    response_length = len(response)
    log(f"Response length: {response_length} (threshold: {LENGTH_THRESHOLD})")

    # WORKAROUND Check (eigener Zaehler, vor Plan-Gate)
    workaround_msg = check_workaround(response)
    if workaround_msg:
        wa_count = get_counter_value(workaround_counter_file)
        if wa_count <= WORKAROUND_MAX_BLOCKS:
            log(f"BLOCKING - workaround (block {wa_count}/{WORKAROUND_MAX_BLOCKS})")
            print(workaround_msg, file=sys.stderr)
            sys.exit(2)
        else:
            log("Workaround triggered but max blocks reached, resetting")
            reset_counter_value(workaround_counter_file)

    # GATE-3 Read Check (eigener Zaehler)
    gate3_msg = check_gate3_reads(response)
    if gate3_msg:
        g3_count = get_counter_value(gate3_counter_file)
        if g3_count <= GATE3_MAX_BLOCKS:
            log(f"BLOCKING - gate3 reads (block {g3_count}/{GATE3_MAX_BLOCKS})")
            print(gate3_msg, file=sys.stderr)
            sys.exit(2)
        else:
            log("Gate3 triggered but max blocks reached, resetting")
            reset_counter_value(gate3_counter_file)

    # PLAN-GATE Check (eigener Zaehler, vor Length-Check)
    plan_gate_msg = check_plan_gate(response)
    if plan_gate_msg:
        pg_count = get_counter_value(plan_gate_counter_file)
        if pg_count <= PLAN_GATE_MAX_BLOCKS:
            log(f"BLOCKING - plan-gate (block {pg_count}/{PLAN_GATE_MAX_BLOCKS})")
            print(plan_gate_msg, file=sys.stderr)
            sys.exit(2)
        else:
            log("Plan-gate triggered but max blocks reached, resetting")
            reset_counter_value(plan_gate_counter_file)

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
            "(Antworte kurz, dann setze deine aktuelle Aufgabe fort)"
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
