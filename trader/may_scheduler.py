"""
may_scheduler.py
Stable monthly scheduler — run once and leave it for the month.

On startup:
  1. Scan fetch_log for missing data files since --backfill-from date
  2. Fetch all missing symbol-days (skips what's already done via progress DB)

Daily (Mon–Fri, US trading days):
  • 17:00 Israel time  → 2-hour paper session  (broker + random_gen + position_manager)
  • 17:30 CT           → data fetch for all configured symbols

IB Gateway restarts: waits up to 10 min before each action — no manual intervention.

Usage:
  python may_scheduler.py
  python may_scheduler.py --backfill-from 2026-04-14
  python may_scheduler.py --dry-run        # paper sessions in dry-run mode
  python may_scheduler.py --no-paper       # data fetch only
  python may_scheduler.py --no-fetch       # paper sessions only
  python may_scheduler.py --self-test
"""

import sys
import time
import subprocess
import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from zoneinfo import ZoneInfo
from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db

log = get_logger("may_scheduler")

IL = ZoneInfo("Asia/Jerusalem")    # IDT in summer = UTC+3, handles DST
CT = ZoneInfo("America/Chicago")

PAPER_HOUR_IL = 17
PAPER_MIN_IL  = 0
FETCH_HOUR_CT = 17
FETCH_MIN_CT  = 30
DEFAULT_BACKFILL_FROM = "2026-04-14"

_HOLIDAYS = {
    date(2026, 1,  1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4,  3), date(2026, 5, 25), date(2026, 7,  3),
    date(2026, 9,  7), date(2026, 11, 26), date(2026, 12, 25),
}


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _HOLIDAYS


def _get_symbols(cfg) -> list:
    override = getattr(getattr(cfg, "fetcher", None), "symbols_override", None)
    return list(override) if override else list(cfg.symbols)


# ── Gateway health ────────────────────────────────────────────────────────────

def _wait_for_gateway(cfg, label: str = "", timeout_s: int = 600) -> bool:
    """
    Poll IB Gateway until it responds or timeout expires.
    On first failure, attempts to launch it via IBC if configured.
    Returns True when connected, False on timeout.
    """
    from fetcher import _connect
    from lib.ibc_launcher import try_start_gateway
    deadline = time.time() + timeout_s
    attempts = 0
    launch_attempted = False
    while time.time() < deadline:
        try:
            ib = _connect(cfg)
            ib.disconnect()
            if attempts > 0:
                log.info(f"Gateway back online{f' ({label})' if label else ''}")
                print(f"  Gateway online ✓", flush=True)
            return True
        except Exception:
            attempts += 1
            if attempts == 1:
                log.warning(f"Gateway not reachable{f' ({label})' if label else ''} — waiting")
                print(f"  Waiting for IB Gateway{f' ({label})' if label else ''}...", flush=True)
            if not launch_attempted:
                launch_attempted = True
                try_start_gateway(cfg, label=label)
            time.sleep(10)
    log.error(f"Gateway still down after {timeout_s // 60} min — skipping {label}")
    return False


# ── Backfill ──────────────────────────────────────────────────────────────────

def _find_missing(db_path: Path, symbols: list, fetch_bid_ask: bool,
                  from_date: date, to_date: date) -> list:
    """Return [(symbol, date)] pairs absent from fetch_log."""
    file_types = ["trades"] + (["bidask"] if fetch_bid_ask else [])
    n_types = len(file_types)

    with get_db(db_path) as con:
        rows = con.execute(
            "SELECT symbol, date FROM fetch_log WHERE status IN ('ok','skipped')"
        ).fetchall()

    done_counts: dict = {}
    for r in rows:
        key = (r["symbol"], r["date"])
        done_counts[key] = done_counts.get(key, 0) + 1

    missing = []
    curr = from_date
    while curr <= to_date:
        if _is_trading_day(curr):
            d_str = curr.isoformat()
            for sym in symbols:
                if done_counts.get((sym, d_str), 0) < n_types:
                    missing.append((sym, curr))
        curr += timedelta(days=1)
    return missing


def _priority_dates(db_path: Path, history_dir: Path) -> list:
    """Return [(sym, date)] that have verified trades but missing tick files, sorted by VT count desc."""
    present = set()
    if history_dir.exists():
        for f in history_dir.glob("*.csv"):
            parts = f.stem.split("_")
            if len(parts) < 3:
                continue
            sym, ftype, dc = parts[0], parts[1], parts[2]
            if f.stat().st_size > 100:
                date_s = f"{dc[:4]}-{dc[4:6]}-{dc[6:]}"
                present.add((sym, date_s, ftype))

    with get_db(db_path) as con:
        rows = con.execute(
            "SELECT DATE(fill_time) AS d, symbol, COUNT(*) AS n "
            "FROM verified_trades GROUP BY d, symbol ORDER BY n DESC"
        ).fetchall()

    result = []
    for r in rows:
        sym, d = r["symbol"], r["d"]
        has_t = (sym, d, "trades") in present
        has_b = (sym, d, "bidask") in present
        if not (has_t and has_b):
            try:
                result.append((sym, date.fromisoformat(d)))
            except ValueError:
                pass
    return result


