# M3 Plan Schema (v0.7)

每轮 `_orch_runs/round_NNN/plan.yaml` 是 M3 的决策与执行账本:

```
_orch_runs/round_NNN/
├── plan.yaml                   ← 本轮写操作账本(events + deferred)
├── narrative.md                ← 推理过程、观察、读了什么(unchanged)
├── subagent_log/               ← 审计型子 agent 产出(unchanged)
├── rejected_suggestions.yaml   ← 被验证为错的假设(unchanged)
└── exec_logs/                  ← 每个 event 的原始 stdout/stderr(可选,约定 NNN_<event>.log)
```

**核心原则**:
- `plan.yaml` **只记录写操作完成的事实**,不记提案、不记中间过程
- 一个 sub-action = `events[]` 数组里的一个 event
- 三类事件:`dag_update` / `env_refine` / `freeze_check`
- 只读型行为(读 build_status、dispatch audit subagent)**不进 plan.yaml**,进 `narrative.md`
- 下轮 backlog 作为 plan.yaml 的 `deferred_to_next_run:` 顶层字段

**写入语义**: append-by-read-modify-write —— 完成一个 sub-action 后,读 `plan.yaml`,append 新 event 到 `events[]`,原子写回(`.tmp` + `mv`)。

---

## 顶层结构

```yaml
round: 1
started_at: 2026-05-19T09:30:00Z
summary: |
  Round 1 focus: cheap M2-only baseline-fix across 3 clusters + 1 DAG fix.
  No freeze (k < 3).

events:
  - event: env_refine
    ...
  - event: dag_update
    ...

deferred_to_next_run:
  - id: cluster_D_maint_027
    ...
```

| 字段 | 说明 |
|---|---|
| `round` | 本轮编号 |
| `started_at` | 本轮 Step 1 开始时间(轮级,event 不再单独记) |
| `summary` | 战略说明 / 本轮总目标(1-3 句) |
| `events` | event 数组,顺序即执行顺序 |
| `deferred_to_next_run` | 本轮明确决定不动的事项 |
| `baseline_run` | (可选)本轮起始的 canonical checkpoint —— 一个 `_testbed_runs/run_NNN/` 的 run id,其归档 CSV = round-start 状态。`verify-dag --cumulative` 用它当 fold 基线(见 §1 revert + "Reading the ledger")。不手动快照,直接引用 M1 的 canonical 归档 |

---

## 每个 event 的通用字段

三个:

| 字段 | 说明 |
|---|---|
| `event` | 事件类型:`dag_update` / `env_refine` / `freeze_check` |
| `seq` | 本轮内的执行序号,从 1 开始,**等于 `events[i].seq == i + 1`** |
| `rationale` | 为什么做这个 sub-action(1-3 句) |

事件级时间戳 / status / exec_log 全部省略 —— 成功 / 失败由各事件类型自己的结果字段表达。

**为什么保留 `seq`**:虽然 YAML 数组天然有序,但 `seq` 是**跨文件锚点**:
- `subagent_log/m1_seq02_dag_update.md` 文件命名引用
- `narrative.md` 里"参见 seq 3 的 split"反向引用
- `deferred_to_next_run.reason` 里"等 seq 4 完成后再决定"
- append 时 M3 自己核对顺序的硬约束

---

## 1 · `dag_update` event

agent 根据 M2 信号 propose DAG 变更 → M1 subagent 校验并 refine → 收敛后落账。**只记最终 ops + M1 最终结果**。

### 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `final_ops` | ✓ | M3 本 event 规划并执行的 ops,**按 op 类型分组**(每类至多出现一次) |
| `m1_result` | ✓ | M1 最终结果(5 个必填 status 字段 + 可选 `applied_refinements`) |
| `verify_dag` | — | (可选)本 event 跑完 `verify-dag` 后 M3 记录的结果:`{passed: bool, mode: cumulative\|strict_ops, run_id: <baseline run_NNN>}`。字段存在但 `passed != true` 一律判 violation;`--require-verify-dag` 额外要求每个 `dag_update` **必须带**该字段(缺失也判 violation)。见 §"机械校验" |
| `revert` | — | 仅 revert 变体(见下);存在时本 event 是纯恢复,**不带 `final_ops`** |

### revert —— dag_update 的恢复变体(一等公民)

