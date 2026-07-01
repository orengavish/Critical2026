"""
lib/critical_lines.py
Parses critical lines files and loads them into the DB.

File format (R-CL-01, R-CL-02):
  data/critical_lines/levels_daily_{SYMBOL}_{YYYYMMDD}.txt
  Each data line: TYPE, PRICE, STRENGTH
  TYPE: SUPPORT | RESISTANCE
  STRENGTH: 1=strong, 2=medium, 3=low

Usage:
    from lib.critical_lines import load_critical_lines, get_armed_lines
    count = load_critical_lines("MES", "2026-04-07", db_path)
    lines = get_armed_lines(con, "MES", "2026-04-07")

Self-test:
    python -m lib.critical_lines --self-test
"""

import sys
import argparse
from pathlib import Path
from datetime import date

from lib.config_loader import get_config
from lib.logger import get_logger

log = get_logger("critical_lines")

MAX_LINES_PER_SYMBOL = 20


def _resolve_cl_dir() -> Path:
    try:
        cfg = get_config()
        return Path(cfg.paths.critical_lines)
    except Exception:
        return Path("data/critical_lines")


def get_file_path(symbol: str, date_str: str, cl_dir: Path = None) -> Path:
    """Return the expected file path for a symbol+date."""
    d = cl_dir or _resolve_cl_dir()
    date_compact = date_str.replace("-", "")  # YYYYMMDD
    return d / f"levels_daily_{symbol}_{date_compact}.txt"


