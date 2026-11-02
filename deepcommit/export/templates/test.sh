#!/usr/bin/env bash
# Grades one step in its own grading stack, built from environment/. Harbor has replaced /app with the agent's /app (the
# task's only artifact) and uploaded this folder to /tests. GENERATED (DeepCommit export): the same in every step.
#
#   seed    reset each repo's test dirs (suite.json suites[].test_excludes) to the base tree in /opt/csf/base/<repo>.git,
#           then copy checks/<repo>/ over the tree. Whatever the agent did in the test dirs is dropped.
#   deploy  for every service in suite.json "services": an app service is deployed with `harbor-svc deploy <svc>` from
#           the agent's /app/<svc>/csf-service.toml; infrastructure is already up. Then its health command: one deploy
#           check each.
#   checks  run each repo's suite: this step's f2p files, then its p2p files (suite.json "files").
#   grade   grade.py: reward 1 only if every check in suite.json passed; writes reward.json and checks.json.
set -uo pipefail

LOG=/logs/verifier
T=/tests
mkdir -p "$LOG"
echo '{"reward": 0}' > "$LOG/reward.json"
shopt -u patsub_replacement 2> /dev/null || true

note() { echo "csf-verify: $*" | tee -a "$LOG/verify.log"; }
give_up() { note "$1"; python3 "$T/grade.py" --error "$1"; exit 0; }

run_group() {  # run_group <repo> <f2p|p2p> <workdir> <run_cmd> <tests>
  local repo=$1 group=$2 workdir=$3 cmd=$4 tests=$5
  if [ -z "$tests" ]; then return 0; fi
  local full=${cmd//\{tests\}/$tests}
  note "running $repo $group: $tests"
  ( cd "/app/$repo/$workdir" && bash -c "$full" ) < /dev/null > "$LOG/$repo.$group.log" 2>&1
  echo $? > "$LOG/$repo.$group.rc"
  tail -n 3 "$LOG/$repo.$group.log" | tee -a "$LOG/verify.log"
}

note "step $(python3 "$T/grade.py" step)"
repos=$(python3 "$T/grade.py" repos) || give_up "suite.json is unreadable"

# seed
for repo in $repos; do
  [ -d "/app/$repo" ] || give_up "/app/$repo is missing from the agent's tree"
  base=/opt/csf/base/$repo.git
  [ -d "$base" ] || give_up "$base is missing from the image"
  while IFS= read -r dir; do
    [ -n "$dir" ] || continue
    note "resetting $repo/$dir to the base tree"
    rm -rf "/app/$repo/$dir"
    if git --git-dir="$base" ls-tree -d --name-only HEAD -- "$dir" | grep -q .; then
      git --git-dir="$base" --work-tree="/app/$repo" checkout HEAD -- "$dir" >> "$LOG/verify.log" 2>&1 \
        || give_up "could not restore $repo/$dir from the base tree"
    fi
  done < <(python3 "$T/grade.py" excludes "$repo")
  if [ -d "$T/checks/$repo" ]; then cp -a "$T/checks/$repo/." "/app/$repo/"; fi
done

# deploy
while IFS=$'\t' read -r sname skind shealth; do
  rc=0
  if [ "$skind" = app ]; then
    note "harbor-svc deploy $sname"
    harbor-svc deploy "$sname" > "$LOG/deploy.$sname.log" 2>&1
    rc=$?
    tail -n 5 "$LOG/deploy.$sname.log" | tee -a "$LOG/verify.log"
    [ "$rc" -eq 3 ] && note "no service $sname in the agent's /app (manifest removed)"
    [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ] && note "harbor-svc deploy $sname failed (exit $rc)"
  else
    note "waiting for infrastructure service $sname"
  fi
  if [ "$rc" -eq 0 ]; then
    for _ in $(seq 1 60); do bash -c "$shealth" > /dev/null 2>&1 && break; sleep 1; done
    bash -c "$shealth" > /dev/null 2>&1 || { note "service $sname: $shealth fails"; rc=1; }
  fi
  [ "$skind" = app ] && { harbor-svc logs "$sname" --tail 500 > "$LOG/service.$sname.log" 2>&1 || true; }
  if [ "$rc" -eq 0 ]; then echo 0; else echo 1; fi > "$LOG/deploy.$sname.rc"
done < <(python3 "$T/grade.py" services)

# checks
while IFS=$'\x1f' read -r repo workdir whole cmd f2p p2p; do
  if [ "$whole" = 1 ]; then
    # the suite's command takes no file list (cargo test): run it once, suite.json picks what counts
    run_group "$repo" f2p "$workdir" "$cmd" "all"
  else
    run_group "$repo" f2p "$workdir" "$cmd" "$f2p"
    run_group "$repo" p2p "$workdir" "$cmd" "$p2p"
  fi
done < <(python3 "$T/grade.py" suites)

python3 "$T/grade.py"
