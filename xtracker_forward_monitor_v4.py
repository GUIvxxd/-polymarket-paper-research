#!/usr/bin/env python3
"""Locked v4 paper monitor: fresh decision books, delayed exits, partial fills, settlements."""
from __future__ import annotations
import hashlib,json,math,os,time,urllib.parse,uuid
from datetime import datetime,UTC
from pathlib import Path
from typing import Any

ROOT=Path('/data/workspace/polymarket-research')
PROOF=ROOT/'reports/xtracker_paper_proof_latest.json'
ARMS=('baseline','entry_limit_025','event_risk_cap_10usd','early_drawdown_exit_25pct')

def resolved_final_counts()->dict[str,int]:
    try: proof=json.loads(PROOF.read_text())
    except Exception:return {}
    grouped={}
    for row in proof.get('resolved_entries') or []:
        try: value=int(row['final_count'])
        except Exception:continue
        grouped.setdefault(str(row.get('event') or ''),set()).add(value)
    return {event:next(iter(values)) for event,values in grouped.items() if event and len(values)==1}

def bucket_hit(bucket:str,count:int)->bool|None:
    b=bucket.strip()
    try:
        if b.startswith('<'): return count<int(b[1:])
        if b.endswith('+'): return count>=int(b[:-1])
        if '-' in b:
            lo,hi=b.split('-',1); return int(lo)<=count<=int(hi)
    except Exception:return None
    return None

def archive(core,kind:str,identity:str,token:str,raw:bytes):
    return core.archive_raw(kind,identity,token,raw)

def exact_public_book(core,token:str,condition:str,kind:str,identity:str)->dict[str,Any]:
    url='https://clob.polymarket.com/book?'+urllib.parse.urlencode({'token_id':token})
    result=core.fetch(url); path,digest=archive(core,kind,identity,token,result['raw'])
    provider=core.provider_time(result['payload']); problems=[]
    if result['http_status']!=200:problems.append('http_status_not_200')
    if result['rtt_ms']>2000:problems.append('rtt_out_of_bounds')
    try:
        stale=(core.parse_time(result['received_at'])-core.parse_time(provider)).total_seconds()*1000
        if stale<0 or stale>2000:problems.append('provider_staleness_out_of_bounds')
    except Exception:stale=None;problems.append('provider_staleness_unverifiable')
    if str(result['payload'].get('asset_id'))!=token:problems.append('asset_id_mismatch')
    if str(result['payload'].get('market')).lower()!=condition.lower():problems.append('condition_id_mismatch')
    return {**result,'raw_path':path,'sha256':digest,'provider_timestamp':provider,'provider_staleness_ms':stale,'problems':problems,'eligible':not problems}

def best_rebalance_row(rows:list[dict[str,Any]],held:dict[str,Any])->dict[str,Any]|None:
    candidates=[]
    for row in rows:
        if row.get('event')!=held.get('event') or row.get('bucket')==held.get('bucket'):continue
        try:edge=float(row.get('edge'));fair=float(row.get('fair'));ask=float((row.get('best_ask_book') or [row.get('ask')])[0])
        except Exception:continue
        if edge>=0.50 and fair>=0.70 and ask<=0.25 and not row.get('depth_error'):candidates.append(row)
    return max(candidates,key=lambda r:(float(r.get('edge') or -999),float(r.get('fair') or -999),-float((r.get('best_ask_book') or [999])[0]))) if candidates else None

def exit_reasons(core,arm:str,pos:dict[str,Any],decision_walk:dict[str,Any],held_row:dict[str,Any]|None,better:dict[str,Any]|None)->list[str]:
    if not decision_walk.get('complete') or decision_walk.get('vwap') is None:return []
    bid=float(decision_walk['vwap']);entry=float(pos['entry_vwap']);reasons=[]
    if bid-entry>=0.03:reasons.append('absolute_profit_exit')
    if entry>0 and (bid-entry)/entry>=0.20:reasons.append('relative_profit_exit')
    if held_row is not None:
        try:
            fair=float(held_row.get('fair'))
            if fair<=0.20 and bid-fair>=0.10:reasons.append('stale_bucket_bid_above_model')
        except Exception:pass
    if better is not None and bid>=entry:
        try:
            if float(better.get('edge'))-float((held_row or {}).get('edge'))>=0.10:reasons.append('profitable_better_bucket_available')
        except Exception:pass
    if arm=='early_drawdown_exit_25pct' and bid<=0.75*entry:reasons.append('registered_25pct_drawdown_exit')
    return reasons

