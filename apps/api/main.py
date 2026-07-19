import os
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional

import pandas as pd
import requests
from dotenv import load_dotenv
from fastapi import FastAPI

import journal
from scoring import compute_candidate_score, score_summary_line

load_dotenv()

# ── Event logging & Telegram alert services ──────────────────────────────────
# Fail-safe: if the service modules are unavailable, the bot continues without them.
try:
    from app.services.event_logger import (
        log_event as _log_evt,
        get_recent_events,
        load_events_from_file,
    )
    from app.services.telegram_alerts import send_telegram_alert
except Exception as _svc_err:
    print(f"[startup] WARNING: alert services not available: {_svc_err}")
    def _log_evt(*a, **kw): pass
    def get_recent_events(*a, **kw): return []
    def load_events_from_file(*a, **kw): return []
    def send_telegram_alert(*a, **kw): return False

# ── Historical analytics & Hermes insights services (Phase 2B, read-only) ─────
# Fail-safe: if unavailable, the analytics endpoints report the error but the
# trading API continues unaffected.
try:
    from app.services import analytics as analytics_service
    from app.services import hermes_insights as hermes_insights_service
except Exception as _analytics_err:
    print(f"[startup] WARNING: analytics services not available: {_analytics_err}")
    analytics_service = None
    hermes_insights_service = None

# ── DRY_RUN mode ──────────────────────────────────────────────────────────────
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

app = FastAPI(title="Trading Bot API V3")

@app.on_event("startup")
def _startup():
    journal.init_db()
    _reconcile_journal_state()
    if ALPACA_PAPER and not ALLOW_LIVE_TRADING:
        print("ALPACA PAPER MODE CONFIRMED — no live money.")
    _check_flat_start()
    # Confirm all session state starts clean — these are module-level globals
    # that reset to their initial values on every process start.
    print(
        f"[startup] SESSION STATE RESET: session_flattened=False | "
        f"consecutive_losses=0 | loss_cooldown=None | daily_loss_shutdown=False"
    )
    _mode = _execution_mode_fields()
    print(
        f"[startup] Trading Bot API V3 ready | "
        f"execution_mode={_mode['execution_mode']} | "
        f"paper={ALPACA_PAPER} | live_locked={not ALLOW_LIVE_TRADING} | "
        f"window={TRADING_WINDOW_START}–{TRADING_WINDOW_END} ET | "
        f"last_entry_time={LAST_ENTRY_TIME} ET | "
        f"watchlist={TRADE_WATCHLIST} | "
        f"risk={RISK_PER_TRADE_PCT*100:.2f}%/trade | "
        f"stop={STOP_LOSS_PCT*100:.1f}% | tp={TAKE_PROFIT_PCT*100:.1f}% | "
        f"score_min={MIN_ENTRY_SCORE} | max_pos={MAX_OPEN_POSITIONS} | "
        f"flatten_at_end={FLATTEN_AT_WINDOW_END}"
    )
    _log_evt(
        "bot_started",
        f"Bot V3 started | execution_mode={_mode['execution_mode']} | watchlist={TRADE_WATCHLIST}",
        severity="success",
        data={**_mode, "watchlist": TRADE_WATCHLIST},
    )
    send_telegram_alert(
        "Bot Started",
        f"Execution mode: {_mode['execution_mode']} | Watchlist: {', '.join(TRADE_WATCHLIST)}",
        severity="success",
    )

trade_log = []

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL")
DATA_URL   = os.getenv("ALPACA_DATA_URL")

# ── Symbol lists ──────────────────────────────────────────────────────────────
# EQUITIES (Hybrid bot name) or TRADE_WATCHLIST/WATCHLIST (legacy): eligible for entries
_tw_env       = os.getenv("EQUITIES", os.getenv("TRADE_WATCHLIST", os.getenv("WATCHLIST", "")))
TRADE_WATCHLIST = [s.strip().upper() for s in _tw_env.split(",") if s.strip()] or ["PLTR", "AMD", "SOFI", "HOOD", "INTC", "XLK"]
WATCHLIST = TRADE_WATCHLIST  # backward-compat alias

# INDEX_ETFS (Hybrid bot name) or REGIME_SYMBOLS (legacy): market-direction checks only — never traded
_rs_env        = os.getenv("INDEX_ETFS", os.getenv("REGIME_SYMBOLS", "SPY,QQQ,IWM"))
REGIME_SYMBOLS = {s.strip().upper() for s in _rs_env.split(",") if s.strip()}
INDEX_ETFS     = sorted(REGIME_SYMBOLS)

# ── V3 Risk & Strategy Constants (all configurable via .env) ──────────────────
# MAX_POSITIONS (Hybrid bot name) or MAX_OPEN_POSITIONS (legacy)
MAX_OPEN_POSITIONS     = int(os.getenv("MAX_OPEN_POSITIONS",    os.getenv("MAX_POSITIONS",    "3")))
TRADE_COOLDOWN_MINUTES = int(os.getenv("TRADE_COOLDOWN_MINUTES","15"))
MIN_TREND_STRENGTH     = float(os.getenv("MIN_TREND_STRENGTH",  "0.01"))
# DAILY_LOSS_STOP (Hybrid bot name) or DAILY_LOSS_LIMIT_PCT (legacy)
DAILY_LOSS_LIMIT_PCT   = float(os.getenv("DAILY_LOSS_LIMIT_PCT", os.getenv("DAILY_LOSS_STOP", "0.03")))
MAX_ALLOCATION_PCT     = float(os.getenv("MAX_ALLOCATION_PCT",  "0.10"))
# RISK_PER_TRADE (Hybrid bot name) or RISK_PER_TRADE_PCT (legacy)
RISK_PER_TRADE_PCT     = float(os.getenv("RISK_PER_TRADE_PCT",  os.getenv("RISK_PER_TRADE",  "0.01")))
STOP_LOSS_PCT          = float(os.getenv("STOP_LOSS_PCT",       "0.03"))
TAKE_PROFIT_PCT        = float(os.getenv("TAKE_PROFIT_PCT",     "0.05"))
TRAILING_STOP_PCT      = float(os.getenv("TRAILING_STOP_PCT",   "0.0"))  # 0 = use fixed stop
MAX_TRADES_PER_SYMBOL  = int(os.getenv("MAX_TRADES_PER_SYMBOL", "1"))

# ── Trading window & safety flags ─────────────────────────────────────────────
TRADING_WINDOW_START = os.getenv("TRADING_WINDOW_START", "09:35")
TRADING_WINDOW_END   = os.getenv("TRADING_WINDOW_END",   "11:30")
ALPACA_PAPER         = os.getenv("ALPACA_PAPER", "true").lower() == "true"
ALLOW_LIVE_TRADING   = os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"


def _execution_mode_fields() -> dict:
    """
    Single source of truth for how execution mode is reported across
    /health, dashboard endpoints, logs, and Telegram — so nothing can
    disagree about whether real money is at risk.

    execution_mode:
      "dry_run"          — DRY_RUN=true, no orders sent anywhere.
      "paper_live"        — orders sent, but only to Alpaca's paper endpoint
                             (DRY_RUN=false, ALPACA_PAPER=true, ALLOW_LIVE_TRADING=false).
      "live_money"        — orders sent to Alpaca's real-money endpoint
                             (ALPACA_PAPER=false AND ALLOW_LIVE_TRADING=true).
      "live_locked_out"   — ALPACA_PAPER=false but ALLOW_LIVE_TRADING=false; the
                             bot/config.py startup hard-lock refuses to run this
                             combination, but it's labeled explicitly just in case.
    """
    if DRY_RUN:
        execution_mode = "dry_run"
    elif ALPACA_PAPER and not ALLOW_LIVE_TRADING:
        execution_mode = "paper_live"
    elif not ALPACA_PAPER and ALLOW_LIVE_TRADING:
        execution_mode = "live_money"
    else:
        execution_mode = "live_locked_out"

    return {
        "environment":         "paper" if ALPACA_PAPER else "live",
        "execution_mode":      execution_mode,
        "paper_trading":       ALPACA_PAPER,
        "real_money_trading":  (not ALPACA_PAPER) and ALLOW_LIVE_TRADING,
        "dry_run":             DRY_RUN,
        "live_trading_locked": not ALLOW_LIVE_TRADING,
        # Deprecated: kept only for dashboards/scripts still reading bot_mode.
        # New code should read execution_mode above instead.
        "bot_mode":            execution_mode,
    }


# After TRADING_WINDOW_END: market-sell all open bot-managed positions.
FLATTEN_AT_WINDOW_END = os.getenv("FLATTEN_AT_WINDOW_END", "false").lower() == "true"
# Warn loudly on startup if Alpaca paper positions already exist before the trading window.
REQUIRE_FLAT_START    = os.getenv("REQUIRE_FLAT_START",    "false").lower() == "true"

# Simulated account equity used for position sizing in DRY_RUN mode.
# Prevents Alpaca paper buying power from inflating position sizes.
PAPER_ACCOUNT_EQUITY   = float(os.getenv("PAPER_ACCOUNT_EQUITY", "1000"))

# ── V3 Signal Enhancement Constants ──────────────────────────────────────────
RSI_PERIOD         = int(os.getenv("RSI_PERIOD",       "14"))
RSI_OVERBOUGHT     = float(os.getenv("RSI_OVERBOUGHT", "75"))
BREAKOUT_LOOKBACK  = int(os.getenv("BREAKOUT_LOOKBACK","20"))
INTRADAY_TIMEFRAME = os.getenv("INTRADAY_TIMEFRAME",   "15Min")
DAILY_TIMEFRAME    = os.getenv("DAILY_TIMEFRAME",      "1Day")

# ── V3 Early Trend & Filter Tuning (all configurable via .env) ────────────────
# These settings loosen the strategy to generate more high-quality entries.
ALLOW_EARLY_TREND_ENTRY       = os.getenv("ALLOW_EARLY_TREND_ENTRY",      "true").lower() == "true"
EARLY_TREND_MAX_SMA_GAP_PCT   = float(os.getenv("EARLY_TREND_MAX_SMA_GAP_PCT", "0.03"))  # SMA20 may be up to 3% below SMA50
SMA20_RISING_BARS             = int(os.getenv("SMA20_RISING_BARS",         "3"))           # SMA20 must have risen this many consecutive bars
MIN_VOLUME_RATIO              = float(os.getenv("MIN_VOLUME_RATIO",         "0.25"))        # 25% of 20-day avg — relaxed gate
REQUIRE_BREAKOUT_FOR_BUY      = os.getenv("REQUIRE_BREAKOUT_FOR_BUY",     "false").lower() == "true"
REQUIRE_INTRADAY_CONFIRMATION = os.getenv("REQUIRE_INTRADAY_CONFIRMATION", "true").lower() == "true"
REQUIRE_SPY_BULLISH           = os.getenv("REQUIRE_SPY_BULLISH",           "false").lower() == "true"

# ── Tiered SPY regime system ──────────────────────────────────────────────────
# Replaces hard REQUIRE_SPY_BULLISH blocking with a 3-tier market-regime filter.
# Bullish SPY  → all normal entries allowed.
# Neutral SPY  → allow only if score ≥ NEUTRAL_SPY_MIN_SCORE AND MACD improving.
# Bearish SPY  → block by default; allow exception only if ALL exception thresholds pass.
# Set REQUIRE_SPY_BULLISH=false (in .env) to activate the tiered system.
ALLOW_NEUTRAL_SPY_ENTRIES              = os.getenv("ALLOW_NEUTRAL_SPY_ENTRIES",              "true").lower() == "true"
NEUTRAL_SPY_MIN_SCORE                  = int(os.getenv("NEUTRAL_SPY_MIN_SCORE",              "70"))
BEARISH_SPY_EXCEPTION_MIN_SCORE        = int(os.getenv("BEARISH_SPY_EXCEPTION_MIN_SCORE",   "82"))
BEARISH_SPY_EXCEPTION_MIN_VOLUME_RATIO = float(os.getenv("BEARISH_SPY_EXCEPTION_MIN_VOLUME_RATIO", "0.08"))
BEARISH_SPY_EXCEPTION_REQUIRE_MACD    = os.getenv("BEARISH_SPY_EXCEPTION_REQUIRE_MACD",     "true").lower() == "true"
# Minimum volume ratio for neutral-SPY entries (stricter than normal MIN_VOLUME_RATIO).
NEUTRAL_SPY_MIN_VOLUME_RATIO          = float(os.getenv("NEUTRAL_SPY_MIN_VOLUME_RATIO",      "0.06"))
# No new entries after this ET time — only exits managed after this cutoff.
LAST_ENTRY_TIME                       = os.getenv("LAST_ENTRY_TIME",                         "11:00")

# ── V3.1 Quality & Diagnostic Enhancements (all configurable via .env) ────────
# Intraday tolerance: allow close to be within this % below intraday SMA20 and still pass
# when combined with ALLOW_STRONG_DAILY_WEAK_INTRADAY=true on a strong daily tier
INTRADAY_SMA_TOLERANCE_PCT         = float(os.getenv("INTRADAY_SMA_TOLERANCE_PCT",        "0.005"))  # 0.5%
ALLOW_STRONG_DAILY_WEAK_INTRADAY   = os.getenv("ALLOW_STRONG_DAILY_WEAK_INTRADAY",         "false").lower() == "true"
# Require MACD histogram to be rising (curr > prev) for early-trend entries — filters weakening momentum
EARLY_TREND_REQUIRE_MACD_IMPROVING = os.getenv("EARLY_TREND_REQUIRE_MACD_IMPROVING",       "true").lower() == "true"

# ── Entry quality scoring ──────────────────────────────────────────────────────
# Minimum 0-100 score for a new entry to be allowed. A=75, A+=85, B=65.
MIN_ENTRY_SCORE       = int(os.getenv("MIN_ENTRY_SCORE",         "75"))
# Set true to allow B-grade (65-74) setups to enter. Default false = A/A+ only.
ALLOW_B_SETUP_ENTRIES = os.getenv("ALLOW_B_SETUP_ENTRIES", "false").lower() == "true"

# ── Quality B setup mode ──────────────────────────────────────────────────────
# Explicit curated pathway for B-grade setups (score 65-74) under stricter conditions.
# When a B-grade setup meets ALL quality-B criteria it is tagged quality_b_allowed
# and proceeds to entry; failures are tagged quality_b_blocked for diagnostic clarity.
QUALITY_B_MIN_SCORE               = int(os.getenv("QUALITY_B_MIN_SCORE",               "68"))
QUALITY_B_MIN_VOLUME_RATIO        = float(os.getenv("QUALITY_B_MIN_VOLUME_RATIO",        "0.06"))
QUALITY_B_REQUIRE_MACD_IMPROVING  = os.getenv("QUALITY_B_REQUIRE_MACD_IMPROVING",        "true").lower() == "true"
QUALITY_B_REQUIRE_INTRADAY_GREEN  = os.getenv("QUALITY_B_REQUIRE_INTRADAY_GREEN",        "true").lower() == "true"
QUALITY_B_MAX_RSI                 = float(os.getenv("QUALITY_B_MAX_RSI",                 "76"))
QUALITY_B_ONLY_IF_SPY_NOT_BEARISH = os.getenv("QUALITY_B_ONLY_IF_SPY_NOT_BEARISH",      "true").lower() == "true"
QUALITY_B_REQUIRE_OPENING_RANGE_BREAK = os.getenv("QUALITY_B_REQUIRE_OPENING_RANGE_BREAK", "true").lower() == "true"

# ── Opening Range Breakout (ORB) ───────────────────────────────────────────────
# Block entries until price breaks above the first OPENING_RANGE_MINUTES of the session.
# A+ setups may bypass if OPENING_RANGE_ALLOW_A_PLUS_EXCEPTION=true, SPY bullish, strong vol, MACD.
OPENING_RANGE_ENABLED               = os.getenv("OPENING_RANGE_ENABLED",               "true").lower() == "true"
OPENING_RANGE_MINUTES               = int(os.getenv("OPENING_RANGE_MINUTES",            "20"))
OPENING_RANGE_REQUIRE_BREAK         = os.getenv("OPENING_RANGE_REQUIRE_BREAK",          "true").lower() == "true"
OPENING_RANGE_ALLOW_A_PLUS_EXCEPTION = os.getenv("OPENING_RANGE_ALLOW_A_PLUS_EXCEPTION","true").lower() == "true"

# ── Anti-chase / extended candle filter ───────────────────────────────────────
# Block entries when price is too extended from the intraday SMA20 (VWAP proxy).
# IEX feed does not provide VWAP directly; intraday SMA20 is used as the reference.
ANTI_CHASE_ENABLED                  = os.getenv("ANTI_CHASE_ENABLED",                  "true").lower() == "true"
MAX_INTRADAY_EXTENSION_PCT          = float(os.getenv("MAX_INTRADAY_EXTENSION_PCT",     "0.012"))
MAX_DISTANCE_FROM_VWAP_PCT          = float(os.getenv("MAX_DISTANCE_FROM_VWAP_PCT",     "0.018"))

# ── Session high pullback block ────────────────────────────────────────────────
# Block entries when price has fallen more than MAX_PULLBACK_FROM_SESSION_HIGH_PCT
# from its intraday high — prevents buying fading opening spikes.
SESSION_HIGH_PULLBACK_BLOCK_ENABLED = os.getenv("SESSION_HIGH_PULLBACK_BLOCK_ENABLED",  "true").lower() == "true"
MAX_PULLBACK_FROM_SESSION_HIGH_PCT  = float(os.getenv("MAX_PULLBACK_FROM_SESSION_HIGH_PCT", "0.008"))

# ── First-30-min caution mode ─────────────────────────────────────────────────
# Between 9:35 and 10:05 ET, require a higher score, stronger volume, and MACD confirmation.
# Intended to prevent premature entries into opening momentum that quickly reverses.
FIRST_30_MIN_CAUTION_ENABLED        = os.getenv("FIRST_30_MIN_CAUTION_ENABLED",        "true").lower() == "true"
FIRST_30_MIN_MIN_SCORE              = int(os.getenv("FIRST_30_MIN_MIN_SCORE",           "75"))
FIRST_30_MIN_MIN_VOLUME_RATIO       = float(os.getenv("FIRST_30_MIN_MIN_VOLUME_RATIO",  "0.12"))

# ── Strict crypto stocks filter ────────────────────────────────────────────────
# High-volatility crypto-correlated stocks (RIOT, MARA) require stricter conditions:
# higher score, stronger volume, and SPY must be bullish (not just neutral).
STRICT_CRYPTO_STOCKS                = os.getenv("STRICT_CRYPTO_STOCKS",                "true").lower() == "true"
STRICT_CRYPTO_MIN_SCORE             = int(os.getenv("STRICT_CRYPTO_MIN_SCORE",          "75"))
STRICT_CRYPTO_MIN_VOLUME_RATIO      = float(os.getenv("STRICT_CRYPTO_MIN_VOLUME_RATIO", "0.12"))
_strict_crypto_env                  = os.getenv("STRICT_CRYPTO_SYMBOLS",               "RIOT,MARA")
STRICT_CRYPTO_SYMBOLS_SET           = {s.strip().upper() for s in _strict_crypto_env.split(",") if s.strip()}

# ── Breakeven protection ───────────────────────────────────────────────────────
# When a position reaches BREAKEVEN_TRIGGER_GAIN_PCT unrealized gain, arm a virtual
# breakeven stop at entry + BREAKEVEN_BUFFER_PCT. Logged to session state; does not
# modify Alpaca bracket orders to avoid held_for_orders conflicts in paper mode.
BREAKEVEN_PROTECTION_ENABLED        = os.getenv("BREAKEVEN_PROTECTION_ENABLED",        "true").lower() == "true"
BREAKEVEN_TRIGGER_GAIN_PCT          = float(os.getenv("BREAKEVEN_TRIGGER_GAIN_PCT",    "0.01"))
BREAKEVEN_BUFFER_PCT                = float(os.getenv("BREAKEVEN_BUFFER_PCT",          "0.001"))

# ── Post-stop-loss cooldown ────────────────────────────────────────────────────
# After a stop-out: cooldown this symbol for STOP_LOSS_SYMBOL_COOLDOWN_MINUTES.
# After 2 stop-outs in the same session: pause all new entries for STOP_LOSS_MARKET_COOLDOWN_MINUTES.
STOP_LOSS_SYMBOL_COOLDOWN_MINUTES   = int(os.getenv("STOP_LOSS_SYMBOL_COOLDOWN_MINUTES","60"))
STOP_LOSS_MARKET_COOLDOWN_MINUTES   = int(os.getenv("STOP_LOSS_MARKET_COOLDOWN_MINUTES","15"))

# ── Execution safety & paper-trade realism (all configurable via .env) ────────
# Kill switch: when true, only position monitoring/protection runs — no new entries.
DISABLE_NEW_ENTRIES         = os.getenv("DISABLE_NEW_ENTRIES",         "false").lower() == "true"
# Slippage applied to paper-trade PnL: buy price * (1+slip), sell price * (1-slip).
SLIPPAGE_PCT                = float(os.getenv("SLIPPAGE_PCT",          "0.0005"))  # 5 bps
# Stale-data guard: skip new entries when latest bar is older than this many hours.
STALE_DATA_MAX_HOURS        = int(os.getenv("STALE_DATA_MAX_HOURS",    "72"))       # 3 trading days
# Symbol-level error cooldown: after N consecutive fetch errors, skip that symbol.
SYMBOL_ERROR_THRESHOLD      = int(os.getenv("SYMBOL_ERROR_THRESHOLD",  "3"))
SYMBOL_ERROR_COOLDOWN_MIN   = int(os.getenv("SYMBOL_ERROR_COOLDOWN_MIN", "60"))
# Observe-only mode: after N consecutive global API failures, disable new entries.
OBSERVE_ONLY_AFTER_FAILURES = int(os.getenv("OBSERVE_ONLY_AFTER_FAILURES", "10"))

# ── Spread filter ─────────────────────────────────────────────────────────────
# Reject new entries when bid/ask spread exceeds this fraction of mid-price.
MAX_SPREAD_PCT           = float(os.getenv("MAX_SPREAD_PCT",            "0.003"))  # 0.3%

# ── Partial take-profit system ────────────────────────────────────────────────
# At PARTIAL_TP_GAIN_PCT gain: sell PARTIAL_TP_SELL_FRAC of the position, move stop to breakeven.
PARTIAL_TP_GAIN_PCT      = float(os.getenv("PARTIAL_TP_GAIN_PCT",       "0.02"))   # +2%
PARTIAL_TP_SELL_FRAC     = float(os.getenv("PARTIAL_TP_SELL_FRAC",      "0.5"))    # sell 50%

# ── Weak momentum exit ────────────────────────────────────────────────────────
# After FORCE_EXIT_WEAK_AFTER ET, exit positions with gain < threshold AND weakening RSI+MACD.
FORCE_EXIT_WEAK_AFTER    = os.getenv("FORCE_EXIT_WEAK_AFTER",           "10:45")   # ET time
FORCE_EXIT_WEAK_GAIN_MAX = float(os.getenv("FORCE_EXIT_WEAK_GAIN_MAX",  "0.005"))  # 0.5% max gain

# ── Consecutive loss cooldown ─────────────────────────────────────────────────
MAX_CONSECUTIVE_LOSSES   = int(os.getenv("MAX_CONSECUTIVE_LOSSES",      "2"))
LOSS_COOLDOWN_MINUTES    = int(os.getenv("LOSS_COOLDOWN_MINUTES",       "30"))

# ── Stable v2 performance baseline ────────────────────────────────────────────
# Only trades entered on/after this date count toward stable_v2 performance metrics.
STABLE_V2_START_DATE     = os.getenv("STABLE_V2_START_DATE",            "2026-05-28")

# ── Session stats (counters reset on startup) ─────────────────────────────────
_session_scanned:  int = 0
_session_signals:  int = 0
_session_entered:  int = 0
_session_skipped:  int = 0
_session_blocked:  int = 0
_session_errors:   int = 0
_session_exited:   int = 0
# Grade breakdown for BUY candidates this session
_session_ap: int = 0  # A+ setups (score ≥ 85)
_session_a:  int = 0  # A  setups (score 75-84)
_session_b:  int = 0  # B  setups (score 65-74)
_session_c:  int = 0  # C  setups (score < 65) or non-BUY signals
# Near-miss tracking (score 60–69 but didn't enter)
_session_near_miss: int = 0
_near_miss_symbols: list = []  # [{symbol, score, grade, gaps}]
# Exit tracking — every close event this session (signal or auto-reconcile)
_session_exits: list = []      # [{symbol, reason, exit_price, entry_price, pnl, timestamp}]
_session_flattened: bool = False  # True once flatten-at-window-end has fired this session

# ── Runtime safety state (in-memory) ─────────────────────────────────────────
_last_trade_time: dict     = {}          # {symbol: datetime} — trade cooldown
_symbol_error_counts: dict = {}          # {symbol: int} — consecutive fetch errors
_symbol_error_cooldown: dict = {}        # {symbol: datetime} — error-cooldown expiry
_api_failure_count: int    = 0           # consecutive global API/data failures
_observe_only_mode: bool   = False       # set True when _api_failure_count exceeds threshold
_last_market_check: Optional[datetime] = None  # timestamp of last market-status call
_last_known_market_state: Optional[bool] = None  # result of last successful clock call
_session_start: datetime = datetime.now(timezone.utc)

# ── Scan loop tracking ────────────────────────────────────────────────────────
_last_scan_at: Optional[datetime] = None              # UTC timestamp of last /trade-watchlist call
_total_scan_cycles: int = 0                            # scan loops completed this session
_last_telegram_scan_summary_at: Optional[datetime] = None  # throttle scan Telegrams to ≤1/15 min

# ── Per-symbol daily trade count (MAX_TRADES_PER_SYMBOL enforcement) ──────────
_session_symbol_trade_count: dict = {}       # {symbol: int} entries today
_session_trade_date: str = ""               # date key for the above dict (YYYY-MM-DD)

# ── Hard daily loss shutdown (set once when limit hit; blocks entries all session) ──
_daily_loss_shutdown: bool = False

# ── Consecutive loss cooldown ─────────────────────────────────────────────────
_consecutive_losses: int = 0
_loss_cooldown_until: Optional[datetime] = None

# ── Partial TP tracking — prevent duplicate partials per symbol per session ───
_partial_tp_executed: set = set()   # symbols where partial TP fired this session

# ── Opening range cache (per symbol, formed once per session day) ─────────────
_opening_range: dict = {}     # {symbol: {"formed": bool, "high": float, "low": float}}

# ── Session high tracker ──────────────────────────────────────────────────────
_session_highs: dict = {}     # {symbol: float} — highest intraday price seen this session

# ── Post-stop-loss cooldown ───────────────────────────────────────────────────
_stop_loss_times: dict = {}               # {symbol: datetime} — last stop-out per symbol
_session_stop_count: int = 0              # stop-loss exits this session
_market_cooldown_until: Optional[datetime] = None  # market-wide pause after 2 stops

# ── Breakeven protection ───────────────────────────────────────────────────────
_breakeven_armed: set = set()             # symbols with breakeven triggered this session
_breakeven_stops: dict = {}              # {symbol: float} — desired breakeven stop price


# ── Shared headers ────────────────────────────────────────────────────────────
def _headers():
    return {
        "APCA-API-KEY-ID":     API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }


# ── Journal reconciliation ────────────────────────────────────────────────────
def _find_broker_closing_fill(
    symbol: str,
    stop_price: float = 0.0,
    tp_price: float = 0.0,
    after_iso: Optional[str] = None,
) -> Optional[dict]:
    """
    Search Alpaca's closed orders for the sell fill that actually closed `symbol`,
    instead of ever inventing an exit price. Shared by startup reconciliation
    (_reconcile_journal_state) and cycle-time bracket reconciliation so both use the
    same lookup/classification logic.

    Returns {"exit_price": float, "exit_reason": str, "order_id": str} when a filled
    sell order with a real fill price is found, or None when no reliable fill can be
    identified — callers must not fabricate a price in that case.
    """
    try:
        ord_resp = requests.get(
            f"{BASE_URL}/v2/orders",
            headers=_headers(),
            params={"status": "closed", "symbols": symbol, "limit": 20},
            timeout=10,
        )
    except Exception:
        return None
    if ord_resp.status_code != 200:
        return None

    try:
        orders = ord_resp.json()
    except Exception:
        return None

    filled_sells = [
        o for o in orders
        if o.get("status") == "filled" and o.get("side") == "sell"
    ]

    if after_iso:
        try:
            after_dt = datetime.fromisoformat(after_iso)
            filled_sells = [
                o for o in filled_sells
                if o.get("filled_at")
                and datetime.fromisoformat(str(o["filled_at"]).replace("Z", "+00:00")) >= after_dt
            ]
        except Exception:
            pass  # if timestamps can't be parsed, fall back to considering all filled sells

    if not filled_sells:
        return None

    # Alpaca returns closed orders most-recent-first — take the most relevant fill.
    last    = filled_sells[0]
    fill_px = float(last.get("filled_avg_price") or 0)
    if fill_px <= 0:
        return None

    order_type = str(last.get("type") or last.get("order_type") or "").lower()
    if stop_price > 0 and fill_px <= stop_price * 1.02:
        exit_reason = "stop_loss_hit"
    elif tp_price > 0 and fill_px >= tp_price * 0.98:
        exit_reason = "take_profit_hit"
    elif "stop" in order_type:
        exit_reason = "stop_loss_hit"
    elif "limit" in order_type:
        exit_reason = "take_profit_hit"
    elif order_type == "market":
        exit_reason = "market_close"
    else:
        exit_reason = "unclassified_exit"

    return {"exit_price": fill_px, "exit_reason": exit_reason, "order_id": last.get("id")}


def _reconcile_journal_state() -> list:
    """
    Compare open journal entries against real Alpaca positions.

    For any journal entry marked open for a symbol Alpaca no longer holds, this
    searches Alpaca's closed orders (via _find_broker_closing_fill) for the real
    closing fill before touching the journal:
      - a confirmed fill is found  -> close with that price, reason "reconciled_<type>",
        data_quality_status="verified"
      - no reliable fill is found  -> close with exit_price=None, exit_reason=
        "unresolved_reconciliation", data_quality_status="unresolved_reconciliation".
        No price is invented, and analytics excludes the row automatically.
    dry-run journal entries never had a real Alpaca order behind them in the first
    place, so they're cleared with exit_reason="reconcile_stale" and tagged
    data_quality_status="suspect_zero_exit" (also analytics-excluded).

    Called on startup to flush stale entries caused by:
    - dry-run trades that were recorded but never really opened
    - positions that closed externally while the bot was offline

    Returns a list of cleared symbol names.
    """
    cleared = []
    try:
        resp = requests.get(f"{BASE_URL}/v2/positions", headers=_headers(), timeout=10)
        alpaca_syms: set = set()
        if resp.status_code == 200:
            for p in resp.json():
                qty = float(p.get("qty", 0))
                if qty > 0:
                    alpaca_syms.add(str(p.get("symbol", "")).upper())
    except Exception as exc:
        print(f"[reconcile] WARNING: could not fetch Alpaca positions: {exc}")
        alpaca_syms = set()

    open_paper = journal.get_open_paper_positions()
    for pos in open_paper:
        sym = str(pos.get("symbol", "")).upper()

        if DRY_RUN:
            # In dry-run mode there are never real Alpaca positions or broker fills —
            # clear journal entries that survived a restart. No price is fabricated as
            # "real"; the row is tagged suspect so analytics excludes it.
            journal.close_paper_trade(
                sym, 0.0, "reconcile_stale",
                data_quality_status="suspect_zero_exit",
                data_quality_note="dry-run journal entry cleared at startup; no real broker fill exists",
            )
            cleared.append(sym)
            print(f"[reconcile] STATE RECONCILED: cleared stale dry-run journal position for {sym}")
            continue

        if sym not in alpaca_syms:
            fill = _find_broker_closing_fill(
                sym,
                stop_price=pos.get("stop_price") or 0.0,
                tp_price=pos.get("take_profit_price") or 0.0,
                after_iso=pos.get("entry_timestamp"),
            )
            if fill:
                journal.close_paper_trade(sym, fill["exit_price"], f"reconciled_{fill['exit_reason']}")
                print(
                    f"[reconcile] STATE RECONCILED: {sym} closed via confirmed broker fill "
                    f"@ {fill['exit_price']} ({fill['exit_reason']})"
                )
            else:
                journal.close_paper_trade(
                    sym, None, "unresolved_reconciliation",
                    data_quality_status="unresolved_reconciliation",
                    data_quality_note=(
                        "journal marked open but Alpaca had no matching position or "
                        "closing fill at startup reconciliation"
                    ),
                )
                print(
                    f"[reconcile] STATE RECONCILED: {sym} — no broker fill found, "
                    f"marked unresolved_reconciliation (no price fabricated)"
                )
            cleared.append(sym)

    if cleared:
        print(f"[reconcile] Cleared {len(cleared)} stale journal position(s): {cleared}")
    else:
        print("[reconcile] No stale journal positions found.")
    return cleared


# ── Cooldown helpers ──────────────────────────────────────────────────────────
def _is_in_cooldown(symbol: str) -> bool:
    last = _last_trade_time.get(symbol)
    if last is None:
        return False
    elapsed_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
    return elapsed_min < TRADE_COOLDOWN_MINUTES


def _record_trade_time(symbol: str):
    _last_trade_time[symbol] = datetime.now(timezone.utc)


# ── Symbol error cooldown helpers ─────────────────────────────────────────────
def _record_symbol_error(symbol: str):
    global _api_failure_count, _observe_only_mode
    _symbol_error_counts[symbol] = _symbol_error_counts.get(symbol, 0) + 1
    _api_failure_count += 1
    if _api_failure_count >= OBSERVE_ONLY_AFTER_FAILURES and not _observe_only_mode:
        _observe_only_mode = True
        print(
            f"[safety] OBSERVE-ONLY MODE activated after {_api_failure_count} "
            f"consecutive API failures — new entries disabled until failures clear"
        )
    if _symbol_error_counts[symbol] >= SYMBOL_ERROR_THRESHOLD:
        expiry = datetime.now(timezone.utc) + timedelta(minutes=SYMBOL_ERROR_COOLDOWN_MIN)
        _symbol_error_cooldown[symbol] = expiry
        print(
            f"[safety] {symbol} in ERROR COOLDOWN for {SYMBOL_ERROR_COOLDOWN_MIN}m "
            f"after {_symbol_error_counts[symbol]} consecutive errors"
        )


def _clear_symbol_error(symbol: str):
    global _api_failure_count, _observe_only_mode
    if _symbol_error_counts.get(symbol, 0) > 0:
        _symbol_error_counts[symbol] = 0
    if _api_failure_count > 0:
        _api_failure_count = max(0, _api_failure_count - 1)
    if _observe_only_mode and _api_failure_count < OBSERVE_ONLY_AFTER_FAILURES:
        _observe_only_mode = False
        print("[safety] OBSERVE-ONLY MODE cleared — API failures dropped below threshold")


def _is_symbol_error_cooldown(symbol: str) -> bool:
    expiry = _symbol_error_cooldown.get(symbol)
    if expiry is None:
        return False
    if datetime.now(timezone.utc) >= expiry:
        del _symbol_error_cooldown[symbol]
        _symbol_error_counts[symbol] = 0
        return False
    return True


# ── Stale-data guard ──────────────────────────────────────────────────────────
def _is_data_stale(df: pd.DataFrame):
    """
    Return (True, reason) if the most recent bar is older than STALE_DATA_MAX_HOURS.
    Approximation: uses bar timestamp from the 't' column; no intrabar precision.
    Returns (False, "") when data is fresh or timestamp unavailable (fail open).
    """
    if df.empty or "t" not in df.columns:
        return False, ""
    try:
        latest_ts_raw = df["t"].iloc[-1]
        if isinstance(latest_ts_raw, str):
            latest_ts = datetime.fromisoformat(latest_ts_raw.replace("Z", "+00:00"))
        else:
            latest_ts = pd.Timestamp(latest_ts_raw).to_pydatetime()
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - latest_ts).total_seconds() / 3600
        if age_hours > STALE_DATA_MAX_HOURS:
            _stale_reason = (
                f"Stale data: latest bar is {round(age_hours, 1)}h old "
                f"(threshold={STALE_DATA_MAX_HOURS}h)"
            )
            _log_evt("stale_data_warning", _stale_reason, severity="warning")
            return True, _stale_reason
    except Exception:
        pass  # fail open — do not block on timestamp parse errors
    return False, ""


# ── Consecutive loss streak tracking ─────────────────────────────────────────

