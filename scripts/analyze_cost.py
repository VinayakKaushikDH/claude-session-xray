#!/usr/bin/env python3
"""
analyze_cost.py — Phase 5.5

Reads index/sessions.json + index/projects.json and writes findings/cost_report.md.
"""

import json, os
from collections import defaultdict
from datetime import datetime, timezone

REPO_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SESSIONS_IN = os.path.join(REPO_DIR, "index", "sessions.json")
PROJECTS_IN = os.path.join(REPO_DIR, "index", "projects.json")
OUT         = os.path.join(REPO_DIR, "findings", "cost_report.md")
STATE       = os.path.join(REPO_DIR, "state.json")


def fmt_tokens(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.0f}K"
    return str(n)


def percentile(sorted_vals, p):
    if not sorted_vals: return 0.0
    idx = int(len(sorted_vals) * p / 100)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


def main():
    sessions = json.load(open(SESSIONS_IN))
    projects = json.load(open(PROJECTS_IN))

    active = [s for s in sessions if s["status"] == "active"]
    total_cost = sum(s["estimated_cost_usd"] for s in active)

    # Cost by model (aggregate across all sessions)
    cost_by_model = defaultdict(float)
    tokens_by_model = defaultdict(lambda: defaultdict(int))
    for s in active:
        for model, mt in s.get("tokens_by_model", {}).items():
            if model == "<synthetic>":
                continue
            # Compute cost share per model using pricing
            pass  # will approximate via proportion of output tokens
        # simpler: attribute cost proportionally by output tokens per model
        total_out = s["tokens_total"].get("output_tokens", 0)
        for model, mt in s.get("tokens_by_model", {}).items():
            if model == "<synthetic>":
                continue
            for k, v in mt.items():
                tokens_by_model[model][k] += v
            if total_out > 0:
                share = mt.get("output_tokens", 0) / total_out
            else:
                share = 1.0 / max(len(s.get("tokens_by_model", {})), 1)
            cost_by_model[model] += s["estimated_cost_usd"] * share

    # Daily cost
    daily_cost = defaultdict(float)
    daily_sessions = defaultdict(int)
    for s in active:
        ts = s.get("start_ts", "")
        if ts:
            day = ts[:10]
            daily_cost[day] += s["estimated_cost_usd"]
            daily_sessions[day] += 1

    # Cost per user turn
    costs_per_turn = []
    for s in active:
        turns = s.get("user_turns", 0)
        cost = s.get("estimated_cost_usd", 0.0)
        if turns > 0:
            costs_per_turn.append(cost / turns)
    costs_per_turn.sort()

    # No-cache hypothetical
    def no_cache_cost(s):
        tt = s.get("tokens_total", {})
        read = tt.get("cache_read_input_tokens", 0)
        creation = tt.get("cache_creation_input_tokens", 0)
        actual = s.get("estimated_cost_usd", 0.0)
        actual_cache = read * 0.30 / 1e6 + creation * 3.75 / 1e6
        hyp_cache = (read + creation) * 3.00 / 1e6
        return actual - actual_cache + hyp_cache

    total_hyp = sum(no_cache_cost(s) for s in active)

    lines = []
    lines.append("# Cost Report\n")
    lines.append(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_\n")

    lines.append("## Total Spend\n")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Estimated actual cost | **${total_cost:.2f}** |")
    lines.append(f"| Hypothetical cost (no caching) | ${total_hyp:.2f} |")
    lines.append(f"| Cache savings | **${total_hyp - total_cost:.2f}** ({(total_hyp-total_cost)*100/total_hyp:.1f}% reduction) |")
    lines.append(f"| Active sessions | {len(active)} |")
    lines.append(f"| Total messages | {sum(s['unique_messages'] for s in active):,} |")
    lines.append(f"| Total user turns | {sum(s.get('user_turns',0) for s in active):,} |\n")

    lines.append("## Cost by Model\n")
    lines.append("| Model | Est. Cost | Output Tokens | Cache Read | Cache Created |")
    lines.append("|-------|-----------|--------------|------------|---------------|")
    for model, cost in sorted(cost_by_model.items(), key=lambda x: -x[1]):
        mt = tokens_by_model[model]
        lines.append(
            f"| {model} | ${cost:.2f} "
            f"| {fmt_tokens(mt['output_tokens'])} "
            f"| {fmt_tokens(mt['cache_read_input_tokens'])} "
            f"| {fmt_tokens(mt['cache_creation_input_tokens'])} |"
        )

    lines.append("\n## Daily Cost Trend\n")
    lines.append("| Date | Sessions | Cost USD |")
    lines.append("|------|----------|----------|")
    for day in sorted(daily_cost.keys()):
        lines.append(f"| {day} | {daily_sessions[day]} | ${daily_cost[day]:.2f} |")

    lines.append("\n## Per-Project Cost Ranking\n")
    proj_ranked = sorted(projects.values(), key=lambda p: -p["estimated_cost_usd"])
    lines.append("| Project | Sessions | Cost | No-Cache Cost | Cache Savings |")
    lines.append("|---------|----------|------|---------------|---------------|")
    for p in proj_ranked:
        if p["active_session_count"] == 0:
            continue
        tt = p["tokens_total"]
        read = tt.get("cache_read_input_tokens", 0)
        creation = tt.get("cache_creation_input_tokens", 0)
        actual = p["estimated_cost_usd"]
        actual_cache = read * 0.30 / 1e6 + creation * 3.75 / 1e6
        hyp = actual - actual_cache + (read + creation) * 3.00 / 1e6
        lines.append(
            f"| `{p['canonical_name']}` | {p['active_session_count']} "
            f"| ${actual:.2f} | ${hyp:.2f} | ${hyp-actual:.2f} |"
        )

    lines.append("\n## Most Expensive Individual Sessions\n")
    top_sessions = sorted(active, key=lambda s: -s["estimated_cost_usd"])[:10]
    lines.append("| Session | Project | Messages | Cost | History Prompt |")
    lines.append("|---------|---------|----------|------|----------------|")
    for s in top_sessions:
        prompt = (s.get("history_prompt") or "—")[:60]
        lines.append(
            f"| `{s['session_id'][:8]}…` "
            f"| `{s['project_canonical']}` "
            f"| {s['unique_messages']} "
            f"| ${s['estimated_cost_usd']:.2f} "
            f"| {prompt} |"
        )

    lines.append("\n## Cost per User Turn\n")
    if costs_per_turn:
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Mean   | ${sum(costs_per_turn)/len(costs_per_turn):.4f} |")
        lines.append(f"| Median | ${percentile(costs_per_turn, 50):.4f} |")
        lines.append(f"| p95    | ${percentile(costs_per_turn, 95):.4f} |")
        lines.append(f"| Max    | ${costs_per_turn[-1]:.4f} |")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Written: {OUT}")

    if os.path.exists(STATE):
        with open(STATE) as f:
            state = json.load(f)
        state.setdefault("analyses", {})["cost_report"] = "complete"
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        with open(STATE, "w") as f:
            json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
