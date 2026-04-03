import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

app = FastAPI()

trade_log = []

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL")
DATA_URL = os.getenv("ALPACA_DATA_URL")

WATCHLIST = ["GOOGL"]


@app.get("/")
def root():
    return {"message": "Trading Bot API is running"}


@app.get("/account")
def get_account():
    url = f"{BASE_URL}/v2/account"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@app.get("/stock/{symbol}")
def get_stock(symbol: str):
    url = f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@app.get("/bars/{symbol}")
def get_bars(symbol: str):
    url = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=120)

    params = {
        "timeframe": "1Day",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": 100,
        "feed": "iex",
        "sort": "asc",
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@app.get("/sma/{symbol}")
def get_sma(symbol: str):
    url = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=180)

    params = {
        "timeframe": "1Day",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": 200,
        "feed": "iex",
        "sort": "asc",
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
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
            "symbol": symbol,
            "latest_close": round(float(latest["c"]), 2),
            "sma_20": round(float(latest["sma_20"]), 2),
            "sma_50": round(float(latest["sma_50"]), 2),
        }
    except Exception as e:
        return {"error": f"Failed to calculate SMA: {str(e)}"}


@app.get("/signal/{symbol}")
def get_signal(symbol: str):
    url = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=180)

    params = {
        "timeframe": "1Day",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": 200,
        "feed": "iex",
        "sort": "asc",
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
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

        close = float(latest["c"])
        sma20 = float(latest["sma_20"])
        sma50 = float(latest["sma_50"])
    except Exception as e:
        return {"error": f"Failed to calculate signal: {str(e)}"}

    if close > sma20 and sma20 > sma50:
        signal = "BUY"
    elif close < sma20:
        signal = "SELL"
    else:
        signal = "HOLD"

    return {
        "symbol": symbol,
        "close": round(close, 2),
        "sma_20": round(sma20, 2),
        "sma_50": round(sma50, 2),
        "signal": signal,
    }


def is_market_open() -> bool:
    url = f"{BASE_URL}/v2/clock"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        return data.get("is_open", False)
    except Exception:
        return False


def calculate_position_size(price: float) -> int:
    url = f"{BASE_URL}/v2/account"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        equity = float(data.get("equity", 0))
    except Exception:
        equity = 0

    if equity <= 0 or price <= 0:
        return 1

    risk_amount = equity * 0.01
    size = int(risk_amount / price)
    return max(size, 1)


def _log_trade(symbol: str, result: dict):
    trade_log.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "signal": result["signal"],
        "starting_qty": result["starting_qty"],
        "actions": result["actions"],
        "message": result.get("message"),
    })


