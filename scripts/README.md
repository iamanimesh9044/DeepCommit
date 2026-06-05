# Scripts Quick Guide

> 便捷的 shell 脚本，用于快速执行常见任务。

**注意事项：**
- 所有脚本需要在项目根目录下运行
- 确保 `DATA/` 目录存在（或设置 `DEEPCOMMIT_DATA_ROOT` 环境变量）
- Trial 工作目录在 `DATA/trial_space/<trial_name>/`


## 配置生成工具

### detect_test_dirs.py - 自动检测测试目录

在处理新数据集时，自动检测仓库的测试目录配置，避免手动配置遗漏。

#### 基本用法

```bash
# 方式 1：仅静态分析（快速）
python scripts/detect_test_dirs.py DATA/github_data/repos/apache_dubbo

# 方式 2：结合 Git 历史分析（推荐，更准确）
python scripts/detect_test_dirs.py DATA/github_data/repos/apache_dubbo \
    --config config/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6.yaml

# 方式 3：直接指定参数
python scripts/detect_test_dirs.py DATA/github_data/repos/apache_dubbo \
    --start-tag dubbo-3.3.3 --end-tag dubbo-3.3.6 \
    --main-branch 3.3
```

#### 参数说明

| 参数 | 说明 |
|------|------|
| `repo_path` | 仓库路径（必填） |
| `--config` | 配置文件路径，自动读取 `main_branch` 等配置 |
| `--start-tag` | 起始 tag |
| `--end-tag` | 结束 tag |
| `--main-branch` | 主分支名称（默认：main） |
| `--format` | 输出格式：`yaml`（默认）或 `json` |

#### 输出示例

```yaml
# Auto-detected test configuration
# Languages: java
# Git range: dubbo-3.3.3..dubbo-3.3.6
test_dirs:
  - "**/*Test.java"
  - "**/*Tests.java"
  - "**/src/test/**"
  - "**/test/**"
  - "dubbo-cluster/src/test/**" # from git history
  - "dubbo-test/**" # from git history

repo_src_dirs:
  - dubbo-cluster
  - dubbo-common
  ...
```

#### Tag 拓扑说明

`detect_test_dirs.py` 不再读取 `use_branch_out_model` 之类的 branch-model 开关。  
DeepCommit 的预处理主流程会在有本地仓库时自动处理 branch-out / release-branch 场景；这个脚本只按给定 tag 范围做历史扫描。

#### 工作原理

1. **语言检测**：根据 `pom.xml`、`package.json`、`Cargo.toml` 等文件判断语言
2. **默认模式**：根据语言添加标准测试目录模式（如 Java 的 `**/src/test/**`）
3. **特殊目录扫描**：发现顶级目录中的 `*-test`、`*-demo` 等目录
4. **Git 历史分析**（可选）：分析两个 tag 之间实际变更的测试文件，补充遗漏的模式

#### 设计原则

> **宁可宽泛，不可遗漏**

- 宽泛的代价：可能多匹配一些非测试文件（几乎无害）
- 遗漏的代价：测试文件被当成源代码变更，导致数据错误 + 重跑 pipeline


## 预处理命令 (Preprocessing)

### 0. 统一预处理脚本

```bash
scripts/run_preproc.sh <command> <repo_spec> [options]
```

统一的预处理脚本，支持数据收集、CodeMaat 分析和 DAG 生成。

**repo_spec 格式：** `owner_repo:start_end`
- `owner_repo`: GitHub 仓库所有者和名称（用下划线连接）
- `start_end`: 起始和结束标签（用下划线连接）

**可用命令：**
- `collect` - 收集 GitHub 数据（releases、PRs、issues、commits）
- `codemaat` - 生成 CodeMaat 分析日志
- `dag` - 生成 commit 依赖关系图（DAG）
- `all` - 按顺序运行所有步骤（collect → codemaat → dag）

#### 基本用法

```bash
# 运行所有预处理步骤（推荐）
scripts/run_preproc.sh all urllib3_urllib3:2.0.6_2.3.0

# 只收集数据
scripts/run_preproc.sh collect urllib3_urllib3:2.0.6_2.3.0

# 只生成 DAG
scripts/run_preproc.sh dag urllib3_urllib3:2.0.6_2.3.0

# 只生成 CodeMaat 日志
scripts/run_preproc.sh codemaat urllib3_urllib3:2.0.6_2.3.0
```

