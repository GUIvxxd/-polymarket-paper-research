#!/usr/bin/env python3
"""Read-only forensic audit of the seven frozen X rebalance loss legs."""
from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path('/data/workspace/polymarket-research')
FREEZE = ROOT / 'reports/xtracker_strategy_freeze/xtracker_frozen_20260720T151927Z'
FROZEN = FREEZE / 'workspace'
OUT = ROOT / 'reports/xtracker_forensics/xtracker_frozen_20260720T151927Z'
TRADES = FROZEN / 'reports/xtracker_rebalance_paper_trades_latest.csv'
SNAPSHOTS = FROZEN / 'reports/xtracker_tweet_snapshots.jsonl'
SUMMARY = FROZEN / 'reports/xtracker_rebalance_paper_summary_latest.json'
MANIFEST = FREEZE / 'FREEZE_MANIFEST.json'


def fnum(value: Any) -> float | None:
    try:
        if value in (None, ''):
            return None
        return float(value)
    except Exception:
        return None


def inum(value: Any) -> int | None:
    try:
        if value in (None, ''):
            return None
        return int(value)
    except Exception:
        return None


def parse_json_cell(value: str | None) -> list[list[float]]:
    if not value:
        return []
    try:
        raw = json.loads(value)
        return [[float(level[0]), float(level[1])] for level in raw if isinstance(level, list) and len(level) >= 2]
    except Exception:
        return []


def levels_from_row(row: dict[str, Any], side: str) -> list[list[float]]:
    full = row.get(f'top_{side}s')
    if isinstance(full, list):
        output=[]
        for level in full:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                try: output.append([float(level[0]), float(level[1])])
                except Exception: pass
        if output:
            return output
    best = row.get(f'best_{side}_book')
    if isinstance(best, (list, tuple)) and len(best) >= 2:
        try: return [[float(best[0]), float(best[1])]]
        except Exception: return []
    return []


def depth_walk(levels: list[list[float]], quantity: float) -> dict[str, Any]:
    remaining=quantity; cost=0.0; used=[]
    for price, available in levels:
        take=min(remaining, max(0.0, available))
        if take:
            used.append([price,take]); cost += price*take; remaining -= take
        if remaining <= 1e-9: break
    filled=quantity-remaining
    return {'requested':quantity,'filled':round(filled,8),'complete':remaining<=1e-9,'vwap':None if not filled else round(cost/filled,8),'levels_used':used}


def exact_book(row: dict[str, Any]) -> bool:
    return bool(row.get('book_timing_quality') == 'exact_request_response' and row.get('book_request_started_at') and row.get('book_response_received_at'))


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z','+00:00'))


def bucket_gap(bucket: str, final_count: int) -> tuple[str,int]:
    if bucket.startswith('<'):
        high=int(bucket[1:])-1
        return ('inside',0) if final_count <= high else ('above',final_count-high)
    if bucket.endswith('+'):
        low=int(bucket[:-1])
        return ('inside',0) if final_count >= low else ('below',low-final_count)
    low,high=[int(x) for x in bucket.split('-',1)]
    if final_count < low: return 'below',low-final_count
    if final_count > high: return 'above',final_count-high
    return 'inside',0


def verify_freeze() -> dict[str, Any]:
    manifest=json.loads(MANIFEST.read_text(encoding='utf-8'))
    errors=[]; aggregate=hashlib.sha256()
    for record in manifest['files']:
        path=FREEZE/record['frozen_path']
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != record['sha256'] or path.stat().st_size != record['bytes']:
            errors.append(record['frozen_path'])
        aggregate.update(record['frozen_path'].encode()+b'\0'+record['sha256'].encode()+b'\0')
    manifest_hash=hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    expected=(FREEZE/'FREEZE_MANIFEST.sha256').read_text().split()[0]
    return {'ok':not errors and aggregate.hexdigest()==manifest['aggregate_path_hash_sha256'] and manifest_hash==expected,'payload_errors':errors,'manifest_sha256':manifest_hash,'aggregate_match':aggregate.hexdigest()==manifest['aggregate_path_hash_sha256'],'manifest_hash_match':manifest_hash==expected}