def settle_positions(core,state:dict[str,Any],decision_time:str)->int:
    finals=resolved_final_counts()
    if not finals:return 0
    proof_raw=PROOF.read_bytes(); proof_sha=hashlib.sha256(proof_raw).hexdigest(); count=0
    closed=state.setdefault('completed_clusters',{}); pnl=state.setdefault('closed_net_pnl_by_arm',{})
    for arm in ARMS:
        positions=state.setdefault('open_positions',{}).setdefault(arm,{})
        for life,pos in list(positions.items()):
            if pos.get('event') not in finals:continue
            final=finals[pos['event']]; won=bucket_hit(str(pos['bucket']),final)
            if won is None:continue
            if pos.get('tick_size') is None: continue
            qty=float(pos['quantity']);payout=qty if won else 0.0
            leg_net=round(payout-float(pos['entry_notional'])-float(pos.get('entry_fee') or 0),8)
            net=round(float(pos.get('lifecycle_realized_net_pnl_usd') or 0)+leg_net,8)
            stress=round(float(pos.get('lifecycle_realized_stressed_net_pnl_usd') or 0)+leg_net-2*float(pos['tick_size'])*qty,8)
            record=core.append_chain(core.EVENTS,{'record_type':'ARM_SETTLEMENT','recorded_at':core.utcnow(),'decision_time':decision_time,'arm':arm,'lifecycle_id':life,'opportunity_id':pos['opportunity_id'],'event':pos['event'],'bucket':pos['bucket'],'condition_id':pos['condition_id'],'yes_token_id':pos['yes_token_id'],'quantity':qty,'final_count':final,'won':won,'settlement_payout':payout,'entry_notional':pos['entry_notional'],'entry_fee':pos.get('entry_fee'),'lifecycle_net_pnl_usd':net,'lifecycle_stressed_net_pnl_usd':stress,'settlement_proof_path':str(PROOF),'settlement_proof_sha256':proof_sha,'execution_evidence_eligible':True,'net_capturable':True,'paper_only':True,'live_order_submitted':False},state,'events')
            closed.setdefault(arm,{})[life]={'record_hash':record['record_hash'],'net_pnl_usd':net,'stressed_net_pnl_usd':stress,'closed_at':decision_time,'reason':'settlement'}
            pnl[arm]=round(float(pnl.get(arm,0))+net,8);del positions[life];count+=1
    return count

def update_status(core,state:dict[str,Any],lock:dict[str,Any],note:str):
    open_positions=state.get('open_positions',{});completed=state.get('completed_clusters',{})
    status={'schema_version':'xtracker_forward_status_v2','updated_at':core.utcnow(),'protocol_id':lock['protocol_id'],'activation_utc':lock['activation_utc'],'paper_only':True,'live_orders':0,'wallet_or_authentication_used':False,'last_processed_decision_time':state.get('last_processed_decision_time'),'last_monitored_decision_time':state.get('last_monitored_decision_time'),'registered_opportunities':len(state.get('seen_opportunities',{})),'entry_evaluations':state.get('entry_evaluations',0),'open_positions_by_arm':{a:len(open_positions.get(a,{})) for a in ARMS},'executable_completed_clusters_by_arm':{a:len(completed.get(a,{})) for a in ARMS},'net_capturable_completed_clusters_by_arm':{a:len(completed.get(a,{})) for a in ARMS},'promotion_gate_passed':False,'minimum_completed_clusters_per_arm':30,'aggregate_pnl_hidden_until_fixed_end':True,'public_record_net_capturable_markouts':0,'note':note,'chain_heads':state.get('chains',{})}
    core.atomic_json(core.STATUS,status)

