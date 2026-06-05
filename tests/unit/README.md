# `tests/unit/` — Wave 1 (deterministic CORE)

Pure-function unit tests for DeepCommit's deterministic **CORE** (stratum 1 of the
project's testing strategy).
No Docker, no git, no live agent — every input is in-memory, every run is reproducible.
The legacy suite lives in [`tests/legacy/`](../legacy/) and is excluded from collection.

## Depth bar (non-negotiable)

Every target gets, at minimum:

1. the **nominal** case,
2. the **empty / degenerate** input,
3. at least one **adversarial / near-miss** case proving the function does *not*
   over-match or mis-bucket, and
4. an assertion on the **exact returned shape/values** (keys, ordering, sub-fields) —
   never bare `assert result` / `assert x is not None`.

Expected values are **ground-truthed by executing the function**, not statically guessed.

## Determinism

Several targets return values derived from `set`/`dict` iteration. Tests are
**`PYTHONHASHSEED`-proof**: they assert on `sorted(...)` / canonical shapes, never on
incidental set-iteration order. CI pins `PYTHONHASHSEED=0` as a second line of defense.
The whole suite is verified identical under `PYTHONHASHSEED=0` and `=1`.

## What Wave 1 covers (508 tests: 505 pass + 3 strict-xfail)

| File | Target (`deepcommit/…`) |
|---|---|
| `test_graph_algorithms.py` | `tools/utils/graph_algorithms.py` — transitive closure / reduction (+ idempotency, witnesses, statistics) |
| `test_dag_analyzer.py` | `tools/dag_analysis/dag_analyzer.py` — cycles, levels, critical path, CSV round-trip |
| `test_commit_dag_v3.py` | `preprocess/commit_dag/commit_dag_v3.py` — `parse_unified_diff` (modify/add/delete/rename/multi-file), `topological_levels` |
| `test_classifier.py` | `harness/test_runner/core/classifier.py` — transition taxonomy, skipped family, severity collapse, summary |
| `test_frozen_check_gate.py` | `harness/main_orch/frozen_check_gate.py` — quantity/signal gates, grading, policy/config locks |
| `test_dockerfile_lint.py` | `harness/prepare_images/build_milestone_image/dockerfile_lint.py` — current rule set |
| `test_find_matching_nodeid.py` | `harness/prepare_images/collect_milestone_patched_tests.py` — per-strategy + no-over-match + shortest-wins |
| `test_diagnose_error_causes.py` | `harness/main_orch/diagnose_error_causes.py` — normalize / classify / record / log (workspace-curated via `_error_taxonomy.yaml`) |
| `test_appendix_a_bugs.py` | strict-xfail pin for the `_is_prerelease` Appendix-A bug |
| `test_repo_hygiene.py` | T0 gate: ban machine-absolute paths / secret assignments in active tests/config |

### Pinned latent bugs (strict-xfail — see spec Appendix A)

- `transitive_reduction` over-removes edges on **cyclic** graphs (reachability loss) — discovered in Wave 1.
- `_is_prerelease` over-broad regex flags final tags containing `test`/`rc` substrings.

A strict-xfail flips to a hard failure the day the source is fixed, forcing the
pin (and its Appendix-A note) to be removed.

## Deferred to later waves (with reason)

| Target | Wave | Why |
|---|---|---|
| `exclude_patterns` / `should_exclude_file` | 3 | belongs to the shared `path_filters` seam; pin once against the SoT |
| `milestone_prompt_render` | 2 | a prompt-**render** test (T2 golden), asserts template output, not pure CORE |
| `milestone_build_status` (waiver/numstat/git) | 3/4 | needs the `assemble_build_status` seam + real git/`DATA` |
| `analyze_milestone_raw_signals`, file loaders, CLI entrypoints | 3/4 | need fs fixtures / subprocess — IO stratum, not CORE |

Source-code edits are mostly out of scope (a CORE bug is **pinned**, not fixed) — the one
exception so far is the `parse_jest_report` whole-suite-error fix (a real-data finding, below).

## Real-data validation (2026-06-02)

The Wave-2 fixtures were synthesized, so they were validated against **real** EvoClaw runs
(`/data2/.../EvoClaw-data`) across Go / Rust / Java / Python / JS:
- `find_matching_nodeid`: confirmed on real multi-language nodeids — **0 over-matches**.
- Report parsers: confirmed on real reports; surfaced & **fixed** a jest false-negative
  (a suite that fails to compile was dropped → now an `error` entry; regression:
  `tests/golden/test_report_parser_golden.py::test_jest_whole_suite_error_emits_error_entry`).
- Real-shape fixtures harvested + scrubbed into `tests/fixtures/report_parser/`:
  `jest_suite_error.json`, `pytest_real_outcomes.json` (real xfailed/xpassed), `cargo_build_fail.log`,
  `maven_build_fail.log`.

**Deferred real-sample upgrades (follow-up):**
- cross-framework build-failure → `error`: cargo/maven/go currently parse a broken build to
  *empty* — the same false-negative family as the jest fix (pinned by `test_build_failure_parses_to_empty`).
- maven **surefire-XML** mode (the preferred path; needs a small XML/tar fixture).
- go_test mixed compile-error + jsonl stream; ginkgo `pending`/`skipped` states.
- django: no real repo in the dataset → remains synthetic-only.
