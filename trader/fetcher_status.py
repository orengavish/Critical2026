"""
fetcher_status.py
Pivot table: dates as rows, symbol-type pairs as columns.

Cell format: rows/expected STATUS
  RUN  — actively fetching (from fetch_progress)
  OK   — finished successfully
  ERR  — finished with error
  SKIP — skipped
  MISS — not in fetch_log

Usage:
  python fetcher_status.py
  python fetcher_status.py --days 20
  python fetcher_status.py --no-tail
  python fetcher_status.py --watch          # refresh every 5s
  python fetcher_status.py --watch 10       # refresh every 10s
"""

import sys
import time
import argparse
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from zoneinfo import ZoneInfo
from lib.config_loader import get_config
from lib.db import get_db

CT = ZoneInfo("America/Chicago")

HOLIDAYS = {
    date(2026, 1,  1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4,  3), date(2026, 5, 25), date(2026, 7,  3),
    date(2026, 9,  7), date(2026, 11, 26), date(2026, 12, 25),
}

CELL_W = 14  # chars per data cell


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def _expected_days(lookback: int) -> list[date]:
    """Last `lookback` trading days with completed sessions, oldest first."""
    now_ct = datetime.now(CT)
    past_close = (now_ct.hour, now_ct.minute) >= (17, 30)
    most_recent = now_ct.date() if past_close else now_ct.date() - timedelta(days=1)
    days, d = [], most_recent
    while len(days) < lookback:
        if _is_trading_day(d):
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    return days


def _load_fetch_log(db_path: Path) -> list[dict]:
    with get_db(db_path) as con:
        rows = con.execute(
            "SELECT symbol, date, file_type, status, rows_fetched "
            "FROM fetch_log ORDER BY fetched_at ASC"  # ASC so latest wins in dict
        ).fetchall()
    return [dict(r) for r in rows]


def _load_fetch_progress(progress_db_path: Path) -> dict:
    """Return {(sym, date_iso, ft): row} keyed by normalized ft (trades/bidask)."""
    if not progress_db_path.exists():
        return {}
    try:
        with get_db(progress_db_path) as con:
            rows = con.execute(
                "SELECT symbol, date, data_type, records_fetched, finished "
                "FROM fetch_progress"
            ).fetchall()
    except Exception:
        return {}
    result = {}
    for r in rows:
        ft = "trades" if r["data_type"] == "TRADES" else "bidask"
        result[(r["symbol"], r["date"], ft)] = {
            "records_fetched": r["records_fetched"] or 0,
            "finished": r["finished"],
        }
    return result


def _fmt_k(n) -> str:
    """Format int as K/M abbreviation."""
    if not n:
        return "0"
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.1f}M" if v < 10 else f"{round(v)}M"
    if n >= 1_000:
        return f"{n // 1000}K"
    return str(n)


def _expected_k(all_rows: list[dict], sym: str, ft: str) -> str:
    counts = [
        r["rows_fetched"] for r in all_rows
        if r["symbol"] == sym and r["file_type"] == ft
        and r["status"] == "ok" and r["rows_fetched"]
    ]
    return _fmt_k(int(statistics.median(counts))) if len(counts) >= 3 else "?"


def _cell(sym: str, date_s: str, ft: str,
          done: dict, prog: dict, exp_k: str) -> str:
    """Single cell content for (sym, date, ft)."""
    key = (sym, date_s, ft)

    p = prog.get(key)
    if p and not p["finished"]:
        return f"{_fmt_k(p['records_fetched'])}/{exp_k} RUN"

    r = done.get(key)
    if r is None:
        # fetch_log only written after full day completes — fall back to progress_db
        if p and p["finished"]:
            return f"{_fmt_k(p['records_fetched'])}/{exp_k} DONE"
        return "MISS"

    status = r["status"]
    rows_k = _fmt_k(r.get("rows_fetched"))

    if status == "skipped":
        return "SKIP"
    if status == "ok":
        return f"{rows_k}/{exp_k} OK"
    if status == "error":
        return f"{rows_k}/{exp_k} ERR" if rows_k != "0" else "ERR"
    return status.upper()[:4]


def _get_symbols(cfg) -> list[str]:
    override = getattr(getattr(cfg, "fetcher", None), "symbols_override", None)
    return list(override) if override else list(cfg.symbols)


