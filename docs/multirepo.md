# Multi-repo milestones and CSF task generation

DeepCommit's single-repo pipeline turns one repository's commit history into milestones (Phase 2), testbeds (Phase 3) and
specifications (Phase 4). The multi-repo extension does the same for a **workspace**: 2 to 5 repositories developed together,
for example an API contract repository plus the SDKs that consume it. A milestone can then change several repositories at
once, and the end product is a Harbor multi-step task (CSF schema 1.4, family `cross-repo-change`) in which one agent keeps
one environment across all steps and every step is graded by hidden tests across all repositories.

A finished example, with every intermediate artifact, is in [`examples/qdrant-clients-v1/`](../examples/qdrant-clients-v1/).

## How it works

```text
workspace manifest (yaml)
        │  validate
        ▼
Phase 1  preproc ──► one preprocessed dataset per repo ──► merge ──► merged workspace dataset
                                                                      (commits, DAG with cross-repo edges, PRs)
        ▼
Phase 2  run ──► seeds, milestones, dependency DAG, decomposition        (LLM agent, git -C repo_data/<repo>)
         bundle ──► single-repo milestones joined into cross-repo milestones
        ▼
Export   export-task ──► Harbor task folder: one gold patch per repo and step, hidden tests, Dockerfile skeleton
        ▼
Finalize finalize-task ──► measure real test outcomes in the task image, curate F2P/P2P ids, fold ungradable
                           steps into their successor, write agent instructions, write README
        ▼
Gates    harbor run -a oracle (every step 1)   harbor run -a nop (every step 0)   check-task
```

### The workspace manifest

`config/<workspace_id>.workspace.yaml` lists the member repositories. Per repository: `name` (directory under `repo_data/`),
`owner`/`repo`, `start`/`end` (tags) or `since`/`until`, `main_branch`, `repo_src_dirs`, `test_dirs`, `exclude`, and a `role`
(`contract | server | sdk | library`). A **contract-only** repository (`contract_only: true`, with `contract_paths`) is
kept sparse: only the contract files (for example `.proto`) are in the task, the repo has no tests, and edits to it are collected
and applied like any other repository's. The `xrepo:` block configures cross-repo edge detection (`pin_files`,
`link_confidence_threshold`, `time_window_days`, `generated_globs`). Examples: [`config/examples/`](../config/examples/);
schema: `deepcommit/multirepo/workspace.py`.

### Cross-repo edges

Commit keys stay bare 7-char SHAs (a collision between repos is a hard error in `validate`); the repo of a commit comes from
`commit_repos.json`; every path in merged data is prefixed `<repo>/<path>`. Phase 1 merge adds two kinds of cross-repo edge:

| Edge | Evidence | Use |
| --- | --- | --- |
| hard (`XREPO`) | a client pins a contract release or commit; a PR names a commit of another repo | milestone must not split it; seeds linked by it share a milestone |
| soft | same identifier in a contract change and a later consumer, close in time | hint only, may be cut |

### Milestone rules that differ from single-repo

A cross-repo milestone changes at least two repositories and no single repository's part is a complete feature; a contract change
is never split from its first consumer; coverage and overlap are measured over the union of commits; `milestones.csv` has a
`repos` column. Commits that no milestone owns are **background**: they are replayed into the base state of the task.

### From milestones to a task

`export-task` writes, per milestone, one gold patch per repository (`steps/<step>/solution/gold/<repo>.patch`) and the hidden test
files. Because background commits are part of the base, a base can be inconsistent (in the Qdrant example the Python client uses
generated stubs that a later milestone regenerates, so the package does not import until then). `finalize-task` therefore measures
instead of guessing:

| Stage | Module | Effect |
| --- | --- | --- |
| measure | `export/templates/measure.py` | In the task image, apply the gold patches step by step. Run the step's own test files before its gold patch, the full suites after it, `--runs` times |
| curate | `export/curate.py` | F2P = the step's own tests that fail before and pass in every run after. P2P = earlier steps' tests passing in the previous and the current state. Flaky tests drop out |
| fold | `export/fold.py` | A step with no F2P test is folded into its successor: gold patches concatenated, steps renumbered, DAG rewritten |
| instructions | `export/gen_instructions.py` | Behaviour-level statement from the gold patch and hidden tests (`claude -p`), no commit hashes, every public name the tests use |
| README | `export/readme.py` | Status table, grading, known limits |

