"""
Polls Telegram for incoming messages and responds with current data on demand.
Only responds to chat IDs listed in TELEGRAM_CHAT_ID (comma-separated).
"""
import os
import re
import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from db.db import get_latest_rate, get_recent_rates, log_user_action
from analysis.analyzer import (
    compute_change_pct, get_trend_description, get_rates_last_n_days,
    SPREAD_ELEVATED, SPREAD_CRITICAL,
)
from alerts.telegram_bot import _escape, _spanish_date, QUICK_REPLY_KEYBOARD

VET_OFFSET = timedelta(hours=-4)  # Venezuela is UTC-4, no DST

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"

_OFFSET_FILE = os.path.join(
    os.getenv("DATA_DIR", os.path.dirname(os.path.dirname(__file__))),
    "telegram_offset.json"
)


def _allowed_chat_ids() -> set:
    raw = os.getenv("TELEGRAM_CHAT_ID", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _load_offset() -> int:
    try:
        if os.path.exists(_OFFSET_FILE):
            with open(_OFFSET_FILE) as f:
                return int(json.load(f).get("offset", 0))
    except Exception:
        pass
    return 0


def _save_offset(offset: int):
    try:
        with open(_OFFSET_FILE, "w") as f:
            json.dump({"offset": offset}, f)
    except Exception as e:
        logger.warning(f"Could not save Telegram offset: {e}")


def _send_to_chat(chat_id: str, text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token) + "/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True,
                  "reply_markup": QUICK_REPLY_KEYBOARD},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        body = getattr(e.response, "text", "") if hasattr(e, "response") else ""
        logger.error(f"Reply send failed: {e}. Response: {body[:200]}")


def _help_message() -> str:
    return (
        "<b>Venezuela Divisas — Comandos</b>\n"
        "────────────────\n"
        "<b>tasa</b>  · tasas actuales y brecha\n"
        "<b>24h</b>   · resumen últimas 24 horas\n"
        "<b>semana</b> · resumen últimos 7 días\n"
        "<b>estado</b> · salud del agente\n\n"
        "<b>precio 50</b>\n"
        "  Cuántos VES son $50 al paralelo (vs BCV)\n\n"
        "<b>convertir 100000</b>\n"
        "  Cuántos USD son 100,000 VES\n\n"
        "<b>convertí 500000</b> / <b>esperé</b>\n"
        "  Registra tu decisión (para el resumen mensual)\n\n"
        "<b>ayuda</b> · este menú"
    )


def _current_rate_message() -> str:
    latest = get_latest_rate()
    if not latest:
        return "Sin datos disponibles todavía. El agente está iniciando."

    bcv = latest.get("bcv_rate")
    parallel = latest.get("parallel_rate")
    spread = latest.get("spread_pct")

    # Extract source's own update time from notes (set during insert)
    source_updated_at = None
    notes = (latest.get("notes") or "")
    if "source_updated_at=" in notes:
        for tok in notes.split(";"):
            tok = tok.strip()
            if tok.startswith("source_updated_at="):
                source_updated_at = tok.split("=", 1)[1]
                break

    status_emoji = "🟢"
    status_label = "NORMAL"
    if spread is not None and spread > SPREAD_CRITICAL:
        status_emoji, status_label = "🔴", "CRÍTICA"
    elif spread is not None and spread > SPREAD_ELEVATED:
        status_emoji, status_label = "🟡", "ELEVADA"

    rates_24h = get_recent_rates(24)
    change_24h = compute_change_pct(rates_24h, 24)

    if change_24h is None:
        change_str = "N/A"
    elif change_24h > 0.5:
        change_str = f"📈 +{change_24h:.1f}%"
    elif change_24h < -0.5:
        change_str = f"📉 {change_24h:.1f}%"
    else:
        change_str = f"➡️ {change_24h:+.1f}%"

    lines = [
        f"<b>Tasas actuales — {_spanish_date(datetime.now(timezone.utc) + VET_OFFSET)}</b>",
        "─" * 16,
        f"BCV:       <code>{bcv:.2f} VES/USD</code>" if bcv is not None else "BCV:       N/A",
        f"Paralelo:  <code>{parallel:.2f} VES/USD</code>" if parallel is not None else "Paralelo:  N/A",
        f"Brecha:    <b>{spread:.1f}%</b>  {status_emoji} {status_label}" if spread is not None else "Brecha:    N/A",
        f"24h:       {change_str}",
    ]

    if source_updated_at:
        try:
            src_ts = datetime.fromisoformat(source_updated_at.replace("Z", "+00:00"))
            if src_ts.tzinfo is None:
                src_ts = src_ts.replace(tzinfo=timezone.utc)
            hours = (datetime.now(timezone.utc) - src_ts).total_seconds() / 3600
            if hours > 2:
                lines.append("")
                lines.append(f"⚠️ Fuente sin actualizar hace {hours:.1f}h")
        except (ValueError, TypeError):
            pass

    return "\n".join(lines)


