"""Monthly retrospective sent on the 1st of each month."""
import logging
from datetime import datetime, timezone, timedelta
from db.db import get_connection, get_user_actions_since
from alerts.telegram_bot import send_message

logger = logging.getLogger(__name__)


def _spanish_month(month: int) -> str:
    names = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
             "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    return names[month - 1]


def _period_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Return (start, end) covering the previous full calendar month."""
    first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = first_this_month - timedelta(seconds=1)
    last_month_start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return last_month_start, last_month_end


def _rates_stats(conn, start_iso: str, end_iso: str) -> dict:
    row = conn.execute(
        "SELECT AVG(parallel_rate) as avg_par, MIN(parallel_rate) as min_par, "
        "MAX(parallel_rate) as max_par, AVG(spread_pct) as avg_spread, "
        "COUNT(*) as readings FROM rates "
        "WHERE timestamp >= ? AND timestamp < ? AND parallel_rate IS NOT NULL",
        (start_iso, end_iso),
    ).fetchone()
    first = conn.execute(
        "SELECT parallel_rate FROM rates WHERE timestamp >= ? AND parallel_rate IS NOT NULL "
        "ORDER BY timestamp ASC LIMIT 1", (start_iso,)
    ).fetchone()
    last = conn.execute(
        "SELECT parallel_rate FROM rates WHERE timestamp < ? AND parallel_rate IS NOT NULL "
        "ORDER BY timestamp DESC LIMIT 1", (end_iso,)
    ).fetchone()
    alerts = conn.execute(
        "SELECT COUNT(*) as n, alert_type FROM alerts "
        "WHERE timestamp >= ? AND timestamp < ? GROUP BY alert_type",
        (start_iso, end_iso),
    ).fetchall()
    return {
        "avg_par": row["avg_par"], "min_par": row["min_par"], "max_par": row["max_par"],
        "avg_spread": row["avg_spread"], "readings": row["readings"],
        "first_par": first["parallel_rate"] if first else None,
        "last_par": last["parallel_rate"] if last else None,
        "alerts_by_type": {r["alert_type"]: r["n"] for r in alerts},
    }


def _action_stats(start_iso: str) -> dict:
    actions = get_user_actions_since(start_iso)
    converted = [a for a in actions if a["action"] == "converted"]
    waited = [a for a in actions if a["action"] == "waited"]
    total_ves = sum(a["amount_ves"] or 0 for a in converted)
    return {
        "converted_count": len(converted),
        "waited_count": len(waited),
        "total_ves_converted": total_ves,
    }


def build_monthly_report(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    start, end = _period_bounds(now)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    conn = get_connection()
    try:
        rs = _rates_stats(conn, start_iso, end_iso)
    finally:
        conn.close()
    acts = _action_stats(start_iso)

    month_label = f"{_spanish_month(start.month).capitalize()} {start.year}"
    lines = [
        f"<b>Resumen mensual — {month_label}</b>",
        "─" * 16,
    ]

    if rs["readings"]:
        depreciation = None
        if rs["first_par"] and rs["last_par"]:
            depreciation = (rs["last_par"] - rs["first_par"]) / rs["first_par"] * 100
        lines += [
            f"Paralelo promedio:   <code>{rs['avg_par']:.2f} VES/USD</code>",
            f"Rango:               {rs['min_par']:.2f} – {rs['max_par']:.2f}",
            f"Brecha promedio:     <b>{rs['avg_spread']:.1f}%</b>",
            f"Lecturas:            {rs['readings']}",
        ]
        if depreciation is not None:
            sign = "+" if depreciation >= 0 else ""
            lines.append(f"Depreciación bolívar: <b>{sign}{depreciation:.2f}%</b> en el mes")
    else:
        lines.append("Sin datos de tasas en el período.")

    if rs["alerts_by_type"]:
        lines += ["", "<b>Alertas del mes</b>"]
        for t, n in sorted(rs["alerts_by_type"].items(), key=lambda x: -x[1]):
            lines.append(f"  {t}: {n}")

    lines += ["", "<b>Tus decisiones</b>"]
    if acts["converted_count"] or acts["waited_count"]:
        lines.append(f"  Conversiones: {acts['converted_count']}")
        if acts["total_ves_converted"]:
            lines.append(f"  Total convertido: {acts['total_ves_converted']:,.0f} VES".replace(",", "."))
        lines.append(f"  Decisiones de esperar: {acts['waited_count']}")
    else:
        lines.append("  Sin decisiones registradas. Usa <code>convertí X</code> o <code>esperé</code>.")

    return "\n".join(lines)


def generate_and_send() -> bool:
    text = build_monthly_report()
    return send_message(text)
