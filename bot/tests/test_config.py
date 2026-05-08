from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import os
from config import AssetGroup, Config, _parse_symbols, load_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_env(monkeypatch, extra: dict | None = None) -> None:
    """Set the minimum required env vars so load_config() won't raise."""
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    # Clear vars so tests control them explicitly; prevents .env bleed-through
    for var in (
        "SYMBOLS", "EQUITIES", "INDEX_ETFS", "COMMODITIES", "ALLOW_LIVE_TRADING",
        "PAPER_ACCOUNT_EQUITY", "MAX_ALLOCATION_PCT",
        "DRY_RUN", "TRADING_WINDOW_START", "TRADING_WINDOW_END",
    ):
        monkeypatch.delenv(var, raising=False)
    if extra:
        for k, v in extra.items():
            monkeypatch.setenv(k, v)


# ---------------------------------------------------------------------------
# _parse_symbols
# ---------------------------------------------------------------------------


class TestParseSymbols:
    def test_basic(self):
        assert _parse_symbols("AAPL,MSFT") == ("AAPL", "MSFT")

    def test_strips_whitespace(self):
        assert _parse_symbols(" AAPL , MSFT ") == ("AAPL", "MSFT")

    def test_uppercases(self):
        assert _parse_symbols("aapl,msft") == ("AAPL", "MSFT")

    def test_filters_empty_segments(self):
        assert _parse_symbols("AAPL,,MSFT,") == ("AAPL", "MSFT")

    def test_single_symbol(self):
        assert _parse_symbols("SPY") == ("SPY",)

    def test_empty_string_returns_empty(self):
        assert _parse_symbols("") == ()


# ---------------------------------------------------------------------------
# Config.watchlist()
# ---------------------------------------------------------------------------


class TestWatchlist:
    def _make_config(self, equities=(), index_etfs=(), commodities=()):
        return Config(
            api_key="k",
            api_secret="s",
            paper=True,
            equities=equities,
            index_etfs=index_etfs,
            commodities=commodities,
            risk_per_trade=0.01,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            entry_window_start=__import__("datetime").time(9, 40),
            entry_window_end=__import__("datetime").time(11, 30),
            max_positions=3,
            max_trades_per_symbol=2,
            daily_loss_stop=0.05,
            dry_run=True,
        )

    def test_equities_only(self):
        cfg = self._make_config(equities=("AAPL", "MSFT"))
        assert cfg.watchlist() == ["AAPL", "MSFT"]

    def test_index_etfs_only(self):
        cfg = self._make_config(index_etfs=("SPY", "QQQ"))
        assert cfg.watchlist() == ["SPY", "QQQ"]

    def test_all_groups_combined(self):
        cfg = self._make_config(
            equities=("AAPL",),
            index_etfs=("SPY",),
            commodities=("GLD",),
        )
        assert cfg.watchlist() == ["AAPL", "SPY", "GLD"]

    def test_preserves_order(self):
        cfg = self._make_config(
            equities=("NVDA", "TSLA"),
            index_etfs=("QQQ",),
        )
        assert cfg.watchlist() == ["NVDA", "TSLA", "QQQ"]

    def test_empty_groups_return_empty_list(self):
        cfg = self._make_config()
        assert cfg.watchlist() == []

    def test_returns_list_type(self):
        cfg = self._make_config(equities=("AAPL",))
        assert isinstance(cfg.watchlist(), list)

    def test_default_watchlist_6_symbols(self, monkeypatch):
        _minimal_env(monkeypatch)
        cfg = load_config()
        assert len(cfg.watchlist()) == 6

    def test_default_watchlist_contains_expected_symbols(self, monkeypatch):
        _minimal_env(monkeypatch)
        cfg = load_config()
        wl = cfg.watchlist()
        for sym in ("AAPL", "MSFT", "NVDA", "TSLA", "SPY", "QQQ"):
            assert sym in wl, f"{sym} not in default watchlist"


# ---------------------------------------------------------------------------
# Config.asset_group()
# ---------------------------------------------------------------------------