撤销一个先前的 dag_update **不是**一个 op(没有 `final_ops: revert` 这种东西),而是把 CSV 恢复到一个 **canonical checkpoint**(某个 `_testbed_runs/run_NNN/` 归档),然后 M1 rebuild。它仍是一个 `dag_update` event,但用 `revert` 块声明、**不带 `final_ops`**:

```yaml
- event: dag_update
  seq: 3
  rationale: |
    seq1 的 D60 抽取 net-negative;恢复到 seq1 之前的 canonical 状态。
  revert:
    restore_from: run_016        # ✓ 必填:canonical checkpoint run id(= 恢复目标状态)
    reverts_seqs: [1, 2]         # 可选:声明它撤销了哪些 seq(供审计)
  m1_result:                     # ✓ 仍必填:恢复后 rebuild 的结果
    final_status: success
    run_id: run_020
```

约束:`revert` 存在时 `restore_from` 必填、`final_ops` 必须缺席(纯恢复;"恢复后再改"用后续独立 event)。语义:**最终 CSV 状态 = 最后一次 revert 的 `restore_from` + 其后事件**——这正是 `verify-dag --cumulative` 的 fold 规则(遇 revert 重置到 `restore_from` 归档,再继续 fold)。restore_from 指向 canonical 归档,既避免手动备份漂移,又让 revert 本身可被验证。

### `final_ops` —— 按 op 类型分组

**约束**:每种 op 类型在一个 event 里**最多出现一次**;同类多次操作都聚到同一组内。

```yaml
final_ops:
  add_edge:
    - {prerequisite_id, dependant_id, rationale}
    - ...
  drop_edge:
    - {prerequisite_id, dependant_id, rationale}
  reverse_edge:
    - {prerequisite_id, dependant_id, rationale}   # 改前的方向
  reassign_commit:
    - {commit_sha, from, to, rationale}
  drop_commit:
    - {commit_sha, from, rationale}
  split_milestone:
    - {milestone_id, at_index OR extracted_commits, new_ids: [M_a, M_b], edge_rewire, rationale}
  merge_milestone:
    - {merge_ids: [...], new_id, rationale}
  drop_milestone:
    - {milestone_id, cascaded_edges, rationale}
```

只列出本 event 用到的类型;未用的 key 直接省略。

**说明**:
- `reverse_edge` 等价于 `drop_edge + add_edge`,但单独保留为了记录意图("认可 Phase 2 推断,只改方向");条目记录**改前/当前方向**,执行时新方向由 prerequisite/dependant 对调得到
- `split_milestone.edge_rewire` 子字段记录入/出边重新挂接:`{inbound, outbound, internal, new_inbound_for_b}`
- M1 Decision Ladder L4b cross_split = `split_milestone` 列表里两个条目,在 rationale 里互相引用
- **`at_index` vs `extracted_commits` — 二选一**:
  - `at_index: N` — 顺序切分(M_a 拿 commits[:N],M_b 拿 commits[N:]);典型用于 phase-by-phase 拆分一个跨阶段的 milestone
  - `extracted_commits: [<sha_list>]` — 抽出指定 SHA(M_b 只拿这些;M_a 保留其余);典型用于把一个 feature commit 从"维护型 milestone"里抽出来形成独立 seed(常出现在 Skill 2 surface 的 revealed_cycle 解决方案里 — 见下方 commit-extraction 示例)
  - 同一条 `split_milestone` 条目里两个字段不可同时出现;`new_ids[1]` (M_b) 是 commit-extraction 的目标新 milestone(典型 naming `seed_<feature>_<year>`)

### `m1_result` 子对象

5 个必填 status 字段 + 1 个可选 `applied_refinements`:

```yaml
m1_result:
  final_status: success            # success | success_cached | partial | escalated
  milestones_success_count: 11
  milestones_conflict_count: 0
  milestones_skipped_count: 0
  run_id: run_016                  # M1 本次产出的 testbed run 编号

  applied_refinements:             # 可选;为空时省略整个字段
    add_edge:                      # L1 ops M1 在 dispatch 授权内自主应用
      - {prerequisite_id, dependant_id, rationale}
    drop_edge:                     # L0 ops
      - {prerequisite_id, dependant_id, rationale}
    reverse_edge:                  # L0.5 ops
      - {prerequisite_id, dependant_id, rationale}   # 改前/当前方向
```

