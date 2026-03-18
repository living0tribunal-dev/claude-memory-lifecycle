#!/usr/bin/env python3
"""Sub-Topic Awareness + Drift Detection + Manifest Context + Archetype Checks

Mode 'awareness': UserPromptSubmit (sync) — Inject manifest context + PAUSED awareness + archetype results
Mode 'drift': Stop (async) — Run archetype checks + compute embedding drift + topic-switch detection
"""
import json
import sys
import re
from datetime import datetime
from pathlib import Path

MODE = sys.argv[1] if len(sys.argv) > 1 else "awareness"

# Paths
STATE_DIR = Path.home() / ".claude" / "state"
LOG_DIR = Path.home() / ".claude" / "logs"
DRIFT_RESULT_FILE = STATE_DIR / "drift-detection-result.json"

# Thresholds
RESPONSE_LENGTH_THRESHOLD = 200
DRIFT_THRESHOLD = 0.35
TOPIC_SWITCH_THRESHOLD = 0.80
TOPIC_SWITCH_BRIEFING_FILE = STATE_DIR / "topic-switch-briefing.json"

# Error pattern matching (ETERNAL_RETURN archetype)
ERROR_PATTERNS_DB = Path.home() / ".claude-mem" / "error-patterns.sqlite3"
ERROR_PATTERN_MATCHES_FILE = STATE_DIR / "error-pattern-matches.json"
ERROR_PATTERN_THRESHOLD = 0.72
ERROR_PATTERN_MAX_MATCHES = 3

# False memory detection (FALSE_MEMORY archetype)
FALSE_MEMORY_FILE = STATE_DIR / "false-memory-result.json"
# Blind actor detection (BLIND_ACTOR archetype)
BLIND_ACTOR_FILE = STATE_DIR / "blind-actor-result.json"
# Deaf receiver detection (DEAF_RECEIVER archetype)
DOC_INDEX_DB = Path.home() / ".claude-mem" / "doc-index.sqlite3"
DEAF_RECEIVER_FILE = STATE_DIR / "deaf-receiver-result.json"
DEAF_RECEIVER_THRESHOLD = 0.70
DEAF_RECEIVER_MAX_MATCHES = 3

# Manifest context (33a: proactive dependency injection)
MANIFEST_CACHE_FILE = STATE_DIR / "manifest-context.json"
MANIFEST_FILES = ["package.json", "requirements.txt", "pyproject.toml", "Cargo.toml", "go.mod"]
MANIFEST_MAX_DEPS = 30
CONFIG_INDICATORS = [
    "tsconfig.json", ".eslintrc.json", ".eslintrc.js", "eslint.config.js",
    "vite.config.ts", "vite.config.js", "webpack.config.js",
    "babel.config.js", "jest.config.js", "vitest.config.ts",
    ".prettierrc", "app.json", "app.config.js", "metro.config.js",
    "setup.cfg", "mypy.ini", ".flake8", "tox.ini",
    "Makefile", "Dockerfile", "docker-compose.yml",
]

BUILTIN_COMMANDS = {
    # Claude Code CLI built-ins
    "help", "clear", "compact", "model", "status", "fast", "debug",
    "memory", "vim", "logout", "login", "config", "cost",
    "doctor", "upgrade", "bug", "feedback", "init", "permissions",
    # Plugin-provided skills (no commands/*.md)
    "simplify", "claude-api", "deep-research", "docx", "mcp-builder",
    "pdf", "pptx", "unified-research", "xlsx", "keybindings-help",
    "code-review", "review-pr", "feature-dev", "frontend-design",
}


def log(message):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_DIR / "hook-execution.log", "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] subtopic-awareness: {message}\n")
    except Exception:
        pass


def find_project_dir():
    """Find _RESEARCH/project/ directory from CWD."""
    cwd = Path.cwd()

    # Case 1: CWD is inside _RESEARCH/project/
    parts = cwd.parts
    for i, part in enumerate(parts):
        if part == "_RESEARCH" and i + 1 < len(parts):
            project_dir = Path(*parts[:i + 2])
            if (project_dir / "STATE.md").exists():
                return project_dir

    # Case 2: CWD has _RESEARCH/ subdirectory
    research_dir = cwd / "_RESEARCH"
    if research_dir.exists():
        for d in sorted(research_dir.iterdir()):
            if d.is_dir() and (d / "STATE.md").exists():
                return d

    return None


