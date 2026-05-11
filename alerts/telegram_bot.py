import os
import requests
import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_message(text: str, parse_mode: str = None) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return False

    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token),
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Telegram message sent successfully")
        return True
    except requests.RequestException as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def send_alert(alert_type: str, message: str) -> bool:
    header = {
        "SPIKE": "ALERTA: Movimiento brusco de tasa",
        "OPPORTUNITY": "OPORTUNIDAD: Bajada de tasa",
        "CRITICAL": "CRITICO: Brecha extrema BCV/Paralelo",
        "WARNING": "ADVERTENCIA: Brecha elevada",
        "MOMENTUM": "ATENCION: Tendencia alcista continua",
        "STALE": "AVISO: Datos BCV desactualizados",
    }.get(alert_type, "ALERTA Venezuela Divisas")

    full_message = f"[{header}]\n\n{message}"
    return send_message(full_message)


def send_daily_brief(message: str) -> bool:
    from datetime import datetime
    date_str = datetime.now().strftime("%A %d de %B, %Y")
    full_message = f"Venezuela Divisas — {date_str}\n\n{message}"
    return send_message(full_message)