`final_status` 4 值含义:
- `success` — `build_report` 显示 `conflict=0 AND skipped_due_to_parent=0`
- `success_cached` — DAG 已经干净,无需 refinement(命中缓存);`applied_refinements` 必然省略
- `partial` — M1 走了 M3 显式授权的 acceptable-drop exit,丢弃的 milestone 记录在 M1 自己的 `analysis_report_run_N.md` 里;未授权时 acceptable-drop 只能返回 `escalated`
- `escalated` — M1 在 dispatch 模式下遇到 L2+ 需求 OR 撞到 whack-a-mole / cross-cutting reformat / structural bedrock,需要 M3 介入;**仍可携带 escalate 前已成功 apply 的 edge ops**

`applied_refinements` 字段:
- 只在 M3 spawn subagent(dispatch)时由 M1 填写;M3 直接 run 时由 M3 自己改 CSV,无需回传
- **仅含 L0 / L0.5 / L1 edge ops**(只写 `dependencies.csv`);L2+ 永远不出现在这里(那是 escalate 信号,不是 apply)
- 结构跟 `final_ops` 同形(按 op 类型分组,数组)
- M1 apply 前必须确认 refinement 不是 M3 `final_ops` 中已有 edge op 的重复/反向,也不触碰 split/merge/drop_milestone composite rewiring;碰撞时不静默改写,改为 `escalated`
- 为空时整个字段省略(不写 `applied_refinements: {}`)
- M3 在 event 收尾时把 M1 的返回 verbatim 落账;**不合并进 `final_ops`**,保留独立审计 trace

`run_id` 是**唯一非可推导的引用** —— 完整路径 = `_testbed_runs/{run_id}/build_report.json`。其它任何细节(M1 自己的 narrative、refine 迭代次数、escalation 提案 L2+ 内容)都在 M1 自己写的 `analysis_report_run_N.md`(workspace root)里;M3 dispatch 简报和 M1 structured response 保存在 `subagent_log/m1_seq{N}_*.md`。plan.yaml 不重复记。

### `dag_update` 事件的权限分区

M3 写 `final_ops`,M1 在 dispatch 模式下可能补 `applied_refinements`。这是按**决策来源和执行阶段**分工,不是按文件天然隔离:

| Op | 谁写 | 触碰哪个 CSV | M1 Decision Ladder 对应级别 |
|---|---|---|---|
| `add_edge` | M3 (plan) 或 M1 (refinement) | `dependencies.csv` | L1 |
| `drop_edge` | M3 (plan) 或 M1 (refinement) | `dependencies.csv` | L0 |
| `reverse_edge` | M3 (plan) 或 M1 (refinement) | `dependencies.csv` | L0.5 |
| `reassign_commit` | **仅 M3** | `milestones.csv` | L2 (M1 dispatch 模式必须 escalate) |
| `drop_commit` | **仅 M3** | `milestones.csv` | L5 (M1 必须 escalate) |
| `split_milestone` | **仅 M3** | `milestones.csv` + `dependencies.csv` | L4a / L4b (M1 必须 escalate) |
| `merge_milestone` | **仅 M3** | `milestones.csv` + `dependencies.csv` | L3 (M1 必须 escalate) |
| `drop_milestone` | **仅 M3** | `milestones.csv` + `dependencies.csv` | L5 (M1 必须 escalate) |

理由:edge ops 由 `prior_modifiers` runtime 证据驱动(机械);milestone ops 涉及跨 milestone 语义(同 issue 归并 / 同主题合并 / 阶段拆分),需要 M3 结合 `rejected_suggestions` + frozen prefix + 跨轮 cluster pattern 判断。M3 也可以在 `final_ops` 中规划 edge ops;M1 的 edge refinement 只表示 M3 plan 应用之后、rebuild 新暴露的额外 runtime 修正。

### Reading the ledger — 合并方程

读一个 `dag_update` event 想还原它造成的真实 CSV diff,做加法:

```
event 的 CSV diff = final_ops  (M3 的 plan,语义层)
                  + m1_result.applied_refinements  (M1 在授权内的 runtime 修正)
```

