#!/usr/bin/env python3
from __future__ import annotations
import ast,csv,hashlib,json
from datetime import datetime,UTC
from pathlib import Path
from typing import Any
ROOT=Path('/data/workspace/polymarket-research');OUT=ROOT/'reports/xtracker_forward_validation/v4';LOCK=ROOT/'config/xtracker_forward_validation_v4.lock.json';STATE=OUT/'state.json';STATUS=OUT/'status.json';EVENTS=OUT/'evidence_events.jsonl';REGISTRY=OUT/'opportunity_registry.jsonl';LEDGER=OUT/'independent_event_ledger.csv'
def canonical(v:Any):return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def sha_bytes(v:bytes):return hashlib.sha256(v).hexdigest()
def sha(p:Path):return sha_bytes(p.read_bytes())
def parse(v:str):return datetime.fromisoformat(v.replace('Z','+00:00'))
def verify_chain(path:Path):
 rows=[];errors=[];prev='0'*64
 for expected,line in enumerate(path.read_text().splitlines(),1):
  row=json.loads(line);rows.append(row);body={k:v for k,v in row.items() if k!='record_hash'};digest=sha_bytes(prev.encode()+canonical(body))
  if row.get('sequence')!=expected:errors.append(f'{path.name}:sequence:{expected}')
  if row.get('previous_hash')!=prev:errors.append(f'{path.name}:previous:{expected}')
  if row.get('record_hash')!=digest:errors.append(f'{path.name}:hash:{expected}')
  prev=digest
 return rows,prev,errors
def raw_refs(value:Any,path=''):
 refs=[]
 if isinstance(value,dict):
  if value.get('raw_path') and value.get('sha256'):refs.append((value['raw_path'],value['sha256'],path))
  for key,item in value.items():
   if key.endswith('_path') and key not in {'raw_path','settlement_proof_path'}:
    digest=value.get(key[:-5]+'_sha256')
    if digest:refs.append((item,digest,path+'/'+key))
   refs+=raw_refs(item,path+'/'+key)
 elif isinstance(value,list):
  for i,item in enumerate(value):refs+=raw_refs(item,path+f'/{i}')
 return refs
def frozen_constants(path:Path):
 tree=ast.parse(path.read_text());wanted={'MIN_ABSOLUTE_PROFIT_EXIT','MIN_RELATIVE_PROFIT_EXIT','FAIR_COLLAPSE_THRESHOLD','STALE_BID_EDGE','BETTER_BUCKET_EDGE_DELTA','REBALANCE_MIN_EDGE','REBALANCE_MIN_FAIR','REBALANCE_MAX_ASK'};out={}
 for node in tree.body:
  if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name) and node.targets[0].id in wanted:out[node.targets[0].id]=ast.literal_eval(node.value)
 return out