def main() -> int:
    freeze_verification=verify_freeze()
    if not freeze_verification['ok']:
        raise SystemExit('frozen evidence verification failed')
    trades=list(csv.DictReader(TRADES.open(newline='',encoding='utf-8')))
    closed=[row for row in trades if row.get('exit_reason')]
    losses=[row for row in closed if (fnum(row.get('paper_pnl')) or 0.0) < 0]
    if len(losses) != 7:
        raise SystemExit(f'expected seven loss legs, found {len(losses)}')

    by_token={str(row['yes_token_id']):[] for row in losses}
    snapshot_records=0
    with SNAPSHOTS.open(encoding='utf-8') as handle:
        for line in handle:
            if not line.strip(): continue
            payload=json.loads(line); snapshot_records += 1
            generated=payload.get('generated_at')
            for row in payload.get('rows') or []:
                token=str(row.get('yes_token_id') or '')
                if token in by_token:
                    by_token[token].append({'generated_at':generated,**row})

    details=[]
    for row in losses:
        entry_price=float(row['entry_price']); qty=float(row.get('quantity') or 100)
        entry_levels=parse_json_cell(row.get('entry_book_asks'))
        entry_walk=depth_walk(entry_levels,qty)
        entry_exact=bool(row.get('entry_book_timing_quality')=='exact_request_response' and row.get('entry_book_request_started_at') and row.get('entry_book_response_received_at'))
        observations=[obs for obs in by_token[str(row['yes_token_id'])] if obs.get('generated_at') and parse_time(str(obs['generated_at'])) > parse_time(row['entry_time'])]
        exact_observations=[obs for obs in observations if exact_book(obs)]
        exact_bid_complete=[]
        for obs in exact_observations:
            walk=depth_walk(levels_from_row(obs,'bid'),qty)
            if walk['complete']:
                exact_bid_complete.append({'generated_at':obs['generated_at'],'request_started_at':obs.get('book_request_started_at'),'response_received_at':obs.get('book_response_received_at'),'provider_timestamp':obs.get('book_provider_timestamp'),'bid_vwap_100':walk['vwap']})
        final_count=int(row['final_count']); direction,gap=bucket_gap(row['bucket'],final_count)
        details.append({
            'position_id':int(row['position_id']), 'event':row['event'], 'handle':row['handle'], 'bucket':row['bucket'], 'source':row['source'],
            'entry_time':row['entry_time'], 'entry_price':entry_price, 'entry_fair':fnum(row.get('entry_fair')), 'entry_edge':fnum(row.get('entry_edge')),
            'entry_count':inum(row.get('entry_count')), 'final_count':final_count, 'outcome_miss_direction':direction, 'outcome_miss_by_posts':gap,
            'stored_paper_pnl_100_shares':float(row['paper_pnl']), 'entry_timing_quality':row.get('entry_book_timing_quality'),
            'entry_request_started_at':row.get('entry_book_request_started_at') or None, 'entry_response_received_at':row.get('entry_book_response_received_at') or None,
            'entry_exact_request_response':entry_exact, 'entry_displayed_depth_walk_100':entry_walk,
            'post_entry_matching_snapshot_rows':len(observations), 'post_entry_exact_book_rows':len(exact_observations),
            'post_entry_exact_bid_books_covering_100':len(exact_bid_complete), 'first_post_entry_exact_bid_book_covering_100':exact_bid_complete[0] if exact_bid_complete else None,
            'strict_causal_entry_fill_available':False,
            'strict_round_trip_eligible':False,
            'strict_ineligibility_reason':'No separately captured post-decision/post-latency entry fill book; historical entry is snapshot_timestamp_only.' if not entry_exact else 'Decision-book capture is not a separately captured post-decision/post-latency fill book.',
        })

    source_summary=json.loads(SUMMARY.read_text(encoding='utf-8'))
    exact_entry_loss_count=sum(item['entry_exact_request_response'] for item in details)
    top_depth_count=sum(item['entry_displayed_depth_walk_100']['complete'] for item in details)
    total_loss=round(sum(item['stored_paper_pnl_100_shares'] for item in details),2)
    strict_all_closed=sum(str(row.get('execution_evidence_eligible')).lower()=='true' for row in closed)
    decision_book_roundtrips=sum(
        row.get('entry_book_timing_quality')=='exact_request_response'
        and row.get('exit_book_timing_quality')=='exact_request_response'
        and bool(row.get('entry_book_request_started_at')) and bool(row.get('exit_book_request_started_at'))
        for row in closed
    )
    result={
        'schema_version':'xtracker_seven_loss_forensic_v1',
        'generated_at':datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00','Z'),
        'freeze_id':FREEZE.name,
        'freeze_verification':freeze_verification,
        'paper_only':True,
        'live_orders':0,
        'wallet_or_authentication_used':False,
        'frozen_report_generated_at':source_summary.get('generated_at'),
        'baseline_integrity':{
            'operational_watchdog_filter':'consensus_v3_2026_07_16',
            'historical_ledger_filter':source_summary.get('rules',{}).get('entry',{}).get('filter_version'),
            'same_strategy':False,
            'consequence':'The seven-loss ledger is a historical v2 replay, not a clean performance sample of the current operational v3 watchdog.'
        },
        'loss_summary':{
            'loss_legs':len(details),
            'conservative_independent_clusters':5,
            'cluster_note':'Ted Cruz July 3-10 and July 7-14 overlap into one account/time cluster; NYC Mayor July 7-14 and July 10-17 overlap into one; the other three loss legs are separate clusters.',
            'stored_fixed_100_share_pnl':total_loss,
            'exact_request_response_entries':exact_entry_loss_count,
            'entry_books_with_displayed_100_share_depth_ignoring_timing':top_depth_count,
            'strict_causal_entry_fills':0,
            'strict_executable_round_trips':0,
        },
        'whole_closed_sample_evidence':{
            'closed_legs':len(closed),
            'frozen_report_execution_evidence_eligible_legs':strict_all_closed,
            'entry_and_exit_decision_books_with_exact_request_response':decision_book_roundtrips,
            'strict_post_decision_post_latency_round_trips':0,
            'warning':'An exact decision-book timestamp is not a causal paper fill; the frozen schema has no separately captured post-decision/post-latency entry fill book.'
        },
        'registered_policy_tests':[
            {'policy':'entry_limit_0.25','historical_executable_completed_clusters':0,'net_expectancy':None,'verdict':'NOT TESTABLE on frozen sample; no strict causal entry fills. Do not use stored marks to claim improvement.'},
            {'policy':'event_risk_cap_10usd','historical_executable_completed_clusters':0,'net_expectancy':None,'verdict':'NOT TESTABLE on frozen sample; reduced sizing cannot repair missing causal entry evidence.'},
            {'policy':'earlier_exit_drawdown_25pct','historical_executable_completed_clusters':0,'net_expectancy':None,'verdict':'NOT TESTABLE on frozen sample; later books cannot retroactively validate a missing entry fill.'},
        ],
        'forensic_findings':[
            'All seven outcomes genuinely settled outside the selected bucket, but none has a timing-valid causal entry fill.',
            'Only three of seven stored entry books show at least 100 shares across the captured levels; four assumed fills beyond captured displayed quantity.',
            'The seven loss legs collapse to five conservative independent account/time clusters.',
            'Four losing entries carried model fair values above 0.73; two exceeded 0.95, showing severe historical overconfidence, not executable calibration evidence.',
            'One loss was a rebalance leg (White House 140-159) after a zero-PnL exit from 120-139; the final count 227 missed the switched bucket by 68.',
            'No entry-price limit, exposure cap, or earlier-exit rule earns historical expectancy credit from this evidence.'
        ],
        'losses':details,
    }
    OUT.mkdir(parents=True,exist_ok=True)
    json_path=OUT/'seven_loss_forensic.json'
    json_path.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    csv_path=OUT/'seven_loss_forensic.csv'
    columns=['position_id','event','handle','bucket','source','entry_time','entry_price','entry_fair','entry_edge','entry_count','final_count','outcome_miss_direction','outcome_miss_by_posts','stored_paper_pnl_100_shares','entry_timing_quality','entry_exact_request_response','entry_displayed_depth_walk_100_complete','entry_displayed_depth_walk_100_filled','post_entry_matching_snapshot_rows','post_entry_exact_book_rows','post_entry_exact_bid_books_covering_100','strict_round_trip_eligible','strict_ineligibility_reason']
    with csv_path.open('w',newline='',encoding='utf-8') as handle:
        writer=csv.DictWriter(handle,fieldnames=columns); writer.writeheader()
        for item in details:
            flat={k:item.get(k) for k in columns}
            flat['entry_displayed_depth_walk_100_complete']=item['entry_displayed_depth_walk_100']['complete']
            flat['entry_displayed_depth_walk_100_filled']=item['entry_displayed_depth_walk_100']['filled']
            writer.writerow(flat)
    lines=[
        '# X strategy seven-loss forensic review','',
        f"- Frozen evidence: `{FREEZE}`",
        f"- Freeze manifest SHA-256: `{freeze_verification['manifest_sha256']}`",
        f"- Frozen ledger cutoff: `{source_summary.get('generated_at')}`",
        '- Paper only; no wallet, authentication, or orders.','',
        '## Verdict','',
        '**The losses are real settlement misses, but the frozen sample cannot estimate executable expectancy.** All seven entries are missing a causal post-decision fill book; zero loss legs and zero completed historical legs pass the strict round-trip standard. Entry limits, risk caps, and earlier exits therefore remain untested—not failed and not validated.','',
        '## Evidence defects','',
        f"- Historical loss legs: `{len(details)}`; conservative independent clusters: `5`; stored 100-share loss: `${abs(total_loss):.2f}`.",
        f"- Exact request/response entry books among losses: `{exact_entry_loss_count}`.",
        f"- Captured entry depth sufficient for 100 shares ignoring timing: `{top_depth_count}/7`.",
        f"- Frozen report execution-eligible completed legs: `{strict_all_closed}`.",
        f"- Completed rows with exact decision-book timestamps on both sides: `{decision_book_roundtrips}`; strict post-latency causal round trips: `0`.",
        '- Operating strategy mismatch: watchdog `consensus_v3_2026_07_16`; historical ledger `tightened_v2_2026_07_15`.','',
        '## Seven loss legs','',
        '| ID | Event | Bucket | Entry | Fair | Final | Miss | Stored P&L | Entry timing | 100-share depth |',
        '|---:|---|---:|---:|---:|---:|---:|---:|---|---|',
    ]
    for item in details:
        lines.append(f"| {item['position_id']} | {item['event']} | {item['bucket']} | ${item['entry_price']:.3f} | {item['entry_fair']:.4f} | {item['final_count']} | {item['outcome_miss_direction']} {item['outcome_miss_by_posts']} | ${item['stored_paper_pnl_100_shares']:.2f} | {item['entry_timing_quality']} | {'yes' if item['entry_displayed_depth_walk_100']['complete'] else 'no'} |")
    lines += ['', '## Policy tests','', '| Candidate | Eligible historical clusters | Net expectancy | Decision |','|---|---:|---:|---|']
    for policy in result['registered_policy_tests']:
        lines.append(f"| `{policy['policy']}` | {policy['historical_executable_completed_clusters']} | n/a | {policy['verdict']} |")
    lines += ['', '## Hard forward gate','', 'Do not alter the operational X rules from this review. Activate a candidate only after one coherent baseline is frozen and after the collector records separate decision and post-latency fill books. Promotion requires at least 30 independent executable, net-capturable completed clusters per arm, positive aggregate net P&L, positive cluster-level expectancy, positive paired uplift, and the preregistered corrected confidence bound above zero.','']
    md_path=OUT/'seven_loss_forensic.md'; md_path.write_text('\n'.join(lines),encoding='utf-8')
    output_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (json_path,csv_path,md_path)}
    (OUT/'OUTPUT_SHA256.json').write_text(json.dumps(output_hashes,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    print(json.dumps({'output_dir':str(OUT),'loss_legs':len(details),'stored_loss':total_loss,'exact_loss_entries':exact_entry_loss_count,'depth_complete_entries':top_depth_count,'strict_round_trips':0,'decision_book_roundtrips_all_closed':decision_book_roundtrips,'hashes':output_hashes},indent=2))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
