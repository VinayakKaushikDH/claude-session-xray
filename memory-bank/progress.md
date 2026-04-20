# Progress

## Status: UI fully working — all 5 views render real data. Run `python3 serve.py` to launch.

## Shipped

| Script | Purpose |
|--------|---------|
| `pricing.json` | Token pricing table (sonnet/opus/haiku) |
| `scripts/index_sessions.py` | Core indexer — 547 files → `index/sessions.json` |
| `scripts/aggregate_projects.py` | Rolls up to `index/projects.json` |
| `scripts/enrich_index.py` | Adds first-prompt text from history.jsonl (optional; 43% match) |
| `scripts/analyze_by_project.py` | Project cost ranking → `findings/by_project.md` |
| `scripts/analyze_by_day.py` | Daily activity/cost trends → `findings/by_day.md` |
| `scripts/analyze_cache_efficiency.py` | Cache hit ratios + savings → `findings/cache_efficiency.md` |
| `scripts/analyze_context_growth.py` | Per-turn context traces for 151 sessions → `index/context_turns/` |
| `scripts/analyze_cost.py` | Full cost breakdown → `findings/cost_report.md` |
| `scripts/generate_overview.py` | Top-level summary → `findings/overview.md` |

## Playwright Audit + Hardcoded Value Fixes (2026-04-19)

- Full Playwright audit: 32/33 checks passing across all 5 views
- Fixed 6 hardcoded "217 sessions" instances: 3 JS template literals in `template.html` (`renderFleet`, `renderAutopsy`, `VIEW_DESCS.fleet`) and 3 static replacements in `serve.py`'s `build_ui()` (masthead issue number, footer fake-data warning, last-scan timestamp)
- Committed as `c132a3b`

## UI Layer (added 2026-04-19)

| File | Purpose |
|------|---------|
| `serve.py` | Single entry point — pipeline + inject data + serve UI + open browser |
| `scripts/generate_ui_data.py` | Transform pipeline output → `window.DATA` shape for UI |
| `ui/template.html` | Design HTML (committed); synthetic data block replaced at build time |
| `ui/index.html` | Generated output (~4.5 MB with data inline); not committed |
| `harness.py` | Debug harness — 5 commands: check / run / inspect / template / serve |

**harness.py commands:**
- `check` — static validation of all data files + template markers (fast, non-destructive)
- `run [--force] [--stage N]` — pipeline stages as subprocesses (real tracebacks, not importlib)
- `inspect [sessions|ui_data|state|all]` — data file summaries + sample records
- `template` — verify DATA injection markers, preview what gets replaced
- `serve [--port N]` — debug HTTP server with JS error overlay injected into page (errors POST to `/__debug/js-error` → printed in terminal)

**Data shape gaps (remaining):**
- `taskType` — keyword heuristic; ~267 untitled sessions fall to "tweak"; `--classify` flag added to `generate_ui_data.py` for LiteLLM batch classification (cache: `index/classifications.json`)
- `success` — proxy: `error_messages == 0`
- `turns[].role` — user turns are stubs; only assistant turns have real ctxK

## Data Enrichment Pass (2026-04-19)

- `analyze_context_growth.py` fully rewritten: 4-pass extraction pairs `tool_use` → `tool_result` correctly; `count_loops()` added; MIN_MESSAGES lowered to 2; cache format changed to dict `{session_id, total_loops, all_files_touched, turns}`
- `generate_ui_data.py` updated: reads new dict-format context_turns; propagates `loops`, `filesTouched`, `toolFailures`, `toolSuccessRate` per session; `--classify` flag added for LiteLLM taskType classification
- `ui/template.html` Autopsy view: replaced 3 hardcoded synthetic blocks with JS heuristics on real session fields (loops/peakCtxK/toolFailures/redirectCount for cause-of-death; real `toolCalls[].target` for loop chains; conditional leakiest-tool annotation)
- Fixed two critical bugs in extraction: (1) streaming chunk dedup missing tool_use blocks in later chunks; (2) user records with `tool_result` silently skipped due to missing `message.id`
- Full re-extraction: 222 sessions processed → 74 with loops, 137 with tool failures, 183 with files touched, 184 total loops in fleet

## Key Findings

- **$1,179 total estimated spend** across 473 active sessions (2026-03-25 → 2026-04-19)
- **claude-squad** most expensive project: $441 across 22 sessions
- **Most expensive single session**: $161 (session `487ee473`)
- **1.25B effective input tokens**; 93% served from cache (~$1,300+ saved vs no-cache)
- 151 sessions with context growth traces in `index/context_turns/`

## VCS

Jujutsu repo initialized. Initial commit: `d0f588d6` ("Initial commit: Claude session analysis pipeline"). `.omc/` excluded via `.gitignore`.

## Design History

Architecture went through architect → critic → revised-architect before implementation. Critic caught: wrong dedup strategy, broken worktree regex, wrong session count (191 vs 547), missing cost model. All corrected before any code was written.
