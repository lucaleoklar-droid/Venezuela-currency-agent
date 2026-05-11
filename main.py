"""
Venezuela Currency Intelligence Agent — main scheduler.
Runs continuously; handles all scheduling internally.
"""
import sys
import os
import logging
import schedule
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

# Use DATA_DIR for log file so it survives Railway restarts
_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
os.makedirs(_DATA_DIR, exist_ok=True)
_LOG_PATH = os.path.join(_DATA_DIR, "agent.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_PATH, encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

from db.db import init_db, insert_rate, upsert_daily_analysis, get_connection
from scrapers.bcv_scraper import scrape_bcv
from scrapers.parallel_scraper import get_parallel_rate
from alerts.alert_rules import process_alerts
from analysis.analyzer import run_analysis
from reports.daily_brief import generate_and_send as send_daily_brief
from reports.weekly_report import generate_report
from reports.github_publisher import commit_weekly_report
from reports.csv_exporter import export_to_github


def _utcnow_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _utctoday():
    return datetime.now(timezone.utc).date().isoformat()


def _derive_urgency(claude_text: str, spread_pct: float | None) -> str:
    """Derive urgency from spread level + Claude's response keywords."""
    text = (claude_text or "").lower()
    if spread_pct is not None and spread_pct > 75:
        return "critical"
    if spread_pct is not None and spread_pct > 50:
        return "high"
    if any(kw in text for kw in ["urgente", "crític", "inmediat", "emergencia"]):
        return "high"
    if spread_pct is not None and spread_pct > 35:
        return "medium"
    if any(kw in text for kw in ["atención", "vigil", "considerar"]):
        return "medium"
    return "low"


def scrape_and_store():
    logger.info("--- Scrape cycle ---")
    ts = _utcnow_iso()

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

    try:
        process_alerts()
    except Exception as e:
        logger.exception(f"Alert processing error: {e}")


def run_analysis_job():
    logger.info("--- Analysis cycle ---")
    try:
        result = run_analysis()
        if "error" not in result:
            urgency = _derive_urgency(result.get("claude_response"), result.get("spread_pct"))
            upsert_daily_analysis(
                date_str=_utctoday(),
                claude_summary=result["claude_response"],
                recommendation=result["claude_response"],
                urgency_level=urgency,
                raw_response=str(result),
            )
            logger.info(f"Analysis stored (urgency={urgency})")
    except Exception as e:
        logger.exception(f"Analysis job error: {e}")


def run_daily_brief():
    logger.info("--- Daily brief ---")
    try:
        send_daily_brief()
    except Exception as e:
        logger.exception(f"Daily brief error: {e}")


def run_weekly_report():
    logger.info("--- Weekly report ---")
    try:
        report = generate_report()
        date_str = datetime.now().strftime("%Y-%m-%d")
        briefs_dir = os.path.join(_DATA_DIR, "briefs")
        os.makedirs(briefs_dir, exist_ok=True)
        local_path = os.path.join(briefs_dir, f"weekly-{date_str}.md")
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info(f"Weekly report saved: {local_path}")
        commit_weekly_report(report)
    except Exception as e:
        logger.exception(f"Weekly report error: {e}")


def heartbeat():
    """Periodic health log — confirms the agent is alive."""
    from scrapers.scraper_health import get_health_report
    h = get_health_report()
    logger.info(f"Heartbeat: data_freshness={h['data_freshness']}, bcv={h['bcv_freshness']}")


def run_csv_export():
    """Export rates and alerts to GitHub as CSV files."""
    try:
        export_to_github()
    except Exception as e:
        logger.exception(f"CSV export error: {e}")


def setup_schedule():
    schedule.every(30).minutes.do(scrape_and_store)
    schedule.every(4).hours.do(run_analysis_job)
    schedule.every().day.at("11:00").do(run_daily_brief)  # 07:00 Venezuela
    schedule.every().monday.at("12:00").do(run_weekly_report)
    schedule.every(6).hours.do(heartbeat)
    schedule.every(1).hours.do(run_csv_export)

    logger.info("Schedule:")
    logger.info("  Scrape:        every 30 min")
    logger.info("  CSV export:    every hour")
    logger.info("  Analysis:      every 4 hours")
    logger.info("  Daily brief:   11:00 UTC (07:00 VET)")
    logger.info("  Weekly report: Mondays 12:00 UTC")
    logger.info("  Heartbeat:     every 6 hours")


def clear_stale_alerts():
    """On startup, mark any undelivered alerts as delivered so old backlogs don't spam."""
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM alerts WHERE delivered=0").fetchone()[0]
    if count:
        conn.execute("UPDATE alerts SET delivered=1 WHERE delivered=0")
        conn.commit()
        logger.info(f"Cleared {count} stale undelivered alerts from previous run")
    conn.close()


def main():
    logger.info("=" * 50)
    logger.info("Venezuela Currency Agent starting")
    logger.info(f"DATA_DIR: {_DATA_DIR}")
    logger.info("=" * 50)

    init_db()
    logger.info("Database initialized")

    clear_stale_alerts()
    scrape_and_store()
    setup_schedule()
    logger.info("Scheduler running. Press Ctrl+C to stop.")

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            logger.exception(f"Scheduler error: {e}")
        time.sleep(30)


if __name__ == "__main__":
    main()
