#!/usr/bin/env python3
"""Multi-module Polymarket public-data research scanner.

Paper-only safety boundary:
- Uses unauthenticated public Gamma, CLOB, Data API, and public price feeds only.
- Does not use private keys, wallets, signing, or authenticated trading endpoints.
- Produces research candidates/watchlists, not live execution instructions.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
STOOQ_QUOTE = "https://stooq.com/q/l/"
UA = "Hermes-Polymarket-Multi-Market-Paper-Research/0.1"
DEFAULT_ROOT = Path("/data/workspace/polymarket-research")
DEFAULT_REPORT_DIR = DEFAULT_ROOT / "reports"
DEFAULT_WALLET_FILE = DEFAULT_ROOT / "data" / "public_wallets.txt"
SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60


@dataclass(frozen=True)
class BookTop:
    token_id: str
    outcome: str
    bid: float | None
    bid_size: float | None
    ask: float | None
    ask_size: float | None
    spread: float | None
    available: bool
    error: str | None = None
    request_started_at: str | None = None
    response_received_at: str | None = None
    provider_timestamp: str | None = None
    bids: list[tuple[float, float]] | None = None
    asks: list[tuple[float, float]] | None = None


@dataclass(frozen=True)
class TickerSpec:
    symbol: str
    yahoo_symbol: str
    stooq_symbol: str | None
    aliases: tuple[str, ...]
    annual_volatility: float
    source_hint: str


@dataclass(frozen=True)
class ExternalPrice:
    symbol: str
    price: float
    retrieved_at_utc: str
    source: str
    raw_symbol: str


@dataclass(frozen=True)
class StockCandidate:
    severity: str
    actionable: bool
    ticker: str
    side: str
    outcome: str
    slug: str
    question: str
    condition_id: str
    end_date: str | None
    seconds_to_end: float | None
    current_price: float | None
    price_source: str | None
    threshold: float | None
    direction: str | None
    fair_probability: float | None
    bid: float | None
    ask: float | None
    ask_size: float | None
    edge: float | None
    model_reason: str
    source_hint: str
    note: str
    market_id: str | None = None
    token_id: str | None = None
    bid_size: float | None = None
    book_request_started_at: str | None = None
    book_response_received_at: str | None = None
    book_provider_timestamp: str | None = None
    book_timing_quality: str | None = None
    book_bids: list[tuple[float, float]] | None = None
    book_asks: list[tuple[float, float]] | None = None


@dataclass(frozen=True)
class NewsCandidate:
    readiness: str
    category: str
    slug: str
    question: str
    condition_id: str
    end_date: str | None
    seconds_to_end: float | None
    source_hint: str
    resolution_source: str | None
    why: str
    next_step: str


@dataclass(frozen=True)
class WalletSummary:
    wallet: str
    status: str
    trades_fetched: int
    closed_positions_fetched: int
    open_positions_fetched: int
    unique_markets: int
    visible_notional: float | None
    realized_pnl_sum: float | None
    win_count: int | None
    loss_count: int | None
    notes: str


class BookBudget:
    def __init__(self, max_requests: int) -> None:
        self.remaining = max(0, max_requests)
        self.requests = 0
        self.errors = 0

    def consume(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        self.requests += 1
        return True


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
    if params:
        url = url + "?" + urlencode(params, doseq=True)
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
        return json.loads(body)


def parse_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        value_f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value_f) or math.isinf(value_f):
        return None
    return value_f


def market_text(market: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("question", "title", "slug", "description", "resolutionSource"):
        value = market.get(key)
        if value is not None and str(value).strip():
            parts.append(str(value).strip())
    return " | ".join(parts)


def market_core_text(market: dict[str, Any]) -> str:
    """Text safe for ticker/rule parsing.

    Resolution/source text often contains exchange names such as Nasdaq and can
    create false stock matches. Use only the visible market identity here.
    """

    parts: list[str] = []
    for key in ("question", "title", "slug"):
        value = market.get(key)
        if value is not None and str(value).strip():
            parts.append(str(value).strip())
    return " | ".join(parts)


def market_str(market: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = market.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def parse_end(end_date: str | None) -> tuple[datetime | None, float | None]:
    if not end_date:
        return None, None
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None, None
    return dt, (dt - datetime.now(UTC)).total_seconds()


def parse_market_datetime(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def market_key(market: dict[str, Any]) -> str:
    return (
        market_str(market, "conditionId")
        or market_str(market, "id")
        or market_str(market, "slug")
        or market_text(market)
    )


TICKERS: tuple[TickerSpec, ...] = (
    TickerSpec("NVDA", "NVDA", "nvda.us", ("nvidia", "$nvda", " nvda "), 0.55, "Yahoo Finance NVDA / Nasdaq official close"),
    TickerSpec("TSLA", "TSLA", "tsla.us", ("tesla", "$tsla", " tsla "), 0.65, "Yahoo Finance TSLA / Nasdaq official close"),
    TickerSpec("AAPL", "AAPL", "aapl.us", ("apple", "$aapl", " aapl "), 0.28, "Yahoo Finance AAPL / Nasdaq official close"),
    TickerSpec("MSFT", "MSFT", "msft.us", ("microsoft", "$msft", " msft "), 0.25, "Yahoo Finance MSFT / Nasdaq official close"),
    TickerSpec("META", "META", "meta.us", ("meta platforms", "facebook stock", "$meta", " meta "), 0.38, "Yahoo Finance META / Nasdaq official close"),
    TickerSpec("AMZN", "AMZN", "amzn.us", ("amazon", "$amzn", " amzn "), 0.35, "Yahoo Finance AMZN / Nasdaq official close"),
    TickerSpec("GOOGL", "GOOGL", "googl.us", ("alphabet", "google stock", "$googl", " googl "), 0.30, "Yahoo Finance GOOGL / Nasdaq official close"),
    TickerSpec("AMD", "AMD", "amd.us", ("advanced micro devices", "$amd", " amd "), 0.55, "Yahoo Finance AMD / Nasdaq official close"),
    TickerSpec("PLTR", "PLTR", "pltr.us", ("palantir", "$pltr", " pltr "), 0.65, "Yahoo Finance PLTR / NYSE official close"),
    TickerSpec("COIN", "COIN", "coin.us", ("coinbase", "$coin", " coin "), 0.70, "Yahoo Finance COIN / Nasdaq official close"),
    TickerSpec("MSTR", "MSTR", "mstr.us", ("microstrategy", "strategy stock", "$mstr", " mstr "), 0.85, "Yahoo Finance MSTR / Nasdaq official close"),
    TickerSpec("SPX", "^GSPC", None, ("s&p 500", "s&p500", "spx", "sp500"), 0.18, "Yahoo Finance ^GSPC / S&P Dow Jones index close"),
    TickerSpec("NASDAQ", "^IXIC", None, ("nasdaq composite", "nasdaq", "ixic"), 0.22, "Yahoo Finance ^IXIC / Nasdaq index close"),
    TickerSpec("DOW", "^DJI", None, ("dow jones", "dow industrial", "dow "), 0.17, "Yahoo Finance ^DJI / Dow Jones index close"),
    TickerSpec("QQQ", "QQQ", "qqq.us", ("invesco qqq", "$qqq", " qqq "), 0.24, "Yahoo Finance QQQ / ETF close"),
    TickerSpec("SPY", "SPY", "spy.us", ("spdr s&p", "$spy", " spy "), 0.18, "Yahoo Finance SPY / ETF close"),
)

STOCK_SEARCH_QUERIES = (
    "stock above",
    "stock close above",
    "Nvidia stock",
    "Tesla stock",
    "Apple stock",
    "Microsoft stock",
    "Meta stock",
    "Amazon stock",
    "Google stock",
    "Palantir stock",
    "AMD stock",
    "Coinbase stock",
    "MicroStrategy stock",
    "S&P 500",
    "Nasdaq",
    "Dow Jones",
    "SPY",
    "QQQ",
)

NEWS_SEARCH_QUERIES = (
    "Federal Reserve",
    "Fed interest rates",
    "CPI inflation",
    "jobs report",
    "unemployment",
    "SEC approval",
    "ETF approval",
    "earnings",
    "acquisition",
    "ceasefire",
    "Ukraine",
    "White House",
    "Supreme Court",
    "resign",
    "tariff",
    "sanctions",
)

DIRECTION_PATTERN = re.compile(
    r"(?P<direction>at\s+or\s+above|at\s+or\s+below|above|over|below|under|"
    r"higher\s+than|greater\s+than|less\s+than|close\s+above|close\s+below|"
    r"finish\s+above|finish\s+below|end\s+above|end\s+below)"
    r"[^0-9$]{0,40}\$?\s*(?P<number>[0-9][0-9,]*(?:\.\d+)?)",
    re.IGNORECASE,
)

NEWS_RULES: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    (
        "official_macro",
        "high",
        "BLS/BEA/FRED or scheduled government release",
        "Use the official release calendar/RSS first; track latency from release timestamp to Polymarket move.",
        ("cpi", "inflation", "jobs report", "unemployment", "payrolls", "pce", "gdp"),
    ),
    (
        "central_bank",
        "high",
        "Federal Reserve FOMC statement/calendar",
        "Map market wording to FOMC statement, dot plot, or rate target page before any paper signal.",
        ("federal reserve", "fomc", "fed rate", "interest rate", "rate cut", "rate hike"),
    ),
    (
        "sec_or_etf",
        "medium_high",
        "SEC EDGAR / exchange filings / issuer press release",
        "Prefer SEC/exchange filing timestamps over headlines; log false rumors separately.",
        ("sec", "etf", "approval", "filing", "spot etf"),
    ),
    (
        "company_event",
        "medium_high",
        "Company investor-relations release, SEC 8-K, earnings calendar",
        "Track official IR/SEC feed; do not trade rumor headlines without confirmation.",
        ("earnings", "revenue", "acquisition", "merger", "ipo", "guidance", "bankruptcy"),
    ),
    (
        "government_policy",
        "medium",
        "White House/Congress/agency official source",
        "Only paper-alert on official statements, bill text, or agency publication; headlines are leads only.",
        ("white house", "congress", "senate", "house", "tariff", "sanction", "executive order"),
    ),
    (
        "court_legal",
        "medium",
        "Court docket/opinion page",
        "Use docket/opinion source as resolver; news articles are secondary confirmation.",
        ("supreme court", "court", "judge", "lawsuit", "verdict", "ruling"),
    ),
    (
        "geopolitical",
        "low_medium",
        "Official government/UN/NATO statements; multiple-source confirmation needed",
        "High false-positive risk. Build source map before any timed paper entries.",
        ("ceasefire", "ukraine", "russia", "israel", "iran", "war", "nato", "peace deal"),
    ),
)


def fetch_gamma_markets(max_markets: int, page_size: int = 100) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    offset = 0
    while len(markets) < max_markets:
        limit = min(page_size, max_markets - len(markets))
        try:
            payload = get_json(
                f"{GAMMA}/markets",
                {
                    "active": "true",
                    "closed": "false",
                    "archived": "false",
                    "limit": limit,
                    "offset": offset,
                },
            )
        except Exception:
            break
        if not isinstance(payload, list) or not payload:
            break
        markets.extend([m for m in payload if isinstance(m, dict)])
        if len(payload) < limit:
            break
        offset += limit
    return markets[:max_markets]


def public_search_markets(query: str, limit: int) -> list[dict[str, Any]]:
    try:
        payload = get_json(f"{GAMMA}/public-search", {"q": query, "limit": limit})
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    containers: list[Any] = []
    for key in ("events", "results", "markets"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, list):
            containers.extend(value)
    for item in containers:
        if not isinstance(item, dict):
            continue
        markets = item.get("markets")
        if isinstance(markets, list) and markets:
            for market in markets:
                if isinstance(market, dict):
                    merged = dict(market)
                    for key in ("title", "slug", "startDate", "endDate", "active", "closed"):
                        merged.setdefault(key, item.get(key))
                    out.append(merged)
        else:
            out.append(item)
    return out


def collect_market_pool(max_markets: int, search_limit: int, include_search: bool = True) -> list[dict[str, Any]]:
    pool = fetch_gamma_markets(max_markets=max_markets)
    if include_search:
        for query in (*STOCK_SEARCH_QUERIES, *NEWS_SEARCH_QUERIES):
            pool.extend(public_search_markets(query, limit=search_limit))
            time.sleep(0.02)
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for market in pool:
        if str(market.get("closed", "")).lower() in {"true", "1"}:
            continue
        key = market_key(market)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(market)
    return unique


def _utc_now_ms() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _book_provider_time(payload: Any) -> str | None:
    value = payload.get("timestamp") if isinstance(payload, dict) else None
    number = to_float(value)
    if number is None:
        return None
    if number > 10_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def top_book(token_id: str, outcome: str) -> BookTop:
    request_started_at = _utc_now_ms()
    try:
        payload = get_json(f"{CLOB}/book", {"token_id": token_id}, timeout=12.0)
        response_received_at = _utc_now_ms()
    except HTTPError as exc:
        return BookTop(token_id, outcome, None, None, None, None, None, False, f"HTTP {exc.code}")
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        return BookTop(token_id, outcome, None, None, None, None, None, False, type(exc).__name__)

    bids = _parse_book_levels(payload.get("bids")) if isinstance(payload, dict) else []
    asks = _parse_book_levels(payload.get("asks")) if isinstance(payload, dict) else []
    bid = max((price for price, _size in bids), default=None)
    ask = min((price for price, _size in asks), default=None)
    bid_size = next((size for price, size in bids if price == bid), None) if bid is not None else None
    ask_size = next((size for price, size in asks if price == ask), None) if ask is not None else None
    spread = ask - bid if ask is not None and bid is not None else None
    return BookTop(
        token_id, outcome, bid, bid_size, ask, ask_size, spread, True,
        request_started_at=request_started_at,
        response_received_at=response_received_at,
        provider_timestamp=_book_provider_time(payload),
        bids=bids,
        asks=asks,
    )


def _parse_book_levels(value: Any) -> list[tuple[float, float]]:
    if not isinstance(value, list):
        return []
    out: list[tuple[float, float]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        price = to_float(item.get("price"))
        size = to_float(item.get("size"))
        if price is not None and size is not None:
            out.append((price, size))
    return out


def detect_ticker(text: str) -> TickerSpec | None:
    normalized = f" {text.lower().replace('-', ' ')} "
    by_symbol = {spec.symbol: spec for spec in TICKERS}

    # Strong signals first: explicit ticker notation in the market title.
    for raw in re.findall(r"\(([A-Za-z]{1,8})\)", text):
        symbol = raw.upper()
        if symbol in by_symbol:
            return by_symbol[symbol]
    for raw in re.findall(r"\$([A-Za-z]{1,8})\b", text):
        symbol = raw.upper()
        if symbol in by_symbol:
            return by_symbol[symbol]

    # Then exact supported symbol words.
    for spec in TICKERS:
        symbol_word = re.compile(rf"(?<![a-z0-9]){re.escape(spec.symbol.lower())}(?![a-z0-9])")
        if symbol_word.search(normalized):
            return spec

    # Finally descriptive aliases such as "S&P 500" or "Nvidia".
    for spec in TICKERS:
        for alias in spec.aliases:
            alias_norm = alias.lower()
            if alias_norm.startswith("$"):
                if alias_norm in normalized:
                    return spec
            elif alias_norm.strip() in normalized:
                return spec
    return None


def is_price_threshold_market_text(text: str) -> bool:
    lowered = text.lower()
    if any(term in lowered for term in ("earnings", "eps", "revenue", "profit", "sales", "guidance")):
        return False
    if any(term in lowered for term in ("round-the-clock", "round the clock", "24/7", "listing", "approval")):
        return False
    return True


def detect_threshold_direction(text: str) -> tuple[float | None, str | None]:
    matches = list(DIRECTION_PATTERN.finditer(text))
    if not matches:
        return None, None
    # Prefer the last directional price, because questions often start with dates/series names.
    match = matches[-1]
    number = to_float(match.group("number").replace(",", ""))
    direction_text = match.group("direction").lower()
    if number is None:
        return None, None
    if number < 0.01 or number > 100_000:
        return None, None
    direction = "below" if any(word in direction_text for word in ("below", "under", "less")) else "above"
    return number, direction


def fetch_external_price(spec: TickerSpec) -> ExternalPrice:
    errors: list[str] = []
    try:
        return fetch_yahoo_price(spec)
    except Exception as exc:
        errors.append(f"yahoo:{type(exc).__name__}")
    if spec.stooq_symbol:
        try:
            return fetch_stooq_price(spec)
        except Exception as exc:
            errors.append(f"stooq:{type(exc).__name__}")
    raise RuntimeError(f"No public price for {spec.symbol}: {'; '.join(errors)}")


def fetch_yahoo_price(spec: TickerSpec) -> ExternalPrice:
    payload = get_json(
        f"{YAHOO_CHART}/{quote(spec.yahoo_symbol, safe='')}",
        {"range": "1d", "interval": "1m"},
        timeout=12.0,
    )
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        raise RuntimeError("missing chart result")
    meta = result.get("meta") or {}
    price = to_float(meta.get("regularMarketPrice") or meta.get("previousClose"))
    if price is None:
        raise RuntimeError("missing regularMarketPrice")
    return ExternalPrice(
        symbol=spec.symbol,
        price=price,
        retrieved_at_utc=utc_now(),
        source="yahoo_chart",
        raw_symbol=spec.yahoo_symbol,
    )


def fetch_stooq_price(spec: TickerSpec) -> ExternalPrice:
    if spec.stooq_symbol is None:
        raise RuntimeError("no stooq symbol")
    url = STOOQ_QUOTE + "?" + urlencode({"s": spec.stooq_symbol, "f": "sd2t2ohlcv", "h": "", "e": "csv"})
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=12.0) as resp:
        text = resp.read().decode("utf-8", "replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise RuntimeError("missing stooq row")
    row = next(csv.DictReader(lines))
    price = to_float(row.get("Close"))
    if price is None:
        raise RuntimeError("missing stooq close")
    return ExternalPrice(
        symbol=spec.symbol,
        price=price,
        retrieved_at_utc=utc_now(),
        source="stooq_delayed_csv",
        raw_symbol=spec.stooq_symbol,
    )


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def probability_above_threshold(
    *,
    current_price: float,
    threshold: float,
    seconds_to_end: float | None,
    annual_volatility: float,
) -> tuple[float, str]:
    if current_price <= 0 or threshold <= 0:
        return 0.5, "invalid current price or threshold"
    if seconds_to_end is None:
        distance = (current_price - threshold) / threshold
        fair = 0.75 if distance > 0 else 0.25 if distance < 0 else 0.5
        return fair, "no end time; rough direction-only fallback"
    if seconds_to_end <= 0:
        fair = 1.0 if current_price > threshold else 0.0 if current_price < threshold else 0.5
        return fair, "market end time has passed; deterministic from latest public price"
    years = max(seconds_to_end, 60.0) / SECONDS_PER_YEAR
    denominator = max(annual_volatility, 0.01) * math.sqrt(years)
    z = math.log(current_price / threshold) / denominator
    fair = normal_cdf(z)
    return fair, f"lognormal terminal-price model: z={z:.3f}, vol={annual_volatility:.2f}, years={years:.5f}"


def binary_books_for_market(market: dict[str, Any], budget: BookBudget) -> dict[str, BookTop]:
    outcomes = [str(x) for x in parse_list(market.get("outcomes"))]
    token_ids = [str(x) for x in parse_list(market.get("clobTokenIds"))]
    books: dict[str, BookTop] = {}
    if len(outcomes) != len(token_ids) or not token_ids:
        return books
    for outcome, token_id in zip(outcomes, token_ids, strict=False):
        if not budget.consume():
            break
        book = top_book(token_id, outcome)
        if not book.available:
            budget.errors += 1
        books[outcome.lower()] = book
        time.sleep(0.02)
    return books


def scan_stock_markets(
    markets: list[dict[str, Any]],
    *,
    max_books: int,
    min_edge: float,
    min_size: float,
    max_ask: float,
    include_all_sides: bool = False,
) -> tuple[list[StockCandidate], dict[str, Any]]:
    price_cache: dict[str, ExternalPrice | Exception] = {}
    budget = BookBudget(max_books)
    candidates: list[StockCandidate] = []
    stats = {
        "markets_seen": len(markets),
        "stock_like_markets": 0,
        "modeled_markets": 0,
        "unmodeled_or_excluded": 0,
        "book_requests": 0,
        "book_errors": 0,
        "actionable_candidates": 0,
    }

    for market in markets:
        text = market_core_text(market)
        spec = detect_ticker(text)
        if spec is None:
            continue
        stats["stock_like_markets"] += 1
        end_date = parse_market_datetime(market.get("endDate"))
        _end_dt, seconds_to_end = parse_end(end_date)
        threshold, direction = detect_threshold_direction(text)
        slug = market_str(market, "slug")
        question = market_str(market, "question", "title")
        condition_id = market_str(market, "conditionId")

        if threshold is None or direction is None or not is_price_threshold_market_text(text):
            stats["unmodeled_or_excluded"] += 1
            continue

        if spec.symbol not in price_cache:
            try:
                price_cache[spec.symbol] = fetch_external_price(spec)
            except Exception as exc:  # keep scan going
                price_cache[spec.symbol] = exc
        price_or_error = price_cache[spec.symbol]
        if isinstance(price_or_error, Exception):
            candidates.append(
                StockCandidate(
                    severity="watchlist",
                    actionable=False,
                    ticker=spec.symbol,
                    side="NONE",
                    outcome="N/A",
                    slug=slug,
                    question=question,
                    condition_id=condition_id,
                    end_date=end_date,
                    seconds_to_end=seconds_to_end,
                    current_price=None,
                    price_source=None,
                    threshold=threshold,
                    direction=direction,
                    fair_probability=None,
                    bid=None,
                    ask=None,
                    ask_size=None,
                    edge=None,
                    model_reason=f"external price unavailable: {price_or_error}",
                    source_hint=spec.source_hint,
                    note="Watchlist only until public reference price is available.",
                )
            )
            continue

        price = price_or_error
        above_prob, reason = probability_above_threshold(
            current_price=price.price,
            threshold=threshold,
            seconds_to_end=seconds_to_end,
            annual_volatility=spec.annual_volatility,
        )
        yes_fair = above_prob if direction == "above" else 1.0 - above_prob
        no_fair = 1.0 - yes_fair
        stats["modeled_markets"] += 1

        books = binary_books_for_market(market, budget)
        stats["book_requests"] = budget.requests
        stats["book_errors"] = budget.errors
        yes_book = _find_book(books, ("yes", "above", "higher"))
        no_book = _find_book(books, ("no", "below", "lower"))

        rows = [
            _stock_candidate_from_book(
                market=market,
                spec=spec,
                price=price,
                threshold=threshold,
                direction=direction,
                fair=yes_fair,
                side="YES",
                book=yes_book,
                seconds_to_end=seconds_to_end,
                reason=reason,
                min_edge=min_edge,
                min_size=min_size,
                max_ask=max_ask,
            ),
            _stock_candidate_from_book(
                market=market,
                spec=spec,
                price=price,
                threshold=threshold,
                direction=direction,
                fair=no_fair,
                side="NO",
                book=no_book,
                seconds_to_end=seconds_to_end,
                reason=reason,
                min_edge=min_edge,
                min_size=min_size,
                max_ask=max_ask,
            ),
        ]
        # Normal reports stay concise; paper ledgers can request both YES/NO
        # rows so existing positions can be marked even when their side is not
        # the current best candidate.
        if include_all_sides:
            candidates.extend(rows)
        else:
            actionable_rows = [row for row in rows if row.actionable]
            if actionable_rows:
                candidates.extend(actionable_rows)
            else:
                candidates.append(max(rows, key=lambda row: row.edge if row.edge is not None else -999.0))

    stats["actionable_candidates"] = sum(1 for c in candidates if c.actionable)
    candidates.sort(
        key=lambda c: (
            0 if c.actionable else 1,
            {"high": 0, "medium": 1, "watchlist": 2, "blocked": 3}.get(c.severity, 9),
            -(c.edge or -999.0),
        )
    )
    return candidates, stats


def _find_book(books: dict[str, BookTop], names: tuple[str, ...]) -> BookTop | None:
    for name, book in books.items():
        if any(alias in name for alias in names):
            return book
    return None


def _stock_candidate_from_book(
    *,
    market: dict[str, Any],
    spec: TickerSpec,
    price: ExternalPrice,
    threshold: float,
    direction: str,
    fair: float,
    side: str,
    book: BookTop | None,
    seconds_to_end: float | None,
    reason: str,
    min_edge: float,
    min_size: float,
    max_ask: float,
) -> StockCandidate:
    end_date = parse_market_datetime(market.get("endDate"))
    ask = book.ask if book is not None else None
    bid = book.bid if book is not None else None
    ask_size = book.ask_size if book is not None else None
    edge = fair - ask if ask is not None else None
    has_size = ask_size is not None and ask_size >= min_size
    actionable = (
        edge is not None
        and edge >= min_edge
        and ask is not None
        and ask <= max_ask
        and has_size
        and (seconds_to_end is None or seconds_to_end > 0)
    )
    if actionable and edge is not None and edge >= 0.20:
        severity = "high"
    elif actionable:
        severity = "medium"
    elif book is None:
        severity = "blocked"
    else:
        severity = "watchlist"
    if book is None:
        note = "No public CLOB book fetched or book budget exhausted; not executable even on paper."
        outcome = "N/A"
    elif not book.available:
        note = f"Book unavailable: {book.error}"
        outcome = book.outcome
    elif not has_size:
        note = f"Top ask size below {min_size:.2f}; liquidity too thin for paper candidate."
        outcome = book.outcome
    elif edge is None or edge < min_edge:
        note = f"Edge below {min_edge:.2f}; keep as watchlist only."
        outcome = book.outcome
    elif ask is not None and ask > max_ask:
        note = f"Ask above max_ask {max_ask:.2f}; avoid overpaying."
        outcome = book.outcome
    else:
        note = "Paper candidate only: external price model beats visible ask after liquidity filter."
        outcome = book.outcome
    return StockCandidate(
        severity=severity,
        actionable=actionable,
        ticker=spec.symbol,
        side=side,
        outcome=outcome,
        slug=market_str(market, "slug"),
        question=market_str(market, "question", "title"),
        condition_id=market_str(market, "conditionId"),
        end_date=end_date,
        seconds_to_end=seconds_to_end,
        current_price=price.price,
        price_source=price.source,
        threshold=threshold,
        direction=direction,
        fair_probability=_clamp(fair),
        bid=bid,
        ask=ask,
        ask_size=ask_size,
        edge=edge,
        model_reason=reason,
        source_hint=spec.source_hint,
        note=note,
        market_id=market_str(market, "id") or None,
        token_id=book.token_id if book is not None else None,
        bid_size=book.bid_size if book is not None else None,
        book_request_started_at=book.request_started_at if book is not None else None,
        book_response_received_at=book.response_received_at if book is not None else None,
        book_provider_timestamp=book.provider_timestamp if book is not None else None,
        book_timing_quality=("exact_request_response" if book is not None and book.request_started_at and book.response_received_at else None),
        book_bids=book.bids if book is not None else None,
        book_asks=book.asks if book is not None else None,
    )


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def scan_news_markets(markets: list[dict[str, Any]], limit: int = 80) -> tuple[list[NewsCandidate], dict[str, Any]]:
    candidates: list[NewsCandidate] = []
    for market in markets:
        text = market_text(market).lower()
        if not text:
            continue
        for category, readiness, source_hint, next_step, keywords in NEWS_RULES:
            matched = [keyword for keyword in keywords if keyword in text]
            if not matched:
                continue
            end_date = parse_market_datetime(market.get("endDate"))
            _end_dt, seconds_to_end = parse_end(end_date)
            candidates.append(
                NewsCandidate(
                    readiness=readiness,
                    category=category,
                    slug=market_str(market, "slug"),
                    question=market_str(market, "question", "title"),
                    condition_id=market_str(market, "conditionId"),
                    end_date=end_date,
                    seconds_to_end=seconds_to_end,
                    source_hint=source_hint,
                    resolution_source=market_str(market, "resolutionSource") or None,
                    why=f"matched: {', '.join(matched[:5])}",
                    next_step=next_step,
                )
            )
            break

    rank = {"high": 0, "medium_high": 1, "medium": 2, "low_medium": 3, "low": 4}
    candidates.sort(key=lambda c: (rank.get(c.readiness, 9), c.seconds_to_end or 10**12, c.category))
    stats = {
        "markets_seen": len(markets),
        "news_watchlist_candidates": len(candidates),
        "high_or_medium_high": sum(1 for c in candidates if c.readiness in {"high", "medium_high"}),
    }
    return candidates[:limit], stats


def load_wallets(cli_wallets: list[str] | None, wallet_file: Path) -> list[str]:
    wallets: list[str] = []
    if cli_wallets:
        wallets.extend(cli_wallets)
    if wallet_file.exists():
        for line in wallet_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            wallets.append(line.split()[0])
    seen: set[str] = set()
    out: list[str] = []
    for wallet in wallets:
        normalized = wallet.strip()
        if not re.fullmatch(r"0x[a-fA-F0-9]{40}", normalized):
            continue
        lower = normalized.lower()
        if lower in seen:
            continue
        seen.add(lower)
        out.append(normalized)
    return out


def scan_wallets(wallets: list[str], limit: int = 500) -> tuple[list[WalletSummary], dict[str, Any]]:
    if not wallets:
        return [
            WalletSummary(
                wallet="",
                status="needs_wallets",
                trades_fetched=0,
                closed_positions_fetched=0,
                open_positions_fetched=0,
                unique_markets=0,
                visible_notional=None,
                realized_pnl_sum=None,
                win_count=None,
                loss_count=None,
                notes=f"Add public wallet addresses to {DEFAULT_WALLET_FILE} or pass --wallet 0x... to rank copy-trade candidates.",
            )
        ], {"wallets_requested": 0, "wallets_scanned": 0}

    summaries: list[WalletSummary] = []
    for wallet in wallets:
        try:
            trades = _fetch_data_api_list("/trades", {"user": wallet, "limit": limit, "offset": 0, "takerOnly": "false"})
            closed = _fetch_data_api_list("/closed-positions", {"user": wallet, "limit": limit, "sizeThreshold": 0})
            open_positions = _fetch_data_api_list("/positions", {"user": wallet, "limit": limit, "sizeThreshold": 0})
        except Exception as exc:
            summaries.append(
                WalletSummary(wallet, "error", 0, 0, 0, 0, None, None, None, None, f"Data API error: {exc}")
            )
            continue

        markets = set()
        notional = 0.0
        notional_seen = False
        for trade in trades:
            markets.add(str(trade.get("conditionId") or trade.get("market") or trade.get("title") or ""))
            size = to_float(trade.get("size") or trade.get("amount"))
            price = to_float(trade.get("price"))
            if size is not None and price is not None:
                notional += abs(size * price)
                notional_seen = True

        pnl_values: list[float] = []
        win_count = 0
        loss_count = 0
        for item in closed:
            pnl = _extract_pnl(item)
            if pnl is None:
                continue
            pnl_values.append(pnl)
            if pnl > 0:
                win_count += 1
            elif pnl < 0:
                loss_count += 1
        pnl_sum = sum(pnl_values) if pnl_values else None
        summaries.append(
            WalletSummary(
                wallet=wallet,
                status="ok",
                trades_fetched=len(trades),
                closed_positions_fetched=len(closed),
                open_positions_fetched=len(open_positions),
                unique_markets=len([m for m in markets if m]),
                visible_notional=round(notional, 4) if notional_seen else None,
                realized_pnl_sum=round(pnl_sum, 4) if pnl_sum is not None else None,
                win_count=win_count if pnl_values else None,
                loss_count=loss_count if pnl_values else None,
                notes="Historical public-data summary only. Must paper-follow future trades before copying anything.",
            )
        )
        time.sleep(0.05)

    summaries.sort(
        key=lambda s: (
            0 if s.status == "ok" else 1,
            -(s.realized_pnl_sum if s.realized_pnl_sum is not None else -10**9),
            -s.trades_fetched,
        )
    )
    stats = {"wallets_requested": len(wallets), "wallets_scanned": sum(1 for s in summaries if s.status == "ok")}
    return summaries, stats


def _fetch_data_api_list(path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    payload = get_json(f"{DATA_API}{path}", params, timeout=20.0)
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _extract_pnl(item: dict[str, Any]) -> float | None:
    for key in (
        "realizedPnl",
        "realizedPNL",
        "pnl",
        "profit",
        "cashPnl",
        "totalPnl",
        "closedPnl",
    ):
        value = to_float(item.get(key))
        if value is not None:
            return value
    return None


def write_outputs(report: dict[str, Any], outdir: Path) -> dict[str, Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = report["retrieved_at_utc"].replace(":", "").replace("-", "")
    json_latest = outdir / "multi_market_research_latest.json"
    md_latest = outdir / "multi_market_research_latest.md"
    stock_csv_latest = outdir / "multi_market_stock_candidates_latest.csv"
    json_path = outdir / f"multi_market_research_{stamp}.json"
    md_path = outdir / f"multi_market_research_{stamp}.md"
    stock_csv_path = outdir / f"multi_market_stock_candidates_{stamp}.csv"

    payload = json.dumps(report, indent=2, ensure_ascii=False)
    json_latest.write_text(payload, encoding="utf-8")
    json_path.write_text(payload, encoding="utf-8")
    markdown = render_markdown(report)
    md_latest.write_text(markdown, encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")

    stock_rows = report.get("stock", {}).get("candidates", [])
    fields = list(StockCandidate.__annotations__.keys())
    for csv_path in (stock_csv_latest, stock_csv_path):
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for row in stock_rows:
                writer.writerow({field: row.get(field) for field in fields})

    return {
        "json_latest": json_latest,
        "md_latest": md_latest,
        "stock_csv_latest": stock_csv_latest,
        "json_timestamped": json_path,
        "md_timestamped": md_path,
        "stock_csv_timestamped": stock_csv_path,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Multi-market Polymarket paper research")
    lines.append("")
    lines.append(f"Retrieved: `{report['retrieved_at_utc']}`")
    lines.append("")
    lines.append("Safety boundary: **paper-only public-data research**. No private keys, no authenticated trading endpoints, no real orders.")
    lines.append("")

    stock = report.get("stock")
    if stock:
        stats = stock.get("stats", {})
        candidates = stock.get("candidates", [])
        lines.append("## Stock / price-market module")
        lines.append("")
        lines.append(
            f"Scanned `{stats.get('markets_seen', 0)}` markets; "
            f"stock-like `{stats.get('stock_like_markets', 0)}`; "
            f"modeled `{stats.get('modeled_markets', 0)}`; "
            f"excluded/unmodeled `{stats.get('unmodeled_or_excluded', 0)}`; "
            f"actionable paper candidates `{stats.get('actionable_candidates', 0)}`."
        )
        lines.append("")
        lines.append("| status | ticker | side | question | price vs threshold | fair | ask | edge | note |")
        lines.append("|---|---:|---:|---|---|---:|---:|---:|---|")
        for row in candidates[:20]:
            price_threshold = _fmt_price_threshold(row)
            lines.append(
                "| "
                f"{_md(row.get('severity'))}{' ✅' if row.get('actionable') else ''} | "
                f"{_md(row.get('ticker'))} | "
                f"{_md(row.get('side'))} | "
                f"{_md(_short(row.get('question') or row.get('slug'), 92))} | "
                f"{_md(price_threshold)} | "
                f"{_fmt_pct(row.get('fair_probability'))} | "
                f"{_fmt_num(row.get('ask'))} | "
                f"{_fmt_num(row.get('edge'))} | "
                f"{_md(_short(row.get('note'), 90))} |"
            )
        lines.append("")

    news = report.get("news")
    if news:
        stats = news.get("stats", {})
        candidates = news.get("candidates", [])
        lines.append("## Real-time news/event module")
        lines.append("")
        lines.append(
            f"Watchlist candidates: `{stats.get('news_watchlist_candidates', 0)}`; "
            f"high/medium-high source readiness: `{stats.get('high_or_medium_high', 0)}`."
        )
        lines.append("")
        lines.append("| readiness | category | question | source hint | next step |")
        lines.append("|---|---|---|---|---|")
        for row in candidates[:20]:
            lines.append(
                "| "
                f"{_md(row.get('readiness'))} | "
                f"{_md(row.get('category'))} | "
                f"{_md(_short(row.get('question') or row.get('slug'), 92))} | "
                f"{_md(_short(row.get('source_hint'), 70))} | "
                f"{_md(_short(row.get('next_step'), 90))} |"
            )
        lines.append("")

    wallet = report.get("wallet")
    if wallet:
        stats = wallet.get("stats", {})
        summaries = wallet.get("summaries", [])
        lines.append("## Public wallet / copy-trade research module")
        lines.append("")
        lines.append(
            f"Wallets requested: `{stats.get('wallets_requested', 0)}`; "
            f"wallets scanned: `{stats.get('wallets_scanned', 0)}`."
        )
        lines.append("")
        lines.append("| wallet | status | trades | closed | pnl sum | note |")
        lines.append("|---|---|---:|---:|---:|---|")
        for row in summaries[:20]:
            lines.append(
                "| "
                f"{_md(_short(row.get('wallet') or 'none', 16))} | "
                f"{_md(row.get('status'))} | "
                f"{row.get('trades_fetched', 0)} | "
                f"{row.get('closed_positions_fetched', 0)} | "
                f"{_fmt_num(row.get('realized_pnl_sum'))} | "
                f"{_md(_short(row.get('notes'), 100))} |"
            )
        lines.append("")

    lines.append("## Interpretation")
    lines.append("")
    lines.append("- Stock/price candidates are only useful when the market rule is a clean above/below threshold and the visible CLOB ask leaves enough edge.")
    lines.append("- News candidates are watchlist items until each market has an authoritative source map and false-positive log.")
    lines.append("- Wallet research is historical only until we paper-follow future entries after discovering a wallet.")
    lines.append("- Nothing here is live-ready without resolved paper PnL, spread/depth checks, and latency assumptions.")
    lines.append("")
    return "\n".join(lines)


def _fmt_price_threshold(row: dict[str, Any]) -> str:
    price = row.get("current_price")
    threshold = row.get("threshold")
    direction = row.get("direction")
    if price is None or threshold is None:
        return "n/a"
    return f"{float(price):.2f} vs {direction} {float(threshold):.2f}"


def _fmt_pct(value: Any) -> str:
    f = to_float(value)
    return "" if f is None else f"{f:.1%}"


def _fmt_num(value: Any) -> str:
    f = to_float(value)
    return "" if f is None else f"{f:.4f}"


def _short(value: Any, n: int) -> str:
    text = "" if value is None else str(value).replace("\n", " ").strip()
    if len(text) <= n:
        return text
    return text[: max(0, n - 1)].rstrip() + "…"


def _md(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def run_self_tests() -> None:
    assert parse_list('["Yes", "No"]') == ["Yes", "No"]
    spec = detect_ticker("Will Nvidia close above $180 on Friday?")
    assert spec is not None and spec.symbol == "NVDA"
    spy_spec = detect_ticker("S&P 500 (SPY) closes above $775 on July 15?")
    assert spy_spec is not None and spy_spec.symbol == "SPY"
    threshold, direction = detect_threshold_direction("Will Nvidia close above $180 on Friday?")
    assert threshold == 180.0 and direction == "above"
    assert not is_price_threshold_market_text("Will IBM (IBM) beat quarterly earnings?")
    assert is_price_threshold_market_text("Will Apple (AAPL) close above $340 this week?")
    fair, _reason = probability_above_threshold(current_price=110, threshold=100, seconds_to_end=86400, annual_volatility=0.3)
    assert fair > 0.5
    fair2, _reason2 = probability_above_threshold(current_price=90, threshold=100, seconds_to_end=86400, annual_volatility=0.3)
    assert fair2 < 0.5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket multi-market paper research scanner.")
    parser.add_argument("--modules", nargs="+", default=["stock"], choices=("all", "stock", "news", "wallet"))
    parser.add_argument("--max-markets", type=int, default=500)
    parser.add_argument("--search-limit", type=int, default=40)
    parser.add_argument("--max-books", type=int, default=80)
    parser.add_argument("--min-edge", type=float, default=0.08)
    parser.add_argument("--min-size", type=float, default=5.0)
    parser.add_argument("--max-ask", type=float, default=0.85)
    parser.add_argument("--wallet", action="append", help="Public wallet address to analyze. Repeatable.")
    parser.add_argument("--wallet-file", type=Path, default=DEFAULT_WALLET_FILE)
    parser.add_argument("--outdir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--self-test", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        run_self_tests()
        print("self-tests passed")
        return 0

    modules = set(args.modules)
    if "all" in modules:
        modules = {"stock", "news", "wallet"}

    need_markets = bool({"stock", "news"} & modules)
    markets = collect_market_pool(args.max_markets, args.search_limit) if need_markets else []
    report: dict[str, Any] = {
        "retrieved_at_utc": utc_now(),
        "paper_only": True,
        "modules": sorted(modules),
        "market_pool_count": len(markets),
        "parameters": {
            "max_markets": args.max_markets,
            "search_limit": args.search_limit,
            "max_books": args.max_books,
            "min_edge": args.min_edge,
            "min_size": args.min_size,
            "max_ask": args.max_ask,
        },
    }

    if "stock" in modules:
        candidates, stats = scan_stock_markets(
            markets,
            max_books=args.max_books,
            min_edge=args.min_edge,
            min_size=args.min_size,
            max_ask=args.max_ask,
        )
        report["stock"] = {"stats": stats, "candidates": [asdict(c) for c in candidates]}

    if "news" in modules:
        candidates, stats = scan_news_markets(markets)
        report["news"] = {"stats": stats, "candidates": [asdict(c) for c in candidates]}

    if "wallet" in modules:
        wallets = load_wallets(args.wallet, args.wallet_file)
        summaries, stats = scan_wallets(wallets)
        report["wallet"] = {"stats": stats, "summaries": [asdict(s) for s in summaries]}

    paths = write_outputs(report, args.outdir)
    print(json.dumps({"retrieved_at_utc": report["retrieved_at_utc"], "paths": {k: str(v) for k, v in paths.items()}}, indent=2))
    if "stock" in report:
        s = report["stock"]["stats"]
        print(
            f"stock: stock_like={s['stock_like_markets']} modeled={s['modeled_markets']} "
            f"actionable={s['actionable_candidates']} book_requests={s['book_requests']}"
        )
    if "news" in report:
        s = report["news"]["stats"]
        print(f"news: candidates={s['news_watchlist_candidates']} high_or_medium_high={s['high_or_medium_high']}")
    if "wallet" in report:
        s = report["wallet"]["stats"]
        print(f"wallet: requested={s['wallets_requested']} scanned={s['wallets_scanned']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
