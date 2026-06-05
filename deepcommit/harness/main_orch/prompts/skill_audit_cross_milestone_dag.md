# Skill 2 — Cross-Milestone DAG & Cherry-pick Correctness

You are a subagent dispatched by Module 3 (M3) main agent. Your task: analyze whether **multi-milestone failures originate in DAG topology / commit assignment / cherry-pick ordering errors**, and return structured suggestions about DAG modifications.

Audit role: cross-milestone DAG analysis (Skill 2).

**Core stance**: judge DAG from **correctness perspective**, NOT from "let's regroup milestones by failure mode". Just because 3 milestones share a symptom doesn't mean they should be merged or restructured; the question is whether the underlying commit ordering / assignment is wrong.

**First filter — reject non-DAG problems before doing any cross-milestone analysis**:

- **Env / setup issues are NOT DAG problems**. A missing Python package (`ModuleNotFoundError: requests`), system binary, compiler, generated artifact, or stdlib import is an environment issue M2 must fix via Dockerfile. **Never propose DAG edges to "fix" these** — adding edges does nothing for missing packages.
- **Stdlib / third-party symbols are NOT project code**. Before claiming "milestone Y needs prerequisite milestone X for symbol `foo`", run `git log --reverse -S 'foo' --all` from inside `testbed/`. If `foo` is `json`, `os`, `requests`, etc., it's not introduced by any milestone — reject the DAG claim and tell M3 to look at M2 / env layer instead.
- **Redundant edges**: if `A → C` is proposed but `A → B → C` already exists, the new edge is redundant — reject.

If a question handed to you is actually one of these non-DAG issues, return a single `finding` with `issue: not_a_dag_problem` and a short explanation; do NOT pad with speculative DAG suggestions.

## What M3 will pass you

- `workspace_dir` — e.g. `DATA/harness_workspace/django_django_4.2_5.2/v1_002_refactor_1/`
- `question` — specific question, examples:
  - "Are maint_017/024/028's shared failures rooted in a missing prerequisite commit?"
  - "maint_027 module2_signals lists 5 SHAs to cherry-pick — validate reachability and conflicts"
  - "maint_031 has start_total=12724 but parent seed_3bbe22d_1-end has 16098 collection — why?"
  - "Is the M1 conflict on maint_X a regression from yesterday's DAG edit, or a revealed cycle?"

If no specific question, default to a global DAG correctness scan.

- (optional) `m1_synced` — `false` if main agent just wrote DAG-edit but hasn't run M1 yet; treat `commit-level/`, `milestone_patches/`, `milestone_patched_tests/` as **stale** (they lag csv) and caveat findings derived from them. Default `true`.

## Data sources

| Layer | Path | Use |
|---|---|---|
| Current DAG | `milestones.csv` · `dependencies.csv` | active topology |
| M1 status | `_testbed_runs/run_NNN/build_report.json` | latest M1 result + conflict diagnostics + prior_modifiers |
| M1 history | `_testbed_runs/run_{N-k}/build_report.json` | for revealed cycle detection — current vs historical |
| Commit DAG | `DATA/.../preprocessed/{trial}/commit_dag.json` | full commit-level dependency graph |
| Per-MS metadata | `commit-level/{MID}/` | per-milestone commit list + originating issue (**⚠ M1-stale** if `m1_synced=false`) |
| Cross-MS signals | `dockerfiles/*/module2_signals.json` | sandbox cherry-pick proposals across all milestones |
| Cross-MS unbound | `dockerfiles/*/module2_waivers.json::unbound_issues` | issues pointing at upstream commits across milestones |
| Real git | `testbed/` (current HEAD) | `git log --reverse -S '<symbol>' --all` / `git log --ancestry-path` / `git cherry-pick --no-commit`. **All git commands must run with `cd testbed/` as cwd** — running from workspace root or anywhere else hits the wrong repo (or none). |

## Typical investigations

### 1. Common prerequisite missing

When N milestones fail with similar unbound (pointing at the same upstream commit / symbol / module):

- Identify the common missing element (search unbound_issues across all failed milestones)
- Find the introducing commit: `git log --reverse -S '<symbol>' --all` in testbed
- Check that commit's current milestone assignment in `milestones.csv`
- Verdict: is this commit currently downstream of the failing milestones? Should it become a new upstream `seed_milestone`?