def find_paused_and_topic(project_dir):
    """Extract PAUSED info and active topic name from STATE.md."""
    state_file = project_dir / "STATE.md"
    if not state_file.exists():
        return None, None

    content = state_file.read_text(encoding='utf-8')

    paused_match = re.search(
        r'## Aktueller Fokus\n(PAUSED:.*?)(?=\n##|\Z)',
        content, re.DOTALL
    )
    if not paused_match:
        return None, None

    paused_text = paused_match.group(1).strip()

    topic_match = re.search(r'Sub-Topic\s+(\S+)\s+gestartet', paused_text)
    topic_name = topic_match.group(1) if topic_match else None

    return paused_text, topic_name


def get_known_projects():
    """Get all known project names from buffer DB."""
    import sqlite3
    db_path = Path.home() / ".claude-mem" / "buffer.sqlite3"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT DISTINCT project FROM buffer_entries WHERE project IS NOT NULL"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def detect_other_project(response_text, current_project_dir):
    """Check if response mentions a different project more than the current one."""
    current_name = current_project_dir.name.lower()
    response_lower = response_text.lower()
    current_count = response_lower.count(current_name)

    for name in get_known_projects():
        name_lower = name.lower()
        if name_lower == current_name:
            continue
        if len(name_lower) < 4:
            continue
        other_count = response_lower.count(name_lower)
        if other_count > 0 and other_count > current_count:
            return name

    return None


def run_manifest_context():
    """Inject project dependencies + config files as context (33a).

    Proactively provides installed package info so Claude doesn't need to
    "know" to check. Architecture v3: injects FACTS, not instructions.
    """
    cwd = Path.cwd()

    # Find nearest manifest (walk up from CWD, like npm resolution)
    manifest_path = None
    manifest_name = None
    home = Path.home()
    search_dir = cwd
    while True:
        for name in MANIFEST_FILES:
            candidate = search_dir / name
            if candidate.exists():
                manifest_path = candidate
                manifest_name = name
                break
        if manifest_path:
            break
        if search_dir == home or search_dir == search_dir.parent:
            break
        search_dir = search_dir.parent

    if not manifest_path:
        MANIFEST_CACHE_FILE.unlink(missing_ok=True)
        return None

    # Check cache (CWD + mtime)
    try:
        mtime = manifest_path.stat().st_mtime
        if MANIFEST_CACHE_FILE.exists():
            cache = json.loads(MANIFEST_CACHE_FILE.read_text(encoding='utf-8'))
            if cache.get("cwd") == str(cwd) and cache.get("mtime") == mtime:
                return cache.get("message")
    except Exception:
        pass

    # Parse dependencies
    deps = _parse_manifest(manifest_path, manifest_name)
    if not deps:
        return None

    # Build message parts
    parts = []

    dep_list = ", ".join(deps[:MANIFEST_MAX_DEPS])
    suffix = f" (+{len(deps) - MANIFEST_MAX_DEPS})" if len(deps) > MANIFEST_MAX_DEPS else ""
    parts.append(f"deps: {dep_list}{suffix}")

    # Scripts (package.json only)
    if manifest_name == "package.json":
        try:
            data = json.loads(manifest_path.read_text(encoding='utf-8'))
            scripts = list(data.get("scripts", {}).keys())
            if scripts:
                parts.append(f"scripts: {', '.join(scripts[:10])}")
        except Exception:
            pass

    # Config files present in CWD
    configs = [n for n in CONFIG_INDICATORS if (cwd / n).exists()]
    if configs:
        parts.append(f"configs: {', '.join(configs)}")

    message = f"Projekt-Kontext ({manifest_name}): {' | '.join(parts)}"

    # Cache result
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        MANIFEST_CACHE_FILE.write_text(json.dumps({
            "cwd": str(cwd),
            "mtime": mtime,
            "message": message,
            "timestamp": datetime.now().isoformat(),
        }), encoding='utf-8')
    except Exception:
        pass

    log(f"Manifest context: {len(deps)} deps from {manifest_name}")
    return message


