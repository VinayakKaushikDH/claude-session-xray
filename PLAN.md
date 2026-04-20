# Claude Session Analysis — Implementation Plan

Status legend: `[ ]` pending · `[~]` in progress · `[x]` done

---

## Phase 1 — Infrastructure Setup

- [x] **1.1** Create `scripts/pricing.json` — per-model token pricing table (sonnet, opus, haiku, synthetic)
- [x] **1.2** Update `state.json` schema to include `file_manifest`, incremental indexing fields, and `cost_report` analysis slot

---

## Phase 2 — Indexer (`scripts/index_sessions.py`)

Core single-pass scanner. Reads all `~/.claude/projects/*/*.jsonl`, writes `index/sessions.json` and updates `state.json`.

### 2.1 JSONL parsing utilities
- [x] `parse_jsonl(filepath)` — line-by-line parser with per-line JSON error recovery (skip + warn, max 3 warnings then summarize)
- [x] `normalize_cwd_to_project(cwd)` — worktree path normalization:
  - Strip `/.claude/worktrees/<name>` suffix (in-project worktrees)
  - Strip `/.claude-squad/worktrees/<1-or-2-segments>` (home-level worktrees)
  - Classify result: `project` / `config` / `meta` / `home-directory`
- [x] `compute_message_cost(usage, model, pricing)` — USD cost for one deduplicated message

### 2.2 Per-file extraction
- [x] Discover all `~/.claude/projects/*/*.jsonl` files (non-recursive — no subagent dirs exist)
- [x] For each file, group records by `message.id` (dedup: one API call = N records, one per content block, all with identical usage — take first, collect all content blocks)
- [x] Skip records with `model == "<synthetic>"` or `isApiErrorMessage == true` from token totals; count them separately as `error_messages`
- [x] Accumulate `tokens_by_model` dict keyed by model name
- [x] Count tool calls by name from `tool_use` content blocks across all records for each message
- [x] Extract `web_search_requests` / `web_fetch_requests` from `usage.server_tool_use` (not top-level)
- [x] Extract cache tier breakdown from `usage.cache_creation.ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`; fall back to flat `cache_creation_input_tokens` if sub-fields absent
- [x] Set `status`: `"active"` / `"error_only"` / `"empty"` based on assistant record presence
- [x] Compute `max_context_input_tokens` = max over all messages of `(input + cache_read + cache_creation)`
- [x] Compute `estimated_cost_usd` by summing `compute_message_cost()` for all deduplicated messages

### 2.3 Sessions index output
- [x] Write each file's summary as one entry in `index/sessions.json`
- [x] Track `file_size_bytes` and `file_mtime_epoch` in `state.json:file_manifest` for incremental re-runs
- [x] Mark `status` in manifest: `"active"` / `"error_only"` / `"empty"`

### 2.4 Incremental support
- [x] On subsequent runs, compare current mtime/size against manifest; only re-process changed or new files
- [x] Replace (not append) the existing entry in `sessions.json` for re-processed files

---

## Phase 3 — Aggregator (`scripts/aggregate_projects.py`)

Reads `index/sessions.json`, writes `index/projects.json`.

- [x] Group sessions by `project_canonical`
- [x] Sum all token fields, cost, tool calls, user turns
- [x] Compute `date_range` = `[min(start_ts.date), max(end_ts.date)]`
- [x] Exclude `status == "empty"` sessions from active counts and token sums
- [x] Write `index/projects.json`
- [x] Update `state.json:last_updated`

---

## Phase 4 — Enrichment (`scripts/enrich_index.py`) — optional, additive

- [x] Load `~/.claude/history.jsonl` (1,745 entries)
- [x] Match each session by `sessionId` → populate `history_prompt` and `history_timestamp`
- [x] ~44% match rate expected; null fields for unmatched sessions — analyses must never require these fields
- [x] Update entries in `sessions.json` in place

---

## Phase 5 — Analysis Scripts

Each script reads from `index/sessions.json` + `index/projects.json` and writes one findings file. All are independent and can run in any order.