def reenter(core,state:dict[str,Any],arm:str,life:str,row:dict[str,Any],decision_time:str,prior_record:str,realized_net:float,realized_stress:float)->bool:
    evidence=core.validate_decision_evidence(row,decision_time)
    if not evidence['eligible']:
        core.append_chain(core.EVENTS,{'record_type':'REBALANCE_ENTRY_EVALUATION','recorded_at':core.utcnow(),'decision_time':decision_time,'arm':arm,'lifecycle_id':life,'condition_id':row.get('condition_id'),'yes_token_id':row.get('yes_token_id'),'decision':'NO_TRADE','problems':evidence['problems'],'prior_exit_record_hash':prior_record,'paper_only':True},state,'events');return False
    token=str(row['yes_token_id']);condition=str(row['condition_id']);identity='rebalance_'+decision_time.replace(':','').replace('-','')+'_'+life+'_'+arm
    metadata=core.fee_metadata(condition,token,identity);latency=max(500,float(evidence.get('rtt_ms') or 0),float(metadata.get('taker_delay_ms') or 0))
    target=core.parse_time(decision_time).timestamp()+latency/1000
    if time.time()<target:time.sleep(target-time.time())
    fill=exact_public_book(core,token,condition,'rebalance_fill_books',identity)
    asks=core.book_levels(fill['payload'],'asks');full=core.walk(asks,100.0);quantity=100;execution=full
    if arm=='event_risk_cap_10usd' and metadata['eligible']:
        quantity,execution,_=core.risk_cap_quantity(asks,float(metadata['rate']),float(metadata['tick_size']),float(metadata['minimum_order_size']))
    rule_ok=bool(execution and execution['complete'])
    if arm=='entry_limit_025':rule_ok=bool(rule_ok and execution['vwap']<=0.25 and execution['marginal_price']<=0.25)
    eligible=bool(evidence['eligible'] and metadata['eligible'] and fill['eligible'] and rule_ok and quantity)
    fee=core.fee_for_walk(execution,float(metadata['rate'])) if eligible else None
    record=core.append_chain(core.EVENTS,{'record_type':'REBALANCE_ENTRY_EVALUATION','recorded_at':core.utcnow(),'decision_time':decision_time,'arm':arm,'lifecycle_id':life,'condition_id':condition,'yes_token_id':token,'bucket':row.get('bucket'),'decision':'PAPER_REBALANCE_ENTRY' if eligible else 'NO_TRADE','quantity':quantity,'execution':execution,'fee_usd':fee,'decision_evidence':evidence,'fee_metadata':metadata,'fill_book':{'request_started_at':fill['started_at'],'response_received_at':fill['received_at'],'provider_timestamp':fill['provider_timestamp'],'raw_path':fill['raw_path'],'sha256':fill['sha256'],'problems':fill['problems']},'execution_evidence_eligible':eligible,'net_capturable':False,'prior_exit_record_hash':prior_record,'paper_only':True,'live_order_submitted':False},state,'events')
    if eligible:
        state['open_positions'][arm][life]={'opportunity_id':'rebalance_'+str(row.get('condition_id')),'condition_id':condition,'yes_token_id':token,'event':row.get('event'),'handle':row.get('handle'),'bucket':row.get('bucket'),'quantity':quantity,'entry_vwap':execution['vwap'],'entry_notional':execution['gross_notional'],'entry_fee':fee,'entry_time':fill['received_at'],'entry_record_hash':record['record_hash'],'provisional_cluster_id':core.provisional_cluster_id(row),'tick_size':metadata['tick_size'],'lifecycle_realized_net_pnl_usd':realized_net,'lifecycle_realized_stressed_net_pnl_usd':realized_stress}
    return eligible

