#!/usr/bin/env python3
"""
serve.py — One-command Claude session analyzer.

Usage:
    python3 serve.py            # incremental re-index, open browser
    python3 serve.py --force    # full re-index from scratch
    python3 serve.py --port 9000

Steps:
  1. Run the analysis pipeline (incremental by default)
  2. Generate index/ui_data.json from real session data
  3. Inject that data into ui/template.html → ui/index.html
  4. Serve ui/ on localhost and open the browser
"""

import http.server
import importlib
import json
import os
import shutil
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

REPO     = Path(__file__).resolve().parent
UI_DIR   = REPO / "ui"
TEMPLATE = UI_DIR / "template.html"
OUT_HTML = UI_DIR / "index.html"
UI_DATA  = REPO / "index" / "ui_data.json"

DESIGN_SRC = Path("/tmp/claude_design/claude-code-analyzer/project/index.html")

DEFAULT_PORT = 8642

# Synthetic data block markers (to be replaced with real data)
DATA_BLOCK_START = "// Synthetic session data"
DATA_BLOCK_END   = "window.DATA = { SESSIONS, FLEET_STATS, SELF_PROFILE, TASK_TYPES, TOOLS, FAILURE_ANALYTICS };"


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline(force: bool = False):
    scripts = str(REPO / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)

    def run(mod_name: str, extra_argv: list = None):
        argv_backup = sys.argv[:]
        sys.argv = [mod_name] + (extra_argv or [])
        try:
            # Use importlib so repeated calls to serve.py in the same process work
            mod = importlib.import_module(mod_name)
            importlib.reload(mod)
            mod.main()
        except SystemExit:
            pass  # some scripts call sys.exit(0) on success
        except Exception as e:
            print(f"  WARNING: {mod_name} failed: {e}")
        finally:
            sys.argv = argv_backup

    print("\n── [1/5] Indexing sessions ──────────────────────────────────")
    run("index_sessions", ["--force"] if force else [])

    print("\n── [2/5] Enriching from history.jsonl ───────────────────────")
    run("enrich_index")

    print("\n── [3/5] Aggregating projects ───────────────────────────────")
    run("aggregate_projects")

    print("\n── [4/5] Building context turn traces ───────────────────────")
    run("analyze_context_growth")

    print("\n── [5/5] Generating UI data ─────────────────────────────────")
    run("generate_ui_data")


# ── UI assembly ───────────────────────────────────────────────────────────────

def ensure_template():
    """Copy design HTML to ui/template.html if not already stored."""
    if TEMPLATE.exists():
        return
    UI_DIR.mkdir(parents=True, exist_ok=True)
    if DESIGN_SRC.exists():
        shutil.copy2(DESIGN_SRC, TEMPLATE)
        print(f"  Stored template from design bundle → {TEMPLATE}")
    else:
        sys.exit(
            f"\nERROR: Design template not found.\n"
            f"  Expected: {TEMPLATE}\n"
            f"  Or:       {DESIGN_SRC}\n\n"
            f"Re-extract the design zip:\n"
            f"  unzip -o ~/Downloads/'Claude Code Analyzer-handoff.zip' -d /tmp/claude_design\n"
            f"Then run serve.py again."
        )


def build_ui():
    """Inject real window.DATA into the template and write ui/index.html."""
    ensure_template()

    content = TEMPLATE.read_text(encoding="utf-8")

    # Load the generated data
    if not UI_DATA.exists():
        sys.exit(f"\nERROR: {UI_DATA} not found — pipeline may have failed.")
    data_json = UI_DATA.read_text(encoding="utf-8")

    # Find and replace the synthetic data block
    try:
        i = content.index(DATA_BLOCK_START)
        j = content.index(DATA_BLOCK_END) + len(DATA_BLOCK_END)
    except ValueError:
        sys.exit(
            "\nERROR: Could not find synthetic data markers in template.html.\n"
            "The design template may have changed. Delete ui/template.html and re-run."
        )

    content = content[:i] + f"window.DATA = {data_json};" + content[j:]

    # Replace remaining hardcoded synthetic placeholders with real values
    data = json.loads(data_json)
    fleet = data.get("FLEET_STATS", {})
    n = fleet.get("total", "?")
    cost = fleet.get("totalCost", 0)
    scan_ts = datetime.now().strftime("%m-%d %H:%M")

    content = content.replace(
        "Iss. 217",
        f"Iss. {n}",
    )
    content = content.replace(
        "No real session data — synthetic mock · 217 specimens · seed=42",
        f"Real data · {n} sessions · ${cost:.0f} total cost · ~/.claude/projects",
    )
    content = content.replace(
        "last scan · 04-19 14:22",
        f"last scan · {scan_ts}",
    )

    UI_DIR.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(content, encoding="utf-8")

    size_kb = OUT_HTML.stat().st_size / 1024
    print(f"\n  UI built → {OUT_HTML} ({size_kb:.0f} KB)")


# ── Server ────────────────────────────────────────────────────────────────────

class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress per-request logs


def start_server(port: int):
    os.chdir(UI_DIR)
    url = f"http://127.0.0.1:{port}"

    try:
        server = http.server.HTTPServer(("127.0.0.1", port), _QuietHandler)
    except OSError as e:
        sys.exit(f"\nERROR: Could not bind to port {port}: {e}\nTry --port <other>")

    print(f"\n{'━' * 52}")
    print(f"  Claude Session X-Ray")
    print(f"  {url}")
    print(f"  Ctrl-C to stop")
    print(f"{'━' * 52}\n")

    webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    force = "--force" in sys.argv
    port  = DEFAULT_PORT
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--port" and i + 1 < len(sys.argv):
            try:
                port = int(sys.argv[i + 1])
            except ValueError:
                sys.exit(f"ERROR: invalid port '{sys.argv[i + 1]}'")

    run_pipeline(force=force)
    build_ui()
    start_server(port)


if __name__ == "__main__":
    main()
