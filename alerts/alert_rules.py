import logging
from datetime import datetime, timezone
from db.db import (
    insert_alert, get_undelivered_alerts, mark_alert_delivered,
    get_cooldown, set_cooldown,
)
from analysis.analyzer import check_spike_alerts, build_spike_message
from alerts.telegram_bot import send_alert
from scrapers.scraper_health import check_bcv_freshness, check_parallel_freshness

logger = logging.getLogger(__name__)

# Cooldowns keyed by alert_type. EMERGENCY/CRITICAL/WARNING use different keys
# so escalation across spread tiers re-fires correctly.
ALERT_COOLDOWN_HOURS = {
    "SPIKE": 6,
    "OPPORTUNITY": 4,
    "EMERGENCY": 1,
    "CRITICAL": 4,
    "WARNING": 24,
    "MOMENTUM": 24,
    "STALE": 24,
}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_on_cooldown(alert_type: str) -> bool:
    last_sent_str = get_cooldown(alert_type)
    if not last_sent_str:
        return False
    cooldown_h = ALERT_COOLDOWN_HOURS.get(alert_type, 12)
    try:
        last_sent = datetime.fromisoformat(last_sent_str)
    except (ValueError, TypeError):
        return False
    elapsed = (_utcnow() - last_sent).total_seconds() / 3600
    return elapsed < cooldown_h


def _mark_cooldown(alert_type: str):
    set_cooldown(alert_type, _utcnow().isoformat())


def process_alerts():
    """Detect alert conditions, generate Claude messages, queue and deliver."""
    alerts = check_spike_alerts()

    bcv_health = check_bcv_freshness()
    if bcv_health["stale"]:
        alerts.append({
            "type": "bcv_stale",
            "detail": bcv_health["message"],
            "bcv_rate": None, "parallel_rate": None, "spread_pct": None,
            "alert_type": "STALE",
        })

    parallel_health = check_parallel_freshness()
    if parallel_health["stale"]:
        alerts.append({
            "type": "parallel_stale",
            "detail": parallel_health["message"],
            "bcv_rate": None, "parallel_rate": None, "spread_pct": None,
            "alert_type": "STALE",
        })

    for alert in alerts:
        alert_type = alert["alert_type"]
        if _is_on_cooldown(alert_type):
            logger.info(f"Alert {alert_type} on cooldown, skipping")
            continue

        try:
            message = build_spike_message(alert)
            insert_alert(
                timestamp=_utcnow().isoformat(),
                alert_type=alert_type,
                message=message,
                bcv_rate=alert.get("bcv_rate"),
                parallel_rate=alert.get("parallel_rate"),
                spread_pct=alert.get("spread_pct"),
            )
            logger.info(f"Alert queued: {alert_type}")
        except Exception as e:
            logger.error(f"Failed to build alert message for {alert_type}: {e}")

    deliver_queued_alerts()


# Must exceed the 30-min scrape interval: delivery retries only happen on the
# next cycle, so a max age below 30 min meant failed sends were never retried.
STALE_ALERT_MAX_AGE_MIN = 45


def deliver_queued_alerts():
    """Send any undelivered alerts via Telegram. Drop alerts older than the max age."""
    from db.db import mark_alert_delivered as _mark
    pending = get_undelivered_alerts()
    now = _utcnow()

    for alert in pending:
        # Drop alerts that are too old to be relevant (prevents backlog spam)
        try:
            queued_at = datetime.fromisoformat(alert["timestamp"])
            age_min = (now - queued_at).total_seconds() / 60
            if age_min > STALE_ALERT_MAX_AGE_MIN:
                logger.info(f"Dropping stale alert id={alert['id']} (age: {age_min:.1f} min)")
                _mark(alert["id"])
                continue
        except (ValueError, TypeError):
            pass
        success = send_alert(
            alert_type=alert["alert_type"],
            message=alert["message"],
            bcv_rate=alert.get("bcv_rate"),
            parallel_rate=alert.get("parallel_rate"),
            spread_pct=alert.get("spread_pct"),
        )
        if success:
            mark_alert_delivered(alert["id"])
            _mark_cooldown(alert["alert_type"])
        else:
            logger.warning(f"Failed to deliver alert id={alert['id']}, will retry")