def _parse_manifest(path, name):
    """Parse dependency names from manifest file."""
    try:
        content = path.read_text(encoding='utf-8')
    except Exception:
        return []

    if name == "package.json":
        return _parse_package_json(content)
    elif name == "requirements.txt":
        return _parse_requirements_txt(content)
    elif name == "pyproject.toml":
        return _parse_pyproject_toml(content)
    elif name == "Cargo.toml":
        return _parse_cargo_toml(content)
    elif name == "go.mod":
        return _parse_go_mod(content)
    return []


def _parse_package_json(content):
    try:
        data = json.loads(content)
        deps = list(data.get("dependencies", {}).keys())
        deps += list(data.get("devDependencies", {}).keys())
        return deps
    except Exception:
        return []


def _parse_requirements_txt(content):
    deps = []
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and not line.startswith('-'):
            name = re.split(r'[>=<!\[;@]', line)[0].strip()
            if name:
                deps.append(name)
    return deps


def _parse_pyproject_toml(content):
    deps = []
    try:
        import tomllib
        data = tomllib.loads(content)
        for dep_str in data.get("project", {}).get("dependencies", []):
            name = re.split(r'[>=<!\[;]', dep_str)[0].strip()
            if name:
                deps.append(name)
        for name in data.get("tool", {}).get("poetry", {}).get("dependencies", {}).keys():
            if name != "python":
                deps.append(name)
    except ImportError:
        in_deps = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith('[') and 'dependencies' in stripped.lower():
                in_deps = True
                continue
            if in_deps:
                if stripped.startswith('['):
                    in_deps = False
                    continue
                if '=' in stripped:
                    name = stripped.split('=')[0].strip().strip('"')
                    if name and name != 'python':
                        deps.append(name)
    except Exception:
        pass
    return deps


def _parse_cargo_toml(content):
    deps = []
    in_deps = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == '[dependencies]':
            in_deps = True
            continue
        if in_deps:
            if stripped.startswith('['):
                break
            if '=' in stripped and not stripped.startswith('#'):
                name = stripped.split('=')[0].strip()
                if name:
                    deps.append(name)
    return deps


def _parse_go_mod(content):
    deps = []
    in_require = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith('require ('):
            in_require = True
            continue
        if stripped.startswith('require ') and '(' not in stripped:
            parts = stripped.split()
            if len(parts) >= 2:
                module = parts[1]
                deps.append(module.split('/')[-1] if '/' in module else module)
            continue
        if in_require:
            if stripped == ')':
                in_require = False
                continue
            parts = stripped.split()
            if parts and not stripped.startswith('//'):
                module = parts[0]
                deps.append(module.split('/')[-1] if '/' in module else module)
    return deps


