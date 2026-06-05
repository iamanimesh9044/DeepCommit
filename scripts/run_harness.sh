#!/usr/bin/env bash
#
# Run harness pipeline trial
#
# Key Parameters:
#   --repo-id            Repository specification (required): owner_repo:start_end
#   --trial-name         Trial name (required)
#   --config-file        Pipeline config (default: harness_pipeline.yaml)
#   --agents             Comma-separated agent list (default: claudecode)
#   --max-retries        Maximum recovery retries per stage (default: 2)
#   --source-result      Source result directory for milestones.csv/dependencies.csv
#                        If not provided, auto-resolved from trial_name
#   --allow-rerun        Allow reusing existing trial name, skip auto-increment
#   --dry-run            Only test setup without executing agent
#
# Examples:
#   # Run with auto-resolved source_result (from trial_name='baseline'):
#   ./scripts/run_harness.sh --repo-id apache_dubbo:dubbo-3.3.3_dubbo-3.3.6 --trial-name baseline
#
#   # Run with explicit source_result:
#   ./scripts/run_harness.sh --repo-id apache_dubbo:dubbo-3.3.3_dubbo-3.3.6 --trial-name test_001 \
#       --source-result apache_dubbo_dubbo-3.3.3_dubbo-3.3.6_baseline
#
#   # Dry run (test setup only):
#   ./scripts/run_harness.sh --repo-id apache_dubbo:dubbo-3.3.3_dubbo-3.3.6 --trial-name baseline --dry-run
#
#   # Resume with allow-rerun:
#   ./scripts/run_harness.sh --repo-id apache_dubbo:dubbo-3.3.3_dubbo-3.3.6 --trial-name baseline --allow-rerun
#

set -euo pipefail

# Load common utilities
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

# Show help if requested
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  python_cmd=$(check_python_env)
  "$python_cmd" -m deepcommit.cli harness run --help
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
Example: $0 --repo-id apache_dubbo:dubbo-3.3.3_dubbo-3.3.6 --trial-name baseline
Example: $0 --repo-id urllib3_urllib3:2.0.6-2.3.0 --trial-name v1 --dry-run"
fi

if [[ "$has_trial_name" == "false" ]]; then
  error_exit "Missing required parameter: --trial-name
Example: $0 --repo-id apache_dubbo:dubbo-3.3.3_dubbo-3.3.6 --trial-name baseline
Example: $0 --repo-id urllib3_urllib3:2.0.6-2.3.0 --trial-name v1 --dry-run"
fi

# Validate repo_id format
if [[ -n "$repo_id_value" ]]; then
  validate_repo_id "$repo_id_value"
fi

# Check Python environment
python_cmd=$(check_python_env)

# Run the harness trial
"$python_cmd" -m deepcommit.cli harness run "${original_args[@]}"
