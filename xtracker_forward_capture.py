#!/usr/bin/env python3
"""Forward-only, public-data, paper execution evidence capture for frozen X v3 rules.

This script never authenticates and never creates orders. It registers the exact v3
selected candidate set, waits through a conservative paper latency, fetches a separate
public CLOB book, archives raw identity/fee evidence, and evaluates paired paper arms.
Historical snapshots are never replayed into this ledger.
"""
from __future__ import annotations

import csv
import errno
import hashlib
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

ROOT=Path('/data/workspace/polymarket-research')
PROTOCOL=ROOT/'config/xtracker_forward_validation_v3.json'
LOCK=ROOT/'config/xtracker_forward_validation_v3.lock.json'
DEPTH=ROOT/'reports/xtracker_tweet_depth_latest.json'
WATCHDOG_STATE=ROOT/'data/xtracker_tweet_watchdog_state.json'
OUT=ROOT/'reports/xtracker_forward_validation/v3'
STATE=OUT/'state.json'
STATUS=OUT/'status.json'
REGISTRY=OUT/'opportunity_registry.jsonl'
EVENTS=OUT/'evidence_events.jsonl'
LEDGER=OUT/'independent_event_ledger.csv'
RAW=OUT/'raw'
UA='Hermes-XForwardPaper/1.0'
ARMS=('baseline','entry_limit_025','event_risk_cap_10usd','early_drawdown_exit_25pct')
LEDGER_FIELDS=(
    'protocol_id','protocol_sha256','baseline_lock_sha256','opportunity_sequence','opportunity_id','provisional_cluster_id',
    'lifecycle_id','condition_id','yes_token_id','event','handle','bucket','question','first_discovered_at','decision_time',
    'selected_by_frozen_v3','watchdog_filter_version','decision_book_timing_quality','decision_book_raw_path','decision_book_sha256',
    'decision_book_request_started_at','decision_book_response_received_at','decision_book_provider_timestamp','registry_record_hash'
)


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00','Z')


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z','+00:00'))


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def canonical(value: Any) -> bytes:
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+f'.tmp-{uuid.uuid4().hex}')
    with temp.open('w',encoding='utf-8') as handle:
        json.dump(value,handle,indent=2,sort_keys=True); handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
    os.replace(temp,path)


def load_json(path: Path, default: Any) -> Any:
    try: return json.loads(path.read_text(encoding='utf-8'))
    except Exception: return default


@contextmanager
def exclusive_run_lock(owner: str = 'xtracker_forward') -> Iterator[dict[str,Any]]:
    """Hold a Linux kernel lock around mutable forward state/chains."""
    try:
        import fcntl
    except ImportError as exc:
        raise SystemExit('exclusive run lock requires Linux/POSIX fcntl; unsupported platform') from exc
    path=OUT/'run.lock'
    path.parent.mkdir(parents=True,exist_ok=True)
    metadata={'schema_version':'exclusive_run_lock_v1','owner':owner,'pid':os.getpid(),'acquired_at':utcnow(),'lock_path':str(path)}
    handle=None
    try:
        fd=os.open(str(path),os.O_RDWR|os.O_CREAT,0o644)
        handle=os.fdopen(fd,'r+',encoding='utf-8')
        try:
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES,errno.EAGAIN):
                raise
            existing=load_json(path,{})
            detail=f" last_owner={existing.get('owner')} last_pid={existing.get('pid')} last_acquired_at={existing.get('acquired_at')}" if existing else ''
            raise SystemExit(f'exclusive run lock already held: {path}{detail}') from exc
        handle.seek(0); handle.truncate()
        json.dump(metadata,handle,sort_keys=True); handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        yield metadata
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
            finally:
                handle.close()


def append_chain(path: Path, record: dict[str,Any], state: dict[str,Any], chain_name: str) -> dict[str,Any]:
    chain=state.setdefault('chains',{}).setdefault(chain_name,{'sequence':0,'last_hash':'0'*64})
    sequence=int(chain['sequence'])+1
    body={**record,'sequence':sequence,'previous_hash':chain['last_hash']}
    digest=sha_bytes(chain['last_hash'].encode()+canonical(body))
    sealed={**body,'record_hash':digest}
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a',encoding='utf-8') as handle:
        handle.write(json.dumps(sealed,sort_keys=True,ensure_ascii=False)+'\n'); handle.flush(); os.fsync(handle.fileno())
    chain.update(sequence=sequence,last_hash=digest)
    return sealed