def awareness_mode():
    """UserPromptSubmit (sync): Inject manifest context + PAUSED awareness + archetype results."""
    messages = []

    # === Non-project-specific checks (run in ANY directory) ===

    # 33a: Manifest context injection
    manifest_msg = run_manifest_context()
    if manifest_msg:
        messages.append(manifest_msg)

    # FALSE_MEMORY injection (state file from drift_mode)
    if FALSE_MEMORY_FILE.exists():
        try:
            fm_result = json.loads(FALSE_MEMORY_FILE.read_text(encoding='utf-8'))
            issues = fm_result.get("issues", [])
            if issues:
                fm_lines = ["FALSE_MEMORY: Letzte Antwort referenzierte nicht-existente Ressourcen:"]
                for issue in issues:
                    if issue["type"] == "file":
                        fm_lines.append(f"  DATEI EXISTIERT NICHT: {issue['path']}")
                    elif issue["type"] == "command":
                        fm_lines.append(f"  COMMAND EXISTIERT NICHT: {issue['name']}")
                messages.append("\n".join(fm_lines))
            FALSE_MEMORY_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    # BLIND_ACTOR injection (state file from drift_mode)
    if BLIND_ACTOR_FILE.exists():
        try:
            ba_result = json.loads(BLIND_ACTOR_FILE.read_text(encoding='utf-8'))
            issues = ba_result.get("issues", [])
            if issues:
                ba_lines = ["BLIND_ACTOR: Letzte Antwort referenzierte Dateien die NICHT gelesen wurden (0 Reads):"]
                for issue in issues:
                    ba_lines.append(f"  NICHT GELESEN: {issue['path']}")
                messages.append("\n".join(ba_lines))
            BLIND_ACTOR_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    # === Project-specific checks (only in _RESEARCH/) ===
    project_dir = find_project_dir()

    if project_dir:
        paused_text, topic_name = find_paused_and_topic(project_dir)

        if paused_text:
            messages.append(f"SUB-TOPIC AKTIV — {paused_text}")
            if topic_name:
                topic_file = project_dir / "research" / topic_name / "TOPIC.md"
                if topic_file.exists():
                    messages.append(f"TOPIC-Datei: research/{topic_name}/TOPIC.md")

        # Check for drift result from previous stop hook
        if DRIFT_RESULT_FILE.exists():
            try:
                result = json.loads(DRIFT_RESULT_FILE.read_text(encoding='utf-8'))
                if result.get("drift_detected"):
                    sim = result.get("similarity", 0)
                    topic = result.get("topic", "?")
                    messages.append(
                        f"DRIFT ERKANNT (sim={sim:.2f} zu '{topic}'): "
                        f"Claudes letzte Antwort war thematisch weit vom aktiven Sub-Topic entfernt. "
                        f"Pruefe ob Themenwechsel stattfindet → /subtopic oder Rueckkehr zum Hauptstrang."
                    )
                DRIFT_RESULT_FILE.unlink(missing_ok=True)
            except Exception:
                pass

        # Check for topic-switch briefing from previous stop hook
        if TOPIC_SWITCH_BRIEFING_FILE.exists():
            try:
                ts_result = json.loads(TOPIC_SWITCH_BRIEFING_FILE.read_text(encoding='utf-8'))
                briefing_output = ts_result.get("briefing_output", "")
                if briefing_output:
                    messages.append(
                        f"THEMENWECHSEL ERKANNT — Re-Briefing:\n{briefing_output}"
                    )
                TOPIC_SWITCH_BRIEFING_FILE.unlink(missing_ok=True)
            except Exception:
                pass

        # Check for error pattern matches from previous stop hook (ETERNAL_RETURN)
        if ERROR_PATTERN_MATCHES_FILE.exists():
            try:
                ep_result = json.loads(ERROR_PATTERN_MATCHES_FILE.read_text(encoding='utf-8'))
                matches = ep_result.get("matches", [])
                if matches:
                    ep_lines = ["BEKANNTE FEHLERMUSTER (aehnliche Aufgaben):"]
                    for m in matches[:2]:
                        preview = m["text"][:150].replace('\n', ' ')
                        ep_lines.append(f"  [sim={m['similarity']}] {preview}")
                    messages.append("\n".join(ep_lines))
                ERROR_PATTERN_MATCHES_FILE.unlink(missing_ok=True)
            except Exception:
                pass

        # Check for deaf receiver results from previous stop hook (DEAF_RECEIVER)
        if DEAF_RECEIVER_FILE.exists():
            try:
                dr_result = json.loads(DEAF_RECEIVER_FILE.read_text(encoding='utf-8'))
                matches = dr_result.get("matches", [])
                if matches:
                    dr_lines = ["DEAF_RECEIVER: Relevante Docs fuer aktuellen Kontext (NICHT im Kontext geladen):"]
                    for m in matches[:2]:
                        dr_lines.append(f"  ~/.claude/{m['path']} — \"{m['description']}\"")
                    messages.append("\n".join(dr_lines))
                DEAF_RECEIVER_FILE.unlink(missing_ok=True)
            except Exception:
                pass

        # Cross-project relevance injection (S39)
        try:
            import sqlite3
            db_path = Path.home() / ".claude-mem" / "buffer.sqlite3"
            if db_path.exists():
                current_project = project_dir.name
                cp_conn = sqlite3.connect(str(db_path))
                cp_rows = cp_conn.execute("""
                    SELECT cr.id, cr.entry_project, cr.similarity, cr.method,
                           SUBSTR(b.text, 1, 200) as preview
                    FROM cross_project_relevance cr
                    JOIN buffer_entries b ON cr.entry_id = b.id
                    WHERE cr.relevant_to = ? AND cr.shown = 0
                    ORDER BY cr.created_at DESC LIMIT 2
                """, (current_project,)).fetchall()
                if cp_rows:
                    cp_lines = ["CROSS-PROJEKT: Findings aus anderen Projekten relevant fuer dieses:"]
                    cp_ids = []
                    for cid, eproj, sim, method, preview in cp_rows:
                        sim_str = f"sim={sim:.3f}" if sim else "keyword"
                        preview_clean = preview.replace('\n', ' ')[:100]
                        cp_lines.append(f"  [{eproj}] ({method}: {sim_str}): {preview_clean}")
                        cp_ids.append(cid)
                    messages.append("\n".join(cp_lines))
                    # Mark as shown
                    cp_conn.executemany(
                        "UPDATE cross_project_relevance SET shown = 1 WHERE id = ?",
                        [(cid,) for cid in cp_ids]
                    )
                    cp_conn.commit()
                cp_conn.close()
        except Exception:
            pass

    # Gate-3 fact-based reminder (v3 architecture: facts not imperatives)
    try:
        import hashlib, os, time as _time
        cwd = str(Path.cwd())
        cwd_hash = hashlib.md5(cwd.encode()).hexdigest()[:8]
        wg_state_file = Path(os.environ.get("TEMP", "/tmp")) / "claude-write-gate" / f"state-{cwd_hash}.json"
        reads_count = 0
        if wg_state_file.exists():
            wg_state = json.loads(wg_state_file.read_text(encoding='utf-8'))
            reads_count = len(wg_state.get("reads", []))
        # Count files in CWD (top-level only, fast)
        cwd_path = Path.cwd()
        all_files = [f for f in cwd_path.iterdir() if f.is_file() and not f.name.startswith('.')]
        n_files = len(all_files)
        # Recently modified (last 1h)
        one_hour_ago = _time.time() - 3600
        recent = sum(1 for f in all_files if f.stat().st_mtime > one_hour_ago)
        recent_str = f", {recent} kuerzlich geaendert" if recent > 0 else ""
        messages.append(f"GATE-3: {n_files} Dateien in CWD{recent_str}. {reads_count} in diesem Prompt gelesen.")
    except Exception:
        messages.append("GATE-3: Erst LESEN (Read/Grep), dann antworten.")

    if messages:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(messages)}}))

    sys.exit(0)


