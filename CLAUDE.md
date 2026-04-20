# Claude Session Analysis Agent

This repo is a self-contained analysis environment for studying Claude Code session data —
specifically **token usage, context growth, cache efficiency, and cost patterns** across
all sessions on this machine.

## Goal

Answer questions like:
- Which projects consumed the most tokens?
- How efficiently is the prompt cache being used?
- Which sessions grew the largest context windows?
- How does token usage break down by model (sonnet/haiku/opus) and by day?
- What tool call patterns drive context bloat?

---

## Data Sources

All raw data lives in `~/.claude/`. Do not modify those files — read only.

| Source | Path | What it contains |
|--------|------|-----------------|
| Aggregate stats | `~/.claude/stats-cache.json` | Daily token counts by model, session totals, message counts |
| Session logs | `~/.claude/projects/<project>/*.jsonl` | Full conversation history; **assistant messages contain `usage` fields** with per-turn token counts |
| Transcripts | `~/.claude/transcripts/ses_*.jsonl` | Tool-level session events (type: user/tool_use/tool_result) |
| Command history | `~/.claude/history.jsonl` | Every user prompt with timestamp, project path, sessionId |
| Session metadata | `~/.claude/sessions/*.json` | pid, cwd, startedAt, entrypoint for active/recent sessions |

### Key schema — assistant message usage block (inside project JSONL files)
```json
{
  "type": "assistant",
  "message": {
    "model": "claude-sonnet-4-6",
    "usage": {
      "input_tokens": 3,
      "cache_creation_input_tokens": 35263,
      "cache_read_input_tokens": 482109,
      "output_tokens": 84
    }
  },
  "sessionId": "...",
  "cwd": "/Users/vinayak.kaushik/...",
  "timestamp": "2026-04-07T..."
}
```

`cwd` on each record tells you which project directory the session was in.

---

## Self-Organization

The agent maintains its own working state in this directory. **Always check these files
at the start of a session before re-doing work.**

```
claudeAnalysis/
├── CLAUDE.md                  # This file — always loaded
├── state.json                 # Tracks what has been indexed/analyzed
├── index/
│   ├── sessions.json          # One entry per discovered session file
│   └── projects.json          # Per-project token aggregates
└── findings/
    ├── overview.md            # High-level summary written after full analysis
    ├── by_project.md          # Token breakdown ranked by project
    ├── by_day.md              # Daily usage trends
    ├── cache_efficiency.md    # Cache read vs creation ratios
    └── context_growth.md      # Sessions with largest context windows
```

### state.json schema
```json
{
  "last_updated": "ISO timestamp",
  "indexing": {
    "status": "pending | in_progress | complete",
    "files_processed": 42,
    "files_total": 54,
    "last_file": "path/to/last/processed.jsonl"
  },
  "analyses": {
    "overview": "pending | complete",
    "by_project": "pending | complete",
    "by_day": "pending | complete",
    "cache_efficiency": "pending | complete",
    "context_growth": "pending | complete"
  }
}
```

### sessions.json schema (one object per session file)
```json
[
  {
    "file": "~/.claude/projects/-Users-.../abc123.jsonl",
    "session_id": "abc123",
    "project_dir": "/Users/vinayak.kaushik/myproject",
    "project_key": "-Users-vinayak-kaushik-myproject",
    "start_ts": "2026-03-25T20:37:59Z",
    "end_ts": "2026-03-25T22:10:00Z",
    "model": "claude-sonnet-4-6",
    "turns": 24,
    "total_input_tokens": 12400,
    "total_output_tokens": 3200,
    "total_cache_creation": 45000,
    "total_cache_read": 380000,
    "tool_calls": 18
  }
]
```

---

## Workflow

When starting a new analysis session, follow this sequence:

1. **Read `state.json`** to understand what work is already done.
2. **Index first** if `indexing.status != "complete"`: scan all project JSONL files,
   extract session-level token totals, write to `index/sessions.json` and `index/projects.json`.
3. **Run analyses** in any order, updating `state.json` as each completes.
4. **Write findings** to `findings/` as markdown — human-readable with tables and charts where possible.
5. **Update `state.json`** with `last_updated` timestamp after every write.

Use Python for all data processing (stdlib only — `json`, `glob`, `os`, `datetime`, `collections`).
Write scripts to `scripts/` if they are reusable; inline one-off analysis in Bash python3 -c.

---

## Useful Quick Commands

```bash
# Count total project session files
find ~/.claude/projects -name "*.jsonl" | wc -l

# Check aggregate stats at a glance
python3 -c "import json; d=json.load(open(os.path.expanduser('~/.claude/stats-cache.json'))); print(json.dumps(d['modelUsage'], indent=2))"

# Find the largest session files (likely highest context usage)
find ~/.claude/projects -name "*.jsonl" -exec wc -l {} + | sort -rn | head -20
```

---

## Notes

- `stats-cache.json` only covers up to `lastComputedDate` (currently 2026-04-15) — gaps after
  that date must be computed from raw JSONL files.
- Some sessions will have `msg_vrtx_` prefixed message IDs — these are Vertex AI routed calls,
  not direct Anthropic API. Token counting is identical.
- `isSidechain: true` records are subagent turns spawned by the main session — count them
  separately when analyzing multi-agent overhead.
- The `cwd` field on JSONL records is the ground truth for project attribution, not the
  directory path of the file itself.
