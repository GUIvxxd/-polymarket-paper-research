# Paper Bot Hardening Behavior Changes

Branch: `codex/paper-bot-hardening`

Baseline: `9ff28142f4a91815a4ccef7f5dc5ab9a689dbe4c`

## Changed Behavior

1. `xtracker_forward_engine_v4.py` now wraps the full v4 capture, enrichment, and monitor sequence in an exclusive lock at `reports/xtracker_forward_validation/v4/run.lock`.
   - If another v4 run is already active, the second run exits before reading/writing state or appending hash-chain records.
   - The lock records owner, pid, acquisition time, and lock path.
   - No strategy thresholds, fills, fees, latency, settlement rules, wallet behavior, or live-order behavior changed.

2. `stock_price_paper_bot.py` now labels legacy stock paper PnL as research-only.
   - Summary JSON, Markdown, closed-position rows, CSV fields, and strategy-signal metadata set `execution_valid_pnl=false` and `net_capturable=false`.
   - Existing paper PnL arithmetic is unchanged.

3. `xtracker_paper_rebalance_ledger.py` now labels historical X rebalance PnL as research-only.
   - Summary JSON, Markdown, lifecycle accounting, trade rows, and strategy-signal metadata set `execution_valid_pnl=false` and `net_capturable=false`.
   - Existing historical replay arithmetic is unchanged.

4. `config/xtracker_forward_validation_v4.lock.json` was updated only to match hardened locked-source hashes and its self-hash.
   - Protocol constants and strategy thresholds were not changed.
