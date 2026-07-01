# Algo System — Implementation Plan
**Version:** 1.0 | **Date:** 2026-04-27  
**Status:** Approved  
**Rules:** `algo_implementation_rules.md`  
**Design:** `system_design.md`  
**Audit:** `choices_done.md`

---

## Quick Reference — Breakpoints

| ID | Breakpoint | Stop after task |
|---|---|---|
| BP-A1 | First data in DB for all symbols | A-03 |
| BP-A2 | Scope A complete | A-05 |
| BP-B1 | First verified trade in DB | B-03 |
| BP-B2 | Scope B complete (≥80% verification rate) | B-06 |
| BP-C1 | First signal generated | C-05 |
| BP-C2 | Scope C complete (all 25 approaches running) | C-17 |
| BP-D1 | Scope D complete | D-05 |

**Between breakpoints: auto-proceed per post-step protocol (Section 5 of rules doc).**

---

## Performance Requirements

| Scope | Must pass before proceeding |
|---|---|
| A | 100% of configured symbols fetched per trading day. Zero corrupt files. No missing sessions. |
| B | ≥ 80% of generated commands reach verified_trade status. Every verified trade has: valid entry, valid non-shutdown exit, no tracing errors, DB record with P&L + both timestamps. |
| C | All 25 approaches produce output every session. ≥ 2 signals generated per session average. A/B harness manually triggerable from browser. |
| D | Backtest results deterministic (± 0.1% across runs). 100% of Scope B verified trades used as input. |

---

## Existing System — What NOT to Rewrite

Before building anything in each scope, confirm these already work:

| Component | File | Already does |
|---|---|---|
| Tick fetcher | `trader/fetcher.py` | Trades + bid/ask, all 4 symbols, pagination, dedup, progress tracking |
| DB layer | `lib/db.py` | SQLite WAL, all CRUD helpers, schema init |
| Config loader | `lib/config_loader.py` | YAML config, dot-notation access |
| Logger | `lib/logger.py` | Per-component log files, standard format |
| Runner | `trader/runner.py` | Subprocess orchestration, clean shutdown |
| Browser | Flask visualizer in `trader/` | Port 5000, existing pages/routes |
| Back-trading engine | `back-trading/engine.py` | Tick replay simulator, grader |

---

## Scope A — Enhanced Fetcher

**Goal:** Every trading day, all configured symbols automatically fetch both trades and bid/ask files into `data/history/`. Browser shows fetch status. No manual invocation needed.

**Note:** The fetcher already works correctly. Scope A is automation + observability, not a rewrite.

---

### A-01 — Fetcher Audit
**Goal:** Fully understand the existing fetcher before touching it.

**Steps:**
1. Read `trader/fetcher.py` end-to-end
2. Run manually: `python fetcher.py --symbol MES --date {yesterday} --bid-ask`
3. Verify both output files appear in `data/history/`
4. Run: `python fetcher.py --self-test` — must pass
5. Document: which config keys it reads, what progress DB it uses, how it handles existing files

**Self-test:** `python fetcher.py --self-test` exits 0. Both CSV files present and non-empty.

**Output:** Understanding doc in `choices_done.md` — list any gaps found.

---

### A-02 — Multi-Symbol Config
**Goal:** `config.yaml` drives the full symbol list for all fetch operations.

**Steps:**
1. Confirm `trader/config.yaml` has `symbols:` list (currently `[MES]`)
2. Add MNQ, MYM, M2K to the list
3. Verify `_EXCHANGE_MAP` in `fetcher.py` already covers all four (it does)
4. Add new config section `fetcher:` with:
   - `auto_fetch_enabled: true`
   - `fetch_bid_ask: true`
   - `fetch_on_startup: true` (fetch prev day if missing on runner start)
   - `symbols_override: []` (empty = use top-level symbols list)
5. Run fetcher manually for each new symbol to confirm they work

**Self-test:** `python fetcher.py --self-test` passes. Config loads all 4 symbols cleanly.

---

### A-03 — Automatic Daily Fetch (Scheduler) `[BREAKPOINT BP-A1 after this]`
**Goal:** After market close each day, runner auto-fetches prev day's data for all symbols without any manual action.