def _update_loss_streak(pnl: Optional[float]):
    """
    Update the consecutive loss counter after every realized exit.
    Winning trades reset the streak. Losing trades increment it and may
    activate the loss cooldown when MAX_CONSECUTIVE_LOSSES is hit.
    """
    global _consecutive_losses, _loss_cooldown_until
    if pnl is None:
        return
    if pnl <= 0:
        _consecutive_losses += 1
        if _consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            _loss_cooldown_until = (
                datetime.now(timezone.utc) + timedelta(minutes=LOSS_COOLDOWN_MINUTES)
            )
            print(
                f"[loss_streak] reason=loss_cooldown_active | "
                f"consecutive_losses={_consecutive_losses} | "
                f"cooldown_minutes={LOSS_COOLDOWN_MINUTES} | "
                f"resumes_at={_loss_cooldown_until.isoformat()}"
            )
    else:
        if _consecutive_losses > 0:
            print(
                f"[loss_streak] Win recorded — resetting streak from {_consecutive_losses} to 0"
            )
        _consecutive_losses = 0


def _is_loss_cooldown_active() -> bool:
    """Return True when a consecutive-loss cooldown is in effect."""
    global _consecutive_losses, _loss_cooldown_until
    if _loss_cooldown_until is None:
        return False
    if datetime.now(timezone.utc) >= _loss_cooldown_until:
        print(
            f"[loss_streak] reason=loss_cooldown_expired | "
            f"consecutive_losses reset to 0 | "
            f"new entries re-enabled"
        )
        _loss_cooldown_until = None
        _consecutive_losses = 0
        return False
    return True


# ── Opening Range Breakout helpers ────────────────────────────────────────────

def _get_or_build_opening_range(symbol: str) -> dict:
    """
    Build or return cached opening range for symbol.
    Opening range = high/low of the first OPENING_RANGE_MINUTES after 9:30 ET.
    Returns {"formed": bool, "high": float|None, "low": float|None}.
    formed=False when the range period has not yet elapsed or data is unavailable.
    Result is cached per symbol so subsequent calls in the same session are free.
    """
    global _opening_range
    if not OPENING_RANGE_ENABLED:
        return {"formed": False, "high": None, "low": None}

    ET = ZoneInfo("America/New_York")
    now_et = datetime.now(ET)
    market_open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    range_end_et   = market_open_et + timedelta(minutes=OPENING_RANGE_MINUTES)

    # Range not yet elapsed — do not block here, caller decides
    if now_et < range_end_et:
        return {"formed": False, "high": None, "low": None}

    # Return cached result when already formed (doesn't change during the session)
    cached = _opening_range.get(symbol)
    if cached and cached.get("formed"):
        return cached

    # Fetch 5-min bars covering the opening range window
    try:
        market_open_utc = market_open_et.astimezone(timezone.utc)
        range_end_utc   = range_end_et.astimezone(timezone.utc)
        url  = f"{DATA_URL}/v2/stocks/{symbol}/bars"
        params = {
            "timeframe": "5Min",
            "start":     market_open_utc.isoformat(),
            "end":       range_end_utc.isoformat(),
            "limit":     20,
            "feed":      "iex",
            "sort":      "asc",
        }
        resp = requests.get(url, headers=_headers(), params=params, timeout=8)
        if resp.status_code != 200:
            return {"formed": False, "high": None, "low": None}
        bars = resp.json().get("bars", [])
        if not bars:
            return {"formed": False, "high": None, "low": None}
        range_high = max(float(b["h"]) for b in bars)
        range_low  = min(float(b["l"]) for b in bars)
        result = {"formed": True, "high": round(range_high, 4), "low": round(range_low, 4)}
        _opening_range[symbol] = result
        print(
            f"[orb] {symbol} | opening_range formed | "
            f"high={result['high']} low={result['low']} | "
            f"range_minutes={OPENING_RANGE_MINUTES} | bars={len(bars)}"
        )
        return result
    except Exception as exc:
        print(f"[orb] {symbol} | WARNING: could not build opening range: {exc} — fail open")
        return {"formed": False, "high": None, "low": None}


# ── Anti-chase / extension filter ─────────────────────────────────────────────

def _check_anti_chase(symbol: str, current_price: float) -> tuple:
    """
    Return (passes: bool, reason: str, extension_pct: float).
    Blocks entry when price is more than MAX_INTRADAY_EXTENSION_PCT above
    the intraday SMA20 (used as VWAP proxy — IEX feed does not provide VWAP).
    Fails open (passes) when data is unavailable.
    Also updates _session_highs for this symbol from latest intraday bar high.
    """
    global _session_highs
    if not ANTI_CHASE_ENABLED:
        return True, "anti_chase_disabled", 0.0

    df = _fetch_bars(symbol, timeframe=INTRADAY_TIMEFRAME, days=3, limit=60)
    if df.empty or len(df) < 5:
        return True, "anti_chase_data_unavailable (fail open)", 0.0

    # Update session high from intraday bar highs while we have the data
    if "h" in df.columns:
        intraday_high = float(df["h"].max())
        if intraday_high > _session_highs.get(symbol, 0.0):
            _session_highs[symbol] = intraday_high

    n = min(20, len(df))
    intraday_sma = float(df["c"].tail(n).mean())
    if intraday_sma <= 0:
        return True, "anti_chase_invalid_sma (fail open)", 0.0

    extension_pct = (current_price - intraday_sma) / intraday_sma
    if extension_pct > MAX_INTRADAY_EXTENSION_PCT:
        return False, (
            f"price ${current_price:.2f} is {extension_pct*100:.2f}% above "
            f"intraday SMA={intraday_sma:.2f} (max={MAX_INTRADAY_EXTENSION_PCT*100:.1f}%)"
        ), round(extension_pct, 4)

    return True, (
        f"anti_chase ok: +{extension_pct*100:.2f}% from intraday SMA=${intraday_sma:.2f}"
    ), round(extension_pct, 4)


# ── Session high pullback helpers ─────────────────────────────────────────────

def _update_session_high(symbol: str, price: float):
    """Update session high for a symbol from any available price."""
    global _session_highs
    if price > 0 and price > _session_highs.get(symbol, 0.0):
        _session_highs[symbol] = price


def _check_falling_from_session_high(symbol: str, current_price: float) -> tuple:
    """
    Return (passes: bool, reason: str, pullback_pct: float).
    Blocks when price has fallen more than MAX_PULLBACK_FROM_SESSION_HIGH_PCT
    from the session high — prevents buying fading opening spikes.
    Fails open when no session high is tracked yet.
    """
    if not SESSION_HIGH_PULLBACK_BLOCK_ENABLED:
        return True, "session_high_pullback_disabled", 0.0

    session_high = _session_highs.get(symbol, 0.0)
    if session_high <= 0 or current_price <= 0:
        return True, "session_high_not_tracked (fail open)", 0.0

    pullback_pct = (session_high - current_price) / session_high
    if pullback_pct > MAX_PULLBACK_FROM_SESSION_HIGH_PCT:
        return False, (
            f"price ${current_price:.2f} is {pullback_pct*100:.2f}% below "
            f"session high ${session_high:.2f} (max pullback={MAX_PULLBACK_FROM_SESSION_HIGH_PCT*100:.1f}%)"
        ), round(pullback_pct, 4)

    return True, (
        f"session_high ok: {pullback_pct*100:.2f}% below ${session_high:.2f}"
    ), round(pullback_pct, 4)


# ── Post-stop-loss cooldown helpers ───────────────────────────────────────────

def _record_stop_loss_event(symbol: str):
    """
    Called when a stop_loss_hit exit fires for a symbol.
    Activates per-symbol cooldown and, after 2 stops, a market-wide pause.
    """
    global _stop_loss_times, _session_stop_count, _market_cooldown_until
    now = datetime.now(timezone.utc)
    _stop_loss_times[symbol] = now
    _session_stop_count += 1
    print(
        f"[stop_cooldown] {symbol} | stop-out recorded | "
        f"session_stop_count={_session_stop_count} | "
        f"symbol_cooldown={STOP_LOSS_SYMBOL_COOLDOWN_MINUTES}m"
    )
    if _session_stop_count >= 2 and _market_cooldown_until is None:
        _market_cooldown_until = now + timedelta(minutes=STOP_LOSS_MARKET_COOLDOWN_MINUTES)
        print(
            f"[stop_cooldown] MARKET COOLDOWN activated after {_session_stop_count} stops | "
            f"new entries paused {STOP_LOSS_MARKET_COOLDOWN_MINUTES}m | "
            f"resumes at {_market_cooldown_until.isoformat()}"
        )


def _is_post_stop_symbol_cooldown(symbol: str) -> bool:
    """Return True when this symbol is in post-stop-loss cooldown."""
    stop_time = _stop_loss_times.get(symbol)
    if stop_time is None:
        return False
    elapsed_min = (datetime.now(timezone.utc) - stop_time).total_seconds() / 60
    return elapsed_min < STOP_LOSS_SYMBOL_COOLDOWN_MINUTES


def _is_market_stop_cooldown_active() -> bool:
    """Return True when the market-wide stop cooldown is in effect."""
    global _market_cooldown_until
    if _market_cooldown_until is None:
        return False
    if datetime.now(timezone.utc) >= _market_cooldown_until:
        print(f"[stop_cooldown] Market cooldown expired — new entries re-enabled")
        _market_cooldown_until = None
        return False
    return True


def _is_past_last_entry_time() -> bool:
    """Return True when ET time is at or after LAST_ENTRY_TIME — no new entries after this."""
    try:
        parts = LAST_ENTRY_TIME.split(":")
        cutoff = dt_time(int(parts[0]), int(parts[1]))
    except Exception:
        cutoff = dt_time(11, 0)
    return datetime.now(ZoneInfo("America/New_York")).time() >= cutoff


def _is_run_bot_active() -> bool:
    """Return True when run_bot.py appears to be running (PID file present and process alive)."""
    pid_file = os.path.join(os.path.dirname(__file__), "bot.pid")
    try:
        if not os.path.exists(pid_file):
            return False
        with open(pid_file) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # signal 0 = existence check only
        return True
    except Exception:
        return False


def _entries_allowed_now() -> bool:
    """Return True when all session-level entry guards are currently clear."""
    return not (
        DISABLE_NEW_ENTRIES
        or _daily_loss_shutdown
        or _is_loss_cooldown_active()
        or _observe_only_mode
        or _is_past_last_entry_time()
    )


# ── Spread filter helper ──────────────────────────────────────────────────────

def _fetch_bid_ask_spread(symbol: str) -> tuple:
    """
    Fetch the latest IEX quote and compute bid/ask spread as a fraction of mid-price.
    Returns (spread_pct, bid, ask) or (None, None, None) on any error.
    Caller must treat None as 'filter skipped — fail open'.
    """
    url = f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest"
    try:
        resp = requests.get(
            url, headers=_headers(), params={"feed": "iex"}, timeout=5
        )
        if resp.status_code != 200:
            return None, None, None
        quote = resp.json().get("quote", {})
        bid = float(quote.get("bp", 0) or 0)
        ask = float(quote.get("ap", 0) or 0)
        if bid <= 0 or ask <= 0:
            return None, None, None
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid if mid > 0 else None
        return (
            round(spread_pct, 6) if spread_pct is not None else None,
            round(bid, 4),
            round(ask, 4),
        )
    except Exception:
        return None, None, None


# ── Weak momentum exit helper ─────────────────────────────────────────────────

def _check_weak_momentum_exit(sym: str, ep: float, cur_price: float) -> bool:
    """
    Return True when a stalling position should be exited early.
    Conditions (all must hold):
      - Current ET time >= FORCE_EXIT_WEAK_AFTER
      - Unrealized gain < FORCE_EXIT_WEAK_GAIN_MAX
      - Intraday RSI is weakening (falling vs prior bar)
      - Intraday MACD histogram is weakening (falling vs prior bar)
    Fails open (returns False) when indicator data is unavailable.
    """
    ET = ZoneInfo("America/New_York")
    now_et = datetime.now(ET)
    try:
        parts = FORCE_EXIT_WEAK_AFTER.split(":")
        weak_time = dt_time(int(parts[0]), int(parts[1]))
    except Exception:
        weak_time = dt_time(10, 45)

    if now_et.time() < weak_time:
        return False  # still inside the prime entry window
    if ep <= 0:
        return False

    gain_pct = (cur_price - ep) / ep
    if gain_pct >= FORCE_EXIT_WEAK_GAIN_MAX:
        return False  # strong runner — let it run

    # Fetch intraday bars to assess momentum direction
    df = _fetch_bars(sym, timeframe=INTRADAY_TIMEFRAME, days=5, limit=50)
    if df.empty or len(df) < 20:
        return False  # no data — fail open

    try:
        closes = df["c"]
        rsi = _rsi_series(closes, 14)
        ml, sl = _macd_series(closes)
        macd_hist = ml - sl

        rsi_now   = float(rsi.iloc[-1])
        rsi_prev  = float(rsi.iloc[-2])
        hist_now  = float(macd_hist.iloc[-1])
        hist_prev = float(macd_hist.iloc[-2])

        if any(pd.isna(v) for v in [rsi_now, rsi_prev, hist_now, hist_prev]):
            return False

        rsi_weakening  = rsi_now < rsi_prev
        macd_weakening = hist_now < hist_prev

        if rsi_weakening and macd_weakening:
            print(
                f"[weak_exit] {sym} | reason=weak_momentum_exit | "
                f"gain={gain_pct*100:.2f}% < {FORCE_EXIT_WEAK_GAIN_MAX*100:.1f}% threshold | "
                f"RSI={rsi_now:.1f}<{rsi_prev:.1f} (weakening) | "
                f"MACD_hist={hist_now:.4f}<{hist_prev:.4f} (weakening) | "
                f"time={now_et.strftime('%H:%M ET')}"
            )
            return True
    except Exception:
        pass  # fail open — never exit without confirmed data

    return False


# ── Open position count ───────────────────────────────────────────────────────
def _count_open_long_positions() -> int:
    # In dry-run mode no real Alpaca orders are submitted, so Alpaca positions
    # will always be empty. Count journal open trades instead so MAX_OPEN_POSITIONS
    # is correctly enforced during paper sessions.
    if DRY_RUN:
        return len(journal.get_open_paper_positions())
    url = f"{BASE_URL}/v2/positions"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        if response.status_code != 200:
            return 0
        positions = response.json()
        return sum(1 for p in positions if float(p.get("qty", 0)) > 0)
    except Exception:
        return 0


# ── Daily loss limit ──────────────────────────────────────────────────────────
def _is_daily_loss_limit_reached():
    """
    Check if today's equity loss exceeds DAILY_LOSS_LIMIT_PCT.
    Fails open (returns False) on API errors so trading is not blocked.
    """
    url = f"{BASE_URL}/v2/account"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        data = response.json()
        equity      = float(data.get("equity",      0))
        last_equity = float(data.get("last_equity", 0))
    except Exception:
        return False, ""

    if last_equity <= 0:
        return False, ""

    daily_change_pct = (equity - last_equity) / last_equity
    if daily_change_pct <= -DAILY_LOSS_LIMIT_PCT:
        loss_pct = round(abs(daily_change_pct) * 100, 2)
        reason = (
            f"Daily loss limit reached — {loss_pct}% loss today "
            f"(limit: {DAILY_LOSS_LIMIT_PCT * 100}%) — new entries disabled"
        )
        return True, reason

    return False, ""


# ── Generic bar fetcher ───────────────────────────────────────────────────────
def _fetch_bars(
    symbol: str,
    timeframe: str = "1Day",
    days: int = 180,
    limit: int = 200,
) -> pd.DataFrame:
    """Fetch OHLCV bars from Alpaca. Returns empty DataFrame on any error."""
    url   = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    params = {
        "timeframe": timeframe,
        "start":     start.isoformat(),
        "end":       end.isoformat(),
        "limit":     limit,
        "feed":      "iex",
        "sort":      "asc",
    }
    try:
        response = requests.get(url, headers=_headers(), params=params, timeout=10)
        response.raise_for_status()
        bars = response.json().get("bars", [])
        if not bars or not isinstance(bars, list):
            _record_symbol_error(symbol)
            return pd.DataFrame()
        _clear_symbol_error(symbol)
        return pd.DataFrame(bars)
    except Exception:
        _record_symbol_error(symbol)
        return pd.DataFrame()


# ── Technical indicator helpers ───────────────────────────────────────────────
def _rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    """Compute RSI series using Wilder's EMA smoothing. RSI=100 when there are no losses."""
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.where(avg_loss > 0, other=1e-10)
    rsi      = 100 - (100 / (1 + rs))
    return rsi.where(avg_loss > 0, other=100.0)


def _macd_series(closes: pd.Series):
    """Return (macd_line, signal_line) as pandas Series."""
    ema12       = closes.ewm(span=12, adjust=False).mean()
    ema26       = closes.ewm(span=26, adjust=False).mean()
    macd_line   = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal_line


# ── Raw signal computation (daily bars, no SPY / MTF) ────────────────────────
def _compute_raw_signal(symbol: str) -> dict:
    """
    Fetch daily bars and compute all V3 technical indicators:
      - SMA20 / SMA50 crossover
      - Trend strength filter
      - Volume confirmation
      - RSI overbought filter
      - MACD bullish filter
      - Breakout confirmation (close > N-bar high)

    Does NOT apply the SPY market-direction or intraday MTF filters.
    """
    df = _fetch_bars(symbol, timeframe="1Day", days=180, limit=200)

    if df.empty:
        return {"error": "No bar data returned"}

    try:
        df["sma_20"] = df["c"].rolling(window=20).mean()
        df["sma_50"] = df["c"].rolling(window=50).mean()

        if "v" in df.columns:
            df["vol_sma_20"] = df["v"].rolling(window=20).mean()

        latest = df.iloc[-1]

        if pd.isna(latest["sma_20"]) or pd.isna(latest["sma_50"]):
            return {"error": "Not enough bars to calculate SMA values (need at least 50)"}

        close        = float(latest["c"])
        sma_20       = float(latest["sma_20"])
        sma_50       = float(latest["sma_50"])
        trend_strength = abs(sma_20 - sma_50) / sma_50
        trend_strong   = trend_strength >= MIN_TREND_STRENGTH

        # Volume confirmation
        vol_sma20_val  = latest.get("vol_sma_20") if "vol_sma_20" in df.columns else None
        has_volume     = vol_sma20_val is not None and not pd.isna(vol_sma20_val)
        if has_volume:
            current_volume   = int(latest["v"])
            vol_sma_20       = float(vol_sma20_val)
            # Relaxed: volume only needs to reach MIN_VOLUME_RATIO of the 20-day average
            volume_confirmed = current_volume >= vol_sma_20 * MIN_VOLUME_RATIO
        else:
            current_volume   = None
            vol_sma_20       = None
            volume_confirmed = True  # fail open when data unavailable

        # RSI
        rsi    = None
        rsi_ok = True
        if len(df) >= RSI_PERIOD + 1:
            try:
                rsi_val = float(_rsi_series(df["c"], RSI_PERIOD).iloc[-1])
                if not pd.isna(rsi_val):
                    rsi    = round(rsi_val, 2)
                    rsi_ok = rsi < RSI_OVERBOUGHT
            except Exception:
                pass  # fail open

        # MACD — also compute histogram and histogram direction for early-trend quality check
        macd_line_val             = None
        macd_signal_val           = None
        macd_bullish              = True
        macd_histogram            = None
        macd_histogram_rising     = True   # fail open — don't block when data is unavailable
        macd_hist_improving_2bars = False  # conservative default — 2-bar check requires 37+ bars
        if len(df) >= 35:
            try:
                ml, sl = _macd_series(df["c"])
                ml_v, sl_v = float(ml.iloc[-1]), float(sl.iloc[-1])
                if not pd.isna(ml_v) and not pd.isna(sl_v):
                    macd_line_val         = round(ml_v, 4)
                    macd_signal_val       = round(sl_v, 4)
                    macd_bullish          = macd_line_val > macd_signal_val
                    macd_histogram        = round(ml_v - sl_v, 4)
                    # Histogram direction: is momentum accelerating vs previous bar?
                    if len(df) >= 36:
                        ml_prev, sl_prev = float(ml.iloc[-2]), float(sl.iloc[-2])
                        if not pd.isna(ml_prev) and not pd.isna(sl_prev):
                            hist_prev             = round(ml_prev - sl_prev, 4)
                            macd_histogram_rising = macd_histogram > hist_prev
                            # 2-bar consecutive improvement: hist[-1] > hist[-2] > hist[-3]
                            if len(df) >= 37:
                                ml_prev2, sl_prev2 = float(ml.iloc[-3]), float(sl.iloc[-3])
                                if not pd.isna(ml_prev2) and not pd.isna(sl_prev2):
                                    hist_prev2 = round(ml_prev2 - sl_prev2, 4)
                                    macd_hist_improving_2bars = (
                                        macd_histogram > hist_prev and hist_prev > hist_prev2
                                    )
            except Exception:
                pass  # fail open

        # Breakout: close must exceed the highest close of the prior BREAKOUT_LOOKBACK bars
        breakout_high      = None
        breakout_confirmed = True
        if len(df) >= BREAKOUT_LOOKBACK + 1:
            past_highs    = df["c"].iloc[-(BREAKOUT_LOOKBACK + 1):-1]
            breakout_high = round(float(past_highs.max()), 2)
            breakout_confirmed = close > breakout_high

        # ── Two-tier entry model ──────────────────────────────────────────────
        # Tier 1 — Strong trend: price above SMA20 and SMA20 crossed above SMA50.
        strong_trend_buy = close > sma_20 and sma_20 > sma_50

        # Tier 2 — Early trend: price above SMA20, SMA20 within EARLY_TREND_MAX_SMA_GAP_PCT
        # of SMA50, and SMA20 has been rising for SMA20_RISING_BARS consecutive bars.
        # Lets the bot enter a developing crossover before it fully completes.
        early_trend_buy   = False
        sma20_is_rising   = False
        sma_gap_pct       = 0.0
        macd_improving_ok = True  # default open — only meaningful inside early-trend check
        if ALLOW_EARLY_TREND_ENTRY and not strong_trend_buy and close > sma_20:
            sma_gap_pct = (sma_50 - sma_20) / sma_50  # positive when SMA20 is below SMA50
            if sma_gap_pct <= EARLY_TREND_MAX_SMA_GAP_PCT:
                # SMA20 must have risen every bar for the last SMA20_RISING_BARS bars
                if len(df) >= SMA20_RISING_BARS + 1:
                    sma20_slice = df["sma_20"].iloc[-(SMA20_RISING_BARS + 1):]
                    sma20_is_rising = all(
                        sma20_slice.iloc[i] < sma20_slice.iloc[i + 1]
                        for i in range(len(sma20_slice) - 1)
                    )
                # Early-trend quality gate: require MACD histogram to be improving
                # (current bar histogram > previous bar histogram).
                # This prevents entering pre-crossover setups with decelerating momentum.
                if EARLY_TREND_REQUIRE_MACD_IMPROVING:
                    macd_improving_ok = macd_histogram_rising
                early_trend_buy = sma20_is_rising and macd_improving_ok

        # Determine base signal and entry tier
        entry_tier = None
        if strong_trend_buy:
            entry_tier    = "strong"
            base_signal   = "BUY"
            signal_reason = (
                f"Strong trend BUY: close={round(close,2)} above SMA20={round(sma_20,2)}"
                f" and SMA20 above SMA50={round(sma_50,2)}"
            )
        elif early_trend_buy:
            entry_tier    = "early"
            base_signal   = "BUY"
            signal_reason = (
                f"Early trend BUY: close above SMA20={round(sma_20,2)}, SMA20 rising"
                f" {SMA20_RISING_BARS} bars, gap to SMA50={round(sma_gap_pct*100,2)}%"
                f" (max={EARLY_TREND_MAX_SMA_GAP_PCT*100}%),"
                f" MACD hist={'rising ✓' if macd_histogram_rising else 'flat'}"
            )
        elif close < sma_20:
            base_signal   = "SELL"
            signal_reason = f"Close={round(close,2)} below SMA20={round(sma_20,2)} — exit signal"
        else:
            base_signal = "HOLD"
            # Give a precise reason so it's clear exactly what needs to change to get a BUY
            if not ALLOW_EARLY_TREND_ENTRY:
                signal_reason = (
                    f"HOLD: price above SMA20={round(sma_20,2)} but SMA20"
                    f" below SMA50={round(sma_50,2)} (early-trend entry disabled)"
                )
            else:
                gap_pct_display = round((sma_50 - sma_20) / sma_50 * 100, 2)
                if gap_pct_display > EARLY_TREND_MAX_SMA_GAP_PCT * 100:
                    signal_reason = (
                        f"HOLD: SMA20={round(sma_20,2)} is {gap_pct_display}% below"
                        f" SMA50={round(sma_50,2)} — gap exceeds"
                        f" {round(EARLY_TREND_MAX_SMA_GAP_PCT*100,1)}% threshold"
                    )
                elif not sma20_is_rising:
                    signal_reason = (
                        f"HOLD: SMA20 gap acceptable ({round((sma_50-sma_20)/sma_50*100,2)}%)"
                        f" but SMA20 not rising for {SMA20_RISING_BARS} consecutive bars"
                    )
                elif not macd_improving_ok:
                    signal_reason = (
                        f"HOLD: SMA20 rising {SMA20_RISING_BARS} bars, gap={round(sma_gap_pct*100,2)}% acceptable"
                        f" but MACD histogram not improving (momentum decelerating —"
                        f" hist={macd_histogram})"
                    )
                else:
                    signal_reason = (
                        f"HOLD: price above SMA20={round(sma_20,2)} but SMA20"
                        f" below SMA50={round(sma_50,2)}"
                    )

        # ── Apply BUY filters — first failure wins ────────────────────────────
        signal = base_signal
        if base_signal == "BUY":
            # Trend strength — only enforced for strong-trend entries.
            # Early-trend uses SMA20 rising as its momentum qualifier instead.
            if entry_tier == "strong" and not trend_strong:
                signal        = "HOLD"
                signal_reason = (
                    f"Blocked: weak trend strength={round(trend_strength,4)}"
                    f" < min={MIN_TREND_STRENGTH}"
                )

            # Volume gate — volume must reach at least MIN_VOLUME_RATIO of the 20-day average
            if signal == "BUY" and not volume_confirmed:
                signal        = "HOLD"
                signal_reason = (
                    f"Blocked: volume {current_volume:,} < {int(MIN_VOLUME_RATIO*100)}%"
                    f" of 20d avg (need {int(vol_sma_20 * MIN_VOLUME_RATIO):,},"
                    f" got {current_volume:,})"
                )

            if signal == "BUY" and rsi is not None and not rsi_ok:
                signal        = "HOLD"
                signal_reason = (
                    f"Blocked: RSI={rsi} overbought (threshold={RSI_OVERBOUGHT})"
                )

            if signal == "BUY" and macd_line_val is not None and not macd_bullish:
                # Relaxation gate: bearish MACD can be overridden when histogram is
                # improving on 2 consecutive bars AND RSI/volume conditions are met.
                # Requires EARLY_TREND_REQUIRE_MACD_IMPROVING=true.
                _rsi_in_range = rsi is not None and 50 <= rsi <= 72
                _macd_relax   = (
                    EARLY_TREND_REQUIRE_MACD_IMPROVING
                    and macd_hist_improving_2bars
                    and _rsi_in_range
                    and volume_confirmed
                )
                if _macd_relax:
                    print(
                        f"[macd_relax] {symbol} | reason=macd_improving_allowed | "
                        f"hist={macd_histogram} | RSI={rsi} | vol_confirmed={volume_confirmed}"
                    )
                    signal_reason = (
                        f"{signal_reason} | MACD bearish but histogram improving 2 bars"
                        f" (hist={macd_histogram}, RSI={rsi}) | reason=macd_improving_allowed"
                    )
                else:
                    if EARLY_TREND_REQUIRE_MACD_IMPROVING:
                        _not_why = (
                            "hist_not_improving_2bars" if not macd_hist_improving_2bars
                            else "rsi_out_of_range" if not _rsi_in_range
                            else "volume_not_confirmed"
                        )
                        _block_tag = f" | reason=macd_bearish_not_improving ({_not_why})"
                    else:
                        _block_tag = ""
                    signal        = "HOLD"
                    signal_reason = (
                        f"Blocked: bearish MACD (line={macd_line_val} < signal={macd_signal_val})"
                        f"{_block_tag}"
                    )

            # Breakout only blocks when REQUIRE_BREAKOUT_FOR_BUY=true (default false)
            if signal == "BUY" and REQUIRE_BREAKOUT_FOR_BUY and not breakout_confirmed:
                signal        = "HOLD"
                signal_reason = (
                    f"Blocked: breakout not confirmed"
                    f" (close={round(close,2)} <= {BREAKOUT_LOOKBACK}d high={breakout_high})"
                )

        return {
            "symbol":             symbol,
            "close":              round(close, 2),
            "sma_20":             round(sma_20, 2),
            "sma_50":             round(sma_50, 2),
            "trend_strength":     round(trend_strength, 4),
            "trend_strong":       trend_strong,
            "entry_tier":         entry_tier,   # "strong", "early", or None
            "current_volume":     current_volume,
            "vol_sma_20":         round(vol_sma_20, 0) if vol_sma_20 is not None else None,
            "volume_confirmed":   volume_confirmed,
            "rsi":                rsi,
            "rsi_ok":             rsi_ok,
            "macd_line":              macd_line_val,
            "macd_signal_line":       macd_signal_val,
            "macd_bullish":           macd_bullish,
            "macd_histogram":             macd_histogram,
            "macd_histogram_rising":      macd_histogram_rising,
            "macd_hist_improving_2bars":  macd_hist_improving_2bars,
            "breakout_high":          breakout_high,
            "breakout_confirmed": breakout_confirmed,
            "signal":             signal,
            "signal_reason":      signal_reason,
        }
    except Exception as e:
        return {"error": f"Failed to calculate signal: {str(e)}"}


# ── SPY market direction filter ───────────────────────────────────────────────
def _is_spy_bullish():
    """
    Return (True, reason) when SPY raw signal is BUY.
    Fails open on data errors so trading is not blocked.
    """
    spy_data = _compute_raw_signal("SPY")
    if "error" in spy_data:
        return True, f"SPY data unavailable ({spy_data['error']}) — market filter skipped"
    if spy_data["signal"] == "BUY":
        return True, "SPY is bullish"
    return False, f"Blocked by market filter (SPY signal={spy_data['signal']})"


def _get_spy_regime():
    """
    Return (regime, reason, spy_rsi, spy_macd_bullish, spy_macd_hist_rising).
    regime ∈ {"bullish", "neutral", "bearish"}.
    BUY → bullish, SELL → bearish, HOLD → neutral.
    Data errors → neutral (fail open — avoids over-blocking on connectivity issues).
    """
    spy_data = _compute_raw_signal("SPY")
    if "error" in spy_data:
        return (
            "neutral",
            f"SPY data unavailable ({spy_data['error']}) — regime neutral (fail open)",
            None, True, True,
        )
    sig              = spy_data.get("signal", "HOLD")
    spy_rsi          = spy_data.get("rsi")
    macd_bullish     = spy_data.get("macd_bullish", True)
    macd_hist_rising = spy_data.get("macd_histogram_rising", True)
    if sig == "BUY":
        return "bullish", "SPY bullish (BUY — above SMA20/SMA50 crossover)",  spy_rsi, macd_bullish, macd_hist_rising
    elif sig == "SELL":
        return "bearish", "SPY bearish (SELL — close below SMA20)",            spy_rsi, macd_bullish, macd_hist_rising
    return     "neutral", f"SPY neutral/HOLD (signal={sig})",                  spy_rsi, macd_bullish, macd_hist_rising


# ── Intraday multi-timeframe confirmation ─────────────────────────────────────
def _get_intraday_confirmation(symbol: str, entry_tier: str = None):
    """
    Check if intraday price is above its 20-bar SMA on INTRADAY_TIMEFRAME.

    Supports a configurable tolerance so a strong daily setup isn't hard-blocked by
    a small intraday lag:
      - If close is within INTRADAY_SMA_TOLERANCE_PCT below intraday SMA20 AND
        ALLOW_STRONG_DAILY_WEAK_INTRADAY=true AND daily tier is "strong" → marginal pass.
      - Otherwise any close below intraday SMA20 is a fail.

    Returns (confirmed: bool, reason: str, margin_pct: float).
      margin_pct is positive when close > SMA20, negative when below.
    Fails open on data errors.
    """
    df = _fetch_bars(symbol, timeframe=INTRADAY_TIMEFRAME, days=10, limit=100)
    if df.empty or len(df) < 20:
        return True, f"Intraday data unavailable ({INTRADAY_TIMEFRAME}) — MTF filter skipped", 0.0

    df["sma_20"] = df["c"].rolling(window=20).mean()
    latest = df.iloc[-1]

    if pd.isna(latest["sma_20"]):
        return True, f"Intraday SMA20 not ready ({INTRADAY_TIMEFRAME}) — MTF filter skipped", 0.0

    close          = float(latest["c"])
    intraday_sma20 = float(latest["sma_20"])
    margin_pct     = (close - intraday_sma20) / intraday_sma20  # + = above SMA20, - = below

    if close > intraday_sma20:
        return True, (
            f"Intraday ({INTRADAY_TIMEFRAME}) close={round(close,2)} "
            f"above SMA20={round(intraday_sma20,2)} (+{round(margin_pct*100,2)}%)"
        ), round(margin_pct, 4)

    # Close is below intraday SMA20 — check tolerance override
    abs_margin = abs(margin_pct)
    if abs_margin <= INTRADAY_SMA_TOLERANCE_PCT and ALLOW_STRONG_DAILY_WEAK_INTRADAY and entry_tier == "strong":
        return True, (
            f"Intraday marginal PASS: close={round(close,2)} is {round(abs_margin*100,2)}% below "
            f"SMA20={round(intraday_sma20,2)} (within {round(INTRADAY_SMA_TOLERANCE_PCT*100,2)}% tolerance,"
            f" strong daily tier override)"
        ), round(margin_pct, 4)

    return False, (
        f"Blocked by intraday trend ({INTRADAY_TIMEFRAME}: "
        f"close={round(close,2)} is {round(abs_margin*100,2)}% below SMA20={round(intraday_sma20,2)})"
    ), round(margin_pct, 4)


def _build_decision_summary(signal_data: dict) -> str:
    """
    Build a concise one-line decision summary for logging and API visibility.

    Examples:
      "BUY [strong-trend] | RSI=58 | MACD↑ | vol✓ | intraday✓ (+1.2%)"
      "BUY [early-trend] | RSI=52 | MACD~ | vol✓ | intraday marginal"
      "HOLD: MACD histogram not improving (momentum decelerating)"
      "SELL: close below SMA20"
    """
    signal = signal_data.get("signal", "?")
    tier   = signal_data.get("entry_tier")

    if signal == "BUY":
        parts = [f"BUY [{tier}-trend]"]
        rsi = signal_data.get("rsi")
        if rsi is not None:
            parts.append(f"RSI={rsi}")
        if signal_data.get("macd_bullish") is True:
            hist_tag = "↑" if signal_data.get("macd_histogram_rising") else "~"
            parts.append(f"MACD{hist_tag}")
        if signal_data.get("volume_confirmed"):
            parts.append("vol✓")
        intraday_ok  = signal_data.get("intraday_confirmed")
        margin_pct   = signal_data.get("intraday_margin_pct", 0.0) or 0.0
        if intraday_ok is True:
            if margin_pct > 0:
                # Normal confirmed: close is above intraday SMA20 by this %
                parts.append(f"intraday✓ (+{round(margin_pct*100,2)}%)")
            elif margin_pct < 0:
                # Tolerance override: close was slightly below intraday SMA20
                parts.append(f"intraday✓ (marginal {round(margin_pct*100,2)}%)")
            else:
                # Data unavailable (fail-open) or symbol not checked
                parts.append("intraday✓")
        return " | ".join(parts)

    reason = signal_data.get("signal_reason", "")
    if signal == "SELL":
        return f"SELL: {reason}"
    if signal == "HOLD":
        # signal_reason may already start with "HOLD: " — strip to avoid double-label
        clean = reason[6:] if reason.startswith("HOLD: ") else reason
        return f"HOLD: {clean}"
    return f"{signal}: {reason}"


def _infer_blocker(signal_reason: str) -> str:
    """Map a signal_reason string to a short blocker tag for cycle-count grouping."""
    if not signal_reason:
        return "hold"
    r = signal_reason.lower()
    if "volume" in r:        return "volume"
    if "rsi" in r:           return "rsi"
    if "macd" in r:          return "macd"
    if "breakout" in r:      return "breakout"
    if "intraday" in r:      return "intraday"
    if "spy" in r:           return "spy_regime"
    if "trend strength" in r: return "trend_strength"
    return "trend"


_SCORE_MAX = {
    "trend": 25, "volume": 15, "rsi": 15, "macd": 15,
    "intraday": 10, "regime": 10, "affordability": 5, "breakout": 5,
}


def _near_miss_gaps(components: dict) -> str:
    """Return a compact string listing score dimensions that lost ≥2 pts."""
    gaps = [
        f"{k}({components.get(k, 0)}/{m})"
        for k, m in _SCORE_MAX.items()
        if m - components.get(k, 0) >= 2
    ]
    return ", ".join(gaps) if gaps else "all dims near-max"


