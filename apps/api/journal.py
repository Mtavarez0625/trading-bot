"""
Persistent paper-trading journal — SQLite backend.

Two tables:
  trade_events  — one row per cycle result per symbol (every _log_trade call)
  paper_trades  — full lifecycle: entry → exit with realized PnL and slippage
"""

import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "trading_journal.db"

_DDL_EVENTS = """
CREATE TABLE IF NOT EXISTS trade_events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT NOT NULL,
    symbol                TEXT NOT NULL,
    signal                TEXT,
    decision_summary      TEXT,
    signal_reason         TEXT,
    blocked_by            TEXT,
    entry_tier            TEXT,
    starting_qty          INTEGER,
    entry_price           REAL,
    stop_price            REAL,
    take_profit_price     REAL,
    trailing_stop_pct     REAL,
    dry_run               INTEGER,
    rsi                   REAL,
    macd_line             REAL,
    macd_signal_line      REAL,
    macd_histogram        REAL,
    macd_histogram_rising INTEGER,
    trend_strength        REAL,
    volume_confirmed      INTEGER,
    current_volume        INTEGER,
    vol_sma_20            REAL,
    breakout_confirmed    INTEGER,
    intraday_confirmed    INTEGER,
    intraday_reason       TEXT,
    spy_bullish           INTEGER,
    spy_reason            TEXT,
    score                 INTEGER,
    grade                 TEXT
)
"""

