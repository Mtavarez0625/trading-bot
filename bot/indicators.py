from __future__ import annotations

import math

import pandas as pd

# Minimum bars required to produce valid values for all indicators.
# EMA50 needs 50 bars; add a small buffer for RSI and rolling volume warmup.
MIN_BARS_REQUIRED = 60


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """
    Exponential moving average using Pandas ewm (span method).
    Returns a Series of NaN when the input is shorter than `period`.
    """
    if len(series) < period:
        return pd.Series([float("nan")] * len(series), index=series.index)
    return series.ewm(span=period, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    RSI using Wilder's smoothing (equivalent to EWM alpha=1/period).
    Returns all-NaN when fewer than period+1 bars are available.
    """
    if len(series) < period + 1:
        return pd.Series([float("nan")] * len(series), index=series.index)

    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    # Use the equivalent form 100 * avg_gain / (avg_gain + avg_loss) to avoid
    # division-by-zero when avg_loss=0 (pure uptrend → RSI=100) or when both
    # are 0 (flat market → NaN, which is correct: not enough signal).
    denom = avg_gain + avg_loss
    rsi = 100.0 * avg_gain / denom
    return rsi


def compute_rolling_volume(series: pd.Series, period: int = 20) -> pd.Series:
    """Rolling mean volume. Returns NaN until `period` bars are available."""
    return series.rolling(window=period, min_periods=period).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute and attach EMA20, EMA50, RSI14, and vol_avg_20 to a copy of df.
    Expects columns: close, volume.
    Does not mutate the original DataFrame.
    """
    df = df.copy()
    df["ema_20"] = compute_ema(df["close"], 20)
    df["ema_50"] = compute_ema(df["close"], 50)
    df["rsi_14"] = compute_rsi(df["close"], 14)
    df["vol_avg_20"] = compute_rolling_volume(df["volume"], 20)
    return df


def _is_valid_float(value) -> bool:
    """Returns True if value is a real, non-NaN number."""
    try:
        return value is not None and not math.isnan(float(value))
    except (TypeError, ValueError):
        return False
