#!/usr/bin/env bash
# Shared verifier for every step. Harbor uploads tests/ plus steps/<step>/tests/ to /tests and runs this in a
# fresh container built from environment/ (the base state), separate from the agent's container.
#
# 1. Apply the agent's patch (/logs/artifacts/model.patch, written by csf-collect) to the clean base tree.
#    Changes under spec/ are dropped: the hidden tests own that directory.
# 2. Copy in the hidden specs and run the agent's migrations against the test database.
# 3. Run this step's fail-to-pass specs and the pass-to-pass specs (earlier steps plus a core regression set).
# 4. grade.rb writes /logs/verifier/reward.json: reward 1 only if every F2P and every P2P example passes.
set -uo pipefail

LOG=/logs/verifier
mkdir -p "$LOG"
# shellcheck source=/dev/null
. /tests/step.env    # STEP, F2P, P2P_EARLIER
P2P="${P2P_EARLIER} $(grep -v '^[[:space:]]*#' /tests/p2p_core.txt | xargs)"

note() { echo "csf-verify: $*" | tee -a "$LOG/verify.log"; }
give_up() { note "$1"; ruby /tests/grade.rb --error "$1"; exit 0; }

note "step ${STEP}"
csf-services start >> "$LOG/verify.log" 2>&1 || give_up "postgres or redis did not start"
cd /app || give_up "/app is missing"

patch=/logs/artifacts/model.patch
if [ -s "$patch" ]; then
  note "applying the agent's patch ($(grep -c '^diff --git' "$patch") files; spec/ excluded)"
  git apply --binary --whitespace=nowarn --exclude='spec/*' "$patch" >> "$LOG/verify.log" 2>&1 \
    || give_up "the agent's changes do not apply to the base tree"
else
  note "no changes from the agent"
fi

cp -a /tests/hidden/. /app/
RAILS_ENV=test bundle exec rails db:migrate >> "$LOG/verify.log" 2>&1 || give_up "db:migrate failed in the test environment"

run_specs() {  # run_specs <name> <spec files...>
  local name=$1; shift
  if [ $# -eq 0 ]; then return 0; fi
  note "running $name: $*"
  bundle exec rspec "$@" --format progress --format json --out "$LOG/$name.json" > "$LOG/$name.log" 2>&1
  tail -n 3 "$LOG/$name.log" | tee -a "$LOG/verify.log"
}

# shellcheck disable=SC2086
run_specs f2p $F2P
# shellcheck disable=SC2086
run_specs p2p $P2P
ruby /tests/grade.rb
