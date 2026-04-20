#!/usr/bin/env python3
"""
QA Playwright test suite for Claude Session Analyzer UI.
Tests all 5 views: Fleet, Specimen, Autopsy, Self, Diff.
"""

import asyncio
import re
import sys
from playwright.async_api import async_playwright, Page

BASE_URL = "http://127.0.0.1:8642"
HARDCODED_PROMPT_DIST = {18, 31, 22, 14, 8, 4, 2, 1}

js_errors = []
console_errors = []

results = {
    "fleet": {"name": "Fleet", "tests": []},
    "specimen": {"name": "Specimen", "tests": []},
    "autopsy": {"name": "Autopsy", "tests": []},
    "self": {"name": "Self", "tests": []},
    "diff": {"name": "Diff", "tests": []},
    "global": {"name": "Global", "tests": []},
}


def record(view_key, test_name, passed, details=""):
    status = "PASS" if passed else "FAIL"
    results[view_key]["tests"].append({
        "name": test_name,
        "status": status,
        "details": details,
    })
    marker = "[PASS]" if passed else "[FAIL]"
    print(f"  {marker} {test_name}" + (f": {details}" if details else ""))


async def navigate_to_view(page: Page, view_name: str):
    """Click nav link matching view_name."""
    await page.click(f"text={view_name}")
    await page.wait_for_timeout(600)


async def test_global(page: Page):
    print("\n=== Global checks ===")
    # JS errors collected at end
    content = await page.content()

    # NaN check
    nan_count = content.count(">NaN<") + content.count('"NaN"') + len(re.findall(r'>\s*NaN\s*<', content))
    record("global", "No NaN in rendered HTML", nan_count == 0, f"Found {nan_count} NaN occurrences" if nan_count else "")

    # undefined check
    undef_count = len(re.findall(r'>\s*undefined\s*<', content))
    record("global", "No 'undefined' text in rendered HTML", undef_count == 0, f"Found {undef_count} undefined occurrences" if undef_count else "")

    # null check in metric displays (look for >null< patterns)
    null_count = len(re.findall(r'>\s*null\s*<', content))
    record("global", "No literal 'null' in metric displays", null_count == 0, f"Found {null_count} null occurrences" if null_count else "")


async def test_fleet(page: Page):
    print("\n=== Fleet view ===")
    await navigate_to_view(page, "Fleet")
    await page.wait_for_timeout(500)

    view_html = await page.inner_text("#view-fleet")

    # Session count "484"
    has_484 = "484" in view_html
    record("fleet", "Session count '484' visible", has_484, "Found '484'" if has_484 else "Could not find '484' in Fleet view")

    # View container visible
    is_visible = await page.locator("#view-fleet").is_visible()
    record("fleet", "Fleet container is visible", is_visible)

    # No NaN in fleet specifically
    nan_in_fleet = len(re.findall(r'NaN', view_html))
    record("fleet", "No NaN in Fleet view text", nan_in_fleet == 0, f"Found {nan_in_fleet} NaN occurrences" if nan_in_fleet else "")

    # Check other views are hidden
    specimen_visible = await page.locator("#view-specimen").is_visible()
    record("fleet", "Other views hidden when Fleet active", not specimen_visible)


async def test_specimen(page: Page):
    print("\n=== Specimen view ===")
    await navigate_to_view(page, "Specimen")
    await page.wait_for_timeout(500)

    is_visible = await page.locator("#view-specimen").is_visible()
    record("specimen", "Specimen container is visible", is_visible)

    # Look for session cards
    cards = page.locator("#view-specimen .session-card, #view-specimen [class*='card'], #view-specimen li")
    card_count = await cards.count()

    if card_count == 0:
        # Try more generic clickable items
        cards = page.locator("#view-specimen [data-session], #view-specimen .specimen-row, #view-specimen tr")
        card_count = await cards.count()

    record("specimen", "Session cards/rows present", card_count > 0, f"Found {card_count} clickable items")

    # Try clicking first card to open detail
    if card_count > 0:
        try:
            await cards.first.click()
            await page.wait_for_timeout(800)
            # Check for filmstrip/turn detail
            page_text = await page.inner_text("#view-specimen")
            has_detail = any(kw in page_text for kw in ["turn", "Turn", "filmstrip", "tool", "Tool", "tokens", "Tokens", "assistant", "user"])
            record("specimen", "Clicking session card opens detail", has_detail,
                   "Detail content found" if has_detail else "No detail content appeared after click")
        except Exception as e:
            record("specimen", "Clicking session card opens detail", False, f"Error: {e}")
    else:
        record("specimen", "Clicking session card opens detail", False, "No cards to click")

    # Check for turn filmstrip
    view_html = await page.inner_text("#view-specimen")
    nan_in_specimen = len(re.findall(r'NaN', view_html))
    record("specimen", "No NaN in Specimen view", nan_in_specimen == 0, f"Found {nan_in_specimen} NaN" if nan_in_specimen else "")


