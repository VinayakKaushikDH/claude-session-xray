# Active Context

## Current State

UI fully working with real data in all fields. All 5 views (Fleet / Specimen / Autopsy / Self / Diff) render real data. `python3 serve.py` runs the full pipeline and opens the browser at `localhost:8642`. Current data: 222 sessions indexed, 74 with loops, 137 with tool failures, 183 with files touched, 184 total loops.

## What Was Done Last Session

- `ui/template.html` visual polish pass synced from new design zip (skipping riso noise re-add):
  - Context chart now dynamic width (`~16px/turn`, max 1600px); H=200, PAD=40; wrapped in centering border div
  - Pink user-turn dots on context chart are interactive: hover tooltip via `attachTip`/`turnTip`, click calls `jumpTurn()` + scrolls filmstrip
  - Specimen filmstrip header now has ← → `.turn-nav` prev/next buttons; `jumpTurn()` function added
  - Files list wrapped in `max-height:320px` scroll container with `overflow-wrap:anywhere`
  - `.turn-nav` CSS rule block added

## Where Things Stand

All 5 views render real data. Visual polish complete. Specimen interactivity (turn nav + chart dot click) fully wired. Remaining gap: `taskType` for ~267 untitled sessions (use `--classify` flag with local LiteLLM proxy at `localhost:36253/v1`, model `gemini-2-5-flash`).

## Next Steps

1. **LLM taskType classification** — run `python3 scripts/generate_ui_data.py --classify` (LiteLLM proxy must be running at `localhost:36253`); results cached in `index/classifications.json`
2. **Playwright re-verification** — run full audit after template changes to confirm all views still pass
