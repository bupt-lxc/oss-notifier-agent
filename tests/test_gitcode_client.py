"""GitCode OpenAPI issue collection and availability tests."""

import httpx
import pytest
from pydantic import SecretStr

from agent.config import settings
from agent.pipeline import gitcode_client
from agent.pipeline.config import PipelineConfig, RepoConfig

ISSUES_PATH = "/api/v5/repos/Ascend/MindIE-Motor/issues"
SINCE = "2026-11-19T00:00:00Z"


def issue(number: int, **overrides) -> dict:
    return {
        "number": str(number),
        "title": "Fix DOCUMENTATION example",
        "body": "Example body",
        "created_at": "2026-11-19T09:00:00+08:00",
        "labels": [{"name": "Docs"}, {"name": "small"}],
        "state": "open",
        "assignees": [],
        "user": {"login": "reporter"},
        "html_url": f"https://gitcode.com/Ascend/MindIE-Motor/issues/{number}",
        **overrides,
    }


async def search(handler, repo_config=None):
    repo_config = repo_config or RepoConfig(repo="Ascend/MindIE-Motor")
    config = PipelineConfig()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = gitcode_client.GitCodeIssuesClient(http)
        try:
            return await client.search_issues(repo_config, config, SINCE, 7)
        finally:
            await client.aclose()
            assert not http.is_closed


async def test_search_filters_and_paginates_issues(monkeypatch):
    monkeypatch.setattr(gitcode_client, "PER_PAGE", 3)
    monkeypatch.setattr(settings, "gitcode_token", SecretStr("test-token"))
    repo = RepoConfig(
        repo="Ascend/MindIE-Motor",
        labels=["docs", "small"],
        search_terms=["typo", "documentation"],
    )
    pages = {
        "1": [
            issue(6, assignees=[{"login": "owner"}]),
            issue(5, labels=[{"name": "Docs"}]),
            issue(4, state="closed"),
        ],
        "2": [
            issue(3, title="Unrelated"),
            issue(2, created_at="2026-11-19T08:00:00+08:00"),
            issue(1, title="Fix documentation example"),
        ],
        "3": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.gitcode.com"
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path == ISSUES_PATH:
            assert request.url.params["state"] == "open"
            assert request.url.params["created_after"] == "2026-11-19T00:00:00+00:00"
            return httpx.Response(200, json=pages[request.url.params["page"]])
        assert request.url.path in {
            f"{ISSUES_PATH}/1/pull_requests",
            f"{ISSUES_PATH}/1/comments",
        }
        return httpx.Response(200, json=[])

    issues = await search(handler, repo)

    [match] = issues
    assert match.repo == "Ascend/MindIE-Motor"
    assert match.html_url.endswith("/1")
    assert match.labels == ["Docs", "small"]
    assert match.body == "Example"
    assert match.truncated


async def test_search_checks_related_prs_and_chronological_claims(monkeypatch):
    monkeypatch.setattr(gitcode_client, "PER_PAGE", 2)
    monkeypatch.setattr(settings, "gitcode_token", None)
    pages = {
        "1": [issue(4), issue(3, body="Original problem.\n我来修复这个问题。")],
        "2": [issue(2), issue(1)],
        "3": [],
    }
    claims = {
        "1": [
            {"body": "我来修复这个问题。", "user": {"login": "ren", "type": None}},
            {"body": "I am working on this.", "user": {"login": "robot", "type": "Bot"}},
        ],
        "2": [{"body": "我放弃认领。", "user": {"login": "ren"}}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        if request.url.path == ISSUES_PATH:
            return httpx.Response(200, json=pages[request.url.params["page"]])
        if request.url.path.endswith("/pull_requests"):
            payload = [{"number": 42}] if request.url.path.endswith("/4/pull_requests") else []
            return httpx.Response(200, json=payload)
        assert not request.url.path.endswith("/4/comments")
        assert request.url.params["order"] == "asc"
        page = request.url.params["page"]
        if request.url.path.endswith("/1/comments"):
            return httpx.Response(200, json=claims[page])
        if request.url.path.endswith("/2/comments"):
            return httpx.Response(200, json=claims["1"] if page == "1" else [])
        return httpx.Response(200, json=[])

    [match] = await search(handler)

    assert match.html_url.endswith("/1")


async def test_search_reports_failed_pr_lookup(monkeypatch):
    monkeypatch.setattr(settings, "gitcode_token", None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == ISSUES_PATH:
            return httpx.Response(200, json=[issue(1)])
        return httpx.Response(429, json={"message": "Too Many Requests"})

    with pytest.raises(httpx.HTTPStatusError) as error:
        await search(handler)

    assert error.value.response.status_code == 429


async def test_search_preserves_full_discussion_for_availability_review(monkeypatch):
    monkeypatch.setattr(settings, "gitcode_token", None)
    body = "Summary\nFull discussion context and a proposed patch."

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == ISSUES_PATH:
            return httpx.Response(200, json=[issue(1, body=body)])
        if request.url.path.endswith("/pull_requests"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                {"body": "First comment", "user": {"login": "reader", "member_role": "admin"}},
                {"body": "Bot comment", "user": {"login": "bot", "type": "Bot"}},
                {"body": "\n ", "user": {"login": "someone"}},
                {"body": "Second comment", "user": {"login": "reporter"}},
            ],
        )

    [match] = await search(handler)

    assert match.author == "reporter"
    assert match.body == "Summary"
    assert match.truncated
    assert match.body + match.body_remainder == body
    assert [comment.model_dump() for comment in match.comments] == [
        {"author": "reader", "author_association": "NONE", "body": "First comment"},
        {"author": "reporter", "author_association": "NONE", "body": "Second comment"},
    ]


async def test_search_skips_issues_whose_full_context_exceeds_triage_budget(monkeypatch):
    monkeypatch.setattr(settings, "gitcode_token", None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == ISSUES_PATH:
            return httpx.Response(200, json=[issue(3, body="b" * 12_001), issue(2), issue(1)])
        if request.url.path.endswith("/2/comments"):
            return httpx.Response(
                200, json=[{"body": "Commentary " * 1_200, "user": {"login": "reader"}}]
            )
        return httpx.Response(200, json=[])

    issues = await search(handler)

    assert [match.html_url for match in issues] == [
        "https://gitcode.com/Ascend/MindIE-Motor/issues/1"
    ]


async def test_search_rejects_paths_outside_owner_and_repo():
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("An invalid repository path must fail before calling GitCode")

    repo = RepoConfig.model_construct(repo="Ascend/..")
    with pytest.raises(ValueError, match="owner/name"):
        await search(handler, repo)