def archive_raw(kind: str, decision_id: str, token: str, raw: bytes) -> tuple[str,str]:
    directory=RAW/kind
    directory.mkdir(parents=True,exist_ok=True)
    safe=''.join(ch for ch in decision_id if ch.isalnum() or ch in '-_')
    path=directory/f'{safe}_{token}.json'
    temp=path.with_suffix(path.suffix+f'.tmp-{uuid.uuid4().hex}')
    with temp.open('wb') as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    os.replace(temp,path)
    return str(path),sha_bytes(raw)


def fetch(url: str) -> dict[str,Any]:
    started=utcnow(); monotonic_start=time.monotonic_ns()
    request=urllib.request.Request(url,headers={'User-Agent':UA})
    with urllib.request.urlopen(request,timeout=30) as response:
        status=response.status; raw=response.read()
    monotonic_end=time.monotonic_ns(); received=utcnow()
    return {'url':url,'started_at':started,'received_at':received,'monotonic_start_ns':monotonic_start,'monotonic_end_ns':monotonic_end,'rtt_ms':round((monotonic_end-monotonic_start)/1_000_000,3),'http_status':status,'raw':raw,'payload':json.loads(raw)}


def provider_time(payload: dict[str,Any]) -> str|None:
    try:
        number=float(payload.get('timestamp'))
        if number>10_000_000_000: number/=1000
        return datetime.fromtimestamp(number,UTC).isoformat(timespec='milliseconds').replace('+00:00','Z')
    except Exception: return None


def book_levels(payload: dict[str,Any], side: str) -> list[tuple[float,float]]:
    levels=[]
    for level in payload.get(side) or []:
        try: levels.append((float(level['price']),float(level['size'])))
        except Exception: continue
    return sorted(levels,key=lambda x:x[0],reverse=(side=='bids'))


def walk(levels: list[tuple[float,float]], quantity: float) -> dict[str,Any]:
    remaining=quantity; gross=0.0; used=[]
    for price,size in levels:
        take=min(remaining,max(0.0,size))
        if take>0:
            used.append({'price':price,'quantity':round(take,8)})
            gross += price*take; remaining -= take
        if remaining<=1e-9: break
    filled=quantity-remaining
    return {'requested_quantity':quantity,'filled_quantity':round(filled,8),'complete':remaining<=1e-9,'gross_notional':round(gross,8),'vwap':None if filled<=0 else round(gross/filled,8),'marginal_price':None if not used else used[-1]['price'],'levels':used}


def fee_for_walk(execution: dict[str,Any], rate: float) -> float:
    total=sum(level['quantity']*rate*level['price']*(1-level['price']) for level in execution['levels'])
    return round(total+1e-12,5)


def candidate_key(row: dict[str,Any]) -> str:
    return '|'.join(str(row.get(key) or '') for key in ('event','handle','bucket','question'))


def lifecycle_id(row: dict[str,Any]) -> str:
    return 'life_'+sha_bytes('|'.join(str(row.get(key) or '') for key in ('event','handle')).encode())[:20]


def provisional_cluster_id(row: dict[str,Any]) -> str:
    return 'cluster_'+sha_bytes(str(row.get('handle') or row.get('event') or '').lower().encode())[:20]


