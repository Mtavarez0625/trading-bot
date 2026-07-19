"""
Tests for the canonical execution-mode labeling used by /health,
/dashboard-data, /strategy-status, startup logs, and Telegram messages.

These replace the old ambiguous bot_mode: "live" naming, which conflated
"orders are actually being submitted" (not a dry run) with "real money is
at risk" (live trading). See main._execution_mode_fields().
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _reload_main_with_env(monkeypatch, **env):
    """Reload main.py with a controlled set of DRY_RUN/ALPACA_PAPER/ALLOW_LIVE_TRADING env vars."""
    for key in ("DRY_RUN", "ALPACA_PAPER", "ALLOW_LIVE_TRADING"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Never let a reload during tests hit Telegram.
    monkeypatch.setenv("TELEGRAM_ALERTS_ENABLED", "false")

    if "main" in sys.modules:
        del sys.modules["main"]
    import main  # noqa: E402
    return main


class TestExecutionModeFields:
    def test_dry_run_true_reports_dry_run(self, monkeypatch):
        main = _reload_main_with_env(
            monkeypatch, DRY_RUN="true", ALPACA_PAPER="true", ALLOW_LIVE_TRADING="false"
        )
        fields = main._execution_mode_fields()
        assert fields["execution_mode"] == "dry_run"
        assert fields["dry_run"] is True
        assert fields["paper_trading"] is True
        assert fields["real_money_trading"] is False
        assert fields["environment"] == "paper"
        assert fields["bot_mode"] == "dry_run"

    def test_paper_live_is_the_normal_paper_trading_state(self, monkeypatch):
        main = _reload_main_with_env(
            monkeypatch, DRY_RUN="false", ALPACA_PAPER="true", ALLOW_LIVE_TRADING="false"
        )
        fields = main._execution_mode_fields()
        assert fields["execution_mode"] == "paper_live"
        assert fields["dry_run"] is False
        assert fields["paper_trading"] is True
        assert fields["real_money_trading"] is False
        assert fields["environment"] == "paper"
        assert fields["live_trading_locked"] is True
        # Legacy field must never say "live" alone for this state.
        assert fields["bot_mode"] != "live"
        assert fields["bot_mode"] == "paper_live"

    def test_live_money_requires_both_gates_open(self, monkeypatch):
        main = _reload_main_with_env(
            monkeypatch, DRY_RUN="false", ALPACA_PAPER="false", ALLOW_LIVE_TRADING="true"
        )
        fields = main._execution_mode_fields()
        assert fields["execution_mode"] == "live_money"
        assert fields["real_money_trading"] is True
        assert fields["environment"] == "live"
        assert fields["live_trading_locked"] is False

    def test_live_locked_out_when_paper_false_but_gate_closed(self, monkeypatch):
        main = _reload_main_with_env(
            monkeypatch, DRY_RUN="false", ALPACA_PAPER="false", ALLOW_LIVE_TRADING="false"
        )
        fields = main._execution_mode_fields()
        assert fields["execution_mode"] == "live_locked_out"
        assert fields["real_money_trading"] is False

    def test_health_endpoint_never_reports_bare_live_in_paper_mode(self, monkeypatch):
        main = _reload_main_with_env(
            monkeypatch, DRY_RUN="false", ALPACA_PAPER="true", ALLOW_LIVE_TRADING="false"
        )
        result = main.health()
        assert result["execution_mode"] == "paper_live"
        assert result["paper_trading"] is True
        assert result["real_money_trading"] is False
        # Backward-compat field retained, but no longer the ambiguous "live".
        assert result["bot_mode"] == "paper_live"