加法成立的不变量:`final_ops` 包含 M3 的所有 op(包括 edge ops 的 plan 部分);`applied_refinements` 只包含 M1 在 M3 plan 之外、build_report 触发的额外 edge ops。两者不是靠"写不同文件"避免冲突:它们可能都影响 `dependencies.csv`。正确约束是 M1 只能追加 non-duplicate / non-contradictory 的 edge refinements;若候选 refinement 与 M3 edge plan 重复、反向,或触碰 composite edge rewiring,必须 de-dup 或返回 `escalated`。

**只读 `final_ops` 不够** —— round k+5 想审计 "上轮 maint_Y 为什么多了一条入边",答案可能在 `applied_refinements` 而不是 `final_ops`。永远读两个字段。

> **机械校验(`verify-dag --strict-ops`)**:此"diff = final_ops + applied_refinements"等价不再只是约定 —— `verify-dag --strict-ops --before <seq_XX_pre> --plan plan.yaml --seq N` 会把本 event 的 `final_ops`(+ `applied_refinements`)应用到 before 快照,断言模拟结果与实际 `milestones.csv` / `dependencies.csv` 在**边集 / 每-milestone commit 归属 / milestone-id 集**上完全相等;不符即 `ops_equivalence` violation(exit 2)。它能抓到"声明了 add_edge 却没加 / 多了未声明的边 / reassign 到错的 milestone / split 出错 new_ids"——这些是 acyclicity + commit-conservation 单独漏过的缺陷。`edge_rewire` 自由文本不参与:host 保留原 id 时其边不变,真实改边由显式 `add_edge`/`drop_edge` 承载。
>
> **整轮形式(`verify-dag --cumulative`)**:`verify-dag --cumulative --plan plan.yaml`(可选 `--through-seq N`)从顶层 `baseline_run` 归档载入 round-start 状态,按 seq fold **本轮全部** `dag_update`(`final_ops`+`applied_refinements`;遇 `revert` 重置到其 `restore_from` 归档),再与**当前** CSV 比对。这样每轮只需一个 canonical 基线(不是每个 dag 一个备份),且直接对应上面的合并方程在整轮上的累加。任一基线/恢复点归档缺 CSV → exit 1(fail-closed,绝不假绿)。

### 完整示例

```yaml
- event: dag_update
  seq: 2
  rationale: |
    Skill 2 confirmed maint_017 → maint_027 missing edge AND c991602
    bridge-commit belongs to maint_025 (same issue #34060).

  final_ops:
    add_edge:
      - prerequisite_id: maint_017
        dependant_id: maint_027
        rationale: verified via `git log --reverse -S` in testbed/
    reassign_commit:
      - commit_sha: c991602
        from: maint_027
        to: maint_025
        rationale: same issue #34060; 0d8fbe2 already in maint_025

  m1_result:
    final_status: success
    milestones_success_count: 11
    milestones_conflict_count: 0
    milestones_skipped_count: 0
    run_id: run_016
    applied_refinements:
      add_edge:
        - {prerequisite_id: maint_M, dependant_id: maint_Y,
           rationale: "prior_modifiers point M→Y; Phase 2 missed"}
      drop_edge:
        - {prerequisite_id: maint_X, dependant_id: maint_Y,
           rationale: "no runtime evidence; spurious Phase 2 inference"}
```

读这个 event 的真实 CSV diff:`final_ops`(maint_017→maint_027 加边 + c991602 reassign)+ `applied_refinements`(maint_M→maint_Y 加边 + maint_X→maint_Y 删边)。

### Pattern · commit-extraction via `split_milestone`

**典型触发**:Skill 2 audit 发现某个 feature commit(如 `f333e3513e8` GeneratedField)被错放进一个"维护型"或"复合型" milestone(如 `milestone_seed_f333e35_1`),导致下游 milestone 想 cherry-pick 这个 feature 时和该 host milestone 的其它依赖形成 `revealed_cycle`。`reassign_commit` 单独不够用(目标 milestone 不存在);需要 `split_milestone` 用 `extracted_commits` 把单个 commit 抽出来形成新 seed milestone,然后用 `add_edge` / `drop_edge` 重挂依赖。

