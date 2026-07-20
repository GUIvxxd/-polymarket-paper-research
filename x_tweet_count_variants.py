#!/usr/bin/env python3
"""Count-variant research for Polymarket Tweet Markets.

Uses X app-only bearer generated in memory from X_API_KEY/X_API_SECRET.
No secret printing/storage. Read-only counts endpoint.
"""
from __future__ import annotations

import base64, csv, datetime as dt, json, os, re, urllib.error, urllib.parse, urllib.request
from pathlib import Path

TOKEN_URL = "https://api.x.com/oauth2/token"
COUNTS_URL = "https://api.x.com/2/tweets/counts/recent"
UA = "Hermes-Polymarket-X-Variant-Counts/0.1"
TARGETS = {
    "Elon Musk": "elonmusk",
    "White House": "WhiteHouse",
    "Zelenskyy": "ZelenskyyUa",
    "Khamenei": "khamenei_ir",
    "Ted Cruz": "tedcruz",
    "CZ": "cz_binance",
}
MONTHS = {"January":1,"February":2,"March":3,"April":4,"May":5,"June":6,"July":7,"August":8,"September":9,"October":10,"November":11,"December":12}
VARIANTS = {
    "all_from_account": "from:{u}",
    "no_retweets": "from:{u} -is:retweet",
    "no_replies": "from:{u} -is:reply",
    "original_only": "from:{u} -is:retweet -is:reply",
}
BUCKET_RE = re.compile(r"^(?:(?:<|\\u003c)(?P<lt>\d+)|(?P<lo>\d+)-(?P<hi>\d+)|(?P<plus>\d+)\+)$")


