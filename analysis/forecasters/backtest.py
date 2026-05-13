"""Backtest harness for forecasters.

NO-LEAKAGE GUARANTEE
--------------------
At each replay timestamp t, we slice history to rows whose timestamp <= t and
pass ONLY that slice to the forecaster. The future reading at t+24h is used
exclusively for scoring (via compute_outcome) and is never embedded in the
history slice handed to the model.

6H SAMPLING RATIONALE
---------------------
Rates are scraped every 30 min, so consecutive rows are heavily
autocorrelated. Backtesting every row would inflate n with near-duplicate
forecasts and give a misleadingly precise mean Brier. We greedily walk forward
at least 6 hours between forecasts (~4 forecasts/day) so each scored point
carries roughly independent information.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import (
    HORIZON_HOURS,
    brier_score,
    compute_outcome,
    log_loss,
    validate_probs,
)

logger = logging.getLogger(__name__)

MIN_HISTORY = 12
SAMPLE_GAP_HOURS = 6
TARGET_TOLERANCE_HOURS = 2


def _parse_ts(s: str) -> datetime:
    # SQLite ISO timestamps; tolerate trailing 'Z'
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if "Z" in s else datetime.fromisoformat(s)


def _load_rates(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT timestamp, bcv_rate, parallel_rate, spread_pct, source, notes "
            "FROM rates WHERE bcv_rate IS NOT NULL AND parallel_rate IS NOT NULL "
            "ORDER BY timestamp ASC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _find_nearest(rates: list[dict], target_dt: datetime,
                  parsed_ts: list[datetime], start_idx: int,
                  tolerance: timedelta) -> int | None:
    """Return index of rate row whose timestamp is closest to target_dt within
    tolerance, searching from start_idx forward. None if no row qualifies."""
    best_idx = None
    best_delta = None
    for j in range(start_idx, len(rates)):
        delta = abs(parsed_ts[j] - target_dt)
        if delta > tolerance and parsed_ts[j] > target_dt and best_idx is not None:
            # Walked past target by more than tolerance; can't improve
            break
        if delta <= tolerance:
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_idx = j
        elif parsed_ts[j] > target_dt + tolerance:
            break
    return best_idx


def backtest(forecaster, db_path: str | None = None, persist: bool = False) -> dict:
    """Replay historical rates and score the forecaster.

    Args:
        forecaster: object with `.name` and `.forecast(history)` -> Probs
        db_path: override DB path (defaults to db.db.DB_PATH)
        persist: if True, write each forecast and score to the DB tables

    Returns summary dict (see module docstring for shape).
    """
    if db_path is None:
        from db.db import DB_PATH as _DEFAULT
        db_path = _DEFAULT

    rates = _load_rates(db_path)
    logger.info("Loaded %d rate rows from %s", len(rates), db_path)

    if len(rates) < MIN_HISTORY + 2:
        logger.warning("Insufficient rate history (%d rows) — need >= %d", len(rates), MIN_HISTORY + 2)
        return {
            "forecaster": getattr(forecaster, "name", "unknown"),
            "n": 0,
            "mean_brier": float("nan"),
            "mean_log_loss": None,
            "outcome_distribution": {"widen": 0, "stable": 0, "narrow": 0},
            "naive_uniform_brier": 2 / 3,
            "details": [],
        }

    parsed_ts = [_parse_ts(r["timestamp"]) for r in rates]
    horizon = timedelta(hours=HORIZON_HOURS)
    gap = timedelta(hours=SAMPLE_GAP_HOURS)
    tol = timedelta(hours=TARGET_TOLERANCE_HOURS)

    details: list[dict] = []
    outcome_counts = {"widen": 0, "stable": 0, "narrow": 0}
    log_losses: list[float] = []

    last_forecast_dt: datetime | None = None

    for i in range(MIN_HISTORY - 1, len(rates)):
        t_dt = parsed_ts[i]
        if last_forecast_dt is not None and (t_dt - last_forecast_dt) < gap:
            continue

        target_dt = t_dt + horizon
        target_idx = _find_nearest(rates, target_dt, parsed_ts, i + 1, tol)
        if target_idx is None:
            continue

        history = rates[: i + 1]
        spread_at_make = rates[i]["spread_pct"]
        spread_at_target = rates[target_idx]["spread_pct"]
        if spread_at_make is None or spread_at_target is None:
            continue

        try:
            raw_probs = forecaster.forecast(history)
        except Exception as e:
            logger.exception("Forecaster %s raised on t=%s: %s",
                             getattr(forecaster, "name", "?"), rates[i]["timestamp"], e)
            continue

        probs = validate_probs(raw_probs)
        outcome = compute_outcome(spread_at_make, spread_at_target)
        b = brier_score(probs, outcome)
        ll = log_loss(probs, outcome)

        outcome_counts[outcome] += 1
        if ll is not None:
            log_losses.append(ll)

        delta_pp = spread_at_target - spread_at_make
        record = {
            "made_at": rates[i]["timestamp"],
            "target_at": rates[target_idx]["timestamp"],
            "spread_at_make": spread_at_make,
            "spread_at_target": spread_at_target,
            "delta_pp": delta_pp,
            "probs": probs,
            "actual_outcome": outcome,
            "brier": b,
            "log_loss": ll,
        }
        details.append(record)
        last_forecast_dt = t_dt

        if persist:
            from db.db import insert_forecast, insert_forecast_score
            fid = insert_forecast(
                made_at=rates[i]["timestamp"],
                target_at=(t_dt + horizon).isoformat(),
                horizon_hours=HORIZON_HOURS,
                model_name=forecaster.name,
                p_widen=probs["widen"],
                p_stable=probs["stable"],
                p_narrow=probs["narrow"],
                spread_at_make=spread_at_make,
                inputs_json=json.dumps({"history_len": len(history)}),
                raw_output=json.dumps(raw_probs) if isinstance(raw_probs, dict) else str(raw_probs),
            )
            insert_forecast_score(
                forecast_id=fid,
                scored_at=datetime.utcnow().isoformat(),
                spread_at_target=spread_at_target,
                delta_pp=delta_pp,
                actual_outcome=outcome,
                brier=b,
                log_loss=ll,
            )

    n = len(details)
    mean_brier = sum(d["brier"] for d in details) / n if n else float("nan")
    mean_ll = (sum(log_losses) / len(log_losses)) if log_losses else None

    logger.info("Backtest %s: n=%d, mean_brier=%.4f", forecaster.name, n, mean_brier if n else -1)

    return {
        "forecaster": forecaster.name,
        "n": n,
        "mean_brier": mean_brier,
        "mean_log_loss": mean_ll,
        "outcome_distribution": outcome_counts,
        "naive_uniform_brier": 2 / 3,
        "details": details,
    }


def score_pending_live(now: datetime | None = None) -> int:
    """Score any matured-but-unscored forecasts in the live DB.

    Returns count of forecasts scored this run.
    """
    from db.db import (
        DB_PATH,
        get_unscored_matured_forecasts,
        insert_forecast_score,
    )

    if now is None:
        now = datetime.utcnow()
    now_iso = now.isoformat()

    pending = get_unscored_matured_forecasts(now_iso)
    if not pending:
        return 0

    rates = _load_rates(DB_PATH)
    if not rates:
        logger.warning("No rates available to score %d pending forecasts", len(pending))
        return 0

    parsed_ts = [_parse_ts(r["timestamp"]) for r in rates]
    tol = timedelta(hours=TARGET_TOLERANCE_HOURS)
    scored = 0

    for f in pending:
        target_dt = _parse_ts(f["target_at"])
        idx = _find_nearest(rates, target_dt, parsed_ts, 0, tol)
        if idx is None:
            # If forecast is severely overdue (>24h past target) with no data, log and skip.
            if now - target_dt > timedelta(hours=24):
                logger.warning(
                    "Forecast id=%s target_at=%s has no rate data within tolerance; "
                    "leaving unscored (>24h overdue).",
                    f.get("id"), f["target_at"],
                )
            continue

        spread_target = rates[idx]["spread_pct"]
        spread_make = f.get("spread_at_make")
        if spread_target is None or spread_make is None:
            continue

        probs = {
            "widen": f["p_widen"],
            "stable": f["p_stable"],
            "narrow": f["p_narrow"],
        }
        outcome = compute_outcome(spread_make, spread_target)
        b = brier_score(probs, outcome)
        ll = log_loss(probs, outcome)
        delta_pp = spread_target - spread_make

        insert_forecast_score(
            forecast_id=f["id"],
            scored_at=now_iso,
            spread_at_target=spread_target,
            delta_pp=delta_pp,
            actual_outcome=outcome,
            brier=b,
            log_loss=ll,
        )
        scored += 1

    logger.info("Scored %d/%d pending forecasts", scored, len(pending))
    return scored


def _print_summary(summary: dict) -> None:
    print(f"=== Backtest: {summary['forecaster']} ===")
    print(f"N forecasts:           {summary['n']}")
    if summary["n"] == 0:
        print("(no forecasts produced — insufficient data)")
        return
    print(f"Mean Brier:            {summary['mean_brier']:.4f}")
    print(f"Naive-uniform Brier:   {summary['naive_uniform_brier']:.4f}")
    if summary["mean_log_loss"] is not None:
        print(f"Mean log-loss:         {summary['mean_log_loss']:.4f}")
    else:
        print("Mean log-loss:         n/a (model emitted zero prob on actual)")
    od = summary["outcome_distribution"]
    total = sum(od.values()) or 1
    print("Outcome distribution:")
    for k in ("widen", "stable", "narrow"):
        print(f"  {k:7s} {od[k]:4d}  ({100 * od[k] / total:5.1f}%)")


if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    railway_db = os.path.join(project_root, "backups", "venezuela_currency_railway.db")

    try:
        from analysis.forecasters.naive import NaiveForecaster
    except ImportError:
        print("Naive forecaster module isn't ready yet — skipping CLI backtest.")
        print("(analysis.forecasters.naive not importable; the sibling agent may still be building it.)")
        sys.exit(0)

    if not os.path.exists(railway_db):
        print(f"Railway DB not found at {railway_db} — skipping CLI backtest.")
        sys.exit(0)

    summary = backtest(NaiveForecaster(), db_path=railway_db, persist=False)
    _print_summary(summary)
