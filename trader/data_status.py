"""
data_status.py
Quick status table of all CSV files in data/history.
Reads file sizes only — no row counting, instant output.

Usage:
  python data_status.py
  python data_status.py --detail    # include row count from fetch_log
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config
from lib.db import get_db


def _fmt_size(b: int) -> str:
    if b >= 1_000_000_000:
        return f"{b/1_000_000_000:.1f}G"
    if b >= 1_000_000:
        return f"{b/1_000_000:.0f}M"
    if b >= 1_000:
        return f"{b/1_000:.0f}K"
    return f"{b}B"


def run(detail: bool = False):
    cfg = get_config()
    try:
        history_dir = Path(cfg.paths.history)
        db_path     = Path(cfg.paths.db)
    except Exception:
        history_dir = Path("data/history")
        db_path     = Path("data/galao.db")

    if not history_dir.exists():
        print(f"History dir not found: {history_dir}")
        return

    # load fetch_log for row counts + status
    fetch_log = {}
    if db_path.exists():
        with get_db(db_path) as con:
            rows = con.execute(
                "SELECT symbol, date, file_type, status, rows_fetched FROM fetch_log"
            ).fetchall()
            for r in rows:
                fetch_log[(r["symbol"], r["date"], r["file_type"])] = r

    # collect files
    files = sorted(history_dir.glob("*.csv"))
    if not files:
        print(f"No CSV files in {history_dir}")
        return

    # parse filename: SYMBOL_type_YYYYMMDD.csv
    entries = []
    for f in files:
        parts = f.stem.split("_")
        if len(parts) < 3:
            continue
        symbol    = parts[0]
        file_type = parts[1]          # trades or bidask
        date_compact = parts[2]       # YYYYMMDD
        try:
            date_s = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"
        except Exception:
            date_s = date_compact
        size   = f.stat().st_size
        exists = size > 0
        log    = fetch_log.get((symbol, date_s, file_type))
        status = "EMPTY" if not exists else (log["status"].upper() if log else "FILE_ONLY")
        rows_k = ""
        if log and log["rows_fetched"]:
            n = log["rows_fetched"]
            rows_k = f"{n//1000}K" if n >= 1000 else str(n)
        entries.append((date_s, symbol, file_type, size, status, rows_k, f.name))

    # print table
    print(f"\n{'DATE':<12} {'SYM':<5} {'TYPE':<7} {'SIZE':>7}  {'ROWS':>7}  STATUS")
    print("-" * 56)

    cur_date = None
    for date_s, symbol, file_type, size, status, rows_k, fname in entries:
        if date_s != cur_date:
            if cur_date is not None:
                print()
            cur_date = date_s
        flag = "  !" if status in ("EMPTY", "CORRUPT") else ""
        print(f"{date_s:<12} {symbol:<5} {file_type:<7} {_fmt_size(size):>7}  {rows_k:>7}  {status}{flag}")

    print("-" * 56)
    total   = len(entries)
    ok      = sum(1 for e in entries if e[4] in ("OK", "FILE_ONLY"))
    corrupt = sum(1 for e in entries if e[4] in ("EMPTY", "CORRUPT"))
    print(f"Total: {total} files  |  OK: {ok}  |  Issues: {corrupt}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Data folder status")
    parser.add_argument("--detail", action="store_true",
                        help="Include row counts from fetch_log")
    args = parser.parse_args()
    run(detail=args.detail)
