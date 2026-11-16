"""Fetch open GitHub issues without an assignee, PR, or explicit claim."""

from __future__ import annotations

import httpx

from agent.config import settings
from agent.logging import get_logger
from agent.pipeline.claims import has_active_claim
from agent.pipeline.config import PipelineConfig, RepoConfig
from agent.pipeline.models import Issue

logger = get_logger(__name__)

API_URL = "https://api.github.com"
SEARCH_URL = f"{API_URL}/search/issues"
PER_PAGE = 50


class GitHubIssuesClient:
    """Thin wrapper around the GitHub Search API for issues."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._client = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if settings.github_token:
            headers["Authorization"] = f"token {settings.github_token}"
        return headers

    async def search_issues(
        self,
        repo_config: RepoConfig,
        pipeline_config: PipelineConfig,
        since_iso: str,
        max_body_chars: int,
    ) -> list[Issue]:
        """Search one repository for new, open issues that are available to take.

        Args:
            repo_config: The repo and its label/search-term overrides.
            pipeline_config: The full PipelineConfig, for default label/term fallback.
            since_iso: ISO-8601 timestamp — only issues created after this are returned.
            max_body_chars: Issue bodies longer than this are truncated.

        Returns:
            Matching issues, truncated to max_body_chars.
        """
        query = self._build_query(repo_config, pipeline_config, since_iso)
        logger.info("Searching issues", extra={"repo": repo_config.repo, "query": query})

        response = await self._client.get(
            SEARCH_URL,
            params={"q": query, "per_page": PER_PAGE},
            headers=self._headers(),
        )
        if response.is_error:
            # httpx.HTTPStatusError's default message doesn't include the
            # response body, which is where GitHub explains *which* limit was
            # hit (primary quota vs. secondary/abuse rate limit) — log it so a
            # failure is actually diagnosable instead of just a bare stack trace.
            logger.error(
                "GitHub search request failed",
                extra={
                    "repo": repo_config.repo,
                    "status_code": response.status_code,
                    "response_body": response.text[:1000],
                },
            )
        response.raise_for_status()
        items = response.json().get("items", [])

        issues = []
        for item in items:
            if item.get("assignee") or item.get("assignees"):
                continue
            body = item.get("body") or ""
            author = (item.get("user") or {}).get("login", "")
            if not await self._is_available(repo_config.repo, item["number"], [(author, body)]):
                continue
            truncated = len(body) > max_body_chars
            if truncated:
                body = body[:max_body_chars]
            issues.append(
                Issue(
                    repo=repo_config.repo,
                    title=item["title"],
                    body=body,
                    labels=[label["name"] for label in item.get("labels", [])],
                    html_url=item["html_url"],
                    created_at=item["created_at"],
                    truncated=truncated,
                )
            )
        logger.info(
            "Search complete",
            extra={
                "repo": repo_config.repo,
                "issue_count": len(issues),
                "filtered_count": len(items) - len(issues),
            },
        )
        return issues

    async def _is_available(self, repo: str, number: int, comments: list[tuple[str, str]]) -> bool:
        url: httpx.URL | None = httpx.URL(
            f"{API_URL}/repos/{repo}/issues/{number}/timeline", params={"per_page": 100}
        )
        try:
            while url is not None:
                response = await self._client.get(url, headers=self._headers())
                response.raise_for_status()
                for event in response.json():
                    if event["event"] == "cross-referenced":
                        source_issue = (event.get("source") or {}).get("issue") or {}
                        if source_issue.get("pull_request") is not None:
                            return False
                    elif event["event"] == "commented":
                        author = event.get("user") or {}
                        if author.get("type") != "Bot":
                            comments.append((author.get("login", ""), event.get("body") or ""))
                next_page = response.links.get("next", {}).get("url")
                url = response.url.join(next_page) if next_page else None
        except httpx.HTTPError:
            logger.warning(
                "Skipping issue whose timeline could not be checked",
                extra={"repo": repo, "number": number},
            )
            return False
        return not has_active_claim(comments)

    def _build_query(
        self, repo_config: RepoConfig, pipeline_config: PipelineConfig, since_iso: str
    ) -> str:
        parts = [
            f"repo:{repo_config.repo}",
            "is:issue",
            "is:open",
            "no:assignee",
            "-linked:pr",
            f"created:>{since_iso}",
        ]

        labels = pipeline_config.labels_for(repo_config)
        for label in labels:
            parts.append(f'label:"{label}"')

        search_terms = pipeline_config.search_terms_for(repo_config)
        if search_terms:
            terms = " OR ".join(f'"{term}"' for term in search_terms)
            parts.append(f"({terms})" if len(search_terms) > 1 else terms)

        return " ".join(parts)
