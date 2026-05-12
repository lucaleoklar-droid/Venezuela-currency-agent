import os
import requests
import logging
import html
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Reply keyboard shown after every brief/alert. Tapping a button sends the
# button text as a regular message; the poller's _handle_command handles it.
QUICK_REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "💱 Tasa"}, {"text": "📊 24h"}, {"text": "📅 Semana"}],
        [{"text": "💰 Precio 50"}, {"text": "🔁 Convertir 100000"}, {"text": "ℹ️ Estado"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

ALERT_HEADERS = {
    "SPIKE":       ("⚡", "MOVIMIENTO BRUSCO"),
    "OPPORTUNITY": ("💰", "OPORTUNIDAD"),
    "CRITICAL":    ("🔴", "ALERTA CRÍTICA"),
    "WARNING":     ("🟡", "BRECHA ELEVADA"),
    "MOMENTUM":    ("📈", "TENDENCIA ALCISTA"),
    "STALE":       ("⚠️", "DATOS BCV DESACTUALIZADOS"),
}

SPANISH_MONTHS = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
}
SPANISH_WEEKDAYS = {
    0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
    4: "viernes", 5: "sábado", 6: "domingo",
}


def _spanish_date(dt: datetime) -> str:
    weekday = SPANISH_WEEKDAYS[dt.weekday()]
    month = SPANISH_MONTHS[dt.month]
    return f"{weekday.capitalize()} {dt.day} de {month}"


def _escape(text: str) -> str:
    """Escape <, >, & for Telegram HTML mode. Preserves bold/code tags if pre-formatted."""
    return html.escape(text, quote=False)


def send_message(text: str, with_keyboard: bool = True) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return False

    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if with_keyboard:
        payload["reply_markup"] = QUICK_REPLY_KEYBOARD

    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token), json=payload, timeout=10,
        )
        resp.raise_for_status()
        logger.info("Telegram message sent")
        return True
    except requests.RequestException as e:
        body = getattr(e.response, "text", "") if hasattr(e, "response") else ""
        logger.error(f"Telegram send failed: {e}. Response: {body[:200]}")
        return False


def send_alert(alert_type: str, message: str, bcv_rate=None, parallel_rate=None, spread_pct=None) -> bool:
    emoji, title = ALERT_HEADERS.get(alert_type, ("🔔", "ALERTA"))

    lines = [f"{emoji} <b>{title}</b>", "─" * 16]

    if bcv_rate is not None and parallel_rate is not None and spread_pct is not None:
        lines += [
            f"BCV:       <code>{bcv_rate:.2f} VES/USD</code>",
            f"Paralelo:  <code>{parallel_rate:.2f} VES/USD</code>",
            f"Brecha:    <b>{spread_pct:.1f}%</b>",
            "",
        ]

    # Escape Claude's prose so & or < in the response don't break HTML parse
    lines.append(_escape(message))
    return send_message("\n".join(lines))


def send_daily_brief(bcv_rate, parallel_rate, spread_pct, spread_status,
                     change_24h, trend_7d, analysis_text) -> bool:
    status_emoji = {"NORMAL": "🟢", "ELEVADA": "🟡", "CRITICA": "🔴"}.get(spread_status, "⚪")

    # Only show direction emoji for moves > 0.5% (smaller moves are noise)
    if change_24h is None:
        change_str = "N/A"
    elif change_24h > 0.5:
        change_str = f"📈 +{change_24h:.1f}%"
    elif change_24h < -0.5:
        change_str = f"📉 {change_24h:.1f}%"
    else:
        change_str = f"➡️ {change_24h:+.1f}%"

    date_str = _spanish_date(datetime.now())

    lines = [
        f"<b>Venezuela Divisas — {date_str}</b>",
        "─" * 16,
        f"BCV:       <code>{bcv_rate:.2f} VES/USD</code>",
        f"Paralelo:  <code>{parallel_rate:.2f} VES/USD</code>",
        f"Brecha:    <b>{spread_pct:.1f}%</b>  {status_emoji} {spread_status}",
        f"24h:       {change_str}",
        f"Tendencia: {_escape(trend_7d)}",
        "",
        _escape(analysis_text),
    ]
    return send_message("\n".join(lines))
