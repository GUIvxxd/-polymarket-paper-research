#!/usr/bin/env python3
"""Exact-ish xtracker-based Tweet Market scanner.

Uses Polymarket's public resolver source:
  https://xtracker.polymarket.com/api/users/{handle}/posts?startDate=...&endDate=...
  https://xtracker.polymarket.com/api/trackings/{id}?includeStats=true

No X API credits used. No live orders. Public read-only data only.
"""
from __future__ import annotations

import datetime as dt
import json, math, re, urllib.parse, urllib.request
from pathlib import Path

UA='Hermes-XTracker-Polymarket-Scanner/0.1'
BASE='https://xtracker.polymarket.com'
GAMMA='https://gamma-api.polymarket.com'
# Fallback only. Normal mode discovers all tracked users from xtracker /api/users,
# which includes X plus related resolver sources such as Truth Social.
FALLBACK_HANDLES=['elonmusk','ZelenskyyUa','tedcruz','WhiteHouse','khamenei_ir','cz_binance','NYCMayor']
OUTDIR=Path('/data/workspace/polymarket-research/reports')


def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':UA}), timeout=30) as r:
        return json.loads(r.read().decode())


def parse_dt(s):
    if not s: return None
    return dt.datetime.fromisoformat(s.replace('Z','+00:00'))


def poisson_cdf(k, lam):
    if k < 0: return 0.0
    term=math.exp(-lam); s=term
    for i in range(1,k+1):
        term *= lam/i; s += term
    return min(max(s,0.0),1.0)


def pr_range(lo, hi, lam):
    if lo is None and hi is not None: return poisson_cdf(hi,lam)
    if lo is not None and hi is None: return 1-poisson_cdf(lo-1,lam)
    if lo is not None and hi is not None: return poisson_cdf(hi,lam)-poisson_cdf(lo-1,lam)
    return 0.0


def parse_bucket(label):
    s=(label or '').replace('\\u003c','<').strip()
    if not s: return None
    if s.startswith('<'):
        try: return (s,None,int(s[1:])-1)
        except Exception: return None
    if s.endswith('+'):
        try: return (s,int(s[:-1]),None)
        except Exception: return None
    if '-' in s:
        try:
            a,b=s.split('-',1); return (s,int(a),int(b))
        except Exception: return None
    return None


def fnum(x):
    try:
        if x is None: return None
        return float(x)
    except Exception: return None


def parse_jsonish(raw):
    if isinstance(raw, list): return raw
    if isinstance(raw, str):
        try: return json.loads(raw)
        except Exception: return []
    return []


def market_price(m):
    best_ask=fnum(m.get('bestAsk'))
    best_bid=fnum(m.get('bestBid'))
    prices=parse_jsonish(m.get('outcomePrices'))
    yes_mid=fnum(prices[0]) if prices else None
    return best_ask if best_ask is not None else yes_mid, best_ask, best_bid, yes_mid


def gamma_event_from_market_link(link):
    if not link or '/event/' not in link: return None
    slug=link.rsplit('/event/',1)[1].strip('/').split('?')[0]
    url=GAMMA+'/events?'+urllib.parse.urlencode({'slug':slug})
    data=get_json(url)
    return data[0] if data else None


