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
from analysis.forecasters.stat import StatForecaster
from analysis.forecasters.stat_v2 import StatV2Forecaster
from analysis.forecasters.stat_v3 import StatV3Forecaster
from analysis.forecasters.momentum import MomentumForecaster
from analysis.forecasters.markov import MarkovForecaster
from analysis.forecasters.ensemble import EnsembleForecaster
from analysis.forecasters.enrich import enrich_history
from analysis.forecasters.backtest import score_pending_live

logger = logging.getLogger(__name__)

LOOKBACK_DAYS_FOR_FORECAST = 30
MIN_HISTORY_FOR_FORECAST = 12


def _load_history(lookback_days: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT timestamp, bcv_rate, parallel_rate, spread_pct "
            "FROM rates "
            "WHERE bcv_rate IS NOT NULL AND parallel_rate IS NOT NULL "
            "AND timestamp >= datetime('now', ?) "
            "ORDER BY timestamp ASC",
            (f"-{lookback_days} days",),
        ).fetchall()
        history = [dict(r) for r in rows]
        # Enrich in-process with the same open connection — avoids reopening
        # and gives Stage 3 forecasters (StatV2) the operational signals they need.
        return enrich_history(history, conn=conn)
    finally:
        conn.close()


DEFAULT_FORECASTERS = (
    NaiveForecaster,
    StatForecaster,
    StatV2Forecaster,
    StatV3Forecaster,
    MomentumForecaster,
    MarkovForecaster,
    EnsembleForecaster,
)


def _run_one(forecaster, history: list[dict], made_at: str, target_at: str,
             spread_at_make: float | None, inputs_meta: dict) -> int | None:
    try:
        probs = forecaster.forecast(history)
    except Exception as e:
        logger.exception(f"Forecaster {forecaster.name} crashed: {e}")
        return None
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
        f"widen={probs['widen']:.2%} stable={probs['stable']:.2%} narrow={probs['narrow']:.2%}"
    )
    return fid


def make_daily_forecast(forecasters=None) -> list[int]:
    """Produce one 24h forecast per registered forecaster and persist each.
    Returns the list of new forecast ids (empty if skipped)."""
    forecasters = forecasters or [cls() for cls in DEFAULT_FORECASTERS]
    history = _load_history(LOOKBACK_DAYS_FOR_FORECAST)
    if len(history) < MIN_HISTORY_FOR_FORECAST:
        logger.warning(
            f"Skipping daily forecast: only {len(history)} historical readings "
            f"(need >={MIN_HISTORY_FOR_FORECAST})"
        )
        return []

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    made_at = now.isoformat()
    target_at = (now + timedelta(hours=HORIZON_HOURS)).isoformat()
    spread_at_make = history[-1].get("spread_pct")
    inputs_meta = {
        "n_history": len(history),
        "lookback_days": LOOKBACK_DAYS_FOR_FORECAST,
        "last_reading_ts": history[-1].get("timestamp"),
        "spread_at_make": spread_at_make,
    }
    logger.info(f"Daily forecast cycle (spread now: {spread_at_make}, target: {target_at})")
    ids = []
    for f in forecasters:
        fid = _run_one(f, history, made_at, target_at, spread_at_make, inputs_meta)
        if fid is not None:
            ids.append(fid)
    return ids


def score_due_forecasts() -> int:
    """Score all forecasts whose 24h horizon has passed but haven't been scored yet."""
    n = score_pending_live()
    if n:
        logger.info(f"Scored {n} matured forecast(s)")
    return n
