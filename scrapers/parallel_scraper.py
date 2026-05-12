import requests
from bs4 import BeautifulSoup
import re
import logging
import time
from datetime import datetime, timezone
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-VE,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.google.com/",
}

MIN_RATE = 1.0
MAX_RATE = 1_000_000.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def parse_rate(text: str) -> float | None:
    if not text:
        return None
    cleaned = text.strip().replace("\xa0", "").replace(" ", "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        val = float(cleaned)
        if MIN_RATE < val < MAX_RATE:
            return round(val, 4)
    except ValueError:
        pass
    return None


def _request_with_retry(url, retries=2, **kwargs):
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15, verify=False, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise last_err


def fetch_dolarapi_ve() -> dict:
    result = {"rate": None, "timestamp": _now_iso(), "source": "ve.dolarapi.com",
              "error": None, "source_updated_at": None}
    try:
        resp = _request_with_retry("https://ve.dolarapi.com/v1/dolares")
        data = resp.json()
        for item in data:
            nombre = item.get("nombre", "").lower()
            if any(kw in nombre for kw in ["paralelo", "parallel", "unofficial"]):
                price = item.get("promedio") or item.get("venta")
                if price:
                    rate = round(float(price), 4)
                    if MIN_RATE < rate < MAX_RATE:
                        result["rate"] = rate
                        result["source_updated_at"] = item.get("fechaActualizacion")
                        logger.info(f"ve.dolarapi.com parallel: {rate} (source updated {result['source_updated_at']})")
                        return result
        for item in data:
            fuente = item.get("fuente", "").lower()
            if "bcv" not in fuente:
                price = item.get("promedio") or item.get("venta")
                if price:
                    rate = round(float(price), 4)
                    if MIN_RATE < rate < MAX_RATE:
                        result["rate"] = rate
                        result["source_updated_at"] = item.get("fechaActualizacion")
                        logger.info(f"ve.dolarapi.com (first non-BCV): {rate}")
                        return result
        result["error"] = "No parallel rate found"
    except Exception as e:
        result["error"] = str(e)
        logger.warning(f"ve.dolarapi.com failed: {e}")
    return result


def scrape_dolartoday() -> dict:
    result = {"rate": None, "timestamp": _now_iso(), "source": "dolartoday.com", "error": None}
    try:
        resp = _request_with_retry("https://dolartoday.com/")
        soup = BeautifulSoup(resp.text, "html.parser")
        for selector in [
            ("div", {"class": re.compile(r"rate|precio|dolar|price|value", re.I)}),
            ("span", {"class": re.compile(r"rate|precio|dolar|price|value", re.I)}),
            ("td", {}),
        ]:
            tag_name, attrs = selector
            elements = soup.find_all(tag_name, attrs) if attrs else soup.find_all(tag_name)
            for el in elements:
                rate = parse_rate(el.get_text(strip=True))
                if rate and MIN_RATE < rate < MAX_RATE:
                    result["rate"] = rate
                    logger.info(f"dolartoday: {rate}")
                    return result
        result["error"] = "Rate not found in page"
    except Exception as e:
        result["error"] = str(e)
        logger.warning(f"dolartoday failed: {e}")
    return result


def get_parallel_rate() -> dict:
    """Try API sources first (more reliable), then scraped sources."""
    for fn in [fetch_dolarapi_ve, scrape_dolartoday]:
        result = fn()
        if result["rate"]:
            return result

    return {
        "rate": None,
        "timestamp": _now_iso(),
        "source": "all",
        "error": "All parallel rate sources failed",
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(get_parallel_rate())
