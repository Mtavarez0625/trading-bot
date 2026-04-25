import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests
from dotenv import load_dotenv
from fastapi import FastAPI

import journal

load_dotenv()

# ── DRY_RUN mode ──────────────────────────────────────────────────────────────
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

app = FastAPI(title="Trading Bot API V3")

@app.on_event("startup")
def _startup():
    journal.init_db()

trade_log = []

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL")
DATA_URL   = os.getenv("ALPACA_DATA_URL")

# ── Watchlist (comma-separated env var or hardcoded default) ──────────────────
_wl_env   = os.getenv("WATCHLIST", "")
WATCHLIST = [s.strip() for s in _wl_env.split(",") if s.strip()] or ["GOOGL", "AAPL", "MSFT"]

# ── V3 Risk & Strategy Constants (all configurable via .env) ──────────────────
MAX_OPEN_POSITIONS     = int(os.getenv("MAX_OPEN_POSITIONS",    "3"))
TRADE_COOLDOWN_MINUTES = int(os.getenv("TRADE_COOLDOWN_MINUTES","15"))
MIN_TREND_STRENGTH     = float(os.getenv("MIN_TREND_STRENGTH",  "0.01"))
DAILY_LOSS_LIMIT_PCT   = float(os.getenv("DAILY_LOSS_LIMIT_PCT","0.03"))
MAX_ALLOCATION_PCT     = float(os.getenv("MAX_ALLOCATION_PCT",  "0.10"))
RISK_PER_TRADE_PCT     = float(os.getenv("RISK_PER_TRADE_PCT",  "0.01"))
STOP_LOSS_PCT          = float(os.getenv("STOP_LOSS_PCT",       "0.03"))
TAKE_PROFIT_PCT        = float(os.getenv("TAKE_PROFIT_PCT",     "0.05"))
TRAILING_STOP_PCT      = float(os.getenv("TRAILING_STOP_PCT",   "0.0"))  # 0 = use fixed stop

# ── V3 Signal Enhancement Constants ──────────────────────────────────────────
RSI_PERIOD         = int(os.getenv("RSI_PERIOD",       "14"))
RSI_OVERBOUGHT     = float(os.getenv("RSI_OVERBOUGHT", "75"))
BREAKOUT_LOOKBACK  = int(os.getenv("BREAKOUT_LOOKBACK","20"))
INTRADAY_TIMEFRAME = os.getenv("INTRADAY_TIMEFRAME",   "15Min")
DAILY_TIMEFRAME    = os.getenv("DAILY_TIMEFRAME",      "1Day")

# ── V3 Early Trend & Filter Tuning (all configurable via .env) ────────────────
# These settings loosen the strategy to generate more high-quality entries.
ALLOW_EARLY_TREND_ENTRY       = os.getenv("ALLOW_EARLY_TREND_ENTRY",      "true").lower() == "true"
EARLY_TREND_MAX_SMA_GAP_PCT   = float(os.getenv("EARLY_TREND_MAX_SMA_GAP_PCT", "0.03"))  # SMA20 may be up to 3% below SMA50
SMA20_RISING_BARS             = int(os.getenv("SMA20_RISING_BARS",         "3"))           # SMA20 must have risen this many consecutive bars
MIN_VOLUME_RATIO              = float(os.getenv("MIN_VOLUME_RATIO",         "0.25"))        # 25% of 20-day avg — relaxed gate
REQUIRE_BREAKOUT_FOR_BUY      = os.getenv("REQUIRE_BREAKOUT_FOR_BUY",     "false").lower() == "true"
REQUIRE_INTRADAY_CONFIRMATION = os.getenv("REQUIRE_INTRADAY_CONFIRMATION", "true").lower() == "true"
REQUIRE_SPY_BULLISH           = os.getenv("REQUIRE_SPY_BULLISH",           "false").lower() == "true"

# ── V3.1 Quality & Diagnostic Enhancements (all configurable via .env) ────────
# Intraday tolerance: allow close to be within this % below intraday SMA20 and still pass
# when combined with ALLOW_STRONG_DAILY_WEAK_INTRADAY=true on a strong daily tier
INTRADAY_SMA_TOLERANCE_PCT         = float(os.getenv("INTRADAY_SMA_TOLERANCE_PCT",        "0.005"))  # 0.5%
ALLOW_STRONG_DAILY_WEAK_INTRADAY   = os.getenv("ALLOW_STRONG_DAILY_WEAK_INTRADAY",         "false").lower() == "true"
# Require MACD histogram to be rising (curr > prev) for early-trend entries — filters weakening momentum
EARLY_TREND_REQUIRE_MACD_IMPROVING = os.getenv("EARLY_TREND_REQUIRE_MACD_IMPROVING",       "true").lower() == "true"

# ── Execution safety & paper-trade realism (all configurable via .env) ────────
# Kill switch: when true, only position monitoring/protection runs — no new entries.
DISABLE_NEW_ENTRIES         = os.getenv("DISABLE_NEW_ENTRIES",         "false").lower() == "true"
# Slippage applied to paper-trade PnL: buy price * (1+slip), sell price * (1-slip).
SLIPPAGE_PCT                = float(os.getenv("SLIPPAGE_PCT",          "0.0005"))  # 5 bps
# Stale-data guard: skip new entries when latest bar is older than this many hours.
STALE_DATA_MAX_HOURS        = int(os.getenv("STALE_DATA_MAX_HOURS",    "72"))       # 3 trading days
# Symbol-level error cooldown: after N consecutive fetch errors, skip that symbol.
SYMBOL_ERROR_THRESHOLD      = int(os.getenv("SYMBOL_ERROR_THRESHOLD",  "3"))
SYMBOL_ERROR_COOLDOWN_MIN   = int(os.getenv("SYMBOL_ERROR_COOLDOWN_MIN", "60"))
# Observe-only mode: after N consecutive global API failures, disable new entries.
OBSERVE_ONLY_AFTER_FAILURES = int(os.getenv("OBSERVE_ONLY_AFTER_FAILURES", "10"))

# ── Runtime safety state (in-memory) ─────────────────────────────────────────
_last_trade_time: dict     = {}          # {symbol: datetime} — trade cooldown
_symbol_error_counts: dict = {}          # {symbol: int} — consecutive fetch errors
_symbol_error_cooldown: dict = {}        # {symbol: datetime} — error-cooldown expiry
_api_failure_count: int    = 0           # consecutive global API/data failures
_observe_only_mode: bool   = False       # set True when _api_failure_count exceeds threshold
_last_market_check: Optional[datetime] = None  # timestamp of last market-status call
_session_start: datetime = datetime.now(timezone.utc)


# ── Shared headers ────────────────────────────────────────────────────────────
def _headers():
    return {
        "APCA-API-KEY-ID":     API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }


# ── Cooldown helpers ──────────────────────────────────────────────────────────
def _is_in_cooldown(symbol: str) -> bool:
    last = _last_trade_time.get(symbol)
    if last is None:
        return False
    elapsed_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
    return elapsed_min < TRADE_COOLDOWN_MINUTES


def _record_trade_time(symbol: str):
    _last_trade_time[symbol] = datetime.now(timezone.utc)


# ── Symbol error cooldown helpers ─────────────────────────────────────────────
def _record_symbol_error(symbol: str):
    global _api_failure_count, _observe_only_mode
    _symbol_error_counts[symbol] = _symbol_error_counts.get(symbol, 0) + 1
    _api_failure_count += 1
    if _api_failure_count >= OBSERVE_ONLY_AFTER_FAILURES and not _observe_only_mode:
        _observe_only_mode = True
        print(
            f"[safety] OBSERVE-ONLY MODE activated after {_api_failure_count} "
            f"consecutive API failures — new entries disabled until failures clear"
        )
    if _symbol_error_counts[symbol] >= SYMBOL_ERROR_THRESHOLD:
        expiry = datetime.now(timezone.utc) + timedelta(minutes=SYMBOL_ERROR_COOLDOWN_MIN)
        _symbol_error_cooldown[symbol] = expiry
        print(
            f"[safety] {symbol} in ERROR COOLDOWN for {SYMBOL_ERROR_COOLDOWN_MIN}m "
            f"after {_symbol_error_counts[symbol]} consecutive errors"
        )


def _clear_symbol_error(symbol: str):
    global _api_failure_count, _observe_only_mode
    if _symbol_error_counts.get(symbol, 0) > 0:
        _symbol_error_counts[symbol] = 0
    if _api_failure_count > 0:
        _api_failure_count = max(0, _api_failure_count - 1)
    if _observe_only_mode and _api_failure_count < OBSERVE_ONLY_AFTER_FAILURES:
        _observe_only_mode = False
        print("[safety] OBSERVE-ONLY MODE cleared — API failures dropped below threshold")


def _is_symbol_error_cooldown(symbol: str) -> bool:
    expiry = _symbol_error_cooldown.get(symbol)
    if expiry is None:
        return False
    if datetime.now(timezone.utc) >= expiry:
        del _symbol_error_cooldown[symbol]
        _symbol_error_counts[symbol] = 0
        return False
    return True