#### Collect 命令示例

```bash
# 使用自定义 workers
scripts/run_preproc.sh collect urllib3_urllib3:2.0.6_2.3.0 --workers 8

# 过滤源码目录（只收集修改了这些目录的 commits）
scripts/run_preproc.sh collect urllib3_urllib3:2.0.6_2.3.0 --repo-src-dirs src/ lib/

# 覆盖已有数据
scripts/run_preproc.sh collect urllib3_urllib3:2.0.6_2.3.0 --overwrite

# 跳过 commit 验证（更快）
scripts/run_preproc.sh collect urllib3_urllib3:2.0.6_2.3.0 --no-verify

# 详细模式
scripts/run_preproc.sh collect urllib3_urllib3:2.0.6_2.3.0 -v
```

**Collect 可选参数：**
- `--workers <n>`: 并发 worker 数量（默认：4）
- `--repo-src-dirs <dirs...>`: 源码目录过滤列表
- `--overwrite`: 覆盖已有数据
- `--no-verify`: 跳过 commit 验证
- `-v, --verbose`: 详细日志

#### DAG 命令示例

```bash
# 基本用法
scripts/run_preproc.sh dag urllib3_urllib3:2.0.6_2.3.0

# 覆盖已有结果并生成图像
scripts/run_preproc.sh dag urllib3_urllib3:2.0.6_2.3.0 --overwrite --generate-image

# 自定义上下文行数
scripts/run_preproc.sh dag urllib3_urllib3:2.0.6_2.3.0 --context-lines 5 --patch-context-lines 5

# 源码目录过滤
scripts/run_preproc.sh dag urllib3_urllib3:2.0.6_2.3.0 --repo-src-dirs src/ lib/
```

**DAG 可选参数：**
- `--overwrite`: 覆盖已有输出文件
- `--generate-image`: 生成 PNG 图像（需要 graphviz）
- `--context-lines <n>`: Git blame 上下文行数（默认：3）
- `--patch-context-lines <n>`: Git apply 上下文行数（默认：3）
- `--repo-src-dirs <dirs...>`: 源码目录过滤
- `--follow-depth <1-3>`: 复制检测深度（默认：2）
- `--no-verify-with-patch`: 禁用 git apply 验证（更快但不够准确）
- `--no-reduce-transitive`: 不移除传递性冗余边
- `--use-pygit2`: 使用 pygit2 库（更快，需要安装）
- `-v, --verbose`: 详细日志

#### CodeMaat 命令示例

```bash
# 基本用法
scripts/run_preproc.sh codemaat urllib3_urllib3:2.0.6_2.3.0

# 指定源码目录
scripts/run_preproc.sh codemaat urllib3_urllib3:2.0.6_2.3.0 --repo-src-dir src/

# 多个源码目录
scripts/run_preproc.sh codemaat urllib3_urllib3:2.0.6_2.3.0 --repo-src-dir src/ --repo-src-dir lib/

# 指定主分支
scripts/run_preproc.sh codemaat urllib3_urllib3:2.0.6_2.3.0 --main-branch master
```

**CodeMaat 可选参数：**
- `--repo-src-dir <dir>`: 源码目录（可多次指定）
- `--main-branch <branch>`: 主分支名称（默认：main）
- `--overwrite`: 覆盖已有输出文件
- `-v, --verbose`: 详细日志

#### 通用选项

所有命令都支持：
- `--output-dir <path>`: 自定义输出目录
- `-v, --verbose`: 详细日志
- `--overwrite`: 覆盖已有文件

#### 环境要求

**Collect/All 命令：**
- 必须设置 `GITHUB_TOKEN` 环境变量
- **本地仓库（推荐）：** 提前克隆到 `DATA/github_data/repos/<owner_repo>`
  ```bash
  cd DATA/github_data/repos
  git clone https://github.com/urllib3/urllib3.git urllib3_urllib3
  ```

**DAG/CodeMaat 命令：**
- 必须有本地仓库（在 `DATA/github_data/repos/<owner_repo>`）

#### 输出目录结构

**默认输出：** `DATA/github_data/preprocessed/<owner_repo>_<start_end>/`