def validate_decision_evidence(row: dict[str,Any], decision_time: str) -> dict[str,Any]:
    problems=[]
    path=Path(str(row.get('decision_book_raw_path') or ''))
    if not path.is_absolute(): path=ROOT/path
    if not path.is_file(): problems.append('missing_raw_decision_book')
    else:
        digest=sha_file(path)
        if digest!=row.get('decision_book_sha256'): problems.append('decision_book_hash_mismatch')
        try:
            payload=json.loads(path.read_text(encoding='utf-8'))
            if str(payload.get('asset_id'))!=str(row.get('yes_token_id')): problems.append('decision_asset_id_mismatch')
            if str(payload.get('market')).lower()!=str(row.get('condition_id')).lower(): problems.append('decision_condition_id_mismatch')
        except Exception: problems.append('invalid_raw_decision_book_json')
    for field in ('book_request_started_at','book_response_received_at','book_provider_timestamp'):
        if not row.get(field): problems.append('missing_'+field)
    rtt=None; staleness=None
    try:
        rtt=(parse_time(row['book_response_received_at'])-parse_time(row['book_request_started_at'])).total_seconds()*1000
        if rtt<0 or rtt>2000: problems.append('decision_book_rtt_out_of_bounds')
    except Exception: pass
    try:
        staleness=(parse_time(decision_time)-parse_time(row['book_provider_timestamp'])).total_seconds()*1000
        if staleness<0 or staleness>2000: problems.append('decision_book_provider_staleness_out_of_bounds')
    except Exception: pass
    if row.get('book_timing_quality')!='exact_request_response': problems.append('decision_timing_not_exact')
    if row.get('decision_book_http_status')!=200: problems.append('decision_book_http_status_not_200')
    return {'eligible':not problems,'problems':sorted(set(problems)),'rtt_ms':None if rtt is None else round(rtt,3),'provider_staleness_ms':None if staleness is None else round(staleness,3),'raw_path':str(path)}


def fee_metadata(condition_id: str, token: str, decision_id: str) -> dict[str,Any]:
    market_url='https://clob.polymarket.com/clob-markets/'+urllib.parse.quote(condition_id,safe='')
    fee_url='https://clob.polymarket.com/fee-rate?'+urllib.parse.urlencode({'token_id':token})
    market=fetch(market_url); fee=fetch(fee_url)
    market_path,market_sha=archive_raw('market_info',decision_id,token,market['raw'])
    fee_path,fee_sha=archive_raw('fee_rate',decision_id,token,fee['raw'])
    payload=market['payload']; fee_payload=fee['payload']; fd=payload.get('fd') or {}
    tokens={str(item.get('t')):item.get('o') for item in payload.get('t') or []}
    problems=[]
    if str(token) not in tokens: problems.append('token_not_in_market_info')
    if str(tokens.get(str(token))).lower()!='yes': problems.append('selected_token_not_yes_outcome')
    if int(payload.get('tbf',-1))!=int(fee_payload.get('base_fee',-2)): problems.append('fee_base_fields_disagree')
    try: rate=float(fd['r'])
    except Exception: rate=None; problems.append('missing_fee_curve_rate')
    if fd.get('e')!=1: problems.append('unsupported_fee_exponent')
    if fd.get('to') is not True: problems.append('fee_curve_not_taker_only')
    try: tick=float(payload['mts']); minimum=float(payload['mos'])
    except Exception: tick=None; minimum=None; problems.append('missing_tick_or_minimum_order')
    return {'eligible':not problems,'problems':problems,'rate':rate,'tick_size':tick,'minimum_order_size':minimum,'taker_delay_ms':250 if payload.get('itode') else 0,'market_info_path':market_path,'market_info_sha256':market_sha,'fee_rate_path':fee_path,'fee_rate_sha256':fee_sha,'market_http_status':market['http_status'],'fee_http_status':fee['http_status'],'fee_descriptor':fd,'taker_base_fee':payload.get('tbf'),'fee_endpoint_base_fee':fee_payload.get('base_fee')}


def risk_cap_quantity(asks: list[tuple[float,float]], fee_rate: float, tick: float, minimum: float) -> tuple[int|None,dict[str,Any]|None,float|None]:
    maximum=min(100,int(math.floor(sum(size for _,size in asks)+1e-9)))
    for quantity in range(maximum,int(math.ceil(minimum))-1,-1):
        execution=walk(asks,float(quantity))
        if not execution['complete']: continue
        fee=fee_for_walk(execution,fee_rate)
        stressed=execution['gross_notional']+fee+tick*quantity
        if stressed<=10+1e-9: return quantity,execution,round(fee,5)
    return None,None,None


def ensure_ledger() -> None:
    if LEDGER.exists(): return
    LEDGER.parent.mkdir(parents=True,exist_ok=True)
    with LEDGER.open('w',newline='',encoding='utf-8') as handle:
        csv.DictWriter(handle,fieldnames=LEDGER_FIELDS).writeheader()


