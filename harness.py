#!/usr/bin/env python3
"""
harness.py — Full-visibility debug harness for the Claude session analyzer.

Gives a future agent (or human) complete observability into every stage of
the pipeline, the data files, and the browser UI — without silent failures.

Commands:
  check      Fast static validation: state, data files, template markers
  run        Run pipeline stages as subprocesses (real tracebacks, full I/O)
  inspect    Dump summaries of data files (sessions, ui_data, state)
  template   Verify DATA injection markers in ui/template.html
  serve      Debug HTTP server: verbose logs + JS error overlay in browser

Usage:
  python3 harness.py check
  python3 harness.py run [--force] [--stage 1-5]
  python3 harness.py inspect [sessions|ui_data|state|all]
  python3 harness.py template
  python3 harness.py serve [--port N]
"""

import http.server
import json
import os
import subprocess
import sys
import textwrap
import time
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

REPO      = Path(__file__).resolve().parent
SCRIPTS   = REPO / "scripts"
UI_DIR    = REPO / "ui"
INDEX_DIR = REPO / "index"
FINDINGS  = REPO / "findings"

STATE_FILE    = REPO / "state.json"
SESSIONS_FILE = INDEX_DIR / "sessions.json"
UI_DATA_FILE  = INDEX_DIR / "ui_data.json"
TEMPLATE_FILE = UI_DIR / "template.html"
OUT_HTML      = UI_DIR / "index.html"
TURNS_DIR     = INDEX_DIR / "context_turns"

# Must match serve.py exactly
DATA_BLOCK_START = "// Synthetic session data"
DATA_BLOCK_END   = "window.DATA = { SESSIONS, FLEET_STATS, SELF_PROFILE, TASK_TYPES, TOOLS };"

PIPELINE_STAGES = [
    (1, "index_sessions.py",      "Index sessions"),
    (2, "enrich_index.py",        "Enrich from history.jsonl"),
    (3, "aggregate_projects.py",  "Aggregate projects"),
    (4, "analyze_context_growth.py", "Build context turn traces"),
    (5, "generate_ui_data.py",    "Generate UI data"),
]