```
urllib3_urllib3_2.0.6_2.3.0/
├── raw_data/              # Collect 命令输出
│   ├── main_commits.jsonl
│   ├── pull_requests.jsonl
│   ├── issues.jsonl
│   └── releases.jsonl
├── main_commits.jsonl     # Collect 命令输出（过滤后）
├── pull_requests.jsonl    # Collect 命令输出（过滤后）
├── issues.jsonl           # Collect 命令输出（过滤后）
├── releases.jsonl         # Collect 命令输出（过滤后）
├── patches/               # Collect 命令输出（commit patches）
│   └── <7-char-sha>.patch
├── metadata.json          # Collect 命令输出 metadata
├── commit_dag.json        # 标准化的 DAG（7字符 SHA）
├── codemaat_log.txt       # CodeMaat 命令输出
└── dag/                   # DAG 命令详细输出
    ├── commit_dag_v3_ctx_3_pctx_3_enhanced.json
    ├── commit_dag_v3_ctx_3_pctx_3_enhanced.dot
    └── commit_dag_v3_ctx_3_pctx_3_enhanced.png  (可选)
```


## 1. 运行试验 (Run Trial) [**Deprecated!!**]

```bash
scripts/run_trial.sh --prompt-file <file> --repo-id <repo> --trial-name <name> [options]
```

**必填参数：**
- `--prompt-file`: 提示词模板文件，相对于 `deepcommit/prompt/` 目录
  - 示例：`example.tmpl` 或 `dev/example.tmpl`
- `--repo-id`: 仓库标识，格式：`owner_repo:start_end`
  - 示例：`urllib3_urllib3:2.0.6_2.3.0`
- `--trial-name`: 试验名称

**可选参数：**
- `--agents`: agent 类型（默认：`claudecode`）
- `--max-retries`: 失败重试次数（默认：`2`）
- `--refresh-views`: 完成后刷新数据库视图（传 `--refresh-views`）

**输出目录：** `DATA/result/<trial_name>/`


## 1.5. 运行多阶段 Pipeline (Multi-Stage Pipeline)

```bash
scripts/run_multi_stage.sh --repo-id <repo> --trial-name <name> [options]
```

多阶段 pipeline 支持按顺序执行多个独立 stages（如 seed discovery → milestone consolidation → dependency inference），每个 stage 都有独立的 agent、输出和 checkpoint。

### 基本用法

```bash
# 首次运行完整 pipeline
./scripts/run_multi_stage.sh \
  --repo-id urllib3_urllib3:2.0.6-2.3.0 \
  --trial-name test_multi_stage_v1

# 使用自定义配置文件
./scripts/run_multi_stage.sh \
  --repo-id urllib3_urllib3:2.0.6-2.3.0 \
  --trial-name my_trial \
  --config-file custom_pipeline.yaml
```

### 必填参数

- `--repo-id`: 仓库标识，格式：`owner_repo:start_end`
  - 示例：`urllib3_urllib3:2.0.6-2.3.0`
- `--trial-name`: 试验名称

### 可选参数

- `--config-file`: Pipeline 配置文件（默认：`multi_stage_pipeline.yaml`）
  - 配置文件位于 `deepcommit/pipeline/configs/`
- `--agents`: Agent 类型，逗号分隔（默认：`claudecode`）
- `--max-retries`: 每个 stage 的最大失败重试次数（默认：`2`）
- `--allow-rerun`: 允许复用已存在的 trial 名称，跳过自动递增
  - 用于修复错误后从 checkpoint 继续执行
- `--rerun-from-stage`: 从指定 stage 重新运行 pipeline
  - 创建新 workspace，自动复制已完成 stages 的输出
- `--rerun-postproc`: 只重新运行 postprocessing 阶段（Phase 3, 4, 5）
  - 跳过 agent execution stages，直接运行 enrichment、analysis 和 upload
  - 自动从 `result/stage*/` 恢复必要的全局文件（如 milestones.csv）

### Checkpoint 与恢复

Pipeline 使用 checkpoint 机制追踪执行状态。如果某个 stage 失败，修复问题后可以使用 `--allow-rerun` 从断点继续：

```bash
# 首次运行（失败在 stage2）
./scripts/run_multi_stage.sh \
  --repo-id urllib3_urllib3:2.0.6-2.3.0 \
  --trial-name test_trial

# 修复问题后，从 checkpoint 继续（复用同名 trial）
./scripts/run_multi_stage.sh \
  --repo-id urllib3_urllib3:2.0.6-2.3.0 \
  --trial-name test_trial \
  --allow-rerun
```

**Checkpoint 文件位置：** `DATA/result/<trial_name>/logs/checkpoint.json`

