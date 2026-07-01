"""
algo/approaches/cl/cl3.py
CL-3: Cumulative Delta Flip Zones

Finds where buyer/seller dominance switched — CVD local extrema.
CVD maxima = buyers exhausted (RESISTANCE); minima = sellers exhausted (SUPPORT).
"""

import sys
import argparse
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
import numpy as np


def _find_local_extrema(series):
    """Return (maxima_indices, minima_indices) in a pandas Series."""
    arr = series.values
    maxima, minima = [], []
    for i in range(1, len(arr) - 1):
        if arr[i] > arr[i-1] and arr[i] > arr[i+1]:
            maxima.append(i)
        elif arr[i] < arr[i-1] and arr[i] < arr[i+1]:
            minima.append(i)
    return maxima, minima


def run(db_path, date: str, symbol: str, processed_path: str, config) -> int:
    from algo.signal_bus import emit

    tick_path = Path(processed_path) / f"{symbol}_{date}_ticks_dir.parquet"
    ticks = pd.read_parquet(str(tick_path)).sort_values("timestamp").reset_index(drop=True)

    min_mag = config.cl3.min_delta_magnitude

    # Build CVD
    delta_per_tick = np.where(ticks["tick_direction"] == "buy",
                               ticks["size"], -ticks["size"])
    ticks["cvd"] = delta_per_tick.cumsum()

    maxima_idxs, minima_idxs = _find_local_extrema(ticks["cvd"])

    all_mags = []
    for i in maxima_idxs:
        prev_min = ticks.loc[:i, "cvd"].min()
        all_mags.append(abs(ticks.loc[i, "cvd"] - prev_min))
    for i in minima_idxs:
        prev_max = ticks.loc[:i, "cvd"].max()
        all_mags.append(abs(ticks.loc[i, "cvd"] - prev_max))

    max_mag = max(all_mags) if all_mags else 1.0

    emitted = 0

    for i in maxima_idxs:
        prev_min  = ticks.loc[:i, "cvd"].min()
        magnitude = abs(ticks.loc[i, "cvd"] - prev_min)
        if magnitude >= min_mag:
            price    = float(ticks.loc[i, "price"])
            strength = min(1.0, magnitude / max_mag)
            emit(db_path, "CL-3", "RESISTANCE", date, price,
                 direction="SELL", strength=strength,
                 tags={"cvd_flip": round(float(magnitude), 1)})
            emitted += 1

    for i in minima_idxs:
        prev_max  = ticks.loc[:i, "cvd"].max()
        magnitude = abs(ticks.loc[i, "cvd"] - prev_max)
        if magnitude >= min_mag:
            price    = float(ticks.loc[i, "price"])
            strength = min(1.0, magnitude / max_mag)
            emit(db_path, "CL-3", "SUPPORT", date, price,
                 direction="BUY", strength=strength,
                 tags={"cvd_flip": round(float(magnitude), 1)})
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

            # 10 buy ticks at 5000 (CVD goes +100), then 10 sell ticks at 5002 (CVD drops -100)
            rows = []
            for i in range(10):
                rows.append({"timestamp": f"t{i:03d}", "price": 5000.0,
                             "size": 10, "tick_direction": "buy"})
            for i in range(10):
                rows.append({"timestamp": f"t{i+10:03d}", "price": 5002.0,
                             "size": 10, "tick_direction": "sell"})

            tick_df = pd.DataFrame(rows)
            tick_path = proc_dir / "MES_2026-07-01_ticks_dir.parquet"
            tick_df.to_parquet(str(tick_path), index=False)

            # min_delta_magnitude=50 so the 100-unit flip triggers
            cfg = SimpleNamespace(cl3=SimpleNamespace(min_delta_magnitude=50))
            count = run(db, "2026-07-01", "MES", str(proc_dir), cfg)
            assert count >= 1, f"Expected >=1 signal, got {count}"

            signals = get_signals_for_date(db, "2026-07-01")
            # RESISTANCE expected near 5002 (CVD maxima before sells reversed it)
            resistance = [s for s in signals if s["category"] == "RESISTANCE"]
            assert len(resistance) >= 1, f"Expected RESISTANCE signal, got {signals}"

        print("[self-test] CL-3: PASS")
        return True
    except Exception as e:
        print(f"[self-test] CL-3: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
