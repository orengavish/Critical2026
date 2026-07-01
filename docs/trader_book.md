# Galao System — Trader Book
# Manual for a Human Broker
Version: 0.1.0 | Date: 2026-04-07

This document explains how to operate the Galao trading strategy manually in IB TWS or IB Gateway.
No Python. No automation. You are the broker.

---

## 1. Order Types in Plain English

Before anything else, understand these four order types:

| IB Type | What it does | Use when |
|---------|-------------|---------|
| **LMT BUY** | "Buy at this price or lower." Waits for price to DROP to your level. | Line is ABOVE current price. Price must fall to reach it. |
| **STP BUY** | "Buy when price RISES to this level." Waits for price to go UP. | Line is BELOW current price. Price must rise to reach it. |
| **LMT SELL** | "Sell at this price or higher." Waits for price to RISE to your level. | Line is BELOW current price. Price must rise to reach it. |
| **STP SELL** | "Sell when price DROPS to this level." Waits for price to fall. | Line is ABOVE current price. Price must fall to reach it. |
| **MKT** | Execute immediately at best available price. | Emergency exits only. |

**Memory trick:**
> LMT = price comes to you.
> STP = price runs away from you, you chase it.

---

## 2. The Decision Rule — Which Order to Place

**Step 1:** Look at the critical line price.
**Step 2:** Compare to current price.
**Step 3:** Pick order type from this table:

```
Is the line ABOVE current price? (price must RISE to reach it)
   → BUY there:  use STP BUY
   → SELL there: use LMT SELL

Is the line BELOW current price? (price must FALL to reach it)
   → BUY there:  use LMT BUY
   → SELL there: use STP SELL
```

**You always place BOTH a BUY and a SELL at every line.**
One bets the line holds (bounce). One bets it breaks. The bracket decides who wins.

---

## 3. Bracket Parameters

Every order is a **bracket**: one entry + one take-profit (TP) + one stop-loss (SL).
Brackets are always **symmetric**: TP distance = SL distance from entry.

```
BUY bracket example (entry=6250, bracket=4pts):
   Entry:       LMT BUY  @ 6250.00
   Take Profit: LMT SELL @ 6254.00  (+4 pts)
   Stop Loss:   STP SELL @ 6246.00  (-4 pts)

SELL bracket example (entry=6250, bracket=4pts):
   Entry:       STP SELL @ 6250.00
   Take Profit: LMT BUY  @ 6246.00  (+4 pts)
   Stop Loss:   STP BUY  @ 6254.00  (-4 pts)
```

**Rules:**
- Quantity: always **1 contract** (MES)
- All prices rounded to nearest **0.25** (MES tick size)
- Bracket sizes to use: from config — e.g. [2, 4] points
- Account: **PAPER account only**

---

## 4. Session Timing

| Time (Chicago CT) | Action |
|-------------------|--------|
| CME Regular open: 08:30 CT | Market opens |
| **09:00 CT (+30 min)** | **Place all opening orders** |
| Throughout the day | Replenish after each fill |
| **14:15 CT (-60 min)** | **Begin shutdown — no new orders** |
| 14:15–15:05 CT | Exit all open positions |
| 15:15 CT | CME regular close |

---

## 5. How to Place a Bracket Order in IB TWS

1. Right-click on the MES contract → **Buy** or **Sell**
2. Set order type to **LMT** or **STP**
3. Set price
4. Set quantity = 1
5. **Attach → Bracket** → enter TP price and SL price
6. Verify account = PAPER
7. Click **Transmit**

*In TWS the bracket is called "Attached Orders" or you can use the Order Ticket → Advanced.*

---

## 6. Examples

---

### Example 1 — Morning Setup (09:00 CT)

**Situation:**
```
Current price:   6200
Critical lines:  6150 (strength 2),  6250 (strength 3),  6400 (strength 2)
Bracket sizes:   2 pts and 4 pts
```

**Step 1 — Classify each line:**

| Line | vs 6200 | BUY order | SELL order |
|------|---------|-----------|------------|
| 6150 | Below (price must fall) | LMT BUY | STP SELL |
| 6250 | Above (price must rise) | STP BUY  | LMT SELL |
| 6400 | Above (price must rise) | STP BUY  | LMT SELL |

**Step 2 — Build all orders (bracket = 4 pts shown):**

