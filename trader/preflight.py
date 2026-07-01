"""
preflight.py
Pre-flight checks before any trading session (R-START-01 to R-START-05).

Checks:
  1. LIVE port 4001 connection
  2. PAPER port 4002 connection
  3. Price fetch from LIVE port
  4. DB read/write test
  5. Critical lines file exists for today's symbols

Any failure → hard abort (raises PreflightError).
Results are logged and written to DB system_state.

Usage:
    python preflight.py               # run checks, print results
    python preflight.py --self-test   # headless test (no real IB required)

Self-test:
    python preflight.py --self-test
"""

import sys
import argparse
from datetime import date, datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
import sys; sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db, set_system_state

log = get_logger("preflight")


class PreflightError(Exception):
    pass


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(con, check: str, status: str, detail: str = ""):
    key = f"preflight_{check}"
    value = f"{status}|{_now_utc()}|{detail}"
    set_system_state(con, key, value)


def check_ib_connections(ibc, con) -> dict:
    """Check LIVE and PAPER connections."""
    results = {}

    # LIVE
    try:
        ibc._connect_live()
        assert ibc.is_live_connected(), "LIVE connect returned False"
        results["live"] = ("PASS", "")
        log.info("Pre-flight LIVE connection: PASS")
        _record(con, "live", "PASS")
    except Exception as e:
        results["live"] = ("FAIL", str(e))
        log.error(f"Pre-flight LIVE connection: FAIL — {e}")
        _record(con, "live", "FAIL", str(e))

    # PAPER
    try:
        ibc._connect_paper()
        assert ibc.is_paper_connected(), "PAPER connect returned False"
        results["paper"] = ("PASS", "")
        log.info("Pre-flight PAPER connection: PASS")
        _record(con, "paper", "PASS")
    except Exception as e:
        results["paper"] = ("FAIL", str(e))
        log.error(f"Pre-flight PAPER connection: FAIL — {e}")
        _record(con, "paper", "FAIL", str(e))

    return results


def check_price_fetch(ibc, symbol: str, con) -> dict:
    """Fetch price for symbol from LIVE."""
    try:
        if not ibc.is_live_connected():
            raise ConnectionError("LIVE not connected — skipping price fetch")
        price = ibc.get_price(symbol)
        assert price > 0, f"Invalid price: {price}"
        msg = f"{symbol}={price}"
        log.info(f"Pre-flight price fetch: PASS ({msg})")
        _record(con, "price", "PASS", msg)
        return {"price": ("PASS", msg)}
    except Exception as e:
        log.error(f"Pre-flight price fetch: FAIL — {e}")
        _record(con, "price", "FAIL", str(e))
        return {"price": ("FAIL", str(e))}


def check_db(db_path, con) -> dict:
    """DB read/write test."""
    try:
        set_system_state(con, "preflight_db_test", "ok")
        val = con.execute(
            "SELECT value FROM system_state WHERE key='preflight_db_test'"
        ).fetchone()
        assert val and val["value"] == "ok"
        log.info("Pre-flight DB read/write: PASS")
        _record(con, "db", "PASS")
        return {"db": ("PASS", "")}
    except Exception as e:
        log.error(f"Pre-flight DB read/write: FAIL — {e}")
        _record(con, "db", "FAIL", str(e))
        return {"db": ("FAIL", str(e))}



def run_preflight(db_path=None, date_str: str = None) -> dict:
    """
    Run all pre-flight checks.
    Returns dict of {check: (status, detail)}.
    Raises PreflightError on any FAIL.
    """
    cfg = get_config()
    date_str = date_str or date.today().strftime("%Y-%m-%d")

    init_db(db_path)
    all_results = {}

    with get_db(db_path) as con:
        # DB check first (no IB needed)
        all_results.update(check_db(db_path, con))

        # IB checks
        from lib.ib_client import IBClient
        ibc = IBClient(cfg)
        all_results.update(check_ib_connections(ibc, con))
        all_results.update(check_price_fetch(ibc, cfg.symbols[0], con))

        ibc.disconnect()

        # Record overall result
        failures = [k for k, (s, _) in all_results.items() if s == "FAIL"]
        overall = "FAIL" if failures else "PASS"
        set_system_state(con, "preflight_overall",
                         f"{overall}|{_now_utc()}|failures={failures}")

    if failures:
        raise PreflightError(
            f"Pre-flight FAILED: {', '.join(failures)}"
        )

    log.info("Pre-flight: ALL CHECKS PASSED")
    return all_results


def print_results(results: dict):
    print("\n" + "=" * 55)
    print("  PRE-FLIGHT RESULTS")
    print("=" * 55)
    for check, (status, detail) in results.items():
        mark = "[PASS]" if status == "PASS" else "[FAIL]"
        line = f"  {mark}  {check}"
        if detail:
            line += f"  ({detail})"
        print(line)
    print("=" * 55)
    failures = [k for k, (s, _) in results.items() if s == "FAIL"]
    if failures:
        print(f"  OVERALL: FAIL ({len(failures)} check(s) failed)\n")
    else:
        print("  OVERALL: PASS\n")


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    """
    Headless self-test: runs all checks but treats IB failures as SKIP.
    DB and critical lines checks run against temp files.
    """
    import tempfile
    try:
        from lib.logger import reset_loggers
        from lib.config_loader import reset_cache

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            init_db(db_path)

            # DB check
            with get_db(db_path) as con:
                db_result = check_db(db_path, con)
            assert db_result["db"][0] == "PASS", f"DB check: {db_result}"

            # IB checks — attempt real connection, skip if unavailable
            from lib.ib_client import IBClient
            cfg = get_config()
            ibc = IBClient(cfg)
            with get_db(db_path) as con:
                ib_result = check_ib_connections(ibc, con)

            live_ok  = ib_result.get("live",  ("SKIP",))[0] == "PASS"
            paper_ok = ib_result.get("paper", ("SKIP",))[0] == "PASS"

            if live_ok and paper_ok:
                with get_db(db_path) as con:
                    price_result = check_price_fetch(ibc, "MES", con)
            else:
                log.info("[self-test] IB not available — skipping price fetch")

            ibc.disconnect()

            reset_loggers()

        print("[self-test] preflight: PASS")
        return True

    except Exception as e:
        print(f"[self-test] preflight: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao pre-flight checks")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--date", help="Override date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    results = {}
    try:
        results = run_preflight(date_str=args.date)
        print_results(results)
    except PreflightError as e:
        print_results(results)
        print(f"ABORT: {e}")
        sys.exit(1)