def _run_backfill(from_date: date, cfg, db_path: Path,
                  output_dir: Path, progress_db_path: Path):
    from fetch_scheduler import _fetch_symbol_day
    yesterday = (datetime.now(CT) - timedelta(days=1)).date()
    symbols = _get_symbols(cfg)
    fetch_bid_ask = getattr(getattr(cfg, "fetcher", None), "fetch_bid_ask", True)

    # Pass 1: priority dates (have verified trades, missing tick files) — sorted by VT count desc
    try:
        history_dir = Path(cfg.paths.history)
    except Exception:
        history_dir = Path("data/history")

    priority = _priority_dates(db_path, history_dir)
    regular  = _find_missing(db_path, symbols, fetch_bid_ask, from_date, yesterday)

    # remove priority dates from regular to avoid double-fetching
    priority_set = {(sym, d) for sym, d in priority}
    regular = [(sym, d) for sym, d in regular if (sym, d) not in priority_set]

    total = len(priority) + len(regular)
    if total == 0:
        log.info("Backfill: nothing missing from %s to %s", from_date, yesterday)
        print(f"  Backfill: all data present from {from_date} ✓\n")
        return

    log.info(f"Backfill: {len(priority)} priority + {len(regular)} regular = {total} symbol-days")
    if priority:
        print(f"  Backfill priority ({len(priority)} dates with verified trades, no tick files):")
        for sym, d in priority:
            print(f"    {d} {sym}")
    print(f"  Backfill: {total} symbol-days total ({from_date} → {yesterday})\n")

    queue = priority + regular
    for i, (sym, d) in enumerate(queue, 1):
        tag = "[P]" if (sym, d) in priority_set else "   "
        log.info(f"Backfill {i}/{total}: {tag} {sym} {d}")
        print(f"  [{i}/{total}] {tag} {sym} {d}", flush=True)
        _fetch_symbol_day(sym, d, fetch_bid_ask, output_dir, progress_db_path, db_path)

    log.info("Backfill complete")
    print("  Backfill: done ✓\n")


# ── Main scheduler loop ───────────────────────────────────────────────────────

