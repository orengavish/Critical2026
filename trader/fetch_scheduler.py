"""
fetch_scheduler.py
Automatic daily tick-data fetcher for all configured symbols.

Runs as a managed subprocess under runner.py.

Behaviour:
  - On startup (if fetch_on_startup=true): fetches yesterday's data for any
    symbol/file-type not already marked finished in the progress DB.
  - Daily: waits until trigger_time_ct (default 17:30 CT) then fetches that
    day's data for all configured symbols (trades + bid/ask if fetch_bid_ask=true).
  - Logs each fetch outcome to the DB table `fetch_log`.
  - Skips weekends and configured holidays automatically.

Usage:
  python fetch_scheduler.py            # run as subprocess
  python fetch_scheduler.py --self-test

Self-test:
  python fetch_scheduler.py --self-test
"""

import sys
import time
import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None
from zoneinfo import ZoneInfo

from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db, insert_fetch_log

log = get_logger("fetch_scheduler")

CT = ZoneInfo("America/Chicago")

HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3), date(2026, 5, 25), date(2026, 7, 3),
    date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
}

_running = True


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS_2026


def _yesterday_ct() -> date:
    return (datetime.now(CT) - timedelta(days=1)).date()


def _get_symbols(cfg) -> list[str]:
    override = getattr(getattr(cfg, "fetcher", None), "symbols_override", None)
    if override:
        return list(override)
    return list(cfg.symbols)


def _fetch_symbol_day(symbol: str, target_date: date,
                      fetch_bid_ask: bool, output_dir: Path,
                      progress_db_path: Path, db_path: Path):
    """
    Fetch one symbol for one day. Writes outcome to fetch_log.
    Imports fetcher functions directly — no subprocess overhead.
    """
    from fetcher import fetch_day, _init_progress_db, _connect
    date_str = target_date.strftime("%Y-%m-%d")
    file_types = ["trades"] + (["bidask"] if fetch_bid_ask else [])

    try:
        cfg = get_config()
        ib = _connect(cfg)
    except Exception as e:
        log.error(f"{symbol} {date_str}: IB connect failed: {e}")
        with get_db(db_path) as con:
            for ft in file_types:
                insert_fetch_log(con, symbol, date_str, ft, "error",
                                 error_msg=f"IB connect failed: {e}")
        return

    progress_conn = _init_progress_db(progress_db_path)
    try:
        results = fetch_day(ib, symbol, target_date, fetch_bid_ask,
                            output_dir, progress_conn)
    except Exception as e:
        log.error(f"{symbol} {date_str}: fetch_day failed: {e}")
        results = {}
        for ft in ("TRADES", "BID_ASK") if fetch_bid_ask else ("TRADES",):
            results[ft] = f"error: {e}"
    finally:
        progress_conn.close()
        try:
            ib.disconnect()
        except Exception:
            pass

    with get_db(db_path) as con:
        for dtype, count in results.items():
            file_type = "trades" if dtype == "TRADES" else "bidask"
            if count == "skipped":
                insert_fetch_log(con, symbol, date_str, file_type, "skipped")
                log.info(f"{symbol} {file_type} {date_str}: skipped (already done)")
            elif isinstance(count, int):
                insert_fetch_log(con, symbol, date_str, file_type, "ok",
                                 rows_fetched=count)
                log.info(f"{symbol} {file_type} {date_str}: ok ({count:,} rows)")
            else:
                insert_fetch_log(con, symbol, date_str, file_type, "error",
                                 error_msg=str(count))
                log.error(f"{symbol} {file_type} {date_str}: {count}")


def _run_fetch_cycle(target_date: date, cfg, db_path: Path,
                     output_dir: Path, progress_db_path: Path):
    """Fetch all symbols for a single date, then validate."""
    if not _is_trading_day(target_date):
        log.info(f"Skipping {target_date} — not a trading day")
        return

    symbols = _get_symbols(cfg)
    fetch_bid_ask = getattr(getattr(cfg, "fetcher", None), "fetch_bid_ask", True)

    log.info(f"Fetch cycle: {target_date} | symbols={symbols} | bid_ask={fetch_bid_ask}")
    for symbol in symbols:
        if not _running:
            break
        _fetch_symbol_day(symbol, target_date, fetch_bid_ask,
                          output_dir, progress_db_path, db_path)

    # Post-fetch validation
    try:
        from validate_fetch import validate_and_update
        date_str = target_date.strftime("%Y-%m-%d")
        file_types = ["trades"] + (["bidask"] if fetch_bid_ask else [])
        for symbol in symbols:
            for ft in file_types:
                validate_and_update(symbol, date_str, ft, output_dir, db_path)
    except Exception as e:
        log.warning(f"Post-fetch validation failed: {e}")


def _startup_backfill(cfg, db_path: Path, output_dir: Path, progress_db_path: Path):
    """On startup: fetch yesterday if missing and it was a trading day."""
    yesterday = _yesterday_ct()
    if not _is_trading_day(yesterday):
        log.info(f"Startup backfill: {yesterday} not a trading day — skipping")
        return
    log.info(f"Startup backfill: checking {yesterday}")
    _run_fetch_cycle(yesterday, cfg, db_path, output_dir, progress_db_path)


