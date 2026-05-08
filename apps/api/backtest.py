#!/usr/bin/env python3
"""
Standalone backtest CLI.

Applies the same entry/exit/scoring rules as the live bot against historical
Alpaca daily bars. Does NOT call the running API — reads .env directly.

Usage:
  cd ~/trading-bot/apps/api
  source venv/bin/activate
  python3 backtest.py --symbols SOFI,HOOD,F,RIOT,MARA,SNAP,XLK \\
                      --start 2024-01-01 --end 2024-12-31

Output: per-symbol stats + combined summary table.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Credentials & endpoints ───────────────────────────────────────────────────
API_KEY    = os.getenv("ALPACA_API_KEY", "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
DATA_URL   = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")

# ── Strategy params (mirror .env defaults) ───────────────────────────────────
STOP_LOSS_PCT          = float(os.getenv("STOP_LOSS_PCT",          "0.03"))
TAKE_PROFIT_PCT        = float(os.getenv("TAKE_PROFIT_PCT",        "0.05"))
MIN_TREND_STRENGTH     = float(os.getenv("MIN_TREND_STRENGTH",     "0.01"))
MIN_VOLUME_RATIO       = float(os.getenv("MIN_VOLUME_RATIO",       "0.25"))
RSI_PERIOD             = int(os.getenv("RSI_PERIOD",               "14"))
RSI_OVERBOUGHT         = float(os.getenv("RSI_OVERBOUGHT",         "80"))
BREAKOUT_LOOKBACK      = int(os.getenv("BREAKOUT_LOOKBACK",        "20"))
SMA20_RISING_BARS      = int(os.getenv("SMA20_RISING_BARS",        "3"))
EARLY_TREND_MAX_GAP    = float(os.getenv("EARLY_TREND_MAX_SMA_GAP_PCT", "0.03"))
ALLOW_EARLY_TREND      = os.getenv("ALLOW_EARLY_TREND_ENTRY",      "true").lower() == "true"
REQUIRE_MACD_IMPROVING = os.getenv("EARLY_TREND_REQUIRE_MACD_IMPROVING", "true").lower() == "true"
REQUIRE_BREAKOUT       = os.getenv("REQUIRE_BREAKOUT_FOR_BUY",     "false").lower() == "true"
PAPER_EQUITY           = float(os.getenv("PAPER_ACCOUNT_EQUITY",   "1000"))
MAX_ALLOCATION_PCT     = float(os.getenv("MAX_ALLOCATION_PCT",     "0.20"))

try:
    from scoring import compute_candidate_score
    _HAS_SCORING = True
except ImportError:
    _HAS_SCORING = False


def _headers() -> dict:
    return {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}


def _fetch_bars(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Fetch daily bars for symbol between start and end (YYYY-MM-DD)."""
    url = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    params = {
        "timeframe": "1Day",
        "start":     start,
        "end":       end,
        "limit":     1000,
        "feed":      "iex",
        "sort":      "asc",
    }
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=30)
        resp.raise_for_status()
        bars = resp.json().get("bars", [])
        if not bars:
            return pd.DataFrame()
        return pd.DataFrame(bars)
    except Exception as exc:
        print(f"  [ERROR] {symbol}: {exc}", file=sys.stderr)
        return pd.DataFrame()


def _rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.where(avg_loss > 0, other=1e-10)
    rsi      = 100 - (100 / (1 + rs))
    return rsi.where(avg_loss > 0, other=100.0)


def _macd(closes: pd.Series):
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    line  = ema12 - ema26
    sig   = line.ewm(span=9, adjust=False).mean()
    return line, sig


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["sma_20"] = df["c"].rolling(window=20).mean()
    df["sma_50"] = df["c"].rolling(window=50).mean()
    if "v" in df.columns:
        df["vol_sma_20"] = df["v"].rolling(window=20).mean()
    df["rsi"]         = _rsi(df["c"], RSI_PERIOD)
    ml, sl            = _macd(df["c"])
    df["macd_line"]   = ml
    df["macd_signal"] = sl
    df["macd_hist"]   = df["macd_line"] - df["macd_signal"]
    df["macd_hist_rising"] = df["macd_hist"] > df["macd_hist"].shift(1)
    df["breakout_high"]    = df["c"].shift(1).rolling(window=BREAKOUT_LOOKBACK).max()
    df["sma20_rising"]     = (
        df["sma_20"]
        .rolling(window=SMA20_RISING_BARS + 1, min_periods=SMA20_RISING_BARS + 1)
        .apply(lambda x: int(all(x[i] < x[i + 1] for i in range(len(x) - 1))), raw=True)
        .fillna(0)
        .astype(bool)
    )
    return df


