import os
import requests
import logging
import html
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_PHOTO_API = "https://api.telegram.org/bot{token}/sendPhoto"

# Telegram caps photo captions at 1024 characters. We hard-truncate longer text
# and ship the remainder as a follow-up text message so nothing is lost.
PHOTO_CAPTION_MAX = 1024

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


def send_photo(image_path: str, caption: str, with_keyboard: bool = True) -> bool:
    """Send a photo with caption. Returns False if file missing or API fails.
    Captions over PHOTO_CAPTION_MAX are split: the caption is truncated and
    the full text is also sent as a follow-up message so nothing is lost."""
    import os
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return False
    if not os.path.exists(image_path):
        logger.warning(f"send_photo: image not found at {image_path}")
        return False

    short_caption = caption
    overflow = None
    if len(caption) > PHOTO_CAPTION_MAX:
        short_caption = caption[: PHOTO_CAPTION_MAX - 4].rstrip() + "…"
        overflow = caption  # send full text afterwards

    data = {"chat_id": chat_id, "caption": short_caption, "parse_mode": "HTML"}
    if with_keyboard:
        import json as _json
        data["reply_markup"] = _json.dumps(QUICK_REPLY_KEYBOARD)

    try:
        with open(image_path, "rb") as f:
            resp = requests.post(
                TELEGRAM_PHOTO_API.format(token=token),
                data=data,
                files={"photo": f},
                timeout=30,
            )
        resp.raise_for_status()
        logger.info("Telegram photo sent")
    except requests.RequestException as e:
        body = getattr(e.response, "text", "") if hasattr(e, "response") else ""
        logger.error(f"Telegram sendPhoto failed: {e}. Response: {body[:200]}")
        return False

    if overflow:
        # Best-effort follow-up; if it fails the photo+short_caption already shipped
        send_message(overflow, with_keyboard=False)
    return True


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


def _build_brief_text(bcv_rate, parallel_rate, spread_pct, spread_status,
                      change_24h, trend_7d, analysis_text) -> str:
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
    return "\n".join(lines)


def send_daily_brief(bcv_rate, parallel_rate, spread_pct, spread_status,
                     change_24h, trend_7d, analysis_text,
                     chart_path: str | None = None) -> bool:
    """Send the daily brief. If chart_path is given and the file exists, ship
    as photo with caption; otherwise fall back to a text message."""
    text = _build_brief_text(bcv_rate, parallel_rate, spread_pct, spread_status,
                             change_24h, trend_7d, analysis_text)
    if chart_path:
        import os
        if os.path.exists(chart_path):
            if send_photo(chart_path, text):
                return True
            logger.warning("Photo send failed; falling back to text-only brief")
    return send_message(text)