def _wait_until_trigger(trigger_time_str: str) -> date:
    """
    Block until the daily trigger time (HH:MM CT).
    Returns the date that was just triggered.
    Last-triggered date is tracked to avoid double-firing.
    """
    last_fired: date = None
    while _running:
        now_ct = datetime.now(CT)
        h, m = map(int, trigger_time_str.split(":"))
        trigger_today = now_ct.replace(hour=h, minute=m, second=0, microsecond=0)

        if now_ct >= trigger_today and now_ct.date() != last_fired:
            last_fired = now_ct.date()
            return now_ct.date()

        # Sleep until trigger, check every 60 seconds
        seconds_to_trigger = (trigger_today - now_ct).total_seconds()
        if seconds_to_trigger > 0:
            sleep_secs = min(60, seconds_to_trigger)
        else:
            # Already past trigger — wait for tomorrow's trigger
            tomorrow_trigger = trigger_today + timedelta(days=1)
            sleep_secs = min(60, (tomorrow_trigger - now_ct).total_seconds())

        time.sleep(max(1, sleep_secs))

    return None


def run():
    cfg = get_config()
    fetcher_cfg = getattr(cfg, "fetcher", None)

    enabled = getattr(fetcher_cfg, "auto_fetch_enabled", True)
    if not enabled:
        log.info("fetch_scheduler: auto_fetch_enabled=false — exiting")
        return

    fetch_on_startup = getattr(fetcher_cfg, "fetch_on_startup", True)
    trigger_time = getattr(fetcher_cfg, "trigger_time_ct", "17:30")

    try:
        db_path = Path(cfg.paths.db)
        output_dir = Path(cfg.paths.history)
        progress_db_path = db_path.parent / "fetch_progress.db"
    except Exception:
        db_path = Path("data/galao.db")
        output_dir = Path("data/history")
        progress_db_path = Path("data/fetch_progress.db")

    init_db(db_path)

    if fetch_on_startup:
        _startup_backfill(cfg, db_path, output_dir, progress_db_path)

    log.info(f"fetch_scheduler: waiting for daily trigger at {trigger_time} CT")
    while _running:
        triggered_date = _wait_until_trigger(trigger_time)
        if triggered_date is None:
            break
        # triggered_date IS the session end date (e.g. trigger at Monday 17:30 → fetch Monday)
        # get_session_bounds(day) covers prev_day 17:00 → day 17:00
        log.info(f"Daily trigger fired — fetching session ending {triggered_date}")
        _run_fetch_cycle(triggered_date, cfg, db_path, output_dir, progress_db_path)


# ── Self-test ──────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "test.db"
            output_dir = tmp_path / "history"
            output_dir.mkdir()
            progress_db_path = tmp_path / "fetch_progress.db"

            init_db(db_path)

            cfg = get_config()

            # 1. Config loads
            symbols = _get_symbols(cfg)
            assert len(symbols) >= 1, "No symbols in config"

            # 2. Trading day logic
            assert _is_trading_day(date(2026, 4, 7)), "2026-04-07 should be trading day"
            assert not _is_trading_day(date(2026, 4, 5)), "2026-04-05 is Sunday"
            assert not _is_trading_day(date(2026, 4, 3)), "2026-04-03 is Good Friday holiday"

            # 3. fetch_log writes work
            with get_db(db_path) as con:
                insert_fetch_log(con, "MES", "2026-04-07", "trades", "ok", 45000)
                insert_fetch_log(con, "MES", "2026-04-07", "bidask", "ok", 120000)

            with get_db(db_path) as con:
                rows = con.execute(
                    "SELECT * FROM fetch_log WHERE symbol='MES'"
                ).fetchall()
            assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"

            # 4. Symbols override
            class FakeFetcherCfg:
                symbols_override = ["MES", "MNQ"]
                auto_fetch_enabled = True
                fetch_bid_ask = True
                fetch_on_startup = False
                trigger_time_ct = "17:30"
            class FakeCfg:
                fetcher = FakeFetcherCfg()
                symbols = ["MES"]
            override_syms = _get_symbols(FakeCfg())
            assert override_syms == ["MES", "MNQ"], f"Override failed: {override_syms}"

            # 5. IB not available — fetch attempt logs error gracefully
            # (Don't actually attempt — just verify the log write path works)
            with get_db(db_path) as con:
                insert_fetch_log(con, "MNQ", "2026-04-07", "trades", "error",
                                 error_msg="IB connect failed: test")
            with get_db(db_path) as con:
                err_rows = con.execute(
                    "SELECT * FROM fetch_log WHERE status='error'"
                ).fetchall()
            assert len(err_rows) == 1

        print("[self-test] fetch_scheduler: PASS")
        return True

    except Exception as e:
        print(f"[self-test] fetch_scheduler: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao daily fetch scheduler")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--symbol", default=None,
                        help="Manual single-symbol fetch (requires --date)")
    parser.add_argument("--date", default=None,
                        help="Date to fetch YYYY-MM-DD (manual mode only)")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    if args.symbol and args.date:
        # Manual single-symbol fetch triggered from browser "Fetch Now"
        cfg = get_config()
        try:
            db_path = Path(cfg.paths.db)
            output_dir = Path(cfg.paths.history)
            progress_db_path = db_path.parent / "fetch_progress.db"
        except Exception:
            db_path = Path("data/galao.db")
            output_dir = Path("data/history")
            progress_db_path = Path("data/fetch_progress.db")
        init_db(db_path)
        target = date.fromisoformat(args.date)
        fetch_bid_ask = getattr(getattr(cfg, "fetcher", None), "fetch_bid_ask", True)
        _fetch_symbol_day(args.symbol.upper(), target, fetch_bid_ask,
                          output_dir, progress_db_path, db_path)
        sys.exit(0)

    run()
