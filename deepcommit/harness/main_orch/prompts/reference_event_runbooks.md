# Phase 3 Module 3 — Event Runbooks (reference)

Per-event execution recipes for M3's Step 3, relocated verbatim from `main.md` to keep them out of
the always-resident skill. Open the matching runbook only when you are about to execute that event type.

**Cross-references:** bare `§Name` / "see §…" point back to **`main.md`** (e.g. §State gate,
§Orchestration-support verbs, §Step 1/2). Deep heuristics cited as "§Reference → …" live in
`reference_operational_patterns.md` (this folder).

---

The audit → decide → execute → record chain looks different for each event type. Internalize these as patterns, not rules.

#### `dag_update` — DAG topology change

```
Step 1 · Analyze
  └─ (optional) dispatch Skill 2 → returns findings + suggested_final_ops
       Skill 2 is read-only diagnostic. You decide whether to act on its suggestions.

Step 2 · Decide
  ├─ Check rejected_suggestions.yaml — has this hypothesis been tried and rejected before?
  ├─ Verify symbol provenance: `cd testbed/ && git log --reverse -S '<symbol>' --all`
  ├─ Verify the proposed edge doesn't create a cycle or duplicate a transitive ancestor
  └─ Append event skeleton with final_ops (grouped by op type, each type appears once)

Step 3 · Execute
  ├─ Edit milestones.csv / dependencies.csv inline (M3 directly modifies these files)
  ├─ Rebuild: run build_milestone_testbed.py. Clean rebuild → read build_report.json directly.
  │    Conflicts needing iterative L0/L0.5/L1 resolution → spawn a Module-1 subagent (brief below);
  │    it applies edge-only Decision Ladder within authority, escalates L2+, returns structured YAML
  ├─ Write m1_result back into the same event (includes applied_refinements when non-empty)
  ├─ Verify mechanically (don't trust the diff by eye): verify-dag --cumulative (§verbs) — no cycle /
  │     commit conservation / ops-equivalence (declared ops since baseline == real CSV diff). Exit 2 →
  │     adjudicate the violation (regression vs revealed cycle, or a mis-recorded op) next event; do NOT
  │     return to Step 1. Record verify_dag: {passed, mode, run_id} on the event.
  └─ If final_status=escalated: read the subagent's analysis_report_run_N.md for the proposed
     L2+ op, append a NEW dag_update event in the next iteration with that op

Return to Step 1 — fresh build_report.json now in your data sources
```

Always rebuild the testbed within a `dag_update` event after CSV edits — testbed tags must rebuild (run `build_milestone_testbed.py`; spawn a Module-1 subagent if conflicts need laddered resolution). A `dag_update` without a rebuild is not a `dag_update`; it's a no-op.

**Prune moot waivers after a closure-changing `dag_update`.** When a dag_update changes a milestone's closure, `bound_waivers` anchored to error_groups that no longer exist go **moot** — not merely stale (the cited commit moved upstream) but pointing at a vanished anchor — and they add noise to the next round's audit. Run `python3 -m deepcommit.harness.prepare_images.reconcile_stale_waivers <changed-cone-MIDs> --work-dir {workspace}` (do `--dry-run` first) to physically delete moot `bound_waivers` and cascade-drop orphaned `commit_moves`; it never touches `_build_runs/` archives. `validate_only` does **not** do this — it leaves all waivers in place.

**Module-1 subagent brief template** (use when a `dag_update` rebuild needs laddered conflict resolution):

```
Task: Validate M3's dag_update by rebuilding testbed; apply L0/L0.5/L1 refinements
      if runtime surfaces them; escalate any L2+ need.
Workspace: {workspace}
Mode: dispatch
M3-applied ops: <enumerate from final_ops>
Authority bounds:
  ✅ Auto-apply: add_edge / drop_edge / reverse_edge, only when non-duplicate and
     non-colliding with M3-applied ops
  🛑 Escalate:   reassign_commit / merge_milestone / split_milestone / drop_milestone / drop_commit
Action: Run the M1 skill's Build command from a cwd where the DeepCommit/harness
        script resolves (usually DeepCommit repo root with --trial); use `cd testbed/`
        only for git provenance commands, never for the build itself.
Return 5 required fields + optional applied_refinements per deepcommit/harness/main_orch/prompts/reference_module1_testbed.md.
```

#### `env_refine` mode=`agent` — full M2 agent invocation

```
Step 1 · Analyze
  ├─ (optional) dispatch Skill 1 → audit findings for the failing milestone(s)
  └─ Check M2 waste patterns: read _build_runs/{MID}/attempt_*/after_agent/ for last 2 attempts
     If pattern A/B/C (bit-identical / frozen Dockerfile / oscillating) → do NOT dispatch agent;
     escalate to dag_update, surgical, regen_dockerfile, or unbound conversion instead.

Step 2 · Decide
  ├─ Cluster milestones by shared failure mode → single event with hints._shared
  ├─ Or per-MS hints when failures diverge significantly
  └─ Append event skeleton

Step 3 · Execute (subprocess CLI)
  ├─ python3 -m deepcommit.harness.prepare_images.build_milestone_image \
  │       <MID_list> --work-dir {workspace} \
  │       --agent claude --model <model> --parallel <P> --max-retries <R>
  ├─ Read each MID's new build_status.json
  └─ Record per-MID failed_gates + attempt_n + (if present) lint_warnings in result

Return to Step 1
```

