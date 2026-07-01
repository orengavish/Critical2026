"""
tracer.py — Galao Trade Tracer
Standalone GUI for end-to-end test-trade tracing.

Shows live status of TEST commands (line_type='TEST') inserted for
pipeline verification.  Requires the main system (runner.py) to be
running so the broker and position_manager can process the orders.

Modes:
    python tracer.py              -- full GUI, no auto-send
    python tracer.py --self-test  -- headless DB/logic validation (no IB)

Workflow:
    Step 1: python tracer.py --self-test   # must PASS before anything else
    Step 2: run system, open tracer, click [Send MKT] (2 orders)
            wait until at least one shows FILLED / POSITION
    Step 3: click [Send All] (10 orders)
            wait until all are SUBMITTED+, ≥1 POSITION, ≥1 FILLED
"""

import sys
import argparse
import time
import tempfile
from pathlib import Path
from datetime import datetime, timezone

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.config_loader import get_config
from lib.db import get_db, init_db

# ── Constants ─────────────────────────────────────────────────────────────────

_TICK     = 0.25
_BRACKET  = 2.0
_REFRESH  = 2000          # ms

STATUS_FG = {
    "PENDING":            "#d29922",
    "SUBMITTING":         "#79c0ff",
    "SUBMITTED":          "#58a6ff",
    "FILLED":             "#3fb950",
    "EXITING":            "#e3b341",
    "CLOSED":             "#8b949e",
    "CANCELLED":          "#6e7681",
    "ERROR":              "#f85149",
    "RECONCILE_REQUIRED": "#f85149",
}
STATUS_BG = {
    "PENDING":            "#2d2a0f",
    "SUBMITTING":         "#1a2940",
    "SUBMITTED":          "#0d2a4a",
    "FILLED":             "#0f2d1a",
    "EXITING":            "#2d1a0f",
    "CLOSED":             "#1c1c1c",
    "CANCELLED":          "#1c1c1c",
    "ERROR":              "#4a1a1a",
    "RECONCILE_REQUIRED": "#4a1a1a",
}

COLS = [
    # (key,          header,   width, anchor)
    ("id",           "ID",       42, "center"),
    ("direction",    "Dir",      38, "center"),
    ("entry_type",   "Type",     38, "center"),
    ("entry_price",  "Entry",    64, "e"),
    ("status",       "Status",   98, "center"),
    ("fill_price",   "Fill@",    64, "e"),
    ("pnl",          "P&L",      62, "e"),
    ("tp_price",     "TP",       64, "e"),
    ("sl_price",     "SL",       64, "e"),
    ("has_pos",      "Pos",      32, "center"),
    ("updated_at",   "Updated",  68, "center"),
]

# ── Data helpers ──────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rt(p: float) -> float:
    return round(round(p / _TICK) * _TICK, 10)


def _get_price_from_api() -> float | None:
    """Query the visualizer for current price (works if runner.py is up)."""
    try:
        import urllib.request, json
        with urllib.request.urlopen("http://127.0.0.1:5000/api/price", timeout=1) as r:
            d = json.loads(r.read())
        return d.get("price")
    except Exception:
        return None


