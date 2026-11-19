"""Prompt template loader.

Loads system prompts from .txt files in this directory.
Centralizes prompt loading so all agents use the same pattern.
"""

import json
from pathlib import Path

from agent.pipeline.models import Issue

PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """Load a prompt template from a .txt file.

    Args:
        name: Filename without extension (e.g., "system" loads "system.txt")

    Returns:
        The prompt text as a string.

    Raises:
        FileNotFoundError: If the prompt file does not exist.
    """
    prompt_path = PROMPTS_DIR / f"{name}.txt"
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"Prompt file not found: {prompt_path}. "
            f"Available prompts: {[f.stem for f in PROMPTS_DIR.glob('*.txt')]}"
        )
    return prompt_path.read_text(encoding="utf-8").strip()


def render_issue_prompt(issue: Issue) -> str:
    """Pass issue text and GitHub-supplied author metadata as structured data."""
    return json.dumps(
        {
            "repository": issue.repo,
            "title": issue.title,
            "labels": issue.labels,
            "url": issue.html_url,
            "issue_author": issue.author,
            "body": issue.body,
            "body_truncated": issue.truncated,
            "body_remainder": issue.body_remainder,
            "comments": [comment.model_dump() for comment in issue.comments],
        },
        ensure_ascii=False,
    )
