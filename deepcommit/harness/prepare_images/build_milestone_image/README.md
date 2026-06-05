# build_milestone_image

为每个 milestone 构建 Docker image，使其测试套件能在 START/END 两个 git 状态下运行。

## 整体架构

```
CLI (main)
  |
  |-- Normal mode:  直接 build
  |-- Refine mode:  先 diagnose -> 过滤 DAG issue -> 只 refine env issue
  +-- Dry-run mode: 只生成 prompt
        |
        v
build_milestones()          <-- 多 milestone 编排（串行 or ThreadPoolExecutor 并行）
        |
        v
build_single_milestone()    <-- 单 milestone 的完整 pipeline
```

## `build_single_milestone` 详细流程

```
输入: milestone_id, milestone_data, work_dir, paths, base_image, ...
                |
                v
+-- Early Exit -------------------------------------------------+
|  Docker image 已存在？                                          |
|    且 build_status.json::built == true？                         |
|      +-- return "validated"                                    |
+----------------------------------------------------------------+
                | image 不存在 or build_status 缺失/失败/refine
                v
+-- Step 1: 生成 Prompt ----------------------------------------+
|  1. 从 metadata.json 读取 repo_src_dirs, test_dir              |
|  2. 从 milestone_data 读取 commits, tag names                  |
|  3. 使用 prompts/base.md.j2 组合 language/repo/validation pack  |
|  4. render_env_setup_prompt() -> Jinja2 组合渲染                 |
|  5. 输出: dockerfiles/{MID}/env_setup_prompt.md                 |
|                                                                 |
|  dry_run -> return "dry_run"                                    |
+----------------------------------------------------------------+
                |
                v
+-- Refine 模式预处理 -----------------------------------------+
|  如果 --refine:                                                |
|    build_refine_prompt() 在 prompt 前面拼接:                    |
|    - 已有的 Dockerfile 路径（让 agent 去读）                    |
|    - diagnosis 报告内容                                         |
+----------------------------------------------------------------+
                |
                v
+-- Retry Loop (max 3 次) -------------------------------------+
|                                                                |
|  +-- Step 2: 调 CLI Agent -------------------------------+    |
|  |  默认: codex exec --model gpt-5.5                     |    |
|  |  可选: claude -p <prompt> --session-id <uuid> ...     |    |
|  |  cwd = work_dir 的上三级目录                           |    |
|  |  timeout 默认 7200s                                    |    |
|  |  同一次 agent retry 内默认最多 resume 3 次              |    |
|  |                                                        |    |
|  |  Agent 在容器外执行, 能:                                |    |
|  |  - 读写 dockerfiles/{MID}/Dockerfile                   |    |
|  |  - 读写 dockerfiles/{MID}/test_config.json             |    |
|  |  - docker build / docker run                           |    |
|  |  - 调用 test runner 脚本                               |    |
|  |                                                        |    |
|  |  失败 -> build_retry_prompt() -> continue              |    |
|  +--------------------------------------------------------+    |
|                     | success                                  |
|                     v                                          |
|  +-- Step 3: 检查构建产物 ------------------------------+    |
|  |  check_build_outputs():                               |    |
|  |  - Dockerfile 存在？                                   |    |
|  |  - test_config.json 存在且 JSON 合法？                 |    |
|  |                                                        |    |
|  |  缺失 -> retry                                        |    |
|  +--------------------------------------------------------+    |
|                     | pass                                     |
|                     v                                          |
|  +-- Step 3.5: Dockerfile Lint --------------------------+    |
|  |  lint_dockerfile() (静态扫 Dockerfile 文本):            |    |
|  |  - no_checkout_file (block)                            |    |
|  |  - no_delete (block)                                   |    |
|  |  - patch_not_committed (block, file-level)             |    |
|  |  - no_src_modify (warn)                                |    |
|  |  - test_file_modify (warn)                             |    |
|  |                                                        |    |
|  |  evaluate_large_src_append() (build 完后基于 git diff): |    |
|  |  - large_src_append (block; 总 +>500 行 / 单文件 +>200) |    |
|  |  - src_function_replaced (block; 单文件 ±50 配对成立)   |    |
|  |  通过 _docker_src_diff_numstat 在 image 内拉            |    |
|  |  pristine_sha vs base_state/end_state 的 diff,          |    |
|  |  经 SrcFileFilter.is_src_file 过滤                      |    |
|  |                                                        |    |
|  |  以上违规一并合入 B_lint_block_clean evidence            |    |
|  +--------------------------------------------------------+    |
|                     | pass                                     |
|                     v                                          |
|  +-- Step 4: 跑测试 + 写 build_status.json --------------+    |
|  |                                                        |    |
|  |  skip_validation -> return "success"                   |    |
|  |                                                        |    |
|  |  4a. 为本轮 validation 分配新的 attempt_N              |    |
|  |                                                        |    |
|  |  4b. run_tests():                                      |    |
|  |      python -m harness.test_runner.                    |    |
|  |        run_milestone_tests {MID}                       |    |
|  |      -> test_results/{MID}/refinement/attempt_N/*      |    |
|  |                                                        |    |
|  |  4c. collect_patched_tests():                          |    |
|  |      -> milestone_patched_tests/{MID}.json             |    |
|  |                                                        |    |
|  |  4d. evaluate_build_status():                          |    |
|  |      - Layer 1: build/run integrity gates              |    |
|  |      - Layer 2: test collection/result quality gates   |    |
|  |      - load_module2_waivers (v2 schema)                |    |
|  |      - verify_bound_waivers (按 reason_code 跑 git    |    |
|  |        log -S / merge-base / cat-file 反向核实)        |    |
|  |      - 通过的 bound_waivers offset 对应 gate           |    |
|  |        (H_patched_completeness / H_error_within_budget)|    |
|  |      - unbound_issues + agent_sufficient 落 raw_metrics|    |
|  |      -> dockerfiles/{MID}/build_status.json            |    |
|  +--------------------------------------------------------+    |
|                     | built == true                            |
|                     v                                          |
|            return "success"                                    |
|                     | built == false +                         |
|                     | module2_acceptance_mode==agent_sufficient|
|                     | + agent_sufficient.value==true           |
|                     v                                          |
|            return "module2_sufficient" (早停, 终态算成功)       |
|                                                                |
+-- retry 时: build_retry_prompt(原始 prompt, error) -----------+
                |
                v 全部 retry 用完
        return "agent_failed" 或 "validation_failed"
```

