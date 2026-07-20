#!/usr/bin/env python3
"""Full Gamma + X count edge analysis for Tweet Markets.

No X API calls here; uses saved X count variants and public Gamma event metadata.
Variant used: no_replies, because Polymarket rules say main feed posts/quote posts/reposts count; replies do not.
"""
from __future__ import annotations

import datetime as dt
import json, math, re, urllib.parse, urllib.request
from pathlib import Path

UA='Hermes-Polymarket-FullTweetEdge/0.1'
VARIANT='no_replies'


def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':UA}), timeout=30) as r:
        return json.loads(r.read().decode())


def poisson_cdf(k, lam):
    if k < 0: return 0.0
    if lam > 700: return 0.0 if k < lam else 1.0
    term=math.exp(-lam); s=term
    for i in range(1,k+1):
        term *= lam/i; s += term
    return min(max(s,0.0),1.0)


def pr_range(lo, hi, lam):
    if lo is None and hi is not None: return poisson_cdf(hi, lam)
    if lo is not None and hi is None: return 1 - poisson_cdf(lo-1, lam)
    if lo is not None and hi is not None: return poisson_cdf(hi, lam)-poisson_cdf(lo-1, lam)
    return 0.0


def parse_bucket(s):
    s=(s or '').replace('\\u003c','<').strip()
    if not s: return None
    if s.startswith('<'):
        try: return (s, None, int(s[1:])-1)
        except: return None
    if s.endswith('+'):
        try: return (s, int(s[:-1]), None)
        except: return None
    if '-' in s:
        try:
            a,b=s.split('-',1); return (s,int(a),int(b))
        except: return None
    return None


def parse_token_ids(raw):
    if raw is None: return []
    if isinstance(raw, list): return raw
    if isinstance(raw, str):
        try:
            val=json.loads(raw)
            return val if isinstance(val,list) else []
        except Exception:
            return []
    return []


def fnum(x):
    try:
        if x is None: return None
        return float(x)
    except Exception:
        return None


def market_yes_price(m):
    # Prefer bestAsk for buy simulation, then outcomePrices[0], then last.
    best_ask=fnum(m.get('bestAsk'))
    best_bid=fnum(m.get('bestBid'))
    last=fnum(m.get('lastTradePrice'))
    prices=m.get('outcomePrices')
    yes_mid=None
    if isinstance(prices, str):
        try: prices=json.loads(prices)
        except Exception: prices=[]
    if isinstance(prices, list) and prices:
        yes_mid=fnum(prices[0])
    return {'bestAsk':best_ask,'bestBid':best_bid,'mid_or_outcome':yes_mid,'lastTradePrice':last,'buy_price': best_ask if best_ask is not None else yes_mid}


def main():
    outdir=Path('/data/workspace/polymarket-research/reports')
    variants=json.loads((outdir/'x_tweet_count_variants_latest.json').read_text())
    rows=[]
    for row in variants.get('rows',[]):
        v=row.get('variants',{}).get(VARIANT,{})
        if not v.get('ok'): continue
        slug=row.get('event_slug') or None
        # slug is absent in variants, recover from page events by id.
        rows.append(row)
    page=json.loads((outdir/'tweet_page_events_latest.json').read_text())
    by_id={str(e.get('event_id')): e for e in page.get('events',[])}
    report=[]
    for row in rows:
        ev=by_id.get(str(row.get('event_id')), {})
        slug=ev.get('slug')
        if not slug: continue
        url='https://gamma-api.polymarket.com/events?'+urllib.parse.urlencode({'slug':slug})
        try:
            events=get_json(url)
        except Exception as e:
            report.append({'title':row.get('title'),'error':f'gamma_fetch_failed:{type(e).__name__}:{e}'})
            continue
        if not events: continue
        ge=events[0]
        v=row['variants'][VARIANT]
        count_now=int(v['count_now'])
        projected=float(v['linear_projected_final'])
        lam=max(projected-count_now, 0.0)
        candidates=[]
        for m in ge.get('markets') or []:
            bucket=parse_bucket(m.get('groupItemTitle'))
            if not bucket: continue
            bucket_label, lo, hi=bucket
            add_lo=None if lo is None else max(lo-count_now,0)
            add_hi=None if hi is None else hi-count_now
            if add_hi is not None and add_hi < 0:
                fair=0.0
            else:
                fair=pr_range(add_lo, add_hi, lam)
            px=market_yes_price(m)
            buy=px['buy_price']
            edge=None if buy is None else fair-buy
            tokens=parse_token_ids(m.get('clobTokenIds'))
            candidates.append({
                'market_id':m.get('id'),
                'question':m.get('question'),
                'bucket':bucket_label,
                'count_now':count_now,
                'lambda_remaining':round(lam,2),
                'model_fair':round(fair,4),
                'bestAsk':px['bestAsk'],
                'bestBid':px['bestBid'],
                'outcome_yes':px['mid_or_outcome'],
                'buy_price_used':buy,
                'edge_fair_minus_buy':None if edge is None else round(edge,4),
                'yes_token_id':tokens[0] if tokens else None,
                'condition_id':m.get('conditionId'),
                'active':m.get('active'),
                'closed':m.get('closed'),
            })
        candidates_sorted=sorted(candidates, key=lambda x: x['edge_fair_minus_buy'] if x['edge_fair_minus_buy'] is not None else -9, reverse=True)
        report.append({
            'event_id':ge.get('id'),
            'title':ge.get('title'),
            'slug':slug,
            'description_excerpt':(ge.get('description') or '')[:500],
            'x_variant_used':VARIANT,
            'count_now':count_now,
            'linear_projected_final':projected,
            'lambda_remaining':round(lam,2),
            'markets_count':len(ge.get('markets') or []),
            'top_buy_candidates':candidates_sorted[:8],
            'top_fade_candidates':sorted(candidates, key=lambda x: x['edge_fair_minus_buy'] if x['edge_fair_minus_buy'] is not None else 9)[:8],
        })
    out=outdir/'x_tweet_count_full_gamma_edge_latest.json'
    out.write_text(json.dumps({'source_variants':str(outdir/'x_tweet_count_variants_latest.json'),'variant_used':VARIANT,'events':report},indent=2,ensure_ascii=False))
    top=[]
    for e in report:
        for c in e.get('top_buy_candidates',[])[:3]:
            if c.get('edge_fair_minus_buy') is not None and c['edge_fair_minus_buy']>0.10 and c.get('bestAsk') is not None:
                top.append({'event':e['title'],'count_now':e['count_now'],'projected':e['linear_projected_final'],'bucket':c['bucket'],'ask':c['bestAsk'],'fair':c['model_fair'],'edge':c['edge_fair_minus_buy'],'question':c['question']})
    top=sorted(top,key=lambda x:x['edge'], reverse=True)[:12]
    print(json.dumps({'latest':str(out),'top_actionable_paper_candidates':top},indent=2,ensure_ascii=False))

if __name__=='__main__': main()
