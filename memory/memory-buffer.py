#!/usr/bin/env python3
"""memory-buffer.py - Phase 1 Memory Buffer System

Buffer-basiertes Memory mit Embeddings und Connection-Tracking.
Ersetzt claude-mem.py add fuer neue Eintraege.
Altes System (claude-mem.py) bleibt als Read-Only-Archiv.

Usage:
    python memory-buffer.py add <text>          # Neuen Eintrag in Buffer
    python memory-buffer.py embed-pending       # Pending Eintraege embedden + Connections
    python memory-buffer.py search <query>      # Semantische Suche
    python memory-buffer.py get <id>            # Eintrag per ID
    python memory-buffer.py connections <id>    # Verbindungen eines Eintrags
    python memory-buffer.py status              # Buffer-Statistiken
    python memory-buffer.py setup-model         # ONNX-Modell einrichten
    python memory-buffer.py route               # Proven Eintraege routen (Gemini)
    python memory-buffer.py conflict-check      # Konflikte mit Ziel-System pruefen (Gemini)
    python memory-buffer.py write-target        # Ins Ziel-System schreiben
    python memory-buffer.py diamond-check      # Isolierte Entries pruefen (Diamant-Schutz)
    python memory-buffer.py age               # Verblassen: isolierte gealterte Entries pruefen
    python memory-buffer.py migrate <id> ...  # Alte claude-mem Entries in Buffer migrieren
"""

import sqlite3
import hashlib
import sys
import json
from datetime import datetime
from pathlib import Path

# UTF-8 for Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ─── Configuration ───────────────────────────────────────────────

BUFFER_DB_PATH = Path.home() / ".claude-mem" / "buffer.sqlite3"
MODEL_DIR = Path.home() / ".claude-mem" / "models" / "qwen3-0.6b-int8"
MODEL_NAME = "qwen3-embedding-0.6b"
EMBEDDING_DIM = 1024
CONNECTION_THRESHOLD = 0.75
CLUSTER_THRESHOLD = 0.80
ROUTING_MODEL = "gemini-2.5-flash"
FALLBACK_MODEL = "gemini-3.1-flash-lite-preview"
AGE_THRESHOLD = 20      # Nach N neueren Entries wird Isolation geprueft
MAX_REPRIEVES = 3       # Nach N positiven Substance-Checks ohne Cluster → expire
RECALL_PROMOTE_THRESHOLD = 3  # Nach N Recalls wird Buffer-Entry zu proven
BRIEFING_MIN_SIM = 0.50       # Minimum Similarity fuer Briefing-Entries
BRIEFING_MAX_ENTRIES = 10     # Hard Cap fuer Briefing

# ─── Lazy-loaded ONNX globals ───────────────────────────────────

_session = None
_tokenizer = None

# ─── Schema ──────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE buffer_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    state TEXT DEFAULT 'buffer'
        CHECK(state IN ('buffer','proven','permanent','expired')),
    created_at TEXT NOT NULL,
    recall_count INTEGER DEFAULT 0,
    last_recalled_at TEXT,
    project TEXT,
    entry_type TEXT
);
CREATE UNIQUE INDEX idx_text_hash ON buffer_entries(text_hash);

