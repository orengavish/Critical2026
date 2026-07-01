"""
trade_count.py
Count verified trades by date.

Usage:
  python trade_count.py
  python trade_count.py --symbol MNQ
  python trade_count.py --days 30
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None

from lib.config_loader import get_config
from lib.db import get_db


def run(symbol: str = None, days: int = 60):
    cfg = get_config()
    try:
        db_path = Path(cfg.paths.db)
    except Exception:
        db_path = Path("data/galao.db")

    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return

    sym_filter = f"AND symbol='{symbol}'" if symbol else ""
    days_filter = f"AND DATE(fill_time) >= DATE('now', '-{days} days')" if days else ""

    with get_db(db_path) as con:
        total = con.execute("SELECT COUNT(*) FROM verified_trades").fetchone()[0]

        rows = con.execute(f"""
            SELECT
                DATE(fill_time)   AS date,
                symbol,
                COUNT(*)          AS n,
                SUM(CASE WHEN exit_reason='TP' THEN 1 ELSE 0 END) AS tp,
                SUM(CASE WHEN exit_reason='SL' THEN 1 ELSE 0 END) AS sl,
                ROUND(SUM(pnl_points), 2) AS pnl
            FROM verified_trades
            WHERE 1=1 {sym_filter} {days_filter}
            GROUP BY DATE(fill_time), symbol
            ORDER BY date DESC, symbol
        """).fetchall()

    if not rows:
        print(f"No verified trades found (total in DB: {total})")
        return

    # header
    print(f"\n{'DATE':<12} {'SYM':<5} {'N':>4}  {'TP':>4}  {'SL':>4}  {'PNL':>8}")
    print("-" * 46)

    grand_n = grand_tp = grand_sl = 0
    grand_pnl = 0.0
    for r in rows:
        date_s, sym, n, tp, sl, pnl = r
        tp = tp or 0
        sl = sl or 0
        pnl = pnl or 0.0
        grand_n += n; grand_tp += tp; grand_sl += sl; grand_pnl += pnl
        print(f"{date_s:<12} {sym:<5} {n:>4}  {tp:>4}  {sl:>4}  {pnl:>8.2f}")

    print("-" * 46)
    print(f"{'TOTAL':<12} {'':5} {grand_n:>4}  {grand_tp:>4}  {grand_sl:>4}  {grand_pnl:>8.2f}")
    print(f"\nAll-time verified trades in DB: {total}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verified trade count by date")
    parser.add_argument("--symbol", help="Filter by symbol (e.g. MES)")
    parser.add_argument("--days",   type=int, default=60,
                        help="Lookback days (default 60, 0 = all)")
    args = parser.parse_args()
    run(symbol=args.symbol, days=args.days)
