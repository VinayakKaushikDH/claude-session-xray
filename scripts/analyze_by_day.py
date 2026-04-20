#!/usr/bin/env python3
"""
analyze_by_day.py — Phase 5.2

Reads index/sessions.json and writes findings/by_day.md.
Buckets sessions by start_ts date and aggregates token/cost totals per day.
"""

import json, os
from collections import defaultdict
from datetime import datetime, timezone

REPO_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SESSIONS_IN = os.path.join(REPO_DIR, "index", "sessions.json")
OUT         = os.path.join(REPO_DIR, "findings", "by_day.md")
STATE       = os.path.join(REPO_DIR, "state.json")


def fmt_tokens(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.0f}K"
    return str(n)


def main():
    sessions = json.load(open(SESSIONS_IN))

    by_day = defaultdict(lambda: {
        "sessions": 0,
        "messages": 0,
        "user_turns": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "input_tokens": 0,
        "cost_usd": 0.0,
    })

    for s in sessions:
        if s["status"] != "active":
            continue
        ts = s.get("start_ts")
        if not ts:
            continue
        day = ts[:10]  # YYYY-MM-DD
        d = by_day[day]
        d["sessions"] += 1
        d["messages"] += s.get("unique_messages", 0)
        d["user_turns"] += s.get("user_turns", 0)
        tt = s.get("tokens_total", {})
        d["output_tokens"]              += tt.get("output_tokens", 0)
        d["cache_read_input_tokens"]    += tt.get("cache_read_input_tokens", 0)
        d["cache_creation_input_tokens"] += tt.get("cache_creation_input_tokens", 0)
        d["input_tokens"]               += tt.get("input_tokens", 0)
        d["cost_usd"]                   += s.get("estimated_cost_usd", 0.0)

    sorted_days = sorted(by_day.keys())

    lines = []
    lines.append("# Token Usage by Day\n")
    lines.append(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_\n")
    lines.append(f"Coverage: {sorted_days[0]} → {sorted_days[-1]} ({len(sorted_days)} days)\n")
    lines.append("> Note: `stats-cache.json` covers up to 2026-04-15 only. "
                 "This analysis uses raw JSONL and covers the full date range.\n")

    lines.append("## Daily Activity\n")
    lines.append("| Date | Sessions | Messages | User Turns | Output | Cache Read | Cache Created | Cost USD |")
    lines.append("|------|----------|----------|------------|--------|------------|---------------|----------|")

    total_cost = 0.0
    for day in sorted_days:
        d = by_day[day]
        lines.append(
            f"| {day} "
            f"| {d['sessions']} "
            f"| {d['messages']:,} "
            f"| {d['user_turns']:,} "
            f"| {fmt_tokens(d['output_tokens'])} "
            f"| {fmt_tokens(d['cache_read_input_tokens'])} "
            f"| {fmt_tokens(d['cache_creation_input_tokens'])} "
            f"| ${d['cost_usd']:.2f} |"
        )
        total_cost += d["cost_usd"]

    lines.append(f"\n**Total: ${total_cost:.2f}** across {len(sorted_days)} active days\n")

    # Busiest days
    lines.append("## Top 5 Most Expensive Days\n")
    top_days = sorted(sorted_days, key=lambda d: -by_day[d]["cost_usd"])[:5]
    for day in top_days:
        d = by_day[day]
        lines.append(f"- **{day}**: ${d['cost_usd']:.2f} — "
                     f"{d['sessions']} sessions, {d['messages']} messages, "
                     f"{fmt_tokens(d['output_tokens'])} output tokens")

    lines.append("\n## Top 5 Busiest Days (by messages)\n")
    top_msg = sorted(sorted_days, key=lambda d: -by_day[d]["messages"])[:5]
    for day in top_msg:
        d = by_day[day]
        lines.append(f"- **{day}**: {d['messages']:,} messages — "
                     f"{d['sessions']} sessions, ${d['cost_usd']:.2f}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Written: {OUT}")

    if os.path.exists(STATE):
        with open(STATE) as f:
            state = json.load(f)
        state.setdefault("analyses", {})["by_day"] = "complete"
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        with open(STATE, "w") as f:
            json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
