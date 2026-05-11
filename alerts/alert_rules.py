import logging
import json
import os
from datetime import datetime
from db.db import insert_alert, get_undelivered_alerts, mark_alert_delivered
from analysis.analyzer import check_spike_alerts, build_spike_message
from alerts.telegram_bot import send_alert
from scrapers.scraper_health import check_bcv_freshness

logger = logging.getLogger(__name__)

# Cooldown keyed by alert_type (SPIKE, CRITICAL, etc) — matches what's stored in DB
ALERT_COOLDOWN_HOURS = {
    "SPIKE": 6,
    "OPPORTUNITY": 4,
    "CRITICAL": 4,
    "WARNING": 24,
    "MOMENTUM": 24,
    "STALE": 24,
}

# Persist cooldown state to disk so Railway restarts don't reset it
_COOLDOWN_FILE = os.path.join(
    os.getenv("DATA_DIR", os.path.dirname(os.path.dirname(__file__))),
    "cooldowns.json"
)


def _load_cooldowns() -> dict:
    try:
        if os.path.exists(_COOLDOWN_FILE):
            with open(_COOLDOWN_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_cooldowns(cooldowns: dict):
    try:
        with open(_COOLDOWN_FILE, "w") as f:
            json.dump(cooldowns, f)
    except Exception as e:
        logger.warning(f"Could not save cooldowns: {e}")


def _is_on_cooldown(alert_type: str) -> bool:
    cooldowns = _load_cooldowns()
    if alert_type not in cooldowns:
        return False
    cooldown_h = ALERT_COOLDOWN_HOURS.get(alert_type, 12)
    last_sent = datetime.fromisoformat(cooldowns[alert_type])
    elapsed = (datetime.utcnow() - last_sent).total_seconds() / 3600
    return elapsed < cooldown_h


def _mark_cooldown(alert_type: str):
    cooldowns = _load_cooldowns()
    cooldowns[alert_type] = datetime.utcnow().isoformat()
    _save_cooldowns(cooldowns)


def process_alerts():
    """Check for alert conditions, generate messages via Claude, queue and deliver."""
    alerts = check_spike_alerts()

    # Check BCV staleness
    bcv_health = check_bcv_freshness()
    if bcv_health["stale"]:
        alerts.append({
            "type": "bcv_stale",
            "detail": bcv_health["message"],
            "bcv_rate": None, "parallel_rate": None, "spread_pct": None,
            "alert_type": "STALE",
        })

    for alert in alerts:
        alert_type = alert["alert_type"]  # SPIKE, CRITICAL, etc
        if _is_on_cooldown(alert_type):
            logger.debug(f"Alert {alert_type} is on cooldown, skipping")
            continue

        try:
            message = build_spike_message(alert)
            timestamp = datetime.utcnow().isoformat()
            insert_alert(timestamp, alert_type, message)
            logger.info(f"Alert queued: {alert_type}")
        except Exception as e:
            logger.error(f"Failed to build alert message for {alert_type}: {e}")

    deliver_queued_alerts()


def deliver_queued_alerts():
    """Send any undelivered alerts via Telegram."""
    from db.db import get_connection
    pending = get_undelivered_alerts()
    for alert in pending:
        # Fetch rate data from the DB row if available
        conn = get_connection()
        row = conn.execute(
            "SELECT r.bcv_rate, r.parallel_rate, r.spread_pct "
            "FROM rates r ORDER BY r.timestamp DESC LIMIT 1"
        ).fetchone()
        conn.close()
        bcv = row["bcv_rate"] if row else None
        parallel = row["parallel_rate"] if row else None
        spread = row["spread_pct"] if row else None

        success = send_alert(alert["alert_type"], alert["message"], bcv, parallel, spread)
        if success:
            mark_alert_delivered(alert["id"])
            _mark_cooldown(alert["alert_type"])
        else:
            logger.warning(f"Failed to deliver alert id={alert['id']}, will retry")
