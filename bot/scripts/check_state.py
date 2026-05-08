#!/usr/bin/env python3
"""
check_state.py — Pre-market state inspection tool.

Prints:
  1. Loaded config (watchlist, risk params, window)
  2. Alpaca open positions (real or paper)
  3. Journal open dry-run positions
  4. Trade cooldown state (apps/api in-memory — read via HTTP)
  5. Sizing preview: qty each symbol would produce right now

Usage (from the bot/ directory with venv active):
    python scripts/check_state.py

Usage against the running API server (apps/api):
    python scripts/check_state.py --api http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv

load_dotenv()


def _print_section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print("=" * 60)


def check_via_api(base_url: str) -> None:
    """Query the running FastAPI bot for its live state."""
    import requests

    _print_section("BOT API STATE")
    print(f"  Querying {base_url} ...")

    try:
        resp = requests.get(f"{base_url}/check-state", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"  ERROR: could not reach bot API: {exc}")
        print("  Make sure the bot server is running.")
        return

    print(f"\n  dry_run              : {data.get('dry_run')}")
    print(f"  paper_account_equity : ${data.get('paper_account_equity', 0):.2f}")
    print(f"  watchlist            : {data.get('watchlist')}")

    positions = data.get("alpaca_positions", [])
    _print_section(f"ALPACA POSITIONS ({len(positions)})")
    if not positions:
        print("  (none)")
    for p in positions:
        print(
            f"  {p.get('symbol'):8s}  qty={p.get('qty'):>6}  "
            f"entry=${p.get('avg_entry') or '?'}  "
            f"mktval=${p.get('market_val') or '?'}  "
            f"unrealized_pl=${p.get('unrealized_pl') or '?'}"
        )

    journal = data.get("journal_open_trades", [])
    _print_section(f"DRY-RUN JOURNAL POSITIONS ({len(journal)})")
    if not journal:
        print("  (none)")
    for p in journal:
        print(
            f"  {p.get('symbol'):8s}  qty={p.get('qty') or '?':>4}  "
            f"entry=${p.get('entry_price') or '?'}  "
            f"stop=${p.get('stop_price') or '?'}  "
            f"tp=${p.get('take_profit_price') or '?'}"
        )

    cooldowns = data.get("trade_cooldowns", {})
    _print_section(f"TRADE COOLDOWNS")
    if not cooldowns:
        print("  (none)")
    for sym, cd in cooldowns.items():
        status = "IN COOLDOWN" if cd.get("in_cooldown") else "ready"
        print(
            f"  {sym:8s}  {status}  "
            f"elapsed={cd.get('elapsed_min')}m  "
            f"remaining={cd.get('remaining_min')}m"
        )

    print(f"\n  observe_only_mode    : {data.get('observe_only_mode')}")
    print(f"  api_failure_count    : {data.get('api_failure_count')}")
    print(f"  session_start_utc    : {data.get('session_start_utc')}")

    # Fetch daily report for a complete summary
    try:
        report = requests.get(f"{base_url}/daily-report", timeout=10).json()
        _print_section("DAILY REPORT")
        cfg = report.get("config", {})
        print(f"  dry_run={cfg.get('dry_run')}  equity=${cfg.get('paper_account_equity')}  "
              f"alloc={cfg.get('max_allocation_pct', 0)*100:.0f}%  "
              f"risk={cfg.get('risk_per_trade_pct', 0)*100:.1f}%  "
              f"SL={cfg.get('stop_loss_pct', 0)*100:.0f}%  "
              f"TP={cfg.get('take_profit_pct', 0)*100:.0f}%")
        cyc = report.get("cycle_summary", {})
        print(
            f"  logged={cyc.get('total_logged')}  "
            f"entered={cyc.get('entered')}  "
            f"exited={cyc.get('exited')}  "
            f"errors={cyc.get('errors')}  "
            f"blocked={cyc.get('blocked')}"
        )
    except Exception:
        pass


def check_via_bot_module() -> None:
    """Use bot/ modules directly to print config and Alpaca positions."""
    from config import load_config
    from portfolio_guard import get_account, count_open_positions, get_open_position_symbols
    from risk import compute_quantity
    from alpaca.trading.client import TradingClient

    _print_section("BOT CONFIG (bot/.env)")
    try:
        cfg = load_config()
    except Exception as exc:
        print(f"  ERROR loading config: {exc}")
        return

    print(f"  dry_run              : {cfg.dry_run}")
    print(f"  paper                : {cfg.paper}")
    print(f"  paper_account_equity : ${cfg.paper_account_equity:.2f}")
    print(f"  max_allocation_pct   : {cfg.max_allocation_pct * 100:.0f}%")
    print(f"  risk_per_trade       : {cfg.risk_per_trade * 100:.1f}%")
    print(f"  stop_loss_pct        : {cfg.stop_loss_pct * 100:.1f}%")
    print(f"  take_profit_pct      : {cfg.take_profit_pct * 100:.1f}%")
    print(f"  daily_loss_stop      : {cfg.daily_loss_stop * 100:.1f}%")
    print(f"  max_positions        : {cfg.max_positions}")
    print(f"  entry_window         : {cfg.entry_window_start}–{cfg.entry_window_end} ET")
    print(f"  watchlist ({len(cfg.watchlist())}) : {cfg.watchlist()}")

    _print_section("ALPACA POSITIONS")
    try:
        client = TradingClient(
            api_key=cfg.api_key,
            secret_key=cfg.api_secret,
            paper=cfg.paper,
        )
        acct = get_account(client)
        if acct:
            print(
                f"  Equity: ${float(acct.equity):.2f}  "
                f"Buying power: ${float(acct.buying_power):.2f}  "
                f"Status: {acct.status}"
            )
        n = count_open_positions(client)
        print(f"  Open positions: {n}")
        open_syms = get_open_position_symbols(client)
        if open_syms:
            print(f"  Symbols: {sorted(open_syms)}")
        else:
            print("  (none)")
    except Exception as exc:
        print(f"  ERROR querying Alpaca: {exc}")

    _print_section("SIZING PREVIEW (dry-run equity)")
    equity = cfg.paper_account_equity if cfg.dry_run else 0.0
    print(f"  {'SYMBOL':8s}  {'EST PRICE':>10}  {'QTY':>5}  {'ALLOC $':>10}  {'RESULT':}")
    # Approximate prices for preview — replace with live quotes for accuracy
    preview_prices = {
        "SPY": 530.0, "QQQ": 460.0, "IWM": 195.0,
        "PLTR": 25.0, "AMD": 100.0, "XLK": 200.0,
        "SOFI": 13.0, "HOOD": 45.0, "INTC": 20.0,
    }
    for sym in cfg.watchlist():
        price = preview_prices.get(sym, 0.0)
        if price <= 0:
            print(f"  {sym:8s}  {'N/A':>10}  {'?':>5}  {'?':>10}  no price estimate")
            continue
        qty = compute_quantity(equity, cfg.risk_per_trade, price, cfg.stop_loss_pct, cfg.max_allocation_pct)
        alloc_usd = qty * price
        result = f"OK (${alloc_usd:.2f})" if qty >= 1 else "SKIP: qty=0 (unaffordable)"
        print(f"  {sym:8s}  ${price:>9.2f}  {qty:>5}  ${alloc_usd:>9.2f}  {result}")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot state inspector")
    parser.add_argument(
        "--api", metavar="URL",
        help="Query the running FastAPI bot server (e.g. http://127.0.0.1:8000)",
    )
    args = parser.parse_args()

    if args.api:
        check_via_api(args.api)
    else:
        check_via_bot_module()


if __name__ == "__main__":
    main()
