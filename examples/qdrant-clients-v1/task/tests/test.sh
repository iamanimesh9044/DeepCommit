#!/usr/bin/env bash
# Shared verifier for every step. Harbor uploads tests/ plus steps/<step>/tests/ to /tests and runs this in a
# fresh container built from environment/ (the base state of every repo), separate from the agent's container.
#
# 1. For every repo, apply the agent's patch (/logs/artifacts/model.<repo>.patch, written by csf-collect) to the clean
#    base tree at /app/<repo>. Changes under the repo's test dirs are dropped: the hidden tests own them.
# 2. Copy /tests/hidden/<repo>/ over the tree.
# 3. Run each repo's suites (tests/suites.json): this step's fail-to-pass files, then the pass-to-pass files
#    (P2P_EARLIER_<REPO> from step.env plus tests/p2p_core.txt).
# 4. grade.py merges every repo's results into one set (ids are <repo>::<id>) and writes /logs/verifier/reward.json:
#    reward 1 only if every F2P and every P2P test across ALL repos passed.
set -uo pipefail

LOG=/logs/verifier
mkdir -p "$LOG"
# shellcheck source=/dev/null
. /tests/step.env    # STEP, F2P_<REPO>, P2P_EARLIER_<REPO>
shopt -u patsub_replacement 2> /dev/null || true

note() { echo "csf-verify: $*" | tee -a "$LOG/verify.log"; }
give_up() { note "$1"; python3 /tests/grade.py --error "$1"; exit 0; }
var_of() { printf '%s' "$1" | tr 'a-z.-' 'A-Z__'; }

run_group() {  # run_group <repo> <f2p|p2p> <workdir> <run_cmd> <tests>
  local repo=$1 group=$2 workdir=$3 cmd=$4 tests=$5
  if [ -z "$tests" ]; then return 0; fi
  local full=${cmd//\{tests\}/$tests}
  note "running $repo $group: $tests"
  ( cd "/app/$repo/$workdir" && bash -c "$full" ) < /dev/null > "$LOG/$repo.$group.log" 2>&1
  echo $? > "$LOG/$repo.$group.rc"
  tail -n 3 "$LOG/$repo.$group.log" | tee -a "$LOG/verify.log"
}

note "step ${STEP}"
repos=$(python3 /tests/grade.py repos) || give_up "tests/suites.json is unreadable"

for repo in $repos; do
  cd "/app/$repo" 2> /dev/null || give_up "/app/$repo is missing"
  patch=/logs/artifacts/model.$repo.patch
  if [ -s "$patch" ]; then
    args=()
    while IFS= read -r pattern; do args+=("--exclude=$pattern"); done < <(python3 /tests/grade.py excludes "$repo")
    note "applying the agent's patch to $repo ($(grep -c '^diff --git' "$patch") files; test dirs excluded)"
    git apply --binary --whitespace=nowarn ${args[@]+"${args[@]}"} "$patch" >> "$LOG/verify.log" 2>&1 \
      || give_up "the agent's changes to $repo do not apply to the base tree"
  else
    note "no changes from the agent in $repo"
  fi
  if [ -d "/tests/hidden/$repo" ]; then cp -a "/tests/hidden/$repo/." "/app/$repo/"; fi
done

# Services the suites talk to (suites.json "services"): start each, wait for its health command.
while IFS=$'\t' read -r sname sstart shealth; do
  note "starting service $sname"
  ( bash -c "$sstart" ) < /dev/null > "$LOG/service.$sname.log" 2>&1 &
  for _ in $(seq 1 60); do bash -c "$shealth" > /dev/null 2>&1 && break; sleep 1; done
  bash -c "$shealth" > /dev/null 2>&1 || give_up "service $sname did not become healthy"
done < <(python3 /tests/grade.py services)

while IFS=$'\t' read -r repo workdir whole cmd; do
  up=$(var_of "$repo")
  f2p_var="F2P_$up"
  p2p_var="P2P_EARLIER_$up"
  core=$(awk -v r="$repo" '$1 == r { print $2 }' /tests/p2p_core.txt | xargs)
  f2p="${!f2p_var:-}"
  p2p="$(printf '%s %s' "${!p2p_var:-}" "$core" | xargs)"
  if [ "$whole" = 1 ]; then
    # the suite's command takes no file list (cargo test): run it once, ids.json picks what counts
    run_group "$repo" f2p "$workdir" "$cmd" "all"
  else
    run_group "$repo" f2p "$workdir" "$cmd" "$f2p"
    run_group "$repo" p2p "$workdir" "$cmd" "$p2p"
  fi
done < <(python3 /tests/grade.py suites)

python3 /tests/grade.py
