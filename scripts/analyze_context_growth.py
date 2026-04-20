#!/usr/bin/env python3
"""
analyze_context_growth.py — Phase 5.4

Does a second pass over raw JSONL for sessions with >= MIN_MESSAGES unique messages,
builds per-turn context window traces with enriched tool data (targets, success/failure),
and writes findings/context_growth.md.

Per-session turn data is cached in index/context_turns/<session_id>.json.

Cache format (dict):
  {
    "session_id": "...",
    "total_loops": 3,
    "all_files_touched": ["path/a", ...],
    "turns": [{
      "turn_index": 0,
      "message_id": "...",
      "timestamp": "...",
      "model": "...",
      "input_tokens": 0,
      "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 0,
      "output_tokens": 0,
      "context_window_tokens": 0,
      "tool_calls": [{"tool": "Bash", "success": true, "target": "npm test"}],
      "files_touched": ["src/auth.ts"],
    }]
  }
"""

import glob, json, os, re
from collections import Counter, defaultdict
from datetime import datetime, timezone

REPO_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HOME         = os.path.expanduser("~")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
SESSIONS_IN  = os.path.join(REPO_DIR, "index", "sessions.json")
TURNS_DIR    = os.path.join(REPO_DIR, "index", "context_turns")
OUT          = os.path.join(REPO_DIR, "findings", "context_growth.md")
STATE        = os.path.join(REPO_DIR, "state.json")
MIN_MESSAGES = 2


