#!/usr/bin/env bash
#
# Run multi-stage pipeline trial with checkpoint support
#
# Key Parameters:
#   --repo-id            Repository specification (required): owner_repo:start_end
#   --trial-name         Trial name (required)
#   --config-file        Pipeline config (default: multi_stage_pipeline.yaml)
#   --agents             Comma-separated agent list (default: claudecode)
#   --max-retries        Maximum recovery retries per stage (default: 2)
#   --allow-rerun        Allow reusing existing trial name, skip auto-increment
#                        Useful for resuming from checkpoint after fixing errors
#   --rerun-from-stage   Rerun pipeline from specific stage (e.g., stage2_milestone_consolidation)
#                        Creates new workspace with completed stages copied from source trial
#   --rerun-postproc     Rerun Phase 3, 4, 5 (postprocessing) after Phase 2 completion
#                        Requires Phase 2 to be fully completed. Uses same trial name.
#
# Examples:
#   # First run (creates test_trial):
#   ./scripts/run_multi_stage.sh --repo-id urllib3_urllib3:2.0.6-2.3.0 --trial-name test_trial
#
#   # Resume from checkpoint after fixing errors (reuses test_trial):
#   ./scripts/run_multi_stage.sh --repo-id urllib3_urllib3:2.0.6-2.3.0 --trial-name test_trial --allow-rerun
#
#   # Rerun postprocessing phases (Phase 3, 4, 5) after Phase 2 completion:
#   ./scripts/run_multi_stage.sh --repo-id urllib3_urllib3:2.0.6-2.3.0 --trial-name test_trial --rerun-postproc
#
#   # Rerun from stage2 (creates test_trial_rerun_stage2 with stage1 output copied):
#   ./scripts/run_multi_stage.sh --repo-id urllib3_urllib3:2.0.6-2.3.0 --trial-name test_trial --rerun-from-stage stage2_milestone_consolidation
#

set -euo pipefail

# Load common utilities
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

# Show help if requested
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  python_cmd=$(check_python_env)
  "$python_cmd" -m deepcommit.cli pipeline-multi run --help
  exit 0
fi

# Save original arguments before validation
original_args=("$@")

# Basic parameter validation
has_repo_id=false
has_trial_name=false
repo_id_value=""

# Parse arguments to check for required parameters
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-id)
      has_repo_id=true
      repo_id_value="${2:-}"
      shift 2
      ;;
    --trial-name)
      has_trial_name=true
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

# Check required parameters
if [[ "$has_repo_id" == "false" ]]; then
  error_exit "Missing required parameter: --repo-id
Example: $0 --repo-id urllib3_urllib3:2.0.6-2.3.0 --trial-name my_multi_stage_trial
Example: $0 --config-file custom_pipeline.yaml --repo-id urllib3_urllib3:2.0.6-2.3.0 --trial-name my_trial"
fi

if [[ "$has_trial_name" == "false" ]]; then
  error_exit "Missing required parameter: --trial-name
Example: $0 --repo-id urllib3_urllib3:2.0.6-2.3.0 --trial-name my_multi_stage_trial
Example: $0 --config-file custom_pipeline.yaml --repo-id urllib3_urllib3:2.0.6-2.3.0 --trial-name my_trial"
fi

# Validate repo_id format
if [[ -n "$repo_id_value" ]]; then
  validate_repo_id "$repo_id_value"
fi

# Check Python environment
python_cmd=$(check_python_env)

# Run the multi-stage trial
"$python_cmd" -m deepcommit.cli pipeline-multi run "${original_args[@]}"
