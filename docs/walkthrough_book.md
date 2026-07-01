# Galao System — Walkthrough Book
# "What Actually Happens" — Full Lifecycle Trace
Version: 0.2.0 | Date: 2026-04-07

---

## 0. Engine Contract (Read First)

These rules are invariants. Every line of code must respect them.

| # | Invariant |
|---|-----------|
| I-01 | One command = one parent bracket in IB. Never more. |
| I-02 | A command may have at most one live parent order in IB at any time. |
| I-03 | A closed command triggers exactly one replenishment. Never zero, never two. |
| I-04 | A child fill (TP or SL) maps to exactly one command. |
| I-05 | Replenishment is per closed command, not per line. |
| I-06 | Cool-down is side-specific (BUY side and SELL side tracked independently per line). |
| I-07 | Shutdown disables replenishment fully and immediately. No new commands after SHUTDOWN is written to DB — including for positions that close during shutdown. |
| I-08 | DB is the source of truth. IB is the execution venue. On conflict, DB wins unless explicitly flagged for reconciliation. |
| I-09 | Event callbacks are the primary status update path. Polling and reconnect sync are repair paths only. |
| I-10 | Only one Broker process runs at a time. No concurrent workers. |
| I-11 | Realized PnL is tracked in the `positions` table (strategy-centric). IB executions are for reconciliation only. |
| I-12 | The system tracks **virtual strategy legs**, not broker net positions. IB may show net zero while the system has 4 active brackets — this is intentional. |
| I-13 | Partial fills are ignored in V1. All fills assumed complete and atomic. |

---

## 0.1 Configuration Values Referenced in This Document

All values below are configurable in `config.yaml`. They are not hardcoded.

| Parameter | Default | Config key |
|-----------|---------|------------|
| Session open offset | 30 min after CME open | `session.open_offset_minutes` |
| Shutdown trigger | 60 min before CME close | `session.shutdown_offset_minutes` |
| Stagnation time | 60 seconds | `position.stagnation_seconds` |
| Stagnation min move | 0.5 points | `position.stagnation_min_move_points` |
| SL cool-down | 30 seconds | `position.sl_cooldown_seconds` |
| Panic threshold | 10 min before close | `shutdown.panic_threshold_minutes` |
| Orderly exit patience | 30 seconds | `shutdown.exit_patience_seconds` |
| IB backup poll interval | 30 seconds | `broker.ib_poll_seconds` |
| Broker command poll | 5 seconds | `broker.command_poll_seconds` |
| Decider replenishment poll | 10 seconds | `decider.replenishment_poll_seconds` |

---

## 0.2 Command Status State Machine

```
                    ┌─────────┐
                    │ PENDING │  ← Decider writes here
                    └────┬────┘
                         │ Broker reads, claims command
                         ▼
                   ┌───────────┐
                   │SUBMITTING │  ← Written BEFORE IB call (claim lock)
                   └─────┬─────┘
              ┌───────────┴──────────┐
         IB accepts              IB rejects
              │                      │
              ▼                      ▼
        ┌───────────┐           ┌─────────┐
        │ SUBMITTED │           │  ERROR  │
        └─────┬─────┘           └─────────┘
              │ Parent fills
              ▼
          ┌────────┐
          │ FILLED │  ← Entry triggered, children (TP+SL) now active
          └────┬───┘
               │ Exit event (TP / SL / stagnation / shutdown)
               ▼
          ┌─────────┐
          │ EXITING │  ← Written when market exit or shutdown exit starts
          └────┬────┘
               │ Exit fill confirmed
               ▼
          ┌────────┐
          │ CLOSED │  ← Terminal. Triggers replenishment check in Decider.
          └────────┘

Additional states:
  CANCELLED          ← Parent cancelled before fill (shutdown step 1, or manual)
  RECONCILE_REQUIRED ← Set on reconnect when DB and IB disagree
```

**Allowed transitions only:**
```
PENDING        → SUBMITTING
SUBMITTING     → SUBMITTED | ERROR
SUBMITTED      → FILLED | CANCELLED
FILLED         → EXITING
EXITING        → CLOSED
CLOSED         → (terminal — Decider may write a new replenishment command)
CANCELLED      → (terminal)
ERROR          → (terminal — requires manual review)
RECONCILE_REQUIRED → SUBMITTED | FILLED | CANCELLED | ERROR  (after reconciliation)
```

