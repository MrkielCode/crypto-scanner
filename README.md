#  Binance Crypto Perpetual Pre-Pump Scanner

A Streamlit app that scans Binance USDⓈ-M USDT perpetual futures for coins
showing early signs of a potential move — before the obvious, already-visible
pump. It scores each coin on two separate, backtested dimensions (**Setup**
and **Trigger**) rather than one blended number, so it can tell "coiled but
quiet" apart from "moving but shaky" apart from "both at once."

**This is a screening tool, not trading advice.** It narrows a few hundred
coins down to a shortlist worth investigating — it does not predict outcomes,
suggest entries/stops/targets, or guarantee anything will move. Every
scoring input was validated against real historical data (see *How the
scoring was built* below), but even the strongest metrics here are modest
statistical signals, not certainties.

---

## Quick start

```bash
pip install streamlit requests pandas numpy
streamlit run crypto_prepump_scanner_v2.py
```

Opens automatically at `http://localhost:8501`. No API key needed — it only
reads public Binance market data.

To deploy it so you can reach it from your phone, see the
[Streamlit Community Cloud](https://share.streamlit.io) deployment steps
(push this file + `requirements.txt` to a GitHub repo, then connect it there).

---

## What it does

- Pulls the full list of Binance USDT-M perpetual futures
- Filters by 24h volume floor, 24h change range, and an editable "Exclude
  Majors" list (BTC/ETH/BNB/etc. are excluded by default so they don't crowd
  out smaller coins with their always-high volume — still used as the
  benchmark for Relative Strength)
- Deep-analyzes the top candidates in parallel (1h/4h/15m candles, daily
  volume history, open interest, funding rate, spot market context)
- Scores each coin on **Setup** (is it positioned for a move?) and
  **Trigger** (is a move starting right now?) — both 0–100
- Classifies each coin as 🚀 Ready / 🟡 Coiled / ⚡ Reactive / ⚪ No Setup
  based on adjustable thresholds
- Shows a sortable leaderboard (with Price, Support, Resistance, and a
  one-click TradingView chart link) plus a drill-down detail panel per coin

## Sidebar filters

| Filter | What it does |
|---|---|
| Minimum 24h futures volume | Liquidity floor — filters out illiquid/thin coins |
| Minimum/Maximum 24h change % | Narrows to coins in your preferred momentum range |
| Parallel requests | Scan speed vs. Binance rate-limit safety |
| Max candidates to deep-analyze | Caps API cost; takes the highest-volume coins first, after majors are excluded |
| Exclude Majors | Editable comma-separated symbol list |
| Relative volume (RVOL) minimum | Today's volume vs. this coin's own 14-day average — custom entry, default 2.0x |
| "Ready" Setup/Trigger thresholds | Tunable cutoffs for the 🚀 Ready label |
| Results to show | How many rows land in the leaderboard |
| Auto refresh | Re-scans on a timer instead of only on button click |

## The two scores

**Setup Score** — built from Cooldown (hours since the coin's last big
move), 24h Change %, RVOL, OI Change %, and Funding Rate. Answers: *is this
coin positioned for a move, even if nothing is happening yet?*

**Trigger Score** — built from Volume Expansion (1h/4h, weighted toward
4h), Resistance Distance %, and CVD Shift (cumulative buy/sell delta).
Answers: *is a move actually starting right now?*

A coin is labeled 🚀 **Ready** only when both scores clear their threshold
(default Setup ≥ 70, Trigger ≥ 60) — this is meant to catch coins that are
both well-positioned *and* actively moving, filtering out both "looks great
on paper but dead quiet" and "spiking with no real foundation."

Higher Low structure (15M/1H/4H), resistance "tests," Relative Strength vs.
BTC/ETH, and spot/futures context are shown for reference but intentionally
**not scored** — they didn't clear the statistical bar described below.

## How the scoring was built

Every metric in `score()` was tested against ~16 days of real Binance
history using a companion script (`backtest_scoring_metrics.py`), computing
each metric's actual Information Coefficient (IC) — a rank correlation
between the metric's value and what really happened afterward — rather than
including metrics on theory alone.

- Setup metrics were tested against: did the coin move +8% within 24h?
- Trigger metrics were tested against: did the coin move +5% within 4h?
- Metrics with |IC| < 0.03 were dropped. **Compression** and
  **OI/Price Divergence** both failed this test and were removed. A
  standalone **OI Change %** metric replaced the divergence formula after
  outperforming it substantially in testing.
- Two findings came back counter to the original design and are scored
  accordingly: coins with a **shorter** cooldown (recently active) and
  coins **already up** on 24h change were *more* likely to continue moving,
  not less — so those two metrics reward the opposite of what you might
  initially expect.

**Important caveat:** this was one ~16-day window — roughly one market
regime. Re-run the backtest periodically on fresh data (see below) to check
whether these relationships hold up over time, rather than trusting one
window indefinitely.

## Re-running the backtest

```bash
python3 backtest_scoring_metrics.py
```

Takes a few minutes (several hundred Binance API calls across ~60 coins).
Produces `backtest_metric_values.csv` (raw panel data) and
`backtest_ic_report.csv` (per-metric IC, sample size, keep/drop verdict).
See the comments at the top of that script for full methodology notes and
known limitations (OI history is capped at ~30 days on Binance's side;
the coin universe is selected by *today's* volume, not a historical
snapshot).

## Known limitations

- No scan-history logging yet — there's currently no way to measure
  whether 🚀 Ready flags actually play out over time
- No BTC/market-regime gate — altcoin setups likely behave differently in
  trending vs. choppy BTC conditions, and this isn't accounted for
- No order-book depth or liquidity check beyond the raw volume floor
- The news/event badge (`get_event_badge`) is wired but inactive — it
  returns nothing until `cryptopanic_token` and/or `coinmarketcal_key` are
  added to Streamlit secrets
- Price Map (R1/R2/S1/S2) levels are simple swing-high/low heuristics, not
  true volume-profile support/resistance

## Files in this project

| File | Purpose |
|---|---|
| `crypto_prepump_scanner_v2.py` | The live Streamlit scanner app |
| `backtest_scoring_metrics.py` | Standalone backtest — validates scoring metrics against real history |
| `requirements.txt` | Python dependencies |
