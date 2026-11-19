"""Fetch open GitHub issues without an assignee, PR, or explicit claim."""

from __future__ import annotations

import re
from asyncio import sleep
from collections import defaultdict
from collections.abc import AsyncIterator
from difflib import unified_diff
from itertools import batched
from time import time
from urllib.parse import quote

import httpx

from agent.config import settings
from agent.logging import get_logger
from agent.pipeline.claims import has_active_claim
from agent.pipeline.config import PipelineConfig, RepoConfig
from agent.pipeline.models import MAX_ISSUE_CONTEXT_CHARS, AuthorPullRequest, Issue, IssueComment

logger = get_logger(__name__)

API_URL = "https://api.github.com"
SEARCH_URL = f"{API_URL}/search/issues"
PER_PAGE = 50
MAX_PR_BODY_CHARS = 1_600
MAX_PR_PATCH_CHARS = 5_000
MAX_PATCH_COMMITS = 3
MAX_PATCH_SOURCE_BYTES = 256_000
PR_SEARCH_AUTHOR_BATCH_SIZE = 10


def _search_retry_delay(response: httpx.Response, fallback: int) -> int | None:
    if response.status_code not in {403, 429}:
        return None
    retry_after = response.headers.get("retry-after", "")
    if retry_after.isdecimal():
        return int(retry_after)
    if response.headers.get("x-ratelimit-remaining") == "0":
        reset = response.headers.get("x-ratelimit-reset", "")
        if reset.isdecimal():
            return max(1, int(reset) - int(time()) + 1)
    if response.status_code == 429 or "secondary rate limit" in response.text.lower():
        return fallback
    return None


