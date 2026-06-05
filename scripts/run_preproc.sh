#!/usr/bin/env bash
# Unified preprocessing script for deepcommit
#
# Usage:
#   ./scripts/run_preproc.sh <command> <repo_spec> [options]
#
# Commands:
#   collect     Collect raw data from GitHub API
#   codemaat    Generate CodeMaat analysis log
#   dag         Generate commit dependency DAG
#   all         Run all preprocessing steps (collect + codemaat + dag)
#
# Examples:
#   ./scripts/run_preproc.sh all urllib3_urllib3:2.0.6_2.3.0
#   ./scripts/run_preproc.sh collect urllib3_urllib3:2.0.6_2.3.0 --workers 4
#   ./scripts/run_preproc.sh dag urllib3_urllib3:2.0.6_2.3.0 --generate-image
#   ./scripts/run_preproc.sh codemaat urllib3_urllib3:2.0.6_2.3.0 --repo-src-dir src/

set -euo pipefail

# Load common utilities
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

# Show help if requested
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  python_cmd=$(check_python_env)
  cat << 'EOF'
Usage: run_preproc.sh <command> <repo_spec> [options]

Commands:
  collect     Collect raw data from GitHub API
  codemaat    Generate CodeMaat analysis log
  dag         Generate commit dependency DAG
  all         Run all preprocessing steps

Examples:
  run_preproc.sh all urllib3_urllib3:2.0.6_2.3.0
  run_preproc.sh collect urllib3_urllib3:2.0.6_2.3.0 --workers 8
  run_preproc.sh dag urllib3_urllib3:2.0.6_2.3.0 --generate-image

For command-specific options, run:
  run_preproc.sh <command> --help

EOF
  exit 0
fi

# Check if command is provided
if [[ $# -eq 0 ]]; then
  error_exit "Missing required argument: <command>

Usage: $0 <command> <repo_spec> [options]

Commands: collect, codemaat, dag, all

Examples:
  $0 all urllib3_urllib3:2.0.6_2.3.0
  $0 collect urllib3_urllib3:2.0.6_2.3.0 --workers 4

Run '$0 --help' for detailed help."
fi

command="$1"
shift

# Validate command
if [[ ! "$command" =~ ^(collect|codemaat|dag|all)$ ]]; then
  error_exit "Invalid command: $command
Valid commands are: collect, codemaat, dag, all"
fi

# Check if repo_spec is provided
if [[ $# -eq 0 ]]; then
  error_exit "Missing required argument: <repo_spec>

Usage: $0 $command <repo_spec> [options]

Note: repo_spec format is 'owner_repo:start_end'
  - owner_repo: GitHub repository owner and name (e.g., urllib3_urllib3)
  - start_end: Tag range (e.g., 2.0.6_2.3.0)

Examples:
  $0 $command urllib3_urllib3:2.0.6_2.3.0
  $0 $command urllib3_urllib3:2.0.6_2.3.0 --verbose

Run '$0 $command --help' for command-specific options."
fi

repo_spec="$1"

# Validate repo_id format if it looks like a full repo_spec
if [[ "$repo_spec" =~ ^[^:]+:[^:]+$ ]]; then
  validate_repo_id "$repo_spec"
fi

# Check if GitHub crawler tokens are set (only for collect and all commands)
if [[ "$command" == "collect" || "$command" == "all" ]]; then
  if [[ -z "${GITHUB_CRAWLER_TOKENS:-}" && -z "${GITHUB_TOKEN:-}" ]]; then
    error_exit "GITHUB_CRAWLER_TOKENS (or GITHUB_TOKEN) environment variable not set.
Please set: export GITHUB_CRAWLER_TOKENS='ghp_xxx,ghp_yyy'"
  fi
fi

# Check Python environment
python_cmd=$(check_python_env)

# Run the preproc command
"$python_cmd" -m deepcommit.cli preproc "$command" "$@"
