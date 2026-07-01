# Galao System — Tech Solutions Book
Version: 0.3.0 | Date: 2026-04-07

---

## 1. IB Gateway — Ports

| Connection | Purpose | Port | Notes |
|------------|---------|------|-------|
| LIVE | Market data, price queries, historical data | **4001** | IB Gateway LIVE |
| PAPER | Order submission, position management | **4002** | IB Gateway PAPER |

**Notes:**
- Galao uses **IB Gateway** (not TWS) — lighter, no UI, preferred for automated systems
- TWS ports differ (7496/7497) — do not use TWS ports
- Port numbers are configurable in `config.yaml`
- Both connections must be authorized in IB Gateway settings

**IB Gateway API settings to enable:**
- Enable ActiveX and Socket Clients: ON
- Socket port: set to match config
- Allow connections from localhost only: ON (recommended)
- Read-Only API: OFF (need order submission on PAPER)

---

## 2. IB Client IDs

**Problem:** IB requires each simultaneous API connection to have a unique Client ID. Using the same ID causes the new connection to disconnect the old one.

**Solution:** Use a pool of client IDs per connection type. Try each in order until one connects.

- LIVE pool: `[101, 102, 103]`
- PAPER pool: `[201, 301, 401]`
- Pools defined in `config.yaml` under `ib.live_client_ids` and `ib.paper_client_ids`

**Opening a connection (ib_insync):**
```python
from ib_insync import IB

def connect_ib(host, port, client_ids, timeout=5):
    ib = IB()
    for cid in client_ids:
        try:
            ib.connect(host, port, clientId=cid, timeout=timeout)
            if ib.isConnected():
                return ib
        except Exception:
            continue
    raise RuntimeError(f"Could not connect on port {port}")

ib_live  = connect_ib('127.0.0.1', 4001, [101, 102, 103])
ib_paper = connect_ib('127.0.0.1', 4002, [201, 301, 401])
```

**Closing a connection properly:**
```python
ib_live.disconnect()
ib_paper.disconnect()
```

**Important:** Always call `disconnect()` on exit. Use a `try/finally` block or `atexit` handler:
```python
import atexit
atexit.register(ib_live.disconnect)
atexit.register(ib_paper.disconnect)
```

**Stale Client ID problem:** If a previous process crashed without disconnecting, IB may hold the client ID for ~30 seconds. Solution: wait and retry, or use a different client ID per session (increment and store in DB).

---

## 3. OCO Bracket Orders in ib_insync

**Strategy:** Bounce at critical line — entry is a **Limit order** (fills when price reaches the line).

**Structure:** 3 linked orders — LMT entry, LMT take-profit, STP stop-loss.

```python
from ib_insync import IB, Future, LimitOrder, StopOrder

def build_bracket_order(ib, contract, direction, entry_price, bracket_size, qty=1):
    """
    direction: 'BUY' or 'SELL'
    BUY:  entry=LMT BUY at price, TP=LMT SELL at price+bracket, SL=STP SELL at price-bracket
    SELL: entry=LMT SELL at price, TP=LMT BUY at price-bracket, SL=STP BUY at price+bracket
    Returns list of [parent, take_profit, stop_loss]
    """
    opposite = 'SELL' if direction == 'BUY' else 'BUY'
    tp = entry_price + bracket_size if direction == 'BUY' else entry_price - bracket_size
    sl = entry_price - bracket_size if direction == 'BUY' else entry_price + bracket_size

    parent = LimitOrder(direction, qty, entry_price)   # LMT entry (bounce)
    parent.orderId = ib.client.getReqId()
    parent.transmit = False

    take_profit = LimitOrder(opposite, qty, tp)        # LMT take profit
    take_profit.orderId = ib.client.getReqId()
    take_profit.parentId = parent.orderId
    take_profit.transmit = False

    stop_loss = StopOrder(opposite, qty, sl)           # STP stop loss
    stop_loss.orderId = ib.client.getReqId()
    stop_loss.parentId = parent.orderId
    stop_loss.transmit = True   # last order transmits whole bracket

    return [parent, take_profit, stop_loss]

# Submit to PAPER port only:
orders = build_bracket_order(ib_paper, contract, 'BUY', 6250.0, 2.0)
trades = [ib_paper.placeOrder(contract, o) for o in orders]
```

**Note:** Entry is `LimitOrder` (not `StopOrder`). Galao uses bounce strategy — LMT fills when price arrives at the critical line. V1 used STP (breakout strategy) — do not copy that pattern.

---

## 4. Futures Contract Resolution (ib_insync)

MES and other micro futures roll quarterly. The active contract month must be resolved at startup.

```python
from ib_insync import Future

def get_active_contract(ib, symbol, exchange='CME', currency='USD'):
    contract = Future(symbol=symbol, exchange=exchange, currency=currency)
    details = ib.reqContractDetails(contract)
    # Sort by expiry, pick nearest future expiry
    active = sorted(details, key=lambda d: d.contract.lastTradeDateOrContractMonth)[0]
    return active.contract
```

Store the resolved contract in DB `system_state` table at session start to avoid repeated lookups.

---

## 5. SQLite Concurrent Access

