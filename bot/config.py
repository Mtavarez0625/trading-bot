from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from enum import Enum
from typing import List, Tuple

from dotenv import load_dotenv

load_dotenv()


class AssetGroup(str, Enum):
    """Logical category for a symbol.
    Drives future per-group strategy/risk overrides without code restructure.
    """
    EQUITY = "equity"
    INDEX_ETF = "index_etf"
    COMMODITY = "commodity"   # reserved — add GLD / IAU here later


@dataclass(frozen=True)
class Config:
    # Alpaca credentials
    api_key: str
    api_secret: str
    paper: bool

    # Symbol groups (immutable tuples)
    equities: Tuple[str, ...]
    index_etfs: Tuple[str, ...]
    commodities: Tuple[str, ...]   # empty by default; GLD/IAU go here later

    # Risk
    risk_per_trade: float    # fraction of equity, e.g. 0.01 = 1%
    stop_loss_pct: float     # e.g. 0.01 = 1%
    take_profit_pct: float   # e.g. 0.02 = 2%

    # Entry window (America/New_York)
    entry_window_start: time
    entry_window_end: time

    # Limits
    max_positions: int
    max_trades_per_symbol: int
    daily_loss_stop: float   # halt new entries if equity drops this fraction from day-start

    # Mode
    dry_run: bool

    # Correlated-group exposure cap (fields with defaults must come last in the dataclass)
    max_etf_group_positions: int = 1  # max simultaneous positions from the correlated ETF group

    # Live-money gate — must be explicitly true to run with ALPACA_PAPER=false
    allow_live_trading: bool = False

    # Dry-run simulated account — used for position sizing when dry_run=True
    paper_account_equity: float = 1000.0

    # Hard cap: a single position cannot exceed this fraction of equity
    max_allocation_pct: float = 0.10

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def watchlist(self) -> List[str]:
        """Return the combined active symbol list across all asset groups."""
        return list(self.equities) + list(self.index_etfs) + list(self.commodities)

    def trade_watchlist(self) -> List[str]:
        """Return only trade-eligible symbols (equities + commodities).
        Index ETFs are regime-only and excluded from new-entry evaluation."""
        return list(self.equities) + list(self.commodities)

    def is_in_etf_group(self, symbol: str) -> bool:
        """Return True if the symbol belongs to the correlated index-ETF group."""
        return symbol.upper() in self.index_etfs

    def asset_group(self, symbol: str) -> AssetGroup:
        """Return the AssetGroup for a given symbol.
        Falls back to EQUITY for any unknown symbol so callers never get None.
        """
        sym = symbol.upper()
        if sym in self.index_etfs:
            return AssetGroup.INDEX_ETF
        if sym in self.commodities:
            return AssetGroup.COMMODITY
        return AssetGroup.EQUITY


def _parse_window_time(raw: str, default: time) -> time:
    """Parse 'HH:MM' string into a time object, returning default on any error."""
    if raw:
        try:
            parts = raw.split(":")
            return time(int(parts[0]), int(parts[1]))
        except Exception:
            pass
    return default


def _parse_symbols(raw: str) -> Tuple[str, ...]:
    """Parse a comma-separated symbol string into a clean uppercase tuple."""
    return tuple(s.strip().upper() for s in raw.split(",") if s.strip())