def _history_24h_message() -> str:
    rates = get_recent_rates(24)
    if not rates:
        return "Sin datos en las últimas 24 horas."

    valid = [r for r in rates if r.get("parallel_rate") is not None]
    if not valid:
        return "Sin datos válidos en las últimas 24 horas."

    parallels = [r["parallel_rate"] for r in valid]
    spreads = [r["spread_pct"] for r in valid if r.get("spread_pct") is not None]

    high = max(parallels)
    low = min(parallels)
    avg = sum(parallels) / len(parallels)

    lines = [
        "<b>Últimas 24 horas — Tasa paralela</b>",
        "─" * 16,
        f"Máxima:    <code>{high:.2f}</code>",
        f"Mínima:    <code>{low:.2f}</code>",
        f"Promedio:  <code>{avg:.2f}</code>",
        f"Lecturas:  {len(valid)}",
    ]
    if spreads:
        lines.append(f"Brecha avg: <b>{sum(spreads)/len(spreads):.1f}%</b>")
    return "\n".join(lines)


def _week_message() -> str:
    rates = get_rates_last_n_days(7)
    if not rates:
        return "Sin datos suficientes para el resumen semanal."
    valid = [r for r in rates if r.get("parallel_rate") is not None]
    if not valid:
        return "Sin datos válidos en los últimos 7 días."

    parallels = [r["parallel_rate"] for r in valid]
    spreads = [r["spread_pct"] for r in valid if r.get("spread_pct") is not None]
    trend = get_trend_description(rates)

    lines = [
        "<b>Últimos 7 días</b>",
        "─" * 16,
        f"Paralela máx:  <code>{max(parallels):.2f}</code>",
        f"Paralela mín:  <code>{min(parallels):.2f}</code>",
        f"Paralela avg:  <code>{sum(parallels)/len(parallels):.2f}</code>",
        f"Brecha avg:    <b>{sum(spreads)/len(spreads):.1f}%</b>" if spreads else "Brecha avg: N/A",
        f"Tendencia:     {_escape(trend)}",
        f"Lecturas:      {len(valid)}",
    ]
    return "\n".join(lines)


def _status_message() -> str:
    from scrapers.scraper_health import check_data_freshness, check_bcv_freshness
    data = check_data_freshness()
    bcv = check_bcv_freshness()

    ok_emoji = "🟢" if data.get("ok") else "🔴"
    bcv_emoji = "🟢" if not bcv.get("stale") else "🟡"

    data_line = (
        f"Datos:   {ok_emoji} última lectura hace {data['hours_since']}h"
        if "hours_since" in data
        else f"Datos:   {ok_emoji} sin datos aún"
    )
    bcv_line = (
        f"BCV:     {bcv_emoji} actualizado hace {bcv['hours_since_update']}h"
        if bcv.get("last_update")
        else f"BCV:     {bcv_emoji} sin datos aún"
    )

    return "\n".join(["<b>Estado del agente</b>", "─" * 16, data_line, bcv_line])


_AMOUNT_RE = re.compile(r"[\d.,]+")


def _parse_amount(text: str) -> float | None:
    """Pull the first number from text. Accepts 50, 50.5, 50,5, 50000, 50.000, 50,000."""
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    raw = m.group(0)
    # If both . and , present, treat the rightmost as decimal separator
    if "." in raw and "," in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        # Lone comma: ambiguous. If 3 digits after comma -> thousands; else decimal.
        after = raw.split(",")[-1]
        raw = raw.replace(",", "") if len(after) == 3 else raw.replace(",", ".")
    elif "." in raw:
        # Lone dot: Spanish thousands separator if last segment is exactly 3 digits
        # AND there's no other interpretation. "100.000" -> 100000; "50.5" -> 50.5.
        parts = raw.split(".")
        if len(parts) >= 2 and all(len(p) == 3 for p in parts[1:]):
            raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _fmt_ves(n: float) -> str:
    return f"{n:,.0f}".replace(",", ".")


def _price_in_ves_message(usd_amount: float) -> str:
    latest = get_latest_rate()
    if not latest or not latest.get("parallel_rate"):
        return "Sin datos de tasa todavía."
    parallel = latest["parallel_rate"]
    bcv = latest.get("bcv_rate")
    ves_parallel = usd_amount * parallel
    lines = [
        f"<b>Precio en bolívares — ${usd_amount:,.2f} USD</b>",
        "─" * 16,
        f"Al paralelo ({parallel:.2f}):  <b>{_fmt_ves(ves_parallel)} VES</b>",
    ]
    if bcv:
        ves_bcv = usd_amount * bcv
        loss = ves_parallel - ves_bcv
        loss_pct = (loss / ves_parallel) * 100 if ves_parallel else 0
        lines.append(f"Al BCV ({bcv:.2f}):       {_fmt_ves(ves_bcv)} VES")
        lines.append("")
        lines.append(f"Diferencia: <b>{_fmt_ves(loss)} VES</b> ({loss_pct:.1f}% de margen)")
        lines.append("Use el paralelo para no perder margen.")
    return "\n".join(lines)


