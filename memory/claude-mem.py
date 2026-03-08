#!/usr/bin/env python3
"""
claude-mem.py - Direkter Zugriff auf claude-mem Chroma-Datenbank
Mit eigener semantischer Suche via sentence-transformers

Usage:
    python claude-mem.py list                    # Alle Dokumente auflisten
    python claude-mem.py search <term>           # Nach Begriff suchen (Text)
    python claude-mem.py semantic <query>        # Semantische Suche (KI-basiert)
    python claude-mem.py get <id>                # Dokument per ID holen
    python claude-mem.py add <text>              # Neues Dokument hinzufuegen
    python claude-mem.py add-file <path>         # Dokument aus Datei hinzufuegen
    python claude-mem.py delete <id>             # Dokument loeschen
    python claude-mem.py count                   # Anzahl Dokumente
    python claude-mem.py startup                 # Kritische Learnings fuer Session-Start
    python claude-mem.py startup-compact         # Kompakte Version fuer Hooks
    python claude-mem.py session-init            # Alles fuer Session-Start (3-in-1, JSON)
    python claude-mem.py backup [path]           # Backup erstellen (JSON)
    python claude-mem.py restore <path>          # Backup wiederherstellen
    python claude-mem.py json                    # Als JSON ausgeben
    python claude-mem.py stats                   # Statistiken anzeigen
    python claude-mem.py embed-all               # Alle Dokumente embedden
    python claude-mem.py embed-status            # Embedding-Status anzeigen
"""

import sqlite3
import os
import sys
import json
import pickle
from datetime import datetime
from pathlib import Path

# UTF-8 Encoding fuer Windows Console erzwingen
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Konfiguration
CHROMA_DB_PATH = Path.home() / ".claude-mem" / "chroma" / "chroma.sqlite3"
EMBEDDINGS_DB_PATH = Path.home() / ".claude-mem" / "embeddings.sqlite3"
BACKUP_DIR = Path.home() / ".claude-mem" / "backups"
COLLECTION_NAME = "claude_memories"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # Schnelles, gutes Modell (384 dim)

# Lazy-Loading fuer sentence-transformers (spart Startzeit)
_model = None
SBERT_AVAILABLE = False

def get_embedding_model():
    """Lazy-load des Embedding-Modells"""
    global _model, SBERT_AVAILABLE
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            print(f"Lade Embedding-Modell '{EMBEDDING_MODEL}'...", file=sys.stderr)
            _model = SentenceTransformer(EMBEDDING_MODEL)
            SBERT_AVAILABLE = True
        except ImportError:
            print("FEHLER: sentence-transformers nicht installiert", file=sys.stderr)
            print("Installieren mit: pip install sentence-transformers", file=sys.stderr)
            return None
        except Exception as e:
            print(f"FEHLER beim Laden des Modells: {e}", file=sys.stderr)
            return None
    return _model


def get_connection():
    """Verbindung zur Chroma SQLite-Datenbank herstellen"""
    if not CHROMA_DB_PATH.exists():
        print(f"FEHLER: Datenbank nicht gefunden: {CHROMA_DB_PATH}")
        sys.exit(1)
    return sqlite3.connect(str(CHROMA_DB_PATH))


def get_embeddings_connection():
    """Verbindung zur Embeddings-Datenbank herstellen (eigene DB)"""
    conn = sqlite3.connect(str(EMBEDDINGS_DB_PATH))
    # Tabelle erstellen falls nicht vorhanden
    conn.execute('''
        CREATE TABLE IF NOT EXISTS document_embeddings (
            doc_id INTEGER PRIMARY KEY,
            embedding BLOB,
            model TEXT,
            created_at TEXT
        )
    ''')
    conn.commit()
    return conn


def get_collection_id(cursor):
    """Collection ID fuer claude_memories holen"""
    cursor.execute("SELECT id FROM collections WHERE name = ?", (COLLECTION_NAME,))
    result = cursor.fetchone()
    if not result:
        print(f"FEHLER: Collection '{COLLECTION_NAME}' nicht gefunden")
        sys.exit(1)
    return result[0]


