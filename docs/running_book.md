# Galao System — Running Book
# Operational command reference, sorted by session order
Version: 0.4.0 | Date: 2026-05-05

---

## 0. Prerequisites (once per machine)

```bash
pip install ib_insync flask pyyaml

mkdir -p logs data/critical_lines data/history
```

IB Gateway must be running:
- LIVE  → port 4001
- PAPER → port 4002
- API connections enabled (IB Gateway → Configure → API → Enable)

---

## 1. New Machine Setup

```bash
git clone https://github.com/orengavish/galgo2006.git galgo2026
cd galgo2026
pip install ib_insync flask pyyaml
mkdir -p logs data/critical_lines data/history
```

---

## 2. Start of Day — Normal Session

### Step 1 — Pull latest code
```bash
git pull
```

### Step 2 — Start background data fetcher (keep running all month)
```bash
python trader/may_scheduler.py --no-paper
# or with backfill from a date:
python trader/may_scheduler.py --no-paper --backfill-from 2026-04-14
# or with paper sessions too (17:00 IL + fetch 17:30 CT):
python trader/may_scheduler.py
```
> **Note:** Backfill blocks the scheduler loop until complete — paper sessions
> will not fire during a long backfill. Run `--no-paper` for pure data collection.

### Step 3 — Start visualizer
```bash
python trader/visualizer/app.py
```
Open http://127.0.0.1:5001

### Step 4 — Enter critical lines (GUI)
Open http://127.0.0.1:5001/lines

Paste your lines in this format:
```
קווי תמיכה: 6765.25?, 6672.50? - 6652.75!, 6598.75!
קווי התנגדות: 6845.75?, 6903.75! - 6912.50, 6953.25?
```
Click **Parse** → verify preview → click **Save to DB**

Strength: `!` = strong · `?` = medium · no suffix = weak

### Step 5 — Run full trading session
```bash
cd trader && python runner.py
```
Starts broker + decider + position_manager. Broker immediately picks up any PENDING commands.

---

## 3. Start of Day — Dry-Run (no IB orders sent)

Use this to test lines, GUI, and logic without touching the paper account.

```bash
python runner.py --dry-run
```

Broker will log `[DRY-RUN] Would submit...` instead of placing orders.
All other components (decider, position_manager, visualizer) run normally.

---

## 4. Start of Day — Skip Preflight

Use when IB Gateway is not yet connected but you want the GUI running.

```bash
python runner.py --no-preflight
```

---

## 5. GUI Pages

Visualizer runs on **port 5001**.

| URL | Purpose |
|-----|---------|
| http://127.0.0.1:5001 | Dashboard — price ladder, active commands, stats, **✕ Flatten All** |
| http://127.0.0.1:5001/lines | **Enter critical lines, reset DB+IB** |
| http://127.0.0.1:5001/active | Active commands (PENDING/SUBMITTED/FILLED) |
| http://127.0.0.1:5001/positions | Open positions with P&L |
| http://127.0.0.1:5001/orders | All commands (full history) |
| http://127.0.0.1:5001/ib-trace | IB events log + IB Gateway raw log |
| http://127.0.0.1:5001/logs | Component log files |
| http://127.0.0.1:5001/preflight | Last preflight results |
| http://127.0.0.1:5001/fetch-status | Fetch progress browser page |

---

## 6. Fetch Monitoring

```bash
python trader/fetcher_status.py              # snapshot: pivot table (dates × symbols)
python trader/fetcher_status.py --watch      # live refresh every 5s (Ctrl+C to stop)
python trader/fetcher_status.py --watch 10   # refresh every 10s
python trader/fetcher_status.py --days 20    # show last 20 trading days
python trader/validate_fetch.py              # validate CSV files vs fetch_log
```

Cell values: `RUN` = actively fetching · `DONE` = finished (not yet in fetch_log) · `OK` = logged · `ERR` = error · `MISS` = not fetched

> `fetch_log` is only written after **both** TRADES and BID_ASK complete for a day.
> Use `DONE` cells to track in-progress days.

---

## 6b. Verified Trade Count

```bash
python trader/trade_count.py                 # all symbols, last 60 days
python trader/trade_count.py --symbol MES
python trader/trade_count.py --days 0        # all time
```

Shows: date · symbol · N trades · TP count · SL count · net PnL points

---

## 7. Reset — Cancel IB Orders + Wipe DB

### Via GUI (recommended)
http://127.0.0.1:5001/lines → **Reset** card at the bottom.
Check "Cancel IB orders" and/or "Wipe DB tables" → click **Reset** → confirm.

### Via CLI
```bash
python runner.py --reset
```
Prompts for confirmation, then sends reqGlobalCancel and wipes DB tables.

Wiped tables: `commands`, `positions`, `ib_events`, `system_state`
Preserved: `critical_lines`, `release_notes`

