# Phase 3 Module 3 — Operational Patterns (reference)

Deep heuristics for specific recurring situations, relocated verbatim from `main.md`'s
"Reference: operational patterns" section. Read on demand, not every round — pull the matching
subsection when you hit its situation (the retrieval index in `main.md` maps situation → subsection).

**Cross-references:** bare `§Name`, "below"/"above" resolve within this file or back to **`main.md`**;
the per-event runbooks cited as "§Step 3 <event>" live in `reference_event_runbooks.md` (this folder).

---

### Gate aggregation semantics

The 3 M2-derived signal gates aggregate per-milestone signals into a subgraph-level ratio. The **aggregation mode** shapes strategy:

| Gate | Aggregation | Reason |
|---|---|---|
| `H_patched_completeness_ratio` | **value_mean** | Each MS has continuous 0–1 completeness. MS at 0.97 contributes 0.97 |
| `H_none_error_fail_to_pass_present_ratio` | pass_count_ratio | Each MS either has ≥ 1 test transitioning to pass (`fail→pass` / `none→pass` / `error→pass` / `skipped→pass`) or doesn't. Boolean |
| `H_anomaly_within_budget_ratio` | pass_count_ratio | Each MS either under anomaly threshold or isn't. Boolean. **Naming**: higher ratio = more MS within anomaly budget = better; threshold ≥ 0.80 |

For `H_patched`, don't think only in flip-pass-or-not terms. **Every percentage point across all milestones matters.** A milestone improving 0.20 → 0.50 contributes more to the subgraph mean than one going 0.97 → 1.0.

**Authoritative value**: gates evaluate against `value_real` (waiver-exclusive raw signal). `value_with_waiver` is informational only — waivers cannot mask the gate.

#### Field-name map (waiver-exclusive raw value)

The same per-MS quantity (raw value of an H gate, before waivers) is stored under three different field names across artifacts. Use this map when cross-referencing:

| Artifact | Path | Field name |
|---|---|---|
| `dockerfiles/{MID}/build_status.json` | `test_collection_and_result_quality.H_<gate>.value` | `value` |
| `_orch_runs/round_NNN/frozen_check_*.json` | `milestones.{MID}.real_value.H_<gate>` | `real_value` |
| `_orch_runs/round_NNN/frozen_check_*.json` | `gates.H_<gate>_ratio.value_real` | `value_real` |

All three refer to the same underlying quantity. When narrative references `H_patched=0.95` without qualification, assume it means raw (waiver-exclusive) — i.e., the path that ends up as `value_real` at the aggregate level. For waiver-included values, the field is `value_with_waiver` on the same gate dict.

### H_patched_completeness mechanics

Per milestone:

```
value_real = patched_collected / effective_tests
```