**No other transitions are valid.** Any status change that skips a state must be logged as an anomaly.

---

## 0.3 Failure & Reconciliation Scenarios

These are not edge cases. They will happen.

| Scenario | Detection | Resolution |
|----------|-----------|------------|
| IB accepted order but DB write failed | On next Broker poll: command still SUBMITTING, IB has live order | Find IB order by order_ref field, write IB order IDs to DB, set SUBMITTED |
| DB has SUBMITTED, IB has no such order | Reconnect sync: IB has no matching order_id | Set command to RECONCILE_REQUIRED, alert via ib_events |
| Reconnect finds filled parent but no exit record | `executions()` returns fill, DB still shows FILLED | Write exit record, set CLOSED, trigger replenishment |
| Duplicate fill callback arrives | Command already CLOSED | Ignore silently, log DEBUG |
| Stagnation exit fires while child orders still live | Broker sends market exit AND cancels both children explicitly | Children cancelled before market exit fills to avoid double-sell |
| Decider replenishes while Broker is finalizing close | Replenishment command written before CLOSED is set | Decider checks for `is_replenishment` flag — if a replenishment already exists for parent_command_id, skip. (I-03) |
| SUBMITTING command found on restart | Broker crashed between claim and IB submit | Check IB for matching order_ref: found → set SUBMITTED; not found → reset to PENDING |

---

## Scenario Setup

```
Symbol:          MES (active contract: MESM6 = June 2026)
Date:            2026-04-07
Critical line:   6250, strength=2
Active brackets: [2, 4] points
Current price at session open (09:00 CT): 6280
Price is ABOVE the line → toggle: LMT BUY + STP SELL
```

---

## Phase 1 — Day Start (09:00 CT)

### 1.1 Decider wakes up

```
09:00:00  Decider reads: data/critical_lines/levels_daily_20260407.txt
          Finds: MES, 6250, 2
          Fetches current price from DB (Broker feeds LIVE price): 6280.00
          6280 > 6250 → toggle: LMT BUY + STP SELL
          Rounds 6250 to nearest 0.25 → 6250.00 (already valid)
```

### 1.2 Decider writes 4 commands to DB

```sql
-- Bracket 2, BUY
INSERT INTO commands (symbol, direction, entry_price, bracket_size,
                      take_profit, stop_loss, line_strength,
                      entry_order_type, status, created_at)
VALUES ('MES', 'BUY', 6250.00, 2.0, 6252.00, 6248.00, 2, 'LMT', 'PENDING', '09:00:00');
-- id=1

-- Bracket 4, BUY
INSERT INTO commands (...) VALUES ('MES','BUY',6250.00,4.0,6254.00,6246.00,2,'LMT','PENDING','09:00:00');
-- id=2

-- Bracket 2, SELL
INSERT INTO commands (...) VALUES ('MES','SELL',6250.00,2.0,6248.00,6252.00,2,'STP','PENDING','09:00:00');
-- id=3

-- Bracket 4, SELL
INSERT INTO commands (...) VALUES ('MES','SELL',6250.00,4.0,6246.00,6254.00,2,'STP','PENDING','09:00:00');
-- id=4
```

### 1.3 DB state after Decider writes

| id | dir | type | entry | TP     | SL     | bracket | strength | status  |
|----|-----|------|-------|--------|--------|---------|----------|---------|
| 1  | BUY | LMT  | 6250  | 6252   | 6248   | 2       | 2        | PENDING |
| 2  | BUY | LMT  | 6250  | 6254   | 6246   | 4       | 2        | PENDING |
| 3  | SELL| STP  | 6250  | 6248   | 6252   | 2       | 2        | PENDING |
| 4  | SELL| STP  | 6250  | 6246   | 6254   | 4       | 2        | PENDING |

---

## Phase 2 — Broker Submits Orders to IB (09:00:01)

### 2.1 Broker polls DB — finds 4 PENDING commands

For each command, Broker builds and submits an OCO bracket to IB PAPER port 4002.

### 2.2 Bracket construction for command id=1 (LMT BUY, bracket=2)

