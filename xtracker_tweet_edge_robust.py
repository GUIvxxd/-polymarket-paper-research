#!/usr/bin/env python3
"""Robust-ish xtracker Tweet Market edge analysis.

Consumes xtracker_tweet_edge_latest.json. Reprices buckets using exact xtracker counts plus
recent hourly rate baselines from xtracker daily stats.
No network/X calls; no live orders.
"""
from __future__ import annotations
import datetime as dt, json, math, statistics
from pathlib import Path

IN=Path('/data/workspace/polymarket-research/reports/xtracker_tweet_edge_latest.json')
OUT=Path('/data/workspace/polymarket-research/reports/xtracker_tweet_edge_robust_latest.json')

# Keep the expensive CLOB depth check focused on candidates that could pass the
# tightened watchdog. Full modeled events are still written under `events`, but
# `top_candidates` no longer gets crowded by very early weekly-window guesses.
MAX_TOP_REMAINING_HOURS = 100.0
EARLY_LOW_BUCKET_REMAINING_HOURS = 48.0


def parse_dt(s): return dt.datetime.fromisoformat(s.replace('Z','+00:00'))

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
    if s.startswith('<'):
        try: return None,int(s[1:])-1
        except: return None
    if s.endswith('+'):
        try: return int(s[:-1]),None
        except: return None
    if '-' in s:
        try:
            a,b=s.split('-',1); return int(a),int(b)
        except: return None
    return None


def low_under_bucket(label):
    parsed=parse_bucket(label)
    if not parsed: return False
    lo,hi=parsed
    return lo is None and hi is not None and hi <= 19


def top_candidate_allowed(platform, meta, edge, ask, remaining, bucket):
    if platform != 'X': return False
    if meta.get('confidence') == 'low': return False
    if edge is None or edge <= 0.10 or ask is None: return False
    if remaining is not None and float(remaining) > MAX_TOP_REMAINING_HOURS: return False
    if low_under_bucket(bucket) and remaining is not None and float(remaining) > EARLY_LOW_BUCKET_REMAINING_HOURS: return False
    return True

def hourly_counts(stats):
    out=[]
    for d in stats.get('daily') or []:
        try:
            out.append((parse_dt(d['date']), int(d.get('count') or 0)))
        except Exception:
            pass
    return sorted(out)

def recent_rate(stats, hours=24):
    rows=hourly_counts(stats)
    if not rows: return None,0
    latest=max(t for t,_ in rows)
    cutoff=latest-dt.timedelta(hours=hours-1e-9)
    vals=[c for t,c in rows if t>=cutoff]
    if not vals: return None,0
    return sum(vals)/min(hours, max(1,len(vals))), sum(vals)

def event_rate(e):
    elapsed=max(float(e.get('elapsed_hours') or 0), 0.01)
    return int(e.get('count_now') or 0)/elapsed


def choose_rate(e, baseline24):
    elapsed=float(e.get('elapsed_hours') or 0)
    r_event=event_rate(e)
    r24, c24=recent_rate(e.get('stats') or {},24)
    notes=[]
    if elapsed < 8:
        # Fresh windows: event rate is unstable; do not treat zero/early pace as an edge.
        rate=baseline24 if baseline24 is not None else (r24 if r24 is not None else r_event)
        notes.append('fresh_window_use_handle_baseline')
    elif r24 is not None:
        # First proof batch over-projected by chasing hot last-24h rates.
        # Make the rate conservative: event-to-date gets most weight, and
        # recent spikes can only lift the rate modestly.
        if r24 > r_event:
            rate=0.75*r_event + 0.25*r24
            notes.append('conservative_event_weighted_rate_spike_capped')
        else:
            rate=0.60*r_event + 0.40*r24
            notes.append('conservative_event_weighted_rate_slowdown_allowed')
    else:
        rate=r_event; notes.append('event_rate_only')
    if baseline24 is not None and elapsed >= 8 and baseline24 < rate:
        # Use handle baseline as a downward shrink/cap only. Do not let another
        # hot overlapping window push the projection higher.
        rate=0.70*rate + 0.30*baseline24
        notes.append('downward_shrunk_to_handle_baseline')
    conf='low' if elapsed<8 else ('medium' if elapsed<36 else 'medium_high')
    return rate, {'event_rate_h':round(r_event,3),'last24_rate_h':None if r24 is None else round(r24,3),'last24_count':c24,'baseline24_rate_h':None if baseline24 is None else round(baseline24,3),'notes':notes,'confidence':conf}


