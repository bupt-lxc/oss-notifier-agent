"""Pipeline entrypoint: scan configured repos, triage new issues, email a digest.

Deterministic orchestration — fetching, filtering, and emailing are plain
Python; the per-issue availability and beginner-scope judgments go
through the triage agent (agent/agents/single.py). Run with:

    uv run python -m agent.pipeline.run
    uv run python -m agent.pipeline.run --dry-run   # print the digest instead of emailing it
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Collection
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import logfire

from agent.config import settings
from agent.logging import configure_logging, get_logger
from agent.pipeline.config import PipelineConfig, RepoPlatform, load_config
from agent.pipeline.email import send_digest
from agent.pipeline.gitcode_client import GitCodeIssuesClient
from agent.pipeline.github_client import GitHubIssuesClient
from agent.pipeline.issue_status import exclusion_reason
from agent.pipeline.models import Issue
from agent.pipeline.sent_state import SentIssueStore
from agent.prompts.templates import render_issue_prompt

if TYPE_CHECKING:
    from agent.agents import AgentOutput

logger = get_logger(__name__)

# GitHub's Search API is much more prone to secondary (abuse) rate limiting
# than the general REST API when hit with rapid consecutive requests, even
# well under the documented per-minute quota — we saw 5 of 7 repos get 403'd
# in production when searched back-to-back with no delay. GitHub's own
# guidance is to space Search API requests out. A flat delay is enough for
# the small watch lists this pipeline scans.
GITHUB_SEARCH_DELAY_SECONDS = 2.0


async def collect_issues(
    config: PipelineConfig,
    github_client: GitHubIssuesClient | None,
    gitcode_client: GitCodeIssuesClient | None = None,
    *,
    sent_urls: Collection[str] = (),
) -> list[Issue]:
    """Collect unseen issues, tolerating individual repo failures."""
    since_iso = (datetime.now(UTC) - timedelta(hours=settings.search_window_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    issues: list[Issue] = []
    seen_urls = set(sent_urls)
    last_error: Exception | None = None
    searched = 0
    for i, repo_config in enumerate(config.repositories):
        if i > 0:
            await asyncio.sleep(GITHUB_SEARCH_DELAY_SECONDS)
        try:
            client = gitcode_client if repo_config.platform == "gitcode" else github_client
            if client is None:
                raise RuntimeError(f"No client available for {repo_config.platform}.")
            repo_issues = await client.search_issues(
                repo_config, config, since_iso, settings.max_issue_body_chars
            )
            for issue in repo_issues:
                if issue.html_url in seen_urls:
                    continue
                if reason := exclusion_reason(issue):
                    logger.info("Skipping %s issue %s", reason, issue.html_url)
                    continue
                seen_urls.add(issue.html_url)
                issues.append(issue)
            searched += 1
        except Exception as error:
            last_error = error
            logger.error(
                "Failed to search repo, skipping",
                extra={"repo": repo_config.repo, "platform": repo_config.platform},
                exc_info=True,
            )

    if not searched and last_error is not None:
        raise RuntimeError("Could not search any configured repository.") from last_error

    if len(issues) > settings.max_issues_per_run:
        logger.warning(
            "Issue count exceeds max_issues_per_run, truncating",
            extra={"found": len(issues), "cap": settings.max_issues_per_run},
        )
        issues = issues[: settings.max_issues_per_run]

    return issues


async def run_agent(prompt: str) -> AgentOutput:
    from agent.agents import run_agent as classify_issue

    return await classify_issue(prompt)


async def triage_issues(issues: list[Issue]) -> list[tuple[Issue, AgentOutput]]:
    """Keep available issues that the agent flags as good first issues."""
    flagged: list[tuple[Issue, AgentOutput]] = []
    for issue in issues:
        verdict = await run_agent(render_issue_prompt(issue))
        if verdict.is_available and verdict.is_good_first_issue:
            flagged.append((issue, verdict))
    return flagged


def print_digest(flagged: list[tuple[Issue, AgentOutput]]) -> None:
    """Print the digest to the console instead of emailing it (--dry-run)."""
    print(f"\n{len(flagged)} good first issue{'s' if len(flagged) != 1 else ''} found:\n")
    for issue, verdict in flagged:
        print(f"- [{issue.repo}] {issue.title}")
        print(f"  {issue.html_url}")
        print(f"  Summary:   {verdict.summary}")
        print(f"  Reasoning: {verdict.reasoning}\n")


async def main(
    dry_run: bool = False,
    *,
    fetch_only: bool = False,
    platform: RepoPlatform | None = None,
    group: str | None = None,
    known_groups: Collection[str] | None = None,
    github_state: bool = False,
) -> None:
    if not fetch_only:
        settings.check_provider_key()
    configure_logging()

    with logfire.span("digest_run"):
        config = load_config()
        repositories = config.repositories
        if platform is not None:
            repositories = [repo for repo in repositories if repo.platform == platform]
        if known_groups is not None:
            used_groups = {repo.effective_schedule_group for repo in repositories}
            if group is not None:
                used_groups.add(group)
            unknown_groups = used_groups.difference(known_groups)
            if unknown_groups:
                raise ValueError(
                    f"Unregistered schedule groups: {', '.join(sorted(unknown_groups))}"
                )

        if group is not None:
            repositories = [repo for repo in repositories if repo.effective_schedule_group == group]
        config = config.model_copy(update={"repositories": repositories})
        logger.info(
            "Loaded pipeline config",
            extra={"repo_count": len(repositories), "platform": platform, "schedule_group": group},
        )

        async with AsyncExitStack() as resources:
            sent_store = None
            sent_urls: Collection[str] = ()
            if github_state and repositories:
                repository = os.environ.get("GITHUB_REPOSITORY")
                token = settings.github_token
                if not repository or not token:
                    raise ValueError("--github-state requires GITHUB_REPOSITORY and GITHUB_TOKEN")
                sent_store = SentIssueStore(repository, token)
                resources.push_async_callback(sent_store.aclose)
                sent_urls = await sent_store.load(dry_run=dry_run or fetch_only)

            platforms = {repo.platform for repo in repositories}
            github_client = GitHubIssuesClient() if "github" in platforms else None
            if github_client is not None:
                resources.push_async_callback(github_client.aclose)
            gitcode_client = GitCodeIssuesClient() if "gitcode" in platforms else None
            if gitcode_client is not None:
                resources.push_async_callback(gitcode_client.aclose)
            issues = await collect_issues(
                config, github_client, gitcode_client, sent_urls=sent_urls
            )
            logger.info("Issues collected", extra={"issue_count": len(issues)})

            if fetch_only:
                print(
                    f"Fetched {len(issues)} candidate issues from {len(repositories)} configured repositories."
                )
                for issue in issues:
                    print(f"- {issue.html_url}")
                return

            flagged = await triage_issues(issues)
            logger.info("Triage complete (%d matched)", len(flagged))

            if not flagged:
                logger.info("No good first issues found, skipping email")
                if dry_run:
                    print("\nNo good first issues found.")
                return

            if dry_run:
                logger.info("Dry run — printing digest instead of emailing")
                print_digest(flagged)
                return

            await send_digest(flagged)
            if sent_store is not None:
                await sent_store.mark_sent(issue.html_url for issue, _ in flagged)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the good-first-issue digest pipeline.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Search and triage issues as normal, but print the digest instead of emailing it.",
    )
    mode.add_argument(
        "--fetch-only",
        action="store_true",
        help="Fetch and filter candidate issues without calling a model or sending email.",
    )
    parser.add_argument(
        "--platform", choices=("github", "gitcode"), help="Scan only this repository platform."
    )
    parser.add_argument("--group", help="Scan only repositories in this schedule group.")
    parser.add_argument("--known-groups", help="Comma-separated groups defined by the workflow.")
    parser.add_argument(
        "--github-state",
        action="store_true",
        help="Use the notifier-state branch in GITHUB_REPOSITORY to deduplicate digests.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(
        main(
            dry_run=args.dry_run,
            fetch_only=args.fetch_only,
            platform=args.platform,
            group=args.group,
            known_groups=args.known_groups.split(",") if args.known_groups else None,
            github_state=args.github_state,
        )
    )