def _monitor_and_sync_positions() -> list:
    """
    Run at the start of every trade cycle.

    1. Fetches all journal-open positions.
    2. Compares against Alpaca's live positions.
    3. If a position disappeared from Alpaca (stop/TP bracket auto-fired),
       closes the journal entry and records the exit reason.
    4. For positions still live in Alpaca, logs current price / P&L / stop / TP.

    Returns a list of status dicts (one per open position).
    Fails open on all API errors — never touches the journal when Alpaca is unreachable.
    """
    global _session_exits, _stop_loss_times, _session_stop_count, _market_cooldown_until
    global _breakeven_armed, _breakeven_stops
    status_lines: list = []
    open_paper = journal.get_open_paper_positions()
    if not open_paper:
        return status_lines

    # Fetch all current Alpaca positions in one call
    alpaca_held: dict = {}
    try:
        resp = requests.get(f"{BASE_URL}/v2/positions", headers=_headers(), timeout=10)
        if resp.status_code == 200:
            for p in resp.json():
                sym = str(p.get("symbol", "")).upper()
                alpaca_held[sym] = p
        else:
            # Non-200 → fail open; don't touch journal
            print(f"[monitor] WARNING: positions fetch returned {resp.status_code} — skipping sync")
            return status_lines
    except Exception as exc:
        print(f"[monitor] WARNING: could not fetch Alpaca positions: {exc} — skipping sync")
        return status_lines

    for pos in open_paper:
        sym        = str(pos.get("symbol", "")).upper()
        ep         = float(pos.get("entry_price") or 0)
        qty        = int(pos.get("qty") or 0)
        stop_price = float(pos.get("stop_price") or 0)
        tp_price   = float(pos.get("take_profit_price") or 0)

        if sym in alpaca_held:
            # Position is still live — compute current status values
            alp         = alpaca_held[sym]
            cur_price   = float(alp.get("current_price") or ep)
            unreal_pl   = float(alp.get("unrealized_pl") or 0)
            unreal_plpc = float(alp.get("unrealized_plpc") or 0) * 100
            pnl_tag     = f"+${unreal_pl:.2f}" if unreal_pl >= 0 else f"-${abs(unreal_pl):.2f}"
            stop_tag    = f"${stop_price:.2f}" if stop_price else "n/a"
            tp_tag      = f"${tp_price:.2f}"   if tp_price  else "n/a"
            print(
                f"[monitor] {sym} OPEN | entry=${ep:.2f} | cur=${cur_price:.2f} | "
                f"P&L={pnl_tag} ({unreal_plpc:.1f}%) | stop={stop_tag} | TP={tp_tag}"
            )

            # ── Weak momentum exit (after FORCE_EXIT_WEAK_AFTER ET, stalling positions) ──
            # Exits positions with low unrealized gain AND weakening RSI + MACD.
            # Does NOT exit strong runners.
            if _check_weak_momentum_exit(sym, ep, cur_price):
                _cancel_all_open_orders_for_symbol(sym, [])
                _submit_order({
                    "symbol": sym, "qty": qty,
                    "side": "sell", "type": "market", "time_in_force": "day",
                })
                est_pnl = round((cur_price - ep) * qty, 2) if ep > 0 else None
                journal.close_paper_trade(sym, cur_price, "weak_momentum_exit")
                _partial_tp_executed.discard(sym)
                _breakeven_armed.discard(sym)
                _breakeven_stops.pop(sym, None)
                _record_trade_time(sym)
                _update_loss_streak(est_pnl)
                print(
                    f"WEAK_EXIT [{sym}] reason=weak_momentum_exit | "
                    f"exit=${cur_price:.2f} | entry=${ep:.2f} | pnl=${est_pnl}"
                )
                _session_exits.append({
                    "symbol":              sym,
                    "reason":              "weak_momentum_exit",
                    "exit_price":          round(cur_price, 2),
                    "entry_price":         round(ep, 2),
                    "pnl":                 est_pnl,
                    "timestamp":           datetime.now(timezone.utc).isoformat(),
                    "exit_trigger_source": "bot_weak_exit",
                })
                status_lines.append({
                    "symbol":              sym,
                    "status":              "WEAK_EXIT",
                    "entry_price":         round(ep, 2),
                    "current_price":       round(cur_price, 2),
                    "unrealized_pnl":      round(unreal_pl, 2),
                    "unrealized_pct":      round(unreal_plpc, 2),
                    "exit_status":         "weak_momentum_exit",
                    "exit_trigger_source": "bot_weak_exit",
                })
                continue  # skip hard-exit checks for this position

            # ── Breakeven protection ───────────────────────────────────────────
            # When position gains >= BREAKEVEN_TRIGGER_GAIN_PCT, arm a virtual breakeven stop.
            # Logged to session state; does not modify Alpaca bracket orders to avoid
            # held_for_orders conflicts in paper mode. Fires before partial TP (+2%).
            if BREAKEVEN_PROTECTION_ENABLED and sym not in _breakeven_armed and ep > 0:
                _be_gain = (cur_price - ep) / ep
                if _be_gain >= BREAKEVEN_TRIGGER_GAIN_PCT:
                    _be_stop = round(ep * (1 + BREAKEVEN_BUFFER_PCT), 2)
                    _breakeven_armed.add(sym)
                    _breakeven_stops[sym] = _be_stop
                    print(
                        f"[breakeven] {sym} | reason=breakeven_stop_armed | "
                        f"gain={_be_gain*100:.2f}% >= {BREAKEVEN_TRIGGER_GAIN_PCT*100:.1f}% trigger | "
                        f"entry=${ep:.2f} | breakeven_stop=${_be_stop:.2f} "
                        f"(buffer={BREAKEVEN_BUFFER_PCT*100:.1f}%)"
                    )

            # ── Partial take-profit (first time gain >= PARTIAL_TP_GAIN_PCT) ──
            # Sells PARTIAL_TP_SELL_FRAC of the position and moves stop to breakeven.
            # Fires at most once per symbol per session.
            if sym not in _partial_tp_executed and qty > 1 and ep > 0:
                _gain_pct = (cur_price - ep) / ep
                if _gain_pct >= PARTIAL_TP_GAIN_PCT:
                    _partial_qty = max(1, int(qty * PARTIAL_TP_SELL_FRAC))
                    if _partial_qty < qty:
                        _existing_sells = [
                            o for o in get_open_orders(sym) if o.get("side") == "sell"
                        ]
                        if not _existing_sells:
                            _submit_order({
                                "symbol": sym, "qty": _partial_qty,
                                "side": "sell", "type": "market", "time_in_force": "day",
                            })
                            _partial_pnl = journal.partial_close_paper_trade(
                                sym, _partial_qty, cur_price, ep
                            )
                            _partial_tp_executed.add(sym)
                            print(
                                f"[partial_tp] {sym} | reason=partial_take_profit | "
                                f"gain={_gain_pct*100:.2f}% | "
                                f"sold={_partial_qty}/{qty} shares | "
                                f"exit=${cur_price:.2f} | partial_pnl={_partial_pnl}"
                            )
                            print(
                                f"[partial_tp] {sym} | reason=stop_moved_breakeven | "
                                f"stop=${ep:.2f} (breakeven)"
                            )
                            if _partial_pnl is not None:
                                _update_loss_streak(_partial_pnl)

            # ── Bot-side hard exit enforcement ───────────────────────────────
            # Fires when Alpaca's bracket order hasn't closed the position even
            # though price has already crossed the stop or TP level.
            hard_exit_reason = None
            if stop_price > 0 and cur_price <= stop_price:
                hard_exit_reason = "stop_loss_hit"
            elif tp_price > 0 and cur_price >= tp_price:
                hard_exit_reason = "take_profit_hit"

            if hard_exit_reason:
                # Duplicate guard: don't send a second sell if one is already open
                existing_sells = [
                    o for o in get_open_orders(sym) if o.get("side") == "sell"
                ]
                if existing_sells:
                    print(
                        f"[bot_exit] {sym} | {hard_exit_reason} triggered but "
                        f"sell order already pending — skipping duplicate"
                    )
                    status_lines.append({
                        "symbol":            sym,
                        "status":            "OPEN",
                        "entry_price":       round(ep, 2),
                        "current_price":     round(cur_price, 2),
                        "unrealized_pnl":    round(unreal_pl, 2),
                        "unrealized_pct":    round(unreal_plpc, 2),
                        "stop_price":        round(stop_price, 2) if stop_price else None,
                        "take_profit_price": round(tp_price, 2) if tp_price else None,
                        "exit_status":       f"{hard_exit_reason}_pending",
                    })
                else:
                    # Cancel any stale bracket legs before submitting the market sell
                    _cancel_all_open_orders_for_symbol(sym, [])
                    close_result = _submit_order({
                        "symbol": sym, "qty": qty,
                        "side": "sell", "type": "market", "time_in_force": "day",
                    })
                    est_pnl = round((cur_price - ep) * qty, 2) if ep > 0 else None
                    journal.close_paper_trade(sym, cur_price, hard_exit_reason)
                    _partial_tp_executed.discard(sym)
                    _breakeven_armed.discard(sym)
                    _breakeven_stops.pop(sym, None)
                    if hard_exit_reason == "stop_loss_hit":
                        _record_stop_loss_event(sym)
                    _record_trade_time(sym)
                    _update_loss_streak(est_pnl)
                    _exit_sev = "warning" if hard_exit_reason == "stop_loss_hit" else "success"
                    _log_evt(
                        hard_exit_reason,
                        f"{sym} {hard_exit_reason.upper()} | entry=${ep:.2f} | exit=${cur_price:.2f} | pnl=${est_pnl}",
                        severity=_exit_sev, symbol=sym,
                        data={"entry_price": ep, "exit_price": cur_price, "est_pnl": est_pnl, "qty": qty},
                    )
                    send_telegram_alert(
                        "Stop Loss Hit" if hard_exit_reason == "stop_loss_hit" else "Take Profit Hit",
                        f"{sym} | entry=${ep:.2f} | exit=${cur_price:.2f} | P&L=${est_pnl}",
                        severity=_exit_sev,
                    )
                    print(
                        f"BOT_EXIT [{sym}] reason={hard_exit_reason} | "
                        f"exit=${cur_price:.2f} | entry=${ep:.2f} | "
                        f"pnl=${est_pnl} | order={close_result}"
                    )
                    exit_record = {
                        "symbol":              sym,
                        "reason":              hard_exit_reason,
                        "exit_price":          round(cur_price, 2),
                        "entry_price":         round(ep, 2),
                        "pnl":                 est_pnl,
                        "timestamp":           datetime.now(timezone.utc).isoformat(),
                        "exit_trigger_source": "bot_hard_exit",
                    }
                    _session_exits.append(exit_record)
                    status_lines.append({
                        "symbol":              sym,
                        "status":              "BOT_EXITED",
                        "entry_price":         round(ep, 2),
                        "current_price":       round(cur_price, 2),
                        "unrealized_pnl":      round(unreal_pl, 2),
                        "unrealized_pct":      round(unreal_plpc, 2),
                        "stop_price":          round(stop_price, 2) if stop_price else None,
                        "take_profit_price":   round(tp_price, 2) if tp_price else None,
                        "exit_status":         hard_exit_reason,
                        "exit_trigger_source": "bot_hard_exit",
                    })
            else:
                status_lines.append({
                    "symbol":            sym,
                    "status":            "OPEN",
                    "entry_price":       round(ep, 2),
                    "current_price":     round(cur_price, 2),
                    "unrealized_pnl":    round(unreal_pl, 2),
                    "unrealized_pct":    round(unreal_plpc, 2),
                    "stop_price":        round(stop_price, 2) if stop_price else None,
                    "take_profit_price": round(tp_price, 2) if tp_price else None,
                    "exit_status":       "holding",
                })
        else:
            # Position gone from Alpaca — bracket order (stop or TP) must have fired.
            # Look up the real closing fill via the shared helper instead of guessing.
            fill = _find_broker_closing_fill(sym, stop_price=stop_price, tp_price=tp_price)
            if fill:
                exit_price  = fill["exit_price"]
                exit_reason = fill["exit_reason"]
                journal.close_paper_trade(sym, exit_price, exit_reason)
            else:
                # No reliable fill found — close so the journal isn't stuck "open" against
                # a position Alpaca no longer has, but never report this as trustworthy data.
                exit_price  = 0.0
                exit_reason = "auto_closed_bracket"
                journal.close_paper_trade(
                    sym, exit_price, exit_reason,
                    data_quality_status="suspect_zero_exit",
                    data_quality_note="no matching broker closing fill found during cycle-time reconciliation",
                )
            est_pnl = round((exit_price - ep) * qty, 2) if exit_price > 0 and ep > 0 else None
            _partial_tp_executed.discard(sym)
            _breakeven_armed.discard(sym)
            _breakeven_stops.pop(sym, None)
            if exit_reason == "stop_loss_hit":
                _record_stop_loss_event(sym)
            _update_loss_streak(est_pnl)
            _ac_sev = "warning" if exit_reason == "stop_loss_hit" else "success"
            _log_evt(
                exit_reason,
                f"{sym} AUTO-CLOSED | reason={exit_reason} | exit=${exit_price:.2f} | entry=${ep:.2f} | pnl=${est_pnl}",
                severity=_ac_sev, symbol=sym,
                data={"entry_price": ep, "exit_price": exit_price, "est_pnl": est_pnl, "exit_reason": exit_reason},
            )
            print(
                f"[monitor] {sym} AUTO-CLOSED | reason={exit_reason} | "
                f"exit=${exit_price:.2f} | entry=${ep:.2f} | est_pnl=${est_pnl}"
            )
            exit_record = {
                "symbol":              sym,
                "reason":              exit_reason,
                "exit_price":          round(exit_price, 2),
                "entry_price":         round(ep, 2),
                "pnl":                 est_pnl,
                "timestamp":           datetime.now(timezone.utc).isoformat(),
                "exit_trigger_source": "alpaca_bracket",
            }
            _session_exits.append(exit_record)
            status_lines.append({
                "symbol":              sym,
                "status":              "AUTO_CLOSED",
                "exit_reason":         exit_reason,
                "exit_price":          round(exit_price, 2),
                "entry_price":         round(ep, 2),
                "est_pnl":             est_pnl,
                "exit_status":         exit_reason,
                "exit_trigger_source": "alpaca_bracket",
            })

    return status_lines


def _send_session_end_telegram() -> None:
    """Send a comprehensive end-of-session Telegram summary. Never raises."""
    try:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entered_today = sum(1 for e in trade_log if e.get("new_entry_opened"))
        exited_today  = sum(1 for e in trade_log if e.get("signal") == "SELL" and (e.get("starting_qty") or 0) > 0)
        errors_today  = sum(1 for e in trade_log if e.get("error"))
        realized_pnl  = 0.0
        try:
            with journal._conn() as _con:
                _rows = _con.execute(
                    f"SELECT realized_pnl FROM paper_trades WHERE {journal.ELIGIBLE_TRADE_SQL} AND DATE(exit_timestamp)=?",
                    (today_str,),
                ).fetchall()
                realized_pnl = round(sum(r["realized_pnl"] for r in _rows if r["realized_pnl"]), 2)
        except Exception:
            pass
        blocker_counts: dict = {}
        for e in trade_log:
            b = e.get("blocked_by")
            if b:
                blocker_counts[b] = blocker_counts.get(b, 0) + 1
        blocker_str = (
            ", ".join(f"{k}×{v}" for k, v in sorted(blocker_counts.items(), key=lambda x: -x[1])[:3])
            if blocker_counts else "none"
        )
        best_nm  = max(_near_miss_symbols, key=lambda x: x["score"]) if _near_miss_symbols else None
        open_pos = journal.get_open_paper_positions()
        is_flat  = len(open_pos) == 0
        lines = [
            f"Scans: {_total_scan_cycles} | Entries: {entered_today} | Exits: {exited_today}",
            f"Realized P&L: ${realized_pnl:+.2f}",
            f"Top blockers: {blocker_str}",
        ]
        if best_nm:
            lines.append(
                f"Best near-miss: {best_nm.get('symbol')} score={best_nm.get('score')} [{best_nm.get('grade', '')}]"
            )
        if errors_today:
            lines.append(f"Errors: {errors_today}")
        lines.append(f"All flat: {'Yes' if is_flat else 'No — check open positions!'}")
        send_telegram_alert(
            "Session Ended",
            "\n".join(lines),
            severity="success" if is_flat else "warning",
        )
    except Exception as exc:
        print(f"[session_end_telegram] WARNING: could not send end-of-session summary: {exc}")


def _flatten_all_positions(reason: str = "flatten_at_window_end") -> list:
    """
    Market-sell every open bot-managed paper position.
    Used when FLATTEN_AT_WINDOW_END=true after TRADING_WINDOW_END.
    Returns a list of per-symbol result dicts.
    """
    global _session_exits, _session_flattened
    open_paper = journal.get_open_paper_positions()
    if not open_paper:
        print(f"[flatten] No open positions to flatten.")
        _session_flattened = True
        return []

    # Deduplicate by symbol: the journal may have multiple open rows for the same
    # symbol if a restart left stale entries. Only flatten each symbol once per call.
    seen_flatten_syms: set = set()
    deduped_paper = []
    for _pos in open_paper:
        _sym = str(_pos.get("symbol", "")).upper()
        if _sym in seen_flatten_syms:
            print(
                f"[flatten] {_sym} | reason=flatten_duplicate_prevented | "
                f"symbol already queued for this flatten cycle — skipping duplicate"
            )
            continue
        seen_flatten_syms.add(_sym)
        deduped_paper.append(_pos)

    print(
        f"[flatten] FLATTEN TRIGGERED — closing {len(deduped_paper)} position(s) "
        f"| reason={reason}"
    )
    results = []
    for pos in deduped_paper:
        sym = str(pos.get("symbol", "")).upper()
        qty = int(pos.get("qty") or 0)
        ep  = float(pos.get("entry_price") or 0)
        if qty <= 0:
            continue
        print(f"[flatten] {sym} | reason=flatten_processed | qty={qty} | entry=${ep:.2f}")

        actions: list = []
        _cancel_all_open_orders_for_symbol(sym, actions)

        close_order = {
            "symbol": sym, "qty": qty,
            "side": "sell", "type": "market", "time_in_force": "day",
        }
        close_result = _submit_order(close_order)
        actions.append({"step": "flatten_close", "response": close_result})

        # Best-effort exit price — fetch last bar close as estimate
        exit_price = ep
        try:
            df = _fetch_bars(sym, timeframe="1Day", days=2, limit=2)
            if not df.empty:
                exit_price = float(df.iloc[-1]["c"])
        except Exception:
            pass

        journal.close_paper_trade(sym, exit_price, reason)
        _partial_tp_executed.discard(sym)
        _breakeven_armed.discard(sym)
        _breakeven_stops.pop(sym, None)
        _record_trade_time(sym)
        est_pnl = round((exit_price - ep) * qty, 2) if ep > 0 else None
        _update_loss_streak(est_pnl)
        print(f"[flatten] {sym} | closed qty={qty} | est_exit=${exit_price:.2f} | est_pnl=${est_pnl}")

        exit_record = {
            "symbol":              sym,
            "reason":              reason,
            "exit_price":          round(exit_price, 2),
            "entry_price":         round(ep, 2),
            "pnl":                 est_pnl,
            "timestamp":           datetime.now(timezone.utc).isoformat(),
            "exit_trigger_source": "manual_flatten" if reason == "manual_flatten" else "flatten_at_window_end",
        }
        _session_exits.append(exit_record)
        results.append({"symbol": sym, "qty": qty, "reason": reason, "actions": actions, "est_pnl": est_pnl})

    _session_flattened = True
    if results:
        _log_evt(
            "bot_flattened",
            f"Flattened {len(results)} position(s) | reason={reason}",
            severity="warning",
            data={"reason": reason, "positions": [r["symbol"] for r in results]},
        )
        send_telegram_alert(
            "Positions Flattened",
            f"Closed {len(results)} position(s). Reason: {reason}",
            severity="warning",
        )
    _send_session_end_telegram()
    return results


def _check_flat_start():
    """
    When REQUIRE_FLAT_START=true, warn loudly if Alpaca paper positions already exist.
    Purely advisory — does NOT block trading.
    """
    if not REQUIRE_FLAT_START:
        return
    try:
        resp = requests.get(f"{BASE_URL}/v2/positions", headers=_headers(), timeout=10)
        if resp.status_code == 200:
            positions = resp.json()
            if positions:
                syms = [p.get("symbol") for p in positions]
                print(
                    f"[flat-start] *** WARNING: REQUIRE_FLAT_START=true but "
                    f"{len(positions)} Alpaca position(s) already open: {syms}. "
                    f"Close manually or reconcile before trading. ***"
                )
            else:
                print("[flat-start] OK — no open Alpaca positions, clean start confirmed.")
    except Exception as exc:
        print(f"[flat-start] WARNING: could not check Alpaca positions on startup: {exc}")


def _build_hold_diagnostic(signal_data: dict) -> str:
    """
    One-line diagnostic for HOLD / SELL-no-position — answers why no BUY fired.
    Includes: score, grade, primary blocker, RSI, vol ratio, trend strength,
    intraday status, and SPY regime status.
    """
    parts = []

    # Score & grade (present when a BUY was scored before being blocked)
    score = signal_data.get("score")
    grade = signal_data.get("grade")
    if score is not None:
        parts.append(f"score={score}[{grade}]")
    else:
        parts.append("score=n/a[no_BUY]")

    # Primary blocker
    reason = signal_data.get("signal_reason", "")
    blocker = _infer_blocker(reason)
    parts.append(f"blocker={blocker}")

    # RSI
    rsi = signal_data.get("rsi")
    if rsi is not None:
        parts.append(f"RSI={rsi:.0f}")

    # Volume ratio
    cv = signal_data.get("current_volume") or 0
    va = signal_data.get("vol_sma_20") or 0
    if va > 0 and cv > 0:
        parts.append(f"vol={cv / va:.2f}x")

    # Trend strength
    ts = signal_data.get("trend_strength")
    if ts is not None:
        parts.append(f"trend_str={ts:.4f}")

    # Intraday confirmation
    intraday = signal_data.get("intraday_confirmed")
    if intraday is True:
        parts.append("intraday=✓")
    elif intraday is False:
        parts.append("intraday=✗")
    else:
        parts.append("intraday=n/a")

    # SPY / market regime
    spy = signal_data.get("spy_bullish")
    if spy is True:
        parts.append("SPY=bull")
    elif spy is False:
        parts.append("SPY=bear")
    else:
        parts.append("SPY=n/a")

    return " | ".join(parts)


# ═════════════════════════════════════════════════════════════════════════════
# Endpoints
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/")
def root():
    return {"message": "Trading Bot API V3 is running"}


@app.get("/health")
def health():
    cooldown_symbols = [
        s for s, exp in _symbol_error_cooldown.items()
        if datetime.now(timezone.utc) < exp
    ]
    return {
        "ok":                    True,
        "version":               "v3",
        **_execution_mode_fields(),
        "disable_new_entries":   DISABLE_NEW_ENTRIES,
        "observe_only_mode":     _observe_only_mode,
        "api_failure_count":     _api_failure_count,
        "stale_data_guard":      f"enabled (max {STALE_DATA_MAX_HOURS}h)",
        "single_instance_lock":  "managed by run_bot.py (PID file)",
        "last_market_check_utc": _last_market_check.isoformat() if _last_market_check else None,
        "session_start_utc":     _session_start.isoformat(),
        "symbols_in_cooldown":   cooldown_symbols,
        "symbol_error_counts":   {k: v for k, v in _symbol_error_counts.items() if v > 0},
        "open_paper_positions":  journal.get_open_paper_positions(),
    }


@app.get("/config-summary")
def config_summary():
    """Returns non-sensitive strategy config so you can confirm .env loaded correctly on startup."""
    return {
        "dry_run":                      DRY_RUN,
        "paper_account_equity":         PAPER_ACCOUNT_EQUITY,
        "effective_dry_run_equity":     PAPER_ACCOUNT_EQUITY if DRY_RUN else None,
        "max_position_value":           round(PAPER_ACCOUNT_EQUITY * MAX_ALLOCATION_PCT, 2) if DRY_RUN else None,
        "trade_watchlist":              TRADE_WATCHLIST,
        "regime_symbols":               sorted(REGIME_SYMBOLS),
        "max_open_positions":           MAX_OPEN_POSITIONS,
        "trade_cooldown_minutes":       TRADE_COOLDOWN_MINUTES,
        "daily_loss_limit_pct":         DAILY_LOSS_LIMIT_PCT,
        "max_allocation_pct":           MAX_ALLOCATION_PCT,
        "risk_per_trade_pct":           RISK_PER_TRADE_PCT,
        "stop_loss_pct":                STOP_LOSS_PCT,
        "take_profit_pct":              TAKE_PROFIT_PCT,
        "trailing_stop_pct":            TRAILING_STOP_PCT,
        "rsi_period":                   RSI_PERIOD,
        "rsi_overbought":               RSI_OVERBOUGHT,
        "min_trend_strength":           MIN_TREND_STRENGTH,
        "breakout_lookback":            BREAKOUT_LOOKBACK,
        "intraday_timeframe":           INTRADAY_TIMEFRAME,
        "allow_early_trend_entry":      ALLOW_EARLY_TREND_ENTRY,
        "early_trend_max_sma_gap_pct":  EARLY_TREND_MAX_SMA_GAP_PCT,
        "sma20_rising_bars":            SMA20_RISING_BARS,
        "min_volume_ratio":             MIN_VOLUME_RATIO,
        "require_breakout_for_buy":            REQUIRE_BREAKOUT_FOR_BUY,
        "require_intraday_confirmation":       REQUIRE_INTRADAY_CONFIRMATION,
        "require_spy_bullish":                 REQUIRE_SPY_BULLISH,
        "allow_neutral_spy_entries":           ALLOW_NEUTRAL_SPY_ENTRIES,
        "neutral_spy_min_score":               NEUTRAL_SPY_MIN_SCORE,
        "bearish_spy_exception_min_score":     BEARISH_SPY_EXCEPTION_MIN_SCORE,
        "bearish_spy_exception_min_volume_ratio": BEARISH_SPY_EXCEPTION_MIN_VOLUME_RATIO,
        "bearish_spy_exception_require_macd":  BEARISH_SPY_EXCEPTION_REQUIRE_MACD,
        "intraday_sma_tolerance_pct":          INTRADAY_SMA_TOLERANCE_PCT,
        "allow_strong_daily_weak_intraday":    ALLOW_STRONG_DAILY_WEAK_INTRADAY,
        "early_trend_require_macd_improving":  EARLY_TREND_REQUIRE_MACD_IMPROVING,
        "min_entry_score":                     MIN_ENTRY_SCORE,
        "allow_b_setup_entries":               ALLOW_B_SETUP_ENTRIES,
    }


@app.get("/account")
def get_account():
    url = f"{BASE_URL}/v2/account"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@app.get("/stock/{symbol}")
def get_stock(symbol: str):
    url = f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@app.get("/bars/{symbol}")
def get_bars(symbol: str):
    url   = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=120)
    params = {
        "timeframe": "1Day",
        "start":     start.isoformat(),
        "end":       end.isoformat(),
        "limit":     100,
        "feed":      "iex",
        "sort":      "asc",
    }
    try:
        response = requests.get(url, headers=_headers(), params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@app.get("/sma/{symbol}")
def get_sma(symbol: str):
    url   = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=180)
    params = {
        "timeframe": "1Day",
        "start":     start.isoformat(),
        "end":       end.isoformat(),
        "limit":     200,
        "feed":      "iex",
        "sort":      "asc",
    }
    try:
        response = requests.get(url, headers=_headers(), params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}

    bars = data.get("bars", [])
    if not bars or not isinstance(bars, list):
        return {"error": "No bar data returned"}

    try:
        df = pd.DataFrame(bars)
        df["sma_20"] = df["c"].rolling(window=20).mean()
        df["sma_50"] = df["c"].rolling(window=50).mean()
        latest = df.iloc[-1]

        if pd.isna(latest["sma_20"]) or pd.isna(latest["sma_50"]):
            return {"error": "Not enough bars to calculate SMA values (need at least 50)"}

        return {
            "symbol":       symbol,
            "latest_close": round(float(latest["c"]),      2),
            "sma_20":       round(float(latest["sma_20"]), 2),
            "sma_50":       round(float(latest["sma_50"]), 2),
        }
    except Exception as e:
        return {"error": f"Failed to calculate SMA: {str(e)}"}


@app.get("/signal/{symbol}")
def get_signal(symbol: str):
    """
    V3 signal endpoint. Full filter chain:
      1. SMA crossover (daily)
      2. Trend strength
      3. Volume confirmation
      4. RSI overbought filter
      5. MACD bullish filter
      6. Breakout confirmation
      7. SPY market-direction filter
      8. Intraday multi-timeframe confirmation
    """
    data = _compute_raw_signal(symbol)
    if "error" in data:
        return data

    spy_bullish          = True
    spy_reason           = "N/A (symbol is SPY)"
    spy_regime           = "bullish"
    spy_rsi              = None
    spy_macd_bullish     = True
    spy_macd_hist_rising = True
    intraday_confirmed   = True
    intraday_reason      = "N/A (symbol is SPY)"
    intraday_margin_pct  = 0.0

    if symbol.upper() != "SPY" and data["signal"] == "BUY":
        # SPY regime filter.
        # REQUIRE_SPY_BULLISH=true → hard-block any non-bullish SPY (backward compat).
        # REQUIRE_SPY_BULLISH=false → tiered system; gating runs in execute_trade() after scoring.
        spy_regime, spy_reason, spy_rsi, spy_macd_bullish, spy_macd_hist_rising = _get_spy_regime()
        spy_bullish = (spy_regime == "bullish")
        if REQUIRE_SPY_BULLISH and not spy_bullish:
            data["signal"]        = "HOLD"
            data["signal_reason"] = spy_reason

        # Intraday MTF confirmation — only blocks when REQUIRE_INTRADAY_CONFIRMATION=true.
        # Passes entry_tier so tolerance override can apply on strong daily setups.
        if data["signal"] == "BUY":
            intraday_confirmed, intraday_reason, intraday_margin_pct = _get_intraday_confirmation(
                symbol, entry_tier=data.get("entry_tier")
            )
            if REQUIRE_INTRADAY_CONFIRMATION and not intraday_confirmed:
                data["signal"]        = "HOLD"
                data["signal_reason"] = intraday_reason

    elif symbol.upper() != "SPY":
        spy_reason      = "Not checked (signal is not BUY)"
        intraday_reason = "Not checked (signal is not BUY)"

    result = {
        "symbol":                data["symbol"],
        "close":                 data["close"],
        "sma_20":                data["sma_20"],
        "sma_50":                data["sma_50"],
        "trend_strength":        data["trend_strength"],
        "trend_strong":          data["trend_strong"],
        "entry_tier":            data.get("entry_tier"),   # "strong", "early", or None
        "current_volume":        data["current_volume"],
        "vol_sma_20":            data["vol_sma_20"],
        "volume_confirmed":      data["volume_confirmed"],
        "rsi":                   data["rsi"],
        "rsi_ok":                data["rsi_ok"],
        "macd_line":             data["macd_line"],
        "macd_signal_line":      data["macd_signal_line"],
        "macd_bullish":          data["macd_bullish"],
        "macd_histogram":             data.get("macd_histogram"),
        "macd_histogram_rising":      data.get("macd_histogram_rising"),
        "macd_hist_improving_2bars":  data.get("macd_hist_improving_2bars"),
        "breakout_high":         data["breakout_high"],
        "breakout_confirmed":    data["breakout_confirmed"],
        # SPY regime fields
        "spy_bullish":           spy_bullish,
        "spy_reason":            spy_reason,
        "spy_regime":            spy_regime,
        "spy_rsi":               spy_rsi,
        "spy_macd_bullish":      spy_macd_bullish,
        "spy_macd_hist_rising":  spy_macd_hist_rising,
        "intraday_confirmed":    intraday_confirmed,
        "intraday_reason":       intraday_reason,
        "intraday_margin_pct":   intraday_margin_pct,
        "signal":                data["signal"],
        "signal_reason":         data["signal_reason"],
    }
    result["decision_summary"] = _build_decision_summary(result)
    return result


def is_market_open() -> bool:
    """
    Check Alpaca clock. On transient API failures, returns the last successfully
    fetched state rather than defaulting to False (which would falsely block trades).
    Only returns False as a cold-start default when no prior state is known.
    """
    global _last_market_check, _last_known_market_state
    url = f"{BASE_URL}/v2/clock"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        response.raise_for_status()
        data = response.json()
        _last_market_check = datetime.now(timezone.utc)
        state = bool(data.get("is_open", False))
        _last_known_market_state = state
        return state
    except requests.exceptions.RequestException as e:
        if _last_known_market_state is not None:
            print(
                f"[market_clock] API error: {e} — "
                f"using last known state (is_open={_last_known_market_state})"
            )
            return _last_known_market_state
        print(f"[market_clock] API error: {e} — no prior state, treating as closed")
        return False
    except Exception as e:
        if _last_known_market_state is not None:
            print(
                f"[market_clock] unexpected error: {e} — "
                f"using last known state (is_open={_last_known_market_state})"
            )
            return _last_known_market_state
        print(f"[market_clock] unexpected error: {e} — no prior state, treating as closed")
        return False


@app.get("/debug-clock")
def debug_clock():
    url = f"{BASE_URL}/v2/clock"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        try:
            payload = response.json()
        except Exception:
            payload = {"raw_text": response.text}
        return {
            "status_code": response.status_code, 
            "payload": payload, 
            "base_url": BASE_URL
        }
    except Exception as e:
        return {
            "error": str(e), 
            "base_url": BASE_URL,
        }


def calculate_position_size(price: float, equity: float = 0, symbol: str = "") -> int:
    """
    V3 position sizing — dual cap:
      1. Risk-based: risk RISK_PER_TRADE_PCT of equity over STOP_LOSS_PCT stop.
      2. Allocation cap: never exceed MAX_ALLOCATION_PCT of equity per trade.
    Final qty = min(risk_based, allocation_based).
    Returns 0 when the symbol is unaffordable — callers must treat 0 as SKIP.
    In DRY_RUN mode uses PAPER_ACCOUNT_EQUITY, not Alpaca paper buying power.
    """
    if equity <= 0:
        if DRY_RUN:
            equity = PAPER_ACCOUNT_EQUITY
        else:
            try:
                response = requests.get(f"{BASE_URL}/v2/account", headers=_headers(), timeout=10)
                equity = float(response.json().get("equity", 0))
            except Exception:
                equity = 0

    if equity <= 0 or price <= 0:
        return 0

    risk_dollars   = equity * RISK_PER_TRADE_PCT
    risk_per_share = price  * STOP_LOSS_PCT
    risk_based_qty = int(risk_dollars / risk_per_share) if risk_per_share > 0 else 0

    max_dollars    = equity * MAX_ALLOCATION_PCT
    allocation_qty = int(max_dollars / price)

    qty = min(risk_based_qty, allocation_qty)

    tag = f"[{symbol}] " if symbol else ""
    print(
        f"[sizing] {tag}price=${price:.2f} | equity=${equity:.2f} | "
        f"max_alloc=${max_dollars:.2f} ({MAX_ALLOCATION_PCT*100:.0f}%) | "
        f"risk_$=${risk_dollars:.2f} ({RISK_PER_TRADE_PCT*100:.1f}%) | "
        f"risk_qty={risk_based_qty} | alloc_qty={allocation_qty} | final_qty={qty}"
    )

    if qty < 1:
        print(
            f"[sizing] SKIP: {tag}qty=0 — ${price:.2f}/share unaffordable "
            f"(max_alloc=${max_dollars:.2f}, need ≥${price:.2f})"
        )
        return 0

    return qty


