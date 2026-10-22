#!/usr/bin/env python3
"""
Sub-penny OTC volume scanner (v1).
Runs once per trading day after the close. Uses Massive (formerly Polygon.io)
free-tier grouped daily bars (one call = whole market incl. OTC), keeps a rolling
30-day history, scores tickers, and posts the top hits to Discord.
"""
import os, sys, json, gzip, time, csv, math, statistics, urllib.parse, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

# ---------- config (override via env vars) ----------
API_KEY        = os.environ.get("MASSIVE_API_KEY", "")
WEBHOOK        = os.environ.get("DISCORD_WEBHOOK", "")
BASE           = "https://api.massive.com"
MAX_PRICE      = float(os.environ.get("MAX_PRICE", "0.001"))     # 0.001 = triple-zero; try 0.01 for double-zero
MIN_VOL_RATIO  = float(os.environ.get("MIN_VOL_RATIO", "5"))      # today's vol vs 30d avg
MIN_DOLLAR_VOL = float(os.environ.get("MIN_DOLLAR_VOL", "2500"))  # filters dust trades
TOP_N          = int(os.environ.get("TOP_N", "15"))
LOOKBACK       = 30
STORE_PRICE    = 0.01        # only store tickers under 1 cent (keeps history file small)
CALL_GAP       = 13          # free tier = 5 calls/min
ET             = ZoneInfo("America/New_York")   # market dates are US Eastern
DATA           = Path(__file__).parent / "data"
HIST_FILE      = DATA / "history.json.gz"