## Refine 模式

Refine 和 Normal 的区别是: 如果 `dockerfiles/{MID}/Dockerfile` 已存在,
agent 会读取现有 Dockerfile / test_config.json 和 `build_status.json` 的
failed_gates/evidence, 在原地修复。`build_status.json::built` 是唯一跳过
rebuild 的权威信号。

```
existing Dockerfile:
  |-- image exists + build_status.json built=true -> validated, skip
  +-- otherwise -> build_refine_prompt(..., build_status failed_gates/evidence)
```

## Three Exits + module2_acceptance_mode

当 hard gate 在"只改 HOW"的限制下无解时, agent 通过 `module2_waivers.json`
(schema v2) 走三个结构化出口, 详见 `prompts/workflow.md.j2` Phase 4.5:

```
bound_waivers   -> 可 git 验证的具体事实, pipeline 自动核实, 通过则 offset gate
                   reason_codes:
                     - symbol_introduced_later     (-> H_patched_completeness)
                     - nodeid_absent_in_states     (-> H_patched_completeness)
                     - deleted_earlier             (-> H_patched_completeness)
                     - error_signature_dag_caused  (-> H_error_within_budget)

unbound_issues  -> 自然语言描述的 trade-off, 不验证, 不影响 gate, 落 raw_metrics
                   推荐 kinds:
                     - environment_unsupportable   (env 装不上, 弃部分覆盖)
                     - dag_missing_edge_suspect    (DAG 错位症状)
                     - runtime_config_change       (runner 配置改动需 audit)
                     - other                       (其他需文档化的 trade-off)

agent_sufficient -> 终态声明 "我尽力了, 请停"
                   value=true 时必须配至少一条 bound/unbound 条目
```

`config/validation_thresholds.yaml::module2_acceptance_mode` 控制 retry 行为:

| 值 | 行为 |
|---|---|
| `hard_gate` (默认) | 只有 hard gate 全过才早停, agent_sufficient 仅落 raw_metrics 不影响 retry |
| `agent_sufficient` | hard gate 失败但 agent_sufficient.value=true 时早停, 终态算 `module2_sufficient` |

`module2_sufficient` 在 main() ok_statuses 里被算作成功——见
`build_milestone_image.py:1167, 1772-1786`。

## 数据流一览

```
输入:
  work_dir/metadata.json          -> milestone 列表、repo 配置
  work_dir/milestones.csv         -> DAG 诊断用
  work_dir/dependencies.csv       -> DAG 诊断用
  work_dir/testbed/               -> 含 milestone tags 的 git repo
  ../base/dockerfiles/Dockerfile  -> base image 的 Dockerfile（参考用）

Agent 产出:
  work_dir/dockerfiles/{MID}/Dockerfile           -> Docker 镜像定义
  work_dir/dockerfiles/{MID}/test_config.json     -> 测试配置
  work_dir/dockerfiles/{MID}/module2_waivers.json -> Three Exits 声明（可选）
  work_dir/dockerfiles/{MID}/env_setup_prompt.md  -> prompt 存档

验证产出:
  work_dir/dockerfiles/{MID}/build_status.json    -> hard gate 权威结果
  work_dir/test_results/{MID}/refinement/attempt_N/* -> refinement per-attempt 测试结果
  work_dir/milestone_patched_tests/{MID}.json     -> patched tests 匹配结果
  work_dir/_build_runs/{MID}/attempt_N/*          -> append-only 复盘 archive（含 agent/wrapper.log + agent/official/*.jsonl）

最终产出:
  Docker image: {repo_id}/{scenario}/{MID}:latest -> 可用于后续测试执行
```

## 模块文件说明

| 文件 | 职责 |
|------|------|
| `build_milestone_image.py` | 主编排：CLI、retry loop、多 milestone 并行 |
| `render_prompt.py` | 模板渲染：填充变量、生成 retry/refine prompt |
| `diagnosis.py` | 14 个 hard gate 评估、module2_waivers v2 加载与 bound_waiver git 核实、git numstat src diff、跑测试、收集 patched tests。文件后段是 debug-only CLI（错误 symbol 提取、DAG root cause 分类），写 `debug_diagnosis_*.json`，不在 hard gate 路径上 |
| `dockerfile_lint.py` | Dockerfile 静态检查：检测 agent 的违规操作（git checkout file、删文件、改 src 等） |
| `prompts/` | 按语言分的 prompt 模板（Python/Java/Go/Rust/JS）+ Three Exits / HOW vs WHY 红线 |

## 使用方式

```bash
# 构建全部 milestone
python -m deepcommit.harness.prepare_images.build_milestone_image \
    --all --work-dir DATA/harness_workspace/repo_range/scenario

# 并行构建
python -m deepcommit.harness.prepare_images.build_milestone_image \
    --all --parallel 4 --work-dir DATA/harness_workspace/repo_range/scenario

# 只生成 prompt（不调 agent）
python -m deepcommit.harness.prepare_images.build_milestone_image \
    --all --dry-run --work-dir DATA/harness_workspace/repo_range/scenario

# 诊断 + 修复 env 问题（跳过 DAG 问题）
python -m deepcommit.harness.prepare_images.build_milestone_image \
    --all --refine --work-dir DATA/harness_workspace/repo_range/scenario

# 备份现有 Dockerfile 并让 agent 从头生成
python -m deepcommit.harness.prepare_images.build_milestone_image \
    --all --regen-dockerfile --work-dir DATA/harness_workspace/repo_range/scenario

# 跳过 agent, 只对当前 Dockerfile + test_config.json + module2_waivers.json 跑 validation
python -m deepcommit.harness.prepare_images.build_milestone_image \
    M001 --validate-current-only \
    --work-dir DATA/harness_workspace/repo_range/scenario

# 构建特定 milestone
python -m deepcommit.harness.prepare_images.build_milestone_image \
    M001 M003 --work-dir DATA/harness_workspace/repo_range/scenario
```