def run(backfill_from: date = None, dry_run: bool = False,
        no_paper: bool = False, no_fetch: bool = False):

    cfg = get_config()
    try:
        db_path          = Path(cfg.paths.db)
        output_dir       = Path(cfg.paths.history)
        progress_db_path = db_path.parent / "fetch_progress.db"
    except Exception:
        db_path          = Path("data/galao.db")
        output_dir       = Path("data/history")
        progress_db_path = Path("data/fetch_progress.db")

    init_db(db_path)
    symbols = _get_symbols(cfg)

    now_il = datetime.now(IL)
    print(f"\n{'='*60}")
    print(f"  MAY SCHEDULER — {now_il.strftime('%Y-%m-%d %H:%M')} IL")
    if no_paper:
        print(f"  Paper sessions : disabled")
    else:
        dry_tag = "  [dry-run]" if dry_run else ""
        print(f"  Paper sessions : {PAPER_HOUR_IL:02d}:{PAPER_MIN_IL:02d} IL  Mon–Fri{dry_tag}")
    if no_fetch:
        print(f"  Data fetch     : disabled")
    else:
        print(f"  Data fetch     : {FETCH_HOUR_CT:02d}:{FETCH_MIN_CT:02d} CT  Mon–Fri  {symbols}")
    print(f"  Backfill from  : {backfill_from or 'none'}")
    print(f"{'='*60}\n")

    # ── Startup backfill ──────────────────────────────────────────────────────
    if not no_fetch and backfill_from:
        if _wait_for_gateway(cfg, "backfill", timeout_s=600):
            _run_backfill(backfill_from, cfg, db_path, output_dir, progress_db_path)
        else:
            print("  Backfill skipped — gateway not reachable within 10 min\n")

    last_paper_date: date = None
    last_fetch_date: date = None
    paper_proc = None

    log.info("Scheduler loop started — polling every 30s")

    while True:
        now_il   = datetime.now(IL)
        now_ct   = datetime.now(CT)
        today_il = now_il.date()
        today_ct = now_ct.date()

        # ── Paper session trigger ─────────────────────────────────────────────
        if (not no_paper
                and _is_trading_day(today_il)
                and last_paper_date != today_il
                and now_il.hour == PAPER_HOUR_IL
                and now_il.minute >= PAPER_MIN_IL):

            if paper_proc is not None and paper_proc.poll() is None:
                log.warning("Paper session still running from previous trigger — marking done")
                last_paper_date = today_il
            else:
                log.info(f"Paper session trigger — {today_il}")
                if _wait_for_gateway(cfg, "paper session", timeout_s=600):
                    cmd = [sys.executable, "daily_paper_session.py"]
                    if dry_run:
                        cmd.append("--dry-run")
                    paper_proc = subprocess.Popen(cmd, cwd=str(Path(__file__).parent))
                    last_paper_date = today_il
                    log.info(f"Paper session started pid={paper_proc.pid}")
                    print(f"  [{now_il.strftime('%H:%M IL')}] Paper session started  (pid={paper_proc.pid})",
                          flush=True)
                else:
                    log.error(f"Paper session {today_il} skipped — gateway unreachable")
                    last_paper_date = today_il   # don't retry same day

        # ── Daily fetch trigger ───────────────────────────────────────────────
        if (not no_fetch
                and _is_trading_day(today_ct)
                and last_fetch_date != today_ct
                and now_ct.hour == FETCH_HOUR_CT
                and now_ct.minute >= FETCH_MIN_CT):

            log.info(f"Fetch trigger — {today_ct}")
            if _wait_for_gateway(cfg, "fetch", timeout_s=600):
                from fetch_scheduler import _run_fetch_cycle
                _run_fetch_cycle(today_ct, cfg, db_path, output_dir, progress_db_path)
                last_fetch_date = today_ct
                print(f"  [{now_ct.strftime('%H:%M CT')}] Fetch complete — {today_ct}",
                      flush=True)
            else:
                log.error(f"Fetch {today_ct} skipped — gateway unreachable")
                last_fetch_date = today_ct   # don't retry same day

        time.sleep(30)


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            init_db(db_path)

            # 1. Trading day logic
            assert _is_trading_day(date(2026, 5, 4))           # Monday
            assert not _is_trading_day(date(2026, 5, 2))        # Saturday
            assert not _is_trading_day(date(2026, 5, 25))       # Memorial Day

            # 2. Missing detection — empty DB → everything missing
            cfg = get_config()
            symbols = _get_symbols(cfg)
            assert len(symbols) >= 1

            missing = _find_missing(db_path, ["MES"], True, date(2026, 5, 1), date(2026, 5, 1))
            assert len(missing) == 1, f"Expected 1 missing, got {len(missing)}"

            # 3. Mark fetched → no longer missing
            with get_db(db_path) as con:
                from lib.db import insert_fetch_log
                insert_fetch_log(con, "MES", "2026-05-01", "trades", "ok", 50000)
                insert_fetch_log(con, "MES", "2026-05-01", "bidask", "ok", 200000)

            missing2 = _find_missing(db_path, ["MES"], True, date(2026, 5, 1), date(2026, 5, 1))
            assert len(missing2) == 0, f"Expected 0 missing after insert, got {len(missing2)}"

            # 4. Weekend excluded from missing
            missing_wk = _find_missing(db_path, ["MES"], True, date(2026, 5, 2), date(2026, 5, 3))
            assert len(missing_wk) == 0, "Weekend days should not appear as missing"

        print("[self-test] may_scheduler: PASS")
        return True

    except Exception as e:
        print(f"[self-test] may_scheduler: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao stable monthly scheduler")
    parser.add_argument("--self-test",       action="store_true")
    parser.add_argument("--backfill-from",   default=DEFAULT_BACKFILL_FROM,
                        help="Fetch missing data from this date (YYYY-MM-DD). "
                             f"Default: {DEFAULT_BACKFILL_FROM}")
    parser.add_argument("--no-backfill",     action="store_true",
                        help="Skip startup backfill scan")
    parser.add_argument("--dry-run",         action="store_true",
                        help="Paper sessions in dry-run mode (no real IB orders)")
    parser.add_argument("--no-paper",        action="store_true",
                        help="Disable paper sessions (data fetch only)")
    parser.add_argument("--no-fetch",        action="store_true",
                        help="Disable data fetch (paper sessions only)")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    backfill_from = None
    if not args.no_backfill and not args.no_fetch:
        try:
            backfill_from = date.fromisoformat(args.backfill_from)
        except ValueError:
            print(f"Invalid --backfill-from date: {args.backfill_from}")
            sys.exit(1)

    run(
        backfill_from=backfill_from,
        dry_run=args.dry_run,
        no_paper=args.no_paper,
        no_fetch=args.no_fetch,
    )
