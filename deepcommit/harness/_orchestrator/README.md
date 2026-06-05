# 模块 3: Main Agent（Phase 3 顶层编排器）

Main Agent 是 Phase 3 的总调度，输入是 **repo + config + Phase 2 的两个
初始 CSV**，负责把这些起始材料变成一套可用的 testbed + Docker 镜像 +
诊断产出。整个过程是多轮迭代的：每轮都在自己的版本化目录里运行，改动
通过**结构化 CSV ops** 记录，任一轮之间可以**分叉回退**而不丢历史。

---

## 顶层调度图

```text
输入: repo + config + milestones.csv + dependencies.csv + phase2_trial 名字

step 0  ensure_base_image()                                  (模块 0)
        └─ 检查 {repo_id}/base:latest, 缺就子进程调 build_image.py

step 1  bootstrap_initial_iter()                             (建起点 iter)
        └─ 物化 <phase2_trial>/ 目录, 写 .bak + .csv, 建 iteration tree

loop N 轮 ─────────────────────────────────────────────────
│
│  process_iter(current_iter):
│    ① testbed_fix_loop (模块 1 + cherry-pick 修复子循环, 最多 5 次)
│    ② build_milestones (模块 2, 复用现有实现)
│    ③ diagnose_all    (程序化粗判)
│    ④ main_loop LLM   (读全部信号, 出 decision_plan.json)
│
│  if plan.halt:  break
│
│  materialize_iteration(
│    parent = plan.next_iteration.branch_from,   ← 可以是任一祖先 (分叉)
│    csv_ops = plan.next_iteration.csv_ops,
│  )
│
└─────────────────────────────────────────────────────────

输出: DATA/harness_workspace/<repo_range>/ 里一棵迭代树,
     + <phase2_trial>_iteration_tree.json 汇总所有轮次
```

---

## Workspace 目录结构

以 `django_django_4.2_5.2` + `phase2_trial = v1_001_fix` 为例，跑 3 轮后：

```text
DATA/harness_workspace/django_django_4.2_5.2/
│
├── v1_001_fix/                             ← 起点 iter (parent=null)
│   ├── milestones.csv.bak                  ← 不可变, 与 Phase 2 原始完全一致
│   ├── milestones.csv                      ← = .bak + 本轮 stage1 fix ops
│   ├── dependencies.csv.bak
│   ├── dependencies.csv
│   ├── initial_source.json                 ← 记录 Phase 2 原始 CSV 的绝对路径
│   ├── testbed/                            ← stage1 产出 (git repo + 里程碑 tags)
│   ├── metadata.json                       ← stage1 产出
│   ├── dockerfiles/                        ← 模块 2 产出 (per-milestone Dockerfile)
│   ├── test_results/                       ← 模块 2 产出 (测试结果)
│   ├── stage1_fix_log.json                 ← stage1 子循环每次 attempt 的 ops
│   ├── stage1_fix_proposal.json            ← stage1 当前 attempt LLM 的原始输出
│   ├── stage1_fix_prompt_attempt{N}.md     ← stage1 prompt 归档
│   ├── stage1_logs/attempt_*.log           ← build_milestone_testbed.py 的原始 stderr
│   ├── decision_plan.json                  ← Main Agent LLM 的决策 (branch_from + csv_ops)
│   ├── main_loop_prompt_rendered.md        ← Main Agent prompt 归档
│   └── _agent_logs/                        ← Claude CLI 的 stdout/stderr
│
├── v1_001_fix_iter002/                     ← parent = v1_001_fix
│   ├── milestones.csv.bak                  ← = parent.milestones.csv + decision_plan.csv_ops
│   ├── milestones.csv                      ← = .bak + 本 iter stage1 fix ops
│   └── ...                                 (结构同上)
│
├── v1_001_fix_iter003/                     ← parent 可能是 v1_001_fix (分叉!), 不是 iter002
│   └── ...
│
└── v1_001_fix_iteration_tree.json          ← 这个 initial 的迭代树汇总
```

