#!/usr/bin/env python3
"""
generate_overview.py — Phase 5.6 (run last)

Reads index/sessions.json + index/projects.json and writes findings/overview.md.
"""

import json, os
from datetime import datetime, timezone

REPO_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SESSIONS_IN = os.path.join(REPO_DIR, "index", "sessions.json")
PROJECTS_IN = os.path.join(REPO_DIR, "index", "projects.json")
OUT         = os.path.join(REPO_DIR, "findings", "overview.md")
STATE       = os.path.join(REPO_DIR, "state.json")


def fmt_tokens(n):
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:     return f"{n/1_000_000:.1f}M"
    if n >= 1_000:         return f"{n/1_000:.0f}K"
    return str(n)


def main():
    sessions = json.load(open(SESSIONS_IN))
    projects = json.load(open(PROJECTS_IN))

    active = [s for s in sessions if s["status"] == "active"]
    empty  = [s for s in sessions if s["status"] == "empty"]
    errors = [s for s in sessions if s["status"] == "error_only"]

    # Grand totals
    total_cost     = sum(s["estimated_cost_usd"] for s in active)
    total_messages = sum(s["unique_messages"] for s in active)
    total_turns    = sum(s.get("user_turns", 0) for s in active)

    grand_tokens = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    for s in active:
        tt = s.get("tokens_total", {})
        for k in grand_tokens:
            grand_tokens[k] += tt.get(k, 0)

    # Effective input = input + cache_creation + cache_read
    effective_input = (grand_tokens["input_tokens"]
                       + grand_tokens["cache_creation_input_tokens"]
                       + grand_tokens["cache_read_input_tokens"])

    # Date range
    dates = sorted(s["start_ts"][:10] for s in active if s.get("start_ts"))
    date_range = f"{dates[0]} → {dates[-1]}" if dates else "unknown"

    # Model breakdown
    model_tokens = {}
    for s in active:
        for model, mt in s.get("tokens_by_model", {}).items():
            if model not in model_tokens:
                model_tokens[model] = {"output_tokens": 0, "sessions": 0}
            model_tokens[model]["output_tokens"] += mt.get("output_tokens", 0)
            model_tokens[model]["sessions"] += 1

    # Top 5 projects by cost
    top_projects = sorted(projects.values(), key=lambda p: -p["estimated_cost_usd"])
    top_projects = [p for p in top_projects if p["active_session_count"] > 0][:5]

    # Top 5 sessions by cost
    top_sessions = sorted(active, key=lambda s: -s["estimated_cost_usd"])[:5]

    lines = []
    lines.append("# Claude Session Analysis — Overview\n")
    lines.append(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_\n")
    lines.append(f"**Date range**: {date_range}\n")

    lines.append("## Session Summary\n")
    lines.append(f"| Status | Count |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Active (has token data) | **{len(active)}** |")
    lines.append(f"| Empty (abandoned/crashed) | {len(empty)} |")
    lines.append(f"| Error-only (API errors) | {len(errors)} |")
    lines.append(f"| **Total session files** | **{len(sessions)}** |\n")

    lines.append("## Token Totals\n")
    lines.append(f"| Category | Tokens |")
    lines.append(f"|----------|--------|")
    lines.append(f"| Output tokens | {fmt_tokens(grand_tokens['output_tokens'])} |")
    lines.append(f"| Cache read (input) | {fmt_tokens(grand_tokens['cache_read_input_tokens'])} |")
    lines.append(f"| Cache creation | {fmt_tokens(grand_tokens['cache_creation_input_tokens'])} |")
    lines.append(f"| Direct input tokens | {fmt_tokens(grand_tokens['input_tokens'])} |")
    lines.append(f"| **Effective input (total)** | **{fmt_tokens(effective_input)}** |")
    lines.append(f"| Unique messages | {total_messages:,} |")
    lines.append(f"| User turns | {total_turns:,} |\n")

    lines.append("## Estimated Cost\n")
    lines.append(f"**Total: ${total_cost:.2f}**\n")
    lines.append("> _Approximate — based on public API pricing. Claude Code may have different rates._\n")

    lines.append("## Model Usage\n")
    lines.append(f"| Model | Sessions | Output Tokens |")
    lines.append(f"|-------|----------|--------------|")
    for model, mt in sorted(model_tokens.items(), key=lambda x: -x[1]["output_tokens"]):
        lines.append(f"| {model} | {mt['sessions']} | {fmt_tokens(mt['output_tokens'])} |")
    lines.append(f"\n> _Note: haiku usage (~90M tokens in stats-cache) is invisible in JSONL files "
                 f"— it comes from internal Claude Code processes, not project API calls._\n")

    lines.append("## Top 5 Projects by Cost\n")
    lines.append(f"| Project | Sessions | Messages | Cost |")
    lines.append(f"|---------|----------|----------|------|")
    for p in top_projects:
        lines.append(
            f"| `{p['canonical_name']}` "
            f"| {p['active_session_count']} "
            f"| {p['total_unique_messages']:,} "
            f"| ${p['estimated_cost_usd']:.2f} |"
        )

    lines.append("\n## Top 5 Sessions by Cost\n")
    lines.append(f"| Session | Project | Messages | Cost | First Prompt |")
    lines.append(f"|---------|---------|----------|------|--------------|")
    for s in top_sessions:
        prompt = (s.get("history_prompt") or "—")[:55]
        lines.append(
            f"| `{s['session_id'][:8]}…` "
            f"| `{s['project_canonical']}` "
            f"| {s['unique_messages']} "
            f"| ${s['estimated_cost_usd']:.2f} "
            f"| {prompt} |"
        )

    lines.append("\n## Detailed Findings\n")
    lines.append("- [By Project](by_project.md)")
    lines.append("- [By Day](by_day.md)")
    lines.append("- [Cache Efficiency](cache_efficiency.md)")
    lines.append("- [Context Growth](context_growth.md)")
    lines.append("- [Cost Report](cost_report.md)")

    lines.append("\n## Known Limitations\n")
    lines.append("1. **Haiku tokens invisible** — stats-cache reports ~90M haiku tokens but zero "
                 "exist in JSONL files. Internal Claude Code processes use haiku but don't write "
                 "to project session files. Total token usage is undercounted by this amount.")
    lines.append("2. **claude-squad worktrees unattributable** — all `~/.claude-squad/worktrees/*` "
                 "sessions are grouped under `home-directory`. The worktree directories have been "
                 "deleted and no git mapping survives, so the actual target repos are unknown.")
    lines.append("3. **Cost estimates approximate** — public API pricing used. Claude Code may "
                 "have negotiated rates that differ.")
    lines.append("4. **Cache tier pricing** — 5-minute and 1-hour ephemeral cache tiers are billed "
                 "at the same rate in this model; actual rates may differ.")
    lines.append("5. **Enrichment sparse** — only ~43% of sessions matched a `history.jsonl` entry; "
                 "first-prompt text is null for the rest.")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Written: {OUT}")

    if os.path.exists(STATE):
        with open(STATE) as f:
            state = json.load(f)
        state.setdefault("analyses", {})["overview"] = "complete"
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        with open(STATE, "w") as f:
            json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