### 5.1 `scripts/analyze_by_project.py` → `findings/by_project.md`
- [x] Ranked table: project, category, sessions, output tokens, cache_read tokens, estimated cost USD, date range
- [x] Per-project model breakdown (sonnet vs opus share)
- [x] Note on `home-directory` bucket explaining it contains claude-squad worktree sessions with unknowable parent repos

### 5.2 `scripts/analyze_by_day.py` → `findings/by_day.md`
- [x] Bucket each message's timestamp to date (use `start_ts` from session entry)
- [x] Table: date, sessions, messages, output tokens, cache_read, cache_creation, estimated cost
- [x] Note coverage gap: `stats-cache.json` ends at 2026-04-15; JSONL covers full range

### 5.3 `scripts/analyze_cache_efficiency.py` → `findings/cache_efficiency.md`
- [x] Per-session `cache_hit_ratio = cache_read / (cache_read + cache_creation + input_tokens)`
- [x] Ephemeral tier breakdown: 5-minute vs 1-hour share of cache creation
- [x] "Worst cache" sessions: lowest ratio among sessions with > 100K total tokens
- [x] Cost impact table: actual cost vs hypothetical cost with no caching

### 5.4 `scripts/analyze_context_growth.py` → `findings/context_growth.md`
- [x] Requires second pass over raw JSONL for sessions with ≥ 10 unique messages (151 sessions)
- [x] Per-turn data: `context_window_tokens = input + cache_read + cache_creation` at each message
- [x] Write per-session turn data to `index/context_turns/<session_id>.json`
- [x] Top 20 sessions by peak context window
- [x] Top 20 sessions by growth rate (linear slope of context_window_tokens vs turn_index)
- [x] Tool-call correlation: which tools precede the largest context jumps

### 5.5 `scripts/analyze_cost.py` → `findings/cost_report.md`
- [x] Total spend breakdown by model
- [x] Daily cost trend
- [x] Per-project cost ranking with cache savings estimate (actual vs no-cache hypothetical)
- [x] Most expensive individual sessions (top 10)
- [x] Cost per user turn (mean, median, p95)

### 5.6 `scripts/generate_overview.py` → `findings/overview.md`
- [x] Run last; reads all other findings + index files
- [x] Totals: sessions, messages, user turns, tokens by category, estimated cost USD
- [x] Model usage share
- [x] Top 5 projects by cost
- [x] Top 5 sessions by cost
- [x] Known limitations section (haiku invisible, claude-squad worktrees unattributable, cost estimates approximate)

---

## Phase 6 — State Updates

- [x] After each analysis script completes, update `state.json:analyses.<name>` to `"complete"`
- [x] After all analyses complete, set `state.json:last_updated` to current timestamp

---

## Known Limitations (to document in overview)

1. **Haiku tokens invisible** — stats-cache shows ~90M haiku tokens but zero exist in JSONL files (internal Claude Code processes, not API-level calls from project sessions)
2. **claude-squad worktrees unattributable** — all `~/.claude-squad/worktrees/*` cwds normalize to `home-directory`; parent repos unknowable (worktrees deleted, no git context)
3. **Cost estimates approximate** — uses public API pricing; Claude Code may have negotiated rates
4. **Cache tier pricing** — uses same rate for 5m and 1h ephemeral tiers (actual rates may differ)
5. **Enrichment is sparse** — only ~44% of sessions match a `history.jsonl` entry

---

## Script Dependency Order

```
pricing.json (manual)
    └── index_sessions.py       [Phase 2]
            └── aggregate_projects.py   [Phase 3]
            └── enrich_index.py         [Phase 4, optional]
            │
            ├── analyze_by_project.py   [Phase 5.1]
            ├── analyze_by_day.py       [Phase 5.2]
            ├── analyze_cache_efficiency.py  [Phase 5.3]
            ├── analyze_context_growth.py    [Phase 5.4, needs raw JSONL]
            ├── analyze_cost.py         [Phase 5.5]
            └── generate_overview.py    [Phase 5.6, runs last]
```
