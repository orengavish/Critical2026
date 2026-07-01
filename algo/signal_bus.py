"""
algo/signal_bus.py
Single write path for all 10 algo approaches to emit detected price levels.

Usage:
    from algo.signal_bus import emit, get_signals_for_date, clear_date, snap_price
"""

import json
import sqlite3
import sys
import argparse
from pathlib import Path

# Ensure project root is on path when running this file directly
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def snap_price(price: float) -> float:
    """Round to nearest 0.25 tick."""
    return round(round(price * 4) / 4, 10)


def emit(db_path, approach_id: str, category: str, date: str, price: float,
         direction: str = None, strength: float = 1.0, confidence: float = 1.0,
         tags: dict = None) -> None:
    """Insert one signal into algo_signals. Price is snapped to 0.25."""
    if category not in ("SUPPORT", "RESISTANCE"):
        raise ValueError(f"category must be SUPPORT or RESISTANCE, got {repr(category)}")
    if not (0.0 <= strength <= 1.0):
        raise ValueError(f"strength must be in [0,1], got {strength}")

    snapped = snap_price(price)
    tags_json = json.dumps(tags) if tags is not None else None

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
            INSERT INTO algo_signals
                (approach_id, category, date, price, direction, strength, confidence, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (approach_id, category, date, snapped, direction, strength, confidence, tags_json))
        con.commit()
    finally:
        con.close()


def get_signals_for_date(db_path, date: str) -> list:
    """Return all signals for a date as list of dicts."""
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM algo_signals WHERE date=? ORDER BY price, approach_id",
            (date,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d["tags"]) if d["tags"] else {}
            result.append(d)
        return result
    finally:
        con.close()


def clear_date(db_path, date: str) -> int:
    """Delete all signals for a date. Returns rows deleted."""
    con = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        cur = con.execute("DELETE FROM algo_signals WHERE date=?", (date,))
        con.commit()
        return cur.rowcount
    finally:
        con.close()


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        # snap_price tests
        assert snap_price(6500.10) == 6500.0,   f"snap_price(6500.10)={snap_price(6500.10)}"
        assert snap_price(6500.20) == 6500.25,  f"snap_price(6500.20)={snap_price(6500.20)}"
        assert snap_price(5000.0)  == 5000.0,   f"snap_price(5000.0)={snap_price(5000.0)}"
        assert snap_price(5000.125) == 5000.0,  f"snap_price(5000.125)={snap_price(5000.125)}"
        assert snap_price(5000.13) == 5000.25,  f"snap_price(5000.13)={snap_price(5000.13)}"

        with tempfile.TemporaryDirectory() as tmp:
            from lib.db import init_db
            db = Path(tmp) / "test.db"
            init_db(db)

            # emit round-trip
            emit(db, "CL-1", "SUPPORT", "2026-07-01", 6500.10, direction="BUY",
                 strength=0.8, tags={"note": "test"})
            emit(db, "CORR-3", "RESISTANCE", "2026-07-01", 6502.0, direction="SELL",
                 strength=0.5)

            signals = get_signals_for_date(db, "2026-07-01")
            assert len(signals) == 2, f"Expected 2, got {len(signals)}"
            assert signals[0]["price"] == 6500.0,    "Price not snapped"
            assert signals[0]["tags"] == {"note": "test"}, "Tags not round-tripped"
            assert signals[1]["category"] == "RESISTANCE"

            # clear_date
            deleted = clear_date(db, "2026-07-01")
            assert deleted == 2, f"Expected 2 deleted, got {deleted}"
            assert get_signals_for_date(db, "2026-07-01") == []

            # validation errors
            try:
                emit(db, "CL-1", "INVALID", "2026-07-01", 5000.0, strength=0.5)
                assert False, "Should have raised"
            except ValueError:
                pass

            try:
                emit(db, "CL-1", "SUPPORT", "2026-07-01", 5000.0, strength=1.5)
                assert False, "Should have raised"
            except ValueError:
                pass

        print("[self-test] signal_bus: PASS")
        return True
    except Exception as e:
        print(f"[self-test] signal_bus: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
