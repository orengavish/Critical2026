# Algo System — Implementation Plan Rules
**Version:** 1.1 | **Date:** 2026-04-27  
**Status:** Approved  
**Purpose:** Rules governing how the 5×5×5 algo implementation plan is structured and executed.  
**Extends:** `rules_book.md` (all existing rules still apply — this doc adds algo-specific rules)

---

## 0. Scope Definitions

The implementation covers four sequential scopes. Each scope is a major deliverable that the next scope depends on.

| ID | Scope | Outcome |
|---|---|---|
| **A** | Enhanced Fetcher | `data/` folder with 2 files per symbol per day: `{symbol}_{date}_trades.csv` + `{symbol}_{date}_bidask.csv`. Runs automatically daily, all configured futures symbols. |
| **B** | Traceback System | All generated trades verified or dropped. Output: a growing, clean set of verified trades usable as backtrader input. |
| **C** | First-Gen Algo Trader | Full 5×5×5 signal generation system. Configurable, A/B testable, improvable. No P&L target yet. |
| **D** | Backtrader | Maximum accuracy backtester running on all verified trades from scope B. A/B tested. |

**Dependency order:** A → B → C → D (strict — no scope starts until the prior scope passes its breakpoint)

---

## 1. Integration Constraint

> **All components go through the existing runner and browser. No exceptions.**

- **Runner:** New components are added as modules to the existing system. No standalone runners outside the existing startup/shutdown lifecycle.
- **Browser:** All output — signals, levels, profiles, A/B results, backtest results — is surfaced through the existing Flask/browser visualizer. New pages/panels are added to the existing visualizer, not separate apps.
- This means Scope A's fetcher, Scope B's traceback, Scope C's signal generator, and Scope D's backtrader all register with the existing system and write to SQLite DB.

---

## 2. Design Doc Rule

| # | Rule |
|---|---|
| R-ALGO-DOC-01 | `docs/system_design.md` is the source of truth for all algo decisions. It must be updated whenever an implementation decision deviates from or extends the design. |
| R-ALGO-DOC-02 | Every autonomous decision made during implementation is logged in `docs/choices_done.md` with: date, what was decided, why, and which design doc section it affects. |
| R-ALGO-DOC-03 | If an implementation step reveals the design doc is wrong or incomplete, update `system_design.md` first, then proceed. Never silently deviate. Every `system_design.md` update requires exactly one new entry in `choices_done.md` describing what changed and why. |
| R-ALGO-DOC-04 | `choices_done.md` is append-only. Entries are never deleted — they are the audit trail. |
| R-ALGO-DOC-05 | Claude may update `system_design.md` autonomously. No user approval required. However R-ALGO-DOC-03 applies without exception — the `choices_done.md` entry must be written in the same step as the design doc update. |

---

## 3. Task Structure Rules

| # | Rule |
|---|---|
| R-ALGO-TASK-01 | The implementation plan is a **sequential numbered list** of tasks. Tasks within a scope may be parallelized only if explicitly marked `[PARALLEL OK]`. All others are strictly sequential. |
| R-ALGO-TASK-02 | Every task must define: (a) goal in one sentence, (b) concrete steps, (c) self-test criteria, (d) performance requirements if applicable. |
| R-ALGO-TASK-03 | **Breakpoints** are defined upfront in the approved plan. A breakpoint is a hard stop requiring user confirmation before proceeding. Breakpoints occur at: scope boundaries, first-data-in-DB, first-signal-generated, first real order, and any point the plan marks `[BREAKPOINT]`. |
| R-ALGO-TASK-04 | Between breakpoints, implementation proceeds autonomously following the post-step protocol (Section 5). |
| R-ALGO-TASK-05 | A task is **complete** only when: code is written, `--self-test` passes (R-DEV-01), release note is written, and git checkpoint is saved. |

---

## 4. Checkpoint / Version Rules

Extends existing `R-VER-*` rules with algo-specific additions:

| # | Rule |
|---|---|
| R-ALGO-VER-01 | Every completed task (per R-ALGO-TASK-05) produces a **git commit** tagged with the task ID (e.g. `[A-03] Fetcher bid/ask parser complete`). |
| R-ALGO-VER-02 | Every **breakpoint** produces a **git tag** (e.g. `scope-A-complete`, `scope-B-complete`) enabling clean rollback to any scope boundary. |
| R-ALGO-VER-03 | Release notes for algo tasks go into the existing `release_notes.md` AND the existing DB table (per R-VER-03), with component prefix `[ALGO-A]`, `[ALGO-B]`, etc. |
| R-ALGO-VER-04 | Before any task that modifies an existing source file, copy to `versions/` per R-VER-01. For new files, no copy needed — git history is sufficient. |
| R-ALGO-VER-05 | Rollback procedure: `git checkout {tag}` restores to any breakpoint. This must always be possible without data loss (only code rolls back, not data). |