def _log_trade(symbol: str, result: dict, signal_data: dict = None):
    _sd = signal_data or {}
    entry = {
        "timestamp":             datetime.now(timezone.utc).isoformat(),
        "symbol":                symbol,
        "signal":                result.get("signal"),
        # Prefer execution-level outcome (e.g. "SKIP: cooldown") over signal-level summary
        "decision_summary":      result.get("decision_summary") or _sd.get("decision_summary"),
        "signal_reason":         result.get("signal_reason") or _sd.get("signal_reason"),
        "entry_tier":            _sd.get("entry_tier"),
        "starting_qty":          result.get("starting_qty"),
        "actions":               result.get("actions", []),
        "message":               result.get("message"),
        "dry_run":               DRY_RUN,
        "stop_loss_price":       result.get("stop_loss_price"),
        "take_profit_price":     result.get("take_profit_price"),
        "blocked_by":            result.get("blocked_by"),
        "entry_price":           result.get("entry_price"),
        "trend_strength":        _sd.get("trend_strength"),
        "volume_confirmed":      _sd.get("volume_confirmed"),
        "current_volume":        _sd.get("current_volume"),
        "vol_sma_20":            _sd.get("vol_sma_20"),
        "rsi":                   _sd.get("rsi"),
        "macd_line":             _sd.get("macd_line"),
        "macd_signal_line":      _sd.get("macd_signal_line"),
        "macd_bullish":          _sd.get("macd_bullish"),
        "macd_histogram":        _sd.get("macd_histogram"),
        "macd_histogram_rising":      _sd.get("macd_histogram_rising"),
        "macd_hist_improving_2bars":  _sd.get("macd_hist_improving_2bars"),
        "breakout_confirmed":    _sd.get("breakout_confirmed"),
        "intraday_confirmed":    _sd.get("intraday_confirmed"),
        "intraday_reason":       _sd.get("intraday_reason"),
        "intraday_margin_pct":   _sd.get("intraday_margin_pct"),
        "spy_bullish":           _sd.get("spy_bullish"),
        "spy_reason":            _sd.get("spy_reason"),
        "score":                 result.get("score") or _sd.get("score"),
        "grade":                 result.get("grade") or _sd.get("grade"),
        "new_entry_opened":      result.get("new_entry_opened", False),
    }
    trade_log.append(entry)
    journal.log_event(entry)
    # Fire structured events for key outcomes so the event log stays complete
    if result.get("new_entry_opened"):
        _log_evt(
            "trade_entered",
            f"{symbol} ENTERED | {entry.get('decision_summary', '')}",
            severity="success", symbol=symbol,
            data={"entry_price": result.get("entry_price"), "score": entry.get("score"), "grade": entry.get("grade")},
        )
        send_telegram_alert(
            "Trade Entered",
            f"{symbol} | {entry.get('decision_summary', '')} | score={entry.get('score')} [{entry.get('grade')}]",
            severity="success",
        )
    elif result.get("signal") == "SELL" and (result.get("starting_qty") or 0) > 0:
        _log_evt(
            "trade_exited",
            f"{symbol} EXITED | {entry.get('decision_summary', '')}",
            severity="info", symbol=symbol,
            data={"decision_summary": entry.get("decision_summary")},
        )
    elif result.get("signal") == "ERROR":
        _log_evt(
            "error",
            f"{symbol} ERROR | {entry.get('message', '')}",
            severity="error", symbol=symbol,
            data={"message": entry.get("message")},
        )
        send_telegram_alert("Bot Error", f"{symbol}: {entry.get('message', '')}", severity="error")


def get_open_orders(symbol: str):
    url    = f"{BASE_URL}/v2/orders"
    params = {"status": "open", "symbols": symbol}
    try:
        response = requests.get(url, headers=_headers(), params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
        return []
    except Exception:
        return []


def cancel_order(order_id: str):
    url = f"{BASE_URL}/v2/orders/{order_id}"
    try:
        response = requests.delete(url, headers=_headers(), timeout=10)
        if response.status_code == 204:
            return {"status": "cancelled"}
        return {"status_code": response.status_code, "response": response.text}
    except Exception as e:
        return {"error": str(e)}


def _submit_order(order: dict) -> dict:
    """Submit an order, or simulate it when DRY_RUN is enabled."""
    if DRY_RUN:
        return {"dry_run": True, "would_submit": order}
    try:
        resp = requests.post(f"{BASE_URL}/v2/orders", json=order, headers=_headers(), timeout=10)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def _poll_order_fill(order_id: str, max_attempts: int = 5, delay_sec: float = 0.5) -> dict:
    """
    Poll a submitted Alpaca order until it reaches a terminal state (filled, rejected,
    canceled, expired) or the bounded attempt budget is exhausted — never an unbounded
    loop, and the total wait is capped at max_attempts * delay_sec (~2.5s by default).

    Returns the last known order dict (which may still be in a non-terminal state if
    the budget ran out), or {} if the order could never be fetched.
    """
    last: dict = {}
    for attempt in range(max_attempts):
        try:
            resp = requests.get(f"{BASE_URL}/v2/orders/{order_id}", headers=_headers(), timeout=10)
            if resp.status_code == 200:
                last = resp.json()
                if last.get("status") in ("filled", "rejected", "canceled", "expired"):
                    return last
        except Exception:
            pass
        if attempt < max_attempts - 1:
            time.sleep(delay_sec)
    return last


def _cancel_order_call(order_id: str) -> dict:
    if DRY_RUN:
        return {"dry_run": True, "would_cancel": order_id}
    return cancel_order(order_id)


def _cancel_all_open_orders_for_symbol(symbol: str, actions: list):
    """Cancel every open order for symbol and append each result to actions."""
    open_orders = get_open_orders(symbol)
    for order in open_orders:
        order_id     = order.get("id")
        cancel_result = _cancel_order_call(order_id) if order_id else {"error": "Missing order id"}
        actions.append({
            "step":     "cancel_order",
            "order_id": order_id,
            "response": cancel_result,
        })


def ensure_protection_for_position(symbol: str, qty: int, entry_price: float, actions: list):
    """Ensure an open long position has stop/take-profit protection.

    Conservative: if ANY sell-side open order already exists for the symbol,
    skip entirely. This prevents "insufficient qty available for order" errors
    caused by multiple sell-side orders competing for the same shares.

    Always appends a diagnostic record to actions so callers can see what happened.
    """
    if qty <= 0 or entry_price <= 0:
        actions.append({
            "step":   "protection_check",
            "status": "skipped_invalid_params",
            "qty":    qty,
            "entry_price": entry_price,
        })
        return

    try:
        open_orders = get_open_orders(symbol)
    except Exception as e:
        actions.append({"step": "protection_check", "status": "skipped_api_error", "error": str(e)})
        return

    sell_orders = [o for o in open_orders if o.get("side") == "sell"]
    if sell_orders:
        # At least one sell-side order is already active — do not add more
        # to avoid held_for_orders / insufficient qty conflicts.
        actions.append({
            "step":              "protection_check",
            "status":            "already_protected",
            "sell_orders_found": len(sell_orders),
            "order_ids":         [o.get("id") for o in sell_orders],
        })
        return

    # No sell-side protection exists at all — place a stop-loss only.
    # (A single sell order is safe; two simultaneous sell orders conflict.)
    stop_price = round(entry_price * (1 - STOP_LOSS_PCT), 2)
    stop_order = {
        "symbol":        symbol,
        "qty":           qty,
        "side":          "sell",
        "type":          "stop",
        "stop_price":    stop_price,
        "time_in_force": "gtc",
    }
    try:
        result = _submit_order(stop_order)
    except Exception as e:
        result = {"error": str(e)}
    actions.append({"step": "ensure_stop_loss", "status": "placed", "price": stop_price, "response": result})


@app.get("/position/{symbol}")
def get_position(symbol: str):
    url = f"{BASE_URL}/v2/positions/{symbol}"
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}

    if response.status_code == 200:
        try:
            return response.json()
        except Exception:
            return {"error": "Invalid JSON in position response"}

    return {"error": True, "status_code": response.status_code, "response": response.text}


@app.post("/trade/{symbol}")
def execute_trade(symbol: str, block_new_entry: bool = False):
    # ── 1. Compute V3 signal ─────────────────────────────────────────────────
    signal_data = get_signal(symbol)
    signal      = signal_data.get("signal")

    if "error" in signal_data:
        _err_msg = signal_data["error"]
        print(f"[execute_trade] {symbol} | ERROR: {_err_msg} | dry_run={DRY_RUN}")
        result = {
            "signal":           "ERROR",
            "signal_reason":    _err_msg,
            "decision_summary": f"ERROR: {_err_msg}",
            "starting_qty":     0,
            "actions":          [],
            "message":          _err_msg,
        }
        _log_trade(symbol, result)
        return result

    # ── Candidate scoring (BUY signals only) ─────────────────────────────────
    # Score is computed before any gate checks so it gets logged even for blocked setups.
    candidate_score  = 0
    candidate_grade  = "C"
    score_components: dict = {}
    if signal == "BUY":
        global _session_ap, _session_a, _session_b, _session_c
        _scored          = compute_candidate_score(signal_data, PAPER_ACCOUNT_EQUITY, MAX_ALLOCATION_PCT)
        candidate_score  = _scored["score"]
        candidate_grade  = _scored["grade"]
        score_components = _scored["components"]
        signal_data["score"]            = candidate_score
        signal_data["grade"]            = candidate_grade
        signal_data["score_components"] = score_components
        print(score_summary_line(symbol, candidate_score, candidate_grade, signal_data))
        if candidate_grade == "A+":   _session_ap += 1
        elif candidate_grade == "A":  _session_a  += 1
        elif candidate_grade == "B":  _session_b  += 1
        else:                         _session_c  += 1

        # Near-miss: BUY signal scored 60–69 — log gaps and track for daily report
        if 60 <= candidate_score <= 69:
            global _session_near_miss, _near_miss_symbols
            _session_near_miss += 1
            gaps = _near_miss_gaps(score_components)
            _nm_macd_state = (
                "bullish" if signal_data.get("macd_bullish")
                else ("improving" if signal_data.get("macd_histogram_rising") else "bearish")
            )
            print(
                f"[NEAR_MISS] {symbol} | score={candidate_score} [{candidate_grade}] | "
                f"gaps: {gaps} | "
                f"trend={score_components.get('trend',0)}/25 "
                f"vol={score_components.get('volume',0)}/15 "
                f"rsi={score_components.get('rsi',0)}/15 "
                f"macd={score_components.get('macd',0)}/15({_nm_macd_state}) "
                f"intraday={score_components.get('intraday',0)}/10 "
                f"regime={score_components.get('regime',0)}/10 "
                f"afford={score_components.get('affordability',0)}/5"
            )
            _near_miss_symbols.append({
                "symbol":     symbol,
                "score":      candidate_score,
                "grade":      candidate_grade,
                "gaps":       gaps,
                "components": dict(score_components),
                "spy_regime": signal_data.get("spy_regime"),
                "macd_state": _nm_macd_state,
                "rsi":        signal_data.get("rsi"),
                "intraday":   signal_data.get("intraday_confirmed"),
            })
            signal_data["near_miss"]      = True
            signal_data["near_miss_gaps"] = gaps

    # ── 2. Get current position ──────────────────────────────────────────────
    position_data   = get_position(symbol)
    starting_qty    = 0
    avg_entry_price = 0.0
    if position_data and "qty" in position_data:
        starting_qty    = int(float(position_data["qty"]))
        avg_entry_price = float(position_data.get("avg_entry_price", 0) or 0)

    _summary = signal_data.get("decision_summary") or signal_data.get("signal_reason", "")
    print(f"[execute_trade] {symbol} | qty={starting_qty} | {_summary} | dry_run={DRY_RUN}")

    actions = []

    # ── Ensure protection for any existing long position ─────────────────────
    # Runs unconditionally — even when the market is closed — because GTC
    # stop/take-profit orders can be placed outside market hours on Alpaca.
    if starting_qty > 0 and avg_entry_price > 0:
        ensure_protection_for_position(symbol, starting_qty, avg_entry_price, actions)

    # ── 3. Market open check ─────────────────────────────────────────────────
    if not is_market_open():
        result = {
            "signal":           signal,
            "signal_reason":    signal_data.get("signal_reason"),
            "decision_summary": "SKIP: market closed",
            "starting_qty":     starting_qty,
            "actions":          actions,
            "message":          "Market is closed",
        }
        _log_trade(symbol, result, signal_data)
        return result

    # ── HOLD ─────────────────────────────────────────────────────────────────
    if signal == "HOLD":
        if starting_qty < 0:
            close_order = {
                "symbol": symbol, "qty": abs(starting_qty),
                "side": "buy", "type": "market", "time_in_force": "day",
            }
            close_result = _submit_order(close_order)
            actions.append({"step": "close_short_on_hold", "response": close_result})
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SELL: closed legacy short {abs(starting_qty)} shares on HOLD",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "message":          "Closed leftover short position during HOLD",
                "dry_run":          DRY_RUN,
            }
            _log_trade(symbol, result, signal_data)
            return result

        _hold_blocker = _infer_blocker(signal_data.get("signal_reason", ""))
        _hold_diag    = _build_hold_diagnostic(signal_data)
        _base_summary = signal_data.get("decision_summary") or f"HOLD: {signal_data.get('signal_reason', '')}"
        result = {
            "signal":           signal,
            "signal_reason":    signal_data.get("signal_reason"),
            "decision_summary": f"{_base_summary} || {_hold_diag}",
            "starting_qty":     starting_qty,
            "actions":          actions,
            "message":          signal_data.get("signal_reason", "Holding"),
            "blocked_by":       _hold_blocker,
            "score":            signal_data.get("score"),
            "grade":            signal_data.get("grade"),
            "near_miss":        signal_data.get("near_miss", False),
            "near_miss_gaps":   signal_data.get("near_miss_gaps"),
        }
        _log_trade(symbol, result, signal_data)
        return result

    # ── BUY ──────────────────────────────────────────────────────────────────
    if signal == "BUY":
        # Already-held guard: check both Alpaca live position AND journal open entry.
        # In live paper mode the journal may have a record even if Alpaca already filled.
        # In dry-run mode Alpaca has no positions so only the journal check applies.
        # Diagnostic classification only — this does not close, add to, or otherwise
        # change the existing position; it only labels why the BUY was skipped.
        if starting_qty > 0 or journal.has_open_paper_trade(symbol):
            _dup_src = "alpaca_position" if starting_qty > 0 else "journal_entry"
            print(
                f"[entry_guard] {symbol} | SKIP: reason=already_held | "
                f"source={_dup_src} | alpaca_qty={starting_qty} | "
                f"journal_open={journal.has_open_paper_trade(symbol)}"
            )
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SKIP: already_held ({_dup_src})",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "already_held",
                "message":          f"{symbol} already has an open position ({_dup_src})",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Max trades per symbol per day
        global _session_symbol_trade_count, _session_trade_date
        _today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if _today_key != _session_trade_date:
            _session_symbol_trade_count.clear()
            _session_trade_date = _today_key
        _sym_trade_count = _session_symbol_trade_count.get(symbol, 0)
        if _sym_trade_count >= MAX_TRADES_PER_SYMBOL:
            print(
                f"[safety] {symbol} | SKIP: max trades per symbol reached "
                f"({_sym_trade_count}/{MAX_TRADES_PER_SYMBOL}) for today"
            )
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": (
                    f"SKIP: max trades per symbol today "
                    f"({_sym_trade_count}/{MAX_TRADES_PER_SYMBOL})"
                ),
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "max_trades_per_symbol",
                "message": (
                    f"{symbol} already entered {_sym_trade_count}× today "
                    f"(limit={MAX_TRADES_PER_SYMBOL})"
                ),
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Kill switch — allow position monitoring but no new entries
        if DISABLE_NEW_ENTRIES:
            print(f"[safety] {symbol} | SKIP: DISABLE_NEW_ENTRIES=true (kill switch active)")
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": "SKIP: kill switch active (DISABLE_NEW_ENTRIES=true)",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "kill_switch",
                "message":          "New entries disabled by kill switch",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Late-window entry guard — no new entries at or after LAST_ENTRY_TIME.
        # Positions already open continue to be managed (exits are never blocked).
        if _is_past_last_entry_time():
            print(
                f"[entry_window] {symbol} | SKIP: reason=late_entry_window | "
                f"cutoff={LAST_ENTRY_TIME} ET (manage exits only)"
            )
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": (
                    f"SKIP: late_entry_window — past {LAST_ENTRY_TIME} ET "
                    f"(entries closed, exits managed)"
                ),
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "late_entry_window",
                "message":          f"No new entries after {LAST_ENTRY_TIME} ET (LAST_ENTRY_TIME guard)",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Hard daily loss shutdown (once triggered, blocks all new entries this session)
        if _daily_loss_shutdown:
            print(
                f"[daily_loss] {symbol} | SKIP: reason=new_entries_disabled | "
                f"observe_only_mode=true | daily_loss_limit_hit"
            )
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": "SKIP: daily_loss_limit_hit — new entries disabled for session",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "daily_loss_shutdown",
                "message":          "Daily loss limit hit — no new entries until session restart",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Consecutive loss cooldown
        if _is_loss_cooldown_active():
            _remaining = round(
                (_loss_cooldown_until - datetime.now(timezone.utc)).total_seconds() / 60, 1
            )
            print(
                f"[loss_streak] {symbol} | SKIP: reason=loss_cooldown_active | "
                f"consecutive_losses={_consecutive_losses} | remaining={_remaining}m"
            )
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": (
                    f"SKIP: loss_cooldown_active — {_consecutive_losses} consecutive losses, "
                    f"{_remaining}m remaining"
                ),
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "loss_cooldown",
                "message": (
                    f"Consecutive loss cooldown: {_consecutive_losses} losses in a row, "
                    f"resuming in {_remaining}m"
                ),
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Observe-only mode after repeated API failures
        if _observe_only_mode:
            print(f"[safety] {symbol} | SKIP: observe-only mode (API failures={_api_failure_count})")
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SKIP: observe-only mode ({_api_failure_count} API failures)",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "observe_only_mode",
                "message":          f"Observe-only mode active after {_api_failure_count} API failures",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Symbol error cooldown
        if _is_symbol_error_cooldown(symbol):
            print(f"[safety] {symbol} | SKIP: error cooldown active")
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": "SKIP: symbol error cooldown active",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "symbol_error_cooldown",
                "message":          f"{symbol} in error cooldown — too many consecutive data failures",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Cooldown (entries only — exits are NEVER blocked)
        if _is_in_cooldown(symbol):
            elapsed_min = round(
                (datetime.now(timezone.utc) - _last_trade_time[symbol]).total_seconds() / 60, 1
            )
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SKIP: cooldown active ({elapsed_min}m elapsed / {TRADE_COOLDOWN_MINUTES}m required)",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "cooldown",
                "message": (
                    f"Cooldown active for {symbol} "
                    f"({elapsed_min}m elapsed / {TRADE_COOLDOWN_MINUTES}m required)"
                ),
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Max open positions
        open_long_count = _count_open_long_positions()
        if open_long_count >= MAX_OPEN_POSITIONS:
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SKIP: max open positions ({open_long_count}/{MAX_OPEN_POSITIONS})",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "max_open_positions",
                "message":          f"Max open positions reached ({open_long_count}/{MAX_OPEN_POSITIONS})",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Daily loss limit
        loss_reached, loss_reason = _is_daily_loss_limit_reached()
        if loss_reached:
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": "SKIP: daily loss guard active",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "daily_loss_limit",
                "message":          loss_reason,
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Max one new entry per cycle
        if block_new_entry:
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": "SKIP: max entries per cycle",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "max_entries_per_cycle",
                "message":          "Entry skipped — max one new entry per cycle reached",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Score gate — require A or A+ setup quality (configurable)
        # B-setups can be unlocked via ALLOW_B_SETUP_ENTRIES=true.
        # This runs AFTER the free/fast checks and BEFORE expensive data calls.
        if candidate_score < MIN_ENTRY_SCORE:
            if candidate_grade == "B" and ALLOW_B_SETUP_ENTRIES:
                print(
                    f"[score] {symbol} | B-setup allowed (ALLOW_B_SETUP_ENTRIES=true) "
                    f"score={candidate_score} [{candidate_grade}]"
                )
            else:
                print(
                    f"[score] {symbol} | SKIP — score={candidate_score} [{candidate_grade}] "
                    f"below threshold={MIN_ENTRY_SCORE}"
                )
                result = {
                    "signal":           signal,
                    "signal_reason":    signal_data.get("signal_reason"),
                    "decision_summary": (
                        f"SKIP: score={candidate_score} [{candidate_grade}] "
                        f"below threshold={MIN_ENTRY_SCORE} "
                        f"(A≥75, A+≥85, B-allowed={ALLOW_B_SETUP_ENTRIES})"
                    ),
                    "starting_qty":     starting_qty,
                    "actions":          actions,
                    "blocked_by":       "score_below_threshold",
                    "score":            candidate_score,
                    "grade":            candidate_grade,
                    "near_miss":        signal_data.get("near_miss", False),
                    "near_miss_gaps":   signal_data.get("near_miss_gaps"),
                    "message": (
                        f"{symbol} quality score={candidate_score} [{candidate_grade}] "
                        f"is below minimum={MIN_ENTRY_SCORE}. "
                        f"Set ALLOW_B_SETUP_ENTRIES=true to trade B setups."
                    ),
                }
                _log_trade(symbol, result, signal_data)
                return result

        # ── Quality B gate — B-grade setups require explicit quality confirmation ───
        # Runs only for grade-B setups (score 65-74). A/A+ setups skip this gate.
        # Checks: score >= QUALITY_B_MIN_SCORE, volume ratio, MACD, RSI, intraday, SPY.
        # Produces tagged log lines (quality_b_allowed / quality_b_blocked) for diagnostics.
        # SPY bearish → quality_b always blocked regardless of other conditions.
        if candidate_grade == "B":
            _qb_cv        = signal_data.get("current_volume") or 0
            _qb_va        = signal_data.get("vol_sma_20") or 1
            _qb_vol_ratio = _qb_cv / _qb_va if _qb_va > 0 else 0.0
            _qb_macd_val  = (
                bool(signal_data.get("macd_bullish")) or
                bool(signal_data.get("macd_histogram_rising"))
            )
            _qb_rsi       = float(signal_data.get("rsi") or 0.0)
            _qb_intraday  = signal_data.get("intraday_confirmed", True)
            _qb_spy       = signal_data.get("spy_regime", "neutral")

            _qb_score_ok    = candidate_score >= QUALITY_B_MIN_SCORE
            _qb_vol_ok      = _qb_vol_ratio >= QUALITY_B_MIN_VOLUME_RATIO
            _qb_macd_ok     = (not QUALITY_B_REQUIRE_MACD_IMPROVING) or _qb_macd_val
            _qb_rsi_ok      = (_qb_rsi <= QUALITY_B_MAX_RSI) if _qb_rsi > 0 else True
            _qb_intraday_ok = (not QUALITY_B_REQUIRE_INTRADAY_GREEN) or bool(_qb_intraday)
            _qb_spy_ok      = (not QUALITY_B_ONLY_IF_SPY_NOT_BEARISH) or (_qb_spy != "bearish")
            # ORB check for Quality B: require price above opening range high
            _qb_orb_ok = True
            if QUALITY_B_REQUIRE_OPENING_RANGE_BREAK and OPENING_RANGE_ENABLED:
                _qb_orb = _get_or_build_opening_range(symbol)
                if _qb_orb.get("formed"):
                    _qb_entry_px = signal_data.get("close", 0.0)
                    _qb_orb_high = _qb_orb.get("high", 0.0)
                    _qb_orb_ok   = (_qb_entry_px > _qb_orb_high) if _qb_orb_high else True
                else:
                    _qb_orb_ok = False  # range not yet formed — block Quality B early entries
            _qb_all_pass    = (
                _qb_score_ok and _qb_vol_ok and _qb_macd_ok and
                _qb_rsi_ok and _qb_intraday_ok and _qb_spy_ok and _qb_orb_ok
            )

            _qb_log_prefix = (
                f"[quality_b] {symbol} | score={candidate_score}[{candidate_grade}] | "
                f"vol_ratio={_qb_vol_ratio:.3f} | RSI={_qb_rsi:.1f} | spy={_qb_spy} | "
                f"macd_ok={_qb_macd_val} | intraday={_qb_intraday}"
            )

            if _qb_all_pass:
                print(f"{_qb_log_prefix} | reason=quality_b_allowed")
            else:
                _qb_why = []
                if not _qb_score_ok:
                    _qb_why.append(f"score {candidate_score} < QUALITY_B_MIN_SCORE={QUALITY_B_MIN_SCORE}")
                if not _qb_vol_ok:
                    _qb_why.append(f"vol_ratio {_qb_vol_ratio:.3f} < {QUALITY_B_MIN_VOLUME_RATIO}")
                if not _qb_macd_ok:
                    _qb_why.append("MACD not bullish/improving")
                if not _qb_rsi_ok:
                    _qb_why.append(f"RSI {_qb_rsi:.1f} > QUALITY_B_MAX_RSI={QUALITY_B_MAX_RSI}")
                if not _qb_intraday_ok:
                    _qb_why.append("intraday not confirmed/green")
                if not _qb_spy_ok:
                    _qb_why.append("SPY bearish — quality_b blocked in bearish regime")
                if not _qb_orb_ok:
                    _qb_why.append("opening range not broken or not yet formed")
                _qb_why_str = "; ".join(_qb_why)
                print(f"{_qb_log_prefix} | reason=quality_b_blocked | why={_qb_why_str}")
                result = {
                    "signal":           signal,
                    "signal_reason":    signal_data.get("signal_reason"),
                    "decision_summary": f"SKIP: quality_b_blocked | {_qb_why_str}",
                    "starting_qty":     starting_qty,
                    "actions":          actions,
                    "blocked_by":       "quality_b_blocked",
                    "score":            candidate_score,
                    "grade":            candidate_grade,
                    "near_miss":        signal_data.get("near_miss", False),
                    "near_miss_gaps":   signal_data.get("near_miss_gaps"),
                    "message": (
                        f"{symbol} B-grade setup did not meet Quality B conditions: {_qb_why_str}"
                    ),
                }
                _log_trade(symbol, result, signal_data)
                return result

        # ── First-30-min caution mode ─────────────────────────────────────────────
        # Between 9:35 and 10:05 ET require score >= FIRST_30_MIN_MIN_SCORE,
        # vol_ratio >= FIRST_30_MIN_MIN_VOLUME_RATIO, and MACD bullish/improving.
        # Prevents premature entries into opening momentum that quickly reverses.
        if FIRST_30_MIN_CAUTION_ENABLED:
            _f30_et = datetime.now(ZoneInfo("America/New_York"))
            _f30_start = _f30_et.replace(hour=9, minute=35, second=0, microsecond=0)
            _f30_end   = _f30_et.replace(hour=10, minute=5, second=0, microsecond=0)
            if _f30_start <= _f30_et < _f30_end:
                _f30_cv        = signal_data.get("current_volume") or 0
                _f30_va        = signal_data.get("vol_sma_20") or 1
                _f30_vol_ratio = _f30_cv / _f30_va if _f30_va > 0 else 0.0
                _f30_macd_ok   = (
                    bool(signal_data.get("macd_bullish")) or
                    bool(signal_data.get("macd_histogram_rising"))
                )
                _f30_score_ok = candidate_score >= FIRST_30_MIN_MIN_SCORE
                _f30_vol_ok   = _f30_vol_ratio >= FIRST_30_MIN_MIN_VOLUME_RATIO
                if not (_f30_score_ok and _f30_vol_ok and _f30_macd_ok):
                    _f30_why = []
                    if not _f30_score_ok:
                        _f30_why.append(f"score {candidate_score} < {FIRST_30_MIN_MIN_SCORE}")
                    if not _f30_vol_ok:
                        _f30_why.append(f"vol_ratio {_f30_vol_ratio:.3f} < {FIRST_30_MIN_MIN_VOLUME_RATIO}")
                    if not _f30_macd_ok:
                        _f30_why.append("MACD not bullish/improving")
                    _f30_why_str = "; ".join(_f30_why)
                    print(
                        f"[first_30_min] {symbol} | SKIP: reason=first_30_min_caution | "
                        f"time={_f30_et.strftime('%H:%M ET')} | {_f30_why_str}"
                    )
                    result = {
                        "signal":           signal,
                        "signal_reason":    signal_data.get("signal_reason"),
                        "decision_summary": f"SKIP: first_30_min_caution | {_f30_why_str}",
                        "starting_qty":     starting_qty,
                        "actions":          actions,
                        "blocked_by":       "first_30_min_caution",
                        "score":            candidate_score,
                        "grade":            candidate_grade,
                        "message": f"First-30-min caution: {_f30_why_str}",
                    }
                    _log_trade(symbol, result, signal_data)
                    return result
                else:
                    print(
                        f"[first_30_min] {symbol} | first_30_min_caution passed | "
                        f"score={candidate_score} vol={_f30_vol_ratio:.3f} macd={_f30_macd_ok} | "
                        f"time={_f30_et.strftime('%H:%M ET')}"
                    )

        # ── Strict crypto stocks filter ────────────────────────────────────────
        # RIOT, MARA (and any STRICT_CRYPTO_SYMBOLS): require score >= STRICT_CRYPTO_MIN_SCORE,
        # vol_ratio >= STRICT_CRYPTO_MIN_VOLUME_RATIO, and SPY must be bullish (not just neutral).
        if STRICT_CRYPTO_STOCKS and symbol.upper() in STRICT_CRYPTO_SYMBOLS_SET:
            _sc_cv         = signal_data.get("current_volume") or 0
            _sc_va         = signal_data.get("vol_sma_20") or 1
            _sc_vol_ratio  = _sc_cv / _sc_va if _sc_va > 0 else 0.0
            _sc_spy_regime = signal_data.get("spy_regime", "neutral")
            _sc_score_ok   = candidate_score >= STRICT_CRYPTO_MIN_SCORE
            _sc_vol_ok     = _sc_vol_ratio >= STRICT_CRYPTO_MIN_VOLUME_RATIO
            _sc_spy_ok     = _sc_spy_regime == "bullish"
            if not (_sc_score_ok and _sc_vol_ok and _sc_spy_ok):
                _sc_why = []
                if not _sc_score_ok:
                    _sc_why.append(f"score {candidate_score} < {STRICT_CRYPTO_MIN_SCORE}")
                if not _sc_vol_ok:
                    _sc_why.append(f"vol_ratio {_sc_vol_ratio:.3f} < {STRICT_CRYPTO_MIN_VOLUME_RATIO}")
                if not _sc_spy_ok:
                    _sc_why.append(f"SPY must be bullish for {symbol} (spy={_sc_spy_regime})")
                _sc_why_str = "; ".join(_sc_why)
                print(f"[strict_crypto] {symbol} | SKIP: reason=strict_crypto_stock_filter | {_sc_why_str}")
                result = {
                    "signal":           signal,
                    "signal_reason":    signal_data.get("signal_reason"),
                    "decision_summary": f"SKIP: strict_crypto_stock_filter | {_sc_why_str}",
                    "starting_qty":     starting_qty,
                    "actions":          actions,
                    "blocked_by":       "strict_crypto_stock_filter",
                    "score":            candidate_score,
                    "grade":            candidate_grade,
                    "message": f"{symbol} strict crypto filter: {_sc_why_str}",
                }
                _log_trade(symbol, result, signal_data)
                return result
            else:
                print(
                    f"[strict_crypto] {symbol} | strict_crypto_stock_filter passed | "
                    f"score={candidate_score} vol={_sc_vol_ratio:.3f} spy={_sc_spy_regime}"
                )

        # ── Post-stop-loss symbol cooldown ────────────────────────────────────
        if _is_post_stop_symbol_cooldown(symbol):
            _sl_elapsed = round(
                (datetime.now(timezone.utc) - _stop_loss_times[symbol]).total_seconds() / 60, 1
            )
            _sl_remaining = round(STOP_LOSS_SYMBOL_COOLDOWN_MINUTES - _sl_elapsed, 1)
            print(
                f"[stop_cooldown] {symbol} | SKIP: reason=post_stop_symbol_cooldown | "
                f"elapsed={_sl_elapsed}m | remaining={_sl_remaining}m"
            )
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": (
                    f"SKIP: post_stop_symbol_cooldown | {symbol} stopped out {_sl_elapsed}m ago, "
                    f"{_sl_remaining}m remaining"
                ),
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "post_stop_symbol_cooldown",
                "score":            candidate_score,
                "grade":            candidate_grade,
                "message": f"{symbol} post-stop cooldown ({_sl_remaining}m remaining)",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # ── Market-wide stop cooldown (after 2 stops this session) ────────────
        if _is_market_stop_cooldown_active():
            _mkt_remaining = round(
                (_market_cooldown_until - datetime.now(timezone.utc)).total_seconds() / 60, 1
            )
            print(
                f"[stop_cooldown] {symbol} | SKIP: reason=market_stop_cooldown | "
                f"session_stops={_session_stop_count} | remaining={_mkt_remaining}m"
            )
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": (
                    f"SKIP: market_stop_cooldown | {_session_stop_count} stops this session, "
                    f"market pause {_mkt_remaining}m remaining"
                ),
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "market_stop_cooldown",
                "score":            candidate_score,
                "grade":            candidate_grade,
                "message": f"Market stop cooldown: {_session_stop_count} stops today, {_mkt_remaining}m pause",
            }
            _log_trade(symbol, result, signal_data)
            return result

        # ── Tiered SPY regime gate ────────────────────────────────────────────────
        # When REQUIRE_SPY_BULLISH=false the tiered system governs SPY-based gating.
        # When REQUIRE_SPY_BULLISH=true the hard-block already fired inside get_signal().
        if not REQUIRE_SPY_BULLISH:
            _spy_regime          = signal_data.get("spy_regime", "neutral")
            _spy_rsi             = signal_data.get("spy_rsi")
            _sym_macd_bullish    = signal_data.get("macd_bullish", False)
            _sym_macd_rising     = signal_data.get("macd_histogram_rising", False)
            _sym_rsi             = signal_data.get("rsi") or 0
            _cv                  = signal_data.get("current_volume") or 0
            _va                  = signal_data.get("vol_sma_20") or 1
            _vol_ratio           = _cv / _va if _va > 0 else 0.0
            _rsi_not_overbought  = _sym_rsi < RSI_OVERBOUGHT

            _spy_log_prefix = (
                f"[spy_regime] {symbol} | spy={_spy_regime} | "
                f"score={candidate_score}[{candidate_grade}] | "
                f"vol_ratio={_vol_ratio:.3f} | rsi={_sym_rsi} | "
                f"spy_rsi={_spy_rsi} | "
                f"macd_bullish={_sym_macd_bullish} | macd_rising={_sym_macd_rising}"
            )

            if _spy_regime == "bullish":
                print(f"{_spy_log_prefix} | reason=spy_bullish_allowed")

            elif _spy_regime == "neutral":
                if not ALLOW_NEUTRAL_SPY_ENTRIES:
                    print(f"{_spy_log_prefix} | reason=spy_neutral_quality_blocked | ALLOW_NEUTRAL_SPY_ENTRIES=false")
                    result = {
                        "signal":           signal,
                        "signal_reason":    signal_data.get("signal_reason"),
                        "decision_summary": "SKIP: spy_neutral_quality_blocked (ALLOW_NEUTRAL_SPY_ENTRIES=false)",
                        "starting_qty":     starting_qty,
                        "actions":          actions,
                        "blocked_by":       "spy_neutral_quality_blocked",
                        "score":            candidate_score,
                        "grade":            candidate_grade,
                        "message":          "SPY is neutral and ALLOW_NEUTRAL_SPY_ENTRIES=false",
                    }
                    _log_trade(symbol, result, signal_data)
                    return result
                elif candidate_score < NEUTRAL_SPY_MIN_SCORE:
                    print(
                        f"{_spy_log_prefix} | reason=spy_neutral_quality_blocked | "
                        f"score {candidate_score} < NEUTRAL_SPY_MIN_SCORE={NEUTRAL_SPY_MIN_SCORE}"
                    )
                    result = {
                        "signal":           signal,
                        "signal_reason":    signal_data.get("signal_reason"),
                        "decision_summary": (
                            f"SKIP: spy_neutral_quality_blocked | SPY=neutral, "
                            f"score={candidate_score} < required={NEUTRAL_SPY_MIN_SCORE}"
                        ),
                        "starting_qty":     starting_qty,
                        "actions":          actions,
                        "blocked_by":       "spy_neutral_quality_blocked",
                        "score":            candidate_score,
                        "grade":            candidate_grade,
                        "message":          f"SPY is neutral — need score ≥ {NEUTRAL_SPY_MIN_SCORE} (have {candidate_score})",
                    }
                    _log_trade(symbol, result, signal_data)
                    return result
                elif not (_sym_macd_bullish or _sym_macd_rising):
                    print(
                        f"{_spy_log_prefix} | reason=spy_neutral_quality_blocked | "
                        f"MACD not improving (macd_bullish={_sym_macd_bullish}, hist_rising={_sym_macd_rising})"
                    )
                    result = {
                        "signal":           signal,
                        "signal_reason":    signal_data.get("signal_reason"),
                        "decision_summary": "SKIP: spy_neutral_quality_blocked | SPY=neutral, MACD not improving or bullish",
                        "starting_qty":     starting_qty,
                        "actions":          actions,
                        "blocked_by":       "spy_neutral_quality_blocked",
                        "score":            candidate_score,
                        "grade":            candidate_grade,
                        "message":          "SPY is neutral — MACD must be bullish or histogram improving",
                    }
                    _log_trade(symbol, result, signal_data)
                    return result
                elif _vol_ratio < NEUTRAL_SPY_MIN_VOLUME_RATIO:
                    print(
                        f"{_spy_log_prefix} | reason=neutral_spy_volume_blocked | "
                        f"vol_ratio={_vol_ratio:.3f} < NEUTRAL_SPY_MIN_VOLUME_RATIO={NEUTRAL_SPY_MIN_VOLUME_RATIO}"
                    )
                    result = {
                        "signal":           signal,
                        "signal_reason":    signal_data.get("signal_reason"),
                        "decision_summary": (
                            f"SKIP: neutral_spy_volume_blocked | SPY=neutral, "
                            f"vol_ratio={_vol_ratio:.3f} < required={NEUTRAL_SPY_MIN_VOLUME_RATIO}"
                        ),
                        "starting_qty":     starting_qty,
                        "actions":          actions,
                        "blocked_by":       "neutral_spy_volume_blocked",
                        "score":            candidate_score,
                        "grade":            candidate_grade,
                        "message": (
                            f"SPY is neutral — need vol_ratio ≥ {NEUTRAL_SPY_MIN_VOLUME_RATIO} "
                            f"(have {_vol_ratio:.3f})"
                        ),
                    }
                    _log_trade(symbol, result, signal_data)
                    return result
                else:
                    print(f"{_spy_log_prefix} | reason=spy_neutral_quality_allowed")

            elif _spy_regime == "bearish":
                _macd_ok_for_exception  = _sym_macd_bullish or _sym_macd_rising
                _score_ok               = candidate_score >= BEARISH_SPY_EXCEPTION_MIN_SCORE
                _vol_ok                 = _vol_ratio >= BEARISH_SPY_EXCEPTION_MIN_VOLUME_RATIO
                _macd_gate_ok           = (not BEARISH_SPY_EXCEPTION_REQUIRE_MACD) or _macd_ok_for_exception
                _all_pass               = _score_ok and _vol_ok and _macd_gate_ok and _rsi_not_overbought

                if _all_pass:
                    print(
                        f"{_spy_log_prefix} | reason=spy_bearish_exception_allowed | "
                        f"score>={BEARISH_SPY_EXCEPTION_MIN_SCORE} | "
                        f"vol_ratio>={BEARISH_SPY_EXCEPTION_MIN_VOLUME_RATIO} | macd_ok={_macd_ok_for_exception}"
                    )
                else:
                    _why_parts = []
                    if not _score_ok:          _why_parts.append(f"score {candidate_score} < {BEARISH_SPY_EXCEPTION_MIN_SCORE}")
                    if not _vol_ok:            _why_parts.append(f"vol_ratio {_vol_ratio:.3f} < {BEARISH_SPY_EXCEPTION_MIN_VOLUME_RATIO}")
                    if not _macd_gate_ok:      _why_parts.append("MACD not bullish/improving")
                    if not _rsi_not_overbought: _why_parts.append(f"RSI {_sym_rsi} overbought (>={RSI_OVERBOUGHT})")
                    _why_str = "; ".join(_why_parts)
                    print(f"{_spy_log_prefix} | reason=spy_bearish_blocked | why={_why_str}")
                    result = {
                        "signal":           signal,
                        "signal_reason":    signal_data.get("signal_reason"),
                        "decision_summary": f"SKIP: spy_bearish_blocked | SPY=bearish, {_why_str}",
                        "starting_qty":     starting_qty,
                        "actions":          actions,
                        "blocked_by":       "spy_bearish_blocked",
                        "score":            candidate_score,
                        "grade":            candidate_grade,
                        "message": (
                            f"SPY is bearish — exception requires score≥{BEARISH_SPY_EXCEPTION_MIN_SCORE}, "
                            f"vol_ratio≥{BEARISH_SPY_EXCEPTION_MIN_VOLUME_RATIO}, MACD bullish/improving, RSI not overbought"
                        ),
                    }
                    _log_trade(symbol, result, signal_data)
                    return result

        # Spread filter — reject entries when bid/ask spread is too wide.
        # Fails open (skips filter) when quote data is unavailable.
        _spread_pct, _bid, _ask = _fetch_bid_ask_spread(symbol)
        if _spread_pct is not None and _spread_pct > MAX_SPREAD_PCT:
            print(
                f"[spread] {symbol} | reason=spread | "
                f"spread_pct={_spread_pct*100:.3f}% > max={MAX_SPREAD_PCT*100:.3f}% | "
                f"bid={_bid} ask={_ask}"
            )
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": (
                    f"SKIP: spread ({_spread_pct*100:.3f}% > "
                    f"{MAX_SPREAD_PCT*100:.3f}% max) | bid={_bid} ask={_ask}"
                ),
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "spread",
                "score":            candidate_score,
                "grade":            candidate_grade,
                "message": (
                    f"{symbol} bid/ask spread {_spread_pct*100:.3f}% exceeds "
                    f"max {MAX_SPREAD_PCT*100:.3f}% | bid={_bid} ask={_ask}"
                ),
            }
            _log_trade(symbol, result, signal_data)
            return result
        elif _spread_pct is None:
            print(f"[spread] {symbol} | spread unavailable — filter skipped (fail open)")

        # Use signal_data close as a rough current-price proxy and update session high
        _entry_price_proxy = signal_data.get("close", 0.0) or 0.0
        _update_session_high(symbol, _entry_price_proxy)

        # ── Opening Range Breakout gate ────────────────────────────────────────
        # Block entries until price breaks above the first OPENING_RANGE_MINUTES high.
        # Prevents entries during the volatile early-session range-formation period.
        if OPENING_RANGE_ENABLED and OPENING_RANGE_REQUIRE_BREAK:
            _orb = _get_or_build_opening_range(symbol)
            if _orb.get("formed"):
                _orb_high    = _orb.get("high", 0.0)
                _orb_broke   = (_entry_price_proxy > _orb_high) if _orb_high else True
                if not _orb_broke:
                    # Check A+ exception: grade A+, SPY bullish, strong vol, MACD bullish/improving
                    _orb_cv        = signal_data.get("current_volume") or 0
                    _orb_va        = signal_data.get("vol_sma_20") or 1
                    _orb_vol_ratio = _orb_cv / _orb_va if _orb_va > 0 else 0.0
                    _orb_macd_ok   = (
                        bool(signal_data.get("macd_bullish")) or
                        bool(signal_data.get("macd_histogram_rising"))
                    )
                    _orb_spy       = signal_data.get("spy_regime", "neutral")
                    _orb_aplus_ok  = (
                        OPENING_RANGE_ALLOW_A_PLUS_EXCEPTION
                        and candidate_grade == "A+"
                        and _orb_spy == "bullish"
                        and _orb_vol_ratio >= 0.15
                        and _orb_macd_ok
                    )
                    if _orb_aplus_ok:
                        print(
                            f"[orb] {symbol} | A+ exception allowed | "
                            f"price={_entry_price_proxy} orb_high={_orb_high} | "
                            f"grade=A+ spy=bullish vol={_orb_vol_ratio:.3f}"
                        )
                    else:
                        print(
                            f"[orb] {symbol} | SKIP: reason=opening_range_not_broken | "
                            f"price={_entry_price_proxy:.2f} <= orb_high={_orb_high:.2f}"
                        )
                        result = {
                            "signal":           signal,
                            "signal_reason":    signal_data.get("signal_reason"),
                            "decision_summary": (
                                f"SKIP: opening_range_not_broken | "
                                f"price=${_entry_price_proxy:.2f} <= ORB high=${_orb_high:.2f}"
                            ),
                            "starting_qty":     starting_qty,
                            "actions":          actions,
                            "blocked_by":       "opening_range_not_broken",
                            "score":            candidate_score,
                            "grade":            candidate_grade,
                            "message": (
                                f"{symbol} price ${_entry_price_proxy:.2f} has not broken "
                                f"opening range high ${_orb_high:.2f}"
                            ),
                        }
                        _log_trade(symbol, result, signal_data)
                        return result
                else:
                    print(
                        f"[orb] {symbol} | orb_break confirmed | "
                        f"price={_entry_price_proxy:.2f} > orb_high={_orb_high:.2f}"
                    )
            else:
                # Range not yet formed (< OPENING_RANGE_MINUTES since open) — block to
                # avoid early entries before the range is established
                print(
                    f"[orb] {symbol} | SKIP: reason=opening_range_not_broken | "
                    f"range still forming (need {OPENING_RANGE_MINUTES}m after 9:30 ET)"
                )
                result = {
                    "signal":           signal,
                    "signal_reason":    signal_data.get("signal_reason"),
                    "decision_summary": (
                        f"SKIP: opening_range_not_broken | "
                        f"opening range still forming ({OPENING_RANGE_MINUTES}m after 9:30 ET)"
                    ),
                    "starting_qty":     starting_qty,
                    "actions":          actions,
                    "blocked_by":       "opening_range_not_broken",
                    "score":            candidate_score,
                    "grade":            candidate_grade,
                    "message": f"Opening range not yet formed (need {OPENING_RANGE_MINUTES}m after 9:30 ET open)",
                }
                _log_trade(symbol, result, signal_data)
                return result

        # ── Anti-chase / extended candle gate ─────────────────────────────────
        # Block when price is too extended from intraday SMA20 (VWAP proxy).
        # Also updates session high from intraday bar data as a side effect.
        _ac_passes, _ac_reason, _ac_ext_pct = _check_anti_chase(symbol, _entry_price_proxy)
        if not _ac_passes:
            print(f"[anti_chase] {symbol} | SKIP: reason=anti_chase_extension | {_ac_reason}")
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SKIP: anti_chase_extension | {_ac_reason}",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "anti_chase_extension",
                "score":            candidate_score,
                "grade":            candidate_grade,
                "message": f"{symbol} anti-chase: {_ac_reason}",
            }
            _log_trade(symbol, result, signal_data)
            return result
        else:
            print(f"[anti_chase] {symbol} | anti_chase passed | {_ac_reason}")

        # ── Falling from session high gate ────────────────────────────────────
        # Block when price has pulled back more than MAX_PULLBACK_FROM_SESSION_HIGH_PCT
        # from the session high — prevents buying fading opening spikes.
        _sfh_passes, _sfh_reason, _sfh_pullback = _check_falling_from_session_high(
            symbol, _entry_price_proxy
        )
        if not _sfh_passes:
            print(f"[session_high] {symbol} | SKIP: reason=falling_from_session_high | {_sfh_reason}")
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SKIP: falling_from_session_high | {_sfh_reason}",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "falling_from_session_high",
                "score":            candidate_score,
                "grade":            candidate_grade,
                "message": f"{symbol} falling from session high: {_sfh_reason}",
            }
            _log_trade(symbol, result, signal_data)
            return result
        else:
            print(f"[session_high] {symbol} | session_high_pullback passed | {_sfh_reason}")

        # Stale data guard — skip new entries when bar data is too old
        _daily_df_check = _fetch_bars(symbol, timeframe="1Day", days=5, limit=5)
        stale, stale_reason = _is_data_stale(_daily_df_check)
        if stale:
            print(f"[safety] {symbol} | SKIP: {stale_reason}")
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SKIP: stale data — {stale_reason}",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "stale_data",
                "message":          stale_reason,
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Cancel any stale open orders before entering a new position
        _cancel_all_open_orders_for_symbol(symbol, actions)

        # Close any leftover short before going long
        if starting_qty < 0:
            close_order = {
                "symbol": symbol, "qty": abs(starting_qty),
                "side": "buy", "type": "market", "time_in_force": "day",
            }
            close_result = _submit_order(close_order)
            actions.append({"step": "close_short", "response": close_result})

        # Position sizing — dry-run uses simulated equity, not Alpaca paper buying power
        if DRY_RUN:
            equity = PAPER_ACCOUNT_EQUITY
        else:
            try:
                acct_resp = requests.get(f"{BASE_URL}/v2/account", headers=_headers(), timeout=10)
                equity    = float(acct_resp.json().get("equity", 0))
            except Exception:
                equity = 0

        entry_price       = signal_data["close"]
        trade_qty         = calculate_position_size(entry_price, equity, symbol)
        stop_loss_price   = round(entry_price * (1 - STOP_LOSS_PCT),   2)
        take_profit_price = round(entry_price * (1 + TAKE_PROFIT_PCT), 2)

        # ── Sizing transparency log ───────────────────────────────────────────
        max_position_value = equity * MAX_ALLOCATION_PCT
        max_risk_dollars   = equity * RISK_PER_TRADE_PCT
        estimated_loss     = trade_qty * entry_price * STOP_LOSS_PCT
        print(
            f"[dry-run sizing]\n"
            f"  symbol={symbol}\n"
            f"  equity=${equity:.2f}\n"
            f"  max_position=${max_position_value:.2f} ({MAX_ALLOCATION_PCT*100:.0f}% alloc)\n"
            f"  risk_dollars=${max_risk_dollars:.2f} ({RISK_PER_TRADE_PCT*100:.2f}% risk)\n"
            f"  entry_price=${entry_price:.2f}\n"
            f"  stop_price=${stop_loss_price:.2f}  (stop_loss={STOP_LOSS_PCT*100:.1f}%)\n"
            f"  calculated_qty={trade_qty}\n"
            f"  position_value=${trade_qty * entry_price:.2f}\n"
            f"  estimated_loss=${estimated_loss:.2f}"
        )

        # ── Explicit sizing sanity guards ─────────────────────────────────────
        # These are defensive checks — the sizing formula already enforces limits,
        # but explicit rejection + logging catches any future sizing regressions.
        if trade_qty < 0:
            reason = f"REJECT: calculated qty={trade_qty} is negative — sizing error"
            print(f"[sizing] {symbol} | {reason}")
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SKIP: {reason}",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "sizing_invalid_qty",
                "message":          reason,
            }
            _log_trade(symbol, result, signal_data)
            return result

        position_value = trade_qty * entry_price
        if position_value > max_position_value * 1.05:
            reason = (
                f"REJECT: position_value=${position_value:.2f} exceeds "
                f"max_alloc=${max_position_value:.2f} ({MAX_ALLOCATION_PCT*100:.0f}% of ${equity:.2f})"
            )
            print(f"[sizing] {symbol} | {reason}")
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SKIP: {reason}",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "sizing_oversize",
                "message":          reason,
            }
            _log_trade(symbol, result, signal_data)
            return result

        if estimated_loss > max_risk_dollars * 3:
            reason = (
                f"REJECT: estimated_loss=${estimated_loss:.2f} exceeds "
                f"3x risk_limit=${max_risk_dollars:.2f} — unrealistic risk"
            )
            print(f"[sizing] {symbol} | {reason}")
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SKIP: {reason}",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "sizing_risk_oversize",
                "message":          reason,
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Guard: qty=0 means this symbol is unaffordable at current equity/allocation.
        # Do NOT record a journal entry — there is no real position to track.
        if trade_qty <= 0:
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": (
                    f"SKIP: qty=0 — ${entry_price:.2f}/share unaffordable "
                    f"(equity=${equity:.2f}, max_alloc={MAX_ALLOCATION_PCT*100:.0f}%"
                    f"=${equity * MAX_ALLOCATION_PCT:.2f})"
                ),
                "starting_qty":     starting_qty,
                "actions":          actions,
                "blocked_by":       "qty_zero_unaffordable",
                "message": (
                    f"Cannot size {symbol}: ${entry_price:.2f}/share exceeds "
                    f"${equity * MAX_ALLOCATION_PCT:.2f} allocation limit "
                    f"({MAX_ALLOCATION_PCT*100:.0f}% of ${equity:.2f})"
                ),
            }
            _log_trade(symbol, result, signal_data)
            return result

        # Submit a single bracket order (market buy + stop-loss + take-profit legs).
        # This avoids the "insufficient qty available" / held_for_orders conflict
        # that occurs when stop and take-profit are submitted as separate orders.
        if TRAILING_STOP_PCT > 0:
            bracket_order = {
                "symbol":        symbol,
                "qty":           trade_qty,
                "side":          "buy",
                "type":          "market",
                "time_in_force": "day",
                "order_class":   "bracket",
                "stop_loss": {
                    "trail_percent": str(round(TRAILING_STOP_PCT * 100, 2)),
                },
                "take_profit": {
                    "limit_price": take_profit_price,
                },
            }
            open_result = _submit_order(bracket_order)
            actions.append({
                "step":          "open_long_bracket",
                "qty":           trade_qty,
                "trail_percent": round(TRAILING_STOP_PCT * 100, 2),
                "take_profit":   take_profit_price,
                "response":      open_result,
            })
        else:
            bracket_order = {
                "symbol":        symbol,
                "qty":           trade_qty,
                "side":          "buy",
                "type":          "market",
                "time_in_force": "day",
                "order_class":   "bracket",
                "stop_loss": {
                    "stop_price": stop_loss_price,
                },
                "take_profit": {
                    "limit_price": take_profit_price,
                },
            }
            open_result = _submit_order(bracket_order)
            actions.append({
                "step":        "open_long_bracket",
                "qty":         trade_qty,
                "stop_price":  stop_loss_price,
                "take_profit": take_profit_price,
                "response":    open_result,
            })

        # ── Confirm the entry fill before trusting an estimated price ────────────
        # DRY_RUN never has a real order to poll — the decision-time price is the
        # only price that exists. For real paper orders, poll (bounded, conservative
        # timeout) until the order reaches a terminal state so the journal never
        # records a "completed" position using only the decision-time estimate.
        entry_data_quality      = "verified"
        entry_data_quality_note = None

        if not DRY_RUN:
            order_id = open_result.get("id")
            if not order_id:
                entry_data_quality = "pending_entry_fill"
                entry_data_quality_note = (
                    f"broker did not return an order id on submit; response={open_result}"
                )
            else:
                polled      = _poll_order_fill(order_id)
                fill_status = polled.get("status")
                if fill_status in ("rejected", "canceled", "expired"):
                    # No position was actually opened — do not journal a phantom entry
                    # or count it toward the daily per-symbol trade limit.
                    result = {
                        "signal":           signal,
                        "signal_reason":    signal_data.get("signal_reason"),
                        "decision_summary": f"SKIP: broker order {fill_status}",
                        "starting_qty":     starting_qty,
                        "actions":          actions,
                        "blocked_by":       f"order_{fill_status}",
                        "message":          f"{symbol} bracket order was {fill_status} by Alpaca — no position opened",
                    }
                    _log_trade(symbol, result, signal_data)
                    return result
                elif fill_status == "filled":
                    filled_px  = float(polled.get("filled_avg_price") or 0)
                    filled_qty = float(polled.get("filled_qty") or 0)
                    if filled_px > 0:
                        if abs(filled_px - entry_price) > 0.005:
                            entry_data_quality_note = (
                                f"confirmed fill ${filled_px:.4f} vs decision-time "
                                f"signal price ${entry_price:.4f}"
                            )
                        entry_price = filled_px
                    if filled_qty > 0:
                        trade_qty = int(filled_qty)
                else:
                    # Still pending after the bounded poll budget — do not fabricate a
                    # fill. Record the position with the decision-time estimate but
                    # flag it so analytics can treat the entry price as unconfirmed.
                    entry_data_quality = "pending_entry_fill"
                    entry_data_quality_note = (
                        f"order accepted (status={fill_status}) but not confirmed filled "
                        f"within poll budget; entry_price is the decision-time estimate"
                    )

        _record_trade_time(symbol)
        entry_tier_label = signal_data.get("entry_tier", "unknown")
        print(
            f"[execute_trade] {symbol} | {'DRY RUN — ' if DRY_RUN else ''}"
            f"ENTERED long [{entry_tier_label}-trend] qty={trade_qty} "
            f"entry=${entry_price:.2f} ({entry_data_quality}) "
            f"stop={stop_loss_price} tp={take_profit_price} | "
            f"{signal_data.get('decision_summary', '')}"
        )

        # Record paper trade lifecycle entry (both dry-run and live for analytics)
        journal.open_paper_trade(symbol, {
            "entry_timestamp":      datetime.now(timezone.utc).isoformat(),
            "entry_price":          entry_price,
            "stop_price":           stop_loss_price,
            "take_profit_price":    take_profit_price,
            "trailing_stop_pct":    TRAILING_STOP_PCT if TRAILING_STOP_PCT > 0 else None,
            "qty":                  trade_qty,
            "slippage_pct":         SLIPPAGE_PCT,
            "entry_tier":           entry_tier_label,
            "rsi":                  signal_data.get("rsi"),
            "macd_line":            signal_data.get("macd_line"),
            "macd_signal_line":     signal_data.get("macd_signal_line"),
            "macd_histogram":       signal_data.get("macd_histogram"),
            "macd_histogram_rising": signal_data.get("macd_histogram_rising"),
            "trend_strength":       signal_data.get("trend_strength"),
            "volume_confirmed":     signal_data.get("volume_confirmed"),
            "breakout_confirmed":   signal_data.get("breakout_confirmed"),
            "intraday_confirmed":   signal_data.get("intraday_confirmed"),
            "entry_score":          candidate_score,
            "entry_grade":          candidate_grade,
            "data_quality_status":  entry_data_quality,
            "data_quality_note":    entry_data_quality_note,
        })

        # Record this entry toward the daily per-symbol trade count
        _session_symbol_trade_count[symbol] = _session_symbol_trade_count.get(symbol, 0) + 1

        result = {
            "signal":            signal,
            "signal_reason":     signal_data.get("signal_reason"),
            "decision_summary":  signal_data.get("decision_summary"),
            "entry_tier":        entry_tier_label,
            "entry_price":       entry_price,
            "starting_qty":      starting_qty,
            "actions":           actions,
            "message":           "DRY RUN — would open long position" if DRY_RUN else "Opened long position",
            "stop_loss_price":   stop_loss_price,
            "take_profit_price": take_profit_price,
            "new_entry_opened":  True,
            "dry_run":           DRY_RUN,
            "score":             candidate_score,
            "grade":             candidate_grade,
        }
        _log_trade(symbol, result, signal_data)
        return result

    # ── SELL ─────────────────────────────────────────────────────────────────
    # Cooldown NEVER blocks exits. SELL that closes a long always executes.
    if signal == "SELL":
        if starting_qty > 0:
            # Cancel stop-loss and take-profit GTC orders BEFORE the market sell
            # to prevent duplicate/oversell fills from lingering protection orders.
            _cancel_all_open_orders_for_symbol(symbol, actions)

            close_order = {
                "symbol": symbol, "qty": abs(starting_qty),
                "side": "sell", "type": "market", "time_in_force": "day",
            }
            close_result = _submit_order(close_order)
            actions.append({"step": "close_long", "response": close_result})
            _record_trade_time(symbol)
            print(
                f"[execute_trade] {symbol} | {'DRY RUN — ' if DRY_RUN else ''}"
                f"closed long qty={starting_qty}"
            )
            journal.close_paper_trade(symbol, signal_data.get("close", 0.0), "signal_exit")
            _partial_tp_executed.discard(symbol)
            _sel_exit_pnl = (
                round((signal_data.get("close", 0.0) - avg_entry_price) * starting_qty, 2)
                if avg_entry_price > 0 else None
            )
            _update_loss_streak(_sel_exit_pnl)
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SELL: closed long {starting_qty} shares",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "message":          "DRY RUN — would close long position" if DRY_RUN else "Closed long position",
                "dry_run":          DRY_RUN,
            }
            _log_trade(symbol, result, signal_data)
            return result

        if starting_qty < 0:
            close_order = {
                "symbol": symbol, "qty": abs(starting_qty),
                "side": "buy", "type": "market", "time_in_force": "day",
            }
            close_result = _submit_order(close_order)
            actions.append({"step": "close_legacy_short", "response": close_result})
            result = {
                "signal":           signal,
                "signal_reason":    signal_data.get("signal_reason"),
                "decision_summary": f"SELL: closed legacy short {abs(starting_qty)} shares",
                "starting_qty":     starting_qty,
                "actions":          actions,
                "message":          "Closed leftover short position",
                "dry_run":          DRY_RUN,
            }
            _log_trade(symbol, result, signal_data)
            return result

        _sell_diag = _build_hold_diagnostic(signal_data)
        result = {
            "signal":           "SELL",
            "signal_reason":    signal_data.get("signal_reason"),
            "decision_summary": f"SELL (no pos — bearish, nothing to exit) | {_sell_diag}",
            "starting_qty":     starting_qty,
            "actions":          actions,
            "message":          f"Bearish signal but no position to exit: {signal_data.get('signal_reason', '')}",
            "blocked_by":       "no_position",
        }
        _log_trade(symbol, result, signal_data)
        return result

    result = {
        "signal":           signal,
        "signal_reason":    signal_data.get("signal_reason"),
        "decision_summary": f"ERROR: unexpected signal state ({signal})",
        "starting_qty":     starting_qty,
        "actions":          actions,
        "message":          "Unexpected signal state",
    }
    _log_trade(symbol, result, signal_data)
    return result


