"""
Venezuela Currency Intelligence Agent — main scheduler.
Runs continuously; handles all scheduling internally.
"""
import sys
import os
import signal
import logging
import schedule
import threading
import time
from datetime import datetime, timezone, timedelta

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
from db.backup import backup_database_to_github
from scrapers.bcv_scraper import scrape_bcv
from scrapers.parallel_scraper import get_parallel_rate
from scrapers.sanity_check import validate_rate
from alerts.alert_rules import process_alerts, deliver_queued_alerts
from analysis.analyzer import run_analysis
from reports.daily_brief import generate_and_send as send_daily_brief
from reports.weekly_report import generate_report
from reports.monthly_report import generate_and_send as send_monthly_report
from reports.github_publisher import commit_weekly_report
from reports.csv_exporter import export_to_github, export_chart_to_github
from alerts.telegram_poller import poll_for_messages
from analysis.forecasters.jobs import make_daily_forecast, score_due_forecasts
from scrapers.oil_fetcher import fetch_recent as fetch_brent_recent
from scrapers.news_scanner import scan_feeds as scan_news_feeds


def _utcnow_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _utctoday():
    return datetime.now(timezone.utc).date().isoformat()


def _derive_urgency(claude_text: str, spread_pct: float | None) -> str:
    """Derive urgency from spread level + Claude's response keywords."""
    from analysis.analyzer import SPREAD_ELEVATED, SPREAD_CRITICAL, SPREAD_EMERGENCY
    text = (claude_text or "").lower()
    if spread_pct is not None and spread_pct > SPREAD_EMERGENCY:
        return "critical"
    if spread_pct is not None and spread_pct > SPREAD_CRITICAL:
        return "high"
    if any(kw in text for kw in ["urgente", "crític", "inmediat", "emergencia"]):
        return "high"
    if spread_pct is not None and spread_pct > SPREAD_ELEVATED:
        return "medium"
    if any(kw in text for kw in ["atención", "vigil", "considerar"]):
        return "medium"
    return "low"


