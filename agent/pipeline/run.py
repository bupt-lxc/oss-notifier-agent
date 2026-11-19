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
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import logfire

from agent.config import settings
from agent.logging import configure_logging, get_logger
from agent.pipeline.config import PipelineConfig, load_config
from agent.pipeline.email import send_digest
from agent.pipeline.gitcode_client import GitCodeIssuesClient
from agent.pipeline.github_client import GitHubIssuesClient
from agent.pipeline.issue_status import exclusion_reason
from agent.pipeline.models import Issue
from agent.prompts.templates import render_issue_prompt

if TYPE_CHECKING:
    from agent.agents import AgentOutput

logger = get_logger(__name__)

# GitHub's Search API is much more prone to secondary (abuse) rate limiting
# than the general REST API when hit with rapid consecutive requests, even
# well under the documented per-minute quota — we saw 5 of 7 repos get 403'd
# in production when searched back-to-back with no delay. GitHub's own
# guidance is to space Search API requests out; this is deliberately a flat
# delay rather than a dynamic backoff, since a run only searches a handful of
# repos once a day and a few extra seconds of latency is irrelevant.
GITHUB_SEARCH_DELAY_SECONDS = 2.0


async def collect_issues(
    config: PipelineConfig,
    github_client: GitHubIssuesClient | None,
    gitcode_client: GitCodeIssuesClient | None = None,
) -> list[Issue]:
    """Search every configured repo, tolerating individual repo failures."""
    since_iso = (datetime.now(UTC) - timedelta(hours=settings.search_window_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    issues: list[Issue] = []
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
                if reason := exclusion_reason(issue):
                    logger.info("Skipping %s issue %s", reason, issue.html_url)
                else:
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


async def main(dry_run: bool = False, *, fetch_only: bool = False) -> None:
    if not fetch_only:
        settings.check_provider_key()
    configure_logging()

    with logfire.span("digest_run"):
        config = load_config()
        logger.info("Loaded pipeline config", extra={"repo_count": len(config.repositories)})

        platforms = {repo.platform for repo in config.repositories}
        async with AsyncExitStack() as stack:
            github_client = GitHubIssuesClient() if "github" in platforms else None
            if github_client is not None:
                stack.push_async_callback(github_client.aclose)
            gitcode_client = GitCodeIssuesClient() if "gitcode" in platforms else None
            if gitcode_client is not None:
                stack.push_async_callback(gitcode_client.aclose)
            issues = await collect_issues(config, github_client, gitcode_client)
        logger.info("Issues collected", extra={"issue_count": len(issues)})

        if fetch_only:
            print(
                f"Fetched {len(issues)} candidate issues from {len(config.repositories)} configured repositories."
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
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(dry_run=args.dry_run, fetch_only=args.fetch_only))
