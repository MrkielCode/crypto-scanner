import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:  # older requests bundles urllib3 differently
    from requests.packages.urllib3.util.retry import Retry
import pandas as pd
import streamlit as st

FAPI = 'https://fapi.binance.com'
SPOT = 'https://api.binance.com'


@st.cache_resource
def get_session():
    s = requests.Session()
    # Bigger connection pool so parallel requests don't queue behind a
    # default pool size of 10 (was silently throttling `workers` > 10).
    adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=Retry(total=0))
    s.mount('https://', adapter)
    s.mount('http://', adapter)
    return s


session = get_session()

st.set_page_config(page_title='Binance Perpetual Pre-Pump Scanner', page_icon='🚀', layout='wide')
st.title('🚀 Binance Crypto Perpetual Pre-Pump Scanner')
st.caption('Scanner universe: Binance USDⓈ-M USDT crypto perpetuals only. Spot data is context for the same coin.')

with st.sidebar:
    st.header('Market')
    st.caption('Only TRADING + PERPETUAL + USDT Binance Futures contracts')
    st.header('Filters')
    min_vol = st.number_input('Minimum 24h futures volume (USDT)', min_value=0.0, value=5_000_000.0, step=1_000_000.0)
    min_change = st.number_input('Minimum 24h change %', value=-2.0, step=1.0)
    max_change = st.number_input('Maximum 24h change %', value=15.0, step=1.0)
    workers = st.slider('Parallel requests', 2, 16, 8)
    st.caption('Lower parallel requests if Binance connections time out.')
    max_candidates = st.slider('Max candidates to deep-analyze', 10, 150, 60)
    st.caption('Caps how many symbols get the full multi-metric analysis (highest 24h volume first, after majors are excluded below). Keeps scans fast even when hundreds of coins match the filters.')

    st.header('Exclude Majors')
    exclude_input = st.text_input(
        'Exclude these symbols (comma-separated)',
        value='BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,TRXUSDT,AVAXUSDT,LINKUSDT,TONUSDT,DOTUSDT'
    )
    excluded_symbols = {s.strip().upper() for s in exclude_input.split(',') if s.strip()}
    st.caption("These blue-chip coins almost always carry the highest 24h volume, so they'd otherwise fill up your candidate slots and crowd out smaller coins that are actually just starting to move. Still used as the BTC/ETH benchmark for Relative Strength. Clear the box to include everything.")

    min_rvol = st.number_input('Relative volume vs 14-day average (min)', min_value=0.0, value=2.0, step=0.5)
    st.caption('Compares today\'s 24h volume to this coin\'s own 14-day average — catches a quiet coin suddenly waking up, not just "big absolute volume." Set to 5.0+ for a stricter, more unusual-activity-only view. Applied after deep analysis, so it only filters within the candidate cap above.')

    st.header('Status Thresholds')
    ready_setup_threshold = st.number_input('"Ready" requires Setup ≥', min_value=0, max_value=100, value=70, step=5)
    ready_trigger_threshold = st.number_input('"Ready" requires Trigger ≥', min_value=0, max_value=100, value=60, step=5)
    st.caption('Backtested starting points, not fixed rules — tune these as live results come in.')

    max_results = st.slider('Results to show', 10, 50, 30)
    auto = st.checkbox('Auto refresh', value=False)
    refresh_seconds = st.slider('Refresh every seconds', 30, 300, 60)


def get_json(base, path, params=None):
    last_error = None
    for attempt in range(3):
        try:
            r = session.get(base + path, params=params, timeout=(5, 8))
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))
        except requests.exceptions.RequestException:
            raise
    raise last_error


@st.cache_data(ttl=300)
def get_perpetual_symbols():
    data = get_json(FAPI, '/fapi/v1/exchangeInfo')
    return {
        x['symbol'] for x in data.get('symbols', [])
        if x.get('status') == 'TRADING'
        and x.get('contractType') == 'PERPETUAL'
        and x.get('quoteAsset') == 'USDT'
        and x.get('baseAsset') not in {'USDT', 'USDC'}
    }


@st.cache_data(ttl=30)
def get_tickers():
    allowed = get_perpetual_symbols()
    data = get_json(FAPI, '/fapi/v1/ticker/24hr')
    return [{
        'symbol': x['symbol'], 'price': float(x['lastPrice']),
        'change_24h': float(x['priceChangePercent']),
        'volume_24h': float(x['quoteVolume']),
        'high_24h': float(x['highPrice']), 'low_24h': float(x['lowPrice'])
    } for x in data if x['symbol'] in allowed]