def get_test_rows(db_path) -> list[dict]:
    """Return TEST commands with position flag, ordered newest-first."""
    with get_db(db_path) as con:
        rows = con.execute(
            """
            SELECT c.*,
                   CASE WHEN p.id IS NOT NULL THEN 1 ELSE 0 END AS has_pos
            FROM   commands c
            LEFT   JOIN positions p
                   ON  p.command_id = c.id AND p.status = 'OPEN'
            WHERE  c.line_type = 'TEST'
            ORDER  BY c.id DESC
            LIMIT  50
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_counters(db_path) -> dict:
    """Status counts + open-position count for TEST commands."""
    with get_db(db_path) as con:
        status_rows = con.execute(
            "SELECT status, COUNT(*) cnt FROM commands "
            "WHERE line_type='TEST' GROUP BY status"
        ).fetchall()
        pos_cnt = con.execute(
            "SELECT COUNT(*) FROM positions p "
            "JOIN commands c ON c.id = p.command_id "
            "WHERE c.line_type='TEST' AND p.status='OPEN'"
        ).fetchone()[0]
    counts = {r["status"]: r["cnt"] for r in status_rows}
    counts["POSITION"] = pos_cnt
    return counts


def calc_pnl(cmd: dict, price: float | None) -> float | None:
    """Realized pnl_points if closed; unrealized vs current price if filled."""
    if cmd.get("pnl_points") is not None:
        return cmd["pnl_points"]
    if cmd["status"] in ("FILLED", "EXITING") and cmd.get("fill_price") and price:
        fp = cmd["fill_price"]
        return round((price - fp) if cmd["direction"] == "BUY" else (fp - price), 2)
    return None


def insert_test_commands(db_path, symbol: str, price: float,
                         mkt_only: bool = False) -> list[int]:
    """Insert PENDING test commands near price. Returns list of new IDs."""
    if mkt_only:
        specs = [
            ("BUY",  "MKT", _rt(price),        _rt(price + _BRACKET), _rt(price - _BRACKET)),
            ("SELL", "MKT", _rt(price),        _rt(price - _BRACKET), _rt(price + _BRACKET)),
        ]
    else:
        specs = [
            # Market — fills immediately
            ("BUY",  "MKT", _rt(price),           _rt(price + _BRACKET),       _rt(price - _BRACKET)),
            ("SELL", "MKT", _rt(price),           _rt(price - _BRACKET),       _rt(price + _BRACKET)),
            # Aggressive LMT (limit past market → fills at market)
            ("BUY",  "LMT", _rt(price + 2.0),    _rt(price + 2.0 + _BRACKET), _rt(price + 2.0 - _BRACKET)),
            ("SELL", "LMT", _rt(price - 2.0),    _rt(price - 2.0 - _BRACKET), _rt(price - 2.0 + _BRACKET)),
            # STP already past market → triggers immediately
            ("BUY",  "STP", _rt(price - _TICK),  _rt(price - _TICK + _BRACKET), _rt(price - _TICK - _BRACKET)),
            ("SELL", "STP", _rt(price + _TICK),  _rt(price + _TICK - _BRACKET), _rt(price + _TICK + _BRACKET)),
            # Near LMT
            ("BUY",  "LMT", _rt(price + 0.5),   _rt(price + 0.5 + _BRACKET),  _rt(price + 0.5 - _BRACKET)),
            ("SELL", "LMT", _rt(price - 0.5),   _rt(price - 0.5 - _BRACKET),  _rt(price - 0.5 + _BRACKET)),
            # Near STP
            ("BUY",  "STP", _rt(price + 0.5),   _rt(price + 0.5 + _BRACKET),  _rt(price + 0.5 - _BRACKET)),
            ("SELL", "STP", _rt(price - 0.5),   _rt(price - 0.5 - _BRACKET),  _rt(price - 0.5 + _BRACKET)),
        ]

    ids = []
    with get_db(db_path) as con:
        for direction, entry_type, entry_price, tp_price, sl_price in specs:
            con.execute(
                "INSERT INTO commands "
                "(symbol, line_price, line_type, line_strength, "
                " direction, entry_type, entry_price, tp_price, sl_price, bracket_size) "
                "VALUES (?, ?, 'TEST', 1, ?, ?, ?, ?, ?, ?)",
                (symbol, _rt(price), direction, entry_type,
                 entry_price, tp_price, sl_price, _BRACKET)
            )
            ids.append(con.execute("SELECT last_insert_rowid()").fetchone()[0])
    return ids


def cancel_pending_test(db_path) -> int:
    """Mark PENDING TEST commands CANCELLED. Returns count."""
    with get_db(db_path) as con:
        cur = con.execute(
            "UPDATE commands SET status='CANCELLED', updated_at=? "
            "WHERE line_type='TEST' AND status='PENDING'",
            (_now_utc(),)
        )
    return cur.rowcount


# ── Self-test (headless) ──────────────────────────────────────────────────────

def self_test() -> bool:
    """
    Validate data layer without IB or GUI.
    Simulates the full PENDING→SUBMITTED→FILLED→POSITION→CLOSED pipeline
    in a temp DB and verifies all tracer queries return correct results.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tracer_test.db"
            init_db(db_path)

            symbol = "MES"
            price  = 6800.0

            # ── 1. Insert test commands ──────────────────────────────────────
            ids = insert_test_commands(db_path, symbol, price, mkt_only=False)
            assert len(ids) == 10, f"Expected 10 commands, got {len(ids)}"

            rows = get_test_rows(db_path)
            assert len(rows) == 10, f"Expected 10 rows, got {len(rows)}"
            assert all(r["status"] == "PENDING"    for r in rows)
            assert all(r["line_type"] == "TEST"    for r in rows)

            # ── 2. Counters: all PENDING ─────────────────────────────────────
            c = get_counters(db_path)
            assert c.get("PENDING") == 10, f"PENDING counter: {c}"
            assert c.get("POSITION", 0) == 0

            # ── 3. Simulate: first command → SUBMITTING → SUBMITTED ──────────
            cmd_id = ids[0]  # first inserted = BUY MKT (lowest id)
            with get_db(db_path) as con:
                con.execute(
                    "UPDATE commands SET status='SUBMITTING', claimed_at=? WHERE id=?",
                    (_now_utc(), cmd_id)
                )
            with get_db(db_path) as con:
                con.execute(
                    "UPDATE commands SET status='SUBMITTED', ib_order_id=1001 WHERE id=?",
                    (cmd_id,)
                )

            c = get_counters(db_path)
            assert c.get("PENDING")   == 9, f"PENDING after submit: {c}"
            assert c.get("SUBMITTED") == 1, f"SUBMITTED after submit: {c}"

            # ── 4. Simulate: SUBMITTED → FILLED ─────────────────────────────
            fill_price = price + 0.25
            fill_time  = _now_utc()
            with get_db(db_path) as con:
                con.execute(
                    "UPDATE commands SET status='FILLED', fill_price=?, fill_time=? WHERE id=?",
                    (fill_price, fill_time, cmd_id)
                )

            c = get_counters(db_path)
            assert c.get("FILLED") == 1, f"FILLED counter: {c}"

            rows = get_test_rows(db_path)
            filled = next(r for r in rows if r["id"] == cmd_id)
            assert filled["status"] == "FILLED"
            assert filled["fill_price"] == fill_price

            # ── 5. Simulate: open position created ───────────────────────────
            with get_db(db_path) as con:
                con.execute(
                    "INSERT INTO positions "
                    "(command_id, symbol, direction, quantity, entry_price, entry_time) "
                    "VALUES (?, ?, ?, 1, ?, ?)",
                    (cmd_id, symbol, "BUY", fill_price, fill_time)
                )

            c = get_counters(db_path)
            assert c["POSITION"] == 1, f"POSITION counter: {c}"

            rows = get_test_rows(db_path)
            filled = next(r for r in rows if r["id"] == cmd_id)
            assert filled["has_pos"] == 1, "has_pos should be 1"

            # ── 6. P&L calculation ───────────────────────────────────────────
            current = price + 1.5
            pnl = calc_pnl(filled, current)
            expected = round(current - fill_price, 2)
            assert pnl == expected, f"P&L: got {pnl}, expected {expected}"

            # ── 7. Simulate: CLOSED with realized P&L ────────────────────────
            realized = round(fill_price - price, 2)  # small loss for realism
            with get_db(db_path) as con:
                con.execute(
                    "UPDATE commands SET status='CLOSED', pnl_points=?, "
                    "exit_price=?, exit_time=?, exit_reason='TP' WHERE id=?",
                    (realized, price + 2.0, _now_utc(), cmd_id)
                )
                # Close position too
                con.execute(
                    "UPDATE positions SET status='CLOSED' WHERE command_id=?",
                    (cmd_id,)
                )

            rows = get_test_rows(db_path)
            closed = next(r for r in rows if r["id"] == cmd_id)
            assert closed["status"] == "CLOSED"
            assert closed["has_pos"] == 0

            pnl_closed = calc_pnl(closed, current)
            assert pnl_closed == realized, f"Realized P&L: {pnl_closed} != {realized}"

            # ── 8. Cancel pending ────────────────────────────────────────────
            n = cancel_pending_test(db_path)
            assert n == 9, f"Expected 9 cancelled, got {n}"

            c = get_counters(db_path)
            assert c.get("PENDING", 0) == 0, f"Should have no PENDING left: {c}"

            # ── 9. Status color coverage ─────────────────────────────────────
            all_statuses = [
                "PENDING","SUBMITTING","SUBMITTED","FILLED",
                "EXITING","CLOSED","CANCELLED","ERROR","RECONCILE_REQUIRED"
            ]
            for s in all_statuses:
                assert s in STATUS_FG, f"Missing STATUS_FG for {s}"
                assert s in STATUS_BG, f"Missing STATUS_BG for {s}"

        print("[self-test] tracer: PASS")
        return True

    except Exception as e:
        print(f"[self-test] tracer: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


# ── GUI ───────────────────────────────────────────────────────────────────────

def run_gui(db_path: Path, symbol: str):
    import tkinter as tk
    from tkinter import ttk, messagebox

    cfg_obj = get_config()

    # ── App window ────────────────────────────────────────────────────────────
    root = tk.Tk()
    root.title("Galao Tracer")
    root.configure(bg="#0d1117")
    root.geometry("920x560")
    root.minsize(800, 400)

    style = ttk.Style()
    style.theme_use("clam")
    style.configure("Treeview",
        background="#161b22", foreground="#c9d1d9",
        fieldbackground="#161b22", rowheight=20,
        font=("Courier New", 11))
    style.configure("Treeview.Heading",
        background="#0d1117", foreground="#8b949e",
        font=("Courier New", 11, "bold"), relief="flat")
    style.map("Treeview",
        background=[("selected", "#1f3a5f")],
        foreground=[("selected", "#ffffff")])

    # ── Top: counters ─────────────────────────────────────────────────────────
    top_frame = tk.Frame(root, bg="#161b22", pady=6)
    top_frame.pack(fill="x", padx=8, pady=(8, 0))

    counter_vars: dict[str, tk.StringVar] = {}
    counter_labels_order = [
        "PENDING", "SUBMITTING", "SUBMITTED", "FILLED", "POSITION", "EXITING",
        "CLOSED", "CANCELLED", "ERROR",
    ]
    for s in counter_labels_order:
        var = tk.StringVar(value=f"{s[:4]}: 0")
        counter_vars[s] = var
        fg = STATUS_FG.get(s, "#8b949e")
        lbl = tk.Label(top_frame, textvariable=var, bg="#161b22", fg=fg,
                       font=("Courier New", 11, "bold"), padx=10)
        lbl.pack(side="left")

    price_var = tk.StringVar(value="Price: --")
    tk.Label(top_frame, textvariable=price_var, bg="#161b22", fg="#3fb950",
             font=("Courier New", 11), padx=10).pack(side="right")

    # ── Button bar ────────────────────────────────────────────────────────────
    btn_frame = tk.Frame(root, bg="#0d1117", pady=4)
    btn_frame.pack(fill="x", padx=8)

    status_var = tk.StringVar(value="Ready")
    tk.Label(btn_frame, textvariable=status_var, bg="#0d1117", fg="#8b949e",
             font=("Courier New", 10), padx=8).pack(side="right")

    def _btn(parent, text, cmd, fg="#3fb950", bg="#1a3a1a"):
        return tk.Button(parent, text=text, command=cmd,
                         bg=bg, fg=fg, activebackground="#30363d",
                         activeforeground="#c9d1d9", relief="flat",
                         font=("Courier New", 11, "bold"),
                         padx=10, pady=3, cursor="hand2")

    def _send(mkt_only: bool):
        price = _get_price_from_api()
        if price is None:
            messagebox.showerror("No Price",
                "Cannot fetch price — is the system (runner.py) running?")
            return
        label = "MKT" if mkt_only else "ALL"
        try:
            ids = insert_test_commands(db_path, symbol, price, mkt_only=mkt_only)
            status_var.set(
                f"Sent {len(ids)} {label} orders near {price:.2f}  "
                f"IDs: {ids[0]}–{ids[-1]}"
            )
            _refresh()
        except Exception as e:
            messagebox.showerror("Send Error", str(e))

    def _cancel():
        n = cancel_pending_test(db_path)
        status_var.set(f"Cancelled {n} PENDING test order(s)")
        _refresh()

    _btn(btn_frame, "Send MKT", lambda: _send(mkt_only=True)).pack(side="left", padx=(0, 4))
    _btn(btn_frame, "Send All", lambda: _send(mkt_only=False),
         fg="#58a6ff", bg="#0d2a4a").pack(side="left", padx=4)
    _btn(btn_frame, "Cancel Pending", _cancel,
         fg="#d29922", bg="#2d2a0f").pack(side="left", padx=4)

    # ── Treeview ──────────────────────────────────────────────────────────────
    tree_frame = tk.Frame(root, bg="#0d1117")
    tree_frame.pack(fill="both", expand=True, padx=8, pady=6)

    col_keys = [c[0] for c in COLS]
    tree = ttk.Treeview(tree_frame, columns=col_keys, show="headings",
                        selectmode="browse")

    for key, header, width, anchor in COLS:
        tree.heading(key, text=header)
        tree.column(key, width=width, anchor=anchor, stretch=False)

    # Configure status-based row tags
    for s, fg in STATUS_FG.items():
        tree.tag_configure(s, foreground=fg, background=STATUS_BG.get(s, "#161b22"))

    vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)

    # ── Status bar ────────────────────────────────────────────────────────────
    bar_frame = tk.Frame(root, bg="#161b22", pady=2)
    bar_frame.pack(fill="x", side="bottom")
    refresh_var = tk.StringVar(value="")
    tk.Label(bar_frame, textvariable=refresh_var, bg="#161b22", fg="#6e7681",
             font=("Courier New", 9)).pack(side="right", padx=8)

    # ── Refresh logic ─────────────────────────────────────────────────────────
    _price_cache: list[float | None] = [None]

    def _refresh():
        # Price
        p = _get_price_from_api()
        _price_cache[0] = p
        price_var.set(f"Price: {p:.2f}" if p else "Price: --")

        # Counters
        try:
            counts = get_counters(db_path)
            for s, var in counter_vars.items():
                n = counts.get(s, 0)
                var.set(f"{s[:4]}: {n}")
        except Exception:
            pass

        # Rows
        try:
            rows = get_test_rows(db_path)
        except Exception:
            return

        # Preserve selection
        sel = tree.selection()
        sel_id = (tree.item(sel[0])["values"][0] if sel else None)

        tree.delete(*tree.get_children())
        new_sel = None

        for r in rows:
            pnl = calc_pnl(r, _price_cache[0])
            pnl_str = (
                f"{'+' if pnl >= 0 else ''}{pnl:.2f}" if pnl is not None else ""
            )
            upd = (r.get("updated_at") or "")[-8:-3]  # HH:MM
            pos_mark = "●" if r["has_pos"] else ""

            vals = (
                r["id"],
                r["direction"],
                r["entry_type"],
                r["entry_price"],
                r["status"],
                r["fill_price"] or "",
                pnl_str,
                r["tp_price"],
                r["sl_price"],
                pos_mark,
                upd,
            )
            tag = r["status"]
            iid = tree.insert("", "end", values=vals, tags=(tag,))
            if r["id"] == sel_id:
                new_sel = iid

        if new_sel:
            tree.selection_set(new_sel)
            tree.see(new_sel)

        refresh_var.set(f"Last refresh: {datetime.now().strftime('%H:%M:%S')}")

    def _schedule_refresh():
        _refresh()
        root.after(_REFRESH, _schedule_refresh)

    _schedule_refresh()
    root.mainloop()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Galao Trade Tracer")
    parser.add_argument("--self-test", action="store_true",
                        help="Headless DB/logic validation — no IB or GUI needed")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    cfg     = get_config()
    db_path = Path(cfg.paths.db)
    symbol  = cfg.symbols[0] if cfg.symbols else "MES"
    init_db(db_path)

    run_gui(db_path, symbol)


if __name__ == "__main__":
    main()
