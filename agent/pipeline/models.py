"""Data models for issue ingestion and triage."""

from __future__ import annotations

from pydantic import BaseModel, Field

# Leave room under the triage agent's token budget without dropping evidence.
MAX_ISSUE_CONTEXT_CHARS = 12_000


class IssueComment(BaseModel):
    """A discussion comment with author metadata supplied by the hosting platform."""

    author: str
    author_association: str
    body: str


class Issue(BaseModel):
    """An issue with the discussion needed to check availability."""

    repo: str
    title: str
    body: str
    labels: list[str]
    html_url: str
    created_at: str
    truncated: bool = False
    author: str = ""
    body_remainder: str = ""
    comments: list[IssueComment] = Field(default_factory=list)
