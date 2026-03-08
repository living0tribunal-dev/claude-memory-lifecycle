#!/usr/bin/env python3
"""Empirical Evaluation — Memory Buffer System."""

import sqlite3
from pathlib import Path

DB = Path.home() / ".claude-mem" / "buffer.sqlite3"


def run():
    conn = sqlite3.connect(str(DB))

    # 1. CONNECTION DISCRIMINATION
    print("=== 1. CONNECTION DISCRIMINATION ===")
    rows = conn.execute("""
        SELECT a.project, b.project, co.similarity
        FROM connections co
        JOIN buffer_entries a ON co.entry_a = a.id
        JOIN buffer_entries b ON co.entry_b = b.id
        WHERE a.state != 'expired' AND b.state != 'expired'
    """).fetchall()

    intra, cross, wildcard = [], [], []
    for pa, pb, sim in rows:
        if pa and pb and pa == pb:
            intra.append(sim)
        elif pa is None or pb is None:
            wildcard.append(sim)
        else:
            cross.append(sim)

    for label, data in [("Intra-Project", intra), ("Cross-Project", cross), ("NULL-Wildcard", wildcard)]:
        if data:
            avg = sum(data) / len(data)
            print(f"  {label:15s}: n={len(data):3d}  avg={avg:.4f}  min={min(data):.4f}  max={max(data):.4f}")
    print(f"  Total active connections: {len(intra) + len(cross) + len(wildcard)}")

    # 2. SIMILARITY DISTRIBUTION
    print("\n=== 2. SIMILARITY DISTRIBUTION ===")
    for label, data in [("Intra-Project", intra), ("Cross-Project", cross)]:
        buckets = {"0.90+": 0, "0.85-0.89": 0, "0.80-0.84": 0, "0.75-0.79": 0}
        for s in data:
            if s >= 0.90: buckets["0.90+"] += 1
            elif s >= 0.85: buckets["0.85-0.89"] += 1
            elif s >= 0.80: buckets["0.80-0.84"] += 1
            else: buckets["0.75-0.79"] += 1
        print(f"  {label}:")
        for bucket, cnt in buckets.items():
            pct = 100 * cnt / len(data) if data else 0
            print(f"    {bucket}: {cnt:3d} ({pct:.0f}%)")

    # 3. TYPE DETECTION ACCURACY
    print("\n=== 3. TYPE DETECTION ACCURACY ===")
    entries = conn.execute("SELECT id, entry_type, text FROM buffer_entries").fetchall()
    correct, wrong, mismatches = 0, 0, []
    for eid, etype, text in entries:
        expected = "insight"
        if text.strip().startswith("AUTO-SESSION-SAVE"):
            expected = "auto-session-save"
        elif "#user-gedanke" in text[:300]:
            expected = "user-gedanke"
        elif "#session-save" in text[:300]:
            expected = "session-save"
        if etype == expected:
            correct += 1
        else:
            wrong += 1
            mismatches.append(f"  [{eid}] expected={expected} got={etype}")
    total = correct + wrong
    print(f"  Correct: {correct}/{total} ({100*correct/total:.1f}%)")
    if mismatches:
        print("  Mismatches:")
        for m in mismatches:
            print(m)

    # 4. AGING AUDIT
    print("\n=== 4. AGING AUDIT ===")
    expired = conn.execute(
        "SELECT id, entry_type, text, project FROM buffer_entries WHERE state = 'expired'"
    ).fetchall()
    categories = {}
    for eid, etype, text, proj in expired:
        if "AUTO-SESSION-SAVE" in text and "User Messages: 0" in text:
            cat = "empty-auto-save"
        elif "AUTO-SESSION-SAVE" in text:
            cat = "auto-save-with-content"
        elif "#session-save" in text[:200]:
            cat = "old-session-save"
        elif proj is None and etype == "insight":
            cat = "unlinked-insight"
        else:
            cat = "other"
        categories[cat] = categories.get(cat, 0) + 1
    print(f"  Total expired: {len(expired)}")
    for cat, cnt in sorted(categories.items(), key=lambda x: -x[1]):
        print(f"    {cat}: {cnt}")

    # 5. LIFECYCLE STATS
    print("\n=== 5. LIFECYCLE STATS ===")
    total_entries = conn.execute("SELECT COUNT(*) FROM buffer_entries").fetchone()[0]
    by_state = conn.execute(
        "SELECT state, COUNT(*) FROM buffer_entries GROUP BY state ORDER BY COUNT(*) DESC"
    ).fetchall()
    for state, cnt in by_state:
        print(f"  {state:10s}: {cnt:3d} ({100*cnt/total_entries:.0f}%)")
    proven = sum(c for s, c in by_state if s in ("proven", "permanent"))
    print(f"  Promotion rate: {proven}/{total_entries} = {100*proven/total_entries:.1f}%")

    # 6. CLUSTER DENSITY
    print("\n=== 6. CLUSTER DENSITY ===")
    buffer_ids = [r[0] for r in conn.execute(
        "SELECT id FROM buffer_entries WHERE state = 'buffer'"
    ).fetchall()]
    connected_ids = set()
    for bid in buffer_ids:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM connections WHERE entry_a = ? OR entry_b = ?", (bid, bid)
        ).fetchone()[0]
        if cnt > 0:
            connected_ids.add(bid)
    isolated = len(buffer_ids) - len(connected_ids)
    print(f"  Buffer entries: {len(buffer_ids)}")
    print(f"  Connected: {len(connected_ids)} ({100*len(connected_ids)/len(buffer_ids):.0f}%)")
    print(f"  Isolated: {isolated}")

    # Connection count distribution for buffer entries
    conn_counts = []
    for bid in buffer_ids:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM connections WHERE entry_a = ? OR entry_b = ?", (bid, bid)
        ).fetchone()[0]
        conn_counts.append(cnt)
    if conn_counts:
        conn_counts.sort()
        print(f"  Connections per entry: min={min(conn_counts)} median={conn_counts[len(conn_counts)//2]} max={max(conn_counts)}")

    # 7. RECALL STATS
    print("\n=== 7. RECALL STATS ===")
    recalled = conn.execute(
        "SELECT id, recall_count, entry_type FROM buffer_entries WHERE recall_count > 0"
    ).fetchall()
    print(f"  Entries with recalls: {len(recalled)}/{total_entries}")
    for eid, rc, etype in recalled:
        print(f"    [{eid}] recalls={rc} type={etype}")

    # 8. AUTO-EXPIRE PRECISION
    print("\n=== 8. AUTO-EXPIRE PRECISION ===")
    auto_expired = conn.execute(
        "SELECT id, text FROM buffer_entries "
        "WHERE entry_type = 'auto-session-save' AND state = 'expired'"
    ).fetchall()
    auto_buffer = conn.execute(
        "SELECT id, text FROM buffer_entries "
        "WHERE entry_type = 'auto-session-save' AND state = 'buffer'"
    ).fetchall()
    empty_in_buffer = sum(1 for _, t in auto_buffer if "User Messages: 0" in t)
    content_expired = sum(1 for _, t in auto_expired if "User Messages: 0" not in t)
    print(f"  Auto-session-saves expired: {len(auto_expired)}")
    print(f"  Auto-session-saves in buffer: {len(auto_buffer)}")
    print(f"  False negatives (empty still in buffer): {empty_in_buffer}")
    print(f"  False positives (content wrongly expired): {content_expired}")

    # 9. USER-GEDANKE PROTECTION
    print("\n=== 9. USER-GEDANKE PROTECTION ===")
    ug_total = conn.execute(
        "SELECT COUNT(*) FROM buffer_entries WHERE entry_type = 'user-gedanke'"
    ).fetchone()[0]
    ug_buffer = conn.execute(
        "SELECT COUNT(*) FROM buffer_entries WHERE entry_type = 'user-gedanke' AND state = 'buffer'"
    ).fetchone()[0]
    ug_expired = conn.execute(
        "SELECT COUNT(*) FROM buffer_entries WHERE entry_type = 'user-gedanke' AND state = 'expired'"
    ).fetchone()[0]
    print(f"  Total user-gedanke: {ug_total}")
    print(f"  Protected (buffer): {ug_buffer}")
    print(f"  Expired before protection: {ug_expired} (pre-S25, before entry_type existed)")

    conn.close()
    print("\n=== EVALUATION COMPLETE ===")


if __name__ == "__main__":
    run()
