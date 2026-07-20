#!/usr/bin/env python3
"""Paper proof tracker for Polymarket xtracker Tweet/Post alerts.

Reads watchdog alerts, treats the first alert for each event/bucket/question as a paper YES entry,
and checks settlement after the xtracker window ends using xtracker's public posts endpoint.

Public-data only: no X API, no wallet, no live orders.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path("/data/workspace/polymarket-research")
REPORTS = ROOT / "reports"
ALERTS = REPORTS / "xtracker_tweet_watchdog_alerts.jsonl"
OUT_JSON = REPORTS / "xtracker_paper_proof_latest.json"
OUT_MD = REPORTS / "xtracker_paper_proof_latest.md"
UA = "Hermes-XTracker-Proof/0.1"
BASE = "https://xtracker.polymarket.com"
MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def load_alerts() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not ALERTS.exists():
        return rows
    for line in ALERTS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def first_entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r.get("key") or "|".join([
            str(r.get("event", "")), str(r.get("handle", "")),
            str(r.get("bucket", "")), str(r.get("question", "")),
        ])
        if key not in seen:
            seen[key] = r
    return list(seen.values())


def parse_window(event: str) -> tuple[dt.datetime, dt.datetime] | None:
    # Examples:
    # Elon Musk # tweets July 3 - July 10, 2026?
    # Ted Cruz # posts July 7 - July 14, 2026?
    m = re.search(
        r"(?P<m1>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(?P<d1>\d{1,2})\s*-\s*"
        r"(?:(?P<m2>January|February|March|April|May|June|July|August|September|October|November|December)\s+)?"
        r"(?P<d2>\d{1,2}),\s*(?P<y>\d{4})",
        event or "",
    )
    if not m:
        return None
    year = int(m.group("y"))
    month1 = MONTHS[m.group("m1")]
    day1 = int(m.group("d1"))
    month2 = MONTHS[m.group("m2") or m.group("m1")]
    day2 = int(m.group("d2"))
    start = dt.datetime(year, month1, day1, 16, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(year, month2, day2, 15, 59, 59, tzinfo=dt.timezone.utc)
    return start, end


def parse_bucket(bucket: str) -> tuple[int | None, int | None] | None:
    s = (bucket or "").strip().replace("\\u003c", "<")
    if s.startswith("<"):
        try:
            return None, int(s[1:]) - 1
        except Exception:
            return None
    if s.endswith("+"):
        try:
            return int(s[:-1]), None
        except Exception:
            return None
    if "-" in s:
        try:
            lo, hi = s.split("-", 1)
            return int(lo), int(hi)
        except Exception:
            return None
    return None


def bucket_hit(bucket: str, count: int) -> bool | None:
    parsed = parse_bucket(bucket)
    if not parsed:
        return None
    lo, hi = parsed
    if lo is not None and count < lo:
        return False
    if hi is not None and count > hi:
        return False
    return True


def iso_z(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def final_count(handle: str, start: dt.datetime, end: dt.datetime) -> tuple[int | None, str | None]:
    params = urllib.parse.urlencode({"startDate": iso_z(start), "endDate": iso_z(end)})
    url = f"{BASE}/api/users/{urllib.parse.quote(handle)}/posts?{params}"
    try:
        data = get_json(url)
        if not data.get("success"):
            return None, f"xtracker_unsuccessful:{str(data)[:200]}"
        posts = data.get("data") or []
        return len(posts), None
    except Exception as e:
        return None, f"{type(e).__name__}:{e}"


def fnum(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def evaluate_entry(r: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    event = r.get("event") or ""
    handle = r.get("handle") or ""
    bucket = r.get("bucket") or ""
    window = parse_window(event)
    entry_price = fnum(r.get("ask"))
    out: dict[str, Any] = {
        "key": r.get("key"),
        "event": event,
        "handle": handle,
        "bucket": bucket,
        "question": r.get("question"),
        "entry_time": r.get("last_seen_at"),
        "entry_price": entry_price,
        "entry_count": r.get("count"),
        "entry_projected": r.get("projected"),
        "entry_fair": r.get("fair"),
        "entry_edge": r.get("edge"),
        "entry_depth": r.get("depth"),
        "yes_token_id": r.get("yes_token_id"),
        "condition_id": r.get("condition_id"),
    }
    if not window:
        out.update({"status": "unknown_window", "error": "could_not_parse_event_window"})
        return out
    start, end = window
    out["start_utc"] = iso_z(start)
    out["end_utc"] = iso_z(end)
    out["hours_to_end"] = round((end - now).total_seconds() / 3600, 2)
    # Wait a little after end; xtracker/import lag can exist.
    if now < end + dt.timedelta(minutes=20):
        out["status"] = "pending"
        return out
    count, err = final_count(handle, start, end)
    if err:
        out.update({"status": "settlement_check_error", "error": err})
        return out
    hit = bucket_hit(bucket, int(count))
    out["final_count"] = count
    out["won"] = hit
    if hit is None or entry_price is None:
        out["status"] = "resolved_unknown_pnl"
        return out
    pnl_per_share = (1.0 - entry_price) if hit else -entry_price
    roi = pnl_per_share / entry_price if entry_price > 0 else None
    out.update({
        "status": "resolved",
        "pnl_per_yes_share": round(pnl_per_share, 4),
        "roi_on_yes_cost": None if roi is None else round(roi, 4),
    })
    return out


def make_markdown(payload: dict[str, Any]) -> str:
    s = payload["summary"]
    lines = [
        "# xtracker Tweet/Post Paper Proof Tracker",
        "",
        f"Generated: `{payload['generated_at']}`",
        "",
        "## Summary",
        "",
        f"- Alert records: `{s['alert_records']}`",
        f"- Distinct paper entries: `{s['distinct_entries']}`",
        f"- Pending: `{s['pending']}`",
        f"- Resolved: `{s['resolved']}`",
        f"- Wins: `{s['wins']}`",
        f"- Losses: `{s['losses']}`",
        f"- Win rate: `{s['win_rate']}`",
        f"- Average ROI on YES cost: `{s['avg_roi_on_yes_cost']}`",
        "",
        "## Next pending resolutions",
        "",
        "| Event | Bucket | Entry ask | Entry edge | Hours to end |",
        "|---|---:|---:|---:|---:|",
    ]
    for e in payload["next_pending"][:12]:
        lines.append(
            f"| {e.get('event')} | `{e.get('bucket')}` | {e.get('entry_price')} | {e.get('entry_edge')} | {e.get('hours_to_end')} |"
        )
    lines += ["", "## Resolved entries", "", "| Event | Bucket | Entry ask | Final count | Won | ROI |", "|---|---:|---:|---:|---:|---:|"]
    for e in payload["resolved_entries"][:50]:
        lines.append(
            f"| {e.get('event')} | `{e.get('bucket')}` | {e.get('entry_price')} | {e.get('final_count')} | {e.get('won')} | {e.get('roi_on_yes_cost')} |"
        )
    if not payload["resolved_entries"]:
        lines.append("| _None yet_ |  |  |  |  |  |")
    lines += [
        "",
        "## Readiness rule",
        "",
        "Do not call the strategy ready until there are at least 20 resolved paper entries, positive ROI after entry prices/depth, and results are not dependent on a single outlier.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    now = utcnow()
    alerts = load_alerts()
    entries = first_entries(alerts)
    evaluated = [evaluate_entry(e, now) for e in entries]
    pending = [e for e in evaluated if e.get("status") == "pending"]
    resolved = [e for e in evaluated if e.get("status") == "resolved"]
    wins = [e for e in resolved if e.get("won") is True]
    losses = [e for e in resolved if e.get("won") is False]
    rois = [float(e["roi_on_yes_cost"]) for e in resolved if e.get("roi_on_yes_cost") is not None]
    avg_roi = None if not rois else round(sum(rois) / len(rois), 4)
    win_rate = None if not resolved else round(len(wins) / len(resolved), 4)
    pending_sorted = sorted(pending, key=lambda e: e.get("hours_to_end", 999999))
    payload = {
        "generated_at": iso_z(now),
        "source_alerts": str(ALERTS),
        "summary": {
            "alert_records": len(alerts),
            "distinct_entries": len(entries),
            "pending": len(pending),
            "resolved": len(resolved),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "avg_roi_on_yes_cost": avg_roi,
        },
        "next_pending": pending_sorted,
        "resolved_entries": resolved,
        "all_entries": evaluated,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    OUT_MD.write_text(make_markdown(payload))
    print(json.dumps({"report_json": str(OUT_JSON), "report_md": str(OUT_MD), "summary": payload["summary"], "next_pending": pending_sorted[:5]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
