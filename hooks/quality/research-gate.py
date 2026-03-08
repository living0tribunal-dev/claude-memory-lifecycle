#!/usr/bin/env python3
"""Research-Gate v2 — BLOCKING PreToolUse Hook fuer Task-Tool.

MECHANISMUS (Stufe 2): BLOCKIERT Research-Tasks bis Plan + User-OK vorliegt.

Flow:
1. Research-Task erkannt → BLOCKED mit voller Inventur
2. Claude MUSS Inventur + Plan dem User zeigen
3. User sagt OK → `python research-approve.py "plan"` setzt Flag
4. Naechster Task-Versuch → Gate liest Flag → approved, loescht Flag

Feuert NUR auf Task-Tool-Aufrufe.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

LOG_FILE = Path.home() / ".claude" / "hooks" / "hook-debug.log"
MCP_PROXY_CONFIG = Path.home() / ".claude" / "config" / "mcp-proxy-config.json"
FLAG_FILE = os.path.join(os.environ.get("TEMP", "/tmp"), "research-plan-approved.flag")

# Research/Analyse Keywords — diese Tasks brauchen Inventur + Plan + OK
RESEARCH_KEYWORDS = [
    "analyse", "analysis", "analys", "research", "recherche", "recherchier",
    "validier", "verifizier", "pruef", "check", "bewert", "evaluier",
    "synthese", "konsolidier", "zusammenfass", "cross-valid",
    "source-check", "fakten", "evidenz", "evidence",
    "deep dive", "tiefenanalyse", "exhausti", "gruendlich",
    "pass 1", "pass 2", "zwei-pass", "kalibrierung",
    "fto", "patent", "prior art", "freedom to operate",
    "marktanalyse", "wettbewerb", "competitor",
]

# Code/Simple Tasks — diese brauchen KEINE Inventur
CODE_KEYWORDS = [
    "explore", "code-review", "test", "build", "lint", "format",
    "find file", "search for", "grep", "glob", "read",
    "simplif", "refactor", "fix bug", "debug",
]

# Web Keywords (Legacy Web-Guard)
WEB_KEYWORDS = [
    "websearch", "webfetch", "web search", "web fetch",
    "search for", "search the web", "google", "live data",
    "current data", "real-time", "live-daten",
    "recherche", "recherchier", "web-recherche",
]

# Spezialisierte Agents
SPECIALIZED_AGENTS = {
    "source-checker": "DOI/URL/PMID Verifikation",
    "heavy-verifier": "Cross-Validation + Source Attribution",
    "research-validator": "Halluzinations-Check",
    "agent-redteam": "Gegenbeweise suchen",
    "agent-bottomup": "Bottom-Up Fakten",
    "agent-cross": "Multi-Perspektiven",
    "agent-derivative": "Derivat/Analog-Suche",
    "agent-pathway-discovery": "Signalwege identifizieren",
    "agent-toyota": "Safety/Dosis/Novelty via 5-WHY",
    "agent-einstellung-breaker": "Mental Sets brechen",
}

# Built-in MCP
BUILTIN_MCP = {
    "PubMed (claude_ai)": "search_articles, get_full_text, find_related",
}


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] ResearchGate: {msg}\n")
    except Exception:
        pass


def load_mcp_servers() -> dict:
    """Liest on-demand MCP Server aus der Proxy-Config, gruppiert nach Tags."""
    try:
        with open(MCP_PROXY_CONFIG, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        return {}

    servers = config.get("mcpServers", {})
    tag_groups = {}

    for name, info in servers.items():
        if not info.get("lazy_load", False):
            continue
        tags = info.get("tags", [])
        for tag in tags:
            if tag not in tag_groups:
                tag_groups[tag] = []
            tag_groups[tag].append(name)

    return tag_groups


def find_relevant_servers(prompt: str, tag_groups: dict) -> list:
    """Findet MCP-Server die zum Task-Prompt passen (Tag-Matching)."""
    prompt_lower = prompt.lower()
    relevant = set()

    for tag, servers in tag_groups.items():
        if tag in prompt_lower:
            relevant.update(servers)

    return sorted(relevant)


def build_inventory(prompt: str) -> str:
    """Baut Inventur mit allen verfuegbaren Ressourcen."""
    tag_groups = load_mcp_servers()
    relevant = find_relevant_servers(prompt, tag_groups)
    all_servers = set()
    for servers in tag_groups.values():
        all_servers.update(servers)

    sections = []

    # Relevant MCP
    if relevant:
        sections.append(f"RELEVANT MCP ({len(relevant)}): {', '.join(relevant)}")

    # All on-demand MCP
    sections.append(f"ALLE ON-DEMAND MCP ({len(all_servers)}): {', '.join(sorted(all_servers))}")

    # Built-in MCP
    builtin = ", ".join(f"{k}" for k in BUILTIN_MCP.keys())
    sections.append(f"BUILT-IN MCP: {builtin}")

    # Agents
    agents = ", ".join(f"{k}" for k in SPECIALIZED_AGENTS.keys())
    sections.append(f"AGENTS ({len(SPECIALIZED_AGENTS)}): {agents}")

    # Tag index
    if tag_groups:
        tag_summary = ", ".join(f"{t}({len(s)})" for t, s in sorted(tag_groups.items()))
        sections.append(f"TAGS: {tag_summary}")

    return "\n".join(sections)


def is_research_task(prompt: str) -> bool:
    """Prueft ob der Task Research/Analyse ist."""
    prompt_lower = prompt.lower()
    research_score = sum(1 for kw in RESEARCH_KEYWORDS if kw in prompt_lower)
    code_score = sum(1 for kw in CODE_KEYWORDS if kw in prompt_lower)
    return research_score > code_score and research_score >= 1


def has_web_keywords(prompt: str) -> bool:
    prompt_lower = prompt.lower()
    return any(kw in prompt_lower for kw in WEB_KEYWORDS)


def check_approval() -> dict | None:
    """Prueft ob ein genehmigter Plan vorliegt."""
    if not os.path.exists(FLAG_FILE):
        return None
    try:
        with open(FLAG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Flag konsumieren (einmalig)
        os.remove(FLAG_FILE)
        return data
    except Exception:
        return None


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        hook_input = {}

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    # Nur auf Task-Tool reagieren
    if tool_name != "Task":
        print(json.dumps({"approved": True}))
        return

    prompt = tool_input.get("prompt", "")
    is_background = tool_input.get("run_in_background", False)

    # --- Web-Guard (Background blocking) ---
    if has_web_keywords(prompt) and is_background:
        log("BLOCKED: Background agent with web keywords")
        print(json.dumps({
            "approved": False,
            "reason": (
                "AGENT-WEB-GUARD: Background Agents koennen KEINE deferred Tools "
                "(WebSearch, WebFetch) nutzen. Entweder: (1) run_in_background=false, oder "
                "(2) Web-Recherche selbst machen und Agent nur fuer Analyse nutzen."
            )
        }))
        return

    # --- Research-Gate (BLOCKING) ---
    if is_research_task(prompt):
        # Pruefe ob Plan genehmigt wurde
        approval = check_approval()

        if approval:
            # Plan vorhanden → durchlassen mit sichtbarer Bestaetigung
            plan = approval.get("plan", "?")
            log(f"APPROVED: Research task with approved plan: {plan}")
            print(json.dumps({
                "approved": True,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": (
                        f"[RESEARCH-GATE] Plan genehmigt. Ausfuehrung: {plan}\n"
                        "EXHAUSTIVE-MODUS: ALLE relevanten Quellen nutzen, "
                        "jede einzeln pruefen, nicht bei erstem Ergebnis stoppen."
                    )
                }
            }))
            return

        # Kein Plan → BLOCKIEREN mit Inventur
        log("BLOCKED: Research task without approved plan")
        inventory = build_inventory(prompt)

        print(json.dumps({
            "approved": False,
            "reason": (
                "RESEARCH-GATE BLOCKIERT — Plan + User-OK erforderlich.\n"
                "\n"
                "PFLICHT vor Research-Agent:\n"
                "1. INVENTUR dem User zeigen (siehe unten)\n"
                "2. PLAN vorstellen (welche Quellen, welche Reihenfolge, was exhaustive bedeutet)\n"
                "3. User-OK abwarten\n"
                "4. Dann: python ~/.claude/scripts/research-approve.py \"plan-zusammenfassung\"\n"
                "5. Dann: Task erneut spawnen\n"
                "\n"
                f"--- INVENTUR ---\n{inventory}\n"
                "--- ENDE INVENTUR ---\n"
                "\n"
                "ZEIGE DIESE INVENTUR DEM USER UND ERSTELLE EINEN PLAN."
            )
        }))
        return

    # --- Web-Guard Warning (foreground) ---
    if has_web_keywords(prompt) and not is_background:
        log("WARN: Foreground agent with web keywords")
        print(json.dumps({
            "approved": True,
            "hookSpecificOutput": {
                "additionalContext": (
                    "HINWEIS: Dieser Agent nutzt evtl. Web-Tools. "
                    "Deferred Tools (WebSearch/WebFetch) sind fuer Agents "
                    "oft nicht verfuegbar. Besser: Web-Daten im Main Thread "
                    "sammeln, dann Agent fuer Analyse starten."
                )
            }
        }))
        return

    # --- Default: durchlassen ---
    log("OK: Task without research keywords")
    print(json.dumps({"approved": True}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        # Fail-open
        print(json.dumps({"approved": True}))