**Steps:**
1. Add `fetch_scheduler.py` to `trader/` — a lightweight scheduler that:
   - Runs as a subprocess under `runner.py`
   - After market close time (configurable, default 17:00 CT), triggers fetch for all symbols, both trades + bid/ask
   - Skips symbols/dates already present and valid (uses fetcher's existing progress DB)
   - Logs success/failure per symbol to DB table `fetch_log` (new table — see step 2)
2. Add `fetch_log` table to DB schema in `lib/db.py`:
   ```
   fetch_log(id, symbol, date, file_type, status, rows_fetched, error_msg, fetched_at)
   ```
   `file_type`: `trades` | `bidask`  
   `status`: `ok` | `skipped` | `error`
3. Register `fetch_scheduler.py` in `runner.py` as a managed subprocess
4. Test: run runner, wait for scheduled trigger (or temporarily set trigger to 2 minutes from now), confirm files appear

**Self-test:** `python fetch_scheduler.py --self-test` exits 0. DB table `fetch_log` created. Config loads. At least one symbol fetched in test mode.

**`[BREAKPOINT BP-A1]` — Stop here. Confirm first real data appears in DB fetch_log for all symbols before continuing.**

---

### A-04 — Fetch Status Browser Page
**Goal:** Browser shows per-symbol, per-day fetch status — what's complete, what's missing, any errors.

**Steps:**
1. Add route `/fetch-status` to the Flask visualizer
2. Page shows a table: `symbol × date` grid for last 30 days
   - Green: both files present and valid
   - Yellow: partial (one file missing)
   - Red: error or missing
   - Grey: weekend/holiday (no fetch expected)
3. Pull data from `fetch_log` DB table
4. Add "Fetch Now" button per symbol (triggers immediate fetch for selected symbol + date)
5. Add `/fetch-status` link to main nav

**Self-test:** Page loads at `localhost:5000/fetch-status`. Grid renders. "Fetch Now" triggers a fetch and updates the table.

---

### A-05 — Data Validation Report `[BREAKPOINT BP-A2 after this]`
**Goal:** Every fetched file is automatically validated. Corrupt or incomplete files are flagged.

**Steps:**
1. Add `validate_fetch.py` to `trader/` — runs after each fetch cycle:
   - Checks CSV schema (correct columns, no empty rows)
   - Checks row count vs prior days (flag if < 20% of median — thin session)
   - Checks price sanity (no zero prices, no jumps > 50 points)
   - Checks timestamp monotonicity (rows in time order)
   - Writes result to `fetch_log` with validation details
2. Add validation status to `/fetch-status` browser page (tooltip or color variant)
3. Run on all existing historical files to baseline current state

**Self-test:** `python validate_fetch.py --self-test` exits 0. Validation runs on 5 past files and correctly flags a deliberately corrupted test file.

**Performance check (Scope A):**
- All 4 symbols have data for every trading day since fetching began
- Zero corrupt files in fetch_log
- `/fetch-status` page shows correct status for all days

**`[BREAKPOINT BP-A2]` — Scope A complete. Review fetch_status page together before starting Scope B.**

---

## Scope B — Traceback System

**Goal:** Every command generated by the system is traced from entry to exit. Valid trades are stored as verified_trades in DB with full P&L and timestamps. Scope D (backtrader) uses this table as its primary input.

**Note:** The existing `tracer.py` handles TEST commands via GUI. Scope B builds an automatic, always-on traceback for ALL commands — existing decider commands now, algo signals later.

---

### B-01 — Verified Trades DB Schema
**Goal:** Design and create the `verified_trades` table that captures fully-traced, validated trades.

**Steps:**
1. Add to `lib/db.py` schema:
   ```sql
   CREATE TABLE IF NOT EXISTS verified_trades (
     id              INTEGER PRIMARY KEY AUTOINCREMENT,
     command_id      INTEGER NOT NULL,        -- FK to commands.id
     symbol          TEXT    NOT NULL,
     direction       TEXT    NOT NULL,        -- BUY | SELL
     entry_price     REAL    NOT NULL,
     entry_time_utc  TEXT    NOT NULL,
     exit_price      REAL    NOT NULL,
     exit_time_utc   TEXT    NOT NULL,
     exit_type       TEXT    NOT NULL,        -- TP | SL | STAGNATION | MANUAL
     pnl_points      REAL    NOT NULL,        -- in ticks/points
     pnl_dollars     REAL    NOT NULL,
     bracket_size    REAL    NOT NULL,
     line_price      REAL    NOT NULL,
     line_type       TEXT    NOT NULL,        -- SUPPORT | RESISTANCE
     verified        INTEGER NOT NULL DEFAULT 0,  -- 1=verified, 0=dropped
     drop_reason     TEXT,                    -- NULL if verified
     traced_at       TEXT    NOT NULL,        -- when traceback ran
     source          TEXT    NOT NULL DEFAULT 'decider'  -- decider | algo
   )
   ```
2. Add DB migration to `init_db()` — safe to run on existing DBs
3. Add helper functions: `insert_verified_trade()`, `get_verified_trades()`, `count_verified()`, `count_dropped()`

**Self-test:** `python -m lib.db --self-test` exits 0. `verified_trades` table created. Helper functions insert and retrieve a test row correctly.

---

### B-02 — Traceback Engine
**Goal:** A background process that monitors commands → positions → CLOSED and captures the complete lifecycle of every trade.

**Steps:**
1. Create `trader/traceback.py` — runs as a subprocess under `runner.py`:
   - Polls DB every `traceback_poll_seconds` (config)
   - For every command reaching status `CLOSED` that has no entry in `verified_trades`:
     - Reads entry from `commands`: entry_price, entry_time (when status became SUBMITTED→FILLED), direction
     - Reads exit from `positions`: exit_price, exit_time, exit_type
     - Computes P&L: `(exit_price - entry_price) × direction_sign × contract_multiplier`
     - Writes raw (unverified) row to `verified_trades`
2. Add config keys under `traceback:`:
   - `traceback_poll_seconds: 5`
   - `contract_multiplier: 5` (MES = $5/point)
3. Register in `runner.py` as a managed subprocess
4. Handle edge case: commands that closed during a SHUTDOWN exit (mark exit_type accordingly)

**Self-test:** `python traceback.py --self-test` exits 0. DB accessible. Logic test: given a synthetic closed command, correct P&L is computed and row is written.

---

### B-03 — Verification Logic `[BREAKPOINT BP-B1 after this]`
**Goal:** Apply the verified trade definition to every raw traceback row. Mark as verified=1 or verified=0 with drop reason.

**Verified trade criteria (from rules doc):**
1. `entry_price` and `entry_time_utc` are present and non-null
2. `exit_type` is NOT a shutdown exit (`SHUTDOWN` | `PANIC` | `ABORT`)
3. No errors in `ib_events` table linked to this command_id during its lifecycle
4. `pnl_points` is a finite number (not NaN/null), `exit_time_utc` is present

**Steps:**
1. Add `_verify_trade(row) → (verified: bool, drop_reason: str | None)` function in `traceback.py`
2. Apply verification immediately after writing each raw row
3. Update `verified=1` or `verified=0, drop_reason=<reason>` in the same DB transaction
4. Expose counters: `total_traced`, `total_verified`, `total_dropped` in `system_state` table (updated after each trace cycle)
5. Run on all existing CLOSED commands retrospectively

**Self-test:** `python traceback.py --self-test` exits 0. Verification logic tested with 4 synthetic cases: one passing all criteria, one with null entry, one with shutdown exit, one with IB error.

**`[BREAKPOINT BP-B1]` — Stop here. Inspect first real verified trade in DB. Check P&L calculation is correct vs what IB shows.**

---

### B-04 — Drop Analysis
**Goal:** Understand why trades are being dropped. Surface drop reasons clearly so issues can be fixed.

**Steps:**
1. Add `drop_reason` enum values: `NULL_ENTRY`, `SHUTDOWN_EXIT`, `IB_ERROR_DURING_TRADE`, `INCOMPLETE_DATA`
2. Group and count drop reasons in `system_state` (e.g. `TRACEBACK_DROP_SHUTDOWN_EXIT: 3`)
3. Add logging: every drop logs WARNING with command_id and reason
4. Add to browser `/traceback` page: drop reason breakdown table

**Self-test:** Synthetic test with each drop reason type. Each correctly categorized. Browser page renders.

---

### B-05 — Traceback Browser Page
**Goal:** Browser page showing all verified trades, stats, and live traceback health.

**Steps:**
1. Add route `/traceback` to Flask visualizer
2. Page shows:
   - Summary bar: total traced / verified / dropped, verification rate %
   - Verified trades table: symbol, direction, entry/exit price, P&L, exit type, date
   - Drop reasons chart: breakdown of why trades were dropped
   - Health indicator: traceback engine running (green) or stopped (red)
3. Add `/traceback` to main nav

**Self-test:** Page loads at `localhost:5000/traceback`. All sections render. Health indicator shows correct state.

---

### B-06 — Backtrader Export `[BREAKPOINT BP-B2 after this]`
**Goal:** Verified trades are exportable in a format Scope D can consume directly.

**Steps:**
1. Add `trader/export_verified.py`:
   - Reads all `verified=1` rows from `verified_trades`
   - Exports to `data/verified_trades_{YYYYMMDD}.csv` with columns:
     ```
     trade_id, symbol, direction, entry_price, entry_time_utc,
     exit_price, exit_time_utc, exit_type, pnl_points, pnl_dollars,
     bracket_size, line_price, line_type, source
     ```
   - Also exports as a SQLite-readable view for direct DB consumption
2. Add `--export` flag to `traceback.py` (triggers export)
3. Add "Export" button to `/traceback` browser page

**Self-test:** `python export_verified.py --self-test` exits 0. Export produces correctly formatted CSV. At least 1 verified trade in export.

**Performance check (Scope B):**
- `total_verified / total_traced ≥ 0.80`
- Every verified trade has all 4 required fields (entry price/time, exit price/time, P&L)
- Zero verified trades with null P&L

**`[BREAKPOINT BP-B2]` — Scope B complete. Review traceback page together. Confirm verification rate ≥ 80% before starting Scope C.**

---

## Scope C — First-Gen Algo Trader

**Goal:** Build the full 5×5×5 signal generation system. Configurable, A/B testable, integrated with runner and browser. No P&L target — correctness of signal generation is the only goal.

**Sub-project location:** `algo/` (new top-level sub-project alongside `trader/` and `back-trading/`)  
**Config:** `algo/algo_config.yaml` — all 25 approaches + combining layer + A/B harness params  
**DB:** Shares `trader/data/galao.db` via `lib/db.py` — adds new tables  
**Integration:** Registered with `trader/runner.py` as subprocess. All output to browser on port 5000.

---

### C-00 — Algo Sub-Project Scaffold
**Goal:** Create the `algo/` directory structure and connect it to runner + browser.

**Steps:**
1. Create `algo/` with:
   ```
   algo/
   ├── algo_config.yaml         # all algo params (see system_design.md § 8)
   ├── algo_runner.py           # entry point for the algo sub-system
   ├── preprocessor.py          # builds volume profiles, bars, direction
   ├── signal_bus.py            # collects all approach outputs → DB
   ├── approaches/
   │   ├── __init__.py
   │   ├── cl/                  # CL-1 through CL-5
   │   ├── corr/                # CORR-1 through CORR-5
   │   ├── vp/                  # VP-1 through VP-5
   │   ├── rd/                  # RD-1 through RD-5
   │   └── tod/                 # TOD-1 through TOD-5
   ├── combining/
   │   ├── category_aggregator.py
   │   ├── gating.py
   │   └── final_signal.py
   └── ab_harness/
       ├── harness.py
       └── metrics.py
   ```
2. Add new DB tables to `lib/db.py`:
   ```sql
   CREATE TABLE IF NOT EXISTS algo_signals (
     id           INTEGER PRIMARY KEY AUTOINCREMENT,
     approach_id  TEXT    NOT NULL,    -- e.g. "CL-1"
     category     TEXT    NOT NULL,    -- CL | CORR | VP | RD | TOD
     session_date TEXT    NOT NULL,
     timestamp    TEXT    NOT NULL,
     direction    TEXT    NOT NULL,    -- long | short | neutral
     strength     REAL    NOT NULL,
     confidence   REAL    NOT NULL,
     expiry       TEXT,
     tags         TEXT,               -- JSON list
     created_at   TEXT    NOT NULL
   );

   CREATE TABLE IF NOT EXISTS algo_final_signals (
     id                   INTEGER PRIMARY KEY AUTOINCREMENT,
     session_date         TEXT  NOT NULL,
     timestamp            TEXT  NOT NULL,
     direction            TEXT  NOT NULL,
     final_strength       REAL  NOT NULL,
     final_confidence     REAL  NOT NULL,
     rd_regime            TEXT,
     vp_day_type          TEXT,
     lunch_active         INTEGER,
     agreeing_categories  TEXT,       -- JSON list
     contracts            INTEGER,
     created_at           TEXT  NOT NULL
   );

   CREATE TABLE IF NOT EXISTS ab_results (
     id               INTEGER PRIMARY KEY AUTOINCREMENT,
     test_id          TEXT NOT NULL,
     param_path       TEXT NOT NULL,
     control_value    TEXT NOT NULL,
     variant_value    TEXT NOT NULL,
     control_metric   REAL,
     variant_metric   REAL,
     p_value          REAL,
     robustness       REAL,
     accepted         INTEGER,
     new_baseline     TEXT,
     tested_at        TEXT NOT NULL
   );
   ```
3. Register `algo/algo_runner.py` in `trader/runner.py` as a managed subprocess
4. `algo_runner.py --self-test` must exit 0 (checks config loads, DB writable, approaches dir exists)

**Self-test:** `python algo/algo_runner.py --self-test` exits 0. All 3 new tables created. Config loads cleanly. Approach directories exist.

---

### C-01 — Algo Config System
**Goal:** Full `algo_config.yaml` for all 25 approaches + combining + A/B harness, matching system_design.md § 8.

**Steps:**
1. Create `algo/algo_config.yaml` with all sections from `system_design.md § 8`:
   - System section (instrument, session, enabled_categories)
   - All 25 approach configs (CL1–CL5, CORR1–5, VP1–5, RD1–5, TOD1–5)
   - Aggregation configs per category
   - RD + VP gate multiplier tables
   - Final aggregation config
   - Position sizing config
   - A/B harness config (walk_forward, significance thresholds)
2. All approaches default to `enabled: true` except CORR (`enabled: false`)
3. Add `algo_config_loader.py` to `algo/` — loads and validates config, provides dot-notation access, logs warnings for missing keys

**Self-test:** `python algo/algo_config_loader.py --self-test` exits 0. All expected keys present. CORR correctly disabled. Missing key raises clear error (not silent None).

---

### C-02 — Data Preprocessor
**Goal:** For each session, convert raw `trades.csv` into processed volume profiles, resampled bars, and direction-classified ticks — the foundation all 25 approaches read from.

**Steps:**
1. Create `algo/preprocessor.py`:
   - **Input:** `data/history/{SYMBOL}_trades_{YYYYMMDD}.csv` (from Scope A)
   - **Output (written to `data/algo_processed/`):**
     - `{SYMBOL}_{DATE}_volume_profile_{W}tick.parquet` for W in [1, 3, 5]
     - `{SYMBOL}_{DATE}_bars_{R}min.parquet` for R in [1, 5, 15]
     - `{SYMBOL}_{DATE}_features.parquet` (ATR, realized_vol, session_high/low/open/close, POC, VAH, VAL)
     - `{SYMBOL}_{DATE}_ticks_with_direction.parquet` (original ticks + tick_rule direction column)
   - Volume profile columns: `price_bucket, total_vol, buy_vol, sell_vol, delta`
   - Bar columns: `timestamp, open, high, low, close, volume, bar_delta`
   - Tick direction: `buy` if price > prev_price, `sell` if lower, `carry_forward` if equal
2. Preprocessor runs nightly after fetch (add to `fetch_scheduler.py`)
3. Add `--date` and `--symbol` args for manual runs
4. Skip if processed files already exist (use `--force` to overwrite)

**Self-test:** `python algo/preprocessor.py --self-test` exits 0. Runs on one past date's trades file. All 9+ output files created. Volume profile columns correct. Direction column present.

---

### C-03 — Signal Bus
**Goal:** Central module all approaches use to emit signals. Writes to `algo_signals` DB table. Handles expiry and deduplication.

**Steps:**
1. Create `algo/signal_bus.py`:
   - `emit(signal: dict)` — validates schema, writes to `algo_signals` table
   - `get_active_signals(as_of: datetime) → list` — returns non-expired signals
   - `purge_expired()` — marks expired signals as stale (add `stale` column)
2. Signal schema validated on emit: approach_id, category, direction, strength (0–1), confidence (0–1) all required
3. Called by every approach — no approach writes directly to DB

**Self-test:** `python algo/signal_bus.py --self-test` exits 0. Emit writes row. Get_active_signals returns it. Expiry logic tested.

---

### C-04 — CL-1 (Volume Histogram Peaks)
**Goal:** First working approach. Reads prev day's volume profile, finds peaks, emits level signals.

**Steps:**
1. Create `algo/approaches/cl/cl1.py`:
   - Loads `{SYMBOL}_{DATE}_volume_profile_{W}tick.parquet` for W in config `bucket_size_ticks`
   - Detects peaks: local maxima where volume > `peak_prominence_ratio × neighborhood_mean`
   - Filters by `min_volume_abs`
   - Classifies each peak as support/resistance/neutral based on position vs session open
   - Emits one signal per peak via `signal_bus.emit()`
   - Strength = normalized prominence ratio (0–1)
2. Respects config: `CL1:` section of `algo_config.yaml`
3. Run at session start (not continuously — levels are static for the day)

**Self-test:** `python algo/approaches/cl/cl1.py --self-test` exits 0. Runs on one past date. Emits at least 1 signal. Signal schema valid. Bucket sizes 1/3/5 all produce output.

---

### C-05 — CL-2 (Directional Traffic) `[BREAKPOINT BP-C1 after this]`
**Goal:** Second approach. Reads tick data, counts directional touches per bucket, emits support/resistance signals.

**Steps:**
1. Create `algo/approaches/cl/cl2.py`:
   - Loads `{SYMBOL}_{DATE}_ticks_with_direction.parquet`
   - For each bucket: counts arrivals_from_below, arrivals_from_above, departures_to_below, departures_to_above
   - Computes bounce_rate_from_below (support score) and bounce_rate_from_above (resistance score)
   - Emits signals for buckets meeting `min_total_visits` and `bounce_threshold`
   - Tags signals as `bounce` (for VP gating later)
2. Respects `CL2:` config section

**Self-test:** `python algo/approaches/cl/cl2.py --self-test` exits 0. Runs on one past date. At least 1 signal emitted. Direction counts are non-negative and sum correctly.

**Performance check (interim):**
- CL-1 and CL-2 both emit signals on the test date
- Signals appear in `algo_signals` table
- Browser page (added in C-14) not needed yet — verify via DB query

**`[BREAKPOINT BP-C1]` — Stop here. Query `algo_signals` table. Review first signals manually. Do detected levels correspond to visible price reactions on the chart?**

---

### C-06 — VP-1 + VP-2 (Value Area + Day Type)
**Goal:** Session context — developing value area live, prior day VA classification at open.

**Steps:**
1. Create `algo/approaches/vp/vp1.py`:
   - Loads developing tick stream during session
   - Computes running POC/VAH/VAL at each update interval
   - Emits `regime` signal (not a price level) with metadata: `{poc, vah, val, poc_migration}`
   - Direction: `long` if POC rising, `short` if falling, `neutral` if stable
2. Create `algo/approaches/vp/vp2.py`:
   - Runs once at session open (pre-market)
   - Loads prior day's volume profile
   - Computes PDVAh, PDVAl, PDPOC
   - Compares today's open to prior VA
   - Emits day_type signal: `trend_day_up` | `trend_day_down` | `range_day` | `open_drive` | `open_rejection`
   - Tags for gating layer consumption

**Self-test:** Both `--self-test` exit 0. VP-2 correctly classifies 3 test dates (one range day, one trend day, one open drive).

---

### C-07 — RD-1 + RD-4 (ADX + Vol Percentile)
**Goal:** Bar-by-bar regime classification from two simple, robust approaches.

**Steps:**
1. Create `algo/approaches/rd/rd1.py`:
   - Loads `{SYMBOL}_{DATE}_bars_5min.parquet`
   - Computes ADX(period) + DI+/DI-
   - Emits regime signal per bar: `trending_up` | `trending_down` | `range_bound` | `compressed`
   - Confidence = ADX value normalized to [0, 1]
2. Create `algo/approaches/rd/rd4.py`:
   - Computes realized vol from bar returns
   - Ranks vs percentile_lookback_days of historical vol
   - Emits: `volatile_shock` | `normal` | `compressed`
3. Create `algo/approaches/rd/rd_composite.py`:
   - Reads latest signals from RD-1 and RD-4
   - Majority vote → composite regime label
   - Writes composite to `system_state` table (key: `ALGO_RD_REGIME`) for gating layer

**Self-test:** All 3 `--self-test` exit 0. Composite correctly resolves disagreement in test cases.

---

### C-08 — Category Aggregator
**Goal:** For each category, combine the N active approach signals into one category signal per the Layer 2 formula.

**Steps:**
1. Create `algo/combining/category_aggregator.py`:
   - Reads active signals per category from `algo_signals`
   - Applies `approach_weights` from config
   - Formula: `Σ(weight_i × strength_i × confidence_i)` per direction
   - Outputs one `CategorySignal` per category: direction, strength, confidence, agreeing_fraction
   - Applies `neutral_if_split` and `min_category_strength` filters
2. Runs on each bar tick (or per config update_frequency)

**Self-test:** `python algo/combining/category_aggregator.py --self-test` exits 0. Given 3 synthetic approach signals (2 long, 1 short), correctly aggregates to long with expected strength.

---

### C-09 — Gating Layer
**Goal:** RD and VP context multipliers applied to category signals. TOD-4 lunch suppression stub.

**Steps:**
1. Create `algo/combining/gating.py`:
   - Reads RD composite regime from `system_state`
   - Reads VP-2 day type from latest `algo_signals` (category=VP, approach=VP-2)
   - Applies `rd_gate_multipliers` and `vp_gate_multipliers` from config to each category signal
   - Applies `lunch_active` suppression (stub — always false until TOD-4 is built in C-16)
   - Outputs gated category signals with effective weights
2. All multiplier tables come from `algo_config.yaml` — no hardcoded values

**Self-test:** `python algo/combining/gating.py --self-test` exits 0. Given trending_up regime + trend_day type, CL breakout signals amplified and bounce signals suppressed per config.

---

### C-10 — Final Signal + Position Sizing
**Goal:** Layer 4+5: gated category signals → one final signal → contract count.

**Steps:**
1. Create `algo/combining/final_signal.py`:
   - Receives gated category signals from `gating.py`
   - Applies `category_base_weights` from config
   - Checks `min_categories_agreeing` confluence requirement
   - Outputs `FinalSignal`: direction, final_strength, final_confidence, agreeing_categories, contracts
   - `contracts = floor(base_contracts × strength × confidence × rd_size_mult)`
   - Writes to `algo_final_signals` DB table
   - If `final_strength < min_strength_to_trade`: writes neutral signal, no contracts

**Self-test:** `python algo/combining/final_signal.py --self-test` exits 0. Correct contracts computed for given inputs. Confluence filter correctly outputs neutral when only 1 category agrees.

---

### C-11 — Algo Signal Integration with Runner
**Goal:** Algo final signals flow into the commands pipeline so they create actual DB commands (and eventually real orders).

**Steps:**
1. Add `algo_decider.py` to `algo/` — reads `algo_final_signals` (non-neutral) and writes `PENDING` commands to `commands` table, same schema as existing decider
2. Commands written by algo_decider have `line_type = 'ALGO'` to distinguish from manual critical lines
3. Existing broker, position_manager, and traceback pick them up automatically (no changes needed — they work on all PENDING commands)
4. Register `algo_decider.py` in `runner.py`
5. Add `algo_decider.py --self-test`

**Self-test:** `python algo/algo_decider.py --self-test` exits 0. Given synthetic final_signal, correct PENDING command written to DB with line_type='ALGO'.

---

### C-12 — Browser: Algo Signal Dashboard
**Goal:** Browser page showing live algo signals, active regime, day type, and final signal output.

**Steps:**
1. Add route `/algo` to Flask visualizer with:
   - **Regime panel:** current RD composite + VP day type + lunch status
   - **Category signals panel:** per-category direction/strength/confidence bars
   - **Final signal panel:** direction, strength, confidence, contracts
   - **Recent signals table:** last 20 `algo_final_signals` rows
   - **Approach status:** which of the 25 are active, last signal time
2. Add `/algo` to main nav
3. Auto-refresh every 10 seconds

**Self-test:** Page loads at `localhost:5000/algo`. All panels render. Data from `algo_signals` and `algo_final_signals` tables displayed correctly.

---

### C-13 — A/B Harness Core
**Goal:** Walk-forward backtesting harness that compares two configs on historical verified trades. Manually triggerable from browser.

**Steps:**
1. Create `algo/ab_harness/harness.py`:
   - `run_test(param_path, control_value, variant_value, start_date, end_date) → ABResult`
   - Loads historical `algo_signals` for date range
   - Runs two signal stacks (control + variant config) on same historical data
   - Computes metrics per walk-forward window: Sharpe, profit_factor, win_rate, signal_count
   - Runs t-test on session returns: p_value
   - Computes robustness: fraction of windows where variant beats control
   - Accepts variant if p_value < threshold AND robustness > threshold AND improvement > min_pct
   - Writes result to `ab_results` table
2. Create `algo/ab_harness/metrics.py`: compute Sharpe, PF, win_rate, max_drawdown from list of trade P&Ls
3. All thresholds from `algo_config.yaml` → `ab_harness:` section

**Self-test:** `python algo/ab_harness/harness.py --self-test` exits 0. Runs synthetic A/B test with known outcome. Correct winner selected.

---

### C-14 — A/B Browser Integration
**Goal:** Trigger A/B tests from browser, view results.

**Steps:**
1. Add route `/ab-harness` to Flask visualizer:
   - Dropdown: select param to test (enumerated from algo_config.yaml)
   - Input: control value, variant value, date range
   - "Run Test" button → triggers harness
   - Results table: all past `ab_results` — param, control vs variant metrics, accepted/rejected
   - "Apply Winner" button: updates `algo_config.yaml` to winning value if accepted
2. Add `/ab-harness` to main nav

**Self-test:** Page loads. "Run Test" triggers harness. Results appear in table. "Apply Winner" updates config file.

---

### C-15 — CL-3, CL-4, CL-5 (Remaining CL Approaches)
**Goal:** Complete the CL category with delta flips, multi-session persistence, velocity rejection.

**Steps:**
1. `algo/approaches/cl/cl3.py` — Cumulative delta flip zones. Requires ticks_with_direction. Emits at delta peak/trough prices.
2. `algo/approaches/cl/cl4.py` — Multi-session persistence. Runs CL-1 on each of last N days independently. Emits only levels appearing in ≥ `persistence_threshold` fraction of days.
3. `algo/approaches/cl/cl5.py` — Velocity rejection. Measures tick-to-tick price velocity, finds reversal zero-crossings.
4. Register all 3 in category aggregator config with initial weights

**Self-test:** Each `--self-test` exits 0. Each emits at least 1 signal on test date. CL category aggregator now aggregates 5 approaches.

---

### C-16 — Remaining VP, RD, TOD Approaches (20 remaining)
**Goal:** Build all remaining 20 approaches. CL and combining layer are validated — now fill in the rest.

**Steps (sequential within each category):**

**VP (3 remaining):**
1. `vp3.py` — TPO shape (time-bucket count per price, shape classification)
2. `vp4.py` — Poor high/low (thin-volume session extremes)
3. `vp5.py` — Volume at extremes (distribution/accumulation)

**RD (3 remaining):**
4. `rd2.py` — Hurst exponent (DFA method on bar returns)
5. `rd3.py` — HMM (hmmlearn, 3-state, trained on historical bars)
6. `rd5.py` — Bollinger Band Width (squeeze detection)
7. Update `rd_composite.py` — now 5-approach weighted vote

**TOD (5 approaches — all new):**
8. Build historical bias table (offline) from all available trades history → save as `data/algo_processed/tod_bias_table.parquet`
9. `tod1.py` — Historical time-bucket bias lookup
10. `tod2.py` — Opening range (OR high/low, breakout/fade signal)
11. `tod3.py` — Open drive detection (first 20 min classification)
12. `tod4.py` — Lunch compression filter (dynamic vol-based suppression) — activates stub in gating layer
13. `tod5.py` — MOC window (statistical mode, browser-triggerable)

**Self-test:** Each `--self-test` exits 0. All 25 `algo_signals` rows appear in DB after a test session run.

---

### C-17 — Full System Integration Test `[BREAKPOINT BP-C2 after this]`
**Goal:** All 25 approaches running, combining layer producing final signals, browser showing everything, A/B harness triggerable.

**Steps:**
1. Run `algo_runner.py` for a full test session on historical data
2. Verify all 25 approaches emit signals
3. Verify category aggregator aggregates all 5 categories
4. Verify gating layer applies RD and VP multipliers
5. Verify final signal written to `algo_final_signals`
6. Verify algo_decider writes PENDING command to `commands` table
7. Run one A/B test from browser — confirm result appears in `ab_results`
8. Check `/algo` page shows all panels populated

**Performance check (Scope C):**
- All 25 approaches produce at least 1 signal on the test session
- Final signal count ≥ 2 per session average (over 5 test sessions)
- A/B test completes and writes result in < 60 seconds

**`[BREAKPOINT BP-C2]` — Scope C complete. Review `/algo` browser page together. Confirm all 25 approaches showing output before starting Scope D.**

---

## Scope D — Backtrader

**Goal:** A deterministic backtest engine that replays all Scope B verified trades through the algo signal system, computes P&L accurately, and is A/B testable from the browser.

**Note:** `back-trading/engine.py` has a simulation engine — Scope D extends it to use verified_trades as input instead of random generation.

---

### D-01 — Backtrader Engine
**Goal:** Load all verified trades from DB, replay each through the tick data, compute accurate P&L.

**Steps:**
1. Create `algo/backtrader/engine.py`:
   - Loads all `verified=1` rows from `verified_trades` table
   - For each trade: loads the tick data file for that session
   - Replays tick-by-tick: confirms fill at entry_price (find first tick at or through that price)
   - Tracks position: TP hit, SL hit, stagnation — matches exit_type from verified trade
   - Records: actual_entry_price, actual_exit_price, actual_pnl_points
   - Compares to recorded values: if delta > 0.25 points → flag as `accuracy_issue`
   - Writes results to `backtest_results` table (new DB table)
2. Add `backtest_results` table to `lib/db.py`:
   ```sql
   CREATE TABLE IF NOT EXISTS backtest_results (
     id                  INTEGER PRIMARY KEY AUTOINCREMENT,
     run_id              TEXT NOT NULL,        -- UUID per backtest run
     verified_trade_id   INTEGER NOT NULL,
     config_snapshot     TEXT NOT NULL,        -- JSON of algo_config at time of run
     actual_entry_price  REAL,
     actual_exit_price   REAL,
     actual_pnl_points   REAL,
     recorded_pnl_points REAL,
     pnl_delta           REAL,                -- actual - recorded
     accuracy_ok         INTEGER,             -- 1 if |delta| <= 0.25
     ran_at              TEXT NOT NULL
   )
   ```

**Self-test:** `python algo/backtrader/engine.py --self-test` exits 0. Runs on 5 synthetic verified trades. Correct P&L computed. Accuracy flags work.

---

### D-02 — Determinism Validation
**Goal:** Prove backtest results are deterministic to ± 0.1% across runs (performance requirement).

**Steps:**
1. Run the same backtest twice with identical config and input
2. Compare all `actual_pnl_points` values row-by-row
3. Assert max difference < 0.25 points (1 tick) on any single trade
4. Assert total P&L difference < 0.1%
5. Add `--validate-determinism` flag to engine: runs twice, compares, exits 0 if pass, 1 if fail
6. Log any non-deterministic trades as WARNING with cause

**Self-test:** `python algo/backtrader/engine.py --validate-determinism` exits 0 on all verified trades.

---

### D-03 — A/B Integration
**Goal:** Backtrader can run in A/B mode — same verified trades, two algo configs — compare results.

**Steps:**
1. Add `--ab-mode --control-config X --variant-config Y` flags to engine
2. Runs both configs on all verified trades
3. Computes Sharpe, PF, win_rate, max_drawdown for each
4. Runs significance test (t-test on trade P&Ls)
5. Writes to `ab_results` table with `source: backtrader`
6. Integrates with existing `/ab-harness` browser page (source filter: `backtrader` vs `live`)

**Self-test:** A/B mode runs with two synthetic configs. Results written. Winner correctly identified.

---

### D-04 — Backtrader Browser Page
**Goal:** Browser page showing backtest results, accuracy report, and A/B comparisons.

**Steps:**
1. Add route `/backtrader` to Flask visualizer:
   - **Run controls:** date range, config version, "Run Backtest" button
   - **Accuracy report:** total trades, accuracy_ok %, max P&L delta, accuracy issues list
   - **Performance summary:** Sharpe, PF, win_rate, max_drawdown for the run
   - **Trade table:** all backtest_results rows for selected run
   - **A/B comparisons:** table of all backtrader A/B results
2. Add `/backtrader` to main nav

**Self-test:** Page loads. "Run Backtest" triggers engine. Results display correctly.

---

### D-05 — Full End-to-End Validation `[BREAKPOINT BP-D1 after this]`
**Goal:** Prove the full pipeline works end-to-end: fetch → preprocess → signal → trace → backtest.

**Steps:**
1. Pick 5 trading days with verified trades in DB
2. Run: fetch (confirmed) → preprocess → algo signals → final signals → commands → trace → verified_trades
3. Run backtrader on those verified trades
4. Check:
   - All 25 approaches produced signals on each day
   - ≥ 80% verified trade rate
   - Backtest deterministic (run twice, ± 0.1%)
   - All results visible in browser on correct pages

**Performance check (Scope D):**
- Backtest deterministic to ± 0.1% ✓
- 100% of verified trades included in backtest run ✓
- Accuracy rate (|pnl_delta| ≤ 0.25) ≥ 95% of trades ✓

**`[BREAKPOINT BP-D1]` — Scope D complete. Full system operational. Review end-to-end with user before live trading consideration.**

---

## Task Summary

| ID | Task | Scope | Builds on |
|---|---|---|---|
| A-01 | Fetcher audit | A | existing fetcher.py |
| A-02 | Multi-symbol config | A | A-01 |
| A-03 `[BP-A1]` | Automatic daily fetch scheduler | A | A-02 |
| A-04 | Fetch status browser page | A | A-03 |
| A-05 `[BP-A2]` | Data validation report | A | A-04 |
| B-01 | verified_trades DB schema | B | lib/db.py |
| B-02 | Traceback engine | B | B-01 |
| B-03 `[BP-B1]` | Verification logic | B | B-02 |
| B-04 | Drop analysis | B | B-03 |
| B-05 | Traceback browser page | B | B-04 |
| B-06 `[BP-B2]` | Backtrader export | B | B-05 |
| C-00 | Algo sub-project scaffold | C | runner.py, lib/db.py |
| C-01 | Algo config system | C | C-00 |
| C-02 | Data preprocessor | C | Scope A output |
| C-03 | Signal bus | C | C-00 |
| C-04 | CL-1 | C | C-02, C-03 |
| C-05 `[BP-C1]` | CL-2 | C | C-04 |
| C-06 | VP-1 + VP-2 | C | C-02, C-03 |
| C-07 | RD-1 + RD-4 | C | C-02, C-03 |
| C-08 | Category aggregator | C | C-04–C-07 |
| C-09 | Gating layer | C | C-08 |
| C-10 | Final signal + position sizing | C | C-09 |
| C-11 | Algo signal → runner integration | C | C-10 |
| C-12 | Algo browser dashboard | C | C-11 |
| C-13 | A/B harness core | C | C-12 |
| C-14 | A/B browser integration | C | C-13 |
| C-15 | CL-3, CL-4, CL-5 | C | C-05 |
| C-16 | Remaining VP/RD/TOD (20 approaches) | C | C-06, C-07 |
| C-17 `[BP-C2]` | Full system integration test | C | C-16 |
| D-01 | Backtrader engine | D | Scope B verified_trades |
| D-02 | Determinism validation | D | D-01 |
| D-03 | A/B integration | D | D-02, C-13 |
| D-04 | Backtrader browser page | D | D-03 |
| D-05 `[BP-D1]` | Full end-to-end validation | D | D-04 |

**Total tasks: 34**  
**Breakpoints: 7**  
**Autonomous steps between breakpoints: 3–6 per segment**

---

*End of implementation plan v1.0 — approved 2026-04-27.*
