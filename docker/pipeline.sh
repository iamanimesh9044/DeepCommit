#!/usr/bin/env bash
# One command per stage, or `all`, from a workspace manifest to a CSF task folder.
#
#   docker compose run --rm pipeline <stage> [extra args]
#   stages: checkout validate preproc phase2 export finalize gates all
#
# Env: MANIFEST (default config/workspace.yaml), TRIAL (default v1), TASK_IMAGE (default csf-task).
# `all` stops before finalize: the task's environment/Dockerfile (toolchains, dependencies, vendored trees) is completed
# once per repo set, then `finalize` and `gates` run against the built image.
set -euo pipefail
cd /work
MANIFEST=${MANIFEST:?set MANIFEST to a config/<workspace>.workspace.yaml}
TRIAL=${TRIAL:-v1}
TASK_IMAGE=${TASK_IMAGE:-csf-task}
dc() { python -m deepcommit.cli.main "$@"; }
ws_id=$(python -c "import yaml,sys; print(yaml.safe_load(open('$MANIFEST'))['workspace_id'])")
stage4=DATA/trial_space/${ws_id}_${TRIAL}/stage4_decomp/output
task_dir() { ls -d DATA/csf_tasks/cross-repo-change/csf-"${ws_id//_/-}"-* 2>/dev/null | tail -1; }

stage=${1:-all}; shift || true
case "$stage" in
  checkout) dc workspace checkout "$MANIFEST" ;;
  validate) dc workspace validate "$MANIFEST" ;;
  preproc)  dc workspace preproc "$MANIFEST" ;;
  phase2)   dc workspace run "$MANIFEST" --trial-name "$TRIAL" "$@" && dc workspace bundle "$MANIFEST" --trial-name "$TRIAL" ;;
  export)   dc workspace export-task "$MANIFEST" --trial-name "$TRIAL" \
              --milestones-csv "$stage4/milestones.csv" --dependencies-csv "$stage4/dependencies.csv" "$@" ;;
  finalize) dc workspace finalize-task "$(task_dir)" --image "$TASK_IMAGE" "$@" && dc workspace check-task "$(task_dir)" ;;
  gates)    # needs Harbor >= 0.25 (multi-step tasks with a separate verifier)
            uv pip install -q --python /opt/venv/bin/python "harbor==0.25.0"
            for agent in oracle nop; do harbor run -p "$(task_dir)" -a "$agent" --jobs-dir "DATA/gates/${ws_id}" --job-name "$agent" -y; done ;;
  all)      "$0" checkout && "$0" validate && "$0" preproc && "$0" phase2 && "$0" export
            echo "next: finish $(task_dir)/environment/Dockerfile, build it as $TASK_IMAGE, then: pipeline finalize && pipeline gates" ;;
  *) echo "unknown stage $stage"; exit 2 ;;
esac
