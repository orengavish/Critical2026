"""
ib_fetcher_paper.py
Windows service — maintains 10 trading days of tick data for all configured symbols.

Behaviour:
  Loop every CHECK_INTERVAL_S (5 min):
    1. Build the list of expected (symbol, date) pairs for the last LOOKBACK_DAYS
       trading days that have a completed session (past 17:30 CT).
    2. Find which pairs are missing or incomplete in fetch_log.
    3. Fetch the oldest missing pair (one at a time).
    4. If nothing is missing: idle until next check.

Install as Windows service (run once as Administrator):
  nssm install IbFetcherPaper "C:\\Python311\\python.exe" "C:\\Projects\\galgo2026\\trader\\ib_fetcher_paper.py"
  nssm set IbFetcherPaper AppDirectory "C:\\Projects\\galgo2026\\trader"
  nssm set IbFetcherPaper AppStdout   "C:\\Projects\\galgo2026\\logs\\ib_fetcher_paper.log"
  nssm set IbFetcherPaper AppStderr   "C:\\Projects\\galgo2026\\logs\\ib_fetcher_paper.log"
  nssm set IbFetcherPaper Start SERVICE_AUTO_START
  nssm start IbFetcherPaper

Usage:
  python ib_fetcher_paper.py             # run directly
  python ib_fetcher_paper.py --self-test
"""

import sys
import time
import signal
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from zoneinfo import ZoneInfo
from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db, insert_fetch_log

log = get_logger("ib_fetcher_paper")

CT = ZoneInfo("America/Chicago")

LOOKBACK_DAYS     = 10   # trading days to maintain
CHECK_INTERVAL_S  = 300  # seconds between idle checks
SESSION_CLOSE_CT  = (17, 30)  # HH, MM — session data available after this time

HOLIDAYS = {
    date(2026, 1,  1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4,  3), date(2026, 5, 25), date(2026, 7,  3),
    date(2026, 9,  7), date(2026, 11, 26), date(2026, 12, 25),
}

_running = True


def _handle_signal(sig, frame):
    global _running
    log.info(f"Signal {sig} received — stopping after current fetch")
    _running = False

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def _expected_days() -> list[date]:
    """
    Last LOOKBACK_DAYS trading days with a completed session, oldest first.
    A session for date D is complete when CT time >= SESSION_CLOSE_CT on day D.
    """
    now_ct = datetime.now(CT)
    h, m   = SESSION_CLOSE_CT
    past_close = (now_ct.hour, now_ct.minute) >= (h, m)
    most_recent = now_ct.date() if past_close else now_ct.date() - timedelta(days=1)

    days = []
    d = most_recent
    while len(days) < LOOKBACK_DAYS:
        if _is_trading_day(d):
            days.append(d)
        d -= timedelta(days=1)

    days.reverse()   # oldest first
    return days


def _find_missing(db_path: Path, symbols: list, fetch_bid_ask: bool) -> list[tuple]:
    """
    Return (symbol, date) pairs where trades or bidask (or both) are absent
    from fetch_log. Ordered oldest-first so we fill history before recent gaps.
    """
    file_types = {"trades", "bidask"} if fetch_bid_ask else {"trades"}
    expected   = _expected_days()

    with get_db(db_path) as con:
        rows = con.execute(
            "SELECT symbol, date, file_type FROM fetch_log "
            "WHERE status IN ('ok', 'skipped')"
        ).fetchall()

    done: dict[tuple, set] = {}
    for r in rows:
        key = (r["symbol"], r["date"])
        done.setdefault(key, set()).add(r["file_type"])

    missing = []
    for d in expected:
        for sym in symbols:
            have = done.get((sym, d.isoformat()), set())
            if not file_types.issubset(have):
                missing.append((sym, d))

    return missing


def _get_symbols(cfg) -> list[str]:
    override = getattr(getattr(cfg, "fetcher", None), "symbols_override", None)
    return list(override) if override else list(cfg.symbols)


