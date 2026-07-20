#!/usr/bin/env bash
set -euo pipefail
cd /data/workspace/polymarket-research
export PYTHONDONTWRITEBYTECODE=1
exec /usr/local/bin/python3 public_record_reaction_watchdog.py
