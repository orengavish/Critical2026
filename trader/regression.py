"""
regression.py
Regression test runner for Galao (R-REG-01 to R-REG-10).

Three test layers:
  Layer 1: self-tests (all --self-test flags)
  Layer 2: feature/logic tests (toggle, bracket math, DB state machine)
  Layer 3: IB integration (submits a real LMT far below market, cancels immediately)

Output: [PASS/FAIL/SKIP] layer: test_name (Xs) -- reason
Summary line at the end.
Results also written to logs/regression.log (R-REG-07).

Usage:
    python regression.py                    # all layers
    python regression.py --quick            # layers 1+2 only
    python regression.py --layer3-only      # layer 3 only
    python regression.py --program broker   # filter by component name
    python regression.py --self-test        # validate regression runner itself

Self-test:
    python regression.py --self-test
"""

import sys
import time
import subprocess
import argparse
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
import sys; sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None


# ── Result types ──────────────────────────────────────────────────────────────

class Result:
    def __init__(self, layer: int, name: str, status: str,
                 elapsed: float, reason: str = ""):
        self.layer   = layer
        self.name    = name
        self.status  = status   # PASS | FAIL | SKIP
        self.elapsed = elapsed
        self.reason  = reason

    def __str__(self):
        r = f"[{self.status:<4}] L{self.layer}: {self.name:<40} ({self.elapsed:.1f}s)"
        if self.reason:
            r += f" -- {self.reason}"
        return r


def _run(fn, layer: int, name: str) -> Result:
    t0 = time.time()
    try:
        ok = fn()
        status = "PASS" if ok else "FAIL"
        reason = "" if ok else "returned False"
    except Exception as e:
        status = "FAIL"
        reason = str(e)
    elapsed = time.time() - t0
    return Result(layer, name, status, elapsed, reason)


def _skip(layer: int, name: str, reason: str) -> Result:
    return Result(layer, name, "SKIP", 0.0, reason)


# ── Layer 1: self-tests ───────────────────────────────────────────────────────

SELF_TEST_MODULES = [
    ("lib.config_loader",  "config_loader"),
    ("lib.logger",         "logger"),
    ("lib.db",             "db"),
    ("lib.order_builder",  "order_builder"),
    ("lib.critical_lines", "critical_lines"),
    ("lib.ib_client",      "ib_client"),
]

SELF_TEST_SCRIPTS = [
    ("release_notes.py",     "release_notes"),
    ("preflight.py",         "preflight"),
    ("broker.py",            "broker"),
    ("decider.py",           "decider"),
    ("position_manager.py",  "position_manager"),
    ("fetcher.py",           "fetcher"),
]


def run_layer1(program_filter: str = None) -> list[Result]:
    results = []
    all_tests = (
        [(f"-m {m}", n) for m, n in SELF_TEST_MODULES] +
        [(s, n) for s, n in SELF_TEST_SCRIPTS]
    )

    for cmd_arg, name in all_tests:
        if program_filter and program_filter not in name:
            continue
        t0 = time.time()
        if cmd_arg.startswith("-m "):
            cmd = ["python", "-m", cmd_arg[3:], "--self-test"]
        else:
            cmd = ["python", cmd_arg, "--self-test"]

        r = subprocess.run(cmd, capture_output=True, text=True)
        elapsed = time.time() - t0
        out = (r.stdout + r.stderr).strip()
        last = [l for l in out.splitlines() if "self-test" in l.lower()]
        if r.returncode == 0:
            results.append(Result(1, name, "PASS", elapsed))
        else:
            reason = last[-1] if last else out[-200:]
            results.append(Result(1, name, "FAIL", elapsed, reason))

    return results


# ── Layer 2: feature/logic tests ─────────────────────────────────────────────

def _test_toggle_rule():
    from lib.order_builder import determine_entry_type
    assert determine_entry_type("BUY",  6520, 6500) == "LMT"
    assert determine_entry_type("BUY",  6480, 6500) == "STP"
    assert determine_entry_type("SELL", 6520, 6500) == "STP"
    assert determine_entry_type("SELL", 6480, 6500) == "LMT"
    assert determine_entry_type("BUY",  6500, 6500) == "LMT"  # at line = above
    assert determine_entry_type("SELL", 6500, 6500) == "STP"
    return True