def get_open_orders(symbol: str):
    url = f"{BASE_URL}/v2/orders"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }
    params = {
        "status": "open",
        "symbols": symbol,
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
        return []
    except Exception:
        return []


def cancel_order(order_id: str):
    url = f"{BASE_URL}/v2/orders/{order_id}"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }

    try:
        response = requests.delete(url, headers=headers, timeout=10)
        if response.status_code == 204:
            return {"status": "cancelled"}
        return {
            "status_code": response.status_code,
            "response": response.text,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/position/{symbol}")
def get_position(symbol: str):
    url = f"{BASE_URL}/v2/positions/{symbol}"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpaca request failed: {str(e)}"}

    if response.status_code == 200:
        try:
            return response.json()
        except Exception:
            return {"error": "Invalid JSON in position response"}

    return {
        "error": True,
        "status_code": response.status_code,
        "response": response.text,
    }


@app.post("/trade/{symbol}")
def execute_trade(symbol: str):
    signal_data = get_signal(symbol)
    signal = signal_data.get("signal")

    if "error" in signal_data:
        result = {
            "signal": "ERROR",
            "starting_qty": 0,
            "actions": [],
            "message": signal_data["error"],
        }
        _log_trade(symbol, result)
        return result

    position_data = get_position(symbol)
    starting_qty = 0

    if position_data and "qty" in position_data:
        starting_qty = int(float(position_data["qty"]))

    print(f"DEBUG execute_trade {symbol} | position_data = {position_data}")
    print(f"DEBUG execute_trade {symbol} | starting_qty = {starting_qty} | signal = {signal}")

    if not is_market_open():
        result = {
            "signal": signal,
            "starting_qty": starting_qty,
            "actions": [],
            "message": "Market is closed",
        }
        _log_trade(symbol, result)
        return result

    trade_qty = calculate_position_size(signal_data["close"])
    actions = []

    orders_url = f"{BASE_URL}/v2/orders"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }

    open_orders = get_open_orders(symbol)
    for order in open_orders:
        order_id = order.get("id")
        if not order_id:
            actions.append({
                "step": "cancel_order",
                "order_id": None,
                "response": {"error": "Missing order id"},
            })
            continue

        cancel_result = cancel_order(order_id)
        actions.append({
            "step": "cancel_order",
            "order_id": order_id,
            "response": cancel_result,
        })

    if signal == "HOLD":
        result = {
            "signal": signal,
            "starting_qty": starting_qty,
            "actions": actions,
            "message": "Holding",
        }
        _log_trade(symbol, result)
        return result

    if signal == "BUY":
        if starting_qty > 0:
            result = {
                "signal": signal,
                "starting_qty": starting_qty,
                "actions": actions,
                "message": "Already in long position",
            }
            _log_trade(symbol, result)
            return result

        # Close any leftover short before going long
        if starting_qty < 0:
            close_order = {
                "symbol": symbol,
                "qty": abs(starting_qty),
                "side": "buy",
                "type": "market",
                "time_in_force": "gtc",
            }
            try:
                close_response = requests.post(orders_url, json=close_order, headers=headers, timeout=10)
                close_result = close_response.json()
            except Exception as e:
                close_result = {"error": str(e)}
            actions.append({
                "step": "close_short",
                "response": close_result,
            })

        open_order = {
            "symbol": symbol,
            "qty": trade_qty,
            "side": "buy",
            "type": "market",
            "time_in_force": "gtc",
        }
        try:
            open_response = requests.post(orders_url, json=open_order, headers=headers, timeout=10)
            open_result = open_response.json()
        except Exception as e:
            open_result = {"error": str(e)}
        actions.append({
            "step": "open_long",
            "response": open_result,
        })

        stop_loss_price = round(signal_data["close"] * 0.97, 2)
        stop_order = {
            "symbol": symbol,
            "qty": trade_qty,
            "side": "sell",
            "type": "stop",
            "stop_price": stop_loss_price,
            "time_in_force": "gtc",
        }
        try:
            stop_response = requests.post(orders_url, json=stop_order, headers=headers, timeout=10)
            stop_result = stop_response.json()
        except Exception as e:
            stop_result = {"error": str(e)}
        actions.append({
            "step": "long_stop_loss",
            "stop_price": stop_loss_price,
            "response": stop_result,
        })

        print(f"DEBUG execute_trade {symbol} | opened long qty={trade_qty} stop={stop_loss_price}")

        result = {
            "signal": signal,
            "starting_qty": starting_qty,
            "actions": actions,
            "message": "Opened long position",
        }
        _log_trade(symbol, result)
        return result

    if signal == "SELL":
        # Close long position
        if starting_qty > 0:
            close_order = {
                "symbol": symbol,
                "qty": abs(starting_qty),
                "side": "sell",
                "type": "market",
                "time_in_force": "gtc",
            }
            try:
                close_response = requests.post(orders_url, json=close_order, headers=headers, timeout=10)
                close_result = close_response.json()
            except Exception as e:
                close_result = {"error": str(e)}
            actions.append({
                "step": "close_long",
                "response": close_result,
            })

            print(f"DEBUG execute_trade {symbol} | closed long qty={starting_qty}")

            result = {
                "signal": signal,
                "starting_qty": starting_qty,
                "actions": actions,
                "message": "Closed long position",
            }
            _log_trade(symbol, result)
            return result

        # Close any leftover short (do not open new short)
        if starting_qty < 0:
            close_order = {
                "symbol": symbol,
                "qty": abs(starting_qty),
                "side": "buy",
                "type": "market",
                "time_in_force": "gtc",
            }
            try:
                close_response = requests.post(orders_url, json=close_order, headers=headers, timeout=10)
                close_result = close_response.json()
            except Exception as e:
                close_result = {"error": str(e)}
            actions.append({
                "step": "close_legacy_short",
                "response": close_result,
            })

            result = {
                "signal": signal,
                "starting_qty": starting_qty,
                "actions": actions,
                "message": "Closed leftover short position",
            }
            _log_trade(symbol, result)
            return result

        result = {
            "signal": signal,
            "starting_qty": starting_qty,
            "actions": actions,
            "message": "No position to close",
        }
        _log_trade(symbol, result)
        return result

    result = {
        "signal": signal,
        "starting_qty": starting_qty,
        "actions": actions,
        "message": "Unexpected signal state",
    }
    _log_trade(symbol, result)
    return result


@app.get("/trade-log")
def get_trade_log():
    return trade_log


