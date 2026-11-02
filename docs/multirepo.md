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

### Repositories that are not on GitHub (Bitbucket, GitLab, self-hosted)

Set `platform: git` on a repository. Phase 1 then needs no API and no token, only a clone at `DATA/github_data/repos/<owner>_<repo>`:

- Make the clone with `git clone` (a copy: DeepCommit rewrites the mainline branch in it). Without release tags, create local ones in the clone for the
  range (`git tag v2026.7.1 <commit>`, `git tag v2026.8.21 <commit>`) and put them in `start` / `end`.
- `workspace preproc` runs `deepcommit/preprocess/linearize.py` on the clone: each first-parent step (a merge commit for a pull request)
  becomes one commit with the same tree, author, dates and message. Without it, merge commits have no patch and the DAG is empty.
  The original tip stays at `refs/deepcommit/orig/<branch>`; tags are moved to the linear commits.
- Pull requests are rebuilt from the `Merged in <branch> (pull request #N)` subjects by `deepcommit/preprocess/git_only_api.py`. Issues,
  comments, labels and reviews do not exist locally and stay empty.
- Cross-repo edges: besides pins and PR links, pull requests that merge the **same feature branch name** in two repositories get a hard
  `xrepo_branch` edge (`xrepo.py`, 45-day window, generic names like `master` ignored).

Use `exclude` for tracked secrets (`**/*.env`) so they never reach patches, hidden tests or vendored trees.

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

`export-task` writes a Harbor task in the CSF service layout (`deepcommit/export/svc_layout.py`):

```
tasks/<family>/<task-id>/
├── task.toml                     settings, milestone cards, service control (manifest mode), [[steps]]
├── README.md, instruction.md     instruction.md: the episode context
├── ledger.json                   GENERATED by finalize-task: every check -> family, version, first/last step, bucket, kind
├── environment/
│   ├── Dockerfile                main: the agent's container, plus the test runner and its packages
│   ├── docker-compose.yaml       main + infrastructure, app-src volume, internal appnet, localhost forwards
│   ├── app/<repo>/               starting code: start commit + background commits, contract-only repos pruned
│   ├── app/<svc>/csf-service.toml   manifest of an app service: runtime, build, command, port, health, depends_on, env
│   └── runtimes/<runtime>/       task-owned images: Dockerfile, csf-run, csf-health
└── steps/mN-<slug>/
    ├── instruction.md
    ├── solution/{gold.patch, solve.sh}   one patch rooted at /app (paths <repo>/...); solve.sh applies it, then
    │                                     `harbor-svc deploy --all` when the task has app services
    └── tests/{test.sh, grade.py, suite.json, checks/<repo>/...}
```

