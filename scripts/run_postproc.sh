#!/usr/bin/env bash
set -euo pipefail

# Load common utilities
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

# Show help if requested or no arguments provided
if [[ $# -eq 0 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  python_cmd=$(check_python_env)
  "$python_cmd" -m deepcommit.cli postprocess --help
  exit 0
fi

# Check if first arg is a subcommand
subcommands=("analyze" "enrich" "all" "fix")
subcommand=""
if [[ " ${subcommands[*]} " =~ " ${1:-} " ]]; then
  subcommand="$1"
  shift
fi

# Validate minimum arguments (trial_name required, repo_id optional)
if [[ $# -lt 1 ]]; then
  error_exit "Insufficient arguments. Expected at least 1 argument.
Usage: $0 [analyze|enrich|all|fix] <trial_name> [repo_id] [options]
Example: $0 analyze my_trial urllib3_urllib3:2.0.6_2.3.0 --verbose
         $0 fix my_trial --dry-run

Run '$0 --help' for detailed help."
fi

trial_name="$1"
shift

# Check Python environment
python_cmd=$(check_python_env)

# Build command
if [[ -n "$subcommand" ]]; then
  "$python_cmd" -m deepcommit.cli postprocess "$subcommand" "$trial_name" "$@"
else
  "$python_cmd" -m deepcommit.cli postprocess "$trial_name" "$@"
fi