def main():
    data=json.loads(IN.read_text())
    events=[e for e in data['events'] if isinstance(e,dict) and not e.get('error')]
    # Baseline per handle: median 24h rate from established windows.
    by_handle={}
    for e in events:
        if float(e.get('elapsed_hours') or 0)>=24:
            r24,c24=recent_rate(e.get('stats') or {},24)
            if r24 is not None:
                by_handle.setdefault(e['handle'],[]).append(r24)
    baseline={h:statistics.median(v) for h,v in by_handle.items() if v}
    analyzed=[]; top=[]
    for e in events:
        count=int(e.get('count_now') or 0)
        remaining=float(e.get('remaining_hours') or 0)
        rate,meta=choose_rate(e, baseline.get(e['handle']))
        lam=max(rate*remaining,0)
        buckets=[]
        platform=e.get('platform') or 'unknown'
        for b in e.get('buckets') or []:
            parsed=parse_bucket(b.get('bucket'))
            if not parsed: continue
            lo,hi=parsed
            if hi is None and lo is not None and count>=lo:
                fair=1.0; status='locked_yes'
            elif hi is not None and count>hi:
                fair=0.0; status='already_over_bucket'
            else:
                add_lo=None if lo is None else max(lo-count,0)
                add_hi=None if hi is None else hi-count
                fair=0.0 if add_hi is not None and add_hi<0 else pr_range(add_lo,add_hi,lam)
                status='model'
            ask=b.get('bestAsk') if b.get('bestAsk') is not None else b.get('outcome_yes')
            edge=None if ask is None else fair-float(ask)
            nb={**b,'platform':platform,'robust_lambda_remaining':round(lam,2),'robust_projected':round(count+lam,2),'robust_fair':round(fair,4),'robust_edge':None if edge is None else round(edge,4),'status':status,'rate_meta':meta}
            buckets.append(nb)
            # "Actionable" depth candidates are X-only and now avoid fresh
            # weekly windows whose early counts create fake low-bucket edges.
            if top_candidate_allowed(platform, meta, edge, ask, remaining, b['bucket']):
                top.append({'event':e['title'],'platform':platform,'handle':e['handle'],'count':count,'remaining_hours':e['remaining_hours'],'bucket':b['bucket'],'ask':ask,'fair':round(fair,4),'edge':round(edge,4),'status':status,'confidence':meta['confidence'],'projected':round(count+lam,2),'question':b.get('question'),'rate_meta':meta})
        analyzed.append({k:e[k] for k in ['platform','handle','title','tracking_id','count_now','remaining_hours','elapsed_hours','linear_projected_final'] if k in e} | {'baseline24_rate_h':baseline.get(e['handle']),'rate_meta':meta,'top_buy':sorted(buckets,key=lambda x:x['robust_edge'] if x['robust_edge'] is not None else -9, reverse=True)[:10]})
    top=sorted(top,key=lambda x:(x['confidence']!='low', x['edge']), reverse=True)[:25]
    OUT.write_text(json.dumps({'source':str(IN),'baseline24_rates':baseline,'events':analyzed,'top_candidates':top},indent=2,ensure_ascii=False))
    print(json.dumps({'report':str(OUT),'top_candidates':top[:12]},indent=2,ensure_ascii=False))

if __name__=='__main__': main()