# ── Stale-data guard ──────────────────────────────────────────────────────────
def _is_data_stale(df: pd.DataFrame):
    """
    Return (True, reason) if the most recent bar is older than STALE_DATA_MAX_HOURS.
    Approximation: uses bar timestamp from the 't' column; no intrabar precision.
    Returns (False, "") when data is fresh or timestamp unavailable (fail open).
    """
    if df.empty or "t" not in df.columns:
        return False, ""
    try:
        latest_ts_raw = df["t"].iloc[-1]
        if isinstance(latest_ts_raw, str):
            latest_ts = datetime.fromisoformat(latest_ts_raw.replace("Z", "+00:00"))
        else:
            latest_ts = pd.Timestamp(latest_ts_raw).to_pydatetime()
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - latest_ts).total_seconds() / 3600
        if age_hours > STALE_DATA_MAX_HOURS:
            return True, (
                f"Stale data: latest bar is {round(age_hours, 1)}h old "
                f"(threshold={STALE_DATA_MAX_HOURS}h)"
            )
    except Exception:
        pass  # fail open — do not block on timestamp parse errors
    return False, ""


# ── Open position count ───────────────────────────────────────────────────────
def _count_open_long_positions() -> int:
    url = f"{BASE_URL}/v2/positions"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        if response.status_code != 200:
            return 0
        positions = response.json()
        return sum(1 for p in positions if float(p.get("qty", 0)) > 0)
    except Exception:
        return 0


# ── Daily loss limit ──────────────────────────────────────────────────────────
def _is_daily_loss_limit_reached():
    """
    Check if today's equity loss exceeds DAILY_LOSS_LIMIT_PCT.
    Fails open (returns False) on API errors so trading is not blocked.
    """
    url = f"{BASE_URL}/v2/account"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        data = response.json()
        equity      = float(data.get("equity",      0))
        last_equity = float(data.get("last_equity", 0))
    except Exception:
        return False, ""

    if last_equity <= 0:
        return False, ""

    daily_change_pct = (equity - last_equity) / last_equity
    if daily_change_pct <= -DAILY_LOSS_LIMIT_PCT:
        loss_pct = round(abs(daily_change_pct) * 100, 2)
        reason = (
            f"Daily loss limit reached — {loss_pct}% loss today "
            f"(limit: {DAILY_LOSS_LIMIT_PCT * 100}%) — new entries disabled"
        )
        return True, reason

    return False, ""


# ── Generic bar fetcher ───────────────────────────────────────────────────────
def _fetch_bars(
    symbol: str,
    timeframe: str = "1Day",
    days: int = 180,
    limit: int = 200,
) -> pd.DataFrame:
    """Fetch OHLCV bars from Alpaca. Returns empty DataFrame on any error."""
    url   = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    params = {
        "timeframe": timeframe,
        "start":     start.isoformat(),
        "end":       end.isoformat(),
        "limit":     limit,
        "feed":      "iex",
        "sort":      "asc",
    }
    try:
        response = requests.get(url, headers=_headers(), params=params, timeout=10)
        response.raise_for_status()
        bars = response.json().get("bars", [])
        if not bars or not isinstance(bars, list):
            _record_symbol_error(symbol)
            return pd.DataFrame()
        _clear_symbol_error(symbol)
        return pd.DataFrame(bars)
    except Exception:
        _record_symbol_error(symbol)
        return pd.DataFrame()


# ── Technical indicator helpers ───────────────────────────────────────────────
def _rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    """Compute RSI series using Wilder's EMA smoothing. RSI=100 when there are no losses."""
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.where(avg_loss > 0, other=1e-10)
    rsi      = 100 - (100 / (1 + rs))
    return rsi.where(avg_loss > 0, other=100.0)


def _macd_series(closes: pd.Series):
    """Return (macd_line, signal_line) as pandas Series."""
    ema12       = closes.ewm(span=12, adjust=False).mean()
    ema26       = closes.ewm(span=26, adjust=False).mean()
    macd_line   = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal_line


