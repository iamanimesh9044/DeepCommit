# Phase 3 Module 1 — DAG Optimize + Testbed Prepare

**Module 1 is the testbed-build step — the `build_milestone_testbed.py` script plus this DAG-refinement runbook — owned and driven by M3** (see § How M3 invokes Module 1). The second-person "you" throughout this file addresses the **subagent M3 spawns** when a rebuild needs iterative conflict resolution; when M3 runs the script directly, M3 plays that role itself. The craft: **diagnose cherry-pick conflicts and apply the smallest DAG edit that resolves them while preserving Phase 2's semantic structure**.

You iteratively refine `milestones.csv` and `dependencies.csv` until `build_milestone_testbed.py` creates all per-milestone start/end git tags cleanly. The DAG is not yours to rebuild — Phase 2 inferred a meaningful software-evolution architecture; you fix the local inconsistencies that runtime cherry-pick exposes.

**Spec references**:

- `phase3_module3/plan_schema.md` — authoritative dispatch contract + op vocabulary

---

## How M3 invokes Module 1

Module 1 is **not an autonomous agent** — M3 is the only caller, and M3 picks one of two paths per rebuild
(there is **no human-invoked / standalone mode**):

| Path | When M3 picks it | Authority | Return |
| --- | --- | --- | --- |
| **Direct run** (no subagent) | A rebuild with no conflict resolution expected — Round 0 baseline, a rebuild after a clean DAG edit, a cache hit | M3 reads `build_report.json` itself; if conflicts surface that need the laddered loop, M3 escalates to the subagent path or composes a `dag_update` | `build_report.json` (M3 reads the summary directly) |
| **Subagent** (M3 spawns one, pointed at this file) | The rebuild surfaces conflicts needing iterative Decision-Ladder resolution (provenance digging, several iterations) — keeps the deep-dive out of M3's context | L0 / L0.5 / L1 (edge ops) autonomous; L2–L5 (milestone ops) must **escalate** to M3 | 5 required fields + optional `applied_refinements` YAML (see § Subagent return contract) |

The authority partition reflects **decision level**, not just which CSV changes:

- **M3 `final_ops`** — M3's planned DAG edit for the event (verified edge ops + any milestone/composite ops).
- **subagent `applied_refinements`** — extra L0/L0.5/L1 edge ops the subagent discovers after M3's plan is applied and a rebuild exposes new runtime evidence. Additional, non-duplicate, non-contradictory corrections only.
- **Milestone/composite ops** (`reassign_commit` / `drop_commit` / `split_milestone` / `merge_milestone` / `drop_milestone`) — subgraph-level semantic decisions; **M3 adjudicates** against `rejected_suggestions` + freeze prefix + cross-round context.

As the dispatched subagent, if your Decision Ladder analysis lands at L2–L5: **do not apply**. Set `final_status: escalated`, propose the op (full reasoning + cycle-check + cited evidence) in `analysis_report_run_N.md`, return the structured YAML. M3 reads your proposal and composes a new `dag_update` event next iteration. L5 acceptable-drop also escalates unless the M3 brief explicitly grants `allow_acceptable_drop: true`.

---

## Operating posture

- **Refinement, not reconstruction** — Phase 2's DAG is a hypothesis stress-tested against runtime; you fix local breaks, you don't rebuild. Every edit preserves:
  - **Coherence** — each milestone is a functionally/semantically cohesive unit. A "Utilities" milestone shouldn't accumulate ORM work.
  - **Balance** — milestone sizes stay roughly uniform (commits and LoC). Don't pile split/merge on one node.
  - **Coverage** — no commit silently disappears. Coverage should stay ~100% in normal iterations; controlled drops are acceptable only at the end (see § Acceptable-drop exit).
- **Runtime evidence beats Phase 2 confidence** — `prior_modifiers` in `build_report.json` is **ground truth**; `dependencies.csv` confidence scores (Strong / Weak) are **claims**. A Weak edge confirmed by runtime stays; a Strong edge contradicted by runtime flips. Verify before acting.
- **Smallest action that resolves** — climb the Decision Ladder only when smaller ops fail. Semantics can override minimality (same-issue scatter → L2 reassign even if L1 add_edge would technically work).
- **Trust the archive** — `_testbed_runs/run_NNN/` auto-archives every build (build_report + input CSV snapshots). Never `cp` backups manually — silent divergence from the canonical archive corrupts the regression-vs-revealed-cycle check across runs.

