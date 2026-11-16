"""Unit tests for agent.config.Settings — no network, no real API calls."""

import pytest
from pydantic import ValidationError

from agent.config import Settings


def test_empty_agent_model_env_var_falls_back_to_default(monkeypatch):
    """Regression test: notify.yml sets AGENT_MODEL: ${{ vars.AGENT_MODEL }}
    unconditionally. When that repo variable doesn't exist, GitHub Actions
    substitutes an empty string rather than omitting the key, so the env var
    is present but empty at runtime. Settings must treat that as unset and
    fall back to the code default, not pass "" to infer_model()."""
    monkeypatch.setenv("AGENT_MODEL", "")

    settings = Settings(_env_file=None)

    assert settings.model == "google:gemini-3.1-flash-lite"


def test_nonempty_agent_model_env_var_still_overrides(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "anthropic:claude-opus-4-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unit-test-dummy-key")

    settings = Settings(_env_file=None)

    assert settings.model == "anthropic:claude-opus-4-8"


def test_dashscope_reads_the_endpoint_and_model_from_environment(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "dashscope")
    monkeypatch.setenv("DASHSCOPE_MODEL", "qwen3.8-flash")
    monkeypatch.setenv("DASHSCOPE_URL", "https://dashscope.example/compatible-mode/v1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-dashscope-key")

    settings = Settings(_env_file=None)

    assert settings.dashscope_model == "qwen3.8-flash"
    assert settings.dashscope_url == "https://dashscope.example/compatible-mode/v1"
    assert settings.dashscope_api_key.get_secret_value() == "fake-dashscope-key"


def test_dashscope_requires_its_environment(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "dashscope")
    monkeypatch.delenv("DASHSCOPE_MODEL", raising=False)
    monkeypatch.delenv("DASHSCOPE_URL", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    with pytest.raises(ValidationError, match="DASHSCOPE_API_KEY"):
        Settings(_env_file=None)
