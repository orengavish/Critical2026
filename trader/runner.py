"""
runner.py
Launches all Galao components in a single command.
Runs preflight first (hard abort on failure), then starts
broker, decider, position_manager, and visualizer as subprocesses.
Ctrl+C shuts everything down cleanly.

Usage:
    python runner.py              # full session
    python runner.py --no-preflight   # skip preflight (dev/test)
    python runner.py --self-test

Self-test:
    python runner.py --self-test
"""

import sys
import time
import signal
import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
import sys; sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db

log = get_logger("runner")

_procs: list[subprocess.Popen] = []
_stopping = False


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _shutdown(sig=None, frame=None):
    global _stopping
    if _stopping:
        return
    _stopping = True
    log.info("Runner shutting down — stopping all components")
    for p in _procs:
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
    # Give them 5 seconds to exit cleanly
    deadline = time.time() + 5
    for p in _procs:
        remaining = max(0, deadline - time.time())
        try:
            p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            p.kill()
    log.info("All components stopped")


signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


def _start(name: str, cmd: list[str]) -> subprocess.Popen:
    """Start a component subprocess, inherit stdout/stderr to console."""
    log.info(f"Starting {name}: {' '.join(cmd)}")
    p = subprocess.Popen(cmd, cwd=str(Path(__file__).parent))
    _procs.append(p)
    return p


def run(skip_preflight: bool = False, dry_run: bool = False,
        gen_bracket: float = None, gen_max_offset: int = None,
        no_random_gen: bool = False):
    cfg = get_config()

    print(f"\n{'='*55}")
    print(f"  GALAO ENGINE  —  {_now_utc()}")
    if dry_run:
        print(f"  *** DRY-RUN MODE — no IB orders will be sent ***")
    print(f"  DB    : {cfg.paths.db}")
    print(f"  Logs  : {cfg.paths.logs}/")
    print(f"  Web   : http://{cfg.visualizer.host}:{cfg.visualizer.port}")
    print(f"{'='*55}\n")

    # 1. Pre-flight (blocking — hard abort on failure)
    if not skip_preflight:
        log.info("Running pre-flight checks...")
        result = subprocess.run(
            [sys.executable, "preflight.py"],
            cwd=str(Path(__file__).parent)
        )
        if result.returncode != 0:
            log.error("Pre-flight FAILED — aborting session")
            sys.exit(1)
        log.info("Pre-flight PASSED")
    else:
        log.warning("Pre-flight skipped (--no-preflight)")

    # 2. Launch components
    broker_cmd = [sys.executable, "broker.py"]
    if dry_run:
        broker_cmd.append("--dry-run")

    gen_cmd = [sys.executable, "random_gen.py"]
    if gen_bracket is not None:
        gen_cmd += ["--bracket", str(gen_bracket)]
    if gen_max_offset is not None:
        gen_cmd += ["--max-offset", str(gen_max_offset)]

    components = [
        ("decider",           [sys.executable, "decider.py"]),
        ("broker",            broker_cmd),
        ("position_manager",  [sys.executable, "position_manager.py"]),
        ("visualizer",        [sys.executable, "visualizer/app.py"]),
        ("fetch_scheduler",   [sys.executable, "fetch_scheduler.py"]),
    ]
    if not no_random_gen:
        components.append(("random_gen", gen_cmd))

    for name, cmd in components:
        _start(name, cmd)
        time.sleep(0.5)  # small stagger so logs don't collide at startup

    log.info(f"All components running. Dashboard: "
             f"http://{cfg.visualizer.host}:{cfg.visualizer.port}")
    log.info("Press Ctrl+C to stop")

    # 3. Monitor — restart crashed components (except visualizer)
    # Exponential backoff per component; give up after 5 consecutive crashes
    restartable  = {"decider", "broker", "position_manager", "fetch_scheduler"}
    proc_map     = {name: p for (name, _), p in zip(components, _procs)}
    cmd_map      = {name: cmd for name, cmd in components}
    restart_count = {name: 0 for name in restartable}
    restart_delay = {name: 5  for name in restartable}   # seconds, doubles on each crash
    MAX_RESTARTS  = 5

    while not _stopping:
        time.sleep(5)
        for name, p in list(proc_map.items()):
            if _stopping:
                break
            if name not in restartable:
                continue
            if p.poll() is None:
                # Still running — reset counters
                restart_count[name] = 0
                restart_delay[name] = 5
                continue

            rc = p.returncode
            restart_count[name] += 1

            if restart_count[name] > MAX_RESTARTS:
                log.error(f"{name} crashed {restart_count[name]} times — giving up")
                continue

            delay = restart_delay[name]
            log.warning(f"{name} exited (rc={rc}), restart "
                        f"{restart_count[name]}/{MAX_RESTARTS} in {delay}s")
            time.sleep(delay)
            restart_delay[name] = min(delay * 2, 60)   # cap at 60s

            if not _stopping:
                new_p = _start(name, cmd_map[name])
                proc_map[name] = new_p


