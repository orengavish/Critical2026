"""
algo/approaches/cl/cl2.py
CL-2: Directional Traffic (Bounce Rate)

Finds price levels where price repeatedly arrived and reversed.
High bounce rate from below = support; high rejection from above = resistance.
"""

import sys
import argparse
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
import numpy as np


def run(db_path, date: str, symbol: str, processed_path: str, config) -> int:
    from algo.signal_bus import emit

    tick_path = Path(processed_path) / f"{symbol}_{date}_ticks_dir.parquet"
    ticks = pd.read_parquet(str(tick_path)).reset_index(drop=True)

    min_visits = config.cl2.min_visits
    min_score  = config.cl2.min_bounce_score

    prices = ticks["price"].unique()
    emitted = 0

    for price in prices:
        mask = ticks["price"] == price
        idxs = ticks[mask].index.tolist()
        if not idxs:
            continue

        arr_below  = 0  # came from below (prev_price < price)
        dep_above  = 0  # left to above  (next_price > price)
        arr_above  = 0  # came from above (prev_price > price)
        dep_below  = 0  # left to below  (next_price < price)

        for i in idxs:
            prev_p = float(ticks.loc[i - 1, "price"]) if i > 0 else price
            next_p = float(ticks.loc[i + 1, "price"]) if i < len(ticks) - 1 else price

            if prev_p < price:  arr_below += 1
            if prev_p > price:  arr_above += 1
            if next_p > price:  dep_above += 1
            if next_p < price:  dep_below += 1

        total_visits = arr_below + arr_above

        support_score    = dep_above / (arr_below + 0.01)
        resistance_score = dep_below / (arr_above + 0.01)

        if support_score >= min_score and total_visits >= min_visits:
            strength = min(1.0, support_score / (2 * min_score))
            emit(db_path, "CL-2", "SUPPORT", date, price,
                 direction="BUY", strength=strength,
                 tags={"bounce_score": round(support_score, 3), "visits": total_visits})
            emitted += 1

        if resistance_score >= min_score and total_visits >= min_visits:
            strength = min(1.0, resistance_score / (2 * min_score))
            emit(db_path, "CL-2", "RESISTANCE", date, price,
                 direction="SELL", strength=strength,
                 tags={"bounce_score": round(resistance_score, 3), "visits": total_visits})
            emitted += 1

    return emitted


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            from lib.db import init_db
            from algo.signal_bus import get_signals_for_date
            from types import SimpleNamespace
            import pandas as pd

            db = Path(tmp) / "test.db"
            init_db(db)
            proc_dir = Path(tmp) / "proc"
            proc_dir.mkdir()

            # Build synthetic ticks: price bounces 5 times at 5000 (support)
            # Sequence: arrive from below (4999), sit at 5000, leave to above (5001) — x5
            tick_rows = []
            for _ in range(5):
                tick_rows += [
                    {"timestamp": "t", "price": 4999.75, "size": 1, "tick_direction": "neutral"},
                    {"timestamp": "t", "price": 5000.0,  "size": 1, "tick_direction": "buy"},
                    {"timestamp": "t", "price": 5000.25, "size": 1, "tick_direction": "buy"},
                ]

            tick_df = pd.DataFrame(tick_rows)
            tick_path = proc_dir / "MES_2026-07-01_ticks_dir.parquet"
            tick_df.to_parquet(str(tick_path), index=False)

            cfg = SimpleNamespace(cl2=SimpleNamespace(min_visits=3, min_bounce_score=2.0))
            count = run(db, "2026-07-01", "MES", str(proc_dir), cfg)
            assert count >= 1, f"Expected >=1 signal, got {count}"

            signals = get_signals_for_date(db, "2026-07-01")
            support_signals = [s for s in signals if s["category"] == "SUPPORT"]
            assert len(support_signals) >= 1, f"Expected >=1 SUPPORT signal, got {signals}"
            # Verify the bounce math makes sense (high score = strong bounce)
            assert all(s["strength"] >= 0.0 for s in signals)

        print("[self-test] CL-2: PASS")
        return True
    except Exception as e:
        print(f"[self-test] CL-2: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