# ── Raw signal computation (daily bars, no SPY / MTF) ────────────────────────
def _compute_raw_signal(symbol: str) -> dict:
    """
    Fetch daily bars and compute all V3 technical indicators:
      - SMA20 / SMA50 crossover
      - Trend strength filter
      - Volume confirmation
      - RSI overbought filter
      - MACD bullish filter
      - Breakout confirmation (close > N-bar high)

    Does NOT apply the SPY market-direction or intraday MTF filters.
    """
    df = _fetch_bars(symbol, timeframe="1Day", days=180, limit=200)

    if df.empty:
        return {"error": "No bar data returned"}

    try:
        df["sma_20"] = df["c"].rolling(window=20).mean()
        df["sma_50"] = df["c"].rolling(window=50).mean()

        if "v" in df.columns:
            df["vol_sma_20"] = df["v"].rolling(window=20).mean()

        latest = df.iloc[-1]

        if pd.isna(latest["sma_20"]) or pd.isna(latest["sma_50"]):
            return {"error": "Not enough bars to calculate SMA values (need at least 50)"}

        close        = float(latest["c"])
        sma_20       = float(latest["sma_20"])
        sma_50       = float(latest["sma_50"])
        trend_strength = abs(sma_20 - sma_50) / sma_50
        trend_strong   = trend_strength >= MIN_TREND_STRENGTH

        # Volume confirmation
        vol_sma20_val  = latest.get("vol_sma_20") if "vol_sma_20" in df.columns else None
        has_volume     = vol_sma20_val is not None and not pd.isna(vol_sma20_val)
        if has_volume:
            current_volume   = int(latest["v"])
            vol_sma_20       = float(vol_sma20_val)
            # Relaxed: volume only needs to reach MIN_VOLUME_RATIO of the 20-day average
            volume_confirmed = current_volume >= vol_sma_20 * MIN_VOLUME_RATIO
        else:
            current_volume   = None
            vol_sma_20       = None
            volume_confirmed = True  # fail open when data unavailable

        # RSI
        rsi    = None
        rsi_ok = True
        if len(df) >= RSI_PERIOD + 1:
            try:
                rsi_val = float(_rsi_series(df["c"], RSI_PERIOD).iloc[-1])
                if not pd.isna(rsi_val):
                    rsi    = round(rsi_val, 2)
                    rsi_ok = rsi < RSI_OVERBOUGHT
            except Exception:
                pass  # fail open

        # MACD — also compute histogram and histogram direction for early-trend quality check
        macd_line_val         = None
        macd_signal_val       = None
        macd_bullish          = True
        macd_histogram        = None
        macd_histogram_rising = True  # fail open — don't block when data is unavailable
        if len(df) >= 35:
            try:
                ml, sl = _macd_series(df["c"])
                ml_v, sl_v = float(ml.iloc[-1]), float(sl.iloc[-1])
                if not pd.isna(ml_v) and not pd.isna(sl_v):
                    macd_line_val         = round(ml_v, 4)
                    macd_signal_val       = round(sl_v, 4)
                    macd_bullish          = macd_line_val > macd_signal_val
                    macd_histogram        = round(ml_v - sl_v, 4)
                    # Histogram direction: is momentum accelerating vs previous bar?
                    if len(df) >= 36:
                        ml_prev, sl_prev = float(ml.iloc[-2]), float(sl.iloc[-2])
                        if not pd.isna(ml_prev) and not pd.isna(sl_prev):
                            macd_histogram_rising = macd_histogram > round(ml_prev - sl_prev, 4)
            except Exception:
                pass  # fail open

        # Breakout: close must exceed the highest close of the prior BREAKOUT_LOOKBACK bars
        breakout_high      = None
        breakout_confirmed = True
        if len(df) >= BREAKOUT_LOOKBACK + 1:
            past_highs    = df["c"].iloc[-(BREAKOUT_LOOKBACK + 1):-1]
            breakout_high = round(float(past_highs.max()), 2)
            breakout_confirmed = close > breakout_high

        # ── Two-tier entry model ──────────────────────────────────────────────
        # Tier 1 — Strong trend: price above SMA20 and SMA20 crossed above SMA50.
        strong_trend_buy = close > sma_20 and sma_20 > sma_50

        # Tier 2 — Early trend: price above SMA20, SMA20 within EARLY_TREND_MAX_SMA_GAP_PCT
        # of SMA50, and SMA20 has been rising for SMA20_RISING_BARS consecutive bars.
        # Lets the bot enter a developing crossover before it fully completes.
        early_trend_buy   = False
        sma20_is_rising   = False
        sma_gap_pct       = 0.0
        macd_improving_ok = True  # default open — only meaningful inside early-trend check
        if ALLOW_EARLY_TREND_ENTRY and not strong_trend_buy and close > sma_20:
            sma_gap_pct = (sma_50 - sma_20) / sma_50  # positive when SMA20 is below SMA50
            if sma_gap_pct <= EARLY_TREND_MAX_SMA_GAP_PCT:
                # SMA20 must have risen every bar for the last SMA20_RISING_BARS bars
                if len(df) >= SMA20_RISING_BARS + 1:
                    sma20_slice = df["sma_20"].iloc[-(SMA20_RISING_BARS + 1):]
                    sma20_is_rising = all(
                        sma20_slice.iloc[i] < sma20_slice.iloc[i + 1]
                        for i in range(len(sma20_slice) - 1)
                    )
                # Early-trend quality gate: require MACD histogram to be improving
                # (current bar histogram > previous bar histogram).
                # This prevents entering pre-crossover setups with decelerating momentum.
                if EARLY_TREND_REQUIRE_MACD_IMPROVING:
                    macd_improving_ok = macd_histogram_rising
                early_trend_buy = sma20_is_rising and macd_improving_ok

        # Determine base signal and entry tier
        entry_tier = None
        if strong_trend_buy:
            entry_tier    = "strong"
            base_signal   = "BUY"
            signal_reason = (
                f"Strong trend BUY: close={round(close,2)} above SMA20={round(sma_20,2)}"
                f" and SMA20 above SMA50={round(sma_50,2)}"
            )
        elif early_trend_buy:
            entry_tier    = "early"
            base_signal   = "BUY"
            signal_reason = (
                f"Early trend BUY: close above SMA20={round(sma_20,2)}, SMA20 rising"
                f" {SMA20_RISING_BARS} bars, gap to SMA50={round(sma_gap_pct*100,2)}%"
                f" (max={EARLY_TREND_MAX_SMA_GAP_PCT*100}%),"
                f" MACD hist={'rising ✓' if macd_histogram_rising else 'flat'}"
            )
        elif close < sma_20:
            base_signal   = "SELL"
            signal_reason = f"Close={round(close,2)} below SMA20={round(sma_20,2)} — exit signal"
        else:
            base_signal = "HOLD"
            # Give a precise reason so it's clear exactly what needs to change to get a BUY
            if not ALLOW_EARLY_TREND_ENTRY:
                signal_reason = (
                    f"HOLD: price above SMA20={round(sma_20,2)} but SMA20"
                    f" below SMA50={round(sma_50,2)} (early-trend entry disabled)"
                )
            else:
                gap_pct_display = round((sma_50 - sma_20) / sma_50 * 100, 2)
                if gap_pct_display > EARLY_TREND_MAX_SMA_GAP_PCT * 100:
                    signal_reason = (
                        f"HOLD: SMA20={round(sma_20,2)} is {gap_pct_display}% below"
                        f" SMA50={round(sma_50,2)} — gap exceeds"
                        f" {round(EARLY_TREND_MAX_SMA_GAP_PCT*100,1)}% threshold"
                    )
                elif not sma20_is_rising:
                    signal_reason = (
                        f"HOLD: SMA20 gap acceptable ({round((sma_50-sma_20)/sma_50*100,2)}%)"
                        f" but SMA20 not rising for {SMA20_RISING_BARS} consecutive bars"
                    )
                elif not macd_improving_ok:
                    signal_reason = (
                        f"HOLD: SMA20 rising {SMA20_RISING_BARS} bars, gap={round(sma_gap_pct*100,2)}% acceptable"
                        f" but MACD histogram not improving (momentum decelerating —"
                        f" hist={macd_histogram})"
                    )
                else:
                    signal_reason = (
                        f"HOLD: price above SMA20={round(sma_20,2)} but SMA20"
                        f" below SMA50={round(sma_50,2)}"
                    )

        # ── Apply BUY filters — first failure wins ────────────────────────────
        signal = base_signal
        if base_signal == "BUY":
            # Trend strength — only enforced for strong-trend entries.
            # Early-trend uses SMA20 rising as its momentum qualifier instead.
            if entry_tier == "strong" and not trend_strong:
                signal        = "HOLD"
                signal_reason = (
                    f"Blocked: weak trend strength={round(trend_strength,4)}"
                    f" < min={MIN_TREND_STRENGTH}"
                )

            # Volume gate — volume must reach at least MIN_VOLUME_RATIO of the 20-day average
            if signal == "BUY" and not volume_confirmed:
                signal        = "HOLD"
                signal_reason = (
                    f"Blocked: volume {current_volume:,} < {int(MIN_VOLUME_RATIO*100)}%"
                    f" of 20d avg (need {int(vol_sma_20 * MIN_VOLUME_RATIO):,},"
                    f" got {current_volume:,})"
                )

            if signal == "BUY" and rsi is not None and not rsi_ok:
                signal        = "HOLD"
                signal_reason = (
                    f"Blocked: RSI={rsi} overbought (threshold={RSI_OVERBOUGHT})"
                )

            if signal == "BUY" and macd_line_val is not None and not macd_bullish:
                signal        = "HOLD"
                signal_reason = (
                    f"Blocked: bearish MACD (line={macd_line_val} < signal={macd_signal_val})"
                )

            # Breakout only blocks when REQUIRE_BREAKOUT_FOR_BUY=true (default false)
            if signal == "BUY" and REQUIRE_BREAKOUT_FOR_BUY and not breakout_confirmed:
                signal        = "HOLD"
                signal_reason = (
                    f"Blocked: breakout not confirmed"
                    f" (close={round(close,2)} <= {BREAKOUT_LOOKBACK}d high={breakout_high})"
                )

        return {
            "symbol":             symbol,
            "close":              round(close, 2),
            "sma_20":             round(sma_20, 2),
            "sma_50":             round(sma_50, 2),
            "trend_strength":     round(trend_strength, 4),
            "trend_strong":       trend_strong,
            "entry_tier":         entry_tier,   # "strong", "early", or None
            "current_volume":     current_volume,
            "vol_sma_20":         round(vol_sma_20, 0) if vol_sma_20 is not None else None,
            "volume_confirmed":   volume_confirmed,
            "rsi":                rsi,
            "rsi_ok":             rsi_ok,
            "macd_line":              macd_line_val,
            "macd_signal_line":       macd_signal_val,
            "macd_bullish":           macd_bullish,
            "macd_histogram":         macd_histogram,
            "macd_histogram_rising":  macd_histogram_rising,
            "breakout_high":          breakout_high,
            "breakout_confirmed": breakout_confirmed,
            "signal":             signal,
            "signal_reason":      signal_reason,
        }
    except Exception as e:
        return {"error": f"Failed to calculate signal: {str(e)}"}


# ── SPY market direction filter ───────────────────────────────────────────────
def _is_spy_bullish():
    """
    Return (True, reason) when SPY raw signal is BUY.
    Fails open on data errors so trading is not blocked.
    """
    spy_data = _compute_raw_signal("SPY")
    if "error" in spy_data:
        return True, f"SPY data unavailable ({spy_data['error']}) — market filter skipped"
    if spy_data["signal"] == "BUY":
        return True, "SPY is bullish"
    return False, f"Blocked by market filter (SPY signal={spy_data['signal']})"


# ── Intraday multi-timeframe confirmation ─────────────────────────────────────
def _get_intraday_confirmation(symbol: str, entry_tier: str = None):
    """
    Check if intraday price is above its 20-bar SMA on INTRADAY_TIMEFRAME.

    Supports a configurable tolerance so a strong daily setup isn't hard-blocked by
    a small intraday lag:
      - If close is within INTRADAY_SMA_TOLERANCE_PCT below intraday SMA20 AND
        ALLOW_STRONG_DAILY_WEAK_INTRADAY=true AND daily tier is "strong" → marginal pass.
      - Otherwise any close below intraday SMA20 is a fail.

    Returns (confirmed: bool, reason: str, margin_pct: float).
      margin_pct is positive when close > SMA20, negative when below.
    Fails open on data errors.
    """
    df = _fetch_bars(symbol, timeframe=INTRADAY_TIMEFRAME, days=10, limit=100)
    if df.empty or len(df) < 20:
        return True, f"Intraday data unavailable ({INTRADAY_TIMEFRAME}) — MTF filter skipped", 0.0

    df["sma_20"] = df["c"].rolling(window=20).mean()
    latest = df.iloc[-1]

    if pd.isna(latest["sma_20"]):
        return True, f"Intraday SMA20 not ready ({INTRADAY_TIMEFRAME}) — MTF filter skipped", 0.0

    close          = float(latest["c"])
    intraday_sma20 = float(latest["sma_20"])
    margin_pct     = (close - intraday_sma20) / intraday_sma20  # + = above SMA20, - = below

    if close > intraday_sma20:
        return True, (
            f"Intraday ({INTRADAY_TIMEFRAME}) close={round(close,2)} "
            f"above SMA20={round(intraday_sma20,2)} (+{round(margin_pct*100,2)}%)"
        ), round(margin_pct, 4)

    # Close is below intraday SMA20 — check tolerance override
    abs_margin = abs(margin_pct)
    if abs_margin <= INTRADAY_SMA_TOLERANCE_PCT and ALLOW_STRONG_DAILY_WEAK_INTRADAY and entry_tier == "strong":
        return True, (
            f"Intraday marginal PASS: close={round(close,2)} is {round(abs_margin*100,2)}% below "
            f"SMA20={round(intraday_sma20,2)} (within {round(INTRADAY_SMA_TOLERANCE_PCT*100,2)}% tolerance,"
            f" strong daily tier override)"
        ), round(margin_pct, 4)

    return False, (
        f"Blocked by intraday trend ({INTRADAY_TIMEFRAME}: "
        f"close={round(close,2)} is {round(abs_margin*100,2)}% below SMA20={round(intraday_sma20,2)})"
    ), round(margin_pct, 4)