def _convert_to_usd_message(ves_amount: float) -> str:
    latest = get_latest_rate()
    if not latest or not latest.get("parallel_rate"):
        return "Sin datos de tasa todavía."
    parallel = latest["parallel_rate"]
    bcv = latest.get("bcv_rate")
    usd_parallel = ves_amount / parallel
    lines = [
        f"<b>Conversión — {_fmt_ves(ves_amount)} VES</b>",
        "─" * 16,
        f"Valor real (paralelo {parallel:.2f}):  <b>${usd_parallel:,.2f} USD</b>",
    ]
    if bcv:
        usd_bcv = ves_amount / bcv
        lines.append(f"Valor oficial (BCV {bcv:.2f}):    ${usd_bcv:,.2f} USD")
        lines.append("")
        lines.append("El paralelo refleja el poder de compra real. Use éste para decisiones.")
    return "\n".join(lines)


def _log_action(chat_id: str | None, action: str, amount: float | None) -> str:
    latest = get_latest_rate()
    rate = latest.get("parallel_rate") if latest else None
    log_user_action(chat_id or "", action, amount, rate)
    if action == "converted":
        if amount and rate:
            usd = amount / rate
            return (f"✅ Registrado: convertiste {_fmt_ves(amount)} VES al paralelo "
                    f"{rate:.2f} = ${usd:,.2f} USD.")
        return "✅ Conversión registrada."
    return "✅ Decisión registrada: esperaste."


def _handle_command(text: str, chat_id: str | None = None) -> str:
    t = (text or "").lower().strip()

    try:
        if t in ["/start", "/help", "/ayuda", "ayuda", "help"]:
            return _help_message()
        # Feedback: log dad's actual decision. Match first token only so
        # "convertir 500" (calculator) doesn't get caught here.
        first_word = t.split()[0] if t.split() else ""
        if first_word in {"convertí", "convertido", "converti"}:
            return _log_action(chat_id, "converted", _parse_amount(t))
        if first_word in {"esperé", "espere", "esperando"}:
            return _log_action(chat_id, "waited", None)
        if any(kw in t for kw in ["semana", "7 dias", "7 días", "weekly", "week"]):
            return _week_message()
        if any(kw in t for kw in ["24h", "historia", "history", "ayer"]):
            return _history_24h_message()
        if any(kw in t for kw in ["estado", "status", "salud", "health"]):
            return _status_message()
        # Pricing: "precio 50" -> how many VES is $50
        if any(kw in t for kw in ["precio", "price", "$", "usd"]):
            amount = _parse_amount(t)
            if amount is None:
                return "Use: <code>precio 50</code> para calcular cuántos VES son $50."
            return _price_in_ves_message(amount)
        # Conversion: "convertir 100000" -> how many USD is 100000 VES
        if any(kw in t for kw in ["convertir", "convert", "bs", "bolivares", "bolívares", "ves"]):
            amount = _parse_amount(t)
            if amount is None:
                return "Use: <code>convertir 100000</code> para convertir 100,000 VES a USD."
            return _convert_to_usd_message(amount)
        if any(kw in t for kw in ["tasa", "dolar", "dólar", "cambio", "rate", "/rate"]):
            return _current_rate_message()
        return _help_message()
    except Exception as e:
        logger.exception(f"Command handler error for {text!r}: {e}")
        return "Hubo un error temporal procesando tu consulta. Intenta de nuevo en un momento."


def poll_for_messages(long_poll: bool = False):
    """Check Telegram for incoming messages and respond.
    long_poll=True uses 25s long polling for near-instant response."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return

    offset = _load_offset()
    allowed = _allowed_chat_ids()
    poll_timeout = 25 if long_poll else 0

    try:
        resp = requests.get(
            TELEGRAM_API.format(token=token) + "/getUpdates",
            params={"offset": offset + 1, "timeout": poll_timeout},
            timeout=poll_timeout + 10,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            return

        new_offset = offset
        for update in data.get("result", []):
            new_offset = max(new_offset, update["update_id"])
            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue
            chat_id = str(msg.get("chat", {}).get("id"))
            text = msg.get("text", "")
            user = msg.get("from", {}).get("first_name", "?")

            if allowed and chat_id not in allowed:
                logger.warning(f"Ignored message from unauthorized chat {chat_id} ({user})")
                continue

            logger.info(f"Query from {user}: {text!r}")
            response = _handle_command(text, chat_id=chat_id)
            _send_to_chat(chat_id, response)

        if new_offset != offset:
            _save_offset(new_offset)

    except Exception as e:
        logger.error(f"Telegram polling failed: {e}")
