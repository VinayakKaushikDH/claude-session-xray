#!/usr/bin/env python3
"""
aggregate_projects.py — Phase 3 aggregator.

Reads index/sessions.json and writes index/projects.json, grouping
active sessions by project_canonical and summing all token/cost fields.

Usage:
    python3 scripts/aggregate_projects.py
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timezone

REPO_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSIONS_IN   = os.path.join(REPO_DIR, "index", "sessions.json")
PROJECTS_OUT  = os.path.join(REPO_DIR, "index", "projects.json")
STATE         = os.path.join(REPO_DIR, "state.json")

TOKEN_FIELDS = [
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cache_creation_ephemeral_5m",
    "cache_creation_ephemeral_1h",
    "web_search_requests",
    "web_fetch_requests",
]


def load_sessions() -> list:
    with open(SESSIONS_IN) as f:
        return json.load(f)


def save_state_analysis(key: str, value: str):
    state_path = STATE
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("analyses", {})[key] = value
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


def main():
    sessions = load_sessions()

    # Buckets keyed by project_canonical
    projects = defaultdict(lambda: {
        "canonical_name": "",
        "category": "",
        "session_count": 0,
        "active_session_count": 0,
        "empty_session_count": 0,
        "error_only_session_count": 0,
        "tokens_total": {k: 0 for k in TOKEN_FIELDS},
        "tokens_by_model": defaultdict(lambda: {k: 0 for k in TOKEN_FIELDS}),
        "estimated_cost_usd": 0.0,
        "tool_calls_by_name": defaultdict(int),
        "tool_calls_total": 0,
        "total_user_turns": 0,
        "total_unique_messages": 0,
        "dates": [],
        "session_ids": [],
    })

    for s in sessions:
        key = s["project_canonical"]
        p = projects[key]
        p["canonical_name"] = key
        p["category"] = s["project_category"]
        p["session_count"] += 1
        p["session_ids"].append(s["session_id"])

        status = s["status"]
        if status == "active":
            p["active_session_count"] += 1
        elif status == "empty":
            p["empty_session_count"] += 1
            continue  # exclude empty sessions from all token/cost sums
        elif status == "error_only":
            p["error_only_session_count"] += 1
            continue

        # Token totals
        tt = s.get("tokens_total", {})
        for field in TOKEN_FIELDS:
            p["tokens_total"][field] += tt.get(field, 0)

        # Per-model tokens
        for model, mt in s.get("tokens_by_model", {}).items():
            for field in TOKEN_FIELDS:
                p["tokens_by_model"][model][field] += mt.get(field, 0)

        # Cost, tool calls, turns, messages
        p["estimated_cost_usd"] += s.get("estimated_cost_usd", 0.0)
        p["tool_calls_total"]   += s.get("tool_calls_total", 0)
        p["total_user_turns"]   += s.get("user_turns", 0)
        p["total_unique_messages"] += s.get("unique_messages", 0)

        for tool, count in s.get("tool_calls_by_name", {}).items():
            p["tool_calls_by_name"][tool] += count

        # Date range
        for ts_field in ("start_ts", "end_ts"):
            ts = s.get(ts_field)
            if ts:
                p["dates"].append(ts[:10])  # YYYY-MM-DD

    # Finalise: convert defaultdicts, compute date ranges, round costs
    output = {}
    for key, p in projects.items():
        dates = sorted(set(p["dates"]))
        output[key] = {
            "canonical_name": p["canonical_name"],
            "category": p["category"],
            "session_count": p["session_count"],
            "active_session_count": p["active_session_count"],
            "empty_session_count": p["empty_session_count"],
            "error_only_session_count": p["error_only_session_count"],
            "date_range": [dates[0], dates[-1]] if dates else [None, None],
            "tokens_total": p["tokens_total"],
            "tokens_by_model": {m: dict(t) for m, t in p["tokens_by_model"].items()},
            "estimated_cost_usd": round(p["estimated_cost_usd"], 4),
            "tool_calls_by_name": dict(sorted(p["tool_calls_by_name"].items(),
                                              key=lambda x: -x[1])),
            "tool_calls_total": p["tool_calls_total"],
            "total_user_turns": p["total_user_turns"],
            "total_unique_messages": p["total_unique_messages"],
            "session_ids": p["session_ids"],
        }

    os.makedirs(os.path.dirname(PROJECTS_OUT), exist_ok=True)
    with open(PROJECTS_OUT, "w") as f:
        json.dump(output, f, indent=2)

    # Print summary sorted by cost
    ranked = sorted(output.values(), key=lambda p: -p["estimated_cost_usd"])
    print(f"{'Project':<40} {'Sessions':>8} {'Messages':>8} {'Cost USD':>10}")
    print("-" * 70)
    for p in ranked:
        print(f"{p['canonical_name']:<40} {p['active_session_count']:>8} "
              f"{p['total_unique_messages']:>8} ${p['estimated_cost_usd']:>9.2f}")
    print(f"\nTotal projects: {len(output)}")
    print(f"Output: {PROJECTS_OUT}")

    save_state_analysis("by_project", "pending")  # analysis not yet written, just index ready


if __name__ == "__main__":
    main()
