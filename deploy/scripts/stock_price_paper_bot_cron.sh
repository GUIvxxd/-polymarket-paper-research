#!/usr/bin/env bash
set -euo pipefail
cd /data/workspace/polymarket-research
export PYTHONDONTWRITEBYTECODE=1
python stock_price_paper_bot.py