def run_topic_switch_briefing(response_embedding, project_dir):
    """Inline briefing using pre-computed embedding against buffer DB."""
    import sqlite3
    import numpy as np

    BRIEFING_MIN_SIM = 0.50
    BRIEFING_MAX_ENTRIES = 10

    db_path = Path.home() / ".claude-mem" / "buffer.sqlite3"
    if not db_path.exists():
        return

    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("""
            SELECT e.entry_id, e.embedding, b.text, b.state, b.project, b.created_at
            FROM entry_embeddings e
            JOIN buffer_entries b ON e.entry_id = b.id
            WHERE b.state != 'expired'
        """).fetchall()
        conn.close()
    except Exception as e:
        log(f"Topic-switch DB FEHLER: {e}")
        return

    if not rows:
        return

    results = []
    for entry_id, emb_blob, text, state, proj, created in rows:
        vec = np.frombuffer(emb_blob, dtype=np.float32).copy()
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        sim = float(np.dot(response_embedding, vec))
        results.append((entry_id, sim, text, state, proj, created))

    results.sort(key=lambda x: x[1], reverse=True)
    results = [r for r in results if r[1] >= BRIEFING_MIN_SIM][:BRIEFING_MAX_ENTRIES]

    if not results:
        return

    lines = ["=== RE-BRIEFING (Topic-Switch) ===",
             f"Relevante Entries: {len(results)}\n"]
    for eid, sim, text, state, proj, created in results:
        preview = text[:200].replace('\n', ' ')
        lines.append(f"  [{eid}] {state} (sim={sim:.3f}) proj={proj or 'NULL'} ({created[:10]})")
        lines.append(f"      {preview}")
        lines.append("")

    output = "\n".join(lines)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TOPIC_SWITCH_BRIEFING_FILE.write_text(json.dumps({
        "briefing_output": output,
        "timestamp": datetime.now().isoformat(),
    }), encoding='utf-8')
    log(f"Topic-switch briefing gespeichert ({len(output)} bytes)")


