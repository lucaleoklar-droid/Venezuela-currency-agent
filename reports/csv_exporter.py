"""Exports rate data and alerts as CSV files committed to GitHub."""
import os
import logging
import base64
import csv
import io
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from db.db import get_connection

load_dotenv()
logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


def _headers():
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return None
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_repo():
    repo = os.getenv("GITHUB_REPO", "")
    if "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    return owner, name


def _build_rates_csv() -> str:
    """Build CSV of all rates in the database."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT timestamp, bcv_rate, parallel_rate, spread_pct, source "
        "FROM rates ORDER BY timestamp DESC"
    ).fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp_utc", "bcv_rate", "parallel_rate", "spread_pct", "source"])
    for r in rows:
        writer.writerow([r["timestamp"], r["bcv_rate"], r["parallel_rate"],
                         r["spread_pct"], r["source"]])
    return buf.getvalue()


def _build_alerts_csv() -> str:
    """Build CSV of all alerts sent."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT timestamp, alert_type, message, delivered, bcv_rate, parallel_rate, spread_pct "
        "FROM alerts ORDER BY timestamp DESC"
    ).fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp_utc", "alert_type", "message", "delivered",
                     "bcv_rate", "parallel_rate", "spread_pct"])
    for r in rows:
        writer.writerow([r["timestamp"], r["alert_type"],
                         (r["message"] or "").replace("\n", " "),
                         r["delivered"], r["bcv_rate"],
                         r["parallel_rate"], r["spread_pct"]])
    return buf.getvalue()


def _commit_file(path: str, content: str, message: str) -> bool:
    """Create or update a file in the GitHub repo."""
    headers = _headers()
    repo = _get_repo()
    if not headers or not repo:
        logger.info("GITHUB_TOKEN or GITHUB_REPO not set — skipping CSV export")
        return False

    owner, name = repo
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    try:
        url = f"{GITHUB_API}/repos/{owner}/{name}/contents/{path}"

        # Get existing SHA if file exists
        resp = requests.get(url, headers=headers, timeout=10)
        sha = resp.json().get("sha") if resp.status_code == 200 else None

        payload = {
            "message": message,
            "content": encoded,
            "committer": {
                "name": "Venezuela Currency Agent",
                "email": "agent@venezuela-currency.bot",
            },
        }
        if sha:
            payload["sha"] = sha

        resp = requests.put(url, headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"GitHub CSV commit failed for {path}: {e}")
        return False


def export_to_github() -> bool:
    """Export rates and alerts CSVs and commit to GitHub."""
    if not os.getenv("GITHUB_TOKEN"):
        return False

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rates_csv = _build_rates_csv()
    alerts_csv = _build_alerts_csv()

    rates_ok = _commit_file("data/rates.csv", rates_csv, f"Data update {ts}")
    alerts_ok = _commit_file("data/alerts.csv", alerts_csv, f"Data update {ts}")

    if rates_ok and alerts_ok:
        logger.info("CSV export complete: rates.csv + alerts.csv")
    return rates_ok and alerts_ok
