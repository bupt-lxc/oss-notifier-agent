"""Recognize explicit RFC and void markers before model triage."""

import re
from unicodedata import normalize

from agent.pipeline.claims import unquoted_sentences
from agent.pipeline.models import Issue

_RFC_MARKERS = {"rfc", "request for comments"}
_VOID_MARKERS = {
    "canceled",
    "cancelled",
    "invalid",
    "invalidated",
    "obsolete",
    "void",
    "voided",
    "withdrawn",
    "作废",
    "已作废",
    "已取消",
    "已废弃",
    "已撤回",
    "无效",
}
_TITLE_PREFIX = re.compile(r"^\[([^\]]+)\]|^\(([^)]+)\)|^([^:]+?)(?:[:,;]|\s[-–—]\s)")
_LABEL_PREFIX = re.compile(r"^(?:type|kind|status)\s*[:/-]\s*")
_BRACKETS = str.maketrans("【】", "[]")
_RFC_DECLARATION = re.compile(
    r"^(?:(?:this issue is (?:an? )?|这是(?:一份|一个)?\s*)rfc(?=$|[.,:;。！？!]|\s+(?:to|for)\b)"
    r"|this rfc (?:proposes|requests)\b|(?:本|此)\s*rfc\s*(?:提议|建议|征求))"
)
_VOID_DECLARATION = re.compile(
    r"^(?:this (?:issue|ticket) (?:is|was|has been) "
    r"|(?:本|此|这个|这条)\s*(?:issue|工单|问题|议题)\s*(?:已经?|现已)?\s*)"
    rf"({'|'.join(sorted(_VOID_MARKERS))})(?=$|[.,:;。！？!]|\s+(?:because|and|as)\b|请忽略)"
)
_REPOSITORY_ROLES = {"OWNER", "MEMBER", "COLLABORATOR"}


def _marker_reason(marker: str) -> str | None:
    if marker in _RFC_MARKERS:
        return "RFC"
    if marker in _VOID_MARKERS:
        return "void"
    return None


def _declared_reason(text: str) -> str | None:
    for sentence in unquoted_sentences(text):
        statement = normalize("NFKC", sentence).casefold().strip("*_# ")
        if _RFC_DECLARATION.match(statement):
            return "RFC"
        if _VOID_DECLARATION.match(statement):
            return "void"
    return None


def exclusion_reason(issue: Issue) -> str | None:
    """Return the exclusion indicated by issue metadata or an explicit declaration."""
    title = normalize("NFKC", issue.title).casefold().strip().translate(_BRACKETS)
    prefix = _TITLE_PREFIX.match(title)
    title_marker = next(group for group in prefix.groups() if group).strip() if prefix else title
    if reason := _marker_reason(title_marker) or _declared_reason(issue.title):
        return reason

    for label in issue.labels:
        marker = _LABEL_PREFIX.sub("", normalize("NFKC", label).casefold().strip())
        if reason := _marker_reason(marker):
            return reason

    if reason := _declared_reason(issue.body + issue.body_remainder):
        return reason
    for comment in issue.comments:
        same_author = bool(issue.author) and comment.author.casefold() == issue.author.casefold()
        if same_author or comment.author_association in _REPOSITORY_ROLES:
            if normalize("NFKC", comment.body).casefold().strip(" .!。！") in _VOID_MARKERS:
                return "void"
            if reason := _declared_reason(comment.body):
                return reason
    return None