def main(core)->int:
    if not core.LOCK.exists():return 0
    lock=core.load_json(core.LOCK,{});state=core.load_json(core.STATE,{})
    if not state:return 0
    watchdog=core.load_json(core.WATCHDOG_STATE,{});depth=core.load_json(core.DEPTH,{})
    decision_time=str(watchdog.get('last_run_at') or state.get('last_processed_decision_time') or '')
    if not decision_time:return 0
    settled=settle_positions(core,state,decision_time)
    if state.get('last_monitored_decision_time')==decision_time:
        core.atomic_json(core.STATE,state);update_status(core,state,lock,'no_new_monitor_decision');return 0
    rows=depth.get('rows') or [];by_token={str(r.get('yes_token_id')):r for r in rows if r.get('yes_token_id')}
    open_positions=state.setdefault('open_positions',{})
    tokens={str(p['yes_token_id']) for positions in open_positions.values() for p in positions.values() if core.parse_time(decision_time)>core.parse_time(p['entry_time'])}
    for token in sorted(tokens):
        sample=next(p for positions in open_positions.values() for p in positions.values() if str(p['yes_token_id'])==token)
        condition=str(sample['condition_id']);identity='monitor_'+decision_time.replace(':','').replace('-','')+'_'+token
        decision=exact_public_book(core,token,condition,'monitor_decision_books',identity)
        held_row=by_token.get(token);better=best_rebalance_row(rows,sample)
        arm_context=[]
        for arm in ARMS:
            found=next(((life,p) for life,p in open_positions.get(arm,{}).items() if str(p['yes_token_id'])==token),None)
            if not found:continue
            life,pos=found;decision_walk=core.walk(core.book_levels(decision['payload'],'bids'),float(pos['quantity']))
            reasons=exit_reasons(core,arm,pos,decision_walk,held_row,better) if decision['eligible'] else []
            arm_context.append((arm,life,pos,decision_walk,reasons))
            core.append_chain(core.EVENTS,{'record_type':'ARM_MONITOR_DECISION','recorded_at':core.utcnow(),'decision_time':decision_time,'arm':arm,'lifecycle_id':life,'condition_id':condition,'yes_token_id':token,'decision_book':{'request_started_at':decision['started_at'],'response_received_at':decision['received_at'],'provider_timestamp':decision['provider_timestamp'],'rtt_ms':decision['rtt_ms'],'raw_path':decision['raw_path'],'sha256':decision['sha256'],'problems':decision['problems']},'decision_bid_walk':decision_walk,'exit_reasons':reasons,'decision':'EXIT_SIGNAL' if reasons else 'HOLD','paper_only':True},state,'events')
        triggered=[v for v in arm_context if v[4]]
        if not triggered:continue
        metadata=core.fee_metadata(condition,token,identity);latency=max(500,float(decision['rtt_ms']),float(metadata.get('taker_delay_ms') or 0));target=core.parse_time(decision['received_at']).timestamp()+latency/1000
        if time.time()<target:time.sleep(target-time.time())
        fill=exact_public_book(core,token,condition,'exit_fill_books',identity)
        bids=core.book_levels(fill['payload'],'bids')
        for arm,life,pos,decision_walk,reasons in triggered:
            execution=core.walk(bids,float(pos['quantity']));filled=float(execution['filled_quantity']);eligible=bool(decision['eligible'] and metadata['eligible'] and fill['eligible'] and filled>0)
            fee=core.fee_for_walk(execution,float(metadata['rate'])) if eligible else None
            if not eligible:
                core.append_chain(core.EVENTS,{'record_type':'ARM_EXIT_EVALUATION','recorded_at':core.utcnow(),'decision_time':decision_time,'arm':arm,'lifecycle_id':life,'exit_reasons':reasons,'decision':'NO_FILL','execution':execution,'fee_metadata':metadata,'fill_problems':fill['problems'],'paper_only':True},state,'events');continue
            old_qty=float(pos['quantity']);fraction=filled/old_qty;entry_cost=float(pos['entry_notional'])*fraction;entry_fee=float(pos.get('entry_fee') or 0)*fraction;net=round(float(execution['gross_notional'])-float(fee)-entry_cost-entry_fee,8);stress=round(net-2*float(metadata['tick_size'])*filled,8)
            lifecycle_net=round(float(pos.get('lifecycle_realized_net_pnl_usd') or 0)+net,8)
            lifecycle_stress=round(float(pos.get('lifecycle_realized_stressed_net_pnl_usd') or 0)+stress,8)
            record=core.append_chain(core.EVENTS,{'record_type':'ARM_EXIT_FILL','recorded_at':core.utcnow(),'decision_time':decision_time,'arm':arm,'lifecycle_id':life,'opportunity_id':pos['opportunity_id'],'condition_id':condition,'yes_token_id':token,'exit_reasons':reasons,'requested_quantity':old_qty,'execution':execution,'exit_fee_usd':fee,'allocated_entry_cost_usd':entry_cost,'allocated_entry_fee_usd':entry_fee,'net_pnl_usd':net,'stressed_net_pnl_usd':stress,'fill_book':{'request_started_at':fill['started_at'],'response_received_at':fill['received_at'],'provider_timestamp':fill['provider_timestamp'],'raw_path':fill['raw_path'],'sha256':fill['sha256'],'problems':fill['problems']},'execution_evidence_eligible':True,'net_capturable':True,'residual_quantity':round(old_qty-filled,8),'paper_only':True,'live_order_submitted':False},state,'events')
            residual=old_qty-filled
            if residual>1e-8:
                pos['quantity']=round(residual,8);pos['entry_notional']=round(float(pos['entry_notional'])-entry_cost,8);pos['entry_fee']=round(float(pos.get('entry_fee') or 0)-entry_fee,8);pos['lifecycle_realized_net_pnl_usd']=lifecycle_net;pos['lifecycle_realized_stressed_net_pnl_usd']=lifecycle_stress;continue
            del open_positions[arm][life]
            completed=state.setdefault('completed_clusters',{}).setdefault(arm,{})
            completed[life]={'record_hash':record['record_hash'],'net_pnl_usd':lifecycle_net,'stressed_net_pnl_usd':lifecycle_stress,'closed_at':decision_time,'reason':'+'.join(reasons)}
            if 'profitable_better_bucket_available' in reasons and better is not None and reenter(core,state,arm,life,better,decision_time,record['record_hash'],lifecycle_net,lifecycle_stress):
                del completed[life]
            else:
                state.setdefault('closed_net_pnl_by_arm',{})[arm]=round(float(state.setdefault('closed_net_pnl_by_arm',{}).get(arm,0))+lifecycle_net,8)
    state['last_monitored_decision_time']=decision_time;state['updated_at']=core.utcnow();core.atomic_json(core.STATE,state)
    update_status(core,state,lock,f'v4 forward entry/exit/settlement evidence active; settlements_this_tick={settled}; PnL hidden until fixed end')
    return 0
