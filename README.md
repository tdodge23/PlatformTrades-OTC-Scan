# OTC Sub-Penny Scanner

Nightly scan of every OTC ticker under $0.001. Flags volume spikes, dormant tickers waking up, and quiet accumulation, then posts the top 15 to Discord. Cost: $0.

## Setup (~10 min)
1. **Massive API key**: sign up free at massive.com and copy your key.
2. **Discord webhook**: Channel settings → Integrations → Webhooks → New Webhook → Copy URL.
3. **GitHub repo**: create a new **private** repo and upload this whole folder, including `.github/workflows/scan.yml`.
4. **Secrets**: repo Settings → Secrets and variables → Actions → add `MASSIVE_API_KEY` and `DISCORD_WEBHOOK`.
5. **First run**: Actions tab → OTC scan → Run workflow. The first run backfills 30 days and takes ~7 min. Every run after that takes ~1 min.

After that it runs automatically at 6pm Pacific, Mon–Fri.

## Tuning (env vars in scan.yml)
| Var | Default | Meaning |
|---|---|---|
| `MAX_PRICE` | 0.001 | Use 0.01 to include double-zeros |
| `MIN_VOL_RATIO` | 5 | Today's volume vs 30-day average |
| `MIN_DOLLAR_VOL` | 1000 | Skips dust trades |
| `TOP_N` | 15 | Alerts per day |

## Backtest a past runner
```
MASSIVE_API_KEY=xxx python backtest.py TICKER 2025-01-01 2025-06-30
```
Shows every day it would have flagged and the best gain over the next 10 days. Free tier only covers ~2 years back.

## Run locally instead
```
pip install requests
MASSIVE_API_KEY=xxx DISCORD_WEBHOOK=xxx python scanner.py
```
