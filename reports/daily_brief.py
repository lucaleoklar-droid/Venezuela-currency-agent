import logging
import re
from datetime import datetime, timezone, timedelta
from db.db import (get_latest_rate, get_recent_rates, get_avg_spread,
                   get_undelivered_alerts, upsert_daily_brief_action,
                   get_latest_forecast, get_latest_p2p_rate)
from analysis.analyzer import (
    compute_change_pct, get_trend_description, get_rates_last_n_days,
    forecast_parallel_24h, SPREAD_ELEVATED, SPREAD_CRITICAL,
)
from analysis.prompts import (DAILY_BRIEF_PROMPT_V2_SYSTEM,
                              DAILY_BRIEF_PROMPT_V2_USER)
from analysis.claude_client import analyze_v2
from alerts.telegram_bot import send_daily_brief
from reports.chart_generator import generate_chart
import os

logger = logging.getLogger(__name__)

# How recent a forecast must be to be trusted as "today's view". Anything older
# is treated as stale and falls back to a "no forecast available" string.
FORECAST_FRESH_HOURS = 4


_VALID_ACTIONS = ("CONVERTIR", "ESPERAR", "NEUTRAL")
_ACTION_RE = re.compile(
    r"^[\s*_`#>-]*acci[oó]n\s*[:\-]\s*\**\s*(convertir|esperar|neutral)\b",
    re.IGNORECASE,
)


def parse_and_enforce_action(text: str) -> tuple[str, str]:
    """Extract the Acción signal from Claude's output. If the model disobeyed
    the prompt (missing prefix, leading sentence, wrong word), default to
    NEUTRAL and prepend the canonical prefix.
    Returns (action_signal, normalized_brief_text)."""
    if not text:
        return "NEUTRAL", "Acción: NEUTRAL\n(sin contenido del modelo)"
    stripped = text.strip()
    first_line = stripped.split("\n", 1)[0]
    m = _ACTION_RE.match(first_line)
    if m:
        action = m.group(1).upper()
        # Normalize the first line to canonical form
        rest = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        normalized = f"Acción: {action}" + (f"\n{rest}" if rest else "")
        return action, normalized
    # Search anywhere in the text as a fallback before defaulting
    for line in stripped.splitlines():
        m = _ACTION_RE.match(line.strip())
        if m:
            action = m.group(1).upper()
            return action, f"Acción: {action}\n{stripped}"
    logger.warning("Daily brief missing Acción prefix — defaulting to NEUTRAL")
    return "NEUTRAL", f"Acción: NEUTRAL\n{stripped}"


def _format_forecast_probs(model_name: str) -> str:
    """Render the latest forecast row for a model as a short, prompt-friendly
    string. Returns 'no disponible' if no recent forecast exists."""
    row = get_latest_forecast(model_name)
    if not row:
        return "no disponible"
    try:
        made_at = datetime.fromisoformat(row["made_at"])
        if made_at.tzinfo is None:
            made_at = made_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, KeyError):
        return "no disponible"
    age_h = (datetime.now(timezone.utc) - made_at).total_seconds() / 3600
    if age_h > FORECAST_FRESH_HOURS:
        return f"obsoleto (hace {age_h:.1f}h)"
    return (f"widen={row['p_widen']:.0%} stable={row['p_stable']:.0%} "
            f"narrow={row['p_narrow']:.0%} (hace {age_h:.1f}h)")


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

    stat_v3_probs = _format_forecast_probs("stat_v3")
    naive_probs = _format_forecast_probs("naive")

    user_prompt = DAILY_BRIEF_PROMPT_V2_USER.format(
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
        stat_v3_probs=stat_v3_probs,
        naive_probs=naive_probs,
    )

    logger.info(f"Generating daily brief with Claude... (stat_v3: {stat_v3_probs})")
    raw_text = analyze_v2(
        system_text=DAILY_BRIEF_PROMPT_V2_SYSTEM,
        user_text=user_prompt,
        max_tokens=260,
        prompt_type="daily_brief",
    )
    action_signal, analysis_text = parse_and_enforce_action(raw_text)
    logger.info(f"Daily brief action signal: {action_signal}")

    today_iso = datetime.now().date().isoformat()
    try:
        upsert_daily_brief_action(
            date_str=today_iso,
            action_signal=action_signal,
            brief_text=analysis_text,
            bcv_rate=bcv,
            parallel_rate=parallel,
            spread_pct=spread,
        )
    except Exception as e:
        logger.exception(f"Failed to store daily brief action: {e}")

    p2p = get_latest_p2p_rate()
    p2p_line = None
    if p2p and p2p.get("mid_price") and parallel:
        diff_pct = (p2p["mid_price"] - parallel) / parallel * 100
        sign = "+" if diff_pct >= 0 else ""
        p2p_line = f"{p2p['mid_price']:.2f} VES/USDT ({sign}{diff_pct:.1f}% vs paralelo)"

    # Generate a fresh chart so the brief ships as a photo with caption.
    # Failure is non-fatal: telegram_bot will fall back to text-only.
    data_dir = os.getenv("DATA_DIR", os.path.dirname(os.path.dirname(__file__)))
    chart_path = os.path.join(data_dir, "daily_brief_chart.png")
    try:
        ok = generate_chart(chart_path, days=14)
        if not ok:
            chart_path = None
    except Exception as e:
        logger.exception(f"Chart generation failed (will send text-only): {e}")
        chart_path = None

    success = send_daily_brief(
        bcv_rate=bcv,
        parallel_rate=parallel,
        spread_pct=spread,
        spread_status=spread_status,
        change_24h=change_24h,
        trend_7d=trend_7d,
        analysis_text=analysis_text,
        chart_path=chart_path,
        p2p_line=p2p_line,
    )
    if success:
        logger.info("Daily brief sent successfully")
    return success