```yaml
- event: dag_update
  seq: 3
  rationale: |
    Skill 2 (subagent_log/skill2_seq01_*.md) identified revealed_cycle:
    f333e3513e8 (GeneratedField, Fixed #31300) sits in milestone_seed_f333e35_1
    but downstream maint_027/maint_032 want to cherry-pick it; existing edge
    maint_027 → maint_013 → milestone_seed_f333e35_1 closes the cycle.
    Extract f333e3513e8 into a new seed; re-attach maint_013's text-dep to
    the new seed; wire new downstream edges.

  final_ops:
    split_milestone:
      - milestone_id: milestone_seed_f333e35_1
        extracted_commits: [f333e3513e8bdf5ffeb6eeb63021c230082e6f95]
        new_ids: [milestone_seed_f333e35_1, seed_generatedfield_2023]
        edge_rewire:
          inbound:                    # 原 host milestone 的入边
            - {keep_on: milestone_seed_f333e35_1, edges: [milestone_seed_2b58238_1 → milestone_seed_f333e35_1]}
          outbound:                   # 原 host milestone 的出边
            - {keep_on: milestone_seed_f333e35_1, edges: [milestone_seed_f333e35_1 → milestone_seed_9946f0b_1, milestone_seed_f333e35_1 → maint_015]}
          new_inbound_for_b: []       # 新 seed 不需要额外入边(它是 feature 起点)
        rationale: |
          new_ids[0] keeps the original ID (the 11 remaining fix-commits stay there).
          new_ids[1] is the extracted feature commit's new home.
          Naming convention: seed_<feature>_<year>.

    drop_edge:
      - prerequisite_id: maint_013
        dependant_id: milestone_seed_f333e35_1
        rationale: "text-dep was actually on f333e3513e8; moves with the commit"

    add_edge:
      - {prerequisite_id: maint_013, dependant_id: seed_generatedfield_2023,
         rationale: "re-attach maint_013's text-dep cycle-safe"}
      - {prerequisite_id: seed_generatedfield_2023, dependant_id: maint_027,
         rationale: "maint_027 unbound#3 cites this SHA — now cycle-safe"}
      - {prerequisite_id: seed_generatedfield_2023, dependant_id: maint_032,
         rationale: "maint_032 unbound#4 cites this SHA"}

  m1_result:
    # ... 5 fields + optional applied_refinements
```

**Why split_milestone, not reassign_commit**:`reassign_commit` 要求目标 milestone 已存在;commit-extraction 的本质是**同时创建新 milestone + 移动 commit**,这是单一原子操作,用 `split_milestone(extracted_commits=...)` 表达比 "先 split→再 reassign" 干净。M1 dispatch 模式下整个 op 由 M3 在 plan 内执行,M1 escalate L4。

**前置条件**:执行此 event 前,M3 必须确认所有受影响 milestones(原 host + 重挂入边的 milestone + 所有新出边的 dependant)的 frozen 状态。若任一在 `_frozen_prefix.json::frozen_milestones` 中,先做 UNFREEZE ceremony(见 `reference_freeze_ceremony.md` § Freeze decision rules(unfreeze 部分))。

**Skill 2 dispatch 时机**:这种 cycle 拆解通常先用 Skill 2 audit 出来(返回 `issue: revealed_cycle` + `suggested_final_ops` 里同时含 reassign_commit / drop_edge / add_edge),M3 verify 后翻译成上面的 `split_milestone + drop_edge + add_edge` 组合。Skill 2 返回的 ops 数组只是"应当达成的最终状态";M3 决定怎么用 plan_schema 表达。

---

## 2 · `env_refine` event

一次 M2 批量调用 = 一个 event。一批就是一批,不分 wave。

### `mode` 四选一

| mode | 说明 | 谁在改文件 |
|---|---|---|
| `agent` | 默认。M2 invoke claude coding agent,可能改 Dockerfile / waivers / test_config | M2 内部 agent |
| `validate_only` | `--validate-current-only`,SHA 已变但 recipe 文件未变,只重跑硬门 | 无 |
| `regen_dockerfile` | fresh restart,丢旧 Dockerfile,agent 重新生成 | M2 内部 agent |
| `surgical` | M3 直接手术式改 `dockerfiles/{MID}/` 下的 recipe 文件,然后跑 validate_only 校验。不 invoke M2 agent | M3 自己 |

### 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `milestones` | ✓ | 本批目标 milestone 列表 |
| `mode` | ✓ | 见上表 |
| `hints` | mode=agent 时 | dict:`{_shared: "..."}` 或 `{maint_X: "..."}` |
| `surgical_edits` | mode=surgical 时 | M3 改了哪些 recipe 文件、选择的 source/path、guard 结果与 rollback snapshot |
| `result` | ✓ | 每个 milestone 的结果 dict |

