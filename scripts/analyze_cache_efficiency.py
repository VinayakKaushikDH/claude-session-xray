#!/usr/bin/env python3
"""
analyze_cache_efficiency.py — Phase 5.3

Reads index/sessions.json and writes findings/cache_efficiency.md.
Computes cache hit ratios, ephemeral tier breakdown, and cost impact.
"""

import json, os
from datetime import datetime, timezone

REPO_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SESSIONS_IN = os.path.join(REPO_DIR, "index", "sessions.json")
OUT         = os.path.join(REPO_DIR, "findings", "cache_efficiency.md")
STATE       = os.path.join(REPO_DIR, "state.json")

# Approximate input token price for sonnet (most common model) for no-cache hypothetical
SONNET_INPUT_PER_M = 3.00


def cache_hit_ratio(tt: dict) -> float:
    """cache_read / (cache_read + cache_creation + input_tokens)"""
    denom = (tt.get("cache_read_input_tokens", 0)
             + tt.get("cache_creation_input_tokens", 0)
             + tt.get("input_tokens", 0))
    if denom == 0:
        return 0.0
    return tt.get("cache_read_input_tokens", 0) / denom


def no_cache_cost(tt: dict, actual_cost: float, pricing_per_m: float = SONNET_INPUT_PER_M) -> float:
    """Hypothetical cost if all cached tokens were billed as regular input tokens."""
    cached = tt.get("cache_read_input_tokens", 0) + tt.get("cache_creation_input_tokens", 0)
    # Replace actual cache costs with full input price for those tokens
    # Actual cache_read cost = cached_read * 0.30/M, cache_creation = cached_creation * 3.75/M
    # Hypothetical = all cached tokens at input price (3.00/M)
    read = tt.get("cache_read_input_tokens", 0)
    creation = tt.get("cache_creation_input_tokens", 0)
    actual_cache_cost = read * 0.30 / 1e6 + creation * 3.75 / 1e6
    hypothetical_cache_cost = cached * pricing_per_m / 1e6
    return actual_cost - actual_cache_cost + hypothetical_cache_cost


def fmt_tokens(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.0f}K"
    return str(n)