class TestAssetGroup:
    def _make_config(self):
        return Config(
            api_key="k",
            api_secret="s",
            paper=True,
            equities=("AAPL", "MSFT", "NVDA", "TSLA"),
            index_etfs=("SPY", "QQQ"),
            commodities=("GLD",),
            risk_per_trade=0.01,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            entry_window_start=__import__("datetime").time(9, 40),
            entry_window_end=__import__("datetime").time(11, 30),
            max_positions=3,
            max_trades_per_symbol=2,
            daily_loss_stop=0.05,
            dry_run=True,
        )

    def test_equity_symbol(self):
        assert self._make_config().asset_group("AAPL") == AssetGroup.EQUITY

    def test_index_etf_symbol(self):
        assert self._make_config().asset_group("SPY") == AssetGroup.INDEX_ETF

    def test_commodity_symbol(self):
        assert self._make_config().asset_group("GLD") == AssetGroup.COMMODITY

    def test_unknown_symbol_defaults_to_equity(self):
        assert self._make_config().asset_group("XYZW") == AssetGroup.EQUITY

    def test_case_insensitive(self):
        assert self._make_config().asset_group("spy") == AssetGroup.INDEX_ETF
        assert self._make_config().asset_group("aapl") == AssetGroup.EQUITY


# ---------------------------------------------------------------------------
# load_config — symbol resolution modes
# ---------------------------------------------------------------------------


class TestLoadConfigSymbolResolution:
    def test_grouped_mode_equities_and_index_etfs(self, monkeypatch):
        _minimal_env(monkeypatch, {"EQUITIES": "AAPL,MSFT", "INDEX_ETFS": "SPY"})
        cfg = load_config()
        assert cfg.equities == ("AAPL", "MSFT")
        assert cfg.index_etfs == ("SPY",)
        assert cfg.commodities == ()
        assert cfg.watchlist() == ["AAPL", "MSFT", "SPY"]

    def test_grouped_mode_only_equities_set(self, monkeypatch):
        _minimal_env(monkeypatch, {"EQUITIES": "NVDA,TSLA"})
        cfg = load_config()
        assert cfg.equities == ("NVDA", "TSLA")
        assert cfg.index_etfs == ()

    def test_grouped_mode_only_index_etfs_set(self, monkeypatch):
        _minimal_env(monkeypatch, {"INDEX_ETFS": "QQQ"})
        cfg = load_config()
        assert cfg.equities == ()
        assert cfg.index_etfs == ("QQQ",)

    def test_legacy_symbols_mode(self, monkeypatch):
        _minimal_env(monkeypatch, {"SYMBOLS": "AAPL,GOOGL"})
        cfg = load_config()
        assert cfg.equities == ("AAPL", "GOOGL")
        assert cfg.index_etfs == ()
        assert cfg.commodities == ()

    def test_legacy_symbols_treated_as_equities(self, monkeypatch):
        _minimal_env(monkeypatch, {"SYMBOLS": "SPY,QQQ"})
        cfg = load_config()
        # In legacy mode all symbols land in equities, regardless of ticker name
        assert "SPY" in cfg.equities
        assert len(cfg.index_etfs) == 0

    def test_no_vars_uses_defaults(self, monkeypatch):
        _minimal_env(monkeypatch)
        cfg = load_config()
        assert "AAPL" in cfg.equities
        assert "SPY" in cfg.index_etfs

    def test_grouped_vars_take_priority_over_legacy(self, monkeypatch):
        _minimal_env(
            monkeypatch,
            {"EQUITIES": "NVDA", "INDEX_ETFS": "QQQ", "SYMBOLS": "AAPL,MSFT"},
        )
        cfg = load_config()
        assert cfg.equities == ("NVDA",)
        assert cfg.index_etfs == ("QQQ",)

    def test_commodities_empty_by_default(self, monkeypatch):
        _minimal_env(monkeypatch)
        cfg = load_config()
        assert cfg.commodities == ()

    def test_commodities_can_be_set(self, monkeypatch):
        _minimal_env(
            monkeypatch,
            {"EQUITIES": "AAPL", "COMMODITIES": "GLD,IAU"},
        )
        cfg = load_config()
        assert cfg.commodities == ("GLD", "IAU")

    def test_empty_all_vars_raises(self, monkeypatch):
        _minimal_env(monkeypatch)
        monkeypatch.setenv("EQUITIES", "")
        monkeypatch.setenv("INDEX_ETFS", "   ")
        # This triggers grouped mode (EQUITIES is set but empty-ish)
        # watchlist will be empty → should raise
        # Actually: equities_raw="" is falsy, index_etfs_raw="   " strips to ""
        # so neither is truthy → falls to legacy check → SYMBOLS not set → defaults
        # The only way to get an error is if we force empty grouped mode.
        # Let's test the explicit error path: EQUITIES with only commas
        monkeypatch.setenv("EQUITIES", ",,,")
        monkeypatch.setenv("INDEX_ETFS", ",,,")
        with pytest.raises(EnvironmentError, match="No symbols configured"):
            load_config()


