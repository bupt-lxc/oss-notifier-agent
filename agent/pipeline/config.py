"""Pipeline configuration: repository groups, labels, and search terms.

This is instance-specific configuration, not a secret credential, but it
still must never be committed — the template repo shouldn't carry any one
user's repo list in git history. It's read from the AGENT_PIPELINE_CONFIG
environment variable — in production that's populated from a GitHub Secret
of the same name; locally, set it in .env. See .env.example for the schema
to copy from.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.config import settings
from agent.logging import get_logger

logger = get_logger(__name__)

RepoPlatform = Literal["github", "gitcode"]

REPO_SCHEDULE_DEFAULTS = {
    ("github", "vllm-project/vllm"): "veryfast",
    ("github", "sgl-project/sglang"): "veryfast",
    ("gitcode", "ascend/mindie-motor"): "fast",
}


class RepoConfig(BaseModel):
    """One watched repository, with optional overrides of the defaults."""

    # extra="forbid": a misindented or misspelled key (e.g. "repositories"
    # nested under "defaults" by accident) should fail loudly at load time,
    # not get silently dropped and leave the pipeline watching zero repos.
    model_config = ConfigDict(extra="forbid")

    repo: str
    platform: RepoPlatform = "github"
    labels: list[str] | None = None
    search_terms: list[str] | None = None
    schedule_group: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]*$")

    @property
    def effective_schedule_group(self) -> str:
        return self.schedule_group or REPO_SCHEDULE_DEFAULTS.get(
            (self.platform, self.repo.casefold()), "daily"
        )

    @model_validator(mode="after")
    def normalize_repository(self) -> RepoConfig:
        if "://" in self.repo:
            url = urlsplit(self.repo)
            if (
                url.scheme != "https"
                or url.netloc.lower() not in {"github.com", "gitcode.com", "atomgit.com"}
                or url.query
                or url.fragment
            ):
                raise ValueError("Use owner/name or an HTTPS repository URL on GitHub or GitCode.")
            platform = "github" if url.netloc.lower() == "github.com" else "gitcode"
            if "platform" in self.model_fields_set and self.platform != platform:
                raise ValueError("Repository URL does not match the configured platform.")
            self.platform = platform
            self.repo = url.path.removeprefix("/").removesuffix("/").removesuffix(".git")
        elif self.platform == "github":
            return self
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo) or any(
            part in {".", ".."} for part in self.repo.split("/")
        ):
            raise ValueError("Repository path must be in owner/name format.")
        return self


class PipelineDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labels: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaults: PipelineDefaults = Field(default_factory=PipelineDefaults)
    repositories: list[RepoConfig] = Field(default_factory=list)

    def labels_for(self, repo: RepoConfig) -> list[str]:
        return repo.labels if repo.labels is not None else self.defaults.labels

    def search_terms_for(self, repo: RepoConfig) -> list[str]:
        return repo.search_terms if repo.search_terms is not None else self.defaults.search_terms


def load_config() -> PipelineConfig:
    """Load pipeline config from settings.pipeline_config (AGENT_PIPELINE_CONFIG).

    Raises:
        ValueError: The setting is unset or empty.
    """
    raw = settings.pipeline_config
    if not raw:
        raise ValueError(
            "AGENT_PIPELINE_CONFIG is not set. See .env.example for the schema and set it "
            "(in .env for local development, or as an Actions secret in production)."
        )

    logger.info("Loading pipeline config")
    data = yaml.safe_load(raw) or {}
    return PipelineConfig.model_validate(data)