**Iteration cost**: `build_milestone_testbed.py` has two caches — a persistent topo-sort cache (per-milestone, keyed by commit-set hash) and a session-scoped main-branch topo index. Warm iterations (most milestones unchanged) finish in seconds; cold rebuilds run somewhat longer. Don't hesitate to rebuild frequently — small tight loops are cheaper than long planning.

---

## Common failure modes (do not commit these)

- **L5 (drop) before exhausting L0–L4** — drop is a clean exit, not a debugging shortcut. Coverage loss > ~10% means structural failure, not exit.
- **Trust Phase 2 confidence over runtime evidence** — a Strong 0.9 edge contradicted by `prior_modifiers` is still wrong. Reverse or drop it.
- **Skip the partition gate** — `validate_dag` catches cycles in `dependencies.csv` but misses cycles induced by milestone grouping; `validate_milestone_partitioning` catches the latter. Run both before every rebuild.
- **Apply L2+ ops in dispatch mode** — milestone-level ops in dispatch mode must **escalate**, not apply. Apply only edge ops autonomously; record L2+ proposals for M3 to adjudicate.
- **Run git commands outside `cd testbed/`** — silently returns wrong provenance (see § Git cwd invariant).
- **Manual `cp` backups** — auto-archive is canonical; manual copies silently diverge from the archive that downstream sanity checks consume.

---

## Subagent return contract (when M3 spawns a subagent)

When M3 spawns you as a subagent, run the inner refinement loop autonomously within authority bounds, then return a minimal structured summary. Do **not** narrate the loop transcript — write loop progress to `analysis_report_run_N.md` and emit only the YAML below.

### Return YAML (5 required fields + optional `applied_refinements`)

```yaml
final_status: success | success_cached | partial | escalated
# success           = build_report shows conflict=0 AND skipped_due_to_parent=0
# success_cached    = no work needed (DAG already clean from cache)
# partial           = acceptable-drop exit taken only when M3 explicitly authorized it
# escalated         = hit L2+ need OR structural bedrock; M3 must intervene

milestones_success_count: <int>
milestones_conflict_count: <int>
milestones_skipped_count: <int>

run_id: run_NNN
# M3 derives the build_report path as _testbed_runs/{run_id}/build_report.json

applied_refinements:                # OPTIONAL — omit entire field if empty
  add_edge:                         # L1 ops you autonomously applied
    - prerequisite_id: <MID>
      dependant_id:    <MID>
      rationale:       <≤60 chars>
  drop_edge:                        # L0 ops
    - prerequisite_id: <MID>
      dependant_id:    <MID>
      rationale:       <≤60 chars>
  reverse_edge:                     # L0.5 ops
    - prerequisite_id: <MID>         # old/current direction; new direction is the swap
      dependant_id:    <MID>
      rationale:       <≤60 chars>
```

### Where each kind of detail lives


| Channel                                                       | Content                                                                                                                                                 |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Return YAML**                                               | The 5 required fields + optional `applied_refinements` (M3 transcribes into event ledger)                                                               |
| `**analysis_report_run_N.md`** (workspace root, M1 writes)    | Per-iteration narrative, Decision Ladder reasoning, **L2+ escalation proposals**, rejected hypotheses, hard-case-pattern detection. M3 reads on demand. |
| `**_testbed_runs/{run_id}/build_report.json`** (auto-written) | Build outcome per milestone, conflict diagnostics, `prior_modifiers`. M3 reads when it wants depth.                                                     |


### Constraints

- **Total YAML output ≤ 200 words**
- Do NOT emit full conflict diagnostics — they're in `build_report.json`
- Do NOT emit per-iteration narrative — that's `analysis_report_run_N.md`
- Do NOT include L2+ ops in `applied_refinements` — those are escalate signals, not apply
- Before applying an edge refinement, check it is not a duplicate/reversal of an M3-applied edge op and does not touch edge rewiring from an M3 composite op; if it collides, escalate instead of silently rewriting M3's plan
- `applied_refinements` uses the same grouped schema as `final_ops`; `reverse_edge` records the old/current direction, and the new direction is implied by swapping prerequisite/dependant
- `partial` is legal in dispatch mode only when the M3 brief explicitly says `allow_acceptable_drop: true`; otherwise an acceptable-drop candidate returns `escalated`
- `escalated` may still carry non-empty `applied_refinements` (edge ops applied before hitting the L2+ blocker — record them)
- `success_cached` always has `applied_refinements` omitted (no work means no refinements)

