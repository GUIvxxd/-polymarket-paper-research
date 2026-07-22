#!/usr/bin/env bash
set -euo pipefail
project_root="${EVENT_EVIDENCE_PROJECT_ROOT:-/data/workspace/polymarket-research}"
cd "$project_root"
export PYTHONDONTWRITEBYTECODE=1

# Prevent overlapping scheduler/manual runs from racing append-only JSONL files.
exec 9>"$project_root/reports/.event_evidence_pipeline.lock"
flock -n 9 || exit 0

# Quiet, deterministic, paper-only evidence maintenance.
# Empty stdout means the no-agent scheduler sends no routine notification.
python3 event_ledger.py >/dev/null
for strategy_signals in \
  reports/xtracker_strategy_decisions.jsonl \
  reports/stock_price_strategy_decisions.jsonl; do
  if [[ -s "$strategy_signals" ]]; then
    python3 event_ledger.py --input "$strategy_signals" >/dev/null
  fi
done
python3 market_state_recorder.py >/dev/null

markout_status=0
python3 markout_worker.py >/dev/null || markout_status=$?
case "$markout_status" in
  0|2) ;;
  *) exit "$markout_status" ;;
esac

report_status=0
python3 evidence_report.py >/dev/null || report_status=$?
if (( report_status != 0 )); then
  exit "$report_status"
fi
exit "$markout_status"
