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
}


def parse_rate(text: str) -> float | None:
    if not text:
        return None
    cleaned = text.strip().replace(",", ".").replace("\xa0", "").replace(" ", "")
    # Remove anything that's not a digit or decimal point
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    # If multiple dots, keep only the last one as decimal
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        val = float(cleaned)
        # Sanity check: BCV rate should be between 1 and 10000
        if 1 < val < 10000:
            return round(val, 4)
    except ValueError:
        pass
    return None


def scrape_bcv() -> dict:
    """
    Scrapes the BCV official VES/USD rate from bcv.org.ve.
    Returns dict with 'rate', 'timestamp', 'source', 'error'.
    """
    result = {"rate": None, "timestamp": datetime.utcnow().isoformat(), "source": "bcv.org.ve", "error": None}

    try:
        resp = requests.get("https://www.bcv.org.ve/", headers=HEADERS, timeout=20, verify=False)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Strategy 1: div with id="dolar"
        dolar_div = soup.find("div", id="dolar")
        if dolar_div:
            strong = dolar_div.find("strong")
            if strong:
                rate = parse_rate(strong.get_text())
                if rate:
                    result["rate"] = rate
                    logger.info(f"BCV rate scraped (strategy 1): {rate}")
                    return result

        # Strategy 2: look for any strong/span near "USD" or "Dólar"
        for tag in soup.find_all(["strong", "span", "td"]):
            text = tag.get_text(strip=True)
            rate = parse_rate(text)
            if rate and 10 < rate < 1000:  # tighter range for plausibility
                # Check if USD/dolar is nearby
                parent_text = tag.parent.get_text().lower() if tag.parent else ""
                if any(kw in parent_text for kw in ["usd", "dólar", "dolar", "$"]):
                    result["rate"] = rate
                    logger.info(f"BCV rate scraped (strategy 2): {rate}")
                    return result

        # Strategy 3: regex over full page text
        text = soup.get_text()
        # Look for patterns like "38.50" or "38,50" near "USD" or "dólar"
        matches = re.findall(r'\b(\d{2,3}[.,]\d{2,4})\b', text)
        for m in matches:
            rate = parse_rate(m)
            if rate and 10 < rate < 1000:
                result["rate"] = rate
                logger.info(f"BCV rate scraped (strategy 3, regex): {rate}")
                return result

        result["error"] = "Could not find USD rate in page"
        logger.warning("BCV scraper: could not find rate in page")

    except requests.RequestException as e:
        result["error"] = str(e)
        logger.error(f"BCV scraper request failed: {e}")
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"BCV scraper unexpected error: {e}")

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = scrape_bcv()
    print(r)
