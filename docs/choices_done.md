# Choices Done — Autonomous Decision Log
**Append-only audit trail of all autonomous decisions made during implementation.**  
Format per entry: date | decision | why | design doc section affected

---

| Date | Decision | Why | Design Doc Section |
|---|---|---|---|
| 2026-04-27 | Created `algo_implementation_rules.md` as the governing doc for the 5×5×5 implementation plan | Needed a rules layer on top of existing `rules_book.md` specific to the algo implementation scopes A–D | `system_design.md` § Implementation Sequence |
| 2026-04-27 | Defined "verified trade" as: valid entry + valid non-shutdown exit + no tracing errors + saved in DB with P&L and timestamps | User definition — precise enough to be measurable in Scope B performance check | `system_design.md` § 7 (Implementation Sequence — Phase B) |
| 2026-04-27 | A/B harness: manually triggered from browser, designed for future automation | User decision — avoids over-engineering for automation now while keeping the path open | `system_design.md` § 6 (A/B Harness) |
| 2026-04-27 | Claude may update `system_design.md` autonomously; every update requires one `choices_done.md` entry in the same step | User approved autonomous updates to avoid bottlenecks; audit trail compensates for reduced oversight | `system_design.md` (all sections) |
| 2026-04-27 | A-01 Fetcher Audit complete — config keys: `paths.history`, `paths.db`, `symbols`, `ib.live_host/port`, `ib.fetcher_client_ids`; progress DB: `data/fetch_progress.db`; incomplete files deleted on restart; `_EXCHANGE_MAP` already covers all 4 symbols; self-test PASS; `data/history/` empty (no historical data yet) | Required upfront understanding before modifying fetcher | `algo_implementation_plan.md` A-01 |
| 2026-04-27 | A-02: Add MNQ, MYM, M2K to `symbols:` in config.yaml; add `fetcher:` config section with auto_fetch_enabled, fetch_bid_ask, fetch_on_startup, symbols_override; live fetch test deferred — IB not running on Sunday, but _EXCHANGE_MAP already covers all 4 symbols so no code change needed | Multi-symbol config required before building scheduler in A-03 | `algo_implementation_plan.md` A-02 |
| 2026-04-27 | BP-A1 override: user approved continuing to A-04/A-05 before first real fetch_log data. fetch_log is empty because today is Sunday (no trading day). First real fetch will fire Monday 2026-04-28 at 17:30 CT | A-04 and A-05 can be built without real data; breakpoint condition will be satisfied organically | `algo_implementation_plan.md` BP-A1 |
| 2026-04-28 | B-01 and B-02 already implemented by remote code pulled 2026-04-27: `completed_trades` table, `verified_trades` view (filters invalid/test/instant trades), `record_completed_trade()` helper, broker calls it on every CLOSED command. No rebuild needed. | Remote code did this work; skipping to B-04/B-05 | `algo_implementation_plan.md` B-01, B-02 |