Every Dockerfile is linked from the compose file (main's, a `build:` of an infrastructure service) or from the runtime catalog in
`task.toml`; `check-task` rejects an unlinked one. The agent's whole `/app` is the submission (`artifacts = [{ source = "/app" }]`):
after each step Harbor copies it into a fresh grading stack built from `environment/`, where `test.sh` resets each repo's test dirs to
`/opt/csf/base/<repo>.git`, copies `checks/<repo>/` in, deploys every app service with `harbor-svc deploy` (one `deploy` check each),
and runs the suites. Because background commits are part of the base, a base can be inconsistent (in the Qdrant example the Python
client uses generated stubs that a later milestone regenerates, so the package does not import until then). `finalize-task`
therefore measures instead of guessing:

| Stage | Module | Effect |
| --- | --- | --- |
| measure | `export/templates/measure.py`, `finalize.measure` | Start the task's stack (main plus its app services, deployed as Harbor deploys them), apply the gold patches step by step. Run the step's own test files before its gold patch, the full suites after it, `--runs` times |
| curate | `export/curate.py` | F2P = the step's own tests that fail before and pass in every run after. P2P = earlier steps' tests passing in the previous and the current state. Flaky tests drop out. Written as the `checks` of `suite.json` |
| fold | `export/fold.py` | A step with no F2P test is folded into its successor: gold patches concatenated, own files merged, steps renumbered, DAG rewritten |
| cards, ledger | `export/svc_layout.py` | `[[metadata.milestone_cards]]` per step and `ledger.json` (never read at grading) |
| instructions | `export/gen_instructions.py` | Behaviour-level statement from the gold patch and hidden tests (`claude -p`), no commit hashes, every public name the tests use |
| README | `export/readme.py` | Status, services, grading, generated Gates section, known limits |

Grading is by id: each step's `tests/suite.json` lists its `checks` (`<repo>::<id>` with a bucket `deploy`, `f2p` or `p2p`) and
`grade.py` counts only those; a step's reward is 1 only if every check passes, the episode score is the mean over steps. `suite.json`
also carries the suites (run command, parser, `whole_suite: true` for runners that cannot take a file list), the services and the files
`test.sh` runs. Before curation a step has no `checks` and is graded by its files.

### Services and the main image (workspace manifest)

Two optional top-level keys of the workspace manifest describe what the exporter cannot read from the repositories (full reference in
the `svc_layout.py` docstring; the Qdrant workspace in `config/qdrant_clients_v1.workspace.yaml` is a working example):

```yaml
csf_services:
  - name: qdrant-server                 # folder under /app and service name
    runtime: {name: qdrant-1.20, dir: csf/runtimes/qdrant-1.20}   # a folder with a Dockerfile (csf-run/csf-health are added)
    command: qdrant --disable-telemetry
    port: 6333
    health: /healthz
    env: {QDRANT__STORAGE__STORAGE_PATH: /tmp/qdrant/storage}
    localhost_ports: [6333, 6334]       # main forwards localhost:<port> to the service (clients that default to localhost)
  # kind: infra + compose: {image: postgres:16, healthcheck: ...} for a service the agent does not deploy
csf_environment:
  apt: [python3, python3-venv]
  toolchains: |                         # Dockerfile lines before the code is copied in
  after_app: |                          # Dockerfile lines after COPY app/ (dependency prefetch, editable installs)
  resources: {cpus: 4, memory_mb: 8192}
  service_control: {app_cpus: 1.0, app_memory_mb: 2048, guide: "..."}
```

App services run under Harbor's app-service template: read-only root, the code volume read-only, tmpfs at `/tmp` and `/build`, a
non-root user. Keep a service's state under `/tmp`.

## Commands

Requirements: Python 3.11+, `uv`, Docker, the `claude` CLI (Phase 2 agent and instruction rewrite), network for the first image build.
Harbor with service control (the deccanai-org fork, `docs/service-control.md`) for the gates.

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

# 3. export a CSF task folder (starting code, compose stack, services, gold patches, hidden tests)
uv run deepcommit workspace export-task config/<ws>.workspace.yaml --trial-name <trial> \
    --milestones-csv <stage4>/milestones.csv --dependencies-csv <stage4>/dependencies.csv

# 4. once per repo set: describe the services and the main image in the manifest (csf_services, csf_environment,
#    csf_suite per repo); without them the Dockerfile keeps TODO markers

# 5. measure, curate, fold, instructions, README (needs Docker and `claude`)
uv run deepcommit workspace finalize-task <task folder>          # --runs 2 --skip-build --no-instructions --model sonnet

# 6. validate and gate
uv run deepcommit workspace check-task <task folder>
harbor run -p <task folder> -a oracle        # every step must score 1
harbor run -p <task folder> -a nop           # step 1 must score 0
```

Browse the result with the HTML viewer: `python3 deepcommit/export/templates/viewer_gen.py <family folder>` writes
`index.html` with each step's instruction, gold code and hidden tests.

## Reproducible setup with Docker Compose

Two compose files remove the open-ended manual steps.

**Pipeline** (`compose.yaml`, `docker/`): one container with git, `uv`, the `claude` CLI and Docker. Each stage is one command:

```bash
MANIFEST=config/<ws>.workspace.yaml docker compose run --rm pipeline all        # checkout, validate, preproc, phase2 (+bundle), export
MANIFEST=... TASK_IMAGE=<image> docker compose run --rm pipeline finalize        # measure, curate, fold, instructions, README, check-task
MANIFEST=... docker compose run --rm pipeline gates                              # oracle and NOP with Harbor 0.25
```

`workspace checkout` is the first stage: it clones every `clone_url` and creates the `start` / `end` tags from `start_date` / `end_date`
(last first-parent commit before the start date and through the end date), so a repo without release tags is fully described by the manifest.
`~/.ssh` is mounted read-only for SSH clones; put `CLAUDE_CODE_OAUTH_TOKEN` in `.env` (or `--env-file`). The task's environment comes from
the manifest's `csf_environment` and `csf_services`, so nothing between `all` and `finalize` is manual once they are written.

**Task** (`<task>/compose.base.yaml`, written by `finalize-task`): builds the base state (pinned commits, background commits applied, dependencies)
and opens a shell in it, without Harbor: `docker compose -f compose.base.yaml run --rm base`.

## Making the image independent of GitHub

The exporter writes each repository's starting tree into `environment/app/<repo>/` (from the local checkouts), so the image never
clones from GitHub. Release binaries a runtime needs are either downloaded with a pinned checksum in its Dockerfile (the Qdrant runtime
does this) or put next to it in the runtime folder; files the main Dockerfile bind-mounts go in `csf_environment.vendor`. Package
registries (PyPI, crates.io, apt) are still reached at build time.

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
