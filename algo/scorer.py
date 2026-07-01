"""
algo/scorer.py
Composite Scorer: aggregates all algo signals into a score per price level,
then gates into critical_lines table based on thresholds.

Axis 1 — SOURCE (max 25): CL algos x2, CORR algos x3, divergence penalty -3
Axis 2 — PARAM  (max 10): POC, VAH/VAL, clean price, session level, proximity
Axis 3 — HISTORY (max 15): win_rate from line_performance over last 20 sessions
"""

import sys
import argparse
import json
import sqlite3
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from datetime import date as date_cls, timedelta

import pandas as pd


CL_ALGOS   = {"CL-1", "CL-2", "CL-3", "CL-4", "CL-5"}
CORR_ALGOS = {"CORR-1", "CORR-2", "CORR-4", "CORR-5"}


def _load_config():
    import yaml
    cfg_path = Path(__file__).parent / "algo_config.yaml"
    with open(cfg_path) as f:
        raw = yaml.safe_load(f)
    from types import SimpleNamespace

    def ns(d):
        if isinstance(d, dict):
            return SimpleNamespace(**{k: ns(v) for k, v in d.items()})
        return d

    return ns(raw)


def score_date(db_path, date: str, config, processed_path: str = None) -> list:
    """
    Score all signals for date, write to line_scores, gate into critical_lines.
    Returns list of dicts with scoring details.
    """
    snap = lambda p: round(round(float(p) * 4) / 4, 10)

    w = config.scorer.weights
    t = config.scorer.thresholds

    # Load session data for Axis 2
    session_data = None
    if processed_path:
        sess_path = Path(processed_path) / f"MES_{date}_sessions.parquet"
        if sess_path.exists():
            sess = pd.read_parquet(str(sess_path))
            session_data = sess.iloc[0].to_dict()

    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row

    try:
        rows = con.execute(
            "SELECT approach_id, category, price, tags FROM algo_signals WHERE date=?",
            (date,)
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return []

    # Group by snapped price + majority category
    from collections import defaultdict
    buckets = defaultdict(lambda: {"approaches": set(), "categories": [], "tags_list": []})
    for row in rows:
        p    = snap(row["price"])
        tags = json.loads(row["tags"]) if row["tags"] else {}
        buckets[p]["approaches"].add(row["approach_id"])
        buckets[p]["categories"].append(row["category"])
        buckets[p]["tags_list"].append((row["approach_id"], tags))

    results = []
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row

    try:
        min_bracket = min(config.scorer.bracket_sizes) if hasattr(config.scorer, "bracket_sizes") else 2

        for price, data in buckets.items():
            from collections import Counter
            cat_counts = Counter(data["categories"])
            line_type  = cat_counts.most_common(1)[0][0]

            # ── Axis 1: SOURCE ─────────────────────────────────────────────
            approaches = data["approaches"]
            cl_hit     = len(approaches & CL_ALGOS)
            corr_hit   = len(approaches & CORR_ALGOS)

            has_divergence = any(
                tags.get("divergence_warning") for _, tags in data["tags_list"]
            )

            axis_source = (cl_hit * w.cl_per_algo +
                           corr_hit * w.corr_per_algo +
                           (w.divergence_penalty if has_divergence else 0))

            # ── Axis 2: PARAM ──────────────────────────────────────────────
            axis_param = 0
            if session_data:
                poc = session_data.get("poc", 0)
                vah = session_data.get("vah", 0)
                val = session_data.get("val", 0)
                s_open = session_data.get("session_open", 0)

                if abs(price - poc) <= 0.25:
                    axis_param += w.poc_bonus
                if abs(price - vah) <= 0.25 or abs(price - val) <= 0.25:
                    axis_param += w.vah_val_bonus
                if price % 5.0 < 0.25 or price % 5.0 > 4.75:
                    axis_param += w.clean_price_bonus
                if abs(price - s_open) <= 0.25:
                    axis_param += w.session_level_bonus
                if abs(price - s_open) < (2 * min_bracket):
                    axis_param += w.proximity_penalty

            axis_param = min(10, max(-10, axis_param))

            # ── Axis 3: HISTORY ────────────────────────────────────────────
            # Last 20 sessions for this price ±0.25
            history_rows = con.execute(
                "SELECT trades_won, trades_lost FROM line_performance"
                " WHERE price BETWEEN ? AND ?"
                " ORDER BY date DESC LIMIT 20",
                (price - 0.25, price + 0.25)
            ).fetchall()

            if history_rows:
                total_won  = sum(r["trades_won"]  for r in history_rows)
                total_lost = sum(r["trades_lost"] for r in history_rows)
                win_rate   = total_won / (total_won + total_lost + 0.001)
            else:
                win_rate = 0.0

            axis_history = w.history_max * win_rate

            # ── Total ──────────────────────────────────────────────────────
            total_score  = axis_source + axis_param + axis_history
            sources_json = json.dumps({
                a: (a in approaches) for a in sorted(CL_ALGOS | CORR_ALGOS | {"CORR-3"})
            })

            # ── Gate ───────────────────────────────────────────────────────
            entered_pipeline  = 0
            strength_assigned = None

            if total_score >= t.strong:
                entered_pipeline, strength_assigned = 1, 1
            elif total_score >= t.medium:
                entered_pipeline, strength_assigned = 1, 2
            elif total_score >= t.weak:
                entered_pipeline, strength_assigned = 1, 3

            # Write to line_scores
            con.execute("""
                INSERT INTO line_scores
                    (date, price, line_type, total_score, axis_source, axis_param, axis_history,
                     sources_json, entered_pipeline, strength_assigned)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (date, price, line_type, total_score, axis_source, axis_param, axis_history,
                  sources_json, entered_pipeline, strength_assigned))

            # Write to critical_lines if above threshold
            if entered_pipeline:
                con.execute("""
                    INSERT INTO critical_lines (symbol, date, line_type, price, strength)
                    VALUES (?, ?, ?, ?, ?)
                """, ("MES", date, line_type, price, strength_assigned))

            con.commit()

            results.append({
                "price":             price,
                "line_type":         line_type,
                "total_score":       total_score,
                "axis_source":       axis_source,
                "axis_param":        axis_param,
                "axis_history":      axis_history,
                "entered_pipeline":  entered_pipeline,
                "strength_assigned": strength_assigned,
                "sources":           list(approaches),
            })

    finally:
        con.close()

    return results


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            from lib.db import init_db
            from algo.signal_bus import emit as bus_emit
            from types import SimpleNamespace

            db = Path(tmp) / "test.db"
            init_db(db)

            def make_cfg(strong=20, medium=14, weak=8):
                return SimpleNamespace(
                    scorer=SimpleNamespace(
                        weights=SimpleNamespace(
                            cl_per_algo=2, corr_per_algo=3, divergence_penalty=-3,
                            poc_bonus=3, vah_val_bonus=2, clean_price_bonus=2,
                            session_level_bonus=2, proximity_penalty=-2, history_max=15,
                        ),
                        thresholds=SimpleNamespace(strong=strong, medium=medium, weak=weak),
                        bracket_sizes=[2, 4],
                    )
                )

            cfg = make_cfg()

            # 3 CL algos + 2 CORR algos = axis_source = 3*2 + 2*3 = 12
            for algo in ["CL-1", "CL-2", "CL-3"]:
                bus_emit(db, algo, "SUPPORT", "2026-07-01", 5000.0, strength=0.8)
            for algo in ["CORR-1", "CORR-2"]:
                bus_emit(db, algo, "SUPPORT", "2026-07-01", 5000.0, strength=0.7)

            results = score_date(db, "2026-07-01", cfg)
            assert len(results) == 1, f"Expected 1 result, got {len(results)}"
            r = results[0]
            assert r["axis_source"] == 12, f"Expected axis_source=12, got {r['axis_source']}"

            # With divergence penalty
            from lib.db import init_db as init2
            db2 = Path(tmp) / "test2.db"
            init2(db2)

            for algo in ["CL-1", "CL-2", "CL-3"]:
                bus_emit(db2, algo, "SUPPORT", "2026-07-01", 5000.0, strength=0.8)
            for algo in ["CORR-1", "CORR-2"]:
                bus_emit(db2, algo, "SUPPORT", "2026-07-01", 5000.0, strength=0.7)
            bus_emit(db2, "CORR-3", "SUPPORT", "2026-07-01", 5000.0,
                     strength=0.0, tags={"divergence_warning": True})

            results2 = score_date(db2, "2026-07-01", cfg)
            r2 = results2[0]
            assert r2["axis_source"] == 9, f"Expected axis_source=9 with divergence, got {r2['axis_source']}"

            # Test gating: score >= 20 → entered_pipeline=1, strength=1
            db3 = Path(tmp) / "test3.db"
            init2(db3)
            for algo in ["CL-1", "CL-2", "CL-3", "CL-4", "CL-5"]:
                bus_emit(db3, algo, "SUPPORT", "2026-07-01", 5000.0, strength=0.9)
            for algo in ["CORR-1", "CORR-2", "CORR-4", "CORR-5"]:
                bus_emit(db3, algo, "SUPPORT", "2026-07-01", 5000.0, strength=0.9)

            results3 = score_date(db3, "2026-07-01", cfg)
            r3 = results3[0]
            # axis_source = 5*2 + 4*3 = 22, total >= 20
            assert r3["entered_pipeline"] == 1, f"Expected entered_pipeline=1, got {r3}"
            assert r3["strength_assigned"] == 1, f"Expected strength=1, got {r3}"

            # Verify critical_lines row was inserted
            con = sqlite3.connect(str(db3))
            count = con.execute("SELECT COUNT(*) FROM critical_lines WHERE date='2026-07-01'").fetchone()[0]
            con.close()
            assert count == 1, f"Expected 1 critical_line, got {count}"

        print("[self-test] scorer: PASS")
        return True
    except Exception as e:
        print(f"[self-test] scorer: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
