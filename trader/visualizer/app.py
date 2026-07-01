"""
visualizer/app.py
Flask web app for Galao monitoring.

Routes:
  GET /            -> dashboard
  GET /active      -> active orders (PENDING/SUBMITTING/SUBMITTED/FILLED)
  GET /positions   -> open positions with unrealized P&L
  GET /orders      -> all commands
  GET /ib-trace    -> IB events log
  GET /logs        -> log file viewer
  GET /preflight   -> last preflight results
  GET /release-notes -> release notes

  API (JSON):
  GET /api/price           -> {price, age_s, stale}
  GET /api/session-state   -> {session}
  GET /api/stats           -> status counts + unrealized_pnl
  GET /api/nearby          -> {price, lines: [{line, commands}]}
  GET /api/commands        -> commands (?status_in=A,B&status=X&symbol=Y&limit=N)
  GET /api/positions       -> open positions
  GET /api/ib-events       -> IB events (?limit=N)
  GET /api/preflight       -> last preflight results from system_state
  GET /api/logs            -> log tail (?component=broker&level=ERROR&limit=N)
  GET /api/release-notes   -> release notes (?program=X)
  GET /api/state           -> system_state rows

Usage:
    python visualizer/app.py                # run server
    python visualizer/app.py --self-test    # headless test

Self-test:
    python visualizer/app.py --self-test
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

_HERE = Path(__file__).parent
_TRADER = _HERE.parent          # trader/visualizer -> trader
_ROOT = _TRADER.parent          # trader -> galgo2026
for _p in (str(_ROOT), str(_TRADER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import re
from flask import Flask, jsonify, request, render_template
from lib.config_loader import get_config
from lib.logger import get_logger
from lib.db import get_db, init_db

log = get_logger("visualizer")

app = Flask(__name__, template_folder="templates", static_folder="static")
_cfg = None
_db_path = None


def _get_cfg():
    global _cfg
    if _cfg is None:
        _cfg = get_config()
    return _cfg


def _get_db_path():
    global _db_path
    if _db_path is None:
        _db_path = Path(_get_cfg().paths.db)
    return _db_path


def _rows_to_list(rows) -> list:
    return [dict(r) for r in rows]


# ── Price API ─────────────────────────────────────────────────────────────────

@app.route("/api/price")
def api_price():
    try:
        from visualizer.price_feed import get_latest
        price, ts = get_latest()
    except Exception:
        price, ts = None, None

    if price is None:
        return jsonify({"price": None, "age_s": None, "stale": True})

    now = datetime.now(timezone.utc)
    age_s = int((now - ts).total_seconds()) if ts else None
    stale = age_s is not None and age_s > 30
    return jsonify({"price": price, "age_s": age_s, "stale": stale})


@app.route("/api/session-state")
def api_session_state():
    try:
        with get_db(_get_db_path()) as con:
            row = con.execute(
                "SELECT value FROM system_state WHERE key='SESSION'"
            ).fetchone()
        return jsonify({"session": row["value"] if row else None})
    except Exception:
        return jsonify({"session": None})


# ── Stats API ─────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with get_db(_get_db_path()) as con:
        status_rows = con.execute(
            "SELECT status, COUNT(*) as cnt FROM commands GROUP BY status"
        ).fetchall()
        closed_today = con.execute(
            "SELECT COUNT(*) FROM commands WHERE status='CLOSED'"
            " AND date(updated_at)=?", (today,)
        ).fetchone()[0]
        errors = con.execute(
            "SELECT COUNT(*) FROM commands WHERE status IN ('ERROR','RECONCILE_REQUIRED')"
        ).fetchone()[0]

        # Unrealized P&L — read from FILLED commands (positions table not populated)
        filled_cmds = _rows_to_list(con.execute(
            "SELECT direction, fill_price, quantity FROM commands WHERE status='FILLED'"
        ).fetchall())

        # Realized P&L today from completed_trades
        pnl_today_rows = _rows_to_list(con.execute(
            "SELECT pnl_points FROM completed_trades WHERE date(exit_time)=?", (today,)
        ).fetchall())

        # Replenish stats
        rp_rows = con.execute(
            "SELECT status, COUNT(*) as cnt FROM commands"
            " WHERE parent_command_id IS NOT NULL GROUP BY status"
        ).fetchall()
        rp_total = con.execute(
            "SELECT COUNT(*) FROM commands WHERE parent_command_id IS NOT NULL"
        ).fetchone()[0]
        rp_closed_pnl = con.execute(
            "SELECT SUM(ct.pnl_points) FROM completed_trades ct"
            " JOIN commands c ON c.id = ct.command_id"
            " WHERE c.parent_command_id IS NOT NULL"
        ).fetchone()[0]

    counts = {r["status"]: r["cnt"] for r in status_rows}
    counts["CLOSED"] = closed_today
    counts["ERROR"]  = errors

    # ── Unrealized P&L ────────────────────────────────────────────────────
    try:
        from visualizer.price_feed import get_latest
        price, _ = get_latest()
    except Exception:
        price = None

    unrealized = None
    if price and filled_cmds:
        unrealized = sum(
            (price - c["fill_price"]) * c["quantity"] if c["direction"] == "BUY"
            else (c["fill_price"] - price) * c["quantity"]
            for c in filled_cmds if c["fill_price"] is not None
        )
    counts["unrealized_pnl"] = round(unrealized, 2) if unrealized is not None else None

    # ── Realized P&L today breakdown ──────────────────────────────────────
    wins   = [r["pnl_points"] for r in pnl_today_rows if r["pnl_points"] > 0]
    losses = [r["pnl_points"] for r in pnl_today_rows if r["pnl_points"] <= 0]
    counts["pnl_today"] = {
        "wins":       len(wins),
        "gains":      round(sum(wins), 2)   if wins   else 0.0,
        "losses":     len(losses),
        "loss_total": round(sum(losses), 2) if losses else 0.0,
        "net":        round(sum(r["pnl_points"] for r in pnl_today_rows), 2) if pnl_today_rows else 0.0,
    }

    # ── Replenish stats ───────────────────────────────────────────────────
    rp_by_status = {r["status"]: r["cnt"] for r in rp_rows}
    counts["replenish"] = {
        "total":     rp_total,
        "pending":   rp_by_status.get("PENDING", 0),
        "submitted": rp_by_status.get("SUBMITTED", 0) + rp_by_status.get("SUBMITTING", 0),
        "filled":    rp_by_status.get("FILLED", 0),
        "closed":    rp_by_status.get("CLOSED", 0),
        "error":     rp_by_status.get("ERROR", 0),
        "pnl":       round(rp_closed_pnl, 2) if rp_closed_pnl is not None else None,
    }

    return jsonify(counts)


# ── Nearby lines API ──────────────────────────────────────────────────────────

@app.route("/api/nearby")
def api_nearby():
    """Return the 8 critical lines nearest to current price, with attached commands."""
    try:
        from visualizer.price_feed import get_latest
        price, _ = get_latest()
    except Exception:
        price = None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db(_get_db_path()) as con:
        lines = _rows_to_list(con.execute(
            "SELECT * FROM critical_lines WHERE date=? AND armed=1 ORDER BY price",
            (today,)
        ).fetchall())

        active_cmds = _rows_to_list(con.execute(
            "SELECT * FROM commands WHERE status IN"
            " ('PENDING','SUBMITTING','SUBMITTED','FILLED')"
        ).fetchall())

    if not price or not lines:
        return jsonify({"price": price, "lines": []})

    # Sort by distance from price, take nearest 8
    lines.sort(key=lambda l: abs(l["price"] - price))
    nearest = lines[:8]

    # Attach commands to each line
    result = []
    for ln in nearest:
        cmds = [c for c in active_cmds if abs(c["line_price"] - ln["price"]) < 0.01]
        result.append({"line": ln, "commands": cmds})

    # Sort by price descending for display (resistance above, support below)
    result.sort(key=lambda x: x["line"]["price"], reverse=True)
    return jsonify({"price": price, "lines": result})


# ── Commands API ──────────────────────────────────────────────────────────────

@app.route("/api/commands")
def api_commands():
    status_in = request.args.get("status_in")   # "PENDING,SUBMITTED,FILLED"
    status    = request.args.get("status")       # single status (legacy)
    symbol    = request.args.get("symbol")
    limit     = int(request.args.get("limit", 200))

    query  = "SELECT * FROM commands WHERE 1=1"
    params = []
    if status_in:
        placeholders = ",".join("?" for _ in status_in.split(","))
        query += f" AND status IN ({placeholders})"
        params.extend(status_in.split(","))
    elif status:
        query += " AND status=?"
        params.append(status)
    if symbol:
        query += " AND symbol=?"
        params.append(symbol)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with get_db(_get_db_path()) as con:
        rows = con.execute(query, params).fetchall()
    return jsonify(_rows_to_list(rows))


# ── Positions API ─────────────────────────────────────────────────────────────

@app.route("/api/positions")
def api_positions():
    with get_db(_get_db_path()) as con:
        rows = con.execute(
            """SELECT p.*,
                      c.tp_price, c.sl_price
               FROM positions p
               LEFT JOIN commands c ON c.id = p.command_id
               WHERE p.status='OPEN'
               ORDER BY p.id DESC"""
        ).fetchall()
    return jsonify(_rows_to_list(rows))


# ── IB Events API ─────────────────────────────────────────────────────────────

@app.route("/api/ib-events")
def api_ib_events():
    limit = int(request.args.get("limit", 500))
    with get_db(_get_db_path()) as con:
        rows = con.execute(
            "SELECT * FROM ib_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return jsonify(_rows_to_list(rows))


# ── Preflight API ─────────────────────────────────────────────────────────────

@app.route("/api/preflight")
def api_preflight():
    with get_db(_get_db_path()) as con:
        rows = con.execute(
            "SELECT * FROM system_state WHERE key LIKE 'preflight_%' ORDER BY key"
        ).fetchall()
    results = {}
    for row in rows:
        parts = row["value"].split("|", 2)
        results[row["key"]] = {
            "status": parts[0] if len(parts) > 0 else "",
            "time":   parts[1] if len(parts) > 1 else "",
            "detail": parts[2] if len(parts) > 2 else "",
        }
    return jsonify(results)


# ── Logs API ──────────────────────────────────────────────────────────────────

@app.route("/api/logs")
def api_logs():
    component = request.args.get("component", "broker")
    level     = request.args.get("level", "").upper()
    limit     = int(request.args.get("limit", 500))

    cfg = _get_cfg()
    log_path = Path(cfg.paths.logs) / f"{component}.log"
    if not log_path.exists():
        return jsonify({"lines": [], "error": f"Not found: {log_path.name}"})

    with open(log_path, encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()

    if level:
        all_lines = [l for l in all_lines if f"| {level}" in l]

    lines = all_lines[-limit:]
    return jsonify({"component": component, "level": level,
                    "count": len(lines), "lines": lines})


# ── Release Notes API ─────────────────────────────────────────────────────────

@app.route("/api/release-notes")
def api_release_notes():
    program = request.args.get("program")
    query   = "SELECT * FROM release_notes WHERE 1=1"
    params  = []
    if program:
        query += " AND program=?"
        params.append(program)
    query += " ORDER BY id DESC"
    with get_db(_get_db_path()) as con:
        rows = con.execute(query, params).fetchall()
    return jsonify(_rows_to_list(rows))


# ── Generate API ─────────────────────────────────────────────────────────────

@app.route("/api/generate", methods=["POST"])
def api_generate():
    """
    POST /api/generate
    Body JSON:
      mode         str    — "random" (default) or "critical_line"
      bracket      float  — bracket size in points (default 8.0)
      types        list   — entry types ["MKT","LMT","STP"]
      count        int    — trades per line or total (default 10, max 200)
      max_offset   int    — max entry offset from anchor price in ticks (default 2)

      -- critical_line mode only --
      line_ids     list   — IDs from critical_lines table (DB-sourced, fully traceable)
      line_text    str    — free-form text to parse (e.g. "support: 7150!, 7140")

    Returns {count, price, commands: [...]}
    """
    import random as _rnd

    body       = request.get_json(force=True) or {}
    mode       = body.get("mode", "random")
    bracket    = float(body.get("bracket", 8.0))
    types      = [t.upper() for t in body.get("types", ["MKT", "LMT", "STP"])]
    count      = min(int(body.get("count", 10)), 200)
    max_offset = int(body.get("max_offset", 2))

    if not types:
        return jsonify({"error": "At least one entry type required"}), 400

    try:
        from visualizer.price_feed import get_latest
        price, _ = get_latest()
    except Exception:
        price = None

    if price is None:
        return jsonify({"error": "No price available — price feed not running"}), 400

    cfg  = _get_cfg()
    tick = cfg.orders.tick_size
    sym  = (cfg.symbols or ["MES"])[0]

    def rt(p):
        return round(round(p / tick) * tick, 10)

    def _make_cmd(anchor_price, direction, entry_type, source, critical_line_id=None,
                  line_type=None, strength=1):
        offset = _rnd.randint(1, max(1, max_offset)) * tick
        if entry_type == "MKT":
            ep = rt(anchor_price)
        elif entry_type == "LMT":
            ep = rt(anchor_price - offset) if direction == "BUY" else rt(anchor_price + offset)
        else:  # STP
            ep = rt(anchor_price + offset) if direction == "BUY" else rt(anchor_price - offset)
        tp = rt(ep + bracket) if direction == "BUY" else rt(ep - bracket)
        sl = rt(ep - bracket) if direction == "BUY" else rt(ep + bracket)
        return {
            "symbol":           sym,
            "line_price":       anchor_price,
            "line_type":        line_type or ("SUPPORT" if direction == "BUY" else "RESISTANCE"),
            "line_strength":    strength,
            "direction":        direction,
            "entry_type":       entry_type,
            "entry_price":      ep,
            "tp_price":         tp,
            "sl_price":         sl,
            "bracket_size":     bracket,
            "source":           source,
            "critical_line_id": critical_line_id,
            "quantity":         cfg.orders.quantity,
        }

    def _insert(trade):
        with get_db(_get_db_path()) as con:
            con.execute("""
                INSERT INTO commands
                    (symbol, line_price, line_type, line_strength,
                     direction, entry_type, entry_price, tp_price, sl_price,
                     bracket_size, source, critical_line_id, quantity, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """, (
                trade["symbol"], trade["line_price"], trade["line_type"], trade["line_strength"],
                trade["direction"], trade["entry_type"],
                trade["entry_price"], trade["tp_price"], trade["sl_price"],
                trade["bracket_size"], trade["source"], trade["critical_line_id"],
                trade["quantity"],
            ))
            return con.execute("SELECT last_insert_rowid()").fetchone()[0]

    inserted = []

    if mode == "critical_line":
        # ── Resolve lines from DB IDs and/or parsed text ──────────────────────
        lines_to_use = []

        line_ids = body.get("line_ids", [])
        if line_ids:
            with get_db(_get_db_path()) as con:
                for lid in line_ids:
                    row = con.execute(
                        "SELECT * FROM critical_lines WHERE id=?", (lid,)
                    ).fetchone()
                    if row:
                        lines_to_use.append({
                            "id":        row["id"],
                            "price":     row["price"],
                            "line_type": row["line_type"],
                            "strength":  row["strength"],
                            "from_db":   True,
                        })

        line_text = body.get("line_text", "").strip()
        if line_text:
            parsed = _parse_lines_text(line_text)
            today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # Save parsed lines to DB so they're traceable, then use their IDs
            with get_db(_get_db_path()) as con:
                for pl in parsed:
                    con.execute(
                        "INSERT INTO critical_lines (symbol, date, line_type, price, strength, armed)"
                        " VALUES (?, ?, ?, ?, ?, 1)",
                        (sym, today, pl["line_type"], pl["price"], pl["strength"])
                    )
                    new_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
                    lines_to_use.append({
                        "id":        new_id,
                        "price":     pl["price"],
                        "line_type": pl["line_type"],
                        "strength":  pl["strength"],
                        "from_db":   False,
                    })

        if not lines_to_use:
            return jsonify({"error": "No critical lines found — select DB lines or paste line text"}), 400

        # Generate `count` trades per line; direction fixed by line_type
        for ln in lines_to_use:
            direction = "BUY" if ln["line_type"] == "SUPPORT" else "SELL"
            for _ in range(count):
                entry_type = _rnd.choice(types)
                trade = _make_cmd(
                    anchor_price     = ln["price"],
                    direction        = direction,
                    entry_type       = entry_type,
                    source           = "critical_line",
                    critical_line_id = ln["id"],
                    line_type        = ln["line_type"],
                    strength         = ln["strength"],
                )
                cmd_id = _insert(trade)
                inserted.append({
                    "id": cmd_id, "direction": direction, "entry_type": entry_type,
                    "entry_price": trade["entry_price"],
                    "critical_line_id": ln["id"], "line_price": ln["price"],
                })

        log.info(f"[generate] critical_line: {len(inserted)} cmds across {len(lines_to_use)} lines "
                 f"bracket={bracket}")

    else:
        # ── Random mode (existing behaviour) ─────────────────────────────────
        for _ in range(count):
            entry_type = _rnd.choice(types)
            direction  = _rnd.choice(["BUY", "SELL"])
            trade = _make_cmd(
                anchor_price = price,
                direction    = direction,
                entry_type   = entry_type,
                source       = f"random_{entry_type.lower()}",
            )
            cmd_id = _insert(trade)
            inserted.append({
                "id": cmd_id, "direction": direction, "entry_type": entry_type,
                "entry_price": trade["entry_price"],
            })

        log.info(f"[generate] random: {len(inserted)} cmds near price={price} bracket={bracket}")

    return jsonify({"count": len(inserted), "price": price, "commands": inserted})


# ── Test Trades API ───────────────────────────────────────────────────────────

@app.route("/api/test-trades", methods=["POST"])
def api_test_trades():
    """Insert ~10 test PENDING commands near current price for end-to-end testing."""
    try:
        from visualizer.price_feed import get_latest
        price, _ = get_latest()
    except Exception:
        price = None

    if price is None:
        return jsonify({"error": "No price available — price feed not running"}), 400

    tick    = 0.25
    bracket = 2.0
    symbol  = _get_cfg().symbols[0] if _get_cfg().symbols else "MES"

    def rt(p):
        return round(round(p / tick) * tick, 10)

    # 10 test orders: MKT long+short, aggressive LMT, STP already-triggered, near LMT/STP
    specs = [
        # Market orders (fill immediately)
        ("BUY",  "MKT", rt(price),        rt(price + bracket),       rt(price - bracket)),
        ("SELL", "MKT", rt(price),        rt(price - bracket),       rt(price + bracket)),
        # Aggressive LMT (limit far past market → fills at market price)
        ("BUY",  "LMT", rt(price + 2.0),  rt(price + 2.0 + bracket), rt(price + 2.0 - bracket)),
        ("SELL", "LMT", rt(price - 2.0),  rt(price - 2.0 - bracket), rt(price - 2.0 + bracket)),
        # STP already past market (triggers immediately → MKT fill)
        ("BUY",  "STP", rt(price - tick), rt(price - tick + bracket), rt(price - tick - bracket)),
        ("SELL", "STP", rt(price + tick), rt(price + tick - bracket), rt(price + tick + bracket)),
        # Near LMT (just inside spread — fills quickly)
        ("BUY",  "LMT", rt(price + 0.5),  rt(price + 0.5 + bracket), rt(price + 0.5 - bracket)),
        ("SELL", "LMT", rt(price - 0.5),  rt(price - 0.5 - bracket), rt(price - 0.5 + bracket)),
        # Near STP (one tick move triggers)
        ("BUY",  "STP", rt(price + 0.5),  rt(price + 0.5 + bracket), rt(price + 0.5 - bracket)),
        ("SELL", "STP", rt(price - 0.5),  rt(price - 0.5 - bracket), rt(price - 0.5 + bracket)),
    ]

    inserted = []
    with get_db(_get_db_path()) as con:
        for direction, entry_type, entry_price, tp_price, sl_price in specs:
            line_type = "SUPPORT" if direction == "BUY" else "RESISTANCE"
            con.execute(
                "INSERT INTO commands"
                " (symbol, line_price, line_type, line_strength,"
                "  direction, entry_type, entry_price, tp_price, sl_price, bracket_size, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'test')",
                (symbol, rt(price), line_type, 1,
                 direction, entry_type, entry_price, tp_price, sl_price, bracket)
            )
            cmd_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            inserted.append({
                "id": cmd_id, "symbol": symbol,
                "direction": direction, "entry_type": entry_type,
                "entry_price": entry_price, "tp_price": tp_price, "sl_price": sl_price,
            })

    log.info(f"[test-trades] Inserted {len(inserted)} test commands near price={price}")
    return jsonify({"count": len(inserted), "price": price, "commands": inserted})


# ── IB Gateway Log API ────────────────────────────────────────────────────────

@app.route("/api/ib-gateway-log")
def api_ib_gateway_log():
    """
    GET /api/ib-gateway-log?limit=500
    Reads the most recent IB Gateway system log file from ib.gateway_log_dir.
    Returns {lines, file, error}.
    """
    limit = int(request.args.get("limit", 500))
    cfg   = _get_cfg()
    log_dir = getattr(cfg.ib, "gateway_log_dir", "") if hasattr(cfg, "ib") else ""

    if not log_dir:
        return jsonify({"lines": [], "file": None,
                        "error": "ib.gateway_log_dir not set in config.yaml"})

    log_dir_path = Path(log_dir)
    if not log_dir_path.exists():
        return jsonify({"lines": [], "file": None,
                        "error": f"Directory not found: {log_dir}"})

    # Find the most recent *.log or any log file in the dir (or subdirs by date)
    candidates = sorted(log_dir_path.glob("**/*.log"), key=lambda p: p.stat().st_mtime,
                        reverse=True)
    if not candidates:
        # Also try .txt files (IB Gateway sometimes uses different extensions)
        candidates = sorted(log_dir_path.glob("**/*.txt"), key=lambda p: p.stat().st_mtime,
                            reverse=True)
    if not candidates:
        return jsonify({"lines": [], "file": None,
                        "error": f"No log files found in {log_dir}"})

    latest = candidates[0]
    try:
        with open(latest, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        lines = all_lines[-limit:]
        return jsonify({"lines": lines, "file": str(latest), "error": None})
    except Exception as e:
        return jsonify({"lines": [], "file": str(latest), "error": str(e)})


# ── Replenish toggle API ──────────────────────────────────────────────────────

@app.route("/api/replenish", methods=["GET"])
def api_replenish_get():
    with get_db(_get_db_path()) as con:
        from lib.db import get_system_state
        val = get_system_state(con, "REPLENISH_ENABLED")
    return jsonify({"enabled": val == "1"})


@app.route("/api/replenish", methods=["POST"])
def api_replenish_set():
    body    = request.get_json(force=True) or {}
    enabled = bool(body.get("enabled", False))
    with get_db(_get_db_path()) as con:
        from lib.db import set_system_state
        set_system_state(con, "REPLENISH_ENABLED", "1" if enabled else "0")
    log.info(f"[replenish] {'ENABLED' if enabled else 'DISABLED'} via API")
    return jsonify({"enabled": enabled})


# ── System State API ──────────────────────────────────────────────────────────

@app.route("/api/state")
def api_state():
    with get_db(_get_db_path()) as con:
        rows = con.execute("SELECT * FROM system_state ORDER BY key").fetchall()
    return jsonify(_rows_to_list(rows))


# ── Lines input API ───────────────────────────────────────────────────────────

_STRENGTH_LABELS = {1: "strong (!)", 2: "medium (?)", 3: "weak (no suffix)"}

def _parse_lines_text(text: str) -> list[dict]:
    """
    Parse Hebrew/free-form critical lines input.

    Recognised section headers (case-insensitive, Hebrew or English):
      קווי תמיכה / support
      קווי התנגדות / resistance

    Per-price suffixes:
      !  → strength 1 (strong)
      ?  → strength 2 (medium)
      (none) → strength 3 (weak)

    Ranges  A - B  inside a comma-separated list add both endpoints separately.
    """
    results = []

    support_text    = ""
    resistance_text = ""

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if "תמיכה" in line or "support" in low:
            idx = line.find(":")
            if idx >= 0:
                support_text = line[idx + 1:].strip()
        elif "התנגדות" in line or "resistance" in low:
            idx = line.find(":")
            if idx >= 0:
                resistance_text = line[idx + 1:].strip()

    def _parse_section(raw: str, line_type: str):
        items = []
        raw = re.sub(r'\s*\([^)]*\)\s*', ' ', raw)  # strip (parenthetical comments)
        for token in raw.split(","):
            for sub in re.split(r'\s+-\s+', token):  # split on " - " zone separator
                sub = sub.strip()
                if not sub:
                    continue
                m = re.match(r'(\d+(?:\.\d+)?)(.*)', sub)
                if not m:
                    continue
                price_str, suffix = m.group(1), m.group(2)
                # * and ! both mean strong; ? means medium; bare = weak
                if '!' in suffix or '*' in suffix:
                    strength = 3
                elif '?' in suffix:
                    strength = 2
                else:
                    strength = 1
                try:
                    items.append({"line_type": line_type,
                                  "price": float(price_str), "strength": strength})
                except ValueError:
                    pass
        return items

    if support_text:
        results.extend(_parse_section(support_text, "SUPPORT"))
    if resistance_text:
        results.extend(_parse_section(resistance_text, "RESISTANCE"))

    return results


@app.route("/api/lines", methods=["GET", "POST"])
def api_lines():
    """
    GET  /api/lines?date=YYYY-MM-DD&symbol=MES
         Returns current critical_lines rows for that date+symbol.

    POST /api/lines
         Body JSON: {text, date, symbol}
         Parses the text and upserts into critical_lines.
         Returns {count, lines} on success or {error} on failure.
    """
    if request.method == "GET":
        date_str = request.args.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        symbol   = request.args.get("symbol", (_get_cfg().symbols or ["MES"])[0])
        with get_db(_get_db_path()) as con:
            rows = _rows_to_list(con.execute(
                "SELECT * FROM critical_lines WHERE symbol=? AND date=? ORDER BY price",
                (symbol, date_str)
            ).fetchall())
        return jsonify(rows)

    # POST
    body   = request.get_json(force=True) or {}
    text   = body.get("text", "").strip()
    date_str = body.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    symbol   = body.get("symbol", (_get_cfg().symbols or ["MES"])[0])

    if not text:
        return jsonify({"error": "No text provided"}), 400

    lines = _parse_lines_text(text)
    if not lines:
        return jsonify({"error": "No valid lines found in input"}), 400
    if len(lines) > 20:
        return jsonify({"error": f"Too many lines ({len(lines)}), max is 20"}), 400

    with get_db(_get_db_path()) as con:
        con.execute(
            "DELETE FROM critical_lines WHERE symbol=? AND date=?",
            (symbol, date_str)
        )
        for ln in lines:
            con.execute(
                "INSERT INTO critical_lines (symbol, date, line_type, price, strength, armed)"
                " VALUES (?, ?, ?, ?, ?, 1)",
                (symbol, date_str, ln["line_type"], ln["price"], ln["strength"])
            )

    log.info(f"Lines input: saved {len(lines)} lines for {symbol} {date_str}")
    return jsonify({"count": len(lines), "lines": lines})


# ── Cancel-all / Flatten API ──────────────────────────────────────────────────

@app.route("/api/cancel-all", methods=["POST"])
def api_cancel_all():
    """
    POST /api/cancel-all
    Cancels all pending IB orders (reqGlobalCancel) and flattens all filled
    positions by placing reverse MKT orders via PAPER.
    Updates DB: PENDING/SUBMITTING/SUBMITTED → CANCELLED; FILLED → CLOSED.
    Does NOT delete any records.
    Returns {ib_cancel, ib_flatten, db_cancelled, db_flattened, errors}.
    """
    from lib.ib_client import IBClient
    from ib_insync import MarketOrder
    now    = datetime.now(timezone.utc).isoformat()
    result = {"ib_cancel": "skipped", "ib_flatten": 0,
              "db_cancelled": 0, "db_flattened": 0, "errors": []}
    ibc = None
    try:
        cfg = _get_cfg()
        ibc = IBClient(cfg)
        ibc.connect(live=True, paper=True)

        # Cancel all open IB orders on both connections
        for label, ib_conn in [("LIVE", ibc.live), ("PAPER", ibc.paper)]:
            if ib_conn and ib_conn.isConnected():
                try:
                    ib_conn.reqGlobalCancel()
                    log.info(f"[cancel-all] reqGlobalCancel → {label}")
                except Exception as e:
                    result["errors"].append(f"reqGlobalCancel {label}: {e}")
        result["ib_cancel"] = "ok"

        # Flatten open positions via PAPER
        if ibc.paper and ibc.paper.isConnected():
            try:
                ibc.paper.reqPositions()
                ibc.paper.sleep(1.5)
                for pos in ibc.paper.positions():
                    qty = pos.position
                    if qty == 0:
                        continue
                    action = "SELL" if qty > 0 else "BUY"
                    try:
                        ibc.place_order(pos.contract,
                                        MarketOrder(action, abs(qty)))
                        result["ib_flatten"] += 1
                        log.info(f"[cancel-all] flatten {pos.contract.symbol}"
                                 f" {qty:+} → {action} MKT")
                    except Exception as e:
                        result["errors"].append(
                            f"close {pos.contract.symbol}: {e}")
            except Exception as e:
                result["errors"].append(f"positions: {e}")

    except Exception as e:
        result["errors"].append(f"IB connect: {e}")
        result["ib_cancel"] = "failed"
        log.warning(f"[cancel-all] IB error: {e}")
    finally:
        if ibc:
            try:
                ibc.disconnect()
            except Exception:
                pass

    # DB: update statuses — no records deleted
    try:
        with get_db(_get_db_path()) as con:
            r1 = con.execute(
                "UPDATE commands SET status='CANCELLED', updated_at=?"
                " WHERE status IN ('PENDING','SUBMITTING','SUBMITTED')",
                (now,)
            )
            result["db_cancelled"] = r1.rowcount
            r2 = con.execute(
                "UPDATE commands SET status='CLOSED',"
                " exit_reason='manual_flatten', exit_time=?, updated_at=?"
                " WHERE status='FILLED'",
                (now, now)
            )
            result["db_flattened"] = r2.rowcount
    except Exception as e:
        result["errors"].append(f"DB: {e}")
        log.error(f"[cancel-all] DB update failed: {e}")

    log.info(f"[cancel-all] {result}")
    return jsonify(result)


# ── Reset API ─────────────────────────────────────────────────────────────────

_WIPE_TABLES = ("commands", "positions", "ib_events", "system_state")


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """
    POST /api/reset
    Body JSON: {cancel_ib: bool, wipe_db: bool}
    Defaults: both true.

    1. Optionally call reqGlobalCancel on LIVE and PAPER.
    2. Optionally wipe operational DB tables (keeps critical_lines, release_notes).
    Returns {ib_cancel: "ok"|"failed"|"skipped", wipe: {table: count}, errors: [...]}
    """
    body      = request.get_json(force=True) or {}
    do_ib     = body.get("cancel_ib", True)
    do_wipe   = body.get("wipe_db",   True)

    result = {"ib_cancel": "skipped", "wipe": {}, "errors": []}

    # ── 1. IB cancel ──────────────────────────────────────────────────────────
    if do_ib:
        try:
            from lib.ib_client import IBClient
            cfg = _get_cfg()
            ibc = IBClient(cfg)
            ibc.connect(live=True, paper=True)
            try:
                ibc.live.reqGlobalCancel()
                log.info("[reset] reqGlobalCancel sent to LIVE")
            except Exception as e:
                result["errors"].append(f"LIVE cancel: {e}")
            try:
                ibc.paper.reqGlobalCancel()
                log.info("[reset] reqGlobalCancel sent to PAPER")
            except Exception as e:
                result["errors"].append(f"PAPER cancel: {e}")
            ibc.disconnect()
            result["ib_cancel"] = "ok"
        except Exception as e:
            result["ib_cancel"] = "failed"
            result["errors"].append(f"IB connect: {e}")
            log.warning(f"[reset] IB cancel failed: {e}")

    # ── 2. DB wipe ────────────────────────────────────────────────────────────
    if do_wipe:
        try:
            with get_db(_get_db_path()) as con:
                for tbl in _WIPE_TABLES:
                    cnt = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                    con.execute(f"DELETE FROM {tbl}")
                    result["wipe"][tbl] = cnt
            log.info(f"[reset] DB wiped: {result['wipe']}")
        except Exception as e:
            result["errors"].append(f"DB wipe: {e}")
            log.error(f"[reset] DB wipe failed: {e}")

    return jsonify(result)


# ── Report API ────────────────────────────────────────────────────────────────

@app.route("/api/report")
def api_report():
    """
    GET /api/report?source=X&symbol=Y&limit=N
    Reads from completed_trades — guaranteed no duplicates, no missing data.
      trades       — list of completed trades (newest first)
      by_source    — per-source stats
      by_exit      — per-exit-reason stats
      summary      — overall totals
      equity_curve — [{time, pnl, cumulative}] sorted by exit_time
    """
    source  = request.args.get("source")
    symbol  = request.args.get("symbol")
    limit   = int(request.args.get("limit", 5000))

    query  = "SELECT * FROM completed_trades WHERE 1=1"
    params = []
    if source:
        query += " AND source=?"
        params.append(source)
    if symbol:
        query += " AND symbol=?"
        params.append(symbol)
    query += " ORDER BY exit_time ASC LIMIT ?"
    params.append(limit)

    with get_db(_get_db_path()) as con:
        rows = _rows_to_list(con.execute(query, params).fetchall())

    trades_with_pnl = rows  # completed_trades always has pnl_points (NOT NULL)

    # ── by_source ──
    src_map: dict = {}
    for r in rows:
        src = r["source"] or "unknown"
        if src not in src_map:
            src_map[src] = {"source": src, "count": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
        src_map[src]["count"] += 1
        pnl = r.get("pnl_points")
        if pnl is not None:
            src_map[src]["total_pnl"] += pnl
            if pnl > 0:
                src_map[src]["wins"] += 1
            else:
                src_map[src]["losses"] += 1
    for s in src_map.values():
        n = s["wins"] + s["losses"]
        s["win_rate"]  = round(100.0 * s["wins"] / n, 1) if n else None
        s["avg_pnl"]   = round(s["total_pnl"] / n, 4)    if n else None
        s["total_pnl"] = round(s["total_pnl"], 4)
    by_source = sorted(src_map.values(), key=lambda x: x["total_pnl"], reverse=True)

    # ── by_exit ──
    exit_map: dict = {}
    for r in rows:
        reason = r["exit_reason"] or "unknown"
        exit_map[reason] = exit_map.get(reason, 0) + 1
    by_exit = [{"reason": k, "count": v} for k, v in sorted(exit_map.items())]

    # ── summary ──
    pnl_vals = [r["pnl_points"] for r in trades_with_pnl]
    total_pnl = sum(pnl_vals) if pnl_vals else 0.0
    wins = sum(1 for p in pnl_vals if p > 0)
    summary = {
        "total_trades": len(rows),
        "trades_with_pnl": len(pnl_vals),
        "total_pnl":    round(total_pnl, 4),
        "win_rate":     round(100.0 * wins / len(pnl_vals), 1) if pnl_vals else None,
        "avg_pnl":      round(total_pnl / len(pnl_vals), 4)    if pnl_vals else None,
        "best_trade":   round(max(pnl_vals), 4)                 if pnl_vals else None,
        "worst_trade":  round(min(pnl_vals), 4)                 if pnl_vals else None,
    }

    # ── equity_curve ──
    equity_curve = []
    cumulative = 0.0
    for r in trades_with_pnl:
        cumulative += r["pnl_points"]
        equity_curve.append({
            "time":       r["exit_time"],
            "pnl":        round(r["pnl_points"], 4),
            "cumulative": round(cumulative, 4),
            "source":     r["source"] or "unknown",
        })

    return jsonify({
        "trades":       list(reversed(rows)),   # newest first for table display
        "by_source":    by_source,
        "by_exit":      by_exit,
        "summary":      summary,
        "equity_curve": equity_curve,
    })


# ── HTML routes ───────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("dashboard.html", active="dashboard")


@app.route("/active")
def active_page():
    return render_template("active.html", active="active")


@app.route("/positions")
def positions_page():
    return render_template("positions.html", active="positions")


@app.route("/orders")
def orders_page():
    return render_template("orders.html", active="orders")


@app.route("/ib-trace")
def ib_trace_page():
    return render_template("ib_trace.html", active="ib_trace")


@app.route("/logs")
def logs_page():
    cfg = _get_cfg()
    log_dir = Path(cfg.paths.logs)
    components = sorted([p.stem for p in log_dir.glob("*.log")]) if log_dir.exists() else ["broker"]
    return render_template("logs.html", active="logs", components=components)


@app.route("/preflight")
def preflight_page():
    return render_template("preflight.html", active="preflight")


@app.route("/release-notes")
def release_notes_page():
    return render_template("release_notes.html", active="release_notes")


@app.route("/db")
def db_view_page():
    return render_template("db_view.html", active="db_view")


@app.route("/report")
def report_page():
    return render_template("report.html", active="report")


@app.route("/lines")
def lines_page():
    cfg = _get_cfg()
    return render_template("lines.html", active="lines",
                           symbols=cfg.symbols or ["MES"])


# ── Fetch Status ──────────────────────────────────────────────────────────────

_FETCH_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


def _is_trading_day(d) -> bool:
    return d.weekday() < 5 and d.strftime("%Y-%m-%d") not in _FETCH_HOLIDAYS


def _fetch_symbols():
    try:
        cfg = _get_cfg()
        override = getattr(getattr(cfg, "fetcher", None), "symbols_override", None)
        return list(override) if override else list(cfg.symbols)
    except Exception:
        return ["MES"]


@app.route("/api/fetch-status")
def api_fetch_status():
    from datetime import date as date_cls, timedelta
    today = date_cls.today()
    days = []
    d = today
    while len(days) < 30:
        if _is_trading_day(d):
            days.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)

    symbols = _fetch_symbols()

    with get_db(_get_db_path()) as con:
        rows = con.execute(
            "SELECT symbol, date, file_type, status, rows_fetched, error_msg"
            " FROM fetch_log ORDER BY fetched_at DESC"
        ).fetchall()

    # Build lookup: (symbol, date, file_type) -> best status
    lookup: dict = {}
    for r in rows:
        key = (r["symbol"], r["date"], r["file_type"])
        if key not in lookup:   # newest first, so first wins
            lookup[key] = {"status": r["status"],
                           "rows": r["rows_fetched"],
                           "error": r["error_msg"]}

    result = {}
    for ds in days:
        result[ds] = {}
        for sym in symbols:
            trades = lookup.get((sym, ds, "trades"))
            bidask = lookup.get((sym, ds, "bidask"))
            result[ds][sym] = {"trades": trades, "bidask": bidask}

    return jsonify({"days": days, "symbols": symbols, "grid": result})


@app.route("/api/fetch-now", methods=["POST"])
def api_fetch_now():
    import subprocess
    data = request.get_json(force=True) or {}
    symbol = data.get("symbol", "").upper()
    date_str = data.get("date", "")
    if not symbol or not date_str:
        return jsonify({"error": "symbol and date required"}), 400

    scheduler = Path(__file__).parent.parent / "fetch_scheduler.py"
    subprocess.Popen(
        [sys.executable, str(scheduler), "--symbol", symbol, "--date", date_str],
        cwd=str(Path(__file__).parent.parent)
    )
    return jsonify({"started": True, "symbol": symbol, "date": date_str})


@app.route("/fetch-status")
def fetch_status_page():
    return render_template("fetch_status.html", active="fetch_status")


# ── Traceback ─────────────────────────────────────────────────────────────────

@app.route("/api/traceback-stats")
def api_traceback_stats():
    """
    Returns verification pipeline stats:
      - closed_commands: all CLOSED commands
      - recorded: rows in completed_trades
      - verified: rows passing verified_trades view filters
      - drop_reasons: breakdown of why trades were not verified
      - daily: per-day {date, closed, recorded, verified, rate}
      - recent_verified: last 50 verified trades (from verified_trades view)
    """
    with get_db(_get_db_path()) as con:
        closed = con.execute(
            "SELECT COUNT(*) FROM commands WHERE status='CLOSED'"
        ).fetchone()[0]

        recorded = con.execute(
            "SELECT COUNT(*) FROM completed_trades"
        ).fetchone()[0]

        verified = con.execute(
            "SELECT COUNT(*) FROM verified_trades"
        ).fetchone()[0]

        # ── Drop reason breakdown ──────────────────────────────────────────
        # Tier 1: CLOSED but not recorded (incomplete data from broker)
        incomplete = con.execute("""
            SELECT COUNT(*) FROM commands
            WHERE status='CLOSED'
            AND id NOT IN (SELECT command_id FROM completed_trades)
        """).fetchone()[0]

        # Tier 2: recorded but filtered by view
        shutdown_exits = con.execute(
            "SELECT COUNT(*) FROM completed_trades WHERE exit_reason='SHUTDOWN'"
        ).fetchone()[0]

        instant_exits = con.execute(
            "SELECT COUNT(*) FROM completed_trades WHERE fill_time=exit_time"
        ).fetchone()[0]

        null_source = con.execute(
            "SELECT COUNT(*) FROM completed_trades WHERE source IS NULL"
        ).fetchone()[0]

        test_source = con.execute(
            "SELECT COUNT(*) FROM completed_trades WHERE source='test'"
        ).fetchone()[0]

        drop_reasons = [
            {"reason": "INCOMPLETE_DATA",  "count": incomplete,
             "note": "CLOSED command missing fill/exit/pnl"},
            {"reason": "SHUTDOWN_EXIT",    "count": shutdown_exits,
             "note": "Position closed by session shutdown"},
            {"reason": "INSTANT_EXIT",     "count": instant_exits,
             "note": "fill_time = exit_time (reconnect artifact)"},
            {"reason": "NULL_SOURCE",      "count": null_source,
             "note": "No source tag on command"},
            {"reason": "TEST_SOURCE",      "count": test_source,
             "note": "Test command excluded by design"},
        ]

        # ── Per-day breakdown ──────────────────────────────────────────────
        daily_rows = _rows_to_list(con.execute("""
            SELECT
                date(exit_time) as day,
                COUNT(*) as recorded,
                SUM(CASE WHEN exit_reason != 'SHUTDOWN'
                         AND fill_time != exit_time
                         AND source IS NOT NULL
                         AND source != 'test'
                    THEN 1 ELSE 0 END) as verified
            FROM completed_trades
            GROUP BY day
            ORDER BY day DESC
            LIMIT 30
        """).fetchall())

        # ── Recent verified trades ─────────────────────────────────────────
        recent = _rows_to_list(con.execute("""
            SELECT command_id, symbol, direction, entry_type,
                   fill_price, fill_time, exit_price, exit_time,
                   exit_reason, pnl_points, bracket_size, source,
                   chain_depth
            FROM verified_trades
            ORDER BY fill_time DESC
            LIMIT 50
        """).fetchall())

    rate = round(100.0 * verified / closed, 1) if closed else None

    return jsonify({
        "closed":       closed,
        "recorded":     recorded,
        "verified":     verified,
        "rate":         rate,
        "drop_reasons": drop_reasons,
        "daily":        daily_rows,
        "recent":       recent,
    })


@app.route("/traceback")
def traceback_page():
    return render_template("traceback.html", active="traceback")


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile
    try:
        from lib.logger import reset_loggers
        from lib.db import init_db, set_system_state

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            (log_dir / "broker.log").write_text(
                "2026-04-07T10:00:00 | INFO | broker | started\n"
                "2026-04-07T10:00:01 | ERROR | broker | test error\n"
            )

            init_db(db_path)

            with get_db(db_path) as con:
                set_system_state(con, "SESSION", "RUNNING")
                con.execute(
                    "INSERT INTO release_notes (program, version, summary)"
                    " VALUES ('test_prog', '0.1.0', 'Test note')"
                )
                con.execute(
                    "INSERT INTO system_state (key, value) VALUES"
                    " ('preflight_db', 'PASS|2026-04-07T10:00:00Z|')"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                )
                con.execute(
                    "INSERT INTO ib_events (event_type, component, message)"
                    " VALUES ('ERROR', 'broker', 'test IB error')"
                )
                con.execute(
                    "INSERT INTO critical_lines (symbol, date, line_type, price, strength)"
                    " VALUES ('MES', '2026-04-07', 'SUPPORT', 5500.0, 1)"
                )

            global _db_path, _cfg
            _db_path = db_path

            # Patch config paths for log dir
            class _FakePaths:
                db   = str(db_path)
                logs = str(log_dir)
            class _FakeCfg:
                paths = _FakePaths()
            _cfg = _FakeCfg()

            app.config["TESTING"] = True
            client = app.test_client()

            routes_200 = [
                "/",
                "/active",
                "/positions",
                "/orders",
                "/ib-trace",
                "/preflight",
                "/release-notes",
                "/api/price",
                "/api/session-state",
                "/api/stats",
                "/api/nearby",
                "/api/commands",
                "/api/commands?status_in=PENDING,SUBMITTED",
                "/api/positions",
                "/api/ib-events",
                "/api/preflight",
                "/api/logs?component=broker",
                "/api/logs?component=broker&level=ERROR",
                "/api/release-notes",
                "/api/state",
            ]

            for route in routes_200:
                resp = client.get(route)
                assert resp.status_code == 200, \
                    f"Route {route}: expected 200, got {resp.status_code}"

            # /logs page needs components list — test separately
            resp = client.get("/logs")
            assert resp.status_code == 200, f"/logs failed"

            # Validate shapes
            pf = client.get("/api/preflight").get_json()
            assert "preflight_db" in pf
            assert pf["preflight_db"]["status"] == "PASS"

            rn = client.get("/api/release-notes").get_json()
            assert isinstance(rn, list) and len(rn) >= 1
            assert rn[0]["program"] == "test_prog"

            stats = client.get("/api/stats").get_json()
            assert isinstance(stats, dict)

            cmds_all = client.get("/api/commands").get_json()
            assert isinstance(cmds_all, list)

            logs_data = client.get("/api/logs?component=broker").get_json()
            assert isinstance(logs_data["lines"], list)
            assert len(logs_data["lines"]) == 2

            logs_err = client.get("/api/logs?component=broker&level=ERROR").get_json()
            assert len(logs_err["lines"]) == 1, \
                f"Expected 1 ERROR line, got {len(logs_err['lines'])}"

            ib = client.get("/api/ib-events").get_json()
            assert isinstance(ib, list) and len(ib) >= 1
            assert ib[0]["event_type"] == "ERROR"

            _db_path = None
            _cfg = None
            reset_loggers()

        print("[self-test] visualizer: PASS")
        return True

    except Exception as e:
        print(f"[self-test] visualizer: FAIL -- {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galao visualizer")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--no-price-feed", action="store_true",
                        help="Skip IB price feed thread (useful for testing)")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    cfg = get_config()
    init_db(Path(cfg.paths.db))

    if not args.no_price_feed:
        import atexit
        from visualizer.price_feed import start as start_price_feed, stop as stop_price_feed
        symbol = cfg.symbols[0] if cfg.symbols else "MES"
        start_price_feed(cfg, symbol=symbol, interval=5)
        atexit.register(stop_price_feed)
        log.info(f"Price feed started for {symbol}")

    log.info(f"Visualizer starting on {cfg.visualizer.host}:{cfg.visualizer.port}")
    app.run(host=cfg.visualizer.host, port=cfg.visualizer.port, debug=False)