def get_all_documents():
    """Alle Dokumente aus der Datenbank holen"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT e.id, e.embedding_id, em.string_value as document
        FROM embeddings e
        LEFT JOIN embedding_metadata em ON e.id = em.id AND em.key = 'chroma:document'
        ORDER BY e.id DESC
    ''')
    results = cursor.fetchall()
    conn.close()
    return results


def compute_embedding(text):
    """Embedding fuer einen Text berechnen"""
    model = get_embedding_model()
    if model is None:
        return None
    return model.encode(text, convert_to_numpy=True)


def cosine_similarity(a, b):
    """Cosine-Similarity zwischen zwei Vektoren"""
    import numpy as np
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def cmd_list(args):
    """Alle Dokumente auflisten"""
    results = get_all_documents()

    if not results:
        print("Keine Dokumente gefunden.")
        return

    print(f"=== {len(results)} DOKUMENTE ===\n")
    for doc_id, embedding_id, content in results:
        content = content or "(leer)"
        lines = content.split('\n')
        title = lines[0][:80]
        preview = content[:150].replace('\n', ' ')
        print(f"[{doc_id}] {title}")
        if len(content) > 80:
            print(f"    {preview}...")
        print()


def cmd_search(args):
    """Nach Begriff in Dokumenten suchen (Text-Suche)"""
    if not args:
        print("Usage: claude-mem.py search <term>")
        sys.exit(1)

    search_term = ' '.join(args).lower()
    results = get_all_documents()

    matches = []
    for doc_id, embedding_id, content in results:
        if content and search_term in content.lower():
            matches.append((doc_id, content))

    if not matches:
        print(f"Keine Treffer fuer: '{search_term}'")
        return

    print(f"=== {len(matches)} TREFFER fuer '{search_term}' ===\n")
    for doc_id, content in matches:
        print(f"--- ID: {doc_id} ---")
        print(content)
        print()


def cmd_semantic(args):
    """Semantische Suche mit sentence-transformers Embeddings"""
    if not args:
        print("Usage: claude-mem.py semantic <query>")
        sys.exit(1)

    query = ' '.join(args)

    # Query-Embedding berechnen
    query_embedding = compute_embedding(query)
    if query_embedding is None:
        print("FEHLER: Konnte Query nicht embedden")
        sys.exit(1)

    # Embeddings-DB oeffnen
    emb_conn = get_embeddings_connection()
    emb_cursor = emb_conn.cursor()
    emb_cursor.execute("SELECT doc_id, embedding FROM document_embeddings")
    stored_embeddings = emb_cursor.fetchall()
    emb_conn.close()

    if not stored_embeddings:
        print("Keine Embeddings vorhanden. Fuehre zuerst 'embed-all' aus.")
        sys.exit(1)

    # Alle Dokumente holen
    docs = get_all_documents()
    doc_dict = {doc_id: content for doc_id, _, content in docs}

    # Similarities berechnen
    results = []
    for doc_id, emb_blob in stored_embeddings:
        if doc_id not in doc_dict:
            continue
        doc_embedding = pickle.loads(emb_blob)
        similarity = cosine_similarity(query_embedding, doc_embedding)
        results.append((doc_id, similarity, doc_dict[doc_id]))

    # Nach Similarity sortieren
    results.sort(key=lambda x: x[1], reverse=True)

    # Top 5 anzeigen
    print(f"=== SEMANTISCHE TREFFER fuer '{query}' ===\n")
    for i, (doc_id, similarity, content) in enumerate(results[:5]):
        print(f"--- [{i+1}] ID: {doc_id} (Score: {similarity:.3f}) ---")
        print(content[:500] if len(content) > 500 else content)
        print()


def cmd_get(args):
    """Dokument per ID holen"""
    if not args:
        print("Usage: claude-mem.py get <id>")
        sys.exit(1)

    try:
        doc_id = int(args[0])
    except ValueError:
        print("FEHLER: ID muss eine Zahl sein")
        sys.exit(1)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT em.string_value as document
        FROM embeddings e
        LEFT JOIN embedding_metadata em ON e.id = em.id AND em.key = 'chroma:document'
        WHERE e.id = ?
    ''', (doc_id,))

    result = cursor.fetchone()
    conn.close()

    if not result:
        print(f"Dokument mit ID {doc_id} nicht gefunden")
        sys.exit(1)

    print(result[0] or "(leer)")


def cmd_add(args):
    """Neues Dokument hinzufuegen (mit automatischem Embedding)"""
    if not args:
        print("Usage: claude-mem.py add <text>")
        sys.exit(1)

    text = ' '.join(args)

    conn = get_connection()
    cursor = conn.cursor()

    # Naechste freie ID finden
    cursor.execute("SELECT MAX(id) FROM embeddings")
    max_id = cursor.fetchone()[0] or 0
    new_id = max_id + 1

    # Collection ID holen
    collection_id = get_collection_id(cursor)

    # Segment ID holen
    cursor.execute("SELECT id FROM segments WHERE collection = ? AND type = 'urn:chroma:segment/metadata/sqlite' LIMIT 1", (collection_id,))
    segment_result = cursor.fetchone()
    if not segment_result:
        cursor.execute("SELECT id FROM segments WHERE collection = ? LIMIT 1", (collection_id,))
        segment_result = cursor.fetchone()
    if not segment_result:
        print("FEHLER: Kein Segment gefunden")
        sys.exit(1)
    segment_id = segment_result[0]

    # Einzigartige embedding_id generieren
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    embedding_id = f"doc-{timestamp}-{new_id}"

    # seq_id als BLOB
    seq_id_blob = new_id.to_bytes(8, byteorder='big')

    # Embedding einfuegen
    cursor.execute('''
        INSERT INTO embeddings (id, segment_id, embedding_id, seq_id)
        VALUES (?, ?, ?, ?)
    ''', (new_id, segment_id, embedding_id, seq_id_blob))

    # Dokument als Metadata speichern
    cursor.execute('''
        INSERT INTO embedding_metadata (id, key, string_value)
        VALUES (?, 'chroma:document', ?)
    ''', (new_id, text))

    # Timestamp hinzufuegen
    cursor.execute('''
        INSERT INTO embedding_metadata (id, key, string_value)
        VALUES (?, 'timestamp', ?)
    ''', (new_id, datetime.now().isoformat()))

    conn.commit()
    conn.close()

    # Embedding berechnen und speichern
    embedding = compute_embedding(text)
    if embedding is not None:
        emb_conn = get_embeddings_connection()
        emb_conn.execute('''
            INSERT OR REPLACE INTO document_embeddings (doc_id, embedding, model, created_at)
            VALUES (?, ?, ?, ?)
        ''', (new_id, pickle.dumps(embedding), EMBEDDING_MODEL, datetime.now().isoformat()))
        emb_conn.commit()
        emb_conn.close()
        print(f"Dokument hinzugefuegt mit ID: {new_id} (+ Embedding)")
    else:
        print(f"Dokument hinzugefuegt mit ID: {new_id} (ohne Embedding)")


def cmd_add_file(args):
    """Dokument aus Datei hinzufuegen"""
    if not args:
        print("Usage: claude-mem.py add-file <path>")
        sys.exit(1)

    file_path = Path(args[0])
    if not file_path.exists():
        print(f"FEHLER: Datei nicht gefunden: {file_path}")
        sys.exit(1)

    text = file_path.read_text(encoding='utf-8')
    cmd_add([text])


def cmd_delete(args):
    """Dokument loeschen (inkl. Embedding)"""
    if not args:
        print("Usage: claude-mem.py delete <id>")
        sys.exit(1)

    try:
        doc_id = int(args[0])
    except ValueError:
        print("FEHLER: ID muss eine Zahl sein")
        sys.exit(1)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM embeddings WHERE id = ?", (doc_id,))
    if not cursor.fetchone():
        print(f"Dokument mit ID {doc_id} nicht gefunden")
        sys.exit(1)

    cursor.execute("DELETE FROM embedding_metadata WHERE id = ?", (doc_id,))
    cursor.execute("DELETE FROM embeddings WHERE id = ?", (doc_id,))

    conn.commit()
    conn.close()

    # Embedding loeschen
    emb_conn = get_embeddings_connection()
    emb_conn.execute("DELETE FROM document_embeddings WHERE doc_id = ?", (doc_id,))
    emb_conn.commit()
    emb_conn.close()

    print(f"Dokument {doc_id} geloescht (inkl. Embedding)")


def cmd_count(args):
    """Anzahl Dokumente zaehlen"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM embeddings")
    count = cursor.fetchone()[0]
    conn.close()
    print(f"Anzahl Dokumente: {count}")


