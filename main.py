"""
Venezuela Currency Intelligence Agent — main scheduler.
Run this script continuously (or as a Windows service).
It handles all scheduling internally.
"""
import sys
import os
import logging
import schedule
import time
from datetime import datetime, timezone

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("agent.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

from db.db import init_db, insert_rate
from scrapers.bcv_scraper import scrape_bcv
from scrapers.parallel_scraper import get_parallel_rate
from alerts.alert_rules import process_alerts
from analysis.analyzer import run_analysis
from db.db import upsert_daily_analysis
from reports.daily_brief import generate_and_send as send_daily_brief
from reports.weekly_report import generate_report
from reports.github_publisher import commit_weekly_report


def scrape_and_store():
    logger.info("--- Scrape cycle ---")
    ts = datetime.utcnow().isoformat()

    bcv_result = scrape_bcv()
    parallel_result = get_parallel_rate()

    bcv_rate = bcv_result.get("rate")
    parallel_rate = parallel_result.get("rate")

    if not bcv_rate:
        logger.warning(f"BCV scrape failed: {bcv_result.get('error')}")
    if not parallel_rate:
        logger.warning(f"Parallel scrape failed: {parallel_result.get('error')}")

    if bcv_rate or parallel_rate:
        spread_pct = None
        if bcv_rate and parallel_rate and bcv_rate > 0:
            spread_pct = round((parallel_rate - bcv_rate) / bcv_rate * 100, 2)

        notes = []
        if bcv_result.get("error"):
            notes.append(f"BCV error: {bcv_result['error']}")
        if parallel_result.get("error"):
            notes.append(f"Parallel error: {parallel_result['error']}")

        insert_rate(
            timestamp=ts,
            bcv_rate=bcv_rate,
            parallel_rate=parallel_rate,
            spread_pct=spread_pct,
            source=parallel_result.get("source", "unknown"),
            notes="; ".join(notes) if notes else None,
        )
        logger.info(f"Stored: BCV={bcv_rate}, Parallel={parallel_rate}, Spread={spread_pct}%")

    # Check for alerts after every scrape
    try:
        process_alerts()
    except Exception as e:
        logger.error(f"Alert processing error: {e}")


def run_analysis_job():
    logger.info("--- Analysis cycle ---")
    try:
        result = run_analysis()
        if "error" not in result:
            today = datetime.utcnow().date().isoformat()
            upsert_daily_analysis(
                date_str=today,
                claude_summary=result["claude_response"],
                recommendation=result["claude_response"],
                urgency_level="unknown",
                raw_response=str(result),
            )
            logger.info("Analysis stored")
    except Exception as e:
        logger.error(f"Analysis job error: {e}")


def run_daily_brief():
    logger.info("--- Daily brief ---")
    try:
        send_daily_brief()
    except Exception as e:
        logger.error(f"Daily brief error: {e}")


def run_weekly_report():
    logger.info("--- Weekly report ---")
    try:
        report = generate_report()
        # Save locally
        date_str = datetime.now().strftime("%Y-%m-%d")
        local_path = os.path.join("briefs", f"weekly-{date_str}.md")
        os.makedirs("briefs", exist_ok=True)
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info(f"Weekly report saved locally: {local_path}")
        # Commit to GitHub
        commit_weekly_report(report)
    except Exception as e:
        logger.error(f"Weekly report error: {e}")


def setup_schedule():
    # Scrape every 30 minutes
    schedule.every(30).minutes.do(scrape_and_store)

    # Analysis every 4 hours
    schedule.every(4).hours.do(run_analysis_job)

    # Daily brief at 11:00 UTC = 07:00 Venezuela time (UTC-4)
    schedule.every().day.at("11:00").do(run_daily_brief)

    # Weekly report every Monday at 12:00 UTC
    schedule.every().monday.at("12:00").do(run_weekly_report)

    logger.info("Schedule configured:")
    logger.info("  - Scrape: every 30 minutes")
    logger.info("  - Analysis: every 4 hours")
    logger.info("  - Daily brief: 11:00 UTC (07:00 VET)")
    logger.info("  - Weekly report: Mondays 12:00 UTC")


def main():
    logger.info("Venezuela Currency Agent starting...")
    init_db()
    logger.info("Database initialized")

    # Run an immediate scrape on startup
    scrape_and_store()

    setup_schedule()
    logger.info("Scheduler running. Press Ctrl+C to stop.")

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
