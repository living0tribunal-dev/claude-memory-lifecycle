#!/usr/bin/env python3
"""Sub-Topic Awareness + Drift Detection + Topic-Switch Hook

Mode 'awareness': UserPromptSubmit (sync) — Inject PAUSED context + drift result + topic-switch briefing
Mode 'drift': Stop (async) — Compute embedding drift (PAUSED TOPIC.md) + topic-switch detection (RESUME_PROMPT.md)
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
MODEL_DIR = Path.home() / ".claude-mem" / "models" / "qwen3-0.6b-int8"

# Thresholds
RESPONSE_LENGTH_THRESHOLD = 200
DRIFT_THRESHOLD = 0.35
TOPIC_SWITCH_THRESHOLD = 0.80
TOPIC_SWITCH_BRIEFING_FILE = STATE_DIR / "topic-switch-briefing.json"


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


def awareness_mode():
    """UserPromptSubmit (sync): Inject PAUSED awareness + previous drift result."""
    messages = []

    project_dir = find_project_dir()
    if not project_dir:
        sys.exit(0)

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


def drift_mode():
    """Stop hook (async): Compute embedding drift + topic-switch detection."""
    try:
        input_data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    last_message = input_data.get("last_assistant_message", "")

    if len(last_message) < RESPONSE_LENGTH_THRESHOLD:
        sys.exit(0)

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
        import onnxruntime as ort
        from transformers import AutoTokenizer

        model_path = MODEL_DIR / "model_quantized.onnx"
        if not model_path.exists():
            log(f"ONNX-Modell nicht gefunden: {model_path}")
            sys.exit(0)

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 2
        opts.intra_op_num_threads = 2
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        session = ort.InferenceSession(
            str(model_path), opts,
            providers=['CPUExecutionProvider']
        )
        tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR), fix_mistral_regex=True)

        # Batch all texts for one inference pass
        texts = [response_text]
        topic_idx = None
        resume_idx = None
        if topic_text:
            topic_idx = len(texts)
            texts.append(topic_text)
        if resume_text:
            resume_idx = len(texts)
            texts.append(resume_text)

        tokens = tokenizer(
            texts,
            padding=True, truncation=True,
            max_length=512, return_tensors="np"
        )

        input_names = {i.name for i in session.get_inputs()}
        feed = {}
        if 'input_ids' in input_names:
            feed['input_ids'] = tokens['input_ids'].astype(np.int64)
        if 'attention_mask' in input_names:
            feed['attention_mask'] = tokens['attention_mask'].astype(np.int64)
        if 'token_type_ids' in input_names and 'token_type_ids' in tokens:
            feed['token_type_ids'] = tokens['token_type_ids'].astype(np.int64)

        outputs = session.run(None, feed)

        # Mean pooling (identical to memory-buffer.py)
        token_embeddings = outputs[0]
        mask = tokens['attention_mask'].astype(np.float32)
        mask_expanded = np.expand_dims(mask, axis=-1)
        sum_emb = np.sum(token_embeddings * mask_expanded, axis=1)
        sum_mask = np.clip(np.sum(mask_expanded, axis=1), 1e-9, None)
        embeddings = sum_emb / sum_mask

        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        embeddings = embeddings / np.clip(norms, 1e-9, None)

        STATE_DIR.mkdir(parents=True, exist_ok=True)

        # Check 1: PAUSED sub-topic drift
        if topic_idx is not None:
            similarity = float(np.dot(embeddings[0], embeddings[topic_idx]))
            drift_detected = similarity < DRIFT_THRESHOLD
            DRIFT_RESULT_FILE.write_text(json.dumps({
                "drift_detected": drift_detected,
                "similarity": similarity,
                "topic": topic_name,
                "timestamp": datetime.now().isoformat(),
            }), encoding='utf-8')
            log(f"Drift: sim={similarity:.3f}, drift={drift_detected}, topic={topic_name}")

        # Check 2: Topic switch (general)
        if resume_idx is not None:
            similarity = float(np.dot(embeddings[0], embeddings[resume_idx]))
            if similarity < TOPIC_SWITCH_THRESHOLD:
                log(f"Topic-switch: sim={similarity:.3f} < {TOPIC_SWITCH_THRESHOLD}")
                run_topic_switch_briefing(embeddings[0], project_dir)
            else:
                # Embedding says on-topic — check project name heuristic
                other = detect_other_project(response_text, project_dir)
                if other:
                    log(f"Topic-switch by name: '{other}' in {project_dir.name} (sim={similarity:.3f})")
                    run_topic_switch_briefing(embeddings[0], project_dir)
                else:
                    log(f"Topic-switch: sim={similarity:.3f} (on-topic)")
                    # Clean up old briefing if back on track
                    TOPIC_SWITCH_BRIEFING_FILE.unlink(missing_ok=True)

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
