import os
import requests
import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

ALERT_HEADERS = {
    "SPIKE":       ("⚡", "MOVIMIENTO BRUSCO"),
    "OPPORTUNITY": ("💰", "OPORTUNIDAD"),
    "CRITICAL":    ("🔴", "ALERTA CRÍTICA"),
    "WARNING":     ("🟡", "BRECHA ELEVADA"),
    "MOMENTUM":    ("📈", "TENDENCIA ALCISTA"),
    "STALE":       ("⚠️", "DATOS BCV DESACTUALIZADOS"),
}


def send_message(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return False

    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Telegram message sent successfully")
        return True
    except requests.RequestException as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def send_alert(alert_type: str, message: str, bcv_rate=None, parallel_rate=None, spread_pct=None) -> bool:
    emoji, title = ALERT_HEADERS.get(alert_type, ("🔔", "ALERTA"))

    lines = [f"{emoji} <b>{title}</b>", "─" * 16]

    if bcv_rate and parallel_rate and spread_pct is not None:
        lines += [
            f"BCV:       <code>{bcv_rate:.2f} VES/USD</code>",
            f"Paralelo:  <code>{parallel_rate:.2f} VES/USD</code>",
            f"Brecha:    <b>{spread_pct:.1f}%</b>",
            "",
        ]

    lines.append(message)
    return send_message("\n".join(lines))


def send_daily_brief(bcv_rate, parallel_rate, spread_pct, spread_status,
                     change_24h, trend_7d, analysis_text) -> bool:
    from datetime import datetime

    status_emoji = {"NORMAL": "🟢", "ELEVADA": "🟡", "CRITICA": "🔴"}.get(spread_status, "⚪")
    change_emoji = "📈" if change_24h > 0 else "📉" if change_24h < 0 else "➡️"
    import locale
    try:
        locale.setlocale(locale.LC_TIME, "es_VE.UTF-8")
    except Exception:
        try:
            locale.setlocale(locale.LC_TIME, "es_ES.UTF-8")
        except Exception:
            pass
    date_str = datetime.now().strftime("%A %d de %B").lower().capitalize()

    lines = [
        f"<b>Venezuela Divisas — {date_str}</b>",
        "─" * 16,
        f"BCV:       <code>{bcv_rate:.2f} VES/USD</code>",
        f"Paralelo:  <code>{parallel_rate:.2f} VES/USD</code>",
        f"Brecha:    <b>{spread_pct:.1f}%</b>  {status_emoji} {spread_status}",
        f"24h:       {change_emoji} {change_24h:+.1f}%",
        f"Tendencia: {trend_7d}",
        "",
        analysis_text,
    ]
    return send_message("\n".join(lines))