def analyze_tracking(handle, user, tr):
    start=parse_dt(tr['startDate']); end=parse_dt(tr.get('endDate'))
    if not start or not end: return None
    now=dt.datetime.now(dt.timezone.utc)
    params={'startDate':tr['startDate'],'endDate':tr['endDate']}
    posts_url=f'{BASE}/api/users/{urllib.parse.quote(handle)}/posts?'+urllib.parse.urlencode(params)
    posts_resp=get_json(posts_url)
    posts=posts_resp.get('data',[]) if posts_resp.get('success') else []
    detail=get_json(f'{BASE}/api/trackings/{tr["id"]}?includeStats=true')
    stats=detail.get('data',{}).get('stats',{}) if detail.get('success') else {}
    count=int(stats.get('total') or len(posts))
    elapsed=max((min(now,end)-start).total_seconds(),1)
    total=max((end-start).total_seconds(),1)
    remaining=max((end-now).total_seconds(),0)
    rate=count/elapsed
    linear_projected=count + rate*remaining
    # Prefer xtracker's own pace because it is the market's resolver source and
    # smooths/rounds progress the same way their UI does. Fall back to our raw
    # elapsed-rate projection only if pace is absent.
    projected=float(stats.get('pace') or linear_projected)
    lam=max(projected-count,0)
    ge=gamma_event_from_market_link(tr.get('marketLink'))
    if not ge: return {'handle':handle,'title':tr.get('title'),'error':'gamma_event_not_found','count':count}
    buckets=[]
    for m in ge.get('markets') or []:
        b=parse_bucket(m.get('groupItemTitle'))
        if not b: continue
        label, lo, hi=b
        add_lo=None if lo is None else max(lo-count,0)
        add_hi=None if hi is None else hi-count
        fair=0.0 if (add_hi is not None and add_hi < 0) else pr_range(add_lo,add_hi,lam)
        buy,bask,bbid,mid=market_price(m)
        edge=None if buy is None else fair-buy
        tokens=parse_jsonish(m.get('clobTokenIds'))
        buckets.append({
            'bucket':label,
            'question':m.get('question'),
            'market_id':m.get('id'),
            'count_now':count,
            'model_projected':round(projected,2),
            'lambda_remaining':round(lam,2),
            'fair':round(fair,4),
            'bestAsk':bask,
            'bestBid':bbid,
            'outcome_yes':mid,
            'buy_price_used':buy,
            'edge':None if edge is None else round(edge,4),
            'yes_token_id':tokens[0] if tokens else None,
            'condition_id':m.get('conditionId'),
            'active':m.get('active'), 'closed':m.get('closed')
        })
    return {
        'handle':handle,
        'user_last_sync':user.get('lastSync'),
        'tracking_id':tr.get('id'),
        'title':tr.get('title'),
        'marketLink':tr.get('marketLink'),
        'startDate':tr.get('startDate'),
        'endDate':tr.get('endDate'),
        'count_now':count,
        'posts_len':len(posts),
        'stats':stats,
        'elapsed_hours':round(elapsed/3600,2),
        'remaining_hours':round(remaining/3600,2),
        'linear_projected_final':round(projected,2),
        'gamma_slug':ge.get('slug'),
        'description_excerpt':(ge.get('description') or '')[:700],
        'top_buy':sorted(buckets,key=lambda x:x['edge'] if x['edge'] is not None else -9, reverse=True)[:10],
        'top_fade':sorted(buckets,key=lambda x:x['edge'] if x['edge'] is not None else 9)[:10],
        'buckets':buckets,
    }


def discover_users():
    """Return tracked resolver users from xtracker.

    The `/api/users` endpoint already includes current/future trackings and platform
    metadata. Fall back to legacy handle fetches if the endpoint shape changes.
    """
    try:
        resp=get_json(f'{BASE}/api/users')
        users=resp.get('data') or []
        if resp.get('success') and isinstance(users,list) and users:
            return users
    except Exception:
        pass
    out=[]
    for handle in FALLBACK_HANDLES:
        try:
            user_resp=get_json(f'{BASE}/api/users/{urllib.parse.quote(handle)}')
            if user_resp.get('success'):
                out.append(user_resp['data'])
        except Exception:
            continue
    return out


def main():
    OUTDIR.mkdir(parents=True,exist_ok=True)
    events=[]
    users=discover_users()
    for user in users:
        handle=user.get('handle')
        if not handle:
            continue
        platform=user.get('platform') or 'unknown'
        # Only active/near-current trackings; include just-starting future windows
        # so new markets enter the research loop before the count begins.
        for tr in user.get('trackings',[]):
            if not tr.get('isActive'): continue
            start=parse_dt(tr['startDate']); end=parse_dt(tr.get('endDate'))
            now=dt.datetime.now(dt.timezone.utc)
            if not start or not end or start > now+dt.timedelta(hours=48) or end < now-dt.timedelta(hours=6):
                continue
            try:
                row=analyze_tracking(handle,user,tr)
                if isinstance(row,dict):
                    row['platform']=platform
                    row['user_name']=user.get('name')
                events.append(row)
            except Exception as e:
                events.append({'handle':handle,'platform':platform,'title':tr.get('title'),'error':f'analyze:{type(e).__name__}:{e}'})
    out={'generated_at':dt.datetime.now(dt.timezone.utc).isoformat(), 'source':'xtracker.polymarket.com public API + Gamma public API', 'discovered_users':len(users), 'events':events}
    (OUTDIR/'xtracker_tweet_edge_latest.json').write_text(json.dumps(out,indent=2,ensure_ascii=False))
    actionable=[]
    for e in events:
        if not isinstance(e,dict) or e.get('error'): continue
        for b in e.get('top_buy',[])[:5]:
            if b.get('edge') is not None and b['edge']>0.10 and b.get('bestAsk') is not None:
                # Avoid nearly impossible stale tails; still include for review.
                actionable.append({'event':e['title'],'handle':e['handle'],'count':e['count_now'],'remaining_hours':e['remaining_hours'],'projected':e['linear_projected_final'],'bucket':b['bucket'],'ask':b['bestAsk'],'fair':b['fair'],'edge':b['edge'],'question':b['question']})
    actionable=sorted(actionable,key=lambda x:x['edge'], reverse=True)[:20]
    print(json.dumps({'report':str(OUTDIR/'xtracker_tweet_edge_latest.json'),'actionable_candidates':actionable},indent=2,ensure_ascii=False))

if __name__=='__main__': main()
