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


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Intraday VWAP that resets each calendar day.

    typical_price = (high + low + close) / 3
    VWAP_t = cumsum(tp * volume) / cumsum(volume), grouped by trading date.

    For DataFrames without a DatetimeIndex the VWAP is computed as a single
    cumulative value over all rows — suitable for unit tests with integer-indexed
    synthetic data.
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    tpv = tp * df["volume"]

    if isinstance(df.index, pd.DatetimeIndex):
        date_key = df.index.normalize()
        cum_tpv = tpv.groupby(date_key).cumsum()
        cum_vol = df["volume"].groupby(date_key).cumsum()
    else:
        cum_tpv = tpv.cumsum()
        cum_vol = df["volume"].cumsum()

    # NaN where cumulative volume is zero (first bar of a zero-volume day)
    return cum_tpv / cum_vol.replace(0, float("nan"))


def compute_relative_volume(volume: pd.Series, period: int = 20) -> pd.Series:
    """
    Current bar volume divided by the rolling average volume.
    Returns NaN until `period` bars are available.
    A value of 2.0 means twice the average volume — the threshold for the
    Hybrid Ross Momentum strategy.
    """
    avg = compute_rolling_volume(volume, period)
    return volume / avg.replace(0, float("nan"))


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute and attach EMA20, EMA50, RSI14, vol_avg_20, vwap, and
    relative_volume to a copy of df.
    Expects columns: open, high, low, close, volume.
    Does not mutate the original DataFrame.
    """
    df = df.copy()
    df["ema_20"] = compute_ema(df["close"], 20)
    df["ema_50"] = compute_ema(df["close"], 50)
    df["rsi_14"] = compute_rsi(df["close"], 14)
    df["vol_avg_20"] = compute_rolling_volume(df["volume"], 20)
    df["vwap"] = compute_vwap(df)
    df["relative_volume"] = compute_relative_volume(df["volume"], 20)
    return df


def _is_valid_float(value) -> bool:
    """Returns True if value is a real, non-NaN number."""
    try:
        return value is not None and not math.isnan(float(value))
    except (TypeError, ValueError):
        return False