### Example A — clean apply + 3 refinements

```yaml
final_status: success
milestones_success_count: 12
milestones_conflict_count: 0
milestones_skipped_count: 0
run_id: run_018
applied_refinements:
  add_edge:
    - {prerequisite_id: <MID_M>, dependant_id: <MID_Y>,
       rationale: "prior_modifiers point M→Y; Phase 2 missed"}
  drop_edge:
    - {prerequisite_id: <MID_X>, dependant_id: <MID_Y>,
       rationale: "no runtime evidence; spurious Phase 2 inference"}
  reverse_edge:
    - prerequisite_id: <MID_A>
      dependant_id: <MID_B>
      rationale: "B in A's prior_modifiers; direction reversed"
```

### Example B — escalated (L4 split needed)

```yaml
final_status: escalated
milestones_success_count: 10
milestones_conflict_count: 1
milestones_skipped_count: 1
run_id: run_019
applied_refinements:                # what M1 managed before escalating
  drop_edge:
    - {prerequisite_id: <MID_X>, dependant_id: <MID_Y>,
       rationale: "no runtime evidence"}
# L4a split proposal for <MID_Z> with full reasoning + edge_rewire
# is in analysis_report_run_19.md
```

M3 reads `analysis_report_run_19.md` for the L4 proposal, then composes a new `dag_update` event with `split_milestone` in `final_ops`.

---

## Git cwd invariant

Every git command in this skill runs with **`cd testbed/` as cwd**. This includes `git show --stat`, `git log -G`, `git log --reverse -S`, `git show -w --stat`, `git cherry-pick --no-commit`, `git merge-base`, and any ad-hoc `git log` usage.

Running elsewhere hits the host repo (or no repo) and **silently returns wrong provenance** — reformat commits report wrong file counts, framework-bridge searches miss the introducing commit, prior-modifier sanity checks confirm or reject the wrong claim. There is no error when you violate it; the wrong findings propagate into wrong Decision Ladder choices.

This invariant applies to **git provenance commands only**. Build/validation commands run from a cwd where the DeepCommit script path resolves (usually the DeepCommit repo root, or a workspace-local copied harness), **not** from `testbed/`.

---

## Inputs (in workspace)

- `milestones.csv`, `dependencies.csv` — editable (subject to dispatch-mode authority)
- `config/{repo_range}.yaml` — read-only
- `DATA/.../preprocessed/{repo_range}/commit_dag.json` — read-only commit DAG

---

## The Loop

Repeat until `build_report.json` shows `conflict=0` AND `skipped_due_to_parent=0` (or escalation conditions trigger):

1. **Build** → `build_report.json` (conflict diagnostics)
  - Each run auto-archives to `_testbed_runs/run_NNN/` (build_report + input CSV snapshots)
  - **Cross-run signal**: in mutual-dependency cases, only one side shows `conflict` per run; the other side's `failing_commit` / `commit_index_in_milestone` lives only in an older archive at `_testbed_runs/run_{N-k}/build_report.json`. Check older archives when diagnosing mutual deps.
2. **Diagnose** — read each conflict entry AND cross-check existing DAG edges (§ Reading existing DAG edges)
3. **Plan** — classify via Decision Ladder; write `analysis_report_run_N.md`
4. **Apply** — edit CSVs directly within authority bounds (dispatch mode: edge ops only)
5. **Verify** — hard gates (`validate_dag` + `validate_milestone_partitioning`) + regression-vs-revealed-cycle check (below)
6. **Rebuild** → loop

Fix **root-cause** conflicts first — those blocking many `skipped_due_to_parent` milestones.

### Regression vs revealed cycle

After an edit, a previously-`success_cached` milestone turning `conflict` looks like regression but may be a **revealed mutual dependency** that the old (wrong) edge was masking. Distinguish before rolling back:


| Signal                                                                                                                                            | Verdict            | Action                                                                                     |
| ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------ | ------------------------------------------------------------------------------------------ |
| New conflict's `prior_modifiers` does NOT mention either MS in your recent edit                                                                   | **Regression**     | Roll back the edit                                                                         |
| New conflict's `prior_modifiers` points at the OTHER side of your edit, AND archived report shows the other side's `prior_modifiers` pointed back | **Revealed cycle** | Do NOT roll back — escalate to L3 merge or L4b cross-split (dispatch mode: escalate to M3) |


