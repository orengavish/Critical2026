"""
algo/approaches/corr/corr1.py
CORR-1: Cross-Instrument Volume Node Alignment

Checks if MNQ/MYM/M2K have high-volume nodes at the same relative price as MES CL-1 levels.
Institutional signature: multiple instruments with volume peaks at the same normalized level.
"""

import sys
import argparse
import sqlite3
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import pandas as pd


def _load_open(processed_path: str, symbol: str, date: str) -> float:
    sess_path = Path(processed_path) / f"{symbol}_{date}_sessions.parquet"
    if not sess_path.exists():
        return None
    sess = pd.read_parquet(str(sess_path))
    return float(sess.iloc[0]["session_open"])


def run(db_path, date: str, processed_path: str, config) -> int:
    from algo.signal_bus import emit, snap_price

    other_symbols = ["MNQ", "MYM", "M2K"]
    tick_tol      = 0.25  # 1 tick

    # Load MES CL-1 signals
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        mes_signals = con.execute(
            "SELECT price, category FROM algo_signals"
            " WHERE approach_id='CL-1' AND date=? AND price IS NOT NULL",
            (date,)
        ).fetchall()
        # Load all other CL-1 signals for today
        other_signals = con.execute(
            "SELECT approach_id, price, category FROM algo_signals"
            " WHERE approach_id='CL-1' AND date=?",
            (date,)
        ).fetchall()
    finally:
        con.close()

    if not mes_signals:
        return 0

    mes_open = _load_open(processed_path, "MES", date)
    if mes_open is None:
        return 0

    # Build lookup: symbol -> set of prices from CL-1 (using sessions to identify symbol)
    # Since we store all symbols' signals under approach_id='CL-1', we need symbol info.
    # We store symbol in tags — but actually, we tag the signal with symbol info.
    # Use a pragmatic approach: load per-symbol from sessions file presence.
    other_opens = {}
    for sym in other_symbols:
        o = _load_open(processed_path, sym, date)
        if o:
            other_opens[sym] = o

    # Re-query with symbol tag
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        all_cl1 = con.execute(
            "SELECT price, category, tags FROM algo_signals"
            " WHERE approach_id='CL-1' AND date=?",
            (date,)
        ).fetchall()
    finally:
        con.close()

    import json
    # Build per-symbol signal sets
    sym_prices = {sym: set() for sym in other_symbols}
    for row in all_cl1:
        tags = json.loads(row["tags"]) if row["tags"] else {}
        sym  = tags.get("symbol")
        if sym in sym_prices:
            sym_prices[sym].add(float(row["price"]))

    emitted = 0
    for mes_sig in mes_signals:
        mes_level    = float(mes_sig["price"])
        confirming   = []

        for sym, sym_open in other_opens.items():
            if mes_open <= 0:
                continue
            equiv = snap_price(mes_level * (sym_open / mes_open))

            # Check if that symbol has a CL-1 signal within 1 tick
            matched = any(abs(p - equiv) <= tick_tol for p in sym_prices[sym])
            if matched:
                confirming.append(sym)

        if confirming:
            strength = len(confirming) / 3.0
            emit(db_path, "CORR-1", mes_sig["category"], date, mes_level,
                 direction="BUY" if mes_sig["category"] == "SUPPORT" else "SELL",
                 strength=min(1.0, strength),
                 tags={"confirming": confirming, "count": len(confirming)})
            emitted += 1

    return emitted


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile, json
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

            # MES open=5000, MNQ open=20000 (4x ratio)
            # MES CL-1 at 5000 → MNQ equivalent = 20000
            def make_sessions(sym, open_p):
                return pd.DataFrame([{
                    "date": "2026-07-01", "symbol": sym, "poc": open_p,
                    "vah": open_p + 10, "val": open_p - 10,
                    "session_open": open_p, "session_high": open_p + 20,
                    "session_low": open_p - 20, "total_volume": 10000,
                }])

            make_sessions("MES", 5000.0).to_parquet(str(proc_dir / "MES_2026-07-01_sessions.parquet"), index=False)
            make_sessions("MNQ", 20000.0).to_parquet(str(proc_dir / "MNQ_2026-07-01_sessions.parquet"), index=False)

            # MES CL-1 signal at 5000 (primary)
            bus_emit(db, "CL-1", "SUPPORT", "2026-07-01", 5000.0,
                     tags={"symbol": "MES"})

            # MNQ CL-1 signal at 20000 (confirming)
            bus_emit(db, "CL-1", "SUPPORT", "2026-07-01", 20000.0,
                     tags={"symbol": "MNQ"})

            cfg = SimpleNamespace(corr=SimpleNamespace(lead_window_seconds=60))
            count = run(db, "2026-07-01", str(proc_dir), cfg)
            assert count >= 1, f"Expected >=1 CORR-1 signal, got {count}"

            signals = get_signals_for_date(db, "2026-07-01")
            corr1 = [s for s in signals if s["approach_id"] == "CORR-1"]
            assert len(corr1) >= 1, f"No CORR-1 signals: {signals}"
            assert "MNQ" in corr1[0]["tags"].get("confirming", []), \
                f"MNQ not in confirming: {corr1[0]['tags']}"

        print("[self-test] CORR-1: PASS")
        return True
    except Exception as e:
        print(f"[self-test] CORR-1: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
