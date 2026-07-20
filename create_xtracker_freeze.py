#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path('/data/workspace/polymarket-research')
SCRIPTS = Path('/data/scripts')
FREEZE_ROOT = ROOT / 'reports' / 'xtracker_strategy_freeze'
STAMP = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
DEST = FREEZE_ROOT / f'xtracker_frozen_{STAMP}'

sources: list[tuple[Path, str, str]] = []

def add(path: Path, destination: str, role: str) -> None:
    if path.is_file() and '__pycache__' not in path.parts and FREEZE_ROOT not in path.parents:
        sources.append((path, destination, role))

for path in sorted(ROOT.glob('xtracker_*.py')):
    add(path, f'workspace/{path.name}', 'strategy_source')
add(ROOT / 'strategy_evidence.py', 'workspace/strategy_evidence.py', 'local_dependency')
for path in sorted((ROOT / 'data').glob('xtracker_*')):
    add(path, f'workspace/data/{path.name}', 'operational_state')
for path in sorted((ROOT / 'config').glob('xtracker_*')):
    add(path, f'workspace/config/{path.name}', 'configuration_or_draft_protocol')
for path in sorted((ROOT / 'reports').glob('xtracker_*')):
    add(path, f'workspace/reports/{path.name}', 'ledger_or_evidence')
for path in sorted(SCRIPTS.glob('xtracker_*')):
    add(path, f'external_scripts/{path.name}', 'scheduler_wrapper')

if not sources:
    raise SystemExit('no X files selected')
if DEST.exists():
    raise SystemExit(f'destination already exists: {DEST}')
DEST.mkdir(parents=True)
records=[]
for source, relative, role in sources:
    target = DEST / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    target_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    if source_hash != target_hash or source.stat().st_size != target.stat().st_size:
        raise RuntimeError(f'copy verification failed: {source}')
    records.append({
        'role': role,
        'source': str(source),
        'frozen_path': relative,
        'bytes': source.stat().st_size,
        'sha256': source_hash,
        'source_mtime_utc': datetime.fromtimestamp(source.stat().st_mtime, UTC).isoformat().replace('+00:00','Z'),
    })

records.sort(key=lambda row: row['frozen_path'])
aggregate = hashlib.sha256()
for row in records:
    aggregate.update(row['frozen_path'].encode() + b'\0' + row['sha256'].encode() + b'\0')
manifest={
    'schema_version':'xtracker_strategy_freeze_v1',
    'freeze_id':DEST.name,
    'frozen_at_utc':datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00','Z'),
    'purpose':'Immutable diagnostic snapshot before seven-loss forensic review; historical sample cannot promote rule changes.',
    'paper_only':True,
    'live_orders_allowed':False,
    'wallet_or_authentication_allowed':False,
    'operational_strategy_rules_changed':False,
    'historical_analysis_policy':'diagnostic only; zero executable rows remain ineligible for expectancy estimation',
    'known_baseline_mismatch':'Operational xtracker_tweet_watchdog.py declares consensus_v3_2026_07_16 while historical xtracker_paper_rebalance_ledger.py declares tightened_v2_2026_07_15.',
    'file_count':len(records),
    'total_bytes':sum(row['bytes'] for row in records),
    'aggregate_path_hash_sha256':aggregate.hexdigest(),
    'files':records,
}
manifest_path=DEST/'FREEZE_MANIFEST.json'
manifest_path.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n',encoding='utf-8')
manifest_sha=hashlib.sha256(manifest_path.read_bytes()).hexdigest()
(DEST/'FREEZE_MANIFEST.sha256').write_text(f'{manifest_sha}  FREEZE_MANIFEST.json\n',encoding='utf-8')

# Reverify all payloads immediately before making the tree read-only.
for row in records:
    path=DEST/row['frozen_path']
    if hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
        raise RuntimeError(f'post-manifest verification failed: {path}')
for path in sorted(DEST.rglob('*'), reverse=True):
    if path.is_file():
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    elif path.is_dir():
        path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
DEST.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
print(json.dumps({'freeze_dir':str(DEST),'manifest':str(manifest_path),'manifest_sha256':manifest_sha,'file_count':len(records),'total_bytes':manifest['total_bytes'],'aggregate_path_hash_sha256':manifest['aggregate_path_hash_sha256'],'read_only':True},indent=2))
