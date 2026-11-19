"""Triage agent: assesses an issue's availability and beginner scope.

One agent, one task — this run is called once per candidate issue by
agent/pipeline/run.py. It does no fetching, filtering, or emailing itself;
that's deterministic Python (see agent/pipeline/). The agent interprets
the issue and its discussion to decide whether it is available and approachable
for a first-time contributor.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.alibaba import AlibabaProvider
from pydantic_ai.usage import UsageLimits

from agent.config import RuntimeSettings, settings
from agent.logging import configure_logging, get_logger
from agent.prompts.templates import load_prompt

logger = get_logger(__name__)

# Guardrail against runaway agentic loops. A run that exceeds either limit
# raises UsageLimitExceeded instead of silently burning tokens. This agent
# makes one structured-output classification call per issue, with up to two
# retries for malformed model output.
USAGE_LIMITS = UsageLimits(request_limit=3, total_tokens_limit=20_000)


# --- Output type ---
class AgentOutput(BaseModel):
    """Triage verdict for a single issue."""

    is_available: bool = Field(
        description="False for RFCs, explicitly void issues, active claims, or a ready-to-submit fix provided by the issue author or a maintainer."
    )
    is_good_first_issue: bool
    reasoning: str = Field(description="用一句简体中文，依据 Issue 正文或评论说明判断理由。")
    summary: str = Field(description="用两句简体中文分别概述需要改动的内容，以及可以从哪里着手。")


# --- Dependencies ---
# No external dependencies — the agent only reasons over the issue text
# passed in the prompt.
@dataclass
class AgentDeps:
    """Runtime dependencies injected into the agent."""

    pass


def _build_model(config: RuntimeSettings) -> str | OpenAIChatModel:
    config.check_provider_key()
    if config.model != "dashscope":
        return config.model

    assert config.dashscope_api_key is not None
    assert config.dashscope_url is not None
    assert config.dashscope_model is not None
    return OpenAIChatModel(
        config.dashscope_model,
        provider=AlibabaProvider(
            api_key=config.dashscope_api_key.get_secret_value(),
            base_url=config.dashscope_url.rstrip("/").removesuffix("/chat/completions"),
        ),
    )


# --- Agent definition ---
agent: Agent[AgentDeps, AgentOutput] = Agent(
    _build_model(settings),
    name="agent",  # labels this agent's run span in Logfire traces
    output_type=AgentOutput,
    retries={"output": 2},
    deps_type=AgentDeps,
    instructions=load_prompt("system"),  # loads agent/prompts/system.txt
)


async def run_agent(user_input: str, deps: AgentDeps | None = None) -> AgentOutput:
    """Classify one issue, given a rendered issue prompt.

    Args:
        user_input: The issue prompt, built by
            agent.prompts.templates.render_issue_prompt().
        deps: Runtime dependencies. Created with defaults if not provided.

    Returns:
        Validated AgentOutput instance.
    """
    if deps is None:
        deps = AgentDeps()

    logger.info("Running triage agent", extra={"user_input": user_input})

    result = await agent.run(user_input, deps=deps, usage_limits=USAGE_LIMITS)

    logger.info("Agent run complete", extra={"output": result.output})
    return result.output


if __name__ == "__main__":
    import asyncio

    configure_logging()
    output = asyncio.run(
        run_agent(
            "Repository: octocat/example-repo\n"
            "Title: Fix typo in README\n"
            "Labels: good first issue\n"
            "URL: https://github.com/octocat/example-repo/issues/1\n\n"
            "Body:\nThe word 'recieve' on line 12 of README.md should be 'receive'."
        )
    )
    print(output)
