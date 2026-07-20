#!/usr/bin/env python3
"""Depth check robust tweet candidates against Polymarket CLOB books."""
from __future__ import annotations
import hashlib, json, os, urllib.parse, urllib.request, uuid
from datetime import UTC, datetime
from pathlib import Path

RAW=Path('/data/workspace/polymarket-research/reports/xtracker_tweet_edge_latest.json')
ROB=Path('/data/workspace/polymarket-research/reports/xtracker_tweet_edge_robust_latest.json')
OUT=Path('/data/workspace/polymarket-research/reports/xtracker_tweet_depth_latest.json')
RAW_DECISIONS=Path('/data/workspace/polymarket-research/reports/xtracker_forward_validation/raw/decision_books')
UA='Hermes-TweetDepth/0.1'

def utc_now():
    return datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00','Z')

def provider_time(payload):
    value=payload.get('timestamp') if isinstance(payload,dict) else None
    try:
        number=float(value)
        if number>10_000_000_000: number/=1000
        return datetime.fromtimestamp(number,UTC).isoformat(timespec='milliseconds').replace('+00:00','Z')
    except (TypeError,ValueError,OverflowError,OSError):
        return None

def fetch_book(token):
    url='https://clob.polymarket.com/book?'+urllib.parse.urlencode({'token_id':token})
    started=utc_now()
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':UA}), timeout=30) as response:
        status=response.status
        raw=response.read()
    payload=json.loads(raw.decode())
    received=utc_now()
    return payload,raw,started,received,status,url

def archive_raw_book(run_id, token, raw):
    RAW_DECISIONS.mkdir(parents=True,exist_ok=True)
    path=RAW_DECISIONS/f'{run_id}_{token}.json'
    temp=path.with_suffix(path.suffix+f'.tmp-{uuid.uuid4().hex}')
    with temp.open('wb') as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    os.replace(temp,path)
    return str(path),hashlib.sha256(raw).hexdigest()

def main():
    run_started_at=utc_now()
    run_id=run_started_at.replace('-','').replace(':','').replace('.','').replace('Z','Z')
    raw=json.loads(RAW.read_text())
    rob=json.loads(ROB.read_text())
    bucket_map={}
    for e in raw['events']:
        if not isinstance(e,dict) or e.get('error'): continue
        for b in e.get('buckets') or []:
            bucket_map[(e['title'], b['bucket'])]=b
    rows=[]
    for c in rob['top_candidates'][:12]:
        b=bucket_map.get((c['event'], c['bucket']))
        if not b or not b.get('yes_token_id'):
            rows.append({**c,'depth_error':'missing_token'}); continue
        try:
            book,raw_book,request_started_at,response_received_at,http_status,book_endpoint=fetch_book(b['yes_token_id'])
            raw_path,raw_sha256=archive_raw_book(run_id,b['yes_token_id'],raw_book)
            asks=sorted([(float(x['price']),float(x['size'])) for x in book.get('asks',[])], key=lambda x:x[0])
            bids=sorted([(float(x['price']),float(x['size'])) for x in book.get('bids',[])], key=lambda x:x[0], reverse=True)
            caps=[0.06,0.10,0.20,0.50,0.90]
            depth={}
            for cap in caps:
                qty=sum(sz for p,sz in asks if p<=cap)
                cost=sum(p*sz for p,sz in asks if p<=cap)
                depth[str(cap)]={'qty':round(qty,2),'cost':round(cost,2),'avg':None if qty==0 else round(cost/qty,4)}
            rows.append({**c,'yes_token_id':b['yes_token_id'],'condition_id':b.get('condition_id'),'best_ask_book':asks[0] if asks else None,'best_bid_book':bids[0] if bids else None,'ask_depth_by_cap':depth,'top_asks':asks[:8],'top_bids':bids[:8],'book_request_started_at':request_started_at,'book_response_received_at':response_received_at,'book_provider_timestamp':provider_time(book),'book_timing_quality':'exact_request_response','decision_book_raw_path':raw_path,'decision_book_sha256':raw_sha256,'decision_book_endpoint':book_endpoint,'decision_book_http_status':http_status})
        except Exception as e:
            rows.append({**c,'depth_error':f'{type(e).__name__}:{e}'})
    OUT.write_text(json.dumps({'run_started_at':run_started_at,'generated_at':utc_now(),'rows':rows}, indent=2, ensure_ascii=False))
    print(json.dumps({'report':str(OUT),'rows':rows[:8]},indent=2,ensure_ascii=False))
if __name__=='__main__': main()