def run_error_pattern_search(response_embedding):
    """Search error patterns against response embedding (ETERNAL_RETURN archetype).

    Matches pre-extracted #learning entries from claude-mem against Claude's
    last response. High-similarity matches indicate the current task is similar
    to a previously documented error — inject as FACT, not instruction.
    """
    import sqlite3
    import numpy as np

    if not ERROR_PATTERNS_DB.exists():
        return

    try:
        conn = sqlite3.connect(str(ERROR_PATTERNS_DB))
        rows = conn.execute(
            "SELECT id, text, embedding, project FROM error_patterns"
        ).fetchall()
        conn.close()
    except Exception as e:
        log(f"Error pattern DB FEHLER: {e}")
        return

    if not rows:
        return

    results = []
    for ep_id, text, emb_blob, proj in rows:
        vec = np.frombuffer(emb_blob, dtype=np.float32).copy()
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        sim = float(np.dot(response_embedding, vec))
        if sim >= ERROR_PATTERN_THRESHOLD:
            results.append({
                "id": ep_id,
                "similarity": round(sim, 3),
                "text": text[:500],
                "project": proj,
            })

    results.sort(key=lambda x: x["similarity"], reverse=True)
    results = results[:ERROR_PATTERN_MAX_MATCHES]

    if results:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ERROR_PATTERN_MATCHES_FILE.write_text(json.dumps({
            "matches": results,
            "timestamp": datetime.now().isoformat(),
        }), encoding='utf-8')
        log(f"Error patterns: {len(results)} matches (top sim={results[0]['similarity']})")
    else:
        ERROR_PATTERN_MATCHES_FILE.unlink(missing_ok=True)


def run_false_memory_check(response_text):
    """Detect file/command references that don't exist (FALSE_MEMORY archetype).

    Checks Claude's response for:
    1. File paths (~/... and absolute) that don't exist on disk
    2. Slash commands that aren't in commands/*.md or built-in list

    Architecture v3: injects FACTS ("file X doesn't exist"), not instructions.
    """
    issues = []
    creation_words = {"creat", "erstell", "writ", "schreib", "anleg", "erzeug"}

    # === 1. File-Existence Check ===
    tilde_pattern = re.compile(r'~[/\\][\w./\\-]+\.\w+')
    abs_pattern = re.compile(r'[A-Za-z]:[/\\][\w./\\-]+\.\w+')
    seen_paths = set()

    for pattern in [tilde_pattern, abs_pattern]:
        for match in pattern.finditer(response_text):
            path_str = match.group(0)

            if path_str in seen_paths:
                continue
            seen_paths.add(path_str)

            # Skip if inside URL
            pre_start = max(0, match.start() - 10)
            pre_text = response_text[pre_start:match.start()]
            if "://" in pre_text or "http" in pre_text:
                continue

            # 100-char creation context filter
            ctx_start = max(0, match.start() - 100)
            context_before = response_text[ctx_start:match.start()].lower()
            if any(w in context_before for w in creation_words):
                continue

            # Check existence
            expanded = Path(path_str).expanduser()
            if not expanded.exists():
                issues.append({"type": "file", "path": path_str})

    # === 2. Command Check ===
    cmd_pattern = re.compile(r'(?:^|[\s`("])/([a-z][\w-]{2,})(?![/\\:])', re.MULTILINE)
    commands_dir = Path.home() / ".claude" / "commands"
    seen_cmds = set()

    for match in cmd_pattern.finditer(response_text):
        cmd_name = match.group(1).lower()

        if cmd_name in seen_cmds or cmd_name in BUILTIN_COMMANDS:
            continue
        seen_cmds.add(cmd_name)

        # 100-char creation context filter
        ctx_start = max(0, match.start() - 100)
        context_before = response_text[ctx_start:match.start()].lower()
        if any(w in context_before for w in creation_words):
            continue

        # Check commands/*.md
        cmd_file = commands_dir / f"{cmd_name}.md"
        if not cmd_file.exists():
            issues.append({"type": "command", "name": f"/{cmd_name}"})

    # === Write results ===
    if issues:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        FALSE_MEMORY_FILE.write_text(json.dumps({
            "issues": issues,
            "timestamp": datetime.now().isoformat(),
        }), encoding='utf-8')
        log(f"FALSE_MEMORY: {len(issues)} issues found")
    else:
        FALSE_MEMORY_FILE.unlink(missing_ok=True)