def _build_decision_summary(signal_data: dict) -> str:
    """
    Build a concise one-line decision summary for logging and API visibility.

    Examples:
      "BUY [strong-trend] | RSI=58 | MACD↑ | vol✓ | intraday✓ (+1.2%)"
      "BUY [early-trend] | RSI=52 | MACD~ | vol✓ | intraday marginal"
      "HOLD: MACD histogram not improving (momentum decelerating)"
      "SELL: close below SMA20"
    """
    signal = signal_data.get("signal", "?")
    tier   = signal_data.get("entry_tier")

    if signal == "BUY":
        parts = [f"BUY [{tier}-trend]"]
        rsi = signal_data.get("rsi")
        if rsi is not None:
            parts.append(f"RSI={rsi}")
        if signal_data.get("macd_bullish") is True:
            hist_tag = "↑" if signal_data.get("macd_histogram_rising") else "~"
            parts.append(f"MACD{hist_tag}")
        if signal_data.get("volume_confirmed"):
            parts.append("vol✓")
        intraday_ok  = signal_data.get("intraday_confirmed")
        margin_pct   = signal_data.get("intraday_margin_pct", 0.0) or 0.0
        if intraday_ok is True:
            if margin_pct > 0:
                # Normal confirmed: close is above intraday SMA20 by this %
                parts.append(f"intraday✓ (+{round(margin_pct*100,2)}%)")
            elif margin_pct < 0:
                # Tolerance override: close was slightly below intraday SMA20
                parts.append(f"intraday✓ (marginal {round(margin_pct*100,2)}%)")
            else:
                # Data unavailable (fail-open) or symbol not checked
                parts.append("intraday✓")
        return " | ".join(parts)

    reason = signal_data.get("signal_reason", "")
    if signal == "SELL":
        return f"SELL: {reason}"
    if signal == "HOLD":
        # signal_reason may already start with "HOLD: " — strip to avoid double-label
        clean = reason[6:] if reason.startswith("HOLD: ") else reason
        return f"HOLD: {clean}"
    return f"{signal}: {reason}"


# ═════════════════════════════════════════════════════════════════════════════
# Endpoints
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/")
def root():
    return {"message": "Trading Bot API V3 is running"}


@app.get("/health")
def health():
    cooldown_symbols = [
        s for s, exp in _symbol_error_cooldown.items()
        if datetime.now(timezone.utc) < exp
    ]
    return {
        "ok":                    True,
        "version":               "v3",
        "dry_run":               DRY_RUN,
        "bot_mode":              "dry_run" if DRY_RUN else "live",
        "disable_new_entries":   DISABLE_NEW_ENTRIES,
        "observe_only_mode":     _observe_only_mode,
        "api_failure_count":     _api_failure_count,
        "stale_data_guard":      f"enabled (max {STALE_DATA_MAX_HOURS}h)",
        "single_instance_lock":  "managed by run_bot.py (PID file)",
        "last_market_check_utc": _last_market_check.isoformat() if _last_market_check else None,
        "session_start_utc":     _session_start.isoformat(),
        "symbols_in_cooldown":   cooldown_symbols,
        "symbol_error_counts":   {k: v for k, v in _symbol_error_counts.items() if v > 0},
        "open_paper_positions":  journal.get_open_paper_positions(),
    }


@app.get("/config-summary")
def config_summary():
    """Returns non-sensitive strategy config so you can confirm .env loaded correctly on startup."""
    return {
        "dry_run":                      DRY_RUN,
        "watchlist":                    WATCHLIST,
        "max_open_positions":           MAX_OPEN_POSITIONS,
        "trade_cooldown_minutes":       TRADE_COOLDOWN_MINUTES,
        "daily_loss_limit_pct":         DAILY_LOSS_LIMIT_PCT,
        "max_allocation_pct":           MAX_ALLOCATION_PCT,
        "risk_per_trade_pct":           RISK_PER_TRADE_PCT,
        "stop_loss_pct":                STOP_LOSS_PCT,
        "take_profit_pct":              TAKE_PROFIT_PCT,
        "trailing_stop_pct":            TRAILING_STOP_PCT,
        "rsi_period":                   RSI_PERIOD,
        "rsi_overbought":               RSI_OVERBOUGHT,
        "min_trend_strength":           MIN_TREND_STRENGTH,
        "breakout_lookback":            BREAKOUT_LOOKBACK,
        "intraday_timeframe":           INTRADAY_TIMEFRAME,
        "allow_early_trend_entry":      ALLOW_EARLY_TREND_ENTRY,
        "early_trend_max_sma_gap_pct":  EARLY_TREND_MAX_SMA_GAP_PCT,
        "sma20_rising_bars":            SMA20_RISING_BARS,
        "min_volume_ratio":             MIN_VOLUME_RATIO,
        "require_breakout_for_buy":            REQUIRE_BREAKOUT_FOR_BUY,
        "require_intraday_confirmation":       REQUIRE_INTRADAY_CONFIRMATION,
        "require_spy_bullish":                 REQUIRE_SPY_BULLISH,
        "intraday_sma_tolerance_pct":          INTRADAY_SMA_TOLERANCE_PCT,
        "allow_strong_daily_weak_intraday":    ALLOW_STRONG_DAILY_WEAK_INTRADAY,
        "early_trend_require_macd_improving":  EARLY_TREND_REQUIRE_MACD_IMPROVING,
    }


@app.get("/account")
def get_account():
    url = f"{BASE_URL}/v2/account"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@app.get("/stock/{symbol}")
def get_stock(symbol: str):
    url = f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@app.get("/bars/{symbol}")
def get_bars(symbol: str):
    url   = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=120)
    params = {
        "timeframe": "1Day",
        "start":     start.isoformat(),
        "end":       end.isoformat(),
        "limit":     100,
        "feed":      "iex",
        "sort":      "asc",
    }
    try:
        response = requests.get(url, headers=_headers(), params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@app.get("/sma/{symbol}")
def get_sma(symbol: str):
    url   = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=180)
    params = {
        "timeframe": "1Day",
        "start":     start.isoformat(),
        "end":       end.isoformat(),
        "limit":     200,
        "feed":      "iex",
        "sort":      "asc",
    }
    try:
        response = requests.get(url, headers=_headers(), params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}

    bars = data.get("bars", [])
    if not bars or not isinstance(bars, list):
        return {"error": "No bar data returned"}

    try:
        df = pd.DataFrame(bars)
        df["sma_20"] = df["c"].rolling(window=20).mean()
        df["sma_50"] = df["c"].rolling(window=50).mean()
        latest = df.iloc[-1]

        if pd.isna(latest["sma_20"]) or pd.isna(latest["sma_50"]):
            return {"error": "Not enough bars to calculate SMA values (need at least 50)"}

        return {
            "symbol":       symbol,
            "latest_close": round(float(latest["c"]),      2),
            "sma_20":       round(float(latest["sma_20"]), 2),
            "sma_50":       round(float(latest["sma_50"]), 2),
        }
    except Exception as e:
        return {"error": f"Failed to calculate SMA: {str(e)}"}


@app.get("/signal/{symbol}")
def get_signal(symbol: str):
    """
    V3 signal endpoint. Full filter chain:
      1. SMA crossover (daily)
      2. Trend strength
      3. Volume confirmation
      4. RSI overbought filter
      5. MACD bullish filter
      6. Breakout confirmation
      7. SPY market-direction filter
      8. Intraday multi-timeframe confirmation
    """
    data = _compute_raw_signal(symbol)
    if "error" in data:
        return data

    spy_bullish         = True
    spy_reason          = "N/A (symbol is SPY)"
    intraday_confirmed  = True
    intraday_reason     = "N/A (symbol is SPY)"
    intraday_margin_pct = 0.0

    if symbol.upper() != "SPY" and data["signal"] == "BUY":
        # SPY market-direction filter — only blocks when REQUIRE_SPY_BULLISH=true.
        # When false, SPY is still evaluated and reported but won't veto a BUY.
        spy_bullish, spy_reason = _is_spy_bullish()
        if REQUIRE_SPY_BULLISH and not spy_bullish:
            data["signal"]        = "HOLD"
            data["signal_reason"] = spy_reason

        # Intraday MTF confirmation — only blocks when REQUIRE_INTRADAY_CONFIRMATION=true.
        # Passes entry_tier so tolerance override can apply on strong daily setups.
        if data["signal"] == "BUY":
            intraday_confirmed, intraday_reason, intraday_margin_pct = _get_intraday_confirmation(
                symbol, entry_tier=data.get("entry_tier")
            )
            if REQUIRE_INTRADAY_CONFIRMATION and not intraday_confirmed:
                data["signal"]        = "HOLD"
                data["signal_reason"] = intraday_reason

    elif symbol.upper() != "SPY":
        spy_reason      = "Not checked (signal is not BUY)"
        intraday_reason = "Not checked (signal is not BUY)"

    result = {
        "symbol":                data["symbol"],
        "close":                 data["close"],
        "sma_20":                data["sma_20"],
        "sma_50":                data["sma_50"],
        "trend_strength":        data["trend_strength"],
        "trend_strong":          data["trend_strong"],
        "entry_tier":            data.get("entry_tier"),   # "strong", "early", or None
        "current_volume":        data["current_volume"],
        "vol_sma_20":            data["vol_sma_20"],
        "volume_confirmed":      data["volume_confirmed"],
        "rsi":                   data["rsi"],
        "rsi_ok":                data["rsi_ok"],
        "macd_line":             data["macd_line"],
        "macd_signal_line":      data["macd_signal_line"],
        "macd_bullish":          data["macd_bullish"],
        "macd_histogram":        data.get("macd_histogram"),
        "macd_histogram_rising": data.get("macd_histogram_rising"),
        "breakout_high":         data["breakout_high"],
        "breakout_confirmed":    data["breakout_confirmed"],
        "spy_bullish":           spy_bullish,
        "spy_reason":            spy_reason,
        "intraday_confirmed":    intraday_confirmed,
        "intraday_reason":       intraday_reason,
        "intraday_margin_pct":   intraday_margin_pct,
        "signal":                data["signal"],
        "signal_reason":         data["signal_reason"],
    }
    result["decision_summary"] = _build_decision_summary(result)
    return result


