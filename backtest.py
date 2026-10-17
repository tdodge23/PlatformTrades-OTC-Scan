#!/usr/bin/env python3
"""
Replay one ticker's history and show which days the scanner would have flagged,
plus the best return over the following 10 trading days.

Usage:  MASSIVE_API_KEY=xxx python backtest.py TICKER 2025-01-01 2025-06-30
Note: Massive free tier only goes back ~2 years.
"""
import sys
import scanner as S

if len(sys.argv) != 4:
    sys.exit(__doc__)
t, start, end = sys.argv[1].upper(), sys.argv[2], sys.argv[3]
j = S.api_get(f"/v2/aggs/ticker/{t}/range/1/day/{start}/{end}", {"adjusted": "true", "limit": 5000})
bars = j.get("results") or []
if not bars:
    sys.exit(f"No data for {t} in that range (outside free-tier history, or ticker changed).")

import datetime as dt
vols = [b["v"] for b in bars]
S.MAX_PRICE = 1e9  # don't price-filter during backtest
print(f"{'date':<12}{'close':>10}{'vol x':>8}{'score':>7}{'next10 max':>12}  reasons")
flags = 0
for i in range(5, len(bars)):
    b = bars[i]
    past = vols[max(0, i - S.LOOKBACK):i]
    recent = []
    for k in range(max(5, i - 4), i):
        base = vols[max(0, k - S.LOOKBACK):k] or [0]
        avg = sum(base) / len(base)
        recent.append(vols[k] / avg if avg else 50)
    s, why, m = S.score_bar(b["o"], b["h"], b["l"], b["c"], b["v"], past, bars[i - 1]["c"], recent)
    if s <= 0:
        continue
    flags += 1
    fwd = [x["h"] for x in bars[i + 1:i + 11]]
    best = (max(fwd) / b["c"] - 1) * 100 if fwd else float("nan")
    d = dt.datetime.utcfromtimestamp(b["t"] / 1000).date()
    print(f"{d!s:<12}{b['c']:>10.5f}{m['ratio']:>7.1f}x{s:>7.0f}{best:>11.0f}%  {'; '.join(why)}")
print(f"\n{flags} flagged days out of {len(bars)}")
