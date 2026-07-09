from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

from data_service import fetch_bars
from indicators import add_indicators, _is_valid_float
from logger import get_logger

log = get_logger(__name__)

SPY_SYMBOL = "SPY"
QQQ_SYMBOL = "QQQ"

# Keep as a public constant so tests can reference it without hard-coding.
REGIME_SYMBOL = SPY_SYMBOL


@dataclass
class RegimeResult:
    is_bullish: bool
    reason: str
    spy_close: Optional[float] = field(default=None)
    spy_vwap: Optional[float] = field(default=None)
    qqq_close: Optional[float] = field(default=None)
    qqq_vwap: Optional[float] = field(default=None)


def _symbol_above_vwap(
    data_client, symbol: str, timeframe
) -> Tuple[bool, Optional[float], Optional[float], str]:
    """
    Fetch bars for `symbol` and test whether its latest close is above VWAP.

    Returns (above_vwap, close, vwap, failure_reason).
    failure_reason is empty string on success.
    """
    df = fetch_bars(data_client, symbol, timeframe)

    if df is None or df.empty:
        return False, None, None, f"No {symbol} data available"

    df = add_indicators(df)
    row = df.iloc[-1]
    close = row.get("close")
    vwap = row.get("vwap")

    if not (_is_valid_float(close) and _is_valid_float(vwap)):
        return False, None, None, f"{symbol} VWAP unavailable (insufficient history)"

    close = float(close)
    vwap = float(vwap)
    return close > vwap, close, vwap, ""


def check_market_regime(data_client, timeframe) -> RegimeResult:
    """
    Determine whether the broad market is in a bullish state.

    Rule: SPY close > SPY VWAP  AND  QQQ close > QQQ VWAP.

    Fail-safe: if either symbol's data is unavailable or VWAP is NaN,
    returns is_bullish=False so the bot never enters on an unknown regime.
    """
    spy_ok, spy_close, spy_vwap, spy_err = _symbol_above_vwap(
        data_client, SPY_SYMBOL, timeframe
    )
    if spy_err:
        log.warning(
            "Market regime: %s — defaulting to BEARISH (fail-safe).", spy_err
        )
        return RegimeResult(is_bullish=False, reason=spy_err)

    qqq_ok, qqq_close, qqq_vwap, qqq_err = _symbol_above_vwap(
        data_client, QQQ_SYMBOL, timeframe
    )
    if qqq_err:
        log.warning(
            "Market regime: %s — defaulting to BEARISH (fail-safe).", qqq_err
        )
        return RegimeResult(
            is_bullish=False,
            reason=qqq_err,
            spy_close=spy_close,
            spy_vwap=spy_vwap,
        )

    failed = []
    if not spy_ok:
        failed.append(
            f"SPY(close={spy_close:.2f}) below VWAP({spy_vwap:.2f})"
        )
    if not qqq_ok:
        failed.append(
            f"QQQ(close={qqq_close:.2f}) below VWAP({qqq_vwap:.2f})"
        )

    if failed:
        reason = "skipped: market regime bearish — " + "; ".join(failed)
        log.warning("Market regime: BEARISH — %s", reason)
        return RegimeResult(
            is_bullish=False,
            reason=reason,
            spy_close=spy_close,
            spy_vwap=spy_vwap,
            qqq_close=qqq_close,
            qqq_vwap=qqq_vwap,
        )

    reason = (
        f"SPY(close={spy_close:.2f}) > VWAP({spy_vwap:.2f}); "
        f"QQQ(close={qqq_close:.2f}) > VWAP({qqq_vwap:.2f})"
    )
    log.info("Market regime: BULLISH — %s", reason)
    return RegimeResult(
        is_bullish=True,
        reason=reason,
        spy_close=spy_close,
        spy_vwap=spy_vwap,
        qqq_close=qqq_close,
        qqq_vwap=qqq_vwap,
    )
