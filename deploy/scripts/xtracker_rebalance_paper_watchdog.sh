#!/usr/bin/env bash
set -euo pipefail
cd /data/workspace/polymarket-research
export PYTHONDONTWRITEBYTECODE=1
exec /usr/local/bin/python3 /data/workspace/polymarket-research/xtracker_rebalance_paper_watchdog.py