@st.cache_data(ttl=30)
def get_spot_tickers():
    data = get_json(SPOT, '/api/v3/ticker/24hr')
    return {x['symbol']: {'volume': float(x['quoteVolume']), 'change': float(x['priceChangePercent'])}
            for x in data if x['symbol'].endswith('USDT')}


@st.cache_data(ttl=25)
def get_klines(symbol, interval, limit):
    # Cached per (symbol, interval, limit): during one scan every metric that
    # needs 1h or 15m candles reuses this single call instead of re-fetching.
    data = get_json(FAPI, '/fapi/v1/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})
    df = pd.DataFrame(data, columns=['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time',
                                      'quote_volume', 'trades', 'taker_buy_volume', 'taker_buy_quote_volume', 'ignore'])
    for c in ('open', 'high', 'low', 'close', 'volume', 'quote_volume', 'taker_buy_volume', 'taker_buy_quote_volume'):
        df[c] = df[c].astype(float)
    # Binance's last candle is often still forming (not yet closed). Using its
    # partial volume as "the current bar" makes every ratio depend on how many
    # minutes into the bar the scan happens to run, rather than real momentum.
    # Drop it so every metric is based on fully-closed candles only.
    if not df.empty and df['close_time'].iloc[-1] > int(time.time() * 1000):
        df = df.iloc[:-1].reset_index(drop=True)
    return df


def ratio(series, n=20):
    if len(series) < n + 1:
        return 0.0
    base = series.iloc[-n - 1:-1].mean()
    return float(series.iloc[-1] / base) if base else 0.0


def structure_and_volume(df1h):
    # Compression used to be computed here too, but the backtest came back
    # IC -0.019 (below the 0.03 cutoff) — dropped from both scoring and
    # display rather than left as dead-weight context.
    if len(df1h) < 25:
        return False, 0.0, 0, 0.0
    high, low, close = df1h['high'], df1h['low'], df1h['close']
    higher_low = low.iloc[-6:-2].min() > low.iloc[-12:-6].min()
    resistance = high.iloc[-21:-1].max()
    current = close.iloc[-1]
    distance = ((resistance - current) / current * 100) if current else 0.0
    tol = 0.005
    tests = int((((high.iloc[-21:-1] - resistance).abs() / resistance) <= tol).sum())
    return bool(higher_low), float(distance), tests, float(resistance)


def cooldown_hours(df1h):
    # Hours since the last |24h change| >= 15%, capped at 14 days — same
    # definition the backtest validated (IC -0.169, the strongest single
    # signal found). Needs ~14 days of 1h candles, which is why the main 1h
    # fetch below was bumped from 50 to 360 candles.
    if len(df1h) < 25:
        return 336
    chg24 = df1h['close'].pct_change(24) * 100
    pump_flag = (chg24.abs() >= 15).fillna(False).values
    counter = 336
    for flagged in pump_flag:
        counter = 0 if flagged else min(counter + 1, 336)
    return int(counter)


def cvd_shift_pct_from_df(df15m):
    # Cumulative (buy - sell) taker volume over the last ~2h of 15m candles,
    # as a % of that same volume — backtest-validated (IC 0.045). Expressed
    # as a % rather than the backtest's raw dollar figure so it's comparable
    # across coins of very different sizes in the live UI.
    if len(df15m) < 8:
        return 0.0
    recent = df15m.tail(8)
    delta = 2 * recent['taker_buy_quote_volume'] - recent['quote_volume']
    total = recent['quote_volume'].sum()
    return float(delta.sum() / total * 100) if total > 0 else 0.0


def higher_low(low_series, recent=(-6, -2), prior=(-12, -6)):
    if len(low_series) < abs(prior[0]):
        return False
    return bool(low_series.iloc[recent[0]:recent[1]].min() > low_series.iloc[prior[0]:prior[1]].min())


def price_levels(df1h, resistance, current):
    # R1/S1 come from structure_and_volume's own lookback window; R2/S2 look
    # at the 50 candles just before that window for a second, nearby level.
    # Capped at 50 (not "everything before") so R2/S2 stay a nearby prior
    # swing even now that df1h carries 360 candles for Cooldown's sake —
    # otherwise R2 could surface a level from two weeks ago instead of a
    # locally relevant one. This is a simple swing-high/low heuristic, not
    # real volume-profile support/resistance — a rough map, not exact levels.
    low = df1h['low']
    support = float(low.iloc[-21:-1].min()) if len(low) >= 21 else current
    support_distance = ((current - support) / current * 100) if current else 0.0
    older_high = df1h['high'].iloc[-71:-21] if len(df1h) >= 71 else df1h['high'].iloc[:-21] if len(df1h) > 21 else pd.Series(dtype=float)
    resistance_2 = float(older_high.max()) if not older_high.empty and older_high.max() > resistance else None
    older_low = low.iloc[-71:-21] if len(df1h) >= 71 else low.iloc[:-21] if len(low) > 21 else pd.Series(dtype=float)
    support_2 = float(older_low.min()) if not older_low.empty and older_low.min() < support else None
    return support, support_distance, resistance_2, support_2


@st.cache_data(ttl=3600)
def get_rvol_baseline(symbol):
    # 14 closed daily candles' average volume, used as "normal" for this coin.
    # Cached an hour since a daily baseline barely moves within that window.
    df = get_klines(symbol, '1d', 15)
    if df.empty:
        return 0.0
    return float(df['quote_volume'].tail(14).mean())


def get_rvol(symbol, volume_24h):
    baseline = get_rvol_baseline(symbol)
    return float(volume_24h / baseline) if baseline > 0 else 0.0


def get_event_badge(symbol):
    # Wired for CryptoPanic (news) / CoinMarketCal (scheduled events, e.g.
    # unlocks) integration. Intentionally inactive until API keys are added
    # to Streamlit secrets — returns None so it stays invisible until then.
    try:
        has_keys = 'cryptopanic_token' in st.secrets or 'coinmarketcal_key' in st.secrets
    except Exception:
        has_keys = False
    if not has_keys:
        return None
    return None  # TODO: fetch + return a short headline once keys are present


def get_oi_change_24h(symbol):
    # Daily-snapshot OI change — this is the version the backtest validated
    # (IC 0.054). The old hourly 2-point check was noisy and never tested;
    # a standalone "OI/Price Divergence" formula was also tested and came in
    # far weaker (IC 0.015) due to outliers from subtracting price change —
    # plain OI change on its own outperformed it.
    try:
        data = get_json(FAPI, '/futures/data/openInterestHist', {'symbol': symbol, 'period': '1d', 'limit': 2})
        if len(data) < 2:
            return 0.0
        old, new = float(data[-2]['sumOpenInterestValue']), float(data[-1]['sumOpenInterestValue'])
        return ((new - old) / old * 100) if old else 0.0
    except Exception:
        return 0.0


def funding(symbol):
    try:
        x = get_json(FAPI, '/fapi/v1/premiumIndex', {'symbol': symbol})
        return float(x.get('lastFundingRate', 0)) * 100
    except Exception:
        return 0.0


def spot_context(symbol, futures_volume):
    x = get_spot_tickers().get(symbol)
    if not x or futures_volume <= 0:
        return 0.0, 0.0, '⚪ Spot unavailable'
    ratio_sf = x['volume'] / futures_volume
    if x['change'] > 0 and ratio_sf >= .75:
        label = '🟢 Spot + Perp active'
    elif ratio_sf < .35:
        label = '🟡 Futures-led'
    elif ratio_sf >= .75:
        label = '🔵 Spot-led / balanced'
    else:
        label = '⚪ Mixed'
    return ratio_sf, x['change'], label


def setup_score(c):
    # "Is this coin positioned for a move, even if nothing is happening yet?"
    # Every input here was tested against a +8%/24h label in a 30-day
    # backtest; point weights are roughly proportional to each metric's
    # Information Coefficient, so the strongest validated signal carries the
    # most weight. Two signs are DELIBERATELY inverted from an earlier,
    # untested design: the data showed short-term momentum persistence
    # (coins already moving tend to keep moving), not accumulation-after-rest.
    s = 0
    cooldown, ch, rvol, oi, fr = c['cooldown_hours'], c['change_24h'], c['rvol'], c['oi_change_24h'], c['funding']

    # Cooldown (max 40, IC -0.169 — strongest Setup signal). Inverted from
    # the original "quiet coins are coiled" theory: shorter time since the
    # last big move scored HIGHER, not lower.
    if cooldown <= 24: s += 40
    elif cooldown <= 72: s += 28
    elif cooldown <= 168: s += 14

    # 24h Change % (max 20, IC 0.081). Also inverted from the original plan:
    # reward already being up, not flat/mild.
    if ch >= 10: s += 20
    elif ch >= 5: s += 14
    elif ch >= 0: s += 6

    # RVOL vs 14-day average (max 18, IC 0.071)
    if rvol >= 5: s += 18
    elif rvol >= 3: s += 13
    elif rvol >= 2: s += 8
    elif rvol >= 1: s += 3

    # OI Change % — standalone, daily snapshots (max 13, IC 0.054). This
    # replaced "OI/Price Divergence," which tested far weaker (IC 0.015);
    # the divergence formula's subtraction was injecting noise, not signal.
    if oi >= 10: s += 13
    elif oi >= 5: s += 9
    elif oi >= 2: s += 5
    elif oi >= 0: s += 2

    # Funding (max 9, IC 0.032 — right at the cutoff, kept at modest weight
    # since the result is inconclusive rather than clearly negative. Note:
    # this does NOT properly test the "deep negative funding = squeeze fuel"
    # theory, which is a tail effect a straight rank correlation can miss.
    if -0.01 <= fr <= 0.03: s += 9
    elif fr > 0.10: s += 0
    else: s += 3

    return max(0, min(100, round(s)))


def trigger_score(c):
    # "Is a move actually starting right now?" Tested against a +5%/4h
    # label. Weighted toward the 4h volume ratio since it backtested far
    # stronger than the 1h version (IC 0.163 vs 0.081).
    s = 0
    v4h, v1h, rd, cvd = c['vol_ratio_4h'], c['vol_ratio_1h'], c['resistance_distance'], c['cvd_shift']

    if v4h >= 3: s += 46
    elif v4h >= 2: s += 32
    elif v4h >= 1.5: s += 18
    elif v4h >= 1: s += 8

    if v1h >= 3: s += 22
    elif v1h >= 2: s += 15
    elif v1h >= 1.5: s += 9
    elif v1h >= 1: s += 4

    if rd >= 10: s += 19
    elif rd >= 5: s += 13
    elif rd >= 2: s += 7
    elif rd > 0: s += 3

    if cvd >= 20: s += 13
    elif cvd >= 10: s += 9
    elif cvd >= 5: s += 5
    elif cvd > 0: s += 2

    return max(0, min(100, round(s)))


def status_label(setup, trigger, ready_setup, ready_trigger):
    if setup >= ready_setup and trigger >= ready_trigger:
        return '🚀 Ready'
    if setup >= ready_setup:
        return '🟡 Coiled'
    if trigger >= ready_trigger:
        return '⚡ Reactive'
    return '⚪ No Setup'


def analyze(c, ref_change):
    try:
        c = dict(c)
        symbol = c['symbol']
        # 360 candles (~15 days) instead of the old 50 — cooldown_hours needs
        # up to 336 hours of lookback to find the last >=15% move.
        df1h = get_klines(symbol, '1h', 360)
        df15m = get_klines(symbol, '15m', 25)
        df4h = get_klines(symbol, '4h', 30)

        (c['higher_low'], c['resistance_distance'],
         c['resistance_tests'], c['resistance']) = structure_and_volume(df1h)
        current = float(df1h['close'].iloc[-1]) if not df1h.empty else c['price']
        c['support'], c['support_distance'], c['resistance_2'], c['support_2'] = price_levels(df1h, c['resistance'], current)
        c['higher_low_15m'] = higher_low(df15m['low']) if not df15m.empty else False
        c['higher_low_4h'] = higher_low(df4h['low']) if not df4h.empty else False

        # Backtest-validated scoring inputs
        c['cooldown_hours'] = cooldown_hours(df1h)
        c['vol_ratio_1h'] = ratio(df1h['quote_volume'], 20)
        c['vol_ratio_4h'] = ratio(df4h['quote_volume'], 20)
        c['cvd_shift'] = cvd_shift_pct_from_df(df15m)
        c['rvol'] = get_rvol(symbol, c['volume_24h'])
        c['oi_change_24h'] = get_oi_change_24h(symbol)
        c['funding'] = funding(symbol)

        # Display-only context (not fed into either score)
        c['spot_futures_ratio'], c['spot_change_24h'], c['spot_context'] = spot_context(symbol, c['volume_24h'])
        c['relative_strength'] = float(c['change_24h'] - ref_change)
        c['event'] = get_event_badge(symbol)

        c['setup'] = setup_score(c)
        c['trigger'] = trigger_score(c)
        c['status'] = status_label(c['setup'], c['trigger'], ready_setup_threshold, ready_trigger_threshold)
        return c
    except Exception:
        return None


def scan():
    try:
        tickers = get_tickers()
    except Exception as e:
        st.error(f"Couldn't reach Binance Futures API ({e}). Check your connection and try again.")
        return pd.DataFrame()

    if not tickers:
        st.warning('Binance returned no perpetual USDT contracts. Try scanning again in a moment.')
        return pd.DataFrame()

    candidates = [x for x in tickers if x['symbol'] not in excluded_symbols
                  and x['volume_24h'] >= min_vol and min_change <= x['change_24h'] <= max_change]
    if not candidates:
        return pd.DataFrame()

    # Cap + prioritize by volume so a loose filter (e.g. min_vol=0) can't
    # trigger hundreds of parallel symbol analyses and choke the scan. Majors
    # are already excluded above, so this ranking is no longer dominated by
    # BTC/ETH/BNB-sized volume.
    candidates.sort(key=lambda x: x['volume_24h'], reverse=True)
    candidates = candidates[:max_candidates]

    t = {x['symbol']: x for x in tickers}
    ref_change = (t.get('BTCUSDT', {}).get('change_24h', 0) + t.get('ETHUSDT', {}).get('change_24h', 0)) / 2

    results = []
    progress = st.progress(0.0, text=f'Analyzing 0/{len(candidates)} candidates...')
    done = 0
    errors = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(analyze, c, ref_change) for c in candidates]
            for f in as_completed(futures):
                x = f.result()
                done += 1
                progress.progress(done / len(candidates), text=f'Analyzing {done}/{len(candidates)} candidates...')
                if x:
                    results.append(x)
                else:
                    errors += 1
    finally:
        progress.empty()

    if errors:
        st.caption(f'{errors} of {len(candidates)} candidates failed to analyze (timeout or bad data) and were skipped.')

    if not results:
        return pd.DataFrame()

    result_df = pd.DataFrame(results)
    result_df = result_df[result_df['rvol'] >= min_rvol]
    if result_df.empty:
        return pd.DataFrame()
    # Setup first — this tool's purpose is catching coins positioned for a
    # move before it's obvious, so "coiled" ranks above "already moving."
    return result_df.sort_values(['setup', 'trigger'], ascending=[False, False]).head(max_results)