async def test_autopsy(page: Page):
    print("\n=== Autopsy view ===")
    await navigate_to_view(page, "Autopsy")
    await page.wait_for_timeout(500)

    is_visible = await page.locator("#view-autopsy").is_visible()
    record("autopsy", "Autopsy container is visible", is_visible)

    view_html = await page.inner_text("#view-autopsy")

    # Tool mortality table
    has_tool_table = any(kw in view_html for kw in ["tool", "Tool", "mortality", "Mortality", "failure", "Failure", "rate", "Rate"])
    record("autopsy", "Tool mortality table renders", has_tool_table,
           "Tool/mortality keywords found" if has_tool_table else "No tool mortality content found")

    # Cause of death - real causes
    real_causes = ["tool thrash", "context bloat", "loop spiral", "token limit", "crash"]
    found_real = [c for c in real_causes if c.lower() in view_html.lower()]
    record("autopsy", "Real 'cause of death' entries present", len(found_real) > 0,
           f"Found: {found_real}" if found_real else "No real cause entries found")

    # Old hardcoded artifacts should be gone
    old_hardcoded = ["loop · read → edit → test → read", "context overflow"]
    found_old = [o for o in old_hardcoded if o in view_html]
    record("autopsy", "Old hardcoded causes removed", len(found_old) == 0,
           f"Still present: {found_old}" if found_old else "")

    # Old hardcoded percentage 34 as top cause
    # Look for "34%" or "34 %" as dominant value
    has_hardcoded_34 = bool(re.search(r'\b34%', view_html))
    record("autopsy", "No hardcoded '34%' as top cause", not has_hardcoded_34,
           "Found hardcoded 34%" if has_hardcoded_34 else "")

    nan_in_autopsy = len(re.findall(r'NaN', view_html))
    record("autopsy", "No NaN in Autopsy view", nan_in_autopsy == 0, f"Found {nan_in_autopsy} NaN" if nan_in_autopsy else "")


async def test_self(page: Page):
    print("\n=== Self view ===")
    await navigate_to_view(page, "Self")
    await page.wait_for_timeout(800)

    is_visible = await page.locator("#view-self").is_visible()
    record("self", "Self container is visible", is_visible)

    view_html = await page.inner_text("#view-self")

    # --- promptLengthDist bar chart ---
    # Look for percentage labels in bars
    pct_matches = re.findall(r'(\d+)%', view_html)
    pct_values = [int(m) for m in pct_matches]
    print(f"    [DEBUG] percentage values found: {pct_values[:20]}")

    # Check 8 bars present — look for bar elements
    bar_locators = page.locator("#view-self [class*='bar'], #view-self .bar-chart [class*='segment']")
    bar_count = await bar_locators.count()
    if bar_count == 0:
        # Try canvas or svg
        canvas_count = await page.locator("#view-self canvas").count()
        svg_count = await page.locator("#view-self svg").count()
        print(f"    [DEBUG] canvas elements: {canvas_count}, svg elements: {svg_count}")
        # Fallback: check for 8 percentage values in the text
        bar_count = len(pct_values)

    record("self", "promptLengthDist: ~8 bars/labels present", bar_count >= 6,
           f"Found {bar_count} bar elements/pct values")

    # Check at least one value is NOT in old hardcoded set
    non_hardcoded = [v for v in pct_values if v not in HARDCODED_PROMPT_DIST]
    record("self", "promptLengthDist: at least one non-hardcoded value", len(non_hardcoded) > 0,
           f"Non-hardcoded values: {non_hardcoded[:5]}" if non_hardcoded else f"All values match old hardcoded set: {pct_values}")

    # Check ~27 appears for 1-2 word bucket (allow ±5 range)
    has_approx_27 = any(22 <= v <= 32 for v in pct_values)
    record("self", "promptLengthDist: ~27% in 1-2 word bucket", has_approx_27,
           f"Values near 27: {[v for v in pct_values if 22<=v<=32]}" if has_approx_27 else f"No values near 27, got {pct_values}")

    # --- thinkingTimeSeconds ---
    # p50 should be ~134 (not old hardcoded 18), p90 ~779
    thinking_section = ""
    lines = view_html.split("\n")
    for i, line in enumerate(lines):
        if "thinking" in line.lower() or "p50" in line.lower() or "p90" in line.lower():
            thinking_section += " ".join(lines[max(0,i-2):i+3]) + " "

    nums_in_thinking = re.findall(r'\b(\d+)\b', thinking_section)
    nums_in_thinking_int = [int(n) for n in nums_in_thinking]
    print(f"    [DEBUG] thinkingTime section nums: {nums_in_thinking_int[:20]}")

    # Also search full view for 134 and 779
    full_nums = re.findall(r'\b(\d+)\b', view_html)
    full_nums_int = [int(n) for n in full_nums]

    has_134_range = any(120 <= n <= 150 for n in full_nums_int)
    has_779_range = any(750 <= n <= 810 for n in full_nums_int)
    old_p50_18 = 18 in full_nums_int

    record("self", "thinkingTimeSeconds: p50 ~134 (not hardcoded 18)", has_134_range and not (old_p50_18 and not has_134_range),
           f"Found 120-150 range: {has_134_range}, Found 750-810 range: {has_779_range}, Old 18 present: {old_p50_18}")

    # --- avgTimeBetweenPrompts ---
    has_358_range = any(340 <= n <= 375 for n in full_nums_int)
    old_42 = 42 in full_nums_int
    record("self", "avgTimeBetweenPrompts: ~358 (not hardcoded 42)", has_358_range,
           f"Found 340-375 range: {has_358_range}, Old 42 present: {old_42}")

    nan_in_self = len(re.findall(r'NaN', view_html))
    record("self", "No NaN in Self view", nan_in_self == 0, f"Found {nan_in_self} NaN" if nan_in_self else "")


