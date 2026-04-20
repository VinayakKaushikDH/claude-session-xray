#!/usr/bin/env python3
"""
analyze_by_project.py — Phase 5.1

Reads index/projects.json and writes findings/by_project.md.
"""

import json, os
from datetime import datetime, timezone

REPO_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PROJECTS_IN  = os.path.join(REPO_DIR, "index", "projects.json")
OUT          = os.path.join(REPO_DIR, "findings", "by_project.md")
STATE        = os.path.join(REPO_DIR, "state.json")


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def main():
    projects = json.load(open(PROJECTS_IN))
    ranked = sorted(projects.values(), key=lambda p: -p["estimated_cost_usd"])
    # exclude projects with zero active sessions
    ranked = [p for p in ranked if p["active_session_count"] > 0]

    lines = []
    lines.append("# Token Usage by Project\n")
    lines.append(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_\n")

    # Summary table
    lines.append("## Ranked by Estimated Cost\n")
    lines.append("| Project | Category | Sessions | Messages | Output Tokens | Cache Read | Cost USD | Date Range |")
    lines.append("|---------|----------|----------|----------|--------------|------------|----------|------------|")

    total_cost = 0.0
    for p in ranked:
        tt = p["tokens_total"]
        date_range = " → ".join(d or "?" for d in p["date_range"])
        lines.append(
            f"| `{p['canonical_name']}` | {p['category']} "
            f"| {p['active_session_count']} "
            f"| {p['total_unique_messages']:,} "
            f"| {fmt_tokens(tt['output_tokens'])} "
            f"| {fmt_tokens(tt['cache_read_input_tokens'])} "
            f"| ${p['estimated_cost_usd']:.2f} "
            f"| {date_range} |"
        )
        total_cost += p["estimated_cost_usd"]

    lines.append(f"\n**Total estimated cost across all projects: ${total_cost:.2f}**\n")

    # Per-project model breakdown
    lines.append("## Model Usage by Project\n")
    lines.append("| Project | Model | Output Tokens | Cache Read | Cost Share |")
    lines.append("|---------|-------|--------------|------------|------------|")
    for p in ranked:
        for model, mt in sorted(p["tokens_by_model"].items()):
            if model == "<synthetic>":
                continue
            # rough cost share
            lines.append(
                f"| `{p['canonical_name']}` | {model} "
                f"| {fmt_tokens(mt['output_tokens'])} "
                f"| {fmt_tokens(mt['cache_read_input_tokens'])} "
                f"| — |"
            )

    # Top tools per project
    lines.append("\n## Top Tool Calls by Project\n")
    for p in ranked[:8]:  # top 8 by cost
        if not p["tool_calls_by_name"]:
            continue
        top_tools = list(p["tool_calls_by_name"].items())[:5]
        tool_str = ", ".join(f"{n}×{t}" for t, n in top_tools)
        lines.append(f"- **{p['canonical_name']}** ({p['tool_calls_total']:,} total): {tool_str}")

    # Note on home-directory bucket
    home = projects.get("home-directory")
    if home and home["active_session_count"] > 0:
        lines.append(f"""
## Note: `home-directory` Bucket

The `home-directory` project ({home['active_session_count']} sessions, \
${home['estimated_cost_usd']:.2f}) contains sessions from \
`~/.claude-squad/worktrees/*` paths. These are claude-squad worker \
sessions whose parent repository cannot be determined — the worktree \
directories have been deleted and no git mapping survives in the data. \
They are grouped here rather than attributed to a specific project.
""")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Written: {OUT}")

    # Update state
    if os.path.exists(STATE):
        with open(STATE) as f:
            state = json.load(f)
        state.setdefault("analyses", {})["by_project"] = "complete"
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        with open(STATE, "w") as f:
            json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
