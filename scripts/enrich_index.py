#!/usr/bin/env python3
"""
enrich_index.py — Phase 4 (optional) enrichment.

Matches sessions in index/sessions.json against ~/.claude/history.jsonl
by sessionId, and populates history_prompt and history_timestamp fields.

This phase is optional and additive — core analyses never require these fields.
Expected match rate: ~44% of sessions.

Usage:
    python3 scripts/enrich_index.py
"""

import json
import os
from datetime import datetime, timezone

REPO_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HOME         = os.path.expanduser("~")
HISTORY      = os.path.join(HOME, ".claude", "history.jsonl")
SESSIONS_IN  = os.path.join(REPO_DIR, "index", "sessions.json")
STATE        = os.path.join(REPO_DIR, "state.json")


def load_history() -> dict:
    """Load history.jsonl and return a dict keyed by sessionId.

    For sessions with multiple history entries, keep the first (earliest) one.
    """
    history = {}
    if not os.path.exists(HISTORY):
        print(f"WARNING: {HISTORY} not found — skipping enrichment")
        return history

    errors = 0
    with open(HISTORY, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                errors += 1
                continue
            sid = entry.get("sessionId")
            if not sid:
                continue
            if sid not in history:
                history[sid] = {
                    "prompt": entry.get("display") or entry.get("prompt"),
                    "timestamp": entry.get("timestamp"),
                }
    if errors:
        print(f"WARNING: {errors} malformed lines skipped in history.jsonl")
    return history


def main():
    sessions = json.load(open(SESSIONS_IN))
    history = load_history()

    matched = 0
    for s in sessions:
        sid = s.get("session_id")
        h = history.get(sid)
        if h:
            s["history_prompt"] = h["prompt"]
            s["history_timestamp"] = h["timestamp"]
            matched += 1

    with open(SESSIONS_IN, "w") as f:
        json.dump(sessions, f, indent=2)

    total = len(sessions)
    pct = matched * 100 // total if total else 0
    print(f"Enriched {matched}/{total} sessions ({pct}%)")
    print(f"Unmatched: {total - matched} sessions (history_prompt=null)")

    # Update state
    if os.path.exists(STATE):
        with open(STATE) as f:
            state = json.load(f)
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        with open(STATE, "w") as f:
            json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