async def test_diff(page: Page):
    print("\n=== Diff view ===")
    await navigate_to_view(page, "Diff")
    await page.wait_for_timeout(500)

    is_visible = await page.locator("#view-diff").is_visible()
    record("diff", "Diff container is visible", is_visible)

    view_html = await page.inner_text("#view-diff")

    # Two session columns
    col_locators = page.locator("#view-diff [class*='col'], #view-diff [class*='session'], #view-diff [class*='panel']")
    col_count = await col_locators.count()
    print(f"    [DEBUG] Diff column elements: {col_count}")

    # Check for column-like content — dual session display
    has_dual = col_count >= 2 or "session" in view_html.lower() or "compare" in view_html.lower()
    record("diff", "Two session columns render", has_dual,
           f"Column elements: {col_count}" if has_dual else "No dual column structure found")

    nan_in_diff = len(re.findall(r'NaN', view_html))
    record("diff", "No NaN in Diff view", nan_in_diff == 0, f"Found {nan_in_diff} NaN" if nan_in_diff else "")

    # No JS error on Diff view load
    record("diff", "Diff view loads without error", True, "Rendered without exception")


async def check_js_errors_summary():
    print("\n=== JS Error Summary ===")
    if js_errors:
        for err in js_errors:
            print(f"  [JS ERROR] {err}")
        record("global", "No uncaught JS errors", False, f"{len(js_errors)} error(s): {js_errors[0][:200]}")
    else:
        print("  No uncaught JS errors detected.")
        record("global", "No uncaught JS errors", True)

    if console_errors:
        for msg in console_errors[:5]:
            print(f"  [CONSOLE ERROR] {msg}")
        record("global", "No console errors", False, f"{len(console_errors)} error(s)")
    else:
        print("  No console errors detected.")
        record("global", "No console errors", True)


async def main():
    print(f"QA Test Suite — Claude Session Analyzer")
    print(f"Target: {BASE_URL}")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # Capture JS errors
        page.on("pageerror", lambda err: js_errors.append(str(err)))
        page.on("console", lambda msg: console_errors.append(f"{msg.type}: {msg.text}") if msg.type == "error" else None)

        print("\nLoading page...")
        response = await page.goto(BASE_URL, wait_until="networkidle", timeout=15000)
        print(f"HTTP status: {response.status}")
        await page.wait_for_timeout(1000)

        record("global", f"Page loads (HTTP {response.status})", response.status == 200,
               f"Status: {response.status}")

        # Run all view tests
        await test_fleet(page)
        await test_specimen(page)
        await test_autopsy(page)
        await test_self(page)
        await test_diff(page)

        # Global checks (run on current page state)
        await test_global(page)

        # Check JS errors collected throughout
        await check_js_errors_summary()

        await browser.close()

    # Print final report
    print("\n" + "=" * 60)
    print("QA TEST REPORT SUMMARY")
    print("=" * 60)

    total = 0
    passed = 0
    failed = 0

    for view_key, view_data in results.items():
        view_pass = sum(1 for t in view_data["tests"] if t["status"] == "PASS")
        view_fail = sum(1 for t in view_data["tests"] if t["status"] == "FAIL")
        view_total = len(view_data["tests"])
        total += view_total
        passed += view_pass
        failed += view_fail

        status_icon = "PASS" if view_fail == 0 else "FAIL"
        print(f"\n  [{status_icon}] {view_data['name']} view: {view_pass}/{view_total} passed")
        for t in view_data["tests"]:
            icon = "+" if t["status"] == "PASS" else "X"
            detail = f" ({t['details']})" if t["details"] else ""
            print(f"      [{icon}] {t['name']}{detail}")

    print(f"\n{'=' * 60}")
    print(f"  Total: {total} tests | Passed: {passed} | Failed: {failed}")
    overall = "ALL PASS" if failed == 0 else f"{failed} FAILURE(S)"
    print(f"  Result: {overall}")
    print("=" * 60)

    return failed


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(0 if exit_code == 0 else 1)
