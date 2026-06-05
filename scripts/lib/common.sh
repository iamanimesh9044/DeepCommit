#!/usr/bin/env bash
# Common utility functions for DeepCommit scripts

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print error message and exit
error_exit() {
    echo -e "${RED}Error:${NC} $1" >&2
    exit "${2:-1}"
}

# Print warning message
warn() {
    echo -e "${YELLOW}Warning:${NC} $1" >&2
}

# Print info message
info() {
    echo -e "${BLUE}Info:${NC} $1" >&2
}

# Print success message
success() {
    echo -e "${GREEN}Success:${NC} $1" >&2
}

# Check if required number of arguments are provided
require_args() {
    local actual=$1
    local required=$2
    local script_name=$3

    if [[ $actual -lt $required ]]; then
        error_exit "Insufficient arguments. Run '$script_name --help' for usage." 1
    fi
}

# Validate repo_id format (owner_repo:start_end)
validate_repo_id() {
    local repo_id=$1

    if [[ ! "$repo_id" =~ ^[^:]+:[^:]+$ ]]; then
        error_exit "Invalid repo_id format: '$repo_id'
Expected format: 'owner_repo:start_end'
Example: 'urllib3_urllib3:2.0.6_2.3.0'" 1
    fi
}

# Check if DATA root directory exists
check_data_root() {
    local data_root="${1:-}"

    if [[ -z "$data_root" ]]; then
        if [[ -n "${DEEPCOMMIT_DATA_ROOT:-${AGENTBENCH_DATA_ROOT:-}}" ]]; then
            data_root="${DEEPCOMMIT_DATA_ROOT:-$AGENTBENCH_DATA_ROOT}"
        else
            local script_dir
            script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
            local project_root
            project_root="$(cd "$script_dir/../.." && pwd)"
            data_root="$project_root/DATA"
        fi
    fi

    if [[ ! -d "$data_root" ]]; then
        error_exit "DATA root directory not found: $data_root
Please ensure the DATA directory exists or set DEEPCOMMIT_DATA_ROOT environment variable." 1
    fi

    echo "$data_root"
}

# Check Python environment
check_python_env() {
    local python_cmd="${PYTHON:-python3}"

    if ! command -v "$python_cmd" >/dev/null 2>&1; then
        error_exit "Python interpreter not found: $python_cmd
Please install Python or set PYTHON environment variable." 1
    fi

    # Check if deepcommit module is available
    if ! "$python_cmd" -c "import deepcommit" 2>/dev/null; then
        warn "deepcommit module not found in current Python environment.
Make sure you're running from the project root or have the correct PYTHONPATH set."
    fi

    echo "$python_cmd"
}

# Check if a file exists
require_file() {
    local file=$1
    local description="${2:-File}"

    if [[ ! -f "$file" ]]; then
        error_exit "$description not found: $file" 1
    fi
}

# Check if a directory exists
require_dir() {
    local dir=$1
    local description="${2:-Directory}"

    if [[ ! -d "$dir" ]]; then
        error_exit "$description not found: $dir" 1
    fi
}

# Show usage with standard format
show_usage() {
    local script_name=$1
    local usage_text=$2

    cat >&2 <<EOF
Usage: $script_name $usage_text

Run '$script_name --help' for detailed help.
EOF
}

# Parse common flags (--help, -h)
handle_common_flags() {
    local arg="${1:-}"

    if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
        return 0
    fi
    return 1
}

# Get project root directory
get_project_root() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local project_root
    project_root="$(cd "$script_dir/../.." && pwd)"
    echo "$project_root"
}

# Validate that a parameter is provided
require_param() {
    local value="${1:-}"
    local param_name="${2:-parameter}"

    if [[ -z "$value" ]]; then
        error_exit "Missing required parameter: $param_name" 1
    fi
}

# Check if running from project root or subdirectory
check_in_project() {
    local project_root
    project_root="$(get_project_root)"

    if [[ ! -f "$project_root/pyproject.toml" ]]; then
        warn "Not running from project root. Some features may not work correctly."
    fi
}
