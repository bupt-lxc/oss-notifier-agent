from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".gitcode" / "workflows"


def read_workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


@pytest.mark.parametrize(
    ("name", "job", "cron", "group", "lookback"),
    [
        ("notify.yml", "daily", "9 14 * * *", "daily", "25"),
        ("notify-fast.yml", "fast", "11 * * * *", "fast", "3"),
    ],
)
def test_gitcode_scheduled_workflows_select_a_fixed_group(name, job, cron, group, lookback):
    workflow = read_workflow(name)
    caller = workflow["jobs"][job]
    assert workflow["on"]["schedule"] == [{"cron": cron}]
    assert caller["uses"] == "./.gitcode/workflows/notify-scan.yml"
    assert caller["with"] == {"schedule_group": group, "lookback_hours": lookback}

    shared = read_workflow("notify-scan.yml")
    assert set(caller["with"]) <= shared["on"]["workflow_call"]["inputs"].keys()


def test_gitcode_manual_workflow_keeps_fetch_only_default():
    workflow = read_workflow("notify.yml")
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert {name: settings["default"] for name, settings in inputs.items()} == {
        "fetch_only": "true",
        "dry_run": "false",
        "gitcode_repo": "",
    }
    caller = workflow["jobs"]["manual"]
    assert caller["uses"] == "./.gitcode/workflows/notify-scan.yml"
    assert set(caller["with"]) == inputs.keys()