def _fetch_pair(symbol: str, target_date: date, fetch_bid_ask: bool,
                output_dir: Path, progress_db_path: Path, db_path: Path):
    """Fetch one (symbol, date) pair. Delegates to fetch_scheduler._fetch_symbol_day."""
    from fetch_scheduler import _fetch_symbol_day
    log.info(f"Fetching {symbol} {target_date} (bid_ask={fetch_bid_ask})")
    print(f"  -> {symbol} {target_date}", flush=True)
    _fetch_symbol_day(symbol, target_date, fetch_bid_ask,
                      output_dir, progress_db_path, db_path)


def run():
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
    output_dir.mkdir(parents=True, exist_ok=True)

    symbols      = _get_symbols(cfg)
    fetch_bid_ask = getattr(getattr(cfg, "fetcher", None), "fetch_bid_ask", True)

    print(f"\nIbFetcherPaper started — symbols={symbols} lookback={LOOKBACK_DAYS}d", flush=True)
    log.info(f"Started — symbols={symbols} lookback={LOOKBACK_DAYS}d fetch_bid_ask={fetch_bid_ask}")

    while _running:
        missing = _find_missing(db_path, symbols, fetch_bid_ask)

        if missing:
            sym, d = missing[0]
            _fetch_pair(sym, d, fetch_bid_ask, output_dir, progress_db_path, db_path)
            # immediately re-check — more might be missing
        else:
            expected = _expected_days()
            log.info(f"All {len(symbols) * len(expected)} pairs present — "
                     f"next check in {CHECK_INTERVAL_S // 60} min")
            print(f"  OK all data present — sleeping {CHECK_INTERVAL_S // 60} min", flush=True)
            for _ in range(CHECK_INTERVAL_S):
                if not _running:
                    break
                time.sleep(1)

    log.info("IbFetcherPaper stopped")
    print("IbFetcherPaper stopped", flush=True)


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        cfg = get_config()

        # 1. Trading day logic
        assert _is_trading_day(date(2026, 5, 4))           # Monday
        assert not _is_trading_day(date(2026, 5, 2))        # Saturday
        assert not _is_trading_day(date(2026, 5, 25))       # Memorial Day

        # 2. _expected_days returns LOOKBACK_DAYS trading days, oldest first
        days = _expected_days()
        assert len(days) == LOOKBACK_DAYS, f"Expected {LOOKBACK_DAYS} days, got {len(days)}"
        assert days == sorted(days), "Days not in ascending order"
        for d in days:
            assert _is_trading_day(d), f"{d} is not a trading day"

        # 3. _find_missing on empty DB returns all pairs
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            init_db(db_path)
            symbols = ["MES", "MNQ"]

            missing = _find_missing(db_path, symbols, fetch_bid_ask=True)
            assert len(missing) == LOOKBACK_DAYS * len(symbols), (
                f"Expected {LOOKBACK_DAYS * len(symbols)} missing, got {len(missing)}"
            )

            # 4. After inserting ok for one pair, it drops from missing
            d0 = days[0]
            with get_db(db_path) as con:
                insert_fetch_log(con, "MES", d0.isoformat(), "trades", "ok", 40000)
                insert_fetch_log(con, "MES", d0.isoformat(), "bidask", "ok", 150000)

            missing2 = _find_missing(db_path, symbols, fetch_bid_ask=True)
            assert len(missing2) == len(missing) - 1, "Completed pair still showing as missing"

            # 5. Partial (only trades done) still shows as missing
            d1 = days[1]
            with get_db(db_path) as con:
                insert_fetch_log(con, "MES", d1.isoformat(), "trades", "ok", 40000)
            missing3 = _find_missing(db_path, symbols, fetch_bid_ask=True)
            assert any(sym == "MES" and dd == d1 for sym, dd in missing3), (
                "Partial pair (only trades) should still be missing"
            )

        print("[self-test] ib_fetcher_paper: PASS")
        return True

    except Exception as e:
        print(f"[self-test] ib_fetcher_paper: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IB paper fetcher service")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    run()
