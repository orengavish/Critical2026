"""
algo/preprocessor.py
Convert raw trades CSV into 3 parquet files per symbol/date.

Input CSV columns: timestamp, price, size, side  (side: 'B' or 'S')

Outputs to data/algo_processed/:
  {SYM}_{DATE}_vol_profile.parquet  — price, total_vol, buy_vol, sell_vol, delta
  {SYM}_{DATE}_ticks_dir.parquet    — timestamp, price, size, tick_direction
  {SYM}_{DATE}_sessions.parquet     — single-row session stats (POC, VAH, VAL, open, high, low)

Usage:
    python algo/preprocessor.py --self-test
    python algo/preprocessor.py --date 2026-07-01 --symbol MES [--force]
"""

import sys
import argparse
import os
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import numpy as np


def _load_config():
    import yaml
    cfg_path = Path(__file__).parent / "algo_config.yaml"
    with open(cfg_path) as f:
        raw = yaml.safe_load(f)
    return SimpleNamespace(
        data=SimpleNamespace(**raw["data"]),
    )


def snap_price(price):
    return round(round(float(price) * 4) / 4, 10)


def _compute_value_area(vol_profile: pd.DataFrame, target_pct: float = 0.70):
    """
    Given vol_profile df (price, total_vol), compute POC, VAH, VAL.
    Value area = smallest contiguous price range containing >= target_pct of volume.
    Standard approach: start at POC, expand up/down one bucket at a time, take whichever
    side adds more volume first.
    """
    df = vol_profile.sort_values("price").reset_index(drop=True)
    total = df["total_vol"].sum()
    if total == 0:
        mid = df["price"].median()
        return mid, mid, mid

    poc_idx = df["total_vol"].idxmax()
    poc = df.loc[poc_idx, "price"]

    target = total * target_pct
    accumulated = df.loc[poc_idx, "total_vol"]
    lo_idx = poc_idx
    hi_idx = poc_idx

    while accumulated < target:
        can_go_down = lo_idx > 0
        can_go_up = hi_idx < len(df) - 1
        if not can_go_down and not can_go_up:
            break
        vol_down = df.loc[lo_idx - 1, "total_vol"] if can_go_down else -1
        vol_up   = df.loc[hi_idx + 1, "total_vol"] if can_go_up   else -1
        if vol_up >= vol_down:
            hi_idx += 1
            accumulated += df.loc[hi_idx, "total_vol"]
        else:
            lo_idx -= 1
            accumulated += df.loc[lo_idx, "total_vol"]

    vah = df.loc[hi_idx, "price"]
    val = df.loc[lo_idx, "price"]
    return poc, vah, val


def run(date: str, symbol: str, history_path: str, processed_path: str,
        force: bool = False) -> bool:
    """
    Process one symbol/date. Returns True if processed, False if skipped (already exists).
    """
    out_dir = Path(processed_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    vol_file  = out_dir / f"{symbol}_{date}_vol_profile.parquet"
    tick_file = out_dir / f"{symbol}_{date}_ticks_dir.parquet"
    sess_file = out_dir / f"{symbol}_{date}_sessions.parquet"

    if not force and vol_file.exists() and tick_file.exists() and sess_file.exists():
        return False  # already processed

    csv_path = Path(history_path) / f"{symbol}_{date}_trades.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {csv_path}")

    df = pd.read_csv(csv_path)
    required = {"timestamp", "price", "size", "side"}
    if not required.issubset(df.columns):
        raise ValueError(f"CSV missing columns: {required - set(df.columns)}")

    df["price"] = df["price"].apply(snap_price)
    df["size"]  = df["size"].astype(int)

    # ── tick_direction ──────────────────────────────────────────────────────
    prev_price = df["price"].shift(1)
    df["tick_direction"] = np.where(
        df["price"] > prev_price, "buy",
        np.where(df["price"] < prev_price, "sell", "neutral")
    )
    # first tick: use side
    if len(df) > 0:
        first_side = str(df.iloc[0]["side"]).upper()
        df.iloc[0, df.columns.get_loc("tick_direction")] = "buy" if first_side == "B" else "sell"

    tick_df = df[["timestamp", "price", "size", "tick_direction"]].copy()
    tick_df.to_parquet(str(tick_file), index=False)

    # ── vol_profile ─────────────────────────────────────────────────────────
    df["buy_vol"]  = np.where(df["side"].str.upper() == "B", df["size"], 0)
    df["sell_vol"] = np.where(df["side"].str.upper() == "S", df["size"], 0)

    vp = df.groupby("price", as_index=False).agg(
        total_vol=("size", "sum"),
        buy_vol=("buy_vol", "sum"),
        sell_vol=("sell_vol", "sum"),
    )
    vp["delta"] = vp["buy_vol"] - vp["sell_vol"]
    vp.to_parquet(str(vol_file), index=False)

    # ── sessions.parquet ────────────────────────────────────────────────────
    poc, vah, val = _compute_value_area(vp)
    sess = pd.DataFrame([{
        "date":          date,
        "symbol":        symbol,
        "poc":           poc,
        "vah":           vah,
        "val":           val,
        "session_open":  float(df.iloc[0]["price"]),
        "session_high":  float(df["price"].max()),
        "session_low":   float(df["price"].min()),
        "total_volume":  int(df["size"].sum()),
    }])
    sess.to_parquet(str(sess_file), index=False)

    return True


# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    import tempfile, random
    try:
        with tempfile.TemporaryDirectory() as tmp:
            hist_dir  = Path(tmp) / "history"
            proc_dir  = Path(tmp) / "processed"
            hist_dir.mkdir()

            # Build synthetic CSV: 500 rows, prices 5000–5010 in 0.25 steps
            prices = [round(5000.0 + i * 0.25, 2) for i in range(41)]
            rng = random.Random(42)
            rows = []
            for i in range(500):
                rows.append({
                    "timestamp": f"2026-07-01T09:{i//60:02d}:{i%60:02d}",
                    "price": rng.choice(prices),
                    "size": rng.randint(1, 20),
                    "side": "B" if i % 2 == 0 else "S",
                })
            import csv
            csv_path = hist_dir / "MES_2026-07-01_trades.csv"
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["timestamp","price","size","side"])
                writer.writeheader()
                writer.writerows(rows)

            # Run preprocessor
            result = run("2026-07-01", "MES", str(hist_dir), str(proc_dir))
            assert result is True, "Should have processed (not skipped)"

            # Check files exist
            vp_path   = proc_dir / "MES_2026-07-01_vol_profile.parquet"
            tick_path = proc_dir / "MES_2026-07-01_ticks_dir.parquet"
            sess_path = proc_dir / "MES_2026-07-01_sessions.parquet"
            assert vp_path.exists(),   "vol_profile.parquet missing"
            assert tick_path.exists(), "ticks_dir.parquet missing"
            assert sess_path.exists(), "sessions.parquet missing"

            # Vol sums match
            vp   = pd.read_parquet(str(vp_path))
            tick = pd.read_parquet(str(tick_path))
            sess = pd.read_parquet(str(sess_path))

            total_from_vp   = vp["total_vol"].sum()
            total_from_tick = tick["size"].sum()
            assert total_from_vp == total_from_tick, \
                f"Vol mismatch: vp={total_from_vp} tick={total_from_tick}"

            # POC is a valid price
            poc = float(sess.iloc[0]["poc"])
            assert poc in set(vp["price"].tolist()), f"POC {poc} not in price list"

            # VAH/VAL are valid prices
            vah = float(sess.iloc[0]["vah"])
            val = float(sess.iloc[0]["val"])
            assert vah >= val, f"VAH {vah} < VAL {val}"

            # Skip if already exists
            result2 = run("2026-07-01", "MES", str(hist_dir), str(proc_dir))
            assert result2 is False, "Should skip if files exist"

            # Force re-run
            result3 = run("2026-07-01", "MES", str(hist_dir), str(proc_dir), force=True)
            assert result3 is True, "Force should re-process"

            # tick_direction column present
            assert "tick_direction" in tick.columns
            assert set(tick["tick_direction"].unique()).issubset({"buy", "sell", "neutral"})

        print("[self-test] preprocessor: PASS")
        return True
    except Exception as e:
        print(f"[self-test] preprocessor: FAIL — {e}")
        import traceback; traceback.print_exc()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--date", help="YYYY-MM-DD")
    parser.add_argument("--symbol", help="e.g. MES")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    cfg = _load_config()
    if not args.date or not args.symbol:
        print("Usage: preprocessor.py --date YYYY-MM-DD --symbol SYM [--force]")
        sys.exit(1)
    processed = run(args.date, args.symbol, cfg.data.history_path,
                    cfg.data.processed_path, args.force)
    print(f"{'Processed' if processed else 'Skipped (already exists)'}: {args.symbol} {args.date}")
