import json
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "notify.yml"


def test_notification_crons_match_monitor_groups():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    triggered_crons = [entry["cron"] for entry in workflow["on"]["schedule"]]
    routes = json.loads(workflow["env"]["MONITOR_SCHEDULES"])

    # GitHub needs the triggers in on.schedule, and the shell needs the group
    # table. Adding a group must keep both entries in sync.
    assert sorted(triggered_crons) == sorted(route["cron"] for route in routes.values())
    assert len(triggered_crons) == len(set(triggered_crons))
    assert {
        "veryfast": ("7,37 * * * *", 2),
        "fast": ("11 * * * *", 3),
        "normal": ("13 */3 * * *", 5),
        "slow": ("17 */12 * * *", 14),
        "daily": ("9 14 * * *", 25),
        "weekly": ("19 14 * * 1", 170),
    }.items() <= {
        group: (route["cron"], route["lookback_hours"]) for group, route in routes.items()
    }.items()
