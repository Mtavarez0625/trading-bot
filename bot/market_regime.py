from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from data_service import fetch_bars
from indicators import add_indicators, _is_valid_float
from logger import get_logger

log = get_logger(__name__)

REGIME_SYMBOL = "SPY"
RSI_FLOOR = 50.0


@dataclass
class RegimeResult:
    is_bullish: bool
    reason: str
    ema_20: Optional[float] = field(default=None)
    ema_50: Optional[float] = field(default=None)
    rsi_14: Optional[float] = field(default=None)


def check_market_regime(data_client, timeframe) -> RegimeResult:
    """
    Determine whether the broad market is in a bullish state.

    Rule: SPY EMA20 > SPY EMA50  AND  SPY RSI14 > 50.

    Fail-safe: if SPY data cannot be fetched or indicators are NaN, returns
    is_bullish=False so the bot never enters on an unknown regime.
    """
    df = fetch_bars(data_client, REGIME_SYMBOL, timeframe)

    if df is None or df.empty:
        log.warning(
            "Market regime: cannot fetch %s bars — defaulting to BEARISH (fail-safe).",
            REGIME_SYMBOL,
        )
        return RegimeResult(
            is_bullish=False,
            reason=f"No {REGIME_SYMBOL} data available",
        )

    df = add_indicators(df)
    row = df.iloc[-1]

    ema_20 = row.get("ema_20")
    ema_50 = row.get("ema_50")
    rsi_14 = row.get("rsi_14")

    if not all(_is_valid_float(v) for v in [ema_20, ema_50, rsi_14]):
        log.warning(
            "Market regime: NaN indicators on %s — defaulting to BEARISH (fail-safe).",
            REGIME_SYMBOL,
        )
        return RegimeResult(
            is_bullish=False,
            reason=f"{REGIME_SYMBOL} indicators are NaN (not enough history)",
        )

    ema_20 = float(ema_20)
    ema_50 = float(ema_50)
    rsi_14 = float(rsi_14)

    trend_ok = ema_20 > ema_50
    rsi_ok = rsi_14 > RSI_FLOOR

    if trend_ok and rsi_ok:
        reason = (
            f"{REGIME_SYMBOL} EMA20({ema_20:.2f}) > EMA50({ema_50:.2f}), "
            f"RSI({rsi_14:.1f}) > {RSI_FLOOR}"
        )
        log.info("Market regime: BULLISH — %s", reason)
        return RegimeResult(
            is_bullish=True,
            reason=reason,
            ema_20=ema_20,
            ema_50=ema_50,
            rsi_14=rsi_14,
        )

    failed = []
    if not trend_ok:
        failed.append(f"EMA20({ema_20:.2f}) not above EMA50({ema_50:.2f})")
    if not rsi_ok:
        failed.append(f"RSI({rsi_14:.1f}) not above {RSI_FLOOR}")

    reason = f"{REGIME_SYMBOL} not bullish: " + "; ".join(failed)
    log.warning("Market regime: BEARISH — %s", reason)
    return RegimeResult(
        is_bullish=False,
        reason=reason,
        ema_20=ema_20,
        ema_50=ema_50,
        rsi_14=rsi_14,
    )
