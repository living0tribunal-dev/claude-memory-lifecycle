#!/usr/bin/env python3
"""SubagentStart Hook: Inject Gate-3 context into subagents.

Subagents don't inherit the parent's write-gate state, making them Gate-3 blind.
This hook injects CWD file info + Gate-3 rule as FACTS (not imperatives).
"""
import hashlib
import json
import sys
import time
from pathlib import Path

from platform_adapter import HookContext


def main():
    ctx = HookContext("SubagentStart")

    # Explore/Plan agents don't need Gate-3 (they only read)
    if ctx.agent_type in ("Explore", "Plan"):
        sys.exit(0)

    messages = []

    # Fact-based Gate-3 context (same as awareness_mode)
    try:
        cwd = ctx.cwd
        cwd_hash = hashlib.md5(cwd.encode()).hexdigest()[:8]
        import os
        wg_state_file = Path(os.environ.get("TEMP", "/tmp")) / "claude-write-gate" / f"state-{cwd_hash}.json"
        reads_count = 0
        if wg_state_file.exists():
            wg_state = json.loads(wg_state_file.read_text(encoding='utf-8'))
            reads_count = len(wg_state.get("reads", []))

        cwd_path = Path(cwd)
        all_files = [f for f in cwd_path.iterdir() if f.is_file() and not f.name.startswith('.')]
        n_files = len(all_files)
        one_hour_ago = time.time() - 3600
        recent = sum(1 for f in all_files if f.stat().st_mtime > one_hour_ago)
        recent_str = f", {recent} kuerzlich geaendert" if recent > 0 else ""

        messages.append(f"GATE-3: {n_files} Dateien in CWD{recent_str}. {reads_count} vom Parent gelesen.")
        messages.append("Regel: Vor Write/Edit die Zieldatei mit Read/Grep lesen.")
    except Exception:
        messages.append("GATE-3: Vor Write/Edit die Zieldatei mit Read/Grep lesen.")

    if messages:
        ctx.inject("\n".join(messages))

    sys.exit(0)


if __name__ == "__main__":
    main()