### Rerun Postprocessing（重跑后处理阶段）

当 agent execution stages (Stage 1-3) 已经完成，但 postprocessing 阶段（enrichment、analysis、upload）失败或需要修改时，使用 `--rerun-postproc`：

```bash
# 重新运行 postprocessing（Phase 3, 4, 5）
./scripts/run_multi_stage.sh \
  --repo-id urllib3_urllib3:2.0.6-2.3.0 \
  --trial-name test_trial \
  --rerun-postproc
```

**行为说明：**
1. 验证 Phase 2 (agent execution) 已完成
2. 清理 Phase 3, 4, 5 的输出文件和 checkpoint 状态
3. 从 `result/stage*/` 恢复必要的全局文件（如 `milestones.csv`）
4. 按顺序执行：
   - Phase 3: Milestone enrichment（添加 LOC、timing 等元数据）
   - Phase 4: Analysis & evaluation（7 种综合分析）
   - Phase 5: Upload & archive（上传到 SQLite）

**使用场景：**
- Stage outputs 已完成，但 enrichment 或 analysis 失败
- 修改了 analysis 脚本，需要重新运行分析
- 手动修复了某些数据文件（如 milestones.csv）
- 需要重新生成 analysis 报告

**注意事项：**
- 必须确保 Phase 2 所有 stages 已完成
- 会清理并重新生成 `result/analysis/` 目录
- 不会重新运行 agent，只运行 Python 脚本处理

### Rerun From Stage（从指定 Stage 重跑）

当你想从某个 stage 重新开始执行（例如调试某个 stage 或尝试不同参数），使用 `--rerun-from-stage`：

```bash
# 从 stage2 重新运行（自动复制 stage1 的输出）
./scripts/run_multi_stage.sh \
  --repo-id urllib3_urllib3:2.0.6-2.3.0 \
  --trial-name test_trial \
  --rerun-from-stage stage2_milestone_consolidation
```

**行为说明：**
1. 创建新 workspace：`test_trial_rerun_stage2`（自动命名）
2. 从源 trial 复制已完成 stages 的输出：
   - `workspace/stage_id/output/` → 新 workspace
   - `result/stage_id/` → 新 result 目录
3. 创建初始 checkpoint，标记前置 stages 为 completed
4. 从指定 stage 开始执行所有后续 stages

**使用场景：**
- 调试某个 stage 的 prompt 或逻辑
- 尝试不同的 agent 参数
- 验证 stage 间的数据传递

**注意事项：**
- 源 trial 的前置 stages 必须全部完成
- 不能从第一个 stage 重跑（使用正常运行即可）
- 如果新 workspace 已存在，会自动添加序号（如 `_rerun_stage2_001`）

### 输出目录结构

**Workspace（工作目录）：** `DATA/trial_space/<trial_name>/`

```
test_trial/
├── deepcommit/              # 代码快照
├── repo_data/               # 仓库链接（symlink）
├── proc_data/               # 预处理数据链接（symlink）
├── result/                  # Result 目录链接（symlink）
├── stage1_seed/             # Stage 1 工作目录
│   ├── input/               # 输入数据（symlinks + 预处理生成）
│   ├── output/              # Stage 输出
│   ├── logs/                # Agent 日志
│   └── trial.mcp.json       # MCP 工具配置
├── stage2_mstone/           # Stage 2 工作目录
│   └── ...
└── stage3_depend/           # Stage 3 工作目录
    └── ...
```

**Result（结果目录）：** `DATA/result/<trial_name>/`

```
test_trial/
├── logs/
│   ├── pipeline.log                  # Pipeline 执行日志
│   ├── checkpoint.json               # Checkpoint 状态
│   └── claude-conversation-*.md      # Agent 对话日志
├── stage1_seed/                      # Stage 1 归档输出
│   ├── agent_artifacts.json
│   └── seed_groups.json
├── stage2_mstone/                    # Stage 2 归档输出
│   ├── agent_artifacts.json
│   ├── milestones.csv
│   └── dependency_proposals.csv
├── stage3_depend/                    # Stage 3 归档输出
│   ├── agent_artifacts.json
│   └── dependencies.csv
├── milestones.csv                    # 全局输出（最终版本）
├── dependencies.csv                  # 全局输出（最终版本）
├── analysis/                         # 分析结果（Phase 4）
│   ├── milestone_coverage_analysis.json
│   ├── milestone_balance_analysis.json
│   ├── dag_validation.json
│   ├── dag_parallelism_analysis.json
│   ├── milestone_partitioning_validation.json
│   ├── milestone_conflicts_analysis.json
│   └── dag_dependency_analysis.json
└── meta.json                         # Trial 元数据
```

