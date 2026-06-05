# Phase 3 Module 3 — Freeze Ceremony (reference)

The **single home for the freeze decision ritual** — the M3-side flow (trigger → script → Skill 3 → fork →
apply) **and** the dispatched audit subagent's own prompt (§ Skill 3, below). Read this before any
`freeze_check`. (Merged from the old `freeze_check` runbook, the "Freeze decision rules" heuristic, and the
former standalone Skill-3 audit file.)

**Cross-references:** bare `§Name` / "see §…" point back to **`main.md`** (e.g. §Orchestration-support verbs
for the `frozen-check` / `freeze-apply` / `unfreeze` flags; §Step 1 for the freeze advisory). Post-freeze
grading exclusions live in `reference_operational_patterns.md`.

---

## The three-component flow (script → Skill 3 → fork)

```
Trigger conditions (all must hold):
  k ≥ 3
  AND sub-loop converged this round
  AND freeze advisory non-empty

Step 3 · Execute (three components, in order)

  1. SCRIPT (objective, waiver-exclusive)
     python3 -m deepcommit.harness.main_orch frozen-check \
         --workspace {workspace} \
         --scope-milestones <scope_MIDs> \
         --candidate-prefix <candidate_MIDs> \
         --output _orch_runs/round_NNN/frozen_check.json

     Exit codes:
       0 → all gates pass        → continue to Skill 3
       2 → some gates fail       → record event with fork_decision=SHOULD_NOT;
                                   plan an M2 redo targeting the failing gate's root cause next round
       1 → input invalid         → candidate is not DAG-closed, or another required input is invalid;
                                   fix before Skill 3 / fork. A non-closed scope may appear as
                                   a warning in the report, but is not itself a freeze candidate.

  2. SKILL 3 (subjective latent risk audit)
     Dispatch Skill 3 with script report + per-MS unbound/signals/attempt history.
     Skill 3 hunts 6 latent-risk categories (defined canonically in § Skill 3, below).
     Returns recommendation: FREEZE / WAIT / SHOULD_NOT.

  3. FORK (decision under full context)
     Fork main agent with both reports as inputs.
     Forked agent has your full mental model + the two structured reports as second opinions.
     Returns ≤ 200-word decision summary: FREEZE / WAIT / SHOULD_NOT + reasoning.

Record the full freeze_check event with all three outputs.
If decision = FREEZE → apply atomically with freeze-apply (§verbs) — never hand-edit _frozen_prefix.json.
  It writes _frozen_prefix.json (atomic + .bak), pins each frozen MID's recipe checkpoint, and re-checks
  invariants (DAG-closed; ⊆ scope; new set top-down) — exit 2 = an invariant failed, nothing written.
```

## Freeze decision rules

**Default rule — three-way alignment.** No single component can freeze on its own. If the script alone could freeze, M2's waivers would self-certify. If Skill 3 alone could freeze, the latent-risk judgment would lack the script's objectivity. If the main agent alone could freeze, the cross-round-trend bias would go unchecked. The three-way default exists so each component checks the others.

**Exception — fork may override Skill 3's *procedural-deferral* WAIT.** Real-world freeze attempts repeatedly hit a pattern: Script PASS + Skill 3 WAIT + fork FREEZE, where Skill 3's WAIT is purely a deferral to the **prior round's fork-WAIT conditions** (operator-side workstreams that haven't progressed), NOT a new latent-risk finding. Strict alignment on identical evidence loops indefinitely. To break the loop, the fork MAY override Skill 3's WAIT and proceed with FREEZE when ALL of the following hold:

1. Skill 3's `latent_risks[]` contains zero entries of severity ≥ medium that are **NEW vs the prior round** — the WAIT is procedural deferral only, not fresh technical risk.
2. The fork's FREEZE rationale **explicitly addresses** the procedural-deferral conditions Skill 3 cites (e.g., "operator-side workstreams are parallel to FREEZE, not gated by it"; or "the WAIT condition is structurally upstream of M3 — waiting does not advance it").
3. The fork's own confidence is **≥ 0.70**.
4. The event records the override explicitly:
   ```yaml
   m3_alignment_check:
     fork_override_skill3: true
     override_rationale: "<one sentence — why the procedural WAIT does not block>"
   ```

The fork must NOT override Skill 3 when Skill 3 cites **new** latent risk vs prior round, or when fork confidence < 0.70 — those signal genuine uncertainty that strict alignment is the right hedge against.

**Why the exception is safe — freezing is reversible, encouraging freezing is cheap.** FREEZE locks DAG topology and recipe files for the frozen milestones; it does **NOT** lock metric values. If the operator later fixes a harness bug / extractor / data-quality issue that lifts per-MS H_patched values, those lifts flow through to the aggregate automatically — the frozen prefix only looks better, not worse. And if a frozen milestone *truly* needs DAG/recipe surgery later, **unfreezing is supported** via `python -m deepcommit.harness.main_orch unfreeze --workspace {workspace} --round {NNN} --remove {ids} --reasoning "{why}"`, which removes the MIDs, appends the `UNFREEZE` history entry, and enforces that the **remaining** frozen set stays DAG-closed (exit `2` if removal would orphan a frozen descendant — unfreeze its descendants too); those MIDs re-enter the mutable pool. The cost asymmetry — FREEZE-too-early = nearly-free to undo, FREEZE-too-late = round after round of stagnation while waiting for operator workstreams M3 can't drive — justifies a **bias toward freezing** when the script PASSES and the fork sees clean evidence. Re-running freeze in the next round on identical evidence is not a "safety check" but a "stuck loop".

---

## Skill 3 — Frozen Latent Risk Assessment · dispatched-subagent prompt

> **Dispatch note (for M3):** component 2 of the flow above = spawn a fresh subagent and point it at this
> section (`reference_freeze_ceremony.md` § Skill 3). Everything below is written **for that subagent**
> (zero-context, second person), not for you. Pass it `candidate_prefix`, the `frozen-check` report,
> `round_k`, `workspace_dir`, and optional `prior_freeze_attempts`.


You are a subagent dispatched by Module 3 (M3) main agent during a `freeze_check` event (§ The three-component flow, above in this file). Your task: given an objective gate report from the `frozen-check` verb plus per-milestone context, assess whether the candidate DAG-closed prefix has **latent risks** that would make freezing now regrettable. Recommend `FREEZE` / `WAIT` / `SHOULD_NOT`.

Audit role: frozen-prefix latent-risk assessment (Skill 3).

**Your job is not to recompute gates** — the script already did that. Your job is to look at signals the script can't quantify: cluster residue, attempt-evolution warning signs, unbound issues that may explode later, suspicious bordering milestones.

### What M3 will pass you

- `candidate_prefix` — DAG-closed milestone set being considered for freeze (e.g. `["maint_020", "maint_017", "maint_026", "maint_028"]`)
- `script_report` — output of the `frozen-check` verb (`frozen_check_gate` module):
  - 7 aggregate gate values with waiver-exclusive real classification
  - Pass/fail per gate at default thresholds
- `round_k` — current round number (for context — earlier rounds have less stability evidence)
- `workspace_dir`
- (optional) `prior_freeze_attempts` — has this prefix been considered before? what happened?

### Data sources

For each milestone in `candidate_prefix`:

| Path | Use |
|---|---|
| `dockerfiles/{MID}/build_status.json` | gate result + raw_metrics (cross-check vs script) |
| `dockerfiles/{MID}/module2_waivers.json::unbound_issues` | **key signal** — issues M2 couldn't bind to waivers |
| `dockerfiles/{MID}/module2_signals.json` | pending cherry-pick proposals that weren't taken |
| `_build_runs/{MID}/attempt_N/meta.json` (latest N) | recent attempt's failed_gates progression |
| `_build_runs/{MID}/` count of attempts | high count = M2 struggled here even if final attempt passed |
| `_orch_runs/round_*/narrative.md` | history of this milestone across rounds |

