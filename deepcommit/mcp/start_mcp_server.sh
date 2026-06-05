#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# workspace_root is 2 levels up from deepcommit/mcp/
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# For absolute imports like "from deepcommit.collect.tools...",
# we need workspace_root in PYTHONPATH
if [[ -n "${PYTHONPATH:-}" ]]; then
  PYTHONPATH="${WORKSPACE_ROOT}:${PYTHONPATH}"
else
  PYTHONPATH="${WORKSPACE_ROOT}"
fi

# Allow callers to select the Python interpreter (and virtualenv) via PYTHON_BIN.
PYTHON_BIN="${PYTHON_BIN:-python}"
if command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v "${PYTHON_BIN}")"
elif [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Error: PYTHON_BIN '${PYTHON_BIN}' is not executable" >&2
  exit 1
fi

cd "${SCRIPT_DIR}"
export PYTHONPATH
exec "${PYTHON_BIN}" mcp_server.py