### Pipeline 执行流程

Multi-stage pipeline 分为 5 个阶段：

1. **Phase 0 - Global Sanity Check**: 验证环境和输入数据
2. **Phase 1 - Global Preprocessing**: 全局预处理（当前跳过）
3. **Phase 2 - Multi-Stage Agent Execution**: 按顺序执行各个 stages
   - 每个 stage 独立运行 agent，生成输出
   - 自动链接前序 stage 的输出到当前 stage 的 input
   - 支持 checkpoint 和失败恢复
4. **Phase 3 - Global Post-Processing**: Milestone enrichment（添加 LOC、timing 等元数据）
   - 自动从 `result/stage*/` 恢复缺失的全局文件（rerun 场景）
5. **Phase 4 - Analysis & Evaluation**: 运行 7 种综合分析
6. **Phase 5 - Upload & Archive**: 上传到 SQLite 数据库

### 常见问题

**Q: 如何查看 pipeline 进度？**

查看 pipeline 日志：
```bash
tail -f DATA/result/<trial_name>/logs/pipeline.log
```

**Q: 某个 stage 失败了怎么办？**

1. 查看该 stage 的 agent 日志：`DATA/result/<trial_name>/logs/claude-conversation-*.md`
2. 修复问题（代码、prompt、数据等）
3. 使用 `--allow-rerun` 从 checkpoint 继续

**Q: 如何只重跑某个 stage？**

使用 `--rerun-from-stage` 参数，会创建新 trial 并复制前置 stages 的输出。

**Q: Stages 已完成，只想重新运行 analysis 怎么办？**

使用 `--rerun-postproc` 参数。这会跳过 agent execution，只重新运行 Phase 3（enrichment）、Phase 4（analysis）和 Phase 5（upload）。适用于以下场景：
- 修改了 analysis 脚本或配置
- 手动修复了数据文件后需要重新生成报告
- Postprocessing 阶段失败但 stages 输出正常

**Q: Rerun 时是否会复制所有数据？**

只复制必要的输出：
- ✅ 复制：`stage_id/output/` (workspace) 和 `result/stage_id/` (result)
- ❌ 不复制：`stage_id/input/`（会自动重建 symlinks）
- ❌ 不复制：`stage_id/logs/`（会生成新日志）
- ❌ 不复制：`stage_id/trial.mcp.json`（会重新生成）


## Harness 工具

### build_milestone_stats.py - Milestone 统计报告

从 harness workspace 的 classification 文件生成 milestone 统计报告（TSV 格式）。

**功能：**
1. 解析每个 milestone 的 `*_classification.json`，提取测试分类统计
2. 根据 `dependencies.csv` 进行拓扑排序
3. 从 `attempt_*/start_summary.json` 和 `end_summary.json` 提取错误数量
4. 从日志扫描环境问题提示（OOM、端口占用等）

#### 基本用法

```bash
# 基本用法
python scripts/build_milestone_stats.py \
    --trial-dir DATA/harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_001/

# 自定义输出文件名
python scripts/build_milestone_stats.py --trial-dir <path> --output stats.tsv

# 跳过环境提示检测（更快）
python scripts/build_milestone_stats.py --trial-dir <path> --no-env-hints
```

#### 参数说明

| 参数 | 说明 |
|------|------|
| `--trial-dir` | Trial 目录，包含 `milestones.csv`、`dependencies.csv` 和 `test_results/` |
| `--output` | 输出文件名（默认：`milestone_stats.tsv`，相对于 trial-dir） |
| `--no-env-hints` | 跳过环境提示扫描 |

#### 输出列

