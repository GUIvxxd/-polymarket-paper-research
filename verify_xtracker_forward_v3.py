#!/usr/bin/env python3
from __future__ import annotations
import csv,hashlib,json
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT=Path('/data/workspace/polymarket-research')
OUT=ROOT/'reports/xtracker_forward_validation/v3'
LOCK=ROOT/'config/xtracker_forward_validation_v3.lock.json'
PROTOCOL=ROOT/'config/xtracker_forward_validation_v3.json'
EVENTS=OUT/'evidence_events.jsonl'; REGISTRY=OUT/'opportunity_registry.jsonl'; LEDGER=OUT/'independent_event_ledger.csv'; STATE=OUT/'state.json'; STATUS=OUT/'status.json'

def canonical(v:Any)->bytes:return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def sha_bytes(v:bytes)->str:return hashlib.sha256(v).hexdigest()
def sha_file(p:Path)->str:return sha_bytes(p.read_bytes())
def parse(v:str)->datetime:return datetime.fromisoformat(v.replace('Z','+00:00'))

def chain(path:Path):
    previous='0'*64; expected=1; errors=[]; rows=[]
    for line in path.read_text().splitlines():
        row=json.loads(line); rows.append(row)
        if row.get('sequence')!=expected: errors.append(f'{path.name}:sequence:{expected}')
        if row.get('previous_hash')!=previous: errors.append(f'{path.name}:previous:{expected}')
        body={k:v for k,v in row.items() if k!='record_hash'}
        digest=sha_bytes(previous.encode()+canonical(body))
        if row.get('record_hash')!=digest: errors.append(f'{path.name}:hash:{expected}')
        previous=digest; expected+=1
    return rows,previous,errors

def main():
    errors=[]
    lock=json.loads(LOCK.read_text()); lock_body={k:v for k,v in lock.items() if k!='lock_sha256'}
    if sha_bytes(canonical(lock_body))!=lock['lock_sha256']:errors.append('lock_self_hash')
    for path,key in [(PROTOCOL,'protocol_sha256'),(ROOT/'xtracker_forward_capture.py','forward_capture_sha256'),(ROOT/'xtracker_tweet_depth_check.py','instrumented_depth_collector_sha256'),(Path('/data/scripts/xtracker_tweet_watchdog.sh'),'sequential_wrapper_sha256')]:
        if sha_file(path)!=lock[key]:errors.append('source_hash:'+str(path))
    registry,registry_head,registry_errors=chain(REGISTRY); events,event_head,event_errors=chain(EVENTS); errors+=registry_errors+event_errors
    state=json.loads(STATE.read_text()); status=json.loads(STATUS.read_text())
    if state['chains']['registry']['last_hash']!=registry_head:errors.append('registry_state_head')
    if state['chains']['events']['last_hash']!=event_head:errors.append('events_state_head')
    if status['chain_heads']!=state['chains']:errors.append('status_chain_heads')
    csv_rows=list(csv.DictReader(LEDGER.open(newline='')))
    if len(csv_rows)!=len(registry):errors.append('ledger_registry_count')
    if [r['registry_record_hash'] for r in csv_rows]!=[r['record_hash'] for r in registry]:errors.append('ledger_registry_hashes')
    entry=[r for r in events if r.get('record_type')=='ARM_ENTRY_EVALUATION']
    raw_errors=[]; causal_errors=[]
    for row in entry:
        references=[]
        fill=row.get('fill_book') or {}; metadata=row.get('fee_metadata') or {}
        references.append((fill.get('raw_path'),fill.get('sha256')))
        references.append((metadata.get('market_info_path'),metadata.get('market_info_sha256')))
        references.append((metadata.get('fee_rate_path'),metadata.get('fee_rate_sha256')))
        for path,digest in references:
            p=Path(str(path or ''))
            if not p.is_file() or sha_file(p)!=digest:raw_errors.append(f"{row.get('sequence')}:{path}")
        if row.get('execution_evidence_eligible'):
            if row.get('decision')!='PAPER_ENTRY' or not (row.get('execution') or {}).get('complete'): causal_errors.append(f"eligible_shape:{row.get('sequence')}")
            if (row.get('decision_evidence') or {}).get('problems') or fill.get('problems') or metadata.get('problems'): causal_errors.append(f"eligible_problems:{row.get('sequence')}")
            delay=(parse(fill['request_started_at'])-parse(row['decision_time'])).total_seconds()*1000
            if delay+1e-6<float(row['paper_order_latency_ms']):causal_errors.append(f"latency:{row.get('sequence')}")
            if row.get('net_capturable') is not False:causal_errors.append(f"premature_net_capture:{row.get('sequence')}")
    errors+=['raw:'+v for v in raw_errors]+causal_errors
    eligible=[r for r in entry if r.get('execution_evidence_eligible')]
    eligible_lifecycles=sorted(set(r['lifecycle_id'] for r in eligible))
    eligible_by_arm={arm:sum(r.get('arm')==arm and r.get('execution_evidence_eligible') for r in entry) for arm in ('baseline','entry_limit_025','event_risk_cap_10usd','early_drawdown_exit_25pct')}
    open_counts={arm:len(rows) for arm,rows in state.get('open_positions',{}).items()}
    if eligible_by_arm!=open_counts:errors.append('eligible_entry_open_position_counts')
    result={'schema_version':'xtracker_forward_audit_v1','verified_at':datetime.now().astimezone().isoformat(),'ok':not errors,'errors':errors,'activation_utc':lock['activation_utc'],'protocol_id':lock['protocol_id'],'registry_records':len(registry),'evidence_records':len(events),'entry_evaluations':len(entry),'raw_references_checked':len(entry)*3,'eligible_entry_records_by_arm':eligible_by_arm,'eligible_underlying_lifecycles':eligible_lifecycles,'executable_completed_clusters_by_arm':status['executable_completed_clusters_by_arm'],'net_capturable_completed_clusters_by_arm':status['net_capturable_completed_clusters_by_arm'],'promotion_gate_passed':status['promotion_gate_passed'],'paper_only':status['paper_only'],'live_orders':status['live_orders'],'wallet_or_authentication_used':status['wallet_or_authentication_used'],'chain_heads':state['chains']}
    atomic=OUT/'audit_latest.json'; atomic.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    md=OUT/'audit_latest.md'; md.write_text('\n'.join(['# X forward v3 evidence audit','',f"- Result: **{'PASS' if result['ok'] else 'FAIL'}**",f"- Registered opportunities: `{len(registry)}`",f"- Evidence records: `{len(events)}`",f"- Entry evaluations: `{len(entry)}`",f"- Raw references verified: `{len(entry)*3}`",f"- Eligible underlying entry lifecycles: `{len(eligible_lifecycles)}`",f"- Executable completed clusters: `{status['executable_completed_clusters_by_arm']}`",f"- Net-capturable completed clusters: `{status['net_capturable_completed_clusters_by_arm']}`",f"- Promotion gate: `{status['promotion_gate_passed']}`",'- Paper only; zero live orders and no wallet/authentication.','']))
    print(json.dumps(result,sort_keys=True))
    return 0 if result['ok'] else 2
if __name__=='__main__':raise SystemExit(main())
