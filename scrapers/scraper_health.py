import logging
from datetime import datetime, timezone
from db.db import get_last_bcv_update, get_latest_rate

logger = logging.getLogger(__name__)

BCV_STALE_HOURS = 48  # BCV doesn't update on weekends


def hours_since(timestamp_str: str) -> float:
    if not timestamp_str:
        return float("inf")
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - ts).total_seconds() / 3600
    except Exception:
        return float("inf")


def check_bcv_freshness() -> dict:
    last_update = get_last_bcv_update()
    hours = hours_since(last_update)
    stale = hours > BCV_STALE_HOURS
    return {
        "source": "bcv.org.ve",
        "last_update": last_update,
        "hours_since_update": round(hours, 1),
        "stale": stale,
        "message": f"BCV data is {round(hours, 1)}h old. Using parallel as reference." if stale else None,
    }


def check_data_freshness() -> dict:
    latest = get_latest_rate()
    if not latest:
        return {"ok": False, "message": "No data in database at all"}
    hours = hours_since(latest["timestamp"])
    ok = hours < 2  # Should have a reading within last 2 hours
    return {
        "ok": ok,
        "last_reading": latest["timestamp"],
        "hours_since": round(hours, 1),
        "message": f"Last reading {round(hours, 1)}h ago — scraper may be down" if not ok else None,
    }


def get_health_report() -> dict:
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "data_freshness": check_data_freshness(),
        "bcv_freshness": check_bcv_freshness(),
    }
