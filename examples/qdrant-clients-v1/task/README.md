# qdrant-clients-v1-001

Cross-repo milestone task derived with DeepCommit from the `qdrant_clients_v1` workspace (qdrant, rust-client, qdrant-client, v1.18.0 to v1.20.0). One agent keeps one environment through the steps below with no reset between them. After each step Harbor collects the agent's work, a clean verifier container applies it to the base state and runs hidden tests across all repositories. Episode score is the mean over steps.

## Status

| Step | Milestones | Gold patch | F2P (repos) | P2P | Gates |
| --- | --- | --- | --- | --- | --- |
| m1-upgrade-tonic-prost-reqwest-dependencies | M02 | rust-client (554 lines) | 1 (rust-client) | 0 | oracle 1 / nop 0 |
| m2-remove-deprecated-fastembed-methods | M21, M22_sub-01, M22_sub-02, M22_sub-04 | qdrant-client (9186 lines) | 272 (qdrant-client) | 1 | oracle 1 / nop 0 |
| m3-remove-max-disk-usage-percent-and-deprec | M08, M22_sub-05 | qdrant-client (2553 lines) | 54 (qdrant-client) | 267 | oracle 1 / nop 0 |
| m4-filter-and-prefix-sdk-consumption-in-pyt | M15, M22_sub-06 | qdrant-client (1254 lines) | 113 (qdrant-client) | 401 | oracle 1 / nop 0 |
| m5-grpc-payload-conversion-performance-qdra | M24 | qdrant (315 lines), qdrant-client (343 lines) | 28 (qdrant-client) | 401 | oracle 1 / nop 0 |
| m6-qdrantserverless-rust-client | M03 | rust-client (2697 lines) | 1 (rust-client) | 400 | oracle 1 / nop 0 |
| m7-rust-client-v1-19-sdk-sync | M25 | rust-client (968 lines) | 8 (rust-client) | 401 | oracle 1 / nop 0 |
| m8-deprecated-search-sdk-sync-in-rust-clien | M14, M04_sub-01, M04_sub-02 | qdrant-client (4052 lines), rust-client (181 lines) | 25 (qdrant-client) | 410 | oracle 1 / nop 0 |
| m9-bm25-and-formula-contract-with-rust-clie | M17_sub-01 | rust-client (517 lines) | 4 (rust-client) | 410 | oracle 1 / nop 0 |
| m10-sync-nested-payload-index-parameters-to | M10 | rust-client (202 lines) | 1 (rust-client) | 414 | oracle 1 / nop 0 |
| m11-python-client-grpc-and-rest-conversion-f | M34, M36 | qdrant-client (135 lines), rust-client (35 lines) | 16 (qdrant-client) | 415 | oracle 1 / nop 0 |

DAG: `m2 -> m3; m3 -> m4; m3 -> m5; m1 + m5 -> m6; m1 -> m7; m7 -> m8; m7 -> m9; m9 -> m10; m3 -> m11`

A step is the smallest run of consecutive milestones that has at least one hidden test going from failing to passing across its gold patch. Milestones that cannot be graded alone (for example a change that leaves the checkout unimportable until generated stubs land in a later milestone) are folded into the next step.

Milestones in steps: 19. Milestones whose commits are background in the base state (no hidden tests of their own): 13. Not built: M35 (blocked), M17_sub-02 (skipped), M17_sub-03 (skipped).

## Repositories

| Repo | Role | Upstream | Base commit |
| --- | --- | --- | --- |
| qdrant | contract (contract only) | https://github.com/qdrant/qdrant | `db3fca327851` |
| rust-client | sdk | https://github.com/qdrant/rust-client | `357dec9e56da` |
| qdrant-client | sdk | https://github.com/qdrant/qdrant-client | `326adefcc215` |

`/app/qdrant` is the contract repository, sparse: only `lib/api/src/grpc/proto/**/*.proto` (the wire contract the clients consume). It has no tests and nothing to build; contract edits are collected and applied like any other repo.

## How grading works

1. `csf-collect` writes `/logs/artifacts/model.<repo>.patch` for every repo (the agent's changes against `/opt/csf/base/<repo>.git`, which the agent cannot alter).
2. The verifier builds a fresh container from `environment/`, applies each patch (changes under test dirs are dropped), copies `tests/hidden/<repo>/` in, starts a real Qdrant server (`tests/suites.json` `services`), runs the suites (pytest for qdrant-client, `cargo test --offline` for rust-client).
3. `tests/grade.py` grades exactly the ids in `steps/<step>/tests/ids.json` (`f2p`: must pass, they failed before the gold patch; `p2p`: must keep passing). `reward` is 1 only if every F2P and P2P id passed in every repo.
4. The ids were measured, not guessed: the gold patches were applied step by step in the task's own image and every test was run twice per state (`deepcommit/export/templates/measure.py`, `curate.py`); a test that failed in any run is excluded. pytest retries a failing test twice (congruence tests compare local mode with the server on random data).

## Running it

Needs Harbor 0.25 or newer (multi-step tasks, schema 1.4) and Docker. The three repositories (pinned commits) and the Qdrant server binary are vendored in `environment/vendor/`, so the build does not depend on GitHub; it still downloads toolchains and crates from their registries, so it needs network. The trial itself needs none.

```bash
harbor run -p <this folder> -a oracle        # gold solution: every step must score 1
harbor run -p <this folder> -a nop           # no change: step 1 must score 0
harbor run -p <this folder> -a claude-code -m <model>
```

## Known limits

- **Cross-repo coupling is weak.** The upstream clients lag the contract: server proto changes reach the clients in release-sync commits, so within one milestone the contract part and the client part rarely depend on each other. Steps whose gold touches several repositories are listed above; their hidden tests are each repository's own tests, so no test checks that two repositories agree with each other.
- Instructions are generated from the gold patch and the hidden tests (`deepcommit/export/gen_instructions.py`), not from a full Phase 4 SRS.
- `-X theirs` was used when replaying 11 background commits and about 25 test-only conflicts were auto-resolved during export (see `_src/csf-qdrant-clients-v1-001/export_report.json`); the measured tests all pass on the resulting states.
- Milestones M35, M17_sub-02 and M17_sub-03 are not built (cherry-pick conflict on `poetry.lock`).
- The Python tests that need FastEmbed models are skipped (no model download at run time); embedding-model tests are not part of any step.
