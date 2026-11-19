import httpx

from agent.pipeline.github_client import MAX_PR_PATCH_CHARS, GitHubIssuesClient


async def test_named_commit_file_uses_raw_blobs_when_list_patch_is_missing():
    repo_path = "/repos/octocat/example-repo"
    feature_commit, parent_commit = "a" * 40, "b" * 40
    filename = "examples/replay #1/proxy.py"
    raw_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"{repo_path}/pulls/42/commits":
            return httpx.Response(
                200,
                json=[
                    {
                        "sha": feature_commit,
                        "author": {"login": "reporter"},
                        "parents": [{"sha": parent_commit}],
                    }
                ],
            )
        if path == f"{repo_path}/commits/{feature_commit}":
            assert request.url.params["per_page"] == "100"
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json={"files": [{"filename": filename, "patch": None}]})
            next_page = request.url.copy_merge_params({"page": 2})
            return httpx.Response(
                200,
                json={"files": [{"filename": "unrelated.py", "patch": "x" * MAX_PR_PATCH_CHARS}]},
                headers={"Link": f'<{next_page}>; rel="next"'},
            )
        assert path == f"{repo_path}/contents/{filename}"
        assert b"replay%20%231" in request.url.raw_path
        assert request.headers["Accept"] == "application/vnd.github.raw+json"
        ref = request.url.params["ref"]
        raw_requests.append(ref)
        return httpx.Response(
            200, text={parent_commit: "old line\n", feature_commit: "new line\n"}[ref]
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = GitHubIssuesClient(http_client)
        patch = await client._get_author_patch(
            "octocat/example-repo", 42, "reporter", "See proxy.py for the concrete change."
        )

    assert raw_requests == [parent_commit, feature_commit]
    assert filename in patch
    assert "-old line" in patch and "+new line" in patch
    assert "unrelated.py" not in patch
