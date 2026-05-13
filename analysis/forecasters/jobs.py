"""Scheduled forecaster jobs — wired into main.py's scheduler.

Two jobs, both safe to run repeatedly:
  - make_daily_forecast(): produce one 24h-ahead forecast per call, log it.
  - score_due_forecasts(): scoring sweep over matured forecasts (delegates).
"""
import json
import logging
from datetime import datetime, timezone, timedelta
from db.db import get_connection, insert_forecast
from analysis.forecasters import HORIZON_HOURS
from analysis.forecasters.naive import NaiveForecaster
from analysis.forecasters.backtest import score_pending_live

logger = logging.getLogger(__name__)

LOOKBACK_DAYS_FOR_FORECAST = 30
MIN_HISTORY_FOR_FORECAST = 12


def _load_history(lookback_days: int) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT timestamp, bcv_rate, parallel_rate, spread_pct "
        "FROM rates "
        "WHERE bcv_rate IS NOT NULL AND parallel_rate IS NOT NULL "
        "AND timestamp >= datetime('now', ?) "
        "ORDER BY timestamp ASC",
        (f"-{lookback_days} days",),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def make_daily_forecast(forecaster=None) -> int | None:
    """Produce a 24h forecast and persist to the forecasts table.
    Returns the new forecast id, or None if skipped (insufficient history)."""
    forecaster = forecaster or NaiveForecaster()
    history = _load_history(LOOKBACK_DAYS_FOR_FORECAST)
    if len(history) < MIN_HISTORY_FOR_FORECAST:
        logger.warning(
            f"Skipping daily forecast: only {len(history)} historical readings "
            f"(need ≥{MIN_HISTORY_FOR_FORECAST})"
        )
        return None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    made_at = now.isoformat()
    target_at = (now + timedelta(hours=HORIZON_HOURS)).isoformat()
    spread_at_make = history[-1].get("spread_pct")

    probs = forecaster.forecast(history)
    inputs_meta = {
        "n_history": len(history),
        "lookback_days": LOOKBACK_DAYS_FOR_FORECAST,
        "last_reading_ts": history[-1].get("timestamp"),
    }
    fid = insert_forecast(
        made_at=made_at,
        target_at=target_at,
        horizon_hours=HORIZON_HOURS,
        model_name=forecaster.name,
        p_widen=probs["widen"],
        p_stable=probs["stable"],
        p_narrow=probs["narrow"],
        spread_at_make=spread_at_make,
        inputs_json=json.dumps(inputs_meta),
        raw_output=json.dumps(probs),
    )
    logger.info(
        f"Forecast #{fid} ({forecaster.name}): "
        f"widen={probs['widen']:.2%} stable={probs['stable']:.2%} narrow={probs['narrow']:.2%} "
        f"(spread now: {spread_at_make}, target: {target_at})"
    )
    return fid


def score_due_forecasts() -> int:
    """Score all forecasts whose 24h horizon has passed but haven't been scored yet."""
    n = score_pending_live()
    if n:
        logger.info(f"Scored {n} matured forecast(s)")
    return n