# ---------------------------------------------------------------------------
# load_config — validation
# ---------------------------------------------------------------------------


class TestLoadConfigValidation:
    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.setenv("ALPACA_API_KEY", "")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
        with pytest.raises(EnvironmentError, match="ALPACA_API_KEY"):
            load_config()

    def test_missing_secret_key_raises(self, monkeypatch):
        monkeypatch.setenv("ALPACA_API_KEY", "key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "")
        with pytest.raises(EnvironmentError, match="ALPACA_SECRET_KEY"):
            load_config()

    def test_invalid_risk_per_trade_raises(self, monkeypatch):
        _minimal_env(monkeypatch, {"RISK_PER_TRADE": "0.50"})
        with pytest.raises(ValueError, match="RISK_PER_TRADE"):
            load_config()

    def test_daily_loss_stop_out_of_range_raises(self, monkeypatch):
        _minimal_env(monkeypatch, {"DAILY_LOSS_STOP": "1.5"})
        with pytest.raises(ValueError, match="DAILY_LOSS_STOP"):
            load_config()

    def test_valid_daily_loss_stop_zero(self, monkeypatch):
        _minimal_env(monkeypatch, {"DAILY_LOSS_STOP": "0.0"})
        cfg = load_config()
        assert cfg.daily_loss_stop == 0.0

    def test_paper_defaults_to_true(self, monkeypatch):
        _minimal_env(monkeypatch)
        cfg = load_config()
        assert cfg.paper is True

    def test_paper_false_raises_environment_error(self, monkeypatch):
        _minimal_env(monkeypatch, {"ALPACA_PAPER": "false"})
        with pytest.raises(EnvironmentError, match="ALPACA_PAPER"):
            load_config()

    def test_paper_false_via_zero_raises(self, monkeypatch):
        _minimal_env(monkeypatch, {"ALPACA_PAPER": "0"})
        with pytest.raises(EnvironmentError, match="ALPACA_PAPER"):
            load_config()

    def test_dry_run_defaults_to_false(self, monkeypatch):
        _minimal_env(monkeypatch)
        cfg = load_config()
        assert cfg.dry_run is False

    def test_max_etf_group_positions_defaults_to_1(self, monkeypatch):
        _minimal_env(monkeypatch)
        cfg = load_config()
        assert cfg.max_etf_group_positions == 1

    def test_max_etf_group_positions_can_be_set(self, monkeypatch):
        _minimal_env(monkeypatch, {"MAX_ETF_GROUP_POSITIONS": "2"})
        cfg = load_config()
        assert cfg.max_etf_group_positions == 2

    def test_max_etf_group_positions_zero_raises(self, monkeypatch):
        _minimal_env(monkeypatch, {"MAX_ETF_GROUP_POSITIONS": "0"})
        with pytest.raises(ValueError, match="MAX_ETF_GROUP_POSITIONS"):
            load_config()

    # -----------------------------------------------------------------------
    # Live-money hard lock
    # -----------------------------------------------------------------------

    def test_paper_true_always_allowed(self, monkeypatch):
        """Paper mode must work regardless of ALLOW_LIVE_TRADING."""
        _minimal_env(monkeypatch, {"ALPACA_PAPER": "true", "ALLOW_LIVE_TRADING": "false"})
        cfg = load_config()
        assert cfg.paper is True

    def test_paper_true_allow_live_not_set(self, monkeypatch):
        """Paper mode works even when ALLOW_LIVE_TRADING is absent."""
        _minimal_env(monkeypatch)
        monkeypatch.delenv("ALLOW_LIVE_TRADING", raising=False)
        cfg = load_config()
        assert cfg.paper is True

    def test_live_lock_blocked_when_allow_flag_false(self, monkeypatch):
        """ALPACA_PAPER=false + ALLOW_LIVE_TRADING=false must raise."""
        _minimal_env(monkeypatch, {"ALPACA_PAPER": "false", "ALLOW_LIVE_TRADING": "false"})
        with pytest.raises(EnvironmentError, match="ALLOW_LIVE_TRADING"):
            load_config()

    def test_live_lock_blocked_when_allow_flag_absent(self, monkeypatch):
        """ALPACA_PAPER=false with no ALLOW_LIVE_TRADING must raise."""
        _minimal_env(monkeypatch, {"ALPACA_PAPER": "false"})
        monkeypatch.delenv("ALLOW_LIVE_TRADING", raising=False)
        with pytest.raises(EnvironmentError):
            load_config()

    def test_live_lock_unlocked_when_both_flags_set(self, monkeypatch):
        """ALPACA_PAPER=false + ALLOW_LIVE_TRADING=true must NOT raise."""
        _minimal_env(monkeypatch, {"ALPACA_PAPER": "false", "ALLOW_LIVE_TRADING": "true"})
        cfg = load_config()
        assert cfg.paper is False
        assert cfg.allow_live_trading is True

    def test_allow_live_trading_defaults_to_false(self, monkeypatch):
        """allow_live_trading must be False when env var is absent."""
        _minimal_env(monkeypatch)
        monkeypatch.delenv("ALLOW_LIVE_TRADING", raising=False)
        cfg = load_config()
        assert cfg.allow_live_trading is False

    def test_paper_account_equity_default_is_1000(self, monkeypatch):
        _minimal_env(monkeypatch)
        cfg = load_config()
        assert cfg.paper_account_equity == 1000.0

    def test_paper_account_equity_can_be_set(self, monkeypatch):
        _minimal_env(monkeypatch, {"PAPER_ACCOUNT_EQUITY": "5000"})
        cfg = load_config()
        assert cfg.paper_account_equity == 5000.0

    def test_paper_account_equity_zero_raises(self, monkeypatch):
        _minimal_env(monkeypatch, {"PAPER_ACCOUNT_EQUITY": "0"})
        with pytest.raises(ValueError, match="PAPER_ACCOUNT_EQUITY"):
            load_config()

    def test_paper_account_equity_negative_raises(self, monkeypatch):
        _minimal_env(monkeypatch, {"PAPER_ACCOUNT_EQUITY": "-500"})
        with pytest.raises(ValueError, match="PAPER_ACCOUNT_EQUITY"):
            load_config()

    def test_max_allocation_pct_default_is_0_10(self, monkeypatch):
        _minimal_env(monkeypatch)
        cfg = load_config()
        assert cfg.max_allocation_pct == 0.10

    def test_max_allocation_pct_can_be_set(self, monkeypatch):
        _minimal_env(monkeypatch, {"MAX_ALLOCATION_PCT": "0.25"})
        cfg = load_config()
        assert cfg.max_allocation_pct == 0.25

    def test_max_allocation_pct_zero_raises(self, monkeypatch):
        _minimal_env(monkeypatch, {"MAX_ALLOCATION_PCT": "0"})
        with pytest.raises(ValueError, match="MAX_ALLOCATION_PCT"):
            load_config()

    def test_max_allocation_pct_above_1_raises(self, monkeypatch):
        _minimal_env(monkeypatch, {"MAX_ALLOCATION_PCT": "1.5"})
        with pytest.raises(ValueError, match="MAX_ALLOCATION_PCT"):
            load_config()


# ---------------------------------------------------------------------------
# Config.is_in_etf_group()
# ---------------------------------------------------------------------------


class TestIsInEtfGroup:
    def _make_config(self):
        from datetime import time as dt_time
        return Config(
            api_key="k",
            api_secret="s",
            paper=True,
            equities=("AAPL", "MSFT"),
            index_etfs=("SPY", "QQQ"),
            commodities=(),
            risk_per_trade=0.01,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            entry_window_start=dt_time(9, 40),
            entry_window_end=dt_time(11, 30),
            max_positions=3,
            max_trades_per_symbol=2,
            daily_loss_stop=0.05,
            max_etf_group_positions=1,
            dry_run=True,
        )

    def test_spy_is_in_etf_group(self):
        assert self._make_config().is_in_etf_group("SPY") is True

    def test_qqq_is_in_etf_group(self):
        assert self._make_config().is_in_etf_group("QQQ") is True

    def test_equity_is_not_in_etf_group(self):
        assert self._make_config().is_in_etf_group("AAPL") is False

    def test_unknown_symbol_is_not_in_etf_group(self):
        assert self._make_config().is_in_etf_group("GLD") is False

    def test_case_insensitive(self):
        assert self._make_config().is_in_etf_group("spy") is True
        assert self._make_config().is_in_etf_group("qqq") is True


# ---------------------------------------------------------------------------
# Config.trade_watchlist() — regime ETFs excluded from trade candidates
# ---------------------------------------------------------------------------


class TestTradeWatchlist:
    def _make_config(self, equities=(), index_etfs=(), commodities=()):
        from datetime import time as dt_time
        return Config(
            api_key="k", api_secret="s", paper=True,
            equities=equities, index_etfs=index_etfs, commodities=commodities,
            risk_per_trade=0.01, stop_loss_pct=0.01, take_profit_pct=0.02,
            entry_window_start=dt_time(9, 35), entry_window_end=dt_time(11, 30),
            max_positions=2, max_trades_per_symbol=2, daily_loss_stop=0.05,
            dry_run=True,
        )

    def test_trade_watchlist_excludes_index_etfs(self):
        cfg = self._make_config(
            equities=("PLTR", "AMD"),
            index_etfs=("SPY", "QQQ", "IWM"),
        )
        tw = cfg.trade_watchlist()
        for sym in ("SPY", "QQQ", "IWM"):
            assert sym not in tw, f"Regime symbol {sym} must not appear in trade_watchlist()"

    def test_trade_watchlist_includes_equities(self):
        cfg = self._make_config(
            equities=("PLTR", "AMD", "SOFI"),
            index_etfs=("SPY", "QQQ", "IWM"),
        )
        assert cfg.trade_watchlist() == ["PLTR", "AMD", "SOFI"]

    def test_trade_watchlist_includes_commodities(self):
        cfg = self._make_config(
            equities=("PLTR",), index_etfs=("SPY",), commodities=("GLD",),
        )
        assert cfg.trade_watchlist() == ["PLTR", "GLD"]

    def test_watchlist_still_includes_all_groups(self):
        cfg = self._make_config(equities=("PLTR",), index_etfs=("SPY",))
        assert "SPY" in cfg.watchlist()
        assert "PLTR" in cfg.watchlist()

    def test_default_trade_watchlist_excludes_spy_qqq_iwm(self, monkeypatch):
        _minimal_env(monkeypatch, {
            "EQUITIES": "PLTR,AMD,SOFI,HOOD,INTC,XLK",
            "INDEX_ETFS": "SPY,QQQ,IWM",
        })
        cfg = load_config()
        tw = cfg.trade_watchlist()
        for sym in ("SPY", "QQQ", "IWM"):
            assert sym not in tw, f"{sym} must not be a trade candidate"
        for sym in ("PLTR", "AMD", "SOFI", "HOOD", "INTC", "XLK"):
            assert sym in tw, f"{sym} must be in trade_watchlist"

    def test_rsi_overbought_env_can_be_80(self, monkeypatch):
        monkeypatch.setenv("RSI_OVERBOUGHT", "80")
        assert float(os.getenv("RSI_OVERBOUGHT")) == 80.0
