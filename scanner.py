#!/usr/bin/env python3
"""
Sub-penny OTC volume scanner (v1).
Runs once per trading day after the close. Uses Massive (formerly Polygon.io)
free-tier grouped daily bars (one call = whole market incl. OTC), keeps a rolling
30-day history, scores tickers, and posts the top hits to Discord.
"""
import os, sys, json, gzip, time, csv, math, datetime as dt
from pathlib import Path
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
    need, d, calls, today = LOOKBACK + 1, dt.date.today(), 0, dt.date.today()
    have = 0
    while have < need and calls < 60:
        ds = d.isoformat()
        if d.weekday() < 5 and ds not in h["closed"]:
            if ds in h["days"]:
                have += 1
            else:
                if calls: time.sleep(CALL_GAP)
                bars = fetch_grouped(ds); calls += 1
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


def scan(h):
    days = sorted(h["days"])
    if len(days) < 6:
        sys.exit("Not enough history yet.")
    latest, prior = days[-1], days[:-1][-LOOKBACK:]
    hits = []
    for t, (o, hi, lo, c, v) in h["days"][latest].items():
        if not (0 < c <= MAX_PRICE) or not (t.isalpha() and t.isupper()):
            continue                                # skip rights/warrants/when-issued (e.g. BCATr)
        vols = [h["days"][d].get(t, [0, 0, 0, 0, 0])[4] for d in prior]
        prev = h["days"][prior[-1]].get(t)
        prev_c = prev[3] if prev else next((h["days"][d][t][3] for d in reversed(prior) if t in h["days"][d]), 0)
        # ratios for the previous 4 days (for streak)
        recent = []
        for i in range(max(0, len(prior) - 4), len(prior)):
            base = vols[max(0, i - LOOKBACK):i] or [0]
            avg = sum(base) / len(base)
            recent.append(vols[i] / avg if avg else (50 if vols[i] else 0))
        s, why, m = score_bar(o, hi, lo, c, v, vols, prev_c, recent)
        if s > 0:
            hits.append({"ticker": t, "close": c, "volume": v, "score": round(s, 1), "reasons": why, **m})
    hits.sort(key=lambda x: x["score"], reverse=True)
    return latest, hits[:TOP_N]


# ---------- output ----------
def post_discord(date, hits):
    if not hits:
        msg = {"content": f"📭 {date}: no sub-${MAX_PRICE} tickers passed the filters."}
        requests.post(WEBHOOK, json=msg, timeout=30); return
    embeds = []
    for x in hits:
        embeds.append({
            "title": f"{x['ticker']}  @ ${x['close']:.4f}   (score {x['score']})",
            "url": f"https://www.otcmarkets.com/stock/{x['ticker']}/overview",
            "description": "\n".join(x["reasons"]) or "Volume spike",
            "color": 0x00FF88 if x["chg"] >= 0 else 0xFF5555,
            "fields": [
                {"name": "Vol vs 30d", "value": f"{x['ratio']:.1f}x", "inline": True},
                {"name": "$ Volume", "value": f"${x['dollar_vol']:,.0f}", "inline": True},
                {"name": "Day chg", "value": f"{x['chg']*100:+.0f}%", "inline": True},
            ],
        })
    for i in range(0, len(embeds), 10):  # Discord max 10 embeds/message
        payload = {"embeds": embeds[i:i + 10]}
        if i == 0:
            payload["content"] = f"🚨 **OTC scan for {date}** — top {len(hits)} under ${MAX_PRICE}"
        requests.post(WEBHOOK, json=payload, timeout=30).raise_for_status()
        time.sleep(1)


def write_csv(date, hits):
    DATA.mkdir(exist_ok=True)
    with open(DATA / "latest.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "ticker", "close", "volume", "score", "vol_ratio", "dollar_vol", "chg_pct", "reasons"])
        for x in hits:
            w.writerow([date, x["ticker"], x["close"], x["volume"], x["score"], round(x["ratio"], 1),
                        round(x["dollar_vol"]), round(x["chg"] * 100, 1), " | ".join(x["reasons"])])


if __name__ == "__main__":
    if not API_KEY:
        sys.exit("Set MASSIVE_API_KEY")
    print("Updating history…")
    hist = update_hist(load_hist())
    save_hist(hist)
    date, hits = scan(hist)
    write_csv(date, hits)
    for x in hits:
        print(f"{x['ticker']:<8} ${x['close']:.4f}  {x['ratio']:>5.1f}x  score {x['score']:>5}  {'; '.join(x['reasons'])}")
    if WEBHOOK:
        post_discord(date, hits)
    print(f"Done: {len(hits)} hits for {date}")
