#!/usr/bin/env python3
"""Low-credit X count probe for Polymarket Tweet Markets.

Reads env OAuth1 credentials, never prints them. Uses public X v2 endpoints only.
Designed to spend a small number of X credits:
- users/by/username for target accounts
- tweets/counts/recent for account+window counts

No posting, no DMs, no mutations.
"""

from __future__ import annotations

import base64
import datetime as dt
import hmac
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.x.com/2"
UA = "Hermes-Polymarket-X-Count-Probe/0.1"
REQUIRED = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]

TARGETS = {
    "Elon Musk": "elonmusk",
    "White House": "WhiteHouse",
    "Zelenskyy": "ZelenskyyUa",
    "Khamenei": "khamenei_ir",
    "Ted Cruz": "tedcruz",
    "CZ": "cz_binance",
}

MONTHS = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}


def enc(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


def oauth_header(method: str, url: str, params: dict[str, str] | None = None) -> str:
    params = dict(params or {})
    oauth = {
        "oauth_consumer_key": os.environ["X_API_KEY"],
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": os.environ["X_ACCESS_TOKEN"],
        "oauth_version": "1.0",
    }
    all_params = {**params, **oauth}
    pairs = sorted((enc(k), enc(v)) for k, v in all_params.items())
    param_str = "&".join(f"{k}={v}" for k, v in pairs)
    base = "&".join([method.upper(), enc(url), enc(param_str)])
    key = enc(os.environ["X_API_SECRET"]) + "&" + enc(os.environ["X_ACCESS_TOKEN_SECRET"])
    oauth["oauth_signature"] = base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    ).decode()
    return "OAuth " + ", ".join(f'{enc(k)}="{enc(v)}"' for k, v in oauth.items())