def parse_jsonl(filepath):
    with open(filepath, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass


def _extract_target(tool_name: str, inp: dict):
    """Return a short string target from a tool_use input dict, or None."""
    if not inp:
        return None
    if tool_name == "Bash":
        cmd = inp.get("command", "")
        return cmd[:80] if cmd else None
    if tool_name in ("Read", "Edit", "Write", "NotebookEdit"):
        return inp.get("file_path") or inp.get("path") or None
    if tool_name == "Glob":
        return inp.get("pattern") or inp.get("path") or None
    if tool_name == "Grep":
        pat = inp.get("pattern") or ""
        path = inp.get("path") or ""
        return (pat[:40] + (" in " + path[:30] if path else "")) or None
    if tool_name in ("WebFetch", "WebSearch"):
        return inp.get("url") or inp.get("query") or None
    return None


def _is_file_tool(tool_name: str) -> bool:
    return tool_name in ("Read", "Edit", "Write", "NotebookEdit", "Glob")


def count_loops(turns: list) -> int:
    """Count runs of 3+ consecutive turns dominated by the same (tool, target) pair."""
    signatures = []
    for t in turns:
        calls = [
            (tc.get("tool"), tc.get("target"))
            for tc in t.get("tool_calls", [])
            if tc.get("target")
        ]
        if calls:
            sig = Counter(calls).most_common(1)[0][0]
        else:
            sig = None
        signatures.append(sig)

    loops = 0
    i = 0
    while i < len(signatures):
        if signatures[i] is None:
            i += 1
            continue
        j = i + 1
        while j < len(signatures) and signatures[j] == signatures[i]:
            j += 1
        if j - i >= 3:
            loops += 1
        i = j
    return loops


def extract_turns(filepath: str) -> list:
    """
    Return list of per-turn dicts sorted by timestamp, with enriched tool data.

    Pairs tool_use blocks (from assistant records) with tool_result blocks
    (from the following user record) via tool_use.id == tool_result.tool_use_id.
    """
    # ── Pass 1: scan ALL records to build tool_use_id → {name, target, success} ──
    # Each assistant message may appear multiple times in the JSONL as streaming
    # chunks; tool_use blocks may be in a later chunk than the first appearance.
    # We scan everything without dedup here so we don't miss them.
    tool_use_map = {}  # tool_use_id -> {name, target, success}
    all_records = []   # (rtype, mid, msg, ts) — raw, not deduped
    for record in parse_jsonl(filepath):
        rtype = record.get("type")
        if rtype not in ("assistant", "user"):
            continue
        msg = record.get("message", {})
        mid = msg.get("id")
        model = msg.get("model", "")
        if rtype == "assistant" and (model == "<synthetic>" or record.get("isApiErrorMessage")):
            continue
        ts = record.get("timestamp", "")
        all_records.append((rtype, mid or "", msg, ts))

        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tid = block.get("id")
                if tid and tid not in tool_use_map:
                    name = block.get("name", "unknown")
                    inp = block.get("input") or {}
                    tool_use_map[tid] = {
                        "name": name,
                        "target": _extract_target(name, inp),
                        "success": True,  # default; overridden by tool_result below
                    }
            elif block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if tid and tid in tool_use_map:
                    tool_use_map[tid]["success"] = not block.get("is_error", False)

    # ── Pass 2: deduplicate assistant records by message_id (keep last for most ──
    # complete content — final streaming chunk includes all blocks).
    seen_asst = {}  # mid -> (msg, ts) — last occurrence wins
    for rtype, mid, msg, ts in all_records:
        if rtype != "assistant" or not mid:
            continue
        seen_asst[mid] = (msg, ts)
    records = [("assistant", mid, msg, ts) for mid, (msg, ts) in seen_asst.items()]

    # ── Pass 3: build assistant turn objects with enriched tool calls ─────────────
    messages = {}  # mid -> turn data
    for rtype, mid, msg, ts in records:
        if rtype != "assistant":
            continue
        if mid in messages:
            continue  # already captured (dedup)
        usage = msg.get("usage", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            content = []

        tool_calls_out = []
        files_in_turn = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tid = block.get("id")
            if tid and tid in tool_use_map:
                tc = tool_use_map[tid]
                tool_calls_out.append({
                    "tool": tc["name"],
                    "success": tc["success"],
                    "target": tc["target"],
                })
                if tc["target"] and _is_file_tool(tc["name"]):
                    files_in_turn.append(tc["target"])

        messages[mid] = {
            "usage": usage,
            "model": msg.get("model", ""),
            "timestamp": ts,
            "tool_calls": tool_calls_out,
            "files_touched": list(set(files_in_turn)),
        }

    # ── Sort by timestamp and build final turn list ──────────────────────────────
    turns = []
    for i, (mid, m) in enumerate(
        sorted(messages.items(), key=lambda x: x[1]["timestamp"])
    ):
        usage = m["usage"]
        ctx = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        turns.append({
            "turn_index": i,
            "message_id": mid,
            "timestamp": m["timestamp"],
            "model": m["model"],
            "input_tokens": usage.get("input_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "context_window_tokens": ctx,
            "tool_calls": m["tool_calls"],
            "files_touched": m["files_touched"],
        })
    return turns


def linear_slope(values: list) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values))
    den = sum((x - mean_x) ** 2 for x in xs)
    return num / den if den else 0.0


def fmt_tokens(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.0f}K"
    return str(n)


def _load_cached(turns_path: str):
    """Load cached context_turns JSON. Returns dict or None if stale/missing."""
    if not os.path.exists(turns_path):
        return None
    try:
        data = json.load(open(turns_path))
        # Old format was a plain list — treat as stale
        if isinstance(data, list):
            return None
        if not isinstance(data, dict) or "turns" not in data:
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def main():
    sessions = json.load(open(SESSIONS_IN))
    qualifying = [
        s for s in sessions
        if s["status"] == "active" and s.get("unique_messages", 0) >= MIN_MESSAGES
    ]
    print(f"Sessions with >= {MIN_MESSAGES} messages: {len(qualifying)}")

    os.makedirs(TURNS_DIR, exist_ok=True)

    all_session_stats = []

    for i, s in enumerate(qualifying):
        sid = s["session_id"]
        turns_path = os.path.join(TURNS_DIR, f"{sid}.json")

        # Try cache (new dict format only)
        cached = _load_cached(turns_path)
        if cached is not None:
            data = cached
            turns = data["turns"]
        else:
            filepath = s["file"]
            if not os.path.exists(filepath):
                continue
            turns = extract_turns(filepath)

            # Session-level aggregates
            total_loops = count_loops(turns)
            all_files = []
            for t in turns:
                all_files.extend(t.get("files_touched", []))
            all_files_touched = sorted(set(all_files))

            data = {
                "session_id": sid,
                "total_loops": total_loops,
                "all_files_touched": all_files_touched,
                "turns": turns,
            }
            with open(turns_path, "w") as f:
                json.dump(data, f)

        if not turns:
            continue

        ctxs = [t["context_window_tokens"] for t in turns]
        peak = max(ctxs)
        slope = linear_slope(ctxs)

        # Context jump analysis
        jumps = []
        for j in range(1, len(turns)):
            delta = turns[j]["context_window_tokens"] - turns[j-1]["context_window_tokens"]
            if delta > 0:
                tool_names = [tc["tool"] if isinstance(tc, dict) else tc
                              for tc in turns[j-1].get("tool_calls", [])]
                jumps.append({
                    "turn": j,
                    "delta": delta,
                    "tools": tool_names,
                })

        all_session_stats.append({
            "session_id": sid,
            "project": s["project_canonical"],
            "messages": len(turns),
            "peak_context": peak,
            "slope": slope,
            "final_context": ctxs[-1],
            "start_context": ctxs[0],
            "jumps": jumps,
        })

        pct = (i + 1) * 100 // len(qualifying)
        if (i + 1) % 20 == 0 or i == len(qualifying) - 1:
            print(f"  [{pct:3d}%] processed {i+1}/{len(qualifying)}")

    # Rankings
    by_peak  = sorted(all_session_stats, key=lambda x: -x["peak_context"])[:20]
    by_slope = sorted(all_session_stats, key=lambda x: -x["slope"])[:20]
    blowups  = [s for s in all_session_stats if s["peak_context"] > 500_000]

    tool_jump_totals = defaultdict(lambda: {"count": 0, "total_delta": 0})
    for stat in all_session_stats:
        for jump in stat["jumps"]:
            for tool in (jump["tools"] or ["(no tool)"]):
                tool_jump_totals[tool]["count"] += 1
                tool_jump_totals[tool]["total_delta"] += jump["delta"]

    lines = []
    lines.append("# Context Growth Analysis\n")
    lines.append(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_\n")
    lines.append(f"Sessions analysed (>= {MIN_MESSAGES} messages): **{len(all_session_stats)}**\n")

    lines.append("## Top 20 Sessions by Peak Context Window\n")
    lines.append("| Session | Project | Messages | Peak Context | Growth Slope |")
    lines.append("|---------|---------|----------|-------------|-------------|")
    for s in by_peak:
        lines.append(
            f"| `{s['session_id'][:8]}…` "
            f"| `{s['project']}` "
            f"| {s['messages']} "
            f"| {fmt_tokens(s['peak_context'])} "
            f"| +{fmt_tokens(int(s['slope']))}/turn |"
        )

    lines.append("\n## Top 20 Sessions by Context Growth Rate\n")
    lines.append("| Session | Project | Messages | Peak Context | Growth Slope |")
    lines.append("|---------|---------|----------|-------------|-------------|")
    for s in by_slope:
        lines.append(
            f"| `{s['session_id'][:8]}…` "
            f"| `{s['project']}` "
            f"| {s['messages']} "
            f"| {fmt_tokens(s['peak_context'])} "
            f"| +{fmt_tokens(int(s['slope']))}/turn |"
        )

    lines.append(f"\n## Blowup Sessions (peak context > 500K tokens)\n")
    if blowups:
        lines.append(f"**{len(blowups)} sessions** exceeded 500K context tokens.\n")
        lines.append("| Session | Project | Peak Context | Messages |")
        lines.append("|---------|---------|-------------|----------|")
        for s in sorted(blowups, key=lambda x: -x["peak_context"]):
            lines.append(
                f"| `{s['session_id'][:8]}…` "
                f"| `{s['project']}` "
                f"| {fmt_tokens(s['peak_context'])} "
                f"| {s['messages']} |"
            )
    else:
        lines.append("No sessions exceeded 500K context tokens.\n")

    lines.append("\n## Tool-Call Correlation with Context Growth\n")
    lines.append("Which tools most often precede large context window jumps.\n")
    lines.append("| Tool | Occurrences Before Jump | Avg Delta |")
    lines.append("|------|------------------------|-----------|")
    top_tools = sorted(tool_jump_totals.items(), key=lambda x: -x[1]["total_delta"])[:15]
    for tool, v in top_tools:
        avg = v["total_delta"] // v["count"] if v["count"] else 0
        lines.append(f"| {tool} | {v['count']} | +{fmt_tokens(avg)} |")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Written: {OUT}")

    if os.path.exists(STATE):
        with open(STATE) as f:
            state = json.load(f)
        state.setdefault("analyses", {})["context_growth"] = "complete"
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        with open(STATE, "w") as f:
            json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
