import sqlite3
import os
from datetime import datetime, date
from contextlib import contextmanager

_data_dir = os.getenv("DATA_DIR", os.path.dirname(os.path.dirname(__file__)))
os.makedirs(_data_dir, exist_ok=True)
DB_PATH = os.path.join(_data_dir, "venezuela_currency.db")


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
                delivered INTEGER DEFAULT 0,
                bcv_rate REAL,
                parallel_rate REAL,
                spread_pct REAL
            );

            CREATE TABLE IF NOT EXISTS daily_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                claude_summary TEXT,
                recommendation TEXT,
                urgency_level TEXT,
                raw_response TEXT
            );

            CREATE TABLE IF NOT EXISTS alert_cooldowns (
                alert_type TEXT PRIMARY KEY,
                last_sent TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS forecasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                made_at TEXT NOT NULL,           -- UTC ISO when forecast was produced
                target_at TEXT NOT NULL,         -- made_at + horizon_hours
                horizon_hours INTEGER NOT NULL,
                model_name TEXT NOT NULL,        -- 'naive', 'stat', 'claude', etc
                p_widen REAL NOT NULL,
                p_stable REAL NOT NULL,
                p_narrow REAL NOT NULL,
                spread_at_make REAL,             -- spread at made_at (so scoring knows the baseline)
                inputs_json TEXT,                -- raw feature snapshot
                raw_output TEXT                  -- whatever the model emitted
            );

            CREATE TABLE IF NOT EXISTS forecast_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                forecast_id INTEGER NOT NULL UNIQUE,
                scored_at TEXT NOT NULL,
                spread_at_target REAL,           -- actual spread at target_at (or nearest reading)
                delta_pp REAL,                   -- spread_at_target - spread_at_make
                actual_outcome TEXT NOT NULL,    -- 'widen' | 'stable' | 'narrow'
                brier REAL NOT NULL,             -- Brier score (multiclass)
                log_loss REAL,                   -- optional, NULL if any prob is 0
                FOREIGN KEY (forecast_id) REFERENCES forecasts(id)
            );

            CREATE INDEX IF NOT EXISTS idx_forecasts_target ON forecasts(target_at);
            CREATE INDEX IF NOT EXISTS idx_forecasts_model ON forecasts(model_name);

            CREATE TABLE IF NOT EXISTS daily_brief_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                action_signal TEXT NOT NULL,  -- CONVERTIR | ESPERAR | NEUTRAL
                brief_text TEXT,
                bcv_rate REAL,
                parallel_rate REAL,
                spread_pct REAL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                chat_id TEXT,
                action TEXT NOT NULL,         -- 'converted' | 'waited'
                amount_ves REAL,              -- nullable; only set for 'converted'
                rate_at_action REAL,          -- parallel rate at the time
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS oil_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_date TEXT NOT NULL UNIQUE,   -- ISO date YYYY-MM-DD (source-defined)
                brent_usd_per_bbl REAL NOT NULL,
                source TEXT NOT NULL,                    -- e.g. 'fred:DCOILBRENTEU'
                fetched_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS news_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                published_at TEXT NOT NULL,              -- article publication time (ISO)
                source TEXT NOT NULL,                    -- 'efectococuyo' | 'bancaynegocios' | 'talcualdigital'
                url TEXT NOT NULL UNIQUE,                -- dedup key
                title TEXT NOT NULL,
                matched_keywords TEXT NOT NULL,          -- JSON list of which keywords matched
                fetched_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS claude_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,                        -- ISO UTC of the API call
                prompt_type TEXT NOT NULL,               -- 'core_analysis' | 'daily_brief' | 'spike_alert' | 'weekly' | other
                model TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_creation_tokens INTEGER,
                latency_ms INTEGER,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS p2p_rates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                asset TEXT NOT NULL,
                fiat TEXT NOT NULL,
                best_bid REAL,
                best_ask REAL,
                mid_price REAL,
                bid_ask_spread_pct REAL,
                n_bid_ads INTEGER,
                n_ask_ads INTEGER,
                source TEXT NOT NULL,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_rates_timestamp ON rates(timestamp);
            CREATE INDEX IF NOT EXISTS idx_alerts_delivered ON alerts(delivered);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_date ON daily_analysis(date);
            CREATE INDEX IF NOT EXISTS idx_oil_date ON oil_prices(observation_date);
            CREATE INDEX IF NOT EXISTS idx_news_published ON news_signals(published_at);
            CREATE INDEX IF NOT EXISTS idx_claude_calls_ts ON claude_calls(ts);
            CREATE INDEX IF NOT EXISTS idx_p2p_rates_timestamp ON p2p_rates(timestamp);
        """)

        # Migrations for legacy databases — add columns that may be missing
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()]
        for col in ["bcv_rate", "parallel_rate", "spread_pct"]:
            if col not in cols:
                conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} REAL")

        # forecasts.abandoned: ISO ts when a forecast was permanently given up on
        # (target had no scorable reading). NULL = still in the scoring queue.
        fcols = [r["name"] for r in conn.execute("PRAGMA table_info(forecasts)").fetchall()]
        if "abandoned" not in fcols:
            conn.execute("ALTER TABLE forecasts ADD COLUMN abandoned TEXT")


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
            "SELECT * FROM rates WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?) ORDER BY timestamp DESC",
            (f"-{hours} hours",)
        ).fetchall()
    return [dict(r) for r in rows]


def get_rates_last_n_days(days: int):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM rates WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?) ORDER BY timestamp ASC",
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
            "WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?) AND spread_pct IS NOT NULL",
            (f"-{days} days",)
        ).fetchone()
    return row["avg"] if row and row["avg"] is not None else None


def get_last_bcv_update():
    with db() as conn:
        row = conn.execute(
            "SELECT timestamp FROM rates WHERE bcv_rate IS NOT NULL ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    return row["timestamp"] if row else None


def insert_alert(timestamp: str, alert_type: str, message: str,
                 bcv_rate=None, parallel_rate=None, spread_pct=None):
    with db() as conn:
        conn.execute(
            "INSERT INTO alerts (timestamp, alert_type, message, bcv_rate, parallel_rate, spread_pct) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (timestamp, alert_type, message, bcv_rate, parallel_rate, spread_pct)
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


def insert_forecast(made_at: str, target_at: str, horizon_hours: int, model_name: str,
                    p_widen: float, p_stable: float, p_narrow: float,
                    spread_at_make: float | None, inputs_json: str | None,
                    raw_output: str | None) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO forecasts (made_at, target_at, horizon_hours, model_name, "
            "p_widen, p_stable, p_narrow, spread_at_make, inputs_json, raw_output) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (made_at, target_at, horizon_hours, model_name,
             p_widen, p_stable, p_narrow, spread_at_make, inputs_json, raw_output)
        )
        return cur.lastrowid


def insert_forecast_score(forecast_id: int, scored_at: str, spread_at_target: float | None,
                          delta_pp: float | None, actual_outcome: str,
                          brier: float, log_loss: float | None):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO forecast_scores "
            "(forecast_id, scored_at, spread_at_target, delta_pp, actual_outcome, brier, log_loss) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (forecast_id, scored_at, spread_at_target, delta_pp, actual_outcome, brier, log_loss)
        )


def get_unscored_matured_forecasts(now_iso: str) -> list[dict]:
    """Forecasts whose target_at has passed, not yet scored, and not abandoned."""
    with db() as conn:
        rows = conn.execute(
            "SELECT f.* FROM forecasts f "
            "LEFT JOIN forecast_scores s ON s.forecast_id = f.id "
            "WHERE s.id IS NULL AND f.abandoned IS NULL AND f.target_at <= ? "
            "ORDER BY f.target_at ASC",
            (now_iso,)
        ).fetchall()
    return [dict(r) for r in rows]


def mark_forecast_abandoned(forecast_id: int, when_iso: str):
    """Permanently retire a forecast that can never be scored (no target data).
    Keeps it out of the scoring sweep without polluting Brier stats."""
    with db() as conn:
        conn.execute(
            "UPDATE forecasts SET abandoned = ? WHERE id = ?",
            (when_iso, forecast_id)
        )


def get_forecast_scores(model_name: str) -> list[dict]:
    """Per-forecast scored rows for one model — for CI, significance, calibration."""
    with db() as conn:
        rows = conn.execute(
            "SELECT f.p_widen, f.p_stable, f.p_narrow, s.brier, s.actual_outcome "
            "FROM forecast_scores s JOIN forecasts f ON f.id = s.forecast_id "
            "WHERE f.model_name = ? ORDER BY s.scored_at ASC",
            (model_name,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_forecast_brier_summary(model_name: str | None = None) -> dict:
    """Aggregate Brier stats. Pass model_name to filter to one model."""
    with db() as conn:
        if model_name:
            row = conn.execute(
                "SELECT COUNT(*) as n, AVG(brier) as mean_brier, "
                "AVG(log_loss) as mean_log_loss "
                "FROM forecast_scores s JOIN forecasts f ON f.id = s.forecast_id "
                "WHERE f.model_name = ?",
                (model_name,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT f.model_name, COUNT(*) as n, AVG(s.brier) as mean_brier, "
                "AVG(s.log_loss) as mean_log_loss "
                "FROM forecast_scores s JOIN forecasts f ON f.id = s.forecast_id "
                "GROUP BY f.model_name"
            ).fetchall()
            return [dict(r) for r in row]
    return dict(row) if row else {}


def upsert_daily_brief_action(date_str: str, action_signal: str, brief_text: str,
                              bcv_rate: float | None, parallel_rate: float | None,
                              spread_pct: float | None):
    with db() as conn:
        conn.execute(
            "INSERT INTO daily_brief_actions "
            "(date, action_signal, brief_text, bcv_rate, parallel_rate, spread_pct, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "action_signal=excluded.action_signal, brief_text=excluded.brief_text, "
            "bcv_rate=excluded.bcv_rate, parallel_rate=excluded.parallel_rate, "
            "spread_pct=excluded.spread_pct, created_at=excluded.created_at",
            (date_str, action_signal, brief_text, bcv_rate, parallel_rate, spread_pct,
             datetime.now().isoformat())
        )


def log_user_action(chat_id: str, action: str, amount_ves: float | None,
                    rate_at_action: float | None, note: str | None = None):
    with db() as conn:
        conn.execute(
            "INSERT INTO user_actions (timestamp, chat_id, action, amount_ves, rate_at_action, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), chat_id, action, amount_ves, rate_at_action, note)
        )


def get_user_actions_since(iso_timestamp: str) -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM user_actions WHERE timestamp >= ? ORDER BY timestamp ASC",
            (iso_timestamp,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_cooldown(alert_type: str) -> str | None:
    with db() as conn:
        row = conn.execute(
            "SELECT last_sent FROM alert_cooldowns WHERE alert_type = ?",
            (alert_type,)
        ).fetchone()
    return row["last_sent"] if row else None


def set_cooldown(alert_type: str, last_sent: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO alert_cooldowns (alert_type, last_sent) VALUES (?, ?) "
            "ON CONFLICT(alert_type) DO UPDATE SET last_sent = excluded.last_sent",
            (alert_type, last_sent)
        )


def get_all_cooldowns() -> dict:
    with db() as conn:
        rows = conn.execute("SELECT alert_type, last_sent FROM alert_cooldowns").fetchall()
    return {r["alert_type"]: r["last_sent"] for r in rows}


def upsert_oil_price(observation_date: str, brent_usd_per_bbl: float,
                     source: str, fetched_at: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO oil_prices (observation_date, brent_usd_per_bbl, source, fetched_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(observation_date) DO UPDATE SET "
            "brent_usd_per_bbl=excluded.brent_usd_per_bbl, "
            "source=excluded.source, fetched_at=excluded.fetched_at",
            (observation_date, brent_usd_per_bbl, source, fetched_at)
        )


def get_latest_oil_price() -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT observation_date, brent_usd_per_bbl, source, fetched_at "
            "FROM oil_prices ORDER BY observation_date DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_oil_price_on_or_before(target_date: str) -> dict | None:
    """Most recent oil reading whose observation_date <= target_date.
    target_date is an ISO YYYY-MM-DD string. Returns None if none available."""
    with db() as conn:
        row = conn.execute(
            "SELECT observation_date, brent_usd_per_bbl "
            "FROM oil_prices WHERE observation_date <= ? "
            "ORDER BY observation_date DESC LIMIT 1",
            (target_date,)
        ).fetchone()
    return dict(row) if row else None


def insert_news_signal(published_at: str, source: str, url: str, title: str,
                       matched_keywords_json: str, fetched_at: str) -> bool:
    """Insert a news signal. Returns True if inserted, False if URL already known."""
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO news_signals "
                "(published_at, source, url, title, matched_keywords, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (published_at, source, url, title, matched_keywords_json, fetched_at)
            )
            return True
        except sqlite3.IntegrityError:
            return False


def get_news_count_between(start_iso: str, end_iso: str) -> int:
    """Count of news signals with published_at in [start_iso, end_iso)."""
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM news_signals "
            "WHERE published_at >= ? AND published_at < ?",
            (start_iso, end_iso)
        ).fetchone()
    return int(row["n"]) if row else 0


def get_weekly_data():
    with db() as conn:
        rates = conn.execute(
            "SELECT date(timestamp) as day, "
            "AVG(bcv_rate) as avg_bcv, AVG(parallel_rate) as avg_parallel, "
            "AVG(spread_pct) as avg_spread, MIN(spread_pct) as min_spread, MAX(spread_pct) as max_spread "
            "FROM rates WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days') "
            "GROUP BY day ORDER BY day ASC"
        ).fetchall()
        alert_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM alerts WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days')"
        ).fetchone()["cnt"]
    return [dict(r) for r in rates], alert_count


def log_claude_call(prompt_type: str, model: str, input_tokens: int | None,
                    output_tokens: int | None, cache_read_tokens: int | None,
                    cache_creation_tokens: int | None, latency_ms: int | None,
                    error: str | None = None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO claude_calls (ts, prompt_type, model, input_tokens, "
            "output_tokens, cache_read_tokens, cache_creation_tokens, latency_ms, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), prompt_type, model, input_tokens,
             output_tokens, cache_read_tokens, cache_creation_tokens, latency_ms, error)
        )


def insert_p2p_rate(timestamp: str, asset: str, fiat: str, best_bid: float | None,
                    best_ask: float | None, mid_price: float | None,
                    bid_ask_spread_pct: float | None, n_bid_ads: int,
                    n_ask_ads: int, source: str, error: str | None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO p2p_rates (timestamp, asset, fiat, best_bid, best_ask, "
            "mid_price, bid_ask_spread_pct, n_bid_ads, n_ask_ads, source, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (timestamp, asset, fiat, best_bid, best_ask, mid_price,
             bid_ask_spread_pct, n_bid_ads, n_ask_ads, source, error)
        )


def get_latest_p2p_rate() -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM p2p_rates WHERE mid_price IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_latest_forecast(model_name: str) -> dict | None:
    """Most recent forecast row for a model. Used by the synthesizer prompt."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM forecasts WHERE model_name = ? "
            "ORDER BY made_at DESC LIMIT 1",
            (model_name,)
        ).fetchone()
    return dict(row) if row else None


def get_claude_usage_summary(days: int = 7) -> dict:
    """Token + call counts over the last N days. Cheap summary for /estado."""
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as n, "
            "SUM(COALESCE(input_tokens,0)) as in_tk, "
            "SUM(COALESCE(output_tokens,0)) as out_tk, "
            "SUM(COALESCE(cache_read_tokens,0)) as cache_read, "
            "SUM(COALESCE(cache_creation_tokens,0)) as cache_create, "
            "SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as errors "
            "FROM claude_calls WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)",
            (f"-{days} days",)
        ).fetchone()
    return dict(row) if row else {}