Quick check: `jq '.milestones["<new_conflict_id>"].prior_modifiers' build_report.json` — if any entry's `milestone_id` matches the other side of your last edit, treat as revealed cycle.

---

## Build command

```bash
PYTHONPATH=. python -m deepcommit.harness.prepare_repo.build_milestone_testbed \
    --trial {TRIAL} --continue-on-error --incremental --verbose
```

Exit code 2 + `build_report.json` written is the normal "conflicts present" state.

Run this from the DeepCommit repo root. If the harness was copied into the workspace, the equivalent workspace-local `python harness/prepare_repo/build_milestone_testbed.py ...` form is also valid. Do **not** prefix this build command with `cd testbed/`; reserve `cd testbed/` for git commands.

In dispatch mode, the M3 brief identifies `{TRIAL}` via the workspace path or as an explicit argument; if neither is given, derive from `{workspace}/config/`. If `--trial` is ambiguous, use explicit paths (`--milestone-csv`, `--dependencies-csv`, `--config`, `--repo-path`, `--output-dir`, `--base-tag`) instead of guessing.

---

## Reading a conflict entry

For each `"status": "conflict"` in `build_report.json.milestones[mid]`:


| Field                                                      | What it tells you                                                                                                                                         |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `failing_commit` / `failing_commit_subject`                | Which commit broke, what it was about                                                                                                                     |
| `failing_files_src` / `rej_hunks[*].preview`               | The actual conflicting code                                                                                                                               |
| `commit_index_in_milestone` / `total_commits_in_milestone` | Position (1/6 = breaks immediately; 26/42 = midway)                                                                                                       |
| `prior_applied_commits`                                    | Commits from THIS milestone applied before failure (empty = pure external; many = internal ordering)                                                      |
| `**prior_modifiers[file]`**                                | **Prior commits that touched the conflicting file, annotated with owning `milestone_id`. Primary signal for add_edge / reverse_edge / merge / reassign.** |


---

## Reading existing DAG edges (before adding new ones)

**Phase 2 edges are claims; `prior_modifiers` is evidence.** Cross-check every existing edge involving a conflict milestone against runtime.

For each existing edge `A → B`:


| Runtime signal                         | Verdict                                                                                   |
| -------------------------------------- | ----------------------------------------------------------------------------------------- |
| A appears in B's `prior_modifiers`     | **Keep** — correct (even at Weak confidence)                                              |
| B appears in A's `prior_modifiers`     | **L0.5 reverse** — flip to `B → A` (regardless of Phase 2 confidence)                     |
| Neither direction appears, Weak conf   | **L0 drop** — likely spurious Phase 2 inference                                           |
| Neither direction appears, Strong conf | **Keep** but flag — no contradiction yet, may be a Phase 2 inference about future commits |


---

## Decision Ladder

Use the least invasive action that works; respect semantics over minimality. **Fix existing edges first** (L0 / L0.5), then add new ones (L1+). Fixing wrong existing edges often resolves multiple conflicts at once.

> **Edge convention**: `A → B` means "apply A before B" = "B depends on A". In `dependencies.csv`: `prerequisite_id=A, dependant_id=B`. A `prior_modifier` milestone is always `A` (prerequisite); the failing milestone is `B` (dependant). Legacy CSVs may use `source_id` / `target_id` headers — readers accept both (same direction).

### Summary table


| L       | Action                                       | Trigger                                                       | Authority (dispatch)    | Cost      |
| ------- | -------------------------------------------- | ------------------------------------------------------------- | ----------------------- | --------- |
| **0**   | `drop_edge`                                  | No `prior_modifiers` evidence in either direction + Weak conf | ✅ edge — auto-apply     | minimal   |
| **0.5** | `reverse_edge`                               | Direction reversed per `prior_modifiers`                      | ✅ edge — auto-apply     | minimal   |
| **1**   | `add_edge`                                   | `prior_modifiers` points to A, A not already an ancestor      | ✅ edge — auto-apply     | low       |
| **2**   | `reassign_commit`                            | Commit semantically belongs in another milestone              | 🛑 milestone — escalate | low       |
| **3**   | `merge_milestone`                            | Mutual `prior_modifiers` / heavy file overlap                 | 🛑 milestone — escalate | medium    |
| **4a**  | `split_milestone` @ `prior_applied` boundary | Internal phase boundary                                       | 🛑 milestone — escalate | high      |
| **4b**  | cross-split (`split_milestone` × 2)          | True mutual fail at specific indices on both sides            | 🛑 milestone — escalate | very high |
| **5**   | `drop_milestone` / `drop_commit`             | Reformat blocker / unsatisfiable / clean exit                 | 🛑 milestone — escalate | exit      |


