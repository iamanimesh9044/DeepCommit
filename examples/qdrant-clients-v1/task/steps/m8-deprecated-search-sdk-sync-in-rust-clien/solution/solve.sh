#!/bin/bash
# Reference solution: apply this step's gold patch to every repo it touches.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
git -C /app/qdrant-client apply --whitespace=nowarn "$here/gold/qdrant-client.patch"
git -C /app/rust-client apply --whitespace=nowarn "$here/gold/rust-client.patch"