def _print_pivot(days: list[date], symbols: list[str], file_types: list[str],
                 done: dict, prog: dict, all_rows: list[dict], title: str):
    """Print pivot table: rows = dates, cols = (sym, ft) pairs."""
    ft_abbrev = {"trades": "T", "bidask": "B"}
    cols = [(sym, ft) for sym in symbols for ft in file_types]

    # Pre-compute expected sizes (median, expensive to repeat per cell)
    exp = {(sym, ft): _expected_k(all_rows, sym, ft)
           for sym in symbols for ft in file_types}

    date_w = 12
    header = f"{'DATE':<{date_w}}"
    for sym, ft in cols:
        label = f"{sym}-{ft_abbrev.get(ft, ft)}"
        header += f"  {label:<{CELL_W}}"
    sep = "-" * len(header)

    print(f"\n{title}")
    print(sep)
    print(header)
    print(sep)

    for d in days:
        date_s = d.isoformat()
        row = f"{date_s:<{date_w}}"
        for sym, ft in cols:
            c = _cell(sym, date_s, ft, done, prog, exp[(sym, ft)])
            row += f"  {c:<{CELL_W}}"
        print(row)

    print(sep)


def _print_active(prog: dict, all_rows: list[dict], symbols: list[str], file_types: list[str]):
    """Print a compact ACTIVE NOW block if any fetch is in progress."""
    active = {k: v for k, v in prog.items() if not v["finished"]}
    if not active:
        return
    print("ACTIVE NOW")
    print("-" * 50)
    for (sym, date_s, ft), p in sorted(active.items()):
        counts = [
            r["rows_fetched"] for r in all_rows
            if r["symbol"] == sym and r["file_type"] == ft
            and r["status"] == "ok" and r["rows_fetched"]
        ]
        exp = int(statistics.median(counts)) if len(counts) >= 3 else None
        fetched = p["records_fetched"]
        if exp:
            pct = min(100, int(fetched / exp * 100))
            bar = "#" * (pct // 5) + "." * (20 - pct // 5)
            print(f"  {sym} {ft} {date_s}  [{bar}] {pct:3d}%  {_fmt_k(fetched)}/{_fmt_k(exp)}")
        else:
            print(f"  {sym} {ft} {date_s}  {_fmt_k(fetched)} ticks (no baseline yet)")
    print()


def run(lookback: int = 10, show_tail: bool = True):
    cfg = get_config()
    try:
        db_path          = Path(cfg.paths.db)
        progress_db_path = db_path.parent / "fetch_progress.db"
    except Exception:
        db_path          = Path("data/galao.db")
        progress_db_path = Path("data/fetch_progress.db")

    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return

    symbols       = _get_symbols(cfg)
    fetch_bid_ask = getattr(getattr(cfg, "fetcher", None), "fetch_bid_ask", True)
    file_types    = ["trades", "bidask"] if fetch_bid_ask else ["trades"]

    all_rows = _load_fetch_log(db_path)
    done     = {(r["symbol"], r["date"], r["file_type"]): r for r in all_rows}
    prog     = _load_fetch_progress(progress_db_path)

    _print_active(prog, all_rows, symbols, file_types)

    main_days = _expected_days(lookback)
    main_set  = {d.isoformat() for d in main_days}

    _print_pivot(main_days, symbols, file_types, done, prog, all_rows,
                 f"FETCH STATUS - last {lookback} trading days")

    if show_tail:
        tail_dates = sorted(
            {date.fromisoformat(r["date"]) for r in all_rows
             if r["date"] not in main_set
             and _is_trading_day(date.fromisoformat(r["date"]))},
            reverse=True
        )
        if tail_dates:
            _print_pivot(tail_dates, symbols, file_types, done, prog, all_rows,
                         f"OLDER DATA ({len(tail_dates)} days)")
        else:
            print("(no older data)")

    total   = len(main_days) * len(symbols) * len(file_types)
    present = sum(
        1 for d in main_days for sym in symbols for ft in file_types
        if done.get((sym, d.isoformat(), ft), {}).get("status") in ("ok", "skipped")
    )
    missing = total - present
    print(f"\nCoverage: {present}/{total} present, {missing} missing\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch status pivot table")
    parser.add_argument("--days",    type=int, default=10,
                        help="Lookback trading days (default 10)")
    parser.add_argument("--no-tail", action="store_true",
                        help="Skip older data tail")
    parser.add_argument("--watch", nargs="?", const=5, type=int, metavar="SECS",
                        help="Live-refresh every N seconds (default 5)")
    args = parser.parse_args()

    if args.watch:
        import os
        interval = args.watch
        try:
            while True:
                os.system("cls" if os.name == "nt" else "clear")
                now = datetime.now().strftime("%H:%M:%S")
                print(f"[live — refreshing every {interval}s — {now} — Ctrl+C to stop]\n")
                run(lookback=args.days, show_tail=not args.no_tail)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        run(lookback=args.days, show_tail=not args.no_tail)
