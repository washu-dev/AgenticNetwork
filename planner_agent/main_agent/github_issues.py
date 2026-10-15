"""Create GitHub issues for user-submitted bug reports and feedback.

This is the single outbound path to GitHub. It reads its token and target repo
from the environment and performs one ``POST /repos/{repo}/issues`` call. Failures
are surfaced as :class:`GitHubIssueError` so the caller can fail loud (HTTP 502)
rather than silently drop a report — there is deliberately no local fallback store.

The repo has no default: issues carry the reporter's email, prompts and trace, so
``GITHUB_ISSUES_REPO`` must name a private repo explicitly.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_API_ROOT = "https://api.github.com"
_TIMEOUT_SECONDS = 10.0


class GitHubIssueError(RuntimeError):
    """Raised when an issue could not be created (missing token/repo or API failure)."""


async def create_issue(title: str, body: str, labels: list[str]) -> str:
    """Create an issue and return its ``html_url``.

    Raises :class:`GitHubIssueError` if the token or repo is missing or GitHub
    returns a non-2xx response / the request fails.
    """
    token = os.getenv("GITHUB_ISSUES_TOKEN", "").strip()
    if not token:
        raise GitHubIssueError("reporting not configured (GITHUB_ISSUES_TOKEN unset)")
    repo = os.getenv("GITHUB_ISSUES_REPO", "").strip()
    if not repo:
        raise GitHubIssueError("reporting not configured (GITHUB_ISSUES_REPO unset)")

    url = f"{_API_ROOT}/repos/{repo}/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"title": title, "body": body, "labels": labels}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        logger.exception("github_issue_request_failed repo=%s", repo)
        raise GitHubIssueError(f"could not reach GitHub: {exc}") from exc

    if resp.status_code >= 300:
        # Body may contain GitHub's error detail; keep it short in the raised message.
        detail = resp.text[:300]
        logger.error(
            "github_issue_create_failed repo=%s status=%s body=%s",
            repo, resp.status_code, detail,
        )
        raise GitHubIssueError(
            f"GitHub returned {resp.status_code} creating the issue"
        )

    html_url = resp.json().get("html_url", "")
    logger.info("github_issue_created repo=%s url=%s", repo, html_url)
    return html_url