```
LINE 6150:
  LMT BUY  @ 6150.00 | TP: 6154.00 | SL: 6146.00   ← buy the dip
  STP SELL @ 6150.00 | TP: 6146.00 | SL: 6154.00   ← sell the breakdown

LINE 6250:
  STP BUY  @ 6250.00 | TP: 6254.00 | SL: 6246.00   ← buy the breakout
  LMT SELL @ 6250.00 | TP: 6246.00 | SL: 6254.00   ← sell the resistance

LINE 6400:
  STP BUY  @ 6400.00 | TP: 6404.00 | SL: 6396.00   ← buy the breakout
  LMT SELL @ 6400.00 | TP: 6396.00 | SL: 6404.00   ← sell the resistance
```

With 2 bracket sizes (2 and 4 pts), you place **12 orders total** (3 lines × 2 directions × 2 brackets).

---

### Example 2 — Price Moves, Order Becomes Wrong

**Situation:**
```
09:00 — You placed STP BUY @ 6250 (price was at 6200, below 6250)
10:15 — Price rose to 6260, then fell back to 6230
         Now price is 6230 — which is BELOW 6250 again? No wait:
         Price is 6230 which is still below 6250 → STP BUY is still correct.

Different scenario:
09:00 — You placed LMT BUY @ 6150 (price was at 6200, above 6150)
10:00 — Price dropped to 6140 (now BELOW 6150)
         LMT BUY @ 6150 would have already filled at 6150 when price passed through.
         That's fine — that's the expected fill.
```

**When to manually re-evaluate:**
After any fill, check: has the price crossed to the other side of the line?
If yes → cancel the old unfilled orders at that line → place fresh orders using the new toggle.

```
Example:
  Line = 6250
  Price dropped from 6280 to 6240 (crossed below 6250)
  
  Old orders at 6250 (placed when price was above):
    LMT BUY  → filled ✓ (this was correct — caught the drop)
    STP SELL → still pending? Check: STP SELL triggers on drop to 6250.
               Price already passed through 6250 going down.
               This order may have filled or is now at wrong side.
               Cancel it. Replace with correct orders for price now at 6240 (below 6250):
    New: STP BUY  @ 6250 (catches recovery back up to 6250)
    New: LMT SELL @ 6250 (catches rise back to 6250)
```

---

### Example 3 — Entering a Position (Fill)

**Situation:**
```
You placed: STP BUY @ 6250 | TP: 6254 | SL: 6246
11:05 — Price rises through 6250
         STP BUY fills @ 6250.00
         You are now LONG 1 MES @ 6250
```

**What happens automatically in IB:**
```
Children activate:
   LMT SELL @ 6254  (take profit — active, waiting)
   STP SELL @ 6246  (stop loss — active, waiting)
```

**What you do:**
- Nothing. Watch the price.
- IB will execute TP or SL automatically.
- Note the fill in your log.
- Prepare the replenishment order (see Example 5).

**What NOT to do:**
- Do not manually interfere with the bracket unless emergency.
- Do not place additional orders on the same line until this bracket resolves.

---

### Example 4 — Normal Exit: Take Profit Hit

**Situation (continuing Example 3):**
```
You entered LONG @ 6250 (bracket 4pts)
11:08 — Price rises to 6254
         LMT SELL @ 6254 fills
         STP SELL @ 6246 auto-cancelled by IB (OCO)
         Position closed.
         P&L: +4 points = +$20 (MES = $5/pt)
```

**What you do:**
1. Confirm the fill in TWS (position should show 0).
2. Log it: line=6250, direction=BUY, bracket=4, result=TP, P&L=+4pts.
3. Replenish: place a fresh STP BUY @ 6250 bracket (same line, same parameters).
   Re-evaluate toggle first: where is price now?

---

### Example 5 — Normal Exit: Stop Loss Hit + Cool-down

**Situation:**
```
You entered LONG @ 6250 (bracket 4pts)
11:08 — Price drops to 6246
         STP SELL @ 6246 fills
         LMT SELL @ 6254 auto-cancelled by IB (OCO)
         Position closed.
         P&L: -4 points = -$20
```

**What you do:**
1. Confirm flat position in TWS.
2. Log it: line=6250, direction=BUY, bracket=4, result=SL, P&L=-4pts.
3. **Wait 30 seconds** before placing the next order at this line (cool-down).
   Reason: the line just broke — entering immediately risks another loss in the same direction.
