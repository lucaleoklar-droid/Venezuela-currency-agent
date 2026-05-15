"""Binance P2P USDT/VES rate scraper.

Supplementary signal alongside the primary parallel scraper. Uses Binance P2P's
internal search endpoint (unofficial — may break without notice). Failure is
always non-fatal: the primary scraper continues regardless.

Fetches best bid (highest buy offer) and best ask (lowest sell offer) by taking
the median of the top valid ads on each side, filtered by merchant completion
rate. Median rather than top-1 prevents a single manipulative ad from skewing
the computed price.

tradeType semantics (from the user's perspective):
  "SELL" → user sells USDT → shows merchants who BUY → sorted price descending
           → median of these prices = best_bid
  "BUY"  → user buys USDT  → shows merchants who SELL → sorted price ascending
           → median of these prices = best_ask

USDT ≈ USD but not identical: USDT typically trades at a 0.1–0.5% premium.
mid_price is VES/USDT, used as a close proxy for VES/USD.
"""
from __future__ import annotations

import logging
import statistics
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://p2p.binance.com",
    "Referer": "https://p2p.binance.com/",
}
_TIMEOUT = 15
_ROWS = 10
_MIN_COMPLETION_RATE = 0.90
_MIN_VALID_ADS = 3        # fewer than this = don't trust the median
_MAX_SPREAD_PCT = 15.0    # bid-ask >15% is suspicious — sanity gate
_MIN_RATE = 1.0
_MAX_RATE = 1_000_000.0
_INTER_REQUEST_SLEEP = 1.0  # seconds between the two requests — be polite


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _fetch_prices(trade_type: str) -> list[float] | None:
    """POST to Binance P2P search for `trade_type` ('BUY' or 'SELL').
    Filters by completion rate. Returns list of valid prices or None on failure."""
    payload = {
        "fiat": "VES",
        "page": 1,
        "rows": _ROWS,
        "tradeType": trade_type,
        "asset": "USDT",
        "countries": [],
        "proMerchantAds": False,
        "shieldMerchantAds": False,
        "filterType": "all",
        "periods": [],
        "additionalKycVerifyFilter": 0,
        "publisherType": None,
        "payTypes": [],
        "classifies": ["mass", "profession", "fiat_trade"],
    }
    try:
        resp = requests.post(
            _URL, json=payload, headers=_HEADERS, timeout=_TIMEOUT
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Binance P2P {trade_type} request failed: {e}")
        return None

    try:
        body = resp.json()
    except ValueError as e:
        logger.warning(f"Binance P2P {trade_type} non-JSON response: {e}")
        return None

    if body.get("code") != "000000" or not body.get("success"):
        logger.warning(
            f"Binance P2P {trade_type} API error: "
            f"code={body.get('code')} msg={body.get('message')}"
        )
        return None

    prices: list[float] = []
    for item in body.get("data") or []:
        try:
            adv = item.get("adv") or {}
            advertiser = item.get("advertiser") or {}

            completion = advertiser.get("monthFinishRate")
            if completion is None or float(completion) < _MIN_COMPLETION_RATE:
                continue

            price_str = adv.get("price")
            if not price_str:
                continue
            price = float(price_str)
            if not (_MIN_RATE < price < _MAX_RATE):
                continue

            prices.append(price)
        except (TypeError, ValueError, KeyError):
            continue

    return prices or None


def fetch_p2p_rate() -> dict:
    """Fetch Binance P2P USDT/VES bid-ask and compute mid-price.

    Returns a dict with keys:
      ok                  : bool — False means data should not be stored
      timestamp           : ISO UTC string
      asset / fiat        : 'USDT' / 'VES'
      best_bid            : float | None — median of merchant buy-side prices
      best_ask            : float | None — median of merchant sell-side prices
      mid_price           : float | None
      bid_ask_spread_pct  : float | None — None if only one side available
      n_bid_ads           : int — number of valid ads used for bid
      n_ask_ads           : int — number of valid ads used for ask
      source              : 'binance_p2p'
      error               : str | None
    """
    ts = _utcnow_iso()
    result: dict = {
        "ok": False,
        "timestamp": ts,
        "asset": "USDT",
        "fiat": "VES",
        "best_bid": None,
        "best_ask": None,
        "mid_price": None,
        "bid_ask_spread_pct": None,
        "n_bid_ads": 0,
        "n_ask_ads": 0,
        "source": "binance_p2p",
        "error": None,
    }

    # SELL side first (user sells USDT → merchant buys → gives us bid prices)
    sell_prices = _fetch_prices("SELL")
    time.sleep(_INTER_REQUEST_SLEEP)
    # BUY side (user buys USDT → merchant sells → gives us ask prices)
    buy_prices = _fetch_prices("BUY")

    if sell_prices is None and buy_prices is None:
        result["error"] = "Both BUY and SELL requests failed"
        logger.warning("Binance P2P: both requests failed")
        return result

    best_bid: float | None = None
    best_ask: float | None = None

    if sell_prices and len(sell_prices) >= _MIN_VALID_ADS:
        best_bid = round(statistics.median(sell_prices), 2)
        result["n_bid_ads"] = len(sell_prices)
    elif sell_prices:
        logger.warning(
            f"Binance P2P: only {len(sell_prices)} valid bid ads "
            f"(need {_MIN_VALID_ADS}) — bid skipped"
        )

    if buy_prices and len(buy_prices) >= _MIN_VALID_ADS:
        best_ask = round(statistics.median(buy_prices), 2)
        result["n_ask_ads"] = len(buy_prices)
    elif buy_prices:
        logger.warning(
            f"Binance P2P: only {len(buy_prices)} valid ask ads "
            f"(need {_MIN_VALID_ADS}) — ask skipped"
        )

    if best_bid is None and best_ask is None:
        result["error"] = "Not enough valid ads on either side"
        return result

    # Both sides available: full quality checks
    if best_bid is not None and best_ask is not None:
        if best_bid > best_ask:
            result["error"] = (
                f"Crossed market: bid {best_bid} > ask {best_ask} — data rejected"
            )
            logger.warning(
                f"Binance P2P crossed market: bid={best_bid} ask={best_ask}"
            )
            return result

        mid_price = round((best_bid + best_ask) / 2, 2)
        spread_pct = round((best_ask - best_bid) / mid_price * 100, 3)

        if spread_pct > _MAX_SPREAD_PCT:
            result["error"] = (
                f"Bid-ask spread {spread_pct:.1f}% exceeds sanity limit "
                f"{_MAX_SPREAD_PCT}% — data rejected"
            )
            logger.warning(f"Binance P2P: suspicious spread {spread_pct:.1f}%")
            return result

        result["bid_ask_spread_pct"] = spread_pct
    else:
        # Degraded: only one side. mid = that side, spread unknown.
        mid_price = best_bid if best_bid is not None else best_ask

    result.update({
        "ok": True,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid_price,
    })
    logger.info(
        f"Binance P2P: bid={best_bid} ask={best_ask} "
        f"mid={mid_price} spread={result.get('bid_ask_spread_pct')}%"
    )
    return result


if __name__ == "__main__":
    import sys
    import os

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from dotenv import load_dotenv
    load_dotenv(os.path.join(project_root, ".env"))

    import json
    result = fetch_p2p_rate()
    print(json.dumps(result, indent=2))