### Per-level detail

**L0 — `drop_edge`** [edge, dispatch-auto]: Existing edge has no runtime evidence in either direction AND is Weak confidence. Remove it; was Phase 2 speculation.

**L0.5 — `reverse_edge`** [edge, dispatch-auto]: Existing edge but B appears in A's `prior_modifiers` (runtime says B-before-A). Flip to `B → A`. Preserves the Phase 2 insight that A and B are related; just fixes direction. Applies regardless of Phase 2 confidence.

**L1 — `add_edge`** [edge, dispatch-auto]: No existing edge; `prior_modifiers` points to A; A not already an ancestor of B. For multiple prior_modifier milestones, drop transitive ancestors (check with `dag_analyzer`). **Before applying, cycle-check**: ensure the new edge doesn't make B reach A through any path.

**L2 — `reassign_commit`** [milestone, escalate in dispatch]: Commit c in A is about the same work as B (shared issue # in subject, fits B's theme). Preserves both themes. Semantic decision.

**L3 — `merge_milestone`** [milestone, escalate in dispatch]: A and B are one work unit (multiple same-issue commits, heavy file overlap, or mutual `prior_modifiers`). Use when reassign would empty one milestone, or when A and B appear in each other's `prior_modifiers`.

**L4a — `split_milestone` @ prior_applied boundary** [milestone, escalate in dispatch]: Most common L4. Split at the `prior_applied` boundary — `_a` = `prior_applied_commits`, `_b` = failing commit + remainder. See § L4a split procedure below for full mechanics.

**L4b — cross-split** [milestone, escalate in dispatch]: Rare. For true mutual dependency where both sides fail at specific indices. M1 and M2 appear in each other's `prior_modifiers` (possibly across archived runs). Split each at its conflict index and interleave: `M1_a (1..k1-1) → M2_a (1..k2-1) → M1_b (k1..N1) → M2_b (k2..N2)`. **Prefer L4a + L2 bridge-reassign over L4b** — L4b needs both indices and is easy to get wrong.

**L5 — `drop_milestone` / `drop_commit`** [milestone, escalate in dispatch]: Either (a) mid-iteration for a single commit whose dependencies can't be satisfied without cycles (typically a cross-cutting reformat dependent — see § Hard-case patterns), or (b) at the end as a clean exit. Full procedure: § Acceptable-drop exit.

**Semantic signal > minimality**: failing commit's subject and prior_modifier subjects sharing an issue # → prefer L2/L3 over L1.

### L4a split procedure (detailed)

Triggered when the missing prerequisite lives in a milestone you can't fit as M's parent without a cycle, OR `prior_applied_commits` points at M's own commits.

1. Read `build_report.json.milestones[M].prior_applied_commits` → set `P`
2. `M_a.commits = [c for c in M.commits if c in P]` (preserves topo order)
3. `M_b.commits = [c for c in M.commits if c not in P]` (starts with the failing commit)
4. In `milestones.csv`: delete `M`'s row, insert `M_a` and `M_b` rows. Recompute `loc` / `src_loc` / `additions` / `deletions` / `touched_`* via `CommitAnalyzer`.
5. In `dependencies.csv`:
  - Retarget all `X → M` to `X → M_a`
  - Retarget all `M → Y` to `M_a → Y` (downstream sees all of M_a's content; M_b flows through internal chain)
  - Add `M_a → M_b` (FUNC Strong 0.95, rationale `[split] internal`)
  - Add inbound edges for `M_b` pointing at milestones owning its missing `prior_modifiers` (the reason for the split)
6. Validate **both** gates (`validate_dag` + `validate_milestone_partitioning` — the latter catches inferred cycles)
7. Rebuild

**Edge cases**: after a later `M_b` drop, also rename `M_a → M` (strip `" (Part 1: ...)"` from title). After a rename, `--incremental` rebuild fails ("CSV changed but tags exist", `parents_hash` shifts); use `--force-rebuild` — the fast-path topo sort keeps cold rebuild cheap.

---

## Hard-case patterns

Before iterating, scan for these patterns — they change the strategy.

### Cross-cutting reformat (auto-formatter / mass rename / repo-wide refactor)

These commits touch hundreds of files; patch-context depends on whole-repo state at their timestamp → unsatisfiable on any partial DAG.

- **Detect**: `git show --stat <sha>` reports >50 files changed, AND `git show -w --stat <sha>` is much smaller (`-w` ignores whitespace; if it shrinks dramatically, the commit is mostly formatting)
- **Consequence**: containing milestone is a *cluster magnet* — commits after it depend on it, commits before it depend on its absence
- **Default action**: usually forces **L5 drop** of the containing milestone (escalate in dispatch)

### Framework-bridge commits

Bug-fix-flavored subjects that actually introduce a new framework / abstraction (subject says "Fixed #X" but body says "This also introduces...").

- **Detect**: `git log --all -G'<new_identifier>' --diff-filter=A` finds the commit that added a class/method/attribute; then search forward for its consumers
- **Consequence**: bridge commit's natural home is the milestone containing its consumers, not where Phase 2 placed it
- **Default action**: **L2 reassign** to the consumer milestone (escalate in dispatch)

### Same-issue scatter

Multiple commits sharing an issue # or shared subject prefix across different milestones.

- **Detect**: cross-reference issue # in commit subjects across `milestones.csv` rows
- **Consequence**: Phase 2 split work that belongs together
- **Default action**: **L2 reassign-to-same-milestone**. Semantic signal dominates. (Escalate in dispatch.)

### Whack-a-mole

Fixing one conflict immediately surfaces a deeper one in the same DAG region.

- **Detect**: 3 consecutive iterations each reveal a new conflict in the same tightly-coupled module / file cluster; each iteration produces a 1-commit gain but creates a new conflict — net zero
- **Consequence**: structural bedrock — usually a cross-cutting reformat or framework-bridge commit whose dependencies genuinely span > 2 milestones
- **Default action**: **L3 merge** the whole cluster, OR **L5 drop** the root obstacle (escalate in dispatch)

---

## Verification tools

All tools invoke via Python with `print(json.dumps(<fn>(...), indent=2))` for inspection.

### Hard gates — MUST pass before every rebuild

```python
from deepcommit.tools.dag_analysis.dag_analyzer import validate_dag
validate_dag('{DEPS_CSV}', save_results=False)

from deepcommit.tools.milestone_analysis.validate_partitioning import validate_milestone_partitioning
validate_milestone_partitioning('{MILESTONES_CSV}', '{COMMIT_DAG_JSON}')
```

`validate_dag` catches cycles in `dependencies.csv`. `validate_milestone_partitioning` catches cycles induced by milestone grouping. **Both must pass.**

### Targeted diagnostics (when planning an action)

```bash
# Candidate edge / dependencies inference from commit DAG
python -m deepcommit.tools.conflict_analysis.milestone_dependency_analyzer \
    --commit-dag {COMMIT_DAG_JSON} --milestones {MILESTONES_CSV} \
    --dependencies {DEPS_CSV} --check M1 M2

# Milestone internal frontier completeness
python -m deepcommit.tools.conflict_analysis.milestone_internal_conflict_analyzer \
    --repo {REPO_PATH} --milestones {MILESTONES_CSV} \
    --dag {COMMIT_DAG_JSON} --dependencies {DEPS_CSV}
```

### Soft monitors (each iteration; flag if trending badly)

```python
from deepcommit.tools.milestone_analysis.milestone_statistics import (
    analyze_commit_coverage,
    analyze_milestone_size_balance,
)
analyze_commit_coverage('{MILESTONES_CSV}', main_commits_file='{MAIN_COMMITS_JSONL}')  # should stay ~100%
analyze_milestone_size_balance('{MILESTONES_CSV}', repo_path='{REPO_PATH}')           # watch CV of commits / LoC
```

Plain `git` is fine for ad-hoc checks (`git show --stat`, `git log --follow`, etc.) — always under `cd testbed/`.

---

## When to stop

### Success target

- `build_report.json`: `conflict=0 AND skipped_due_to_parent=0`, 100% commit coverage retained
- `validate_dag.is_valid_dag=true`, `validate_milestone_partitioning` cycle-free

### Stop matrix — trigger → action


| Trigger                                                                                 | Action                                                                         |
| --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Coverage drops below ~100% mid-iteration                                                | Inspect recent edits; commits were silently dropped — **rollback last edit**   |
| Size CV (commits / LoC) grows markedly vs. initial run                                  | Refinement became destructive restructuring — **rollback last merge/split**    |
| Same milestone appears in `prior_modifiers` across 3+ new conflicts after an edit       | That edit likely broke something — **revert it**                               |
| 3 iterations without conflict-count reduction                                           | **Escalate one ladder level** on the most-cited milestone in `prior_modifiers` |
| Whack-a-mole / cross-cutting reformat / sustained progress < 1 milestone per 3 rebuilds | **Acceptable-drop exit** (below); in dispatch mode → escalate                  |


### Acceptable-drop exit

Fall back to a clean partial build when further iteration stops paying off. Criteria for the fallback to count as acceptable:

- Commit coverage loss ≤ ~10% of total commits
- Dropped milestones are concentrated (a single obstacle cluster), not scattered
- All remaining milestones `success` or `success_cached`, zero conflicts, zero skipped

**Dispatch rule**: acceptable-drop is a proposal by default. In normal dispatch mode, write the drop set and rationale to `analysis_report_run_N.md` and return `final_status: escalated`. Only perform the CSV edits below and return `partial` when the M3 brief explicitly includes `allow_acceptable_drop: true`.

**Procedure** (only when the M3 brief grants `allow_acceptable_drop: true`):

1. Identify the set to drop: the conflicting milestone(s) + every `skipped_due_to_parent` transitively blocked
2. Edit `milestones.csv` — remove those rows
3. Edit `dependencies.csv` — remove every row where `prerequisite_id` or `dependant_id` is in the drop set; also remove self-loops (`M_a → M_b` where `_b` dropped)
4. If any surviving milestone was named `M_a` with a dropped `M_b` sibling, rename `M_a → M` and strip `" (Part 1: ...)"` from title
5. Validate both gates
6. `--force-rebuild` (incremental fails after ID renames — `parents_hash` shifts)
7. Expect `success: N` with `N = total - dropped`; no `conflict` or `skipped`

Record dropped milestones + count of dropped commits in `analysis_report_run_N.md` so Phase 4 (downstream) knows what's missing.

---

## Reference: worked examples

Illustrative — consult when classifying a conflict; not iteration-by-iteration reference.

### Example 1 — same-issue scatter → L2 over L1

A milestone `<MID_F>` has a conflict at commit 1/6, `prior_applied=[]`, all `prior_modifiers` in a single other milestone `<MID_O>`. L1 (`add_edge <MID_O> → <MID_F>`) would resolve it mechanically.

**But** the failing commit's subject is `Refs #<N>` and the prior modifier subjects include `Fixed #<N>` — **same issue #**. Semantic signal beats minimality → **L2 reassign** the failing commit from `<MID_F>` to `<MID_O>`.

In dispatch mode: escalate; M3 composes a `reassign_commit` op.

### Example 2 — mutual dependency → look for bridge commit (avoid L4b)

Two milestones each appear in the other's `prior_modifiers`, but for different files:


| Failing                  | Conflict file | `prior_modifiers` includes |
| ------------------------ | ------------- | -------------------------- |
| `<MID_A>` @ commit k/N   | `<file_X>`    | `<MID_B>`                  |
| `<MID_B>` @ commit k'/N' | `<file_Y>`    | `<MID_A>`                  |


Neither pure `add_edge` nor `reverse_edge` works. **Before jumping to L4b cross-split, look for a single bridge commit**: most mutual dependencies reduce to one misclassified commit.

Investigate the failing commit in `<MID_A>`: is it about a topic that semantically belongs in `<MID_B>` (shared issue # or fits B's theme, while only secondarily touching A's files)? If yes — **L2 reassign that bridge commit**; the mutual dependency dissolves with one move.

Only if no single bridge commit accounts for the mutual crossing, escalate to L4a or L3. L4b cross-split is almost never the right call — it requires both sides' indices and is easy to get wrong.

In dispatch mode: escalate; M3 composes the `reassign_commit` op.