# ── ANSI colours ───────────────────────────────────────────────────────────────

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    RED    = "\033[31m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    CYAN   = "\033[36m"
    DIM    = "\033[2m"

def ok(msg):    print(f"  {C.GREEN}✓{C.RESET} {msg}")
def fail(msg):  print(f"  {C.RED}✗{C.RESET} {msg}")
def warn(msg):  print(f"  {C.YELLOW}⚠{C.RESET} {msg}")
def info(msg):  print(f"  {C.CYAN}→{C.RESET} {msg}")
def head(msg):  print(f"\n{C.BOLD}{msg}{C.RESET}")
def dim(msg):   print(f"{C.DIM}{msg}{C.RESET}")


# ══════════════════════════════════════════════════════════════════════════════
# CHECK — static validation
# ══════════════════════════════════════════════════════════════════════════════

def cmd_check():
    """Validate all data files and template markers without running anything."""
    head("── CHECK ──────────────────────────────────────────────────────────────")
    errors = []

    # 1. State
    head("1. state.json")
    if not STATE_FILE.exists():
        fail(f"state.json missing: {STATE_FILE}")
        errors.append("state.json missing")
    else:
        try:
            state = json.loads(STATE_FILE.read_text())
            idx = state.get("indexing", {})
            status = idx.get("status", "?")
            color  = C.GREEN if status == "complete" else C.YELLOW
            print(f"  indexing.status = {color}{status}{C.RESET}")
            info(f"files_processed={idx.get('files_processed','?')}  "
                 f"files_total={idx.get('files_total','?')}  "
                 f"last_run={idx.get('last_run','?')}")
            analyses = state.get("analyses", {})
            if analyses:
                for k, v in analyses.items():
                    sym = "✓" if v == "complete" else "⚠"
                    c   = C.GREEN if v == "complete" else C.YELLOW
                    print(f"    {c}{sym}{C.RESET} analyses.{k} = {v}")
            ok("state.json readable")
        except Exception as e:
            fail(f"state.json parse error: {e}")
            errors.append("state.json parse error")

    # 2. sessions.json
    head("2. index/sessions.json")
    if not SESSIONS_FILE.exists():
        fail(f"sessions.json missing: {SESSIONS_FILE}")
        errors.append("sessions.json missing")
    else:
        try:
            sessions = json.loads(SESSIONS_FILE.read_text())
            by_status = {}
            for s in sessions:
                st = s.get("status", "?")
                by_status[st] = by_status.get(st, 0) + 1
            ok(f"{len(sessions)} total sessions")
            for st, n in sorted(by_status.items()):
                info(f"  status={st}: {n}")

            # Spot-check required fields on active sessions
            active = [s for s in sessions if s.get("status") == "active"]
            required = ["session_id", "project_canonical", "tokens_total",
                        "tool_calls_by_name", "start_ts", "estimated_cost_usd"]
            missing_fields = set()
            for s in active[:20]:
                for f in required:
                    if f not in s:
                        missing_fields.add(f)
            if missing_fields:
                warn(f"Missing fields in active sessions (sample): {missing_fields}")
                errors.append(f"sessions.json missing fields: {missing_fields}")
            else:
                ok(f"Required fields present in sample of {min(20, len(active))} active sessions")

            # Cost sanity
            total_cost = sum(s.get("estimated_cost_usd", 0) for s in active)
            info(f"Total estimated cost (active): ${total_cost:.2f}")
        except Exception as e:
            fail(f"sessions.json parse error: {e}")
            errors.append("sessions.json parse error")

    # 3. ui_data.json
    head("3. index/ui_data.json")
    if not UI_DATA_FILE.exists():
        fail(f"ui_data.json missing — run: python3 harness.py run --stage 5")
        errors.append("ui_data.json missing")
    else:
        size_mb = UI_DATA_FILE.stat().st_size / 1024 / 1024
        try:
            data = json.loads(UI_DATA_FILE.read_text())
            required_keys = ["SESSIONS", "FLEET_STATS", "SELF_PROFILE", "TASK_TYPES", "TOOLS"]
            missing = [k for k in required_keys if k not in data]
            if missing:
                fail(f"ui_data.json missing top-level keys: {missing}")
                errors.append(f"ui_data.json missing keys: {missing}")
            else:
                ok(f"ui_data.json has all required keys ({size_mb:.1f} MB)")
                sessions_list = data.get("SESSIONS", [])
                info(f"SESSIONS: {len(sessions_list)} entries")
                fleet = data.get("FLEET_STATS", {})
                info(f"FLEET_STATS: total={fleet.get('total')}, "
                     f"totalCost=${fleet.get('totalCost')}, "
                     f"totalTokensM={fleet.get('totalTokensM')}M")

                # Validate first session shape
                if sessions_list:
                    s0 = sessions_list[0]
                    ui_required = ["id", "project", "model", "start", "costUSD",
                                   "peakCtxK", "totalTokensK", "toolCallCount",
                                   "turns", "nTurns", "taskType"]
                    missing_ui = [f for f in ui_required if f not in s0]
                    if missing_ui:
                        warn(f"First SESSIONS entry missing UI fields: {missing_ui}")
                    else:
                        ok(f"First session has all expected UI fields")

                    n_with_turns = sum(1 for s in sessions_list if s.get("turns"))
                    n_empty_turns = sum(1 for s in sessions_list if not s.get("turns"))
                    info(f"Sessions with turn data: {n_with_turns}, empty turns: {n_empty_turns}")
        except json.JSONDecodeError as e:
            fail(f"ui_data.json is not valid JSON: {e}")
            errors.append("ui_data.json invalid JSON")
        except Exception as e:
            fail(f"ui_data.json check error: {e}")
            errors.append(str(e))

    # 4. Template markers
    head("4. ui/template.html markers")
    _check_template_markers(errors)

    # 5. ui/index.html
    head("5. ui/index.html (generated)")
    if not OUT_HTML.exists():
        warn("ui/index.html not yet generated — run: python3 harness.py run")
    else:
        size_kb = OUT_HTML.stat().st_size / 1024
        ok(f"ui/index.html exists ({size_kb:.0f} KB)")
        content = OUT_HTML.read_text(encoding="utf-8", errors="replace")
        if "window.DATA = {" in content:
            ok("window.DATA injection detected in index.html")
        else:
            fail("window.DATA not found in index.html — template injection failed")
            errors.append("window.DATA not injected into index.html")

    # 6. context_turns
    head("6. index/context_turns/")
    if TURNS_DIR.exists():
        n_turns = len(list(TURNS_DIR.glob("*.json")))
        ok(f"{n_turns} context_turns files cached")
    else:
        warn("context_turns/ directory missing — will be created by stage 4")

    # Summary
    head("── SUMMARY ─────────────────────────────────────────────────────────────")
    if errors:
        print(f"\n  {C.RED}{C.BOLD}{len(errors)} problem(s) found:{C.RESET}")
        for e in errors:
            fail(e)
        print()
        return 1
    else:
        print(f"\n  {C.GREEN}{C.BOLD}All checks passed.{C.RESET}\n")
        return 0


def _check_template_markers(errors=None):
    """Check DATA_BLOCK markers exist in template.html. Returns True if ok."""
    if errors is None:
        errors = []
    if not TEMPLATE_FILE.exists():
        fail(f"template.html missing: {TEMPLATE_FILE}")
        errors.append("template.html missing")
        return False

    content = TEMPLATE_FILE.read_text(encoding="utf-8", errors="replace")
    lines   = content.splitlines()
    ok_count = 0

    for marker_name, marker in [("DATA_BLOCK_START", DATA_BLOCK_START),
                                  ("DATA_BLOCK_END",   DATA_BLOCK_END)]:
        try:
            pos = content.index(marker)
            # Find line number
            line_no = content[:pos].count("\n") + 1
            # Show context
            ctx = lines[line_no - 1][:80]
            ok(f"{marker_name} found at line {line_no}: {C.DIM}{ctx!r}{C.RESET}")
            ok_count += 1
        except ValueError:
            fail(f"{marker_name} NOT found in template.html")
            info(f"  Expected: {marker!r}")
            errors.append(f"template missing marker: {marker_name}")

    if ok_count == 2:
        # Measure the block to be replaced
        try:
            i = content.index(DATA_BLOCK_START)
            j = content.index(DATA_BLOCK_END) + len(DATA_BLOCK_END)
            block_size = j - i
            info(f"Replacement block size: {block_size} chars ({block_size//1024} KB of synthetic data to replace)")
        except ValueError:
            pass

    return ok_count == 2


# ══════════════════════════════════════════════════════════════════════════════
# RUN — pipeline stages as subprocesses
# ══════════════════════════════════════════════════════════════════════════════

def cmd_run(force: bool = False, stage: int = None):
    """Run pipeline stages as subprocesses — real tracebacks, full I/O."""
    head("── RUN ─────────────────────────────────────────────────────────────────")
    info(f"force={force}  stage={stage or 'all'}")
    print()

    stages_to_run = PIPELINE_STAGES
    if stage is not None:
        stages_to_run = [(n, s, d) for n, s, d in PIPELINE_STAGES if n == stage]
        if not stages_to_run:
            fail(f"Unknown stage {stage}. Valid stages: 1-{len(PIPELINE_STAGES)}")
            return 1

    total_start = time.monotonic()
    failed = []

    for num, script_name, description in stages_to_run:
        script_path = SCRIPTS / script_name
        if not script_path.exists():
            fail(f"[{num}] {description}: script not found at {script_path}")
            failed.append(description)
            continue

        args = [sys.executable, str(script_path)]
        if num == 1 and force:
            args.append("--force")

        print(f"\n{C.BOLD}── [{num}/{len(PIPELINE_STAGES)}] {description} {'─' * (50 - len(description))}{C.RESET}")
        print(f"{C.DIM}cmd: {' '.join(args)}{C.RESET}\n")

        start = time.monotonic()
        try:
            result = subprocess.run(
                args,
                cwd=str(REPO),
                capture_output=False,   # stream directly to terminal
                text=True,
            )
            elapsed = time.monotonic() - start

            if result.returncode == 0:
                ok(f"Stage {num} completed in {elapsed:.1f}s")
            else:
                fail(f"Stage {num} exited with code {result.returncode} after {elapsed:.1f}s")
                failed.append(description)

        except Exception as e:
            elapsed = time.monotonic() - start
            fail(f"Stage {num} raised exception after {elapsed:.1f}s: {e}")
            traceback.print_exc()
            failed.append(description)

    total_elapsed = time.monotonic() - total_start
    head(f"── RESULT ──────────────────────────────────────────────────────────────")
    info(f"Total time: {total_elapsed:.1f}s")
    if failed:
        fail(f"Failed stages: {', '.join(failed)}")
        return 1
    else:
        ok("All stages completed successfully")
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# INSPECT — data file summaries
# ══════════════════════════════════════════════════════════════════════════════

def cmd_inspect(what: str = "all"):
    """Print summaries of data files."""
    head("── INSPECT ─────────────────────────────────────────────────────────────")

    if what in ("state", "all"):
        head("state.json")
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
            print(json.dumps(state, indent=2)[:3000])
        else:
            warn("state.json missing")

    if what in ("sessions", "all"):
        head("index/sessions.json — summary")
        if not SESSIONS_FILE.exists():
            warn("sessions.json missing")
        else:
            sessions = json.loads(SESSIONS_FILE.read_text())
            active   = [s for s in sessions if s.get("status") == "active"]
            info(f"Total: {len(sessions)}, Active: {len(active)}")

            # Project breakdown (top 15)
            proj_cost = {}
            proj_msgs = {}
            for s in active:
                p = s.get("project_canonical", "?")
                proj_cost[p] = proj_cost.get(p, 0) + s.get("estimated_cost_usd", 0)
                proj_msgs[p] = proj_msgs.get(p, 0) + s.get("unique_messages", 0)

            print(f"\n  {'Project':<40} {'Sessions':>8} {'Messages':>10} {'Cost':>8}")
            print(f"  {'-'*40} {'-'*8} {'-'*10} {'-'*8}")
            proj_sessions = {}
            for s in active:
                p = s.get("project_canonical", "?")
                proj_sessions[p] = proj_sessions.get(p, 0) + 1
            ranked = sorted(proj_cost, key=proj_cost.get, reverse=True)[:15]
            for p in ranked:
                print(f"  {p[:40]:<40} {proj_sessions.get(p,0):>8} "
                      f"{proj_msgs.get(p,0):>10} ${proj_cost[p]:>7.2f}")

            # Model breakdown
            model_counts = {}
            for s in active:
                for m in (s.get("models_used") or []):
                    model_counts[m] = model_counts.get(m, 0) + 1
            print(f"\n  Models used across sessions:")
            for m, n in sorted(model_counts.items(), key=lambda x: -x[1]):
                print(f"    {m}: {n} sessions")

            # Sample record
            if active:
                print(f"\n  Sample active session (first):")
                s0 = active[0]
                sample = {k: v for k, v in s0.items()
                          if k not in ("tokens_by_model", "tool_calls_by_name")}
                print(textwrap.indent(json.dumps(sample, indent=2)[:1500], "    "))

    if what in ("ui_data", "all"):
        head("index/ui_data.json — summary")
        if not UI_DATA_FILE.exists():
            warn("ui_data.json missing")
        else:
            data = json.loads(UI_DATA_FILE.read_text())
            fleet = data.get("FLEET_STATS", {})
            print(f"\n  FLEET_STATS:")
            print(textwrap.indent(json.dumps(fleet, indent=2), "    "))

            sessions = data.get("SESSIONS", [])
            print(f"\n  SESSIONS: {len(sessions)} entries")
            if sessions:
                s0 = sessions[0]
                print(f"  First session:")
                # Print without turns (too verbose)
                s0_short = {k: v for k, v in s0.items() if k != "turns"}
                print(textwrap.indent(json.dumps(s0_short, indent=2)[:1500], "    "))

                n_turns_total = sum(len(s.get("turns", [])) for s in sessions[:50])
                print(f"\n  Turn data check (first 50 sessions): {n_turns_total} turns total")

            task_breakdown = {}
            for s in sessions:
                t = s.get("taskType", "?")
                task_breakdown[t] = task_breakdown.get(t, 0) + 1
            print(f"\n  Task type distribution: {task_breakdown}")

            sp = data.get("SELF_PROFILE", {})
            if sp:
                print(f"\n  SELF_PROFILE keys: {list(sp.keys())}")
                print(f"  avgPromptWords={sp.get('avgPromptWords')}, "
                      f"peakHour={sp.get('peakHour')}, "
                      f"favoriteTools={sp.get('favoriteTools')}")


# ══════════════════════════════════════════════════════════════════════════════
# TEMPLATE — marker verification + preview
# ══════════════════════════════════════════════════════════════════════════════

def cmd_template():
    """Verify DATA injection markers and preview the replacement."""
    head("── TEMPLATE ────────────────────────────────────────────────────────────")

    errors = []
    ok_markers = _check_template_markers(errors)

    if ok_markers:
        content = TEMPLATE_FILE.read_text(encoding="utf-8", errors="replace")
        i = content.index(DATA_BLOCK_START)
        j = content.index(DATA_BLOCK_END) + len(DATA_BLOCK_END)
        block = content[i:j]
        lines = TEMPLATE_FILE.read_text().splitlines()

        print(f"\n  Synthetic data block ({len(block)} chars, lines "
              f"{content[:i].count(chr(10))+1}–{content[:j].count(chr(10))+1}):\n")
        for line in block.splitlines()[:12]:
            dim(f"    {line}")
        if block.count("\n") > 12:
            dim(f"    ... ({block.count(chr(10))+1} lines total)")

        if UI_DATA_FILE.exists():
            size_kb = UI_DATA_FILE.stat().st_size / 1024
            info(f"\n  ui_data.json ({size_kb:.0f} KB) would replace this block "
                 f"in ui/index.html")
        else:
            warn("\n  ui_data.json not found — run 'python3 harness.py run --stage 5' first")

    if errors:
        print(f"\n  {C.RED}Marker problems:{C.RESET}")
        for e in errors:
            fail(e)
        return 1
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# SERVE — debug HTTP server with JS error overlay
# ══════════════════════════════════════════════════════════════════════════════

# JS snippet injected before </body> — captures JS errors and displays them
# as an overlay panel in the browser, plus logs to a server endpoint.
_JS_DEBUG_SNIPPET = """
<script>
(function() {
  var errs = [];
  var panel = null;

  function showPanel() {
    if (!panel) {
      panel = document.createElement('div');
      panel.id = '__debug_panel';
      panel.style.cssText = [
        'position:fixed','bottom:0','right:0','max-width:600px','max-height:40vh',
        'overflow:auto','background:#1a0000','color:#ff6b6b','font:12px monospace',
        'padding:12px','border:2px solid #ff0000','z-index:999999',
        'white-space:pre-wrap','word-break:break-all'
      ].join(';');
      document.body.appendChild(panel);
    }
    panel.textContent = '🔴 JS ERRORS (' + errs.length + ')\\n\\n' + errs.join('\\n\\n');
  }

  function report(msg, source, line, col, err) {
    var entry = '✗ ' + msg + (source ? ' @ ' + source.split('/').pop() + ':' + line : '');
    if (err && err.stack) entry += '\\n' + err.stack.split('\\n').slice(1,4).join('\\n');
    errs.push(entry);
    showPanel();
    fetch('/__debug/js-error', {
      method: 'POST',
      body: JSON.stringify({msg: msg, source: source, line: line, col: col,
                            stack: err ? err.stack : null, ts: Date.now()})
    }).catch(function(){});
    return false;
  }

  window.onerror = report;
  window.addEventListener('unhandledrejection', function(e) {
    report('UnhandledRejection: ' + (e.reason && e.reason.message || e.reason),
           window.location.href, 0, 0, e.reason);
  });

  var _ce = console.error.bind(console);
  console.error = function() {
    var msg = Array.from(arguments).map(String).join(' ');
    report('[console.error] ' + msg, window.location.href, 0, 0, null);
    return _ce.apply(console, arguments);
  };

  console.log('[harness] JS debug overlay active — errors will appear here and POST to /__debug/js-error');
})();
</script>
"""


class _DebugHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler with verbose logging and JS error endpoint."""

    def log_message(self, fmt, *args):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        code = args[1] if len(args) > 1 else "?"
        color = C.GREEN if str(code).startswith("2") else C.YELLOW if str(code).startswith("3") else C.RED
        print(f"  {C.DIM}{ts}{C.RESET}  {color}{code}{C.RESET}  {args[0]}")

    def do_GET(self):
        # Inject debug script into index.html
        if self.path in ("/", "/index.html"):
            try:
                html = OUT_HTML.read_text(encoding="utf-8")
                if _JS_DEBUG_SNIPPET not in html:
                    html = html.replace("</body>", _JS_DEBUG_SNIPPET + "</body>", 1)
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
                return
            except FileNotFoundError:
                self.send_error(404, f"ui/index.html not found — run 'python3 harness.py run' first")
                return
            except Exception as e:
                self.send_error(500, str(e))
                return
        super().do_GET()

    def do_POST(self):
        if self.path == "/__debug/js-error":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                err = json.loads(body)
                ts  = datetime.now().strftime("%H:%M:%S")
                print(f"\n  {C.RED}{C.BOLD}[JS ERROR @ {ts}]{C.RESET}")
                print(f"  {C.RED}{err.get('msg','?')}{C.RESET}")
                if err.get("source"):
                    print(f"  source: {err['source']}:{err.get('line','?')}")
                if err.get("stack"):
                    for line in (err["stack"] or "").splitlines()[:6]:
                        print(f"  {C.DIM}{line}{C.RESET}")
                print()
            except Exception:
                pass
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(405)


def cmd_serve(port: int = 8642):
    """Debug server: verbose HTTP logging + JS error overlay + browser open."""
    head("── SERVE ───────────────────────────────────────────────────────────────")

    # Pre-flight check
    if not OUT_HTML.exists():
        warn("ui/index.html not found. Building it now via stage 5 only...")
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "generate_ui_data.py")],
            cwd=str(REPO),
        )
        if result.returncode != 0:
            fail("generate_ui_data.py failed — run 'python3 harness.py run' for full pipeline")
            return 1

    # Startup summary
    if UI_DATA_FILE.exists():
        try:
            data  = json.loads(UI_DATA_FILE.read_text())
            fleet = data.get("FLEET_STATS", {})
            nsess = fleet.get("total", "?")
            cost  = fleet.get("totalCost", "?")
            toks  = fleet.get("totalTokensM", "?")
            info(f"Data: {nsess} sessions, ${cost} total, {toks}M tokens")
        except Exception:
            pass

    os.chdir(UI_DIR)
    url = f"http://127.0.0.1:{port}"

    try:
        server = http.server.HTTPServer(("127.0.0.1", port), _DebugHandler)
    except OSError as e:
        fail(f"Cannot bind to port {port}: {e}")
        return 1

    print(f"\n  {C.BOLD}Debug Server{C.RESET}")
    print(f"  {C.CYAN}{url}{C.RESET}")
    print(f"  JS errors → terminal + red overlay panel in browser")
    print(f"  HTTP requests logged with status codes")
    print(f"  Ctrl-C to stop\n")

    webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    cmd = args[0]
    rest = args[1:]

    if cmd == "check":
        sys.exit(cmd_check())

    elif cmd == "run":
        force = "--force" in rest
        stage = None
        if "--stage" in rest:
            idx = rest.index("--stage")
            try:
                stage = int(rest[idx + 1])
            except (IndexError, ValueError):
                fail("--stage requires a number 1-5")
                sys.exit(1)
        sys.exit(cmd_run(force=force, stage=stage))

    elif cmd == "inspect":
        what = rest[0] if rest else "all"
        cmd_inspect(what)

    elif cmd == "template":
        sys.exit(cmd_template())

    elif cmd == "serve":
        port = 8642
        if "--port" in rest:
            idx = rest.index("--port")
            try:
                port = int(rest[idx + 1])
            except (IndexError, ValueError):
                fail("--port requires a number")
                sys.exit(1)
        sys.exit(cmd_serve(port=port))

    else:
        fail(f"Unknown command: {cmd!r}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