4. After 30 seconds: re-evaluate toggle, place fresh orders.

---

### Example 6 — Stagnation: Position Not Moving

**Situation:**
```
You entered LONG @ 6250
60+ seconds pass — price is stuck at 6250.25, barely moving
TP is at 6254, SL is at 6246 — neither is close to filling
```

**What you do:**
1. Cancel the TP order (LMT SELL @ 6254).
2. Cancel the SL order (STP SELL @ 6246).
3. Place **MKT SELL** to close the position immediately.
4. Log: exit_reason = STAGNATION.
5. Replenish immediately (no cool-down needed for stagnation).

**Why exit?** A non-moving line is wasting capital and blocking the next opportunity.

---

### Example 7 — Emergency Exit (All Positions)

**Situation:** News event, connection problem, end of day approaching, or any reason to exit everything immediately.

**Steps:**
1. **Cancel all pending orders** — in TWS: Account → Orders → select all → Cancel.
   Or use the "Cancel All" button if available.
2. For each open position: place **MKT SELL** (if long) or **MKT BUY** (if short).
3. Confirm all positions = 0 in the Positions panel.
4. Do not place any new orders.

**Priority order when exiting multiple positions:**
1. Largest losing positions first.
2. Then remaining positions by size.

---

### Example 8 — End of Day Shutdown (14:15 CT)

**Situation:** It is 60 minutes before CME close (14:15 CT).

**Step 1 — Cancel all pending entries (unfilled parent orders):**
- In TWS: go to Orders tab → select all pending (not filled) → Cancel.
- Leave TP and SL orders on any OPEN positions alone for now.

**Step 2 — Tighten stops:**
- For each open position: modify the SL to within 1 point of current price.
- Example: Long @ 6250, current price 6258 → move SL from 6246 to 6257.

**Step 3 — Orderly exit:**
- Exit one position at a time using LMT orders near the bid/ask.
- Wait 30 seconds between each.
- If LMT doesn't fill within 30 seconds, switch to MKT.

**Step 4 — Panic (if 10 minutes or less remain):**
- Cancel all remaining orders.
- MKT exit all remaining positions simultaneously.
- Confirm flat.

---

## 7. Replenishment — What to Do After Every Fill

After any position closes (TP, SL, or stagnation):

```
1. Note current price
2. Note which line was hit
3. Re-evaluate: is current price now above or below the line?
4. Apply the decision rule (Section 2)
5. Place fresh bracket orders at the same line
6. (If SL exit: wait 30 seconds first)
```

**Example:**
```
Line = 6250, bracket = 4pts
Fill at 11:05, exit at 11:08 (TP hit)
Current price after exit = 6255 (above 6250)

Re-evaluate: price 6255 is ABOVE 6250
   BUY:  LMT BUY  @ 6250 | TP: 6254 | SL: 6246
   SELL: STP SELL @ 6250 | TP: 6246 | SL: 6254

Place both. Done.
```

---

## 8. Quick Reference Card

```
┌─────────────────────────────────────────────────────────────┐
│                  GALAO — ORDER CHEAT SHEET                  │
├──────────────────┬──────────────────┬───────────────────────┤
│ Line vs Price    │ BUY entry        │ SELL entry            │
├──────────────────┼──────────────────┼───────────────────────┤
│ Line ABOVE price │ STP BUY          │ LMT SELL              │
│ Line BELOW price │ LMT BUY          │ STP SELL              │
├──────────────────┴──────────────────┴───────────────────────┤
│ Bracket:  TP = entry ± bracket_size  (same distance both)   │
│ Quantity: 1 contract (MES)                                  │
│ Account:  PAPER only                                        │
│ Tick:     round all prices to 0.25                          │
├─────────────────────────────────────────────────────────────┤
│ TIMING                                                      │
│   Open orders:   09:00 CT  (CME open + 30 min)             │
│   Stop trading:  14:15 CT  (CME close - 60 min)            │
├─────────────────────────────────────────────────────────────┤
│ AFTER A FILL                                                │
│   TP exit:  replenish immediately                           │
│   SL exit:  wait 30 sec, then replenish                    │
│   Stagnation (60s, no 0.5pt move): MKT exit, replenish     │
├─────────────────────────────────────────────────────────────┤
│ EMERGENCY                                                   │
│   Cancel all orders → MKT exit all positions → confirm flat │
└─────────────────────────────────────────────────────────────┘
```