```python
contract = Future('MES', 'MESM6', 'CME', 'USD')

# Entry: LMT BUY at 6250
parent         = LimitOrder('BUY', 1, 6250.00)
parent.orderId = 1001          # assigned by IB
parent.transmit = False

# Take Profit: LMT SELL at 6252
take_profit         = LimitOrder('SELL', 1, 6252.00)
take_profit.orderId = 1002
take_profit.parentId = 1001
take_profit.transmit = False

# Stop Loss: STP SELL at 6248
stop_loss         = StopOrder('SELL', 1, 6248.00)
stop_loss.orderId = 1003
stop_loss.parentId = 1001
stop_loss.transmit = True    # ← transmits all 3 to IB

ib_paper.placeOrder(contract, parent)
ib_paper.placeOrder(contract, take_profit)
ib_paper.placeOrder(contract, stop_loss)
```

### 2.3 IB accepts the bracket

IB returns `orderStatus` callbacks:
```
orderId=1001  status=PreSubmitted  (parent waiting for fill)
orderId=1002  status=PreSubmitted  (TP waiting — inactive until parent fills)
orderId=1003  status=PreSubmitted  (SL waiting — inactive until parent fills)
```

### 2.4 Broker updates DB for command id=1

```sql
UPDATE commands SET
  status = 'SUBMITTED',
  ib_parent_order_id = 1001,
  ib_tp_order_id     = 1002,
  ib_sl_order_id     = 1003
WHERE id = 1;
```

### 2.5 Same process repeats for commands 2, 3, 4

| cmd id | IB parent | IB TP | IB SL | status    |
|--------|-----------|-------|-------|-----------|
| 1      | 1001      | 1002  | 1003  | SUBMITTED |
| 2      | 1004      | 1005  | 1006  | SUBMITTED |
| 3      | 1007      | 1008  | 1009  | SUBMITTED |
| 4      | 1010      | 1011  | 1012  | SUBMITTED |

### 2.6 IB state at this point (4 open brackets)

```
IB PAPER account — open orders:
  1001  LMT BUY  MES 6250.00  qty=1  status=PreSubmitted
  1002  LMT SELL MES 6252.00  qty=1  status=PreSubmitted (child of 1001)
  1003  STP SELL MES 6248.00  qty=1  status=PreSubmitted (child of 1001)
  1004  LMT BUY  MES 6250.00  qty=1  status=PreSubmitted
  1005  LMT SELL MES 6254.00  qty=1  status=PreSubmitted (child of 1004)
  1006  STP SELL MES 6246.00  qty=1  status=PreSubmitted (child of 1004)
  1007  STP SELL MES 6250.00  qty=1  status=PreSubmitted
  1008  LMT BUY  MES 6248.00  qty=1  status=PreSubmitted (child of 1007)
  1009  STP BUY  MES 6252.00  qty=1  status=PreSubmitted (child of 1007)
  1010  STP SELL MES 6250.00  qty=1  status=PreSubmitted
  1011  LMT BUY  MES 6246.00  qty=1  status=PreSubmitted (child of 1010)
  1012  STP BUY  MES 6254.00  qty=1  status=PreSubmitted (child of 1010)
```

---

## Phase 3 — Price Drops to 6250 (10:23 CT)

### 3.1 What happens in IB

Price hits 6250.00. IB evaluates all open orders:

```
LMT BUY  1001 @ 6250 → FILLS  (price dropped TO 6250, limit buy satisfied)
LMT BUY  1004 @ 6250 → FILLS
STP SELL 1007 @ 6250 → FILLS  (price dropped TO 6250, stop sell triggered)
STP SELL 1010 @ 6250 → FILLS
```

All 4 parent orders fill simultaneously.

**Important — net position:**
IB nets positions in the same symbol.
- 2 × LONG (from 1001, 1004) + 2 × SHORT (from 1007, 1010) = **net 0**
- IB holds net position = 0, but all 8 child orders (TP+SL for each bracket) are now ACTIVE

**Note:** Despite the net-zero position, the 4 OCO bracket pairs are independently active and will each resolve to a TP or SL exit. This is intentional — user acknowledged "I don't mind if they both catch."

### 3.2 IB fires callbacks to Broker

```
orderStatus: orderId=1001, status=Filled, avgFillPrice=6250.00, time=10:23:14
orderStatus: orderId=1004, status=Filled, avgFillPrice=6250.00, time=10:23:14
orderStatus: orderId=1007, status=Filled, avgFillPrice=6250.00, time=10:23:14
orderStatus: orderId=1010, status=Filled, avgFillPrice=6250.00, time=10:23:14

orderStatus: orderId=1002, status=Submitted  (TP now ACTIVE)
orderStatus: orderId=1003, status=Submitted  (SL now ACTIVE)
orderStatus: orderId=1005, status=Submitted
orderStatus: orderId=1006, status=Submitted
orderStatus: orderId=1008, status=Submitted
orderStatus: orderId=1009, status=Submitted
orderStatus: orderId=1011, status=Submitted
orderStatus: orderId=1012, status=Submitted
```