def cmd_startup(args):
    """Kritische Learnings fuer Session-Start laden"""
    results = get_all_documents()

    critical_terms = ['KRITISCH', 'LEARNING', 'FEHLER', 'WICHTIG', 'FIX', 'BUG', 'IMPLEMENTATION']
    critical_docs = []

    for doc_id, embedding_id, content in results:
        if content:
            content_upper = content.upper()
            if any(term in content_upper for term in critical_terms):
                critical_docs.append((doc_id, content))

    if not critical_docs:
        print("Keine kritischen Learnings gefunden.")
        return

    print(f"=== {len(critical_docs)} KRITISCHE LEARNINGS ===\n")
    for doc_id, content in critical_docs:
        preview = content[:300].replace('\n', '\n    ')
        print(f"[{doc_id}] {preview}")
        if len(content) > 300:
            print("    ...")
        print()


def cmd_startup_compact(args):
    """Kompakte Version fuer Hooks - nur Zusammenfassung"""
    results = get_all_documents()

    critical_terms = ['KRITISCH', 'LEARNING', 'FEHLER', 'WICHTIG', 'FIX', 'BUG']
    critical_docs = []

    for doc_id, embedding_id, content in results:
        if content:
            content_upper = content.upper()
            if any(term in content_upper for term in critical_terms):
                first_line = content.split('\n')[0][:100]
                critical_docs.append((doc_id, first_line))

    print(f"claude-mem: {len(results)} Dokumente, {len(critical_docs)} kritisch")
    if critical_docs:
        print("Letzte Learnings:")
        for doc_id, title in critical_docs[:5]:
            print(f"  [{doc_id}] {title}")


