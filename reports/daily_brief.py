import logging
from datetime import datetime
from db.db import get_latest_rate, get_recent_rates, get_avg_spread, get_undelivered_alerts
from analysis.analyzer import (
    compute_change_pct, get_trend_description, get_rates_last_n_days,
    forecast_parallel_24h, SPREAD_ELEVATED, SPREAD_CRITICAL,
)
from analysis.prompts import DAILY_BRIEF_PROMPT
from analysis.claude_client import analyze
from alerts.telegram_bot import send_daily_brief

logger = logging.getLogger(__name__)


def get_spread_status(spread_pct: float) -> str:
    if spread_pct is None:
        return "N/A"
    if spread_pct > SPREAD_CRITICAL:
        return "CRITICA"
    elif spread_pct > SPREAD_ELEVATED:
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
    avg_spread_7d_ago = get_avg_spread(14)

    bcv = latest.get("bcv_rate")
    parallel = latest.get("parallel_rate")
    spread = latest.get("spread_pct")

    if bcv is None or parallel is None or spread is None:
        logger.warning("Missing rate data for daily brief — skipping")
        return False

    spread_status = get_spread_status(spread)
    change_24h_raw = compute_change_pct(rates_24h, 24)
    change_24h = change_24h_raw if change_24h_raw is not None else 0.0
    trend_7d = get_trend_description(rates_7d)

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

    forecast = forecast_parallel_24h()
    if forecast:
        forecast_str = (f"{forecast['point']:.2f} VES/USD "
                        f"(rango 95%: {forecast['low']:.2f}–{forecast['high']:.2f})")
    else:
        forecast_str = "datos insuficientes para proyección"

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
        forecast_24h=forecast_str,
    )

    logger.info("Generating daily brief with Claude...")
    analysis_text = analyze(prompt, max_tokens=240)

    success = send_daily_brief(
        bcv_rate=bcv,
        parallel_rate=parallel,
        spread_pct=spread,
        spread_status=spread_status,
        change_24h=change_24h,
        trend_7d=trend_7d,
        analysis_text=analysis_text,
    )
    if success:
        logger.info("Daily brief sent successfully")
    return success