@app.get("/trade-log")
def get_trade_log():
    return trade_log


@app.get("/session-summary")
def session_summary():
    """Lightweight paper-trading review: counts and recent decisions from the in-memory trade log."""
    from collections import Counter

    if not trade_log:
        return {"message": "No trades logged this session", "total": 0}

    total          = len(trade_log)
    signal_counts  = dict(Counter(e.get("signal") for e in trade_log))
    blocked_counts = dict(Counter(
        e.get("blocked_by") for e in trade_log if e.get("blocked_by")
    ))
    tier_counts    = dict(Counter(
        e.get("entry_tier") for e in trade_log if e.get("entry_tier")
    ))

    # Recent 15 decisions, newest first
    recent = [
        {
            "timestamp":        e.get("timestamp"),
            "symbol":           e.get("symbol"),
            "signal":           e.get("signal"),
            "decision_summary": e.get("decision_summary") or e.get("signal_reason"),
            "entry_tier":       e.get("entry_tier"),
            "blocked_by":       e.get("blocked_by"),
            "rsi":              e.get("rsi"),
            "macd_histogram":   e.get("macd_histogram"),
        }
        for e in trade_log[-15:]
    ]
    recent.reverse()

    return {
        "total_logged":     total,
        "signal_counts":    signal_counts,
        "blocked_counts":   blocked_counts,
        "entry_tier_counts": tier_counts,
        "recent_decisions": recent,
    }


@app.post("/flatten")
def flatten_positions():
    """
    Close all open bot-managed paper positions immediately via market sell.
    Also triggered automatically by run_bot.py when FLATTEN_AT_WINDOW_END=true
    and the trading window has ended.
    Safe to call manually at any time (e.g., end-of-day cleanup).
    """
    results = _flatten_all_positions("manual_flatten")
    return {
        "flattened_count": len(results),
        "positions":       results,
        "message": (
            f"Flattened {len(results)} position(s)." if results
            else "No open positions to flatten — already flat."
        ),
        "flatten_at_window_end_enabled": FLATTEN_AT_WINDOW_END,
    }


@app.post("/reconcile")
def reconcile():
    """
    Manually reconcile journal state against live Alpaca positions.
    Clears stale open journal entries for symbols where Alpaca reports no position.
    Safe to call at any time — closes only entries with no real backing position.
    """
    cleared = _reconcile_journal_state()
    return {
        "cleared_symbols": cleared,
        "cleared_count": len(cleared),
        "message": (
            f"Cleared {len(cleared)} stale position(s)." if cleared
            else "No stale positions found — journal is clean."
        ),
    }


@app.get("/check-state")
def check_state():
    """
    Print local bot state, Alpaca open positions, and dry-run journal positions.
    Use this before market open to verify the bot starts clean.
    """
    # Alpaca real/paper positions
    alpaca_positions = []
    try:
        resp = requests.get(f"{BASE_URL}/v2/positions", headers=_headers(), timeout=10)
        if resp.status_code == 200:
            alpaca_positions = [
                {
                    "symbol":      p.get("symbol"),
                    "qty":         p.get("qty"),
                    "avg_entry":   p.get("avg_entry_price"),
                    "market_val":  p.get("market_value"),
                    "unrealized_pl": p.get("unrealized_pl"),
                }
                for p in resp.json()
            ]
    except Exception as exc:
        alpaca_positions = [{"error": str(exc)}]

    # Journal dry-run paper positions
    open_paper = journal.get_open_paper_positions()

    # Trade cooldown state
    cooldown_status = {}
    now = datetime.now(timezone.utc)
    for sym, last_time in _last_trade_time.items():
        elapsed_min = round((now - last_time).total_seconds() / 60, 1)
        remaining   = max(0.0, round(TRADE_COOLDOWN_MINUTES - elapsed_min, 1))
        cooldown_status[sym] = {
            "elapsed_min": elapsed_min,
            "remaining_min": remaining,
            "in_cooldown": remaining > 0,
        }

    return {
        "dry_run":              DRY_RUN,
        "paper_account_equity": PAPER_ACCOUNT_EQUITY,
        "trade_watchlist":      TRADE_WATCHLIST,
        "regime_symbols":       sorted(REGIME_SYMBOLS),
        "alpaca_positions":     alpaca_positions,
        "journal_open_trades":  open_paper,
        "trade_cooldowns":      cooldown_status,
        "observe_only_mode":    _observe_only_mode,
        "api_failure_count":    _api_failure_count,
        "session_start_utc":    _session_start.isoformat(),
    }


