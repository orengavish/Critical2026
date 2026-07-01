"""
algo/algo_runner.py
Orchestrates the full algo pipeline: preflight → preprocess → CL algos → CORR algos → scorer → email.

Usage:
    python algo/algo_runner.py --self-test
    python algo/algo_runner.py --preflight
    python algo/algo_runner.py --morning-run [--date YYYY-MM-DD]
"""

import sys
import argparse
from pathlib import Path
from datetime import date as date_cls, timedelta
import yaml
from types import SimpleNamespace


def _load_algo_config():
    cfg_path = Path(__file__).parent / "algo_config.yaml"
    with open(cfg_path) as f:
        raw = yaml.safe_load(f)

    def ns(d):
        if isinstance(d, dict):
            return SimpleNamespace(**{k: ns(v) for k, v in d.items()})
        if isinstance(d, list):
            return d
        return d

    return ns(raw)


def _business_days_back(from_date: date_cls, n: int):
    result = []
    d = from_date
    while len(result) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            result.append(d.isoformat())
    return result


def preflight(config=None, history_path: str = None) -> bool:
    """
    Check that history_path exists and last 2 business days have all 4 symbol CSVs.
    Returns True if all OK, False otherwise. Prints specific missing files.
    """
    if config is None:
        config = _load_algo_config()

    h_path   = Path(history_path or config.data.history_path)
    symbols  = config.data.symbols
    today    = date_cls.today()
    check_days = _business_days_back(today, 2)

    errors = []

    if not h_path.exists():
        errors.append(f"history_path does not exist: {h_path}")
    else:
        for d in check_days:
            for sym in symbols:
                f = h_path / f"{sym}_{d}_trades.csv"
                if not f.exists():
                    errors.append(f"MISSING: {f.name}")

    if errors:
        for e in errors:
            print(f"[preflight] {e}")
        return False

    print("[preflight] PASS")
    return True