def _test_bracket_prices():
    from lib.order_builder import calc_bracket_prices, round_tick
    tick = 0.25

    # LMT BUY at 6500, bracket=4
    p = calc_bracket_prices("BUY", "LMT", 6500.0, 4.0, tick)
    assert p["entry_price"] == 6500.0
    assert p["tp_price"]    == 6504.0
    assert p["sl_price"]    == 6496.0

    # STP SELL at 6500, bracket=2
    p = calc_bracket_prices("SELL", "STP", 6500.0, 2.0, tick)
    assert p["entry_price"] == 6499.75
    assert p["tp_price"]    == 6497.75
    assert p["sl_price"]    == 6501.75

    # Symmetry: TP dist == SL dist
    for direction in ("BUY", "SELL"):
        for entry_type in ("LMT", "STP"):
            for bracket in (2.0, 4.0):
                p = calc_bracket_prices(direction, entry_type, 6500.0, bracket, tick)
                entry = p["entry_price"]
                if direction == "BUY":
                    tp_dist = p["tp_price"] - entry
                    sl_dist = entry - p["sl_price"]
                else:
                    tp_dist = entry - p["tp_price"]
                    sl_dist = p["sl_price"] - entry
                assert abs(tp_dist - sl_dist) < 0.001, \
                    f"Asymmetric: {direction} {entry_type} {bracket}: tp={tp_dist} sl={sl_dist}"
    return True


def _test_tick_rounding():
    from lib.order_builder import round_tick
    assert round_tick(6500.1,  0.25) == 6500.0
    assert round_tick(6500.13, 0.25) == 6500.25
    assert round_tick(6500.12, 0.25) == 6500.0
    assert round_tick(6500.0,  0.25) == 6500.0
    assert round_tick(6500.25, 0.25) == 6500.25
    return True