For prefix boundary (milestone right after candidate prefix in DAG order):

| Path | Use |
|---|---|
| `dockerfiles/{boundary_MID}/module2_waivers.json::unbound_issues` | does it reference any candidate milestone? |
| `dockerfiles/{boundary_MID}/module2_signals.json` | does it propose cherry-picks back into candidate? |

### What to look for — 6 latent risk categories

Each category has a **canonical snake_case label** used in the YAML output's `category` field. Use exactly these labels:

| Heading | `category` label |
|---|---|
| A. Hidden cheat in technically-passing milestone | `hidden_cheat` |
| B. Unbound issues pointing at unfrozen tail | `unbound_to_tail` |
| C. Cluster residue | `cluster_residue` |
| D. Attempt count anomaly | `attempt_count_anomaly` |
| E. Boundary leak | `boundary_leak` |
| F. Borderline gate score | `borderline_score` |

#### A. Hidden cheat in technically-passing milestone

Milestone is `built=true` and passes all gates in script report, BUT:

- Many `unbound_issues` referencing "future feature" that's documented but not actually needed for tests to pass
- Recent attempt's `agent/wrapper.log` shows agent considered adding shim and abandoned — could come back
- `H_indirect_src_modify=true` flag in raw_metrics suggests env writes touched source files

**Why this matters**: shim that "didn't fire this attempt" can fire again later if M2 retries; freeze locks the cheat in.

#### B. Unbound issues pointing at unfrozen tail

Candidate milestone's `unbound_issues` reference SHAs / symbols that originate from unfrozen tail milestones.

**Why this matters**: freezing this milestone means unfrozen tail can't be cleaned up upstream-of-it later, but its issue says it depends on something that's still moving.

**Special case — `candidate_prefix == scope_milestones`**: full-scope candidate, no unfrozen tail exists. This category is **N/A by definition** — `unbound_issues` cannot reference a tail that doesn't exist. Mark `severity: none` with `evidence: "no tail (candidate == scope)"` in the YAML, do not hunt.

#### C. Cluster residue

Multiple candidate milestones share a `cluster` pattern (same unbound topic, same missing prerequisite) but only **some** are technically green.

**Why this matters**: cluster homogeneity says they should all behave the same way. If 3 of 4 green and 1 still red, the 3 green are likely fragile — same root cause might re-trip them.

#### D. Attempt count anomaly

A candidate milestone has 6+ attempts in `_build_runs/{MID}/` before passing.

**Why this matters**: M2 struggled here. The "passing" attempt may be cheating-by-other-means or got lucky.

#### E. Boundary leak

The first unfrozen-tail milestone (right after candidate prefix in DAG order) has `module2_signals` proposing a cherry-pick back into a candidate milestone, OR its `unbound_issues` says "I need X from {candidate_MID}".

**Why this matters**: post-freeze, you can't go back and add X. The tail will be stuck.