# ── Reset ────────────────────────────────────────────────────────────────────

def reset_session():
    """
    --reset: cancel all live IB orders and wipe operational DB tables.
    Keeps critical_lines and release_notes intact.
    """
    cfg = get_config()
    db_path = Path(cfg.paths.db)

    print(f"\n{'='*55}")
    print(f"  GALAO RESET  —  {_now_utc()}")
    print(f"  DB: {db_path}")
    print(f"{'='*55}")
    print("  This will:")
    print("    1. Cancel ALL open IB orders (reqGlobalCancel)")
    print("    2. Wipe commands, positions, ib_events, system_state")
    print()
    confirm = input("  Type YES to continue: ").strip()
    if confirm != "YES":
        print("  Aborted.")
        return

    # Step 1 — Cancel all IB orders
    print("\n[1/2] Cancelling all IB orders via reqGlobalCancel...")
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        from lib.ib_client import IBClient
        ibc = IBClient(cfg)
        ibc.connect(live=True, paper=False)
        ibc.live.reqGlobalCancel()
        log.info("reqGlobalCancel sent to IB LIVE")
        # Also cancel paper if configured
        try:
            ibc.connect(live=False, paper=True)
            ibc.paper.reqGlobalCancel()
            log.info("reqGlobalCancel sent to IB PAPER")
        except Exception:
            pass
        ibc.disconnect()
        print("    Done — IB global cancel sent.")
    except Exception as e:
        print(f"    WARNING: IB cancel failed ({e}) — continuing with DB wipe anyway.")

    # Step 2 — Wipe operational tables
    print("\n[2/2] Wiping DB tables...")
    init_db(db_path)   # ensure schema exists
    with get_db(db_path) as con:
        for tbl in ("commands", "positions", "ib_events", "system_state"):
            cnt = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            con.execute(f"DELETE FROM {tbl}")
            print(f"    {tbl}: deleted {cnt} rows")

    print(f"\n  Reset complete. DB is clean. Safe to restart runner.\n")


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    """
    Self-test: verify config loads, all component scripts exist,
    and preflight runs without error (uses --self-test on each).
    """
    try:
        cfg = get_config()

        # 1. All component scripts exist
        scripts = [
            "preflight.py", "decider.py", "broker.py",
            "position_manager.py", "visualizer/app.py", "fetch_scheduler.py",
        ]
        for s in scripts:
            p = Path(__file__).parent / s
            assert p.exists(), f"Missing script: {s}"

        # 2. Config has visualizer section
        assert hasattr(cfg, "visualizer"), "Config missing visualizer section"
        assert cfg.visualizer.port > 0, f"Invalid port: {cfg.visualizer.port}"

        # 3. Preflight self-test passes
        r = subprocess.run(
            [sys.executable, "preflight.py", "--self-test"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent)
        )
        assert r.returncode == 0, f"preflight --self-test failed: {r.stdout}{r.stderr}"

        print("[self-test] runner: PASS")
        return True

    except Exception as e:
        print(f"[self-test] runner: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao runner — starts all components")
    parser.add_argument("--self-test",     action="store_true")
    parser.add_argument("--no-preflight",  action="store_true",
                        help="Skip pre-flight checks (dev/testing only)")
    parser.add_argument("--reset",         action="store_true",
                        help="Cancel all IB orders and wipe DB, then exit")
    parser.add_argument("--dry-run",        action="store_true",
                        help="Run without sending any IB orders (broker logs instead)")
    parser.add_argument("--no-random-gen", action="store_true",
                        help="Do not start random trade generator")
    parser.add_argument("--bracket",        type=float, default=None, metavar="PTS",
                        help="Bracket size for random_gen (default: 8.0)")
    parser.add_argument("--max-offset",     type=int,   default=None, metavar="TICKS",
                        help="Max entry offset ticks for random_gen (default: 2)")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    if args.reset:
        reset_session()
        sys.exit(0)

    run(skip_preflight=args.no_preflight, dry_run=args.dry_run,
        gen_bracket=args.bracket, gen_max_offset=args.max_offset,
        no_random_gen=args.no_random_gen)