### 3.3 Broker updates DB on fills

```sql
UPDATE commands SET
  status     = 'FILLED',
  fill_price = 6250.00,
  filled_at  = '10:23:14'
WHERE id IN (1, 2, 3, 4);
```

### 3.4 DB state after fills

| id | dir  | type | entry | TP   | SL   | bracket | status | fill_price | filled_at |
|----|------|------|-------|------|------|---------|--------|------------|-----------|
| 1  | BUY  | LMT  | 6250  | 6252 | 6248 | 2       | FILLED | 6250.00    | 10:23:14  |
| 2  | BUY  | LMT  | 6250  | 6254 | 6246 | 4       | FILLED | 6250.00    | 10:23:14  |
| 3  | SELL | STP  | 6250  | 6248 | 6252 | 2       | FILLED | 6250.00    | 10:23:14  |
| 4  | SELL | STP  | 6250  | 6246 | 6254 | 4       | FILLED | 6250.00    | 10:23:14  |

### 3.5 Broker logs to ib_events

```sql
INSERT INTO ib_events (timestamp, event_type, ib_order_id, data)
VALUES ('10:23:14', 'fill', 1001, '{"price":6250.00,"qty":1,"side":"BUY"}');
-- repeated for 1004, 1007, 1010
```

---

## Phase 4 — Stagnation Monitor (10:23 to 10:24)

### 4.1 Broker starts stagnation timer for each filled position

```
Position 1 (cmd=1): entry=6250, time=10:23:14, threshold=60s, min_move=0.5pt
Position 2 (cmd=2): entry=6250, time=10:23:14
Position 3 (cmd=3): entry=6250, time=10:23:14
Position 4 (cmd=4): entry=6250, time=10:23:14
```

Every few seconds, Broker checks:
```
current_price = 6251.25  (from LIVE port)
time_in_trade = 28s
abs(6251.25 - 6250.00) = 1.25 > 0.5 → stagnation NOT triggered
```

---

## Phase 5 — Exit Scenarios

*Four independent exits — one per bracket. Shown separately below.*

---

### Scenario A — TP Hit (cmd=1, bracket=2, BUY)

**10:24:10** — Price rises to 6252.00

```
IB: LMT SELL 1002 @ 6252 FILLS
IB: STP SELL 1003 @ 6248 CANCELLED automatically (OCO partner filled)

Callback to Broker:
  orderStatus: orderId=1002, status=Filled, avgFillPrice=6252.00
  orderStatus: orderId=1003, status=Cancelled
```

Broker updates DB:
```sql
UPDATE commands SET
  exit_price  = 6252.00,
  exit_at     = '10:24:10',
  exit_reason = 'TP',
  status      = 'CLOSED'
WHERE id = 1;

INSERT INTO positions (command_id, symbol, direction, entry_price, entry_time,
                       exit_price, exit_time, pnl_points, exit_reason)
VALUES (1, 'MES', 'BUY', 6250.00, '10:23:14', 6252.00, '10:24:10', 2.0, 'TP');
```

**Result: +2.0 points**

---

### Scenario B — SL Hit (cmd=3, bracket=2, SELL)

**10:24:22** — Price rises to 6252.00 (bad for the SHORT position)

```
IB: STP BUY 1009 @ 6252 FILLS  (stop loss for the SELL position)
IB: LMT BUY 1008 @ 6248 CANCELLED automatically

Callback to Broker:
  orderStatus: orderId=1009, status=Filled, avgFillPrice=6252.00
  orderStatus: orderId=1008, status=Cancelled
```

Broker updates DB:
```sql
UPDATE commands SET
  exit_price  = 6252.00,
  exit_at     = '10:24:22',
  exit_reason = 'SL',
  status      = 'CLOSED'
WHERE id = 3;

INSERT INTO positions (...) VALUES (3,'MES','SELL',6250.00,'10:23:14',6252.00,'10:24:22',-2.0,'SL');
```

**Result: -2.0 points**