---

## 7. Self-Tests (no IB required for most)

Run all component self-tests to verify a new machine is working:

```bash
python runner.py --self-test
python preflight.py --self-test
python decider.py --self-test
python broker.py --self-test
python tracer.py --self-test
python -m lib.db --self-test
python -m lib.critical_lines --self-test
python visualizer/app.py --self-test
```

Or run them all at once:
```bash
for script in runner.py preflight.py decider.py broker.py tracer.py; do
  python $script --self-test
done
python -m lib.db --self-test
python -m lib.critical_lines --self-test
python visualizer/app.py --self-test
```

---

## 8. Run Components Individually (debugging)

```bash
python visualizer/app.py          # dashboard only (no trading)
python visualizer/app.py --no-price-feed   # dashboard without IB price feed

python broker.py                  # broker only (needs IB)
python broker.py --dry-run        # broker without IB

python decider.py                 # decider only
python preflight.py               # run preflight checks and print results
python tracer.py                  # trade tracer GUI (separate window)
```

---

## 9. IB Gateway Log (in IB Trace page)

Set this in `config.yaml` to see the raw IB Gateway log in the GUI:

```yaml
ib:
  gateway_log_dir: "C:\\Jts\\<your_username>"
```

Then open http://127.0.0.1:5001/ib-trace — right panel shows the live log.

---

## 10. End of Day

The system shuts down automatically 60 minutes before CME close (configurable: `session.shutdown_offset_minutes`).

Manual shutdown: press **Ctrl+C** in the runner terminal.

After stopping:
```bash
git add -A
git commit -m "session YYYY-MM-DD notes"
git push
```

---

## 11. Config Tuning (config.yaml)

| Key | Default | What it controls |
|-----|---------|-----------------|
| `orders.active_brackets` | `[2, 4]` | Bracket sizes in points |
| `orders.quantity` | `1` | Contracts per order |
| `orders.tick_size` | `0.25` | MES tick (do not change) |
| `session.open_offset_minutes` | `30` | Minutes after CME open to start |
| `session.shutdown_offset_minutes` | `60` | Minutes before CME close to stop |
| `position.stagnation_seconds` | `60` | Stagnation timer |
| `position.stagnation_min_move_points` | `0.5` | Min move to avoid stagnation exit |
| `position.sl_cooldown_seconds` | `30` | Cooldown after SL hit |
| `broker.command_poll_seconds` | `5` | How often broker polls for new commands |
| `broker.ib_poll_seconds` | `30` | Backup fill poll interval |
| `ib.gateway_log_dir` | `""` | Path to IB Gateway log directory |

---

## 12. Daily Git Workflow

```bash
git pull                   # before starting each session
# ... make changes or run session ...
git add -A
git commit -m "brief note"
git push
```

---

## 13. Back-Trading Engine

Located in `back-trading/`. Run from the `back-trading/` directory.

### Self-test (no IB required)
```bash
cd back-trading
python engine.py --self-test
```

### Fetch tick data for a date
```bash
python engine.py --fetch --date 2026-04-09
```
Downloads TRADES + BID_ASK ticks for the day into `data/bars/`.
Files are cached — re-running skips already-fetched dates.

### Historical simulation (offline)
```bash
python engine.py --date 2026-04-09
python engine.py --from 2026-04-01 --to 2026-04-09
```
Generates synthetic orders, simulates fills tick-by-tick, prints P&L timeline.

### Reality model (run on market day, requires IB paper)
```bash
python engine.py --reality-model
```
Submits generated orders to IB paper in real time throughout the day.
At 15:00 CT, compares paper fills to simulated fills → prints grade.

### Back-trading config (back-trading/config.yaml)
| Key | Default | What it controls |
|-----|---------|-----------------|
| `generator.n_timestamps` | `20` | Random order placements per session |
| `generator.entry_offset_min` | `0.25` | Min distance from market price (pts) |
| `generator.entry_offset_max` | `1.50` | Max distance from market price (pts) |
| `generator.bracket_sizes` | `[2, 16]` | TP/SL distances tested (pts) |
| `grader.fill_match_ticks` | `1` | Ticks within which sim=paper counts as match |
| `grader.target_grade_pct` | `80` | Target accuracy % for the simulation model |

---

## 14. Troubleshooting

| Symptom | Check |
|---------|-------|
| Preflight FAIL: LIVE/PAPER | Is IB Gateway running? Are ports 4001/4002 open? |
| Decider generates 0 commands | Were lines entered for today via /lines page? |
| IB Trace empty | broker.py must be running; events are only captured while broker is live |
| Commands stuck at SUBMITTED | Check IB Trace for errors; check IB Gateway paper account |
| Dashboard shows no price | Price feed needs LIVE connection; check IB Gateway |
| `All client IDs exhausted` | Another process is using all client IDs; kill stale processes |
