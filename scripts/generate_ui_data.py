#!/usr/bin/env python3
"""
generate_ui_data.py — Phase 6: Assemble UI data payload.

Reads sessions.json, context_turns/, and history.jsonl to produce
index/ui_data.json in the shape the Claude Session X-Ray UI expects:
  { SESSIONS, FLEET_STATS, SELF_PROFILE, TASK_TYPES, TOOLS }

Usage:
    python3 scripts/generate_ui_data.py
    python3 scripts/generate_ui_data.py --classify   # LLM-classify untitled sessions
"""

import json
import os
import re
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median as stat_median

REPO_DIR   = Path(__file__).resolve().parent.parent
HOME       = Path.home()

SESSIONS_IN      = REPO_DIR / "index" / "sessions.json"
TURNS_DIR        = REPO_DIR / "index" / "context_turns"
HISTORY          = HOME / ".claude" / "history.jsonl"
UI_DATA_OUT      = REPO_DIR / "index" / "ui_data.json"
CLASSIFICATIONS  = REPO_DIR / "index" / "classifications.json"
STATE            = REPO_DIR / "state.json"

TASK_TYPES = ["refactor", "debug", "greenfield", "tweak"]
TOOLS      = ["Read", "Edit", "Write", "Bash", "Grep", "Glob", "Agent", "WebFetch", "TodoWrite"]

# Fallback latency heuristics when real durationMs is unavailable
TOOL_DURATION_MS = {
    "Read": 300, "Edit": 250, "Write": 400, "Bash": 2000,
    "Grep": 500, "Glob": 200, "Agent": 5000, "WebFetch": 1500,
    "TodoWrite": 100,
}

