# System Patterns

## Script Execution Order

```
index_sessions.py
  → aggregate_projects.py
  → enrich_index.py          (optional; 43% match rate — must not be required)
  → [analyze_*.py in parallel]
  → generate_overview.py
```

All scripts are incremental: `index_sessions.py` tracks file mtime + size in `state.json:file_manifest` and skips unchanged files.

## Dedup Pattern (Critical)

One API call produces N JSONL records — one per content block (thinking, text, tool_use). All N records share **identical** usage fields. Correct approach: group by `message.id` within a file, count usage once (first seen), collect content blocks from all records for tool call tracking. Do NOT sum usage across records for the same message.id.

Cross-file dedup is not needed — verified zero message ID overlaps across all 547 files.

## Worktree Path Normalization

All `~/.claude-squad/worktrees/*` cwds normalize to `"home-directory"` bucket after regex — worktrees are deleted, no git mapping survives.

Two variants of worktree paths exist (1-segment and 2-segment after `worktrees/`):

```python
# Strip claude-squad worktrees (1 or 2 segments)
cwd = re.sub(r'/\.claude(?:-squad)?/worktrees/(?:[^/]+/)?[^/]+$', '', cwd)
# Strip in-project worktrees
cwd = re.sub(r'/\.claude/worktrees/[^/]+$', '', cwd)
```

## Filtering Rules

- `model == "<synthetic>"` records are API errors with all-zero usage — filter out (these are the 22 "error-only" sessions)
- Sessions with zero assistant records (52 "empty" sessions) should be excluded from averages
- `isSidechain=true` records: zero exist in current dataset; field reserved for future proofing

## Context Growth Threshold

`context_turns/` traces are generated for sessions with >= 2 unique messages (lowered from 10). 222 sessions now qualify.

## JSONL Streaming Chunk Bug (Critical)

One API call produces multiple JSONL records sharing the same `message.id` — one per content block (thinking, text, tool_use). Tool_use blocks appear in **later** chunks, not the first. Any dedup strategy that takes only the first occurrence of a `message.id` will miss all tool calls for that turn. Fix: scan all records unconditionally for `tool_use` and `tool_result` content, only dedup for building the turn list.

## Tool Result Pairing: User Records Have No message.id

`tool_result` blocks live in user-role records, which typically have no `message.id` field. Processing that gates on `if not mid: continue` will silently skip all tool failure signals. Fix: collect user records regardless of `message.id`, match to tool calls via `tool_result.tool_use_id`.

## context_turns Cache Format

Old format (list of turn dicts) is incompatible with new format. New format is a top-level dict: `{session_id, total_loops, all_files_touched, turns: [...]}`. `_load_cached()` in `analyze_context_growth.py` checks for list type and returns `None` to force re-extraction. Old cache files must be deleted and re-extracted when upgrading.

## Loop Detection Algorithm

In `analyze_context_growth.py`, `count_loops(turns)` counts runs of 3+ consecutive turns where the same dominant `(tool_name, target)` pair recurs. "Dominant" = the pair appears more than once in that turn's tool calls.

## Template Injection Gotcha

`ui/template.html` contains a synthetic `<script>` block that defines `window.DATA` with placeholder values including `const TASK_TYPES`. When `serve.py`'s `build_ui()` runs, it replaces that entire block with real data — erasing any `const` vars declared inside it. Any JS code that references those vars bare (not via `window.DATA.X`) will throw `ReferenceError` and crash all rendering. Fix: always destructure from `window.DATA` at the top of each render function — never rely on local `const`s from the synthetic block.

## build_ui() Is a Separate Step from Pipeline Stages

`serve.py` has two distinct phases: (1) pipeline stages that produce `index/ui_data.json`, and (2) `build_ui()` which injects that data into `ui/template.html` → `ui/index.html`. Editing `ui/template.html` and re-running only a pipeline stage (e.g. `harness.py run --stage 5`) does NOT regenerate `ui/index.html`. Must run `build_ui()` (or the full `python3 serve.py`) after any template change.

## serve.py Static Replacement Pattern

`build_ui()` does string replacement on the raw HTML before writing `ui/index.html`. Use this for any value that is known at build time (session count, last-scan timestamp, total cost). Pattern: find a placeholder string in `template.html`, then in `build_ui()` call `html = html.replace("<placeholder>", computed_value)`. This runs after the `window.DATA` injection, so replacements are independent of the JS data block.

## kpi() Generates .cell, Not .kpi

The `kpi()` helper function in `template.html` generates elements with class `.cell`. The `.kpi` class is only a CSS layout wrapper container. Playwright selectors and any JS targeting KPI cards must use `.cell`, not `.kpi`.

## Playwright: Use domcontentloaded, Not networkidle

`page.goto(url, wait_until="networkidle")` times out because Google Fonts requests never fully settle. Correct pattern: `page.goto(url, wait_until="domcontentloaded")` followed by `page.wait_for_timeout(2000)`.

## Non-Breaking Hyphen in Title

The "x‑ray" title in the UI uses U+2011 (non-breaking hyphen), not a regular ASCII hyphen. String comparisons or test assertions against this title must use the actual character, not `-`.

## Context Chart: Dynamic Width + Interactive Dots

`contextChartSVG()` in `template.html` uses `W = Math.min(1600, turns.length * 16 + 80)`, H=200, PAD=40; SVG has explicit `width`/`height` attrs and no border (border is on the wrapping centering div). Pink user-turn circles have `data-turn` attrs, `cursor:pointer`, and are wired post-render in `renderSpecimen()` to `attachTip` + `jumpTurn()` — do not add duplicate wiring.

## Specimen Turn Navigation

Specimen filmstrip header has ← → prev/next `<button class="turn-nav">` elements; `jumpTurn(i)` handles scroll + toggle-open. These exist — do not re-add them when syncing from external design files.

## Specimen Bottom Row Column Order (Deliberate)

Files column is LEFT, tools column is RIGHT in the Specimen bottom row — commit `7cefe14`. This is intentional. Do not swap even if a reference design zip has them reversed.

## Riso Noise Overlay — Permanently Removed

The `body::before` SVG noise overlay was deliberately removed in commit `ba6b79a`. Do NOT add it back, even if a new design reference includes it.

## Files List Scroll Cap

The files list in Specimen view is wrapped in `max-height:320px; overflow-y:auto` with `overflow-wrap:anywhere` on filenames to prevent long paths from breaking layout.