def add_ledger_row(record: dict[str,Any], lock: dict[str,Any]) -> None:
    ensure_ledger()
    row={
        'protocol_id':lock['protocol_id'],'protocol_sha256':lock['protocol_sha256'],'baseline_lock_sha256':lock['lock_sha256'],
        'opportunity_sequence':record['sequence'],'opportunity_id':record['opportunity_id'],'provisional_cluster_id':record['provisional_cluster_id'],
        'lifecycle_id':record['lifecycle_id'],'condition_id':record.get('condition_id'),'yes_token_id':record.get('yes_token_id'),'event':record.get('event'),
        'handle':record.get('handle'),'bucket':record.get('bucket'),'question':record.get('question'),'first_discovered_at':record['recorded_at'],
        'decision_time':record['decision_time'],'selected_by_frozen_v3':record['selected_by_frozen_v3'],'watchdog_filter_version':record['watchdog_filter_version'],
        'decision_book_timing_quality':record.get('decision_book_timing_quality'),'decision_book_raw_path':record.get('decision_book_raw_path'),
        'decision_book_sha256':record.get('decision_book_sha256'),'decision_book_request_started_at':record.get('decision_book_request_started_at'),
        'decision_book_response_received_at':record.get('decision_book_response_received_at'),'decision_book_provider_timestamp':record.get('decision_book_provider_timestamp'),
        'registry_record_hash':record['record_hash']
    }
    with LEDGER.open('a',newline='',encoding='utf-8') as handle:
        writer=csv.DictWriter(handle,fieldnames=LEDGER_FIELDS); writer.writerow(row); handle.flush(); os.fsync(handle.fileno())


def write_status(state: dict[str,Any], lock: dict[str,Any], note: str) -> None:
    positions=state.get('open_positions',{})
    status={
        'schema_version':'xtracker_forward_status_v1','updated_at':utcnow(),'protocol_id':lock['protocol_id'],'activation_utc':lock['activation_utc'],
        'paper_only':True,'live_orders':0,'wallet_or_authentication_used':False,'last_processed_decision_time':state.get('last_processed_decision_time'),
        'registered_opportunities':len(state.get('seen_opportunities',{})),'entry_evaluations':state.get('entry_evaluations',0),
        'open_positions_by_arm':{arm:len(positions.get(arm,{})) for arm in ARMS},'executable_completed_clusters_by_arm':{arm:0 for arm in ARMS},
        'net_capturable_completed_clusters_by_arm':{arm:0 for arm in ARMS},'promotion_gate_passed':False,
        'public_record_net_capturable_markouts':0,'note':note,'chain_heads':state.get('chains',{})
    }
    atomic_json(STATUS,status)


