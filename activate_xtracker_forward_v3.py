#!/usr/bin/env python3
from __future__ import annotations
import hashlib,json,os,uuid
from datetime import UTC,datetime
from pathlib import Path

ROOT=Path('/data/workspace/polymarket-research')
LOCK=ROOT/'config/xtracker_forward_validation_v3.lock.json'
FREEZE=ROOT/'reports/xtracker_strategy_freeze/xtracker_frozen_20260720T151927Z'

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
if LOCK.exists(): raise SystemExit('refusing to replace existing activation lock')
manifest=json.loads((FREEZE/'FREEZE_MANIFEST.json').read_text())
frozen={r['source']:r['sha256'] for r in manifest['files']}
required=['/data/workspace/polymarket-research/xtracker_tweet_watchdog.py','/data/workspace/polymarket-research/xtracker_paper_rebalance_ledger.py','/data/workspace/polymarket-research/xtracker_tweet_depth_check.py']
missing=[p for p in required if p not in frozen]
if missing: raise SystemExit(f'missing frozen sources: {missing}')
body={
 'schema_version':'xtracker_forward_validation_lock_v1',
 'protocol_id':'xtracker_forward_v3_20260720',
 'activation_utc':datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00','Z'),
 'paper_only':True,'live_orders_allowed':False,'wallet_or_authentication_allowed':False,'historical_rows_count':False,
 'protocol_path':str(ROOT/'config/xtracker_forward_validation_v3.json'),
 'protocol_sha256':sha(ROOT/'config/xtracker_forward_validation_v3.json'),
 'frozen_strategy_dir':str(FREEZE),
 'frozen_strategy_manifest_sha256':sha(FREEZE/'FREEZE_MANIFEST.json'),
 'frozen_decision_rule_hashes':{p:frozen[p] for p in required},
 'forward_capture_sha256':sha(ROOT/'xtracker_forward_capture.py'),
 'instrumented_depth_collector_sha256':sha(ROOT/'xtracker_tweet_depth_check.py'),
 'sequential_wrapper_sha256':sha(Path('/data/scripts/xtracker_tweet_watchdog.sh')),
 'candidate_parameters':{'entry_limit_max_vwap_and_marginal':0.25,'event_risk_cap_usd_including_fee_and_one_tick_buffer':10.0,'early_drawdown_exit_ratio':0.75},
 'fixed_end':{'completed_independent_clusters':100,'calendar_days':180,'whichever_first':True},
 'minimum_executable_net_capturable_clusters_per_arm':30,
 'amendment_rule':'substantive change requires v4 and a new forward start; this lock is never overwritten'
}
body['lock_sha256']=hashlib.sha256(canonical(body)).hexdigest()
LOCK.parent.mkdir(parents=True,exist_ok=True)
tmp=LOCK.with_suffix(LOCK.suffix+f'.tmp-{uuid.uuid4().hex}')
with tmp.open('w') as h:
 json.dump(body,h,indent=2,sort_keys=True); h.write('\n'); h.flush(); os.fsync(h.fileno())
os.replace(tmp,LOCK)
os.chmod(LOCK,0o444)
print(json.dumps({'lock':str(LOCK),'activation_utc':body['activation_utc'],'lock_sha256':body['lock_sha256'],'protocol_sha256':body['protocol_sha256']}))
