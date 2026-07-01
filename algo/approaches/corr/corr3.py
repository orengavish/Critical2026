"""
algo/approaches/corr/corr3.py
CORR-3: Divergence Risk Flag

Flags MES levels where 2+ other instruments show the opposite direction.
Divergence = unclear institutional intent = higher risk. Penalizes score by -3.
"""

import sys
import argparse
import sqlite3
import json
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def run(db_path, date: str, config) -> int:
    from algo.signal_bus import emit, snap_price

    tick_tol = 0.25

    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        # MES CL-1 signals
        mes_signals = con.execute(
            "SELECT price, category FROM algo_signals"
            " WHERE approach_id='CL-1' AND date=?",
            (date,)
        ).fetchall()
        # All CL-1 signals with symbol tag
        all_cl1 = con.execute(
            "SELECT price, category, tags FROM algo_signals"
            " WHERE approach_id='CL-1' AND date=?",
            (date,)
        ).fetchall()
        # CORR-1 confirming instruments per MES level
        corr1_signals = con.execute(
            "SELECT price, tags FROM algo_signals"
            " WHERE approach_id='CORR-1' AND date=?",
            (date,)
        ).fetchall()
    finally:
        con.close()

    if not mes_signals:
        return 0

    # Build: price -> confirming instruments (from CORR-1)
    confirmed_instruments = {}
    for row in corr1_signals:
        tags = json.loads(row["tags"]) if row["tags"] else {}
        confirmed_instruments[float(row["price"])] = tags.get("confirming", [])

    # Build per-symbol category lookup
    sym_signals = {}
    for row in all_cl1:
        tags = json.loads(row["tags"]) if row["tags"] else {}
        sym  = tags.get("symbol")
        if sym and sym != "MES":
            if sym not in sym_signals:
                sym_signals[sym] = []
            sym_signals[sym].append((float(row["price"]), row["category"]))

    emitted = 0
    for mes_sig in mes_signals:
        mes_level   = float(mes_sig["price"])
        mes_cat     = mes_sig["category"]
        instruments = confirmed_instruments.get(mes_level, list(sym_signals.keys()))

        contradictions = 0
        for sym in instruments:
            for sym_price, sym_cat in sym_signals.get(sym, []):
                if abs(sym_price - mes_level) <= tick_tol * 4:  # rough proximity
                    if sym_cat != mes_cat:
                        contradictions += 1
                        break

        if contradictions >= 2:
            emit(db_path, "CORR-3", mes_cat, date, mes_level,
                 strength=0.0,  # purely a penalty signal
                 tags={"divergence_warning": True, "contradiction_count": contradictions})
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

            db = Path(tmp) / "test.db"
            init_db(db)

            # MES: SUPPORT at 5000
            bus_emit(db, "CL-1", "SUPPORT", "2026-07-01", 5000.0, tags={"symbol": "MES"})
            # MNQ: RESISTANCE at 5001 (roughly same zone, opposite direction)
            bus_emit(db, "CL-1", "RESISTANCE", "2026-07-01", 5001.0, tags={"symbol": "MNQ"})
            # MYM: RESISTANCE at 5000 (opposite direction)
            bus_emit(db, "CL-1", "RESISTANCE", "2026-07-01", 5000.0, tags={"symbol": "MYM"})

            cfg = SimpleNamespace(corr=SimpleNamespace())
            count = run(db, "2026-07-01", cfg)
            assert count >= 1, f"Expected >=1 divergence signal, got {count}"

            signals = get_signals_for_date(db, "2026-07-01")
            corr3 = [s for s in signals if s["approach_id"] == "CORR-3"]
            assert len(corr3) >= 1, "No CORR-3 signals found"
            assert corr3[0]["tags"].get("divergence_warning") is True

        print("[self-test] CORR-3: PASS")
        return True
    except Exception as e:
        print(f"[self-test] CORR-3: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
