# Trading Bot

An algorithmic paper trading research system built on the [Alpaca Markets](https://alpaca.markets) API. The bot scans a configurable watchlist, evaluates technical signals, and submits bracket orders (entry + stop-loss + take-profit) automatically during market hours.

> **Disclaimer:** This project is for educational and research purposes only. It is not financial advice. Past performance in paper trading does not guarantee future results. Always use paper trading mode before risking real capital. The author assumes no liability for financial losses of any kind.

---

## Features

- **Signal engine** — EMA crossover (20/50), RSI-14 threshold, and volume confirmation
- **Bracket orders** — automated stop-loss and take-profit on every entry
- **Daily loss stop** — halts new entries when equity drops past a configurable threshold
- **Max position guard** — enforces a hard cap on simultaneous open positions
- **Per-symbol trade limits** — prevents over-trading a single ticker in one session
- **Market clock awareness** — skips cycles when the market is closed or outside the entry window
- **Asset group routing** — separate symbol lists for equities, index ETFs, and commodities
- **Dry-run mode** — full signal evaluation with no orders submitted
- **Structured logging** — per-cycle logs with equity snapshots, signal diagnostics, and trade records
- **Performance tracker** — session-level stats (signals, skips, attempts, executions)
- **FastAPI dashboard** — REST endpoints for live account data, signal inspection, backtesting, and trade history
- **SQLite trade journal** — persistent record of every trade decision via the API layer
- **pytest test suite** — unit tests covering strategy, indicators, risk, config, and more

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Broker API | [Alpaca Markets](https://alpaca.markets) (`alpaca-py`) |
| REST API | FastAPI + Uvicorn |
| Data | Alpaca Historical Data API |
| Indicators | pandas (EMA, RSI, volume rolling avg) |
| Persistence | SQLite (trade journal) |
| Testing | pytest |
| Config | python-dotenv |

---

## Architecture

```
trading-bot/
├── bot/                     # Standalone algorithmic trading engine
│   ├── main.py              # Entry point — polling loop, cycle orchestration
│   ├── config.py            # Environment-based configuration + asset groups
│   ├── strategy.py          # Signal evaluation (EMA, RSI, volume)
│   ├── indicators.py        # Technical indicator calculations
│   ├── trader.py            # Bracket order submission
│   ├── risk.py              # Position sizing, stop/take-profit levels
│   ├── portfolio_guard.py   # Account safety checks (positions, orders, equity)
│   ├── market_clock.py      # Market hours + entry window enforcement
│   ├── data_service.py      # Historical bar fetching from Alpaca
│   ├── state.py             # Per-session trade state (counts, day reset)
│   ├── performance_tracker.py  # Session stats aggregation
│   ├── signal_logger.py     # Signal audit trail to file
│   ├── trade_logger.py      # Trade execution records to file
│   ├── logger.py            # Shared structured logger
│   ├── requirements.txt     # Python dependencies
│   └── tests/               # pytest unit test suite
│
└── apps/
    └── api/                 # FastAPI monitoring and control layer
        ├── main.py          # REST API — signal scan, backtest, trade log, account info
        ├── journal.py       # SQLite trade journal (init + write)
        └── run_bot.py       # Bot runner daemon (polls API, handles market clock)
```

The two components can run independently:

- **`bot/`** — a self-contained daemon that connects directly to Alpaca and runs the full signal-to-order pipeline. Designed to run standalone via `python main.py`.
- **`apps/api/`** — a FastAPI application that exposes the same trading logic as REST endpoints. `run_bot.py` acts as a scheduler that polls the API's `/trade-watchlist` endpoint on a timer, enabling separation of concerns between strategy execution and HTTP monitoring.

---

## Setup

### Prerequisites

- Python 3.11+
- An [Alpaca Markets](https://alpaca.markets) account (paper trading is free)
- Alpaca API key and secret from the Alpaca dashboard

### Installation

**Bot (standalone engine):**

```bash
cd bot
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp ../.env.example .env       # then fill in your credentials
```

**API layer:**

```bash
cd apps/api
python -m venv venv
source venv/bin/activate
pip install fastapi uvicorn alpaca-py pandas python-dotenv requests
cp ../../.env.example .env    # then fill in your credentials
```

---

## Configuration

Copy `.env.example` to `.env` inside the relevant directory and fill in your values. A full reference is in [`.env.example`](.env.example).

### Core variables

| Variable | Description | Default |
|---|---|---|
| `ALPACA_API_KEY` | Alpaca API key ID | — |
| `ALPACA_SECRET_KEY` | Alpaca secret key | — |
| `ALPACA_PAPER` | `true` = paper trading, `false` = live | `true` |
| `DRY_RUN` | Log signals but submit no orders | `false` |
| `EQUITIES` | Comma-separated equity watchlist | `AAPL,MSFT,GOOGL` |
| `INDEX_ETFS` | Comma-separated ETF watchlist | `SPY,QQQ` |
| `RISK_PER_TRADE` | Fraction of equity risked per trade | `0.01` |
| `STOP_LOSS_PCT` | Stop-loss distance from entry | `0.02` |
| `TAKE_PROFIT_PCT` | Take-profit distance from entry | `0.04` |
| `MAX_POSITIONS` | Max simultaneous open positions | `3` |
| `DAILY_LOSS_STOP` | Halt entries if equity drops this fraction | `0.03` |

---

## Running the Bot

```bash
cd bot
source venv/bin/activate
python main.py
```

The bot will:
1. Load config from `.env`
2. Connect to Alpaca (paper by default)
3. Log account equity and watchlist on startup
4. Poll every 5 minutes during market hours
5. Evaluate signals and submit bracket orders when conditions are met
6. Stop cleanly on `Ctrl+C` and print a session summary

---

## Running the FastAPI API

**Start the API server:**

```bash
cd apps/api
source venv/bin/activate
uvicorn main:app --reload --port 8000
```

Interactive docs are available at `http://127.0.0.1:8000/docs`.

**Key endpoints:**

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Health check + version |
| `GET` | `/account` | Live account equity and buying power |
| `GET` | `/market-status` | Whether the market is currently open |
| `GET` | `/signal/{symbol}` | Evaluate a signal for a single symbol |
| `POST` | `/trade/{symbol}` | Trigger a trade attempt for one symbol |
| `POST` | `/trade-watchlist` | Scan and trade the full watchlist |
| `GET` | `/scan-watchlist` | Dry-scan signals across the watchlist |
| `GET` | `/backtest/{symbol}` | Simple backtest for a single symbol |
| `GET` | `/backtest-watchlist` | Backtest across the full watchlist |
| `GET` | `/positions-watchlist` | Open positions for all watched symbols |
| `GET` | `/performance-summary` | Session-level performance stats |
| `GET` | `/recent-trades` | Most recent trade journal entries |
| `GET` | `/config-summary` | Active strategy configuration |

**Run the bot daemon against the API:**

```bash
# In a separate terminal (API must be running first)
cd apps/api
python run_bot.py
```

`run_bot.py` polls `/trade-watchlist` every 5 minutes during the active trading window (9:30–11:30 ET by default) and handles market-closed/error states gracefully.

---

## Testing

The bot module includes a pytest suite covering signal logic, indicators, risk calculations, config loading, and more.

```bash
cd bot
source venv/bin/activate
pytest tests/ -v
```

Test files:

| File | Coverage |
|---|---|
| `test_strategy.py` | Signal evaluation (uptrend, downtrend, edge cases) |
| `test_indicators.py` | EMA, RSI, volume indicator calculations |
| `test_risk.py` | Position sizing, stop/TP price computation |
| `test_config.py` | Config loading and environment variable parsing |
| `test_market_clock.py` | Market hours and entry window logic |
| `test_portfolio_guard.py` | Account and position guard functions |
| `test_performance_tracker.py` | Session stat aggregation |

---

## Roadmap

- [ ] Multi-timeframe signal confirmation (daily + intraday)
- [ ] Trailing stop-loss support
- [ ] Telegram / webhook trade notifications
- [ ] Web dashboard UI (React + FastAPI)
- [ ] Walk-forward backtesting with Sharpe ratio reporting
- [ ] Docker + docker-compose setup for one-command deployment
- [ ] CI/CD pipeline (GitHub Actions)
- [ ] Support for options paper trading

---

## Screenshots / Demo

> _Screenshots and a demo video will be added here. Coming soon._

<!-- Add images with:
  ![Bot startup log](docs/assets/startup.png)
  ![FastAPI docs](docs/assets/api-docs.png)
-->

---

## Author

**Marcos Tavarez**
Full-Stack Developer

- Portfolio: [marcostavarez.com](https://marcostavarez.com)
- GitHub: [@mtavarez0625](https://github.com/mtavarez0625)

---

> Built for learning, experimentation, and portfolio demonstration. Always paper trade first.
