# Paper Bot Hardening Behavior Changes

Branch: `codex/paper-bot-hardening`

Baseline: `9ff28142f4a91815a4ccef7f5dc5ab9a689dbe4c`

## Changed Behavior

1. `xtracker_forward_engine_v4.py` now wraps the full v4 capture, enrichment, and monitor sequence in an exclusive lock at `reports/xtracker_forward_validation/v4/run.lock`.
   - Linux uses a nonblocking kernel `fcntl.flock`; if another v4 run is active, the second run exits before capture, enrichment, or monitoring executes.
   - The lock descriptor remains open for the full sequence and is released automatically on normal return, exceptions, and process death.
   - `run.lock` is a persistent coordination path. It is not unlinked on release and requires no stale-file cleanup; owner metadata is diagnostic only.
   - Operational use fails closed on platforms without `fcntl`; there is no pathname-lock fallback.
   - No strategy thresholds, fills, fees, latency, settlement rules, wallet behavior, or live-order behavior changed.

2. `stock_price_paper_bot.py` classifies legacy stock paper PnL as research-only.
   - Future generated rows and derived summary, Markdown, JSON, CSV, and strategy-signal views set `execution_valid_pnl=false` and `net_capturable=false`.
   - Existing persisted rows are not migrated. When an older row omits either field, every active derived reporting/export view defaults it to false without mutating the raw row.
   - Existing paper PnL arithmetic is unchanged.

3. `xtracker_paper_rebalance_ledger.py` classifies historical X rebalance PnL as research-only.
   - Future generated rows and derived summary, Markdown, JSON, CSV, XLSX, lifecycle, and strategy-signal views set `execution_valid_pnl=false` and `net_capturable=false`.
   - Existing append-only strategy-signal rows and other historical evidence are not rewritten or superseded on this branch. Missing legacy labels default to false in active derived views.
   - Existing historical replay arithmetic is unchanged.

4. V4 locked files now have repository attributes that preserve LF bytes across checkouts, and manifest tests hash exact bytes without newline normalization.
   - `config/xtracker_forward_validation_v4.lock.json` is updated only for locked source files changed by this repair and its canonical self-hash.
   - Protocol constants and strategy thresholds were not changed.
