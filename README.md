# OSS Notifier Agent

[![CI](https://github.com/tmtabor/oss-notifier-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/tmtabor/oss-notifier-agent/actions/workflows/ci.yml)

Scans a configured list of GitHub repositories on a schedule. Skips issues with an assignee, a
linked or referencing pull request, or a clear claim in the issue or its comments. An LLM reads
the remaining issues and their discussion to assess beginner suitability and exclude active claims
or complete fixes that the issue author or a maintainer has said can be submitted as a PR. The
pipeline emails a digest of the matches.
Runs entirely on GitHub Actions.

Built on [agent-template](https://github.com/tmtabor/agent-template).

**This is a GitHub template repository.** Click **Use this template** to create your own copy.
No instance-specific configuration — your repo watch list, labels, search terms, or credentials —
should ever be committed. Configure your copy through GitHub Actions secrets and variables; see
[Use this template](#use-this-template) below.

## Stack
- Python 3.13, uv
- Pydantic AI v2 (agents) + pydantic-evals (evals)
- Logfire (observability)
- pytest + pytest-asyncio
- GitHub Actions (scheduling), GitHub Search API (ingestion), SMTP or Postmark (email delivery)

## How it works

```
GitHub Search API → timeline and filters → [triage agent, one call per issue] → digest builder → SMTP or Postmark
```

The triage agent (`agent/agents/single.py`) checks availability and beginner suitability in one
call. The pipeline skips an issue if GitHub's timeline is unavailable or the full discussion
exceeds the triage budget.

## Use this template

1. Click **Use this template** → **Create a new repository** on GitHub.
2. **Write your repo watch list** as YAML (schema and examples in `.env.example`), and set it as
   the value of a new **`AGENT_PIPELINE_CONFIG`** secret
   (Settings → Secrets and variables → Actions → New repository secret). This is your watch list —
   it's never committed to git.
3. **Add the secrets for your model and mail provider**. The default model uses Google/Gemini.
4. **Enable and trigger the workflow manually** once (Actions → OSS Notifier Agent → Run workflow)
   to confirm an email arrives before trusting the cron schedule. GitHub disables scheduled
   workflows on new repositories created from a template until you enable them.

| Secret | Required | Notes |
|---|---|---|
| `AGENT_PIPELINE_CONFIG` | Always | Your repo watch list — see step 2 above |
| `GOOGLE_API_KEY` | If `AGENT_MODEL` is a `google:...` model (the default) | |
| `ANTHROPIC_API_KEY` | For Anthropic API key authentication | |
| `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` | For an Anthropic-compatible gateway that uses bearer authentication | |
| `OPENAI_API_KEY` | If `AGENT_MODEL` is an `openai:...` model | |
| `DASHSCOPE_API_KEY`, `DASHSCOPE_URL` | If `AGENT_MODEL` is `dashscope` | DashScope credentials and OpenAI-compatible endpoint |
| `SMTP_EMAIL`, `SMTP_PASSWORD` | For SMTP | Mailbox address and SMTP authorization code |
| `POSTMARK_SERVER_TOKEN` | For Postmark | From your Postmark server |
| `AGENT_EMAIL_FROM`, `AGENT_EMAIL_TO` | For Postmark or to override SMTP addresses | SMTP defaults both to `SMTP_EMAIL` |
| `LOGFIRE_TOKEN` | Optional | Falls back to console-only tracing if unset |

`GITHUB_TOKEN` needs no setup — GitHub Actions provides it automatically for reading/searching
public repo issues. `AGENT_MODEL` itself isn't sensitive; set it as a repo **variable**
(Settings → Secrets and variables → Actions → Variables) if you want to override the default, or
leave it unset.

For DashScope, set `AGENT_MODEL=dashscope` and set `DASHSCOPE_MODEL` to the model name
in repository variables. For local runs, set the same names in the environment.

For SMTP over TLS, also set the repository variable `SMTP_SERVER` (e.g. `smtp.163.com`).
`SMTP_PORT` defaults to `465`.

### Repository schedules

Each repository in `AGENT_PIPELINE_CONFIG` can set one `schedule_group`. Configured
`vllm-project/vllm` and `sgl-project/sglang` entries default to `veryfast`, and all other entries
default to `daily`. An explicit group overrides either default.

| Group | Cadence | UTC cron | Issue lookback |
|---|---|---|---|
| `veryfast` | Every 30 minutes | `7,37 * * * *` | 2 hours |
| `fast` | Every hour | `11 * * * *` | 3 hours |
| `normal` | Every 3 hours | `13 */3 * * *` | 5 hours |
| `slow` | Every 12 hours | `17 */12 * * *` | 14 hours |
| `daily` | Every day | `9 14 * * *` | 25 hours |
| `weekly` | Every Monday | `19 14 * * 1` | 170 hours |

To add a custom group, add a unique cron under `on.schedule` and its group and lookback under
`MONITOR_SCHEDULES` in [notify.yml](.github/workflows/notify.yml). Set `schedule_group` on its
repository entries. Use a lowercase name with letters, digits, or hyphens, starting with a letter.
Give the lookback a buffer for delayed runs.

On manual Actions runs, select a `group` to use its lookback, or leave it blank to scan all
repositories with the usual 25-hour window. Set `dry_run` to preview without sending or writing
notification history.

Actions runs share `sent-issues.json` on the `notifier-state` branch, created automatically on the
first real run with configured repositories. The notify job uses `contents: write` for this branch.
The file records Issue URLs and send times, so it is visible on public notifier repositories.
The pipeline skips recorded Issues before triage and updates the file after the mail provider
accepts a digest. If a run stops between acceptance and recording, a later scan may resend the
digest. The first run starts with empty history unless you seed the state file with previously sent
Issue URLs and their timestamps.

## Local development

```bash
# Install dependencies
uv sync --group dev

# Optional (Claude Code only): install the pydantic-ai and logfire skills
uvx library-skills install --all --claude

# Copy and configure environment
cp .env.example .env
# Edit .env: add your GOOGLE_API_KEY (or override AGENT_MODEL + the matching provider key), and
# uncomment/edit the AGENT_PIPELINE_CONFIG example with your own repos

# Run unit tests (no API calls, no API key needed)
uv run pytest

# Run evals — requires a real API key, see Evals below
uv run pytest -m eval

# Run the full pipeline once, locally
uv run python -m agent.pipeline.run

# Same, but print the digest to the console instead of emailing it — everything
# up through triage still runs for real (GitHub search + real model calls),
# only email delivery is skipped
uv run python -m agent.pipeline.run --dry-run

# Preview one group locally with the configured local lookback
uv run python -m agent.pipeline.run --group veryfast --dry-run

# Lint
uv run ruff check .

# Format
uv run ruff format .
```

## Project structure

```
agent/
├── config.py           # Settings — validates the provider API key at import time (raises if missing)
├── logging.py           # Logfire setup — configure_logging(), get_logger()
├── agents/
│   ├── __init__.py       #   canonical names (run_agent, AgentOutput, …)
│   └── single.py          #   the triage agent — classifies one issue per call
├── prompts/
│   ├── system.txt         # Triage criteria — edit this to change what counts as "good first issue"
│   └── templates.py        # load_prompt(), render_issue_prompt()
└── pipeline/             # Deterministic orchestration — NOT agent tools
    ├── config.py           # Pipeline config loading (AGENT_PIPELINE_CONFIG env var / secret)
    ├── models.py            # Issue model
    ├── github_client.py     # GitHub Search API client
    ├── email.py              # SMTP or Postmark digest delivery
    ├── sent_state.py         # Shared GitHub Actions notification history
    └── run.py                 # Entrypoint: uv run python -m agent.pipeline.run

tests/    # Unit tests against TestModel and mocked HTTP — no API calls, no API key needed
evals/    # Pass/fail + dataset + LLM-as-judge evals — real API calls, run with -m eval
.github/workflows/
├── ci.yml       # CI: ruff check, format check, unit tests (no secrets needed)
└── notify.yml    # The scheduled digest run (needs the secrets above)
```

## Configuration

All settings are read from the environment (see `.env.example`). Agent-specific variables carry an
`AGENT_` prefix so a generic name like `MODEL` in your shell can't silently change the provider;
API keys and `LOGFIRE_TOKEN` keep their standard names because the provider SDKs read those exact
variables directly.

| Variable | Default | Notes |
|---|---|---|
| `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | Required for whichever provider `AGENT_MODEL` uses. `Settings` validates this at import time and raises immediately if it's missing — not a lazy/runtime check. |
| `AGENT_MODEL` | `google:gemini-3.1-flash-lite` | Per-issue triage is a cheap, high-volume classification task, so the default is a small/fast model. Any pydantic-ai model string works, including `ollama:*` for local models (no API key needed). |
| `AGENT_JUDGE_MODEL` | `anthropic:claude-sonnet-5` | Used only by the LLM-as-judge evals. Kept on Anthropic regardless of `AGENT_MODEL` to avoid self-assessment bias. |
| `LOGFIRE_TOKEN` | unset | If set, traces go to Logfire cloud. If unset, traces print to the console — no separate dev-mode flag needed. |
| `AGENT_LOG_LEVEL` | `INFO` | Standard Python logging level. |
| `GITHUB_TOKEN` | unset | GitHub Search API auth. Provided automatically in Actions; optional (rate-limited) locally. |
| `AGENT_PIPELINE_CONFIG` | — | Required. YAML repo watch list — see [Use this template](#use-this-template). |
| `POSTMARK_SERVER_TOKEN` / `AGENT_EMAIL_FROM` / `AGENT_EMAIL_TO` | — | Required to actually send the digest email. |
| `AGENT_SEARCH_WINDOW_HOURS` | `25` | Search lookback for local and full manual runs. Scheduled and group-specific Actions runs use the group's lookback. |
| `AGENT_MAX_ISSUE_BODY_CHARS` | `4000` | Body characters used to assess beginner suitability. Availability checks also consider the rest of the body and the discussion. |
| `AGENT_MAX_ISSUES_PER_RUN` | `30` | Cost guardrail across the whole run, on top of the triage agent's own `USAGE_LIMITS`. |

## Usage limits

`agent/agents/single.py` defines a `USAGE_LIMITS` constant passed to every triage call — a
guardrail against runaway agentic loops. `request_limit` caps model round-trips; `total_tokens_limit`
caps overall spend. Exceeding either raises `UsageLimitExceeded` instead of silently burning
tokens. This is a single structured-output call per issue (no tool loop), so the limits are tuned
low; `AGENT_MAX_ISSUES_PER_RUN` is the guardrail for the whole batch.

## Customizing the triage criteria

Edit `agent/prompts/system.txt` to change what counts as a "good first issue." It's loaded via
`load_prompt("system")` in `agent/prompts/templates.py`.

## Observability

All agent runs and model requests are automatically traced via `logfire.instrument_pydantic_ai()`
— no per-agent setup needed. Cloud vs. console output is controlled by `LOGFIRE_TOKEN`, see
Configuration above. Each pipeline run is also wrapped in a `digest_run` Logfire span.

## Evals

- Pass/fail evals: `evals/test_pass_fail.py` — includes a `pydantic_evals` Dataset eval driven by
  `evals/fixtures/triage_cases.json` (synthetic issue text, safe to commit). Add cases to that JSON
  file to grow the eval.
- LLM-as-judge evals: `evals/test_llm_judge.py` — graded by `AGENT_JUDGE_MODEL`, checking that
  `reasoning`/`summary` are faithful to the issue content.

Both files share the same `@pytest.mark.eval` marker. `uv run pytest -m eval` runs all of them and
requires a real API key; the LLM-judge evals also cost money (an extra model call per test).

## License

BSD 3-Clause — see [LICENSE](LICENSE).