省略字段:
- `parallel`(并发数是执行细节,不入账)
- `flags`(`regen_dockerfile` 已由 mode 表达;`clear_attempts` 是低频开关,需要时写进 rationale)

### `result` 结构

直接是 `per_milestone` 的 dict;`failed_gates: []` 即代表 built 成功,不重复写 `built` 字段。

```yaml
result:
  maint_020: {failed_gates: [],                     attempt_n: 3}
  maint_014: {failed_gates: [H_f2f_not_above_base], attempt_n: 12, lint_warnings: 2}
  maint_015: {failed_gates: [H_f2f_not_above_base], attempt_n: 11}
```

**per-milestone 字段**:

| 字段 | 必填 | 说明 |
|---|---|---|
| `failed_gates` | ✓ | 失败的硬门列表(空数组即 built 成功) |
| `attempt_n` | ✓ | 本次 M2 跑到第几次 attempt |
| `lint_warnings` | 仅在 > 0 时写 | `build_run_integrity.violations` 中的 warn 计数。**预警 M2 agent 可能用了 indirect_src_modify / heredoc 写源码等可疑行为**。详情去 `_build_runs/{MID}/attempt_{N}/after_validation/build_status.json` 读 |
| `status` | 仅在 reverted 时写 | surgical 事件回滚/中止的显式标识。Enum: `reverted_due_to_guard_collapse`(guard 决策放弃 — 如 F2P 塌陷)、`reverted_due_to_harness_block`(harness rebuild 死掉 — 如 base image 损坏)、`reverted_due_to_validation_fail`(validate-only 子步骤异常)。Absence = 正常完成。**`failed_gates` 在 reverted 时记录 post-revert (即 pre-edit) state**;`attempt_n` 保持 reverted 前的值不变。语义见 `main.md §"Reverted events"` |
| `guard_decision` | 仅在 reverted 时写 | 简短人类可读字符串,说明为何 revert(供下一轮快速判断要不要 re-attempt 同一 surgical edit)|

**为什么单列 `lint_warnings`**: 在 `passed: true` 的硬门下,lint 仍可能有 warn,意味着 agent 走了**捷径而非真修复**(典型:用 `cat > /tmp/x.py << PYEOF ... ; python /tmp/x.py` 这种 heredoc 间接改源码的 shim)。M3 必须能一眼看到这种红旗。

### `mode=surgical` 约束

`surgical` 是 **env_refine 的一个 sub-action**,不是新的 event 类型。它的对象仍然是某个 milestone 的 env recipe,目标是刷新该 milestone 的可评估环境。

**允许编辑的文件边界**:
- 只能直接编辑 `dockerfiles/{MID}/Dockerfile`、`dockerfiles/{MID}/test_config.json`、`dockerfiles/{MID}/module2_waivers.json`
- 不允许直接编辑 checked-out repo 里的 `src/`、`tests/` 或项目源码目录
- 如果需要改变 start-state 的 src 内容,必须把动作编码进 recipe(例如 Dockerfile 中的 `RUN cd /testbed && git checkout <source_ref> -- <paths>`),而不是手写/伪造源码 shim

**常见 intent**:

| intent | 说明 |
|---|---|
| `waiver_edit` | 修正 stale/invalid waiver,不改变测试执行语义 |
| `test_config_routing` | 调整测试选择、nodeid、runner 参数等 recipe 级路由 |
| `runner_config` | Dockerfile / runner 层依赖或命令修正 |

> **不要 preapply start-state src(已废弃的 intent)。** own-src 的 `error→pass` 已被 classifier 计入(进 `patched_collected` + `H_none_error_fail_to_pass_present`,且 own-src 的 `start_only_error` 被豁免 START error budget——见 `reference_operational_patterns.md` § H_patched_completeness mechanics + § Testbed start-state invariant)。预应用只是把 `error→pass` 变成 `pass→pass`,而该 test 早已计入分子——**H_patched 拿不到任何增益**。若 patched test 缺的是**别的 owner milestone** 的 src,那是闭包缺口,走 `dag_update`(加 prereq);**绝不**用 recipe 把闭包外 src 注入 start-state(破坏 start-state 不变量、属 shim 作弊)。

