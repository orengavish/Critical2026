"""
algo/grader.py
Post-Session Grader: evaluates how each critical line performed after session close.

Records to line_performance: was_tested, price_respected, trades_won/lost, pnl.
This feeds Axis 3 (history score) going forward.
"""

import sys
import argparse
import sqlite3
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
import numpy as np


def run_post_session(db_path, date: str, config, processed_path: str = None) -> int:
    """
    Grade all critical_lines for date. Returns number of rows written to line_performance.
    """
    tick_tol     = 0.25  # 1 tick — "was tested" if price within 0.25 of level
    respect_dist = 0.5   # 2 ticks — bounce or break threshold

    # Load MES ticks
    ticks = None
    if processed_path:
        tick_path = Path(processed_path) / f"MES_{date}_ticks_dir.parquet"
        if tick_path.exists():
            ticks = pd.read_parquet(str(tick_path))
            try:
                ticks["time_sec"] = pd.to_datetime(ticks["timestamp"]).astype(np.int64) / 1e9
            except Exception:
                ticks["time_sec"] = ticks.index.astype(float)
            ticks = ticks.sort_values("time_sec").reset_index(drop=True)

    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row

    try:
        # Get lines that entered pipeline today
        lines = con.execute(
            "SELECT price, line_type FROM line_scores"
            " WHERE date=? AND entered_pipeline=1",
            (date,)
        ).fetchall()

        if not lines:
            # Fallback: check critical_lines directly
            lines = con.execute(
                "SELECT price, line_type FROM critical_lines WHERE date=?",
                (date,)
            ).fetchall()

        written = 0
        for line in lines:
            price     = float(line["price"])
            line_type = line["line_type"]

            was_tested      = 0
            price_respected = None

            if ticks is not None and len(ticks) > 0:
                prices_arr = ticks["price"].values

                # was_tested: any tick within 1 tick
                near_mask = np.abs(prices_arr - price) <= tick_tol
                was_tested = int(np.any(near_mask))

                if was_tested:
                    # Find first near-touch index
                    near_idxs = np.where(near_mask)[0]
                    first_touch = int(near_idxs[0])

                    # Look at next 20 ticks after touch
                    after = prices_arr[first_touch: first_touch + 20]

                    if len(after) >= 2:
                        if line_type == "SUPPORT":
                            # Bounce: price moved up 2+ ticks after touching
                            price_respected = int(np.any(after > price + respect_dist))
                        else:
                            # Bounce: price moved down 2+ ticks after touching
                            price_respected = int(np.any(after < price - respect_dist))

            # Get trades near this line
            trades = con.execute(
                """SELECT pnl_points FROM verified_trades
                   WHERE fill_price BETWEEN ? AND ?
                   AND fill_time LIKE ?""",
                (price - 0.5, price + 0.5, f"{date}%")
            ).fetchall()

            trades_won  = sum(1 for t in trades if t["pnl_points"] and t["pnl_points"] > 0)
            trades_lost = sum(1 for t in trades if t["pnl_points"] and t["pnl_points"] <= 0)
            pnl_total   = sum(t["pnl_points"] for t in trades if t["pnl_points"])

            con.execute("""
                INSERT INTO line_performance
                    (date, price, line_type, was_tested, price_respected,
                     trades_won, trades_lost, pnl_points_total)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (date, price, line_type, was_tested, price_respected,
                  trades_won, trades_lost, pnl_total))
            written += 1

        con.commit()
        return written

    finally:
        con.close()


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            from lib.db import init_db
            from algo.signal_bus import emit as bus_emit
            from algo.scorer import score_date
            from types import SimpleNamespace
            import pandas as pd, numpy as np

            db = Path(tmp) / "test.db"
            init_db(db)
            proc_dir = Path(tmp) / "proc"
            proc_dir.mkdir()

            # Insert enough signals to pass scoring threshold (score >= 8 = weak)
            for algo in ["CL-1", "CL-2", "CL-3", "CL-4", "CL-5"]:
                bus_emit(db, algo, "SUPPORT", "2026-07-01", 5000.0, strength=0.9)

            cfg = SimpleNamespace(
                scorer=SimpleNamespace(
                    weights=SimpleNamespace(
                        cl_per_algo=2, corr_per_algo=3, divergence_penalty=-3,
                        poc_bonus=3, vah_val_bonus=2, clean_price_bonus=2,
                        session_level_bonus=2, proximity_penalty=-2, history_max=15,
                    ),
                    thresholds=SimpleNamespace(strong=20, medium=14, weak=8),
                    bracket_sizes=[2, 4],
                )
            )
            score_date(db, "2026-07-01", cfg)

            # Build ticks: bounce at 5000 (goes down to 5000, then rises)
            bounce_rows = []
            for i, p in enumerate([5001.0, 5000.5, 5000.0, 5000.5, 5001.0, 5001.5, 5002.0]):
                bounce_rows.append({"timestamp": str(i), "price": p,
                                    "size": 1, "tick_direction": "neutral"})
            bounce_df = pd.DataFrame(bounce_rows)
            bounce_df.to_parquet(str(proc_dir / "MES_2026-07-01_ticks_dir.parquet"), index=False)

            grader_cfg = SimpleNamespace()
            written = run_post_session(db, "2026-07-01", grader_cfg, str(proc_dir))
            assert written >= 1, f"Expected >=1 row written, got {written}"

            con = sqlite3.connect(str(db))
            con.row_factory = sqlite3.Row
            perf = con.execute("SELECT * FROM line_performance WHERE date='2026-07-01'").fetchall()
            con.close()

            assert len(perf) >= 1, "No line_performance rows"
            row = dict(perf[0])
            assert row["was_tested"] == 1, f"Expected was_tested=1, got {row}"
            assert row["price_respected"] == 1, f"Expected bounce (respected=1), got {row}"

            # Test: break scenario (price goes BELOW the support after touching)
            from lib.db import init_db as init2
            db2 = Path(tmp) / "test2.db"
            init2(db2)

            for algo in ["CL-1", "CL-2", "CL-3", "CL-4", "CL-5"]:
                bus_emit(db2, algo, "SUPPORT", "2026-07-01", 5000.0, strength=0.9)
            score_date(db2, "2026-07-01", cfg)

            break_rows = []
            for i, p in enumerate([5001.0, 5000.5, 5000.0, 4999.5, 4999.0, 4998.5]):
                break_rows.append({"timestamp": str(i), "price": p,
                                   "size": 1, "tick_direction": "neutral"})
            pd.DataFrame(break_rows).to_parquet(
                str(proc_dir / "MES_2026-07-01_ticks_dir.parquet"), index=False)

            written2 = run_post_session(db2, "2026-07-01", grader_cfg, str(proc_dir))
            assert written2 >= 1

            con2 = sqlite3.connect(str(db2))
            con2.row_factory = sqlite3.Row
            perf2 = con2.execute("SELECT * FROM line_performance WHERE date='2026-07-01'").fetchall()
            con2.close()
            row2 = dict(perf2[0])
            assert row2["price_respected"] == 0, f"Expected break (respected=0), got {row2}"

            # Test: untested line
            db3 = Path(tmp) / "test3.db"
            init2(db3)
            for algo in ["CL-1", "CL-2", "CL-3", "CL-4", "CL-5"]:
                bus_emit(db3, algo, "SUPPORT", "2026-07-01", 5010.0, strength=0.9)
            score_date(db3, "2026-07-01", cfg)

            # Ticks nowhere near 5010
            far_rows = [{"timestamp": str(i), "price": 5000.0 + i*0.25, "size": 1, "tick_direction": "buy"}
                        for i in range(10)]
            pd.DataFrame(far_rows).to_parquet(
                str(proc_dir / "MES_2026-07-01_ticks_dir.parquet"), index=False)

            run_post_session(db3, "2026-07-01", grader_cfg, str(proc_dir))
            con3 = sqlite3.connect(str(db3))
            con3.row_factory = sqlite3.Row
            perf3 = con3.execute("SELECT * FROM line_performance WHERE date='2026-07-01'").fetchall()
            con3.close()
            row3 = dict(perf3[0])
            assert row3["was_tested"] == 0, f"Expected untested, got {row3}"
            assert row3["price_respected"] is None, f"Expected None for untested, got {row3}"

        print("[self-test] grader: PASS")
        return True
    except Exception as e:
        print(f"[self-test] grader: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--post-session", action="store_true")
    parser.add_argument("--date", help="YYYY-MM-DD (defaults to today)")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    if args.post_session:
        from lib.config_loader import get_config
        from datetime import date as date_cls
        import yaml

        cfg_path = Path(__file__).parent / "algo_config.yaml"
        with open(cfg_path) as f:
            raw = yaml.safe_load(f)
        from types import SimpleNamespace
        algo_cfg = SimpleNamespace()

        date_str = args.date or date_cls.today().isoformat()
        from lib.db import _resolve_path
        db_path = _resolve_path()
        n = run_post_session(db_path, date_str, algo_cfg,
                             processed_path=raw["data"]["processed_path"])
        print(f"Grader: {n} lines recorded for {date_str}")
