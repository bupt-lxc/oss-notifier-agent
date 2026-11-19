#!/usr/bin/env bash
set -euo pipefail

[[ -n "${ANTHROPIC_BASE_URL:-}" ]] || unset ANTHROPIC_BASE_URL
[[ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]] || unset ANTHROPIC_AUTH_TOKEN
if [[ -z "${ANTHROPIC_API_KEY:-}" && -n "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
  export ANTHROPIC_API_KEY="$ANTHROPIC_AUTH_TOKEN"
fi

args=(--platform gitcode)
notify_mode=fetch
target=
event="${EVENT_NAME:-}"
if [[ "$event" == "Scheduler" || "$event" == "schedule" ]]; then
  group="${SCHEDULE_GROUP:?GitCode schedule group is required}"
  export AGENT_SEARCH_WINDOW_HOURS="${LOOKBACK_HOURS:?GitCode lookback is required}"
  args+=(--group "$group")
  printf 'GitCode schedule group: %s (lookback: %s hours)\n' "$group" "$AGENT_SEARCH_WINDOW_HOURS"
  scheduled_email="${AGENT_SCHEDULED_EMAIL:-}"
  if [[ "${scheduled_email,,}" == "true" ]]; then
    notify_mode=email
  fi
elif [[ -f .gitcode/.notify-inputs ]]; then
  mapfile -t manual_inputs < .gitcode/.notify-inputs
  notify_mode="${manual_inputs[0]}"
  target="${manual_inputs[1]:-}"
fi

case "$notify_mode" in
  fetch) args+=(--fetch-only) ;;
  preview) args+=(--dry-run) ;;
  email) ;;
  *) echo 'Unknown GitCode notification mode' >&2; exit 1 ;;
esac

AGENT_PIPELINE_CONFIG="${GITCODE_PIPELINE_CONFIG:-${AGENT_PIPELINE_CONFIG:-}}"
if [[ -n "$target" ]]; then
  printf 'Scanning GitCode repository %s\n' "$target"
  printf -v AGENT_PIPELINE_CONFIG 'repositories:\n  - repo: %s\n    platform: gitcode\n' "$target"
elif [[ -z "$AGENT_PIPELINE_CONFIG" ]]; then
  AGENT_PIPELINE_CONFIG=$'repositories:\n  - repo: Ascend/MindIE-Motor\n    platform: gitcode\n    schedule_group: fast\n'
fi
export AGENT_PIPELINE_CONFIG
exec python -m uv run --python 3.14 --locked --no-dev python -m agent.pipeline.run "${args[@]}"