def _test_db_state_machine():
    import tempfile
    from pathlib import Path
    from lib.db import init_db, get_db, update_command_status

    VALID_TRANSITIONS = [
        ("PENDING",    "SUBMITTING"),
        ("SUBMITTING", "SUBMITTED"),
        ("SUBMITTED",  "FILLED"),
        ("FILLED",     "EXITING"),
        ("EXITING",    "CLOSED"),
        ("SUBMITTING", "ERROR"),
        ("SUBMITTED",  "CANCELLED"),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_db(db_path)

        for from_status, to_status in VALID_TRANSITIONS:
            with get_db(db_path) as con:
                con.execute("""
                    INSERT INTO commands
                        (symbol, line_price, line_type, line_strength,
                         direction, entry_type, entry_price, tp_price, sl_price,
                         bracket_size, status)
                    VALUES ('MES',6500,2,'SUPPORT','BUY','LMT',6500,6502,6498,2,?)
                """, (from_status,))
                cid = con.execute("SELECT last_insert_rowid()").fetchone()[0]

            with get_db(db_path) as con:
                update_command_status(con, cid, to_status)

            with get_db(db_path) as con:
                row = con.execute("SELECT status FROM commands WHERE id=?", (cid,)).fetchone()
            assert row["status"] == to_status, \
                f"Transition {from_status}->{to_status} failed: got {row['status']}"
    return True


def _test_claim_lock():
    import tempfile
    from pathlib import Path
    from lib.db import init_db, get_db
    from broker import _claim_command

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_db(db_path)

        with get_db(db_path) as con:
            con.execute("""
                INSERT INTO commands
                    (symbol, line_price, line_type, line_strength,
                     direction, entry_type, entry_price, tp_price, sl_price,
                     bracket_size)
                VALUES ('MES',6500,2,'SUPPORT','BUY','LMT',6500,6502,6498,2)
            """)
            cid = con.execute("SELECT last_insert_rowid()").fetchone()[0]

        assert _claim_command(db_path, cid),     "First claim should succeed"
        assert not _claim_command(db_path, cid), "Second claim should fail"
    return True


def _test_replenishment_no_double():
    import tempfile
    from pathlib import Path
    from lib.db import init_db, get_db, update_command_status
    from lib.critical_lines import get_file_path, load_critical_lines
    from decider import replenish, _now_utc

    cfg = __import__("lib.config_loader", fromlist=["get_config"]).get_config()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path  = tmp_path / "test.db"
        cl_dir   = tmp_path / "cl"
        cl_dir.mkdir()
        init_db(db_path)

        today = date.today().strftime("%Y-%m-%d")
        fp = get_file_path("MES", today, cl_dir)
        fp.write_text("SUPPORT, 6490.0, 2\n")
        load_critical_lines("MES", today, db_path, cl_dir)

        with get_db(db_path) as con:
            con.execute("""
                INSERT INTO commands
                    (symbol, line_price, line_type, line_strength,
                     direction, entry_type, entry_price, tp_price, sl_price,
                     bracket_size, status, fill_price, fill_time)
                VALUES ('MES',6490,'SUPPORT',2,'BUY','LMT',6490,6492,6488,2,
                        'FILLED',6490.0,?)
            """, (_now_utc(),))

        n1 = replenish("MES", today, 6500.0, cfg, db_path)
        n2 = replenish("MES", today, 6500.0, cfg, db_path)
        assert n1 == 1, f"Expected 1 replenishment, got {n1}"
        assert n2 == 0, f"Expected 0 on second call, got {n2}"
    return True


def _test_critical_lines_parser():
    import tempfile
    from pathlib import Path
    from lib.critical_lines import parse_file

    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "test.txt"

        # Valid file
        f.write_text("# comment\nSUPPORT, 6500.00, 1\nRESISTANCE, 6510.25, 3\n")
        lines = parse_file(f)
        assert len(lines) == 2
        assert lines[0]["line_type"] == "SUPPORT"
        assert lines[0]["price"]     == 6500.0
        assert lines[0]["strength"]  == 1
        assert lines[1]["line_type"] == "RESISTANCE"

        # Bad type
        f.write_text("UNKNOWN, 6500, 1\n")
        try:
            parse_file(f)
            return False  # should have raised
        except ValueError:
            pass

        # Bad strength
        f.write_text("SUPPORT, 6500, 5\n")
        try:
            parse_file(f)
            return False
        except ValueError:
            pass
    return True


def _test_sl_cooldown_logic():
    import tempfile
    from pathlib import Path
    from lib.db import init_db, get_db
    from lib.critical_lines import get_file_path, load_critical_lines
    from position_manager import check_sl_cooldowns, _now_utc

    cfg = __import__("lib.config_loader", fromlist=["get_config"]).get_config()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path  = tmp_path / "test.db"
        cl_dir   = tmp_path / "cl"
        cl_dir.mkdir()
        init_db(db_path)

        today = date.today().strftime("%Y-%m-%d")
        fp = get_file_path("MES", today, cl_dir)
        fp.write_text("SUPPORT, 6490.0, 2\n")
        load_critical_lines("MES", today, db_path, cl_dir)

        # Insert a recent SL close
        with get_db(db_path) as con:
            con.execute("""
                INSERT INTO commands
                    (symbol, line_price, line_type, line_strength,
                     direction, entry_type, entry_price, tp_price, sl_price,
                     bracket_size, status, fill_price, fill_time,
                     exit_price, exit_time, exit_reason)
                VALUES ('MES',6490,'SUPPORT',2,'BUY','LMT',6490,6492,6488,
                        2,'CLOSED',6490,?,6488,?,'SL')
            """, (_now_utc(), _now_utc()))

        n = check_sl_cooldowns(db_path, cfg, today)
        assert n == 1, f"Expected 1 disarm, got {n}"

        with get_db(db_path) as con:
            row = con.execute(
                "SELECT armed FROM critical_lines WHERE price=6490.0"
            ).fetchone()
        assert row["armed"] == 0
    return True


LAYER2_TESTS = [
    ("toggle_rule",          _test_toggle_rule),
    ("bracket_prices",       _test_bracket_prices),
    ("tick_rounding",        _test_tick_rounding),
    ("db_state_machine",     _test_db_state_machine),
    ("claim_lock",           _test_claim_lock),
    ("replenishment_no_double", _test_replenishment_no_double),
    ("critical_lines_parser",   _test_critical_lines_parser),
    ("sl_cooldown_logic",    _test_sl_cooldown_logic),
]


def run_layer2(program_filter: str = None) -> list[Result]:
    results = []
    for name, fn in LAYER2_TESTS:
        if program_filter and program_filter not in name:
            continue
        results.append(_run(fn, 2, name))
    return results


# ── Layer 3: IB integration ───────────────────────────────────────────────────

def _test_ib_paper_order(ibc, cfg):
    """
    Submit a real LMT BUY order far below market to PAPER, verify acceptance,
    then cancel immediately (R-REG-05).
    """
    from ib_insync import LimitOrder

    contract = ibc.get_contract("MES")
    price = ibc.get_price("MES")
    if not price or price <= 0:
        raise ValueError("Could not fetch current price for L3 test")

    test_price = round(price - 500, 0)  # 500 pts below market — guaranteed no fill
    order = LimitOrder("BUY", 1, test_price)
    trade = ibc.paper.placeOrder(contract, order)
    ibc.paper.sleep(1.0)

    # Verify IB accepted (order has an ID and is in open orders)
    open_ids = {o.orderId for o in ibc.paper.openOrders()}
    assert trade.order.orderId in open_ids, \
        f"Order {trade.order.orderId} not found in open orders"

    # Cancel immediately
    ibc.paper.cancelOrder(trade.order)
    ibc.paper.sleep(1.0)
    return True


def run_layer3(program_filter: str = None) -> list[Result]:
    from lib.config_loader import get_config
    from lib.ib_client import IBClient

    cfg = get_config()
    ibc = IBClient(cfg)

    try:
        ibc.connect(live=True, paper=True)
    except ConnectionError as e:
        return [_skip(3, "ib_paper_order", f"IB Gateway not available: {e}")]

    if not ibc.is_live_connected() or not ibc.is_paper_connected():
        ibc.disconnect()
        return [_skip(3, "ib_paper_order", "Could not connect to both ports")]

    results = []
    if not program_filter or "ib" in program_filter:
        results.append(_run(lambda: _test_ib_paper_order(ibc, cfg), 3, "ib_paper_order"))

    ibc.disconnect()
    return results


# ── Output ────────────────────────────────────────────────────────────────────

def write_log(results: list[Result], log_path: Path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n=== Regression run {ts} ===\n")
        for r in results:
            f.write(str(r) + "\n")
        passed = sum(1 for r in results if r.status == "PASS")
        failed = sum(1 for r in results if r.status == "FAIL")
        skipped = sum(1 for r in results if r.status == "SKIP")
        f.write(f"SUMMARY: {passed} passed, {failed} failed, {skipped} skipped\n")


def print_results(results: list[Result]):
    print()
    for r in results:
        print(str(r))
    passed  = sum(1 for r in results if r.status == "PASS")
    failed  = sum(1 for r in results if r.status == "FAIL")
    skipped = sum(1 for r in results if r.status == "SKIP")
    total   = len(results)
    print(f"\nSUMMARY: {total} tests — {passed} passed, {failed} failed, {skipped} skipped")
    if failed:
        print("RESULT: FAIL")
    else:
        print("RESULT: PASS")


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    """Validate the regression runner itself: run L1+L2, assert no failures."""
    try:
        results = run_layer1() + run_layer2()
        failures = [r for r in results if r.status == "FAIL"]
        if failures:
            for r in failures:
                print(f"  FAIL: {r}")
            print(f"[self-test] regression: FAIL -- {len(failures)} test(s) failed")
            return False
        print("[self-test] regression: PASS")
        return True
    except Exception as e:
        print(f"[self-test] regression: FAIL -- {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao regression runner")
    parser.add_argument("--self-test",    action="store_true")
    parser.add_argument("--quick",        action="store_true", help="Layers 1+2 only")
    parser.add_argument("--layer3-only",  action="store_true")
    parser.add_argument("--program",      help="Filter tests by component name")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    from lib.config_loader import get_config
    cfg = get_config()
    log_path = Path(cfg.paths.logs) / "regression.log"

    all_results = []

    if not args.layer3_only:
        print("=== Layer 1: Self-tests ===")
        r1 = run_layer1(args.program)
        all_results.extend(r1)

        print("=== Layer 2: Feature/logic tests ===")
        r2 = run_layer2(args.program)
        all_results.extend(r2)

    if not args.quick:
        print("=== Layer 3: IB integration ===")
        r3 = run_layer3(args.program)
        all_results.extend(r3)

    print_results(all_results)
    write_log(all_results, log_path)
    print(f"\nLog written to {log_path}")

    failed = sum(1 for r in all_results if r.status == "FAIL")
    sys.exit(1 if failed else 0)