**关键不变量**：

- `*.csv.bak` 是 read-only（`chmod 0444`），保证"stage1 跑之前的状态"永远可查
- `DATA/result/<phase2_trial>/` 下的 Phase 2 原始 CSVs **永不被 Main Agent 触碰**
- 同一个 `repo_range` 可以有多个 initial（多个 phase2_trial），每个独立一棵树，互不干扰

---

## 每个 iter 的 CSV 怎么来的

**起点 iter**（`v1_001_fix/`）：

```text
v1_001_fix/milestones.csv
    = DATA/result/<phase2_trial>/milestones.csv           (Phase 2 原始, 拷贝到 .bak)
    + apply(v1_001_fix/stage1_fix_log.applied_ops)        (stage1 cherry-pick 修复)
```

**非起点 iter**（`v1_001_fix_iter002/`）：

```text
v1_001_fix_iter002/milestones.csv
    = parent 的 milestones.csv                            (parent 末态)
    + apply(parent/decision_plan.next_iteration.csv_ops)  (Main Agent 规划)
    + apply(v1_001_fix_iter002/stage1_fix_log.applied_ops) (本轮 stage1 修复)
```

三种 diff 对应三种 LLM 活动：

| 看什么 | diff 命令 |
|--------|----------|
| Phase 2 → 起点 iter：**仅 stage1 修复** | `diff v1_001_fix/*.csv.bak v1_001_fix/*.csv` |
| iter N → iter N+1 **规划动作** | `diff iter_N/*.csv iter_{N+1}/*.csv.bak` |
| iter N+1 **stage1 修复** | `diff iter_{N+1}/*.csv.bak iter_{N+1}/*.csv` |

---

## CSV ops 目录（Phase A）

全部 CSV 改动都走 ops，不允许直接改文件。每条 op **必须带 `rationale`**。

```text
add_edge        {source_id, target_id, rationale}
remove_edge     {source_id, target_id, rationale}
move_commit     {commit_sha, from_milestone_id, to_milestone_id, rationale}
drop_commit     {commit_sha, milestone_id, rationale}
drop_milestone  {id, rationale}
```

编排器在应用时会做文件级校验（不论 op 来自 Main Agent 还是 stage1 子 agent）：

- `add_edge`: 检查 source/target 存在、不自环、不重复、**不成环**
- `move_commit` / `drop_commit`: 检查 SHA 在指定 milestone 里、不模糊匹配
- `drop_milestone`: 级联删除所有涉及它的边

被拒绝的 op 会附带 `_reject_reason` 写入日志，不中断其他 op。

Phase B（以后）：`merge_milestones` / `split_milestone`。

---

## Stage1 Cherry-pick 修复子循环

`build_milestone_testbed.py` 失败时不是硬停，而是：

```text
attempt 1..5:
  rc = subprocess build_milestone_testbed.py [--incremental if attempt > 1]
  if rc == 0: 成功, 退出
  else:
    LLM(cherry_pick_fix.tmpl) 读 stderr + 当前 CSVs + 之前 attempts 的 ops
                              → 输出 ops 列表到 stage1_fix_proposal.json
    apply_csv_ops() 应用 (含 dedup/cycle/SHA 校验)
    如果全被拒 → 退出
```

每次 attempt 的 `{stderr, proposed_ops, applied_ops, rejected_ops}` 都累积写到
`stage1_fix_log.json`。这个日志会在 Main Agent LLM 复核时作为输入。

---

## Main Agent LLM 复核 + 分叉

每轮 iter 末尾，LLM 读完所有信号后输出 `decision_plan.json`：

