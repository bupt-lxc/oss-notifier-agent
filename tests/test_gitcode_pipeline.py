import pytest
from pydantic import ValidationError

from agent.pipeline import run
from agent.pipeline.config import PipelineConfig, RepoConfig


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
