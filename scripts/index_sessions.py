#!/usr/bin/env python3
"""
index_sessions.py — Phase 2 indexer.

Scans all ~/.claude/projects/*/*.jsonl files, extracts per-session token
summaries, and writes index/sessions.json. Supports incremental re-runs:
only re-processes files whose mtime or size has changed since the last run.

Usage:
    python3 scripts/index_sessions.py [--force]

    --force   Re-process all files regardless of mtime/size changes.
"""

import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

# ── Paths ────────────────────────────────────────────────────────────────────

REPO_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME        = os.path.expanduser("~")
PROJECTS    = os.path.join(HOME, ".claude", "projects")
PRICING     = os.path.join(REPO_DIR, "scripts", "pricing.json")
STATE       = os.path.join(REPO_DIR, "state.json")
INDEX_DIR   = os.path.join(REPO_DIR, "index")
SESSIONS_OUT = os.path.join(INDEX_DIR, "sessions.json")

# ── Pricing ───────────────────────────────────────────────────────────────────

def load_pricing() -> dict:
    with open(PRICING) as f:
        return json.load(f)


def compute_message_cost(usage: dict, model: str, pricing: dict) -> float:
    """Return estimated USD cost for one deduplicated API call."""
    p = pricing.get(model)
    if not p:
        return 0.0

    M = 1_000_000

    cost = 0.0
    cost += usage.get("input_tokens", 0) / M * p["input"]
    cost += usage.get("output_tokens", 0) / M * p["output"]
    cost += usage.get("cache_read_input_tokens", 0) / M * p["cache_read"]

    # Prefer sub-field tier breakdown; fall back to flat field
    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, dict) and cache_creation:
        cost += cache_creation.get("ephemeral_5m_input_tokens", 0) / M * p["cache_creation_5m"]
        cost += cache_creation.get("ephemeral_1h_input_tokens", 0) / M * p["cache_creation_1h"]
    else:
        cost += usage.get("cache_creation_input_tokens", 0) / M * p["cache_creation_5m"]

    return cost


# ── JSONL parsing ─────────────────────────────────────────────────────────────

def parse_jsonl(filepath: str):
    """Yield parsed records from a JSONL file, skipping malformed lines."""
    errors = 0
    with open(filepath, encoding="utf-8", errors="replace") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                errors += 1
                if errors <= 3:
                    print(f"  WARNING {os.path.basename(filepath)}:{line_num}: malformed JSON, skipping",
                          file=sys.stderr)
    if errors > 3:
        print(f"  WARNING {os.path.basename(filepath)}: {errors} total malformed lines skipped",
              file=sys.stderr)


# ── Project attribution ───────────────────────────────────────────────────────

_DEVELOPER = os.path.join(HOME, "Developer")
_CONFIG    = os.path.join(HOME, ".config")
_DOT_CLAUDE = os.path.join(HOME, ".claude")


def normalize_cwd_to_project(cwd: str) -> tuple[str, str]:
    """
    Map a raw cwd to (canonical_project_name, category).

    Categories: project | config | meta | home-directory | unknown
    """
    if not cwd:
        return ("unknown", "unknown")

    # 1. In-project worktree: <project>/.claude/worktrees/<name>
    cwd = re.sub(r"/\.claude/worktrees/[^/]+$", "", cwd)

    # 2. Home-level claude-squad worktrees (1 or 2 path segments after worktrees/)
    #    e.g. ~/.claude-squad/worktrees/vinayak.kaushik/branch-hash
    #    e.g. ~/.claude-squad/worktrees/branchname
    cwd = re.sub(r"/\.claude-squad/worktrees/(?:[^/]+/)?[^/]+$", "", cwd)

    # Classify the normalised path
    if cwd == HOME:
        return ("home-directory", "home-directory")

    if cwd == _DOT_CLAUDE:
        return ("claude-config", "meta")

    if "/.claude/projects/" in cwd:
        return ("claude-projects-meta", "meta")

    if cwd == _DEVELOPER:
        return ("developer-root", "meta")

    if cwd.startswith(_DEVELOPER + "/"):
        name = cwd[len(_DEVELOPER) + 1:]
        return (name, "project")

    if cwd.startswith(_CONFIG + "/"):
        name = cwd[len(_CONFIG) + 1:]
        return (name, "config")

    # Fallback: use full path
    return (cwd, "unknown")


