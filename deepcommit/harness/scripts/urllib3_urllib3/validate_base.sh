#!/bin/bash
# ==============================================================================
# Validate Base Image for urllib3_urllib3
# ==============================================================================
#
# This script validates the base Docker image for urllib3 testing.
# By default, it will auto-build the image if it doesn't exist.
#
# Usage:
#   ./validate_base.sh                  # Auto-build if needed, then validate
#   ./validate_base.sh --force-rebuild  # Force rebuild even if exists
#   ./validate_base.sh --no-build       # Only validate (fail if not exists)
#   ./validate_base.sh --run-tests      # Also run tests and save results
#   ./validate_base.sh --help           # Show help
#
# ==============================================================================

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
WORK_DIR="$REPO_ROOT/DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2"

# Required packages for urllib3 2.0.6 base environment
REQUIRED_PACKAGES=(
    pytest
    coverage
    trustme
    tornado
    cryptography
)

# Parse arguments
FORCE_REBUILD=false
NO_BUILD=false
RUN_TESTS=false
PYTEST_ARGS=""
HELP=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --force-rebuild)
            FORCE_REBUILD=true
            shift
            ;;
        --no-build)
            NO_BUILD=true
            shift
            ;;
        --run-tests)
            RUN_TESTS=true
            shift
            ;;
        --pytest-args)
            PYTEST_ARGS="$2"
            shift 2
            ;;
        --help|-h)
            HELP=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

if [ "$HELP" = true ]; then
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Validate the base Docker image for urllib3 testing."
    echo "By default, auto-builds the image if it doesn't exist."
    echo ""
    echo "Options:"
    echo "  --force-rebuild       Force rebuild even if image exists"
    echo "  --no-build            Only validate, fail if image doesn't exist"
    echo "  --run-tests           Run tests and save results to test_results/"
    echo "  --pytest-args ARGS    Additional pytest arguments (e.g., '--ignore=test/slow')"
    echo "  --help, -h            Show this help message"
    echo ""
    echo "Configuration:"
    echo "  Work directory: $WORK_DIR"
    echo "  Image name: urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2/base:latest"
    echo "  Test results: $WORK_DIR/test_results/base/"
    exit 0
fi

# Check work directory exists
if [ ! -d "$WORK_DIR" ]; then
    echo "Error: Work directory not found: $WORK_DIR"
    exit 1
fi

echo "=============================================="
echo "Validating urllib3 Base Image"
echo "=============================================="
echo ""
echo "Work directory: $WORK_DIR"
echo ""

# Build command
CMD="python $REPO_ROOT/harness/prepare_images/image_builder/validate_image.py"
CMD="$CMD --work-dir $WORK_DIR"
CMD="$CMD --required-packages ${REQUIRED_PACKAGES[*]}"
CMD="$CMD --test-dir test/"
CMD="$CMD --output $WORK_DIR/dockerfiles/base/validation.json"

if [ "$FORCE_REBUILD" = true ]; then
    CMD="$CMD --force-rebuild"
fi

if [ "$NO_BUILD" = true ]; then
    CMD="$CMD --no-build"
fi

if [ "$RUN_TESTS" = true ]; then
    CMD="$CMD --run-tests"
    CMD="$CMD --test-output-dir $WORK_DIR/test_results/base"
fi

if [ -n "$PYTEST_ARGS" ]; then
    CMD="$CMD --pytest-args \"$PYTEST_ARGS\""
fi

# Run validation
echo "Running: $CMD"
echo ""

$CMD

echo ""
echo "=============================================="
echo "Validation Complete"
echo "=============================================="