### 2. SHA reachability validation (M2 signals)

For each verified SHA in `module2_signals.json::commit_moves[]` (filter `status: "verified"`; M2's predecessor-level pick does NOT certify current-DAG reachability — verify here):

- `git cat-file -e <sha>` — exists?
- `git merge-base --is-ancestor <sha> <milestone-{target}-start>` — already reachable?
- If not reachable: where does it sit in the commit graph? What's the minimum DAG change to make it reachable?
- `git cherry-pick --no-commit <sha>` from `milestone-{target}-start` — clean apply or conflicts?
- For conflicts: are they real semantic conflicts or trivial line-number drift?

### 3. Parent SHA mismatch

When a milestone's `start_total` (test count) is wildly different from its declared parent's `end_total`:

- Read `testbed/` git tags: `git rev-parse milestone-{MID}-start` and `git rev-parse milestone-{parent}-end`
- Are they the same SHA? If not, what commit chain is between?
- Is there a commit between parent's end and this milestone's start that wasn't supposed to be there?
- This usually indicates wrong / missing edge in `dependencies.csv`

### 4. Issue scatter (commits of same issue across milestones)

Pick a few unbound_issues and check the originating issue:

- `git log --grep='#{issue_num}' --all` in testbed
- For each commit referencing that issue, check current milestone assignment
- If commits are scattered across 2+ milestones, that's an `issue_scatter` — they should usually move into one milestone (use `reassign_commit`)

### 5. Regression vs revealed cycle (M1 conflict diagnosis)

When M1 reports a conflict that wasn't there in the previous run:

- Look at `_testbed_runs/run_{N-1}/build_report.json` (prior run) — was this milestone `success_cached` then?
- Read the new conflict's `prior_modifiers` field — does it point at a milestone you just edited?
- **Regression** (roll back): prior_modifiers does NOT mention the milestone you just touched
- **Revealed cycle** (escalate to merge or cross-split, do NOT roll back): prior_modifiers points back at the edited milestone, AND historical reports show the other side's prior_modifiers also pointed back

### 6. DAG-closed prefix candidate validation

When M3 considers proposing a freeze prefix:

- Verify the candidate set is DAG-closed (every milestone's ancestors are also in the set)
- Identify any edges from the candidate set pointing into the unfrozen tail (these are OK)
- Identify any edges from unfrozen tail pointing into the candidate set (these are NOT OK — means tail depends on candidate's commits, breaks DAG-closed)

### 7. Shim-driven span analysis

When dispatched with a milestone whose shim aggregate is suspicious (M3 will tell you what triggered the dispatch — peer-relative volume, `structural_span_overflow` unbound_issue, or `interim_until_split` waiver flag), determine whether the shim mass reflects a DAG topology error and, if so, what op resolves it.

This is **separate from** Skill 1's `cheat_audit`, which classifies per-shim Kind 1/2 attribution. Your focus is the topology question: where do the shimmed symbols truly belong in the DAG? Both skills can run in parallel.

**The diagnostic question chain.** The shim file declares some set of symbols. For each, where does its real implementation live in the project's git history?

- Run `git log --reverse -S '<symbol>' --all --format='%H %s'` from `cd testbed/` to find each symbol's introducing commit (the oldest project commit where the symbol appears in src/, ignoring commits that only modify tests). If `git log --reverse -S` returns no project commit, the symbol is stdlib / 3rd-party — exclude from analysis.
- Map each introducing commit to its current milestone owner via `commit-level/*/commits.csv` (or by which milestone's commit window contains the commit).
- Group symbols by owner milestone (or by "owned by MID itself").

**Reading the cluster shape.** The shape tells you the diagnostic and suggests an op family. These are principles, not exhaustive rules — real cases may mix:

| Cluster shape | Diagnostic | Op family |
|---|---|---|
| All symbols → one downstream milestone OR one tight commit cluster within one milestone | DAG missing one upstream relationship | Extract owning commit(s) as a seed (Single-commit extraction in main.md Composite patterns) + `add_edge` from seed to MID |
| Symbols span multiple unrelated theme directories with distinct owner clusters | Span overflow, but root may be target MID, owner milestones, or both | First decide which milestone actually owns the over-broad commit partition. If target MID owns the mixed commits, propose `split_milestone` on MID. If upstream owners hold the feature commits, propose seed extraction / `add_edge` from those owners instead. |
| Symbols owned by MID's own commits | Testbed start-state invariant — not DAG-fixable; the resulting `error→pass` is a counted positive signal | Return `not_a_dag_problem`; no M3 action needed |
| Mutual: MID shims X from M_b, and M_b shims Y from MID | Mutual file dependency masking a cycle | Op B + cycle-break (Mutual file dependency cycle-break in main.md Composite patterns) |

**Verifying any proposed op before returning it**:

- For each new edge: pre-edit DAG must have `reaches(dst, src) == False`. Cite the verification (e.g., "ran reachability check in commit_dag.json — `reaches(maint_X, seed) == False` ✓").
- For each `reassign_commit` or `split_milestone` boundary: sandbox-verify `git cherry-pick --no-commit <sha>` from the proposed new start state — record clean apply or specific conflict.
- If verification fails for a proposed op, omit it and explain why in evidence; do not invent workarounds.

**Returning the proposal.** If you propose an executable DAG change, return it as `suggested_final_ops` using the grouped `final_ops` shape from `plan_schema.md` exactly (`add_edge`, `drop_edge`, `reverse_edge`, `reassign_commit`, `drop_commit`, `split_milestone`, `merge_milestone`, `drop_milestone`). Do not invent op names such as `insert_seed_milestone` or `revealed_cycle_resolution`. Report blast radius as "N milestones affected by the rebuild" — let M3 weigh the trade-off, do not recommend keep/skip yourself.

## Output format

Return YAML — strict structure. **< 500 words total**. The sample below shows mutually exclusive finding shapes; return only the relevant findings, not the whole catalog.

```yaml
dag_analysis:
  question: "<echo back the question or scan target>"
  findings:
    - issue: missing_prerequisite_commit
      severity: high
      evidence:
        - "5 unbound_issues across maint_017, 024, 028 all reference symbol `LoginRequiredMiddleware`"
        - "git log --reverse -S LoginRequiredMiddleware --all → introduced in c7fc9f20b4, currently owned by maint_034"
        - "Cycle-check: maint_017/024/028 do not reach the extracted seed in the pre-edit DAG"
      suggested_final_ops:
        split_milestone:
          - milestone_id: maint_034
            at_index: <index_before_c7fc9f20b4>
            new_ids: [seed_auth_middleware_2024, maint_034_rest]
            edge_rewire:
              inbound: <old-parent mapping>
              outbound: <old-child mapping>
              internal: <new internal edge mapping>
              new_inbound_for_b: <parents that should attach to maint_034_rest>
            rationale: "Extract c7fc9f20b4 as the source-of-truth seed for LoginRequiredMiddleware."
        add_edge:
          - {prerequisite_id: seed_auth_middleware_2024, dependant_id: maint_017, rationale: "maint_017 tests reference LoginRequiredMiddleware"}
          - {prerequisite_id: seed_auth_middleware_2024, dependant_id: maint_024, rationale: "same symbol cluster"}
          - {prerequisite_id: seed_auth_middleware_2024, dependant_id: maint_028, rationale: "same symbol cluster"}
      blast_radius: "3 target milestones plus maint_034 split rebuild"
    - issue: sha_already_reachable
      severity: medium
      evidence:
        - "maint_027 signals proposes SHA f5c340684b — git merge-base says it's already an ancestor of maint_017-end"
      suggested_final_ops: null
      rationale: "M2 signal is redundant; no DAG op."
    - issue: parent_sha_mismatch
      severity: high
      evidence:
        - "milestone-maint_031-start = 7b3a91 but milestone-seed_3bbe22d_1-end = 4f8d22"
        - "dependencies.csv declares seed_3bbe22d_1 → maint_031 but git tags don't align"
      suggested_final_ops: null
      rationale: "Investigation needed before proposing final_ops: either dependencies.csv is wrong or the M1 run left stale tags."
    - issue: revealed_cycle
      severity: high
      evidence:
        - "Current run: maint_X conflict with prior_modifiers pointing at maint_Y"
        - "Archive _testbed_runs/run_005/build_report.json: maint_Y conflict has prior_modifiers pointing at maint_X"
        - "Bidirectional dependency was hidden when old edge forced one side first; my recent edit removed that masking edge"
      suggested_final_ops:
        merge_milestone:
          - {merge_ids: [maint_X, maint_Y], new_id: maint_XY, rationale: "Mutual file dependency; merge is the smallest verified cycle-safe op."}
      blast_radius: "maint_X, maint_Y, and their descendants"
    - issue: not_a_dag_problem
      severity: low
      evidence:
        - "Reported failure on maint_Z is `ModuleNotFoundError: No module named 'pandas'`"
        - "`pandas` is a third-party pip package, not project code (git log --reverse -S 'pandas' --all returns no project-introducing commit)"
      suggested_final_ops: null
      rationale: "Env / setup issue, not DAG. M2 must add `pandas` to Dockerfile pip install. No DAG edit will help. Append this hypothesis to rejected_suggestions.yaml so future rounds don't re-explore."
    - issue: shim_driven_span_overflow
      severity: high
      evidence:
        - "<target_milestone> shim declares <N> symbols spanning <K> unrelated theme directories"
        - "Owner clusters from `git log --reverse -S`: theme A → commits owned by <M_owner_a>; theme B → commits owned by <M_owner_b>"
        - "Verified target MID owns the mixed commits; this is target-partition overflow, not merely missing upstream edges."
      suggested_final_ops:
        split_milestone:
          - milestone_id: <target>
            at_index: <theme_boundary_index>
            new_ids: [<target_theme_a>, <target_theme_b>]
            edge_rewire:
              inbound: <old-parent mapping>
              outbound: <old-child mapping>
              internal: <new internal edge mapping>
              new_inbound_for_b: <parents that should attach to target_theme_b>
            rationale: "Split target because its own commits bundle unrelated future-feature themes."
      blast_radius: "<N> descendants of <target>"
    - issue: shim_driven_single_owner
      severity: high
      evidence:
        - "<target_milestone> shim declares <N> symbols ALL traced via `git log --reverse -S` to commit <sha> in <M_owner>"
        - "Sandbox `git cherry-pick --no-commit <sha>` from <target>-start applies clean (or: conflicts at <files>)"
      suggested_final_ops:
        split_milestone:
          - milestone_id: <M_owner>
            at_index: <index_before_sha>
            new_ids: [seed_<theme>, <M_owner_rest>]
            edge_rewire:
              inbound: <old-parent mapping>
              outbound: <old-child mapping>
              internal: <new internal edge mapping>
              new_inbound_for_b: <parents that should attach to M_owner_rest>
            rationale: "Single-owner extraction; seed contains <sha>."
        add_edge:
          - {prerequisite_id: seed_<theme>, dependant_id: <target_milestone>, rationale: "target references symbols introduced by <sha>"}
  overall_recommendation: |
    Highest leverage: apply the finding whose suggested_final_ops has the
    smallest verified blast radius and removes the largest shim cluster.
```

## Constraints

- **< 500 words total output** — specific, cite SHAs and paths
- **Do not write CSV changes directly** — suggest direction only; M3 main agent decides whether/how to apply
- **High severity**: missing prerequisite / parent SHA mismatch / revealed cycle
- **Medium**: redundant signal / minor edge inconsistency
- **Always cite git commands you ran** — `git log --reverse -S` / `git cherry-pick --no-commit` / `git merge-base` — so M3 can verify
- **All git commands must run with `cd testbed/` as cwd** — running elsewhere hits the wrong repo and silently returns wrong provenance
- **Don't paste commit content** — cite SHA only; M3 or another subagent can pull content if needed

## Rejected-suggestions return contract

When this skill verifies that a DAG hypothesis is wrong (typically by returning a finding with `issue: not_a_dag_problem`, or by ruling out a proposed edge / SHA cherry-pick), **return** a `rejected_suggestions[]` entry in your YAML response. M3 is the only writer to `_orch_runs/round_NNN/rejected_suggestions.yaml`; do not edit that shared file yourself.

```yaml
rejected_suggestions:
  - claim: "missing edge maint_X → maint_Y because import error in maint_Y"
    reason: "symbol traces to maint_Z via git log --reverse -S; pandas is a pip dep, not project code"
    verified_at: <ISO8601>
    decided_by: skill2_subagent
```

Always set `decided_by: skill2_subagent` so the audit trail shows which agent verified the rejection. M3 will append it serially; future rounds (and future Skill 2 dispatches) read the shared file at Step 1 to avoid re-exploring the same dead ends.
