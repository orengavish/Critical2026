"""
show_verified.py
Display all verified trades with full detail.

Usage:
  python show_verified.py
  python show_verified.py --symbol MES
  python show_verified.py --date 2026-04-21
  python show_verified.py --source random_mkt
  python show_verified.py --tail 20          # last N trades
"""

import sys
import argparse
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config
from lib.db import get_db


def run(symbol=None, date=None, source=None, tail=None):
    cfg = get_config()
    try:
        db_path = Path(cfg.paths.db)
    except Exception:
        db_path = Path("data/galao.db")

    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return

    filters = ["1=1"]
    params  = []
    if symbol: filters.append("symbol=?");  params.append(symbol)
    if date:   filters.append("DATE(fill_time)=?"); params.append(date)
    if source: filters.append("source=?");  params.append(source)
    where = " AND ".join(filters)
    limit = f"LIMIT {tail}" if tail else ""

    with get_db(db_path) as con:
        total = con.execute(f"SELECT COUNT(*) FROM verified_trades WHERE {where}",
                            params).fetchone()[0]
        rows = con.execute(f"""
            SELECT
                DATE(fill_time)  AS date,
                symbol, source, direction, entry_type,
                bracket_size,
                fill_price, exit_price, pnl_points,
                exit_reason,
                fill_time, exit_time
            FROM verified_trades
            WHERE {where}
            ORDER BY fill_time DESC
            {limit}
        """, params).fetchall()

    if not rows:
        print(f"No verified trades found (total in DB: {con.execute('SELECT COUNT(*) FROM verified_trades').fetchone()[0]})")
        return

    # summary
    tp    = sum(1 for r in rows if r["exit_reason"] == "TP")
    sl    = sum(1 for r in rows if r["exit_reason"] == "SL")
    pnl   = sum(r["pnl_points"] for r in rows)
    print(f"\nVerified trades: {total} total  |  showing: {len(rows)}")
    print(f"TP: {tp}  SL: {sl}  Net PnL: {pnl:+.2f} pts\n")

    # header
    print(f"{'DATE':<12} {'SYM':<5} {'DIR':<5} {'TYPE':<4} {'BKT':>4}  "
          f"{'FILL':>8}  {'EXIT':>8}  {'PNL':>6}  {'REASON':<5}  SOURCE")
    print("-" * 82)

    cur_date = None
    for r in rows:
        if r["date"] != cur_date:
            if cur_date is not None:
                print()
            cur_date = r["date"]
        pnl_s = f"{r['pnl_points']:+.2f}"
        print(f"{r['date']:<12} {r['symbol']:<5} {r['direction']:<5} {r['entry_type']:<4} "
              f"{r['bracket_size']:>4.0f}  "
              f"{r['fill_price']:>8.2f}  {r['exit_price']:>8.2f}  "
              f"{pnl_s:>6}  {r['exit_reason']:<5}  {r['source']}")

    print("-" * 82)
    print(f"Net PnL: {pnl:+.2f} pts\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Show all verified trades")
    parser.add_argument("--symbol", help="Filter by symbol")
    parser.add_argument("--date",   help="Filter by date YYYY-MM-DD")
    parser.add_argument("--source", help="Filter by source (random_mkt, random_lmt, critical_line...)")
    parser.add_argument("--tail",   type=int, help="Show last N trades only")
    args = parser.parse_args()
    run(symbol=args.symbol, date=args.date, source=args.source, tail=args.tail)