def _fetch_daily_bars(symbol: str, days: int = 365) -> pd.DataFrame:
    url = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
    }

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    params = {
        "timeframe": "1Day",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": 500,
        "feed": "iex",
        "sort": "asc",
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception:
        return pd.DataFrame()

    bars = data.get("bars", [])
    if not bars or not isinstance(bars, list):
        return pd.DataFrame()

    try:
        df = pd.DataFrame(bars)
        df["sma_20"] = df["c"].rolling(window=20).mean()
        df["sma_50"] = df["c"].rolling(window=50).mean()
        return df
    except Exception:
        return pd.DataFrame()


@app.get("/backtest/{symbol}")
def backtest(symbol: str):
    df = _fetch_daily_bars(symbol)

    if df.empty:
        return {"error": "No data returned for symbol"}

    df = df.dropna(subset=["sma_20", "sma_50"]).reset_index(drop=True)

    if df.empty:
        return {"error": "Not enough data to calculate SMA 50"}

    in_trade = False
    entry_date = None
    entry_price = None
    stop_loss_price = None
    trades = []

    for _, row in df.iterrows():
        close = float(row["c"])
        sma_20 = float(row["sma_20"])
        sma_50 = float(row["sma_50"])
        date = row["t"]

        if in_trade:
            if close <= stop_loss_price:
                trades.append({
                    "side": "long",
                    "entry_date": entry_date,
                    "entry_price": round(entry_price, 2),
                    "exit_date": date,
                    "exit_price": round(close, 2),
                    "pnl": round(close - entry_price, 2),
                    "exit_reason": "stop_loss",
                })
                in_trade = False
                entry_date = None
                entry_price = None
                stop_loss_price = None
                continue

            if close < sma_20:
                trades.append({
                    "side": "long",
                    "entry_date": entry_date,
                    "entry_price": round(entry_price, 2),
                    "exit_date": date,
                    "exit_price": round(close, 2),
                    "pnl": round(close - entry_price, 2),
                    "exit_reason": "signal_exit",
                })
                in_trade = False
                entry_date = None
                entry_price = None
                stop_loss_price = None
                continue

        if not in_trade:
            if close > sma_20 and sma_20 > sma_50:
                in_trade = True
                entry_date = date
                entry_price = close
                stop_loss_price = round(entry_price * 0.97, 2)

    if in_trade:
        last = df.iloc[-1]
        last_close = float(last["c"])
        trades.append({
            "side": "long",
            "entry_date": entry_date,
            "entry_price": round(entry_price, 2),
            "exit_date": last["t"],
            "exit_price": round(last_close, 2),
            "pnl": round(last_close - entry_price, 2),
            "exit_reason": "final_bar_exit",
        })

    total_trades = len(trades)
    winning_trades = sum(1 for t in trades if t["pnl"] > 0)
    losing_trades = sum(1 for t in trades if t["pnl"] <= 0)
    win_rate = round(winning_trades / total_trades * 100, 1) if total_trades > 0 else 0.0
    total_profit = round(sum(t["pnl"] for t in trades), 2)
    total_return_pct = round(
        sum((t["exit_price"] - t["entry_price"]) / t["entry_price"] for t in trades) * 100,
        2,
    ) if trades else 0.0

    return {
        "symbol": symbol,
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": win_rate,
        "total_profit": total_profit,
        "total_return_pct": total_return_pct,
        "trades": trades,
    }


@app.get("/positions-watchlist")
def positions_watchlist():
    results = []

    for symbol in WATCHLIST:
        try:
            data = get_position(symbol)
            print(f"DEBUG positions_watchlist {symbol} | {data}")

            if not data or "qty" not in data:
                results.append({
                    "symbol": symbol,
                    "qty": 0,
                    "side": "flat",
                    "market_value": 0,
                    "unrealized_pl": 0,
                })
                continue

            qty = int(float(data["qty"]))

            if qty > 0:
                side = "long"
            elif qty < 0:
                side = "short"
            else:
                side = "flat"

            results.append({
                "symbol": symbol,
                "qty": qty,
                "side": side,
                "market_value": float(data.get("market_value", 0)),
                "unrealized_pl": float(data.get("unrealized_pl", 0)),
            })
        except Exception as e:
            results.append({
                "symbol": symbol,
                "error": str(e),
            })

    return {"results": results}


@app.post("/trade-watchlist")
def trade_watchlist():
    results = []

    for symbol in WATCHLIST:
        try:
            result = execute_trade(symbol)
            results.append({
                "symbol": symbol,
                "signal": result.get("signal"),
                "starting_qty": result.get("starting_qty"),
                "actions": result.get("actions", []),
                "message": result.get("message"),
            })
        except Exception as e:
            results.append({
                "symbol": symbol,
                "error": str(e),
            })

    return {"results": results}


@app.get("/scan-watchlist")
def scan_watchlist():
    results = []

    for symbol in WATCHLIST:
        try:
            data = get_signal(symbol)

            if "error" in data:
                results.append({"symbol": symbol, "error": data["error"]})
                continue

            results.append({
                "symbol": symbol,
                "close": data["close"],
                "sma_20": data["sma_20"],
                "sma_50": data["sma_50"],
                "signal": data["signal"],
            })
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})

    return {"results": results}


@app.get("/backtest-watchlist")
def backtest_watchlist():
    results = []

    for symbol in WATCHLIST:
        try:
            data = backtest(symbol)

            if "error" in data:
                results.append({"symbol": symbol, "error": data["error"]})
                continue

            results.append({
                "symbol": data["symbol"],
                "total_trades": data["total_trades"],
                "winning_trades": data["winning_trades"],
                "losing_trades": data["losing_trades"],
                "win_rate": data["win_rate"],
                "total_profit": data["total_profit"],
                "total_return_pct": data["total_return_pct"],
            })
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})

    valid_results = [r for r in results if "error" not in r]

    if not valid_results:
        return {"error": "No backtest data available for any symbol in the watchlist", "results": results}

    best_by_profit = max(valid_results, key=lambda r: r["total_profit"])["symbol"]
    best_by_win_rate = max(valid_results, key=lambda r: r["win_rate"])["symbol"]

    return {
        "results": results,
        "best_symbol_by_profit": best_by_profit,
        "best_symbol_by_win_rate": best_by_win_rate,
    }
