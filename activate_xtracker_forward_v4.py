#!/usr/bin/env python3
from __future__ import annotations
import hashlib,json,os,uuid
from datetime import datetime,UTC
from pathlib import Path
ROOT=Path('/data/workspace/polymarket-research')
LOCK=ROOT/'config/xtracker_forward_validation_v4.lock.json'
FREEZE=ROOT/'reports/xtracker_strategy_freeze/xtracker_frozen_20260720T151927Z'
def sha(p:Path):return hashlib.sha256(p.read_bytes()).hexdigest()
def canonical(v):return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
if LOCK.exists():raise SystemExit('refusing to overwrite v4 activation lock')
manifest=json.loads((FREEZE/'FREEZE_MANIFEST.json').read_text());frozen={r['source']:r['sha256'] for r in manifest['files']}
watchdog=ROOT/'xtracker_tweet_watchdog.py';ledger=ROOT/'xtracker_paper_rebalance_ledger.py'
for path in (watchdog,ledger):
 if sha(path)!=frozen[str(path)]:raise SystemExit(f'frozen decision source changed: {path}')
locked=[ROOT/'config/xtracker_forward_validation_v4.json',ROOT/'xtracker_forward_capture.py',ROOT/'xtracker_forward_monitor_v4.py',ROOT/'xtracker_forward_engine_v4.py',ROOT/'xtracker_tweet_depth_check.py',watchdog,Path('/data/scripts/xtracker_tweet_watchdog.sh')]
body={'schema_version':'xtracker_forward_validation_lock_v2','protocol_id':'xtracker_forward_v4_20260720','activation_utc':datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00','Z'),'paper_only':True,'live_orders_allowed':False,'wallet_or_authentication_allowed':False,'historical_rows_count':False,'v3_rows_count':False,'protocol_sha256':sha(ROOT/'config/xtracker_forward_validation_v4.json'),'forward_capture_sha256':sha(ROOT/'xtracker_forward_capture.py'),'locked_source_sha256':{str(p):sha(p) for p in locked},'frozen_strategy_manifest_path':str(FREEZE/'FREEZE_MANIFEST.json'),'frozen_strategy_manifest_sha256':sha(FREEZE/'FREEZE_MANIFEST.json'),'frozen_decision_rule_hashes':{str(watchdog):frozen[str(watchdog)],str(ledger):frozen[str(ledger)]},'exact_exit_constants':{'minimum_absolute_profit_per_share':0.03,'minimum_relative_profit':0.20,'fair_collapse_threshold':0.20,'stale_bid_edge':0.10,'better_bucket_edge_delta':0.10,'rebalance_minimum_edge':0.50,'rebalance_minimum_fair':0.70,'rebalance_maximum_ask':0.25},'candidate_parameters':{'entry_limit_max_vwap_and_marginal':0.25,'event_risk_cap_usd_including_fee_and_one_tick_buffer':10.0,'early_drawdown_exit_ratio':0.75},'fixed_end':{'completed_independent_clusters':100,'calendar_days':180,'whichever_first':True},'minimum_executable_net_capturable_clusters_per_arm':30,'amendment_rule':'substantive change requires v5 and resets the forward sample'}
body['lock_sha256']=hashlib.sha256(canonical(body)).hexdigest()
LOCK.parent.mkdir(parents=True,exist_ok=True);tmp=LOCK.with_suffix(LOCK.suffix+f'.tmp-{uuid.uuid4().hex}')
with tmp.open('w') as h:json.dump(body,h,indent=2,sort_keys=True);h.write('\n');h.flush();os.fsync(h.fileno())
os.replace(tmp,LOCK);os.chmod(LOCK,0o444)
print(json.dumps({'lock':str(LOCK),'activation_utc':body['activation_utc'],'lock_sha256':body['lock_sha256'],'locked_sources':len(locked)}))
