"""Daily off-volume backup of the SQLite DB to the GitHub repo."""
import os
import sqlite3
import tempfile
import logging
from datetime import datetime, timezone
from db.db import DB_PATH
from reports.csv_exporter import _commit_file

logger = logging.getLogger(__name__)


def backup_database_to_github() -> bool:
    if not os.getenv("GITHUB_TOKEN") or not os.getenv("GITHUB_REPO"):
        logger.error("GITHUB_TOKEN or GITHUB_REPO not set; skipping DB backup")
        return False

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = f.name

        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp_path)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
            src.close()

        with open(tmp_path, "rb") as f:
            blob = f.read()

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        ok = _commit_file(
            "backups/venezuela_currency.db",
            blob,
            f"DB backup {ts}",
        )
        if ok:
            logger.info(f"DB backup committed to GitHub ({len(blob)} bytes)")
        else:
            logger.error("DB backup commit failed")
        return ok

    except Exception as e:
        logger.error(f"DB backup failed: {e}")
        return False

    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError as e:
                logger.warning(f"Could not delete temp backup file {tmp_path}: {e}")
