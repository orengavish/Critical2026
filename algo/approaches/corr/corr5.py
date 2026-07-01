"""
algo/approaches/corr/corr5.py
CORR-5: Multi-Instrument POC Confluence

3+ instruments with POC at same normalized price = consensus fair value.
Decisive bounce or break expected at this level.
"""

import sys
import argparse
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import pandas as pd


def _load_sessions(processed_path: str, symbol: str, date: str) -> dict | None:
    sess_path = Path(processed_path) / f"{symbol}_{date}_sessions.parquet"
    if not sess_path.exists():
        return None
    sess = pd.read_parquet(str(sess_path))
    row  = sess.iloc[0]
    return {
        "poc":          float(row["poc"]),
        "session_open": float(row["session_open"]),
    }


def run(db_path, date: str, processed_path: str, config) -> int:
    from algo.signal_bus import emit, snap_price

    symbols        = ["MES", "MNQ", "MYM", "M2K"]
    min_instruments = config.corr.confluence_min_instruments
    tick_range     = config.corr.confluence_tick_range * 0.25  # in price units

    # Load sessions for all symbols
    sessions = {}
    for sym in symbols:
        s = _load_sessions(processed_path, sym, date)
        if s:
            sessions[sym] = s

    if "MES" not in sessions or len(sessions) < 2:
        return 0

    mes_open = sessions["MES"]["session_open"]
    if mes_open <= 0:
        return 0

    # Convert each instrument's POC to MES-equivalent price
    poc_equiv = {}
    for sym, s in sessions.items():
        sym_open = s["session_open"]
        if sym_open <= 0:
            continue
        equiv = snap_price(s["poc"] * (mes_open / sym_open))
        poc_equiv[sym] = equiv

    if len(poc_equiv) < 2:
        return 0

    # Cluster POC equivalents within tick_range
    prices   = list(poc_equiv.values())
    symbols_ = list(poc_equiv.keys())
    used     = [False] * len(prices)
    clusters = []

    for i in range(len(prices)):
        if used[i]:
            continue
        cluster = [(symbols_[i], prices[i])]
        used[i] = True
        for j in range(i + 1, len(prices)):
            if not used[j] and abs(prices[j] - prices[i]) <= tick_range:
                cluster.append((symbols_[j], prices[j]))
                used[j] = True
        clusters.append(cluster)

    emitted = 0
    for cluster in clusters:
        if len(cluster) >= min_instruments:
            centroid   = snap_price(sum(p for _, p in cluster) / len(cluster))
            inst_names = [sym for sym, _ in cluster]
            poc_list   = [round(p, 4) for _, p in cluster]
            strength   = min(1.0, len(cluster) / 4.0)
            # Emit as SUPPORT (no directional bias — just a confluence zone)
            emit(db_path, "CORR-5", "SUPPORT", date, centroid,
                 strength=strength,
                 tags={"instruments": inst_names, "poc_prices": poc_list,
                       "count": len(cluster)})
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

            def make_sessions(sym, open_p, poc):
                return pd.DataFrame([{
                    "date": "2026-07-01", "symbol": sym, "poc": poc,
                    "vah": poc + 5, "val": poc - 5,
                    "session_open": open_p, "session_high": poc + 10,
                    "session_low": poc - 10, "total_volume": 1000,
                }])

            # 3 instruments with POC at equivalent MES price 5000
            # MES open=5000, POC=5000
            # MNQ open=20000, POC=20000 → equiv = 20000 * (5000/20000) = 5000
            # MYM open=40000, POC=40000 → equiv = 40000 * (5000/40000) = 5000
            make_sessions("MES", 5000.0, 5000.0).to_parquet(str(proc_dir / "MES_2026-07-01_sessions.parquet"), index=False)
            make_sessions("MNQ", 20000.0, 20000.0).to_parquet(str(proc_dir / "MNQ_2026-07-01_sessions.parquet"), index=False)
            make_sessions("MYM", 40000.0, 40000.0).to_parquet(str(proc_dir / "MYM_2026-07-01_sessions.parquet"), index=False)

            cfg = SimpleNamespace(corr=SimpleNamespace(
                confluence_min_instruments=3, confluence_tick_range=2))
            count = run(db, "2026-07-01", str(proc_dir), cfg)
            assert count >= 1, f"Expected >=1 CORR-5 signal with 3 instruments, got {count}"

            signals = get_signals_for_date(db, "2026-07-01")
            corr5 = [s for s in signals if s["approach_id"] == "CORR-5"]
            assert len(corr5) >= 1, "No CORR-5 signals"
            assert corr5[0]["tags"]["count"] >= 3

            # Test: only 2 instruments — should NOT emit
            from lib.db import init_db as init2
            db2 = Path(tmp) / "test2.db"
            init2(db2)
            # Remove MYM
            (proc_dir / "MYM_2026-07-01_sessions.parquet").unlink(missing_ok=True)

            count2 = run(db2, "2026-07-01", str(proc_dir), cfg)
            assert count2 == 0, f"Expected 0 with only 2 instruments, got {count2}"

        print("[self-test] CORR-5: PASS")
        return True
    except Exception as e:
        print(f"[self-test] CORR-5: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
