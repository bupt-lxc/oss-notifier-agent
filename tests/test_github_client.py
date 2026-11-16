"""Unit tests for agent.pipeline.github_client — mocked transport, no network."""

import httpx
import pytest

from agent.pipeline.config import PipelineConfig, RepoConfig
from agent.pipeline.github_client import GitHubIssuesClient

SEARCH_RESPONSE = {
    "items": [
        {
            "number": 1,
            "title": "Fix typo in README",
            "body": "The word 'recieve' should be 'receive'.",
            "labels": [{"name": "good first issue"}],
            "html_url": "https://github.com/octocat/example-repo/issues/1",
            "created_at": "2026-07-20T00:00:00Z",
        },
        {
            "number": 2,
            "title": "No body issue",
            "body": None,
            "labels": [],
            "html_url": "https://github.com/octocat/example-repo/issues/2",
            "created_at": "2026-07-20T01:00:00Z",
        },
    ]
}


@pytest.fixture
def repo_config():
    return RepoConfig(repo="octocat/example-repo")


@pytest.fixture
def pipeline_config():
    return PipelineConfig(repositories=[RepoConfig(repo="octocat/example-repo")])


async def test_search_issues_parses_response(repo_config, pipeline_config):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/timeline"):
            return httpx.Response(200, json=[])
        assert "repo:octocat/example-repo" in request.url.params["q"]
        return httpx.Response(200, json=SEARCH_RESPONSE)

    client = GitHubIssuesClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        issues = await client.search_issues(
            repo_config, pipeline_config, "2026-07-19T00:00:00Z", 4000
        )
    finally:
        await client._client.aclose()

    assert len(issues) == 2
    assert issues[0].title == "Fix typo in README"
    assert issues[0].labels == ["good first issue"]
    assert issues[1].body == ""  # None body normalized to empty string


async def test_search_issues_truncates_long_body(repo_config, pipeline_config):
    long_body = "x" * 100
    response = {
        "items": [
            {
                "number": 3,
                "title": "Long issue",
                "body": long_body,
                "labels": [],
                "html_url": "https://github.com/octocat/example-repo/issues/3",
                "created_at": "2026-07-20T00:00:00Z",
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/timeline"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=response)

    client = GitHubIssuesClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        issues = await client.search_issues(
            repo_config, pipeline_config, "2026-07-19T00:00:00Z", 10
        )
    finally:
        await client._client.aclose()

    assert len(issues[0].body) == 10
    assert issues[0].truncated is True


async def test_search_issues_raises_on_http_error(repo_config, pipeline_config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "rate limited"})

    client = GitHubIssuesClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.search_issues(repo_config, pipeline_config, "2026-07-19T00:00:00Z", 4000)
    finally:
        await client._client.aclose()


async def test_build_query_includes_labels_and_terms(repo_config):
    pipeline_config = PipelineConfig(
        defaults={"labels": ["good first issue"], "search_terms": ["typo", "docs"]},
        repositories=[repo_config],
    )
    client = GitHubIssuesClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    )
    try:
        query = client._build_query(repo_config, pipeline_config, "2026-07-19T00:00:00Z")
    finally:
        await client._client.aclose()

    assert "repo:octocat/example-repo" in query
    assert "is:open" in query
    assert "no:assignee" in query
    assert "-linked:pr" in query
    assert 'label:"good first issue"' in query
    assert '"typo" OR "docs"' in query


async def test_search_skips_assigned_claimed_and_pr_linked_issues(repo_config, pipeline_config):
    items = [
        {
            **SEARCH_RESPONSE["items"][0],
            "number": number,
            "html_url": f"https://github.com/octocat/example-repo/issues/{number}",
        }
        for number in range(1, 9)
    ]
    items[0]["assignees"] = [{"login": "assigned-user"}]
    items[1]["body"] = "I'll take this."
    events = {
        2: [],
        3: [{"event": "commented", "body": "我来修这个。", "user": {"type": "User"}}],
        4: [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {"state": "closed", "pull_request": {"url": "https://example.test"}}
                },
            }
        ],
        5: [
            {"event": "commented", "body": "I'll take this.", "user": {"login": "first"}},
            {"event": "commented", "body": "Closing my claim.", "user": {"login": "first"}},
        ],
        6: [{"event": "cross-referenced", "source": {"issue": {"number": 9}}}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/issues":
            return httpx.Response(200, json={"items": items})
        number = int(request.url.path.split("/")[-2])
        assert number != 1
        if number == 7:
            return httpx.Response(403, json={"message": "rate limited"})
        if number == 8 and request.url.params.get("page") != "2":
            next_page = request.url.copy_merge_params({"page": 2})
            return httpx.Response(200, json=[], headers={"Link": f'<{next_page}>; rel="next"'})
        if number == 8:
            return httpx.Response(200, json=[{"event": "commented", "body": "I'll take this."}])
        return httpx.Response(200, json=events[number])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = GitHubIssuesClient(http_client=http_client)
        issues = await client.search_issues(
            repo_config, pipeline_config, "2026-07-19T00:00:00Z", 4000
        )

    assert [issue.html_url for issue in issues] == [
        "https://github.com/octocat/example-repo/issues/5",
        "https://github.com/octocat/example-repo/issues/6",
    ]
