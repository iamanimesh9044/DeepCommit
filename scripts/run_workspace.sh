#!/usr/bin/env bash
#
# Run a multi-repo workspace trial (multi-stage pipeline over a merged workspace)
#
# Prerequisites: `deepcommit workspace preproc <manifest>` (or per-member preproc + `workspace merge`)
#
# Key Parameters:
#   --workspace     Workspace manifest, e.g. config/livekit_stack_v1.workspace.yaml (required)
#   --trial-name    Trial name (required)
#   Everything else is forwarded to `deepcommit workspace run`
#   (--config-file, --agents, --max-retries, --allow-rerun, --rerun-from-stage, --rerun-postproc, ...)
#
# Example:
#   ./scripts/run_workspace.sh --workspace config/livekit_stack_v1.workspace.yaml --trial-name v1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  python_cmd=$(check_python_env)
  "$python_cmd" -m deepcommit.cli workspace run --help
  exit 0
fi

workspace=""
has_trial_name=false
forward=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace)
      workspace="${2:-}"
      shift 2
      ;;
    --trial-name)
      has_trial_name=true
      forward+=("$1" "${2:-}")
      shift 2
      ;;
    *)
      forward+=("$1")
      shift
      ;;
  esac
done

if [[ -z "$workspace" ]]; then
  error_exit "Missing required parameter: --workspace
Example: $0 --workspace config/livekit_stack_v1.workspace.yaml --trial-name v1"
fi
if [[ "$has_trial_name" == "false" ]]; then
  error_exit "Missing required parameter: --trial-name
Example: $0 --workspace config/livekit_stack_v1.workspace.yaml --trial-name v1"
fi
require_file "$workspace"

python_cmd=$(check_python_env)
"$python_cmd" -m deepcommit.cli workspace run "$workspace" "${forward[@]}"
