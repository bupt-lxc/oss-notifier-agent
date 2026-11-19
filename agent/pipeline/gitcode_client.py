"""Fetch open GitCode issues without an assignee, linked PR, or active claim."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime
from itertools import count
from urllib.parse import quote

import httpx

from agent.config import settings
from agent.logging import get_logger
from agent.pipeline.claims import has_active_claim
from agent.pipeline.config import PipelineConfig, RepoConfig
from agent.pipeline.models import Issue

logger = get_logger(__name__)

API_URL = "https://api.gitcode.com/api/v5"
PER_PAGE = 100


class GitCodeIssuesClient:
    """Read issues, associated pull requests, and comments from GitCode OpenAPI."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._client = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get_list(self, url: str, **params: str | int) -> list[dict]:
        headers = {"Accept": "application/json"}
        if settings.gitcode_token:
            headers["Authorization"] = f"Bearer {settings.gitcode_token.get_secret_value()}"
        response = await self._client.get(url, headers=headers, params=params)
        response.raise_for_status()
        items = response.json()
        if not isinstance(items, list):
            raise ValueError(f"Expected a list from GitCode API at {response.url.path}")
        return items

    async def _paginate(self, url: str, **params: str) -> AsyncIterator[dict]:
        for page in count(1):
            items = await self._get_list(url, page=page, per_page=PER_PAGE, **params)
            for item in items:
                yield item
            if len(items) < PER_PAGE:
                return

    async def search_issues(
        self,
        repo_config: RepoConfig,
        pipeline_config: PipelineConfig,
        since_iso: str,
        max_body_chars: int,
    ) -> list[Issue]:
        """Return available issues created after the given timestamp."""
        repo = repo_config.repo
        if not re.fullmatch(r"[-\w.]+/[-\w.]+", repo) or any(
            part in {".", ".."} for part in repo.split("/")
        ):
            raise ValueError("GitCode repository must use owner/name")
        url = f"{API_URL}/repos/{quote(repo, safe='/')}/issues"
        since = datetime.fromisoformat(since_iso)
        required_labels = {label.casefold() for label in pipeline_config.labels_for(repo_config)}
        search_terms = [term.casefold() for term in pipeline_config.search_terms_for(repo_config)]

        logger.info("Searching GitCode issues", extra={"repo": repo})
        issues = []
        async for item in self._paginate(
            url, state="open", sort="created", direction="desc", created_after=since.isoformat()
        ):
            if (
                item["state"] not in {"open", "opened"}
                or item.get("assignee")
                or item.get("assignees")
            ):
                continue
            if datetime.fromisoformat(item["created_at"]) <= since:
                continue
            labels = [label["name"] for label in item.get("labels") or []]
            if not required_labels.issubset(label.casefold() for label in labels):
                continue
            title = item["title"]
            body = item.get("body") or ""
            title_lower, body_lower = title.casefold(), body.casefold()
            if search_terms and not any(
                term in title_lower or term in body_lower for term in search_terms
            ):
                continue
            number = str(item["number"])
            if not re.fullmatch(r"[-\w]+", number):
                raise ValueError("GitCode API returned an invalid issue number")
            author = (item.get("user") or {}).get("login") or ""
            if not await self._is_available(f"{url}/{number}", author, body):
                continue
            issues.append(
                Issue(
                    repo=repo,
                    title=title,
                    body=body[:max_body_chars],
                    labels=labels,
                    html_url=item["html_url"],
                    created_at=item["created_at"],
                    truncated=len(body) > max_body_chars,
                )
            )

        logger.info("GitCode search complete", extra={"repo": repo, "issue_count": len(issues)})
        return issues

    async def _is_available(self, url: str, author: str, body: str) -> bool:
        if await self._get_list(f"{url}/pull_requests"):
            return False
        discussion = [(author, body)]
        async for comment in self._paginate(f"{url}/comments", order="asc"):
            user = comment.get("user") or {}
            if user.get("type") != "Bot":
                discussion.append((user.get("login") or "", comment.get("body") or ""))
        return not has_active_claim(discussion)