def bearer():
    key, secret = os.environ.get("X_API_KEY"), os.environ.get("X_API_SECRET")
    if not key or not secret:
        return None, {"ok": False, "error": "missing X_API_KEY/X_API_SECRET"}
    auth = base64.b64encode(f"{urllib.parse.quote(key)}:{urllib.parse.quote(secret)}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode({"grant_type":"client_credentials"}).encode(), headers={"Authorization":f"Basic {auth}","Content-Type":"application/x-www-form-urlencoded;charset=UTF-8","User-Agent":UA}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data=json.loads(r.read().decode())
        return data.get("access_token"), {"ok": bool(data.get("access_token")), "status": 200}
    except urllib.error.HTTPError as e:
        body=e.read().decode("utf-8","replace")[:600]
        try: err=json.loads(body)
        except Exception: err={"raw": body}
        return None, {"ok": False, "status": e.code, "error": err}


def get_count(token, query, start, end):
    params={"query":query,"start_time":start,"end_time":end,"granularity":"day"}
    req=urllib.request.Request(COUNTS_URL+"?"+urllib.parse.urlencode(params), headers={"Authorization":f"Bearer {token}","User-Agent":UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data=json.loads(r.read().decode())
        return {"ok": True, "status": r.status, "count": int(data.get("meta",{}).get("total_tweet_count",0)), "buckets": data.get("data",[])}
    except urllib.error.HTTPError as e:
        body=e.read().decode("utf-8","replace")[:600]
        try: err=json.loads(body)
        except Exception: err={"raw": body}
        return {"ok": False, "status": e.code, "error": err}


def target(title):
    for display,u in TARGETS.items():
        if title.startswith(display + " #"):
            return u
    return None


def parse_window(title, end_date):
    m=re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\s+-\s+(?:(January|February|March|April|May|June|July|August|September|October|November|December)\s+)?(\d{1,2}),\s+(\d{4})", title)
    if not m: return None
    m1,d1,m2,d2,y=m.groups(); y=int(y); m1n=MONTHS[m1]; m2n=MONTHS[m2] if m2 else m1n
    start=dt.datetime(y,m1n,int(d1),0,0,tzinfo=dt.UTC)
    if end_date:
        try: end=dt.datetime.fromisoformat(end_date.replace("Z","+00:00")).astimezone(dt.UTC)
        except Exception: end=dt.datetime(y,m2n,int(d2),23,59,tzinfo=dt.UTC)
    else:
        end=dt.datetime(y,m2n,int(d2),23,59,tzinfo=dt.UTC)
    return start,end


def clamp(start,end):
    now=dt.datetime.now(dt.UTC); earliest=now-dt.timedelta(days=6,hours=23,minutes=50); safe=now-dt.timedelta(seconds=30)
    return max(start,earliest), min(end,safe), now


def parse_prices(s):
    out=[]
    for part in (s or '').split('|'):
        if '=' not in part: continue
        k,v=part.strip().split('=',1)
        try: out.append((k.strip().replace('\\u003c','<'), float(v)))
        except Exception: pass
    return out


def in_bucket(bucket,x):
    m=BUCKET_RE.match(bucket.replace('\\u003c','<'))
    if not m: return False
    if m.group('lt'): return x < int(m.group('lt'))
    if m.group('plus'): return x >= int(m.group('plus'))
    return int(m.group('lo')) <= x <= int(m.group('hi'))


def project(count,start,now,end):
    elapsed=max((now-start).total_seconds()/3600, 0.01); total=max((end-start).total_seconds()/3600,0.01)
    return count/elapsed*total


def main():
    outdir=Path('/data/workspace/polymarket-research/reports'); outdir.mkdir(parents=True, exist_ok=True)
    events=json.loads((outdir/'tweet_page_events_latest.json').read_text()).get('events',[])
    token, tokstatus=bearer()
    result={"retrieved_at_utc":dt.datetime.now(dt.UTC).isoformat().replace('+00:00','Z'),"bearer_status":tokstatus,"rows":[],"estimated_credit_spend_usd":0.0,"notes":[]}
    if not token:
        (outdir/'x_tweet_count_variants_latest.json').write_text(json.dumps(result,indent=2))
        print(json.dumps({"bearer_ok":False,"status":tokstatus},indent=2)); return
    calls=0
    for ev in events:
        title=ev.get('title',''); u=target(title)
        if not u: continue
        win=parse_window(title, ev.get('end_date'))
        if not win: continue
        full_start, full_end=win; start,end,now=clamp(full_start, full_end)
        if start>=end:
            continue
        prices=parse_prices(ev.get('prices'))
        row={"event_id":ev.get('event_id'),"title":title,"x_username":u,"full_start_utc":full_start.isoformat().replace('+00:00','Z'),"full_end_utc":full_end.isoformat().replace('+00:00','Z'),"count_start_utc":start.isoformat().replace('+00:00','Z'),"count_end_utc":end.isoformat().replace('+00:00','Z'),"market_leading_group":ev.get('leading_group'),"market_leading_yes":ev.get('leading_yes'),"market_prices":ev.get('prices'),"variants":{}}
        for name,tmpl in VARIANTS.items():
            query=tmpl.format(u=u)
            resp=get_count(token, query, row['count_start_utc'], row['count_end_utc']); calls += 1
            v={"query":query,"ok":resp.get('ok'),"status":resp.get('status')}
            if resp.get('ok'):
                cnt=resp['count']; proj=project(cnt, full_start, now, full_end)
                matches=[{"bucket":b,"yes_price":p} for b,p in prices if in_bucket(b,proj)]
                current_matches=[{"bucket":b,"yes_price":p} for b,p in prices if in_bucket(b,cnt)]
                v.update({"count_now":cnt,"linear_projected_final":round(proj,2),"current_bucket_prices":current_matches,"projected_bucket_prices":matches})
            else:
                v['error']=resp.get('error')
            row['variants'][name]=v
        result['rows'].append(row)
        if calls >= 44:
            result['notes'].append('Stopped after 44 count calls to conserve credits.')
            break
    result['estimated_credit_spend_usd']=round(calls*0.005,4)
    json_path=outdir/'x_tweet_count_variants_latest.json'; json_path.write_text(json.dumps(result,indent=2,ensure_ascii=False))
    csv_path=outdir/'x_tweet_count_variants_latest.csv'
    with csv_path.open('w',newline='') as f:
        w=csv.writer(f); w.writerow(['title','variant','count_now','linear_projected_final','market_leading_group','market_leading_yes','projected_bucket_prices'])
        for r in result['rows']:
            for name,v in r['variants'].items():
                w.writerow([r['title'],name,v.get('count_now'),v.get('linear_projected_final'),r.get('market_leading_group'),r.get('market_leading_yes'),json.dumps(v.get('projected_bucket_prices'))])
    summary=[]
    for r in result['rows']:
        compact={"title":r['title'],"market_leading":f"{r.get('market_leading_group')}@{r.get('market_leading_yes')}","variants":{}}
        for name,v in r['variants'].items():
            compact['variants'][name]={"count":v.get('count_now'),"proj":v.get('linear_projected_final'),"proj_bucket":v.get('projected_bucket_prices')}
        summary.append(compact)
    print(json.dumps({"bearer_ok":tokstatus.get('ok'),"calls":calls,"estimated_credit_spend_usd":result['estimated_credit_spend_usd'],"rows":len(result['rows']),"latest":str(json_path),"csv":str(csv_path),"summary":summary},indent=2,ensure_ascii=False))

if __name__ == '__main__': main()
