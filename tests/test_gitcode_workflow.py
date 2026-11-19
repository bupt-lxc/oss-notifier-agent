from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".gitcode" / "workflows"


def read_workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


@pytest.mark.parametrize(
    ("name", "cron", "group", "lookback"),
    [
        ("notify.yml", "9 14 * * *", "daily", "25"),
        ("notify-fast.yml", "11 * * * *", "fast", "3"),
    ],
)
def test_gitcode_scheduled_workflows_select_a_fixed_group_in_a_direct_job(
    name, cron, group, lookback
):
    workflow = read_workflow(name)
    assert workflow["on"]["schedule"] == [{"cron": cron}]
    job = workflow["jobs"]["notify"]
    assert job["env"]["SCHEDULE_GROUP"] == group
    assert job["env"]["LOOKBACK_HOURS"] == lookback
    assert job["steps"][-1]["run"] == "bash .gitcode/run-notify.sh"
    if group == "fast":
        assert "workflow_dispatch" not in workflow["on"]
        assert all(step.get("name") != "Read manual inputs" for step in job["steps"])


def test_gitcode_manual_workflow_reads_inputs_directly():
    workflow = read_workflow("notify.yml")
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert {name: settings["default"] for name, settings in inputs.items()} == {
        "fetch_only": "true",
        "dry_run": "false",
        "gitcode_repo": "",
    }
    step = next(
        step for step in workflow["jobs"]["notify"]["steps"] if step["name"] == "Read manual inputs"
    )
    assert "Manual" in step["if"]
    assert step["env"] == {
        "FETCH_ONLY_INPUT": "${{ inputs.fetch_only }}",
        "DRY_RUN_INPUT": "${{ inputs.dry_run }}",
        "GITCODE_REPO_INPUT": "${{ inputs.gitcode_repo }}",
    }
    assert ".gitcode/.notify-inputs" in step["run"]


def test_gitcode_workflows_share_runtime_environment():
    daily = read_workflow("notify.yml")["jobs"]["notify"]["steps"][-1]["env"]
    hourly = read_workflow("notify-fast.yml")["jobs"]["notify"]["steps"][-1]["env"]

    assert daily == hourly
    assert daily["EVENT_NAME"] == "${{ atomgit.event_name }}"
    assert daily["GITCODE_PIPELINE_CONFIG"] == "${{ secrets.GITCODE_PIPELINE_CONFIG }}"
    assert daily["AGENT_PIPELINE_CONFIG"] == "${{ secrets.AGENT_PIPELINE_CONFIG }}"
    assert "GITHUB_TOKEN" not in daily
    assert len(daily) <= 20  # AtomGit's step environment limit.