def is_market_open() -> bool:
    global _last_market_check
    url = f"{BASE_URL}/v2/clock"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        response.raise_for_status()
        data = response.json()
        _last_market_check = datetime.now(timezone.utc)
        return bool(data.get("is_open", False))
    except requests.exceptions.RequestException as e:
        print(f"[market_clock] request error: {e}")
        return False
    except Exception as e:
        print(f"[market_clock] unexpected error: {e}")
        return False


@app.get("/debug-clock")
def debug_clock():
    url = f"{BASE_URL}/v2/clock"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        try:
            payload = response.json()
        except Exception:
            payload = {"raw_text": response.text}
        return {
            "status_code": response.status_code, 
            "payload": payload, 
            "base_url": BASE_URL
        }
    except Exception as e:
        return {
            "error": str(e), 
            "base_url": BASE_URL,
        }


def calculate_position_size(price: float, equity: float = 0) -> int:
    """
    V3 position sizing — dual cap:
      1. Risk-based: risk RISK_PER_TRADE_PCT of equity over STOP_LOSS_PCT stop.
      2. Allocation cap: never exceed MAX_ALLOCATION_PCT of equity per trade.
    Final qty = min(risk_based, allocation_based), floor at 1.
    """
    if equity <= 0:
        try:
            response = requests.get(f"{BASE_URL}/v2/account", headers=_headers(), timeout=10)
            equity = float(response.json().get("equity", 0))
        except Exception:
            equity = 0

    if equity <= 0 or price <= 0:
        return 1

    risk_dollars      = equity * RISK_PER_TRADE_PCT
    risk_per_share    = price  * STOP_LOSS_PCT
    risk_based_qty    = int(risk_dollars / risk_per_share) if risk_per_share > 0 else 1

    max_dollars       = equity * MAX_ALLOCATION_PCT
    allocation_qty    = int(max_dollars / price)

    return max(min(risk_based_qty, allocation_qty), 1)


def _log_trade(symbol: str, result: dict, signal_data: dict = None):
    _sd = signal_data or {}
    entry = {
        "timestamp":             datetime.now(timezone.utc).isoformat(),
        "symbol":                symbol,
        "signal":                result.get("signal"),
        # Prefer execution-level outcome (e.g. "SKIP: cooldown") over signal-level summary
        "decision_summary":      result.get("decision_summary") or _sd.get("decision_summary"),
        "signal_reason":         result.get("signal_reason") or _sd.get("signal_reason"),
        "entry_tier":            _sd.get("entry_tier"),
        "starting_qty":          result.get("starting_qty"),
        "actions":               result.get("actions", []),
        "message":               result.get("message"),
        "dry_run":               DRY_RUN,
        "stop_loss_price":       result.get("stop_loss_price"),
        "take_profit_price":     result.get("take_profit_price"),
        "blocked_by":            result.get("blocked_by"),
        "entry_price":           result.get("entry_price"),
        "trend_strength":        _sd.get("trend_strength"),
        "volume_confirmed":      _sd.get("volume_confirmed"),
        "current_volume":        _sd.get("current_volume"),
        "vol_sma_20":            _sd.get("vol_sma_20"),
        "rsi":                   _sd.get("rsi"),
        "macd_line":             _sd.get("macd_line"),
        "macd_signal_line":      _sd.get("macd_signal_line"),
        "macd_bullish":          _sd.get("macd_bullish"),
        "macd_histogram":        _sd.get("macd_histogram"),
        "macd_histogram_rising": _sd.get("macd_histogram_rising"),
        "breakout_confirmed":    _sd.get("breakout_confirmed"),
        "intraday_confirmed":    _sd.get("intraday_confirmed"),
        "intraday_reason":       _sd.get("intraday_reason"),
        "intraday_margin_pct":   _sd.get("intraday_margin_pct"),
        "spy_bullish":           _sd.get("spy_bullish"),
        "spy_reason":            _sd.get("spy_reason"),
    }
    trade_log.append(entry)
    journal.log_event(entry)


def get_open_orders(symbol: str):
    url    = f"{BASE_URL}/v2/orders"
    params = {"status": "open", "symbols": symbol}
    try:
        response = requests.get(url, headers=_headers(), params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
        return []
    except Exception:
        return []


def cancel_order(order_id: str):
    url = f"{BASE_URL}/v2/orders/{order_id}"
    try:
        response = requests.delete(url, headers=_headers(), timeout=10)
        if response.status_code == 204:
            return {"status": "cancelled"}
        return {"status_code": response.status_code, "response": response.text}
    except Exception as e:
        return {"error": str(e)}


def _submit_order(order: dict) -> dict:
    """Submit an order, or simulate it when DRY_RUN is enabled."""
    if DRY_RUN:
        return {"dry_run": True, "would_submit": order}
    try:
        resp = requests.post(f"{BASE_URL}/v2/orders", json=order, headers=_headers(), timeout=10)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def _cancel_order_call(order_id: str) -> dict:
    if DRY_RUN:
        return {"dry_run": True, "would_cancel": order_id}
    return cancel_order(order_id)


def _cancel_all_open_orders_for_symbol(symbol: str, actions: list):
    """Cancel every open order for symbol and append each result to actions."""
    open_orders = get_open_orders(symbol)
    for order in open_orders:
        order_id     = order.get("id")
        cancel_result = _cancel_order_call(order_id) if order_id else {"error": "Missing order id"}
        actions.append({
            "step":     "cancel_order",
            "order_id": order_id,
            "response": cancel_result,
        })


def ensure_protection_for_position(symbol: str, qty: int, entry_price: float, actions: list):
    """Ensure an open long position has stop/take-profit protection.

    Conservative: if ANY sell-side open order already exists for the symbol,
    skip entirely. This prevents "insufficient qty available for order" errors
    caused by multiple sell-side orders competing for the same shares.

    Always appends a diagnostic record to actions so callers can see what happened.
    """
    if qty <= 0 or entry_price <= 0:
        actions.append({
            "step":   "protection_check",
            "status": "skipped_invalid_params",
            "qty":    qty,
            "entry_price": entry_price,
        })
        return

    try:
        open_orders = get_open_orders(symbol)
    except Exception as e:
        actions.append({"step": "protection_check", "status": "skipped_api_error", "error": str(e)})
        return

    sell_orders = [o for o in open_orders if o.get("side") == "sell"]
    if sell_orders:
        # At least one sell-side order is already active — do not add more
        # to avoid held_for_orders / insufficient qty conflicts.
        actions.append({
            "step":              "protection_check",
            "status":            "already_protected",
            "sell_orders_found": len(sell_orders),
            "order_ids":         [o.get("id") for o in sell_orders],
        })
        return

    # No sell-side protection exists at all — place a stop-loss only.
    # (A single sell order is safe; two simultaneous sell orders conflict.)
    stop_price = round(entry_price * (1 - STOP_LOSS_PCT), 2)
    stop_order = {
        "symbol":        symbol,
        "qty":           qty,
        "side":          "sell",
        "type":          "stop",
        "stop_price":    stop_price,
        "time_in_force": "gtc",
    }
    try:
        result = _submit_order(stop_order)
    except Exception as e:
        result = {"error": str(e)}
    actions.append({"step": "ensure_stop_loss", "status": "placed", "price": stop_price, "response": result})


@app.get("/position/{symbol}")
def get_position(symbol: str):
    url = f"{BASE_URL}/v2/positions/{symbol}"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}

    if response.status_code == 200:
        try:
            return response.json()
        except Exception:
            return {"error": "Invalid JSON in position response"}

    return {"error": True, "status_code": response.status_code, "response": response.text}