```json
{
  "iteration": "v1_001_fix",
  "next_iteration": {
    "branch_from": "v1_001_fix",
    "csv_ops": [
      {
        "op": "add_edge",
        "source_id": "milestone_http",
        "target_id": "milestone_core_handlers",
        "rationale": "Verified via git log -S: BaseHandler defined in 4ea230 of milestone_http, used by milestone_core_handlers."
      }
    ]
  },
  "rejected_suggestions": [
    {
      "programmatic_claim": "dag_missing_edge M_a → M_b",
      "reason": "False positive: `json` is stdlib, not defined in M_a."
    }
  ],
  "skip_milestones": [],
  "halt": false,
  "halt_reason": "",
  "notes": "",
  "summary": "Verified 1 edge, rejected 1 false positive."
}
```

### 关键字段

| 字段 | 编排器行为 |
|------|-----------|
| `next_iteration.branch_from` | 下一轮从哪个 iter 拷 CSV。**默认是当前 iter；设成祖先 = 分叉回退** |
| `next_iteration.csv_ops` | 套在 `branch_from` 的 CSV 上形成下一轮的起点 |
| `halt` | `true` 就停外层循环 |
| `skip_milestones` | 暂未生效，为后续过滤预留 |

### 分叉的典型用法

- 当前 iter **比 parent 更差** → LLM 不要在坏基础上继续堆改动，而是把
  `branch_from` 设成更早的健康祖先，带上不同 ops 重试
- 当前 iter **跟 parent 一样** → 可能动作没用，LLM 要么换 ops 要么 halt

历史喂给 LLM 的方式：prompt 渲染时把整个 `iteration_tree.json` 摘成
`history_summary` 塞进去（每 iter 一行 `{name, parent, health, status}`），
让 LLM 有全局视角。

---

## CLI 用法

### 最小调用

```bash
python3 -m deepcommit.harness.orchestrator.main_agent \
    --repo-path DATA/github_data/repos/django_django \
    --config config/django_django_4.2_5.2.yaml \
    --repo-range django_django_4.2_5.2 \
    --phase2-trial v1_001_fix
```

这会从 `DATA/result/v1_001_fix/` 找 CSVs（按惯例路径），跑 3 轮默认迭代。

### 显式指定 CSV 路径 + 调参

```bash
python3 -m deepcommit.harness.orchestrator.main_agent \
    --repo-path DATA/github_data/repos/django_django \
    --config config/django_django_4.2_5.2.yaml \
    --repo-range django_django_4.2_5.2 \
    --phase2-trial v1_001_fix \
    --milestones-csv path/to/milestones.csv \
    --dependencies-csv path/to/dependencies.csv \
    --max-iterations 5 \
    --parallel 4 \
    --language python
```

### 已有 base image 时跳过模块 0

```bash
python3 -m deepcommit.harness.orchestrator.main_agent \
    ... \
    --skip-base-image
```

（一般不用显式加，因为 `ensure_base_image` 已经会自动检测和复用。）

---

## 参数表

### 核心输入

| 参数 | 必填 | 说明 |
|------|------|------|
| `--repo-path` | ✓ | 源 git repo，e.g. `DATA/github_data/repos/django_django` |
| `--config` | ✓ | repo config YAML |
| `--repo-range` | ✓ | workspace 子目录名，e.g. `django_django_4.2_5.2` |
| `--phase2-trial` | ✓ | Phase 2 trial 名；也是起点 iter 的目录名 |
| `--milestones-csv` | — | 默认 `DATA/result/<phase2_trial>/milestones.csv` |
| `--dependencies-csv` | — | 默认 `DATA/result/<phase2_trial>/dependencies.csv` |
| `--workspace-root` | — | 默认 `DATA/harness_workspace` |

### 循环控制

| 参数 | 默认 | 说明 |
|------|------|------|
| `--max-iterations` | 3 | 最多跑几轮 iter |
| `--skip-base-image` | off | 不执行模块 0 检测/构建 |

### 模块 2 透传

| 参数 | 默认 |
|------|------|
| `--parallel` / `-p` | 1 |
| `--max-retries` | 3 |
| `--language` | python |
| `--test-framework` | pytest |
| `--module2-timeout` | 1800 |

