"""
release_notes.py
Read and write release notes stored in the DB.

Usage:
    python release_notes.py                  # show all notes
    python release_notes.py --program db     # filter by program name
    python release_notes.py --self-test

Self-test:
    python release_notes.py --self-test
"""

import sys
import argparse
from pathlib import Path

_ROOT = Path(__file__).parent.parent
import sys; sys.path.insert(0, str(_ROOT)) if str(_ROOT) not in sys.path else None


def add_note(program: str, version: str, summary: str, details: str = None, db_path=None):
    """Insert a release note into the DB."""
    from lib.db import get_db
    with get_db(db_path) as con:
        con.execute(
            "INSERT INTO release_notes (program, version, summary, details)"
            " VALUES (?, ?, ?, ?)",
            (program, version, summary, details)
        )


def show_notes(program: str = None, db_path=None):
    """Print release notes to stdout, optionally filtered by program name."""
    from lib.db import get_db
    with get_db(db_path) as con:
        if program:
            rows = con.execute(
                "SELECT * FROM release_notes WHERE program=? ORDER BY created_at DESC",
                (program,)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM release_notes ORDER BY created_at DESC"
            ).fetchall()

    if not rows:
        print("No release notes found.")
        return

    for row in rows:
        print("-" * 60)
        print(f"  Program : {row['program']}  v{row['version']}")
        print(f"  Date    : {row['created_at']}")
        print(f"  Summary : {row['summary']}")
        if row['details']:
            print("  Details :")
            for line in row['details'].splitlines():
                print(f"    {line}")
    print("-" * 60)
    print(f"  Total: {len(rows)} note(s)")


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        from lib.db import init_db

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            init_db(db_path)

            # Write two notes
            add_note("db", "0.1.0", "Initial DB schema", "Tables: commands, positions, ...", db_path)
            add_note("logger", "0.1.0", "Logging setup", None, db_path)

            # Read all
            from lib.db import get_db
            with get_db(db_path) as con:
                rows = con.execute("SELECT * FROM release_notes ORDER BY id").fetchall()

            assert len(rows) == 2, f"Expected 2 notes, got {len(rows)}"
            assert rows[0]["program"] == "db"
            assert rows[1]["program"] == "logger"
            assert rows[0]["details"] is not None
            assert rows[1]["details"] is None

            # Filter by program
            with get_db(db_path) as con:
                rows_db = con.execute(
                    "SELECT * FROM release_notes WHERE program='db'"
                ).fetchall()
            assert len(rows_db) == 1

        print("[self-test] release_notes: PASS")
        return True

    except Exception as e:
        print(f"[self-test] release_notes: FAIL — {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao release notes reader")
    parser.add_argument("--program", help="Filter by program name")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    show_notes(program=args.program)