def _last_data_age_minutes() -> float | None:
    """Minutes since the most recent rate. None if DB empty."""
    from db.db import get_latest_rate
    latest = get_latest_rate()
    if not latest or not latest.get("timestamp"):
        return None
    try:
        last = datetime.fromisoformat(latest["timestamp"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() / 60
    except (ValueError, TypeError):
        return None


_SCRAPE_TS_FILE = os.path.join(_DATA_DIR, "last_scrape_attempted.txt")


def _record_scrape_attempt():
    """Write current UTC time to a file so restarts know when we last ran.
    fsync so a SIGKILL between write and close doesn't leave the file empty."""
    with open(_SCRAPE_TS_FILE, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())
        f.flush()
        os.fsync(f.fileno())


def _last_scrape_attempt_minutes() -> float | None:
    """Minutes since last scrape attempt (from file). None if file missing."""
    try:
        with open(_SCRAPE_TS_FILE) as f:
            last = datetime.fromisoformat(f.read().strip())
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() / 60
    except (FileNotFoundError, ValueError):
        return None


def scrape_and_store():
    logger.info("--- Scrape cycle ---")
    ts = _utcnow_iso()
    _record_scrape_attempt()

    bcv_result = scrape_bcv()
    parallel_result = get_parallel_rate()

    bcv_rate = bcv_result.get("rate")
    parallel_rate = parallel_result.get("rate")

    if not bcv_rate:
        logger.warning(f"BCV scrape failed: {bcv_result.get('error')}")
    if not parallel_rate:
        logger.warning(f"Parallel scrape failed: {parallel_result.get('error')}")

    sanity_flags = []
    if parallel_rate is not None:
        ok, reason = validate_rate(parallel_rate, parallel_result.get("source", "unknown"), "parallel")
        if not ok:
            logger.warning(f"Parallel rate rejected: {reason}")
            parallel_rate = None
        elif reason:
            sanity_flags.append(f"parallel {reason}")
    if bcv_rate is not None:
        ok, reason = validate_rate(bcv_rate, "bcv.org.ve", "bcv")
        if not ok:
            logger.warning(f"BCV rate rejected: {reason}")
            bcv_rate = None
        elif reason:
            sanity_flags.append(f"bcv {reason}")

    new_data = False
    if bcv_rate or parallel_rate:
        spread_pct = None
        if bcv_rate and parallel_rate and bcv_rate > 0:
            spread_pct = round((parallel_rate - bcv_rate) / bcv_rate * 100, 2)

        from db.db import get_latest_rate
        prev = get_latest_rate()
        if prev and prev.get("bcv_rate") == bcv_rate and prev.get("parallel_rate") == parallel_rate:
            logger.info(f"Unchanged from last reading (BCV={bcv_rate}, Parallel={parallel_rate}) — skipping insert")
        else:
            notes = []
            if bcv_result.get("error"):
                notes.append(f"BCV error: {bcv_result['error']}")
            if parallel_result.get("error"):
                notes.append(f"Parallel error: {parallel_result['error']}")
            src_updated = parallel_result.get("source_updated_at")
            if src_updated:
                notes.append(f"source_updated_at={src_updated}")
            notes.extend(sanity_flags)

            insert_rate(
                timestamp=ts,
                bcv_rate=bcv_rate,
                parallel_rate=parallel_rate,
                spread_pct=spread_pct,
                source=parallel_result.get("source", "unknown"),
                notes="; ".join(notes) if notes else None,
            )
            logger.info(f"Stored: BCV={bcv_rate}, Parallel={parallel_rate}, Spread={spread_pct}%")
            new_data = True

    try:
        process_alerts()
    except Exception as e:
        logger.exception(f"Alert processing error: {e}")

    if new_data:
        try:
            export_to_github()
        except Exception as e:
            logger.exception(f"CSV export error: {e}")
    else:
        logger.info("No new data — skipping GitHub export")


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


def run_db_backup():
    try:
        backup_database_to_github()
    except Exception as e:
        logger.exception(f"DB backup error: {e}")


def run_chart_export():
    try:
        export_chart_to_github()
    except Exception as e:
        logger.exception(f"Chart export error: {e}")


def run_daily_forecast():
    try:
        make_daily_forecast()
    except Exception as e:
        logger.exception(f"Daily forecast error: {e}")


def run_score_forecasts():
    try:
        score_due_forecasts()
    except Exception as e:
        logger.exception(f"Forecast scoring error: {e}")


def run_brent_fetch():
    try:
        fetch_brent_recent()
    except Exception as e:
        logger.exception(f"Brent fetch error: {e}")


def maybe_backfill_brent():
    """One-shot bootstrap: if oil_prices is empty, backfill full history from
    FRED. Self-healing — runs at most once per fresh DB. Subsequent restarts
    skip this in O(1)."""
    conn = get_connection()
    try:
        n = conn.execute("SELECT COUNT(*) FROM oil_prices").fetchone()[0]
    except Exception as e:
        logger.warning(f"oil_prices count failed (table may not exist yet): {e}")
        return
    finally:
        conn.close()
    if n > 0:
        logger.info(f"oil_prices has {n} rows — skipping bootstrap backfill")
        return
    logger.info("oil_prices empty — running one-time Brent backfill from FRED")
    try:
        from scrapers.oil_fetcher import backfill
        result = backfill(start_date="2024-01-01")
        logger.info(f"Brent backfill result: {result}")
    except Exception as e:
        logger.exception(f"Brent backfill failed (forecasters will degrade to uniform until Brent populates): {e}")


def run_news_scan():
    try:
        scan_news_feeds()
    except Exception as e:
        logger.exception(f"News scan error: {e}")


def run_monthly_report():
    # schedule library can't do "1st of month", so we gate inside the daily job
    today = datetime.now(timezone.utc).day
    if today != 1:
        return
    logger.info("--- Monthly retrospective ---")
    try:
        send_monthly_report()
    except Exception as e:
        logger.exception(f"Monthly report error: {e}")


def setup_schedule():
    schedule.every(30).minutes.do(scrape_and_store)
    schedule.every(4).hours.do(run_analysis_job)
    schedule.every().day.at("11:00").do(run_daily_brief)  # 07:00 Venezuela
    schedule.every().day.at("11:30").do(run_monthly_report)  # fires only on day-of-month=1
    schedule.every().monday.at("12:00").do(run_weekly_report)
    schedule.every().day.at("07:00").do(run_db_backup)  # 03:00 Venezuela — quietest hour
    schedule.every(2).hours.do(run_chart_export)  # decoupled from scrape — heavy matplotlib work
    schedule.every(6).hours.do(heartbeat)
    schedule.every().day.at("10:30").do(run_brent_fetch)  # 25 min before forecast
    schedule.every(3).hours.do(run_news_scan)  # Stage 3 news signal
    schedule.every().day.at("10:55").do(run_daily_forecast)  # ~5min before daily brief
    schedule.every().hour.do(run_score_forecasts)  # mature any 24h-old forecasts

    logger.info("Schedule:")
    logger.info("  Scrape:        every 30 min (CSVs export inline, chart does NOT)")
    logger.info("  Telegram:      long-poll thread (near-instant queries)")
    logger.info("  Analysis:      every 4 hours")
    logger.info("  Daily brief:   11:00 UTC (07:00 VET)")
    logger.info("  Weekly report: Mondays 12:00 UTC")
    logger.info("  DB backup:     daily 07:00 UTC (03:00 VET)")
    logger.info("  Chart export:  every 2 hours")
    logger.info("  Heartbeat:     every 6 hours")
    logger.info("  Brent fetch:   daily 10:30 UTC (FRED DCOILBRENTEU)")
    logger.info("  News scan:     every 3 hours (FX intervention keywords)")
    logger.info("  Forecast:      daily 10:55 UTC (naive + stat + stat_v2 + stat_v3, 24h horizon)")
    logger.info("  Forecast score: every hour (matures past forecasts)")


def start_telegram_thread():
    """Background daemon that long-polls Telegram for queries (near-instant response)."""
    def _loop():
        logger.info("Telegram polling thread started")
        while True:
            try:
                poll_for_messages(long_poll=True)
            except Exception as e:
                logger.exception(f"Telegram poll error: {e}")
                time.sleep(5)
    threading.Thread(target=_loop, daemon=True, name="telegram-poller").start()


def clear_stale_alerts():
    """On startup, drop only *old* undelivered alerts (no backlog spam).
    Fresh undelivered alerts are left for deliver_queued_alerts() to attempt."""
    from alerts.alert_rules import STALE_ALERT_MAX_AGE_MIN
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
              - timedelta(minutes=STALE_ALERT_MAX_AGE_MIN)).isoformat()
    conn = get_connection()
    cur = conn.execute(
        "UPDATE alerts SET delivered=1 WHERE delivered=0 AND timestamp < ?",
        (cutoff,),
    )
    dropped = cur.rowcount
    remaining = conn.execute("SELECT COUNT(*) FROM alerts WHERE delivered=0").fetchone()[0]
    conn.commit()
    conn.close()
    if dropped:
        logger.info(f"Dropped {dropped} stale undelivered alerts from previous run")
    if remaining:
        logger.info(f"{remaining} fresh undelivered alerts will be retried")


def _install_signal_handlers(process_start):
    """Log when Railway (or anyone) sends a kill signal. Helps diagnose
    why the bot is being restarted — if we see SIGTERM in the logs,
    Railway is killing us (OOM, healthcheck, etc.) rather than us crashing."""
    def _handler(signum, frame):
        uptime = (datetime.now(timezone.utc) - process_start).total_seconds() / 60
        name = signal.Signals(signum).name
        logger.warning(f"!! Received {name} after {uptime:.1f} min uptime — shutting down")
        # Flush handlers so the message actually lands in Railway logs
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        sys.exit(0)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass  # Not available on this platform / in this thread


def _cleanup_legacy_files():
    """One-time cleanup of pre-SQLite-cooldown state files on the volume.
    Migrates any cooldowns.json entries into the alert_cooldowns table, then
    deletes the file. telegram_offset.json is left in place — still in use."""
    legacy_path = os.path.join(_DATA_DIR, "cooldowns.json")
    if not os.path.exists(legacy_path):
        return
    try:
        import json
        from db.db import set_cooldown
        with open(legacy_path) as f:
            data = json.load(f)
        migrated = 0
        if isinstance(data, dict):
            for alert_type, last_sent in data.items():
                if isinstance(last_sent, str):
                    set_cooldown(alert_type, last_sent)
                    migrated += 1
        os.unlink(legacy_path)
        logger.info(f"Legacy cooldowns.json: migrated {migrated} entries, file removed")
    except Exception as e:
        logger.warning(f"Legacy cooldowns.json cleanup failed: {e}")


def _log_memory():
    """Log RSS at startup so we can correlate restart loops with memory pressure."""
    try:
        import resource
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KB, macOS reports bytes; assume Linux on Railway
        logger.info(f"Memory at startup: ~{rss_kb / 1024:.0f} MB RSS")
    except (ImportError, AttributeError):
        pass


def main():
    process_start = datetime.now(timezone.utc)
    logger.info("=" * 50)
    logger.info("Venezuela Currency Agent starting")
    logger.info(f"DATA_DIR: {_DATA_DIR}")
    logger.info(f"Scrape-ts file: {_SCRAPE_TS_FILE}")
    logger.info(f"Process started: {process_start.isoformat()} (PID {os.getpid()})")
    logger.info("=" * 50)
    _install_signal_handlers(process_start)
    _log_memory()

    init_db()
    logger.info("Database initialized")
    _cleanup_legacy_files()
    maybe_backfill_brent()

    from db.db import get_all_cooldowns
    cds = get_all_cooldowns()
    logger.info(f"Cooldown state at startup: {len(cds)} active — {cds}")

    attempt_age = _last_scrape_attempt_minutes()
    data_age = _last_data_age_minutes()
    logger.info(
        f"Last scrape attempt: {f'{attempt_age:.1f} min ago' if attempt_age is not None else 'never'} | "
        f"last stored reading: {f'{data_age:.1f} min ago' if data_age is not None else 'none'}"
    )

    clear_stale_alerts()

    if attempt_age is None or attempt_age > 25:
        logger.info("Startup scrape: running catch-up")
        scrape_and_store()
    else:
        logger.info(f"Startup scrape skipped — last attempt {attempt_age:.1f} min ago (restart-loop guard, scheduler will pick up)")
        # Still flush any queued alerts the prior process may not have delivered
        try:
            deliver_queued_alerts()
        except Exception as e:
            logger.exception(f"Startup alert flush error: {e}")

    start_telegram_thread()
    setup_schedule()
    logger.info("Scheduler running. Press Ctrl+C to stop.")

    last_uptime_log = process_start
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            logger.exception(f"Scheduler error: {e}")
        now = datetime.now(timezone.utc)
        if (now - last_uptime_log).total_seconds() >= 600:  # every 10 min
            uptime_min = (now - process_start).total_seconds() / 60
            logger.info(f"Alive — uptime {uptime_min:.1f} min")
            last_uptime_log = now
        time.sleep(30)


if __name__ == "__main__":
    main()