### Agent 控制

| 参数 | 默认 | 说明 |
|------|------|------|
| `--model` | claude-opus-4-6 | 所有 LLM 调用共用 |
| `--stage1-fix-timeout` | 600 | stage1 单次 fix agent 超时 (秒) |
| `--testbed-subprocess-timeout` | 3600 | `build_milestone_testbed.py` 单次调用超时 |
| `--llm-review-timeout` | 600 | Main Agent LLM 复核超时 |

---

## 文件索引

| 文件 | 作用 |
|------|------|
| `main_agent.py` | CLI + 外层循环 (`run_main_agent`) |
| `main_agent.py::_process_iteration` | 单 iter 处理 (stage1→module2→diagnose→review) |
| `main_agent.py::_llm_review` | 调 Main Agent LLM + 解析 `decision_plan.json` |
| `ensure_base_image.py` | base image 检测/自动构建（reuse + verify 策略，不是 builder） |
| `testbed_fix_loop.py::run_stage1_with_fix_loop` | 模块 1 testbed 构建 + cherry-pick 修复子循环 |
| `iteration_manager.py::IterationTree` | 迭代树状态 (load/save/best-so-far) |
| `iteration_manager.py::bootstrap_initial_iter` | 起点 iter 物化 |
| `iteration_manager.py::materialize_iteration` | 从 parent + ops 物化新 iter |
| `csv_ops.py::apply_csv_ops` | 5 个 op 的应用 + 文件级校验 |
| `prompts/cherry_pick_fix.tmpl` | stage1 子 agent prompt |
| `prompts/main_loop.tmpl` | Main Agent 复核 prompt |

### 被复用的现有组件

- `deepcommit.harness.prepare_images.build_milestone_image.build_milestone_image`
  `build_milestones()` / `infer_paths_from_work_dir()` / `discover_milestones()` / `ClaudeAgentRunner`
- `deepcommit.harness.prepare_images.build_milestone_image.diagnosis`
  `diagnose_all()`
- `deepcommit.harness.prepare_repo.build_milestone_testbed` (CLI, subprocess)
- `deepcommit.harness.prepare_images.image_builder.build_image` (CLI, subprocess)

---

## DAG 方向约定

**`dependencies.csv` 里一行 `source_id, target_id` 的含义：**

> source 指向 target，source 是 target 的 prerequisite / 父节点，target 依赖 source。

即：箭头方向 = 执行顺序 = 拓扑序从左到右。拓扑排序 (`build_milestone_testbed.py::topological_sort`) 就是按这个含义把 source 排在 target 前面的。

`MissingEdge`（`diagnose_all` 产出的缺边报告）用**同一套** source/target 命名：

- `source_milestone` = 符号定义所在的 milestone（前置）
- `target_milestone` = 出错的 milestone（后置）
- `source_commit` = 源头 commit（定义处）
- `target_commit` = 目标 commit（使用处）

把一条 `MissingEdge` 写成 `add_edge` op：`source_id = source_milestone,
target_id = target_milestone`（字段名和语义都一一对应）。下游所有代码
（csv_ops、topological_sort、cycle check）都按 `source_id / target_id` 字面
含义处理。

---

## 目前不做（后续增强）

- ❌ **Phase B ops**：`merge_milestones` / `split_milestone`（涉及 commit 重分配，更复杂）
- ❌ **skip_milestones 生效**：schema 里有字段，但编排器还没按它过滤 Module 2
- ❌ **BaseStage 整合**：目前是独立脚本，没接入 `pipeline/stages_harness/` 体系
  也没在 `harness_pipeline.yaml` 里启用 stage 2
- ❌ **旧 iter 磁盘清理**：每轮全量 copy testbed，长期跑占磁盘；未来可以用 git
  worktree 或 TTL 清理
- ❌ **跨 iter 记忆**：每次 LLM 调用是独立 session；依赖 prompt 里注入的
  `history_summary` 提供连续性
