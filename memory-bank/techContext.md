# Tech Context

## Version Control

This repo uses **jujutsu (jj)** for version control, not git. Always use `jj` commands.

## Data Sources

| Source | Path | Notes |
|--------|------|-------|
| Session JSONL files | `~/.claude/projects/*/*.jsonl` | Non-recursive glob; one file = one session; session_id = filename stem (UUID) |
| Aggregate stats | `~/.claude/stats-cache.json` | Cutoff: 2026-04-15; gaps after that date must come from raw JSONL |
| Command history | `~/.claude/history.jsonl` | Only 43% of sessions have a matching entry here |

## Verified Data Facts (as of 2026-04-19)

- 547 non-subagent JSONL files, 326 MB
- 473 active sessions, 52 empty (no assistant records), 22 error-only (synthetic model)
- Zero `isSidechain=true` records in current dataset
- Zero cross-file message ID overlaps
- 222 sessions with context_turns traces (MIN_MESSAGES=2); 74 with loops, 137 with tool failures, 183 with files touched

## Usage Object Schema

```json
{
  "input_tokens": 3,
  "cache_creation_input_tokens": 35263,
  "cache_read_input_tokens": 482109,
  "output_tokens": 84,
  "cache_creation": {
    "ephemeral_5m_input_tokens": 0,
    "ephemeral_1h_input_tokens": 35263
  },
  "server_tool_use": {
    "web_search_requests": 0,
    "web_fetch_requests": 0
  }
}
```

`cache_creation` is a nested dict — not flat on the usage object. `server_tool_use` is also nested, not top-level.

## JS Debugging

`python3 playwright` (sync API) is available in the environment and is the right tool for capturing browser JS errors from the terminal. Use `page.on("pageerror", ...)` and `page.on("console", ...)` to collect all errors without needing to open DevTools manually. `harness.py serve` also injects a `window.onerror` overlay that POSTs JS errors to `/__debug/js-error` and prints them in the terminal.

## Haiku Tokens

~90M haiku tokens appear in `stats-cache.json` but are completely absent from JSONL files. These come from internal Claude Code processes, not project API calls. Do not expect to account for them via JSONL analysis.

## Cost Estimates (approximate)

Total estimated spend: **$1,179** (2026-03-25 → 2026-04-19). Claude Code may have different negotiated rates, so treat these as approximations.
