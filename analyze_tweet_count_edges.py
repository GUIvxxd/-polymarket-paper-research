#!/usr/bin/env python3
"""Analyze X count variants vs Polymarket Tweet Market prices.

This is paper/research only. It uses already-collected X count variant data and public Polymarket page prices.
No API calls.
"""
from __future__ import annotations

import json, math
from pathlib import Path

VARIANT_PRIORITY = ["no_replies", "all_from_account", "no_retweets", "original_only"]


def poisson_cdf(k: int, lam: float) -> float:
    if k < 0:
        return 0.0
    # Stable iterative sum for the λ values here.
    term = math.exp(-lam)
    s = term
    for i in range(1, k + 1):
        term *= lam / i
        s += term
    return min(max(s, 0.0), 1.0)


def poisson_range_prob(lo: int | None, hi: int | None, lam: float) -> float:
    if lo is None and hi is not None:
        return poisson_cdf(hi, lam)
    if lo is not None and hi is None:
        return 1.0 - poisson_cdf(lo - 1, lam)
    if lo is not None and hi is not None:
        return poisson_cdf(hi, lam) - poisson_cdf(lo - 1, lam)
    return 0.0


def parse_bucket(bucket: str):
    b = bucket.replace("\\u003c", "<").strip()
    if b.startswith("<"):
        return None, int(b[1:]) - 1
    if b.endswith("+"):
        return int(b[:-1]), None
    if "-" in b:
        a, c = b.split("-", 1)
        return int(a), int(c)
    return None, None


def parse_prices(s: str | None):
    out = []
    for part in (s or "").split("|"):
        if "=" not in part:
            continue
        k, v = part.strip().split("=", 1)
        try:
            out.append((k.strip().replace("\\u003c", "<"), float(v)))
        except Exception:
            pass
    return out


def choose_variant(row: dict) -> str:
    # Use the variant whose projection lands in the current market-leading bucket if possible.
    lead = row.get("market_leading_group")
    lead_norm = str(lead).replace("\\u003c", "<")
    for name in VARIANT_PRIORITY:
        v = row.get("variants", {}).get(name, {})
        for match in v.get("projected_bucket_prices") or []:
            if match.get("bucket") == lead_norm:
                return name
    # Otherwise default to no_replies, which matches the public X Posts tab better than original-only.
    return "no_replies"


def main():
    path = Path("/data/workspace/polymarket-research/reports/x_tweet_count_variants_latest.json")
    data = json.loads(path.read_text())
    rows = []
    for row in data.get("rows", []):
        variant = choose_variant(row)
        v = row.get("variants", {}).get(variant, {})
        if not v.get("ok"):
            continue
        count_now = v.get("count_now")
        projected = float(v.get("linear_projected_final") or 0)
        # Estimate λ remaining from projected - current. This is simplistic but useful near expiry.
        lam = max(projected - count_now, 0.0)
        prices = parse_prices(row.get("market_prices"))
        bucket_rows = []
        for bucket, price in prices:
            lo, hi = parse_bucket(bucket)
            add_lo = None if lo is None else max(lo - count_now, 0)
            add_hi = None if hi is None else hi - count_now
            if add_hi is not None and add_hi < 0:
                fair = 0.0
            else:
                fair = poisson_range_prob(add_lo, add_hi, lam)
            bucket_rows.append({
                "bucket": bucket,
                "market_yes": price,
                "model_fair": round(fair, 4),
                "edge_fair_minus_price": round(fair - price, 4),
            })
        best_buy = max(bucket_rows, key=lambda x: x["edge_fair_minus_price"]) if bucket_rows else None
        best_fade = min(bucket_rows, key=lambda x: x["edge_fair_minus_price"]) if bucket_rows else None
        rows.append({
            "title": row.get("title"),
            "variant_used": variant,
            "count_now": count_now,
            "linear_projected_final": projected,
            "lambda_remaining": round(lam, 2),
            "market_leading": f"{row.get('market_leading_group')}@{row.get('market_leading_yes')}",
            "bucket_model": bucket_rows,
            "best_paper_buy": best_buy,
            "best_paper_fade": best_fade,
            "caveats": [
                "variant/rules not yet fully verified against Polymarket resolution text",
                "Poisson/linear rate model is first-pass; not a proven edge",
                "must verify CLOB bid/ask/depth before simulated trade"
            ],
        })
    out = Path("/data/workspace/polymarket-research/reports/x_tweet_count_edge_analysis_latest.json")
    out.write_text(json.dumps({"source": str(path), "rows": rows}, indent=2, ensure_ascii=False))
    top = sorted(rows, key=lambda r: (r.get("best_paper_buy") or {}).get("edge_fair_minus_price", -9), reverse=True)[:8]
    print(json.dumps({"latest": str(out), "top_candidates": top}, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
