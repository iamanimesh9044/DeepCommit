# DeepCommit Internals

The **how-it-works** companion to `execution_guide.md` (which is run-it usage only). It explains what each
phase's modules do internally and the exact commands they run when you intervene. Read this to understand
the pipeline; use `execution_guide.md` to run it.

## Phase 1: Config Preparation

`config/<repo_range>.yaml` is the **source-boundary contract**: it tells every downstream phase what counts
as product **source** vs **test** vs **excluded**. It is authored once, before preprocess, because Phase 1
does not analyze the repo blindly — the whole pipeline filters against this boundary, so a wrong one drifts
silently: `repo_src_dirs` too narrow drops real product commits / too wide pollutes the DAG with
docs·benchmarks·generated code; incomplete `test_dirs` misclassifies tests as product logic; a wrong
`main_branch` resolves the range against the wrong lineage.

**The matching contract** (the #1 silent-corruption source — the three fields do NOT share semantics):

- `repo_src_dirs` = **literal path prefixes** (`str.startswith`, trailing-slash normalized), NOT globs —
  `sklearn/*` matches nothing.
- `test_dirs` / `exclude` = **gitwildmatch globs, root-anchored** — `tests/**` matches only a *root*
  `tests/`; nested tests need `**/tests/**`. Languages differ: Go tests are co-located `*_test.go` (no test
  dir), Java tests are a parallel `src/test/` tree — the skill carries the per-language matrix.
- `exclude` only targets files *inside* `repo_src_dirs` (everything else is already dropped by the prefix filter).

**The tool** (`deepcommit/preprocess/prepare_range_config/`) is a clean agent prompt + a deterministic gate:

1. `prefetch.py` emits a context brief — detected languages + a per-top-level-dir file histogram (source
   lives where the `src-ext` counts cluster) + the changed-files-in-range breakdown.
2. The agent (`skill.md`) drafts the four fields from the brief, then loops draft → validate → fix.
3. `validate.py` is the gate. It classifies the repo's files through the **single canonical `SrcFileFilter`**
   — the same engine preprocess and the harness use, so a
   green gate means the config behaves identically downstream. It reports per-FileClass counts, **ERRORs**
   (block, exit 1: a glob in `repo_src_dirs`, zero source files, obsolete `struct_field_fixes`) and **WARNs**
   (advisory: nested tests misclassified as source, source-extension files outside the boundary, Phase-3 fields).

Only boundary fields are authored here. Phase-3 build/runtime config (`base_image_name`, `build_command`,
`language`, `test_framework`) lives in the workspace, never in this file — the harness must not mutate the
hand-authored config.

## Phase 3: env_resolution — Modules 1, 2, 3

Phase 3 runs as a pre-loop **Base Env Build**, a **Module 1 ↔ 2 loop** driven by the **Module 3
meta-orchestrator**, and a post-loop **Test Curation** (see `execution_guide.md` § Phase 3 for the commands
you actually run). Normally you just launch M3 and it drives the loop; the sections below explain what each
module does and the commands it — or you, on intervention — runs.

### Module 1: Milestone DAG Optimization + Testbed Preparation (loop)

This module is an **agent-driven iterative loop**, not a one-shot command. Phase 2's milestone DAG almost always has some edges/partitions that cause cherry-pick conflicts for non-trivial version ranges — the agent refines the DAG until `build_milestone_testbed.py` creates all milestone tags cleanly.

**Standard invocation** — use this for every iteration:

```bash
python3 -m deepcommit.harness.prepare_repo.build_milestone_testbed \
    --trial scikit-learn_scikit-learn_1.5.2_1.6.0_v1 \
    --continue-on-error --incremental --verbose
```

**Workflow**: invoke the skill [`reference_module1_testbed`](../deepcommit/harness/main_orch/prompts/reference_module1_testbed.md). 

**Requires:** Phase 2 result (`milestones.csv` + `dependencies.csv`), base image from Base Env Build.

**Output:** `DATA/harness_workspace/<repo_range>/<trial_suffix>/`:
- `testbed/` — git repo with `milestone-{MID}-start` + `milestone-{MID}-end` tags
- `milestones.csv` / `dependencies.csv` — refined (may differ from Phase 2 output)
- `build_report.json` — latest run's conflict diagnostics
- `analysis_report_run_N.md` — per-iteration agent reasoning

### Module 2: Environment Configuration and Test Collection (loop)

For each milestone, a Claude agent writes a `Dockerfile` (`FROM <base_image>`) and `test_config.json`, then builds the image and validates it against three constraints: (1) project compiles, (2) tests run, (3) milestone-patched tests are all collected.

Milestone images can be built in parallel because each one is `FROM <base_image>` (not chained off another milestone's image) and source state is already pinned by Module 1 as `milestone-{MID}-start`/`-end` tags in `testbed/`. If a milestone's failure actually stems from a DAG topology problem (a commit placed in the wrong milestone), retrying Module 2 in isolation won't fix it; you need to go back to Module 1 — Module 3 is what routes between the two automatically.

**Build all milestones:**

```bash
python3 -m deepcommit.harness.prepare_images.build_milestone_image \
    --work-dir DATA/harness_workspace/scikit-learn_scikit-learn_1.5.2_1.6.0/v1 \
    --all \
    --parallel 4
```

**Build specific milestones (positional IDs):**

```bash
python3 -m deepcommit.harness.prepare_images.build_milestone_image \
    M001 M002 \
    --work-dir DATA/harness_workspace/scikit-learn_scikit-learn_1.5.2_1.6.0/v1 \
    --parallel 2
```

**Requires:**

- Module 1 output: `testbed/` with `milestone-{MID}-start/end` tags, `metadata.json`, `milestone_patched_tests/`
- Base Env Build output: base Docker image (default inferred as `{repo_id}/base:latest`; override with `--base-image`)

**Output:**

```
DATA/harness_workspace/<repo_range>/<scenario>/
├── dockerfiles/
│   └── {MID}/
│       ├── Dockerfile
│       ├── test_config.json
│       ├── env_setup_prompt.md     # archived rendered prompt (includes refine context if applicable)
│       └── build_status.json       # authoritative Module 2 hard-gate result
├── test_results/{MID}/refinement/attempt_N/
│   ├── start.json, end.json, classification.json
│   └── summaries / skip_analysis.md   # refinement hard-gate evidence, not final benchmark result
├── build_runs/{MID}/attempt_N/
│   ├── after_agent/
│   └── after_validation/               # build_status snapshots and copied latest_attempt artifacts
├── agent_logs/                     # per-attempt agent stdout/stderr
└── build_summary.json              # per-run build orchestration status
```

Docker images are tagged `{repo_id}/{scenario}/{MID}:latest`.

**Iteration.** Per-milestone retries (up to `--max-retries`) trigger when artifacts are missing, Dockerfile lint fails, tests fail, metrics fail, or patched tests aren't collected. Inspect `build_summary.json` for failed milestones; rerun the same command (refine mode picks up where it left off) or pass specific milestone IDs. See `build_milestone_image/README.md` for the full pipeline and mode flags.

Optional small-batch peer-context experiments can run several related milestones together to inspect shared environment or runner failures, but the peer signal is advisory only and does not change Module 2 hard gates or the milestone DAG.

### Module 3: Meta-Orchestrator (decision layer, skill-driven)

Phase 3's top-level decision agent. Reads Modules 1/2 artifacts, decides per round whether to iterate M2 (`env_refine`), refine M1 DAG (`dag_update`), do a surgical recipe edit, or freeze a DAG-closed prefix. Operates as a **4-step iteration loop** (Analyze → Decide → Execute → loop), one event per turn, with `plan.yaml` as the append-only event ledger.

**Skill + spec:**

- `deepcommit/harness/main_orch/prompts/main.md` — main meta-agent operating manual
- `deepcommit/harness/main_orch/prompts/skill_audit_{single_milestone,cross_milestone_dag}.md` (Skill 1 / Skill 2) + `deepcommit/harness/main_orch/prompts/reference_freeze_ceremony.md` § Skill 3 — the three audit subagents M3 dispatches via Agent tool when complexity exceeds main-agent context
- `deepcommit/harness/main_orch/frozen_check_gate.py` — 7-gate `frozen_check` evaluator (called per round when a DAG-closed prefix looks ready to freeze)

**Invocation:** currently a manual **standalone `claude` CLI subprocess** that ingests the M3 skill, one round per `_orch_runs/round_NNN/` directory. A production launcher (parallel to M2's batch CLI) is pending.

**Cross-round memory:** `_frozen_prefix.json` (frozen MIDs + `scope_milestones` + history) at workspace root + per-round `narrative.md` + accumulated `rejected_suggestions.yaml` + `deferred_to_next_run` backlog.

**Per-round output** under `_orch_runs/round_NNN/`:

```
plan.yaml                            append-only events + deferred backlog
narrative.md                         reasoning trail (Analyze / Decide / Execute)
subagent_log/{type}_seq{NN}_*.md     one file per Skill / M1 / fork dispatch
rejected_suggestions.yaml            verified-wrong hypotheses (cross-round immune system)
frozen_check.json                    frozen_check_gate.py output (freeze_check events only)
fork_freeze_decision.md              fork subagent FREEZE/WAIT report (freeze_check events only)
snapshots/seq_XX_pre/{MID}/          pre-state backups before validate_only / surgical
exec_logs/NN_<event>.log             optional raw stdout/stderr per event
launch_brief.md                      operator's round-start brief (launcher-injected)
```

The `freeze_check` event is a **3-component ceremony**: script gate (mechanical, 7-gate threshold check) + Skill 3 latent-risk audit (judgement: 6 risk categories) + fork decision (M3's contrarian self vs. its own plan). Override pattern per `reference_freeze_ceremony.md § Freeze decision rules` Exception clause.

## Phase 4: srs_verify — SRS generation + human verification

Phase 4 consumes the complete `env_resolution` bundle and produces the **SRS** (Software Requirements
Specification) — the only input the agent receives at evaluation time. A good SRS must (1) describe the
milestone's requirements accurately, (2) leak no implementation detail (no function/variable names or code
structure from the gold patch), and (3) match what the F2P tests check. It is reverse-engineered from the
gold patch, then verified. (The `generate_srs` command is in `execution_guide.md` § Phase 4.)

**Requires:** Phase 3 complete for every milestone — `testbed/` with start/end tags, `dockerfiles/{MID}/`,
per-milestone Docker images, `milestone_patches/{MID}.patch`, and `test_results/{MID}/benchmark/f2p_tests/`
from post-loop benchmark replicates, not Module 2 refinement.

### SRS Generation

A three-agent sequential pipeline reverse-engineers requirements from the gold patch.

**Output:** `DATA/github_data/preprocessed/<repo_range>/srs/{version}/{MID}/SRS.md` + `env_dependencies.md` + prompt archives.

### Human verification (required before use)

Phase 4 is currently **one-shot** — no dry-run solves the task to validate SRS. A human must review each
milestone before it goes into the benchmark:

- **SRS.md**: covers the milestone scope without leaking implementation (no function/variable names from the gold patch, no code structure hints)
- **F2P tests** (from Phase 3 Test Curation): the set matches what the SRS asks for (no extraneous tests, nothing missing)
- **Mark verified**: rename the version directory (e.g., `srs/v1/{MID}/` → `srs/v1_verified/{MID}/`) once reviewed. Unverified SRS should not be used for evaluation.

Known gaps: automatic dry-run validation (an agent solving the task from the SRS alone, to catch missing or leaked information), iterative SRS refinement, and auto-propagation of SRS-unresolvable findings back to Phase 3 (DAG edits, milestone re-partitioning) are all TODO.