**`lint_warnings > 0` in a built milestone is a red flag**: M2 produced a built image but `build_run_integrity` detected suspect patterns (e.g., heredoc-based indirect src modification, agent shortcut). The milestone passed gates but cheated to get there. Flag in narrative; consider `regen_dockerfile` or a DAG edit next round.

#### `env_refine` mode=`validate_only` — cheap rerun, no agent

```
Step 1 · Analyze
  └─ For each candidate MID, compare current input fingerprint to last attempt's:
     (start_sha, end_sha, dockerfile_sha256, test_config_sha256,
      module2_waivers_sha256, module2_signals_sha256)
     Hashes live in _build_runs/{MID}/attempt_N/after_validation/input_hashes.json

Step 2 · Decide
  ├─ Skip the milestone entirely if all six hashes unchanged (prior output is bit-identical reuse)
  └─ Include in this event the MIDs where only testbed SHAs changed (M1 just rebuilt) but recipes
     are unchanged — they need a gate recompute, not a fresh agent invocation

Step 3 · Execute (subprocess CLI)
  ├─ snapshot (§State gate, pre-op) → build_milestone_image … --validate-current-only --parallel <P>
  ├─ Read new build_status.json — gates may pass without any agent work
  └─ Post-op preflight (§State gate); rebuild_crashed ⇒ snapshot --restore

Return to Step 1
```

`--validate-current-only` re-runs test collection + Layer 2 intersection but doesn't invoke the agent. Cheap fallback after a DAG edit that changed upstream SHAs.

What it does NOT do:
- Does **not** clear stale waivers (a waiver granted in a prior attempt remains even if the cited future commit is now upstream)
- Does refresh `patched_collected` against current testbed state

(Rebuild-died hazard + recovery: see §State gate.)

#### `env_refine` mode=`surgical` — M3-direct recipe edit, no agent

**Precondition: `preflight` must say `safe`** (§State gate) — surgical's validate forces a full Docker rebuild, so a broken rebuild path fails every edit regardless of content. `blocked` / `needs_rebuild` ⇒ surgical is **BLOCKED this round**: record `deferred_to_next_run` priority=critical with the findings, surface to operator, and continue with audit-only (Skill 1/2/3) / DAG-only / `freeze_check` events.

```
Step 1 · Analyze
  └─ Classify the trigger — is this a narrow recipe-only fix?
       Valid:   stale waiver edit, test_config routing fix, runner-command fix
       Invalid: behavior-substituting shim, full Dockerfile rewrite, anything
                that needs M2's reasoning from scratch → use mode=agent

Step 2 · Decide
  ├─ snapshot (§verbs) the recipe files before editing → snapshots/seq_XX_pre/{MID}/ (backs up Dockerfile
  │     + test_config + waivers + signals + build_status.json; one call covers a multi-MS batch)
  └─ Append event skeleton with surgical_edits[] (intent, target_files, source_ref, guard, rollback)

Step 3 · Execute (M3 edits files directly)
  ├─ Edit ONLY: dockerfiles/{MID}/{Dockerfile, test_config.json, module2_waivers.json}
  │     Boundary: never edit checked-out repo src/ or tests/ directly
  │     Boundary: do not create behavior-substituting shims — if functionality is genuinely
  │               missing, record an unbound issue or escalate to dag_update
  ├─ Validate: build_milestone_image.py --validate-current-only
  ├─ Apply guard: compare pre/post H_patched gain vs F2P signal
  │     Keep when H_patched gain is meaningful and F2P remains above per-MS + aggregate thresholds
  │     Revert when F2P collapses for negligible H_patched gain, or any hard gate gets worse
  │     After revert, re-run validate-only so result reflects the restored recipe state
  └─ Record result (post-keep-or-revert state) + guard decision

Return to Step 1
```

Surgical is a **scalpel**, not a shim factory. If you find yourself reaching for surgical to "make a test pass" without semantic justification, that's a cheat — record as unbound_issue or escalate via dag_update instead.

#### `env_refine` mode=`regen_dockerfile` — agent fresh restart

```
Step 1 · Analyze
  └─ Confirmed cheat detected (Skill 1 cheat_audit returned KIND2_VIOLATIONS_PRESENT)
     OR M2 stuck in pattern C (oscillating Dockerfile then steady-state same failed_gates for ≥3 attempts)
     AND you have specific reason to suspect the current Dockerfile is the structural blocker

Step 2 · Decide
  └─ Append event with mode=regen_dockerfile, single milestone

Step 3 · Execute (subprocess CLI)
  └─ python3 -m deepcommit.harness.prepare_images.build_milestone_image \
        <MID> --work-dir {workspace} \
        --agent claude --model <model> --regen-dockerfile --parallel 1

Return to Step 1
```

Rare. Most M2 issues are resolved by hint refinement (mode=agent) or DAG edits, not Dockerfile rewrites.