_DDL_PAPER = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                TEXT NOT NULL,
    entry_timestamp       TEXT NOT NULL,
    exit_timestamp        TEXT,
    entry_price           REAL,
    stop_price            REAL,
    take_profit_price     REAL,
    trailing_stop_pct     REAL,
    qty                   INTEGER,
    slippage_pct          REAL,
    exit_price            REAL,
    exit_reason           TEXT,
    realized_pnl          REAL,
    realized_pnl_pct      REAL,
    realized_r_multiple   REAL,
    hold_time_minutes     REAL,
    entry_tier            TEXT,
    rsi                   REAL,
    macd_line             REAL,
    macd_signal_line      REAL,
    macd_histogram        REAL,
    macd_histogram_rising INTEGER,
    trend_strength        REAL,
    volume_confirmed      INTEGER,
    breakout_confirmed    INTEGER,
    intraday_confirmed    INTEGER,
    entry_score           INTEGER,
    entry_grade           TEXT,
    is_open               INTEGER DEFAULT 1
)
"""


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _migrate_db(con):
    """Add columns introduced after the initial schema — safe to run on any existing DB."""
    existing_events = {row[1] for row in con.execute("PRAGMA table_info(trade_events)")}
    for col, typ in [("score", "INTEGER"), ("grade", "TEXT")]:
        if col not in existing_events:
            con.execute(f"ALTER TABLE trade_events ADD COLUMN {col} {typ}")

    existing_paper = {row[1] for row in con.execute("PRAGMA table_info(paper_trades)")}
    for col, typ in [("entry_score", "INTEGER"), ("entry_grade", "TEXT")]:
        if col not in existing_paper:
            con.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {typ}")


def init_db():
    with _conn() as con:
        con.execute(_DDL_EVENTS)
        con.execute(_DDL_PAPER)
        _migrate_db(con)
    print(f"[journal] DB ready at {DB_PATH}")


# ── trade_events ──────────────────────────────────────────────────────────────

def log_event(event: dict):
    def _bool_int(v):
        return int(bool(v)) if v is not None else None

    sql = """
    INSERT INTO trade_events (
        timestamp, symbol, signal, decision_summary, signal_reason,
        blocked_by, entry_tier, starting_qty, entry_price, stop_price,
        take_profit_price, trailing_stop_pct, dry_run,
        rsi, macd_line, macd_signal_line, macd_histogram, macd_histogram_rising,
        trend_strength, volume_confirmed, current_volume, vol_sma_20,
        breakout_confirmed, intraday_confirmed, intraday_reason, spy_bullish, spy_reason,
        score, grade
    ) VALUES (
        :timestamp, :symbol, :signal, :decision_summary, :signal_reason,
        :blocked_by, :entry_tier, :starting_qty, :entry_price, :stop_price,
        :take_profit_price, :trailing_stop_pct, :dry_run,
        :rsi, :macd_line, :macd_signal_line, :macd_histogram, :macd_histogram_rising,
        :trend_strength, :volume_confirmed, :current_volume, :vol_sma_20,
        :breakout_confirmed, :intraday_confirmed, :intraday_reason, :spy_bullish, :spy_reason,
        :score, :grade
    )
    """
    try:
        with _conn() as con:
            con.execute(sql, {
                "timestamp":             event.get("timestamp"),
                "symbol":                event.get("symbol"),
                "signal":                event.get("signal"),
                "decision_summary":      event.get("decision_summary"),
                "signal_reason":         event.get("signal_reason"),
                "blocked_by":            event.get("blocked_by"),
                "entry_tier":            event.get("entry_tier"),
                "starting_qty":          event.get("starting_qty"),
                "entry_price":           event.get("entry_price"),
                "stop_price":            event.get("stop_loss_price"),
                "take_profit_price":     event.get("take_profit_price"),
                "trailing_stop_pct":     event.get("trailing_stop_pct"),
                "dry_run":               _bool_int(event.get("dry_run")),
                "rsi":                   event.get("rsi"),
                "macd_line":             event.get("macd_line"),
                "macd_signal_line":      event.get("macd_signal_line"),
                "macd_histogram":        event.get("macd_histogram"),
                "macd_histogram_rising": _bool_int(event.get("macd_histogram_rising")),
                "trend_strength":        event.get("trend_strength"),
                "volume_confirmed":      _bool_int(event.get("volume_confirmed")),
                "current_volume":        event.get("current_volume"),
                "vol_sma_20":            event.get("vol_sma_20"),
                "breakout_confirmed":    _bool_int(event.get("breakout_confirmed")),
                "intraday_confirmed":    _bool_int(event.get("intraday_confirmed")),
                "intraday_reason":       event.get("intraday_reason"),
                "spy_bullish":           _bool_int(event.get("spy_bullish")),
                "spy_reason":            event.get("spy_reason"),
                "score":                 event.get("score"),
                "grade":                 event.get("grade"),
            })
    except Exception as e:
        print(f"[journal] WARNING: failed to log event for {event.get('symbol')}: {e}")


# ── paper_trades ──────────────────────────────────────────────────────────────

def has_open_paper_trade(symbol: str) -> bool:
    """Return True if there is an open paper trade for this symbol."""
    try:
        with _conn() as con:
            row = con.execute(
                "SELECT id FROM paper_trades WHERE symbol=? AND is_open=1 LIMIT 1",
                (symbol,),
            ).fetchone()
            return row is not None
    except Exception:
        return False


def open_paper_trade(symbol: str, entry: dict):
    """Record a newly entered paper trade. Call this on BUY execution."""
    def _bool_int(v):
        return int(bool(v)) if v is not None else None

    sql = """
    INSERT INTO paper_trades (
        symbol, entry_timestamp, entry_price, stop_price, take_profit_price,
        trailing_stop_pct, qty, slippage_pct,
        entry_tier, rsi, macd_line, macd_signal_line, macd_histogram,
        macd_histogram_rising, trend_strength, volume_confirmed,
        breakout_confirmed, intraday_confirmed, entry_score, entry_grade, is_open
    ) VALUES (
        :symbol, :entry_timestamp, :entry_price, :stop_price, :take_profit_price,
        :trailing_stop_pct, :qty, :slippage_pct,
        :entry_tier, :rsi, :macd_line, :macd_signal_line, :macd_histogram,
        :macd_histogram_rising, :trend_strength, :volume_confirmed,
        :breakout_confirmed, :intraday_confirmed, :entry_score, :entry_grade, 1
    )
    """
    try:
        with _conn() as con:
            con.execute(sql, {
                "symbol":               symbol,
                "entry_timestamp":      entry.get("entry_timestamp"),
                "entry_price":          entry.get("entry_price"),
                "stop_price":           entry.get("stop_price"),
                "take_profit_price":    entry.get("take_profit_price"),
                "trailing_stop_pct":    entry.get("trailing_stop_pct"),
                "qty":                  entry.get("qty"),
                "slippage_pct":         entry.get("slippage_pct", 0.0),
                "entry_tier":           entry.get("entry_tier"),
                "rsi":                  entry.get("rsi"),
                "macd_line":            entry.get("macd_line"),
                "macd_signal_line":     entry.get("macd_signal_line"),
                "macd_histogram":       entry.get("macd_histogram"),
                "macd_histogram_rising": _bool_int(entry.get("macd_histogram_rising")),
                "trend_strength":       entry.get("trend_strength"),
                "volume_confirmed":     _bool_int(entry.get("volume_confirmed")),
                "breakout_confirmed":   _bool_int(entry.get("breakout_confirmed")),
                "intraday_confirmed":   _bool_int(entry.get("intraday_confirmed")),
                "entry_score":          entry.get("entry_score"),
                "entry_grade":          entry.get("entry_grade"),
            })
    except Exception as e:
        print(f"[journal] WARNING: failed to open paper trade for {symbol}: {e}")


def close_paper_trade(symbol: str, exit_price: float, exit_reason: str):
    """
    Close the most recent open paper trade for symbol.
    PnL includes slippage stored at entry time.
    Approximation: uses signal-bar close as exit price (end-of-bar, not exact tick).
    """
    try:
        with _conn() as con:
            row = con.execute(
                "SELECT id, entry_timestamp, entry_price, stop_price, qty, slippage_pct "
                "FROM paper_trades WHERE symbol=? AND is_open=1 "
                "ORDER BY entry_timestamp DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            if not row:
                return

            trade_id    = row["id"]
            entry_price = row["entry_price"] or 0.0
            stop_price  = row["stop_price"]  or 0.0
            qty         = row["qty"]         or 0
            slippage    = row["slippage_pct"] or 0.0
            entry_ts    = row["entry_timestamp"]

            # Slippage: buy higher on entry, sell lower on exit
            eff_entry = entry_price * (1 + slippage)
            eff_exit  = exit_price  * (1 - slippage)

            realized_pnl     = round((eff_exit - eff_entry) * qty, 4)
            realized_pnl_pct = (
                round((eff_exit - eff_entry) / eff_entry * 100, 4)
                if eff_entry > 0 else 0.0
            )
            risk_per_share = eff_entry - stop_price
            realized_r = (
                round((eff_exit - eff_entry) / risk_per_share, 4)
                if risk_per_share > 0 else None
            )

            exit_ts = datetime.now(timezone.utc).isoformat()
            hold_minutes = None
            try:
                entry_dt = datetime.fromisoformat(entry_ts)
                exit_dt  = datetime.fromisoformat(exit_ts)
                hold_minutes = round((exit_dt - entry_dt).total_seconds() / 60, 1)
            except Exception:
                pass

            con.execute("""
                UPDATE paper_trades SET
                    exit_timestamp=?, exit_price=?, exit_reason=?,
                    realized_pnl=?, realized_pnl_pct=?, realized_r_multiple=?,
                    hold_time_minutes=?, is_open=0
                WHERE id=?
            """, (
                exit_ts, exit_price, exit_reason,
                realized_pnl, realized_pnl_pct, realized_r,
                hold_minutes, trade_id,
            ))

            print(
                f"[journal] {symbol} paper trade closed | exit={exit_price} "
                f"reason={exit_reason} pnl={realized_pnl} R={realized_r}"
            )
    except Exception as e:
        print(f"[journal] WARNING: failed to close paper trade for {symbol}: {e}")


def get_open_paper_positions() -> list:
    try:
        with _conn() as con:
            rows = con.execute(
                "SELECT symbol, entry_timestamp, entry_price, stop_price, "
                "take_profit_price, qty, entry_tier "
                "FROM paper_trades WHERE is_open=1 ORDER BY entry_timestamp DESC"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


# ── Analytics queries ─────────────────────────────────────────────────────────

def query_performance_summary() -> dict:
    try:
        with _conn() as con:
            total_entries = con.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
            total_exits   = con.execute("SELECT COUNT(*) FROM paper_trades WHERE is_open=0").fetchone()[0]
            open_count    = con.execute("SELECT COUNT(*) FROM paper_trades WHERE is_open=1").fetchone()[0]

            closed = con.execute(
                "SELECT realized_pnl, realized_r_multiple, hold_time_minutes "
                "FROM paper_trades WHERE is_open=0 AND realized_pnl IS NOT NULL"
            ).fetchall()

            if not closed:
                return {
                    "total_entries":  total_entries,
                    "total_exits":    total_exits,
                    "open_positions": open_count,
                    "message":        "No closed trades yet",
                }

            pnls   = [r["realized_pnl"] for r in closed]
            wins   = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            rs     = [r["realized_r_multiple"] for r in closed if r["realized_r_multiple"] is not None]
            holds  = [r["hold_time_minutes"]  for r in closed if r["hold_time_minutes"]  is not None]

            win_rate  = round(len(wins)   / len(pnls) * 100, 1) if pnls else 0.0
            loss_rate = round(len(losses) / len(pnls) * 100, 1) if pnls else 0.0
            avg_win   = round(sum(wins)   / len(wins),   4) if wins   else 0.0
            avg_loss  = round(sum(losses) / len(losses), 4) if losses else 0.0
            total_pnl = round(sum(pnls), 4)

            gross_profit  = sum(wins)
            gross_loss    = abs(sum(losses))
            profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else None
            expectancy    = round((win_rate / 100 * avg_win) + (loss_rate / 100 * avg_loss), 4)

            # Max drawdown on cumulative PnL curve
            running = peak = max_dd = 0.0
            for p in pnls:
                running += p
                if running > peak:
                    peak = running
                dd = peak - running
                if dd > max_dd:
                    max_dd = dd

            # Best/worst symbol
            sym_rows = con.execute(
                "SELECT symbol, SUM(realized_pnl) as total FROM paper_trades "
                "WHERE is_open=0 AND realized_pnl IS NOT NULL GROUP BY symbol"
            ).fetchall()
            sym_pnl = {r["symbol"]: r["total"] for r in sym_rows}

            return {
                "total_entries":       total_entries,
                "total_exits":         total_exits,
                "open_positions":      open_count,
                "win_rate":            win_rate,
                "loss_rate":           loss_rate,
                "avg_win":             avg_win,
                "avg_loss":            avg_loss,
                "expectancy":          expectancy,
                "profit_factor":       profit_factor,
                "total_simulated_pnl": total_pnl,
                "max_drawdown":        round(max_dd, 4),
                "avg_hold_minutes":    round(sum(holds) / len(holds), 1) if holds else None,
                "avg_r_multiple":      round(sum(rs) / len(rs), 3) if rs else None,
                "largest_winner":      round(max(pnls), 4) if pnls else None,
                "largest_loser":       round(min(pnls), 4) if pnls else None,
                "best_symbol":         max(sym_pnl, key=sym_pnl.get) if sym_pnl else None,
                "worst_symbol":        min(sym_pnl, key=sym_pnl.get) if sym_pnl else None,
            }
    except Exception as e:
        return {"error": str(e)}


def query_symbol_performance() -> list:
    try:
        with _conn() as con:
            symbols = [r[0] for r in con.execute(
                "SELECT DISTINCT symbol FROM trade_events ORDER BY symbol"
            ).fetchall()]

            results = []
            for sym in symbols:
                total_setups  = con.execute(
                    "SELECT COUNT(*) FROM trade_events WHERE symbol=?", (sym,)
                ).fetchone()[0]
                entries_taken = con.execute(
                    "SELECT COUNT(*) FROM paper_trades WHERE symbol=?", (sym,)
                ).fetchone()[0]
                blocked_count = con.execute(
                    "SELECT COUNT(*) FROM trade_events WHERE symbol=? AND blocked_by IS NOT NULL", (sym,)
                ).fetchone()[0]

                blocker_rows = con.execute(
                    "SELECT blocked_by, COUNT(*) as cnt FROM trade_events "
                    "WHERE symbol=? AND blocked_by IS NOT NULL GROUP BY blocked_by",
                    (sym,),
                ).fetchall()
                blocker_breakdown = {r["blocked_by"]: r["cnt"] for r in blocker_rows}

                closed = con.execute(
                    "SELECT realized_pnl, realized_r_multiple FROM paper_trades "
                    "WHERE symbol=? AND is_open=0 AND realized_pnl IS NOT NULL",
                    (sym,),
                ).fetchall()

                pnls = [r["realized_pnl"] for r in closed]
                rs   = [r["realized_r_multiple"] for r in closed if r["realized_r_multiple"] is not None]
                wins = [p for p in pnls if p > 0]

                results.append({
                    "symbol":            sym,
                    "total_setups":      total_setups,
                    "entries_taken":     entries_taken,
                    "blocked_count":     blocked_count,
                    "win_rate":          round(len(wins) / len(pnls) * 100, 1) if pnls else None,
                    "total_pnl":         round(sum(pnls), 4) if pnls else 0.0,
                    "avg_pnl":           round(sum(pnls) / len(pnls), 4) if pnls else None,
                    "avg_r":             round(sum(rs) / len(rs), 3) if rs else None,
                    "blocker_breakdown": blocker_breakdown,
                })

            return results
    except Exception as e:
        return [{"error": str(e)}]


def query_recent_trades(limit: int = 20) -> list:
    try:
        with _conn() as con:
            rows = con.execute(
                "SELECT symbol, entry_timestamp, exit_timestamp, entry_price, exit_price, "
                "exit_reason, realized_pnl, realized_pnl_pct, realized_r_multiple, "
                "qty, entry_tier, hold_time_minutes, is_open "
                "FROM paper_trades ORDER BY entry_timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]
