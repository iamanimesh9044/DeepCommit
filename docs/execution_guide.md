# DeepCommit Execution Guide

## Overview

DeepCommit transforms a repository's commit history into a structured, executable benchmark through four phases. Each phase reads the previous phase's output and produces artifacts for the next.

```
Phase 1 (Preprocessing)  →  Phase 2 (DAG Construction)  →  Phase 3 (env_resolution)  →  Phase 4 (srs_verify)
```

Phase 3 (`env_resolution`) wraps every runtime artifact: the pre-loop **Base Env Build**, the agent loop that builds testbeds + per-milestone Docker images (Modules 1-2-3), and the post-loop **Test Curation** that extracts the F2P test set. Phase 4 (`srs_verify`) consumes that bundle and produces the SRS prompt that agents will see at evaluation time, with dry-run verification.

## Phase 1: Preprocessing

Collects raw data from GitHub, builds commit dependency DAG, runs co-change analysis.

### Step 0: Clone the repository

```bash
git clone <repo_url> DATA/github_data/repos/{owner}_{repo}
```

### Step 1: Prepare range config

Create the source-boundary config `config/<owner>_<repo>_<start>_<end>.yaml` with the
**prepare_range_config** tool — an agent that drafts the boundary and validates it until green:

```bash
./scripts/run_prepare_config.sh <owner_repo:start_end>
```

**Output:** `config/<repo_range>.yaml` (`repo_src_dirs` / `test_dirs` / `exclude` / `main_branch`).

See [internals § Phase 1](internals.md#phase-1-config-preparation) for how the gate works and the per-field
matching rules.

### Step 2: Run preprocess

```bash
./scripts/run_preproc.sh all <repo_spec>
```

**Requires:**

- Local repo at `DATA/github_data/repos/<owner_repo>` (see Phase 0)
- `GITHUB_CRAWLER_TOKENS` env var (comma-separated GitHub PATs)

**Output:**

```
DATA/github_data/preprocessed/scikit-learn_scikit-learn_1.5.2_1.6.0/
├── main_commits.jsonl
├── commit_dag.json
├── issues.jsonl, releases.jsonl, pull_requests.jsonl
├── patches/
├── codemaat/, codemaat_analysis.json
├── dag/
└── symbol/
```

## Phase 2: Milestone DAG Construction

Multi-stage agent pipeline that discovers milestones and infers dependencies.

```bash
./scripts/run_multi_stage.sh \
    --repo-id scikit-learn_scikit-learn:1.5.2_1.6.0 \
    --trial-name v1
```

**Requires:**

- Phase 1 output at `DATA/github_data/preprocessed/<repo_range>/`
- Claude Code CLI with auth (OAuth via `claude login`, or `ANTHROPIC_API_KEY` env var)

**Output:**

```
DATA/trial_space/scikit-learn_scikit-learn_1.5.2_1.6.0_v1/
├── result/
│   ├── milestones.csv      ← primary output
│   ├── dependencies.csv    ← primary output
│   └── logs/, analysis/, meta.json
├── stage1_seed/ .. stage4_decomp/   (per-stage workspaces)
└── *_prompt_rendered.md             (agent prompts)
```

## Phase 3: env_resolution

Phase 3 turns the Phase 2 milestone DAG into the runtime artifacts EvoClaw evaluates on: a `testbed/` git repo carrying per-milestone `start`/`end` tags, one Docker image per milestone, and the fail-to-pass (F2P) test set. It runs as a pre-loop **Base Env Build**, a **Module 1 ↔ 2 loop** driven by the **Module 3 meta-orchestrator**, and a post-loop **Test Curation**.

### Entry point: launch the Module 3 meta-orchestrator

Phase 3 is launched as a single agent. Module 3's prompt (`main.md`) is the orchestrator — point a Claude Code agent at it and it drives the whole Module 1 + 2 + 3 loop on its own, one round per `_orch_runs/round_NNN/`:

```bash
claude -p "$(cat deepcommit/harness/main_orch/prompts/main.md)"
```

Each round it reads the Module 1/2 artifacts, decides whether to refine the DAG (Module 1), rebuild a milestone's environment (Module 2), or freeze a finished prefix, and appends the decision to a `plan.yaml` ledger.

**One-shot results are rarely good enough**, so the loop is built for human intervention: you can read a round's ledger, run any module's command yourself to inspect or repair a milestone, then hand control back to the orchestrator. What each module does and the exact commands it runs are in **[internals.md](internals.md)** § Phase 3 — that is where you step in.

### Pre-loop: Base Env Build (one-time per repo range)

```bash
python3 -m deepcommit.harness.prepare_images.image_builder.build_image \
    --config config/scikit-learn_scikit-learn_1.5.2_1.6.0.yaml \
    --target base \
    --repo-path DATA/github_data/repos/scikit-learn_scikit-learn
```

**Requires:** Local repo, config YAML

**Output:** `DATA/harness_workspace/<repo_range>/base/dockerfiles/Dockerfile` + Docker image

### What each module does (Modules 1 / 2 / 3)

You normally don't run Modules 1 and 2 by hand — the Module 3 orchestrator launched above drives them.
For when you step in (inspect a round, run a module's command to repair a milestone, then hand control
back), each module's job, exact commands, and I/O are documented in **[internals.md](internals.md)** § Phase 3.

### Post-loop: Test Curation

Once the Main Agent loop converges (every milestone has a clean testbed tag + a passing Docker image), Test Curation runs the actual tests on the frozen artifacts to produce the F2P test set used downstream. This is a one-way, non-iterative step — it consumes stable artifacts and does not feed back into Modules 1-3.

Run tests on start/end Docker images (3 attempts for flaky detection), then extract fail-to-pass (F2P) test source:

```bash
python3 -m deepcommit.harness.test_runner.run_milestone_tests \
    --work-dir DATA/harness_workspace/<repo_range>/<trial> \
    --replicates 3

python3 -m deepcommit.harness.filter_tests.extract_f2p_tests \
    --work-dir DATA/harness_workspace/<repo_range>/<trial>
```

**Requires:** converged Modules 1-3 output for every milestone — `testbed/` with start/end tags, `dockerfiles/{MID}/Dockerfile`, per-milestone Docker images, `milestone_patched_tests/{MID}.json`.

**Output:**

```
test_results/{MID}/
└── benchmark/
    ├── replicate_1/
    │   ├── start.json, end.json
    │   └── classification.json
    ├── replicate_2/
    ├── replicate_3/
    ├── {MID}_classification.json   # final; includes stable_classification (flaky excluded)
    ├── test_summary.json           # final post-loop summary
    └── f2p_tests/                  # extracted F2P test source + metadata
```

## Phase 4: srs_gen

With Phase 3's `env_resolution` bundle in hand, generate the **SRS** (Software Requirements Specification —
the only input the agent receives at evaluation time) by reverse-engineering it from the gold patch:

```bash
python3 -m deepcommit.harness.gen_srs.generate_srs \
    --work-dir DATA/harness_workspace/<repo_range>/<trial> \
    --trial-name v1
```

**SRS verification is not part of the code path.** The benchmark relies on **human verification with agent
support**: a person reviews each milestone's SRS — accurate scope, no implementation leak, matches the F2P
tests — and marks it verified before it is used. The generation pipeline, the verification checklist, and
known gaps are in **[internals.md](internals.md)** § Phase 4.

## Multi-repo workspaces

A **workspace** groups 2-5 repositories developed together (for example an API/contract repo plus a server and an SDK) so that milestones can span repositories. Single-repo runs are unchanged: with one repository nothing below applies and prompts render exactly as before. Example manifests: [`config/examples/qdrant_clients.workspace.yaml`](../config/examples/qdrant_clients.workspace.yaml) and [`config/examples/temporal_stack.workspace.yaml`](../config/examples/temporal_stack.workspace.yaml). Design details are in [internals.md](internals.md) § Multi-repo workspaces.

### Manifest

`config/<workspace_id>.workspace.yaml` lists the member repos. Each member keeps the single-repo fields (`owner`, `repo`, `start`/`end` tag or `since`/`until`, `main_branch`, `repo_src_dirs` as literal path prefixes, `test_dirs`, `exclude`) plus `name` (the directory name under `repo_data/`), `role` (`contract | server | sdk | library`) and, for contract repos, `contract_paths`. An `xrepo:` block configures cross-repo edge detection (`pin_files`, `link_confidence_threshold`, `time_window_days`, `generated_globs`). The schema is documented in `deepcommit/multirepo/workspace.py`.

### CLI

```bash
deepcommit workspace <validate|preproc|merge|run> <manifest>
```

| Command | What it does |
|---|---|
| `validate` | Parse the manifest, check names/roles/ranges, check that bare 7-char SHAs are unique across the repos (a collision is a hard error) |
| `preproc` | Run Phase 1 per repository into `preprocessed/<ws>/<repo>/` |
| `merge` | Build `preprocessed/<ws>/merged/` (merged commits, DAG with cross-repo edges, `commit_repos.json`, repos table) |
| `run` | Run Phase 2 on the merged data |

The `workspace` CLI is being implemented in the same effort; its exact flags may differ until it lands.

### Data layout

```text
preprocessed/<ws>/<repo>/        # per-repo Phase 1 output, unchanged format
preprocessed/<ws>/merged/        # input of Phase 2
    main_commits.jsonl           # commits carry repo, committed_at, merged_via_pr_key
    commit_dag.json              # edges_dag (textual + hard xrepo) and edges_soft (hints)
    commit_repos.json            # bare sha -> repo
    pull_requests.jsonl, issues.jsonl   # carry repo and pr_key (<repo>#<n>)
trial_space/<id>/repo_data/<repo>/   # one checkout per repo
```

Conventions: commit keys stay **bare 7-char SHAs** (unique inside a workspace; the repo comes from `commit_repos.json`); every path in merged data is prefixed `<repo>/<path>`.

### What changes in Phase 2 (milestone DAG construction)

Prompts switch on `is_multi_repo` and receive `workspace_id` and `workspace_repos_table`. The agent inspects code with `git -C repo_data/<repo> ...` (MCP tools route automatically). Rules redefined for workspaces: coverage and overlap over the union of commits (reported per repo and overall); path completeness enforced on textual edges and **hard** cross-repo edges only (soft edges are hints and may be cut); seeds linked by a hard edge share a group; about 20 seeds per repo; `src_loc` summed per repo with CV on the sum; contract change never split from its first consumer; one `integration_test_commit` per repo; new `repos` column in `milestones.csv`; new dependency type `XREPO`; a cross-repo milestone must change at least 2 repos and no single repo's part may be a complete feature.

### Phase 3 and Phase 4 (in progress)

Multi-repo testbeds (per-repo branches/tags, one image with all toolchains at `/workspace/<repo>/`, a list of test suites with `{repo}::` test ids, one gold patch per repo) and multi-repo SRS generation (per-repo "Interface contract" section, boundary vs internal names) are **in progress** and not yet usable.

### Known limits

- Phase 3-4 support is not finished (above).
- Cross-repo edge heuristics are unvalidated on real data; expect to tune `link_confidence_threshold`.
- The compile-check of split commits is skipped for cross-repo milestones.
- Max 5 repos per workspace; repos must have non-colliding 7-char SHAs.
