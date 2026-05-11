import logging
from datetime import datetime
from db.db import insert_alert, get_undelivered_alerts, mark_alert_delivered
from analysis.analyzer import check_spike_alerts, build_spike_message
from alerts.telegram_bot import send_alert
from scrapers.scraper_health import check_bcv_freshness

logger = logging.getLogger(__name__)

# How many hours to wait before sending the same alert type again
ALERT_COOLDOWN_HOURS = {
    "rate_spike_24h": 6,
    "rate_drop_12h": 4,
    "spread_critical": 4,
    "spread_elevated": 24,
    "momentum_rising": 24,
    "bcv_stale": 24,
}

_last_alerts: dict[str, datetime] = {}


def _is_on_cooldown(alert_type: str) -> bool:
    if alert_type not in _last_alerts:
        return False
    cooldown_h = ALERT_COOLDOWN_HOURS.get(alert_type, 12)
    elapsed = (datetime.utcnow() - _last_alerts[alert_type]).total_seconds() / 3600
    return elapsed < cooldown_h


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
        alert_type = alert["type"]
        if _is_on_cooldown(alert_type):
            logger.debug(f"Alert {alert_type} is on cooldown, skipping")
            continue

        try:
            message = build_spike_message(alert)
            timestamp = datetime.utcnow().isoformat()
            insert_alert(timestamp, alert["alert_type"], message)
            logger.info(f"Alert queued: {alert_type}")
        except Exception as e:
            logger.error(f"Failed to build alert message for {alert_type}: {e}")

    deliver_queued_alerts()


def deliver_queued_alerts():
    """Send any undelivered alerts via Telegram."""
    pending = get_undelivered_alerts()
    for alert in pending:
        success = send_alert(alert["alert_type"], alert["message"])
        if success:
            mark_alert_delivered(alert["id"])
            _last_alerts[alert.get("alert_type", "unknown")] = datetime.utcnow()
        else:
            logger.warning(f"Failed to deliver alert id={alert['id']}, will retry")