@app.get("/daily-report")
def daily_report():
    """
    Full paper-trading report: session stats, open positions, and historical performance.
    Session stats cover this server run only (resets on restart).
    Historical stats cover all closed trades in the journal DB.
    Run after market close or any time for a snapshot.
    """
    from collections import Counter

    # ── Alpaca account snapshot ────────────────────────────────────────────────
    alpaca_equity = None
    buying_power  = None
    try:
        acct = requests.get(f"{BASE_URL}/v2/account", headers=_headers(), timeout=10).json()
        alpaca_equity = acct.get("equity")
        buying_power  = acct.get("buying_power")
    except Exception:
        pass

    # ── Alpaca open positions ─────────────────────────────────────────────────
    alpaca_positions = []
    try:
        pos_resp = requests.get(f"{BASE_URL}/v2/positions", headers=_headers(), timeout=10)
        if pos_resp.status_code == 200:
            alpaca_positions = [
                {
                    "symbol":        p.get("symbol"),
                    "qty":           p.get("qty"),
                    "avg_entry":     p.get("avg_entry_price"),
                    "market_value":  p.get("market_value"),
                    "unrealized_pl": p.get("unrealized_pl"),
                    "side":          p.get("side"),
                }
                for p in pos_resp.json()
            ]
    except Exception:
        pass

    # ── Alpaca open orders ────────────────────────────────────────────────────
    alpaca_orders = []
    try:
        ord_resp = requests.get(
            f"{BASE_URL}/v2/orders", headers=_headers(),
            params={"status": "open", "limit": 50}, timeout=10
        )
        if ord_resp.status_code == 200:
            alpaca_orders = [
                {
                    "id":         o.get("id"),
                    "symbol":     o.get("symbol"),
                    "side":       o.get("side"),
                    "type":       o.get("type"),
                    "qty":        o.get("qty"),
                    "status":     o.get("status"),
                    "stop_price": o.get("stop_price"),
                    "limit_price": o.get("limit_price"),
                    "created_at": o.get("created_at"),
                }
                for o in ord_resp.json()
            ]
    except Exception:
        pass

    # ── Session stats (in-memory trade log, resets on restart) ────────────────
    total_logged   = len(trade_log)
    signal_counts  = dict(Counter(e.get("signal") for e in trade_log))
    blocked_counts = dict(Counter(
        e.get("blocked_by") for e in trade_log if e.get("blocked_by")
    ))
    buy_signal_count = sum(1 for e in trade_log if e.get("signal") == "BUY")
    total_blocked    = sum(1 for e in trade_log if e.get("blocked_by"))
    entered_count  = sum(1 for e in trade_log if e.get("new_entry_opened"))
    exited_count   = sum(
        1 for e in trade_log
        if e.get("signal") == "SELL" and (e.get("starting_qty") or 0) > 0
    )
    error_count    = sum(1 for e in trade_log if e.get("signal") == "ERROR")
    near_miss_count = sum(1 for e in trade_log if e.get("near_miss"))
    best_near_miss  = (
        max(_near_miss_symbols, key=lambda x: x["score"])
        if _near_miss_symbols else None
    )

    # Grade breakdown for BUY candidates evaluated this session
    ap_count   = sum(1 for e in trade_log if e.get("grade") == "A+")
    a_count    = sum(1 for e in trade_log if e.get("grade") == "A")
    b_count    = sum(1 for e in trade_log if e.get("grade") == "B")
    c_count    = sum(1 for e in trade_log if e.get("grade") == "C" and e.get("score") is not None)
    scores_all = [e["score"] for e in trade_log if e.get("score") is not None]
    avg_score  = round(sum(scores_all) / len(scores_all), 1) if scores_all else None

    # Trades closed during this session (since _session_start)
    session_closed_trades = 0
    session_realized_pnl  = 0.0
    session_wins          = 0
    try:
        with journal._conn() as con:
            s_rows = con.execute(
                f"SELECT realized_pnl FROM paper_trades WHERE {journal.ELIGIBLE_TRADE_SQL} "
                "AND exit_timestamp >= ?",
                (_session_start.isoformat(),),
            ).fetchall()
            session_closed_trades = len(s_rows)
            session_realized_pnl  = round(sum(r["realized_pnl"] for r in s_rows if r["realized_pnl"]), 4)
            session_wins          = sum(1 for r in s_rows if r["realized_pnl"] and r["realized_pnl"] > 0)
    except Exception:
        pass

    session_win_rate = (
        round(session_wins / session_closed_trades * 100, 1)
        if session_closed_trades else None
    )

    # ── Open positions: journal + Alpaca live data + bot-managed flag ─────────
    open_trades = journal.get_open_paper_positions()
    bot_managed_syms = {str(p.get("symbol", "")).upper() for p in open_trades}
    unrealized_pnl_total = 0.0
    for pos in open_trades:
        sym = str(pos.get("symbol", "")).upper()
        try:
            # Prefer live Alpaca unrealized P&L if available
            alp_match = next((p for p in alpaca_positions if p.get("symbol") == sym), None)
            if alp_match:
                cur_price  = float(alp_match.get("market_value") or 0) / max(int(alp_match.get("qty") or 1), 1)
                unreal_pl  = float(alp_match.get("unrealized_pl") or 0)
                unreal_pct = float(alp_match.get("unrealized_plpc") or 0) * 100 if alp_match.get("unrealized_plpc") is not None else 0
                pos["current_price"]      = round(cur_price, 2)
                pos["unrealized_pnl"]     = round(unreal_pl, 2)
                pos["unrealized_pnl_pct"] = round(unreal_pct, 2)
                pos["bot_managed"]        = True
                unrealized_pnl_total += unreal_pl
            else:
                df = _fetch_bars(sym, timeframe="1Day", days=3, limit=3)
                if not df.empty:
                    cur_price  = float(df.iloc[-1]["c"])
                    ep         = float(pos.get("entry_price") or 0)
                    qty        = int(pos.get("qty") or 0)
                    if ep > 0 and qty > 0:
                        unreal_pnl = round((cur_price - ep) * qty, 2)
                        unreal_pct = round((cur_price - ep) / ep * 100, 2)
                        pos["current_price"]      = round(cur_price, 2)
                        pos["unrealized_pnl"]     = unreal_pnl
                        pos["unrealized_pnl_pct"] = unreal_pct
                        pos["bot_managed"]        = True
                        unrealized_pnl_total += unreal_pnl
        except Exception:
            pass

    # Mark Alpaca positions not in journal as externally opened (not bot-managed)
    for ap in alpaca_positions:
        if ap.get("symbol", "").upper() not in bot_managed_syms:
            ap["bot_managed"] = False

    # ── Today's exits (session exits list + any closed in journal today) ───────
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_exits_db: list = []
    try:
        with journal._conn() as con:
            # Raw listing — intentionally unfiltered by data quality so nothing is hidden
            # from the journal view; performance stats elsewhere exclude suspect rows.
            rows = con.execute(
                "SELECT symbol, exit_timestamp, exit_price, entry_price, realized_pnl, "
                "exit_reason, data_quality_status "
                "FROM paper_trades WHERE is_open=0 AND DATE(exit_timestamp) = ?",
                (today_str,),
            ).fetchall()
            today_exits_db = [
                {
                    "symbol":              r["symbol"],
                    "exit_time":           r["exit_timestamp"],
                    "exit_price":          r["exit_price"],
                    "entry_price":         r["entry_price"],
                    "realized_pnl":        r["realized_pnl"],
                    "reason":              r["exit_reason"],
                    "data_quality_status": r["data_quality_status"],
                }
                for r in rows
            ]
    except Exception:
        pass

    # ── Historical performance (all-time journal, closed trades) ──────────────
    perf = {}
    try:
        perf = journal.query_performance_summary()
    except Exception:
        pass

    # ── Stale symbol detection ─────────────────────────────────────────────────
    # Trades for symbols no longer in the active watchlist skew historical stats.
    stale_symbols: list = []
    try:
        with journal._conn() as con:
            hist_syms = {r[0] for r in con.execute(
                "SELECT DISTINCT symbol FROM paper_trades"
            ).fetchall()}
        stale_symbols = sorted(hist_syms - set(TRADE_WATCHLIST) - REGIME_SYMBOLS)
    except Exception:
        pass

    stale_warning = (
        f"Historical stats contain stale trades from older configurations. "
        f"Symbols no longer in watchlist: {stale_symbols}"
        if stale_symbols else None
    )

    # ── New summary sections ──────────────────────────────────────────────────
    ET = ZoneInfo("America/New_York")
    now_et = datetime.now(ET)

    now_utc = datetime.now(timezone.utc)
    _last_scan_age_min = (
        round((now_utc - _last_scan_at).total_seconds() / 60, 1) if _last_scan_at else None
    )
    market_state_summary = {
        "current_date":            now_et.strftime("%Y-%m-%d"),
        "current_et_time":         now_et.strftime("%H:%M ET"),
        "is_open_now":             _last_known_market_state,
        "last_checked_utc":        _last_market_check.isoformat() if _last_market_check else None,
        "last_scan_age_minutes":   _last_scan_age_min,
        "trading_window":          f"{TRADING_WINDOW_START}–{TRADING_WINDOW_END} ET",
        "last_entry_time":         LAST_ENTRY_TIME,
        "past_last_entry_time":    _is_past_last_entry_time(),
        "entries_allowed_now":     _entries_allowed_now(),
        # Renamed from "api_is_live" — this reflects that the FastAPI process
        # is actively serving requests, NOT that live-money trading is enabled.
        # See execution_mode / paper_trading fields for the trading-mode signal.
        "api_server_active":       True,
        "run_bot_active":          _is_run_bot_active(),
        "flatten_at_window_end":   FLATTEN_AT_WINDOW_END,
        "session_flattened":       _session_flattened,
    }

    scan_state_summary = {
        "total_scan_cycles":       _total_scan_cycles,
        "total_symbols_evaluated": total_logged,
        "last_scan_at_utc":        _last_scan_at.isoformat() if _last_scan_at else None,
        "buy_signals_seen":        buy_signal_count,
        "entries_taken":           entered_count,
        "entries_blocked":         total_blocked,
        "near_misses":             near_miss_count,
        "errors":                  error_count,
        "avg_symbols_per_cycle":   (
            round(total_logged / _total_scan_cycles, 1)
            if _total_scan_cycles > 0 else 0
        ),
    }

    # Determine why the bot did not trade (if applicable)
    no_trade_reason = None
    if entered_count == 0:
        if total_logged == 0:
            no_trade_reason = "no_scans_ran — verify bot runner is active and API is reachable"
        elif buy_signal_count == 0:
            spy_blocks = (
                blocked_counts.get("spy_regime", 0)
                + blocked_counts.get("spy_bearish_blocked", 0)
                + blocked_counts.get("spy_neutral_quality_blocked", 0)
            )
            if spy_blocks > 0:
                no_trade_reason = f"spy_filter_blocked_all — SPY regime blocked entries ({spy_blocks} blocks)"
            elif not _last_known_market_state:
                no_trade_reason = "market_was_closed — no scans ran during open market hours"
            else:
                no_trade_reason = "no_qualifying_setup — all symbols in HOLD or SELL"
        else:
            # Had BUY signals but all blocked
            if blocked_counts.get("spy_bearish_blocked", 0) > 0:
                no_trade_reason = f"spy_bearish_blocked ({blocked_counts['spy_bearish_blocked']} blocks) — no exception setups qualified"
            elif blocked_counts.get("spy_neutral_quality_blocked", 0) > 0:
                no_trade_reason = f"spy_neutral_quality_blocked ({blocked_counts['spy_neutral_quality_blocked']} blocks) — score < {NEUTRAL_SPY_MIN_SCORE} or MACD weak"
            elif blocked_counts.get("score_below_threshold", 0) > 0:
                no_trade_reason = f"score_too_low (threshold={MIN_ENTRY_SCORE})"
            elif blocked_counts.get("max_open_positions", 0) > 0:
                no_trade_reason = "max_positions_already_reached"
            elif blocked_counts.get("daily_loss_limit", 0) > 0:
                no_trade_reason = "daily_loss_limit_reached"
            elif blocked_counts.get("cooldown", 0) > 0:
                no_trade_reason = "trade_cooldown_active"
            elif blocked_counts.get("qty_zero_unaffordable", 0) > 0:
                no_trade_reason = "symbols_unaffordable_at_current_equity"
            elif blocked_counts.get("max_trades_per_symbol", 0) > 0:
                no_trade_reason = "max_trades_per_symbol_reached"
            else:
                top = max(blocked_counts, key=blocked_counts.get) if blocked_counts else "unknown"
                no_trade_reason = f"blocked_by_{top}"

    today_trade_summary = {
        "did_trade":             entered_count > 0,
        "entries":               entered_count,
        "exits":                 exited_count,
        "session_realized_pnl":  session_realized_pnl,
        "session_win_rate_pct":  session_win_rate,
        "closed_trades":         session_closed_trades,
        "no_trade_reason":       no_trade_reason,
        "blocked_breakdown":     blocked_counts,
        "grade_breakdown":       {
            "A+": ap_count, "A": a_count, "B": b_count, "C": c_count,
            "avg_score": avg_score,
        },
        "best_near_miss":        best_near_miss,
    }

    max_dollar_risk = round(PAPER_ACCOUNT_EQUITY * RISK_PER_TRADE_PCT, 2)
    max_dollar_loss = round(PAPER_ACCOUNT_EQUITY * MAX_ALLOCATION_PCT * STOP_LOSS_PCT, 2)
    risk_state_summary = {
        "paper_account_equity":      PAPER_ACCOUNT_EQUITY,
        "risk_per_trade_pct":        RISK_PER_TRADE_PCT,
        "max_dollar_risk_per_trade": max_dollar_risk,
        "stop_loss_pct":             STOP_LOSS_PCT,
        "take_profit_pct":           TAKE_PROFIT_PCT,
        "max_dollar_loss_per_trade": max_dollar_loss,
        "max_open_positions":        MAX_OPEN_POSITIONS,
        "max_trades_per_symbol":     MAX_TRADES_PER_SYMBOL,
        "daily_loss_limit_pct":      DAILY_LOSS_LIMIT_PCT,
        "daily_loss_limit_usd":      round(PAPER_ACCOUNT_EQUITY * DAILY_LOSS_LIMIT_PCT, 2),
        "flatten_at_window_end":     FLATTEN_AT_WINDOW_END,
        "flatten_happened":          _session_flattened,
        "observe_only_mode":         _observe_only_mode,
        "disable_new_entries":       DISABLE_NEW_ENTRIES,
    }

    return {
        "report_time_utc":   datetime.now(timezone.utc).isoformat(),
        "session_start_utc": _session_start.isoformat(),
        "stale_data_warning": stale_warning,
        "market_state_summary": market_state_summary,
        "scan_state_summary":   scan_state_summary,
        "today_trade_summary":  today_trade_summary,
        "risk_state_summary":   risk_state_summary,

        "config": {
            "dry_run":                    DRY_RUN,
            "alpaca_paper":               ALPACA_PAPER,
            "allow_live_trading":         ALLOW_LIVE_TRADING,
            "flatten_at_window_end":      FLATTEN_AT_WINDOW_END,
            "require_flat_start":         REQUIRE_FLAT_START,
            "paper_account_equity":       PAPER_ACCOUNT_EQUITY,
            "max_allocation_pct":         MAX_ALLOCATION_PCT,
            "risk_per_trade_pct":         RISK_PER_TRADE_PCT,
            "stop_loss_pct":              STOP_LOSS_PCT,
            "take_profit_pct":            TAKE_PROFIT_PCT,
            "max_open_positions":         MAX_OPEN_POSITIONS,
            "max_trades_per_symbol":      MAX_TRADES_PER_SYMBOL,
            "daily_loss_limit_pct":       DAILY_LOSS_LIMIT_PCT,
            "trading_window_start":       TRADING_WINDOW_START,
            "trading_window_end":         TRADING_WINDOW_END,
            "trade_cooldown_min":         TRADE_COOLDOWN_MINUTES,
            "require_spy_bullish":        REQUIRE_SPY_BULLISH,
            "require_intraday_confirm":   REQUIRE_INTRADAY_CONFIRMATION,
            "min_entry_score":            MIN_ENTRY_SCORE,
            "allow_b_setup_entries":      ALLOW_B_SETUP_ENTRIES,
            "min_volume_ratio":           MIN_VOLUME_RATIO,
            "watchlist":                  TRADE_WATCHLIST,
            "index_etfs":                 INDEX_ETFS,
        },

        "account": {
            "alpaca_equity":           alpaca_equity,
            "buying_power":            buying_power,
            "effective_sizing_equity": PAPER_ACCOUNT_EQUITY if DRY_RUN else alpaca_equity,
        },

        "session_stats": {
            "total_scans":          total_logged,
            "buy_signals":          buy_signal_count,
            "entered":              entered_count,
            "exited":               exited_count,
            "blocked_total":        total_blocked,
            "blocked_by_reason":    blocked_counts,
            "near_miss_count":      near_miss_count,
            "best_near_miss":       best_near_miss,
            "errors":               error_count,
            "avg_entry_score":      avg_score,
            "session_closed_trades": session_closed_trades,
            "session_realized_pnl":  session_realized_pnl,
            "session_win_rate":      session_win_rate,
        },

        "setup_grades": {
            "A+":       ap_count,
            "A":        a_count,
            "B":        b_count,
            "C":        c_count,
            "avg_score": avg_score,
        },

        "open_positions": {
            "count":                  len(open_trades),
            "unrealized_pnl_est":     round(unrealized_pnl_total, 2),
            "journal_trades":         open_trades,
            "alpaca_positions":       [
                dict(p, bot_managed=(p.get("symbol", "").upper() in bot_managed_syms))
                for p in alpaca_positions
            ],
            "alpaca_open_orders":     alpaca_orders,
        },

        "exits_today": {
            "count":   len(today_exits_db),
            # Annotate DB exits with a source tag so the report is self-explanatory
            "db_exits": [
                dict(
                    r,
                    exit_trigger_source=(
                        "bot_hard_exit"          if r.get("reason") in ("stop_loss_hit", "take_profit_hit")
                        else "flatten_at_window_end" if r.get("reason") == "flatten_at_window_end"
                        else "manual_flatten"    if r.get("reason") == "manual_flatten"
                        else "alpaca_bracket"    if r.get("reason") == "auto_closed_bracket"
                        else "signal_exit"
                    ),
                )
                for r in today_exits_db
            ],
            "session_exits": _session_exits,
            "bot_hard_exits_count": sum(
                1 for e in _session_exits if e.get("exit_trigger_source") == "bot_hard_exit"
            ),
        },

        "historical_performance": {
            "note": stale_warning or "All historical trades are from current watchlist symbols.",
            "stale_symbols": stale_symbols,
            "realized_pnl":       perf.get("total_simulated_pnl"),
            "win_rate":           perf.get("win_rate"),
            "loss_rate":          perf.get("loss_rate"),
            "expectancy":         perf.get("expectancy"),
            "profit_factor":      perf.get("profit_factor"),
            "avg_win":            perf.get("avg_win"),
            "avg_loss":           perf.get("avg_loss"),
            "avg_hold_minutes":   perf.get("avg_hold_minutes"),
            "avg_r_multiple":     perf.get("avg_r_multiple"),
            "max_drawdown":       perf.get("max_drawdown"),
            "largest_winner":     perf.get("largest_winner"),
            "largest_loser":      perf.get("largest_loser"),
            "best_symbol":        perf.get("best_symbol"),
            "worst_symbol":       perf.get("worst_symbol"),
            "total_closed_trades": perf.get("total_exits"),
            "open_positions_db":   perf.get("open_positions"),
        },
    }


@app.get("/affordability")
def affordability():
    """
    Show affordability for every trade watchlist symbol at current equity.
    Returns price, shares purchasable at MAX_ALLOCATION_PCT, and whether ≥1 share fits.
    Useful before market open to confirm the watchlist is viable.
    """
    equity    = PAPER_ACCOUNT_EQUITY  # always simulated in dry-run
    max_alloc = equity * MAX_ALLOCATION_PCT
    results   = []
    for sym in TRADE_WATCHLIST:
        try:
            df = _fetch_bars(sym, timeframe="1Day", days=5, limit=5)
            if df.empty:
                results.append({
                    "symbol": sym, "affordable": False,
                    "error": "No price data available",
                })
                continue
            price = float(df.iloc[-1]["c"])
            shares = int(max_alloc / price) if price > 0 else 0
            results.append({
                "symbol":          sym,
                "price":           round(price, 2),
                "max_alloc_usd":   round(max_alloc, 2),
                "shares_possible": shares,
                "affordable":      shares >= 1,
                "pct_of_equity":   round(price / equity * 100, 1) if equity > 0 else None,
                "decision":        "OK" if shares >= 1 else "SKIP — unaffordable at current price",
            })
        except Exception as exc:
            results.append({"symbol": sym, "affordable": False, "error": str(exc)})

    affordable_count = sum(1 for r in results if r.get("affordable"))
    return {
        "equity":            equity,
        "max_alloc_usd":     round(max_alloc, 2),
        "alloc_pct":         MAX_ALLOCATION_PCT,
        "affordable_count":  affordable_count,
        "total_symbols":     len(TRADE_WATCHLIST),
        "symbols":           results,
    }


@app.get("/readiness-check")
def readiness_check():
    """
    Live-trading readiness gate. All checks must pass before setting DRY_RUN=false.
    Run this after ≥10 days of paper trading before considering a live transition.
    """
    checks = []
    all_passed = True

    def _chk(name: str, passed: bool, reason: str):
        nonlocal all_passed
        if not passed:
            all_passed = False
        checks.append({"check": name, "passed": passed, "reason": reason})

    # 1. DRY_RUN must still be true (user controls the live switch manually)
    _chk("dry_run_guard",
         DRY_RUN,
         "DRY_RUN=true ✓ — system is in safe paper mode"
         if DRY_RUN
         else "WARNING: DRY_RUN=false — real orders will be submitted if you start the bot")

    # 2. ALLOW_LIVE_TRADING must be false (default safe state)
    allow_live = os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"
    _chk("live_trading_locked",
         not allow_live,
         "ALLOW_LIVE_TRADING=false ✓ — live-money lock is engaged"
         if not allow_live
         else "WARNING: ALLOW_LIVE_TRADING=true — live gate is UNLOCKED")

    # 3. Minimum 10 dry-run session days logged
    try:
        with journal._conn() as con:
            distinct_days = con.execute(
                "SELECT COUNT(DISTINCT DATE(timestamp)) FROM trade_events"
            ).fetchone()[0]
    except Exception:
        distinct_days = 0
    MIN_SESSIONS = 10
    _chk("min_sessions",
         distinct_days >= MIN_SESSIONS,
         f"{distinct_days} session day(s) logged (need ≥{MIN_SESSIONS})"
         + (" ✓" if distinct_days >= MIN_SESSIONS else " — keep running dry-run sessions"))

    # 4. No ERROR signals in last 5 days
    try:
        with journal._conn() as con:
            recent_errors = con.execute(
                "SELECT COUNT(*) FROM trade_events WHERE signal='ERROR' "
                "AND timestamp >= datetime('now', '-5 days')"
            ).fetchone()[0]
    except Exception:
        recent_errors = 999
    _chk("no_recent_errors",
         recent_errors == 0,
         "No ERROR signals in last 5 days ✓"
         if recent_errors == 0
         else f"{recent_errors} ERROR signal(s) in last 5 days — investigate before going live")

    # 5. No qty=0 journal entries
    try:
        with journal._conn() as con:
            qty_zero = con.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE qty=0 OR qty IS NULL"
            ).fetchone()[0]
    except Exception:
        qty_zero = 999
    _chk("no_qty_zero",
         qty_zero == 0,
         "No qty=0 journal entries ✓"
         if qty_zero == 0
         else f"{qty_zero} qty=0 entry(s) found — sizing guard may be broken")

    # 6. Daily loss limit has been triggered at least once (proves it works)
    try:
        with journal._conn() as con:
            dll_count = con.execute(
                "SELECT COUNT(*) FROM trade_events WHERE blocked_by='daily_loss_limit'"
            ).fetchone()[0]
    except Exception:
        dll_count = 0
    _chk("daily_loss_limit_tested",
         dll_count > 0,
         f"Daily loss limit triggered {dll_count} time(s) ✓"
         if dll_count > 0
         else "Daily loss limit never triggered — manually test by simulating a 3%+ equity drop")

    # 7. Stop-loss exits observed (proves exit logic works)
    try:
        with journal._conn() as con:
            sl_exits = con.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE exit_reason LIKE '%stop%'"
            ).fetchone()[0]
    except Exception:
        sl_exits = 0
    _chk("stop_loss_observed",
         sl_exits > 0,
         f"Stop-loss exits observed {sl_exits} time(s) ✓"
         if sl_exits > 0
         else "No stop-loss exits observed — run /backtest/{symbol} to validate stop logic")

    # 8. Expectancy is not deeply negative
    perf = {}
    try:
        perf = journal.query_performance_summary()
    except Exception:
        pass
    if "expectancy" in perf:
        exp = float(perf["expectancy"])
        _chk("expectancy_acceptable",
             exp >= -10.0,
             f"Expectancy=${exp:.4f} ✓" if exp >= -10.0
             else f"Expectancy=${exp:.4f} — too negative; review strategy before going live")
    else:
        _chk("expectancy_acceptable",
             False,
             "Not enough closed trades to compute expectancy (run /backtest/{symbol})")

    # 9. Max drawdown within safe limit ($150 = 15% of $1k account)
    if "max_drawdown" in perf:
        max_dd = float(perf["max_drawdown"])
        DD_LIMIT = 150.0
        _chk("max_drawdown_safe",
             max_dd <= DD_LIMIT,
             f"Max drawdown=${max_dd:.2f} ≤ ${DD_LIMIT:.2f} ✓"
             if max_dd <= DD_LIMIT
             else f"Max drawdown=${max_dd:.2f} exceeds ${DD_LIMIT:.2f} limit")

    # 10. Win rate above 35% (minimum viability)
    if "win_rate" in perf:
        wr = float(perf["win_rate"])
        _chk("win_rate_viable",
             wr >= 35.0,
             f"Win rate={wr:.1f}% ✓" if wr >= 35.0
             else f"Win rate={wr:.1f}% is below 35% minimum")

    passed = sum(1 for c in checks if c["passed"])
    total  = len(checks)

    # Overall verdict — only ready if all hard checks pass AND still in dry-run
    hard_failed = [c for c in checks if not c["passed"] and c["check"] not in
                   ("dry_run_guard", "live_trading_locked")]
    is_ready = len(hard_failed) == 0

    return {
        "ready_for_live":  is_ready and not DRY_RUN and allow_live,
        "checks_passed":   passed,
        "checks_total":    total,
        "verdict": (
            "ALL CHECKS PASSED — but DRY_RUN=false + ALLOW_LIVE_TRADING=true are still required to go live"
            if is_ready
            else f"NOT READY — {total - passed} check(s) failed. Resolve all issues first."
        ),
        "checks":          checks,
        "performance":     perf,
    }


@app.get("/performance-summary")
def performance_summary():
    """Aggregate PnL, win rate, expectancy, drawdown across all closed paper trades."""
    return journal.query_performance_summary()


@app.get("/symbol-performance")
def symbol_performance():
    """Per-symbol breakdown: setups, entries, win rate, PnL, avg R, blocker counts."""
    return journal.query_symbol_performance()


@app.get("/recent-trades")
def recent_trades(limit: int = 20):
    """Most recent paper trade lifecycles (open and closed), newest first."""
    return journal.query_recent_trades(limit=limit)


@app.post("/test-buy/{symbol}")
def test_buy(symbol: str):
    if not is_market_open():
        return {"message": "Market is closed"}
    url   = f"{BASE_URL}/v2/orders"
    order = {"symbol": symbol, "qty": 1, "side": "buy", "type": "market", "time_in_force": "day"}
    try:
        response = requests.post(url, json=order, headers=_headers(), timeout=10)
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