Grading is by id: `steps/<step>/tests/ids.json` lists `<repo>::<id>` for `f2p` and `p2p`, and `tests/grade.py` counts only those. A step's
reward is 1 only if every F2P and every P2P id passes; the episode score is the mean over steps. `tests/suites.json` declares, per
repo, the run command and parser (`pytest`, `cargo`), `whole_suite: true` for runners that cannot take a file list, and `services`
(for example a real Qdrant server that `test.sh` starts before the suites).

## Commands

Requirements: Python 3.11+, `uv`, Docker, the `claude` CLI (Phase 2 agent and instruction rewrite), network for the first image build.
Harbor 0.25 or newer for the gates (0.20 cannot run multi-step tasks with a separate verifier).

```bash
uv sync

# 0. describe the repos, then check the manifest
uv run deepcommit workspace validate config/<ws>.workspace.yaml

# 1. Phase 1 per repo, then merge into one dataset (clones repos, builds commit DAG and cross-repo edges)
uv run deepcommit workspace preproc config/<ws>.workspace.yaml
uv run deepcommit workspace merge   config/<ws>.workspace.yaml

# 2. Phase 2: milestones and dependency DAG (LLM agent)
uv run deepcommit workspace run     config/<ws>.workspace.yaml --trial-name <trial>
uv run deepcommit workspace bundle  config/<ws>.workspace.yaml --trial-name <trial>        # --dry-run to preview

# 3. export a CSF task folder (gold patch per repo, hidden tests, Dockerfile skeleton)
uv run deepcommit workspace export-task config/<ws>.workspace.yaml --trial-name <trial> \
    --milestones-csv <stage4>/milestones.csv --dependencies-csv <stage4>/dependencies.csv

# 4. by hand, once per repo set: finish environment/Dockerfile (toolchains, dependency prefetch, services, editable installs),
#    vendor the pinned repo trees and release binaries into environment/vendor/, fill tests/suites.json

# 5. measure, curate, fold, instructions, README (needs Docker and `claude`)
uv run deepcommit workspace finalize-task <task folder>          # --runs 2 --skip-build --no-instructions --model sonnet

# 6. validate and gate
uv run deepcommit workspace check-task <task folder>
harbor run -p <task folder> -a oracle        # every step must score 1
harbor run -p <task folder> -a nop           # every step must score 0
```

Browse the result with the HTML viewer: `python3 deepcommit/export/templates/viewer_gen.py <family folder>` writes
`index.html` with each step's instruction, gold code and hidden tests.

## Making the image independent of GitHub

Clone the pinned commits once and keep them with the task, so the task still builds if an upstream repository or release is deleted:

```bash
git -C repo_data/<repo> archive --format=tar.gz -o <task>/environment/vendor/<repo>.tar.gz <base-commit>
curl -fsSL -o <task>/environment/vendor/qdrant-<arch>-unknown-linux-musl.tar.gz <release asset url>
```

`environment/Dockerfile` bind-mounts `vendor/` per build step (`RUN --mount=type=bind,source=vendor,target=/tmp/vendor ...`), unpacks each tree,
and creates a one-commit git repository per repo. Package registries (PyPI, crates.io, apt) are still reached at build time.

## Worked example

`examples/qdrant-clients-v1/`: Qdrant (contract only: `.proto` files) + `rust-client` + `qdrant-client`, v1.18.0 to v1.20.0.
Phase 2 found 35 milestones: 19 became steps at export, 13 went into the base as background, 3 were not built (`poetry.lock` conflict). Measurement then folded the 19 into 11 steps; the oracle scores 1 on
all 11 and the no-op scores 0 on all 11. See that folder's README for the step table and the artifacts.

## Known limits

- **Weak cross-repo coupling when upstream clients lag the contract.** In Qdrant the server proto reaches the clients in release-sync
  commits, so within a milestone the contract part and the client part rarely depend on each other, and the hidden tests are each repo's
  own, so no test checks that two repos agree. Pick repo sets that change together (a contract plus SDKs released in lockstep).
- Cross-repo edge heuristics are unvalidated beyond the pilot; tune `link_confidence_threshold`.
- Replaying background commits uses `-X theirs`, and test-only conflicts are auto-resolved at export; both are unreviewed.
- Instructions are generated from the gold patch and hidden tests, not from a full Phase 4 specification, so they can leak more of the solution.
- The Dockerfile is completed by hand per repo set (`Dockerfile.tmpl` is not updated). The exporter does not yet write `environment/vendor/`.
- At most 5 repositories per workspace.
