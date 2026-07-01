"""
algo/approaches/cl/cl5.py
CL-5: Velocity Rejection

Finds price levels where price reversed with the highest speed.
High-velocity reversals = urgent institutional response.
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
    from algo.signal_bus import emit, snap_price

    tick_path = Path(processed_path) / f"{symbol}_{date}_ticks_dir.parquet"
    ticks = pd.read_parquet(str(tick_path)).sort_values("timestamp").reset_index(drop=True)

    min_vel = config.cl5.min_velocity

    # Parse timestamps to seconds
    try:
        ticks["time_sec"] = pd.to_datetime(ticks["timestamp"]).astype(np.int64) / 1e9
    except Exception:
        # Fallback: use index as proxy time
        ticks["time_sec"] = ticks.index.astype(float)

    prices = ticks["price"].values
    times  = ticks["time_sec"].values

    if len(prices) < 3:
        return 0

    # Compute velocity between consecutive ticks
    dt       = np.diff(times)
    dp       = np.diff(prices)
    velocity = dp / (dt + 0.001)  # pts/sec

    max_mag = float(np.max(np.abs(velocity))) if len(velocity) > 0 else 1.0

    emitted = 0
    seen    = set()

    # Find sign reversals: velocity goes strongly positive then strongly negative (or reverse)
    for i in range(1, len(velocity)):
        v_prev = velocity[i - 1]
        v_curr = velocity[i]

        # Rising then falling at the same price zone = RESISTANCE
        if v_prev > min_vel and v_curr < -min_vel:
            price    = snap_price(float(prices[i]))
            magnitude = float(abs(v_prev))
            key = (price, "RESISTANCE")
            if key not in seen:
                seen.add(key)
                strength = min(1.0, magnitude / (max_mag + 1e-9))
                emit(db_path, "CL-5", "RESISTANCE", date, price,
                     direction="SELL", strength=strength,
                     tags={"velocity": round(magnitude, 4)})
                emitted += 1

        # Falling then rising at the same price zone = SUPPORT
        elif v_prev < -min_vel and v_curr > min_vel:
            price    = snap_price(float(prices[i]))
            magnitude = float(abs(v_prev))
            key = (price, "SUPPORT")
            if key not in seen:
                seen.add(key)
                strength = min(1.0, magnitude / (max_mag + 1e-9))
                emit(db_path, "CL-5", "SUPPORT", date, price,
                     direction="BUY", strength=strength,
                     tags={"velocity": round(magnitude, 4)})
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

            # Price rises quickly to 5003, then falls quickly from 5003
            # Rising: 5000->5001->5002->5003 each 0.1 seconds (velocity = 10 pts/sec)
            # Falling: 5003->5002->5001->5000 each 0.1 seconds (velocity = -10 pts/sec)
            rows = []
            t = 0.0
            for p in [5000.0, 5001.0, 5002.0, 5003.0]:
                rows.append({"timestamp": f"{t:.1f}", "price": p,
                             "size": 1, "tick_direction": "buy"})
                t += 0.1
            for p in [5002.0, 5001.0, 5000.0]:
                rows.append({"timestamp": f"{t:.1f}", "price": p,
                             "size": 1, "tick_direction": "sell"})
                t += 0.1

            tick_df = pd.DataFrame(rows)
            tick_path = proc_dir / "MES_2026-07-01_ticks_dir.parquet"
            tick_df.to_parquet(str(tick_path), index=False)

            cfg = SimpleNamespace(cl5=SimpleNamespace(min_velocity=0.5))
            count = run(db, "2026-07-01", "MES", str(proc_dir), cfg)
            assert count >= 1, f"Expected >=1 signal, got {count}"

            signals = get_signals_for_date(db, "2026-07-01")
            resistance = [s for s in signals if s["category"] == "RESISTANCE"]
            assert len(resistance) >= 1, f"Expected RESISTANCE at 5003, got {signals}"
            assert resistance[0]["price"] == 5003.0, \
                f"Expected RESISTANCE at 5003, got {resistance[0]['price']}"

        print("[self-test] CL-5: PASS")
        return True
    except Exception as e:
        print(f"[self-test] CL-5: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