def run_deaf_receiver_check(response_embedding):
    """Find relevant docs for current context (DEAF_RECEIVER archetype).

    Compares Claude's response embedding against pre-embedded doc index.
    High-similarity docs are injected as FACTS on next UserPromptSubmit:
    "these docs exist and are relevant to your current task."

    Architecture v3: injects FACTS (doc existence), not instructions (read them).
    """
    import sqlite3
    import numpy as np

    if not DOC_INDEX_DB.exists():
        return

    try:
        conn = sqlite3.connect(str(DOC_INDEX_DB))
        rows = conn.execute(
            "SELECT id, path, name, description, embedding FROM doc_index"
        ).fetchall()
        conn.close()
    except Exception as e:
        log(f"DEAF_RECEIVER DB error: {e}")
        return

    if not rows:
        return

    results = []
    for doc_id, path, name, description, emb_blob in rows:
        vec = np.frombuffer(emb_blob, dtype=np.float32).copy()
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        sim = float(np.dot(response_embedding, vec))
        if sim >= DEAF_RECEIVER_THRESHOLD:
            results.append({
                "id": doc_id,
                "similarity": round(sim, 3),
                "path": path,
                "name": name,
                "description": description,
            })

    results.sort(key=lambda x: x["similarity"], reverse=True)
    results = results[:DEAF_RECEIVER_MAX_MATCHES]

    if results:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        DEAF_RECEIVER_FILE.write_text(json.dumps({
            "matches": results,
            "timestamp": datetime.now().isoformat(),
        }), encoding='utf-8')
        log(f"DEAF_RECEIVER: {len(results)} relevant docs (top sim={results[0]['similarity']})")
    else:
        DEAF_RECEIVER_FILE.unlink(missing_ok=True)


def run_blind_actor_check(response_text):
    """Detect if Claude discussed files without reading any (BLIND_ACTOR archetype).

    Checks: 0 reads in write-gate state + response references existing files.
    Architecture v3: injects FACT ("you discussed X without reading anything").
    """
    import os
    import hashlib

    # Read write-gate state for current prompt
    wg_dir = os.path.join(os.environ.get("TEMP", "/tmp"), "claude-write-gate")
    cwd_hash = hashlib.md5(os.getcwd().encode()).hexdigest()[:8]
    wg_file = os.path.join(wg_dir, f"state-{cwd_hash}.json")

    try:
        with open(wg_file, 'r') as f:
            wg_state = json.load(f)
    except Exception:
        return

    reads = wg_state.get("reads", [])

    # If Claude read any files this prompt, skip
    if len(reads) >= 1:
        BLIND_ACTOR_FILE.unlink(missing_ok=True)
        return

    # 0 reads — check if response references existing files
    creation_words = {"creat", "erstell", "writ", "schreib", "anleg", "erzeug"}
    tilde_pattern = re.compile(r'~[/\\][\w./\\-]+\.\w+')
    abs_pattern = re.compile(r'[A-Za-z]:[/\\][\w./\\-]+\.\w+')
    referenced_files = set()

    for pattern in [tilde_pattern, abs_pattern]:
        for match in pattern.finditer(response_text):
            path_str = match.group(0)

            # Skip URLs
            pre_start = max(0, match.start() - 10)
            if "://" in response_text[pre_start:match.start()]:
                continue

            # Skip creation context
            ctx_start = max(0, match.start() - 100)
            if any(w in response_text[ctx_start:match.start()].lower() for w in creation_words):
                continue

            # Only existing files (non-existent = FALSE_MEMORY, not BLIND_ACTOR)
            expanded = Path(path_str).expanduser()
            if expanded.exists():
                referenced_files.add(path_str)

    if referenced_files:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        BLIND_ACTOR_FILE.write_text(json.dumps({
            "issues": [{"path": p} for p in referenced_files],
            "reads_this_prompt": 0,
            "timestamp": datetime.now().isoformat(),
        }), encoding='utf-8')
        log(f"BLIND_ACTOR: {len(referenced_files)} files referenced with 0 reads")
    else:
        BLIND_ACTOR_FILE.unlink(missing_ok=True)


