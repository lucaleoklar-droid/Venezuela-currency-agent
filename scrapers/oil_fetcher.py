"""Brent crude oil price fetcher — FRED series DCOILBRENTEU.

Stage 3 operational signal. Pulls daily Brent prices from the St Louis Fed's
FRED API and upserts them into the `oil_prices` table. Idempotent: re-fetching
the same date is a no-op (UNIQUE on observation_date + ON CONFLICT upsert).

Two modes:
  - fetch_recent(): default daily job — pulls last ~14 days, upserts new rows.
  - backfill(): one-shot call to populate full history (first deployment only).

Requires FRED_API_KEY in env. Free key, instant signup at
https://fredaccount.stlouisfed.org/apikeys.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta

import requests

from db.db import upsert_oil_price, get_latest_oil_price

logger = logging.getLogger(__name__)

FRED_SERIES = "DCOILBRENTEU"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_SOURCE_TAG = f"fred:{FRED_SERIES}"
RECENT_WINDOW_DAYS = 14
REQUEST_TIMEOUT = 20


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _fred_call(observation_start: str | None = None) -> list[dict] | None:
    """Hit FRED. Returns list of observations or None on failure."""
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        logger.error("FRED_API_KEY not set — cannot fetch Brent prices")
        return None

    params = {
        "series_id": FRED_SERIES,
        "api_key": api_key,
        "file_type": "json",
    }
    if observation_start:
        params["observation_start"] = observation_start

    try:
        resp = requests.get(FRED_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"FRED request failed: {e}")
        return None

    try:
        data = resp.json()
    except ValueError as e:
        logger.error(f"FRED returned non-JSON: {e}")
        return None

    obs = data.get("observations")
    if not isinstance(obs, list):
        logger.error(f"FRED response missing observations: {data}")
        return None
    return obs


def _ingest_observations(obs: list[dict]) -> tuple[int, int]:
    """Persist FRED observations. Returns (inserted_or_updated, skipped)."""
    written = 0
    skipped = 0
    fetched_at = _utcnow_iso()
    for o in obs:
        date_str = o.get("date")
        val_str = o.get("value")
        if not date_str or val_str in (None, "", "."):
            # FRED uses "." for missing values (e.g. weekends, holidays)
            skipped += 1
            continue
        try:
            val = float(val_str)
        except (TypeError, ValueError):
            skipped += 1
            continue
        upsert_oil_price(
            observation_date=date_str,
            brent_usd_per_bbl=val,
            source=FRED_SOURCE_TAG,
            fetched_at=fetched_at,
        )
        written += 1
    return written, skipped


def fetch_recent() -> dict:
    """Pull last RECENT_WINDOW_DAYS of Brent observations and upsert.
    Safe to run daily; trivial work on FRED's side."""
    start = (datetime.now(timezone.utc).date() - timedelta(days=RECENT_WINDOW_DAYS)).isoformat()
    obs = _fred_call(observation_start=start)
    if obs is None:
        return {"ok": False, "written": 0, "skipped": 0, "error": "FRED call failed"}
    written, skipped = _ingest_observations(obs)
    latest = get_latest_oil_price()
    if latest:
        logger.info(
            f"Brent fetch: written={written} skipped={skipped} "
            f"latest={latest['observation_date']} "
            f"price=${latest['brent_usd_per_bbl']:.2f}"
        )
    else:
        logger.info(f"Brent fetch: written={written} skipped={skipped} latest=none")
    return {"ok": True, "written": written, "skipped": skipped, "latest": latest}


def backfill(start_date: str = "2024-01-01") -> dict:
    """One-shot full-history fetch. Use once on first deployment.
    Pulls everything from start_date forward."""
    obs = _fred_call(observation_start=start_date)
    if obs is None:
        return {"ok": False, "written": 0, "skipped": 0, "error": "FRED call failed"}
    written, skipped = _ingest_observations(obs)
    logger.info(f"Brent backfill from {start_date}: written={written} skipped={skipped}")
    return {"ok": True, "written": written, "skipped": skipped}


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from dotenv import load_dotenv
    load_dotenv(os.path.join(project_root, ".env"))

    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        start = sys.argv[2] if len(sys.argv) > 2 else "2024-01-01"
        result = backfill(start_date=start)
    else:
        result = fetch_recent()
    print(result)
