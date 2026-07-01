"""
algo/approaches/corr/corr4.py
CORR-4: Spread Anomaly

Unusual MES-MNQ spread z-score at a critical level = institutional rebalancing.
"""

import sys
import argparse
import sqlite3
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import pandas as pd
import numpy as np


def _load_ticks(processed_path: str, symbol: str, date: str) -> pd.DataFrame | None:
    tick_path = Path(processed_path) / f"{symbol}_{date}_ticks_dir.parquet"
    if not tick_path.exists():
        return None
    df = pd.read_parquet(str(tick_path))
    try:
        df["time_sec"] = pd.to_datetime(df["timestamp"]).astype(np.int64) / 1e9
    except Exception:
        df["time_sec"] = df.index.astype(float)
    return df.sort_values("time_sec").reset_index(drop=True)


def _load_open(processed_path: str, symbol: str, date: str) -> float | None:
    sess_path = Path(processed_path) / f"{symbol}_{date}_sessions.parquet"
    if not sess_path.exists():
        return None
    sess = pd.read_parquet(str(sess_path))
    return float(sess.iloc[0]["session_open"])


def run(db_path, date: str, processed_path: str, config) -> int:
    from algo.signal_bus import emit

    window    = config.corr.spread_window_ticks
    threshold = config.corr.anomaly_threshold
    tick_tol  = 0.25

    mes_open = _load_open(processed_path, "MES", date)
    mnq_open = _load_open(processed_path, "MNQ", date)
    if not mes_open or not mnq_open:
        return 0

    mes_ticks = _load_ticks(processed_path, "MES", date)
    mnq_ticks = _load_ticks(processed_path, "MNQ", date)
    if mes_ticks is None or mnq_ticks is None:
        return 0

    # Merge on nearest timestamp (forward fill MNQ into MES timeframe)
    merged = pd.merge_asof(
        mes_ticks.sort_values("time_sec"),
        mnq_ticks[["time_sec", "price"]].rename(columns={"price": "mnq_price"}).sort_values("time_sec"),
        on="time_sec",
        direction="nearest"
    )

    # Compute spread: MES - (MNQ * MES_open / MNQ_open)
    merged["spread"] = merged["price"] - (merged["mnq_price"] * (mes_open / mnq_open))

    # Rolling z-score
    rolling_mean = merged["spread"].rolling(window, min_periods=max(1, window // 4)).mean()
    rolling_std  = merged["spread"].rolling(window, min_periods=max(1, window // 4)).std().fillna(1e-9)
    merged["z_score"] = (merged["spread"] - rolling_mean) / rolling_std

    # Load MES critical levels
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        mes_signals = con.execute(
            "SELECT price, category FROM algo_signals"
            " WHERE approach_id='CL-1' AND date=?",
            (date,)
        ).fetchall()
    finally:
        con.close()

    emitted = 0
    seen    = set()

    for sig in mes_signals:
        mes_level = float(sig["price"])
        # Rows where MES was near the level
        near = merged[abs(merged["price"] - mes_level) <= tick_tol]
        if near.empty:
            continue

        max_z = float(near["z_score"].abs().max())
        if max_z > threshold and mes_level not in seen:
            seen.add(mes_level)
            # cheap_mes: negative spread (MES underpriced) at support = bullish
            best_z_row   = near.loc[near["z_score"].abs().idxmax()]
            actual_z     = float(best_z_row["z_score"])
            cheap_mes    = actual_z < 0  # MES below fair value
            strength     = min(1.0, max_z / (2 * threshold))
            emit(db_path, "CORR-4", sig["category"], date, mes_level,
                 direction="BUY" if sig["category"] == "SUPPORT" else "SELL",
                 strength=strength,
                 tags={"z_score": round(actual_z, 3), "cheap_mes": cheap_mes})
            emitted += 1

    return emitted


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            from lib.db import init_db
            from algo.signal_bus import emit as bus_emit, get_signals_for_date
            from types import SimpleNamespace
            import pandas as pd

            db = Path(tmp) / "test.db"
            init_db(db)
            proc_dir = Path(tmp) / "proc"
            proc_dir.mkdir()

            # Sessions
            def make_sessions(sym, open_p):
                return pd.DataFrame([{
                    "date": "2026-07-01", "symbol": sym, "poc": open_p,
                    "vah": open_p + 5, "val": open_p - 5,
                    "session_open": open_p, "session_high": open_p + 10,
                    "session_low": open_p - 10, "total_volume": 1000,
                }])

            make_sessions("MES", 5000.0).to_parquet(str(proc_dir / "MES_2026-07-01_sessions.parquet"), index=False)
            make_sessions("MNQ", 20000.0).to_parquet(str(proc_dir / "MNQ_2026-07-01_sessions.parquet"), index=False)

            # Build 300 ticks with anomalous spread near 5000
            # Normal spread ~0, but at tick 150 (near price 5000) spread = -10 (z > 2)
            mes_prices = [5000.25] * 149 + [5000.0] + [5000.25] * 150
            mnq_ratio  = 4.0  # MNQ = 4x MES open
            # Normal MNQ: mes_price * 4
            mnq_prices = [p * mnq_ratio for p in mes_prices]
            # At tick 150, inflate MNQ to create spread anomaly
            mnq_prices[149] = mes_prices[149] * mnq_ratio + 50  # MES looks cheap

            times = list(range(300))
            mes_df = pd.DataFrame({"timestamp": [str(t) for t in times],
                                   "price": mes_prices, "size": [1]*300, "tick_direction": ["neutral"]*300})
            mnq_df = pd.DataFrame({"timestamp": [str(t) for t in times],
                                   "price": mnq_prices, "size": [1]*300, "tick_direction": ["neutral"]*300})

            mes_df.to_parquet(str(proc_dir / "MES_2026-07-01_ticks_dir.parquet"), index=False)
            mnq_df.to_parquet(str(proc_dir / "MNQ_2026-07-01_ticks_dir.parquet"), index=False)

            bus_emit(db, "CL-1", "SUPPORT", "2026-07-01", 5000.0)

            cfg = SimpleNamespace(corr=SimpleNamespace(
                spread_window_ticks=50, anomaly_threshold=2.0))
            count = run(db, "2026-07-01", str(proc_dir), cfg)
            assert count >= 1, f"Expected >=1 CORR-4 signal, got {count}"

            signals = get_signals_for_date(db, "2026-07-01")
            corr4 = [s for s in signals if s["approach_id"] == "CORR-4"]
            assert len(corr4) >= 1, f"No CORR-4 signals: {signals}"

        print("[self-test] CORR-4: PASS")
        return True
    except Exception as e:
        print(f"[self-test] CORR-4: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
