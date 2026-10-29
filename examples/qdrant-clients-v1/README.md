# Example: Qdrant contract + two SDKs (qdrant-clients-v1)

A complete run of the multi-repo pipeline, kept so you can see what each stage produced. How the pipeline works and the commands
to run it are in [docs/multirepo.md](../../docs/multirepo.md).

**Repos** (Qdrant v1.18.0 to v1.20.0): `qdrant` (contract only: the `.proto` files), `rust-client`, `qdrant-client`.
**Result:** a Harbor multi-step task with 11 steps. Oracle: every step scores 1. No-op: every step scores 0.

The task itself (78 MB with the vendored repositories and the Qdrant server binary) is in the bucket as a zip; see
[ARTIFACTS.md](ARTIFACTS.md). This folder holds everything else, and the task without its large files.

## Stages and where to look

| Stage | Folder | What is in it |
| --- | --- | --- |
| Workspace | `qdrant_clients_v1.workspace.yaml`, `workspace_meta/` | The manifest, the repo table, which repo each commit belongs to |
| Phase 1 seeds | `phase1_seed/milestone_seeds.jsonl` | Candidate seed commits per repo |
| Phase 2 milestones | `phase2_milestones/` | Per-batch proposals, `seed_groups.json`, `consolidation_report.md`, `bundle_report.json`, final `milestones.csv` (35 milestones, `repos` column) |
| Phase 3 dependencies | `phase3_dependencies/` | Pre-selected candidates and inferred dependencies between milestones |
| Prompts | `prompts/` | The rendered prompts of the stages |
| Export | `export/` | `export_report.json` (milestone of every step, background commits, conflicts), `measure.json.gz` (per-test outcomes of the 19 exported steps, before and after each gold patch, 2 runs), `measure_folded.json.gz` (the same merged into 11 steps), `graph.html` (commit and milestone graph), `task_viewer.html` (instruction, gold code and hidden tests per step) |
| Gates | `gates/` | `oracle_vendor.json` and `nop_vendor.json` (oracle 11/11 = 1 and no-op 11/11 = 0 on the vendored image), `oracle_nop_earlier.json` (first oracle and no-op runs) |
| Task | `task/` | `task.toml`, README, Dockerfile, `tests/` (grader, suites), and per step the instruction, gold patches and F2P/P2P ids. Without `environment/vendor/` and the hidden test sources |

## What happened to the 35 milestones

| Outcome | Count | Why |
| --- | --- | --- |
| Became a step | 19 | Cross-repo or single-repo milestones with owned tests |
| Background | 13 | No owned tests; their commits are replayed into the base |
| Not built | 3 | M35, M17_sub-02, M17_sub-03: cherry-pick conflict on `poetry.lock` |

Measurement then folded the 19 steps into 11, because the base state is inconsistent (the Python client uses generated gRPC stubs that
a later milestone regenerates, so it does not import until then). The folded steps:

| Step | Milestones | Gold patch |
| --- | --- | --- |
| m1 | M02 | rust-client |
| m2 | M21, M22_sub-01, M22_sub-02, M22_sub-04 | qdrant-client |
| m3 | M08, M22_sub-05 | qdrant-client |
| m4 | M15, M22_sub-06 | qdrant-client |
| m5 | M24 | qdrant + qdrant-client |
| m6 | M03 | rust-client |
| m7 | M25 | rust-client |
| m8 | M14, M04_sub-01, M04_sub-02 | qdrant-client + rust-client |
| m9 | M17_sub-01 | rust-client |
| m10 | M10 | rust-client |
| m11 | M34, M36 | qdrant-client + rust-client |

DAG: `m2 -> m3; m3 -> m4; m3 -> m5; m1 + m5 -> m6; m1 -> m7; m7 -> m8; m7 -> m9; m9 -> m10; m3 -> m11`.

## Limits of this example

- Cross-repo coupling is weak: the upstream clients lag the contract, so only m5, m8 and m11 change more than one repo, and no test checks that two repos agree.
- 11 background commits were replayed with `-X theirs` and about 25 test-only conflicts were auto-resolved; neither was reviewed.
- Instructions were generated from the gold patch and hidden tests, not from a full Phase 4 specification.
- No real-agent run was done, so the difficulty is unknown.
