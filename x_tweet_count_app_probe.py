#!/usr/bin/env python3
"""App-only X recent-count probe for Polymarket Tweet Markets.

No secrets printed. Does not store bearer token. Public read-only count endpoints only.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_URL = "https://api.x.com/oauth2/token"
API = "https://api.x.com/2"
UA = "Hermes-Polymarket-X-AppOnly-Counts/0.1"

TARGETS = {
    "Elon Musk": "elonmusk",
    "White House": "WhiteHouse",
    "Zelenskyy": "ZelenskyyUa",
    "Khamenei": "khamenei_ir",
    "Ted Cruz": "tedcruz",
    "CZ": "cz_binance",
}

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}

BUCKET_RE = re.compile(r"^(?:(?P<lt><|\\u003c)(?P<lt_n>\d+)|(?P<lo>\d+)-(?P<hi>\d+)|(?P<plus>\d+)\+)$")


def get_bearer() -> tuple[str | None, dict]:
    key = os.environ.get("X_API_KEY")
    secret = os.environ.get("X_API_SECRET")
    if not key or not secret:
        return None, {"ok": False, "status": None, "error": "missing X_API_KEY/X_API_SECRET"}
    auth = base64.b64encode(f"{urllib.parse.quote(key)}:{urllib.parse.quote(secret)}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "User-Agent": UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            parsed = json.loads(r.read().decode())
        token = parsed.get("access_token")
        return token, {"ok": bool(token), "status": 200, "token_type": parsed.get("token_type")}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:600]
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body}
        return None, {"ok": False, "status": e.code, "error": parsed}
    except Exception as e:
        return None, {"ok": False, "status": None, "error": {"type": type(e).__name__, "message": str(e)}}


def call_bearer(token: str, path: str, params: dict[str, str], label: str) -> dict:
    url = API + path
    full = url + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(full, headers={"Authorization": f"Bearer {token}", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"label": label, "ok": True, "status": r.status, "json": json.loads(r.read().decode())}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:800]
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body}
        return {"label": label, "ok": False, "status": e.code, "error": parsed}
    except Exception as e:
        return {"label": label, "ok": False, "status": None, "error": {"type": type(e).__name__, "message": str(e)}}


def parse_window(title: str, end_date: str | None) -> tuple[dt.datetime, dt.datetime] | None:
    m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\s+-\s+(?:(January|February|March|April|May|June|July|August|September|October|November|December)\s+)?(\d{1,2}),\s+(\d{4})", title)
    if not m:
        return None
    m1, d1, m2, d2, y = m.groups()
    year = int(y)
    month1 = MONTHS[m1]
    month2 = MONTHS[m2] if m2 else month1
    start = dt.datetime(year, month1, int(d1), 0, 0, tzinfo=dt.UTC)
    if end_date:
        try:
            end = dt.datetime.fromisoformat(end_date.replace("Z", "+00:00")).astimezone(dt.UTC)
        except Exception:
            end = dt.datetime(year, month2, int(d2), 23, 59, tzinfo=dt.UTC)
    else:
        end = dt.datetime(year, month2, int(d2), 23, 59, tzinfo=dt.UTC)
    return start, end


def target_for_title(title: str) -> str | None:
    for display, username in TARGETS.items():
        if title.startswith(display + " #"):
            return username
    return None


def clamp_recent_window(start: dt.datetime, end: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    now = dt.datetime.now(dt.UTC)
    earliest = now - dt.timedelta(days=6, hours=23, minutes=50)
    # X requires end_time to be at least 10 seconds before request time.
    safe_now = now - dt.timedelta(seconds=30)
    return max(start, earliest), min(end, safe_now)


def parse_prices(price_str: str | None) -> list[tuple[str, float]]:
    if not price_str:
        return []
    out = []
    for part in price_str.split("|"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        try:
            out.append((k.strip().replace("\\u003c", "<"), float(v)))
        except ValueError:
            pass
    return out


def bucket_contains(bucket: str, x: float) -> bool:
    bucket = bucket.replace("\\u003c", "<")
    m = BUCKET_RE.match(bucket)
    if not m:
        return False
    if m.group("lt"):
        return x < int(m.group("lt_n"))
    if m.group("plus"):
        return x >= int(m.group("plus"))
    return int(m.group("lo")) <= x <= int(m.group("hi"))


def model_projection(count_now: int, start: dt.datetime, now: dt.datetime, end: dt.datetime) -> dict:
    elapsed_h = max((now - start).total_seconds() / 3600, 0.01)
    total_h = max((end - start).total_seconds() / 3600, 0.01)
    remaining_h = max((end - now).total_seconds() / 3600, 0.0)
    rate = count_now / elapsed_h
    projected = count_now + rate * remaining_h
    return {
        "elapsed_hours": round(elapsed_h, 3),
        "remaining_hours": round(remaining_h, 3),
        "rate_per_hour": round(rate, 4),
        "linear_projected_final": round(projected, 2),
    }


def main() -> int:
    outdir = Path("/data/workspace/polymarket-research/reports")
    events_path = outdir / "tweet_page_events_latest.json"
    data = json.loads(events_path.read_text())
    events = data.get("events", [])
    token, token_status = get_bearer()
    result = {
        "retrieved_at_utc": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "bearer_token_status": token_status,
        "counts": [],
        "estimated_credit_spend_usd": 0.0,
        "notes": [],
    }
    if not token:
        out = outdir / "x_tweet_count_app_latest.json"
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(json.dumps({"bearer_ok": False, "status": token_status, "latest": str(out)}, indent=2))
        return 0

    count_calls = 0
    now = dt.datetime.now(dt.UTC)
    for ev in events:
        title = ev.get("title", "")
        username = target_for_title(title)
        if not username:
            continue
        parsed = parse_window(title, ev.get("end_date"))
        if not parsed:
            continue
        full_start, full_end = parsed
        start, end = clamp_recent_window(full_start, full_end)
        if start >= end:
            result["counts"].append({"title": title, "x_username": username, "status": "skipped_window_outside_recent_or_future"})
            continue
        # Conservative first pass: original posts only. If market rules count replies, this undercounts.
        query = f"from:{username} -is:retweet -is:reply"
        params = {
            "query": query,
            "start_time": start.isoformat().replace("+00:00", "Z"),
            "end_time": end.isoformat().replace("+00:00", "Z"),
            "granularity": "day",
        }
        resp = call_bearer(token, "/tweets/counts/recent", params, f"counts_{username}_{ev.get('event_id')}")
        count_calls += 1
        row = {
            "event_id": ev.get("event_id"),
            "title": title,
            "x_username": username,
            "count_query": query,
            "window_start_utc": params["start_time"],
            "window_end_utc": params["end_time"],
            "full_market_start_utc": full_start.isoformat().replace("+00:00", "Z"),
            "full_market_end_utc": full_end.isoformat().replace("+00:00", "Z"),
            "market_leading_group": ev.get("leading_group"),
            "market_leading_yes": ev.get("leading_yes"),
            "market_prices": ev.get("prices"),
            "status": resp["status"],
            "ok": resp["ok"],
        }
        if resp["ok"]:
            total = int(resp["json"].get("meta", {}).get("total_tweet_count", 0))
            row["count_now"] = total
            proj = model_projection(total, full_start, now, full_end)
            row.update(proj)
            prices = parse_prices(ev.get("prices"))
            row["projected_bucket_prices"] = [
                {"bucket": b, "yes_price": p} for b, p in prices if bucket_contains(b, proj["linear_projected_final"])
            ]
            row["current_count_bucket_prices"] = [
                {"bucket": b, "yes_price": p} for b, p in prices if bucket_contains(b, total)
            ]
            # Simple flag, not a trade recommendation.
            if row["projected_bucket_prices"]:
                best = max(row["projected_bucket_prices"], key=lambda x: x["yes_price"])
                row["paper_signal_note"] = f"linear_projection_in_{best['bucket']} at market yes {best['yes_price']:.3f}"
            else:
                row["paper_signal_note"] = "projection outside visible buckets or hidden bucket missing"
        else:
            row["error"] = resp.get("error")
        result["counts"].append(row)
        if count_calls >= 12:
            result["notes"].append("Stopped after 12 count calls to conserve credits.")
            break

    result["estimated_credit_spend_usd"] = round(count_calls * 0.005, 4)
    out = outdir / "x_tweet_count_app_latest.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    summary_rows = []
    for c in result["counts"]:
        if c.get("ok"):
            summary_rows.append({
                "title": c["title"],
                "count_now": c.get("count_now"),
                "projected": c.get("linear_projected_final"),
                "market_leading": f"{c.get('market_leading_group')}@{c.get('market_leading_yes')}",
                "signal": c.get("paper_signal_note"),
            })
        else:
            summary_rows.append({"title": c.get("title"), "status": c.get("status"), "ok": c.get("ok")})
    print(json.dumps({
        "bearer_ok": token_status.get("ok"),
        "count_calls": count_calls,
        "count_ok": sum(1 for c in result["counts"] if c.get("ok")),
        "estimated_credit_spend_usd": result["estimated_credit_spend_usd"],
        "latest": str(out),
        "summary": summary_rows[:12],
        "notes": result["notes"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