def cmd_session_init(args):
    """Alle Session-Start Daten in EINEM Aufruf (spart 2 Python-Prozesse + 2 DB-Verbindungen).
    Kombiniert: search 'AUTO-COMPACT MARKER' + startup-compact + search 'SESSION-ZUSAMMENFASSUNG'
    Output: JSON fuer session-startup.sh
    """
    results = get_all_documents()

    # 1. Auto-Compact Check
    post_compact = False
    for doc_id, _, content in results:
        if content and "AUTO-COMPACT MARKER" in content.upper():
            post_compact = True
            break

    # 2. Startup-Compact Info
    critical_terms = ['KRITISCH', 'LEARNING', 'FEHLER', 'WICHTIG', 'FIX', 'BUG']
    critical_docs = []
    for doc_id, _, content in results:
        if content:
            content_upper = content.upper()
            if any(term in content_upper for term in critical_terms):
                first_line = content.split('\n')[0][:100]
                critical_docs.append((doc_id, first_line))

    # Compact memory info
    lines = [f"claude-mem: {len(results)} Dokumente, {len(critical_docs)} kritisch"]
    if critical_docs:
        lines.append("Letzte Learnings:")
        for doc_id, title in critical_docs[:5]:
            lines.append(f"  [{doc_id}] {title}")
    memory_info = '\n'.join(lines)

    # Extended info (fuer post-compact)
    extended_lines = []
    if post_compact:
        all_critical = [(d, c) for d, _, c in results
                        if c and any(t in c.upper() for t in critical_terms)]
        extended_lines.append(f"=== {len(all_critical)} KRITISCHE LEARNINGS ===\n")
        for doc_id, content_item in all_critical[:10]:
            preview = content_item[:300].replace('\n', '\n    ')
            extended_lines.append(f"[{doc_id}] {preview}")
            if len(content_item) > 300:
                extended_lines.append("    ...")
            extended_lines.append("")
    extended_info = '\n'.join(extended_lines)

    # 3. Letzte Session-Zusammenfassungen
    session_summaries = []
    for doc_id, _, content in results:
        if content and "SESSION-ZUSAMMENFASSUNG" in content.upper():
            session_summaries.append((doc_id, content[:500]))

    last_session_lines = []
    for doc_id, content in session_summaries[:3]:
        last_session_lines.append(f"--- ID: {doc_id} ---")
        last_session_lines.append(content)
        last_session_lines.append("")
    last_session = '\n'.join(last_session_lines)

    # JSON Output
    output = {
        "post_compact": post_compact,
        "memory_info": memory_info,
        "memory_info_extended": extended_info,
        "last_session": last_session,
        "doc_count": len(results),
        "critical_count": len(critical_docs)
    }
    print(json.dumps(output, ensure_ascii=False))


