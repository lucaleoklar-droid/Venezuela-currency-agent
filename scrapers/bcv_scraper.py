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
}

# Plausible VES/USD range (covers historic and accounts for hyperinflation)
MIN_RATE = 1.0
MAX_RATE = 1_000_000.0


def parse_rate(text: str) -> float | None:
    if not text:
        return None
    cleaned = text.strip().replace("\xa0", "").replace(" ", "")
    # Spanish decimal style: comma as decimal, period as thousands. Handle both.
    # If both present, comma is decimal (Spanish): "1.234,56" → "1234.56"
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


def _fetch_bcv(retries: int = 2) -> str | None:
    """Fetches BCV homepage with retry on failure."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                "https://www.bcv.org.ve/",
                headers=HEADERS,
                timeout=20,
                verify=False,
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    logger.error(f"BCV fetch failed after retries: {last_err}")
    return None


def scrape_bcv() -> dict:
    """Scrapes the BCV official VES/USD rate from bcv.org.ve."""
    result = {
        "rate": None,
        "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "source": "bcv.org.ve",
        "error": None,
    }

    html = _fetch_bcv()
    if html is None:
        result["error"] = "Failed to fetch BCV homepage"
        return result

    try:
        soup = BeautifulSoup(html, "html.parser")

        # Strategy 1: div with id="dolar" (most reliable)
        dolar_div = soup.find("div", id="dolar")
        if dolar_div:
            strong = dolar_div.find("strong")
            if strong:
                rate = parse_rate(strong.get_text())
                if rate:
                    result["rate"] = rate
                    logger.info(f"BCV rate (strategy 1, #dolar): {rate}")
                    return result

        # Strategy 2: any tag near USD/Dólar text
        for tag in soup.find_all(["strong", "span", "td"]):
            text = tag.get_text(strip=True)
            rate = parse_rate(text)
            if rate and MIN_RATE < rate < MAX_RATE:
                parent_text = tag.parent.get_text().lower() if tag.parent else ""
                if any(kw in parent_text for kw in ["usd", "dólar", "dolar", "$"]):
                    result["rate"] = rate
                    logger.info(f"BCV rate (strategy 2, USD-adjacent): {rate}")
                    return result

        # Strategy 3: regex fallback over full text (allow up to 7 digits before decimal)
        text = soup.get_text()
        matches = re.findall(r"\b(\d{1,7}[.,]\d{2,6})\b", text)
        for m in matches:
            rate = parse_rate(m)
            if rate and MIN_RATE < rate < MAX_RATE:
                result["rate"] = rate
                logger.info(f"BCV rate (strategy 3, regex): {rate}")
                return result

        result["error"] = "Could not find USD rate in page"
        logger.warning("BCV scraper: rate not found")

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"BCV scraper unexpected error: {e}")

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(scrape_bcv())