def parse_file(file_path: Path) -> list[dict]:
    """
    Parse a critical lines file.
    Returns list of dicts: {line_type, price, strength}
    Skips comment and blank lines.
    Raises FileNotFoundError if missing, ValueError on bad format.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"Critical lines file not found: {file_path}")

    lines = []
    with open(file_path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = [p.strip() for p in stripped.split(",")]
            if len(parts) != 3:
                raise ValueError(
                    f"{file_path}:{lineno} — expected 3 fields, got {len(parts)}: {stripped!r}"
                )
            line_type, price_str, strength_str = parts
            line_type = line_type.upper()
            if line_type not in ("SUPPORT", "RESISTANCE"):
                raise ValueError(
                    f"{file_path}:{lineno} — unknown type {line_type!r}, "
                    f"expected SUPPORT or RESISTANCE"
                )
            try:
                price = float(price_str)
            except ValueError:
                raise ValueError(f"{file_path}:{lineno} — invalid price: {price_str!r}")
            try:
                strength = int(strength_str)
            except ValueError:
                raise ValueError(
                    f"{file_path}:{lineno} — invalid strength: {strength_str!r}"
                )
            if strength not in (1, 2, 3):
                raise ValueError(
                    f"{file_path}:{lineno} — strength must be 1/2/3, got {strength}"
                )
            lines.append({"line_type": line_type, "price": price, "strength": strength})

    if len(lines) > MAX_LINES_PER_SYMBOL:
        raise ValueError(
            f"{file_path}: too many lines ({len(lines)}), max is {MAX_LINES_PER_SYMBOL}"
        )

    log.info(f"Parsed {len(lines)} critical lines from {file_path.name}")
    return lines


def load_critical_lines(symbol: str, date_str: str,
                        db_path=None, cl_dir: Path = None) -> int:
    """
    Parse the critical lines file for symbol+date and insert into DB.
    Clears existing rows for this symbol+date before inserting (idempotent).
    Returns number of lines loaded.

    Raises FileNotFoundError if the file is missing (R-CL-05: blocks symbol).
    """
    from lib.db import get_db
    file_path = get_file_path(symbol, date_str, cl_dir)
    lines = parse_file(file_path)

    with get_db(db_path) as con:
        con.execute(
            "DELETE FROM critical_lines WHERE symbol=? AND date=?",
            (symbol, date_str)
        )
        for line in lines:
            con.execute(
                "INSERT INTO critical_lines (symbol, date, line_type, price, strength, armed)"
                " VALUES (?, ?, ?, ?, ?, 1)",
                (symbol, date_str, line["line_type"], line["price"], line["strength"])
            )

    log.info(f"Loaded {len(lines)} critical lines for {symbol} {date_str} into DB")
    return len(lines)


def get_armed_lines(con, symbol: str, date_str: str) -> list:
    """Return armed critical lines for symbol+date from DB."""
    return con.execute(
        "SELECT * FROM critical_lines WHERE symbol=? AND date=? AND armed=1"
        " ORDER BY price",
        (symbol, date_str)
    ).fetchall()


def disarm_line(con, line_id: int):
    """Disarm a critical line (SL cool-down, R-POS-02)."""
    con.execute(
        "UPDATE critical_lines SET armed=0 WHERE id=?",
        (line_id,)
    )
    log.info(f"Critical line {line_id} disarmed (SL cool-down)")


def rearm_line(con, line_id: int):
    """Re-arm a critical line after cool-down expires."""
    con.execute(
        "UPDATE critical_lines SET armed=1 WHERE id=?",
        (line_id,)
    )
    log.info(f"Critical line {line_id} re-armed")


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile, os
    try:
        from lib.db import init_db, get_db
        from lib.logger import reset_loggers

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cl_dir   = tmp_path / "cl"
            cl_dir.mkdir()
            db_path  = tmp_path / "test.db"
            init_db(db_path)

            # 1. Write a valid file
            today = "2026-04-07"
            file_path = get_file_path("MES", today, cl_dir)
            file_path.write_text(
                "# test\n"
                "SUPPORT,    6500.00, 1\n"
                "RESISTANCE, 6510.25, 2\n"
                "SUPPORT,    6490.75, 3\n"
            )
            count = load_critical_lines("MES", today, db_path, cl_dir)
            assert count == 3, f"Expected 3 lines, got {count}"

            with get_db(db_path) as con:
                rows = get_armed_lines(con, "MES", today)
            assert len(rows) == 3
            prices = {r["price"] for r in rows}
            assert 6500.0  in prices
            assert 6510.25 in prices
            assert 6490.75 in prices

            # 2. Idempotent reload
            count2 = load_critical_lines("MES", today, db_path, cl_dir)
            assert count2 == 3
            with get_db(db_path) as con:
                total = con.execute("SELECT COUNT(*) FROM critical_lines").fetchone()[0]
            assert total == 3, f"Reload duplicated rows: {total}"

            # 3. Disarm / rearm
            with get_db(db_path) as con:
                rows = get_armed_lines(con, "MES", today)
                line_id = rows[0]["id"]
                disarm_line(con, line_id)
            with get_db(db_path) as con:
                armed = get_armed_lines(con, "MES", today)
            assert len(armed) == 2, f"Expected 2 armed after disarm, got {len(armed)}"
            with get_db(db_path) as con:
                rearm_line(con, line_id)
            with get_db(db_path) as con:
                armed2 = get_armed_lines(con, "MES", today)
            assert len(armed2) == 3

            # 4. Missing file → FileNotFoundError
            raised = False
            try:
                load_critical_lines("MES", "2099-01-01", db_path, cl_dir)
            except FileNotFoundError:
                raised = True
            assert raised, "Missing file should raise FileNotFoundError"

            # 5. Bad format → ValueError
            bad_file = get_file_path("MES", "2026-04-08", cl_dir)
            bad_file.write_text("NOTATYPE, 6500, 1\n")
            raised = False
            try:
                parse_file(bad_file)
            except ValueError:
                raised = True
            assert raised, "Bad type should raise ValueError"

            # 6. Too many lines → ValueError
            many_file = get_file_path("MES", "2026-04-09", cl_dir)
            many_file.write_text(
                "\n".join(f"SUPPORT, {6500+i*0.25}, 1" for i in range(21)) + "\n"
            )
            raised = False
            try:
                parse_file(many_file)
            except ValueError:
                raised = True
            assert raised, "Too many lines should raise ValueError"

            reset_loggers()

        print("[self-test] critical_lines: PASS")
        return True

    except Exception as e:
        print(f"[self-test] critical_lines: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(0 if self_test() else 1)
    # Show today's lines
    from datetime import date as _date
    today = _date.today().strftime("%Y-%m-%d")
    cfg = get_config()
    for sym in cfg.symbols:
        fp = get_file_path(sym, today)
        if fp.exists():
            lines = parse_file(fp)
            print(f"{sym} {today}: {len(lines)} lines")
            for ln in lines:
                print(f"  {ln['line_type']:<12} {ln['price']:>8.2f}  strength={ln['strength']}")
        else:
            print(f"{sym} {today}: file not found ({fp})")
