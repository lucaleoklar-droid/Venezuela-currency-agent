import os
import logging
import base64
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


def _headers():
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN not set")
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_repo() -> tuple[str, str]:
    repo = os.getenv("GITHUB_REPO", "")
    if "/" not in repo:
        raise ValueError("GITHUB_REPO must be in format 'owner/repo'")
    owner, name = repo.split("/", 1)
    return owner, name


def commit_weekly_report(report_content: str) -> bool:
    try:
        owner, repo = _get_repo()
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"briefs/weekly-{date_str}.md"
        encoded = base64.b64encode(report_content.encode("utf-8")).decode("utf-8")

        # Check if file already exists (need its SHA to update)
        url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{filename}"
        resp = requests.get(url, headers=_headers(), timeout=10)
        sha = resp.json().get("sha") if resp.status_code == 200 else None

        payload = {
            "message": f"Weekly currency report {date_str}",
            "content": encoded,
            "committer": {
                "name": "Venezuela Currency Agent",
                "email": "agent@venezuela-currency.bot",
            },
        }
        if sha:
            payload["sha"] = sha

        resp = requests.put(url, headers=_headers(), json=payload, timeout=15)
        resp.raise_for_status()
        logger.info(f"Weekly report committed to GitHub: {filename}")
        return True

    except Exception as e:
        logger.error(f"GitHub publish failed: {e}")
        return False