def main() -> int:
    if len(sys.argv)>1 and sys.argv[1]=='status':
        print(STATUS.read_text() if STATUS.exists() else json.dumps({'status':'not_started'})); return 0
    if not LOCK.exists():
        return 0
    lock=load_json(LOCK,{})
    if lock.get('lock_sha256')!=sha_bytes(canonical({k:v for k,v in lock.items() if k!='lock_sha256'})):
        raise SystemExit('invalid baseline lock self-hash')
    if sha_file(PROTOCOL)!=lock.get('protocol_sha256'):
        raise SystemExit('protocol hash mismatch')
    state=load_json(STATE,{'schema_version':'xtracker_forward_state_v1','seen_opportunities':{},'entered_lifecycles':{},'open_positions':{arm:{} for arm in ARMS},'entry_evaluations':0,'chains':{}})
    depth=load_json(DEPTH,{})
    watchdog=load_json(WATCHDOG_STATE,{})
    decision_time=str(watchdog.get('last_run_at') or '')
    depth_generated=str(depth.get('generated_at') or '')
    if not decision_time or not depth_generated:
        write_status(state,lock,'waiting_for_instrumented_depth_run'); return 0
    if parse_time(decision_time)<parse_time(lock['activation_utc']):
        write_status(state,lock,'waiting_for_post_activation_decision'); return 0
    if state.get('last_processed_decision_time')==decision_time:
        write_status(state,lock,'no_new_decision'); return 0
    if abs((parse_time(decision_time)-parse_time(depth_generated)).total_seconds())>120:
        raise SystemExit('depth report and watchdog state are not from the same run window')

    selected_keys=set((watchdog.get('candidates') or {}).keys())
    rows=depth.get('rows') or []
    seen=state.setdefault('seen_opportunities',{})
    selected_rows=[]
    for row in rows:
        condition=str(row.get('condition_id') or '')
        token=str(row.get('yes_token_id') or '')
        opportunity_id='opp_'+sha_bytes((condition+'|'+str(row.get('bucket'))).encode())[:24]
        selected=candidate_key(row) in selected_keys
        if opportunity_id not in seen:
            registered=append_chain(REGISTRY,{
                'record_type':'OPPORTUNITY_REGISTERED','recorded_at':utcnow(),'decision_time':decision_time,'opportunity_id':opportunity_id,
                'provisional_cluster_id':provisional_cluster_id(row),'lifecycle_id':lifecycle_id(row),'condition_id':condition or None,'yes_token_id':token or None,
                'event':row.get('event'),'handle':row.get('handle'),'bucket':row.get('bucket'),'question':row.get('question'),
                'selected_by_frozen_v3':selected,'watchdog_filter_version':watchdog.get('filter_version'),
                'decision_book_timing_quality':row.get('book_timing_quality'),'decision_book_raw_path':row.get('decision_book_raw_path'),
                'decision_book_sha256':row.get('decision_book_sha256'),'decision_book_request_started_at':row.get('book_request_started_at'),
                'decision_book_response_received_at':row.get('book_response_received_at'),'decision_book_provider_timestamp':row.get('book_provider_timestamp'),
                'pre_activation':False,'historical_replay':False
            },state,'registry')
            seen[opportunity_id]={'first_decision_time':decision_time,'lifecycle_id':registered['lifecycle_id'],'condition_id':condition}
            add_ledger_row(registered,lock)
        append_chain(EVENTS,{'record_type':'V3_DECISION','recorded_at':utcnow(),'decision_time':decision_time,'opportunity_id':opportunity_id,'selected_by_frozen_v3':selected,'condition_id':condition or None,'yes_token_id':token or None,'watchdog_filter_note':row.get('watchdog_filter_note'),'depth_error':row.get('depth_error')},state,'events')
        if selected and condition and token:
            selected_rows.append((opportunity_id,row))

    for opportunity_id,row in selected_rows:
        life=lifecycle_id(row)
        if life in state.setdefault('entered_lifecycles',{}):
            continue
        decision_evidence=validate_decision_evidence(row,decision_time)
        decision_rtt=max(0.0,float(decision_evidence.get('rtt_ms') or 0.0))
        target=parse_time(decision_time).timestamp()+max(0.5,decision_rtt/1000)
        if time.time()<target: time.sleep(target-time.time())
        decision_id=decision_time.replace('-','').replace(':','').replace('.','').replace('Z','Z')+'_'+opportunity_id
        token=str(row['yes_token_id']); condition=str(row['condition_id'])
        metadata=fee_metadata(condition,token,decision_id)
        latency_ms=max(500.0,decision_rtt,float(metadata.get('taker_delay_ms') or 0))
        target=parse_time(decision_time).timestamp()+latency_ms/1000
        if time.time()<target: time.sleep(target-time.time())
        fill_url='https://clob.polymarket.com/book?'+urllib.parse.urlencode({'token_id':token})
        fill=fetch(fill_url); fill_path,fill_sha=archive_raw('fill_books',decision_id,token,fill['raw'])
        fill_provider=provider_time(fill['payload'])
        fill_problems=[]
        if fill['http_status']!=200: fill_problems.append('fill_http_status_not_200')
        if fill['rtt_ms']>2000: fill_problems.append('fill_rtt_out_of_bounds')
        try:
            if (parse_time(fill['started_at'])-parse_time(decision_time)).total_seconds()*1000+1e-6<latency_ms: fill_problems.append('fill_request_before_latency')
        except Exception: fill_problems.append('fill_latency_unverifiable')
        try:
            staleness=(parse_time(fill['received_at'])-parse_time(fill_provider)).total_seconds()*1000
            if staleness<0 or staleness>2000: fill_problems.append('fill_provider_staleness_out_of_bounds')
        except Exception: fill_problems.append('fill_provider_staleness_unverifiable')
        if str(fill['payload'].get('asset_id'))!=token: fill_problems.append('fill_asset_id_mismatch')
        if str(fill['payload'].get('market')).lower()!=condition.lower(): fill_problems.append('fill_condition_id_mismatch')
        causal_ok=decision_evidence['eligible'] and metadata['eligible'] and not fill_problems
        asks=book_levels(fill['payload'],'asks')
        full=walk(asks,100.0)
        baseline_fee=fee_for_walk(full,float(metadata['rate'])) if full['complete'] and metadata.get('rate') is not None else None
        risk_qty,risk_execution,risk_fee=(None,None,None)
        if metadata['eligible']:
            risk_qty,risk_execution,risk_fee=risk_cap_quantity(asks,float(metadata['rate']),float(metadata['tick_size']),float(metadata['minimum_order_size']))
        arm_specs={
            'baseline':(100,full,baseline_fee,full['complete']),
            'entry_limit_025':(100,full,baseline_fee,bool(full['complete'] and full['vwap']<=0.25 and full['marginal_price']<=0.25)),
            'event_risk_cap_10usd':(risk_qty,risk_execution,risk_fee,risk_qty is not None),
            'early_drawdown_exit_25pct':(100,full,baseline_fee,full['complete']),
        }
        any_entry=False
        for arm,(quantity,execution,fee,arm_rule_ok) in arm_specs.items():
            eligible=bool(causal_ok and arm_rule_ok and execution)
            record=append_chain(EVENTS,{
                'record_type':'ARM_ENTRY_EVALUATION','recorded_at':utcnow(),'decision_time':decision_time,'paper_order_latency_ms':latency_ms,
                'opportunity_id':opportunity_id,'lifecycle_id':life,'provisional_cluster_id':provisional_cluster_id(row),'arm':arm,
                'condition_id':condition,'yes_token_id':token,'event':row.get('event'),'handle':row.get('handle'),'bucket':row.get('bucket'),
                'decision_evidence':decision_evidence,'fee_metadata':metadata,'fill_book':{'request_started_at':fill['started_at'],'response_received_at':fill['received_at'],'provider_timestamp':fill_provider,'rtt_ms':fill['rtt_ms'],'raw_path':fill_path,'sha256':fill_sha,'problems':fill_problems},
                'requested_quantity':quantity,'execution':execution,'fee_usd':fee,'execution_evidence_eligible':eligible,'net_capturable':False,
                'decision':'PAPER_ENTRY' if eligible else 'NO_TRADE','paper_only':True,'live_order_submitted':False
            },state,'events')
            state['entry_evaluations']=int(state.get('entry_evaluations',0))+1
            if eligible:
                any_entry=True
                state.setdefault('open_positions',{}).setdefault(arm,{})[life]={
                    'opportunity_id':opportunity_id,'condition_id':condition,'yes_token_id':token,'event':row.get('event'),'handle':row.get('handle'),'bucket':row.get('bucket'),
                    'quantity':quantity,'entry_vwap':execution['vwap'],'entry_notional':execution['gross_notional'],'entry_fee':fee,
                    'entry_time':fill['received_at'],'entry_record_hash':record['record_hash'],'provisional_cluster_id':provisional_cluster_id(row)
                }
        state['entered_lifecycles'][life]={'decision_time':decision_time,'opportunity_id':opportunity_id,'any_arm_entered':any_entry}

    state['last_processed_decision_time']=decision_time
    state['updated_at']=utcnow()
    atomic_json(STATE,state)
    write_status(state,lock,'forward_entry_evidence_active; completed-trade promotion counts remain zero until independently captured exits or settlement')
    print(json.dumps({'protocol_id':lock['protocol_id'],'decision_time':decision_time,'rows_registered':len(rows),'selected_rows':len(selected_rows),'registered_total':len(seen),'open_positions_by_arm':{arm:len(state['open_positions'].get(arm,{})) for arm in ARMS},'paper_only':True},sort_keys=True))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