def main():
    sessions = json.load(open(SESSIONS_IN))
    active = [s for s in sessions if s["status"] == "active"]

    # Compute per-session metrics
    metrics = []
    for s in active:
        tt = s.get("tokens_total", {})
        ratio = cache_hit_ratio(tt)
        total_cached = tt.get("cache_read_input_tokens", 0) + tt.get("cache_creation_input_tokens", 0)
        actual_cost = s.get("estimated_cost_usd", 0.0)
        hyp_cost = no_cache_cost(tt, actual_cost)
        metrics.append({
            "session_id": s["session_id"],
            "project": s["project_canonical"],
            "messages": s.get("unique_messages", 0),
            "cache_hit_ratio": ratio,
            "cache_read": tt.get("cache_read_input_tokens", 0),
            "cache_creation": tt.get("cache_creation_input_tokens", 0),
            "cache_creation_5m": tt.get("cache_creation_ephemeral_5m", 0),
            "cache_creation_1h": tt.get("cache_creation_ephemeral_1h", 0),
            "total_cached": total_cached,
            "actual_cost": actual_cost,
            "hypothetical_cost": hyp_cost,
            "savings": hyp_cost - actual_cost,
        })

    total_actual = sum(m["actual_cost"] for m in metrics)
    total_hyp    = sum(m["hypothetical_cost"] for m in metrics)
    total_savings = total_hyp - total_actual

    lines = []
    lines.append("# Cache Efficiency\n")
    lines.append(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_\n")

    lines.append("## Summary\n")
    lines.append(f"- **Active sessions analysed**: {len(metrics)}")
    lines.append(f"- **Total actual cost**: ${total_actual:.2f}")
    lines.append(f"- **Hypothetical cost (no caching)**: ${total_hyp:.2f}")
    lines.append(f"- **Cache savings**: ${total_savings:.2f} ({total_savings*100/total_hyp:.1f}% reduction)\n")

    # Distribution of cache hit ratios
    buckets = {"0–20%": 0, "20–40%": 0, "40–60%": 0, "60–80%": 0, "80–100%": 0}
    for m in metrics:
        r = m["cache_hit_ratio"]
        if r < 0.2:   buckets["0–20%"] += 1
        elif r < 0.4: buckets["20–40%"] += 1
        elif r < 0.6: buckets["40–60%"] += 1
        elif r < 0.8: buckets["60–80%"] += 1
        else:         buckets["80–100%"] += 1

    lines.append("## Cache Hit Ratio Distribution\n")
    lines.append("| Ratio Bucket | Sessions |")
    lines.append("|--------------|----------|")
    for bucket, count in buckets.items():
        bar = "█" * (count // 2)
        lines.append(f"| {bucket} | {count} {bar} |")

    # Ephemeral tier breakdown
    total_5m = sum(m["cache_creation_5m"] for m in metrics)
    total_1h = sum(m["cache_creation_1h"] for m in metrics)
    total_cc = total_5m + total_1h
    if total_cc > 0:
        lines.append(f"\n## Ephemeral Cache Tier Breakdown\n")
        lines.append(f"| Tier | Tokens | Share |")
        lines.append(f"|------|--------|-------|")
        lines.append(f"| 5-minute | {fmt_tokens(total_5m)} | {total_5m*100/total_cc:.1f}% |")
        lines.append(f"| 1-hour   | {fmt_tokens(total_1h)} | {total_1h*100/total_cc:.1f}% |")

    # Worst-cache sessions (lowest ratio, > 100K total cached tokens)
    big_sessions = [m for m in metrics if m["total_cached"] > 100_000]
    worst = sorted(big_sessions, key=lambda m: m["cache_hit_ratio"])[:10]
    lines.append("\n## Worst Cache Efficiency (sessions with >100K cached tokens)\n")
    lines.append("| Project | Session | Messages | Hit Ratio | Cache Read | Cache Created | Savings Lost |")
    lines.append("|---------|---------|----------|-----------|------------|---------------|--------------|")
    for m in worst:
        lines.append(
            f"| `{m['project']}` | `{m['session_id'][:8]}…` "
            f"| {m['messages']} "
            f"| {m['cache_hit_ratio']*100:.1f}% "
            f"| {fmt_tokens(m['cache_read'])} "
            f"| {fmt_tokens(m['cache_creation'])} "
            f"| ${m['hypothetical_cost'] - m['actual_cost']:.2f} |"
        )

    # Best-cache sessions
    best = sorted([m for m in metrics if m["total_cached"] > 100_000],
                  key=lambda m: -m["cache_hit_ratio"])[:10]
    lines.append("\n## Best Cache Efficiency (sessions with >100K cached tokens)\n")
    lines.append("| Project | Session | Messages | Hit Ratio | Savings |")
    lines.append("|---------|---------|----------|-----------|---------|")
    for m in best:
        lines.append(
            f"| `{m['project']}` | `{m['session_id'][:8]}…` "
            f"| {m['messages']} "
            f"| {m['cache_hit_ratio']*100:.1f}% "
            f"| ${m['savings']:.2f} |"
        )

    # Per-project cost impact
    from collections import defaultdict
    by_proj = defaultdict(lambda: {"actual": 0.0, "hyp": 0.0})
    for m in metrics:
        by_proj[m["project"]]["actual"] += m["actual_cost"]
        by_proj[m["project"]]["hyp"]    += m["hypothetical_cost"]

    lines.append("\n## Cost Impact by Project\n")
    lines.append("| Project | Actual Cost | No-Cache Cost | Savings |")
    lines.append("|---------|-------------|---------------|---------|")
    for proj, v in sorted(by_proj.items(), key=lambda x: -x[1]["actual"]):
        savings = v["hyp"] - v["actual"]
        lines.append(f"| `{proj}` | ${v['actual']:.2f} | ${v['hyp']:.2f} | ${savings:.2f} |")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Written: {OUT}")

    if os.path.exists(STATE):
        with open(STATE) as f:
            state = json.load(f)
        state.setdefault("analyses", {})["cache_efficiency"] = "complete"
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        with open(STATE, "w") as f:
            json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
