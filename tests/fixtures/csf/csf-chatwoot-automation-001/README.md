# csf-chatwoot-automation-001

A Harbor multi-step task: the agent rebuilds Chatwoot's automation rules one milestone at a time, keeping its own code from step to step. Each step is graded in a separate verifier container.

This folder is the **pilot slice**: the first 2 of the 8 milestones planned in the [automation roadmap proposal](../../../data/261024-chatwoot-proposals/proposals/req6-loc0-r3-iso3-tc0-roadmap/automation-rules-roadmap.md).

| Step | Milestone | Gold | Hidden F2P specs |
|---|---|---|---|
| `m1-automation-rules-api` | Rule model, validations, reauthorization lifecycle, admin CRUD + clone API, attachments | 337 lines, 13 files | rule model spec, controller spec, `spec/csf/m1_automation_rules_api_spec.rb` (67 examples) |
| `m2-automation-condition-matching` | `ConditionValidationService` and `ConditionsFilterService`: filter-engine matching, `attribute_changed` gating, Captain short-circuit | 283 lines, 3 files | both service specs, `spec/csf/m2_attribute_changed_spec.rb` |

## Status (2026-10-26)

| Check | Result |
|---|---|
| Task loads in Harbor 0.24.0 (`Task(...)`) | ✓ 2 steps, separate verifier, collect hook |
| Environment image builds on spark | ✓ 26 s on top of `csf/chatwoot-ce:3564ec1e`, 5.55 GB |
| M1 reference solution, verifier run by hand | ✓ reward 1. F2P 67/67, P2P 650/650, verifier 78 s |
| M1 no change, verifier run by hand | ✓ reward 0. F2P fails to load (`AutomationRule` missing), P2P 650/650 |
| M2 reference solution and the M2 negative control (M1 gold only), by hand | pending: the first try hit a Postgres start-up race in the verifier container, since fixed in the base image |
| Harbor oracle and NOP gates | pending |
| Full kept suite in state_0 | not run yet |
| Agent trials | not run yet |

## How grading works

1. **Collect.** After each step, the `[[verifier.collect]]` hook `csf-collect` runs in the agent's container. It writes `/logs/artifacts/model.patch`: everything the agent changed under `/app` since the base state. The diff is taken against `/opt/csf/base.git`, a copy of the base commit kept outside `/app`, so the agent's own git use doesn't change what is graded.
2. **Verify.** Harbor starts a fresh verifier container from `environment/` (the base state) and uploads `tests/` plus `steps/<step>/tests/` to `/tests`. [`tests/test.sh`](tests/test.sh) then:
   - applies the patch, dropping changes under `spec/`;
   - copies in `tests/hidden/`;
   - runs the agent's migrations on the test database;
   - runs the step's F2P specs, then the P2P specs (earlier steps' F2P plus [`tests/p2p_core.txt`](tests/p2p_core.txt)).
3. **Grade.** [`tests/grade.rb`](tests/grade.rb) writes `reward.json`. The reward is 1 only if every F2P and every P2P example passes. The file also carries `f2p_total`, `f2p_passed`, `p2p_total` and `p2p_passed`, which the `/harbor-gates` skill reads. `ctrf.json` lists every example.
4. **Combine.** `multi_step_reward_strategy = "mean"`: the trial reward is the mean over steps.

## How the states were cut

The states were built as a commit series on top of Chatwoot CE (`3564ec1e` with `enterprise/` and `spec/enterprise/` removed). Each commit removes one milestone, top-down: macros backend (M8), the delayed-automation backend (M6), the listener and actions (M3), condition matching (M2), the rule model and API (M1). A final commit hides the specs. Reversing a removal commit gives that milestone's gold.

- **`environment/base.patch`** takes the CE tree to state_0. It is applied at image build through a BuildKit bind mount, so it never lands in an image layer. It does three things:
  - **Removes code:** the backend of M1, M2, M3, M6 and M8, plus their integration sites (routes, `Account`/`User`/`Conversation` associations and callbacks, the dispatcher entry, the `Reauthorizable` rule branches, the activity-message branches, the scheduler line). It also deletes the four delayed-automation migrations and hand-edits `db/schema.rb`.
  - **Hides specs:** the 15 automation and macro backend specs and their 3 factories.
  - **Trims kept specs:** the examples in 4 of them that need removed code (`account_spec`, `conversation_spec`, `action_cable_handoff_assignment_spec`, the rule branches of `reauthorizable_shared.rb`).
- **`steps/*/solution/gold.patch`** is that milestone's change. `solve.sh` applies it.
- **Frontend is not removed yet.** The automation and macro Vue code is still in state_0. It moves into the base when the frontend steps (M4, M5, M7, M8) are added. M1 and M2 touch only the backend, so their gold and tests stay the same when that happens.
- **The `listener` and the delayed hunks follow upstream.** The state_3 listener is upstream's pre-delay file, from commit `38a317962`, the parent of `0c606babe` "feat: time based automation".
- **`db/schema.rb` keeps upstream formatting.** A load + dump round trip on Postgres 16 rewrites 5 unrelated index lines and the `Schema[7.1]` header. The unmodified CE tree shows the same diff, so the state_0 schema edits are exact.

## Run it

The base image must exist on the Docker host (see [`tasks/_images/chatwoot-ce`](../../_images/chatwoot-ce/README.md)). Harbor 0.24.0 on spark lives in its own virtualenv:

```bash
rsync -a tasks/feature-evolution/csf-chatwoot-automation-001/ nvidia_spark_01_astitva:~/csf/tasks/csf-chatwoot-automation-001/
ssh nvidia_spark_01_astitva 'cd ~/csf && harbor-venv/bin/harbor run -p tasks/csf-chatwoot-automation-001 -a oracle -o runs'
ssh nvidia_spark_01_astitva 'cd ~/csf && harbor-venv/bin/harbor run -p tasks/csf-chatwoot-automation-001 -a nop -o runs'
```

## Known limits

- In a multi-step task, Harbor's NOP agent changes nothing at any step. From step 2 on, the earlier steps' F2P specs are part of P2P, so they fail under NOP too. The per-step negative control is therefore "gold for steps 1..k-1, nothing at step k": F2P must fail and P2P must pass. That control is run by hand.
- The hidden specs live in this folder. That is fine while the repo is private; a public release needs them encrypted or kept out of the repo.
- Upstream specs that stub internals are reused unchanged for now. The proposal's §7 lists the ones to rewrite before the later steps.
