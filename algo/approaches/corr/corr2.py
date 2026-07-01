"""
algo/approaches/corr/corr2.py
CORR-2: Lead-Lag Confirmation

NQ typically leads ES. If MNQ tested the equivalent level before MES, that's a stronger signal.
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
    return pd.read_parquet(str(tick_path))


def _load_open(processed_path: str, symbol: str, date: str) -> float | None:
    sess_path = Path(processed_path) / f"{symbol}_{date}_sessions.parquet"
    if not sess_path.exists():
        return None
    sess = pd.read_parquet(str(sess_path))
    return float(sess.iloc[0]["session_open"])


def run(db_path, date: str, processed_path: str, config) -> int:
    from algo.signal_bus import emit, snap_price

    lead_window = config.corr.lead_window_seconds
    tick_tol    = 0.25

    # Load MES CL-1 signals
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

    if not mes_signals:
        return 0

    mes_open = _load_open(processed_path, "MES", date)
    mnq_open = _load_open(processed_path, "MNQ", date)
    if not mes_open or not mnq_open:
        return 0

    mes_ticks = _load_ticks(processed_path, "MES", date)
    mnq_ticks = _load_ticks(processed_path, "MNQ", date)
    if mes_ticks is None or mnq_ticks is None:
        return 0

    def to_seconds(df):
        try:
            return pd.to_datetime(df["timestamp"]).astype(np.int64) / 1e9
        except Exception:
            return df.index.astype(float)

    mes_ticks = mes_ticks.copy()
    mnq_ticks = mnq_ticks.copy()
    mes_ticks["time_sec"] = to_seconds(mes_ticks)
    mnq_ticks["time_sec"] = to_seconds(mnq_ticks)

    emitted = 0
    for sig in mes_signals:
        mes_level = float(sig["price"])
        mnq_equiv = snap_price(mes_level * (mnq_open / mes_open))

        # Times where MNQ was within 1 tick of equivalent
        mnq_touches = mnq_ticks[abs(mnq_ticks["price"] - mnq_equiv) <= tick_tol]["time_sec"].tolist()
        # Times where MES was within 1 tick of level
        mes_touches = mes_ticks[abs(mes_ticks["price"] - mes_level) <= tick_tol]["time_sec"].tolist()

        if not mnq_touches or not mes_touches:
            continue

        mnq_first = min(mnq_touches)
        mes_first = min(mes_touches)

        lead_sec = mes_first - mnq_first
        if 0 < lead_sec <= lead_window:
            strength = max(0.0, 1.0 - (lead_sec / lead_window))
            emit(db_path, "CORR-2", sig["category"], date, mes_level,
                 direction="BUY" if sig["category"] == "SUPPORT" else "SELL",
                 strength=strength,
                 tags={"lead_seconds": round(lead_sec, 2), "mnq_equiv": mnq_equiv})
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

            def make_sessions(sym, open_p):
                return pd.DataFrame([{
                    "date": "2026-07-01", "symbol": sym, "poc": open_p,
                    "vah": open_p + 10, "val": open_p - 10,
                    "session_open": open_p, "session_high": open_p + 20,
                    "session_low": open_p - 20, "total_volume": 10000,
                }])

            make_sessions("MES", 5000.0).to_parquet(
                str(proc_dir / "MES_2026-07-01_sessions.parquet"), index=False)
            make_sessions("MNQ", 20000.0).to_parquet(
                str(proc_dir / "MNQ_2026-07-01_sessions.parquet"), index=False)

            # MNQ touches 20000 at 09:01:40, MES touches 5000 at 09:02:10 (30s later)
            mnq_ticks = pd.DataFrame([
                {"timestamp": "2026-07-01T09:01:40", "price": 20000.0, "size": 1, "tick_direction": "buy"},
                {"timestamp": "2026-07-01T09:05:00", "price": 20001.0, "size": 1, "tick_direction": "buy"},
            ])
            mes_ticks = pd.DataFrame([
                {"timestamp": "2026-07-01T09:02:10", "price": 5000.0, "size": 1, "tick_direction": "buy"},
                {"timestamp": "2026-07-01T09:05:00", "price": 5001.0, "size": 1, "tick_direction": "buy"},
            ])
            mnq_ticks.to_parquet(str(proc_dir / "MNQ_2026-07-01_ticks_dir.parquet"), index=False)
            mes_ticks.to_parquet(str(proc_dir / "MES_2026-07-01_ticks_dir.parquet"), index=False)

            bus_emit(db, "CL-1", "SUPPORT", "2026-07-01", 5000.0)

            cfg = SimpleNamespace(corr=SimpleNamespace(lead_window_seconds=60))
            count = run(db, "2026-07-01", str(proc_dir), cfg)
            assert count >= 1, f"Expected >=1 CORR-2 signal (MNQ led by 30s), got {count}"

            # Test: MES touches first — no emission
            from lib.db import init_db as init_db2
            db2 = Path(tmp) / "test2.db"
            init_db2(db2)

            # MES touches at 09:01:40, MNQ touches at 09:02:10 (MES leads)
            mes_ticks2 = pd.DataFrame([
                {"timestamp": "2026-07-01T09:01:40", "price": 5000.0, "size": 1, "tick_direction": "buy"},
            ])
            mnq_ticks2 = pd.DataFrame([
                {"timestamp": "2026-07-01T09:02:10", "price": 20000.0, "size": 1, "tick_direction": "buy"},
            ])
            mes_ticks2.to_parquet(str(proc_dir / "MES_2026-07-01_ticks_dir.parquet"), index=False)
            mnq_ticks2.to_parquet(str(proc_dir / "MNQ_2026-07-01_ticks_dir.parquet"), index=False)

            bus_emit(db2, "CL-1", "SUPPORT", "2026-07-01", 5000.0)
            count2 = run(db2, "2026-07-01", str(proc_dir), cfg)
            assert count2 == 0, f"Expected 0 when MES leads, got {count2}"

        print("[self-test] CORR-2: PASS")
        return True
    except Exception as e:
        print(f"[self-test] CORR-2: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