def _backtest_symbol(symbol: str, start: str, end: str) -> dict:
    df_raw = _fetch_bars(symbol, start, end)
    if df_raw.empty:
        return {"symbol": symbol, "error": "No data"}

    df = _enrich(df_raw).dropna(subset=["sma_20", "sma_50"]).reset_index(drop=True)
    if df.empty:
        return {"symbol": symbol, "error": "Insufficient data after indicator warm-up"}

    has_vol  = "vol_sma_20" in df.columns and "v" in df.columns
    has_rsi  = "rsi"         in df.columns
    has_macd = "macd_line"   in df.columns

    in_trade      = False
    entry_price   = stop_price = tp_price = entry_date = entry_score = None
    trades: list  = []
    equity_curve  = [1.0]
    running_eq    = 1.0

    for _, row in df.iterrows():
        close  = float(row["c"])
        sma_20 = float(row["sma_20"])
        sma_50 = float(row["sma_50"])
        date   = row["t"]

        if in_trade:
            exit_reason = None
            if close <= stop_price:
                exit_reason = "STOP_LOSS"
            elif close >= tp_price:
                exit_reason = "TAKE_PROFIT"
            elif close < sma_20:
                exit_reason = "MOMENTUM_FAILURE"

            if exit_reason:
                pnl_pct     = (close - entry_price) / entry_price
                running_eq *= (1 + pnl_pct)
                equity_curve.append(running_eq)
                risk_per_share = entry_price - stop_price
                r_mult         = ((close - entry_price) / risk_per_share) if risk_per_share > 0 else None
                trades.append({
                    "entry_date":  entry_date,
                    "exit_date":   date,
                    "entry_price": round(entry_price, 2),
                    "exit_price":  round(close, 2),
                    "pnl_pct":     round(pnl_pct * 100, 2),
                    "exit_reason": exit_reason,
                    "r_multiple":  round(r_mult, 3) if r_mult is not None else None,
                    "entry_score": entry_score,
                })
                in_trade = False
                entry_price = stop_price = tp_price = entry_date = entry_score = None
            continue

        # ── Entry conditions (mirrors live logic) ──────────────────────────
        strong_trend = close > sma_20 and sma_20 > sma_50
        early_trend  = False

        if ALLOW_EARLY_TREND and not strong_trend and close > sma_20:
            sma_gap = (sma_50 - sma_20) / sma_50 if sma_50 > 0 else 1.0
            rising  = bool(row.get("sma20_rising", False))
            if sma_gap <= EARLY_TREND_MAX_GAP and rising:
                early_trend = True
                if REQUIRE_MACD_IMPROVING and has_macd:
                    if not bool(row.get("macd_hist_rising", True)):
                        early_trend = False

        if not (strong_trend or early_trend):
            continue

        if strong_trend:
            ts = abs(sma_20 - sma_50) / sma_50
            if ts < MIN_TREND_STRENGTH:
                continue

        if has_vol and not pd.isna(row.get("vol_sma_20", float("nan"))):
            if float(row["v"]) < float(row["vol_sma_20"]) * MIN_VOLUME_RATIO:
                continue

        if has_rsi and not pd.isna(row.get("rsi", float("nan"))):
            if float(row["rsi"]) >= RSI_OVERBOUGHT:
                continue

        if has_macd:
            ml = row.get("macd_line")
            ms = row.get("macd_signal")
            if ml is not None and ms is not None and not pd.isna(ml) and not pd.isna(ms):
                if float(ml) <= float(ms):
                    continue

        if REQUIRE_BREAKOUT and not pd.isna(row.get("breakout_high", float("nan"))):
            if close <= float(row["breakout_high"]):
                continue

        # ── Affordability check ────────────────────────────────────────────
        max_alloc = PAPER_EQUITY * MAX_ALLOCATION_PCT
        if close > max_alloc:
            continue

        # ── Score (optional, for analytics only — does not gate backtest) ──
        score_val = None
        if _HAS_SCORING:
            vol_ratio = None
            if has_vol and not pd.isna(row.get("vol_sma_20", float("nan"))) and float(row.get("vol_sma_20", 0)) > 0:
                vol_ratio = float(row["v"]) / float(row["vol_sma_20"])
            fake_sd = {
                "entry_tier":            "strong" if strong_trend else "early",
                "trend_strength":        abs(sma_20 - sma_50) / sma_50,
                "current_volume":        int(row.get("v", 0)) if has_vol else None,
                "vol_sma_20":            float(row.get("vol_sma_20", 0)) if has_vol else None,
                "volume_confirmed":      True,
                "rsi":                   float(row["rsi"]) if has_rsi and not pd.isna(row.get("rsi")) else None,
                "macd_bullish":          True,
                "macd_histogram_rising": bool(row.get("macd_hist_rising", True)),
                "intraday_confirmed":    True,
                "intraday_margin_pct":   0.005,
                "spy_bullish":           True,
                "close":                 close,
                "breakout_confirmed":    True,
            }
            scored    = compute_candidate_score(fake_sd, PAPER_EQUITY, MAX_ALLOCATION_PCT)
            score_val = scored["score"]

        in_trade    = True
        entry_price = close
        stop_price  = round(close * (1 - STOP_LOSS_PCT), 2)
        tp_price    = round(close * (1 + TAKE_PROFIT_PCT), 2)
        entry_date  = date
        entry_score = score_val

    # Close any open trade at end of date range
    if in_trade and not df.empty:
        last_row  = df.iloc[-1]
        exit_price = float(last_row["c"])
        pnl_pct    = (exit_price - entry_price) / entry_price
        running_eq *= (1 + pnl_pct)
        equity_curve.append(running_eq)
        risk_per_share = entry_price - stop_price
        r_mult         = ((exit_price - entry_price) / risk_per_share) if risk_per_share > 0 else None
        trades.append({
            "entry_date":  entry_date,
            "exit_date":   last_row["t"],
            "entry_price": round(entry_price, 2),
            "exit_price":  round(exit_price, 2),
            "pnl_pct":     round(pnl_pct * 100, 2),
            "exit_reason": "END_OF_PERIOD",
            "r_multiple":  round(r_mult, 3) if r_mult is not None else None,
            "entry_score": entry_score,
        })

    # ── Summary stats ──────────────────────────────────────────────────────
    total   = len(trades)
    wins    = [t for t in trades if t["pnl_pct"] > 0]
    losses  = [t for t in trades if t["pnl_pct"] <= 0]
    pnl_pcts = [t["pnl_pct"] for t in trades]
    rs       = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]

    win_rate      = round(len(wins) / total * 100, 1) if total else 0.0
    avg_win       = round(sum(t["pnl_pct"] for t in wins)   / len(wins),   2) if wins   else 0.0
    avg_loss      = round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0.0
    gross_profit  = sum(t["pnl_pct"] for t in wins)   if wins   else 0.0
    gross_loss    = abs(sum(t["pnl_pct"] for t in losses)) if losses else 0.0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None
    expectancy    = round(
        (win_rate / 100 * avg_win) + ((100 - win_rate) / 100 * avg_loss), 2
    ) if total else 0.0
    avg_r = round(sum(rs) / len(rs), 3) if rs else None
    total_ret = round(sum(pnl_pcts), 2)

    # Max drawdown on equity curve
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return {
        "symbol":           symbol,
        "bars_tested":      len(df),
        "total_trades":     total,
        "winning_trades":   len(wins),
        "losing_trades":    len(losses),
        "win_rate_pct":     win_rate,
        "avg_win_pct":      avg_win,
        "avg_loss_pct":     avg_loss,
        "profit_factor":    profit_factor,
        "expectancy_pct":   expectancy,
        "avg_r_multiple":   avg_r,
        "total_return_pct": total_ret,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "final_equity_x":  round(running_eq, 4),
        "trades":           trades,
    }