def drift_mode():
    """Stop hook (async): Run archetype checks + compute embedding drift."""
    try:
        input_data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    last_message = input_data.get("last_assistant_message", "")

    if len(last_message) < RESPONSE_LENGTH_THRESHOLD:
        sys.exit(0)

    # === Non-project-specific checks (run in ANY directory) ===

    # FALSE_MEMORY check (no embeddings needed, runs on full response)
    try:
        run_false_memory_check(last_message)
    except Exception as e:
        log(f"FALSE_MEMORY check FEHLER: {e}")

    # BLIND_ACTOR check (cross-ref with write-gate, no embeddings needed)
    try:
        run_blind_actor_check(last_message)
    except Exception as e:
        log(f"BLIND_ACTOR check FEHLER: {e}")

    # === Project-specific checks (need _RESEARCH/ context) ===
    project_dir = find_project_dir()
    if not project_dir:
        sys.exit(0)

    response_text = last_message[:1000]

    # Gather comparison targets
    paused_text, topic_name = find_paused_and_topic(project_dir)

    topic_text = None
    if topic_name:
        topic_file = project_dir / "research" / topic_name / "TOPIC.md"
        if topic_file.exists():
            topic_text = topic_file.read_text(encoding='utf-8')[:1000]

    resume_text = None
    resume_file = project_dir / "RESUME_PROMPT.md"
    if resume_file.exists():
        resume_text = resume_file.read_text(encoding='utf-8')[:1000]

    if not topic_text and not resume_text:
        sys.exit(0)

    try:
        import numpy as np
        sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))
        from embedding_client import get_embedding

        # Get embeddings via persistent server (77ms each, no 19s model load)
        response_emb = get_embedding(response_text)
        if response_emb is None:
            log("Embedding server not running — skipping drift/topic/error checks")
            sys.exit(0)

        topic_emb = get_embedding(topic_text) if topic_text else None
        resume_emb = get_embedding(resume_text) if resume_text else None

        STATE_DIR.mkdir(parents=True, exist_ok=True)

        # Check 1: PAUSED sub-topic drift
        if topic_emb is not None:
            similarity = float(np.dot(response_emb, topic_emb))
            drift_detected = similarity < DRIFT_THRESHOLD
            DRIFT_RESULT_FILE.write_text(json.dumps({
                "drift_detected": drift_detected,
                "similarity": similarity,
                "topic": topic_name,
                "timestamp": datetime.now().isoformat(),
            }), encoding='utf-8')
            log(f"Drift: sim={similarity:.3f}, drift={drift_detected}, topic={topic_name}")

        # Check 2: Topic switch (general)
        if resume_emb is not None:
            similarity = float(np.dot(response_emb, resume_emb))
            if similarity < TOPIC_SWITCH_THRESHOLD:
                log(f"Topic-switch: sim={similarity:.3f} < {TOPIC_SWITCH_THRESHOLD}")
                run_topic_switch_briefing(response_emb, project_dir)
            else:
                # Embedding says on-topic — check project name heuristic
                other = detect_other_project(response_text, project_dir)
                if other:
                    log(f"Topic-switch by name: '{other}' in {project_dir.name} (sim={similarity:.3f})")
                    run_topic_switch_briefing(response_emb, project_dir)
                else:
                    log(f"Topic-switch: sim={similarity:.3f} (on-topic)")
                    # Clean up old briefing if back on track
                    TOPIC_SWITCH_BRIEFING_FILE.unlink(missing_ok=True)

        # Check 3: Error pattern matching (ETERNAL_RETURN archetype)
        run_error_pattern_search(response_emb)

        # Check 4: Relevant docs not in context (DEAF_RECEIVER archetype)
        run_deaf_receiver_check(response_emb)

    except Exception as e:
        log(f"Drift/Topic-switch FEHLER: {e}")

    sys.exit(0)


if __name__ == "__main__":
    if MODE == "awareness":
        awareness_mode()
    elif MODE == "drift":
        drift_mode()
    else:
        print(f"Unknown mode: {MODE}", file=sys.stderr)
        sys.exit(1)