**guard / rollback**:
- 每个 surgical batch 先写 `rollback_snapshot`,至少覆盖将被编辑的 `dockerfiles/{MID}/` recipe 文件
- validate-only 后记录 `H_patched_completeness` 和 F2P 信号的 pre/post 摘要
- 如果 H_patched 收益很小但 F2P 信号明显跌穿,或 aggregate / per-milestone F2P gate 变差,应 revert snapshot 并在 `guard.decision: revert` 中说明
- `guard` 记录 surgical edit 的 pre/post 指标;`result` 记录 event 结束后的最终状态。若 `guard.decision: revert`,先恢复 snapshot,必要时再跑 validate-only,然后把 post-revert 状态写进 `result`

### `surgical_edits` 子结构

每个条目描述一个 milestone 的一类 surgical 改动:

```yaml
surgical_edits:
  - milestone: maint_027
    intent: waiver_edit
    rollback_snapshot: _orch_runs/manual_002/snapshots/batch_02_pre/maint_027/
    target_files:
      - dockerfiles/maint_027/module2_waivers.json
    guard:
      H_patched_completeness: {before: 0.953, after: 0.953}
      F2P_count: {before: 112, after: 112}
      decision: keep
      rationale: |
        Removed a moot waiver whose cited future commit is now upstream;
        signals unchanged, audit noise dropped.
    change: |
      Deleted one stale bound_waiver from module2_waivers.json (its anchor
      commit moved upstream by a prior dag_update); no test-execution change.
```

### 完整示例 A · mode=agent,per-milestone hint

```yaml
- event: env_refine
  seq: 1
  rationale: |
    Cluster A: 1 trivial waiver fix + 2 F2F triage probes.

  milestones: [maint_020, maint_014, maint_015]
  mode: agent

  hints:
    maint_020: |
      Fix the 1 invalid_bound_waiver. Do NOT regenerate Dockerfile.
    maint_014: |
      Single-gate H_f2f at 7.7%. Run F2F triage: identify cluster,
      propose targeted waivers OR document as unbound_issue.
    maint_015: |
      Same pattern as maint_014: H_f2f at 6.5%.

  result:
    maint_020: {failed_gates: [],                     attempt_n: 3}
    maint_014: {failed_gates: [H_f2f_not_above_base], attempt_n: 12}
    maint_015: {failed_gates: [H_f2f_not_above_base], attempt_n: 11}
```

### 完整示例 B · mode=surgical

```yaml
- event: env_refine
  seq: 5
  rationale: |
    maint_027 carried a moot bound_waiver: its cited future commit was brought
    upstream by an earlier dag_update, so the waiver pointed at a vanished
    anchor and only added audit noise. M3 deleted it surgically and re-validated
    that signals held before keeping.

  milestones: [maint_027]
  mode: surgical

  surgical_edits:
    - milestone: maint_027
      intent: waiver_edit
      rollback_snapshot: _orch_runs/manual_002/snapshots/batch_02_pre/maint_027/
      target_files:
        - dockerfiles/maint_027/module2_waivers.json
      guard:
        H_patched_completeness: {before: 0.953, after: 0.953}
        F2P_count: {before: 112, after: 112}
        decision: keep
        rationale: |
          Signals unchanged after removing the moot waiver; noise dropped.
      change: |
        Deleted one stale bound_waiver from module2_waivers.json (anchor commit
        moved upstream by a prior dag_update); no test-execution change.

  result:
    maint_027: {failed_gates: [], attempt_n: 4}
```

---

## 3 · `freeze_check` event

跑 `frozen-check` verb + Skill 3 latent risk + fork 决定。写入 `_frozen_prefix.json` 之后落账。

### 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `candidate_prefix` | ✓ | 提议冻结的 milestone 集合(必须 DAG-closed) |
| `script_gates_passed` | ✓ | `7/7` 等硬门通过比 |
| `skill3_risk` | ✓ | Skill 3 风险摘要:`{level, concerns}` |
| `fork_decision` | ✓ | `FREEZE` / `WAIT` / `SHOULD_NOT` |
| `decision_summary` | ✓ | fork 给的 < 200 词结论 |
| `newly_frozen` | ✓ | 新冻结的 milestone(WAIT/SHOULD_NOT 时为 `[]`) |
| `m3_alignment_check` | 仅在 fork override Skill 3 时写 | 当 fork 用 `reference_freeze_ceremony.md` § Freeze decision rules 的 procedural-WAIT 例外 override 了 Skill 3 时,显式记录:`{fork_override_skill3: true, override_rationale: "<一句话>"}`。不 override 时省略;省略 = 三层 align。详细规则见 `reference_freeze_ceremony.md` § Freeze decision rules |