def render():
    try:
        _render()
    except Exception as e:
        st.error(f'Something went wrong during this scan cycle: {e}. It will retry on the next scan.')


def _render():
    triggered = st.button('🔄 Scan now', type='primary')
    if triggered or 'df' not in st.session_state:
        with st.spinner('Scanning Binance USDT perpetual futures...'):
            st.session_state.df = scan()
            st.session_state.last_scan = time.strftime('%H:%M:%S')

    if 'last_scan' in st.session_state:
        st.caption(f'Last scan: {st.session_state.last_scan}')

    df = st.session_state.get('df', pd.DataFrame())
    if df.empty:
        st.warning('No candidates matched the filters. Try lowering the volume/RVOL thresholds or widening the 24h change range.')
        return

    # Slim leaderboard: just enough to triage at a glance. Everything else
    # lives in the detail panel below so this never turns into a wide,
    # messy table again.
    lb = df.copy()
    lb.insert(0, 'Rank', range(1, len(lb) + 1))
    lb['Price'] = lb['price'].map(lambda x: f'{x:.8g}')
    lb['Support'] = lb['support'].map(lambda x: f'{x:.8g}')
    lb['Resistance'] = lb['resistance'].map(lambda x: f'{x:.8g}')
    lb['24h %'] = lb['change_24h'].map(lambda x: f'{x:.2f}%')
    lb['RVOL'] = lb['rvol'].map(lambda x: f'{x:.2f}x')
    lb['chart'] = lb['symbol'].apply(lambda s: f'https://www.tradingview.com/chart/?symbol=BINANCE%3A{s}')
    st.dataframe(
        lb[['Rank', 'symbol', 'Price', 'Support', 'Resistance', 'setup', 'trigger', 'status', '24h %', 'RVOL', 'chart']]
        .rename(columns={'symbol': 'Coin', 'setup': 'Setup', 'trigger': 'Trigger', 'status': 'Status'}),
        hide_index=True, use_container_width=True,
        column_config={'chart': st.column_config.LinkColumn('Chart', display_text='Open ↗')}
    )

    st.divider()
    st.subheader('Exact Levels')
    options = df['symbol'].tolist()
    selected = st.selectbox('Select a coin', options, index=0)
    row = df[df['symbol'] == selected].iloc[0]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric('Setup', row['setup'])
    c2.metric('Trigger', row['trigger'])
    c3.metric('Status', row['status'])
    c4.metric('RVOL (14d)', f"{row['rvol']:.2f}x")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric('Current Price', f"{row['price']:.8g}")
    c6.metric('Resistance', f"{row['resistance']:.8g}", f"{row['resistance_distance']:.2f}% away")
    c7.metric('Support', f"{row['support']:.8g}", f"-{row['support_distance']:.2f}% away")
    c8.metric('CVD Shift', f"{row['cvd_shift']:+.2f}%")

    if row['event']:
        st.info(f"📰 {row['event']}")

    st.markdown('**Price Map**')
    levels = [('R2', row['resistance_2']), ('R1', row['resistance']), ('Current', row['price']),
              ('S1', row['support']), ('S2', row['support_2'])]
    pm = pd.DataFrame([{'Level': lvl, 'Price': (f"{val:.8g}" if val is not None else '—')} for lvl, val in levels])
    st.table(pm.set_index('Level'))
    st.caption('R2/S2 are the next swing high/low further back than R1/S1 — a rough map, not exact volume-based support/resistance.')

    st.markdown('**Structure**')
    def badge(v):
        return '✅' if v else '❌'
    st.write(f"15M Higher Low: {badge(row['higher_low_15m'])}  |  1H Higher Low: {badge(row['higher_low'])}  |  4H Higher Low: {badge(row['higher_low_4h'])}")
    st.caption('Shown for context — not scored (backtested separately and not included in either score above).')

    with st.expander('Score breakdown & more metrics for this coin'):
        st.write({
            'Cooldown': f"{row['cooldown_hours']}h since last ≥15% move",
            '24h Change %': f"{row['change_24h']:.2f}%",
            'RVOL (14d)': f"{row['rvol']:.2f}x",
            'OI Change % (24h)': f"{row['oi_change_24h']:.2f}%",
            'Funding rate': f"{row['funding']:.4f}%",
            'Vol ratio — 4h': f"{row['vol_ratio_4h']:.2f}x",
            'Vol ratio — 1h': f"{row['vol_ratio_1h']:.2f}x",
            'Resistance distance %': f"{row['resistance_distance']:.2f}%",
            'CVD Shift %': f"{row['cvd_shift']:+.2f}%",
            '24h Volume': f"${row['volume_24h'] / 1e6:.1f}M",
            'Spot/Futures ratio': f"{row['spot_futures_ratio']:.2f}x",
            'Spot 24h %': f"{row['spot_change_24h']:.2f}%",
            'Spot context': row['spot_context'],
            'Relative Strength vs BTC/ETH': f"{row['relative_strength']:+.2f}%",
            'Resistance tests': int(row['resistance_tests']),
            'TradingView': f"https://www.tradingview.com/chart/?symbol=BINANCE%3A{row['symbol']}",
        })
        st.caption('Relative Strength and Resistance tests are shown for context only — neither is backtest-validated, so neither feeds Setup or Trigger.')

    st.info('🚀 Ready = Setup and Trigger both clear your thresholds — the actionable zone. 🟡 Coiled = well-positioned, not moving yet. ⚡ Reactive = moving now without a strong base (late-entry risk). ⚪ No Setup = neither. Every scoring input was tested against real 30-day Binance data — see the backtest script for methodology — but this is still a shortlist tool, not a signal to trade blind.')


# Auto-refresh: prefer st.fragment(run_every=...) (Streamlit ≥1.33) so only
# this section reruns on a timer, instead of blocking the whole script with
# time.sleep() and reloading the entire page every cycle.
if hasattr(st, 'fragment'):
    scanner = st.fragment(run_every=refresh_seconds)(render) if auto else st.fragment(render)
    scanner()
else:
    render()
    if auto:
        time.sleep(refresh_seconds)
        st.rerun()
