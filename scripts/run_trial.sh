#!/usr/bin/env bash

set -euo pipefail

# Load common utilities
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

# Show help if requested
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  python_cmd=$(check_python_env)
  "$python_cmd" -m deepcommit.cli trial run --help
  exit 0
fi

# Save original arguments before validation
original_args=("$@")

# Basic parameter validation
has_prompt_file=false
has_repo_id=false
has_trial_name=false
repo_id_value=""

# Parse arguments to check for required parameters
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prompt-file)
      has_prompt_file=true
      shift 2
      ;;
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
if [[ "$has_prompt_file" == "false" ]]; then
  error_exit "Missing required parameter: --prompt-file
Example: $0 --prompt-file example.tmpl --repo-id urllib3_urllib3:2.0.6_2.3.0 --trial-name my_trial"
fi

if [[ "$has_repo_id" == "false" ]]; then
  error_exit "Missing required parameter: --repo-id
Example: $0 --prompt-file example.tmpl --repo-id urllib3_urllib3:2.0.6_2.3.0 --trial-name my_trial"
fi

if [[ "$has_trial_name" == "false" ]]; then
  error_exit "Missing required parameter: --trial-name
Example: $0 --prompt-file example.tmpl --repo-id urllib3_urllib3:2.0.6_2.3.0 --trial-name my_trial"
fi

# Validate repo_id format
if [[ -n "$repo_id_value" ]]; then
  validate_repo_id "$repo_id_value"
fi

# Check Python environment
python_cmd=$(check_python_env)

# Run the trial
"$python_cmd" -m deepcommit.cli trial run "${original_args[@]}"