@app.post("/trade/{symbol}")
def execute_trade(symbol: str, block_new_entry: bool = False):
    # ── 1. Compute V3 signal ─────────────────────────────────────────────────
    signal_data = get_signal(symbol)
    signal      = signal_data.get("signal")

    if "error" in signal_data:
        _err_msg = signal_data["error"]
        print(f"[execute_trade] {symbol} | ERROR: {_err_msg} | dry_run={DRY_RUN}")
        result = {
            "signal":           "ERROR",
            "signal_reason":    _err_msg,
            "decision_summary": f"ERROR: {_err_msg}",
            "starting_qty":     0,
            "actions":          [],
            "message":          _err_msg,
        }
        _log_trade(symbol, result)
        return result

    # ── 2. Get current position ──────────────────────────────────────────────
    position_data   = get_position(symbol)
    starting_qty    = 0
    avg_entry_price = 0.0
    if position_data and "qty" in position_data:
        starting_qty    = int(float(position_data["qty"]))
        avg_entry_price = float(position_data.get("avg_entry_price", 0) or 0)

    _summary = signal_data.get("decision_summary") or signal_data.get("signal_reason", "")
    print(f"[execute_trade] {symbol} | qty={starting_qty} | {_summary} | dry_run={DRY_RUN}")

    actions = []

    # ── Ensure protection for any existing long position ─────────────────────
    # Runs unconditionally — even when the market is closed — because GTC
    # stop/take-profit orders can be placed outside market hours on Alpaca.
    if starting_qty > 0 and avg_entry_price > 0:
        ensure_protection_for_position(symbol, starting_qty, avg_entry_price, actions)

    # ── 3. Market open check ─────────────────────────────────────────────────
    if not is_market_open():
        result = {
            "signal":           signal,
            "signal_reason":    signal_data.get("signal_reason"),
            "decision_summary": "SKIP: market closed",
            "starting_qty":     starting_qty,
            "actions":          actions,
            "message":          "Market is closed",
        }
        _log_trade(symbol, result, signal_data)
        return result

    # ── HOLD ─────────────────────────────────────────────────────────────────
    if signal == "HOLD":
        if starting_qty < 0:
            close_order = {
                "symbol": symbol, "qty": abs(starting_qty),
                "side": "buy", "type": "market", "time_in_force": "day",
            }
            close_result = _submit_order(close_order)
            actions.append({"step": "close_short_on_hold", "response": close_result})
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SELL: closed legacy short {abs(starting_qty)} shares on HOLD",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "message":          "Closed leftover short position during HOLD",
                "dry_run":          DRY_RUN,
            }
            _log_trade(symbol, result, signal_data)
            return result

        result = {
            "signal":           signal,
            "signal_reason":    signal_data.get("signal_reason"),
            "decision_summary": signal_data.get("decision_summary"),
            "starting_qty":     starting_qty,
            "actions":          actions,
            "message":          signal_data.get("signal_reason", "Holding"),
        }
        _log_trade(symbol, result, signal_data)
        return result

    # ── BUY ──────────────────────────────────────────────────────────────────
    if signal == "BUY":
        # Already long (real position OR open paper trade in dry-run)
        if starting_qty > 0 or (DRY_RUN and journal.has_open_paper_trade(symbol)):
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": "SKIP: already in long position",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "message":          "Already in long position",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Kill switch — allow position monitoring but no new entries
        if DISABLE_NEW_ENTRIES:
            print(f"[safety] {symbol} | SKIP: DISABLE_NEW_ENTRIES=true (kill switch active)")
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": "SKIP: kill switch active (DISABLE_NEW_ENTRIES=true)",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "kill_switch",
                "message":          "New entries disabled by kill switch",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Observe-only mode after repeated API failures
        if _observe_only_mode:
            print(f"[safety] {symbol} | SKIP: observe-only mode (API failures={_api_failure_count})")
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SKIP: observe-only mode ({_api_failure_count} API failures)",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "observe_only_mode",
                "message":          f"Observe-only mode active after {_api_failure_count} API failures",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Symbol error cooldown
        if _is_symbol_error_cooldown(symbol):
            print(f"[safety] {symbol} | SKIP: error cooldown active")
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": "SKIP: symbol error cooldown active",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "symbol_error_cooldown",
                "message":          f"{symbol} in error cooldown — too many consecutive data failures",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Cooldown (entries only — exits are NEVER blocked)
        if _is_in_cooldown(symbol):
            elapsed_min = round(
                (datetime.now(timezone.utc) - _last_trade_time[symbol]).total_seconds() / 60, 1
            )
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SKIP: cooldown active ({elapsed_min}m elapsed / {TRADE_COOLDOWN_MINUTES}m required)",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "cooldown",
                "message": (
                    f"Cooldown active for {symbol} "
                    f"({elapsed_min}m elapsed / {TRADE_COOLDOWN_MINUTES}m required)"
                ),
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Max open positions
        open_long_count = _count_open_long_positions()
        if open_long_count >= MAX_OPEN_POSITIONS:
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SKIP: max open positions ({open_long_count}/{MAX_OPEN_POSITIONS})",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "max_open_positions",
                "message":          f"Max open positions reached ({open_long_count}/{MAX_OPEN_POSITIONS})",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Daily loss limit
        loss_reached, loss_reason = _is_daily_loss_limit_reached()
        if loss_reached:
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": "SKIP: daily loss guard active",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "daily_loss_limit",
                "message":          loss_reason,
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Max one new entry per cycle
        if block_new_entry:
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": "SKIP: max entries per cycle",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "max_entries_per_cycle",
                "message":          "Entry skipped — max one new entry per cycle reached",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Stale data guard — skip new entries when bar data is too old
        _daily_df_check = _fetch_bars(symbol, timeframe="1Day", days=5, limit=5)
        stale, stale_reason = _is_data_stale(_daily_df_check)
        if stale:
            print(f"[safety] {symbol} | SKIP: {stale_reason}")
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SKIP: stale data — {stale_reason}",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "stale_data",
                "message":          stale_reason,
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Cancel any stale open orders before entering a new position
        _cancel_all_open_orders_for_symbol(symbol, actions)

        # Close any leftover short before going long
        if starting_qty < 0:
            close_order = {
                "symbol": symbol, "qty": abs(starting_qty),
                "side": "buy", "type": "market", "time_in_force": "day",
            }
            close_result = _submit_order(close_order)
            actions.append({"step": "close_short", "response": close_result})

        # Position sizing
        try:
            acct_resp = requests.get(f"{BASE_URL}/v2/account", headers=_headers(), timeout=10)
            equity    = float(acct_resp.json().get("equity", 0))
        except Exception:
            equity = 0

        trade_qty        = calculate_position_size(signal_data["close"], equity)
        entry_price      = signal_data["close"]
        stop_loss_price  = round(entry_price * (1 - STOP_LOSS_PCT),   2)
        take_profit_price = round(entry_price * (1 + TAKE_PROFIT_PCT), 2)

        # Submit a single bracket order (market buy + stop-loss + take-profit legs).
        # This avoids the "insufficient qty available" / held_for_orders conflict
        # that occurs when stop and take-profit are submitted as separate orders.
        if TRAILING_STOP_PCT > 0:
            bracket_order = {
                "symbol":        symbol,
                "qty":           trade_qty,
                "side":          "buy",
                "type":          "market",
                "time_in_force": "day",
                "order_class":   "bracket",
                "stop_loss": {
                    "trail_percent": str(round(TRAILING_STOP_PCT * 100, 2)),
                },
                "take_profit": {
                    "limit_price": take_profit_price,
                },
            }
            open_result = _submit_order(bracket_order)
            actions.append({
                "step":          "open_long_bracket",
                "qty":           trade_qty,
                "trail_percent": round(TRAILING_STOP_PCT * 100, 2),
                "take_profit":   take_profit_price,
                "response":      open_result,
            })
        else:
            bracket_order = {
                "symbol":        symbol,
                "qty":           trade_qty,
                "side":          "buy",
                "type":          "market",
                "time_in_force": "day",
                "order_class":   "bracket",
                "stop_loss": {
                    "stop_price": stop_loss_price,
                },
                "take_profit": {
                    "limit_price": take_profit_price,
                },
            }
            open_result = _submit_order(bracket_order)
            actions.append({
                "step":        "open_long_bracket",
                "qty":         trade_qty,
                "stop_price":  stop_loss_price,
                "take_profit": take_profit_price,
                "response":    open_result,
            })

        _record_trade_time(symbol)
        entry_tier_label = signal_data.get("entry_tier", "unknown")
        print(
            f"[execute_trade] {symbol} | {'DRY RUN — ' if DRY_RUN else ''}"
            f"ENTERED long [{entry_tier_label}-trend] qty={trade_qty} "
            f"stop={stop_loss_price} tp={take_profit_price} | "
            f"{signal_data.get('decision_summary', '')}"
        )

        # Record paper trade lifecycle entry (both dry-run and live for analytics)
        journal.open_paper_trade(symbol, {
            "entry_timestamp":      datetime.now(timezone.utc).isoformat(),
            "entry_price":          entry_price,
            "stop_price":           stop_loss_price,
            "take_profit_price":    take_profit_price,
            "trailing_stop_pct":    TRAILING_STOP_PCT if TRAILING_STOP_PCT > 0 else None,
            "qty":                  trade_qty,
            "slippage_pct":         SLIPPAGE_PCT,
            "entry_tier":           entry_tier_label,
            "rsi":                  signal_data.get("rsi"),
            "macd_line":            signal_data.get("macd_line"),
            "macd_signal_line":     signal_data.get("macd_signal_line"),
            "macd_histogram":       signal_data.get("macd_histogram"),
            "macd_histogram_rising": signal_data.get("macd_histogram_rising"),
            "trend_strength":       signal_data.get("trend_strength"),
            "volume_confirmed":     signal_data.get("volume_confirmed"),
            "breakout_confirmed":   signal_data.get("breakout_confirmed"),
            "intraday_confirmed":   signal_data.get("intraday_confirmed"),
        })

        result = {
            "signal":            signal,
            "signal_reason":     signal_data.get("signal_reason"),
            "decision_summary":  signal_data.get("decision_summary"),
            "entry_tier":        entry_tier_label,
            "entry_price":       entry_price,
            "starting_qty":      starting_qty,
            "actions":           actions,
            "message":           "DRY RUN — would open long position" if DRY_RUN else "Opened long position",
            "stop_loss_price":   stop_loss_price,
            "take_profit_price": take_profit_price,
            "new_entry_opened":  True,
            "dry_run":           DRY_RUN,
        }
        _log_trade(symbol, result, signal_data)
        return result

    # ── SELL ─────────────────────────────────────────────────────────────────
    # Cooldown NEVER blocks exits. SELL that closes a long always executes.
    if signal == "SELL":
        if starting_qty > 0:
            # Cancel stop-loss and take-profit GTC orders BEFORE the market sell
            # to prevent duplicate/oversell fills from lingering protection orders.
            _cancel_all_open_orders_for_symbol(symbol, actions)

            close_order = {
                "symbol": symbol, "qty": abs(starting_qty),
                "side": "sell", "type": "market", "time_in_force": "day",
            }
            close_result = _submit_order(close_order)
            actions.append({"step": "close_long", "response": close_result})
            _record_trade_time(symbol)
            print(
                f"[execute_trade] {symbol} | {'DRY RUN — ' if DRY_RUN else ''}"
                f"closed long qty={starting_qty}"
            )
            journal.close_paper_trade(symbol, signal_data.get("close", 0.0), "signal_exit")
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SELL: closed long {starting_qty} shares",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "message":          "DRY RUN — would close long position" if DRY_RUN else "Closed long position",
                "dry_run":          DRY_RUN,
            }
            _log_trade(symbol, result, signal_data)
            return result

        if starting_qty < 0:
            close_order = {
                "symbol": symbol, "qty": abs(starting_qty),
                "side": "buy", "type": "market", "time_in_force": "day",
            }
            close_result = _submit_order(close_order)
            actions.append({"step": "close_legacy_short", "response": close_result})
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SELL: closed legacy short {abs(starting_qty)} shares",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "message":          "Closed leftover short position",
                "dry_run":          DRY_RUN,
            }
            _log_trade(symbol, result, signal_data)
            return result

        result = {
            "signal":           signal,
            "signal_reason":    signal_data.get("signal_reason"),
            "decision_summary": "SKIP: no position to close",
            "starting_qty":     starting_qty,
            "actions":          actions,
            "message":          "No position to close",
        }
        _log_trade(symbol, result, signal_data)
        return result

    result = {
        "signal":           signal,
        "signal_reason":    signal_data.get("signal_reason"),
        "decision_summary": f"ERROR: unexpected signal state ({signal})",
        "starting_qty":     starting_qty,
        "actions":          actions,
        "message":          "Unexpected signal state",
    }
    _log_trade(symbol, result, signal_data)
    return result