def _pull_request_context(pull: dict) -> AuthorPullRequest:
    merged_at = pull.get("merged_at") or (pull.get("pull_request") or {}).get("merged_at")
    return AuthorPullRequest(
        title=pull["title"],
        body=(pull.get("body") or "")[:MAX_PR_BODY_CHARS],
        html_url=pull["html_url"],
        state="merged" if merged_at else "open",
        merged_at=merged_at,
    )


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
        """Search one repository for new, open issues to assess for availability.

        Args:
            repo_config: The repo and its label/search-term overrides.
            pipeline_config: The full PipelineConfig, for default label/term fallback.
            since_iso: ISO-8601 timestamp — only issues created after this are returned.
            max_body_chars: Issue bodies longer than this are truncated.

        Returns:
            Matching issues with a scope excerpt of at most max_body_chars.
        """
        query = self._build_query(repo_config, pipeline_config, since_iso)
        logger.info("Searching issues", extra={"repo": repo_config.repo, "query": query})

        response = await self._get_search_page(
            httpx.URL(SEARCH_URL, params={"q": query, "per_page": PER_PAGE})
        )
        items = response.json().get("items", [])

        issues = []
        for item in items:
            if item.get("assignee") or item.get("assignees"):
                continue
            body = item.get("body") or ""
            author = (item.get("user") or {}).get("login") or ""
            number = item["number"]
            comments = await self._get_timeline_comments(repo_config.repo, number)
            if comments is None:
                continue
            claim_messages = [(author, body)]
            claim_messages.extend((comment.author, comment.body) for comment in comments)
            if has_active_claim(claim_messages):
                continue

            body_remainder = body[max_body_chars:]
            issues.append(
                Issue(
                    repo=repo_config.repo,
                    title=item["title"],
                    body=body[:max_body_chars],
                    labels=[label["name"] for label in item.get("labels", [])],
                    html_url=item["html_url"],
                    created_at=item["created_at"],
                    truncated=len(body) > max_body_chars,
                    author=author,
                    body_remainder=body_remainder,
                    comments=comments,
                )
            )
        issues = await self._attach_author_pull_requests(repo_config.repo, since_iso, issues)
        issues = [issue for issue in issues if self._fits_triage_budget(issue)]
        logger.info(
            "Search complete",
            extra={
                "repo": repo_config.repo,
                "issue_count": len(issues),
                "filtered_count": len(items) - len(issues),
            },
        )
        return issues

    def _fits_triage_budget(self, issue: Issue) -> bool:
        context_chars = (
            len(issue.body)
            + len(issue.body_remainder)
            + sum(
                len(comment.author) + len(comment.author_association) + len(comment.body)
                for comment in issue.comments
            )
            + sum(
                len(pull.title) + len(pull.body) + len(pull.html_url) + len(pull.patch_excerpt)
                for pull in issue.author_pull_requests
            )
        )
        if context_chars <= MAX_ISSUE_CONTEXT_CHARS:
            return True
        logger.info(
            "Skipping issue whose discussion and PR context exceed the triage budget",
            extra={"repo": issue.repo, "issue_url": issue.html_url, "chars": context_chars},
        )
        return False

    async def _get_search_page(self, url: httpx.URL) -> httpx.Response:
        for fallback in (60, 120, None):
            response = await self._client.get(url, headers=self._headers())
            if fallback is None:
                break
            delay = _search_retry_delay(response, fallback)
            if delay is None:
                break
            logger.warning("GitHub search rate limited. Retrying in %d seconds.", delay)
            await sleep(delay)
        if response.is_error:
            logger.error(
                "GitHub search request failed",
                extra={"status_code": response.status_code, "response_body": response.text[:1000]},
            )
        response.raise_for_status()
        return response

    async def _get_pages(
        self, url: httpx.URL, *, items_key: str | None = None
    ) -> AsyncIterator[dict]:
        is_search = url.path == "/search/issues"
        next_url: httpx.URL | None = url
        while next_url is not None:
            if is_search:
                response = await self._get_search_page(next_url)
            else:
                response = await self._client.get(next_url, headers=self._headers())
            response.raise_for_status()
            payload = response.json()
            if is_search and payload.get("incomplete_results"):
                raise httpx.HTTPError("Incomplete GitHub PR search results")
            items = payload[items_key] if items_key else payload
            for item in items:
                yield item
            next_page = response.links.get("next", {}).get("url")
            next_url = response.url.join(next_page) if next_page else None

    async def _find_author_pull_requests(
        self, repo: str, authors: set[str], since_iso: str
    ) -> dict[str, dict[int, AuthorPullRequest]]:
        pulls_by_author: dict[str, dict[int, AuthorPullRequest]] = defaultdict(dict)
        for batch in batched(sorted(authors), PR_SEARCH_AUTHOR_BATCH_SIZE, strict=False):
            # Repeated author qualifiers are ORed by GitHub's REST search.
            qualifiers = " ".join(
                f"author:app/{author.removesuffix('[bot]')}"
                if author.endswith("[bot]")
                else f"author:{author}"
                for author in batch
            )
            for state in ("is:open", f"is:merged merged:>={since_iso}"):
                query = f"repo:{repo} is:pr {state} {qualifiers}"
                url = httpx.URL(SEARCH_URL, params={"q": query, "per_page": 100})
                async for pull in self._get_pages(url, items_key="items"):
                    author = ((pull.get("user") or {}).get("login") or "").casefold()
                    pr_data = pull.get("pull_request") or {}
                    if author in batch and (
                        pull.get("state") == "open" or pr_data.get("merged_at")
                    ):
                        pulls_by_author[author][pull["number"]] = _pull_request_context(pull)
        return pulls_by_author

    async def _attach_author_pull_requests(
        self, repo: str, since_iso: str, issues: list[Issue]
    ) -> list[Issue]:
        authors = {issue.author.casefold() for issue in issues if issue.author}
        if not authors:
            return issues

        try:
            pulls_by_author = await self._find_author_pull_requests(repo, authors, since_iso)
        except httpx.HTTPError:
            logger.warning(
                "Skipping issues whose authors' PRs could not be checked", extra={"repo": repo}
            )
            return [issue for issue in issues if not issue.author]

        pull_link = re.compile(
            rf"https://github\.com/{re.escape(repo)}/pull/([1-9][0-9]{{0,11}})\b", re.I
        )
        issue_refs: dict[str, tuple[int, ...]] = {}
        evidence_by_ref: dict[tuple[str, int], list[str]] = defaultdict(list)
        for issue in issues:
            if not issue.author:
                continue
            author = issue.author.casefold()
            author_text = "\n".join(
                [issue.body + issue.body_remainder]
                + [
                    comment.body
                    for comment in issue.comments
                    if comment.author.casefold() == author
                ]
            )
            numbers = tuple(dict.fromkeys(int(number) for number in pull_link.findall(author_text)))
            issue_refs[issue.html_url] = numbers
            for number in numbers:
                evidence_by_ref[author, number].extend((issue.title, author_text))

        checked: dict[tuple[str, int], AuthorPullRequest | None] = {}
        verified = []
        for issue in issues:
            author = issue.author.casefold()
            if not author:
                verified.append(issue)
                continue
            author_pulls = pulls_by_author.get(author, {})
            referenced: dict[int, AuthorPullRequest] = {}
            try:
                for number in issue_refs[issue.html_url]:
                    key = (author, number)
                    if key not in checked:
                        checked[key] = await self._get_referenced_author_pr(
                            repo,
                            number,
                            author,
                            author_pulls.get(number),
                            "\n".join(evidence_by_ref[key]),
                        )
                    if (pull := checked[key]) is not None:
                        referenced[number] = pull
            except httpx.HTTPError:
                logger.warning(
                    "Skipping issue whose referenced PR could not be checked",
                    extra={"repo": repo, "issue_url": issue.html_url},
                )
                continue
            issue.author_pull_requests = list((author_pulls | referenced).values())
            verified.append(issue)
        return verified

    async def _get_referenced_author_pr(
        self,
        repo: str,
        number: int,
        author: str,
        pull: AuthorPullRequest | None,
        evidence_text: str,
    ) -> AuthorPullRequest | None:
        if pull is None:
            response = await self._client.get(
                f"{API_URL}/repos/{repo}/pulls/{number}", headers=self._headers()
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
            login = ((data.get("user") or {}).get("login") or "").casefold()
            if login != author or (data.get("state") != "open" and not data.get("merged_at")):
                return None
            pull = _pull_request_context(data)
        patch = await self._get_author_patch(repo, number, author, f"{evidence_text}\n{pull.body}")
        return pull.model_copy(update={"patch_excerpt": patch})

    async def _get_author_patch(
        self, repo: str, number: int, author: str, evidence_text: str
    ) -> str:
        # The aggregate PR diff can include unrelated upstream merges. Inspect the
        # author's most recent non-merge commits for concrete changes instead.
        repo_url = f"{API_URL}/repos/{repo}"
        url = httpx.URL(f"{repo_url}/pulls/{number}/commits", params={"per_page": 100})
        commits = [
            (item["sha"], item["parents"][0].get("sha"))
            async for item in self._get_pages(url)
            if len(item.get("parents", [])) == 1
            and ((item.get("author") or {}).get("login") or "").casefold() == author
        ]

        async def raw_source(path: str, ref: str) -> str | None:
            if any(part in {"", ".", ".."} for part in path.split("/")):
                return None
            source_url = httpx.URL(
                f"{repo_url}/contents/{quote(path, safe='/')}", params={"ref": ref}
            )
            headers = self._headers() | {"Accept": "application/vnd.github.raw+json"}
            source = bytearray()
            async with self._client.stream("GET", source_url, headers=headers) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    if len(source) + len(chunk) > MAX_PATCH_SOURCE_BYTES:
                        return None
                    source.extend(chunk)
            return source.decode("utf-8", errors="replace")

        text = re.sub(r"https?://[^/\s)]+", "", evidence_text)
        named_excerpts = dict.fromkeys(re.findall(r"[\w-]+(?:\.[\w-]+)+", text), "")
        fallback = ""
        for sha, parent_sha in reversed(commits[-MAX_PATCH_COMMITS:]):
            detail_url = httpx.URL(f"{repo_url}/commits/{sha}", params={"per_page": 100})
            async for file in self._get_pages(detail_url, items_key="files"):
                filename = file["filename"]
                name = filename.rsplit("/", 1)[-1]
                patch = file.get("patch")
                if name in named_excerpts:
                    if not patch and parent_sha:
                        status = file.get("status")
                        previous = file.get("previous_filename") or filename
                        before = "" if status == "added" else await raw_source(previous, parent_sha)
                        after = "" if status == "removed" else await raw_source(filename, sha)
                        if before is not None and after is not None:
                            patch = "\n".join(
                                unified_diff(before.splitlines(), after.splitlines(), lineterm="")
                            )
                    if patch:
                        named_excerpts[name] += f"{filename}\n{patch}\n\n"
                        named_excerpts[name] = named_excerpts[name][:MAX_PR_PATCH_CHARS]
                        if all(named_excerpts.values()):
                            return "".join(named_excerpts.values())[:MAX_PR_PATCH_CHARS]
                elif patch and len(fallback) < MAX_PR_PATCH_CHARS:
                    fallback = f"{fallback}{filename}\n{patch}\n\n"[:MAX_PR_PATCH_CHARS]
                    if len(fallback) == MAX_PR_PATCH_CHARS and not named_excerpts:
                        return fallback
        return "".join(named_excerpts.values())[:MAX_PR_PATCH_CHARS] or fallback

    async def _get_timeline_comments(self, repo: str, number: int) -> list[IssueComment] | None:
        """Return chronological comments, or None when a PR exists or the timeline fails."""
        comments: list[IssueComment] = []
        url = httpx.URL(
            f"{API_URL}/repos/{repo}/issues/{number}/timeline", params={"per_page": 100}
        )
        try:
            async for event in self._get_pages(url):
                if event["event"] == "cross-referenced":
                    source_issue = (event.get("source") or {}).get("issue") or {}
                    if source_issue.get("pull_request") is not None:
                        return None
                elif event["event"] == "commented":
                    author = event.get("user") or {}
                    body = event.get("body") or ""
                    if author.get("type") != "Bot" and body.strip():
                        comments.append(
                            IssueComment(
                                author=author.get("login", ""),
                                author_association=event.get("author_association") or "NONE",
                                body=body,
                            )
                        )
        except httpx.HTTPError:
            logger.warning(
                "Skipping issue whose timeline could not be checked",
                extra={"repo": repo, "number": number},
            )
            return None
        return comments

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