- `effective_tests` = `(tests_added ∪ tests_modified) − tests_deleted` aggregated by `collect_milestone_patched_tests.py`. Includes src→test inference.
- `patched_collected` = the subset the test runner collected and classified into a transition category. Pass-ending transitions (`F→P`, `none→P`, `error→P`) and the stable/regression ones (`P→P`, `F→F`, `P→F`, `S→S`) all count — "collected" means the test ran and was classifiable at all. `error→P` (errors at `-start` because the milestone's own src is absent, passes at `-end`) is a counted positive signal, not a gap.

Four implications:

1. **Waivers don't enter the numerator.** `value_with_waiver` is informational only. Don't plan around stacking waivers.
2. **`bw_payoff` doesn't predict `patched_collected` gain.** It measures backward-payoff at commit-signal level, not collection level. Don't forecast H_patched gains from `bw_payoff` numbers.
3. **`error→pass` is a counted positive signal, not a gap.** When a patched test errors at `start` (the milestone's own src isn't applied yet, so the symbol it references is absent) and passes at `end`, it counts in `patched_collected` and in the `H_none_error_fail_to_pass_present` gate — exactly like `none→pass`. No surgical preapply, DAG op, or special handling: it already credits.
4. **"Sandbox cherry-pick verified clean" rationale ≠ guaranteed collection-fix.** "Advances anchor failure point" = partial; "made test collect" = strong evidence. When a test imports several future symbols, ALL must be brought upstream in the same batch.

### Testbed start-state invariant

`build_milestone_testbed.py` constructs `milestone-{MID}-start` as:

- **src/** = cumulative cherry-pick of all transitive prereqs' src commits (does NOT include MID's own src commits)
- **non-src/** = snapshot from MID's last commit (= byte-identical to `milestone-{MID}-end`'s non-src/)

Consequence: a test in MID's `effective_tests` that references an API introduced by MID's OWN src commits errors at `-start` (the API isn't there yet) and passes at `-end` → an `error→pass` transition. This is a **fundamental invariant**, not a DAG defect — the testbed enforces it so test-state transitions reflect only src changes.

`error→pass` from this invariant is a **counted positive signal** (in `patched_collected` and the `H_none_error_fail_to_pass_present` gate), so it needs no action. Do **not** reach for a DAG op (`reassign_commit` / `split_milestone` / `merge_milestone`) to "fix" it: DAG ops can't change "the start state lacks the milestone's own src," and nothing needs fixing — the signal already credits.

### Leverage analysis (deciding which milestone to improve)

Given the `value_mean` aggregation, compute **expected impact** per candidate action:

```
delta_aggregate = (new_real_value - old_real_value) / N_milestones_in_candidate
```

Combine with **probability of success** (effort cost × likelihood of working). Cheap high-probability moves on near-threshold milestones can be the right call even when impact looks small — stacking several such moves can cross the aggregate threshold.

Don't pick by impact alone.

### Composite `dag_update` patterns (beyond single-op edits)

Two composite patterns recur often enough that you should compose them as one event (multiple op categories in one `final_ops` block).

#### Single-commit extraction

When one commit needs to be the source-of-truth for multiple candidate milestones (fixes patched_tests across several milestones, but no single existing home reaches all of them):

```yaml
final_ops:
  split_milestone:
    - milestone_id: <old_owner>       # omit if SHA was orphan
      at_index: 1
      new_ids: [seed_<theme>, <old_owner>]
      edge_rewire:
        new_inbound_for_b: []          # seed has no parents (root-adjacent)
        outbound:
          - from_b_to: [<old_owner_outbound_targets>]
      rationale: extract <SHA> as standalone (multi-target / orphan / cycle-break shared commit)
  add_edge:
    - {prerequisite_id: seed_<theme>, dependant_id: <target_a>, rationale: ...}
    - {prerequisite_id: seed_<theme>, dependant_id: <target_b>, rationale: ...}
```

Verify before applying:
- Cycle-check each new edge (target must not reach the new seed in pre-edit DAG)
- Sandbox-verify cherry-pick of the single SHA applies at the new (root-adjacent) position; if not, the commit has implicit prereqs → escalate (likely an in-milestone refactor, not a clean extraction)

**⚠ Not for a foundational-symbol collapse.** Extraction is for a *feature / test-providing* commit that needs a shared home. It is the WRONG tool when a milestone **collapses** (H_patched ≈ 0, ~1 test collected, `import <pkg>` crashes on a missing maintenance symbol):

- The collapse is a **cascade, not one symbol** — extract the symbol the crash names and the next missing one surfaces. Whack-a-mole.
- The extracted commit drags its **early-dated ladder** into every downstream closure, regressing already-built milestones (observed in practice: 0 fixes, several `built → build-break` → full revert).

Collapse = a *closure too narrow to reconstruct a working tree*, missing a whole band of foundational maintenance — not one absent commit. Fix by **broadening the closure** (add the proper chronological-predecessor maintenance milestones as prereqs) or make a **scope decision**, not extraction. Verify a symbol's reachability by **content** (`git show milestone-{MID}-end:<path>`), never `git merge-base --is-ancestor` — the cherry-pick testbed rewrites SHAs, so a present symbol can show MISSING.

#### Mutual file dependency cycle-break

When two milestones A, B have mutual file dependency (A owns commit X providing a symbol B needs; B owns commit Y providing a symbol A needs):

```yaml
final_ops:
  reassign_commit:
    - {commit_sha: X, from: A, to: seed_<theme>, rationale: ...}
  drop_edge:
    - {prerequisite_id: A, dependant_id: B, rationale: dissolves — X no longer owned by A}
  add_edge:
    - {prerequisite_id: seed_<theme>, dependant_id: A, rationale: A still needs X}
    - {prerequisite_id: seed_<theme>, dependant_id: B, rationale: B still needs X}
    - {prerequisite_id: B, dependant_id: A, rationale: Y-side dep, cycle-safe now}
```

Verify:
- After `drop_edge`, `reaches(A, B) == False` AND `reaches(B, A) == False`
- After the final `add_edge B→A`, `reaches(A, B) == False` still (no cycle)

If the new seed is genuinely new (not pre-existing in `milestones.csv`), the implicit `add_milestone` precedes the dag_update event — write the new row to `milestones.csv` before the event runs; the schema records the SHA's new home via `reassign_commit.to`.

### M2 waste patterns — detect before retrying

Each M2 agent attempt has meaningful $ and wall-clock cost. Before scheduling a new `env_refine` mode=`agent` on a milestone that has prior attempts, **read its `_build_runs/{MID}/attempt_*/after_agent/` history** and classify the last two attempts' diffs:

| Pattern | Detection | Action |
|---|---|---|
| **A · Bit-identical attempts** | `sha256(attempt_N/after_agent/<file>) == sha256(attempt_{N-1}/after_agent/<file>)` for Dockerfile + waivers + signals | **STOP retries.** Same input → same output. Re-invoking burns full cost for zero new info. |
| **B · Frozen Dockerfile, marginal waiver churn** | Same Dockerfile sha across ≥ 3 attempts AND `|bound_waivers count Δ| ≤ 2` AND `|unbound_issues count Δ| ≤ 2` | **STOP retries.** Agent has converged on `agent_sufficient` state. Either escalate or switch to `regen_dockerfile` if you have strong reason to suspect the Dockerfile is the blocker. |
| **C · Oscillating Dockerfile then steady-state** | Same `failed_gates` set + same Dockerfile sha across last 3 attempts; waiver diff < 100 lines between consecutive | **STOP retries.** M2 fully converged. Further attempts won't change `failed_gates`. Decide next strategy: DAG-edit, surgical recipe fix, unbound conversion, or `halt: stuck`. |
| **D · Real incremental progress** | New `bound_waivers` / `unbound_issues` entries with populated `affected_error_groups[]`; new `commit_moves` entries with `status: "verified"`; `failed_gates` shrinks OR `adjusted_end_error` drops materially | **KEEP RETRYING.** This is the legitimate M2 work cycle. Continue until A/B/C emerges or `max-retries` hit. |

**Special trigger**: when the upstream commit set for a milestone is unchanged (`start_sha + end_sha` unchanged since last attempt) AND the prior attempt was pattern A/B/C → the milestone is in **stable saturation**. Don't reissue M2 without first changing something material (DAG edit, hint injection, surgical recipe fix).

### Saturated milestones still contribute

A milestone stuck at `H_patched_completeness.real_value` well below 1.0 (saturated) **still contributes its raw value** to the subgraph aggregate via `value_mean`. It's not zero. So:

- A/B/C-pattern saturation ≠ zero contribution. The milestone keeps its existing partial value. We just stop spending M2 retries on it.
- DAG-edit or split for the saturated milestone is the path to improve its raw_value. If we can add even one cycle-safe ancestor that brings in features → tests collect → raw_value bumps.
- The waste-pattern check answers "should I retry M2?", not "is this milestone hopeless?". Saturated milestones still need DAG-edit or surgical attention to improve their raw_value contribution.

### Suspect the metric before grinding the DAG

When an aggregate gate won't cross threshold and per-MS `real_value`s look stuck, first ask whether the gap is *real* or a **measurement artifact** — before spending env/DAG attempts to brute past it. The collection matcher has documented blind spots (nodeid-prefix mismatch when `test_cmd`'s rootdir is wrong → silent 0/N false-fail; subtest-block enrichment misses → distinct errors collapse into one generic bucket). And a prior attempt's `unbound_issue` count is routinely inflated 7–10× by shims absorbing symptoms — reconcile against the latest `classification.json` / `end_summary`, not the stale count. If you find a **concrete, verifiable** signal the harness under-counts, record it `deferred_to_next_run` priority=high and surface to the operator (that's a harness fix), rather than grinding env/DAG work around it.

Guard: defer **only** with a cited, reproduced artifact in the narrative — name the specific nodeid that false-fails under the wrong rootdir, or the exact error bucket that collapses N distinct crashes into one. A blanket "the metric might under-count" is not enough, and is not a license to dodge ordinary low coverage.

### Built is a strict per-MS verdict — read the gate *class*, don't take `built` at face value

`built=true` (the legacy verdict) requires EVERY gate to pass — a deliberately strict bar. It is **not** a quality score, and `built=false` does **not** mean low quality: some gates are **whole-suite** and can be inflated by **TIMING_DAG** tests (tests ahead of the milestone's own src, failing/erroring at both START and END — often hundreds:1 vs the milestone's own patched tests). So `built=false` is not itself a quality verdict — M3 reads the gate **class** (below), not the binary.

**Two gate classes.** Each gate measures one thing (one computation per name). A failure is either HARD (**blocks** the verdict) or SOFT (**advisory**) — and one gate may carry *both* a strict SOFT warn-line and a tolerant HARD block-line on the **same** number (e.g. `H_error_within_budget`: SOFT-warns at `0`, HARD-blocks at `0.10`). **Both classes are real quality signals the agent is expected to fix — SOFT means "non-blocking", NOT "ignore".**
- **HARD — blocks `built`; must be fixed to ship.** The milestone's own patched-test quality (`H_patched_completeness`, `H_patched_clean_signal`, `H_none_error_fail_to_pass_present`); the suite-health floors `H_error_within_budget` (persistent-error budget — **HARD threshold `0.10`** on the adjusted-error ratio: blocks only on a genuinely-high error rate / catastrophic collapse, deliberately tolerant of whole-suite TIMING_DAG noise) and `H_total_not_below_base` (collection did not collapse); plus build-run integrity (`B_image_exists` / `B_state_tags_consistent` / `B_tests_ran_successfully` / …) + `B_lint_block_clean`.
- **SOFT — does NOT block `built`, but still matters and should be improved.** The whole-suite distribution views `H_f2f_not_above_base`, `H_pass_to_pass_majority`, `H_s2s_not_above_base`, `H_anomaly_aggregate`, plus `H_error_within_budget` re-checked at the **strict `0`** line (any residual adjusted error → a SOFT warning). They run over the FULL collected suite, so ahead-of-src tests can inflate them — a soft failure is therefore *not automatically* a milestone defect, but where it reflects the milestone's own tests it is a genuine regression to fix. **The gates stay deliberately blunt — they do NOT try to separate TIMING_DAG from real errors; that is M3's job, from the SOFT signal via `diagnose-errors` (below).** Treat SOFT as "fix it unless it is verifiably whole-suite pollution", never as a free pass.

Per-gate detail is at `test_collection_and_result_quality.H_*.{value, threshold, passed, evidence}` (read those; don't carry thresholds in your head). **Going forward** the harness also emits the split explicitly — the failing SOFT gates under `raw_metrics.soft_warnings` (each `{gate, value, threshold, hint}`) plus a hard-only verdict alongside the legacy `built`. **Older `build_status` predates this** and carries only `built` + `failed_gates`; for those, derive the class from the static HARD/SOFT lists above (the lists are the source of truth — the field is only a convenience).

**How to read a `built=false`:**
1. Partition `failed_gates` into HARD vs SOFT by the lists above (or read `raw_metrics.soft_warnings` when present).
2. **HARD failures are ship blockers — fix them.** A patched-scoped gate ⇒ DAG/env work on the milestone's own tests; `H_error_within_budget` ⇒ env repair (run the H_error root-cause triage below to fix real causes, not grind on TIMING_DAG); `H_total_not_below_base` ⇒ recover the collapsed collection.
3. **SOFT failures don't block, but don't wave them through.** Tell real-vs-pollution by comparing **whole vs patched**: the gate's own `value`/`evidence` is the **whole-suite** number; the milestone's **patched-scoped** counterpart lives in `raw_metrics.transitions_patched` (e.g. patched `fail_to_fail` for f2f). `whole >> patched` ⇒ TIMING_DAG pollution → accept (or relocate the ahead-of-src tests via a DAG edit). Otherwise the patched view shows a real regression → fix it like any quality issue; it simply isn't a ship blocker. (`evidence` is whole-suite only — it does NOT carry the patched number; use `transitions_patched` / `diagnose-errors`.)

Caveat: `H_error_within_budget` rests on the `error` outcome, which is **pytest-specific today** (issue #15 — every framework should emit one). On frameworks without `error` it reads ratio 0; don't infer "no env problems" from a passing H_error there.

### H_error root-cause triage — classify before any env fix

`H_error_within_budget` failing with hundreds of `error->error` tests does **not** mean "the env is broken, go fix the env." The per-test message is often a generic bridge placeholder (`"Django runtests.py reported error"`) or a bare `Traceback` header — but the real exception is still in the artifacts (`end.json` / `end_default.json` `tests[].{setup,call,teardown}.{crash.message,longrepr}`; for the pure `django_runtests_bridge` framework it is in the sibling `end_default.json.log`). **No Docker rerun is needed to recover it.**

Run the offline triage over the whole scope first:

```
python3 -m deepcommit.harness.main_orch diagnose-errors {workspace} --all --json {out}
```

It recovers each error's true cause and buckets every milestone (via the workspace taxonomy — see the curation note below):

| Category | Meaning | Action |
|---|---|---|
| `ENV_FIXABLE` | shared env/config root cause (SQLite-FK / migration, missing `INSTALLED_APPS`) | one base-image / settings fix clears it across many milestones — worth a **one-time batch investment** |
| `TIMING_DAG` | tests reference an API absent at this milestone-end (attr/kwarg/import skew, "introduced post-milestone") | **fixing env does nothing** — needs a DAG fix (bring the introducing milestone upstream) or the milestone is a `non_graded` candidate |
| `HARNESS_ARTIFACT` | error injected by the harness itself (e.g. env-patch "not picklable" guard) | not a real defect; candidate for an error-budget exemption |
| `TEST_FAIL` | genuine assertion failure | a real signal — treat as F2F, not env |
| `NEEDS_PROVENANCE` | a substring cannot separate ENV from DAG | verify by provenance before routing; do not trust the guess |

The high-leverage lesson: **only `ENV_FIXABLE` milestones reward a batch env fix.** In practice the bulk of H_error mass is `TIMING_DAG` (the suite collected tests that run ahead of the milestone's own src) — pouring env effort there is wasted motion; those are DAG-repair or non-grade decisions. Triage *before* committing env work.

**The taxonomy is workspace-curated, not fixed.** Buckets come from `_error_taxonomy.yaml` (workspace top-level), so the lens adapts to the repo under analysis. A built-in starter ships as a **non-binding** example: adopt it with `--print-starter` (redirect into the file), start empty, or override any starter rule with a higher-priority one. No file present → the starter applies; `inherit_starter: false` ignores it entirely.

Read the report's self-audit before trusting a verdict: `dominant_fraction` (how concentrated a milestone's dominant cause is), `other_pct` + `top_other_causes` (mass the taxonomy does not recognize), `unused_rules` (a rule shadowed by an earlier one), and `ungrounded_rules` (rules you added without provenance). Low fraction or high OTHER means the lens does not fit — read the raw causes, do not act on the category. The `NEEDS_PROVENANCE` category is the honest landing for a cause a substring cannot resolve to ENV vs DAG.

**Grow it deliberately.** When an OTHER cause recurs and matters, ground it once by provenance (the post-freeze audit rule below) before adding a rule — a rule is a claim, and an ungrounded one auto-mislabels every error it matches (the report lists it under `ungrounded_rules`). M3 is the sole writer (as for `rejected_suggestions.yaml`): a subagent may return a rule candidate; M3 verifies and appends it specific-first (first-match-wins).

### Grading exclusions — `non_graded` / `selected` (post-freeze only)

Two optional lists narrow which milestones the **signal-gate ratios** aggregate over. They are an *evaluation* choice, not a structural one: every milestone stays in the DAG and is still built/run, and the quantity gates still cover the full candidate.

- **`non-graded_milestone_ids.txt`** (blacklist) — still built and run, but excluded from the ratios because it lacks a *correct evaluation environment* or has *no collected evaluation tests* (its signal would mislead — NOT because its quality is merely low). The `diagnose-errors` `TIMING_DAG` / `HARNESS_ARTIFACT` milestones are natural candidates, but verify per-symbol before listing — a `TIMING_DAG` cause can also be a genuinely DAG-fixable missing upstream.
- **`selected_milestone_ids.txt`** (whitelist) — grade ONLY these. Use *sparingly*, for branches / standalone milestones that are simply too poor or too hard to repair and not worth the spend.

**Anti-cheat — exclusions are POST-freeze only.** Dropping weak milestones *before* a freeze to lift a ratio over the bar is gaming. The correct order: freeze the **full** scope first (nothing cherry-picked away), then — in a later, post-freeze analysis round — decide exclusions. the `frozen-check` gate enforces transparency, not a hard block: whenever an exclusion is used it also emits `signal_gates_full_scope` (the same gates with NO exclusions) beside `gates`, so a suspicious gap between graded and full-scope ratios is always visible to an auditor. Record every excluded milestone's reason in the round narrative.

```
python3 -m deepcommit.harness.main_orch frozen-check --workspace {ws} \
  --candidate-prefix <ids> --scope-milestones <ids> \
  --non-graded-milestones-file {ws}/non-graded_milestone_ids.txt
```

### Post-freeze grading audit — how to decide what to exclude

`Grading exclusions` (above) is the *mechanism*; this is the *procedure*. Run it only post-freeze (full scope already frozen), and judge on **evaluation-set health** — can this milestone's own tests be collected and produce signal? — NOT on score or `built`.

**Do not trust your own broad heuristic; it will misfire.** A first-pass macro picture (`hp low` / `f2p=0` / `lint>0` / `waiver-propped`) repeatedly mislabels: lint/waiver flags turn out benign, a "low-quality" milestone is actually a metric blind spot, a "drop it" collapse is actually DAG-fixable. Use a **two-stage subagent flow** — never adjudicate from your own eyeballed picture alone:

1. **Macro stage — subagent + picture, *together*.** Pair a broad read (per-MS `H_patched` / f2p-signal / `diagnose-errors` buckets / whole-vs-patched) with a **macro subagent** that sanity-checks that picture across the whole scope. Output is a short list of *suspect* milestones, **not verdicts**.
2. **Per-MS stage — widen, then verify.** For each suspect (widen the set if the macro stage was uncertain — don't verify only the most-obvious), dispatch a **per-MS subagent** that verifies the eval-set root cause by **provenance**: does the missing/erroring test reference a symbol owned by THIS milestone or a future one? Content-verify with `git show milestone-{MID}-end:<path>`, **never `git merge-base`** (the cherry-pick testbed rewrites SHAs).

Each suspect resolves to one of three, **conservatively — fixable ⇒ fix, don't drop**:
- **`KEEP_GRADED`** — eval set is healthy, or a metric blind spot made it *look* bad while its own patched tests are fine. Most "suspects" land here.
- **`DAG_FIXABLE`** — the tests belong to the milestone but a closure gap / missing upstream blocks them (e.g. a collapse whose crash symbol is owned by an un-prereq'd milestone). Fix the DAG; do **not** exclude.
- **`NON_GRADED`** — genuinely un-evaluable: no eval tests at all (a deletion-only milestone), or its only tests need an environment the harness cannot provide (a native lib / external service it cannot install — a permanent env gate, not a fixable bug). Only these go in `non-graded_milestone_ids.txt`, each with its reason.

The `signal_gates_full_scope` transparency keeps this honest: a graded-vs-full gap is always auditable, so every exclusion must be defensible by eval-set health, never score-driven.

### DAG-closed diagnostics vs freeze eligibility

the `frozen-check` verb may report `warnings` when `scope_milestones` is not DAG-closed; those scope-level numbers are partial diagnostics. A non-DAG-closed `candidate_prefix` is different: the script may still write partial gate values, but the CLI exits `1` and M3 must not proceed to Skill 3 / fork / FREEZE. For actual FREEZE, the candidate **must** be fully DAG-closed before recording in `_frozen_prefix.json`.
