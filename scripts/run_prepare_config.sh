#!/usr/bin/env bash
#
# Phase-1: prepare + validate a range config via the prepare_range_config skill.
#
# Pipeline: prefetch context brief -> Claude agent drafts the boundary config and
# loops on the deterministic validation gate until green -> (optional) preprocess.
#
# Usage:
#   scripts/run_prepare_config.sh <owner_repo:start_end> [options]
#
# Options:
#   --repo PATH       repo path (default: DATA/github_data/repos/<owner_repo>)
#   --no-agent        run only the deterministic steps (prefetch, then validate an
#                     existing config if present) — no Claude invocation
#   --then-preproc    on a green gate, chain ./scripts/run_preproc.sh all <spec>
#
# Examples:
#   scripts/run_prepare_config.sh scikit-learn_scikit-learn:1.5.2_1.6.0
#   scripts/run_prepare_config.sh urllib3_urllib3:2.0.6_2.3.0 --then-preproc
#   scripts/run_prepare_config.sh django_django:4.2_5.2 --no-agent   # gate an existing config

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [[ $# -eq 0 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

spec="$1"; shift
repo_path=""; run_agent=1; then_preproc=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo_path="$2"; shift 2 ;;
    --no-agent) run_agent=0; shift ;;
    --then-preproc) then_preproc=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ "$spec" != *:* ]]; then
  echo "error: expected <owner_repo:start_end>, got '$spec'" >&2; exit 2
fi
owner_repo="${spec%%:*}"
range="${spec#*:}"
start="${range%_*}"
end="${range##*_}"
[[ -z "$repo_path" ]] && repo_path="DATA/github_data/repos/${owner_repo}"
config_path="config/${owner_repo}_${start}_${end}.yaml"

if [[ ! -d "$repo_path" ]]; then
  echo "error: repo not found: $repo_path" >&2; exit 2
fi

PY="${PYTHON:-python3}"
work_dir="$(mktemp -d)"; brief="${work_dir}/context_brief.md"
trap 'rm -rf "$work_dir"' EXIT

echo "[prepare-config] repo=$repo_path  range=$start..$end  -> $config_path"

# 1. prefetch context brief
"$PY" -m deepcommit.preprocess.prepare_range_config.prefetch \
  --repo "$repo_path" --start-tag "$start" --end-tag "$end" --out "$brief"
echo "[prepare-config] brief: $brief"

validate() {
  "$PY" -m deepcommit.preprocess.prepare_range_config.validate \
    --repo "$repo_path" --config "$config_path" --end-ref "$end"
}

if [[ "$run_agent" -eq 0 ]]; then
  echo "[prepare-config] --no-agent: brief + (gate if config exists)"
  cat "$brief"
  [[ -f "$config_path" ]] && { echo; validate; } || echo "[prepare-config] (no config yet at $config_path)"
  exit 0
fi

# 2. agent drafts the config and loops on the gate until green
skill="deepcommit/preprocess/prepare_range_config/skill.md"
prompt="$(cat "$skill")

---
## Your task
Repo: \`$repo_path\`   Range: \`$start\` .. \`$end\`   Write the config to: \`$config_path\`
Validate with:
    $PY -m deepcommit.preprocess.prepare_range_config.validate --repo $repo_path --config $config_path --end-ref $end
Loop until the gate is green, then write the final config.

---
$(cat "$brief")"

if ! command -v claude >/dev/null 2>&1; then
  echo "error: 'claude' CLI not found; re-run with --no-agent or install Claude Code" >&2; exit 3
fi
echo "[prepare-config] launching agent…"
claude -p "$prompt"

# 3. final gate on the produced config
echo "[prepare-config] final validation:"
if validate; then
  echo "[prepare-config] ✓ config is green: $config_path"
  if [[ "$then_preproc" -eq 1 ]]; then
    echo "[prepare-config] chaining preprocess…"
    "$SCRIPT_DIR/run_preproc.sh" all "$spec"
  fi
else
  echo "[prepare-config] ✗ gate still failing — inspect $config_path" >&2; exit 1
fi