**Special case — `candidate_prefix == scope_milestones`**: no "first unfrozen-tail milestone" exists. This category is **N/A by definition**. Mark `severity: none` with `evidence: "no tail (candidate == scope)"`, do not hunt. (If you spot an `unbound_issue` referencing a milestone OUTSIDE the scope, that's a separate concern — surface it under category C cluster residue or A residual lint, not here.)

#### F. Borderline gate score

Even when script says PASS, check borderline numbers:

- `H_none_error_fail_to_pass_present_ratio` (counts MS with any F2P / N2P / error→P / skipped→P) just barely above threshold (e.g. 0.81 vs 0.80)?
- Multiple milestones with exactly 1 F2P (vs robust 5+ F2P)?
- `H_anomaly_within_budget_ratio` just barely above threshold, with high anomaly counts within passing milestones?

**Why this matters**: bordering scores are fragile to noise. One re-run might tip them.

### Output format

YAML — strict structure. **< 500 words total**.

```yaml
frozen_risk_assessment:
  candidate_prefix: ["maint_020", "maint_017", "maint_026", "maint_028"]
  round_k: 4
  script_summary:
    gates_passed: 7   # echo from script report
    gates_failed: 0
    borderline_count: 1
  latent_risks:
    - category: hidden_cheat
      milestone: maint_017
      severity: medium
      evidence:
        - "module2_waivers.unbound_issues has 6 entries referencing django.contrib.async session"
        - "attempt_5/agent/wrapper.log L120-140: agent considered shim approach (rejected)"
      consequence: "Future M2 retry might re-introduce shim if unbound stays unaddressed"
    - category: attempt_count_anomaly
      milestone: maint_028
      severity: low
      evidence:
        - "8 attempts before passing; final attempt's input_hashes differ only marginally from attempt_7"
      consequence: "Pass may not be robust; one noise event could re-trip H_f2f"
    - category: boundary_leak
      milestone: maint_028 → maint_027 (first unfrozen)
      severity: high
      evidence:
        - "maint_027 module2_signals proposes cherry-pick of SHA in maint_028's range"
        - "Freezing prevents this cherry-pick path"
      consequence: "maint_027 will be stuck without cherry-pick option after freeze"
  recommendation: WAIT      # FREEZE / WAIT / SHOULD_NOT
  reasoning: |
    Script gates all pass but boundary_leak is severe — maint_027 (immediately downstream)
    has unresolved dependency on commit currently inside maint_028. Freezing maint_028 cuts
    off the only remaining clean path for 027. Recommend WAIT one round: let M3 resolve
    027's cherry-pick proposal first, then re-evaluate freeze of the same prefix.
  if_wait_what_to_watch:
    - "maint_027 module2_signals resolution in next round"
    - "Whether maint_017's unbound async-session issues get acknowledged via waiver or stay open"
```

### Recommendation rubric

The three outcomes track different signals — keep them separate:

- **`SHOULD_NOT`** — you have **concrete evidence the script's PASS is untrustworthy** (a hard gate is actually below threshold, or a disqualifying defect the aggregate missed). You are dispatched only *after* the script passed (exit 0), so a genuine script *fail* never reaches you — M3 records SHOULD_NOT without dispatching you. Use this verdict only to flag a PASS you can disprove with a cited gate + contradicting artifact.
- **`WAIT`** — script gates all PASS, **but you found latent risks** (subjective). Recommend M3 hold off freeze for 1+ more rounds to let risks resolve. Use this for: hidden cheat signals, boundary leaks, attempt-count anomalies, cluster residue, or 2+ medium-severity risks even when each individually tractable. Conservative bias: when in doubt, choose WAIT.
- **`FREEZE`** — script gates all PASS **and** no high-severity latent risks **and** boundary is clean. Borderline scores acceptable if isolated.

Two distinct signal sources, two distinct outcomes — don't conflate them:

| Signal | Source | Recommended |
|---|---|---|
| Script PASS is disprovable (you cite evidence) | your check vs the `frozen-check` report | `SHOULD_NOT` |
| Latent risks present (script PASS) | subjective (your assessment) | `WAIT` |
| Both clean | both | `FREEZE` |

### Constraints

- **< 500 words total**
- **Don't recompute gates** — trust the script unless you find evidence of error (then flag as a finding, not a recompute)
- **Always check boundary** — first unfrozen-tail milestone gets special scrutiny
- **No fixes proposed** — only assessment. M3 decides next action.
- **Cite paths and counts** for every risk
- **Be conservative** — when in doubt, recommend WAIT. Freeze locks DAG/recipe state and unfreeze should be rare, so freeze-regret carries real cost — but it is **not** irreversible (§ Freeze decision rules: unfreeze is supported).
