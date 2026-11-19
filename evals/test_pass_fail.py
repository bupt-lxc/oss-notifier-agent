"""Pass/fail evals for the good-first-issue triage agent.

These evals test for specific, verifiable outputs.
Run with: uv run pytest -m eval
"""

from dataclasses import dataclass

import pytest
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from agent.agents import run_agent
from agent.pipeline.models import Issue, IssueComment
from agent.prompts.templates import render_issue_prompt

TYPO_FIX_ISSUE = (
    "Repository: octocat/example-repo\n"
    "Title: Fix typo in README\n"
    "Labels: good first issue\n"
    "URL: https://github.com/octocat/example-repo/issues/1\n\n"
    "Body:\nThe word 'recieve' on line 12 of README.md should be 'receive'."
)

ARCHITECTURE_REDESIGN_ISSUE = (
    "Repository: octocat/example-repo\n"
    "Title: Redesign caching layer for better performance\n"
    "Labels: enhancement\n"
    "URL: https://github.com/octocat/example-repo/issues/2\n\n"
    "Body:\nOur current caching approach doesn't scale well under high concurrency. We should "
    "investigate alternative architectures such as a distributed cache with consistent hashing, "
    "potentially replacing the current LRU implementation across every subsystem that depends on it."
)


@pytest.mark.eval
async def test_agent_flags_clear_typo_fix():
    """A localized, clearly-described fix should be flagged as a good first issue."""
    output = await run_agent(TYPO_FIX_ISSUE)
    assert output.is_good_first_issue is True
    assert output.reasoning
    assert output.summary


@pytest.mark.eval
async def test_agent_rejects_architecture_redesign():
    """A cross-cutting architectural change should not be flagged."""
    output = await run_agent(ARCHITECTURE_REDESIGN_ISSUE)
    assert output.is_good_first_issue is False


@pytest.mark.eval
@pytest.mark.parametrize(
    ("body_remainder", "discussion", "expected"),
    [
        pytest.param(
            "",
            [
                (
                    "junior",
                    "I can take this if no one's on it. I'll update the formatter and add a test.",
                )
            ],
            False,
            id="claim-with-a-plan",
        ),
        pytest.param(
            "\nVerified fix: in formatter.py, add `s = s.rstrip()` before the line count check. "
            "This patch passes the formatter tests. I can put up a PR if maintainers agree.",
            [],
            False,
            id="author-offers-pr-in-body",
        ),
        pytest.param(
            "",
            [
                (
                    "reporter",
                    "Replace `return s` in format_line in formatter.py with `return s.rstrip()`. The formatter tests pass with that change.",
                ),
                ("REPORTER", "Feel free to submit a PR with that fix."),
            ],
            False,
            id="author-fix-and-pr-across-comments",
        ),
        pytest.param(
            "",
            [
                (
                    "reporter",
                    "PRs welcome. Trimming output might help, but I haven't found the cause.",
                )
            ],
            True,
            id="generic-pr-invitation",
        ),
        pytest.param(
            "",
            [
                ("junior", "I'll take this."),
                ("junior", "I'm no longer working on it. It's open to others."),
            ],
            True,
            id="released-claim",
        ),
    ],
)
async def test_agent_checks_issue_availability(body_remainder, discussion, expected):
    issue = Issue(
        repo="octocat/example-repo",
        title="Formatter leaves trailing whitespace",
        body="The format_line function in formatter.py leaves trailing whitespace in its output.",
        labels=["good first issue"],
        html_url="https://github.com/octocat/example-repo/issues/1",
        created_at="2026-07-20T00:00:00Z",
        author="reporter",
        body_remainder=body_remainder,
        truncated=bool(body_remainder),
        comments=[
            IssueComment(author=author, author_association="NONE", body=body)
            for author, body in discussion
        ],
    )

    output = await run_agent(render_issue_prompt(issue))

    assert output.is_available is expected, output.reasoning
    if not expected:
        assert not output.is_good_first_issue


# --- Dataset eval driven by evals/fixtures/triage_cases.json ---


@dataclass
class MatchesExpectedVerdict(Evaluator[str, bool]):
    """Pass if the agent's is_good_first_issue verdict matches the expected boolean."""

    def evaluate(self, ctx: EvaluatorContext[str, bool]) -> bool:
        if ctx.expected_output is None:
            return ctx.output is not None
        return ctx.output == ctx.expected_output


@pytest.mark.eval
async def test_fixture_dataset(triage_cases: list[dict]):
    """Run every case in evals/fixtures/triage_cases.json through the agent.

    Add cases to that JSON file to grow this eval — no code changes needed
    unless a case requires a new kind of check, in which case add an
    Evaluator like MatchesExpectedVerdict above.
    """
    dataset = Dataset(
        cases=[
            Case(
                name=case["name"],
                inputs=case["inputs"]["user_input"],
                expected_output=case.get("expected_output"),
                metadata=case.get("metadata"),
            )
            for case in triage_cases
        ],
        evaluators=[MatchesExpectedVerdict()],
    )

    async def task(user_input: str) -> bool:
        output = await run_agent(user_input)
        return output.is_good_first_issue

    report = await dataset.evaluate(task)
    report.print(include_input=True, include_output=True)

    averages = report.averages()
    assert averages is not None
    assert averages.assertions == 1.0, "One or more eval cases failed — see the report above."