def main():
 errors=[];lock=json.loads(LOCK.read_text());body={k:v for k,v in lock.items() if k!='lock_sha256'}
 if sha_bytes(canonical(body))!=lock['lock_sha256']:errors.append('lock_self_hash')
 for path,digest in lock['locked_source_sha256'].items():
  p=Path(path)
  if not p.is_file() or sha(p)!=digest:errors.append('locked_source:'+path)
 manifest=Path(lock['frozen_strategy_manifest_path'])
 if sha(manifest)!=lock['frozen_strategy_manifest_sha256']:errors.append('frozen_manifest_hash')
 constants=frozen_constants(manifest.parent/'workspace/xtracker_paper_rebalance_ledger.py');expected={'MIN_ABSOLUTE_PROFIT_EXIT':0.03,'MIN_RELATIVE_PROFIT_EXIT':0.20,'FAIR_COLLAPSE_THRESHOLD':0.20,'STALE_BID_EDGE':0.10,'BETTER_BUCKET_EDGE_DELTA':0.10,'REBALANCE_MIN_EDGE':0.50,'REBALANCE_MIN_FAIR':0.70,'REBALANCE_MAX_ASK':0.25}
 if constants!=expected:errors.append('frozen_constant_mismatch')
 registry,registry_head,e1=verify_chain(REGISTRY);events,event_head,e2=verify_chain(EVENTS);errors+=e1+e2
 state=json.loads(STATE.read_text());status=json.loads(STATUS.read_text())
 if state['chains']['registry']['last_hash']!=registry_head:errors.append('registry_head')
 if state['chains']['events']['last_hash']!=event_head:errors.append('events_head')
 if status['chain_heads']!=state['chains']:errors.append('status_heads')
 ledger=list(csv.DictReader(LEDGER.open(newline='')))
 if len(ledger)!=len(registry):errors.append('ledger_count')
 if [r['registry_record_hash'] for r in ledger]!=[r['record_hash'] for r in registry]:errors.append('ledger_hash_link')
 activation=parse(lock['activation_utc'])
 for row in registry+events:
  if row.get('decision_time') and parse(row['decision_time'])<activation:errors.append('preactivation:'+str(row.get('record_hash')))
 references=[]
 for row in events:references+=raw_refs(row)
 seen=set();raw_errors=[]
 for path,digest,where in references:
  key=(path,digest)
  if key in seen:continue
  seen.add(key);p=Path(path)
  if not p.is_file() or sha(p)!=digest:raw_errors.append(where+':'+path)
 errors+=['raw:'+v for v in raw_errors]
 entries=[r for r in events if r.get('record_type')=='ARM_ENTRY_EVALUATION'];eligible=[r for r in entries if r.get('execution_evidence_eligible')]
 for row in eligible:
  if row.get('decision')!='PAPER_ENTRY' or not (row.get('execution') or {}).get('complete'):errors.append('eligible_entry_shape')
  if (row.get('decision_evidence') or {}).get('problems') or (row.get('fee_metadata') or {}).get('problems') or (row.get('fill_book') or {}).get('problems'):errors.append('eligible_entry_problems')
  delay=(parse(row['fill_book']['request_started_at'])-parse(row['decision_time'])).total_seconds()*1000
  if delay+1e-6<float(row['paper_order_latency_ms']):errors.append('eligible_entry_latency')
 by_arm={a:sum(r.get('arm')==a and r.get('execution_evidence_eligible') for r in entries) for a in ('baseline','entry_limit_025','event_risk_cap_10usd','early_drawdown_exit_25pct')}
 open_counts={a:len(state.get('open_positions',{}).get(a,{})) for a in by_arm}
 completed={a:len(state.get('completed_clusters',{}).get(a,{})) for a in by_arm}
 if any(by_arm[a]!=open_counts[a]+completed[a] for a in by_arm):errors.append('entry_position_completion_reconciliation')
 if any(key in status for key in ('aggregate_net_pnl','mean_expectancy','arm_ranking')):errors.append('interim_pnl_exposed')
 result={'schema_version':'xtracker_forward_v4_audit_v1','verified_at':datetime.now(UTC).isoformat(),'ok':not errors,'errors':errors,'activation_utc':lock['activation_utc'],'lock_sha256':lock['lock_sha256'],'registry_records':len(registry),'evidence_records':len(events),'entry_evaluations':len(entries),'unique_raw_references_verified':len(seen),'eligible_entry_records_by_arm':by_arm,'eligible_underlying_entry_lifecycles':len(set(r['lifecycle_id'] for r in eligible)),'open_positions_by_arm':open_counts,'completed_clusters_by_arm':completed,'promotion_gate_passed':False,'paper_only':status['paper_only'],'live_orders':status['live_orders'],'wallet_or_authentication_used':status['wallet_or_authentication_used'],'chain_heads':state['chains']}
 (OUT/'audit_latest.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n');(OUT/'audit_latest.md').write_text('\n'.join(['# X forward v4 audit','',f"- Result: **{'PASS' if result['ok'] else 'FAIL'}**",f"- Registered opportunities: `{len(registry)}`",f"- Evidence records: `{len(events)}`",f"- Unique raw references verified: `{len(seen)}`",f"- Eligible underlying entry lifecycles: `{result['eligible_underlying_entry_lifecycles']}`",f"- Completed clusters: `{completed}`",'- Promotion remains false; interim PnL is hidden.','- Paper only; zero live orders and no wallet/authentication.','']))
 print(json.dumps(result,sort_keys=True));return 0 if result['ok'] else 2
if __name__=='__main__':raise SystemExit(main())