def call(path: str, params: dict[str, str], label: str) -> dict:
    url = API + path
    full = url + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(
        full,
        headers={"Authorization": oauth_header("GET", url, params), "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            return {"label": label, "status": r.status, "ok": True, "json": json.loads(body)}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:800]
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body}
        return {"label": label, "status": e.code, "ok": False, "error": parsed}
    except Exception as e:
        return {"label": label, "status": None, "ok": False, "error": {"type": type(e).__name__, "message": str(e)}}


def parse_window(title: str, end_date: str | None) -> tuple[dt.datetime, dt.datetime] | None:
    # Examples: "Elon Musk # tweets July 3 - July 10, 2026?"
    m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\s+-\s+(?:(January|February|March|April|May|June|July|August|September|October|November|December)\s+)?(\d{1,2}),\s+(\d{4})", title)
    if not m:
        return None
    m1, d1, m2, d2, y = m.groups()
    year = int(y)
    month1 = MONTHS[m1]
    month2 = MONTHS[m2] if m2 else month1
    start = dt.datetime(year, month1, int(d1), 0, 0, tzinfo=dt.UTC)
    # Polymarket events observed end at 16:00Z; use event end if available, else date boundary.
    if end_date:
        try:
            end = dt.datetime.fromisoformat(end_date.replace("Z", "+00:00")).astimezone(dt.UTC)
        except Exception:
            end = dt.datetime(year, month2, int(d2), 23, 59, tzinfo=dt.UTC)
    else:
        end = dt.datetime(year, month2, int(d2), 23, 59, tzinfo=dt.UTC)
    return start, end


def load_events(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return data.get("events", [])


def target_for_title(title: str) -> str | None:
    for display in TARGETS:
        if title.startswith(display + " #"):
            return TARGETS[display]
    return None


def clamp_recent_window(start: dt.datetime, end: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    # X recent counts is limited to recent window. If start is older than 7 days, use what it accepts.
    now = dt.datetime.now(dt.UTC)
    earliest = now - dt.timedelta(days=6, hours=23, minutes=50)
    return max(start, earliest), min(end, now)


def main() -> int:
    outdir = Path("/data/workspace/polymarket-research/reports")
    outdir.mkdir(parents=True, exist_ok=True)
    events_path = outdir / "tweet_page_events_latest.json"
    present = all(os.environ.get(k) for k in REQUIRED)
    result: dict = {
        "retrieved_at_utc": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "oauth1_env_present": present,
        "source_events": str(events_path),
        "endpoint_probe": {},
        "account_ids": {},
        "counts": [],
        "estimated_credit_spend_usd": None,
        "notes": [],
    }
    if not present:
        result["notes"].append("OAuth1 env vars missing; no X calls made.")
        print(json.dumps(result, indent=2))
        return 0

    # Identity check: cheap sanity. It may be free or billable depending X rules; keep one call.
    me = call("/users/me", {"user.fields": "id,username"}, "users_me")
    result["endpoint_probe"]["users_me"] = {"status": me["status"], "ok": me["ok"]}

    # Lookup only accounts present in current tweet markets, excluding Truth Social.
    events = load_events(events_path)
    needed_usernames: list[str] = []
    candidate_events: list[dict] = []
    for ev in events:
        username = target_for_title(ev.get("title", ""))
        if not username:
            continue
        parsed = parse_window(ev.get("title", ""), ev.get("end_date"))
        if not parsed:
            continue
        if username not in needed_usernames:
            needed_usernames.append(username)
        candidate_events.append({**ev, "x_username": username})

    user_lookup_calls = 0
    for username in needed_usernames:
        resp = call(f"/users/by/username/{username}", {"user.fields": "id,username"}, f"lookup_{username}")
        user_lookup_calls += 1
        result["endpoint_probe"][f"lookup_{username}"] = {"status": resp["status"], "ok": resp["ok"]}
        if resp["ok"] and resp["json"].get("data"):
            result["account_ids"][username] = resp["json"]["data"].get("id")
        else:
            result["notes"].append(f"lookup failed for {username}: status={resp['status']}")

    # Count only if lookup worked. Recent counts does not require user IDs, but lookup confirms access.
    count_calls = 0
    for ev in candidate_events:
        username = ev["x_username"]
        start_end = parse_window(ev.get("title", ""), ev.get("end_date"))
        if not start_end:
            continue
        start, end = clamp_recent_window(*start_end)
        if start >= end:
            result["counts"].append({
                "title": ev.get("title"),
                "x_username": username,
                "status": "skipped_window_outside_recent",
            })
            continue
        # Count original posts by account, excluding retweets/replies. This may need adjustment if
        # Polymarket rules count replies; treat as first-pass conservative signal.
        query = f"from:{username} -is:retweet -is:reply"
        params = {
            "query": query,
            "start_time": start.isoformat().replace("+00:00", "Z"),
            "end_time": end.isoformat().replace("+00:00", "Z"),
            "granularity": "day",
        }
        resp = call("/tweets/counts/recent", params, f"counts_{username}_{ev.get('event_id')}")
        count_calls += 1
        row = {
            "event_id": ev.get("event_id"),
            "title": ev.get("title"),
            "x_username": username,
            "window_start_utc": params["start_time"],
            "window_end_utc": params["end_time"],
            "market_leading_group": ev.get("leading_group"),
            "market_leading_yes": ev.get("leading_yes"),
            "market_prices": ev.get("prices"),
            "status": resp["status"],
            "ok": resp["ok"],
        }
        if resp["ok"]:
            meta = resp["json"].get("meta", {})
            row["total_count"] = meta.get("total_tweet_count")
            row["buckets"] = resp["json"].get("data", [])
        else:
            row["error"] = resp.get("error")
        result["counts"].append(row)
        # Keep first run bounded; enough for active window research, low credit.
        if count_calls >= 12:
            result["notes"].append("Stopped after 12 count calls to conserve credits.")
            break

    # Official pricing: user read $0.010/resource, counts recent $0.005/request. /users/me not included here.
    result["estimated_credit_spend_usd"] = round(user_lookup_calls * 0.010 + count_calls * 0.005, 4)
    out = outdir / "x_tweet_count_probe_latest.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    print(json.dumps({
        "oauth1_env_present": result["oauth1_env_present"],
        "users_me": result["endpoint_probe"].get("users_me"),
        "lookups_ok": sum(1 for k, v in result["endpoint_probe"].items() if k.startswith("lookup_") and v.get("ok")),
        "lookups_total": sum(1 for k in result["endpoint_probe"] if k.startswith("lookup_")),
        "count_calls": count_calls,
        "count_ok": sum(1 for c in result["counts"] if c.get("ok")),
        "estimated_credit_spend_usd": result["estimated_credit_spend_usd"],
        "latest": str(out),
        "notes": result["notes"][:5],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
