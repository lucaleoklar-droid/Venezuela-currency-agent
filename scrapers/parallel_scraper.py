import requests
from bs4 import BeautifulSoup
import re
import logging
from datetime import datetime
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


def parse_rate(text: str) -> float | None:
    if not text:
        return None
    cleaned = text.strip().replace(",", ".").replace("\xa0", "").replace(" ", "")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        val = float(cleaned)
        if 1 < val < 10000:
            return round(val, 4)
    except ValueError:
        pass
    return None


def fetch_pydolarve() -> dict:
    """Uses pydolarve.org — a public JSON API for Venezuela parallel rate."""
    result = {"rate": None, "timestamp": datetime.utcnow().isoformat(),
              "source": "pydolarve.org", "error": None}
    try:
        resp = requests.get(
            "https://pydolarve.org/api/v1/dollar?page=criptodolar",
            headers=HEADERS, timeout=15, verify=False
        )
        resp.raise_for_status()
        data = resp.json()
        # Response has monitors dict; 'enparalelovzla' is the parallel rate
        monitors = data.get("monitors", {})
        for key in ["enparalelovzla", "paralelo", "dolartoday"]:
            monitor = monitors.get(key, {})
            price = monitor.get("price")
            if price:
                result["rate"] = round(float(price), 4)
                logger.info(f"pydolarve.org rate ({key}): {result['rate']}")
                return result
        result["error"] = "Rate key not found in response"
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"pydolarve.org request failed: {e}")
    return result


def fetch_dolarapi_ve() -> dict:
    """Uses ve.dolarapi.com — another public JSON API for Venezuela rates."""
    result = {"rate": None, "timestamp": datetime.utcnow().isoformat(),
              "source": "ve.dolarapi.com", "error": None}
    try:
        resp = requests.get(
            "https://ve.dolarapi.com/v1/dolares",
            headers=HEADERS, timeout=15, verify=False
        )
        resp.raise_for_status()
        data = resp.json()
        # Returns list of rate objects; find the parallel one
        for item in data:
            nombre = item.get("nombre", "").lower()
            if any(kw in nombre for kw in ["paralelo", "parallel", "unofficial"]):
                price = item.get("promedio") or item.get("venta")
                if price:
                    result["rate"] = round(float(price), 4)
                    logger.info(f"ve.dolarapi.com parallel rate: {result['rate']}")
                    return result
        # Fallback: first item that's not BCV
        for item in data:
            fuente = item.get("fuente", "").lower()
            if "bcv" not in fuente:
                price = item.get("promedio") or item.get("venta")
                if price:
                    result["rate"] = round(float(price), 4)
                    logger.info(f"ve.dolarapi.com rate (first non-BCV): {result['rate']}")
                    return result
        result["error"] = "No parallel rate found in response"
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"ve.dolarapi.com request failed: {e}")
    return result


def scrape_monitor_dolar() -> dict:
    """Scrapes monitordolarvenezuela.com for parallel USD rate."""
    result = {"rate": None, "timestamp": datetime.utcnow().isoformat(),
              "source": "monitordolarvenezuela.com", "error": None}

    try:
        resp = requests.get("https://monitordolarvenezuela.com/", headers=HEADERS, timeout=20, verify=False)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Strategy 1: look for the main rate display element
        # Monitor Dolar typically shows rate in a prominent heading/div
        for selector in [
            ("div", {"class": re.compile(r"rate|precio|dolar|price", re.I)}),
            ("p", {"class": re.compile(r"rate|precio|dolar|price", re.I)}),
            ("h1", {}), ("h2", {}), ("h3", {}),
        ]:
            tag_name, attrs = selector
            elements = soup.find_all(tag_name, attrs) if attrs else soup.find_all(tag_name)
            for el in elements:
                text = el.get_text(strip=True)
                rate = parse_rate(text)
                if rate and 10 < rate < 1000:
                    result["rate"] = rate
                    logger.info(f"Monitor Dólar rate (strategy 1): {rate}")
                    return result

        # Strategy 2: regex over page text
        text = soup.get_text()
        # Find rates in plausible range for parallel dollar
        matches = re.findall(r'\b(\d{2,3}[.,]\d{2,4})\b', text)
        candidates = []
        for m in matches:
            rate = parse_rate(m)
            if rate and 10 < rate < 1000:
                candidates.append(rate)
        if candidates:
            # Take the most common value or median
            candidates.sort()
            result["rate"] = candidates[len(candidates) // 2]
            logger.info(f"Monitor Dólar rate (strategy 2, regex median): {result['rate']}")
            return result

        result["error"] = "Could not find rate in page"
        logger.warning("Monitor Dólar scraper: could not find rate")

    except requests.RequestException as e:
        result["error"] = str(e)
        logger.error(f"Monitor Dólar request failed: {e}")
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Monitor Dólar unexpected error: {e}")

    return result


def scrape_dolartoday() -> dict:
    """Scrapes dolartoday.com as fallback parallel rate source."""
    result = {"rate": None, "timestamp": datetime.utcnow().isoformat(),
              "source": "dolartoday.com", "error": None}

    try:
        resp = requests.get("https://dolartoday.com/", headers=HEADERS, timeout=20, verify=False)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # DolarToday shows rates in a prominent table/div
        for selector in [
            ("div", {"class": re.compile(r"rate|precio|dolar|price|value", re.I)}),
            ("span", {"class": re.compile(r"rate|precio|dolar|price|value", re.I)}),
            ("td", {}),
        ]:
            tag_name, attrs = selector
            elements = soup.find_all(tag_name, attrs) if attrs else soup.find_all(tag_name)
            for el in elements:
                text = el.get_text(strip=True)
                rate = parse_rate(text)
                if rate and 10 < rate < 1000:
                    result["rate"] = rate
                    logger.info(f"DolarToday rate: {rate}")
                    return result

        result["error"] = "Could not find rate in page"

    except requests.RequestException as e:
        result["error"] = str(e)
        logger.error(f"DolarToday request failed: {e}")
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"DolarToday unexpected error: {e}")

    return result


def get_parallel_rate() -> dict:
    """
    Gets parallel rate. Tries API sources first (more reliable), then scrapers.
    """
    # Try API sources first
    for fn in [fetch_pydolarve, fetch_dolarapi_ve, scrape_monitor_dolar, scrape_dolartoday]:
        result = fn()
        if result["rate"]:
            return result
        logger.warning(f"{result['source']} failed: {result['error']}")

    return {"rate": None, "timestamp": datetime.utcnow().isoformat(),
            "source": "all", "error": "All parallel rate sources failed"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = get_parallel_rate()
    print(r)