def morning_run(date_str: str = None, db_path=None, config=None):
    """
    Full morning pipeline. Raises on critical failure after sending error email.
    """
    config   = config or _load_algo_config()
    date_str = date_str or date_cls.today().isoformat()

    proc_path = config.data.processed_path
    hist_path = config.data.history_path
    symbols   = config.data.symbols

    # Resolve DB path
    if db_path is None:
        from lib.db import _resolve_path
        db_path = _resolve_path()

    def _send_error(step, exc):
        try:
            from lib.mailer import send
            send(f"Critical2026 MORNING ERROR — {step}",
                 f"Step: {step}\nDate: {date_str}\nError: {exc}\n\n"
                 f"critical_lines NOT cleared. Falling back to previous/manual lines.")
        except Exception:
            pass

    # Step 1: Preflight
    if not preflight(config, hist_path):
        _send_error("preflight", "Data path check failed")
        return False

    # Step 2: Preprocess all symbols
    try:
        from algo.preprocessor import run as preprocess
        for sym in symbols:
            try:
                preprocess(date_str, sym, hist_path, proc_path)
            except FileNotFoundError:
                pass  # symbol not available today — skip
    except Exception as e:
        _send_error("preprocessor", e)
        return False

    # Step 3: Clear today's signals
    try:
        from algo.signal_bus import clear_date
        clear_date(db_path, date_str)
    except Exception as e:
        _send_error("signal_bus.clear_date", e)
        return False

    # Step 4: Run CL algos for all symbols
    try:
        from algo.approaches.cl import cl1, cl2, cl3, cl4, cl5
        for sym in symbols:
            try:
                cl1.run(db_path, date_str, sym, proc_path, config)
                cl2.run(db_path, date_str, sym, proc_path, config)
                cl3.run(db_path, date_str, sym, proc_path, config)
                cl5.run(db_path, date_str, sym, proc_path, config)
            except Exception:
                pass  # one symbol failing doesn't abort others
        cl4.run(db_path, date_str, config)
    except Exception as e:
        _send_error("CL algos", e)
        return False

    # Step 5: Run CORR algos
    try:
        from algo.approaches.corr import corr1, corr2, corr3, corr4, corr5
        corr1.run(db_path, date_str, proc_path, config)
        corr2.run(db_path, date_str, proc_path, config)
        corr3.run(db_path, date_str, config)
        corr4.run(db_path, date_str, proc_path, config)
        corr5.run(db_path, date_str, proc_path, config)
    except Exception as e:
        _send_error("CORR algos", e)
        return False

    # Step 6: Score
    try:
        from algo.scorer import score_date
        results = score_date(db_path, date_str, config, proc_path)
    except Exception as e:
        _send_error("scorer", e)
        return False

    # Step 7: Summary email
    try:
        n_total  = len(results)
        n_strong = sum(1 for r in results if r.get("strength_assigned") == 1)
        n_medium = sum(1 for r in results if r.get("strength_assigned") == 2)
        n_weak   = sum(1 for r in results if r.get("strength_assigned") == 3)
        n_enter  = sum(1 for r in results if r.get("entered_pipeline"))
        body = (
            f"Morning run complete for {date_str}\n\n"
            f"Levels scored:     {n_total}\n"
            f"Entered pipeline:  {n_enter}  "
            f"(Strong:{n_strong}  Medium:{n_medium}  Weak:{n_weak})\n\n"
        )
        if results:
            body += "Top levels:\n"
            for r in sorted(results, key=lambda x: -x["total_score"])[:10]:
                body += (f"  {r['price']:.2f} {r['line_type']:<12} "
                         f"score={r['total_score']:.1f} "
                         f"S:{r['axis_source']:.0f} P:{r['axis_param']:.0f} H:{r['axis_history']:.1f}"
                         f"{'  ✓ pipeline' if r['entered_pipeline'] else ''}\n")

        from lib.mailer import send
        send(f"Critical2026 Morning Run {date_str} — {n_enter} lines entered", body)
    except Exception as e:
        print(f"[morning_run] email failed: {e}")  # non-fatal

    return True


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile, os
    from pathlib import Path
    try:
        # 1. Config loads
        cfg = _load_algo_config()
        assert hasattr(cfg, "data"), "Missing data section"
        assert hasattr(cfg, "cl1"),  "Missing cl1 section"
        assert hasattr(cfg, "scorer"), "Missing scorer section"

        # 2. All required files/dirs exist
        algo_dir = Path(__file__).parent
        required_files = [
            algo_dir / "algo_config.yaml",
            algo_dir / "signal_bus.py",
            algo_dir / "preprocessor.py",
            algo_dir / "scorer.py",
            algo_dir / "grader.py",
            algo_dir / "approaches" / "cl" / "cl1.py",
            algo_dir / "approaches" / "cl" / "cl2.py",
            algo_dir / "approaches" / "cl" / "cl3.py",
            algo_dir / "approaches" / "cl" / "cl4.py",
            algo_dir / "approaches" / "cl" / "cl5.py",
            algo_dir / "approaches" / "corr" / "corr1.py",
            algo_dir / "approaches" / "corr" / "corr2.py",
            algo_dir / "approaches" / "corr" / "corr3.py",
            algo_dir / "approaches" / "corr" / "corr4.py",
            algo_dir / "approaches" / "corr" / "corr5.py",
        ]
        missing = [str(f) for f in required_files if not f.exists()]
        assert not missing, f"Missing files: {missing}"

        # 3. Preflight with temp dir + mock CSV files
        with tempfile.TemporaryDirectory() as tmp:
            hist_dir = Path(tmp) / "hist"
            hist_dir.mkdir()

            from datetime import date as dc, timedelta
            today     = dc.today()
            bdays     = _business_days_back(today, 2)
            symbols   = cfg.data.symbols

            for d in bdays:
                for sym in symbols:
                    (hist_dir / f"{sym}_{d}_trades.csv").write_text("timestamp,price,size,side\n")

            cfg2 = SimpleNamespace(
                data=SimpleNamespace(
                    history_path=str(hist_dir),
                    symbols=symbols,
                )
            )
            ok = preflight(cfg2, str(hist_dir))
            assert ok, "Preflight should PASS with mock files"

            # Missing one file
            (hist_dir / f"MES_{bdays[0]}_trades.csv").unlink()
            ok2 = preflight(cfg2, str(hist_dir))
            assert not ok2, "Preflight should FAIL with missing file"

        print("[self-test] algo_runner: PASS")
        return True
    except Exception as e:
        print(f"[self-test] algo_runner: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test",    action="store_true")
    parser.add_argument("--preflight",    action="store_true")
    parser.add_argument("--morning-run",  action="store_true")
    parser.add_argument("--date",         help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    if args.preflight:
        sys.exit(0 if preflight() else 1)

    if args.morning_run:
        ok = morning_run(date_str=args.date)
        sys.exit(0 if ok else 1)