### Cool-down triggered for this line:

```
Broker writes to DB:
  INSERT INTO system_state (key, value, updated_at)
  VALUES ('cooldown_MES_6250_SELL', '10:24:52', '10:24:22');
  -- cooldown expires at 10:24:22 + 30s = 10:24:52
```

Decider detects SL exit, checks system_state — cooldown active.
Replenishment for SELL side of line 6250 is **held** until 10:24:52.

---

### Scenario C — Stagnation Exit (cmd=2, bracket=4, BUY)

**10:24:35** — 61 seconds in trade. Price is 6250.25.

```
Broker stagnation check:
  time_in_trade = 61s > 60s threshold
  abs(6250.25 - 6250.00) = 0.25 < 0.5 threshold
  → STAGNATION triggered
```

Broker sends market exit:
```python
market_exit = MarketOrder('SELL', 1)
ib_paper.placeOrder(contract, market_exit)

# Also cancel the still-pending TP and SL children:
ib_paper.cancelOrder(1005)  # TP
ib_paper.cancelOrder(1006)  # SL
```

Broker updates DB:
```sql
UPDATE commands SET
  exit_price  = 6250.25,   -- market fill price
  exit_at     = '10:24:35',
  exit_reason = 'STAGNATION',
  status      = 'CLOSED'
WHERE id = 2;

INSERT INTO positions (...) VALUES (2,'MES','BUY',6250.00,'10:23:14',6250.25,'10:24:35',0.25,'STAGNATION');
```

**Result: +0.25 points (slippage from market exit)**

---

## Phase 6 — Replenishment (Decider, ~10:24)

### 6.1 Decider polls DB — detects closed commands

```
10:24:15  Decider poll:
  SELECT * FROM commands WHERE status='CLOSED' AND replenished=0;
  → finds cmd id=1 (TP exit), id=2 (STAGNATION exit)
  → cmd id=3 (SL exit): checks system_state cooldown → cooldown active, skip for now
  → cmd id=4: still open (bracket=4 SELL, not yet exited)
```

### 6.2 Replenishment for cmd=1 (BUY, TP exit)

Decider fetches current price: 6252.50 (now ABOVE 6250 again)
Toggle re-evaluation: 6252.50 > 6250 → LMT BUY

```sql
INSERT INTO commands (symbol, direction, entry_price, bracket_size,
                      take_profit, stop_loss, line_strength,
                      entry_order_type, status, is_replenishment,
                      parent_command_id, created_at)
VALUES ('MES','BUY',6250.00,2.0,6252.00,6248.00,2,'LMT','PENDING',1,1,'10:24:15');
-- id=5
```

### 6.3 Replenishment for cmd=2 (BUY, STAGNATION exit)

Same toggle evaluation → same order type:

```sql
INSERT INTO commands (...) VALUES ('MES','BUY',6250.00,4.0,6254.00,6246.00,2,'LMT','PENDING',1,2,'10:24:15');
-- id=6
```

### 6.4 Cool-down expires at 10:24:52 — cmd=3 (SELL) replenished

```
10:24:52  Decider poll:
  cooldown_MES_6250_SELL expired
  current price = 6251.00 > 6250 → STP SELL

INSERT INTO commands (...) VALUES ('MES','SELL',6250.00,2.0,6248.00,6252.00,2,'STP','PENDING',1,3,'10:24:52');
-- id=7
```

### 6.5 Broker picks up new PENDING commands (ids 5, 6, 7)

Submits fresh brackets to IB PAPER port. Cycle repeats.

---

## Phase 7 — IB Status Query Pattern

### How Broker tracks open orders throughout the day

**Method 1 — Event-driven (primary):**
IB pushes callbacks on every status change. Broker handles:
```python
@ib.orderStatusEvent
def on_order_status(trade):
    update_db(trade.order.orderId, trade.orderStatus.status, trade.orderStatus.avgFillPrice)
```

**Method 2 — Periodic poll (backup, every 30s):**
```python
open_trades = ib_paper.openTrades()
for trade in open_trades:
    sync_status_to_db(trade)
```

This catches any missed callbacks (e.g. after reconnect).

**Method 3 — On reconnect (mandatory):**
```python
def on_reconnect():
    trades    = ib_paper.openTrades()
    positions = ib_paper.positions()
    executions = ib_paper.executions()
    sync_all_to_db(trades, positions, executions)
```