def load_config() -> Config:
    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    api_secret = os.getenv("ALPACA_SECRET_KEY", "").strip()

    if not api_key:
        raise EnvironmentError("ALPACA_API_KEY is missing or empty.")
    if not api_secret:
        raise EnvironmentError("ALPACA_SECRET_KEY is missing or empty.")

    paper = os.getenv("ALPACA_PAPER", "true").strip().lower() in ("1", "true", "yes")
    dry_run = os.getenv("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")
    allow_live_trading = (
        os.getenv("ALLOW_LIVE_TRADING", "false").strip().lower() in ("1", "true", "yes")
    )

    # Live-money hard lock: paper trading is always allowed; live trading requires
    # ALPACA_PAPER=false AND ALLOW_LIVE_TRADING=true (both explicit opt-ins).
    if not paper and not allow_live_trading:
        raise EnvironmentError(
            "ALPACA_PAPER is false and ALLOW_LIVE_TRADING is not set to true. "
            "Real-money trading is locked. "
            "Set ALLOW_LIVE_TRADING=true only if you intend to trade with real money."
        )

    # ------------------------------------------------------------------
    # Symbol resolution — three modes, applied in priority order:
    #
    #   1. Explicit grouped vars (EQUITIES and/or INDEX_ETFS) → grouped mode
    #   2. Legacy SYMBOLS var (backward compat) → all treated as equities
    #   3. No vars set at all → sensible defaults
    # ------------------------------------------------------------------
    equities_raw = os.getenv("EQUITIES", "").strip()
    index_etfs_raw = os.getenv("INDEX_ETFS", "").strip()
    commodities_raw = os.getenv("COMMODITIES", "").strip()
    legacy_raw = os.getenv("SYMBOLS", "").strip()

    if equities_raw or index_etfs_raw:
        # Explicit grouped mode
        equities = _parse_symbols(equities_raw) if equities_raw else ()
        index_etfs = _parse_symbols(index_etfs_raw) if index_etfs_raw else ()
        commodities = _parse_symbols(commodities_raw) if commodities_raw else ()
    elif legacy_raw:
        # Legacy single-list: treat everything as equities
        equities = _parse_symbols(legacy_raw)
        index_etfs = ()
        commodities = ()
    else:
        # Sensible defaults — 4 large-cap equities + 2 broad-market ETFs
        equities = ("AAPL", "MSFT", "NVDA", "TSLA")
        index_etfs = ("SPY", "QQQ")
        commodities = ()

    all_symbols = list(equities) + list(index_etfs) + list(commodities)
    if not all_symbols:
        raise EnvironmentError(
            "No symbols configured — set EQUITIES, INDEX_ETFS, or SYMBOLS."
        )

    risk_per_trade = float(os.getenv("RISK_PER_TRADE", "0.01"))
    stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "0.03"))
    take_profit_pct = float(os.getenv("TAKE_PROFIT_PCT", "0.05"))
    max_positions = int(os.getenv("MAX_POSITIONS", "2"))
    paper_account_equity = float(os.getenv("PAPER_ACCOUNT_EQUITY", "1000.0"))
    max_allocation_pct = float(os.getenv("MAX_ALLOCATION_PCT", "0.10"))
    max_trades_per_symbol = int(os.getenv("MAX_TRADES_PER_SYMBOL", "2"))
    daily_loss_stop = float(os.getenv("DAILY_LOSS_STOP", "0.05"))
    max_etf_group_positions = int(os.getenv("MAX_ETF_GROUP_POSITIONS", "1"))
    # allow_live_trading already parsed above (used in the hard-lock check)

    if not (0 < risk_per_trade <= 0.10):
        raise ValueError(
            f"RISK_PER_TRADE must be between 0 and 0.10, got {risk_per_trade}"
        )
    if not (0 < stop_loss_pct <= 0.20):
        raise ValueError(
            f"STOP_LOSS_PCT must be between 0 and 0.20, got {stop_loss_pct}"
        )
    if not (0 < take_profit_pct <= 0.50):
        raise ValueError(
            f"TAKE_PROFIT_PCT must be between 0 and 0.50, got {take_profit_pct}"
        )
    if max_positions < 1:
        raise ValueError("MAX_POSITIONS must be >= 1")
    if max_trades_per_symbol < 1:
        raise ValueError("MAX_TRADES_PER_SYMBOL must be >= 1")
    if max_etf_group_positions < 1:
        raise ValueError("MAX_ETF_GROUP_POSITIONS must be >= 1")
    if not (0.0 <= daily_loss_stop <= 1.0):
        raise ValueError(
            f"DAILY_LOSS_STOP must be between 0.0 and 1.0, got {daily_loss_stop}"
        )
    if paper_account_equity <= 0:
        raise ValueError(
            f"PAPER_ACCOUNT_EQUITY must be positive, got {paper_account_equity}"
        )
    if not (0 < max_allocation_pct <= 1.0):
        raise ValueError(
            f"MAX_ALLOCATION_PCT must be between 0 and 1.0, got {max_allocation_pct}"
        )

    entry_window_start = _parse_window_time(
        os.getenv("TRADING_WINDOW_START", "").strip(), time(9, 35)
    )
    entry_window_end = _parse_window_time(
        os.getenv("TRADING_WINDOW_END", "").strip(), time(11, 30)
    )

    return Config(
        api_key=api_key,
        api_secret=api_secret,
        paper=paper,
        equities=equities,
        index_etfs=index_etfs,
        commodities=commodities,
        risk_per_trade=risk_per_trade,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        entry_window_start=entry_window_start,
        entry_window_end=entry_window_end,
        max_positions=max_positions,
        max_trades_per_symbol=max_trades_per_symbol,
        daily_loss_stop=daily_loss_stop,
        max_etf_group_positions=max_etf_group_positions,
        allow_live_trading=allow_live_trading,
        dry_run=dry_run,
        paper_account_equity=paper_account_equity,
        max_allocation_pct=max_allocation_pct,
    )