def cmd_backup(args):
    """Backup aller Dokumente als JSON erstellen"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    if args:
        backup_path = Path(args[0])
    else:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = BACKUP_DIR / f"claude-mem-backup-{timestamp}.json"

    results = get_all_documents()

    backup_data = {
        "version": "1.0",
        "created_at": datetime.now().isoformat(),
        "collection": COLLECTION_NAME,
        "document_count": len(results),
        "documents": []
    }

    for doc_id, embedding_id, content in results:
        backup_data["documents"].append({
            "id": doc_id,
            "embedding_id": embedding_id,
            "content": content
        })

    with open(backup_path, 'w', encoding='utf-8') as f:
        json.dump(backup_data, f, indent=2, ensure_ascii=False)

    print(f"Backup erstellt: {backup_path}")
    print(f"Dokumente: {len(results)}")


def cmd_restore(args):
    """Backup aus JSON wiederherstellen"""
    if not args:
        print("Usage: claude-mem.py restore <path>")
        sys.exit(1)

    backup_path = Path(args[0])
    if not backup_path.exists():
        print(f"FEHLER: Backup nicht gefunden: {backup_path}")
        sys.exit(1)

    with open(backup_path, 'r', encoding='utf-8') as f:
        backup_data = json.load(f)

    print(f"Backup geladen: {backup_data.get('created_at', 'unbekannt')}")
    print(f"Dokumente im Backup: {backup_data.get('document_count', 0)}")

    existing = get_all_documents()
    existing_ids = {doc_id for doc_id, _, _ in existing}

    added = 0
    skipped = 0

    for doc in backup_data.get("documents", []):
        if doc["id"] in existing_ids:
            skipped += 1
            continue

        if doc.get("content"):
            cmd_add([doc["content"]])
            added += 1

    print(f"\nWiederherstellung abgeschlossen:")
    print(f"  Hinzugefuegt: {added}")
    print(f"  Uebersprungen (existieren bereits): {skipped}")


def cmd_json(args):
    """Alle Dokumente als JSON ausgeben"""
    results = get_all_documents()
    output = [{"id": doc_id, "embedding_id": emb_id, "content": content}
              for doc_id, emb_id, content in results]
    print(json.dumps(output, indent=2, ensure_ascii=False))


def cmd_stats(args):
    """Statistiken anzeigen"""
    results = get_all_documents()

    if not results:
        print("Keine Dokumente vorhanden.")
        return

    total_chars = sum(len(content or "") for _, _, content in results)
    avg_chars = total_chars / len(results) if results else 0

    categories = {
        'KRITISCH': 0,
        'LEARNING': 0,
        'IMPLEMENTATION': 0,
        'SESSION': 0,
        'FIX': 0,
        'VALIDIERUNG': 0
    }

    for _, _, content in results:
        if content:
            upper = content.upper()
            for cat in categories:
                if cat in upper:
                    categories[cat] += 1

    # Embedding-Status
    emb_conn = get_embeddings_connection()
    emb_cursor = emb_conn.cursor()
    emb_cursor.execute("SELECT COUNT(*) FROM document_embeddings")
    embedded_count = emb_cursor.fetchone()[0]
    emb_conn.close()

    print("=== STATISTIKEN ===\n")
    print(f"Gesamt Dokumente: {len(results)}")
    print(f"Mit Embedding: {embedded_count} ({100*embedded_count/len(results):.0f}%)")
    print(f"Gesamt Zeichen: {total_chars:,}")
    print(f"Durchschnitt pro Dokument: {avg_chars:.0f} Zeichen")
    print(f"\nKategorien:")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        if count > 0:
            print(f"  {cat}: {count}")


def cmd_embed_all(args):
    """Alle Dokumente embedden (fuer semantische Suche)"""
    results = get_all_documents()

    if not results:
        print("Keine Dokumente zum Embedden.")
        return

    # Bestehende Embeddings pruefen
    emb_conn = get_embeddings_connection()
    emb_cursor = emb_conn.cursor()
    emb_cursor.execute("SELECT doc_id FROM document_embeddings")
    existing_ids = {row[0] for row in emb_cursor.fetchall()}

    # Fehlende Embeddings finden
    to_embed = [(doc_id, content) for doc_id, _, content in results
                if doc_id not in existing_ids and content]

    if not to_embed:
        print(f"Alle {len(results)} Dokumente haben bereits Embeddings.")
        return

    print(f"Embedde {len(to_embed)} Dokumente...")

    # Modell laden
    model = get_embedding_model()
    if model is None:
        print("FEHLER: Konnte Embedding-Modell nicht laden")
        sys.exit(1)

    # Batch-Embedding fuer Effizienz
    texts = [content for _, content in to_embed]
    embeddings = model.encode(texts, convert_to_numpy=True, show_progress_bar=True)

    # Speichern
    for (doc_id, _), embedding in zip(to_embed, embeddings):
        emb_conn.execute('''
            INSERT OR REPLACE INTO document_embeddings (doc_id, embedding, model, created_at)
            VALUES (?, ?, ?, ?)
        ''', (doc_id, pickle.dumps(embedding), EMBEDDING_MODEL, datetime.now().isoformat()))

    emb_conn.commit()
    emb_conn.close()

    print(f"\n{len(to_embed)} Dokumente embedded.")
    print(f"Gesamt mit Embedding: {len(existing_ids) + len(to_embed)}/{len(results)}")


def cmd_embed_status(args):
    """Embedding-Status anzeigen"""
    results = get_all_documents()

    emb_conn = get_embeddings_connection()
    emb_cursor = emb_conn.cursor()
    emb_cursor.execute("SELECT doc_id, model, created_at FROM document_embeddings")
    embeddings = emb_cursor.fetchall()
    emb_conn.close()

    embedded_ids = {row[0] for row in embeddings}
    missing = [doc_id for doc_id, _, content in results if doc_id not in embedded_ids and content]

    print("=== EMBEDDING STATUS ===\n")
    print(f"Dokumente gesamt: {len(results)}")
    print(f"Mit Embedding: {len(embeddings)}")
    print(f"Ohne Embedding: {len(missing)}")
    print(f"Modell: {EMBEDDING_MODEL}")

    if missing:
        print(f"\nFehlende IDs: {missing[:10]}{'...' if len(missing) > 10 else ''}")
        print(f"\nFuehre 'embed-all' aus um fehlende Embeddings zu erstellen.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print(f"\nEmbedding-Modell: {EMBEDDING_MODEL}")
        print(f"Datenbank: {CHROMA_DB_PATH}")
        print(f"Embeddings: {EMBEDDINGS_DB_PATH}")
        sys.exit(0)

    command = sys.argv[1].lower()
    args = sys.argv[2:]

    commands = {
        'list': cmd_list,
        'search': cmd_search,
        'semantic': cmd_semantic,
        'get': cmd_get,
        'add': cmd_add,
        'add-file': cmd_add_file,
        'delete': cmd_delete,
        'count': cmd_count,
        'startup': cmd_startup,
        'startup-compact': cmd_startup_compact,
        'session-init': cmd_session_init,
        'backup': cmd_backup,
        'restore': cmd_restore,
        'json': cmd_json,
        'stats': cmd_stats,
        'embed-all': cmd_embed_all,
        'embed-status': cmd_embed_status,
    }

    if command in commands:
        commands[command](args)
    else:
        print(f"Unbekannter Befehl: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == '__main__':
    main()
