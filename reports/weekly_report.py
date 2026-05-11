import logging
from datetime import datetime
from db.db import get_weekly_data, get_rates_last_n_days
from analysis.prompts import WEEKLY_REPORT_PROMPT
from analysis.claude_client import analyze

logger = logging.getLogger(__name__)


def find_best_day(daily_rows: list) -> str:
    if not daily_rows:
        return "sin datos"
    # Best day = lowest spread (cheapest to convert bolivars to USD)
    best = min(daily_rows, key=lambda r: r["avg_spread"] or 999)
    return best["day"] if best else "sin datos"


def build_weekly_table(daily_rows: list) -> str:
    lines = ["Fecha      | BCV avg | Paralelo avg | Brecha avg | Brecha min | Brecha max"]
    lines.append("-" * 72)
    for r in daily_rows:
        lines.append(
            f"{r['day']} | "
            f"{r['avg_bcv']:.2f if r['avg_bcv'] else 'N/A':>7} | "
            f"{r['avg_parallel']:.2f if r['avg_parallel'] else 'N/A':>12} | "
            f"{r['avg_spread']:.1f if r['avg_spread'] else 'N/A':>10}% | "
            f"{r['min_spread']:.1f if r['min_spread'] else 'N/A':>10}% | "
            f"{r['max_spread']:.1f if r['max_spread'] else 'N/A':>10}%"
        )
    return "\n".join(lines)


def generate_report() -> str:
    daily_rows, alert_count = get_weekly_data()

    if not daily_rows:
        return "Sin datos suficientes para generar el reporte semanal."

    weekly_table = build_weekly_table(daily_rows)
    all_spreads = [r["avg_spread"] for r in daily_rows if r["avg_spread"]]
    avg_spread = sum(all_spreads) / len(all_spreads) if all_spreads else None
    max_spread = max(r["max_spread"] for r in daily_rows if r["max_spread"]) if daily_rows else None
    min_spread = min(r["min_spread"] for r in daily_rows if r["min_spread"]) if daily_rows else None
    best_day = find_best_day(daily_rows)

    prompt = WEEKLY_REPORT_PROMPT.format(
        weekly_table=weekly_table,
        avg_spread=f"{avg_spread:.1f}" if avg_spread else "N/A",
        max_spread=f"{max_spread:.1f}" if max_spread else "N/A",
        min_spread=f"{min_spread:.1f}" if min_spread else "N/A",
        best_day=best_day,
        alert_count=alert_count,
    )

    logger.info("Generating weekly report with Claude...")
    analysis = analyze(prompt, max_tokens=600)

    week_str = datetime.now().strftime("Semana del %d de %B, %Y")
    report = f"# Reporte Semanal Venezuela Divisas\n{week_str}\n\n"
    report += "## Datos de la semana\n\n"
    report += f"```\n{weekly_table}\n```\n\n"
    report += f"**Alertas disparadas esta semana:** {alert_count}\n"
    report += f"**Mejor dia para convertir:** {best_day}\n\n"
    report += "## Análisis\n\n"
    report += analysis

    return report