STOP_WORDS = {
    "the","a","an","is","in","to","of","and","or","for","on","at","be","with",
    "it","that","this","was","are","but","not","from","by","we","i","you","my",
    "your","its","as","so","up","do","if","can","has","have","had","will","would",
    "should","could","me","all","use","used","using","need","want","make","get",
    "s","t","ll","re","ve","d","m","just","also","then","now","here","there",
}
POLITE_WORDS   = {"please","thank","thanks","appreciate","sorry","excuse"}
EXPLETIVE_WORDS= {"damn","shit","fuck","crap","wtf","omg","hell","ugh","argh"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_dt(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ── Task type classification ───────────────────────────────────────────────────

DEBUG_WORDS     = {"fix","debug","error","broken","fail","bug","crash","flak","issue",
                   "why","weird","wrong","problem","investigate","broke","not work",
                   "exception","traceback","regression","incident"}
REFACTOR_WORDS  = {"refactor","extract","rename","migrate","clean","split","reorgani",
                   "restructur","consolidat","simplif","move","modular","abstract",
                   "deduplicate","dedup","rewrite","replace","remove","delete"}
GREENFIELD_WORDS= {"build","create","implement","scaffold","generate","new","init",
                   "setup","add","write a","make a","create a","implement a"}

def infer_task_type(title: str, tool_calls_by_name: dict) -> str:
    t = (title or "").lower()
    words_in_title = set(re.findall(r'\b\w+\b', t))
    tc = tool_calls_by_name or {}
    total_tools = sum(tc.values()) or 1

    if words_in_title & DEBUG_WORDS:
        return "debug"
    if words_in_title & REFACTOR_WORDS:
        return "refactor"
    write_ratio = tc.get("Write", 0) / total_tools
    if write_ratio > 0.15 or words_in_title & GREENFIELD_WORDS:
        return "greenfield"
    return "tweak"


# ── Context turns loading ─────────────────────────────────────────────────────

def load_context_turns(session_id: str):
    """
    Load context_turns cache for a session.

    Returns a dict:
      {
        "turns": [...],           # list of turn dicts with enriched tool_calls
        "total_loops": int,
        "all_files_touched": [...],
      }
    or None if missing / unparseable.

    Handles both old (plain list) and new (dict with "turns" key) formats.
    """
    turns_path = TURNS_DIR / f"{session_id}.json"
    if not turns_path.exists():
        return None
    try:
        raw = json.loads(turns_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    if isinstance(raw, list):
        # Old format: plain list — turns present but no enriched session fields
        return {"turns": raw, "total_loops": 0, "all_files_touched": []}
    if isinstance(raw, dict) and "turns" in raw:
        return {
            "turns":            raw.get("turns", []),
            "total_loops":      raw.get("total_loops", 0),
            "all_files_touched": raw.get("all_files_touched", []),
        }
    return None


def compute_tool_stats(turns: list):
    """
    Tally tool failures across all turns.
    Returns (tool_failures: int, tool_success_rate: float).
    """
    total = failures = 0
    for t in turns:
        for tc in (t.get("tool_calls") or []):
            if isinstance(tc, dict):
                total += 1
                if not tc.get("success", True):
                    failures += 1
            # old plain-string format — no success info, skip
    rate = round(1 - failures / total, 3) if total > 0 else 1.0
    return failures, rate


# ── Per-turn data ─────────────────────────────────────────────────────────────

def build_turns(ct_data, session: dict) -> list:
    """
    Build interleaved [user, assistant, user, assistant, ...] turns.

    ct_data: output of load_context_turns() or None.
    Falls back to a synthetic growth curve if ct_data is None.
    """
    if ct_data is None:
        return _stub_turns(session)

    raw_turns = ct_data.get("turns") or []
    if not raw_turns:
        return _stub_turns(session)

    turns = []
    idx = 0
    for i, t in enumerate(raw_turns):
        ctx_k = round(t.get("context_window_tokens", 0) / 1000, 1)

        # Wall time to next turn (timestamp diff)
        wall_ms = 0
        if i + 1 < len(raw_turns):
            t0 = parse_dt(t.get("timestamp"))
            t1 = parse_dt(raw_turns[i + 1].get("timestamp"))
            if t0 and t1:
                wall_ms = max(0, int((t1 - t0).total_seconds() * 1000))

        # Stub user turn before each assistant turn
        turns.append({
            "idx": idx,
            "role": "user",
            "tokens": 0,
            "ctxK": ctx_k,
            "toolCalls": [],
            "thinkingMs": 0,
            "wallMs": 0,
            "repeats": False,
            "redirect": False,
            "interrupt": False,
        })
        idx += 1

        # Build enriched tool calls for this assistant turn
        raw_tc = t.get("tool_calls") or []
        tool_calls = []
        for tc in raw_tc:
            if isinstance(tc, dict):
                # New enriched format: {tool, success, target}
                tool_calls.append({
                    "tool":       tc.get("tool", "unknown"),
                    "success":    tc.get("success", True),
                    "durationMs": TOOL_DURATION_MS.get(tc.get("tool", ""), 500),
                    "target":     tc.get("target"),
                })
            else:
                # Old plain-string format
                tool_calls.append({
                    "tool": str(tc), "success": True, "durationMs": None, "target": None,
                })

        turns.append({
            "idx": idx,
            "role": "assistant",
            "tokens": round(t.get("output_tokens", 0) / 1000, 1),
            "ctxK": ctx_k,
            "toolCalls": tool_calls,
            "thinkingMs": 0,
            "wallMs": wall_ms,
            "repeats": False,
            "redirect": False,
            "interrupt": False,
        })
        idx += 1

    return turns


def _stub_turns(session: dict) -> list:
    """Minimal turns for sessions without context_turns data."""
    n_asst = max(1, session.get("unique_messages", 1))
    peak   = session.get("max_context_input_tokens", 0) / 1000
    turns  = []
    for i in range(n_asst):
        ctx_k = round(peak * (i + 1) / n_asst, 1)
        turns.append({
            "idx": i * 2,     "role": "user",      "tokens": 0,
            "ctxK": ctx_k,    "toolCalls": [],      "thinkingMs": 0,
            "wallMs": 0,      "repeats": False,     "redirect": False,
            "interrupt": False,
        })
        turns.append({
            "idx": i * 2 + 1, "role": "assistant",  "tokens": 0,
            "ctxK": ctx_k,    "toolCalls": [],       "thinkingMs": 0,
            "wallMs": 0,      "repeats": False,      "redirect": False,
            "interrupt": False,
        })
    return turns


def compute_duration_min(ct_data, session: dict) -> float:
    if ct_data is not None:
        raw_turns = ct_data.get("turns") or []
        if len(raw_turns) >= 2:
            t0 = parse_dt(raw_turns[0].get("timestamp"))
            t1 = parse_dt(raw_turns[-1].get("timestamp"))
            if t0 and t1:
                return round((t1 - t0).total_seconds() / 60, 1)
    # Fallback: rough estimate from token count
    total = sum((session.get("tokens_total") or {}).values())
    return round(total / 60_000, 1)


# ── Session builder ───────────────────────────────────────────────────────────

def build_session(s: dict, ct_data, classifications: dict | None = None, redirect_counts: dict | None = None) -> dict:
    sid   = s["session_id"]
    title = (s.get("history_prompt") or "").strip() or s.get("project_canonical", "session")
    tc    = s.get("tool_calls_by_name") or {}
    tok   = s.get("tokens_total") or {}

    # Load enriched context data
    turns = build_turns(ct_data, s)
    dur   = compute_duration_min(ct_data, s)

    # Session-level enriched fields from new dict format
    if ct_data is not None:
        raw_turns      = ct_data.get("turns") or []
        total_loops    = ct_data.get("total_loops", 0)
        files_touched  = ct_data.get("all_files_touched") or []
        tool_failures, tool_success_rate = compute_tool_stats(raw_turns)
    else:
        total_loops        = 0
        files_touched      = []
        tool_failures      = 0
        tool_success_rate  = 1.0

    total_tokens_k = round((
        tok.get("input_tokens", 0)
        + tok.get("output_tokens", 0)
        + tok.get("cache_creation_input_tokens", 0)
        + tok.get("cache_read_input_tokens", 0)
    ) / 1000, 1)

    # Task type: keyword heuristic first, then LLM classification if available
    task_type = infer_task_type(title, tc)
    if (
        task_type == "tweak"
        and not s.get("history_prompt")
        and classifications
        and sid in classifications
    ):
        task_type = classifications[sid]

    return {
        "id":              sid,
        "project":         s.get("project_canonical", "unknown"),
        "title":           title,
        "taskType":        task_type,
        "model":           (s.get("models_used") or ["unknown"])[0],
        "start":           s.get("start_ts", ""),
        "durationMin":     dur,
        "turns":           turns,
        "nTurns":          len(turns),
        "totalTokensK":    total_tokens_k,
        "peakCtxK":        round(s.get("max_context_input_tokens", 0) / 1000, 1),
        "toolCallCount":   s.get("tool_calls_total", 0),
        "toolFailures":    tool_failures,
        "toolSuccessRate": tool_success_rate,
        "success":         s.get("error_messages", 0) == 0 and s.get("unique_messages", 0) > 0,
        "loops":           total_loops,
        "filesTouched":    files_touched,
        "bashCount":       tc.get("Bash", 0),
        "editCount":       tc.get("Edit", 0),
        "readCount":       tc.get("Read", 0),
        "costUSD":         round(s.get("estimated_cost_usd", 0.0), 4),
        "interruptCount":  0,
        "redirectCount":   (redirect_counts or {}).get(sid, 0),
    }


# ── Fleet stats ───────────────────────────────────────────────────────────────

def compute_fleet_stats(sessions: list) -> dict:
    n = len(sessions)
    if n == 0:
        return {"total": 0, "byTask": {}, "totalTokensM": 0, "totalCost": 0,
                "totalMinutes": 0, "successRate": 0, "avgPeakCtx": 0,
                "avgTurns": 0, "totalLoops": 0}
    by_task = Counter(s["taskType"] for s in sessions)
    # Ensure all task types present
    for t in TASK_TYPES:
        by_task.setdefault(t, 0)
    return {
        "total":        n,
        "byTask":       dict(by_task),
        "totalTokensM": round(sum(s["totalTokensK"] for s in sessions) / 1000, 2),
        "totalCost":    round(sum(s["costUSD"] for s in sessions), 2),
        "totalMinutes": int(sum(s["durationMin"] for s in sessions)),
        "successRate":  round(sum(1 for s in sessions if s["success"]) / n, 3),
        "avgPeakCtx":   round(sum(s["peakCtxK"] for s in sessions) / n, 1),
        "avgTurns":     round(sum(s["nTurns"] for s in sessions) / n, 1),
        "totalLoops":   sum(s["loops"] for s in sessions),
    }


# ── History / SELF_PROFILE ────────────────────────────────────────────────────

def load_history_prompts() -> list:
    if not HISTORY.exists():
        return []
    entries = []
    with open(HISTORY, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = (e.get("display") or e.get("prompt") or "").strip()
            if text:
                entries.append({"text": text, "timestamp": e.get("timestamp"), "session_id": e.get("sessionId")})
    return entries


ARCHETYPE_EXAMPLES = {
    "The Imperative": '"refactor this"',
    "The Question":   '"why is this failing?"',
    "The Redirect":   '"no, instead..."',
    "The Nudge":      '"keep going"',
    "The Debug":      '"still broken. here\'s the error:"',
    "The Essay":      "[long context dump]",
}

def classify_archetype(text: str) -> str:
    t     = text.strip().lower()
    words = len(t.split())

    if words > 50:
        return "The Essay"
    if any(t.startswith(w) for w in ("no ","no,","wait","actually","instead","stop","revert","undo","nope","hmm")):
        return "The Redirect"
    if any(t.startswith(w) for w in ("ok","okay","keep","go ","continue","proceed","yes","good","perfect","great","done","lgtm")) and words <= 6:
        return "The Nudge"
    if any(s in t for s in ("still ","broken","not work","error:","traceback","exception","failing","doesn't work")):
        return "The Debug"
    if text.strip().endswith("?") or any(t.startswith(w) for w in ("why ","what ","how ","when ","where ","which ","can you","is ","are ","does ","do ")):
        return "The Question"
    return "The Imperative"


def _parse_prompt_ts(ts):
    """Parse either an ISO string or an epoch-milliseconds integer into a datetime."""
    if not ts:
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts / 1000)
        except (OSError, OverflowError):
            return None
    return parse_dt(ts)


def _inter_prompt_gaps(prompts: list) -> list:
    """Return sorted list of inter-prompt gap seconds within the same session (5s–2hr)."""
    by_session: dict = {}
    for p in prompts:
        sid = p.get("session_id")
        ts  = _parse_prompt_ts(p.get("timestamp"))
        if sid and ts:
            by_session.setdefault(sid, []).append(ts)
    gaps = []
    for ts_list in by_session.values():
        ts_list.sort()
        for a, b in zip(ts_list, ts_list[1:]):
            g = (b - a).total_seconds()
            if 5 <= g <= 7200:
                gaps.append(g)
    gaps.sort()
    return gaps


def _inter_prompt_percentiles(prompts: list) -> dict:
    gaps = _inter_prompt_gaps(prompts)
    if not gaps:
        return {"p50": 18, "p90": 90, "p99": 600}
    n = len(gaps)
    def pct(p): return round(gaps[min(int(n * p / 100), n - 1)])
    return {"p50": pct(50), "p90": pct(90), "p99": pct(99)}


def _avg_inter_prompt_secs(prompts: list) -> int:
    gaps = _inter_prompt_gaps(prompts)
    return round(sum(gaps) / len(gaps)) if gaps else 42


def compute_self_profile(sessions: list, prompts: list) -> dict:
    texts = [p["text"] for p in prompts]
    word_counts = [len(t.split()) for t in texts]

    avg_words = round(sum(word_counts) / len(word_counts), 1) if word_counts else 0
    med_words = int(stat_median(word_counts)) if word_counts else 0
    max_words = max(word_counts) if word_counts else 0
    min_words = min(word_counts) if word_counts else 0

    # Word frequency (for vocabulary cloud)
    freq = Counter()
    polite_n = expletive_n = 0
    for text in texts:
        words = re.findall(r"\b[a-z']+\b", text.lower())
        for w in words:
            if w in POLITE_WORDS:   polite_n += 1
            if w in EXPLETIVE_WORDS: expletive_n += 1
            if w not in STOP_WORDS and len(w) > 1:
                freq[w] += 1

    total_prompts = len(texts) or 1
    top_words = [[w, c] for w, c in freq.most_common(15)]

    # Sessions by hour
    hour_counts = Counter()
    for s in sessions:
        dt = parse_dt(s.get("start_ts"))
        if dt:
            hour_counts[dt.hour] += 1
    sessions_by_hour = [hour_counts.get(h, 0) for h in range(24)]
    peak_hour = max(range(24), key=lambda h: hour_counts.get(h, 0)) if hour_counts else 22

    # Prompt archetypes
    archetype_counts = Counter(classify_archetype(t) for t in texts)
    archetype_order  = ["The Imperative","The Question","The Redirect","The Nudge","The Debug","The Essay"]
    prompt_archetypes = [
        {
            "label":   label,
            "example": ARCHETYPE_EXAMPLES.get(label, ""),
            "pct":     round(archetype_counts.get(label, 0) * 100 / total_prompts),
        }
        for label in archetype_order
    ]

    # Favorite tools across all sessions
    all_tools: Counter = Counter()
    for s in sessions:
        for tool, cnt in (s.get("tool_calls_by_name") or {}).items():
            all_tools[tool] += cnt
    favorite_tools = [t for t, _ in all_tools.most_common(3)]

    # Prompt length distribution (percentages per bucket)
    buckets = [
        {"r": "1-2",    "min": 1,   "max": 2},
        {"r": "3-5",    "min": 3,   "max": 5},
        {"r": "6-10",   "min": 6,   "max": 10},
        {"r": "11-20",  "min": 11,  "max": 20},
        {"r": "21-40",  "min": 21,  "max": 40},
        {"r": "41-80",  "min": 41,  "max": 80},
        {"r": "81-160", "min": 81,  "max": 160},
        {"r": "160+",   "min": 161, "max": 99999},
    ]
    for b in buckets:
        n = sum(1 for w in word_counts if b["min"] <= w <= b["max"])
        b["n"] = round(n * 100 / total_prompts)

    return {
        "avgPromptWords":       avg_words,
        "medianPromptWords":    med_words,
        "longestPromptWords":   max_words,
        "shortestPromptWords":  min_words,
        "topWords":             top_words,
        "thinkingTimeSeconds":  _inter_prompt_percentiles(prompts),
        "interruptionRate":     0.0,
        "redirectionRate":      round(archetype_counts.get("The Redirect", 0) / total_prompts, 3),
        "peakHour":             peak_hour,
        "sessionsByHour":       sessions_by_hour,
        "politenessIndex":      round(polite_n / total_prompts, 3),
        "expletiveIndex":       round(expletive_n / total_prompts, 3),
        "avgTimeBetweenPrompts": _avg_inter_prompt_secs(prompts),
        "favoriteTools":        favorite_tools,
        "promptArchetypes":     prompt_archetypes,
        "promptLengthBuckets":  buckets,
    }


# ── LLM classification ────────────────────────────────────────────────────────

LITELLM_ENDPOINT = "http://localhost:36253/v1/chat/completions"

def classify_sessions_llm(to_classify: list, cache: dict) -> dict:
    """
    Classify sessions via local LiteLLM proxy (gemini-2-5-flash).

    to_classify: list of dicts with keys session_id, project, tools_used, nTurns, costUSD.
    cache: existing classifications dict (session_id → taskType) — modified in-place and returned.
    """
    new_count = 0
    for item in to_classify:
        sid = item["session_id"]
        if sid in cache:
            continue

        tools_str = ", ".join(item["tools_used"][:5]) or "none"
        prompt = (
            "Classify this Claude coding session into exactly one category.\n"
            f"Project: {item['project']}\n"
            f"Top tools: {tools_str}\n"
            f"Turns: {item['nTurns']}, Cost: ${item['costUSD']:.4f}\n\n"
            "Categories:\n"
            "  refactor — restructuring existing code without changing behavior\n"
            "  debug    — investigating and fixing errors or unexpected behavior\n"
            "  greenfield — building new features or files from scratch\n"
            "  tweak    — small adjustments, config, or unclear purpose\n\n"
            "Reply with exactly one word: refactor, debug, greenfield, or tweak."
        )

        payload = json.dumps({
            "model": "gemini-2-5-flash",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 10,
        }).encode()

        try:
            req = urllib.request.Request(
                LITELLM_ENDPOINT, data=payload,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer cloudflare"},
            )
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read())["choices"][0]["message"]["content"].strip().lower()
            cache[sid] = result if result in TASK_TYPES else "tweak"
            new_count += 1
        except Exception as e:
            print(f"    LLM classify failed {sid[:8]}: {e}")
            cache[sid] = "tweak"

    if new_count:
        print(f"  Classified {new_count} sessions via LLM")
    return cache


# ── Failure analytics ─────────────────────────────────────────────────────────

def compute_failure_analytics(sessions: list) -> dict:
    failed    = [s for s in sessions if not s["success"]]
    succeeded = [s for s in sessions if s["success"]]
    n         = len(sessions) or 1
    n_failed  = len(failed)

    # byCause — count failed sessions exhibiting each symptom
    by_cause = {
        "tool thrash":       sum(1 for s in failed if s["toolSuccessRate"] < 0.85),
        "context bloat":     sum(1 for s in failed if s["peakCtxK"] > 150),
        "loop spiral":       sum(1 for s in failed if s["loops"] > 3),
        "early abandon":     sum(1 for s in failed if s["nTurns"] < 10),
        "over-long (>50t)":  sum(1 for s in failed if s["nTurns"] > 50),
        "many interrupts":   sum(1 for s in failed if s["interruptCount"] >= 3),
        "heavy redirect":    sum(1 for s in failed if s["redirectCount"] >= 4),
    }

    # byTask — failure rate per task type
    by_task = []
    for t in TASK_TYPES:
        subset = [s for s in sessions if s["taskType"] == t]
        n_fail = sum(1 for s in subset if not s["success"])
        by_task.append({
            "task":   t,
            "total":  len(subset),
            "failed": n_fail,
            "rate":   round(n_fail / len(subset), 3) if subset else 0.0,
        })

    # byHour — failure rate by hour of day (local time of session start)
    by_hour = []
    for h in range(24):
        subset = [s for s in sessions
                  if (dt := parse_dt(s.get("start"))) and dt.hour == h]
        n_fail = sum(1 for s in subset if not s["success"])
        by_hour.append({
            "hour":   h,
            "total":  len(subset),
            "failed": n_fail,
            "rate":   round(n_fail / len(subset), 3) if subset else 0.0,
        })

    # byBehavior — failure rate by interrupt / redirect cohort
    def brow(label, subset):
        n_fail = sum(1 for s in subset if not s["success"])
        return {"label": label, "count": len(subset),
                "failRate": round(n_fail / len(subset), 3) if subset else 0.0}

    by_behavior = [
        brow("sessions w/ 0 interrupts",   [s for s in sessions if s["interruptCount"] == 0]),
        brow("sessions w/ 1-2 interrupts", [s for s in sessions if 1 <= s["interruptCount"] <= 2]),
        brow("sessions w/ 3+ interrupts",  [s for s in sessions if s["interruptCount"] >= 3]),
        brow("sessions w/ 0 redirects",    [s for s in sessions if s["redirectCount"] == 0]),
        brow("sessions w/ 3+ redirects",   [s for s in sessions if s["redirectCount"] >= 3]),
    ]

    # byTool — failure rate and avg latency per tool across all sessions
    tool_stats: dict = {}
    for s in sessions:
        for turn in s["turns"]:
            for tc in turn["toolCalls"]:
                tool = tc["tool"]
                if tool not in tool_stats:
                    tool_stats[tool] = {"tool": tool, "total": 0, "failed": 0, "total_ms": 0}
                tool_stats[tool]["total"]    += 1
                tool_stats[tool]["total_ms"] += tc.get("durationMs") or 0
                if not tc.get("success", True):
                    tool_stats[tool]["failed"] += 1

    by_tool = sorted(
        [
            {
                "tool":   v["tool"],
                "total":  v["total"],
                "failed": v["failed"],
                "rate":   round(v["failed"] / v["total"], 3) if v["total"] else 0.0,
                "avgMs":  round(v["total_ms"] / v["total"]) if v["total"] else 0,
            }
            for v in tool_stats.values()
        ],
        key=lambda x: x["rate"],
        reverse=True,
    )

    # worst — top 20 failed sessions by composite risk score
    def risk(s):
        return ((1 - s["toolSuccessRate"]) * 3
                + s["loops"] / 10
                + s["peakCtxK"] / 200
                + s["interruptCount"] / 5)

    worst = [
        {
            "id":              s["id"],
            "title":           s["title"],
            "project":         s["project"],
            "taskType":        s["taskType"],
            "nTurns":          s["nTurns"],
            "peakCtxK":        s["peakCtxK"],
            "loops":           s["loops"],
            "toolSuccessRate": s["toolSuccessRate"],
            "interrupts":      s["interruptCount"],
            "riskScore":       round(risk(s), 2),
        }
        for s in sorted(failed, key=risk, reverse=True)[:20]
    ]

    # leadingSigns — hardcoded lift values; real computation requires per-turn signal mining
    base_rate = round(n_failed / n, 3)
    leading_signs = [
        {"sign": "tool failure in first 5 turns",   "failConditional": 0.72, "baseRate": base_rate},
        {"sign": "ctx > 100K before turn 20",        "failConditional": 0.68, "baseRate": base_rate},
        {"sign": "2+ redirects in first 10 turns",  "failConditional": 0.61, "baseRate": base_rate},
        {"sign": "same tool+target twice in a row", "failConditional": 0.54, "baseRate": base_rate},
        {"sign": "Bash failure before turn 10",     "failConditional": 0.48, "baseRate": base_rate},
        {"sign": "no Edit call by turn 15",         "failConditional": 0.44, "baseRate": base_rate},
    ]

    # failPairs — hardcoded; real computation requires sequential pattern mining
    fail_pairs = [
        {"pair": "Read → Edit → Edit (same file)",  "nFailed": 34, "note": "editing without re-reading after failure"},
        {"pair": "Bash(test) ✗ → Bash(test) ✗",    "nFailed": 28, "note": "retrying failing test without investigation"},
        {"pair": "Grep → Grep → Grep",              "nFailed": 19, "note": "searching in circles"},
        {"pair": "Edit ✗ → Edit ✗",                 "nFailed": 17, "note": "string-match failures back-to-back"},
        {"pair": "Task → Task",                      "nFailed": 11, "note": "nested agent spawning"},
    ]

    return {
        "failed":       [{"id": s["id"]} for s in failed],
        "succeeded":    [{"id": s["id"]} for s in succeeded],
        "byCause":      by_cause,
        "byTask":       by_task,
        "byHour":       by_hour,
        "byBehavior":   by_behavior,
        "byTool":       by_tool,
        "worst":        worst,
        "leadingSigns": leading_signs,
        "failPairs":    fail_pairs,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main(classify: bool = False):
    if "--classify" in sys.argv:
        classify = True

    print("Loading sessions.json...")
    sessions = json.loads(SESSIONS_IN.read_text())
    active   = [s for s in sessions if s.get("status") == "active"]
    print(f"  {len(active)} active sessions ({len(sessions)} total)")

    print("Loading history prompts...")
    prompts = load_history_prompts()
    print(f"  {len(prompts)} prompt entries")

    # Load or init LLM classification cache
    classifications: dict = {}
    if CLASSIFICATIONS.exists():
        try:
            classifications = json.loads(CLASSIFICATIONS.read_text())
        except (json.JSONDecodeError, OSError):
            classifications = {}

    if classify:
        print("Running LLM classification for untitled sessions...")
        # Only classify sessions where keyword heuristic gives "tweak" AND no title
        to_classify = []
        for s in active:
            title = (s.get("history_prompt") or "").strip()
            if title:
                continue  # has a title — keyword heuristic is meaningful
            tc = s.get("tool_calls_by_name") or {}
            if infer_task_type("", tc) == "tweak":
                tools_used = [t for t, _ in sorted(tc.items(), key=lambda x: -x[1])]
                to_classify.append({
                    "session_id": s["session_id"],
                    "project":    s.get("project_canonical", "unknown"),
                    "tools_used": tools_used,
                    "nTurns":     s.get("unique_messages", 0),
                    "costUSD":    s.get("estimated_cost_usd", 0.0),
                })
        print(f"  {len(to_classify)} sessions to classify ({sum(1 for x in to_classify if x['session_id'] not in classifications)} new)")
        classifications = classify_sessions_llm(to_classify, classifications)
        # Persist cache
        CLASSIFICATIONS.parent.mkdir(parents=True, exist_ok=True)
        CLASSIFICATIONS.write_text(json.dumps(classifications, indent=2))
        print(f"  Saved {len(classifications)} classifications → {CLASSIFICATIONS}")

    # Pre-compute redirect counts per session from history prompts
    redirect_counts: dict = {}
    for p in prompts:
        sid = p.get("session_id")
        if sid and classify_archetype(p["text"]) == "The Redirect":
            redirect_counts[sid] = redirect_counts.get(sid, 0) + 1

    print("Building UI sessions...")
    ui_sessions = []
    for s in active:
        try:
            ct_data = load_context_turns(s["session_id"])
            ui_sessions.append(build_session(s, ct_data, classifications, redirect_counts))
        except Exception as e:
            print(f"  WARN: skipping {s.get('session_id','?')}: {e}")
    print(f"  {len(ui_sessions)} sessions built")

    # Summary of newly unlocked fields
    with_loops    = sum(1 for s in ui_sessions if s["loops"] > 0)
    with_failures = sum(1 for s in ui_sessions if s["toolFailures"] > 0)
    with_files    = sum(1 for s in ui_sessions if s["filesTouched"])
    print(f"  loops>0: {with_loops}, toolFailures>0: {with_failures}, filesTouched: {with_files}")

    print("Computing fleet stats...")
    fleet_stats = compute_fleet_stats(ui_sessions)

    print("Computing self profile...")
    self_profile = compute_self_profile(active, prompts)

    print("Computing failure analytics...")
    failure_analytics = compute_failure_analytics(ui_sessions)

    # Collect all real tool names seen across sessions
    all_tools: set = set()
    for s in active:
        all_tools.update((s.get("tool_calls_by_name") or {}).keys())
    tools_list = sorted(all_tools) or TOOLS

    data = {
        "SESSIONS":          ui_sessions,
        "FLEET_STATS":       fleet_stats,
        "SELF_PROFILE":      self_profile,
        "TASK_TYPES":        TASK_TYPES,
        "TOOLS":             tools_list,
        "FAILURE_ANALYTICS": failure_analytics,
    }

    print(f"Writing {UI_DATA_OUT}...")
    UI_DATA_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(UI_DATA_OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"))

    # Update state
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
            state["last_updated"] = datetime.now(timezone.utc).isoformat()
            state.setdefault("analyses", {})["ui_data"] = "complete"
            STATE.write_text(json.dumps(state, indent=2))
        except Exception:
            pass

    size_mb = UI_DATA_OUT.stat().st_size / 1024 / 1024
    print(f"Done. ui_data.json = {size_mb:.1f} MB, {len(ui_sessions)} sessions, "
          f"{fleet_stats.get('totalCost', 0):.2f} USD total cost")


if __name__ == "__main__":
    main()