# ---------- data ----------
def api_get(path, params=None):
    params = dict(params or {}, apiKey=API_KEY)
    for attempt in range(5):
        r = requests.get(BASE + path, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(60); continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Rate-limited repeatedly on {path}")


def fetch_grouped(date):
    """All US stock daily bars for one date, OTC included. None if market closed / not ready."""
    j = api_get(f"/v2/aggs/grouped/locale/us/market/stocks/{date}",
                {"adjusted": "true", "include_otc": "true"})
    res = j.get("results") or []
    if not res:
        return None
    return {b["T"]: [b.get("o", 0), b.get("h", 0), b.get("l", 0), b.get("c", 0), b.get("v", 0)]
            for b in res if b.get("c", 1) < STORE_PRICE}


def load_hist():
    if HIST_FILE.exists():
        with gzip.open(HIST_FILE, "rt") as f:
            return json.load(f)
    return {"days": {}, "closed": []}


def save_hist(h):
    DATA.mkdir(exist_ok=True)
    with gzip.open(HIST_FILE, "wt") as f:
        json.dump(h, f, separators=(",", ":"))


def update_hist(h):
    """Make sure we have the latest LOOKBACK+1 trading days. First run backfills (~7 min on free tier)."""
    # Use the US market's date, not the server's (GitHub runs in UTC, which is
    # already "tomorrow" by 5pm Pacific).
    today = dt.datetime.now(ET).date()
    need, d, calls = LOOKBACK + 1, today, 0
    have = 0
    while have < need and calls < 60:
        ds = d.isoformat()
        if d.weekday() < 5 and ds not in h["closed"]:
            if ds in h["days"]:
                have += 1
            else:
                if calls: time.sleep(CALL_GAP)
                try:
                    bars = fetch_grouped(ds)
                except requests.HTTPError as e:
                    # The free tier rejects a day that isn't finished yet. Skip it;
                    # older days still raise so a bad API key isn't hidden.
                    if (today - d).days > 1:
                        raise
                    print(f"  {ds} not available yet ({e.response.status_code}), using earlier days")
                    bars = None
                calls += 1
                if bars:
                    h["days"][ds] = bars; have += 1
                    print(f"  fetched {ds}: {len(bars)} sub-penny tickers")
                elif (today - d).days > 3:      # only mark closed once it's surely final
                    h["closed"].append(ds)
        d -= dt.timedelta(days=1)
    keep = sorted(h["days"])[-need:]
    h["days"] = {k: h["days"][k] for k in keep}
    h["closed"] = h["closed"][-60:]
    return h


# ---------- scoring (shared with backtest.py) ----------
def score_bar(o, hi, lo, c, v, past_vols, prev_c, recent_ratios):
    """Return (score, reasons, metrics) for one ticker-day. past_vols = prior LOOKBACK days (0 if no trades)."""
    avg = sum(past_vols) / len(past_vols) if past_vols else 0
    dormant = avg == 0 or sum(1 for x in past_vols if x > 0) <= 3
    ratio = (v / avg) if avg > 0 else 50.0
    dollar_vol = c * v
    chg = ((c / prev_c) - 1) if prev_c else 0
    streak = 0
    for r in reversed(recent_ratios):          # consecutive prior days at 3x+
        if r >= 3: streak += 1
        else: break

    if ratio < MIN_VOL_RATIO or dollar_vol < MIN_DOLLAR_VOL:
        return 0, [], {}
    if chg >= 5.0:                              # +500% on a sub-penny = almost always a bad print
        return 0, [], {}

    reasons, s = [], min(ratio, 50) * 2
    s += min(10, 5 * math.log10(dollar_vol / MIN_DOLLAR_VOL))   # real money behind it
    if dormant:
        s += 15; reasons.append("💤 Dormant ticker waking up")
    if streak:
        s += 5 * min(streak, 4); reasons.append(f"🔁 {streak + 1} straight days of 3x+ volume")
    if abs(chg) < 0.25 and ratio >= MIN_VOL_RATIO:
        s += 20; reasons.append("🧲 Heavy volume, price barely moved (accumulation?)")
    if hi > 0 and c >= hi * 0.98 and c > o:
        s += 5; reasons.append("📈 Closed at high of day")
    if chg > 1.0:
        s -= 20; reasons.append("⚠️ Already up 100%+ today — may be late")
    return s, reasons, {"ratio": ratio, "dollar_vol": dollar_vol, "chg": chg, "streak": streak}


def tick_for(c):
    """Minimum price step. Sub-penny OTC names trade in $0.0001 steps; below that, $0.000001."""
    return 0.0001 if c >= 0.0001 else 0.000001


def chart_url(closes, vols):
    """30-day price line + volume bars as a quickchart.io image (free, no key)."""
    cfg = {"type": "bar",
           "data": {"labels": list(range(len(closes))),
                    "datasets": [
                        {"type": "line", "data": closes, "borderColor": "#00ff88", "fill": False,
                         "pointRadius": 0, "borderWidth": 2, "yAxisID": "p"},
                        {"data": [round(v / 1e6, 1) for v in vols], "backgroundColor": "rgba(130,130,255,0.45)",
                         "yAxisID": "v"}]},
           "options": {"legend": {"display": False},
                       "scales": {"xAxes": [{"display": False}],
                                  "yAxes": [{"id": "p", "position": "left", "ticks": {"fontColor": "#bbb"}},
                                            {"id": "v", "position": "right", "display": False}]}}}
    return ("https://quickchart.io/chart?bkg=%23202225&w=500&h=160&c="
            + urllib.parse.quote(json.dumps(cfg, separators=(",", ":"))))


def scan(h):
    days = sorted(h["days"])
    if len(days) < 6:
        sys.exit("Not enough history yet.")
    latest, prior = days[-1], days[:-1][-LOOKBACK:]
    flags = h.setdefault("flags", {})
    recent_flag_days = [d for d in days[-11:-1] if d in flags]
    hits = []
    for t, (o, hi, lo, c, v) in h["days"][latest].items():
        if not (0 < c <= MAX_PRICE) or not (t.isalpha() and t.isupper()):
            continue                                # skip rights/warrants/when-issued (e.g. BCATr)
        vols = [h["days"][d].get(t, [0, 0, 0, 0, 0])[4] for d in prior]
        prev = h["days"][prior[-1]].get(t)
        prev_c = prev[3] if prev else next((h["days"][d][t][3] for d in reversed(prior) if t in h["days"][d]), 0)
        recent = []
        for i in range(max(0, len(prior) - 4), len(prior)):
            base = vols[max(0, i - LOOKBACK):i] or [0]
            avg = sum(base) / len(base)
            recent.append(vols[i] / avg if avg else (50 if vols[i] else 0))
        s, why, m = score_bar(o, hi, lo, c, v, vols, prev_c, recent)
        if s <= 0:
            continue

        # --- Tier 1 enrichment ---
        bars = [h["days"][d].get(t) for d in prior] + [[o, hi, lo, c, v]]
        closes, last = [], None
        for b in bars:                                # carry forward close on no-trade days
            if b: last = b[3]
            closes.append(last if last is not None else c)
        all_vols = [b[4] if b else 0 for b in bars]
        highs = [b[1] for b in bars if b and b[1] > 0]
        lows = [b[2] for b in bars if b and b[2] > 0]
        hi30, lo30 = max(highs), min(lows)
        pos = (c - lo30) / (hi30 - lo30) if hi30 > lo30 else 0.5
        if pos <= 0.25:
            where = f"🟢 Near 30d low (base)"; s += 5
        elif pos >= 0.8:
            where = f"🔴 Near 30d high (extended)"; s -= 5
        else:
            where = f"🟡 Mid-range"
        dvs = [(b[3] * b[4]) if b else 0 for b in bars[-5:]]
        max_size = 0.10 * statistics.median(dvs)      # ~10% of typical daily $ volume
        repeat = sum(1 for d in recent_flag_days if t in flags[d])
        if repeat:
            s += 5 * min(repeat, 3); why = why + [f"🔂 Flagged {repeat}x in last 10 days"]
        tk = tick_for(c)

        hits.append({"ticker": t, "close": c, "volume": v, "score": round(s, 1), "reasons": why, **m,
                     "tick1": tk / c, "tick2": 2 * tk / c, "max_size": max_size, "where": where,
                     "range": (lo30, hi30), "repeat": repeat, "chart": chart_url(closes, all_vols)})
    hits.sort(key=lambda x: x["score"], reverse=True)
    hits = hits[:TOP_N]
    flags[latest] = {x["ticker"]: x["close"] for x in hits}   # remember for scorecard + repeat flags
    for d in list(flags):
        if d not in h["days"]:
            del flags[d]
    return latest, hits


def scorecard(h):
    """How did the previous scan's flags do on the next trading day? Plus a 10-day hit rate."""
    days, flags = sorted(h["days"]), h.get("flags", {})
    latest = days[-1]
    prev = [d for d in days[:-1] if flags.get(d)]
    if not prev:
        return None
    fd = prev[-1]
    nd = days[days.index(fd) + 1]
    rows = []
    for t, fc in flags[fd].items():
        b = h["days"][nd].get(t)
        if b:
            rows.append((t, fc, (b[3] / fc - 1) * 100, (b[1] / fc - 1) * 100))
        else:
            rows.append((t, fc, None, None))
    rows.sort(key=lambda r: 1e9 if r[3] is None else -r[3])     # best first, no-trades last
    # 10-day hit rate: next-day high reached at least +1 tick
    hits = total = 0
    for d in days[-11:-1]:
        if d not in flags or days.index(d) + 1 >= len(days):
            continue
        n = days[days.index(d) + 1]
        for t, fc in flags[d].items():
            total += 1
            b = h["days"][n].get(t)
            if b and b[1] >= fc + tick_for(fc) * 0.999:
                hits += 1
    return {"flag_date": fd, "next_date": nd, "rows": rows, "hits": hits, "total": total}


# ---------- output ----------
def post(payload):
    requests.post(WEBHOOK, json=payload, timeout=30).raise_for_status()
    time.sleep(1)


def post_scorecard(sc):
    lines = []
    for t, fc, cl, hi in sc["rows"]:
        if cl is None:
            lines.append(f"`{t:<6}` ${fc:.4f} → no trades")
        else:
            icon = "🟢" if hi >= 25 else ("🟡" if hi > 0 else "🔴")
            lines.append(f"{icon} `{t:<6}` ${fc:.4f} → close {cl:+.0f}%  ·  high {hi:+.0f}%")
    rate = f"{sc['hits']}/{sc['total']} ({sc['hits'] / sc['total'] * 100:.0f}%)" if sc["total"] else "n/a"
    post({"embeds": [{
        "title": f"📊 Scorecard: {sc['flag_date']} flags → {sc['next_date']}",
        "description": "\n".join(lines)[:4000],
        "color": 0x5865F2,
        "footer": {"text": f"Last 10 days: {rate} of flags traded at least +1 tick higher the next day"},
    }]})


def post_discord(date, hits, sc):
    if sc:
        post_scorecard(sc)
    if not hits:
        post({"content": f"📭 {date}: no sub-${MAX_PRICE} tickers passed the filters."})
        return
    embeds = []
    for x in hits:
        size = f"${x['max_size']:,.0f}" if x["max_size"] >= 100 else "⚠️ too thin"
        lo, hi = x["range"]
        embeds.append({
            "title": f"{x['ticker']}  @ ${x['close']:.4f}   (score {x['score']})",
            "url": f"https://www.otcmarkets.com/stock/{x['ticker']}/overview",
            "description": "\n".join(x["reasons"] + [x["where"]]) or "Volume spike",
            "color": 0x00FF88 if x["chg"] >= 0 else 0xFF5555,
            "fields": [
                {"name": "Vol vs 30d", "value": f"{x['ratio']:.1f}x", "inline": True},
                {"name": "$ Volume", "value": f"${x['dollar_vol']:,.0f}", "inline": True},
                {"name": "Day chg", "value": f"{x['chg']*100:+.0f}%", "inline": True},
                {"name": "Next tick", "value": f"+{x['tick1']*100:.0f}% / 2 ticks +{x['tick2']*100:.0f}%", "inline": True},
                {"name": "Max size", "value": size, "inline": True},
                {"name": "30d range", "value": f"${lo:.4f}–${hi:.4f}", "inline": True},
                {"name": "Links", "value": f"[Stocktwits](https://stocktwits.com/symbol/{x['ticker']}) · "
                                           f"[Chart](https://www.tradingview.com/chart/?symbol=OTC:{x['ticker']})",
                 "inline": False},
            ],
            "image": {"url": x["chart"]},
        })
    for i in range(0, len(embeds), 5):              # 5 per message keeps us under Discord's size cap
        payload = {"embeds": embeds[i:i + 5]}
        if i == 0:
            payload["content"] = f"🚨 **OTC scan for {date}** — top {len(hits)} under ${MAX_PRICE}"
        post(payload)


def write_csv(date, hits):
    DATA.mkdir(exist_ok=True)
    with open(DATA / "latest.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "ticker", "close", "volume", "score", "vol_ratio", "dollar_vol", "chg_pct",
                    "next_tick_pct", "max_size", "repeat_10d", "where", "reasons"])
        for x in hits:
            w.writerow([date, x["ticker"], x["close"], x["volume"], x["score"], round(x["ratio"], 1),
                        round(x["dollar_vol"]), round(x["chg"] * 100, 1), round(x["tick1"] * 100),
                        round(x["max_size"]), x["repeat"], x["where"], " | ".join(x["reasons"])])


if __name__ == "__main__":
    if not API_KEY:
        sys.exit("Set MASSIVE_API_KEY")
    print("Updating history…")
    hist = update_hist(load_hist())
    newest = sorted(hist["days"])[-1]
    if newest in hist.get("flags", {}) and os.environ.get("FORCE") != "1":
        # No new trading day since the last scan: don't re-post the same alerts.
        save_hist(hist)
        sys.exit(print(f"Already scanned {newest}; no new trading day yet.") or 0)
    sc = scorecard(hist)
    date, hits = scan(hist)
    save_hist(hist)
    write_csv(date, hits)
    for x in hits:
        print(f"{x['ticker']:<8} ${x['close']:.4f}  {x['ratio']:>5.1f}x  score {x['score']:>5}  {'; '.join(x['reasons'])}")
    if WEBHOOK:
        post_discord(date, hits, sc)
    print(f"Done: {len(hits)} hits for {date}")
