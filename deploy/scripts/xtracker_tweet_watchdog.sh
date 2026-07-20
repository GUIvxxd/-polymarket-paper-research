#!/usr/bin/env bash
set -euo pipefail
/usr/local/bin/python3 /data/workspace/polymarket-research/xtracker_tweet_watchdog.py
exec /usr/local/bin/python3 /data/workspace/polymarket-research/xtracker_forward_engine_v4.py
