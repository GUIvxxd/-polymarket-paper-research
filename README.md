# Polymarket Paper Research

Paper-only Polymarket research, evidence collection, deterministic replay, and simulated execution tools.

> **Safety boundary:** this project does not authorize live trading. Do not add a wallet, private key, signing flow, authenticated order endpoint, or live-order path. The currently scheduled jobs use public data and record paper decisions only.

## Repository status

This repository root is:

```text
/data/workspace/polymarket-research
```

The active scheduler and runtime state already exist on the host. Creating this repository does **not** install, start, stop, restart, or replace any scheduled process. Read [HANDOFF.md](HANDOFF.md) and [deploy/runtime-manifest.md](deploy/runtime-manifest.md) before running operational entrypoints.

## Components

| Area | Primary modules |
|---|---|
| Generic public-market scan | `strategy_scanner.py`, `edge_watchdog.py`, `scan_polymarket_edges.py` |
| Stock/price paper bot | `multi_market_research_bot.py`, `stock_price_paper_bot.py` |
| X post-count research | `xtracker_tweet_watchdog.py`, `xtracker_tweet_depth_check.py`, `xtracker_paper_rebalance_ledger.py` |
| Locked X forward validation | `xtracker_forward_capture.py`, `xtracker_forward_engine_v4.py`, `xtracker_forward_monitor_v4.py` |
| Public-record signals | `public_record_reaction_bot.py`, `public_record_reaction_watchdog.py` |
| Shared evidence pipeline | `event_ledger.py`, `market_state_recorder.py`, `markout_worker.py`, `evidence_report.py` |
| Both-sides collector/replay | `both_sides_spike/`, `both_sides_supervised_runner.py` |
| Deployment templates | `deploy/scripts/`, `deploy/runtime-manifest.md` |

## Runtime

- Verified host runtime: **Python 3.13.14** at `/usr/local/bin/python3`
- Active root is Python-only; no Node application or `package.json` is present.
- Runtime dependencies are pinned in `requirements.txt`.
- Test dependencies are pinned in `requirements-dev.txt`.

The bare `python` executable on this host may resolve to a different environment. Use the intended virtual environment locally and `/usr/local/bin/python3` when reproducing the current host runtime.

## Safe local setup

Do this in an isolated checkout. Do not point tests at the live `data/` or `reports/` trees.

```bash
cd /data/workspace/polymarket-research
/usr/local/bin/python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

The code currently contains absolute references to `/data/workspace/polymarket-research`. A clone at another path is suitable for source review and most unit tests, but operational runs require either this exact path or a separately reviewed path-configuration change. Do not make that change while a formal X validation window is active.

## Tests

Core deterministic suite:

```bash
python -m unittest -v \
  test_both_sides_spike.py \
  test_event_evidence_pipeline.py \
  test_strategy_evidence_integration.py \
  test_xtracker_forward_capture.py \
  test_xtracker_forward_v4.py
```

Narrow self-tests that do not run the scheduled loop:

```bash
python stock_price_paper_bot.py --self-test
python multi_market_research_bot.py --self-test
python public_record_reaction_bot.py --self-test
```

Host-only collector-supervisor integration test:

```bash
python -m unittest -v test_both_sides_collector_supervisor.py
```

That test currently imports `/data/scripts/both_sides_collector_supervisor.py`, which is an external host dependency and is not copied into this repository.

## Deterministic replay

The both-sides replay test constructs hash-chained fixtures in temporary directories and verifies three identical canonical replay hashes:

```bash
python -m unittest -v test_both_sides_spike.RawLogReplayTests
```

To audit a copied collector run without altering the raw archive:

```bash
python -m both_sides_spike audit /path/to/copied/run/manifest.json
```

The audit writes generated audit output beside the copied manifest. Do not run ad hoc replay or verification against an actively growing production archive.

## Runtime data is not in Git

The following remain on the host and are intentionally ignored:

- `/data/workspace/polymarket-research/data/`
- `/data/workspace/polymarket-research/reports/`
- `/data/workspace/polymarket-research/repos/`
- `/data/scripts/` live scheduler wrappers and host helpers

Back up those paths separately. A Git clone alone is not a production restore.

## Handoff documents

- [HANDOFF.md](HANDOFF.md): full architecture, status, limitations, performance snapshot, run/test/replay procedures, and rollback.
- [AGENTS.md](AGENTS.md): mandatory Codex safety and development rules.
- [deploy/runtime-manifest.md](deploy/runtime-manifest.md): schedule and command snapshot without secrets.
