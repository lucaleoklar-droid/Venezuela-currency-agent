"""History enrichment for Stage 3 features.

Takes a list of rate-reading dicts (timestamp/bcv_rate/parallel_rate/spread_pct)
and adds two operational-signal keys to each row:

  - brent_usd_per_bbl  : most recent Brent observation on-or-before this row's date
  - news_count_7d      : count of news_signals with published_at in [t-7d, t)

The enricher is the join layer between `rates` and the Stage 3 side tables —
keeping it out of the forecaster modules so each forecaster only sees a single
"history" list with all features it needs.

Caching the side-table data and using bisect makes this O((R + O + N) log) per
call instead of O(R * O * N) — relevant for the backtest which sweeps thousands
of points across hundreds of oil rows.
"""
from __future__ import annotations

import bisect
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Iterable

logger = logging.getLogger(__name__)

NEWS_LOOKBACK = timedelta(days=7)
BRENT_LOOKBACK = timedelta(days=7)


def _parse_ts(s):
    if isinstance(s, datetime):
        return s
    # Tolerate trailing 'Z'
    if isinstance(s, str) and s.endswith("Z"):
        return datetime.fromisoformat(s[:-1])
    return datetime.fromisoformat(s)


def _date_of(ts) -> str:
    """ISO YYYY-MM-DD for a timestamp value."""
    return _parse_ts(ts).date().isoformat()


def _load_oil(conn) -> list[tuple[str, float]]:
    """All oil observations, ascending by date.
    Returns [] if the table doesn't exist (e.g. backtest against an old snapshot)."""
    try:
        rows = conn.execute(
            "SELECT observation_date, brent_usd_per_bbl FROM oil_prices "
            "ORDER BY observation_date ASC"
        ).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            logger.debug("oil_prices table not present — enrich proceeds with brent=None")
            return []
        raise
    return [(r[0], float(r[1])) for r in rows]


def _load_news_timestamps(conn) -> list[str]:
    """All news_signal published_at values, ascending.
    Returns [] if the table doesn't exist."""
    try:
        rows = conn.execute(
            "SELECT published_at FROM news_signals ORDER BY published_at ASC"
        ).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            logger.debug("news_signals table not present — enrich proceeds with news_count_7d=0")
            return []
        raise
    return [r[0] for r in rows]


def _oil_on_or_before(oil_rows: list[tuple[str, float]], target_date: str) -> float | None:
    """Latest oil price with observation_date <= target_date."""
    if not oil_rows:
        return None
    # bisect on (date,) by date-string — ISO dates sort lexically
    dates = [d for d, _ in oil_rows]
    idx = bisect.bisect_right(dates, target_date) - 1
    if idx < 0:
        return None
    return oil_rows[idx][1]


def _news_count_window(news_ts: list[str], start_iso: str, end_iso: str) -> int:
    """Count of news_signals with published_at in [start_iso, end_iso)."""
    if not news_ts:
        return 0
    lo = bisect.bisect_left(news_ts, start_iso)
    hi = bisect.bisect_left(news_ts, end_iso)
    return hi - lo


def enrich_history(history: list[dict], conn: sqlite3.Connection | None = None) -> list[dict]:
    """Return a new list of rows, each augmented with brent_usd_per_bbl and
    news_count_7d. Original rows are not mutated. Missing oil → None (don't
    fabricate); missing news → 0 (count of nothing is zero).

    Pass `conn` to share an open connection (avoids reopening for batch use);
    otherwise this opens its own short-lived connection.
    """
    if not history:
        return []

    close_after = False
    if conn is None:
        from db.db import get_connection
        conn = get_connection()
        close_after = True

    try:
        oil_rows = _load_oil(conn)
        news_ts = _load_news_timestamps(conn)
    finally:
        if close_after:
            conn.close()

    defaults = {
        "brent_usd_per_bbl": None,
        "brent_usd_per_bbl_7d_ago": None,
        "news_count_7d": 0,
    }

    enriched: list[dict] = []
    for r in history:
        ts = r.get("timestamp")
        if ts is None:
            enriched.append({**r, **defaults})
            continue

        try:
            t = _parse_ts(ts)
        except (ValueError, TypeError):
            enriched.append({**r, **defaults})
            continue

        date_str = t.date().isoformat()
        date_7d_str = (t - BRENT_LOOKBACK).date().isoformat()
        brent_now = _oil_on_or_before(oil_rows, date_str)
        brent_prev = _oil_on_or_before(oil_rows, date_7d_str)

        start_iso = (t - NEWS_LOOKBACK).isoformat()
        end_iso = t.isoformat()
        news_n = _news_count_window(news_ts, start_iso, end_iso)

        enriched.append({
            **r,
            "brent_usd_per_bbl": brent_now,
            "brent_usd_per_bbl_7d_ago": brent_prev,
            "news_count_7d": news_n,
        })

    return enriched
