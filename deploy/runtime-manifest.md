# Runtime and Schedule Manifest

Snapshot prepared at **2026-07-20T23:20:36Z** for migration/handoff only. No scheduler entry was stopped, restarted, paused, resumed, edited, or created during preparation.

## Canonical paths

| Purpose | Path |
|---|---|
| Active project root | `/data/workspace/polymarket-research` |
| Live scheduler wrappers | `/data/scripts` |
| Repository wrapper templates | `/data/workspace/polymarket-research/deploy/scripts` |
| Mutable state/database | `/data/workspace/polymarket-research/data` |
| Logs/reports/raw evidence | `/data/workspace/polymarket-research/reports` |
| Verified host interpreter | `/usr/local/bin/python3` (Python 3.13.14) |

The repository templates are documentation/deployment inputs only. The scheduler still executes the original files in `/data/scripts`.

## Active scheduled jobs

Observed through the Hermes scheduler API. Job IDs are host-specific snapshots; list the scheduler again before any future operation.

| Job | Job ID | Schedule | Live script | Scheduler workdir | Effective project cwd | Status at snapshot |
|---|---|---:|---|---|---|---|
| `polymarket-paper-edge-watchdog` | `2d7f14260627` | every 15m | `/data/scripts/polymarket_edge_watchdog.sh` | `/data/workspace` | absolute paths into project root | enabled; latest observed `ok` |
| `polymarket-xtracker-tweet-watchdog` | `c3debc18dbec` | every 5m | `/data/scripts/xtracker_tweet_watchdog.sh` | `/data/workspace` | Python modules use project root | enabled; latest observed `ok` |
| `stock-price-polymarket-paper-bot` | `22aa3c2cd322` | every 60m | `/data/scripts/stock_price_paper_bot_cron.sh` | scheduler default | wrapper `cd`s to project root | enabled; latest observed `ok` |
| `polymarket-xtracker-rebalance-paper-watchdog` | `693fe8a68a71` | every 60m | `/data/scripts/xtracker_rebalance_paper_watchdog.sh` | `/data/workspace` | wrapper `cd`s to project root | enabled; latest observed `ok` |
| `public-record-reaction-us-day-hourly` | `b7e2e37fb187` | `5 11-22 * * 1-5` UTC | `/data/scripts/public_record_reaction_watchdog.sh` | scheduler default | wrapper `cd`s to project root | enabled; latest observed `ok` |
| `event-evidence-pipeline-watchdog` | `3bbc14cbee5b` | every 5m | `/data/scripts/event_evidence_pipeline_watchdog.sh` | project root | project root | enabled; latest observed `ok` |

A separate research-monitor job, `bot-niche-x-gamechanger-watch` (`ffa9e86afb87`), runs every 480m and is not a trading bot. Its latest observed delivery error was `unknown platform 'webui'`.

## Exact live wrapper commands

### Generic edge watchdog

```bash
exec /usr/local/bin/python3 /data/workspace/polymarket-research/edge_watchdog.py
```

### X watchdog and locked v4 forward engine

Sequential execution is intentional:

```bash
/usr/local/bin/python3 /data/workspace/polymarket-research/xtracker_tweet_watchdog.py
exec /usr/local/bin/python3 /data/workspace/polymarket-research/xtracker_forward_engine_v4.py
```

Do not insert another process between these steps or run a duplicate instance.

### Stock paper bot

```bash
cd /data/workspace/polymarket-research
export PYTHONDONTWRITEBYTECODE=1
python stock_price_paper_bot.py
```

### X historical rebalance/exit paper watchdog

```bash
cd /data/workspace/polymarket-research
export PYTHONDONTWRITEBYTECODE=1
exec /usr/local/bin/python3 /data/workspace/polymarket-research/xtracker_rebalance_paper_watchdog.py
```

### Public-record reaction watchdog

```bash
cd /data/workspace/polymarket-research
export PYTHONDONTWRITEBYTECODE=1
exec /usr/local/bin/python3 public_record_reaction_watchdog.py
```

### Evidence pipeline

The live wrapper acquires an exclusive non-blocking lock at `reports/.event_evidence_pipeline.lock`, then runs:

```bash
python3 event_ledger.py
python3 event_ledger.py --input reports/xtracker_strategy_decisions.jsonl
python3 event_ledger.py --input reports/stock_price_strategy_decisions.jsonl
python3 market_state_recorder.py
python3 markout_worker.py
python3 evidence_report.py
```

The two `--input` calls run only when their source JSONL files are non-empty.

## Non-recurring collector commands

The both-sides collector is not currently scheduled. Its last supervised qualification used external host helpers:

```text
/data/scripts/both_sides_collector_supervisor.py
/data/scripts/both_sides_collector_postrun_audit.py
```

The helper-copy operation was blocked during migration preparation, so these two Python files remain external dependencies. Do not assume a clean Git clone contains them.

Safe code-level CLI discovery:

```bash
python -m both_sides_spike --help
python -m both_sides_spike smoke --help
python -m both_sides_spike rolling --help
python -m both_sides_spike audit --help
```

Do not start a long collector without a disk preflight, 1 GiB reserve, bounded duration, supervision, terminal manifest, and post-run replay.

## Configuration

Committed configuration belongs under `config/`:

- `xtracker_forward_validation_v1.json`
- `xtracker_forward_validation_v2.json`
- `xtracker_forward_validation_v3.json`
- `xtracker_forward_validation_v3.lock.json`
- `xtracker_forward_validation_v4.json`
- `xtracker_forward_validation_v4.lock.json`

The v4 lock also binds:

- exact source hashes,
- the external live X wrapper at `/data/scripts/xtracker_tweet_watchdog.sh`, and
- an immutable freeze manifest under `reports/xtracker_strategy_freeze/`.

Therefore a source-only clone cannot execute the current v4 engine until the matching external wrapper and runtime evidence tree are restored at their expected paths. Do not edit the lock to bypass this requirement.

## Environment variables

The scheduled paper bots use public data and do not require Polymarket wallet credentials. Optional legacy/manual X API probes read these variable names:

- `X_API_KEY`
- `X_API_SECRET`
- `X_ACCESS_TOKEN`
- `X_ACCESS_TOKEN_SECRET`

Values must be provided outside Git. No wallet/private-key/live-order variable should be introduced.

## Scheduler verification

Before a handoff or deployment, verify without changing anything:

1. List jobs through the Hermes scheduler interface.
2. Confirm every operational job remains enabled and its last status is `ok`.
3. Confirm live wrapper paths still point to `/data/scripts`.
4. Confirm project paths still point to `/data/workspace/polymarket-research`.
5. Do not treat scheduler `ok` as profitability or executable-edge evidence.