---

## 5. Post-Step Protocol

After every completed task, execute in order:

### 5.1 — Self-Brainstorm
Ask internally:
- Did we implement the best approach available, or did we take a shortcut?
- Is there anything that needs automatic approval before next step can work? (e.g. a config value, a file path, a DB schema change)
- Did we introduce any technical debt that will block a later scope?
- Is the output exactly what the next step needs as input?

If any answer is "no" or "not sure": fix it before moving on. Log the decision in `choices_done.md`.

### 5.2 — Design Doc Review
- Re-read the relevant section of `system_design.md` for the next task.
- Re-read the next 2–3 tasks in the plan.
- Ask: does the next task still make sense given what was just built?
- If **minor gap** (can be resolved without user): fix it, update design doc, log in `choices_done.md`, proceed.
- If **show stopper** (contradicts approved plan, missing dependency, wrong assumption): stop and surface to user before proceeding.

### 5.3 — Auto-Proceed
- If no show stopper found: proceed to next task without asking.
- If next task is a `[BREAKPOINT]`: stop regardless and wait for user.
- If next task requires data/access not yet available: stop and report what is needed.

---

## 6. Performance Requirements Rules

| # | Rule |
|---|---|
| R-ALGO-PERF-01 | Every scope must define at least one **measurable performance requirement** before implementation begins. |
| R-ALGO-PERF-02 | Performance is checked at the end of the scope, before the breakpoint. |
| R-ALGO-PERF-03 | If a performance requirement is not met: **stop, do not proceed to the next scope**. Brainstorm causes and surface to user. |
| R-ALGO-PERF-04 | Performance requirements are defined in the implementation plan doc, not here. This doc only defines the rule that they must exist and be enforced. |

**Known performance requirements (to be refined in the plan):**

| Scope | Requirement |
|---|---|
| A — Fetcher | 100% of configured symbols fetched for each trading day. Zero corrupt files (all pass validation). No missing sessions. |
| B — Traceback | A **verified trade** is defined as: (1) valid entry point — price + timestamp recorded cleanly, (2) valid exit point — not a shutdown/abort exit, (3) no errors in the tracing process, (4) saved in DB with P&L and both timestamps. Verified trades must cover ≥ 80% of all generated signals. |
| C — Algo Trader | All 25 approaches produce output every session. A/B harness is manually triggerable from browser and designed for future automation. System generates ≥ 2 signals per session on average. |
| D — Backtrader | Backtest results deterministic (reproducible to ± 0.1% across runs). Uses 100% of Scope B verified trades. |

---

## 7. Stop Conditions

Execution stops (waits for user) in any of these cases:

| Condition | Type |
|---|---|
| Defined `[BREAKPOINT]` reached | Expected |
| `--self-test` fails after 2 retry attempts | Hard stop |
| Performance requirement not met at scope end | Hard stop |
| Show-stopper found in design doc review (5.2) | Hard stop |
| Task requires live IB connection not available | Dependency stop |
| Task requires data from prior scope not yet complete | Dependency stop |
| Autonomous decision would change the approved plan significantly | Escalation stop |

---

## 8. What This Doc Does NOT Define

The following are defined in the **implementation plan doc** (not here):
- The actual task list and breakpoints
- The specific performance requirement values
- Scope A/B/C/D task breakdown
- Timeline / ordering within scopes

The following are defined in **`rules_book.md`** (not duplicated here):
- `--self-test` rule (R-DEV-01)
- Versioning workflow (R-VER-*)
- Logging standards (R-LOG-*)
- Error handling (R-ERR-*)
- DB communication rule (R-DEV-04)
- No hardcoded config (R-DEV-05)

---

## 9. Resolved Design Decisions

| # | Decision | Resolution |
|---|---|---|
| Q1 | `choices_done.md` format | Structured entry per R-ALGO-DOC-02: date / decision / why / design doc section affected. Append-only. |
| Q2 | Parallel tasks | Strict sequential throughout. Tasks marked `[PARALLEL OK]` only if explicitly approved in the plan. |
| Q3 | Breakpoint granularity | Scope boundaries + **first-data-in-DB** + **first-signal-generated** + first real order. Now in R-ALGO-TASK-03. |
| Q4 | Verified trade definition | (1) valid entry point, (2) valid non-shutdown exit, (3) no tracing errors, (4) saved in DB with P&L + both timestamps. Now in Section 6. |
| Q5 | Backtrader accuracy | Deterministic to ± 0.1% across runs. Uses 100% of Scope B verified trades. Further benchmark TBD after Scope B complete. |
| Q6 | A/B harness trigger | Manually triggered from browser. Designed to support future automation without rework. |
| Q7 | `system_design.md` update ownership | Claude may update autonomously. Every update requires one `choices_done.md` entry in the same step (R-ALGO-DOC-05). |

---

*End of implementation rules v1.1 — approved 2026-04-27.*