@app.get("/trade-log")
def get_trade_log():
    return trade_log


@app.get("/session-summary")
def session_summary():
    """Lightweight paper-trading review: counts and recent decisions from the in-memory trade log."""
    from collections import Counter

    if not trade_log:
        return {"message": "No trades logged this session", "total": 0}

    total          = len(trade_log)
    signal_counts  = dict(Counter(e.get("signal") for e in trade_log))
    blocked_counts = dict(Counter(
        e.get("blocked_by") for e in trade_log if e.get("blocked_by")
    ))
    tier_counts    = dict(Counter(
        e.get("entry_tier") for e in trade_log if e.get("entry_tier")
    ))

    # Recent 15 decisions, newest first
    recent = [
        {
            "timestamp":        e.get("timestamp"),
            "symbol":           e.get("symbol"),
            "signal":           e.get("signal"),
            "decision_summary": e.get("decision_summary") or e.get("signal_reason"),
            "entry_tier":       e.get("entry_tier"),
            "blocked_by":       e.get("blocked_by"),
            "rsi":              e.get("rsi"),
            "macd_histogram":   e.get("macd_histogram"),
        }
        for e in trade_log[-15:]
    ]
    recent.reverse()

    return {
        "total_logged":     total,
        "signal_counts":    signal_counts,
        "blocked_counts":   blocked_counts,
        "entry_tier_counts": tier_counts,
        "recent_decisions": recent,
    }


@app.get("/performance-summary")
def performance_summary():
    """Aggregate PnL, win rate, expectancy, drawdown across all closed paper trades."""
    return journal.query_performance_summary()


@app.get("/symbol-performance")
def symbol_performance():
    """Per-symbol breakdown: setups, entries, win rate, PnL, avg R, blocker counts."""
    return journal.query_symbol_performance()


@app.get("/recent-trades")
def recent_trades(limit: int = 20):
    """Most recent paper trade lifecycles (open and closed), newest first."""
    return journal.query_recent_trades(limit=limit)


@app.post("/test-buy/{symbol}")
def test_buy(symbol: str):
    if not is_market_open():
        return {"message": "Market is closed"}
    url   = f"{BASE_URL}/v2/orders"
    order = {"symbol": symbol, "qty": 1, "side": "buy", "type": "market", "time_in_force": "day"}
    try:
        response = requests.post(url, json=order, headers=_headers(), timeout=10)
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


