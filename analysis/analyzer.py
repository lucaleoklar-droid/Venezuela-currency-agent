import logging
from datetime import datetime, timezone
from db.db import get_recent_rates, get_rates_last_n_days, get_avg_spread, get_latest_rate
from analysis.prompts import CORE_ANALYSIS_PROMPT, SPIKE_ALERT_PROMPT
from analysis.claude_client import analyze

logger = logging.getLogger(__name__)


def compute_change_pct(rates: list, hours: int) -> float | None:
    if len(rates) < 2:
        return None
    # rates are newest-first from get_recent_rates
    current = rates[0].get("parallel_rate")
    cutoff_ts = datetime.utcnow().timestamp() - hours * 3600
    # Find the rate closest to `hours` ago
    past_rate = None
    for r in rates:
        try:
            ts = datetime.fromisoformat(r["timestamp"]).timestamp()
        except Exception:
            continue
        if ts <= cutoff_ts:
            past_rate = r.get("parallel_rate")
            break
    if not current or not past_rate or past_rate == 0:
        return None
    return round((current - past_rate) / past_rate * 100, 2)


def get_trend_description(rates_asc: list, days: int = 7) -> str:
    if len(rates_asc) < 3:
        return "insuficientes datos"
    # Group by day, get avg per day
    daily = {}
    for r in rates_asc:
        day = r["timestamp"][:10]
        if day not in daily:
            daily[day] = []
        if r.get("parallel_rate"):
            daily[day].append(r["parallel_rate"])
    avgs = [sum(v) / len(v) for v in daily.values() if v]
    if len(avgs) < 2:
        return "sin tendencia clara"
    # Simple linear direction
    first_half = sum(avgs[: len(avgs) // 2]) / (len(avgs) // 2)
    second_half = sum(avgs[len(avgs) // 2:]) / (len(avgs) - len(avgs) // 2)
    diff_pct = (second_half - first_half) / first_half * 100
    if diff_pct > 2:
        return f"subiendo ({diff_pct:+.1f}%)"
    elif diff_pct < -2:
        return f"bajando ({diff_pct:+.1f}%)"
    else:
        return "estable"


def check_consecutive_rising(rates_asc: list, days: int = 3) -> bool:
    daily = {}
    for r in rates_asc:
        day = r["timestamp"][:10]
        if r.get("parallel_rate"):
            daily.setdefault(day, []).append(r["parallel_rate"])
    day_avgs = [(d, sum(v) / len(v)) for d, v in sorted(daily.items()) if v]
    if len(day_avgs) < days:
        return False
    recent = day_avgs[-days:]
    return all(recent[i][1] < recent[i + 1][1] for i in range(len(recent) - 1))


def format_7day_table(rates_7d: list) -> str:
    daily = {}
    for r in rates_7d:
        day = r["timestamp"][:10]
        daily.setdefault(day, {"bcv": [], "parallel": [], "spread": []})
        if r.get("bcv_rate"):
            daily[day]["bcv"].append(r["bcv_rate"])
        if r.get("parallel_rate"):
            daily[day]["parallel"].append(r["parallel_rate"])
        if r.get("spread_pct"):
            daily[day]["spread"].append(r["spread_pct"])

    lines = ["Fecha      | BCV    | Paralelo | Brecha"]
    lines.append("-" * 42)
    for day in sorted(daily.keys()):
        d = daily[day]
        bcv = f"{sum(d['bcv'])/len(d['bcv']):.2f}" if d["bcv"] else "N/A"
        par = f"{sum(d['parallel'])/len(d['parallel']):.2f}" if d["parallel"] else "N/A"
        spr = f"{sum(d['spread'])/len(d['spread']):.1f}%" if d["spread"] else "N/A"
        lines.append(f"{day} | {bcv:>6} | {par:>8} | {spr:>6}")
    return "\n".join(lines)


def run_analysis() -> dict:
    rates_24h = get_recent_rates(24)
    rates_7d = get_rates_last_n_days(7)
    latest = get_latest_rate()

    if not latest:
        logger.warning("No rate data available for analysis")
        return {"error": "No data"}

    bcv_rate = latest.get("bcv_rate", "N/A")
    parallel_rate = latest.get("parallel_rate", "N/A")
    spread_pct = latest.get("spread_pct", "N/A")
    change_24h = compute_change_pct(rates_24h, 24) or 0.0
    trend_7d = get_trend_description(rates_7d)
    avg_spread_30d = get_avg_spread(30)
    table = format_7day_table(rates_7d)

    prompt = CORE_ANALYSIS_PROMPT.format(
        bcv_rate=bcv_rate,
        parallel_rate=parallel_rate,
        spread_pct=spread_pct,
        change_24h=f"{change_24h:+.2f}" if isinstance(change_24h, float) else change_24h,
        trend_7d=trend_7d,
        avg_spread_30d=f"{avg_spread_30d:.1f}" if isinstance(avg_spread_30d, (int, float)) else "N/A",
        last_7_days_table=table,
    )

    logger.info("Running Claude analysis...")
    response = analyze(prompt)

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "bcv_rate": bcv_rate,
        "parallel_rate": parallel_rate,
        "spread_pct": spread_pct,
        "change_24h": change_24h,
        "trend_7d": trend_7d,
        "claude_response": response,
    }


def check_spike_alerts() -> list[dict]:
    """Returns list of alert dicts if any thresholds are breached."""
    alerts = []
    rates_24h = get_recent_rates(24)
    rates_12h = get_recent_rates(12)
    latest = get_latest_rate()

    if not latest:
        return alerts

    parallel = latest.get("parallel_rate")
    bcv = latest.get("bcv_rate")
    spread = latest.get("spread_pct")

    # Rate spike > 3% in 24h
    change_24h = compute_change_pct(rates_24h, 24)
    if change_24h and abs(change_24h) > 3:
        direction = "subió" if change_24h > 0 else "bajó"
        alerts.append({
            "type": "rate_spike_24h",
            "detail": f"La tasa paralela {direction} {abs(change_24h):.1f}% en las últimas 24h",
            "bcv_rate": bcv, "parallel_rate": parallel, "spread_pct": spread,
            "alert_type": "SPIKE",
        })

    # Rate drop > 2% in 12h (opportunity)
    change_12h = compute_change_pct(rates_12h, 12)
    if change_12h and change_12h < -2:
        alerts.append({
            "type": "rate_drop_12h",
            "detail": f"La tasa bajó {abs(change_12h):.1f}% en las últimas 12h — posible oportunidad de conversión",
            "bcv_rate": bcv, "parallel_rate": parallel, "spread_pct": spread,
            "alert_type": "OPPORTUNITY",
        })

    # Spread critical > 20%
    if spread and spread > 20:
        alerts.append({
            "type": "spread_critical",
            "detail": f"Brecha entre BCV y paralelo: {spread:.1f}% (CRÍTICA — umbral: 20%)",
            "bcv_rate": bcv, "parallel_rate": parallel, "spread_pct": spread,
            "alert_type": "CRITICAL",
        })
    # Spread elevated > 12%
    elif spread and spread > 12:
        alerts.append({
            "type": "spread_elevated",
            "detail": f"Brecha entre BCV y paralelo: {spread:.1f}% (ELEVADA — umbral: 12%)",
            "bcv_rate": bcv, "parallel_rate": parallel, "spread_pct": spread,
            "alert_type": "WARNING",
        })

    # 3-day momentum
    rates_7d = get_rates_last_n_days(7)
    if check_consecutive_rising(rates_7d, 3):
        alerts.append({
            "type": "momentum_rising",
            "detail": "La tasa paralela ha subido 3 días consecutivos",
            "bcv_rate": bcv, "parallel_rate": parallel, "spread_pct": spread,
            "alert_type": "MOMENTUM",
        })

    return alerts


def build_spike_message(alert: dict) -> str:
    prompt = SPIKE_ALERT_PROMPT.format(
        alert_type=alert["alert_type"],
        bcv_rate=alert.get("bcv_rate", "N/A"),
        parallel_rate=alert.get("parallel_rate", "N/A"),
        spread_pct=alert.get("spread_pct", "N/A"),
        detail=alert["detail"],
    )
    return analyze(prompt, max_tokens=150)
