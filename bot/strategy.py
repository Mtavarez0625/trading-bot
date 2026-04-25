from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from indicators import add_indicators, MIN_BARS_REQUIRED, _is_valid_float

REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}
RSI_THRESHOLD = 55.0


@dataclass
class SignalResult:
    signal: bool
    enough_bars: bool
    trend_ok: bool      # EMA20 > EMA50
    rsi_ok: bool        # RSI14 > RSI_THRESHOLD
    volume_ok: bool     # current volume > rolling 20-bar avg volume
    ema_20: Optional[float]
    ema_50: Optional[float]
    rsi_14: Optional[float]
    volume: Optional[float]
    vol_avg_20: Optional[float]
    reason: str


def evaluate_signal(df: Optional[pd.DataFrame]) -> SignalResult:
    """
    Evaluate whether an entry signal is present on the latest bar.

    Args:
        df: DataFrame of bars sorted ascending by time.
            Must contain: open, high, low, close, volume.

    Returns:
        SignalResult with signal boolean and full diagnostics.
    """
    _no_signal = _make_no_signal

    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return _no_signal(enough_bars=False, reason="Empty or missing DataFrame")

    missing_cols = REQUIRED_COLUMNS - set(df.columns)
    if missing_cols:
        return _no_signal(
            enough_bars=False,
            reason=f"Missing required columns: {sorted(missing_cols)}",
        )

    if len(df) < MIN_BARS_REQUIRED:
        return _no_signal(
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

    if not all(_is_valid_float(v) for v in [ema_20, ema_50, rsi_14, volume, vol_avg_20]):
        return SignalResult(
            signal=False,
            enough_bars=True,
            trend_ok=False,
            rsi_ok=False,
            volume_ok=False,
            ema_20=_safe_float(ema_20),
            ema_50=_safe_float(ema_50),
            rsi_14=_safe_float(rsi_14),
            volume=_safe_float(volume),
            vol_avg_20=_safe_float(vol_avg_20),
            reason="NaN in one or more indicators — insufficient history",
        )

    ema_20 = float(ema_20)
    ema_50 = float(ema_50)
    rsi_14 = float(rsi_14)
    volume = float(volume)
    vol_avg_20 = float(vol_avg_20)

    trend_ok = ema_20 > ema_50
    rsi_ok = rsi_14 > RSI_THRESHOLD
    volume_ok = volume > vol_avg_20
    signal = trend_ok and rsi_ok and volume_ok

    failed = []
    if not trend_ok:
        failed.append(f"EMA20({ema_20:.2f}) not above EMA50({ema_50:.2f})")
    if not rsi_ok:
        failed.append(f"RSI({rsi_14:.2f}) not above {RSI_THRESHOLD}")
    if not volume_ok:
        failed.append(f"volume({volume:.0f}) not above avg({vol_avg_20:.0f})")

    reason = "All conditions met" if signal else "; ".join(failed)

    return SignalResult(
        signal=signal,
        enough_bars=True,
        trend_ok=trend_ok,
        rsi_ok=rsi_ok,
        volume_ok=volume_ok,
        ema_20=ema_20,
        ema_50=ema_50,
        rsi_14=rsi_14,
        volume=volume,
        vol_avg_20=vol_avg_20,
        reason=reason,
    )


def _make_no_signal(enough_bars: bool, reason: str) -> SignalResult:
    return SignalResult(
        signal=False,
        enough_bars=enough_bars,
        trend_ok=False,
        rsi_ok=False,
        volume_ok=False,
        ema_20=None,
        ema_50=None,
        rsi_14=None,
        volume=None,
        vol_avg_20=None,
        reason=reason,
    )


def _safe_float(value) -> Optional[float]:
    try:
        v = float(value)
        import math
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None
