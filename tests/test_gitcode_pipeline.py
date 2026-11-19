import pytest
from pydantic import ValidationError

from agent.pipeline import run
from agent.pipeline.config import PipelineConfig, RepoConfig
from agent.pipeline.models import Issue, IssueComment


def test_watch_list_accepts_gitcode_urls_and_keeps_github_default():
    config = PipelineConfig.model_validate(
        {
            "repositories": [
                {"repo": "octocat/example"},
                {"repo": "https://gitcode.com/Ascend/MindIE-Motor"},
                {"repo": "Ascend/another", "platform": "gitcode"},
            ]
        }
    )

    assert [(repo.platform, repo.repo) for repo in config.repositories] == [
        ("github", "octocat/example"),
        ("gitcode", "Ascend/MindIE-Motor"),
        ("gitcode", "Ascend/another"),
    ]


@pytest.mark.parametrize("repo", ["https://gitcode.com/Ascend/x/issues", "Ascend/.."])
def test_rejects_gitcode_non_repository_paths(repo):
    with pytest.raises(ValidationError, match="owner/name"):
        RepoConfig(repo=repo, platform="gitcode")


async def test_fetch_only_routes_gitcode_and_skips_model_and_email(monkeypatch, capsys):
    searched = []

    class FakeGitCodeClient:
        async def search_issues(self, repo_config, *_):
            searched.append(repo_config.repo)
            return []

        async def aclose(self):
            pass

    def unexpected(*args):
        raise AssertionError("Fetch-only should only run the configured GitCode scan")

    monkeypatch.setattr(run, "GitCodeIssuesClient", FakeGitCodeClient)
    monkeypatch.setattr(run, "GitHubIssuesClient", unexpected)
    monkeypatch.setattr(run, "triage_issues", unexpected)
    monkeypatch.setattr(run, "send_digest", unexpected)
    monkeypatch.setattr(run.settings, "google_api_key", None)
    monkeypatch.setattr(run.settings, "model", "google:gemini-3.1-flash-lite")
    monkeypatch.setattr(
        run.settings,
        "pipeline_config",
        "repositories:\n  - repo: https://gitcode.com/Ascend/MindIE-Motor",
    )

    await run.main(fetch_only=True)

    assert searched == ["Ascend/MindIE-Motor"]
    assert "0 candidate issues" in capsys.readouterr().out


async def test_gitcode_fast_run_uses_its_own_platform_and_group(monkeypatch, capsys):
    searched = []

    class FakeGitCodeClient:
        async def search_issues(self, repo_config, *_):
            searched.append(repo_config.repo)
            return []

        async def aclose(self):
            pass

    def unexpected_github_client():
        pytest.fail("A GitCode run must not query GitHub")

    monkeypatch.setattr(run, "GitHubIssuesClient", unexpected_github_client)
    monkeypatch.setattr(run, "GitCodeIssuesClient", FakeGitCodeClient, raising=False)
    monkeypatch.setattr(
        run.settings,
        "pipeline_config",
        """
repositories:
  - repo: github/independent
    schedule_group: github-only
  - repo: github/fast-project
    schedule_group: fast
  - repo: https://gitcode.com/Ascend/MindIE-Motor
  - repo: https://gitcode.com/Ascend/daily-project
""",
    )

    await run.main(
        platform="gitcode", group="fast", known_groups=["fast", "daily"], fetch_only=True
    )

    assert searched == ["Ascend/MindIE-Motor"]
    assert "from 1 configured repositories" in capsys.readouterr().out


async def test_fetch_only_reports_failure_when_the_only_repo_cannot_be_read(monkeypatch):
    class UnavailableGitCodeClient:
        async def search_issues(self, *_):
            raise RuntimeError("GitCode is unavailable")

        async def aclose(self):
            pass

    monkeypatch.setattr(run, "GitCodeIssuesClient", UnavailableGitCodeClient)
    monkeypatch.setattr(
        run.settings,
        "pipeline_config",
        "repositories:\n  - repo: https://gitcode.com/Ascend/MindIE-Motor",
    )

    with pytest.raises(RuntimeError, match="Could not search any configured repository") as error:
        await run.main(fetch_only=True)

    assert str(error.value.__cause__) == "GitCode is unavailable"


@pytest.mark.parametrize(
    "excluded",
    [
        {"title": "[RFC]: Proposal for the deployment API"},
        {"labels": ["kind: RFC"]},
        {"body": "Overview\n\nThis issue is an RFC."},
        {"title": "【已作废】接口连通性测试 issue，请忽略"},
        {"labels": ["status: voided"]},
        {"body_remainder": "\n\n本 Issue 已作废，请忽略。"},
        {
            "comments": [
                IssueComment(author="Reporter", author_association="NONE", body="此工单已作废。")
            ]
        },
    ],
)
async def test_explicit_rfc_and_void_issues_are_filtered_before_candidate_limit(
    excluded, monkeypatch
):
    issue = Issue(
        repo="Ascend/MindIE-Motor",
        title="Fix RFC 9110 parsing for a void return value",
        body=(
            "This issue is an RFC 9110 compliance bug.\n"
            "> This issue has been withdrawn.\n"
            "```\nThis issue is an RFC.\n```"
        ),
        labels=["documentation"],
        html_url="https://gitcode.com/Ascend/MindIE-Motor/issues/100",
        created_at="2026-11-19T00:00:00Z",
        author="reporter",
        comments=[
            IssueComment(
                author="visitor", author_association="NONE", body="This issue has been withdrawn."
            )
        ],
    )

    class FakeClient:
        async def search_issues(self, *_):
            return [issue.model_copy(update={"title": "First issue", **excluded}), issue]

    monkeypatch.setattr(run.settings, "max_issues_per_run", 1)
    config = PipelineConfig(repositories=[RepoConfig(repo=issue.repo, platform="gitcode")])

    assert await run.collect_issues(config, None, FakeClient()) == [issue]