| 列名 | 说明 |
|------|------|
| `mstone_id` | Milestone ID |
| `pass_to_pass` | 测试前后都通过 |
| `pass_to_fail` | 测试从通过变为失败 |
| `pass_to_skip` | 测试从通过变为跳过 |
| `fail_to_pass` | 测试从失败变为通过 |
| `fail_to_fail` | 测试前后都失败 |
| `fail_to_skip` | 测试从失败变为跳过 |
| `skip_to_pass` | 测试从跳过变为通过 |
| `skip_to_fail` | 测试从跳过变为失败 |
| `skip_to_skip` | 测试前后都跳过 |
| `new_tests` | 新增测试数 |
| `removed_tests` | 移除测试数 |
| `total_before` | 变更前测试总数 |
| `total_after` | 变更后测试总数 |
| `flaky_total` | Flaky 测试总数 |
| `flaky_in_start` | 起始时的 flaky 测试 |
| `flaky_in_end` | 结束时的 flaky 测试 |
| `attempts` | 合并的 attempt 数量 |
| `errors_start` | 起始测试错误数 |
| `errors_end` | 结束测试错误数 |
| `env_hints` | 环境问题提示数 |

#### 输入目录结构

```
<trial_dir>/
├── milestones.csv              # Milestone 列表
├── dependencies.csv            # Milestone 依赖关系
└── test_results/
    ├── M001/
    │   ├── M001_classification.json   # 分类结果
    │   ├── attempt_1/
    │   │   ├── start_summary.json     # 起始测试摘要
    │   │   ├── end_summary.json       # 结束测试摘要
    │   │   ├── start_default.log      # 起始日志（用于 env hints）
    │   │   └── end_default.log        # 结束日志
    │   └── attempt_2/...
    └── M002/...
```


## 2. 后处理 (Post-processing)

```bash
scripts/run_postproc.sh [subcommand] <trial_name> [repo_id] [options]
```

统一的后处理脚本，支持 enrichment（添加元数据）、analysis（综合分析）和 fix（修复依赖）。

**子命令：**
- `analyze` - 仅运行综合 DAG 分析（Phase 4）
- `enrich` - 仅为 milestones 添加 LoC、timing 等元数据（Phase 3）
- `all` - 运行 enrich + analyze（默认，不指定子命令时）
- `fix` - 修复 dependencies（添加缺失、移除冗余）

**示例：**
```bash
# 运行全部后处理（enrich + analyze）
scripts/run_postproc.sh my_trial urllib3_urllib3:2.0.6_2.3.0

# 仅运行分析
scripts/run_postproc.sh analyze my_trial --verbose

# 仅运行 enrichment
scripts/run_postproc.sh enrich my_trial

# 修复依赖关系（dry-run 模式预览）
scripts/run_postproc.sh fix my_trial --dry-run

# 实际修复依赖
scripts/run_postproc.sh fix my_trial
```

**可选参数：**
- `-v, --verbose`: 详细日志（Debug level）
- `--overwrite`: 强制重新分析（忽略已有结果）
- `--dry-run`: 仅输出到控制台，不保存文件（用于测试）
- `--result-root <path>`: 指定 trial 目录的根路径（默认：`DATA/result/`）
- `--repo-src-dirs <dirs...>`: 源码目录过滤
- `--exclude <patterns...>`: 排除的 glob 模式

**注意：** `repo_id` 是可选的，可以从 `trial_name` 自动推断。

**使用其他目录（如 harness_workspace）：**
```bash
# 指定 harness_workspace 下的 trial
scripts/run_postproc.sh analyze baseline_001 django_django:4.2_5.2 \
    --result-root DATA/harness_workspace/django_django_4.2_5.2 --dry-run
```

**输出目录：** `<result_root>/<trial_name>/analysis/`


## 3. 载入会话 (Attach Session)

```bash
scripts/attach_session.sh <trial_name>
```

恢复到指定试验的 Claude Code 会话，继续对话或查看状态。

**提示：** 支持试验名称的部分匹配（后缀匹配）


## 4. 查找对话日志 (Find Conversation Logs)

```bash
scripts/find_conversation.sh <trial_name>
```

显示试验的 **Markdown 格式对话日志文件路径**，包含完整的 User 和 Claude 对话记录，以及所有 Claude 的 工具调用及参数

**输出示例：**
```
DATA/result/mcp_test_008/logs/claude-conversation-2025-10-12-16fb7dcc.md
```


## 5. 刷新/安装 SQLite 视图

```bash
python -m deepcommit.cli db refresh-views
```

**可选参数：**
- `--db-path <path>`: 自定义数据库路径（默认：`DATA/result/trials.sqlite`）
- `--sql-path <path>`: 自定义 SQL 文件（默认：`scripts/sql/views.sql`）

**提示：** 视图定义在 `scripts/sql/views.sql`，可随时重新运行更新。
