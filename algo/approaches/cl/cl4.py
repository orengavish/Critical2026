"""
algo/approaches/cl/cl4.py
CL-4: Multi-Session Persistence

Levels that appeared in CL-1 output for N consecutive days = true market structure.
"""

import sys
import argparse
import sqlite3
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from datetime import date as date_cls, timedelta
from collections import Counter


def _get_business_days_before(date_str: str, n: int):
    """Return n business days before (and not including) date_str."""
    d = date_cls.fromisoformat(date_str)
    result = []
    while len(result) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            result.append(d.isoformat())
    return result


def run(db_path, date: str, config) -> int:
    from algo.signal_bus import emit, snap_price

    lookback  = config.cl4.lookback_days
    min_pers  = config.cl4.min_persistence

    prior_days = _get_business_days_before(date, lookback)

    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" * len(prior_days))
        rows = con.execute(
            f"SELECT date, price, category FROM algo_signals"
            f" WHERE approach_id='CL-1' AND date IN ({placeholders})",
            prior_days
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return 0

    # Cluster prices within 0.25 tick
    # Group by snapped price (already snapped) + category
    from collections import defaultdict
    clusters = defaultdict(lambda: {"dates": set(), "categories": []})

    for row in rows:
        key = (snap_price(row["price"]), )
        clusters[key]["dates"].add(row["date"])
        clusters[key]["categories"].append(row["category"])

    emitted = 0
    for (price,), data in clusters.items():
        days_present = len(data["dates"])
        if days_present >= min_pers:
            # majority vote for category
            cat_counts = Counter(data["categories"])
            category   = cat_counts.most_common(1)[0][0]
            strength   = days_present / lookback
            emit(db_path, "CL-4", category, date, price,
                 direction="BUY" if category == "SUPPORT" else "SELL",
                 strength=strength,
                 tags={"persistence_days": days_present, "lookback": lookback})
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

            # Business days before 2026-07-01: 2026-06-30, 2026-06-29, 2026-06-26, 2026-06-25, 2026-06-24
            for d in ["2026-06-26", "2026-06-29", "2026-06-30"]:
                bus_emit(db, "CL-1", "SUPPORT", d, 5000.0, strength=0.8)

            # Insert CL-1 signal for only 2 days at 5005.0 (should NOT be emitted at min_persistence=3)
            for d in ["2026-06-29", "2026-06-30"]:
                bus_emit(db, "CL-1", "SUPPORT", d, 5005.0, strength=0.7)

            cfg = SimpleNamespace(cl4=SimpleNamespace(lookback_days=5, min_persistence=3))
            count = run(db, "2026-07-01", cfg)
            assert count >= 1, f"Expected >=1 signal, got {count}"

            signals = get_signals_for_date(db, "2026-07-01")
            cl4_signals = [s for s in signals if s["approach_id"] == "CL-4"]
            prices = {s["price"] for s in cl4_signals}

            assert 5000.0 in prices, f"Expected 5000.0 in CL-4 output, got {prices}"
            assert 5005.0 not in prices, f"5005.0 should NOT be in CL-4 (only 2 days)"

        print("[self-test] CL-4: PASS")
        return True
    except Exception as e:
        print(f"[self-test] CL-4: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
