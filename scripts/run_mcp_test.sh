#!/usr/bin/env bash
#
# Quick MCP test script
# Tests MCP tool accessibility using Stage 0
#
# Usage:
#   ./scripts/run_mcp_test.sh
#   ./scripts/run_mcp_test.sh --trial-name my_test
#

set -euo pipefail

# Load common utilities
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

# Default values
TRIAL_NAME="${TRIAL_NAME:-mcp_test_$(date +%Y%m%d_%H%M%S)}"
REPO_ID="urllib3_urllib3:2.0.6-2.3.0"
CONFIG_FILE="mcp_test_pipeline.yaml"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --trial-name)
      TRIAL_NAME="$2"
      shift 2
      ;;
    --help|-h)
      echo "Usage: $0 [--trial-name NAME]"
      echo ""
      echo "Options:"
      echo "  --trial-name NAME    Trial name (default: mcp_test_<timestamp>)"
      echo "  --help, -h          Show this help message"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      echo "Use --help for usage information"
      exit 1
      ;;
  esac
done

# Check Python environment
python_cmd=$(check_python_env)

echo "============================================"
echo "MCP Tool Test"
echo "============================================"
echo "Trial name:    $TRIAL_NAME"
echo "Config:        $CONFIG_FILE"
echo "Repository:    $REPO_ID"
echo "============================================"
echo ""

# Run the test
"$python_cmd" -m deepcommit.cli pipeline-multi run \
  --repo-id "$REPO_ID" \
  --trial-name "$TRIAL_NAME" \
  --config-file "$CONFIG_FILE" \
  --agents claudecode

echo ""
echo "============================================"
echo "Test completed!"
echo "Check results in: DATA/results/$TRIAL_NAME/stage0_mcp_test/"
echo "============================================"