---

## Phase 8 — End of Day Shutdown (14:15 CT)

### 8.1 Decider writes shutdown signal

```sql
UPDATE system_state SET value='SHUTDOWN', updated_at='14:15:00'
WHERE key='session_state';
```

### 8.2 Broker detects SHUTDOWN

Assume at 14:15:00 there is still one open position (cmd=4, bracket=4 SELL, entered at 6250, currently at 6249).

**Step 1 — Cancel all pending (unfilled) parent orders:**
```python
for cmd in get_pending_commands():          # any SUBMITTED but not yet FILLED
    ib_paper.cancelOrder(cmd.ib_parent_order_id)
```

**Step 2 — Tighten stops on open positions to 1 point:**
```python
# cmd=4 is FILLED, position is open
# Current SL is at 6254. Modify to current_price + 1.0 = 6250.00
modify_stop(ib_order_id=1012, new_stop=6250.00)
```

**Step 3 — Orderly exit (one by one, patience=30s):**
```python
market_exit = MarketOrder('BUY', 1)   # close the SELL position
ib_paper.placeOrder(contract, market_exit)
# cancel remaining bracket children
ib_paper.cancelOrder(1011)  # TP
ib_paper.cancelOrder(1012)  # SL (now tightened)
time.sleep(30)              # wait exit_patience_seconds
```

Broker updates DB:
```sql
UPDATE commands SET exit_price=6249.50, exit_at='14:15:08',
                    exit_reason='SHUTDOWN_ORDERLY', status='CLOSED'
WHERE id=4;
```

**Step 4 — Panic mode** (if any positions remain within 10 minutes of close):
All remaining positions exited simultaneously with market orders. Logged as `SHUTDOWN_PANIC`.

---

## Phase 9 — End of Day DB Summary for Line 6250

| id | dir  | bracket | entry  | exit   | pnl    | exit_reason       | strength |
|----|------|---------|--------|--------|--------|-------------------|----------|
| 1  | BUY  | 2       | 6250   | 6252   | +2.0   | TP                | 2        |
| 2  | BUY  | 4       | 6250   | 6250.25| +0.25  | STAGNATION        | 2        |
| 3  | SELL | 2       | 6250   | 6252   | -2.0   | SL                | 2        |
| 4  | SELL | 4       | 6250   | 6249.50| +0.50  | SHUTDOWN_ORDERLY  | 2        |
| 5  | BUY  | 2       | 6250   | ...    | ...    | (replenishment)   | 2        |
| 6  | BUY  | 4       | 6250   | ...    | ...    | (replenishment)   | 2        |
| 7  | SELL | 2       | 6250   | ...    | ...    | (replenishment)   | 2        |

**Net for this line, first cycle: +0.75 points across 4 positions**

---

## Summary — Full Flow Diagram

```
DECIDER                        DB                         BROKER                    IB (PAPER)
  │                             │                             │                          │
  │── read critical lines ──────┤                             │                          │
  │── write 4 PENDING cmds ────►│                             │                          │
  │                             │◄── poll PENDING ────────────│                          │
  │                             │─── return 4 cmds ──────────►│                          │
  │                             │                             │── placeOrder x4 ────────►│
  │                             │                             │◄── orderId callbacks ────│
  │                             │◄── write SUBMITTED ─────────│                          │
  │                             │                             │                          │
  │                             │                    [price hits 6250]                   │
  │                             │                             │◄── fill callbacks ───────│
  │                             │◄── write FILLED ────────────│                          │
  │◄── poll FILLED ─────────────│                             │                          │
  │                             │                             │  [stagnation / TP / SL]  │
  │                             │◄── write exit + positions ──│                          │
  │◄── detect closed ───────────│                             │                          │
  │── write replenishment ─────►│                             │                          │
  │                             │◄── poll PENDING ────────────│                          │
  │                             │─── return new cmds ────────►│                          │
  │                             │                             │── placeOrder ───────────►│
  │                             │                             │                          │
  │  [T-60min]                  │                             │                          │
  │── write SHUTDOWN ──────────►│                             │                          │
  │                             │◄── detect SHUTDOWN ─────────│                          │
  │                             │                             │── cancelOrder ───────────►│
  │                             │                             │── modify stops ──────────►│
  │                             │                             │── market exit ───────────►│
  │                             │◄── write CLOSED ────────────│                          │
```