def _print_table(results: list[dict]):
    """Print aligned summary table."""
    headers = [
        "Symbol", "Trades", "WinRate%", "AvgWin%", "AvgLoss%",
        "PF", "Expect%", "AvgR", "TotalRet%", "MaxDD%",
    ]
    rows = []
    for r in results:
        if "error" in r:
            rows.append([r["symbol"], "ERROR: " + r["error"]] + [""] * 8)
            continue
        rows.append([
            r["symbol"],
            str(r["total_trades"]),
            f"{r['win_rate_pct']:.1f}",
            f"{r['avg_win_pct']:.2f}",
            f"{r['avg_loss_pct']:.2f}",
            str(r["profit_factor"]) if r["profit_factor"] else "N/A",
            f"{r['expectancy_pct']:.2f}",
            str(r["avg_r_multiple"]) if r["avg_r_multiple"] else "N/A",
            f"{r['total_return_pct']:.2f}",
            f"{r['max_drawdown_pct']:.2f}",
        ])

    col_w = [max(len(h), max((len(row[i]) for row in rows), default=0)) for i, h in enumerate(headers)]
    sep   = "  ".join("-" * w for w in col_w)
    head  = "  ".join(h.ljust(col_w[i]) for i, h in enumerate(headers))
    print("\n" + head)
    print(sep)
    for row in rows:
        print("  ".join(str(v).ljust(col_w[i]) for i, v in enumerate(row)))
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Backtest trading strategy on historical Alpaca daily bars",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--symbols", required=True,
        help="Comma-separated ticker list, e.g. SOFI,HOOD,F,RIOT,MARA,SNAP,XLK",
    )
    parser.add_argument(
        "--start", required=True,
        help="Start date YYYY-MM-DD",
    )
    parser.add_argument(
        "--end", required=True,
        help="End date YYYY-MM-DD (inclusive)",
    )
    parser.add_argument(
        "--trades", action="store_true",
        help="Print individual trade list for each symbol",
    )
    args = parser.parse_args()

    if not API_KEY or not SECRET_KEY:
        sys.exit("[ERROR] ALPACA_API_KEY / ALPACA_SECRET_KEY not set. Load your .env first.")

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    print(f"\nBacktest: {args.start} → {args.end}")
    print(f"Symbols : {', '.join(symbols)}")
    print(f"Config  : SL={STOP_LOSS_PCT*100:.1f}%  TP={TAKE_PROFIT_PCT*100:.1f}%  "
          f"Max alloc={MAX_ALLOCATION_PCT*100:.0f}%  Equity=${PAPER_EQUITY:.0f}")
    print("-" * 60)

    results = []
    for sym in symbols:
        print(f"  Running {sym}...", end=" ", flush=True)
        r = _backtest_symbol(sym, args.start, args.end)
        if "error" in r:
            print(f"ERROR — {r['error']}")
        else:
            print(
                f"{r['total_trades']} trades | win={r['win_rate_pct']}% | "
                f"PF={r['profit_factor']} | MaxDD={r['max_drawdown_pct']}%"
            )
            if args.trades:
                for t in r["trades"]:
                    score_tag = f" score={t['entry_score']}" if t.get("entry_score") is not None else ""
                    print(
                        f"    {t['entry_date'][:10]} → {t['exit_date'][:10]} | "
                        f"entry={t['entry_price']} exit={t['exit_price']} | "
                        f"pnl={t['pnl_pct']:+.2f}% | {t['exit_reason']}"
                        f"{score_tag}"
                    )
        results.append(r)

    # Summary table
    valid = [r for r in results if "error" not in r]
    _print_table(results)

    if valid:
        # Best / worst symbol
        best  = max(valid, key=lambda r: r["total_return_pct"])
        worst = min(valid, key=lambda r: r["total_return_pct"])
        all_trades = sum(r["total_trades"] for r in valid)
        all_wins   = sum(r["winning_trades"] for r in valid)
        avg_wr     = round(all_wins / all_trades * 100, 1) if all_trades else 0.0
        print(f"Combined: {all_trades} trades across {len(valid)} symbols | "
              f"avg win rate={avg_wr}%")
        print(f"Best:  {best['symbol']} +{best['total_return_pct']:.2f}%")
        print(f"Worst: {worst['symbol']} {worst['total_return_pct']:+.2f}%\n")


if __name__ == "__main__":
    main()