省略字段:
- `script_result_path`(标准路径就是 `_orch_runs/round_NNN/frozen_check.json`,不重复记)
- `subagent_assessment.log`(标准路径 `subagent_log/skill3_seq{NN}_freeze.md`,不重复记)

`fork_decision` 本身就是结果状态,不需要额外 `status` 字段。

### 完整示例

```yaml
- event: freeze_check
  seq: 6
  rationale: |
    Sub-loop converged; maint_020 + maint_017 built clean across 2 rounds.

  candidate_prefix: [maint_020, maint_017]
  script_gates_passed: 7/7

  skill3_risk:
    level: low
    concerns:
      - maint_017 has 2 weak-confidence prerequisite edges

  fork_decision: FREEZE
  decision_summary: |
    Both objective gates and latent risk align. Weak edges are
    downstream-only.

  newly_frozen: [maint_020, maint_017]
```

---

## 4 · `deferred_to_next_run`(顶层字段)

本轮明确决定**不动**的事项,下轮 Step 1 第一件事就读这部分。

```yaml
deferred_to_next_run:
  - id: cluster_D_maint_027
    proposed_event: dag_update
    target: [maint_027]
    reason: |
      Needs Skill 2 subagent to validate 4-5 cherry-pick SHAs M2 proposed in
      unbound_issues. Walk each SHA from maint_017-end reachability.
    priority: high

  - id: cluster_F_maint_026
    proposed_event: env_refine
    target: [maint_026]
    reason: |
      27 residual_missing despite 33 verified waivers. Observe Round 1
      outcome first before deciding M2 redo vs DAG edit.
    priority: medium
```

字段:`id` / `proposed_event` / `target` / `reason` / `priority` (high/medium/low)。

---

## 设计取舍说明

| 决策 | 理由 |
|---|---|
| `plan.yaml` 单文件 + events 数组 | 人读友好;整轮一眼看完 |
| append-by-read-modify-write | 一轮 events 通常 < 20,原子写避免损坏 |
| 合并 `dag_edit` + `m1_batch` → `dag_update` | 本质同一闭环 |
| 不区分 wave | 一批就是一批;真要先后跑就两个 event |
| 只记 final state,不记 proposed | refine 过程在 narrative 和 M1 subagent log |
| 审计 dispatch 不入账 | Skill 1/2 只读不写 |
| `deferred` 合进 plan.yaml | 一个文件看完整轮决策画面 |
| `surgical` 作为 env_refine 子 mode | 本质是 recipe-only env 刷新,不是新的 top-level action |
| event 字段极简(event + seq + rationale) | 状态由类型字段隐式表达;seq 作为跨文件锚点保留(subagent_log 文件名 / narrative / deferred 引用) |
| `final_ops` 按 op 类型分组,每类只一次 | 一眼看清"这一轮加了几条边/拆了几个 milestone",同类聚合便于审计 |
| `m1_result` 5 个必填 status 字段 + 可选 `applied_refinements` | status 字段最小化;`applied_refinements` 单独承载 M1 dispatch 模式下的边操作以保 ledger fidelity;其他细节去 M1 subagent log |
| M1 dispatch 模式只能追加 edge refinements | edge ops 机械可推导;milestone ops 涉及跨 MS 语义,只能 M3 拍板。partition 靠决策来源、执行阶段和 collision check,不是靠文件物理隔离 |
| `lint_warnings` 仅计数 | 详情在 build_status.json 里;ledger 只承担"是否有红旗"的快速扫视 |
| `env_refine` 去掉 parallel / flags / built / still_failing | 执行参数不入账;`failed_gates: []` 已隐含 built 状态 |
| `freeze_check` 去掉 script/log 路径字段 | 标准化路径(`frozen_check.json` / `subagent_log/skill3_seq{NN}_freeze.md`)由约定承载 |
