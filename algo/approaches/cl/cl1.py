"""
algo/approaches/cl/cl1.py
CL-1: Volume Histogram Peaks

Finds price levels where the most volume traded — institutional price memory.
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
    """
    Detect volume peaks and emit to signal bus.
    config must have .cl1.min_prominence and .cl1.min_vol_pct
    Returns number of signals emitted.
    """
    from algo.signal_bus import emit

    vp_path   = Path(processed_path) / f"{symbol}_{date}_vol_profile.parquet"
    sess_path = Path(processed_path) / f"{symbol}_{date}_sessions.parquet"

    vp   = pd.read_parquet(str(vp_path)).sort_values("price").reset_index(drop=True)
    sess = pd.read_parquet(str(sess_path))
    session_open = float(sess.iloc[0]["session_open"])

    total_vol   = vp["total_vol"].sum()
    min_vol_abs = config.cl1.min_vol_pct * total_vol
    min_prom    = config.cl1.min_prominence

    prominences = []
    for i, row in vp.iterrows():
        # 2 neighbors above + 2 below (clamped to edges)
        neighbor_idxs = [max(0, i-2), max(0, i-1), min(len(vp)-1, i+1), min(len(vp)-1, i+2)]
        neighbor_vols = [vp.loc[j, "total_vol"] for j in neighbor_idxs if j != i]
        mean_neighbor = float(np.mean(neighbor_vols)) if neighbor_vols else 1.0
        prom = row["total_vol"] / (mean_neighbor + 1e-9)
        prominences.append(prom)

    vp["prominence"] = prominences
    max_prom = vp["prominence"].max()

    peaks = vp[(vp["prominence"] >= min_prom) & (vp["total_vol"] >= min_vol_abs)]

    emitted = 0
    for _, row in peaks.iterrows():
        price    = float(row["price"])
        strength = min(1.0, float(row["prominence"]) / max_prom)
        category = "RESISTANCE" if price > session_open else "SUPPORT"
        emit(db_path, "CL-1", category, date, price,
             direction="SELL" if category == "RESISTANCE" else "BUY",
             strength=strength, tags={"prominence": round(float(row["prominence"]), 3),
                                      "vol": int(row["total_vol"])})
        emitted += 1

    return emitted


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile, csv, random
    try:
        with tempfile.TemporaryDirectory() as tmp:
            from lib.db import init_db
            from algo.signal_bus import get_signals_for_date
            from algo.preprocessor import run as preprocess
            from types import SimpleNamespace

            db = Path(tmp) / "test.db"
            init_db(db)

            hist_dir = Path(tmp) / "hist"
            proc_dir = Path(tmp) / "proc"
            hist_dir.mkdir()

            # Build synthetic CSV with clear peaks at 5000.0 and 5005.0
            rows = []
            rng = random.Random(99)
            for i in range(2000):
                # Heavily weight 5000.0 and 5005.0
                price = rng.choices(
                    [5000.0, 5000.25, 5000.5, 5001.0, 5002.0, 5003.0, 5004.0,
                     5005.0, 5005.25, 5005.5, 5006.0, 5007.0, 5008.0],
                    weights=[40, 2, 2, 2, 2, 2, 2, 40, 2, 2, 2, 2, 2],
                    k=1
                )[0]
                rows.append({
                    "timestamp": f"2026-07-01T09:00:{i%60:02d}",
                    "price": price,
                    "size": rng.randint(5, 15),
                    "side": "B" if rng.random() < 0.5 else "S",
                })

            csv_path = hist_dir / "MES_2026-07-01_trades.csv"
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["timestamp","price","size","side"])
                writer.writeheader()
                writer.writerows(rows)

            preprocess("2026-07-01", "MES", str(hist_dir), str(proc_dir))

            cfg = SimpleNamespace(cl1=SimpleNamespace(min_prominence=1.5, min_vol_pct=0.01))
            count = run(db, "2026-07-01", "MES", str(proc_dir), cfg)
            assert count >= 2, f"Expected >=2 signals, got {count}"

            signals = get_signals_for_date(db, "2026-07-01")
            prices  = {s["price"] for s in signals}
            assert 5000.0 in prices, f"Expected 5000.0 in peaks, got {prices}"
            assert 5005.0 in prices, f"Expected 5005.0 in peaks, got {prices}"

            for s in signals:
                assert 0.0 <= s["strength"] <= 1.0, f"strength out of range: {s['strength']}"

        print("[self-test] CL-1: PASS")
        return True
    except Exception as e:
        print(f"[self-test] CL-1: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
