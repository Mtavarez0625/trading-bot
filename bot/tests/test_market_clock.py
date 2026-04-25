from __future__ import annotations

import os
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import market_clock
from market_clock import is_within_entry_window

ET = ZoneInfo("America/New_York")

WINDOW_START = time(9, 40)
WINDOW_END = time(11, 30)


def _fake_et(hour: int, minute: int, second: int = 0):
    """Return a callable that produces a fixed ET datetime."""
    dt = datetime(2024, 4, 1, hour, minute, second, tzinfo=ET)
    return lambda: dt


# ---------------------------------------------------------------------------
# Entry window
# ---------------------------------------------------------------------------


class TestEntryWindow:
    def test_inside_window(self, monkeypatch):
        monkeypatch.setattr(market_clock, "now_et", _fake_et(10, 0))
        assert is_within_entry_window(WINDOW_START, WINDOW_END) is True

    def test_before_window(self, monkeypatch):
        monkeypatch.setattr(market_clock, "now_et", _fake_et(9, 35))
        assert is_within_entry_window(WINDOW_START, WINDOW_END) is False

    def test_after_window(self, monkeypatch):
        monkeypatch.setattr(market_clock, "now_et", _fake_et(12, 0))
        assert is_within_entry_window(WINDOW_START, WINDOW_END) is False

    def test_exactly_at_window_start(self, monkeypatch):
        monkeypatch.setattr(market_clock, "now_et", _fake_et(9, 40))
        assert is_within_entry_window(WINDOW_START, WINDOW_END) is True

    def test_exactly_at_window_end(self, monkeypatch):
        monkeypatch.setattr(market_clock, "now_et", _fake_et(11, 30))
        assert is_within_entry_window(WINDOW_START, WINDOW_END) is True

    def test_one_second_before_start(self, monkeypatch):
        monkeypatch.setattr(market_clock, "now_et", _fake_et(9, 39, 59))
        assert is_within_entry_window(WINDOW_START, WINDOW_END) is False

    def test_one_minute_after_end(self, monkeypatch):
        monkeypatch.setattr(market_clock, "now_et", _fake_et(11, 31))
        assert is_within_entry_window(WINDOW_START, WINDOW_END) is False

    def test_custom_window(self, monkeypatch):
        monkeypatch.setattr(market_clock, "now_et", _fake_et(14, 0))
        assert is_within_entry_window(time(13, 0), time(15, 0)) is True

    def test_custom_window_outside(self, monkeypatch):
        monkeypatch.setattr(market_clock, "now_et", _fake_et(12, 0))
        assert is_within_entry_window(time(13, 0), time(15, 0)) is False


# ---------------------------------------------------------------------------
# is_market_open (mocked trading client)
# ---------------------------------------------------------------------------


class TestIsMarketOpen:
    def test_returns_true_when_clock_is_open(self):
        class FakeClock:
            is_open = True

        class FakeClient:
            def get_clock(self):
                return FakeClock()

        assert market_clock.is_market_open(FakeClient()) is True

    def test_returns_false_when_clock_is_closed(self):
        class FakeClock:
            is_open = False

        class FakeClient:
            def get_clock(self):
                return FakeClock()

        assert market_clock.is_market_open(FakeClient()) is False

    def test_returns_false_on_api_exception(self):
        class FakeClient:
            def get_clock(self):
                raise ConnectionError("Timeout")

        assert market_clock.is_market_open(FakeClient()) is False