# ── Backtest helpers ──────────────────────────────────────────────────────────
def _enrich_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Add all V3 indicator columns to a daily-bar DataFrame for backtesting."""
    df = df.copy()
    df["sma_20"] = df["c"].rolling(window=20).mean()
    df["sma_50"] = df["c"].rolling(window=50).mean()

    if "v" in df.columns:
        df["vol_sma_20"] = df["v"].rolling(window=20).mean()

    df["rsi"]         = _rsi_series(df["c"], RSI_PERIOD)
    ml, sl            = _macd_series(df["c"])
    df["macd_line"]   = ml
    df["macd_signal"] = sl
    # MACD histogram direction — mirrors live EARLY_TREND_REQUIRE_MACD_IMPROVING logic
    df["macd_histogram"]        = df["macd_line"] - df["macd_signal"]
    df["macd_histogram_rising"] = df["macd_histogram"] > df["macd_histogram"].shift(1)
    # Breakout high: max close of the prior BREAKOUT_LOOKBACK bars (not including current bar)
    df["breakout_high"] = df["c"].shift(1).rolling(window=BREAKOUT_LOOKBACK).max()

    # Early trend indicator: True when SMA20 has risen for each of the last SMA20_RISING_BARS bars.
    df["sma20_rising"] = (
        df["sma_20"]
        .rolling(window=SMA20_RISING_BARS + 1, min_periods=SMA20_RISING_BARS + 1)
        .apply(lambda x: int(all(x[i] < x[i + 1] for i in range(len(x) - 1))), raw=True)
        .fillna(0)
        .astype(bool)
    )

    return df


@app.get("/backtest/{symbol}")
def backtest(symbol: str):
    """
    V3 backtest: applies the full V3 entry filter chain and models
    stop-loss, take-profit, and signal-based exits.
    Returns win/loss stats, avg win/loss, profit factor, and max drawdown.
    """
    df = _fetch_bars(symbol, timeframe="1Day", days=365, limit=500)

    if df.empty:
        return {"error": "No data returned for symbol"}

    df = _enrich_dataframe(df)
    df = df.dropna(subset=["sma_20", "sma_50"]).reset_index(drop=True)

    if df.empty:
        return {"error": "Not enough data to calculate indicators"}

    # Pre-check which columns are available
    has_volume_col              = "vol_sma_20"          in df.columns and "v" in df.columns
    has_rsi_col                 = "rsi"                 in df.columns
    has_macd_col                = "macd_line"           in df.columns and "macd_signal" in df.columns
    has_breakout_col            = "breakout_high"       in df.columns
    has_sma20_rising_col        = "sma20_rising"        in df.columns
    has_macd_histogram_rising_col = "macd_histogram_rising" in df.columns

    in_trade         = False
    entry_date       = entry_price = stop_loss_price = take_profit_price = None
    trades           = []
    running_equity   = 1.0
    equity_curve     = [1.0]

    for _, row in df.iterrows():
        close  = float(row["c"])
        sma_20 = float(row["sma_20"])
        sma_50 = float(row["sma_50"])
        date   = row["t"]

        if in_trade:
            exit_reason = None
            if close <= stop_loss_price:
                exit_reason = "stop_loss"
            elif close >= take_profit_price:
                exit_reason = "take_profit"
            elif close < sma_20:
                exit_reason = "signal_exit"

            if exit_reason:
                pnl     = round(close - entry_price, 2)
                pnl_pct = round((close - entry_price) / entry_price * 100, 2)
                running_equity *= (1 + (close - entry_price) / entry_price)
                equity_curve.append(running_equity)
                trades.append({
                    "side":              "long",
                    "entry_date":        entry_date,
                    "entry_price":       round(entry_price,       2),
                    "exit_date":         date,
                    "exit_price":        round(close,             2),
                    "stop_loss_price":   round(stop_loss_price,   2),
                    "take_profit_price": round(take_profit_price, 2),
                    "pnl":               pnl,
                    "pnl_pct":           pnl_pct,
                    "exit_reason":       exit_reason,
                })
                in_trade   = False
                entry_date = entry_price = stop_loss_price = take_profit_price = None
            continue  # stay in trade or move to next bar after exit

        # V3 two-tier entry conditions (mirrors live signal logic)
        # Tier 1: Strong trend — SMA20 above SMA50
        strong_trend = close > sma_20 and sma_20 > sma_50

        # Tier 2: Early trend — price above SMA20, SMA20 near SMA50 and rising
        early_trend = False
        if ALLOW_EARLY_TREND_ENTRY and not strong_trend and close > sma_20:
            sma_gap = (sma_50 - sma_20) / sma_50 if sma_50 > 0 else 1.0
            rising  = bool(row["sma20_rising"]) if has_sma20_rising_col else False
            if sma_gap <= EARLY_TREND_MAX_SMA_GAP_PCT and rising:
                early_trend = True
                # Mirror live EARLY_TREND_REQUIRE_MACD_IMPROVING gate for backtest accuracy
                if EARLY_TREND_REQUIRE_MACD_IMPROVING and has_macd_histogram_rising_col:
                    hist_val = row.get("macd_histogram_rising")
                    if hist_val is not None and not pd.isna(hist_val):
                        if not bool(hist_val):
                            early_trend = False

        if not (strong_trend or early_trend):
            continue

        # Trend strength only enforced for strong-trend tier
        if strong_trend:
            trend_strength = abs(sma_20 - sma_50) / sma_50
            if trend_strength < MIN_TREND_STRENGTH:
                continue

        # Volume — relaxed to MIN_VOLUME_RATIO of 20-day average
        if has_volume_col and not pd.isna(row["vol_sma_20"]):
            if float(row["v"]) < float(row["vol_sma_20"]) * MIN_VOLUME_RATIO:
                continue

        if has_rsi_col and not pd.isna(row["rsi"]):
            if float(row["rsi"]) >= RSI_OVERBOUGHT:
                continue

        if has_macd_col and not pd.isna(row["macd_line"]) and not pd.isna(row["macd_signal"]):
            if float(row["macd_line"]) <= float(row["macd_signal"]):
                continue

        # Breakout only enforced when REQUIRE_BREAKOUT_FOR_BUY=true
        if REQUIRE_BREAKOUT_FOR_BUY and has_breakout_col and not pd.isna(row["breakout_high"]):
            if close <= float(row["breakout_high"]):
                continue

        in_trade         = True
        entry_date       = date
        entry_price      = close
        stop_loss_price  = round(entry_price * (1 - STOP_LOSS_PCT),   2)
        take_profit_price = round(entry_price * (1 + TAKE_PROFIT_PCT), 2)

    # Close any open trade at end of data
    if in_trade:
        last       = df.iloc[-1]
        exit_price = float(last["c"])
        pnl        = round(exit_price - entry_price, 2)
        pnl_pct    = round((exit_price - entry_price) / entry_price * 100, 2)
        running_equity *= (1 + (exit_price - entry_price) / entry_price)
        equity_curve.append(running_equity)
        trades.append({
            "side":              "long",
            "entry_date":        entry_date,
            "entry_price":       round(entry_price,       2),
            "exit_date":         last["t"],
            "exit_price":        round(exit_price,        2),
            "stop_loss_price":   round(stop_loss_price,   2),
            "take_profit_price": round(take_profit_price, 2),
            "pnl":               pnl,
            "pnl_pct":           pnl_pct,
            "exit_reason":       "final_bar_exit",
        })

    # ── Summary metrics ────────────────────────────────────────────────────────
    total_trades  = len(trades)
    wins          = [t for t in trades if t["pnl"] > 0]
    losses        = [t for t in trades if t["pnl"] <= 0]
    win_rate      = round(len(wins) / total_trades * 100, 1) if total_trades > 0 else 0.0

    avg_win       = round(sum(t["pnl"] for t in wins)   / len(wins),   2) if wins   else 0.0
    avg_loss      = round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0.0
    gross_profit  = sum(t["pnl"] for t in wins)                              if wins   else 0.0
    gross_loss    = abs(sum(t["pnl"] for t in losses))                        if losses else 0.0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None
    total_profit  = round(sum(t["pnl"] for t in trades), 2)
    total_return_pct = round(
        sum((t["exit_price"] - t["entry_price"]) / t["entry_price"] for t in trades) * 100, 2
    ) if trades else 0.0

    # Max drawdown from equity curve
    peak   = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    max_drawdown_pct = round(max_dd * 100, 2)

    return {
        "symbol":           symbol,
        "total_trades":     total_trades,
        "winning_trades":   len(wins),
        "losing_trades":    len(losses),
        "win_rate":         win_rate,
        "avg_win":          avg_win,
        "avg_loss":         avg_loss,
        "profit_factor":    profit_factor,
        "total_profit":     total_profit,
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "trades":           trades,
    }


@app.get("/positions-watchlist")
def positions_watchlist():
    results = []
    for symbol in WATCHLIST:
        try:
            data = get_position(symbol)
            print(f"[positions_watchlist] {symbol} | {data}")

            if not data or "qty" not in data:
                results.append({
                    "symbol": symbol, "qty": 0, "side": "flat",
                    "market_value": 0, "unrealized_pl": 0,
                })
                continue

            qty  = int(float(data["qty"]))
            side = "long" if qty > 0 else "short" if qty < 0 else "flat"
            results.append({
                "symbol":       symbol,
                "qty":          qty,
                "side":         side,
                "market_value": float(data.get("market_value",  0)),
                "unrealized_pl": float(data.get("unrealized_pl", 0)),
            })
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})

    return {"results": results}


@app.post("/trade-watchlist")
def trade_watchlist():
    global _last_scan_at, _total_scan_cycles, _daily_loss_shutdown, _observe_only_mode
    global _session_flattened, _consecutive_losses, _loss_cooldown_until
    global _session_symbol_trade_count, _session_trade_date, _session_start
    global _opening_range, _session_highs, _stop_loss_times, _session_stop_count
    global _market_cooldown_until, _breakeven_armed, _breakeven_stops
    global _last_telegram_scan_summary_at

    _last_scan_at = datetime.now(timezone.utc)
    _total_scan_cycles += 1

    # Day-rollover reset: when the calendar date changes reset all per-session state.
    # This handles the case where the API process runs continuously across midnight.
    _today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _session_trade_date and _today_key != _session_trade_date:
        _session_flattened = False
        _consecutive_losses = 0
        _loss_cooldown_until = None
        _daily_loss_shutdown = False
        _session_symbol_trade_count.clear()
        _session_trade_date = _today_key
        # Reset in-memory session lists so daily-report/near-misses reflect only today.
        # Historical journal data is preserved — only the in-memory view resets.
        _session_start = datetime.now(timezone.utc)
        trade_log.clear()
        _near_miss_symbols.clear()
        _session_exits.clear()
        # S-tier state resets
        _opening_range.clear()
        _session_highs.clear()
        _stop_loss_times.clear()
        _session_stop_count = 0
        _market_cooldown_until = None
        _breakeven_armed.clear()
        _breakeven_stops.clear()
        print(
            f"[day_reset] New trading day {_today_key} — full session state reset: "
            f"session_flattened=False | consecutive_losses=0 | loss_cooldown=None | "
            f"daily_loss_shutdown=False | trade_log cleared | near_misses cleared | "
            f"session_exits cleared | opening_range cleared | session_highs cleared | "
            f"stop_cooldowns cleared | breakeven cleared | session_start updated"
        )

    _et_now = datetime.now(ZoneInfo("America/New_York"))
    print(
        f"[scan] CYCLE #{_total_scan_cycles} START | "
        f"{_et_now.strftime('%H:%M:%S ET')} | "
        f"market_open={_last_known_market_state} | "
        f"watchlist={TRADE_WATCHLIST} | "
        f"dry_run={DRY_RUN}"
    )

    # ── Hard daily loss check — fires once per session, immediately shuts down ─
    if not _daily_loss_shutdown:
        loss_reached, loss_reason = _is_daily_loss_limit_reached()
        if loss_reached:
            _daily_loss_shutdown = True
            _observe_only_mode = True
            print(
                f"[daily_loss] HARD SHUTDOWN TRIGGERED | reason=daily_loss_limit_hit | "
                f"{loss_reason}"
            )
            print(
                f"[daily_loss] reason=new_entries_disabled | observe_only_mode=true"
            )
            # Immediately flatten all open positions
            print("[daily_loss] Flattening all open positions due to daily loss limit hit...")
            _flatten_all_positions("daily_loss_limit_hit")
            _log_evt(
                "daily_loss_warning",
                f"Daily loss limit hit — entries disabled | {loss_reason}",
                severity="error",
                data={"loss_reason": loss_reason},
            )
            send_telegram_alert("Daily Loss Shutdown", loss_reason, severity="error")

    # ── Position monitor: sync journal vs Alpaca, reconcile auto-closes ───────
    # Runs BEFORE trade logic so execute_trade() sees a clean journal state
    # (e.g., a bracket-filled stop-loss is already reconciled before we re-evaluate).
    position_statuses = _monitor_and_sync_positions()

    results         = []
    new_entry_taken = False  # Allow at most one new long entry per cycle
    for symbol in TRADE_WATCHLIST:
        # Regime symbols (SPY, QQQ, IWM) are market-direction filters only — never trade them.
        if symbol.upper() in REGIME_SYMBOLS:
            print(f"[safety] BLOCKED: {symbol} is a regime/index symbol — skipping trade execution")
            results.append({
                "symbol":           symbol,
                "signal":           "SKIP",
                "decision_summary": f"SKIP: {symbol} is a regime symbol — not eligible for trading",
                "blocked_by":       "regime_symbol",
                "new_entry_opened": False,
            })
            continue
        try:
            result = execute_trade(symbol, block_new_entry=new_entry_taken)
            if result.get("new_entry_opened"):
                new_entry_taken = True
            results.append({
                "symbol":            symbol,
                "signal":            result.get("signal"),
                "decision_summary":  result.get("decision_summary") or result.get("signal_reason"),
                "signal_reason":     result.get("signal_reason"),
                "entry_tier":        result.get("entry_tier"),
                "starting_qty":      result.get("starting_qty"),
                "actions":           result.get("actions", []),
                "message":           result.get("message"),
                "dry_run":           result.get("dry_run", DRY_RUN),
                "blocked_by":        result.get("blocked_by"),
                "stop_loss_price":   result.get("stop_loss_price"),
                "take_profit_price": result.get("take_profit_price"),
                "score":             result.get("score"),
                "grade":             result.get("grade"),
                "near_miss":         result.get("near_miss", False),
                "near_miss_gaps":    result.get("near_miss_gaps"),
                "new_entry_opened":  result.get("new_entry_opened", False),
            })
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})

    # Tally cycle stats
    _cycle_entered = sum(1 for r in results if r.get("new_entry_opened"))
    _cycle_exited  = sum(1 for r in results if r.get("signal") == "SELL" and (r.get("starting_qty") or 0) > 0)
    _cycle_errors  = sum(1 for r in results if r.get("error"))
    _cycle_signals = sum(1 for r in results if r.get("signal") == "BUY")
    _cycle_blocked = sum(1 for r in results if r.get("blocked_by"))

    # Top candidate this cycle — highest-scored result
    _scored_results = [r for r in results if r.get("score") is not None]
    _top_candidate  = max(_scored_results, key=lambda r: r.get("score", 0)) if _scored_results else None
    _top_sym     = _top_candidate.get("symbol")   if _top_candidate else None
    _top_score   = _top_candidate.get("score")    if _top_candidate else None
    _top_grade   = _top_candidate.get("grade")    if _top_candidate else None
    _top_blocker = _top_candidate.get("blocked_by") if _top_candidate else None
    _top_reason  = (
        (_top_candidate.get("decision_summary") or _top_candidate.get("signal_reason"))
        if _top_candidate else None
    )

    # Always log every scan cycle so /events has a complete record even on quiet days
    _log_evt(
        "scan_cycle_completed",
        f"Cycle #{_total_scan_cycles} | scanned={len(results)} | signals={_cycle_signals} | entered={_cycle_entered} | blocked={_cycle_blocked} | errors={_cycle_errors}",
        severity="info",
        data={
            "scan_cycle_number":     _total_scan_cycles,
            "symbols_evaluated":     len(results),
            "buy_signals_seen":      _cycle_signals,
            "entries_taken":         _cycle_entered,
            "entries_blocked":       _cycle_blocked,
            "errors":                _cycle_errors,
            "top_candidate_symbol":  _top_sym,
            "top_candidate_score":   _top_score,
            "top_candidate_grade":   _top_grade,
            "top_candidate_blocker": _top_blocker,
            "top_candidate_reason":  _top_reason,
            "market_open":           _last_known_market_state,
            "entries_allowed_now":   _entries_allowed_now(),
        },
    )

    # Throttled Telegram scan summary — at most once every 15 minutes
    _now_for_tg = datetime.now(timezone.utc)
    _tg_interval = 15 * 60  # seconds
    if (
        _last_telegram_scan_summary_at is None
        or (_now_for_tg - _last_telegram_scan_summary_at).total_seconds() >= _tg_interval
    ):
        _open_count = len(journal.get_open_paper_positions())
        _today_key2 = _now_for_tg.strftime("%Y-%m-%d")
        _daily_pnl  = 0.0
        try:
            with journal._conn() as _tg_con:
                _pnl_rows = _tg_con.execute(
                    f"SELECT realized_pnl FROM paper_trades WHERE {journal.ELIGIBLE_TRADE_SQL} AND DATE(exit_timestamp)=?",
                    (_today_key2,),
                ).fetchall()
                _daily_pnl = round(sum(r["realized_pnl"] for r in _pnl_rows if r["realized_pnl"]), 2)
        except Exception:
            pass
        _entries_so_far = sum(1 for e in trade_log if e.get("new_entry_opened"))
        _tg_lines = [f"Scan #{_total_scan_cycles} | {len(results)} symbols"]
        if _top_sym:
            _top_line = f"Top: {_top_sym} score={_top_score}[{_top_grade}]"
            if _top_blocker:
                _top_line += f" blocked={_top_blocker}"
            _tg_lines.append(_top_line)
        else:
            _tg_lines.append("Top: none scored")
        _tg_lines.append(f"Entries today: {_entries_so_far}")
        _tg_lines.append(f"Open positions: {_open_count}")
        _tg_lines.append(f"Realized P&L: ${_daily_pnl:+.2f}")
        send_telegram_alert("Scan Summary", "\n".join(_tg_lines), severity="info")
        _last_telegram_scan_summary_at = _now_for_tg

    return {
        "results":           results,
        "dry_run":           DRY_RUN,
        "position_statuses": position_statuses,
    }


@app.get("/market-status")
def market_status():
    return {"is_open": is_market_open()}


@app.get("/scan-watchlist")
def scan_watchlist():
    """V3: Full filtered signals (RSI, MACD, breakout, MTF) for each trade watchlist symbol."""
    results = []
    for symbol in TRADE_WATCHLIST:
        try:
            data = get_signal(symbol)
            if "error" in data:
                results.append({"symbol": symbol, "error": data["error"]})
                continue
            results.append({
                "symbol":                data["symbol"],
                "close":                 data["close"],
                "sma_20":                data["sma_20"],
                "sma_50":                data["sma_50"],
                "trend_strength":        data.get("trend_strength"),
                "trend_strong":          data.get("trend_strong"),
                "entry_tier":            data.get("entry_tier"),
                "current_volume":        data.get("current_volume"),
                "vol_sma_20":            data.get("vol_sma_20"),
                "volume_confirmed":      data.get("volume_confirmed"),
                "rsi":                   data.get("rsi"),
                "rsi_ok":                data.get("rsi_ok"),
                "macd_line":             data.get("macd_line"),
                "macd_signal_line":      data.get("macd_signal_line"),
                "macd_bullish":          data.get("macd_bullish"),
                "macd_histogram":        data.get("macd_histogram"),
                "macd_histogram_rising": data.get("macd_histogram_rising"),
                "breakout_high":         data.get("breakout_high"),
                "breakout_confirmed":    data.get("breakout_confirmed"),
                "spy_bullish":           data.get("spy_bullish"),
                "spy_reason":            data.get("spy_reason"),
                "intraday_confirmed":    data.get("intraday_confirmed"),
                "intraday_reason":       data.get("intraday_reason"),
                "intraday_margin_pct":   data.get("intraday_margin_pct"),
                "signal":                data["signal"],
                "signal_reason":         data.get("signal_reason"),
                "decision_summary":      data.get("decision_summary"),
            })
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})

    return {"results": results}


@app.get("/runtime-stats")
def runtime_stats():
    """
    Real-time scan loop diagnostics.
    Shows current ET time, market/window state, cadence, and why the bot is or isn't scanning.
    Safe to poll frequently — reads only in-memory state, makes no external calls.
    """
    ET = ZoneInfo("America/New_York")
    now_utc = datetime.now(timezone.utc)
    now_et = datetime.now(ET)
    et_str = now_et.strftime("%H:%M:%S ET (%Y-%m-%d)")

    try:
        ws = TRADING_WINDOW_START.split(":")
        window_start = dt_time(int(ws[0]), int(ws[1]))
        we = TRADING_WINDOW_END.split(":")
        window_end = dt_time(int(we[0]), int(we[1]))
    except Exception:
        window_start = dt_time(9, 35)
        window_end = dt_time(11, 30)

    et_time = now_et.time()
    market_is_open = _last_known_market_state
    inside_window = window_start <= et_time < window_end
    opening_cutover = dt_time(10, 0)
    opening_momentum_mode = inside_window and (market_is_open is True) and et_time < opening_cutover

    if market_is_open is False:
        reason_not_scanning = "market_closed"
        next_action = "waiting for NYSE open (09:30 ET)"
        scan_cadence = 1800
    elif market_is_open is None:
        reason_not_scanning = "market_status_unknown"
        next_action = "retrying market status check (transient error)"
        scan_cadence = 60
    elif et_time < window_start:
        reason_not_scanning = "before_trading_window"
        mins_to_start = (window_start.hour * 60 + window_start.minute) - (et_time.hour * 60 + et_time.minute)
        next_action = f"waiting for window open ({TRADING_WINDOW_START} ET, ~{mins_to_start}m away)"
        scan_cadence = 60 if mins_to_start <= 35 else 300
    elif et_time >= window_end:
        reason_not_scanning = "after_trading_window"
        flatten_tag = " | flatten already triggered" if _session_flattened else " | flatten pending"
        next_action = f"trading done for today{flatten_tag}"
        scan_cadence = 1800
    else:
        reason_not_scanning = None
        cadence_tag = "60s opening momentum" if opening_momentum_mode else "300s normal"
        next_action = f"scanning ({cadence_tag})"
        scan_cadence = 60 if opening_momentum_mode else 300

    return {
        "current_et_time":         et_str,
        "market_is_open":          market_is_open,
        "inside_trading_window":   inside_window,
        "trading_window":          f"{TRADING_WINDOW_START}–{TRADING_WINDOW_END} ET",
        "opening_momentum_mode":   opening_momentum_mode,
        "next_action":             next_action,
        "reason_not_scanning":     reason_not_scanning,
        "last_scan_at_utc":        _last_scan_at.isoformat() if _last_scan_at else None,
        "last_scan_at_et":         (
            _last_scan_at.astimezone(ET).strftime("%H:%M:%S ET")
            if _last_scan_at else None
        ),
        "total_scan_cycles":       _total_scan_cycles,
        "scan_cadence_seconds":    scan_cadence,
        "session_start_utc":       _session_start.isoformat(),
        "uptime_minutes":          round((now_utc - _session_start).total_seconds() / 60, 1),
        "watchlist":               TRADE_WATCHLIST,
        "session_mini_stats": {
            "total_symbols_evaluated": len(trade_log),
            "entered":                 sum(1 for e in trade_log if e.get("new_entry_opened")),
            "near_misses":             sum(1 for e in trade_log if e.get("near_miss")),
            "errors":                  sum(1 for e in trade_log if e.get("signal") == "ERROR"),
        },
        "safety_flags": {
            "flatten_at_window_end": FLATTEN_AT_WINDOW_END,
            "session_flattened":     _session_flattened,
            "observe_only_mode":     _observe_only_mode,
            "disable_new_entries":   DISABLE_NEW_ENTRIES,
            "alpaca_paper":          ALPACA_PAPER,
            "allow_live_trading":    ALLOW_LIVE_TRADING,
            "dry_run":               DRY_RUN,
        },
    }


@app.get("/strategy-status")
def strategy_status():
    """
    Current strategy state: mode, signal filters, risk config, and live session state.
    Use before market open to confirm the bot is configured correctly.
    """
    open_count = _count_open_long_positions()
    loss_reached, loss_reason = _is_daily_loss_limit_reached()
    cooldown_symbols = [
        s for s, exp in _symbol_error_cooldown.items()
        if datetime.now(timezone.utc) < exp
    ]
    trade_cooldowns = {
        sym: {
            "elapsed_min": round((datetime.now(timezone.utc) - last).total_seconds() / 60, 1),
            "remaining_min": max(0.0, round(TRADE_COOLDOWN_MINUTES - (datetime.now(timezone.utc) - last).total_seconds() / 60, 1)),
        }
        for sym, last in _last_trade_time.items()
    }
    return {
        **_execution_mode_fields(),
        # Deprecated: kept for any script still reading "mode" as a string.
        # Use execution_mode instead.
        "mode":            "DRY_RUN" if DRY_RUN else "PAPER_LIVE",
        "paper_confirmed": ALPACA_PAPER and not ALLOW_LIVE_TRADING,
        "signal_filters": {
            "require_spy_bullish":                REQUIRE_SPY_BULLISH,
            "allow_neutral_spy_entries":          ALLOW_NEUTRAL_SPY_ENTRIES,
            "neutral_spy_min_score":              NEUTRAL_SPY_MIN_SCORE,
            "bearish_spy_exception_min_score":    BEARISH_SPY_EXCEPTION_MIN_SCORE,
            "bearish_spy_exception_min_vol_ratio": BEARISH_SPY_EXCEPTION_MIN_VOLUME_RATIO,
            "bearish_spy_exception_require_macd": BEARISH_SPY_EXCEPTION_REQUIRE_MACD,
            "require_intraday_confirmation":      REQUIRE_INTRADAY_CONFIRMATION,
            "require_breakout_for_buy":           REQUIRE_BREAKOUT_FOR_BUY,
            "allow_early_trend_entry":            ALLOW_EARLY_TREND_ENTRY,
            "early_trend_require_macd_improving": EARLY_TREND_REQUIRE_MACD_IMPROVING,
            "min_volume_ratio":                   MIN_VOLUME_RATIO,
            "rsi_overbought":                     RSI_OVERBOUGHT,
            "min_entry_score":                    MIN_ENTRY_SCORE,
            "allow_b_setup_entries":              ALLOW_B_SETUP_ENTRIES,
        },
        "risk": {
            "paper_account_equity":   PAPER_ACCOUNT_EQUITY,
            "risk_per_trade_pct":     RISK_PER_TRADE_PCT,
            "max_dollar_risk_trade":  round(PAPER_ACCOUNT_EQUITY * RISK_PER_TRADE_PCT, 2),
            "stop_loss_pct":          STOP_LOSS_PCT,
            "take_profit_pct":        TAKE_PROFIT_PCT,
            "max_allocation_pct":     MAX_ALLOCATION_PCT,
            "max_open_positions":     MAX_OPEN_POSITIONS,
            "max_trades_per_symbol":  MAX_TRADES_PER_SYMBOL,
            "daily_loss_limit_pct":   DAILY_LOSS_LIMIT_PCT,
            "trade_cooldown_min":     TRADE_COOLDOWN_MINUTES,
            "flatten_at_window_end":  FLATTEN_AT_WINDOW_END,
        },
        "live_state": {
            "open_positions_count":  open_count,
            "daily_loss_hit":        loss_reached,
            "daily_loss_reason":     loss_reason if loss_reached else None,
            "observe_only_mode":     _observe_only_mode,
            "disable_new_entries":   DISABLE_NEW_ENTRIES,
            "api_failure_count":     _api_failure_count,
            "symbols_error_cooldown": cooldown_symbols,
            "trade_cooldowns":       trade_cooldowns,
            "session_flattened":     _session_flattened,
        },
    }


@app.get("/recent-signals")
def recent_signals(limit: int = 20):
    """
    Recent BUY signals from this session, newest first.
    Shows score, grade, whether the entry was taken, and what blocked it if not.
    Returns empty list (not an error) when no signals exist yet.
    """
    if not trade_log:
        return {
            "message": "No signals logged yet — bot may not have scanned during open market hours.",
            "signals": [],
            "total":   0,
        }
    buy_signals = [e for e in trade_log if e.get("signal") == "BUY"]
    recent = list(reversed(buy_signals[-limit:]))
    return {
        "total_buy_signals": len(buy_signals),
        "showing":           len(recent),
        "signals": [
            {
                "timestamp":        e.get("timestamp"),
                "symbol":           e.get("symbol"),
                "score":            e.get("score"),
                "grade":            e.get("grade"),
                "entry_tier":       e.get("entry_tier"),
                "entered":          e.get("new_entry_opened", False),
                "blocked_by":       e.get("blocked_by"),
                "decision_summary": e.get("decision_summary"),
                "rsi":              e.get("rsi"),
                "macd_histogram":   e.get("macd_histogram"),
                "volume_confirmed": e.get("volume_confirmed"),
                "spy_bullish":      e.get("spy_bullish"),
                "intraday_confirmed": e.get("intraday_confirmed"),
            }
            for e in recent
        ],
    }


@app.get("/blocked-signals")
def blocked_signals(limit: int = 30):
    """
    Signals that were blocked this session, with blocker reasons and counts.
    Useful for diagnosing why the bot didn't enter when it could have.
    Returns empty list (not an error) when nothing was blocked.
    """
    from collections import Counter
    if not trade_log:
        return {
            "message":       "No signals logged yet.",
            "blocked":       [],
            "total_blocked": 0,
        }
    blocked = [e for e in trade_log if e.get("blocked_by")]
    recent = list(reversed(blocked[-limit:]))
    blocker_summary = dict(Counter(e.get("blocked_by") for e in blocked))
    return {
        "total_blocked":   len(blocked),
        "blocker_summary": blocker_summary,
        "showing":         len(recent),
        "blocked_entries": [
            {
                "timestamp":        e.get("timestamp"),
                "symbol":           e.get("symbol"),
                "blocked_by":       e.get("blocked_by"),
                "signal":           e.get("signal"),
                "score":            e.get("score"),
                "grade":            e.get("grade"),
                "decision_summary": e.get("decision_summary"),
            }
            for e in recent
        ],
    }


@app.get("/top-near-misses")
def top_near_misses():
    """
    Symbols that almost qualified this session (score 60–69) but fell short.
    Shows score component breakdown and which dimensions had the largest gaps.
    Returns empty list (not an error) when no near-misses have been recorded.
    """
    if not _near_miss_symbols:
        return {
            "message":    "No near-misses logged this session.",
            "near_misses": [],
            "total":      0,
        }
    sorted_nm = sorted(_near_miss_symbols, key=lambda x: x.get("score", 0), reverse=True)
    # Deduplicate by symbol (keep highest score per symbol across cycles)
    seen: set = set()
    deduped = []
    for nm in sorted_nm:
        sym = nm.get("symbol")
        if sym not in seen:
            seen.add(sym)
            deduped.append(nm)

    # Annotate each near-miss with a human-readable component breakdown
    enriched = []
    for nm in deduped:
        comp = nm.get("components", {})
        enriched.append({
            "symbol":         nm.get("symbol"),
            "score":          nm.get("score"),
            "grade":          nm.get("grade"),
            "spy_regime":     nm.get("spy_regime"),
            "macd_state":     nm.get("macd_state"),
            "rsi":            nm.get("rsi"),
            "intraday":       nm.get("intraday"),
            "gaps":           nm.get("gaps"),
            "score_components": {
                "trend":        f"{comp.get('trend',0)}/25",
                "volume":       f"{comp.get('volume',0)}/15",
                "rsi":          f"{comp.get('rsi',0)}/15",
                "macd":         f"{comp.get('macd',0)}/15",
                "intraday":     f"{comp.get('intraday',0)}/10",
                "regime":       f"{comp.get('regime',0)}/10",
                "affordability":f"{comp.get('affordability',0)}/5",
                "breakout":     f"{comp.get('breakout',0)}/5",
            },
        })

    return {
        "total":       len(_near_miss_symbols),
        "unique_syms": len(deduped),
        "near_misses": enriched,
        "tip": (
            "These setups almost qualified. Review 'score_components' and 'gaps' "
            "to see which dimensions need improvement. "
            "Setups with spy_regime=bearish will not benefit from Quality B mode."
        ),
    }


@app.get("/backtest-watchlist")
def backtest_watchlist():
    results = []
    for symbol in TRADE_WATCHLIST:
        try:
            data = backtest(symbol)
            if "error" in data:
                results.append({"symbol": symbol, "error": data["error"]})
                continue
            results.append({
                "symbol":           data["symbol"],
                "total_trades":     data["total_trades"],
                "winning_trades":   data["winning_trades"],
                "losing_trades":    data["losing_trades"],
                "win_rate":         data["win_rate"],
                "avg_win":          data.get("avg_win"),
                "avg_loss":         data.get("avg_loss"),
                "profit_factor":    data.get("profit_factor"),
                "total_profit":     data["total_profit"],
                "total_return_pct": data["total_return_pct"],
                "max_drawdown_pct": data.get("max_drawdown_pct"),
            })
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})

    valid_results = [r for r in results if "error" not in r]

    if not valid_results:
        return {"error": "No backtest data available for any symbol in the watchlist", "results": results}

    best_by_profit   = max(valid_results, key=lambda r: r["total_profit"])["symbol"]
    best_by_win_rate = max(valid_results, key=lambda r: r["win_rate"])["symbol"]

    return {
        "results":               results,
        "best_symbol_by_profit":   best_by_profit,
        "best_symbol_by_win_rate": best_by_win_rate,
    }


@app.get("/post-market-review")
def post_market_review():
    """
    Post-market session review — run after 11:30 ET or after market close.
    Summarizes scans, signals, entries, exits, realized P&L, win rate, near-misses,
    blocked reasons, open positions, risk warnings, and suggested adjustments for tomorrow.
    All data is read-only from in-memory state and the journal DB.
    """
    from collections import Counter

    total_symbols_evaluated = len(trade_log)
    buy_signals  = sum(1 for e in trade_log if e.get("signal") == "BUY")
    entered      = sum(1 for e in trade_log if e.get("new_entry_opened"))
    exited       = sum(1 for e in trade_log if e.get("signal") == "SELL" and (e.get("starting_qty") or 0) > 0)
    blocked      = sum(1 for e in trade_log if e.get("blocked_by"))
    errors       = sum(1 for e in trade_log if e.get("signal") == "ERROR")
    blocked_breakdown = dict(Counter(e.get("blocked_by") for e in trade_log if e.get("blocked_by")))

    # P&L from journal (session only)
    session_realized_pnl = 0.0
    session_wins = 0
    session_closed = 0
    try:
        with journal._conn() as con:
            rows = con.execute(
                f"SELECT realized_pnl FROM paper_trades WHERE {journal.ELIGIBLE_TRADE_SQL} AND exit_timestamp >= ?",
                (_session_start.isoformat(),),
            ).fetchall()
            session_closed = len(rows)
            session_realized_pnl = round(sum(r["realized_pnl"] for r in rows if r["realized_pnl"]), 4)
            session_wins = sum(1 for r in rows if r["realized_pnl"] and r["realized_pnl"] > 0)
    except Exception:
        pass

    win_rate = round(session_wins / session_closed * 100, 1) if session_closed > 0 else None

    # Open positions still on the books
    open_positions = journal.get_open_paper_positions()

    # Best near-miss
    best_nm = (
        max(_near_miss_symbols, key=lambda x: x.get("score", 0))
        if _near_miss_symbols else None
    )

    # Risk warnings
    risk_warnings = []
    if not FLATTEN_AT_WINDOW_END:
        risk_warnings.append(
            "FLATTEN_AT_WINDOW_END=false — positions may remain open overnight. "
            "Consider calling POST /flatten manually."
        )
    if open_positions and FLATTEN_AT_WINDOW_END and not _session_flattened:
        risk_warnings.append(
            f"{len(open_positions)} position(s) still open but flatten has not fired yet. "
            "Call POST /flatten if trading window has ended."
        )
    if open_positions and _session_flattened:
        risk_warnings.append(
            f"{len(open_positions)} position(s) still appear open in journal after flatten — "
            "verify Alpaca filled the close orders. Call GET /check-state."
        )
    if session_realized_pnl < -(PAPER_ACCOUNT_EQUITY * DAILY_LOSS_LIMIT_PCT):
        risk_warnings.append(
            f"Session realized loss ${abs(session_realized_pnl):.2f} exceeds "
            f"daily loss limit (${round(PAPER_ACCOUNT_EQUITY * DAILY_LOSS_LIMIT_PCT, 2):.2f}). "
            "Review trades before tomorrow."
        )
    if errors > 0:
        risk_warnings.append(
            f"{errors} ERROR signal(s) encountered — check Alpaca API connectivity "
            "and data feed. Run GET /health for details."
        )
    if _observe_only_mode:
        risk_warnings.append(
            "Bot is in observe-only mode due to repeated API failures. "
            "Restart the API server to reset this state."
        )

    # Suggested adjustments
    suggestions = []
    if total_symbols_evaluated == 0:
        suggestions.append(
            "No scans ran this session — verify run_bot.py is running and the API is reachable."
        )
    elif buy_signals == 0:
        suggestions.append(
            "No BUY signals generated. Likely causes: "
            "SPY filter blocked all entries (check GET /signal/SPY), "
            "symbols all in HOLD/downtrend, or market was closed during the window."
        )
    elif entered == 0 and buy_signals > 0:
        top_blocker = max(blocked_breakdown, key=blocked_breakdown.get) if blocked_breakdown else "unknown"
        suggestions.append(
            f"Had {buy_signals} BUY signal(s) but none entered. "
            f"Top blocker: '{top_blocker}'. "
            "Check GET /blocked-signals for the full breakdown."
        )
    if win_rate is not None and win_rate < 40 and session_closed >= 3:
        suggestions.append(
            f"Win rate is {win_rate}% (below 40% threshold). "
            f"Consider raising MIN_ENTRY_SCORE above {MIN_ENTRY_SCORE} or reviewing "
            "which signal dimensions are weakest with GET /top-near-misses."
        )
    if len(_near_miss_symbols) >= 3:
        suggestions.append(
            f"{len(_near_miss_symbols)} near-miss(es) today — many setups fell just short. "
            "Review GET /top-near-misses to see which scoring dimensions to watch."
        )
    if not suggestions:
        suggestions.append(
            "Session looks clean. Continue monitoring with same settings."
        )

    return {
        "generated_at_utc":  datetime.now(timezone.utc).isoformat(),
        "session_start_utc": _session_start.isoformat(),
        "session_summary": {
            "scan_cycles":             _total_scan_cycles,
            "symbols_evaluated":       total_symbols_evaluated,
            "buy_signals":             buy_signals,
            "entries":                 entered,
            "exits":                   exited,
            "blocked":                 blocked,
            "near_misses":             len(_near_miss_symbols),
            "errors":                  errors,
        },
        "performance": {
            "realized_pnl":         session_realized_pnl,
            "closed_trades":        session_closed,
            "winning_trades":       session_wins,
            "win_rate_pct":         win_rate,
            "open_positions_count": len(open_positions),
        },
        "blocked_reasons":    blocked_breakdown,
        "best_near_miss":     best_nm,
        "open_positions":     open_positions,
        "session_exits":      _session_exits,
        "risk_warnings":      risk_warnings if risk_warnings else ["No risk warnings."],
        "suggested_adjustments": suggestions,
        "active_config_snapshot": {
            "min_entry_score":                     MIN_ENTRY_SCORE,
            "allow_b_setup_entries":               ALLOW_B_SETUP_ENTRIES,
            "require_spy_bullish":                 REQUIRE_SPY_BULLISH,
            "allow_neutral_spy_entries":           ALLOW_NEUTRAL_SPY_ENTRIES,
            "neutral_spy_min_score":               NEUTRAL_SPY_MIN_SCORE,
            "bearish_spy_exception_min_score":     BEARISH_SPY_EXCEPTION_MIN_SCORE,
            "bearish_spy_exception_min_vol_ratio": BEARISH_SPY_EXCEPTION_MIN_VOLUME_RATIO,
            "stop_loss_pct":                       STOP_LOSS_PCT,
            "take_profit_pct":                     TAKE_PROFIT_PCT,
            "risk_per_trade_pct":                  RISK_PER_TRADE_PCT,
            "max_open_positions":                  MAX_OPEN_POSITIONS,
            "max_trades_per_symbol":               MAX_TRADES_PER_SYMBOL,
            "daily_loss_limit_pct":                DAILY_LOSS_LIMIT_PCT,
            "flatten_at_window_end":               FLATTEN_AT_WINDOW_END,
        },
    }


@app.get("/stable-performance")
def stable_performance():
    """
    Performance metrics for stable bot sessions only (since STABLE_V2_START_DATE).
    Filters out pre-stable legacy sessions that were run before the bot reached
    production-grade quality. Historical data is preserved — only the metrics view
    changes.

    Metrics: win rate, profit factor, expectancy, avg winner, avg loser,
             max drawdown, avg hold time, max consecutive losses, grade breakdown.
    """
    metrics = journal.query_stable_v2_performance(STABLE_V2_START_DATE)
    return {
        "stable_v2_start_date": STABLE_V2_START_DATE,
        "note": (
            f"Includes only trades entered on or after {STABLE_V2_START_DATE}. "
            f"Legacy sessions before this date are excluded from these metrics. "
            f"Historical journal data is intact — nothing was deleted."
        ),
        "metrics": metrics,
    }


@app.get("/session-safety-state")
def session_safety_state():
    """
    Live view of all in-session safety state: daily loss shutdown, loss cooldown,
    partial TP tracker, observe-only mode, and flatten state.
    Safe to poll frequently — reads only in-memory state.
    """
    cooldown_remaining = None
    if _loss_cooldown_until is not None:
        secs = max(0.0, (_loss_cooldown_until - datetime.now(timezone.utc)).total_seconds())
        cooldown_remaining = round(secs / 60, 1)

    return {
        "live_trading_disabled":              not ALLOW_LIVE_TRADING,
        "paper_mode_confirmed":               ALPACA_PAPER and not ALLOW_LIVE_TRADING,
        "session_flattened":                  _session_flattened,
        "disable_new_entries":                DISABLE_NEW_ENTRIES,
        "last_entry_time":                    LAST_ENTRY_TIME,
        "entries_allowed_now":                _entries_allowed_now(),
        "allow_neutral_spy_entries":          ALLOW_NEUTRAL_SPY_ENTRIES,
        "neutral_spy_min_score":              NEUTRAL_SPY_MIN_SCORE,
        "neutral_spy_min_volume_ratio":       NEUTRAL_SPY_MIN_VOLUME_RATIO,
        "daily_loss_shutdown":                _daily_loss_shutdown,
        "daily_loss_limit_pct":               DAILY_LOSS_LIMIT_PCT,
        "observe_only_mode":                  _observe_only_mode,
        "consecutive_losses":                 _consecutive_losses,
        "loss_cooldown_active":               _is_loss_cooldown_active(),
        "loss_cooldown_remaining_min":        cooldown_remaining,
        "max_consecutive_losses":             MAX_CONSECUTIVE_LOSSES,
        "loss_cooldown_minutes":              LOSS_COOLDOWN_MINUTES,
        "partial_tp_executed_syms":           sorted(_partial_tp_executed),
        "partial_tp_gain_pct":                PARTIAL_TP_GAIN_PCT,
        "partial_tp_sell_frac":               PARTIAL_TP_SELL_FRAC,
        "spread_filter_max_pct":              MAX_SPREAD_PCT,
        "force_exit_weak_after":              FORCE_EXIT_WEAK_AFTER,
        "force_exit_weak_gain_max":           FORCE_EXIT_WEAK_GAIN_MAX,
        "bearish_spy_exception_min_score":    BEARISH_SPY_EXCEPTION_MIN_SCORE,
    }


@app.get("/testing-readiness")
def testing_readiness():
    """
    Paper testing readiness dashboard.
    Shows current session safety state, stable performance metrics, and all
    real_money_ready criteria.  real_money_ready is always false during paper test
    and will remain false until every criterion is independently satisfied.
    """
    now_utc = datetime.now(timezone.utc)

    # Scan recency — flag if last scan was more than 15 minutes ago
    last_scan_age_min = (
        round((now_utc - _last_scan_at).total_seconds() / 60, 1)
        if _last_scan_at else None
    )
    stale_data_warning = last_scan_age_min is not None and last_scan_age_min > 15

    # Open orders
    open_orders_count = 0
    unresolved_open_orders = False
    try:
        ord_resp = requests.get(
            f"{BASE_URL}/v2/orders", headers=_headers(),
            params={"status": "open", "limit": 50}, timeout=10,
        )
        if ord_resp.status_code == 200:
            open_orders_count = len(ord_resp.json())
            unresolved_open_orders = open_orders_count > 0
    except Exception:
        pass

    # Open positions
    open_positions_count = _count_open_long_positions()

    # Today entries / errors from in-memory trade log
    today_str = now_utc.strftime("%Y-%m-%d")
    today_entries = sum(
        1 for e in trade_log
        if e.get("new_entry_opened") and (e.get("timestamp") or "")[:10] == today_str
    )
    today_errors = sum(
        1 for e in trade_log
        if e.get("signal") == "ERROR" and (e.get("timestamp") or "")[:10] == today_str
    )

    # Stable performance metrics
    stable = journal.query_stable_v2_performance(STABLE_V2_START_DATE)
    stable_total_closed      = stable.get("total_closed", 0)
    stable_win_rate          = stable.get("win_rate", 0.0) or 0.0
    stable_profit_factor     = stable.get("profit_factor", 0.0) or 0.0
    stable_total_pnl         = stable.get("total_pnl", 0.0) or 0.0
    stable_max_consec_losses = stable.get("max_consecutive_losses", 0) or 0

    # Real-money readiness criteria (all must be true before considering live trading)
    criteria = {
        "stable_total_closed_ge_30":     stable_total_closed >= 30,
        "stable_win_rate_ge_45":         stable_win_rate >= 45.0,
        "stable_profit_factor_ge_1_2":   stable_profit_factor >= 1.2,
        "stable_total_pnl_positive":     stable_total_pnl > 0,
        "max_consecutive_losses_le_3":   stable_max_consec_losses <= 3,
        "no_unresolved_open_orders":     not unresolved_open_orders,
        "no_stale_data_warning":         not stale_data_warning,
    }
    criteria_met  = sum(criteria.values())
    criteria_total = len(criteria)

    return {
        "paper_mode_confirmed":     ALPACA_PAPER and not ALLOW_LIVE_TRADING,
        "live_trading_disabled":    not ALLOW_LIVE_TRADING,
        "open_positions_count":     open_positions_count,
        "open_orders_count":        open_orders_count,
        "run_bot_active":           _is_run_bot_active(),
        "last_scan_at_utc":         _last_scan_at.isoformat() if _last_scan_at else None,
        "last_scan_age_minutes":    last_scan_age_min,
        "today_entries":            today_entries,
        "today_errors":             today_errors,
        "daily_loss_shutdown":      _daily_loss_shutdown,
        "loss_cooldown_active":     _is_loss_cooldown_active(),
        "session_flattened":        _session_flattened,
        "entries_allowed_now":      _entries_allowed_now(),
        "last_entry_time":          LAST_ENTRY_TIME,
        "stable_total_closed":      stable_total_closed,
        "stable_win_rate":          stable_win_rate,
        "stable_profit_factor":     stable_profit_factor,
        "stable_total_pnl":         stable_total_pnl,
        "real_money_ready":         False,
        "real_money_criteria":      criteria,
        "criteria_met":             criteria_met,
        "criteria_total":           criteria_total,
        "note": (
            "real_money_ready is always false during paper test. "
            f"Currently {criteria_met}/{criteria_total} criteria met. "
            "All criteria must be true before considering live trading."
        ),
    }


@app.get("/s-tier-readiness")
def s_tier_readiness():
    """
    S-tier paper testing readiness dashboard.
    Shows session safety state, S-tier filter status, stable performance, and risk warnings.
    real_money_ready is always false — this endpoint is for paper test monitoring only.
    """
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    # Scan recency
    last_scan_age_min = (
        round((now_utc - _last_scan_at).total_seconds() / 60, 1)
        if _last_scan_at else None
    )

    # Open orders and positions
    open_orders_count = 0
    try:
        ord_resp = requests.get(
            f"{BASE_URL}/v2/orders", headers=_headers(),
            params={"status": "open", "limit": 50}, timeout=10,
        )
        if ord_resp.status_code == 200:
            open_orders_count = len(ord_resp.json())
    except Exception:
        pass
    open_positions_count = _count_open_long_positions()

    # Today stats from trade log
    today_entries = sum(
        1 for e in trade_log
        if e.get("new_entry_opened") and (e.get("timestamp") or "")[:10] == today_str
    )
    today_exits = sum(
        1 for e in trade_log
        if e.get("signal") == "SELL" and (e.get("starting_qty") or 0) > 0
        and (e.get("timestamp") or "")[:10] == today_str
    )
    today_realized_pnl = 0.0
    today_wins = 0
    today_closed = 0
    try:
        with journal._conn() as con:
            rows = con.execute(
                f"SELECT realized_pnl FROM paper_trades WHERE {journal.ELIGIBLE_TRADE_SQL} AND DATE(exit_timestamp)=?",
                (today_str,),
            ).fetchall()
            today_closed = len(rows)
            today_realized_pnl = round(sum(r["realized_pnl"] for r in rows if r["realized_pnl"]), 4)
            today_wins = sum(1 for r in rows if r["realized_pnl"] and r["realized_pnl"] > 0)
    except Exception:
        pass
    today_win_rate = round(today_wins / today_closed * 100, 1) if today_closed > 0 else None

    # Stable performance metrics
    stable = journal.query_stable_v2_performance(STABLE_V2_START_DATE)
    stable_total_closed      = stable.get("total_closed", 0)
    stable_win_rate          = stable.get("win_rate", 0.0) or 0.0
    stable_profit_factor     = stable.get("profit_factor", 0.0) or 0.0
    stable_total_pnl         = stable.get("total_pnl", 0.0) or 0.0
    stable_max_consec_losses = stable.get("max_consecutive_losses", 0) or 0

    # Opening range status per symbol
    orb_status = {}
    for sym in TRADE_WATCHLIST:
        cached = _opening_range.get(sym)
        if cached and cached.get("formed"):
            orb_status[sym] = {"formed": True, "high": cached["high"], "low": cached["low"]}
        else:
            orb_status[sym] = {"formed": False}

    # Breakeven protection status
    breakeven_status = {
        sym: {"armed": True, "be_stop": _breakeven_stops.get(sym)}
        for sym in _breakeven_armed
    }

    # Stop-loss cooldown status
    now = datetime.now(timezone.utc)
    stop_cooldowns = {}
    for sym, stop_time in _stop_loss_times.items():
        elapsed   = (now - stop_time).total_seconds() / 60
        remaining = max(0.0, STOP_LOSS_SYMBOL_COOLDOWN_MINUTES - elapsed)
        if remaining > 0:
            stop_cooldowns[sym] = {
                "remaining_min": round(remaining, 1),
                "elapsed_min":   round(elapsed, 1),
            }

    # Risk warnings
    risk_warnings = []
    if stable_win_rate < 30 and stable_total_closed >= 5:
        risk_warnings.append(
            f"Win rate {stable_win_rate:.1f}% is critically low (need ≥45% for real money)"
        )
    if stable_max_consec_losses >= 5:
        risk_warnings.append(
            f"Max consecutive losses = {stable_max_consec_losses} (need ≤3 for real money)"
        )
    if _daily_loss_shutdown:
        risk_warnings.append("Daily loss shutdown active — no new entries this session")
    if open_orders_count > 0:
        risk_warnings.append(f"{open_orders_count} unresolved open order(s)")
    if last_scan_age_min and last_scan_age_min > 15:
        risk_warnings.append(f"Last scan was {last_scan_age_min}m ago — bot may not be scanning")

    # S-tier real-money readiness (all criteria must be met)
    stier_criteria = {
        "stable_total_closed_ge_30":     stable_total_closed >= 30,
        "stable_win_rate_ge_45":         stable_win_rate >= 45.0,
        "stable_profit_factor_ge_1_2":   stable_profit_factor >= 1.2,
        "stable_total_pnl_positive":     stable_total_pnl > 0,
        "max_consecutive_losses_le_3":   stable_max_consec_losses <= 3,
        "no_unresolved_open_orders":     open_orders_count == 0,
    }

    # Recommended next action
    if not _is_run_bot_active():
        recommended = "START run_bot.py — bot runner is not active"
    elif _daily_loss_shutdown:
        recommended = "OBSERVE ONLY — daily loss limit hit, wait for tomorrow"
    elif stable_total_closed < 5:
        recommended = "Continue paper test — accumulate more sample trades before analyzing"
    elif stable_win_rate < 30 and stable_total_closed >= 10:
        recommended = "Review entry criteria — win rate critically low, consider raising MIN_ENTRY_SCORE"
    else:
        recommended = "Continue paper test — collect more data under S-tier rules"

    # Entry quality rejection counts from session trade log
    def _count_blocked(reason: str) -> int:
        return sum(1 for e in trade_log if e.get("blocked_by") == reason)

    return {
        "paper_mode_confirmed":         ALPACA_PAPER and not ALLOW_LIVE_TRADING,
        "live_trading_disabled":        not ALLOW_LIVE_TRADING,
        "real_money_ready":             False,
        "open_positions_count":         open_positions_count,
        "open_orders_count":            open_orders_count,
        "run_bot_active":               _is_run_bot_active(),
        "today_entries":                today_entries,
        "today_exits":                  today_exits,
        "today_realized_pnl":           today_realized_pnl,
        "today_win_rate":               today_win_rate,
        "stable_total_closed":          stable_total_closed,
        "stable_win_rate":              stable_win_rate,
        "stable_profit_factor":         stable_profit_factor,
        "stable_total_pnl":             stable_total_pnl,
        "max_consecutive_losses":       stable_max_consec_losses,
        "s_tier_real_money_criteria":   stier_criteria,
        "opening_range_enabled":        OPENING_RANGE_ENABLED,
        "opening_range_minutes":        OPENING_RANGE_MINUTES,
        "anti_chase_enabled":           ANTI_CHASE_ENABLED,
        "anti_chase_max_extension_pct": MAX_INTRADAY_EXTENSION_PCT,
        "first_30_min_caution_enabled": FIRST_30_MIN_CAUTION_ENABLED,
        "breakeven_protection_enabled": BREAKEVEN_PROTECTION_ENABLED,
        "breakeven_trigger_gain_pct":   BREAKEVEN_TRIGGER_GAIN_PCT,
        "strict_crypto_stocks":         STRICT_CRYPTO_STOCKS,
        "strict_crypto_symbols":        sorted(STRICT_CRYPTO_SYMBOLS_SET),
        "opening_range_status":         orb_status,
        "session_high_pullback": {
            "enabled":          SESSION_HIGH_PULLBACK_BLOCK_ENABLED,
            "max_pullback_pct": MAX_PULLBACK_FROM_SESSION_HIGH_PCT,
            "session_highs":    {k: round(v, 4) for k, v in _session_highs.items()},
        },
        "breakeven_protection_status":  breakeven_status,
        "stop_loss_symbol_cooldowns":   stop_cooldowns,
        "session_stop_count":           _session_stop_count,
        "market_stop_cooldown_active":  _is_market_stop_cooldown_active(),
        "market_cooldown_until":        _market_cooldown_until.isoformat() if _market_cooldown_until else None,
        "unresolved_risk_warnings":     risk_warnings,
        "recommended_next_action":      recommended,
        "entry_quality_rejection_summary": {
            "first_30_min_caution":       _count_blocked("first_30_min_caution"),
            "strict_crypto_stock_filter": _count_blocked("strict_crypto_stock_filter"),
            "opening_range_not_broken":   _count_blocked("opening_range_not_broken"),
            "anti_chase_extension":       _count_blocked("anti_chase_extension"),
            "falling_from_session_high":  _count_blocked("falling_from_session_high"),
            "post_stop_symbol_cooldown":  _count_blocked("post_stop_symbol_cooldown"),
            "market_stop_cooldown":       _count_blocked("market_stop_cooldown"),
            "quality_b_blocked":          _count_blocked("quality_b_blocked"),
        },
        "s_tier_note": (
            "real_money_ready=false always. Need ≥30 closed trades under S-tier rules, "
            "win_rate ≥45%, profit_factor ≥1.2, total_pnl >0, max_consec_losses ≤3."
        ),
    }


# ── Event log endpoint ────────────────────────────────────────────────────────

@app.get("/events")
def get_events(limit: int = 50):
    """Return the most recent structured bot events from the JSONL event log, newest first."""
    events = load_events_from_file(limit=max(1, min(limit, 500)))
    return {"count": len(events), "events": events}


# ── Dashboard data endpoint ───────────────────────────────────────────────────

@app.get("/dashboard-data")
def dashboard_data():
    """
    Combined snapshot for a monitoring dashboard.
    Returns bot state, market status, open positions, PnL, and recent events.
    All data is read-only — no trades are placed by this endpoint.
    """
    now_utc = datetime.now(timezone.utc)
    now_et  = datetime.now(ZoneInfo("America/New_York"))
    today   = now_utc.strftime("%Y-%m-%d")

    # Open positions from journal
    open_positions = journal.get_open_paper_positions()

    # Today's realized PnL from journal
    realized_pnl = 0.0
    try:
        with journal._conn() as con:
            rows = con.execute(
                f"SELECT realized_pnl FROM paper_trades WHERE {journal.ELIGIBLE_TRADE_SQL} AND DATE(exit_timestamp)=?",
                (today,),
            ).fetchall()
            realized_pnl = round(sum(r["realized_pnl"] for r in rows if r["realized_pnl"]), 4)
    except Exception:
        pass

    # Unrealized PnL from live Alpaca positions
    unrealized_pnl = 0.0
    try:
        pos_resp = requests.get(f"{BASE_URL}/v2/positions", headers=_headers(), timeout=10)
        if pos_resp.status_code == 200:
            for p in pos_resp.json():
                unrealized_pnl += float(p.get("unrealized_pl") or 0)
            unrealized_pnl = round(unrealized_pnl, 4)
    except Exception:
        pass

    return {
        "bot_running":         _is_run_bot_active(),
        "market_open":         _last_known_market_state,
        "current_et_time":     now_et.strftime("%H:%M:%S ET"),
        "last_scan_time":      _last_scan_at.isoformat() if _last_scan_at else None,
        "open_positions":      open_positions,
        "realized_pnl":        realized_pnl,
        "unrealized_pnl":      unrealized_pnl,
        "daily_pnl":           round(realized_pnl + unrealized_pnl, 4),
        "recent_events":       get_recent_events(limit=20),
        "alerts_enabled":      os.getenv("TELEGRAM_ALERTS_ENABLED", "false").lower() == "true",
        "daily_loss_shutdown": _daily_loss_shutdown,
        # dry_run / paper_mode kept for existing dashboard components
        # (RiskPanel, StatusBar, page.tsx read these directly).
        "dry_run":             DRY_RUN,
        "paper_mode":          ALPACA_PAPER,
        **_execution_mode_fields(),
    }


# ── Hermes read-only session summary ─────────────────────────────────────────
# SAFETY RULES — enforced by design, not just convention:
#   - This endpoint is read-only analysis only.
#   - Hermes / AI must NEVER place trades through this or any endpoint.
#   - Hermes / AI must NEVER change strategy settings automatically.

@app.get("/hermes/session-summary")
def hermes_session_summary():
    """
    Rule-based session summary for Hermes (read-only analysis only).
    Uses both event log data AND in-memory session state so results are accurate
    even on quiet days when the event log only contains scan_cycle events.

    SAFETY: AI/Hermes cannot place trades or change settings via this endpoint.
    """
    now_utc = datetime.now(timezone.utc)
    today   = now_utc.strftime("%Y-%m-%d")
    events  = get_recent_events(limit=200)

    # Classify events by type
    entered_events = [e for e in events if e.get("event_type") == "trade_entered"]
    exited_events  = [e for e in events if e.get("event_type") in ("trade_exited", "bot_flattened")]
    stop_events    = [e for e in events if e.get("event_type") == "stop_loss_hit"]
    tp_events      = [e for e in events if e.get("event_type") == "take_profit_hit"]
    warn_events    = [e for e in events if e.get("severity") == "warning"]
    error_events   = [e for e in events if e.get("severity") == "error"]
    scan_events    = [e for e in events if e.get("event_type") == "scan_cycle_completed"]

    # Session state (authoritative — always accurate regardless of event log completeness)
    entries_today  = sum(1 for e in trade_log if e.get("new_entry_opened"))
    exits_today    = sum(1 for e in trade_log if e.get("signal") == "SELL" and (e.get("starting_qty") or 0) > 0)
    errors_today   = sum(1 for e in trade_log if e.get("error"))
    buy_signals    = sum(1 for e in trade_log if e.get("signal") == "BUY")
    blocked_total  = sum(1 for e in trade_log if e.get("blocked_by"))

    # Blocker breakdown
    blocker_counts: dict = {}
    for e in trade_log:
        b = e.get("blocked_by")
        if b:
            blocker_counts[b] = blocker_counts.get(b, 0) + 1

    # Best near miss from in-memory tracker
    best_nm = max(_near_miss_symbols, key=lambda x: x["score"]) if _near_miss_symbols else None

    # ── Why no trades? ─────────────────────────────────────────────────────────
    why_no_trades = None
    if entries_today == 0:
        if _total_scan_cycles == 0:
            why_no_trades = "No scan cycles ran — verify run_bot.py is active and the API is reachable."
        elif buy_signals == 0:
            top_blockers = sorted(blocker_counts.items(), key=lambda x: -x[1])[:3]
            if top_blockers:
                blist = ", ".join(f"{k} ({v}×)" for k, v in top_blockers)
                why_no_trades = f"No BUY signals generated in {_total_scan_cycles} cycles. Top blockers: {blist}."
            else:
                why_no_trades = (
                    f"No BUY signals in {_total_scan_cycles} scan cycles — "
                    "no symbol met the full entry criteria."
                )
        else:
            top_blockers = sorted(blocker_counts.items(), key=lambda x: -x[1])[:3]
            blist = ", ".join(f"{k} ({v}×)" for k, v in top_blockers) if top_blockers else "unknown"
            why_no_trades = (
                f"{buy_signals} BUY signal(s) generated but {blocked_total} evaluation(s) blocked. "
                f"Top block reasons: {blist}."
            )

    # ── Best near miss explanation ─────────────────────────────────────────────
    near_miss_explanation = None
    if best_nm:
        gaps = best_nm.get("gaps") or best_nm.get("near_miss_gaps") or "details not recorded"
        near_miss_explanation = (
            f"{best_nm.get('symbol')} — score={best_nm.get('score')} [{best_nm.get('grade', '')}] | "
            f"missing: {gaps}"
        )

    # ── Bot correctness check ─────────────────────────────────────────────────
    bot_behaved_correctly = True
    correctness_notes = []
    if errors_today > 0:
        bot_behaved_correctly = False
        correctness_notes.append(f"{errors_today} error(s) encountered — check API logs")
    if _daily_loss_shutdown:
        correctness_notes.append("Daily loss shutdown is active — expected if loss limit was hit today")
    if not _is_run_bot_active():
        bot_behaved_correctly = False
        correctness_notes.append("run_bot.py does not appear to be active — bot may have stopped")
    if _total_scan_cycles > 0 and entries_today == 0 and errors_today == 0:
        correctness_notes.append(
            f"Bot completed {_total_scan_cycles} scan cycle(s) with 0 errors and 0 entries — "
            "normal when market conditions don't meet entry criteria"
        )
    if not correctness_notes:
        correctness_notes.append("All systems nominal")

    # ── What needs review ──────────────────────────────────────────────────────
    needs_review = []
    if error_events:
        needs_review.append(f"{len(error_events)} error event(s) — check bot logs for root cause")
    if stop_events:
        needs_review.append(f"{len(stop_events)} stop-loss hit(s) — review entry quality or risk sizing")
    if _daily_loss_shutdown:
        needs_review.append("Daily loss shutdown is active — no new entries until tomorrow")
    if warn_events:
        warn_types = sorted({e.get("event_type") for e in warn_events})
        needs_review.append(f"Warnings logged: {', '.join(warn_types)}")
    if not _is_run_bot_active():
        needs_review.append("Bot runner (run_bot.py) does not appear to be active")
    if best_nm:
        needs_review.append(
            f"Best near-miss ({best_nm.get('symbol')}, score={best_nm.get('score')}) was close to threshold — "
            "worth reviewing if conditions were reasonable"
        )
    if not needs_review:
        needs_review.append("Nothing unusual — session looks clean")

    # ── What happened narrative ────────────────────────────────────────────────
    what_happened = []
    if entries_today:
        what_happened.append(f"Entered {entries_today} trade(s)")
    if exits_today:
        what_happened.append(f"Exited {exits_today} position(s)")
    if stop_events:
        syms = sorted({e.get("symbol") for e in stop_events if e.get("symbol")})
        what_happened.append(f"Stop loss triggered: {', '.join(syms)}")
    if tp_events:
        syms = sorted({e.get("symbol") for e in tp_events if e.get("symbol")})
        what_happened.append(f"Take profit hit: {', '.join(syms)}")
    if error_events:
        what_happened.append(f"{len(error_events)} error event(s) logged")
    if not what_happened:
        what_happened.append(
            f"No significant trading activity — {_total_scan_cycles} scan cycle(s) completed cleanly"
        )

    return {
        "safety_note":            "READ-ONLY — Hermes/AI cannot place trades or change strategy settings",
        "session_date":           today,
        "what_happened":          what_happened,
        "why_no_trades":          why_no_trades,
        "best_near_miss":         near_miss_explanation,
        "main_blockers": [
            {"blocker": k, "count": v}
            for k, v in sorted(blocker_counts.items(), key=lambda x: -x[1])[:5]
        ],
        "bot_behaved_correctly":  bot_behaved_correctly,
        "correctness_notes":      correctness_notes,
        "trades_entered": [
            {"symbol": e.get("symbol"), "time": e.get("timestamp_utc"), "message": e.get("message")}
            for e in entered_events
        ],
        "trades_exited": [
            {"symbol": e.get("symbol"), "type": e.get("event_type"), "time": e.get("timestamp_utc"), "message": e.get("message")}
            for e in (exited_events + stop_events + tp_events)
        ],
        "warnings_errors": [
            {"type": e.get("event_type"), "severity": e.get("severity"), "message": e.get("message"), "time": e.get("timestamp_utc")}
            for e in (warn_events + error_events)
        ],
        "needs_review": needs_review,
        "session_stats": {
            "entries_today":    entries_today,
            "exits_today":      exits_today,
            "errors_today":     errors_today,
            "buy_signals_seen": buy_signals,
            "entries_blocked":  blocked_total,
            "near_misses":      len(_near_miss_symbols),
            "scan_cycles":      _total_scan_cycles,
            "market_open":      _last_known_market_state,
            "daily_shutdown":   _daily_loss_shutdown,
            "session_flattened": _session_flattened,
            "data_source":      "events+state" if scan_events else "state_only",
        },
    }


# ── Hermes end-of-day review (read-only, conservative) ────────────────────────
# SAFETY RULES — enforced by design, not just convention:
#   - Read-only analysis only. This code path never places trades, never
#     starts/stops the bot, and never edits strategy or risk settings.
#   - strategy_change_recommendation defaults to "NO_CHANGE" and only ever
#     returns "REVIEW_AFTER_MORE_DATA" when the same blocker has repeated
#     across multiple distinct days of logged data. It never proposes a
#     specific new value and nothing here is ever applied automatically.

def _detect_blocker_pattern_across_days(min_days: int = 3, min_days_matching: int = 2) -> dict:
    """
    Conservative multi-day pattern check used only to decide whether the
    end-of-day review may suggest "REVIEW_AFTER_MORE_DATA" instead of the
    default "NO_CHANGE". Looks at every scan_cycle_completed event available
    in the JSONL log (spans server restarts/days) and checks whether the
    same blocker was the top blocker on multiple distinct calendar days.
    Purely informational — never changes anything by itself.
    """
    from collections import Counter, defaultdict

    try:
        events = load_events_from_file(limit=5000)
    except Exception:
        events = []

    by_day_blockers: dict = defaultdict(Counter)
    for e in events:
        if e.get("event_type") != "scan_cycle_completed":
            continue
        day = (e.get("timestamp_utc") or "")[:10]
        blocker = (e.get("data") or {}).get("top_candidate_blocker")
        if day and blocker:
            by_day_blockers[day][blocker] += 1

    dominant_by_day = {
        day: counter.most_common(1)[0][0]
        for day, counter in by_day_blockers.items()
        if counter
    }

    distinct_days = len(dominant_by_day)
    repeated_blocker = None
    repeated_blocker_day_count = 0
    if distinct_days >= min_days:
        overall = Counter(dominant_by_day.values())
        top_blocker, top_count = overall.most_common(1)[0]
        if top_count >= min_days_matching:
            repeated_blocker = top_blocker
            repeated_blocker_day_count = top_count

    return {
        "distinct_days_observed":     distinct_days,
        "dominant_blocker_by_day":    dominant_by_day,
        "repeated_blocker":           repeated_blocker,
        "repeated_blocker_day_count": repeated_blocker_day_count,
    }


def _build_end_of_day_review() -> dict:
    """
    Aggregates session scan data, journal P&L, the event log, and a
    conservative multi-day pattern check into a single read-only review.
    Never places trades, never starts/stops the bot, never changes
    strategy or risk settings — see SAFETY RULES above.
    """
    from collections import Counter

    now_utc = datetime.now(timezone.utc)
    now_et  = datetime.now(ZoneInfo("America/New_York"))
    today   = now_et.strftime("%Y-%m-%d")

    events = get_recent_events(limit=300)

    # ── Bot behavior / scan stats (session, in-memory — authoritative) ────────
    total_evaluated = len(trade_log)
    buy_signals     = sum(1 for e in trade_log if e.get("signal") == "BUY")
    entered         = sum(1 for e in trade_log if e.get("new_entry_opened"))
    exited          = sum(1 for e in trade_log if e.get("signal") == "SELL" and (e.get("starting_qty") or 0) > 0)
    blocked_total   = sum(1 for e in trade_log if e.get("blocked_by"))
    errors_today    = sum(1 for e in trade_log if e.get("signal") == "ERROR" or e.get("error"))
    blocker_counts  = dict(Counter(e.get("blocked_by") for e in trade_log if e.get("blocked_by")))

    # ── Market behavior (SPY regime observed this session) ─────────────────────
    spy_regime_counts   = dict(Counter(e.get("spy_regime") for e in trade_log if e.get("spy_regime")))
    dominant_spy_regime = max(spy_regime_counts, key=spy_regime_counts.get) if spy_regime_counts else None

    # ── Trade summary (session realized P&L from journal) ──────────────────────
    session_closed, session_realized_pnl, session_wins = 0, 0.0, 0
    try:
        with journal._conn() as con:
            rows = con.execute(
                f"SELECT realized_pnl FROM paper_trades WHERE {journal.ELIGIBLE_TRADE_SQL} AND exit_timestamp >= ?",
                (_session_start.isoformat(),),
            ).fetchall()
            session_closed        = len(rows)
            session_realized_pnl  = round(sum(r["realized_pnl"] for r in rows if r["realized_pnl"]), 4)
            session_wins          = sum(1 for r in rows if r["realized_pnl"] and r["realized_pnl"] > 0)
    except Exception:
        pass
    session_win_rate = round(session_wins / session_closed * 100, 1) if session_closed else None
    open_positions    = journal.get_open_paper_positions()

    trade_summary = {
        "entries":              entered,
        "exits":                exited,
        "closed_trades":        session_closed,
        "winning_trades":       session_wins,
        "win_rate_pct":         session_win_rate,
        "realized_pnl":         session_realized_pnl,
        "open_positions_count": len(open_positions),
        "open_positions":       open_positions,
        "session_exits":        _session_exits,
    }

    # ── No-trade analysis ────────────────────────────────────────────────────
    no_trade_analysis = None
    if entered == 0:
        if total_evaluated == 0:
            no_trade_analysis = (
                "No scan cycles ran this session — verify run_bot.py was active "
                "and the API was reachable."
            )
        elif buy_signals == 0:
            no_trade_analysis = (
                f"{total_evaluated} evaluation(s) across {_total_scan_cycles} scan cycle(s) "
                "produced zero BUY signals — no symbol met the full entry criteria today."
            )
        else:
            top_blockers = sorted(blocker_counts.items(), key=lambda x: -x[1])[:3]
            blist = ", ".join(f"{k} ({v}x)" for k, v in top_blockers) if top_blockers else "unknown"
            no_trade_analysis = (
                f"{buy_signals} BUY signal(s) generated but {blocked_total} evaluation(s) were "
                f"blocked. Top blockers: {blist}."
            )

    # ── Best opportunity ─────────────────────────────────────────────────────
    best_nm = max(_near_miss_symbols, key=lambda x: x.get("score", 0)) if _near_miss_symbols else None
    best_opportunity = None
    if best_nm:
        best_opportunity = {
            "symbol":     best_nm.get("symbol"),
            "score":      best_nm.get("score"),
            "grade":      best_nm.get("grade"),
            "gaps":       best_nm.get("gaps"),
            "spy_regime": best_nm.get("spy_regime"),
        }
    elif entered > 0:
        try:
            with journal._conn() as con:
                row = con.execute(
                    f"SELECT symbol, realized_pnl FROM paper_trades WHERE {journal.ELIGIBLE_TRADE_SQL} "
                    "AND exit_timestamp >= ? ORDER BY realized_pnl DESC LIMIT 1",
                    (_session_start.isoformat(),),
                ).fetchone()
                if row:
                    best_opportunity = {"symbol": row["symbol"], "realized_pnl": row["realized_pnl"]}
        except Exception:
            pass

    # ── Blocker breakdown ────────────────────────────────────────────────────
    blocker_breakdown = [
        {"blocker": k, "count": v}
        for k, v in sorted(blocker_counts.items(), key=lambda x: -x[1])
    ]

    # ── Error review ─────────────────────────────────────────────────────────
    error_events = [e for e in events if e.get("severity") == "error"]
    error_review = {
        "error_count":  errors_today,
        "error_events": [
            {"type": e.get("event_type"), "message": e.get("message"), "time": e.get("timestamp_utc")}
            for e in error_events
        ],
        "note": (
            "No errors today."
            if errors_today == 0 and not error_events
            else "Review these before the next session — repeated API/data errors can mask real signals."
        ),
    }

    # ── Risk review ──────────────────────────────────────────────────────────
    risk_review = {
        "daily_loss_shutdown_triggered": _daily_loss_shutdown,
        "session_flattened":             _session_flattened,
        "observe_only_mode":             _observe_only_mode,
        "max_open_positions":            MAX_OPEN_POSITIONS,
        "open_positions_at_review_time": len(open_positions),
        "risk_per_trade_pct":            RISK_PER_TRADE_PCT,
        "daily_loss_limit_pct":          DAILY_LOSS_LIMIT_PCT,
        "stop_loss_pct":                 STOP_LOSS_PCT,
        "take_profit_pct":               TAKE_PROFIT_PCT,
        "flatten_at_window_end":         FLATTEN_AT_WINDOW_END,
        "notes":                         [],
    }
    if open_positions and FLATTEN_AT_WINDOW_END and not _session_flattened:
        risk_review["notes"].append(
            f"{len(open_positions)} position(s) still open and flatten has not fired yet."
        )
    if session_realized_pnl < -(PAPER_ACCOUNT_EQUITY * DAILY_LOSS_LIMIT_PCT):
        risk_review["notes"].append("Session realized loss exceeds the configured daily loss limit.")
    if not risk_review["notes"]:
        risk_review["notes"].append("No risk threshold breaches observed this session.")

    # ── Data quality notes ───────────────────────────────────────────────────
    data_quality_notes = []
    if total_evaluated == 0:
        data_quality_notes.append("No scan data recorded this session — review may be incomplete.")
    if not _is_run_bot_active():
        data_quality_notes.append("run_bot.py does not appear to be active as of this review.")
    stale_symbols = []
    try:
        with journal._conn() as con:
            hist_syms = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM paper_trades").fetchall()}
        stale_symbols = sorted(hist_syms - set(TRADE_WATCHLIST) - REGIME_SYMBOLS)
    except Exception:
        pass
    if stale_symbols:
        data_quality_notes.append(
            f"Historical DB contains trades for symbols no longer on the watchlist: {stale_symbols}."
        )
    if not data_quality_notes:
        data_quality_notes.append("Data looks complete for this session.")

    # ── Multi-day blocker pattern (informational only, never auto-applied) ─────
    pattern = _detect_blocker_pattern_across_days()

    # ── Lessons learned (descriptive, not prescriptive) ─────────────────────────
    lessons_learned = []
    if blocker_counts:
        top_blocker, top_count = max(blocker_counts.items(), key=lambda x: x[1])
        lessons_learned.append(f"'{top_blocker}' was the most common block today ({top_count}x).")
    if best_nm:
        lessons_learned.append(
            f"{best_nm.get('symbol')} came closest to qualifying (score={best_nm.get('score')}) "
            "without triggering an entry."
        )
    if pattern["repeated_blocker"]:
        lessons_learned.append(
            f"'{pattern['repeated_blocker']}' has been the dominant blocker on "
            f"{pattern['repeated_blocker_day_count']} of the last {pattern['distinct_days_observed']} "
            "logged days — worth watching, not yet acted on."
        )
    if errors_today > 0:
        lessons_learned.append(f"{errors_today} error(s) occurred — check logs for root cause before next session.")
    if not lessons_learned:
        lessons_learned.append("Nothing notable stood out today — session ran as configured.")

    # ── Recommended next steps (process only — never strategy changes) ─────────
    recommended_next_steps = []
    if errors_today > 0:
        recommended_next_steps.append("Investigate today's errors via GET /events before the next session.")
    if open_positions and not _session_flattened:
        recommended_next_steps.append("Confirm open positions are intentional and flatten manually if needed.")
    if pattern["repeated_blocker"]:
        recommended_next_steps.append(
            f"Keep tracking '{pattern['repeated_blocker']}' over the next few sessions before considering "
            "any config review."
        )
    if not recommended_next_steps:
        recommended_next_steps.append("No action needed — continue monitoring the next session as usual.")

    # ── Strategy change recommendation — conservative by design, see SAFETY RULES ──
    if pattern["repeated_blocker"]:
        strategy_change_recommendation = {
            "verdict": "REVIEW_AFTER_MORE_DATA",
            "reason": (
                f"'{pattern['repeated_blocker']}' has been the dominant entry blocker on "
                f"{pattern['repeated_blocker_day_count']} of the last {pattern['distinct_days_observed']} "
                "days with logged scan data. This is a repeated pattern worth a manual review, "
                "not a one-day signal."
            ),
            "note": "Informational only. Hermes never changes strategy or risk settings automatically.",
        }
    else:
        strategy_change_recommendation = {
            "verdict": "NO_CHANGE",
            "reason": (
                "No repeated multi-day pattern detected yet. A single session is never enough "
                "data to justify a change."
            ),
            "note": "Informational only. Hermes never changes strategy or risk settings automatically.",
        }

    # ── Executive summary ───────────────────────────────────────────────────
    if entered > 0:
        executive_summary = (
            f"{entered} entr{'y' if entered == 1 else 'ies'} today, {exited} exit(s), "
            f"session realized P&L ${session_realized_pnl:.2f} across {session_closed} closed trade(s)."
        )
    elif buy_signals > 0:
        executive_summary = (
            f"No entries today despite {buy_signals} BUY signal(s) — {blocked_total} evaluation(s) blocked. "
            f"{total_evaluated} symbol-scan(s) across {_total_scan_cycles} cycle(s)."
        )
    else:
        executive_summary = (
            f"Quiet session — {total_evaluated} symbol-scan(s) across {_total_scan_cycles} cycle(s), "
            "no BUY signals generated."
        )

    return {
        "safety_note": (
            "READ-ONLY — Hermes cannot place trades, change strategy/risk settings, "
            "or start/stop the bot."
        ),
        "session_date":      today,
        "generated_at_utc":  now_utc.isoformat(),
        "executive_summary": executive_summary,
        "market_behavior": {
            "dominant_spy_regime_seen":  dominant_spy_regime,
            "spy_regime_breakdown":      spy_regime_counts,
            "market_open_last_checked":  _last_known_market_state,
        },
        "bot_behavior": {
            "scan_cycles":         _total_scan_cycles,
            "symbols_evaluated":   total_evaluated,
            "buy_signals":         buy_signals,
            "entries":             entered,
            "exits":               exited,
            "blocked":             blocked_total,
            "errors":              errors_today,
            "run_bot_active":      _is_run_bot_active(),
            "daily_loss_shutdown": _daily_loss_shutdown,
            "session_flattened":   _session_flattened,
        },
        "trade_summary":                   trade_summary,
        "no_trade_analysis":               no_trade_analysis,
        "best_opportunity":                best_opportunity,
        "blocker_breakdown":               blocker_breakdown,
        "error_review":                    error_review,
        "risk_review":                     risk_review,
        "data_quality_notes":              data_quality_notes,
        "lessons_learned":                 lessons_learned,
        "recommended_next_steps":          recommended_next_steps,
        "strategy_change_recommendation":  strategy_change_recommendation,
        "multi_day_pattern_data":          pattern,
    }


def _tg_safe(value) -> str:
    """
    Strip/replace characters that break Telegram's legacy Markdown parser
    (parse_mode="Markdown" in send_telegram_alert, used unescaped elsewhere
    in this file too). A lone "_" or "[" in dynamic text like a blocker name
    or "NO_CHANGE" is read as an unclosed entity and makes Telegram reject
    the whole message with a 400.
    """
    if value is None:
        return ""
    s = str(value).replace("_", " ")
    for ch in "*`[]":
        s = s.replace(ch, "")
    return s


def _format_eod_telegram_message(review: dict) -> str:
    """Render the end-of-day review as a compact Telegram-friendly message."""
    ts = review["trade_summary"]
    bo = review.get("best_opportunity")
    if bo and "score" in bo:
        best_line = f"{_tg_safe(bo.get('symbol'))} score={bo.get('score')} grade={_tg_safe(bo.get('grade') or '-')}"
    elif bo:
        best_line = f"{_tg_safe(bo.get('symbol'))} pnl=${(bo.get('realized_pnl') or 0):.2f}"
    else:
        best_line = "None"

    top_blockers  = review.get("blocker_breakdown", [])[:3]
    blockers_line = (
        ", ".join(f"{_tg_safe(b['blocker'])} x{b['count']}" for b in top_blockers)
        if top_blockers else "None"
    )

    risk = review["risk_review"]
    risk_status = (
        "SHUTDOWN"   if risk["daily_loss_shutdown_triggered"] else
        "FLATTENED"  if risk["session_flattened"] else
        "NORMAL"
    )

    verdict   = _tg_safe(review["strategy_change_recommendation"]["verdict"])
    next_step = _tg_safe((review.get("recommended_next_steps") or ["Continue monitoring."])[0])

    lines = [
        "MISSION CONTROL DAILY REVIEW",
        f"Date: {review['session_date']}",
        f"Scans: {review['bot_behavior']['scan_cycles']}",
        f"Signals: {review['bot_behavior']['buy_signals']}",
        f"Trades: {ts['entries']} entries / {ts['exits']} exits (${ts['realized_pnl']:.2f})",
        f"Best Opportunity: {best_line}",
        f"Top Blockers: {blockers_line}",
        f"Risk Status: {risk_status}",
        f"Hermes Verdict: {verdict}",
        f"Next Step: {next_step}",
    ]
    return "\n".join(lines)


@app.get("/hermes/end-of-day-review")
def hermes_end_of_day_review():
    """
    Full read-only end-of-day analysis for Hermes. Aggregates session scan
    data, journal P&L, the event log, and a conservative multi-day pattern
    check into a single review.

    SAFETY: read-only analysis only. Never places trades, never starts/stops
    the bot, never changes strategy or risk settings — see the SAFETY RULES
    comment above _detect_blocker_pattern_across_days.
    """
    return _build_end_of_day_review()


@app.get("/hermes/end-of-day-telegram")
def hermes_end_of_day_telegram():
    """
    Compact Telegram-formatted rendering of the end-of-day review.
    Read-only — does not send anything. Use POST /test-hermes-eod-telegram
    to actually deliver it to Telegram.
    """
    review  = _build_end_of_day_review()
    message = _format_eod_telegram_message(review)
    return {"session_date": review["session_date"], "message": message}


@app.post("/test-hermes-eod-telegram")
def test_hermes_eod_telegram():
    """
    Send the current end-of-day review to Telegram as a compact message.
    Read-only analysis only — does not place trades, start/stop the bot,
    or change any settings.
    """
    review  = _build_end_of_day_review()
    message = _format_eod_telegram_message(review)
    sent    = send_telegram_alert("Mission Control Daily Review", message, severity="info")
    enabled = os.getenv("TELEGRAM_ALERTS_ENABLED", "false").lower() == "true"
    return {
        "telegram_enabled": enabled,
        "sent":             sent,
        "message":          message,
    }


# ── Test endpoint: verify Telegram message formatting ─────────────────────────

@app.post("/test-telegram-summary")
def test_telegram_summary():
    """
    Send a fake scan summary and a fake end-of-session summary to Telegram.
    Use this to verify message formatting without waiting for market hours.
    Does not affect bot state or trading logic.
    """
    scan_msg = "\n".join([
        "Scan #99 | 8 symbols",
        "Top: PLTR score=72[B] blocked=score_too_low",
        "Entries today: 0",
        "Open positions: 0",
        "Realized P&L: $0.00",
    ])
    scan_ok = send_telegram_alert("Scan Summary [TEST]", scan_msg, severity="info")

    session_msg = "\n".join([
        "Scans: 41 | Entries: 0 | Exits: 0",
        "Realized P&L: $0.00",
        "Top blockers: score_too_low×28, first_30min×8, anti_chase×4",
        "Best near-miss: PLTR score=72 [B]",
        "All flat: Yes",
    ])
    session_ok = send_telegram_alert("Session Ended [TEST]", session_msg, severity="success")

    enabled = os.getenv("TELEGRAM_ALERTS_ENABLED", "false").lower() == "true"
    return {
        "telegram_enabled":      enabled,
        "scan_summary_sent":     scan_ok,
        "session_summary_sent":  session_ok,
        "note": (
            "Test messages sent (or skipped if TELEGRAM_ALERTS_ENABLED is not 'true'). "
            "No real bot data was used."
        ),
        "messages": {
            "scan_summary":    scan_msg,
            "session_summary": session_msg,
        },
    }


# ── Phase 2B: Historical analytics (read-only, journal-backed) ────────────────

_ANALYTICS_UNAVAILABLE = {"error": "analytics service not available — check startup logs"}


def _clamp_days(days: int) -> int:
    """Keep lookback windows sane: 1–365 days."""
    return max(1, min(days, 365))


@app.get("/analytics/equity-curve")
def analytics_equity_curve(days: int = 30):
    """Daily realized PnL and cumulative equity curve from the journal."""
    if analytics_service is None:
        return _ANALYTICS_UNAVAILABLE
    return analytics_service.get_equity_curve(_clamp_days(days))


@app.get("/analytics/daily-history")
def analytics_daily_history(days: int = 30):
    """Per-trading-day stats (trades, win rate, PnL, avg R, best/worst)."""
    if analytics_service is None:
        return _ANALYTICS_UNAVAILABLE
    return analytics_service.get_daily_history(_clamp_days(days))


@app.get("/analytics/symbol-history/{symbol}")
def analytics_symbol_history(symbol: str, days: int = 30):
    """Date-windowed performance for one symbol, with per-day breakdown."""
    if analytics_service is None:
        return _ANALYTICS_UNAVAILABLE
    return analytics_service.get_symbol_history(symbol, _clamp_days(days))


@app.get("/analytics/trends")
def analytics_trends(window: int = 5):
    """Rolling win-rate/expectancy/avg-R trend: recent N sessions vs prior N."""
    if analytics_service is None:
        return _ANALYTICS_UNAVAILABLE
    return analytics_service.get_trend_metrics(max(1, min(window, 20)))


@app.get("/analytics/blocker-history")
def analytics_blocker_history(days: int = 30):
    """Entry-gate block frequency over time, ranked and per-day."""
    if analytics_service is None:
        return _ANALYTICS_UNAVAILABLE
    return analytics_service.get_blocker_history(_clamp_days(days))


@app.get("/analytics/time-of-day")
def analytics_time_of_day(days: int = 30):
    """Win/loss clustering by ET entry hour."""
    if analytics_service is None:
        return _ANALYTICS_UNAVAILABLE
    return analytics_service.get_time_of_day_stats(_clamp_days(days))


@app.get("/analytics/tier-performance")
def analytics_tier_performance(days: int = 30):
    """Closed-trade performance grouped by entry tier."""
    if analytics_service is None:
        return _ANALYTICS_UNAVAILABLE
    return analytics_service.get_tier_performance(_clamp_days(days))


# ── Phase 2B: Hermes insights (strictly read-only, per HERMES_RULES.md) ───────

@app.get("/hermes/weekly-review")
def hermes_weekly_review():
    """
    Read-only weekly review: last 5 trading sessions vs the prior 5, with
    best/worst trades, dominant blockers, narrative, and review questions.

    SAFETY: report-only. Never places trades or changes any settings.
    """
    if hermes_insights_service is None:
        return _ANALYTICS_UNAVAILABLE
    return hermes_insights_service.build_weekly_review()


@app.get("/hermes/patterns")
def hermes_patterns(days: int = 30):
    """
    Read-only cross-session pattern detection: repeated stop-outs, tier
    over/under-performance, weak entry hours, blocker spikes. Every pattern
    includes the supporting numbers.

    SAFETY: report-only. Never places trades or changes any settings.
    """
    if hermes_insights_service is None:
        return _ANALYTICS_UNAVAILABLE
    return hermes_insights_service.detect_patterns(_clamp_days(days))


@app.get("/hermes/blocked-signal-analysis")
def hermes_blocked_signal_analysis(days: int = 30):
    """
    Read-only analysis of which entry gates block most often, plus
    near-miss evaluations (A/A+ setups blocked by non-score gates, or
    scores within 10 points of the entry threshold).

    SAFETY: report-only. Never places trades or changes any settings.
    """
    if hermes_insights_service is None:
        return _ANALYTICS_UNAVAILABLE
    return hermes_insights_service.build_blocked_signal_analysis(
        _clamp_days(days), min_entry_score=MIN_ENTRY_SCORE
    )
