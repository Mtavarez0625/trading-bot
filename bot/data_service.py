from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from logger import get_logger

log = get_logger(__name__)

REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}

# Warn if the latest bar is older than this during market hours.
STALE_WARN_MINUTES = 30

# How many calendar days back to request bars.
# For 5-minute bars we need ~60 bars; use 3 trading days with buffer.
LOOKBACK_DAYS = 5


def fetch_bars(
    data_client,
    symbol: str,
    timeframe,
    lookback_days: int = LOOKBACK_DAYS,
) -> Optional[pd.DataFrame]:
    """
    Fetch intraday bars for `symbol` from Alpaca.

    Returns a DataFrame sorted ascending by timestamp with lowercase column names,
    or None on any failure. Does not raise.
    """
    from alpaca.data.requests import StockBarsRequest

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)

    try:
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            feed="iex",
        )
        bars = data_client.get_stock_bars(request)
    except Exception as exc:
        log.error("[%s] API call failed: %s", symbol, exc)
        return None

    if bars is None:
        log.warning("[%s] Received None from get_stock_bars.", symbol)
        return None

    try:
        df = bars.df
    except Exception as exc:
        log.error("[%s] Could not access .df on bars response: %s", symbol, exc)
        return None

    if df is None or df.empty:
        log.warning("[%s] Bars DataFrame is empty.", symbol)
        return None

    # alpaca-py returns MultiIndex(symbol, timestamp) for single-symbol requests in some versions
    if isinstance(df.index, pd.MultiIndex):
        level_vals = df.index.get_level_values(0)
        if symbol not in level_vals:
            log.error("[%s] Symbol not found in MultiIndex levels: %s", symbol, list(level_vals.unique()))
            return None
        df = df.xs(symbol, level=0)

    # Normalize column names to lowercase
    df.columns = [c.lower() for c in df.columns]

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        log.error("[%s] Missing columns after normalization: %s", symbol, sorted(missing))
        return None

    # Ensure ascending time order
    df = df.sort_index()

    if df.empty:
        log.warning("[%s] DataFrame is empty after sort.", symbol)
        return None

    _warn_if_stale(df, symbol)

    log.info("[%s] Fetched %d bars (latest: %s).", symbol, len(df), df.index[-1])
    return df


def _warn_if_stale(df: pd.DataFrame, symbol: str) -> None:
    """Log a warning if the latest bar timestamp is suspiciously old."""
    try:
        latest_ts = df.index[-1]
        if hasattr(latest_ts, "tzinfo") and latest_ts.tzinfo is None:
            latest_ts = latest_ts.tz_localize("UTC")
        age_minutes = (datetime.now(timezone.utc) - latest_ts).total_seconds() / 60
        if age_minutes > STALE_WARN_MINUTES:
            log.warning(
                "[%s] Latest bar is %.0f min old (threshold %d min). Data may be stale.",
                symbol, age_minutes, STALE_WARN_MINUTES,
            )
    except Exception as exc:
        log.debug("[%s] Could not check bar freshness: %s", symbol, exc)
