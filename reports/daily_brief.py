import logging
from datetime import datetime
from db.db import get_latest_rate, get_recent_rates, get_avg_spread, get_undelivered_alerts
from analysis.analyzer import compute_change_pct, get_trend_description, get_rates_last_n_days
from analysis.prompts import DAILY_BRIEF_PROMPT
from analysis.claude_client import analyze
from alerts.telegram_bot import send_daily_brief

logger = logging.getLogger(__name__)


def get_spread_status(spread_pct: float) -> str:
    if spread_pct is None:
        return "N/A"
    if spread_pct > 20:
        return "CRITICA"
    elif spread_pct > 12:
        return "ELEVADA"
    else:
        return "NORMAL"


def generate_and_send():
    latest = get_latest_rate()
    if not latest:
        logger.warning("No rate data for daily brief")
        return False

    rates_24h = get_recent_rates(24)
    rates_7d = get_rates_last_n_days(7)
    avg_spread_7d_ago = get_avg_spread(14)  # rough "last week" comparison

    bcv = latest.get("bcv_rate", "N/A")
    parallel = latest.get("parallel_rate", "N/A")
    spread = latest.get("spread_pct")
    spread_status = get_spread_status(spread)
    change_24h = compute_change_pct(rates_24h, 24) or 0.0
    trend_7d = get_trend_description(rates_7d)

    # Compare to last week's average spread
    current_avg = get_avg_spread(7)
    if avg_spread_7d_ago and current_avg:
        diff = current_avg - avg_spread_7d_ago
        if abs(diff) < 0.5:
            vs_last_week = "similar a la semana pasada"
        elif diff > 0:
            vs_last_week = f"brecha {diff:.1f}% más alta que la semana pasada"
        else:
            vs_last_week = f"brecha {abs(diff):.1f}% más baja que la semana pasada"
    else:
        vs_last_week = "sin datos comparativos"

    pending_alerts = get_undelivered_alerts()
    active_alerts = f"{len(pending_alerts)} alerta(s) pendiente(s)" if pending_alerts else "ninguna"

    prompt = DAILY_BRIEF_PROMPT.format(
        date=datetime.now().strftime("%A %d de %B, %Y"),
        bcv_rate=bcv,
        parallel_rate=parallel,
        spread_pct=f"{spread:.1f}" if spread else "N/A",
        spread_status=spread_status,
        change_24h=f"{change_24h:+.2f}%",
        trend_7d=trend_7d,
        vs_last_week=vs_last_week,
        active_alerts=active_alerts,
    )

    logger.info("Generating daily brief with Claude...")
    brief_text = analyze(prompt, max_tokens=300)

    # Build full message
    lines = [
        f"BCV: {bcv} VES/USD  |  Paralelo: {parallel} VES/USD",
        f"Brecha: {spread:.1f}% ({spread_status})  |  Cambio 24h: {change_24h:+.2f}%",
        "",
        brief_text,
    ]
    full_message = "\n".join(lines)

    success = send_daily_brief(full_message)
    if success:
        logger.info("Daily brief sent successfully")
    return success
