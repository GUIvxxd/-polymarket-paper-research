#!/usr/bin/env python3
"""Public-record reaction scanner for stock/event-market research.

Paper/research only. This scanner watches official public records, classifies
potential market-moving records, maps them to public tickers when possible, and
writes timestamped audit artifacts. It does not place orders, call brokers, hold
wallet keys, or produce live-trading instructions.

Current MVP sources:
- SEC EDGAR current filings Atom feed + SEC company ticker map.
- USAspending award search for contracts, grants, and loans.
- Optional Polymarket Gamma active-market keyword matching for event-market leads.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path("/data/workspace/polymarket-research")
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
STATE_PATH = DATA / "public_record_reaction_state.json"
TICKER_CACHE = DATA / "sec_company_tickers_cache.json"
SIGNALS_JSONL = REPORTS / "public_record_reaction_signals.jsonl"
SUMMARY_JSON = REPORTS / "public_record_reaction_summary_latest.json"
SUMMARY_MD = REPORTS / "public_record_reaction_summary_latest.md"
SIGNALS_CSV = REPORTS / "public_record_reaction_signals_latest.csv"

VERSION = "public_record_reaction_v2_2026_07_17"
ADAPTER_REVISION = "public_record_adapter_v2_2026_07_17"
UA = "Hermes public-record-reaction paper scanner contact: research@example.com"
BROWSER_UA = "Mozilla/5.0 Hermes public-record-reaction paper scanner"
SEC_CURRENT_FEED = "https://www.sec.gov/cgi-bin/browse-edgar"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
USASPENDING_SEARCH = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"

AWARD_GROUPS = {
    "contracts": ["A", "B", "C", "D"],
    "grants": ["02", "03", "04", "05"],
    "loans": ["07", "08", "F003", "F004"],
}

SEC_FORM_BASE_SCORES = {
    "8-K": 72,
    "8-K/A": 65,
    "6-K": 65,
    "10-Q": 52,
    "10-K": 52,
    "S-1": 76,
    "S-1/A": 62,
    "S-3": 70,
    "S-3/A": 58,
    "424B4": 76,
    "424B5": 72,
    "424B2": 55,
    "SC 13D": 78,
    "SC 13D/A": 70,
    "SCHEDULE 13D": 78,
    "SCHEDULE 13D/A": 70,
    "SCHEDULE 13G": 58,
    "SCHEDULE 13G/A": 50,
    "4": 62,
    "3": 40,
    "5": 42,
}

SEC_ITEM_HINTS = {
    "1.01": ("material definitive agreement", 8),
    "2.01": ("acquisition/disposition completion", 8),
    "2.02": ("earnings/results information", 5),
    "2.03": ("new direct financial obligation / debt", 16),
    "2.04": ("accelerating obligation / default trigger", 18),
    "2.05": ("exit/disposal/restructuring costs", 12),
    "2.06": ("material impairment", 15),
    "3.01": ("exchange delisting/noncompliance notice", 18),
    "3.02": ("unregistered securities sale / financing", 14),
    "4.01": ("auditor change", 10),
    "5.02": ("executive/director change", 6),
    "7.01": ("regulation FD disclosure", 4),
    "8.01": ("other material event", 5),
    "9.01": ("financial statements/exhibits", 2),
}

BORING_ENTITY_WORDS = {
    "the", "and", "company", "co", "corp", "corporation", "inc", "incorporated",
    "llc", "ltd", "limited", "lp", "plc", "holdings", "holding", "group", "fund",
    "trust", "etf", "portfolio", "portfolios", "partners", "capital", "management",
    "international", "class", "common", "stock", "series", "sa", "nv", "ag",
}

CSV_FIELDS = [
    "detected_at",
    "source",
    "record_type",
    "signal_score",
    "priority",
    "ticker",
    "company",
    "match_confidence",
    "record_date",
    "headline",
    "reason",
    "amount",
    "agency",
    "url",
    "polymarket_match_count",
    "new_signal",
]


def utc_now_dt() -> datetime:
    return datetime.now(UTC)


def utc_now() -> str:
    return utc_now_dt().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_date(dt: datetime) -> str:
    return dt.date().isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def http_json(url: str, *, data: dict[str, Any] | None = None, timeout: int = 40, browser_ua: bool = False) -> Any:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {
        "User-Agent": BROWSER_UA if browser_ua else UA,
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def http_text(url: str, *, params: dict[str, Any] | None = None, timeout: int = 40, browser_ua: bool = False) -> str:
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_UA if browser_ua else UA,
            "Accept": "application/atom+xml,application/xml,text/xml,text/html,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def sha_id(*parts: Any) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:20]


def clean_html(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"<br\s*/?>", " | ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    text = html.unescape(str(name)).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [w for w in text.split() if w and w not in BORING_ENTITY_WORDS]
    return " ".join(words)


def token_set(name: str | None) -> set[str]:
    return set(normalize_name(name).split())


def money(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def priority_from_score(score: float) -> str:
    if score >= 85:
        return "HIGH"
    if score >= 70:
        return "MEDIUM"
    if score >= 55:
        return "WATCH"
    return "LOW"


def parse_sec_title(title: str) -> dict[str, str | None]:
    # Typical: "8-K - APPLE INC (0000320193) (Filer)"
    form = None
    company = title
    cik = None
    role = None
    if " - " in title:
        form, company = title.split(" - ", 1)
        form = form.strip()
    m = re.search(r"\((\d{6,10})\)\s*\(([^)]+)\)\s*$", company)
    if m:
        cik = m.group(1).zfill(10)
        role = m.group(2)
        company = company[: m.start()].strip()
    return {"form": form, "company": company.strip(), "cik": cik, "role": role}


def parse_sec_items(text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for item, (label, boost) in SEC_ITEM_HINTS.items():
        if re.search(rf"Item\s+{re.escape(item)}\b", text, flags=re.I):
            found.append({"item": item, "label": label, "score_boost": boost})
    if re.search(r"credit agreement|loan agreement|term loan|revolving credit|debt financing", text, flags=re.I):
        found.append({"item": "loan_keyword", "label": "loan/credit/debt keyword", "score_boost": 12})
    return found


def load_company_tickers(cache_hours: int = 24 * 7) -> dict[str, Any]:
    cached = load_json(TICKER_CACHE, {})
    fetched_at = cached.get("fetched_at")
    if fetched_at:
        try:
            dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            if utc_now_dt() - dt < timedelta(hours=cache_hours) and cached.get("companies"):
                return cached
        except Exception:
            pass
    raw = http_json(SEC_TICKERS_URL)
    companies = []
    for item in raw.values():
        cik = str(item.get("cik_str") or "").zfill(10)
        ticker = str(item.get("ticker") or "").upper().strip()
        title = str(item.get("title") or "").strip()
        if cik and ticker and title:
            companies.append({
                "cik": cik,
                "ticker": ticker,
                "title": title,
                "normalized_title": normalize_name(title),
                "tokens": sorted(token_set(title)),
            })
    payload = {"fetched_at": utc_now(), "count": len(companies), "companies": companies}
    save_json(TICKER_CACHE, payload)
    return payload


def company_indexes(ticker_payload: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    companies = ticker_payload.get("companies") or []
    by_cik = {str(c.get("cik") or "").zfill(10): c for c in companies}
    return by_cik, companies


def ordered_phrase_present(needle_words: list[str], haystack_words: list[str]) -> bool:
    """True when all company words appear as a contiguous token phrase.

    This avoids dangerous substring matches such as public ticker `NU` matching
    `Blue Origin Manufacturing` because the letters "nu" appear inside a word.
    """
    if not needle_words or len(needle_words) > len(haystack_words):
        return False
    width = len(needle_words)
    return any(haystack_words[i : i + width] == needle_words for i in range(len(haystack_words) - width + 1))


def best_company_match(name: str, companies: list[dict[str, Any]], min_confidence: float = 0.82) -> dict[str, Any] | None:
    norm = normalize_name(name)
    if not norm:
        return None
    query_words = norm.split()
    query_tokens = set(query_words)
    if not query_tokens:
        return None
    best: tuple[float, dict[str, Any] | None, str] = (0.0, None, "")
    for company in companies:
        company_norm = company.get("normalized_title") or ""
        if not company_norm:
            continue
        comp_words = company_norm.split()
        comp_tokens = set(comp_words)
        if not comp_tokens:
            continue
        confidence = 0.0
        method = "token_overlap"
        if norm == company_norm:
            confidence = 1.0
            method = "exact_normalized"
        elif len(comp_words) >= 2 and ordered_phrase_present(comp_words, query_words):
            confidence = min(0.97, len(comp_words) / max(1, len(query_words)))
            method = "company_phrase_in_recipient"
        elif len(query_words) >= 2 and ordered_phrase_present(query_words, comp_words):
            confidence = min(0.95, len(query_words) / max(1, len(comp_words)))
            method = "recipient_phrase_in_company"
        else:
            overlap = len(query_tokens & comp_tokens)
            # Require at least two real shared tokens. One-token overlaps create
            # many false positives (ATI, NU, ON, SCI, PPL, etc.).
            if overlap >= 2:
                confidence = overlap / max(len(query_tokens), len(comp_tokens))
        if confidence > best[0]:
            best = (confidence, company, method)
    if best[1] and best[0] >= min_confidence:
        return {**best[1], "match_confidence": round(best[0], 3), "match_method": best[2]}
    return None


def fetch_sec_current(count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params = {"action": "getcurrent", "owner": "include", "count": str(count), "output": "atom"}
    request_started_at = utc_now()
    text = http_text(SEC_CURRENT_FEED, params=params)
    response_received_at = utc_now()
    root = ET.fromstring(text.encode("utf-8"))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    rows: list[dict[str, Any]] = []
    feed_updated = root.findtext("a:updated", default="", namespaces=ns)
    for entry in root.findall("a:entry", ns):
        title = entry.findtext("a:title", default="", namespaces=ns).strip()
        updated = entry.findtext("a:updated", default="", namespaces=ns).strip()
        summary = clean_html(entry.findtext("a:summary", default="", namespaces=ns))
        link = ""
        for link_node in entry.findall("a:link", ns):
            if link_node.attrib.get("href"):
                link = link_node.attrib["href"]
                break
        form = None
        category = entry.find("a:category", ns)
        if category is not None:
            form = category.attrib.get("term")
        parsed = parse_sec_title(title)
        if form:
            parsed["form"] = form
        filed = None
        m_filed = re.search(r"Filed:\s*(\d{4}-\d{2}-\d{2})", summary)
        if m_filed:
            filed = m_filed.group(1)
        acc = None
        m_acc = re.search(r"AccNo:\s*([0-9-]+)", summary)
        if m_acc:
            acc = m_acc.group(1)
        rows.append({
            "source": "SEC_EDGAR",
            "title": title,
            "headline": title,
            "updated": updated,
            "source_published_at": updated or filed,
            "source_timestamp_precision": "exact_second" if updated else "date_only",
            "first_seen_at": response_received_at,
            "request_started_at": request_started_at,
            "response_received_at": response_received_at,
            "fetched_at": response_received_at,
            "parsed_at": utc_now(),
            "record_date": filed or updated,
            "summary_text": summary,
            "url": link,
            "accession": acc,
            **parsed,
        })
    return rows, {"feed_updated": feed_updated, "source_url": SEC_CURRENT_FEED, "raw_count": len(rows)}


def classify_sec_record(row: dict[str, Any], by_cik: dict[str, dict[str, Any]]) -> dict[str, Any]:
    form = str(row.get("form") or "").upper().strip()
    cik = str(row.get("cik") or "").zfill(10) if row.get("cik") else ""
    company_meta = by_cik.get(cik)
    ticker = company_meta.get("ticker") if company_meta else None
    company = company_meta.get("title") if company_meta else row.get("company")
    base = SEC_FORM_BASE_SCORES.get(form, 30)
    items = parse_sec_items(str(row.get("summary_text") or ""))
    item_boost = sum(int(i["score_boost"]) for i in items)
    ticker_boost = 8 if ticker else 0
    score = min(100, base + item_boost + ticker_boost)
    item_labels = [f"Item {i['item']}: {i['label']}" for i in items if str(i.get("item")) != "loan_keyword"]
    keyword_labels = [str(i["label"]) for i in items if str(i.get("item")) == "loan_keyword"]
    reason_parts = [f"SEC form {form or 'unknown'}"] + item_labels + keyword_labels
    if ticker:
        reason_parts.append(f"matched SEC ticker {ticker}")
    else:
        reason_parts.append("no public ticker match from SEC ticker map")
    decision_at = utc_now()
    return {
        "id": sha_id("sec", row.get("accession"), row.get("url"), cik, form),
        "version": VERSION,
        "adapter_revision": ADAPTER_REVISION,
        "detected_at": decision_at,
        "first_seen_at": row.get("first_seen_at") or row.get("response_received_at") or row.get("fetched_at"),
        "request_started_at": row.get("request_started_at"),
        "response_received_at": row.get("response_received_at") or row.get("fetched_at"),
        "fetched_at": row.get("fetched_at"),
        "parsed_at": row.get("parsed_at") or row.get("fetched_at"),
        "decision_at": decision_at,
        "source_published_at": row.get("source_published_at") or row.get("updated") or row.get("record_date"),
        "source_timestamp_precision": row.get("source_timestamp_precision") or ("exact_second" if row.get("updated") else "date_only"),
        "source": "SEC_EDGAR",
        "source_group": "filings",
        "record_type": form or "UNKNOWN",
        "record_date": row.get("record_date"),
        "headline": row.get("headline"),
        "company": company,
        "cik": cik or None,
        "ticker": ticker,
        "match_confidence": 1.0 if ticker else None,
        "match_method": "sec_cik" if ticker else None,
        "signal_score": score,
        "priority": priority_from_score(score),
        "reason": "; ".join(reason_parts),
        "sec_items": items,
        "amount": None,
        "agency": None,
        "url": row.get("url"),
        "raw": {k: row.get(k) for k in ("accession", "summary_text", "role", "updated")},
    }


def fetch_usaspending_awards(days: int, per_group_limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    end = utc_now_dt().date()
    start = end - timedelta(days=max(days, 1))
    fields = [
        "Award ID",
        "Recipient Name",
        "Award Amount",
        "Start Date",
        "Award Date",
        "Action Date",
        "Last Modified Date",
        "End Date",
        "Awarding Agency",
        "Award Type",
        "Description",
    ]
    rows: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for group, codes in AWARD_GROUPS.items():
        payload = {
            "filters": {
                "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}],
                "award_type_codes": codes,
            },
            "fields": fields,
            "page": 1,
            "limit": per_group_limit,
            "sort": "Award Amount",
            "order": "desc",
            "subawards": False,
        }
        try:
            request_started_at = utc_now()
            data = http_json(USASPENDING_SEARCH, data=payload)
            response_received_at = utc_now()
        except Exception as exc:
            errors[group] = f"{type(exc).__name__}: {exc}"
            continue
        for item in data.get("results") or []:
            item["_award_group"] = group
            item["_request_started_at"] = request_started_at
            item["_response_received_at"] = response_received_at
            item["_fetched_at"] = response_received_at
            item["_parsed_at"] = utc_now()
            rows.append(item)
    meta = {"date_range": {"start_date": start.isoformat(), "end_date": end.isoformat()}, "raw_count": len(rows), "errors": errors}
    return rows, meta


def classify_award_record(row: dict[str, Any], companies: list[dict[str, Any]]) -> dict[str, Any]:
    recipient = str(row.get("Recipient Name") or "").strip()
    match = best_company_match(recipient, companies)
    amount = money(row.get("Award Amount"))
    group = str(row.get("_award_group") or "awards")
    score = 38
    if amount is not None:
        if amount >= 1_000_000_000:
            score = 84
        elif amount >= 100_000_000:
            score = 74
        elif amount >= 10_000_000:
            score = 63
        elif amount >= 1_000_000:
            score = 53
        else:
            score = 42
    if group == "loans":
        score += 10
    if match:
        score += 8
    score = min(100, score)
    ticker = match.get("ticker") if match else None
    company = match.get("title") if match else recipient
    confidence = match.get("match_confidence") if match else None
    reason = [
        f"USAspending {group[:-1] if group.endswith('s') else group} award",
        f"amount={amount}" if amount is not None else "amount missing",
        f"agency={row.get('Awarding Agency') or 'unknown'}",
    ]
    if ticker:
        reason.append(f"matched public ticker {ticker} via recipient name")
    else:
        reason.append("no confident public ticker match")
    award_id = row.get("Award ID") or row.get("generated_internal_id") or row.get("internal_id")
    record_date = row.get("Last Modified Date") or row.get("Award Date") or row.get("Action Date") or row.get("Start Date") or row.get("End Date")
    date_label = "last_modified" if row.get("Last Modified Date") else "award/start_date"
    if row.get("Last Modified Date"):
        reason.append(f"last_modified={row.get('Last Modified Date')}")
    decision_at = utc_now()
    source_precision = "source_clock_unverified" if record_date and len(str(record_date)) > 10 else "date_only"
    raw_record = {key: value for key, value in row.items() if not str(key).startswith("_")}
    return {
        "id": sha_id("usaspending", group, award_id, recipient, amount, record_date),
        "version": VERSION,
        "adapter_revision": ADAPTER_REVISION,
        "detected_at": decision_at,
        "first_seen_at": row.get("_response_received_at") or row.get("_fetched_at"),
        "request_started_at": row.get("_request_started_at"),
        "response_received_at": row.get("_response_received_at") or row.get("_fetched_at"),
        "fetched_at": row.get("_fetched_at"),
        "parsed_at": row.get("_parsed_at") or row.get("_fetched_at"),
        "decision_at": decision_at,
        "source_published_at": record_date,
        "source_timestamp_precision": source_precision,
        "source": "USAspending",
        "source_group": group,
        "record_type": str(row.get("Award Type") or group),
        "record_date": record_date,
        "record_date_type": date_label,
        "headline": f"{recipient} - {row.get('Award Type') or group} - ${amount:,.0f}" if amount is not None else f"{recipient} - {row.get('Award Type') or group}",
        "company": company,
        "recipient": recipient,
        "cik": match.get("cik") if match else None,
        "ticker": ticker,
        "match_confidence": confidence,
        "match_method": match.get("match_method") if match else None,
        "signal_score": score,
        "priority": priority_from_score(score),
        "reason": "; ".join(reason),
        "amount": amount,
        "agency": row.get("Awarding Agency"),
        "url": "https://www.usaspending.gov/search/?hash=public-record-reaction",  # generic search URL; award deep-links vary by generated ID
        "raw": raw_record,
    }


def fetch_polymarket_markets(limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if limit <= 0:
        return [], {"disabled": True}
    params = {"active": "true", "closed": "false", "limit": str(limit)}
    url = GAMMA_MARKETS + "?" + urllib.parse.urlencode(params)
    try:
        data = http_json(url, browser_ua=True)
        if not isinstance(data, list):
            return [], {"error": "unexpected non-list response"}
        return data, {"raw_count": len(data), "source_url": url}
    except Exception as exc:
        return [], {"error": f"{type(exc).__name__}: {exc}"}


def market_match_score(signal: dict[str, Any], market: dict[str, Any]) -> float:
    question = str(market.get("question") or market.get("title") or "")
    slug = str(market.get("slug") or "")
    original_text = f"{question} {slug}"
    text = original_text.lower()
    ticker = str(signal.get("ticker") or "").strip()
    company_tokens = token_set(str(signal.get("company") or ""))
    market_tokens = token_set(text)
    overlap = len(company_tokens & market_tokens)
    # Short/common-word tickers (ON, C, BA, etc.) are never sufficient alone.
    # For longer symbols, require explicit ticker notation or company corroboration.
    explicit_ticker = bool(
        ticker and (
            re.search(rf"(?<![A-Z0-9])\${re.escape(ticker)}(?![A-Z0-9])", original_text, re.IGNORECASE)
            or re.search(rf"\({re.escape(ticker)}\)", original_text, re.IGNORECASE)
        )
    )
    ticker_word = bool(len(ticker) >= 3 and re.search(rf"\b{re.escape(ticker)}\b", original_text, re.IGNORECASE))
    if explicit_ticker:
        return 1.0
    if ticker_word and overlap >= 1:
        return 0.9
    if overlap >= 2:
        return min(0.95, overlap / max(1, len(company_tokens)))
    return 0.0


def attach_polymarket_matches(signals: list[dict[str, Any]], markets: list[dict[str, Any]], max_matches: int = 3) -> None:
    for signal in signals:
        scored = []
        for market in markets:
            score = market_match_score(signal, market)
            if score >= 0.35:
                scored.append((score, market))
        scored.sort(key=lambda x: x[0], reverse=True)
        signal["polymarket_matches"] = [
            {
                "score": round(score, 3),
                "question": market.get("question"),
                "slug": market.get("slug"),
                "market_id": market.get("id"),
                "condition_id": market.get("conditionId") or market.get("condition_id"),
                "token_ids": market.get("clobTokenIds") or market.get("token_ids"),
                "outcomes": market.get("outcomes"),
                "url": f"https://polymarket.com/event/{market.get('slug')}" if market.get("slug") else None,
            }
            for score, market in scored[:max_matches]
        ]


def dedupe_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for signal in signals:
        sid = signal.get("id")
        if sid in seen:
            continue
        seen.add(sid)
        out.append(signal)
    return out


def write_csv(path: Path, signals: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for signal in signals:
            writer.writerow({
                "detected_at": signal.get("detected_at"),
                "source": signal.get("source"),
                "record_type": signal.get("record_type"),
                "signal_score": signal.get("signal_score"),
                "priority": signal.get("priority"),
                "ticker": signal.get("ticker") or "",
                "company": signal.get("company") or "",
                "match_confidence": signal.get("match_confidence") or "",
                "record_date": signal.get("record_date") or "",
                "headline": signal.get("headline") or "",
                "reason": signal.get("reason") or "",
                "amount": signal.get("amount") or "",
                "agency": signal.get("agency") or "",
                "url": signal.get("url") or "",
                "polymarket_match_count": len(signal.get("polymarket_matches") or []),
                "new_signal": signal.get("new_signal"),
            })


def md_table_rows(signals: list[dict[str, Any]], limit: int = 15) -> list[str]:
    rows = ["| Priority | Score | Source | Ticker | Company / headline | Reason | Link |", "|---|---:|---|---|---|---|---|"]
    for signal in signals[:limit]:
        link = signal.get("url") or ""
        link_md = f"[source]({link})" if str(link).startswith("http") else ""
        company = str(signal.get("company") or "")[:80]
        headline = str(signal.get("headline") or "")[:100]
        label = company if company and company != headline else headline
        reason = str(signal.get("reason") or "")[:130].replace("|", "/")
        rows.append(
            f"| {signal.get('priority')} | {signal.get('signal_score')} | {signal.get('source')}:{signal.get('record_type')} | {signal.get('ticker') or ''} | {label.replace('|','/')} | {reason} | {link_md} |"
        )
    return rows


def write_markdown(summary: dict[str, Any], signals: list[dict[str, Any]]) -> None:
    by_source = Counter(s.get("source") for s in signals)
    high = [s for s in signals if s.get("priority") == "HIGH"]
    new_count = sum(1 for s in signals if s.get("new_signal"))
    lines = [
        "# Public Record Reaction Bot — Latest Paper Scan",
        "",
        "Safety: **paper/research only**. No broker, no wallet, no private key, no live orders.",
        "",
        f"- Generated: `{summary.get('generated_at')}`",
        f"- Version: `{summary.get('version')}`",
        f"- Signals kept: `{summary.get('signals_kept')}`",
        f"- New signals vs state: `{new_count}`",
        f"- High-priority signals: `{len(high)}`",
        f"- SEC raw filings scanned: `{summary.get('sources', {}).get('sec', {}).get('raw_count')}`",
        f"- USAspending raw awards scanned: `{summary.get('sources', {}).get('usaspending', {}).get('raw_count')}`",
        f"- Polymarket active markets scanned for optional matches: `{summary.get('sources', {}).get('polymarket', {}).get('raw_count', 0)}`",
        "",
        "## Source mix",
        "",
    ]
    for source, count in sorted(by_source.items()):
        lines.append(f"- `{source}`: `{count}`")
    lines.extend(["", "## Top signals", ""])
    lines.extend(md_table_rows(signals))
    lines.extend([
        "",
        "## How to read this",
        "",
        "This is the public-record layer your boss described: official record appears → timestamped detection → ticker/company mapping → optional Polymarket market matching → paper/research signal log. It does **not** claim live stock edge yet because reliable quote/execution adapters and delayed outcome tracking still need to be added.",
        "",
        "## Files",
        "",
        f"- JSON summary: `{SUMMARY_JSON}`",
        f"- CSV signals: `{SIGNALS_CSV}`",
        f"- JSONL audit log: `{SIGNALS_JSONL}`",
    ])
    SUMMARY_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_scan(args: argparse.Namespace) -> dict[str, Any]:
    generated_at = utc_now()
    ticker_payload = load_company_tickers()
    by_cik, companies = company_indexes(ticker_payload)

    sec_rows, sec_meta = fetch_sec_current(args.sec_count)
    sec_signals = [classify_sec_record(row, by_cik) for row in sec_rows]

    award_rows, award_meta = fetch_usaspending_awards(args.award_days, args.award_limit)
    award_signals = [classify_award_record(row, companies) for row in award_rows]

    signals = dedupe_signals(sec_signals + award_signals)
    signals = [s for s in signals if float(s.get("signal_score") or 0) >= args.min_score or (args.keep_ticker_matches and s.get("ticker"))]
    signals.sort(key=lambda s: (float(s.get("signal_score") or 0), bool(s.get("ticker")), str(s.get("record_date") or "")), reverse=True)

    markets, poly_meta = fetch_polymarket_markets(args.polymarket_limit)
    attach_polymarket_matches(signals, markets)

    state = load_json(STATE_PATH, {"seen_signal_ids": []})
    seen = set(state.get("seen_signal_ids") or [])
    new_signals = []
    for signal in signals:
        is_new = signal.get("id") not in seen
        signal["new_signal"] = is_new
        if is_new:
            new_signals.append(signal)
            seen.add(signal.get("id"))

    summary = {
        "generated_at": generated_at,
        "version": VERSION,
        "parameters": vars(args),
        "signals_kept": len(signals),
        "new_signals": len(new_signals),
        "high_priority_signals": sum(1 for s in signals if s.get("priority") == "HIGH"),
        "medium_priority_signals": sum(1 for s in signals if s.get("priority") == "MEDIUM"),
        "watch_signals": sum(1 for s in signals if s.get("priority") == "WATCH"),
        "ticker_matched_signals": sum(1 for s in signals if s.get("ticker")),
        "polymarket_matched_signals": sum(1 for s in signals if s.get("polymarket_matches")),
        "sources": {
            "sec": sec_meta,
            "usaspending": award_meta,
            "polymarket": poly_meta,
            "sec_ticker_cache": {"fetched_at": ticker_payload.get("fetched_at"), "count": ticker_payload.get("count")},
        },
        "files": {
            "summary_json": str(SUMMARY_JSON),
            "summary_md": str(SUMMARY_MD),
            "signals_csv": str(SIGNALS_CSV),
            "signals_jsonl": str(SIGNALS_JSONL),
            "state": str(STATE_PATH),
        },
        "top_signals": signals[:20],
    }

    if not args.no_write:
        REPORTS.mkdir(parents=True, exist_ok=True)
        DATA.mkdir(parents=True, exist_ok=True)
        save_json(SUMMARY_JSON, summary)
        write_csv(SIGNALS_CSV, signals)
        append_jsonl(SIGNALS_JSONL, new_signals)
        save_json(STATE_PATH, {
            "updated_at": generated_at,
            "version": VERSION,
            "seen_signal_ids": sorted(x for x in seen if x),
            "last_summary": {
                "signals_kept": len(signals),
                "new_signals": len(new_signals),
                "high_priority_signals": summary["high_priority_signals"],
                "ticker_matched_signals": summary["ticker_matched_signals"],
            },
        })
        write_markdown(summary, signals)

    return summary


def run_self_tests() -> None:
    parsed = parse_sec_title("8-K - EXAMPLE CORP (0000123456) (Filer)")
    assert parsed["form"] == "8-K"
    assert parsed["company"] == "EXAMPLE CORP"
    assert parsed["cik"] == "0000123456"
    assert parsed["role"] == "Filer"
    assert normalize_name("Lockheed Martin Corporation") == "lockheed martin"
    fake_companies = [
        {"ticker": "LMT", "title": "LOCKHEED MARTIN CORP", "cik": "0000936468", "normalized_title": "lockheed martin", "tokens": ["lockheed", "martin"]},
        {"ticker": "NU", "title": "Nu Holdings Ltd.", "cik": "0001691493", "normalized_title": "nu", "tokens": ["nu"]},
        {"ticker": "PPL", "title": "PPL Corp", "cik": "0000922224", "normalized_title": "ppl", "tokens": ["ppl"]},
    ]
    match = best_company_match("Lockheed Martin Corporation", fake_companies)
    assert match and match["ticker"] == "LMT" and match["match_confidence"] >= 0.9
    assert best_company_match("Blue Origin Manufacturing LLC", fake_companies) is None
    assert best_company_match("The Johns Hopkins University Applied Physics Laboratory LLC", fake_companies) is None
    items = parse_sec_items("Item 1.01 Entry into a Material Definitive Agreement. Item 2.03 Creation of a Direct Financial Obligation.")
    assert {i["item"] for i in items} >= {"1.01", "2.03"}
    assert priority_from_score(90) == "HIGH"
    assert priority_from_score(60) == "WATCH"
    print("self-tests passed")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper-only public-record reaction scanner")
    parser.add_argument("--sec-count", type=int, default=120, help="SEC current-feed row count")
    parser.add_argument("--award-days", type=int, default=7, help="USAspending lookback window")
    parser.add_argument("--award-limit", type=int, default=25, help="USAspending rows per award group")
    parser.add_argument("--polymarket-limit", type=int, default=300, help="Active Gamma markets to scan for keyword matches; 0 disables")
    parser.add_argument("--min-score", type=float, default=55, help="Minimum signal score to keep")
    parser.add_argument("--keep-ticker-matches", action="store_true", default=True, help="Keep any public-ticker matched signal even below min score")
    parser.add_argument("--no-write", action="store_true", help="Run scan without writing report/state files")
    parser.add_argument("--self-test", action="store_true", help="Run narrow unit tests and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.self_test:
        run_self_tests()
        return 0
    try:
        summary = run_scan(args)
    except urllib.error.HTTPError as exc:
        body = exc.read(1000).decode("utf-8", "replace") if hasattr(exc, "read") else ""
        print(f"public-record scanner error: HTTP {exc.code}: {body}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"public-record scanner error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        "public_record_reaction "
        f"signals={summary['signals_kept']} new={summary['new_signals']} "
        f"high={summary['high_priority_signals']} ticker_matches={summary['ticker_matched_signals']} "
        f"poly_matches={summary['polymarket_matched_signals']} report={SUMMARY_MD}"
    )
    top = summary.get("top_signals") or []
    for signal in top[:5]:
        print(
            f"- {signal.get('priority')} score={signal.get('signal_score')} "
            f"{signal.get('source')} {signal.get('record_type')} "
            f"{signal.get('ticker') or ''} {signal.get('company')}: {signal.get('reason')}"
        )
    print("Paper-only: no broker, no wallet, no private keys, no live orders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