**Problem:** Decider and Broker both read/write SQLite simultaneously.

**Solution:** SQLite supports WAL (Write-Ahead Logging) mode for better concurrent read/write:

```python
import sqlite3

conn = sqlite3.connect('data/galao.db', timeout=10)
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('PRAGMA busy_timeout=5000')   # wait up to 5s on lock
```

**Rule:** Each process opens its own connection. Never share a connection across threads/processes.

**Connection pattern (lib/db.py):**
```python
def get_conn():
    conn = sqlite3.connect(config.paths.db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn
```

---

## 6. CME Trading Hours (Chicago Time = CT = UTC-5/UTC-6)

| Session | CME Globex Open | CME Close | Galao Open (open+30m) | Galao Shutdown (close-60m) |
|---------|----------------|-----------|----------------------|---------------------------|
| MES (S&P futures) | Sunday 17:00 CT | Friday 16:00 CT | varies | varies |
| Regular hours | 08:30 CT | 15:15 CT | 09:00 CT | 14:15 CT |

**Note:** System uses CME regular session hours for trading decisions (08:30–15:15 CT). Extended hours (Globex overnight) are not traded.

**Time handling:** All times stored in DB as UTC. Display can convert to CT. Use `pytz` or `zoneinfo`:

```python
from zoneinfo import ZoneInfo
from datetime import datetime

CT = ZoneInfo('America/Chicago')
now_ct = datetime.now(CT)
```

---

## 7. `--self-test` Flag Pattern

Every Python script must implement this:

```python
import argparse
import sys

def self_test():
    """Run basic diagnostics. Return True if pass."""
    try:
        # Test 1: config loads
        cfg = load_config()
        assert cfg is not None

        # Test 2: DB is reachable
        conn = get_conn()
        conn.execute('SELECT 1')
        conn.close()

        # Add component-specific tests here
        print('[self-test] PASS')
        return True
    except Exception as e:
        print(f'[self-test] FAIL: {e}')
        return False

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    main()
```

---

## 8. Logging

Each component writes to its own log file in `logs/`.

```python
import logging

def get_logger(name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(f'logs/{name}.log')
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(fh)
    return logger
```

---

## 9. Order Type Mental Model

**The simplest way to remember which IB order type to use:**

> **LMT = price comes TO you**
> **STP = price runs AWAY from you (you chase it)**

| Strategy | Price direction to line | IB Order | Memory |
|----------|------------------------|----------|--------|
| Buy the dip (support below) | Price falls DOWN to line | `LMT BUY` | Price comes down to your bid |
| Buy the breakout (level above) | Price rises UP to line | `STP BUY` | Price runs up, you chase |
| Sell resistance (level above) | Price rises UP to line | `LMT SELL` | Price comes up to your offer |
| Sell breakdown (support below) | Price falls DOWN to line | `STP SELL` | Price runs down, you chase |

**Example — current price 6200:**

| Line | Relationship | Action | Order |
|------|-------------|--------|-------|
| 6150 | Below — price must DROP | Buy the dip | `LMT BUY @ 6150` |
| 6250 | Above — price must RISE | Buy the breakout | `STP BUY @ 6250` |
| 6400 | Above — price must RISE | Sell resistance | `LMT SELL @ 6400` |

*Source: confirmed by both Claude and GPT analysis.*

---

## 10. IDE — Cursor Setup

**IDE:** Cursor (VS Code fork with built-in AI)

### Extensions to install
| Extension | Purpose |
|-----------|---------|
| Python | Core Python support |
| Pylance | Type checking, IntelliSense |
| SQLite Viewer | Inspect `galao.db` live while system runs |

### `.cursorrules`
File at project root (`galao/.cursorrules`) tells Cursor's AI the project rules — PAPER-only trading, self-test requirement, DB-only communication, allowed bracket sizes, etc. Always keep this file up to date when rules change.

### Recommended terminal layout
Run all background processes side by side using Cursor's split terminal:
```
[ Terminal 1: decider.py ] [ Terminal 2: broker.py ]
[ Terminal 3: fetcher.py ] [ Terminal 4: sqlite3 data/galao.db ]
```

### Useful Cursor shortcuts
- `Ctrl+Shift+P` → "Split Terminal" — set up multi-process view
- `Ctrl+L` — open AI chat with full codebase context
- `Ctrl+K` — inline AI edit

---

## 10. Known Issues & Solutions

| # | Issue | Solution |
|---|-------|----------|
| T-01 | IB Gateway drops connection periodically | Implement reconnect loop with exponential backoff in `ib_client.py` |
| T-02 | Stale client ID after crash | Store client ID in DB; on startup, try ID, on fail increment and retry |
| T-03 | SQLite lock contention | WAL mode + busy_timeout (see section 5) |
| T-04 | Bracket order partial fill | Track parent fill status; only replenish when parent is fully filled |
| T-05 | Contract month rollover during session | Resolve contract at startup only; do not roll mid-session |
| T-06 | IB rejects too many simultaneous orders | Throttle order submission; IB limit ~50 orders/second |
| T-07 | Clock drift between system and IB | Use IB server time (`ib.reqCurrentTime()`) for session timing decisions |