# ── Per-file extraction ───────────────────────────────────────────────────────

def extract_session(filepath: str, pricing: dict) -> dict:
    """
    Parse one JSONL file and return a session index entry.

    Dedup strategy: one API call = N records (one per content block), all with
    identical usage fields. Group by message.id, count usage once (first seen),
    collect content blocks from all records for that message.
    """
    # message_id -> {usage, model, stop_reason, timestamp, cwd, content_blocks, is_error}
    messages = {}
    first_cwd = None
    first_ts = None
    last_ts = None
    user_turns = 0
    git_branch = None

    for record in parse_jsonl(filepath):
        rtype = record.get("type")

        # Track timestamps and cwd from any record
        ts = record.get("timestamp")
        if ts:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts

        cwd = record.get("cwd")
        if cwd and first_cwd is None:
            first_cwd = cwd

        if git_branch is None:
            git_branch = record.get("gitBranch")

        if rtype == "user":
            user_turns += 1
            continue

        if rtype != "assistant":
            continue

        msg = record.get("message", {})
        mid = msg.get("id")
        if not mid:
            continue

        model = msg.get("model", "")
        is_error = record.get("isApiErrorMessage", False) or model == "<synthetic>"
        usage = msg.get("usage", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            content = []

        if mid not in messages:
            messages[mid] = {
                "usage": usage,
                "model": model,
                "stop_reason": msg.get("stop_reason"),
                "timestamp": record.get("timestamp"),
                "cwd": cwd,
                "content_blocks": list(content),
                "is_error": is_error,
            }
        else:
            # Additional records for the same message: accumulate content blocks only
            messages[mid]["content_blocks"].extend(content)

    # ── Aggregate across deduplicated messages ────────────────────────────────

    tokens_by_model = defaultdict(lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_ephemeral_5m": 0,
        "cache_creation_ephemeral_1h": 0,
        "web_search_requests": 0,
        "web_fetch_requests": 0,
    })
    tool_calls_by_name = defaultdict(int)
    total_cost = 0.0
    error_messages = 0
    max_context = 0
    active_message_count = 0

    for mid, m in messages.items():
        if m["is_error"]:
            error_messages += 1
            continue

        active_message_count += 1
        model = m["model"]
        usage = m["usage"]
        t = tokens_by_model[model]

        t["input_tokens"]                += usage.get("input_tokens", 0)
        t["output_tokens"]               += usage.get("output_tokens", 0)
        t["cache_creation_input_tokens"] += usage.get("cache_creation_input_tokens", 0)
        t["cache_read_input_tokens"]     += usage.get("cache_read_input_tokens", 0)

        cc = usage.get("cache_creation")
        if isinstance(cc, dict) and cc:
            t["cache_creation_ephemeral_5m"] += cc.get("ephemeral_5m_input_tokens", 0)
            t["cache_creation_ephemeral_1h"] += cc.get("ephemeral_1h_input_tokens", 0)

        stu = usage.get("server_tool_use", {})
        t["web_search_requests"] += stu.get("web_search_requests", 0)
        t["web_fetch_requests"]  += stu.get("web_fetch_requests", 0)

        # Peak context window for this message
        ctx = (usage.get("input_tokens", 0)
               + usage.get("cache_read_input_tokens", 0)
               + usage.get("cache_creation_input_tokens", 0))
        if ctx > max_context:
            max_context = ctx

        # Tool calls
        for block in m["content_blocks"]:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name", "unknown")
                tool_calls_by_name[name] += 1

        # Cost
        total_cost += compute_message_cost(usage, model, pricing)

    # ── Totals ────────────────────────────────────────────────────────────────

    tokens_total = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_ephemeral_5m": 0,
        "cache_creation_ephemeral_1h": 0,
        "web_search_requests": 0,
        "web_fetch_requests": 0,
    }
    for t in tokens_by_model.values():
        for k in tokens_total:
            tokens_total[k] += t[k]

    # ── Status ────────────────────────────────────────────────────────────────

    if active_message_count == 0 and error_messages == 0:
        status = "empty"
    elif active_message_count == 0:
        status = "error_only"
    else:
        status = "active"

    # ── Project attribution ───────────────────────────────────────────────────

    project_canonical, project_category = normalize_cwd_to_project(first_cwd or "")

    # ── Build entry ───────────────────────────────────────────────────────────

    session_id = os.path.splitext(os.path.basename(filepath))[0]
    stat = os.stat(filepath)

    return {
        "file": filepath,
        "file_size_bytes": stat.st_size,
        "file_mtime_epoch": int(stat.st_mtime),
        "session_id": session_id,
        "cwd_raw": first_cwd,
        "project_canonical": project_canonical,
        "project_category": project_category,
        "git_branch": git_branch,
        "status": status,
        "start_ts": first_ts,
        "end_ts": last_ts,
        "unique_messages": active_message_count,
        "error_messages": error_messages,
        "total_records": len(messages),
        "user_turns": user_turns,
        "models_used": sorted(tokens_by_model.keys()),
        "tokens_by_model": {k: dict(v) for k, v in tokens_by_model.items()},
        "tokens_total": tokens_total,
        "estimated_cost_usd": round(total_cost, 6),
        "tool_calls_by_name": dict(tool_calls_by_name),
        "tool_calls_total": sum(tool_calls_by_name.values()),
        "max_context_input_tokens": max_context,
        "history_prompt": None,
        "history_timestamp": None,
    }


# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE, "w") as f:
        json.dump(state, f, indent=2)


def load_sessions() -> list:
    if os.path.exists(SESSIONS_OUT):
        with open(SESSIONS_OUT) as f:
            return json.load(f)
    return []


def save_sessions(sessions: list):
    os.makedirs(INDEX_DIR, exist_ok=True)
    with open(SESSIONS_OUT, "w") as f:
        json.dump(sessions, f, indent=2)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    force = "--force" in sys.argv

    pricing = load_pricing()
    state = load_state()
    manifest = state.get("file_manifest", {})

    # Discover all session JSONL files (non-recursive: only one level under projects/)
    pattern = os.path.join(PROJECTS, "*", "*.jsonl")
    all_files = sorted(glob.glob(pattern))
    total = len(all_files)
    print(f"Discovered {total} JSONL files")

    # Determine which files need processing
    to_process = []
    for fp in all_files:
        try:
            stat = os.stat(fp)
        except OSError:
            continue
        size = stat.st_size
        mtime = int(stat.st_mtime)
        prev = manifest.get(fp)
        if force or prev is None or prev["size_bytes"] != size or prev["mtime_epoch"] != mtime:
            to_process.append(fp)

    print(f"Files to process: {len(to_process)} {'(forced full re-index)' if force else '(new/changed)'}")

    if not to_process:
        print("Nothing to do — index is up to date.")
        return

    # Load existing sessions index; build lookup by file path for updates
    sessions = load_sessions()
    sessions_by_file = {s["file"]: i for i, s in enumerate(sessions)}

    processed = 0
    empty = 0
    errors = 0

    for fp in to_process:
        processed += 1
        pct = processed * 100 // len(to_process)
        print(f"  [{pct:3d}%] {os.path.basename(os.path.dirname(fp))}/{os.path.basename(fp)}", end="")

        try:
            entry = extract_session(fp, pricing)
        except Exception as e:
            print(f" ERROR: {e}", file=sys.stderr)
            errors += 1
            continue

        status_tag = f" [{entry['status']}]" if entry["status"] != "active" else ""
        print(f"  {entry['unique_messages']} msgs  ${entry['estimated_cost_usd']:.4f}{status_tag}")

        if entry["status"] == "empty":
            empty += 1

        # Update or append in sessions list
        if fp in sessions_by_file:
            sessions[sessions_by_file[fp]] = entry
        else:
            sessions_by_file[fp] = len(sessions)
            sessions.append(entry)

        # Update manifest
        manifest[fp] = {
            "size_bytes": entry["file_size_bytes"],
            "mtime_epoch": entry["file_mtime_epoch"],
            "status": entry["status"],
        }

    # Save
    save_sessions(sessions)

    active_total = sum(1 for s in sessions if s["status"] == "active")
    state["indexing"] = {
        "status": "complete",
        "files_processed": processed,
        "files_total": total,
        "files_empty": empty,
        "last_run": datetime.now(timezone.utc).isoformat(),
    }
    state["file_manifest"] = manifest
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    print(f"\nDone. {active_total} active sessions, {empty} empty, {errors} errors.")
    print(f"Output: {SESSIONS_OUT}")


if __name__ == "__main__":
    main()
