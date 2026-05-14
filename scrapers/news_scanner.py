"""Venezuelan FX-news RSS keyword scanner.

Stage 3 operational signal. Polls a small basket of Venezuelan financial-news
RSS feeds, keyword-matches article titles against an FX-intervention vocabulary,
and writes matches to the `news_signals` table.

The downstream feature is `news_intervention_count_7d`: a count of matched
headlines in the past 7 days. Sparse, interpretable, robust to single-source
noise. Keywords are deliberately narrow to keep false-positive rate low.

Sources chosen for: Venezuelan financial focus, public RSS, Spanish-language,
independent reporting (so they're not just echoing BCV's press releases).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import feedparser
import requests

from db.db import insert_news_signal

logger = logging.getLogger(__name__)

# Feed list. Each entry: (source_tag, url).
# Tight basket on purpose — broader = noisier, and we'd rather have 3 reliable
# signals than 10 chatty ones drowning the keyword signal.
# Original picks (bancaynegocios, talcualdigital) were dropped on 2026-05-14:
# bancaynegocios actively blocks non-browser TCP connections, talcualdigital
# doesn't serve RSS. Replaced with elnacional and runrun.es — both confirmed
# live, Venezuela-focused, and cover politics + economics.
FEEDS: tuple[tuple[str, str], ...] = (
    ("efectococuyo", "https://efectococuyo.com/feed/"),
    ("elnacional", "https://www.elnacional.com/feed/"),
    ("runrunes", "https://runrun.es/feed/"),
)

# Keywords (Spanish, narrow). Matched case-insensitively against titles only —
# bodies have too much general macro text and would false-positive constantly.
# Word-boundary regex so "subasta" doesn't match "subastalo".
KEYWORDS: tuple[str, ...] = (
    "subasta de divisas",
    "intervención cambiaria",
    "ajuste cambiario",
    "tipo de cambio oficial",
    "bcv inyecta",
    "bcv interviene",
    "dicom",
    "sicad",
    "devaluación",
    "control cambiario",
)

REQUEST_TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_KEYWORD_PATTERNS = tuple(
    (kw, re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE))
    for kw in KEYWORDS
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _match_keywords(title: str) -> list[str]:
    if not title:
        return []
    return [kw for kw, pat in _KEYWORD_PATTERNS if pat.search(title)]


def _parse_entry_time(entry) -> str | None:
    """Extract a usable ISO timestamp from a feedparser entry.
    Falls back to None if no parseable date is present."""
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None) or entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).replace(tzinfo=None).isoformat()
            except (TypeError, ValueError):
                continue
    # String fallbacks — let feedparser have already tried; just use now() as last resort
    return None


def _fetch_feed(url: str) -> bytes | None:
    """Pull the raw feed bytes with a real UA. feedparser's built-in fetcher
    sometimes gets blocked by WAFs that 403 the default UA."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as e:
        logger.warning(f"Feed fetch failed for {url}: {e}")
        return None


def scan_feeds() -> dict:
    """Pull all configured feeds, match keywords on titles, write new matches.
    Idempotent via UNIQUE(url) — re-scanning the same feed won't duplicate."""
    fetched_at = _utcnow_iso()
    total_entries = 0
    matched_inserted = 0
    matched_duplicate = 0
    feed_results: dict[str, dict] = {}

    for source_tag, url in FEEDS:
        raw = _fetch_feed(url)
        if raw is None:
            feed_results[source_tag] = {"ok": False, "entries": 0, "matched": 0}
            continue

        parsed = feedparser.parse(raw)
        entries = parsed.get("entries", [])
        total_entries += len(entries)

        feed_matched = 0
        for entry in entries:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue
            matches = _match_keywords(title)
            if not matches:
                continue
            published_at = _parse_entry_time(entry) or fetched_at
            inserted = insert_news_signal(
                published_at=published_at,
                source=source_tag,
                url=link,
                title=title,
                matched_keywords_json=json.dumps(matches, ensure_ascii=False),
                fetched_at=fetched_at,
            )
            if inserted:
                matched_inserted += 1
                feed_matched += 1
                logger.info(f"News match [{source_tag}] {matches}: {title[:80]}")
            else:
                matched_duplicate += 1

        feed_results[source_tag] = {
            "ok": True,
            "entries": len(entries),
            "matched": feed_matched,
        }

        # Be polite — small delay between feed hits
        time.sleep(0.5)

    logger.info(
        f"News scan: feeds={len(FEEDS)} entries={total_entries} "
        f"new_matches={matched_inserted} dup_matches={matched_duplicate}"
    )
    return {
        "ok": True,
        "total_entries": total_entries,
        "matched_inserted": matched_inserted,
        "matched_duplicate": matched_duplicate,
        "feeds": feed_results,
    }


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from dotenv import load_dotenv
    load_dotenv(os.path.join(project_root, ".env"))

    print(scan_feeds())