CREATE TABLE entry_embeddings (
    entry_id INTEGER PRIMARY KEY REFERENCES buffer_entries(id),
    embedding BLOB NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE connections (
    entry_a INTEGER NOT NULL REFERENCES buffer_entries(id),
    entry_b INTEGER NOT NULL REFERENCES buffer_entries(id),
    similarity REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (entry_a, entry_b),
    CHECK (entry_a < entry_b)
);
CREATE INDEX idx_conn_a ON connections(entry_a);
CREATE INDEX idx_conn_b ON connections(entry_b);

CREATE TABLE system_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# ─── Database ────────────────────────────────────────────────────

def get_db():
    """Connect to buffer DB, create schema if needed."""
    BUFFER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(BUFFER_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    if 'buffer_entries' not in tables:
        conn.executescript(SCHEMA_SQL)
        conn.execute("INSERT INTO system_meta VALUES ('schema_version', '1')")
        conn.execute("INSERT INTO system_meta VALUES ('embedding_model', ?)", (MODEL_NAME,))
        conn.execute("INSERT INTO system_meta VALUES ('embedding_dimensions', ?)", (str(EMBEDDING_DIM),))
        conn.execute("INSERT INTO system_meta VALUES ('connection_threshold', ?)", (str(CONNECTION_THRESHOLD),))
        conn.commit()

    # Migration: project column
    cols = {r[1] for r in conn.execute("PRAGMA table_info(buffer_entries)").fetchall()}
    if 'project' not in cols:
        conn.execute("ALTER TABLE buffer_entries ADD COLUMN project TEXT")
        conn.commit()

    # Migration: reprieve_count column
    if 'reprieve_count' not in cols:
        conn.execute("ALTER TABLE buffer_entries ADD COLUMN reprieve_count INTEGER DEFAULT 0")
        conn.commit()

    # Migration: entry_type column + backfill + cleanup
    if 'entry_type' not in cols:
        conn.execute("ALTER TABLE buffer_entries ADD COLUMN entry_type TEXT")
        # Backfill existing entries
        conn.execute("""
            UPDATE buffer_entries SET entry_type = CASE
                WHEN text LIKE 'AUTO-SESSION-SAVE%' THEN 'auto-session-save'
                WHEN substr(text, 1, 300) LIKE '%#decision%' OR substr(text, 1, 300) LIKE '%#entscheidung%' THEN 'decision'
                WHEN substr(text, 1, 300) LIKE '%#user-gedanke%' THEN 'user-gedanke'
                WHEN substr(text, 1, 300) LIKE '%#session-save%' THEN 'session-save'
                ELSE 'insight'
            END
        """)
        # Auto-expire empty auto-session-saves still in buffer
        conn.execute("""
            UPDATE buffer_entries SET state = 'expired'
            WHERE entry_type = 'auto-session-save'
            AND state = 'buffer'
            AND text LIKE '%User Messages: 0%'
        """)
        # Cleanup: delete connections where both entries are expired
        conn.execute("""
            DELETE FROM connections
            WHERE entry_a IN (SELECT id FROM buffer_entries WHERE state = 'expired')
            AND entry_b IN (SELECT id FROM buffer_entries WHERE state = 'expired')
        """)
        conn.commit()

    # Migration: remove cross-project connections (write-time filter added in S25)
    cross_cleaned = conn.execute(
        "SELECT value FROM system_meta WHERE key = 'cross_project_connections_cleaned'"
    ).fetchone()
    if not cross_cleaned:
        conn.execute("""
            DELETE FROM connections WHERE EXISTS (
                SELECT 1 FROM buffer_entries a, buffer_entries b
                WHERE connections.entry_a = a.id AND connections.entry_b = b.id
                AND a.project IS NOT NULL AND b.project IS NOT NULL
                AND a.project != b.project
            )
        """)
        conn.execute(
            "INSERT INTO system_meta VALUES ('cross_project_connections_cleaned', '1')"
        )
        conn.commit()

    # Phase 3 migration: routing_decisions
    if 'routing_decisions' not in tables:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS routing_decisions (
                entry_id INTEGER PRIMARY KEY REFERENCES buffer_entries(id),
                target_system TEXT NOT NULL
                    CHECK(target_system IN ('hook','rules','claude-mem','research','claude-md')),
                target_path TEXT,
                action TEXT
                    CHECK(action IS NULL OR action IN ('CREATE','UPDATE','REPLACE')),
                conflict_entries TEXT,
                resolution_text TEXT,
                routed_at TEXT NOT NULL,
                checked_at TEXT
            )
        """)
        conn.commit()

    return conn

# ─── Text utilities ──────────────────────────────────────────────

def normalize_text(text):
    """Normalize for hashing: strip + collapse whitespace."""
    return ' '.join(text.strip().split())

def compute_hash(text):
    """SHA256 of normalized text."""
    return hashlib.sha256(normalize_text(text).encode('utf-8')).hexdigest()


def detect_entry_type(text):
    """Detect entry type from text content. Priority: auto > decision > user-gedanke > session-save > insight."""
    if text.strip().startswith('AUTO-SESSION-SAVE'):
        return 'auto-session-save'
    first_300 = text[:300]
    if '#decision' in first_300 or '#entscheidung' in first_300:
        return 'decision'
    if '#user-gedanke' in first_300:
        return 'user-gedanke'
    if '#session-save' in first_300:
        return 'session-save'
    return 'insight'


def token_set(text):
    """Normalize text to a set of lowercase word tokens."""
    return set(normalize_text(text).lower().split())


def jaccard_overlap(set_a, set_b):
    """Jaccard similarity: |A ∩ B| / |A ∪ B|."""
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def parse_json_response(text):
    """Parse JSON from Gemini response, handling markdown code blocks."""
    text = text.strip()
    if text.startswith('```'):
        lines = text.split('\n')
        start = 1
        end = len(lines) - 1 if lines[-1].strip() == '```' else len(lines)
        text = '\n'.join(lines[start:end]).strip()
    return json.loads(text)


def infer_project_path():
    """Infer project path from CWD if inside _RESEARCH/."""
    cwd = Path.cwd()
    parts = cwd.parts
    for i, part in enumerate(parts):
        if part == '_RESEARCH' and i + 1 < len(parts):
            return Path(*parts[:i+2])
    return None

# ─── ONNX Model ─────────────────────────────────────────────────

def load_model():
    """Lazy-load ONNX session + tokenizer."""
    global _session, _tokenizer
    if _session is not None:
        return True

    # Try quantized first, then regular
    model_path = MODEL_DIR / "model_quantized.onnx"
    if not model_path.exists():
        model_path = MODEL_DIR / "model.onnx"
    if not model_path.exists():
        print(f"FEHLER: Kein ONNX-Modell in {MODEL_DIR}", file=sys.stderr)
        print("Einrichten mit: memory-buffer.py setup-model", file=sys.stderr)
        return False

    try:
        import onnxruntime as ort
        from transformers import AutoTokenizer

        print(f"Lade Modell aus {model_path.name}...", file=sys.stderr)

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 2
        opts.intra_op_num_threads = 2
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        _session = ort.InferenceSession(
            str(model_path), opts,
            providers=['CPUExecutionProvider']
        )
        _tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR), fix_mistral_regex=True)
        print("Modell geladen.", file=sys.stderr)
        return True

    except ImportError as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        print("Installieren: pip install onnxruntime transformers", file=sys.stderr)
        return False
    except Exception as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        return False


def embed_texts(texts):
    """Batch-embed texts. Returns (N, dim) numpy array or None."""
    if not load_model():
        return None

    import numpy as np

    tokens = _tokenizer(
        texts, padding=True, truncation=True,
        max_length=512, return_tensors="np"
    )

    # Build input dict from what model expects
    input_names = {i.name for i in _session.get_inputs()}
    feed = {}
    if 'input_ids' in input_names:
        feed['input_ids'] = tokens['input_ids'].astype(np.int64)
    if 'attention_mask' in input_names:
        feed['attention_mask'] = tokens['attention_mask'].astype(np.int64)
    if 'token_type_ids' in input_names and 'token_type_ids' in tokens:
        feed['token_type_ids'] = tokens['token_type_ids'].astype(np.int64)

    outputs = _session.run(None, feed)

    # Mean pooling over token dimension
    token_embeddings = outputs[0]  # (batch, seq_len, hidden)
    mask = tokens['attention_mask'].astype(np.float32)
    mask_expanded = np.expand_dims(mask, axis=-1)  # (batch, seq_len, 1)
    sum_emb = np.sum(token_embeddings * mask_expanded, axis=1)
    sum_mask = np.clip(np.sum(mask_expanded, axis=1), 1e-9, None)
    embeddings = sum_emb / sum_mask

    # L2 normalize
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    return embeddings / np.clip(norms, 1e-9, None)


def embedding_to_blob(embedding):
    """Convert numpy vector to raw float32 bytes."""
    return embedding.astype('float32').tobytes()


def blob_to_embedding(blob):
    """Convert raw bytes back to numpy vector."""
    import numpy as np
    return np.frombuffer(blob, dtype=np.float32)

# ─── Gemini Resilient Wrapper ─────────────────────────────────────

def gemini_generate(prompt, model=None, response_mime_type="application/json"):
    """Resilient Gemini call with model fallback and key rotation."""
    import os

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("FEHLER: google-genai nicht installiert. pip install google-genai", file=sys.stderr)
        return None

    primary = model or ROUTING_MODEL
    fallback = FALLBACK_MODEL if primary != FALLBACK_MODEL else ROUTING_MODEL

    keys = [
        ('ROUTING', os.environ.get('GEMINI_API_KEY_ROUTING')),
        ('DEFAULT', os.environ.get('GEMINI_API_KEY')),
    ]
    keys = [(name, k) for name, k in keys if k]

    if not keys:
        print("FEHLER: Keine GEMINI_API_KEY* gesetzt.", file=sys.stderr)
        return None

    config = types.GenerateContentConfig(
        response_mime_type=response_mime_type
    ) if response_mime_type else None

    errors = []

    for attempt_model in [primary, fallback]:
        for key_name, key in keys:
            try:
                client = genai.Client(api_key=key)
                response = client.models.generate_content(
                    model=attempt_model,
                    contents=prompt,
                    config=config
                )
                return response.text
            except Exception as e:
                errors.append(f"{attempt_model}/{key_name}: {e}")

    # Total failure
    print("\n!!! GEMINI TOTALAUSFALL !!!", file=sys.stderr)
    for err in errors:
        print(f"  - {err}", file=sys.stderr)
    return None


# ─── Commands ────────────────────────────────────────────────────

def cmd_add(args):
    """Add text to buffer (fast, no model needed)."""
    if not args:
        print("Usage: memory-buffer.py add [--project NAME] <text>")
        sys.exit(1)

    # Parse --project parameter
    project = None
    if '--project' in args:
        idx = args.index('--project')
        if idx + 1 < len(args):
            project = args[idx + 1]
            args = args[:idx] + args[idx + 2:]

    text = ' '.join(args)
    text_hash = compute_hash(text)
    entry_type = detect_entry_type(text)

    # Auto-expire empty auto-session-saves
    initial_state = 'buffer'
    if entry_type == 'auto-session-save' and 'User Messages: 0' in text:
        initial_state = 'expired'

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO buffer_entries (text, text_hash, state, created_at, project, entry_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (text, text_hash, initial_state, datetime.now().isoformat(), project, entry_type)
        )
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        if initial_state == 'expired':
            print(f"EXPIRED:{new_id} (empty auto-session-save)")
        else:
            print(f"ADDED:{new_id} (type={entry_type})")
    except sqlite3.IntegrityError:
        conn.close()
        print("DUPLICATE")


def cmd_embed_pending(args):
    """Batch-embed all entries without embeddings, then track connections."""
    import numpy as np

    conn = get_db()

    # Get pending entries (no embedding yet)
    pending = conn.execute("""
        SELECT b.id, b.text, b.project FROM buffer_entries b
        LEFT JOIN entry_embeddings e ON b.id = e.entry_id
        WHERE e.entry_id IS NULL AND b.state != 'expired'
        ORDER BY b.id
    """).fetchall()

    if not pending:
        print("Keine pending Eintraege.")
        conn.close()
        return

    print(f"{len(pending)} Eintraege zu embedden...")

    # Get threshold
    threshold_row = conn.execute(
        "SELECT value FROM system_meta WHERE key = 'connection_threshold'"
    ).fetchone()
    threshold = float(threshold_row[0]) if threshold_row else CONNECTION_THRESHOLD

    # Batch embed
    texts = [text for _, text, _ in pending]
    ids = [id_ for id_, _, _ in pending]
    pending_projects = {id_: proj for id_, _, proj in pending}

    embeddings = embed_texts(texts)
    if embeddings is None:
        conn.close()
        sys.exit(1)

    # Load existing embeddings for connection tracking (exclude expired)
    existing = conn.execute(
        "SELECT e.entry_id, e.embedding, b.project FROM entry_embeddings e "
        "JOIN buffer_entries b ON e.entry_id = b.id "
        "WHERE b.state != 'expired'"
    ).fetchall()

    existing_ids = []
    existing_projects = {}
    existing_matrix = None
    if existing:
        existing_ids = [r[0] for r in existing]
        existing_projects = {r[0]: r[2] for r in existing}
        existing_vecs = [blob_to_embedding(r[1]) for r in existing]
        existing_matrix = np.stack(existing_vecs)  # (M, dim)

    now = datetime.now().isoformat()

    # Store embeddings
    for entry_id, embedding in zip(ids, embeddings):
        conn.execute(
            "INSERT INTO entry_embeddings (entry_id, embedding, model, dimensions, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (entry_id, embedding_to_blob(embedding), MODEL_NAME, EMBEDDING_DIM, now)
        )

    # Connections: new vs existing
    new_connections = 0

    if existing_matrix is not None and len(existing_matrix) > 0:
        for i, (entry_id, embedding) in enumerate(zip(ids, embeddings)):
            sims = existing_matrix @ embedding  # (M,) — both L2-normalized
            for j, sim in enumerate(sims):
                if sim >= threshold:
                    # Skip cross-project connections
                    proj_new = pending_projects.get(entry_id)
                    proj_ext = existing_projects.get(existing_ids[j])
                    if proj_new and proj_ext and proj_new != proj_ext:
                        continue
                    a, b = min(entry_id, existing_ids[j]), max(entry_id, existing_ids[j])
                    try:
                        conn.execute(
                            "INSERT INTO connections (entry_a, entry_b, similarity, created_at) "
                            "VALUES (?, ?, ?, ?)",
                            (a, b, float(sim), now)
                        )
                        new_connections += 1
                    except sqlite3.IntegrityError:
                        pass

    # Connections: new vs new
    if len(embeddings) > 1:
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                sim = float(np.dot(embeddings[i], embeddings[j]))
                if sim >= threshold:
                    # Skip cross-project connections
                    proj_i = pending_projects.get(ids[i])
                    proj_j = pending_projects.get(ids[j])
                    if proj_i and proj_j and proj_i != proj_j:
                        continue
                    a, b = min(ids[i], ids[j]), max(ids[i], ids[j])
                    try:
                        conn.execute(
                            "INSERT INTO connections (entry_a, entry_b, similarity, created_at) "
                            "VALUES (?, ?, ?, ?)",
                            (a, b, sim, now)
                        )
                        new_connections += 1
                    except sqlite3.IntegrityError:
                        pass

    conn.commit()
    conn.close()

    print(f"{len(ids)} embedded, {new_connections} Connections.")


def cmd_search(args):
    """Semantic search in buffer."""
    if not args:
        print("Usage: memory-buffer.py search <query>")
        sys.exit(1)

    import numpy as np

    query = ' '.join(args)

    query_emb = embed_texts([query])
    if query_emb is None:
        sys.exit(1)
    query_vec = query_emb[0]

    conn = get_db()

    rows = conn.execute("""
        SELECT e.entry_id, e.embedding, b.text, b.state, b.created_at
        FROM entry_embeddings e
        JOIN buffer_entries b ON e.entry_id = b.id
        WHERE b.state != 'expired'
    """).fetchall()

    if not rows:
        print("Keine Embeddings vorhanden. Erst 'embed-pending' ausfuehren.")
        conn.close()
        return

    # Compute similarities
    results = []
    for entry_id, emb_blob, text, state, created in rows:
        vec = blob_to_embedding(emb_blob)
        sim = float(np.dot(query_vec, vec))
        results.append((entry_id, sim, text, state, created))

    results.sort(key=lambda x: x[1], reverse=True)

    # Track recalls for top results
    top_ids = [r[0] for r in results[:5]]
    now = datetime.now().isoformat()
    for eid in top_ids:
        conn.execute(
            "UPDATE buffer_entries SET recall_count = recall_count + 1, "
            "last_recalled_at = ? WHERE id = ?",
            (now, eid)
        )
    conn.commit()

    # Recall-based promotion (second promotion path from design)
    promoted_ids = []
    for eid in top_ids:
        row = conn.execute(
            "SELECT recall_count, state FROM buffer_entries WHERE id = ?",
            (eid,)
        ).fetchone()
        if row and row[1] == 'buffer' and row[0] >= RECALL_PROMOTE_THRESHOLD:
            conn.execute(
                "UPDATE buffer_entries SET state = 'proven' WHERE id = ?",
                (eid,)
            )
            promoted_ids.append(eid)
    if promoted_ids:
        conn.commit()
        for eid in promoted_ids:
            print(f"  ** [{eid}] promoted to proven (recall >= {RECALL_PROMOTE_THRESHOLD}) **")
        print()

    # Text fallback for non-embedded entries
    non_embedded = conn.execute("""
        SELECT b.id, b.text, b.state, b.created_at
        FROM buffer_entries b
        LEFT JOIN entry_embeddings e ON b.id = e.entry_id
        WHERE e.entry_id IS NULL AND b.state != 'expired'
    """).fetchall()

    conn.close()

    print(f"=== TREFFER fuer '{query}' ===\n")
    for i, (eid, sim, text, state, created) in enumerate(results[:5]):
        print(f"[{i+1}] ID:{eid} Score:{sim:.3f} [{state}] ({created[:10]})")
        preview = text[:300].replace('\n', '\n    ')
        print(f"    {preview}")
        if len(text) > 300:
            print("    ...")
        print()

    if non_embedded:
        query_lower = query.lower()
        text_matches = [(id_, text) for id_, text, state, created
                        in non_embedded if query_lower in text.lower()]
        if text_matches:
            print(f"--- {len(text_matches)} Text-Treffer (nicht embedded) ---\n")
            for id_, text in text_matches[:3]:
                print(f"  ID:{id_} {text[:200]}")
                print()


def cmd_get(args):
    """Get entry by ID."""
    if not args:
        print("Usage: memory-buffer.py get <id>")
        sys.exit(1)

    try:
        entry_id = int(args[0])
    except ValueError:
        print("FEHLER: ID muss eine Zahl sein")
        sys.exit(1)

    conn = get_db()
    row = conn.execute(
        "SELECT id, text, state, created_at, recall_count, last_recalled_at "
        "FROM buffer_entries WHERE id = ?",
        (entry_id,)
    ).fetchone()

    if not row:
        print(f"Eintrag {entry_id} nicht gefunden.")
        conn.close()
        sys.exit(1)

    id_, text, state, created, recalls, last_recalled = row

    has_emb = conn.execute(
        "SELECT 1 FROM entry_embeddings WHERE entry_id = ?", (entry_id,)
    ).fetchone() is not None

    conn_count = conn.execute(
        "SELECT COUNT(*) FROM connections WHERE entry_a = ? OR entry_b = ?",
        (entry_id, entry_id)
    ).fetchone()[0]

    conn.close()

    print(f"ID: {id_}")
    print(f"State: {state}")
    print(f"Erstellt: {created}")
    print(f"Recalls: {recalls}")
    if last_recalled:
        print(f"Letzter Recall: {last_recalled}")
    print(f"Embedding: {'ja' if has_emb else 'pending'}")
    print(f"Connections: {conn_count}")
    print(f"---")
    print(text)


def cmd_connections(args):
    """Show connections for an entry."""
    if not args:
        print("Usage: memory-buffer.py connections <id>")
        sys.exit(1)

    try:
        entry_id = int(args[0])
    except ValueError:
        print("FEHLER: ID muss eine Zahl sein")
        sys.exit(1)

    conn = get_db()

    rows = conn.execute("""
        SELECT
            CASE WHEN c.entry_a = ? THEN c.entry_b ELSE c.entry_a END as other_id,
            c.similarity,
            b.text,
            b.state
        FROM connections c
        JOIN buffer_entries b ON b.id =
            CASE WHEN c.entry_a = ? THEN c.entry_b ELSE c.entry_a END
        WHERE c.entry_a = ? OR c.entry_b = ?
        ORDER BY c.similarity DESC
    """, (entry_id, entry_id, entry_id, entry_id)).fetchall()

    conn.close()

    if not rows:
        print(f"Keine Connections fuer Eintrag {entry_id}.")
        return

    print(f"=== {len(rows)} CONNECTIONS fuer ID:{entry_id} ===\n")
    for other_id, sim, text, state in rows:
        preview = text[:100].replace('\n', ' ')
        print(f"  -> ID:{other_id} Sim:{sim:.3f} [{state}] {preview}")


def cmd_status(args):
    """Show buffer statistics."""
    conn = get_db()

    total = conn.execute("SELECT COUNT(*) FROM buffer_entries").fetchone()[0]
    by_state = conn.execute(
        "SELECT state, COUNT(*) FROM buffer_entries GROUP BY state"
    ).fetchall()
    embedded = conn.execute("SELECT COUNT(*) FROM entry_embeddings").fetchone()[0]
    pending = total - embedded
    conn_count = conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0]

    avg_conn = 0.0
    if embedded > 0:
        avg_conn = (conn_count * 2) / embedded

    threshold_row = conn.execute(
        "SELECT value FROM system_meta WHERE key = 'connection_threshold'"
    ).fetchone()

    # Most connected entries
    top_connected = conn.execute("""
        SELECT entry_id, COUNT(*) as cnt
        FROM (
            SELECT entry_a as entry_id FROM connections
            UNION ALL
            SELECT entry_b as entry_id FROM connections
        )
        GROUP BY entry_id
        ORDER BY cnt DESC
        LIMIT 5
    """).fetchall()

    conn.close()

    print("=== BUFFER STATUS ===\n")
    print(f"Eintraege: {total}")
    for state, count in by_state:
        print(f"  {state}: {count}")
    print(f"Embedded: {embedded}/{total} ({pending} pending)")
    print(f"Connections: {conn_count}")
    print(f"Avg Connections/Eintrag: {avg_conn:.1f}")
    print(f"Threshold: {threshold_row[0] if threshold_row else CONNECTION_THRESHOLD}")

    if top_connected:
        print(f"\nMeist verbunden:")
        for eid, cnt in top_connected:
            print(f"  ID:{eid} -> {cnt} Connections")

    print(f"\nDB: {BUFFER_DB_PATH}")
    print(f"Modell: {MODEL_DIR}")


def cmd_setup_model(args):
    """Provide instructions or auto-setup for ONNX model."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    if (MODEL_DIR / "model_quantized.onnx").exists():
        print(f"Quantisiertes Modell vorhanden: {MODEL_DIR / 'model_quantized.onnx'}")
        return
    if (MODEL_DIR / "model.onnx").exists():
        print(f"ONNX-Modell vorhanden: {MODEL_DIR / 'model.onnx'}")
        print("Fuer INT8-Quantisierung: memory-buffer.py quantize-model")
        return

    print("=== ONNX Modell Setup ===\n")
    print("Schritt 1 — Dependencies:")
    print("  pip install onnxruntime transformers optimum")
    print()
    print("Schritt 2 — Export zu ONNX:")
    print(f"  python -m optimum.exporters.onnx \\")
    print(f"    --model Qwen/Qwen3-Embedding-0.6B \\")
    print(f"    --task feature-extraction \\")
    print(f"    \"{MODEL_DIR}\"")
    print()
    print("Schritt 3 — INT8 Quantisierung (optional, spart ~75% RAM):")
    print(f"  python -c \"from onnxruntime.quantization import quantize_dynamic, QuantType; \\")
    print(f"    quantize_dynamic('{MODEL_DIR}/model.onnx', \\")
    print(f"    '{MODEL_DIR}/model_quantized.onnx', weight_type=QuantType.QInt8)\"")
    print()
    print(f"Modell-Verzeichnis: {MODEL_DIR}")


# ─── Phase 2: Cluster + Noise-Filter + Konsolidierung ─────────

def find_clusters(conn, threshold=None):
    """Find connected components in similarity graph above threshold."""
    if threshold is None:
        threshold = CLUSTER_THRESHOLD

    edges = conn.execute(
        "SELECT c.entry_a, c.entry_b FROM connections c "
        "JOIN buffer_entries ba ON c.entry_a = ba.id "
        "JOIN buffer_entries bb ON c.entry_b = bb.id "
        "WHERE c.similarity >= ? AND ba.state != 'expired' AND bb.state != 'expired' "
        "AND (ba.project IS NULL OR bb.project IS NULL OR ba.project = bb.project)",
        (threshold,)
    ).fetchall()

    # Build adjacency list
    adj = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    # BFS to find connected components
    visited = set()
    clusters = []
    for node in adj:
        if node in visited:
            continue
        cluster = []
        queue = [node]
        while queue:
            n = queue.pop(0)
            if n in visited:
                continue
            visited.add(n)
            cluster.append(n)
            for neighbor in adj.get(n, set()):
                if neighbor not in visited:
                    queue.append(neighbor)
        clusters.append(sorted(cluster))

    return clusters


def cmd_clusters(args):
    """Detect and display clusters (>= 3 connected entries)."""
    threshold = CLUSTER_THRESHOLD
    if args:
        try:
            threshold = float(args[0])
        except ValueError:
            pass

    conn = get_db()
    clusters = find_clusters(conn, threshold)

    # Filter for size >= 3
    clusters = [c for c in clusters if len(c) >= 3]

    if not clusters:
        print(f"Keine Cluster (>= 3) bei Threshold {threshold:.2f}.")
        conn.close()
        return

    print(f"=== {len(clusters)} CLUSTER (Threshold {threshold:.2f}) ===\n")

    for i, cluster_ids in enumerate(clusters):
        # Get texts and internal similarities
        entries = []
        for eid in cluster_ids:
            row = conn.execute(
                "SELECT text, state FROM buffer_entries WHERE id = ?", (eid,)
            ).fetchone()
            if row:
                entries.append((eid, row[0], row[1]))

        # Average internal similarity
        sims = []
        for j in range(len(cluster_ids)):
            for k in range(j + 1, len(cluster_ids)):
                a, b = min(cluster_ids[j], cluster_ids[k]), max(cluster_ids[j], cluster_ids[k])
                row = conn.execute(
                    "SELECT similarity FROM connections WHERE entry_a = ? AND entry_b = ?",
                    (a, b)
                ).fetchone()
                if row:
                    sims.append(row[0])

        avg_sim = sum(sims) / len(sims) if sims else 0.0

        print(f"--- Cluster {i+1}: {len(entries)} Eintraege, Avg Similarity: {avg_sim:.3f} ---")
        for eid, text, state in entries:
            preview = text[:120].replace('\n', ' ')
            print(f"  [{eid}] [{state}] {preview}")
        print()

    conn.close()


NOISE_OVERLAP_THRESHOLD = 0.80
CONSOLIDATION_MODEL = "gemini-2.5-flash"


def validate_cluster_coherence(cluster_ids, conn):
    """Check if all pairs in cluster have similarity >= CLUSTER_THRESHOLD.
    Returns (is_coherent, min_sim, missing_pairs)."""
    ids = list(cluster_ids)
    n = len(ids)
    placeholders = ','.join('?' * n)

    rows = conn.execute(f"""
        SELECT entry_a, entry_b, similarity FROM connections
        WHERE entry_a IN ({placeholders}) AND entry_b IN ({placeholders})
    """, ids + ids).fetchall()

    found = {}
    id_set = set(ids)
    for a, b, sim in rows:
        if a in id_set and b in id_set:
            found[(min(a, b), max(a, b))] = sim

    min_sim = 1.0
    missing = 0
    for i in range(n):
        for j in range(i + 1, n):
            pair = (min(ids[i], ids[j]), max(ids[i], ids[j]))
            if pair not in found:
                missing += 1
                min_sim = 0.0
            elif found[pair] < min_sim:
                min_sim = found[pair]

    is_coherent = missing == 0 and min_sim >= CLUSTER_THRESHOLD
    return is_coherent, min_sim, missing


def find_coherent_subcluster(entry_ids, conn):
    """Find largest coherent sub-cluster by iteratively removing weakest-linked node."""
    ids = list(entry_ids)
    n = len(ids)

    # Load all pairwise similarities once
    placeholders = ','.join('?' * n)
    rows = conn.execute(f"""
        SELECT entry_a, entry_b, similarity FROM connections
        WHERE entry_a IN ({placeholders}) AND entry_b IN ({placeholders})
    """, ids + ids).fetchall()

    sim_map = {}
    id_set = set(ids)
    for a, b, sim in rows:
        if a in id_set and b in id_set:
            sim_map[(min(a, b), max(a, b))] = sim

    def check_coherent(subset):
        min_sim = 1.0
        for i in range(len(subset)):
            for j in range(i + 1, len(subset)):
                pair = (min(subset[i], subset[j]), max(subset[i], subset[j]))
                sim = sim_map.get(pair, 0.0)
                if sim < min_sim:
                    min_sim = sim
        return min_sim >= CLUSTER_THRESHOLD, min_sim

    while len(ids) >= 3:
        coherent, ms = check_coherent(ids)
        if coherent:
            return ids

        # Try removing each node, pick the one that maximizes min_sim
        best_removal = None
        best_min_sim = -1

        for candidate in ids:
            remaining = [x for x in ids if x != candidate]
            if len(remaining) < 3:
                continue
            _, ms = check_coherent(remaining)
            if ms > best_min_sim:
                best_min_sim = ms
                best_removal = candidate

        if best_removal is None:
            return None

        ids.remove(best_removal)

    return None


def cmd_classify_clusters(args):
    """Classify clusters as noise or knowledge, expire noise clusters."""
    dry_run = '--dry-run' in args

    conn = get_db()
    clusters = find_clusters(conn)
    clusters = [c for c in clusters if len(c) >= 3]

    if not clusters:
        print("Keine Cluster (>= 3) gefunden.")
        conn.close()
        return

    print(f"=== CLUSTER-KLASSIFIKATION ===\n")

    total_expired = 0
    knowledge_clusters = 0

    for i, cluster_ids in enumerate(clusters):
        # Load texts
        entries = []
        for eid in cluster_ids:
            row = conn.execute(
                "SELECT text, state FROM buffer_entries WHERE id = ?", (eid,)
            ).fetchone()
            if row:
                entries.append((eid, row[0], row[1]))

        # Compute pairwise token overlap (Jaccard)
        sets = [(eid, token_set(text)) for eid, text, state in entries]
        overlaps = []
        for j in range(len(sets)):
            for k in range(j + 1, len(sets)):
                overlaps.append(jaccard_overlap(sets[j][1], sets[k][1]))

        avg_overlap = sum(overlaps) / len(overlaps) if overlaps else 0.0

        if avg_overlap > NOISE_OVERLAP_THRESHOLD:
            # Noise cluster
            label = "NOISE"
            if dry_run:
                action = f"  → {len(entries)} Eintraege WUERDEN expired (--dry-run)"
            else:
                for eid, text, state in entries:
                    if state != 'expired':
                        conn.execute(
                            "UPDATE buffer_entries SET state = 'expired' WHERE id = ?",
                            (eid,)
                        )
                        total_expired += 1
                action = f"  → {len(entries)} Eintraege expired"
        else:
            # Knowledge cluster
            label = "WISSEN"
            knowledge_clusters += 1
            action = f"  → bereit fuer Konsolidierung"

        print(f"Cluster {i+1}: {label} ({len(entries)} Eintraege, Token-Overlap: {avg_overlap:.2f})")
        for eid, text, state in entries:
            preview = text[:100].replace('\n', ' ')
            print(f"  [{eid}] [{state}] {preview}")
        print(action)
        print()

    if not dry_run:
        conn.commit()

    conn.close()

    print(f"--- Zusammenfassung ---")
    print(f"Noise expired: {total_expired}")
    print(f"Wissens-Cluster: {knowledge_clusters}")


def validate_consolidation(results, entries):
    """Validate LLM consolidation output (list of strings). Returns (is_valid, reason)."""
    if not results:
        return False, "Leerer Output"

    refusal_patterns = [
        "i can't", "i cannot", "i'm sorry", "i am sorry",
        "as an ai", "i'm not able", "i am not able",
        "ich kann nicht", "es tut mir leid", "als ki",
    ]

    for i, result in enumerate(results):
        if not result or not result.strip():
            return False, f"Eintrag {i+1}: Leerer Text"
        if len(result.strip()) < 50:
            return False, f"Eintrag {i+1}: Zu kurz ({len(result)} Zeichen)"
        result_lower = result.lower()
        for pattern in refusal_patterns:
            if pattern in result_lower:
                return False, f"LLM-Verweigerung erkannt: '{pattern}'"

    total_result_len = sum(len(r) for r in results)
    total_input_len = sum(len(text) for _, text in entries)
    if total_result_len > total_input_len * 1.5:
        return False, f"Output laenger als Input ({total_result_len} > {total_input_len})"

    return True, "OK"


def consolidate_cluster(entries):
    """Send cluster entries to Gemini 2.5 Flash for consolidation.
    Returns list of consolidated text strings, or None on error."""
    entries_text = "\n\n---\n\n".join(
        f"[Eintrag {eid}]\n{text}" for eid, text in entries
    )

    prompt = (
        f"Konsolidiere diese {len(entries)} verwandten Memory-Eintraege.\n\n"
        f"Regeln:\n"
        f"- Behalte ALLE Fakten und Erkenntnisse\n"
        f"- Eliminiere Redundanz und Wiederholungen\n"
        f"- Schreibe kompakt aber vollstaendig\n"
        f"- Keine Meta-Kommentare (\"dieser Eintrag fasst zusammen...\")\n"
        f"- Sprache: wie die Original-Eintraege\n"
        f"- Beginne direkt mit dem Inhalt\n"
        f"- WICHTIG: Wenn die Eintraege VERSCHIEDENE Themen behandeln, "
        f"erstelle SEPARATE Eintraege pro Thema\n\n"
        f"Eintraege:\n\n{entries_text}\n\n"
        f"Antworte mit JSON-Array von Strings. Jedes Element ist ein konsolidierter Text. "
        f"Beispiel: [\"Konsolidierter Text hier...\"]. "
        f"Wenn alle zum selben Thema: ein Element. Mehrere Themen: mehrere Strings."
    )

    response_text = gemini_generate(prompt, model=CONSOLIDATION_MODEL)
    if response_text is None:
        return None

    try:
        result = parse_json_response(response_text)

        if isinstance(result, list):
            texts = []
            for item in result:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, dict) and 'text' in item:
                    texts.append(item['text'])
                elif isinstance(item, dict):
                    # Fallback: flatten dict values to text
                    parts = []
                    for v in item.values():
                        if isinstance(v, str):
                            parts.append(v)
                        elif isinstance(v, dict):
                            parts.extend(str(sv) for sv in v.values() if isinstance(sv, str))
                    if parts:
                        texts.append(' '.join(parts))
            return texts if texts else None
        elif isinstance(result, dict) and 'text' in result:
            return [result['text']]
        else:
            print(f"FEHLER: Unerwartetes Format: {type(result)}", file=sys.stderr)
            return None

    except json.JSONDecodeError as e:
        print(f"FEHLER: JSON-Parse fehlgeschlagen: {e}", file=sys.stderr)
        print(f"Response: {response.text[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        return None


def cmd_consolidate(args):
    """Consolidate knowledge clusters via Gemini 2.5 Flash."""
    dry_run = '--dry-run' in args

    conn = get_db()
    clusters = find_clusters(conn)
    clusters = [c for c in clusters if len(c) >= 3]

    if not clusters:
        print("Keine Cluster (>= 3) gefunden.")
        conn.close()
        return

    print("=== KONSOLIDIERUNG ===\n")

    consolidated_count = 0

    for i, cluster_ids in enumerate(clusters):
        # Load non-expired entries
        entries = []
        entry_projects = {}
        for eid in cluster_ids:
            row = conn.execute(
                "SELECT text, state, project FROM buffer_entries WHERE id = ?", (eid,)
            ).fetchone()
            if row and row[1] != 'expired':
                entries.append((eid, row[0]))
                entry_projects[eid] = row[2]

        if len(entries) < 3:
            continue

        # Skip noise clusters
        sets = [(eid, token_set(text)) for eid, text in entries]
        overlaps = []
        for j in range(len(sets)):
            for k in range(j + 1, len(sets)):
                overlaps.append(jaccard_overlap(sets[j][1], sets[k][1]))
        avg_overlap = sum(overlaps) / len(overlaps) if overlaps else 0.0

        if avg_overlap > NOISE_OVERLAP_THRESHOLD:
            print(f"Cluster {i+1}: NOISE (uebersprungen)\n")
            continue

        # Coherence check: all pairs must have sim >= CLUSTER_THRESHOLD
        entry_ids = [eid for eid, _ in entries]
        is_coherent, min_sim, missing = validate_cluster_coherence(entry_ids, conn)
        if not is_coherent:
            # Try sub-cluster extraction
            coherent_ids = find_coherent_subcluster(entry_ids, conn)
            if coherent_ids and len(coherent_ids) >= 3:
                removed = set(entry_ids) - set(coherent_ids)
                entries = [(eid, text) for eid, text in entries if eid in coherent_ids]
                entry_ids = coherent_ids
                _, min_sim, _ = validate_cluster_coherence(entry_ids, conn)
                print(f"Cluster {i+1}: SUB-CLUSTER ({len(entries)} Eintraege, "
                      f"entfernt: {removed}, min_sim: {min_sim:.3f})")
            else:
                print(f"Cluster {i+1}: INKOHERENT ({len(entries)} Eintraege, "
                      f"min_sim={min_sim:.3f}, {missing} fehlende Paare) -> uebersprungen\n")
                continue

        print(f"Cluster {i+1}: WISSEN ({len(entries)} Eintraege, "
              f"Token-Overlap: {avg_overlap:.2f}, min_sim: {min_sim:.3f})")
        for eid, text in entries:
            preview = text[:100].replace('\n', ' ')
            print(f"  [{eid}] {preview}")

        if dry_run:
            print(f"  -> WUERDE konsolidiert (--dry-run)\n")
            continue

        # Call Gemini
        print(f"  -> Konsolidiere via Gemini...")
        results = consolidate_cluster(entries)
        if results is None:
            print(f"  -> FEHLER: Konsolidierung fehlgeschlagen\n")
            continue

        # Validate output
        is_valid, reason = validate_consolidation(results, entries)
        if not is_valid:
            print(f"  -> FEHLER: Validierung fehlgeschlagen: {reason}\n")
            continue

        # Determine project from cluster entries (unanimous non-NULL or None)
        projects = {entry_projects[eid] for eid, _ in entries if entry_projects.get(eid)}
        cluster_project = projects.pop() if len(projects) == 1 else None

        # Store consolidated entries as proven
        now = datetime.now().isoformat()
        new_ids = []
        all_inserted = True
        for result_text in results:
            text_hash = compute_hash(result_text)
            try:
                conn.execute(
                    "INSERT INTO buffer_entries (text, text_hash, state, created_at, project, entry_type) "
                    "VALUES (?, ?, 'proven', ?, ?, ?)",
                    (result_text, text_hash, now, cluster_project, detect_entry_type(result_text))
                )
                new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                new_ids.append(new_id)
            except sqlite3.IntegrityError:
                print(f"  -> Duplikat uebersprungen: {result_text[:80]}")
                all_inserted = False

        if not new_ids:
            print(f"  -> Alle Eintraege waren Duplikate\n")
            continue

        # Expire originals
        for eid, text in entries:
            conn.execute(
                "UPDATE buffer_entries SET state = 'expired' WHERE id = ?",
                (eid,)
            )

        conn.commit()
        consolidated_count += 1

        for new_id in new_ids:
            row = conn.execute("SELECT text FROM buffer_entries WHERE id = ?", (new_id,)).fetchone()
            preview = row[0][:200].replace('\n', ' ') if row else ''
            print(f"  -> Neuer Eintrag ID:{new_id} [proven]")
            print(f"     {preview}")
        if len(results) > 1:
            print(f"  -> {len(results)} Eintraege aus Multi-Topic-Split")
        print()

    conn.close()

    print(f"--- Zusammenfassung ---")
    print(f"Konsolidiert: {consolidated_count} Cluster")


# ─── Phase 3: Routing + Conflict-Check + Write ───────────────────

def fetch_target_content(target_system, entry_text, project_path=None):
    """Fetch existing content from target system for conflict checking."""
    import subprocess

    if target_system == 'hook':
        hooks_dir = Path.home() / ".claude" / "hooks"
        content = []
        if hooks_dir.exists():
            for f in sorted(hooks_dir.glob("*.py")):
                try:
                    text = f.read_text(encoding='utf-8', errors='replace')
                    lines = text.split('\n')[:30]
                    content.append(f"--- {f.name} ---\n" + '\n'.join(lines))
                except Exception:
                    pass
        return content

    elif target_system == 'rules':
        rules_dir = Path.home() / ".claude" / "rules"
        content = []
        if rules_dir.exists():
            for f in sorted(rules_dir.glob("*.md")):
                try:
                    text = f.read_text(encoding='utf-8', errors='replace')
                    content.append(f"--- {f.name} ---\n{text}")
                except Exception:
                    pass
        return content

    elif target_system == 'claude-mem':
        claude_mem = Path.home() / ".claude" / "scripts" / "claude-mem.py"
        if claude_mem.exists():
            words = entry_text.split()[:10]
            query = ' '.join(w for w in words if len(w) > 3)[:100]
            try:
                result = subprocess.run(
                    [sys.executable, str(claude_mem), "search", query],
                    capture_output=True, text=True, timeout=10,
                    encoding='utf-8', errors='replace'
                )
                if result.stdout.strip():
                    return [result.stdout.strip()]
            except Exception:
                pass
        return []

    elif target_system == 'research':
        content = []
        if project_path:
            p = Path(project_path)
            summary = p / "SUMMARY.md"
            if summary.exists():
                try:
                    text = summary.read_text(encoding='utf-8', errors='replace')
                    content.append(f"--- SUMMARY.md ---\n{text}")
                except Exception:
                    pass
            research_dir = p / "research"
            if research_dir.exists():
                for f in sorted(research_dir.glob("*.md")):
                    try:
                        text = f.read_text(encoding='utf-8', errors='replace')
                        lines = text.split('\n')[:20]
                        content.append(f"--- research/{f.name} ---\n" + '\n'.join(lines))
                    except Exception:
                        pass
        return content

    elif target_system == 'claude-md':
        content = []
        global_cmd = Path.home() / "CLAUDE.md"
        if global_cmd.exists():
            try:
                text = global_cmd.read_text(encoding='utf-8', errors='replace')
                content.append(f"--- ~/CLAUDE.md ---\n{text}")
            except Exception:
                pass
        if project_path:
            proj_cmd = Path(project_path) / "CLAUDE.md"
            if proj_cmd.exists():
                try:
                    text = proj_cmd.read_text(encoding='utf-8', errors='replace')
                    content.append(f"--- Projekt CLAUDE.md ---\n{text}")
                except Exception:
                    pass
        return content

    return []


def route_entry(entry_id, entry_text):
    """Route a proven entry to its target system via Gemini."""
    prompt = (
        "Klassifiziere diesen Memory-Eintrag fuer das Routing.\n\n"
        "Routing-Matrix:\n"
        "| Typ | Global | Projekt-spezifisch |\n"
        "|-----|--------|-------------------|\n"
        "| Imperativ (erzwingbar per bash/grep) | hook | claude-md |\n"
        "| Imperativ (Leitlinie/Regel) | rules | claude-md |\n"
        "| Deklarativ (Wissen/Fakten) | claude-mem | research |\n\n"
        "Kriterien:\n"
        "- WICHTIG: Was IST der Eintrag, nicht was ERWAEHNT er. "
        "Ein Eintrag der Hooks beschreibt/dokumentiert ist DEKLARATIV (Wissen ueber Hooks). "
        "Nur ein Eintrag der SELBST eine erzwingbare Regel formuliert ist IMPERATIV.\n"
        "- Session-Zusammenfassungen, Fortschrittsberichte, Implementierungs-Dokumentation = immer DEKLARATIV\n"
        "- hook vs rules: Kann die Regel per bash grep auf einen Command-String pruefen? Ja=hook, Nein=rules\n"
        "- Global vs Projekt: Gilt fuer ALLE Projekte? Global. Nur fuer ein bestimmtes? Projekt-spezifisch.\n"
        "- Imperativ vs Deklarativ: Steuert es Verhalten/Workflow? Imperativ. Ist es Wissen/Fakten? Deklarativ.\n\n"
        f"Eintrag:\n{entry_text}\n\n"
        "Antworte NUR mit einem JSON-Objekt:\n"
        '{"target_system": "hook|rules|claude-mem|research|claude-md", "reasoning": "kurze Begruendung"}\n'
    )

    response_text = gemini_generate(prompt)
    if response_text is None:
        return None

    try:
        result = parse_json_response(response_text)

        valid_targets = {'hook', 'rules', 'claude-mem', 'research', 'claude-md'}
        if result.get('target_system') not in valid_targets:
            print(f"FEHLER: Ungueltiges target_system: {result.get('target_system')}", file=sys.stderr)
            return None

        return result

    except json.JSONDecodeError as e:
        print(f"FEHLER: JSON-Parse fehlgeschlagen: {e}", file=sys.stderr)
        print(f"Response: {response.text[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        return None


def conflict_check_entry(entry_text, target_system, existing_content):
    """Check for conflicts with existing content via Gemini."""
    existing_text = "\n\n".join(existing_content) if existing_content else "(leer — nichts vorhanden)"

    prompt = (
        f"Pruefe ob dieser neue Eintrag mit dem bestehenden Inhalt im Ziel-System '{target_system}' "
        f"in Konflikt steht oder bereits vorhanden ist.\n\n"
        f"Neuer Eintrag:\n{entry_text}\n\n"
        f"Bestehender Inhalt im Ziel-System:\n{existing_text}\n\n"
        f"Bestimme:\n"
        f"1. action: CREATE (nichts Aehnliches vorhanden), UPDATE (Aehnliches vorhanden, ergaenzen), "
        f"oder REPLACE (Veraltetes vorhanden, ersetzen)\n"
        f"2. conflict_entries: Was genau im Ziel-System ist betroffen? (kurz)\n"
        f"3. resolution_text: Der fertige Text der geschrieben werden soll.\n"
        f"   - Bei CREATE: der vollstaendige neue Inhalt\n"
        f"   - Bei UPDATE: NUR die neue Sektion zum Anhaengen\n"
        f"   - Bei REPLACE: der Ersatz-Block (User reviewed manuell)\n\n"
        "Antworte mit JSON: "
        '{"action": "CREATE|UPDATE|REPLACE", "conflict_entries": "...", "resolution_text": "..."}\n'
    )

    response_text = gemini_generate(prompt)
    if response_text is None:
        return None

    try:
        result = parse_json_response(response_text)

        valid_actions = {'CREATE', 'UPDATE', 'REPLACE'}
        if result.get('action') not in valid_actions:
            print(f"FEHLER: Ungueltige action: {result.get('action')}", file=sys.stderr)
            return None

        return result

    except json.JSONDecodeError as e:
        print(f"FEHLER: JSON-Parse fehlgeschlagen: {e}", file=sys.stderr)
        print(f"Response: {response.text[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        return None


def cmd_route(args):
    """Route proven entries to their target systems via Gemini."""
    project_path, args = parse_project_arg(args)
    dry_run = '--dry-run' in args

    conn = get_db()

    pending = conn.execute("""
        SELECT b.id, b.text FROM buffer_entries b
        LEFT JOIN routing_decisions r ON b.id = r.entry_id
        WHERE b.state = 'proven' AND r.entry_id IS NULL
        ORDER BY b.id
    """).fetchall()

    if not pending:
        print("Keine proven Eintraege zum Routen.")
        conn.close()
        return

    if project_path:
        print(f"Projekt: {project_path}")
    print(f"=== ROUTING: {len(pending)} Eintraege ===\n")

    routed = 0
    for entry_id, text in pending:
        preview = text[:150].replace('\n', ' ')
        print(f"[{entry_id}] {preview}")

        if dry_run:
            print(f"  -> WUERDE geroutet (--dry-run)\n")
            continue

        result = route_entry(entry_id, text)
        if result is None:
            print(f"  -> FEHLER: Routing fehlgeschlagen\n")
            continue

        target = result['target_system']
        reasoning = result.get('reasoning', '')

        # Store project path for project-specific targets
        stored_path = str(project_path) if project_path and target in ('research', 'claude-md') else None

        conn.execute(
            "INSERT INTO routing_decisions (entry_id, target_system, target_path, routed_at) "
            "VALUES (?, ?, ?, ?)",
            (entry_id, target, stored_path, datetime.now().isoformat())
        )
        conn.commit()
        routed += 1

        print(f"  -> {target} ({reasoning})\n")

    conn.close()
    print(f"--- Geroutet: {routed}/{len(pending)} ---")


def parse_project_arg(args):
    """Extract --project PATH from args, return (project_path, remaining_args)."""
    remaining = []
    project_path = None
    i = 0
    while i < len(args):
        if args[i] == '--project' and i + 1 < len(args):
            project_path = Path(args[i + 1])
            i += 2
        else:
            remaining.append(args[i])
            i += 1
    if project_path is None:
        project_path = infer_project_path()
    return project_path, remaining


def cmd_conflict_check(args):
    """Check for conflicts between routed entries and target systems."""
    dry_run = '--dry-run' in args

    conn = get_db()

    pending = conn.execute("""
        SELECT r.entry_id, b.text, r.target_system, r.target_path
        FROM routing_decisions r
        JOIN buffer_entries b ON r.entry_id = b.id
        WHERE r.checked_at IS NULL
        ORDER BY r.entry_id
    """).fetchall()

    if not pending:
        print("Keine Eintraege zum Conflict-Check.")
        conn.close()
        return

    print(f"=== CONFLICT-CHECK: {len(pending)} Eintraege ===\n")

    checked = 0
    for entry_id, text, target_system, stored_path in pending:
        preview = text[:150].replace('\n', ' ')
        print(f"[{entry_id}] -> {target_system}")
        print(f"  {preview}")

        existing = fetch_target_content(target_system, text, stored_path)
        print(f"  Fetch: {len(existing)} Quellen aus {target_system}")
        if stored_path:
            print(f"  Projekt: {stored_path}")

        if dry_run:
            print(f"  -> WUERDE geprueft (--dry-run)\n")
            continue

        result = conflict_check_entry(text, target_system, existing)
        if result is None:
            print(f"  -> FEHLER: Conflict-Check fehlgeschlagen\n")
            continue

        action = result['action']
        conflict = result.get('conflict_entries', '')
        resolution = result.get('resolution_text', '')

        conn.execute("""
            UPDATE routing_decisions
            SET action = ?, conflict_entries = ?, resolution_text = ?,
                checked_at = ?
            WHERE entry_id = ?
        """, (action, conflict, resolution, datetime.now().isoformat(), entry_id))
        conn.commit()
        checked += 1

        print(f"  -> {action}: {conflict}")
        if resolution:
            res_preview = resolution[:200].replace('\n', ' ')
            print(f"  -> Text: {res_preview}")
        print()

    conn.close()
    print(f"--- Geprueft: {checked}/{len(pending)} ---")


def cmd_write_target(args):
    """Execute writes to target systems based on routing decisions."""
    import subprocess

    dry_run = '--dry-run' in args

    conn = get_db()

    pending = conn.execute("""
        SELECT r.entry_id, b.text, r.target_system, r.action,
               r.resolution_text, r.conflict_entries, r.target_path
        FROM routing_decisions r
        JOIN buffer_entries b ON r.entry_id = b.id
        WHERE r.checked_at IS NOT NULL AND b.state = 'proven'
        ORDER BY r.entry_id
    """).fetchall()

    if not pending:
        print("Keine Eintraege zum Schreiben.")
        conn.close()
        return

    print(f"=== WRITE-TARGET: {len(pending)} Eintraege ===\n")

    written = 0
    for entry_id, text, target_system, action, resolution_text, conflict_entries, stored_path in pending:
        preview = text[:100].replace('\n', ' ')
        print(f"[{entry_id}] {action} -> {target_system}")
        print(f"  {preview}")

        if not resolution_text:
            print(f"  -> SKIP: Kein resolution_text\n")
            continue

        # Auto-write: claude-mem CREATE
        if target_system == 'claude-mem' and action in ('CREATE', 'UPDATE'):
            if dry_run:
                print(f"  -> WUERDE zu claude-mem hinzugefuegt (--dry-run)\n")
                continue

            claude_mem = Path.home() / ".claude" / "scripts" / "claude-mem.py"
            try:
                result = subprocess.run(
                    [sys.executable, str(claude_mem), "add", resolution_text],
                    capture_output=True, text=True, timeout=60,
                    encoding='utf-8', errors='replace'
                )
                if result.returncode == 0:
                    conn.execute(
                        "UPDATE buffer_entries SET state = 'permanent' WHERE id = ?",
                        (entry_id,)
                    )
                    conn.commit()
                    written += 1
                    print(f"  -> GESCHRIEBEN: claude-mem add ({result.stdout.strip()})\n")
                else:
                    print(f"  -> FEHLER: {result.stderr.strip()}\n")
            except Exception as e:
                print(f"  -> FEHLER: {e}\n")

        # Auto-write: research CREATE/UPDATE
        elif target_system == 'research' and action in ('CREATE', 'UPDATE'):
            if not stored_path:
                print(f"  -> SKIP: Kein Projekt-Pfad in routing_decisions\n")
                continue

            proj = Path(stored_path)
            if action == 'CREATE':
                research_dir = proj / "research"
                research_dir.mkdir(exist_ok=True)
                existing = sorted(research_dir.glob("*.md"))
                next_num = len(existing) + 1
                words = [w.lower() for w in resolution_text.split()[:5] if w.isalnum() and len(w) > 2]
                slug = '-'.join(words)[:40] if words else 'entry'
                fname = f"{next_num:02d}-{slug}.md"
                target_file = research_dir / fname

                if dry_run:
                    print(f"  -> WUERDE erstellt: {target_file} (--dry-run)\n")
                    continue

                target_file.write_text(resolution_text, encoding='utf-8')
                conn.execute(
                    "UPDATE buffer_entries SET state = 'permanent' WHERE id = ?",
                    (entry_id,)
                )
                conn.commit()
                written += 1
                print(f"  -> GESCHRIEBEN: {target_file}\n")

            elif action == 'UPDATE':
                summary = proj / "SUMMARY.md"
                if not summary.exists():
                    print(f"  -> SKIP: {summary} nicht gefunden\n")
                    continue

                if dry_run:
                    print(f"  -> WUERDE angehaengt an: {summary} (--dry-run)\n")
                    continue

                with open(summary, 'a', encoding='utf-8') as f:
                    f.write(f"\n\n{resolution_text}")

                conn.execute(
                    "UPDATE buffer_entries SET state = 'permanent' WHERE id = ?",
                    (entry_id,)
                )
                conn.commit()
                written += 1
                print(f"  -> ANGEHAENGT an: {summary}\n")

        # User-review: hooks, rules, claude-md, REPLACE
        else:
            print(f"  -> VORSCHLAG (User-Review noetig):")
            print(f"  Action: {action}")
            if conflict_entries:
                print(f"  Betroffen: {conflict_entries}")
            print(f"  --- Resolution Text ---")
            for line in resolution_text.split('\n'):
                print(f"  {line}")
            print(f"  --- Ende ---\n")

    conn.close()
    print(f"--- Geschrieben: {written}/{len(pending)} ---")


# ─── Phase 4: Antizipation ────────────────────────────────────────

def _find_resume_context(project_name):
    """Auto-discover rich context from RESUME_PROMPT.md for better embedding search."""
    if not project_name:
        return None
    search_paths = [
        Path.cwd() / "_RESEARCH" / project_name / "RESUME_PROMPT.md",
        Path.home() / "_RESEARCH" / project_name / "RESUME_PROMPT.md",
    ]
    for p in search_paths:
        if p.exists():
            try:
                return p.read_text(encoding='utf-8')
            except Exception:
                return None
    return None


def cmd_briefing(args):
    """Context-aware briefing: show relevant buffer entries for current project."""
    import numpy as np

    quick = '--quick' in args
    all_projects = '--all-projects' in args
    args = [a for a in args if a not in ('--quick', '--all-projects')]

    project_path, args = parse_project_arg(args)
    project = None if all_projects else (project_path.name if project_path else None)
    if args:
        context = ' '.join(args)
    else:
        resume = _find_resume_context(project)
        context = resume if resume else project

    conn = get_db()

    # Get all non-expired entries with embeddings
    rows = conn.execute("""
        SELECT e.entry_id, e.embedding, b.text, b.state, b.project, b.created_at
        FROM entry_embeddings e
        JOIN buffer_entries b ON e.entry_id = b.id
        WHERE b.state != 'expired'
    """).fetchall()

    if not rows:
        print("Keine aktiven Entries im Buffer.")
        conn.close()
        return

    # Embedding search if context available
    if context:
        query_emb = embed_texts([context])
        if query_emb is not None:
            query_vec = query_emb[0]

            results = []
            for entry_id, emb_blob, text, state, proj, created in rows:
                # Project filter: match project, NULL, or no filter
                if project and proj and proj != project:
                    continue
                vec = blob_to_embedding(emb_blob)
                sim = float(np.dot(query_vec, vec))
                results.append((entry_id, sim, text, state, proj, created))

            results.sort(key=lambda x: x[1], reverse=True)
            results = [r for r in results if r[1] >= BRIEFING_MIN_SIM][:BRIEFING_MAX_ENTRIES]

            print(f"=== BRIEFING: {project or 'alle Projekte'} ===")
            ctx_display = context.replace('\n', ' ')[:80] + ("..." if len(context) > 80 else "")
            print(f"Kontext: \"{ctx_display}\"")
            print(f"Relevante Entries: {len(results)}\n")

            for eid, sim, text, state, proj, created in results:
                preview = text[:200].replace('\n', ' ')
                print(f"  [{eid}] {state} (sim={sim:.3f}) proj={proj or 'NULL'} ({created[:10]})")
                print(f"      {preview}")
                print()

            # Consistency check: detect contradictions among loaded entries
            if len(results) >= 2 and not quick:
                entries_for_check = "\n\n".join(
                    f"[{eid}] {text[:500]}"
                    for eid, sim, text, state, proj, created in results
                )
                consistency_prompt = (
                    "Pruefe diese Memory-Eintraege auf Widersprueche.\n\n"
                    "Eintraege:\n" + entries_for_check + "\n\n"
                    "Frage: Widersprechen sich zwei oder mehr Eintraege inhaltlich?\n"
                    "Beispiele fuer Widersprueche:\n"
                    "- Unterschiedliche Werte fuer dieselbe Konfiguration\n"
                    "- Gegensaetzliche Entscheidungen zum selben Thema\n"
                    "- Veraltete Fakten neben aktuellen\n\n"
                    "Antworte mit JSON:\n"
                    '{"has_conflicts": false, "conflicts": []}\n'
                    "oder\n"
                    '{"has_conflicts": true, "conflicts": ['
                    '{"entry_ids": [A, B], "description": "..."}]}\n'
                )
                try:
                    check_text = gemini_generate(
                        consistency_prompt, model=FALLBACK_MODEL
                    )
                    if check_text:
                        check = parse_json_response(check_text)
                        if check.get('has_conflicts'):
                            print("  !!! WIDERSPRUECHE ERKANNT !!!")
                            for conflict in check.get('conflicts', []):
                                ids = conflict.get('entry_ids', [])
                                desc = conflict.get('description', '')
                                print(f"  Entries {ids}: {desc}")
                            print()
                except Exception:
                    pass  # Non-critical, don't break briefing

            conn.close()
            return

    # SQL fallback: all non-expired entries for project
    if project:
        fallback = conn.execute(
            "SELECT id, text, state, project, created_at FROM buffer_entries "
            "WHERE state != 'expired' AND (project = ? OR project IS NULL) "
            "ORDER BY created_at DESC", (project,)
        ).fetchall()
    else:
        fallback = conn.execute(
            "SELECT id, text, state, project, created_at FROM buffer_entries "
            "WHERE state != 'expired' ORDER BY created_at DESC"
        ).fetchall()

    print(f"=== BRIEFING: {project or 'alle Projekte'} ===")
    print(f"Aktive Entries: {len(fallback)}\n")

    for eid, text, state, proj, created in fallback:
        preview = text[:200].replace('\n', ' ')
        print(f"  [{eid}] {state} | proj={proj or 'NULL'} | {created[:10]}")
        print(f"      {preview}")
        print()

    conn.close()


# ─── Phase 5: Diamond-Check ──────────────────────────────────────

def substance_check(entry_text):
    """Check if an isolated entry has substance worth preserving (Gemini)."""
    prompt = (
        "Bewerte diesen Memory-Eintrag.\n\n"
        "Frage: Enthaelt dieser Eintrag ein spezifisches, verwertbares technisches Learning "
        "oder eine konkrete Erkenntnis, die in zukuenftigen Sessions nuetzlich waere?\n\n"
        "JA-Kriterien:\n"
        "- Konkretes technisches Wissen (z.B. 'Library X braucht Config Y')\n"
        "- Spezifische Fehlerloesung (z.B. 'Bug in Z, Fix ist W')\n"
        "- Architektur-Entscheidung mit Begruendung\n"
        "- Workflow-Erkenntnis die wiederverwendbar ist\n\n"
        "NEIN-Kriterien:\n"
        "- Generische Session-Zusammenfassung ohne konkretes Wissen\n"
        "- Leere oder fast leere Eintraege\n"
        "- Reine Statusmeldungen ohne Substanz\n"
        "- Duplikate von offensichtlich bekanntem Wissen\n\n"
        f"Eintrag:\n{entry_text}\n\n"
        'Antworte NUR mit JSON: {"valuable": true, "reasoning": "kurze Begruendung"} '
        'oder {"valuable": false, "reasoning": "kurze Begruendung"}\n'
    )

    response_text = gemini_generate(prompt)
    if response_text is None:
        return None

    result = parse_json_response(response_text)
    return result


def cmd_diamond_check(args):
    """Check isolated buffer entries for diamonds (valuable singletons)."""
    import subprocess

    dry_run = '--dry-run' in args

    conn = get_db()

    # Step 0: Run embed-pending if there are pending entries
    pending_count = conn.execute(
        "SELECT COUNT(*) FROM buffer_entries b "
        "LEFT JOIN entry_embeddings e ON b.id = e.entry_id "
        "WHERE b.state = 'buffer' AND e.entry_id IS NULL"
    ).fetchone()[0]

    if pending_count > 0:
        print(f"Schritt 0: {pending_count} pending Entries -> embed-pending zuerst...\n")
        conn.close()
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "embed-pending"],
            timeout=120
        )
        conn = get_db()

    # Step 1: Find all clusters
    clusters = find_clusters(conn)
    big_clusters = [c for c in clusters if len(c) >= 3]

    # Collect all IDs that are in a cluster >= 3
    clustered_ids = set()
    for c in big_clusters:
        clustered_ids.update(c)

    # Step 2: Find buffer entries with embeddings NOT in any cluster >= 3
    all_buffer = conn.execute(
        "SELECT b.id, b.text, b.project FROM buffer_entries b "
        "JOIN entry_embeddings e ON b.id = e.entry_id "
        "WHERE b.state = 'buffer'"
    ).fetchall()

    candidates = [(eid, text, project) for eid, text, project in all_buffer
                  if eid not in clustered_ids]

    if not candidates:
        print("Keine isolierten Entries gefunden.")
        conn.close()
        return

    print(f"=== DIAMOND-CHECK: {len(candidates)} Kandidaten ===\n")

    promoted = 0
    expired = 0

    for eid, text, project in candidates:
        preview = text[:100].replace('\n', ' ')
        print(f"[{eid}] proj={project} {preview}")

        # Step 3a: Mechanical noise check
        if len(text.strip()) < 50:
            if dry_run:
                print(f"  -> WUERDE expired (< 50 Zeichen) [--dry-run]\n")
                continue
            conn.execute(
                "UPDATE buffer_entries SET state = 'expired' WHERE id = ?",
                (eid,)
            )
            conn.commit()
            expired += 1
            print(f"  -> EXPIRED (< 50 Zeichen)\n")
            continue

        # Step 3b: Gemini substance check
        if dry_run:
            print(f"  -> WUERDE Gemini Substance-Check ausfuehren [--dry-run]\n")
            continue

        result = substance_check(text)
        if result is None:
            print(f"  -> SKIP: Gemini-Fehler\n")
            continue

        valuable = result.get('valuable', False)
        reasoning = result.get('reasoning', '')

        if valuable:
            conn.execute(
                "UPDATE buffer_entries SET state = 'proven' WHERE id = ?",
                (eid,)
            )
            conn.commit()
            promoted += 1
            print(f"  -> PROMOTED to proven (Diamant)")
            print(f"     Grund: {reasoning}\n")
        else:
            conn.execute(
                "UPDATE buffer_entries SET state = 'expired' WHERE id = ?",
                (eid,)
            )
            conn.commit()
            expired += 1
            print(f"  -> EXPIRED")
            print(f"     Grund: {reasoning}\n")

    conn.close()

    print(f"--- Zusammenfassung ---")
    print(f"Promoted: {promoted}, Expired: {expired}")


CHROMA_DB_PATH = Path.home() / ".claude-mem" / "chroma" / "chroma.sqlite3"


def cmd_migrate(args):
    """Lazy Migration: alte claude-mem Entries in den Buffer uebernehmen."""
    if not args:
        print("Usage: memory-buffer.py migrate <id> [<id2> ...]")
        print("  Migriert alte claude-mem Eintraege in den Buffer.")
        print("  IDs sind claude-mem Dokument-IDs.")
        sys.exit(1)

    dry_run = '--dry-run' in args
    ids = []
    for a in args:
        if a == '--dry-run':
            continue
        try:
            ids.append(int(a))
        except ValueError:
            print(f"FEHLER: '{a}' ist keine gueltige ID")
            sys.exit(1)

    if not CHROMA_DB_PATH.exists():
        print(f"FEHLER: Alte claude-mem DB nicht gefunden: {CHROMA_DB_PATH}")
        sys.exit(1)

    old_conn = sqlite3.connect(str(CHROMA_DB_PATH))
    conn = get_db()

    migrated = 0
    skipped = 0

    for doc_id in ids:
        row = old_conn.execute(
            "SELECT em.string_value FROM embeddings e "
            "LEFT JOIN embedding_metadata em ON e.id = em.id "
            "AND em.key = 'chroma:document' WHERE e.id = ?",
            (doc_id,)
        ).fetchone()

        if not row or not row[0]:
            print(f"  [{doc_id}] nicht gefunden oder leer -> skip")
            skipped += 1
            continue

        text = row[0]
        preview = text[:100].replace('\n', ' ')

        # Dedup check
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        existing = conn.execute(
            "SELECT id FROM buffer_entries WHERE text_hash = ?",
            (text_hash,)
        ).fetchone()

        if existing:
            print(f"  [{doc_id}] bereits im Buffer (ID {existing[0]}) -> skip")
            skipped += 1
            continue

        if dry_run:
            print(f"  [{doc_id}] WUERDE migrieren: {preview} [--dry-run]")
            continue

        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO buffer_entries (text, text_hash, state, created_at, entry_type) "
            "VALUES (?, ?, 'buffer', ?, ?)",
            (text, text_hash, now, detect_entry_type(text))
        )
        conn.commit()
        migrated += 1
        print(f"  [{doc_id}] -> Buffer: {preview}")

    old_conn.close()
    conn.close()

    print(f"\n--- Zusammenfassung ---")
    print(f"Migriert: {migrated}, Uebersprungen: {skipped}")
    if migrated > 0:
        print("Tipp: 'embed-pending' ausfuehren fuer Embeddings + Connections.")


def cmd_age(args):
    """Verblassen: Check isolated buffer entries that aged past AGE_THRESHOLD."""
    import subprocess

    dry_run = '--dry-run' in args

    # Parse optional --threshold N
    threshold = AGE_THRESHOLD
    for i, arg in enumerate(args):
        if arg == '--threshold' and i + 1 < len(args):
            threshold = int(args[i + 1])

    conn = get_db()

    # Step 0: Run embed-pending if there are pending entries
    pending_count = conn.execute(
        "SELECT COUNT(*) FROM buffer_entries b "
        "LEFT JOIN entry_embeddings e ON b.id = e.entry_id "
        "WHERE b.state = 'buffer' AND e.entry_id IS NULL"
    ).fetchone()[0]

    if pending_count > 0:
        print(f"Schritt 0: {pending_count} pending Entries -> embed-pending zuerst...\n")
        conn.close()
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "embed-pending"],
            timeout=120
        )
        conn = get_db()

    # Step 1: Find buffer entries with >= threshold newer entries
    max_id = conn.execute("SELECT MAX(id) FROM buffer_entries").fetchone()[0] or 0

    candidates_raw = conn.execute(
        "SELECT id, text, project, reprieve_count FROM buffer_entries "
        "WHERE state = 'buffer' AND (? - id) >= ? "
        "AND (entry_type IS NULL OR entry_type NOT IN ('user-gedanke', 'decision'))",
        (max_id, threshold)
    ).fetchall()

    if not candidates_raw:
        print(f"Keine Buffer-Entries mit >= {threshold} neueren Entries gefunden.")
        conn.close()
        return

    print(f"=== AGE CHECK: {len(candidates_raw)} Entries mit >= {threshold} neuere ===\n")

    # Step 2: Find clusters to check isolation
    clusters = find_clusters(conn)
    big_clusters = [c for c in clusters if len(c) >= 3]
    clustered_ids = set()
    for c in big_clusters:
        clustered_ids.update(c)

    # Step 3: Filter to isolated entries only
    isolated = [(eid, text, project, reprieves)
                for eid, text, project, reprieves in candidates_raw
                if eid not in clustered_ids]

    non_isolated = len(candidates_raw) - len(isolated)
    if non_isolated > 0:
        print(f"  {non_isolated} in Clustern -> skip (normaler Pfad)\n")

    if not isolated:
        print("Keine isolierten gealterten Entries gefunden.")
        conn.close()
        return

    print(f"  {len(isolated)} isolierte Entries -> Pruefung\n")

    reprieved = 0
    expired = 0
    limbo_expired = 0

    for eid, text, project, reprieves in isolated:
        preview = text[:100].replace('\n', ' ')
        age = max_id - eid
        print(f"[{eid}] proj={project} reprieves={reprieves} alter={age} | {preview}")

        # Step 4a: Limbo-Schutz
        if reprieves >= MAX_REPRIEVES:
            if dry_run:
                print(f"  -> WUERDE expired (Limbo: {reprieves}/{MAX_REPRIEVES}) [--dry-run]\n")
                continue
            conn.execute(
                "UPDATE buffer_entries SET state = 'expired' WHERE id = ?",
                (eid,)
            )
            conn.commit()
            limbo_expired += 1
            print(f"  -> EXPIRED (Limbo: {reprieves}/{MAX_REPRIEVES})\n")
            continue

        # Step 4b: Mechanical noise check
        if len(text.strip()) < 50:
            if dry_run:
                print(f"  -> WUERDE expired (< 50 Zeichen) [--dry-run]\n")
                continue
            conn.execute(
                "UPDATE buffer_entries SET state = 'expired' WHERE id = ?",
                (eid,)
            )
            conn.commit()
            expired += 1
            print(f"  -> EXPIRED (< 50 Zeichen)\n")
            continue

        # Step 4c: Gemini substance check
        if dry_run:
            print(f"  -> WUERDE Gemini Substance-Check [--dry-run]\n")
            continue

        result = substance_check(text)
        if result is None:
            print(f"  -> SKIP: Gemini-Fehler\n")
            continue

        valuable = result.get('valuable', False)
        reasoning = result.get('reasoning', '')

        if valuable:
            conn.execute(
                "UPDATE buffer_entries SET reprieve_count = reprieve_count + 1 WHERE id = ?",
                (eid,)
            )
            conn.commit()
            reprieved += 1
            print(f"  -> REPRIEVE ({reprieves + 1}/{MAX_REPRIEVES})")
            print(f"     Grund: {reasoning}\n")
        else:
            conn.execute(
                "UPDATE buffer_entries SET state = 'expired' WHERE id = ?",
                (eid,)
            )
            conn.commit()
            expired += 1
            print(f"  -> EXPIRED (Verblassen)")
            print(f"     Grund: {reasoning}\n")

    conn.close()

    print(f"--- Zusammenfassung ---")
    print(f"Reprieved: {reprieved}, Expired: {expired}, Limbo-Expired: {limbo_expired}")


# ─── Main ────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print(f"\nDB:     {BUFFER_DB_PATH}")
        print(f"Modell: {MODEL_DIR}")
        sys.exit(0)

    command = sys.argv[1].lower()
    args = sys.argv[2:]

    commands = {
        'add': cmd_add,
        'embed-pending': cmd_embed_pending,
        'search': cmd_search,
        'get': cmd_get,
        'connections': cmd_connections,
        'status': cmd_status,
        'setup-model': cmd_setup_model,
        'clusters': cmd_clusters,
        'classify-clusters': cmd_classify_clusters,
        'consolidate': cmd_consolidate,
        'route': cmd_route,
        'conflict-check': cmd_conflict_check,
        'write-target': cmd_write_target,
        'briefing': cmd_briefing,
        'diamond-check': cmd_diamond_check,
        'age': cmd_age,
        'migrate': cmd_migrate,
    }

    if command in commands:
        commands[command](args)
    else:
        print(f"Unbekannter Befehl: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == '__main__':
    main()
