"""Data models for the GitHub issue ingestion pipeline."""

from __future__ import annotations

from pydantic import BaseModel, Field


class IssueComment(BaseModel):
    """A timeline comment with author metadata supplied by GitHub."""

    author: str
    author_association: str
    body: str


class Issue(BaseModel):
    """A GitHub issue with the discussion needed to check availability."""

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
