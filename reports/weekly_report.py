import logging
from datetime import datetime
from db.db import get_weekly_data
from analysis.prompts import WEEKLY_REPORT_PROMPT
from analysis.claude_client import analyze

logger = logging.getLogger(__name__)


def _fmt(val, precision=2, suffix="", na="N/A"):
    """Format a numeric value or return na if None."""
    if val is None:
        return na
    return f"{val:.{precision}f}{suffix}"


def find_best_day_for_ves_to_usd(daily_rows: list) -> str:
    """Best day to convert VES → USD = lowest parallel rate (less VES needed per dollar)."""
    valid = [r for r in daily_rows if r.get("avg_parallel") is not None]
    if not valid:
        return "sin datos"
    return min(valid, key=lambda r: r["avg_parallel"])["day"]


def find_best_day_for_usd_to_ves(daily_rows: list) -> str:
    """Best day to convert USD → VES = highest parallel rate (more VES per dollar)."""
    valid = [r for r in daily_rows if r.get("avg_parallel") is not None]
    if not valid:
        return "sin datos"
    return max(valid, key=lambda r: r["avg_parallel"])["day"]


def build_weekly_table(daily_rows: list) -> str:
    lines = [
        "Fecha      | BCV avg  | Paralelo avg | Brecha avg | Brecha min | Brecha max",
        "-" * 76,
    ]
    for r in daily_rows:
        lines.append(
            f"{r['day']} | "
            f"{_fmt(r['avg_bcv']):>8} | "
            f"{_fmt(r['avg_parallel']):>12} | "
            f"{_fmt(r['avg_spread'], precision=1, suffix='%'):>10} | "
            f"{_fmt(r['min_spread'], precision=1, suffix='%'):>10} | "
            f"{_fmt(r['max_spread'], precision=1, suffix='%'):>10}"
        )
    return "\n".join(lines)


def generate_report() -> str:
    daily_rows, alert_count = get_weekly_data()

    if not daily_rows:
        return "Sin datos suficientes para generar el reporte semanal."

    weekly_table = build_weekly_table(daily_rows)
    all_spreads = [r["avg_spread"] for r in daily_rows if r["avg_spread"] is not None]
    all_max_spreads = [r["max_spread"] for r in daily_rows if r["max_spread"] is not None]
    all_min_spreads = [r["min_spread"] for r in daily_rows if r["min_spread"] is not None]

    avg_spread = sum(all_spreads) / len(all_spreads) if all_spreads else None
    max_spread = max(all_max_spreads) if all_max_spreads else None
    min_spread = min(all_min_spreads) if all_min_spreads else None

    best_for_ves_usd = find_best_day_for_ves_to_usd(daily_rows)
    best_for_usd_ves = find_best_day_for_usd_to_ves(daily_rows)

    prompt = WEEKLY_REPORT_PROMPT.format(
        weekly_table=weekly_table,
        avg_spread=_fmt(avg_spread, precision=1),
        max_spread=_fmt(max_spread, precision=1),
        min_spread=_fmt(min_spread, precision=1),
        best_day=f"{best_for_ves_usd} (mejor para vender bolívares)",
        alert_count=alert_count,
    )

    logger.info("Generating weekly report with Claude...")
    analysis = analyze(prompt, max_tokens=600, prompt_type="weekly_report")

    week_str = datetime.now().strftime("Semana del %d de %B, %Y")
    report = f"# Reporte Semanal Venezuela Divisas\n{week_str}\n\n"
    report += "## Datos de la semana\n\n"
    report += f"```\n{weekly_table}\n```\n\n"
    report += f"**Alertas disparadas:** {alert_count}\n"
    report += f"**Mejor día para vender bolívares (VES → USD):** {best_for_ves_usd}\n"
    report += f"**Mejor día para vender dólares (USD → VES):** {best_for_usd_ves}\n\n"
    report += "## Análisis\n\n"
    report += analysis

    return report
