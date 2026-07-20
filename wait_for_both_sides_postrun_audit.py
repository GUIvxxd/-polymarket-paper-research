#!/usr/bin/env python3
import json,time
from pathlib import Path
AUDIT=Path('/data/workspace/polymarket-research/reports/both_sides_spike/supervised/runs/supervised_20260720T143929Z_691704ba/postrun_audit.json')
deadline=time.time()+4200
while time.time()<deadline:
    if AUDIT.exists():
        try:
            result=json.loads(AUDIT.read_text())
            verdict='PASS' if result.get('passed') else 'FAIL'
            print(f"Both-sides two-hour qualification {verdict}: frames={result.get('durable_frame_count')}, parser_errors={result.get('parser_errors')}, raw_integrity={(result.get('raw_integrity') or {}).get('ok')}, state={result.get('supervisor_state')}, audit={AUDIT}")
            raise SystemExit(0 if result.get('passed') else 3)
        except json.JSONDecodeError:
            pass
    time.sleep(10)
print(f'Both-sides qualification audit did not appear before watcher timeout: {AUDIT}')
raise SystemExit(4)