# ── Backtest helpers ──────────────────────────────────────────────────────────
def _enrich_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Add all V3 indicator columns to a daily-bar DataFrame for backtesting."""
    df = df.copy()
    df["sma_20"] = df["c"].rolling(window=20).mean()
    df["sma_50"] = df["c"].rolling(window=50).mean()

    if "v" in df.columns:
        df["vol_sma_20"] = df["v"].rolling(window=20).mean()

    df["rsi"]         = _rsi_series(df["c"], RSI_PERIOD)
    ml, sl            = _macd_series(df["c"])
    df["macd_line"]   = ml
    df["macd_signal"] = sl
    # MACD histogram direction — mirrors live EARLY_TREND_REQUIRE_MACD_IMPROVING logic
    df["macd_histogram"]        = df["macd_line"] - df["macd_signal"]
    df["macd_histogram_rising"] = df["macd_histogram"] > df["macd_histogram"].shift(1)
    # Breakout high: max close of the prior BREAKOUT_LOOKBACK bars (not including current bar)
    df["breakout_high"] = df["c"].shift(1).rolling(window=BREAKOUT_LOOKBACK).max()

    # Early trend indicator: True when SMA20 has risen for each of the last SMA20_RISING_BARS bars.
    df["sma20_rising"] = (
        df["sma_20"]
        .rolling(window=SMA20_RISING_BARS + 1, min_periods=SMA20_RISING_BARS + 1)
        .apply(lambda x: int(all(x[i] < x[i + 1] for i in range(len(x) - 1))), raw=True)
        .fillna(0)
        .astype(bool)
    )

    return df


@app.get("/backtest/{symbol}")
def backtest(symbol: str):
    """
    V3 backtest: applies the full V3 entry filter chain and models
    stop-loss, take-profit, and signal-based exits.
    Returns win/loss stats, avg win/loss, profit factor, and max drawdown.
    """
    df = _fetch_bars(symbol, timeframe="1Day", days=365, limit=500)

    if df.empty:
        return {"error": "No data returned for symbol"}

    df = _enrich_dataframe(df)
    df = df.dropna(subset=["sma_20", "sma_50"]).reset_index(drop=True)

    if df.empty:
        return {"error": "Not enough data to calculate indicators"}

    # Pre-check which columns are available
    has_volume_col              = "vol_sma_20"          in df.columns and "v" in df.columns
    has_rsi_col                 = "rsi"                 in df.columns
    has_macd_col                = "macd_line"           in df.columns and "macd_signal" in df.columns
    has_breakout_col            = "breakout_high"       in df.columns
    has_sma20_rising_col        = "sma20_rising"        in df.columns
    has_macd_histogram_rising_col = "macd_histogram_rising" in df.columns

    in_trade         = False
    entry_date       = entry_price = stop_loss_price = take_profit_price = None
    trades           = []
    running_equity   = 1.0
    equity_curve     = [1.0]

    for _, row in df.iterrows():
        close  = float(row["c"])
        sma_20 = float(row["sma_20"])
        sma_50 = float(row["sma_50"])
        date   = row["t"]

        if in_trade:
            exit_reason = None
            if close <= stop_loss_price:
                exit_reason = "stop_loss"
            elif close >= take_profit_price:
                exit_reason = "take_profit"
            elif close < sma_20:
                exit_reason = "signal_exit"

            if exit_reason:
                pnl     = round(close - entry_price, 2)
                pnl_pct = round((close - entry_price) / entry_price * 100, 2)
                running_equity *= (1 + (close - entry_price) / entry_price)
                equity_curve.append(running_equity)
                trades.append({
                    "side":              "long",
                    "entry_date":        entry_date,
                    "entry_price":       round(entry_price,       2),
                    "exit_date":         date,
                    "exit_price":        round(close,             2),
                    "stop_loss_price":   round(stop_loss_price,   2),
                    "take_profit_price": round(take_profit_price, 2),
                    "pnl":               pnl,
                    "pnl_pct":           pnl_pct,
                    "exit_reason":       exit_reason,
                })
                in_trade   = False
                entry_date = entry_price = stop_loss_price = take_profit_price = None
            continue  # stay in trade or move to next bar after exit

        # V3 two-tier entry conditions (mirrors live signal logic)
        # Tier 1: Strong trend — SMA20 above SMA50
        strong_trend = close > sma_20 and sma_20 > sma_50

        # Tier 2: Early trend — price above SMA20, SMA20 near SMA50 and rising
        early_trend = False
        if ALLOW_EARLY_TREND_ENTRY and not strong_trend and close > sma_20:
            sma_gap = (sma_50 - sma_20) / sma_50 if sma_50 > 0 else 1.0
            rising  = bool(row["sma20_rising"]) if has_sma20_rising_col else False
            if sma_gap <= EARLY_TREND_MAX_SMA_GAP_PCT and rising:
                early_trend = True
                # Mirror live EARLY_TREND_REQUIRE_MACD_IMPROVING gate for backtest accuracy
                if EARLY_TREND_REQUIRE_MACD_IMPROVING and has_macd_histogram_rising_col:
                    hist_val = row.get("macd_histogram_rising")
                    if hist_val is not None and not pd.isna(hist_val):
                        if not bool(hist_val):
                            early_trend = False

        if not (strong_trend or early_trend):
            continue

        # Trend strength only enforced for strong-trend tier
        if strong_trend:
            trend_strength = abs(sma_20 - sma_50) / sma_50
            if trend_strength < MIN_TREND_STRENGTH:
                continue

        # Volume — relaxed to MIN_VOLUME_RATIO of 20-day average
        if has_volume_col and not pd.isna(row["vol_sma_20"]):
            if float(row["v"]) < float(row["vol_sma_20"]) * MIN_VOLUME_RATIO:
                continue

        if has_rsi_col and not pd.isna(row["rsi"]):
            if float(row["rsi"]) >= RSI_OVERBOUGHT:
                continue

        if has_macd_col and not pd.isna(row["macd_line"]) and not pd.isna(row["macd_signal"]):
            if float(row["macd_line"]) <= float(row["macd_signal"]):
                continue

        # Breakout only enforced when REQUIRE_BREAKOUT_FOR_BUY=true
        if REQUIRE_BREAKOUT_FOR_BUY and has_breakout_col and not pd.isna(row["breakout_high"]):
            if close <= float(row["breakout_high"]):
                continue

        in_trade         = True
        entry_date       = date
        entry_price      = close
        stop_loss_price  = round(entry_price * (1 - STOP_LOSS_PCT),   2)
        take_profit_price = round(entry_price * (1 + TAKE_PROFIT_PCT), 2)

    # Close any open trade at end of data
    if in_trade:
        last       = df.iloc[-1]
        exit_price = float(last["c"])
        pnl        = round(exit_price - entry_price, 2)
        pnl_pct    = round((exit_price - entry_price) / entry_price * 100, 2)
        running_equity *= (1 + (exit_price - entry_price) / entry_price)
        equity_curve.append(running_equity)
        trades.append({
            "side":              "long",
            "entry_date":        entry_date,
            "entry_price":       round(entry_price,       2),
            "exit_date":         last["t"],
            "exit_price":        round(exit_price,        2),
            "stop_loss_price":   round(stop_loss_price,   2),
            "take_profit_price": round(take_profit_price, 2),
            "pnl":               pnl,
            "pnl_pct":           pnl_pct,
            "exit_reason":       "final_bar_exit",
        })

    # ── Summary metrics ────────────────────────────────────────────────────────
    total_trades  = len(trades)
    wins          = [t for t in trades if t["pnl"] > 0]
    losses        = [t for t in trades if t["pnl"] <= 0]
    win_rate      = round(len(wins) / total_trades * 100, 1) if total_trades > 0 else 0.0

    avg_win       = round(sum(t["pnl"] for t in wins)   / len(wins),   2) if wins   else 0.0
    avg_loss      = round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0.0
    gross_profit  = sum(t["pnl"] for t in wins)                              if wins   else 0.0
    gross_loss    = abs(sum(t["pnl"] for t in losses))                        if losses else 0.0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None
    total_profit  = round(sum(t["pnl"] for t in trades), 2)
    total_return_pct = round(
        sum((t["exit_price"] - t["entry_price"]) / t["entry_price"] for t in trades) * 100, 2
    ) if trades else 0.0

    # Max drawdown from equity curve
    peak   = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    max_drawdown_pct = round(max_dd * 100, 2)

    return {
        "symbol":           symbol,
        "total_trades":     total_trades,
        "winning_trades":   len(wins),
        "losing_trades":    len(losses),
        "win_rate":         win_rate,
        "avg_win":          avg_win,
        "avg_loss":         avg_loss,
        "profit_factor":    profit_factor,
        "total_profit":     total_profit,
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "trades":           trades,
    }


@app.get("/positions-watchlist")
def positions_watchlist():
    results = []
    for symbol in WATCHLIST:
        try:
            data = get_position(symbol)
            print(f"[positions_watchlist] {symbol} | {data}")

            if not data or "qty" not in data:
                results.append({
                    "symbol": symbol, "qty": 0, "side": "flat",
                    "market_value": 0, "unrealized_pl": 0,
                })
                continue

            qty  = int(float(data["qty"]))
            side = "long" if qty > 0 else "short" if qty < 0 else "flat"
            results.append({
                "symbol":       symbol,
                "qty":          qty,
                "side":         side,
                "market_value": float(data.get("market_value",  0)),
                "unrealized_pl": float(data.get("unrealized_pl", 0)),
            })
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})

    return {"results": results}


@app.post("/trade-watchlist")
def trade_watchlist():
    results         = []
    new_entry_taken = False  # Allow at most one new long entry per cycle
    for symbol in WATCHLIST:
        try:
            result = execute_trade(symbol, block_new_entry=new_entry_taken)
            if result.get("new_entry_opened"):
                new_entry_taken = True
            results.append({
                "symbol":            symbol,
                "signal":            result.get("signal"),
                "decision_summary":  result.get("decision_summary") or result.get("signal_reason"),
                "signal_reason":     result.get("signal_reason"),
                "entry_tier":        result.get("entry_tier"),
                "starting_qty":      result.get("starting_qty"),
                "actions":           result.get("actions", []),
                "message":           result.get("message"),
                "dry_run":           result.get("dry_run", DRY_RUN),
                "blocked_by":        result.get("blocked_by"),
                "stop_loss_price":   result.get("stop_loss_price"),
                "take_profit_price": result.get("take_profit_price"),
            })
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})

    return {"results": results, "dry_run": DRY_RUN}


@app.get("/market-status")
def market_status():
    return {"is_open": is_market_open()}


@app.get("/scan-watchlist")
def scan_watchlist():
    """V3: Full filtered signals (RSI, MACD, breakout, MTF) for each watchlist symbol."""
    results = []
    for symbol in WATCHLIST:
        try:
            data = get_signal(symbol)
            if "error" in data:
                results.append({"symbol": symbol, "error": data["error"]})
                continue
            results.append({
                "symbol":                data["symbol"],
                "close":                 data["close"],
                "sma_20":                data["sma_20"],
                "sma_50":                data["sma_50"],
                "trend_strength":        data.get("trend_strength"),
                "trend_strong":          data.get("trend_strong"),
                "entry_tier":            data.get("entry_tier"),
                "current_volume":        data.get("current_volume"),
                "vol_sma_20":            data.get("vol_sma_20"),
                "volume_confirmed":      data.get("volume_confirmed"),
                "rsi":                   data.get("rsi"),
                "rsi_ok":                data.get("rsi_ok"),
                "macd_line":             data.get("macd_line"),
                "macd_signal_line":      data.get("macd_signal_line"),
                "macd_bullish":          data.get("macd_bullish"),
                "macd_histogram":        data.get("macd_histogram"),
                "macd_histogram_rising": data.get("macd_histogram_rising"),
                "breakout_high":         data.get("breakout_high"),
                "breakout_confirmed":    data.get("breakout_confirmed"),
                "spy_bullish":           data.get("spy_bullish"),
                "spy_reason":            data.get("spy_reason"),
                "intraday_confirmed":    data.get("intraday_confirmed"),
                "intraday_reason":       data.get("intraday_reason"),
                "intraday_margin_pct":   data.get("intraday_margin_pct"),
                "signal":                data["signal"],
                "signal_reason":         data.get("signal_reason"),
                "decision_summary":      data.get("decision_summary"),
            })
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})

    return {"results": results}


@app.get("/backtest-watchlist")
def backtest_watchlist():
    results = []
    for symbol in WATCHLIST:
        try:
            data = backtest(symbol)
            if "error" in data:
                results.append({"symbol": symbol, "error": data["error"]})
                continue
            results.append({
                "symbol":           data["symbol"],
                "total_trades":     data["total_trades"],
                "winning_trades":   data["winning_trades"],
                "losing_trades":    data["losing_trades"],
                "win_rate":         data["win_rate"],
                "avg_win":          data.get("avg_win"),
                "avg_loss":         data.get("avg_loss"),
                "profit_factor":    data.get("profit_factor"),
                "total_profit":     data["total_profit"],
                "total_return_pct": data["total_return_pct"],
                "max_drawdown_pct": data.get("max_drawdown_pct"),
            })
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})

    valid_results = [r for r in results if "error" not in r]

    if not valid_results:
        return {"error": "No backtest data available for any symbol in the watchlist", "results": results}

    best_by_profit   = max(valid_results, key=lambda r: r["total_profit"])["symbol"]
    best_by_win_rate = max(valid_results, key=lambda r: r["win_rate"])["symbol"]

    return {
        "results":               results,
        "best_symbol_by_profit":   best_by_profit,
        "best_symbol_by_win_rate": best_by_win_rate,
    }
