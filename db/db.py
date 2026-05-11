import sqlite3
import os
from datetime import datetime, date
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "venezuela_currency.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS rates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                bcv_rate REAL,
                parallel_rate REAL,
                spread_pct REAL,
                source TEXT,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                alert_type TEXT,
                message TEXT,
                delivered INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS daily_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                claude_summary TEXT,
                recommendation TEXT,
                urgency_level TEXT,
                raw_response TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_rates_timestamp ON rates(timestamp);
            CREATE INDEX IF NOT EXISTS idx_alerts_delivered ON alerts(delivered);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_date ON daily_analysis(date);
        """)


def insert_rate(timestamp: str, bcv_rate: float, parallel_rate: float,
                spread_pct: float, source: str, notes: str = None):
    with db() as conn:
        conn.execute(
            "INSERT INTO rates (timestamp, bcv_rate, parallel_rate, spread_pct, source, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (timestamp, bcv_rate, parallel_rate, spread_pct, source, notes)
        )


def get_recent_rates(hours: int = 24):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM rates WHERE timestamp >= datetime('now', ?) ORDER BY timestamp DESC",
            (f"-{hours} hours",)
        ).fetchall()
    return [dict(r) for r in rows]


def get_rates_last_n_days(days: int):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM rates WHERE timestamp >= datetime('now', ?) ORDER BY timestamp ASC",
            (f"-{days} days",)
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_rate():
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM rates WHERE bcv_rate IS NOT NULL AND parallel_rate IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_avg_spread(days: int):
    with db() as conn:
        row = conn.execute(
            "SELECT AVG(spread_pct) as avg FROM rates "
            "WHERE timestamp >= datetime('now', ?) AND spread_pct IS NOT NULL",
            (f"-{days} days",)
        ).fetchone()
    return row["avg"] if row and row["avg"] else None


def get_last_bcv_update():
    with db() as conn:
        row = conn.execute(
            "SELECT timestamp FROM rates WHERE bcv_rate IS NOT NULL ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    return row["timestamp"] if row else None


def insert_alert(timestamp: str, alert_type: str, message: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO alerts (timestamp, alert_type, message) VALUES (?, ?, ?)",
            (timestamp, alert_type, message)
        )


def get_undelivered_alerts():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE delivered = 0 ORDER BY timestamp ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_alert_delivered(alert_id: int):
    with db() as conn:
        conn.execute("UPDATE alerts SET delivered = 1 WHERE id = ?", (alert_id,))


def upsert_daily_analysis(date_str: str, claude_summary: str, recommendation: str,
                          urgency_level: str, raw_response: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO daily_analysis (date, claude_summary, recommendation, urgency_level, raw_response) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "claude_summary=excluded.claude_summary, recommendation=excluded.recommendation, "
            "urgency_level=excluded.urgency_level, raw_response=excluded.raw_response",
            (date_str, claude_summary, recommendation, urgency_level, raw_response)
        )


def get_weekly_data():
    with db() as conn:
        rates = conn.execute(
            "SELECT date(timestamp) as day, "
            "AVG(bcv_rate) as avg_bcv, AVG(parallel_rate) as avg_parallel, "
            "AVG(spread_pct) as avg_spread, MIN(spread_pct) as min_spread, MAX(spread_pct) as max_spread "
            "FROM rates WHERE timestamp >= datetime('now', '-7 days') "
            "GROUP BY day ORDER BY day ASC"
        ).fetchall()
        alert_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM alerts WHERE timestamp >= datetime('now', '-7 days')"
        ).fetchone()["cnt"]
    return [dict(r) for r in rates], alert_count
