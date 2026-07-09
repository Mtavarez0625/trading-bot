from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from indicators import add_indicators, MIN_BARS_REQUIRED, _is_valid_float

REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}

# Hybrid Ross Momentum Trend Strategy — entry thresholds
RSI_THRESHOLD = 58.0        # minimum RSI for entry
REL_VOL_MIN = 2.0           # minimum relative volume (2× average)
VWAP_EXTENSION_MAX = 0.06   # reject if price > 6% above VWAP
SPREAD_MAX_PCT = 0.04       # reject if bar range (high-low)/close > 4%


@dataclass
class SignalResult:
    signal: bool
    enough_bars: bool
    trend_ok: bool        # EMA20 > EMA50
    rsi_ok: bool          # RSI14 >= RSI_THRESHOLD (58)
    volume_ok: bool       # current volume > rolling 20-bar avg
    vwap_ok: bool         # close > VWAP
    rel_vol_ok: bool      # relative_volume >= REL_VOL_MIN (2.0)
    not_extended: bool    # close not more than VWAP_EXTENSION_MAX above VWAP
    spread_ok: bool       # (high - low) / close < SPREAD_MAX_PCT
    ema_20: Optional[float]
    ema_50: Optional[float]
    rsi_14: Optional[float]
    volume: Optional[float]
    vol_avg_20: Optional[float]
    vwap: Optional[float]
    relative_volume: Optional[float]
    reason: str


def evaluate_signal(df: Optional[pd.DataFrame]) -> SignalResult:
    """
    Evaluate whether a Hybrid Ross Momentum Trade entry signal is present
    on the latest bar.

    ALL of the following must be true for signal=True:
      1. close > VWAP
      2. EMA20 > EMA50
      3. RSI14 >= 58
      4. relative volume >= 2.0
      5. current volume > 20-bar rolling avg
      6. price not more than 6% above VWAP
      7. bar range (high-low)/close < 4%  (spread proxy)

    Args:
        df: DataFrame of bars sorted ascending by time.
            Must contain: open, high, low, close, volume.

    Returns:
        SignalResult with signal boolean and full diagnostics.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return _make_no_signal(enough_bars=False, reason="Empty or missing DataFrame")

    missing_cols = REQUIRED_COLUMNS - set(df.columns)
    if missing_cols:
        return _make_no_signal(
            enough_bars=False,
            reason=f"Missing required columns: {sorted(missing_cols)}",
        )

    if len(df) < MIN_BARS_REQUIRED:
        return _make_no_signal(
            enough_bars=False,
            reason=f"Insufficient bars: have {len(df)}, need {MIN_BARS_REQUIRED}",
        )

    df = add_indicators(df)
    row = df.iloc[-1]

    ema_20 = row.get("ema_20")
    ema_50 = row.get("ema_50")
    rsi_14 = row.get("rsi_14")
    volume = row.get("volume")
    vol_avg_20 = row.get("vol_avg_20")
    vwap = row.get("vwap")
    relative_volume = row.get("relative_volume")
    close = row.get("close")
    high = row.get("high")
    low = row.get("low")

    # Core indicators must be valid to proceed; VWAP and relative_volume are
    # also required for the new conditions.
    if not all(_is_valid_float(v) for v in [ema_20, ema_50, rsi_14, volume, vol_avg_20, vwap]):
        return SignalResult(
            signal=False,
            enough_bars=True,
            trend_ok=False,
            rsi_ok=False,
            volume_ok=False,
            vwap_ok=False,
            rel_vol_ok=False,
            not_extended=False,
            spread_ok=False,
            ema_20=_safe_float(ema_20),
            ema_50=_safe_float(ema_50),
            rsi_14=_safe_float(rsi_14),
            volume=_safe_float(volume),
            vol_avg_20=_safe_float(vol_avg_20),
            vwap=_safe_float(vwap),
            relative_volume=_safe_float(relative_volume),
            reason="NaN in one or more indicators — insufficient history",
        )

    ema_20 = float(ema_20)
    ema_50 = float(ema_50)
    rsi_14 = float(rsi_14)
    volume = float(volume)
    vol_avg_20 = float(vol_avg_20)
    vwap = float(vwap)
    close = float(close)
    high = float(high)
    low = float(low)

    # relative_volume may still be NaN when vol_avg_20 was zero; treat as 0.
    relative_volume = float(relative_volume) if _is_valid_float(relative_volume) else 0.0

    # --- Condition evaluation ---
    trend_ok = ema_20 > ema_50
    rsi_ok = rsi_14 >= RSI_THRESHOLD
    volume_ok = volume > vol_avg_20
    vwap_ok = close > vwap
    rel_vol_ok = relative_volume >= REL_VOL_MIN
    extension_pct = (close - vwap) / vwap if vwap > 0 else 0.0
    not_extended = extension_pct <= VWAP_EXTENSION_MAX
    spread_pct = (high - low) / close if close > 0 else 1.0
    spread_ok = spread_pct < SPREAD_MAX_PCT

    signal = (
        trend_ok and rsi_ok and volume_ok
        and vwap_ok and rel_vol_ok and not_extended and spread_ok
    )

    # Build a skip-reason string that mirrors the log messages in main.py
    failed = []
    if not vwap_ok:
        failed.append(f"skipped: price below VWAP({vwap:.2f})")
    if not trend_ok:
        failed.append(f"skipped: EMA20({ema_20:.2f}) not above EMA50({ema_50:.2f})")
    if not rsi_ok:
        failed.append(f"skipped: RSI below {RSI_THRESHOLD} (got {rsi_14:.1f})")
    if not rel_vol_ok:
        failed.append(
            f"skipped: relative volume below {REL_VOL_MIN} (got {relative_volume:.2f})"
        )
    if not volume_ok:
        failed.append(f"skipped: volume({volume:.0f}) not above avg({vol_avg_20:.0f})")
    if not not_extended:
        failed.append(
            f"skipped: price too extended above VWAP"
            f" ({extension_pct * 100:.1f}% > {VWAP_EXTENSION_MAX * 100:.0f}%)"
        )
    if not spread_ok:
        failed.append(
            f"skipped: spread too wide"
            f" ({spread_pct * 100:.1f}% > {SPREAD_MAX_PCT * 100:.0f}%)"
        )

    reason = "All conditions met" if signal else "; ".join(failed)

    return SignalResult(
        signal=signal,
        enough_bars=True,
        trend_ok=trend_ok,
        rsi_ok=rsi_ok,
        volume_ok=volume_ok,
        vwap_ok=vwap_ok,
        rel_vol_ok=rel_vol_ok,
        not_extended=not_extended,
        spread_ok=spread_ok,
        ema_20=ema_20,
        ema_50=ema_50,
        rsi_14=rsi_14,
        volume=volume,
        vol_avg_20=vol_avg_20,
        vwap=vwap,
        relative_volume=relative_volume,
        reason=reason,
    )


def _make_no_signal(enough_bars: bool, reason: str) -> SignalResult:
    return SignalResult(
        signal=False,
        enough_bars=enough_bars,
        trend_ok=False,
        rsi_ok=False,
        volume_ok=False,
        vwap_ok=False,
        rel_vol_ok=False,
        not_extended=False,
        spread_ok=False,
        ema_20=None,
        ema_50=None,
        rsi_14=None,
        volume=None,
        vol_avg_20=None,
        vwap=None,
        relative_volume=None,
        reason=reason,
    )


def _safe_float(value) -> Optional[float]:
    try:
        v = float(value)
        import math
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None
