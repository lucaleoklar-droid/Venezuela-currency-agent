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


_COMMITTER = {"name": "Venezuela Currency Agent",
              "email": "agent@venezuela-currency.bot"}


def _default_branch(owner: str, repo: str) -> str:
    resp = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}",
                        headers=_headers(), timeout=10)
    resp.raise_for_status()
    return resp.json().get("default_branch", "master")


def commit_files(files: list[tuple[str, bytes]], message: str) -> bool:
    """Commit multiple files as ONE atomic git commit via the Git Data API.

    Unlike the Contents API (one commit per file), this builds a single tree
    and commit for all `files`. Crucially, if the resulting tree is identical
    to the current one (no file actually changed), it commits NOTHING — this
    is what stops the repo filling with no-op "update" commits.

    `files`: list of (repo_path, content_bytes). Returns True on success or
    on a legitimate no-op; False only on error.
    """
    if not files:
        return True
    try:
        owner, repo = _get_repo()
    except ValueError as e:
        logger.error(f"GitHub commit skipped: {e}")
        return False
    try:
        branch = _default_branch(owner, repo)
        base = f"{GITHUB_API}/repos/{owner}/{repo}"

        ref = requests.get(f"{base}/git/ref/heads/{branch}",
                           headers=_headers(), timeout=10)
        ref.raise_for_status()
        base_commit_sha = ref.json()["object"]["sha"]

        base_commit = requests.get(f"{base}/git/commits/{base_commit_sha}",
                                   headers=_headers(), timeout=10)
        base_commit.raise_for_status()
        base_tree_sha = base_commit.json()["tree"]["sha"]

        tree_entries = []
        for path, content in files:
            blob = requests.post(
                f"{base}/git/blobs", headers=_headers(), timeout=15,
                json={"content": base64.b64encode(content).decode("utf-8"),
                      "encoding": "base64"},
            )
            blob.raise_for_status()
            tree_entries.append({
                "path": path, "mode": "100644", "type": "blob",
                "sha": blob.json()["sha"],
            })

        tree = requests.post(
            f"{base}/git/trees", headers=_headers(), timeout=15,
            json={"base_tree": base_tree_sha, "tree": tree_entries},
        )
        tree.raise_for_status()
        new_tree_sha = tree.json()["sha"]

        # Nothing actually changed — skip the commit entirely.
        if new_tree_sha == base_tree_sha:
            logger.info(f"GitHub: no changes ({len(files)} files) — commit skipped")
            return True

        commit = requests.post(
            f"{base}/git/commits", headers=_headers(), timeout=15,
            json={"message": message, "tree": new_tree_sha,
                  "parents": [base_commit_sha], "committer": _COMMITTER,
                  "author": _COMMITTER},
        )
        commit.raise_for_status()
        new_commit_sha = commit.json()["sha"]

        upd = requests.patch(
            f"{base}/git/refs/heads/{branch}", headers=_headers(), timeout=15,
            json={"sha": new_commit_sha, "force": False},
        )
        upd.raise_for_status()
        logger.info(f"GitHub: committed {len(files)} files in 1 commit ({new_commit_sha[:7]})")
        return True
    except Exception as e:
        logger.error(f"GitHub batched commit failed ({len(files)} files): {e}")
        return False


def commit_file(path: str, content: bytes, message: str) -> bool:
    """Single-file commit. Thin wrapper over commit_files so every caller
    gets atomic + skip-if-unchanged behaviour."""
    return commit_files([(path, content)], message)


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
