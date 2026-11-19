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

import logfire

from agent.agents import AgentOutput, run_agent
from agent.config import settings
from agent.logging import configure_logging, get_logger
from agent.pipeline.config import PipelineConfig, load_config
from agent.pipeline.email import send_digest
from agent.pipeline.github_client import GitHubIssuesClient
from agent.pipeline.models import Issue
from agent.pipeline.sent_state import SentIssueStore
from agent.prompts.templates import render_issue_prompt

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
    github_client: GitHubIssuesClient,
    *,
    sent_urls: Collection[str] = (),
) -> list[Issue]:
    """Collect unseen issues, tolerating individual repo failures."""
    since_iso = (datetime.now(UTC) - timedelta(hours=settings.search_window_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    issues: list[Issue] = []
    seen_urls = set(sent_urls)
    for i, repo_config in enumerate(config.repositories):
        if i > 0:
            await asyncio.sleep(GITHUB_SEARCH_DELAY_SECONDS)
        try:
            repo_issues = await github_client.search_issues(
                repo_config, config, since_iso, settings.max_issue_body_chars
            )
            for issue in repo_issues:
                if issue.html_url not in seen_urls:
                    seen_urls.add(issue.html_url)
                    issues.append(issue)
        except Exception:
            logger.error(
                "Failed to search repo, skipping", extra={"repo": repo_config.repo}, exc_info=True
            )

    if len(issues) > settings.max_issues_per_run:
        logger.warning(
            "Issue count exceeds max_issues_per_run, truncating",
            extra={"found": len(issues), "cap": settings.max_issues_per_run},
        )
        issues = issues[: settings.max_issues_per_run]

    return issues


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
    group: str | None = None,
    known_groups: Collection[str] | None = None,
    github_state: bool = False,
) -> None:
    configure_logging()

    with logfire.span("digest_run"):
        config = load_config()
        if known_groups is not None:
            used_groups = {repo.effective_schedule_group for repo in config.repositories}
            if group is not None:
                used_groups.add(group)
            unknown_groups = used_groups.difference(known_groups)
            if unknown_groups:
                raise ValueError(
                    f"Unregistered schedule groups: {', '.join(sorted(unknown_groups))}"
                )

        if group is not None:
            repositories = [
                repo for repo in config.repositories if repo.effective_schedule_group == group
            ]
            config = config.model_copy(update={"repositories": repositories})
        logger.info(
            "Loaded pipeline config",
            extra={"repo_count": len(config.repositories), "schedule_group": group},
        )

        async with AsyncExitStack() as resources:
            sent_store = None
            sent_urls: Collection[str] = ()
            if github_state and config.repositories:
                repository = os.environ.get("GITHUB_REPOSITORY")
                token = settings.github_token
                if not repository or not token:
                    raise ValueError("--github-state requires GITHUB_REPOSITORY and GITHUB_TOKEN")
                sent_store = SentIssueStore(repository, token)
                resources.push_async_callback(sent_store.aclose)
                sent_urls = await sent_store.load(dry_run=dry_run)

            github_client = GitHubIssuesClient()
            try:
                issues = await collect_issues(config, github_client, sent_urls=sent_urls)
            finally:
                await github_client.aclose()
            logger.info("Issues collected", extra={"issue_count": len(issues)})

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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Search and triage issues as normal, but print the digest instead of emailing it.",
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
            group=args.group,
            known_groups=args.known_groups.split(",") if args.known_groups else None,
            github_state=args.github_state,
        )
    )
