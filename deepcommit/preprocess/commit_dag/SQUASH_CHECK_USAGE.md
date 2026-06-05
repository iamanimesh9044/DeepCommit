# squash_check.py 使用指南

## 概述

`squash_check.py` 是一个用于验证 commit DAG 中各层 commits 是否可以安全 squash 的工具。

## 核心功能

1. **DAG 模式** (`--mode dag`): 基于 JSON 的拓扑结构验证
2. **Validate 模式** (`--mode validate`): 实际执行 cherry-pick 验证单个层级
3. **Validate-All 模式** (`--mode validate-all`): 批量验证所有层级

---

## 使用示例

### 1. DAG 结构验证

快速检查 DAG 是否存在环、同层内部依赖等问题。

```bash
python deepcommit/collect/collectors/commit_dag/squash_check.py \
    --dag deepcommit/collect/collectors/commit_dag/full_commit.json \
    --mode dag \
    --edge-kinds "line,file,container"
```

**特点**:
- 不需要仓库路径
- 执行速度快（秒级）
- 仅做静态拓扑分析

### 2. 验证单个层级 (Validate 模式)

#### 2.1 验证 Level 0

```bash
python deepcommit/collect/collectors/commit_dag/squash_check.py \
    --dag deepcommit/collect/collectors/commit_dag/full_commit.json \
    --repo DATA/raw_github_repo/bare_repo/urllib3_urllib3.git \
    --mode validate \
    --level 0 \
    --tries 3 \
    --randomize
```

**工作流程**:
1. 计算 Level 0 所有 commits 的 merge-base 作为起始点
2. 按指定顺序（或随机顺序）cherry-pick 所有 Level 0 commits
3. 验证不同顺序是否产生相同的 tree

#### 2.2 验证 Level N (N > 0)

```bash
python deepcommit/collect/collectors/commit_dag/squash_check.py \
    --dag deepcommit/collect/collectors/commit_dag/full_commit.json \
    --repo DATA/raw_github_repo/bare_repo/urllib3_urllib3.git \
    --mode validate \
    --level 2 \
    --tries 3
```

**工作流程**:
1. 收集 Level 0 到 Level N-1 的所有 commits (前置层)
2. 计算前置层的 merge-base 作为起始点
3. 按拓扑顺序 cherry-pick 所有前置层 commits
4. 在累计的 base 上 cherry-pick 目标层 commits
5. 验证结果一致性

**输出示例**:
```json
{
  "mode": "validate",
  "level": 2,
  "base": "f28deff1...",
  "base_strategy": "merge-base of prior levels",
  "num_prior_commits": 19,
  "num_target_commits": 7,
  "randomize": false,
  "tries": 3,
  "all_success": true,
  "deterministic_tree": true,
  "num_unique_trees": 1,
  "unique_trees": ["38b27646..."],
  "attempts": [...]
}
```

### 3. 批量验证所有层级 (Validate-All 模式)

#### 3.1 快速测试（仅显示摘要）

**时间**: ~5-10分钟
**适用**: 快速了解整体情况

```bash
python deepcommit/collect/collectors/commit_dag/squash_check.py \
    --dag deepcommit/collect/collectors/commit_dag/full_commit.json \
    --repo DATA/raw_github_repo/bare_repo/urllib3_urllib3.git \
    --mode validate-all \
    --tries 1 \
    --summary-only
```

#### 3.2 完整测试（保存报告）

**时间**: ~5-10分钟
**适用**: 需要详细结果

```bash
python deepcommit/collect/collectors/commit_dag/squash_check.py \
    --dag deepcommit/collect/collectors/commit_dag/full_commit.json \
    --repo DATA/raw_github_repo/bare_repo/urllib3_urllib3.git \
    --mode validate-all \
    --tries 1 \
    --out DATA/raw_github_repo/validate_all_result.json
```

#### 3.3 详细模式（显示失败信息）

**时间**: ~15-30分钟
**适用**: 深入分析失败原因

```bash
python deepcommit/collect/collectors/commit_dag/squash_check.py \
    --dag deepcommit/collect/collectors/commit_dag/full_commit.json \
    --repo DATA/raw_github_repo/bare_repo/urllib3_urllib3.git \
    --mode validate-all \
    --tries 3 \
    --verbose \
    --out DATA/raw_github_repo/validate_all_verbose.json
```

#### 3.4 随机顺序测试

**时间**: ~15-30分钟
**适用**: 验证 commits 是否真正独立

```bash
python deepcommit/collect/collectors/commit_dag/squash_check.py \
    --dag deepcommit/collect/collectors/commit_dag/full_commit.json \
    --repo DATA/raw_github_repo/bare_repo/urllib3_urllib3.git \
    --mode validate-all \
    --tries 3 \
    --randomize \
    --out DATA/raw_github_repo/validate_all_randomized.json
```

---

## 输出格式说明

### Validate 模式输出

#### Summary Only 输出
```
================================================================================
Validate Mode - Cherry-pick Test Results
================================================================================
Mode: validate
Target level: 2
Base commit: f28deff1cf162c67...
Base strategy: merge-base of prior levels

Prior commits (lower levels): 19
Target commits (this level): 7
Randomize order: False
Number of tries: 3

✓ Result: ALL ATTEMPTS SUCCEEDED
  Unique trees produced: 1
  Deterministic result: ✓
```

#### 完整输出（包含每次尝试的详情）
```
Attempt details (3 total):
Try    Success    Tree                 Failed At
--------------------------------------------------------------------------------
1      ✓          38b2764697aa3f2f     -
2      ✓          38b2764697aa3f2f     -
3      ✓          38b2764697aa3f2f     -
================================================================================
```

### Validate-All 模式输出

#### Summary Only 输出
```
================================================================================
Validate All Levels - Batch Cherry-pick Test Results
================================================================================
Total levels tested: 60
Tries per level: 1
Randomize order: False

✓ Result: ALL LEVELS PASSED
  Successful levels: 60/60
  Failed levels: 0/60
```

#### 完整输出
```
Level-by-level results:
Level    Commits    Success    Deterministic   Unique Trees
--------------------------------------------------------------------------------
0        13         ✓          ✓               1
1        6          ✓          ✓               1
2        7          ✓          ✓               1
...

Summary by result type:
  ✓ Fully successful levels (60):
    Level 0: 13 commits
    Level 1: 6 commits
    ...
================================================================================
```

---

## 参数说明

| 参数 | 说明 | 默认值 | 适用模式 |
|------|------|--------|----------|
| `--dag` | DAG JSON文件路径 | 必需 | 所有 |
| `--repo` | Git仓库路径 | 必需(validate/validate-all) | validate, validate-all |
| `--mode` | 运行模式 | dag | 所有 |
| `--level` | 指定验证的层级 | 无（全部） | validate |
| `--edge-kinds` | 作为依赖的边类型，逗号分隔 | line,file | dag |
| `--tries` | 每层尝试次数 | 3 | validate, validate-all |
| `--randomize` | 随机化commit顺序 | false | validate, validate-all |
| `--verbose` | 显示详细错误信息 | false | 所有 |
| `--summary-only` | 仅显示摘要 | false | 所有 |
| `--out` | 保存JSON报告路径 | 无（输出到终端） | 所有 |

---

## 结果解释

### 状态标记

- **✓** = 该层所有尝试都成功
- **⚠** = 该层部分尝试成功
- **✗** = 该层所有尝试都失败

### Deterministic（确定性）

- **✓** = 所有尝试产生相同的 tree（顺序无关）
- **✗** = 不同尝试产生不同的 tree（可能有顺序依赖）

### Unique Trees（唯一树数量）

- **1** = 所有尝试产生相同结果（理想情况）
- **>1** = 不同尝试产生不同结果（需要注意）

### 结果判断

#### ✅ 成功的情况

```json
{
  "all_success": true,
  "deterministic_tree": true,
  "num_unique_trees": 1
}
```

**含义**: 所有随机顺序都成功，且产生相同的 tree
→ 这些 commits 是真正可交换的 (commutative)

#### ⚠️ 部分成功

```json
{
  "all_success": false,
  "any_success": true,
  "num_unique_trees": 2
}
```

**含义**:
- 有些顺序失败（有冲突）
- 成功的尝试产生了不同的 tree
→ 这些 commits 不能任意顺序 squash

#### ❌ 完全失败

```json
{
  "all_success": false,
  "any_success": false,
  "num_unique_trees": 0
}
```

**含义**: 所有尝试都失败
→ 需要检查 DAG 依赖关系或前置层的顺序

---

## 查看结果（使用 jq）

### 查看JSON报告摘要

```bash
jq '{
  total_levels,
  successful_levels,
  failed_levels,
  partial_success_levels
}' DATA/raw_github_repo/validate_all_result.json
```

### 找出失败的层级

```bash
jq -r '.results_by_level | to_entries[] |
  select(.value.all_success == false) |
  "Level \(.key): \(.value.num_target_commits) commits - " +
  if .value.any_success then "PARTIAL" else "FAILED" end' \
  DATA/raw_github_repo/validate_all_result.json
```

### 查看特定层级的失败详情

```bash
jq '.results_by_level["5"].attempts[] |
  select(.success == false) |
  {try, failed_at, conflict_files}' \
  DATA/raw_github_repo/validate_all_result.json
```

---

## 典型工作流

### 工作流 1: 快速全局检查

```bash
# 步骤 1: DAG 静态分析
python deepcommit/collect/collectors/commit_dag/squash_check.py \
    --dag deepcommit/collect/collectors/commit_dag/full_commit.json \
    --mode dag \
    --edge-kinds "line,file,container" \
    --summary-only

# 步骤 2: 批量验证所有层
python deepcommit/collect/collectors/commit_dag/squash_check.py \
    --dag deepcommit/collect/collectors/commit_dag/full_commit.json \
    --repo DATA/raw_github_repo/bare_repo/urllib3_urllib3.git \
    --mode validate-all \
    --tries 1 \
    --out DATA/raw_github_repo/validate_all_result.json

# 步骤 3: 针对失败层深入分析（假设 Level 5 失败）
python deepcommit/collect/collectors/commit_dag/squash_check.py \
    --dag deepcommit/collect/collectors/commit_dag/full_commit.json \
    --repo DATA/raw_github_repo/bare_repo/urllib3_urllib3.git \
    --mode validate \
    --level 5 \
    --tries 5 \
    --verbose
```

### 工作流 2: 逐层验证

```bash
# 从 Level 0 开始，逐层验证
for level in {0..10}; do
  echo "Testing level $level..."
  python deepcommit/collect/collectors/commit_dag/squash_check.py \
    --dag deepcommit/collect/collectors/commit_dag/full_commit.json \
    --repo DATA/raw_github_repo/bare_repo/urllib3_urllib3.git \
    --mode validate \
    --level $level \
    --tries 3 \
    --out results/level_${level}.json
done
```

---

## 最佳实践

1. **从 DAG 模式开始**: 先用 DAG 模式快速检查拓扑结构
2. **首次运行用 `--tries 1`**: 快速了解整体情况，节省时间
3. **失败层单独分析**: 对失败的层级使用 `--verbose` 深入调查
4. **保存完整报告**: 使用 `--out` 保存 JSON 结果供后续分析
5. **验证随机性**: 使用 `--randomize` 确认 commits 真正独立
6. **增加测试次数**: 对关键层级使用 `--tries 10` 或更多

---

## 故障排除

### 问题 1: "git not found"

**解决**: 确保 git 已安装并在 PATH 中
```bash
which git
git --version
```

### 问题 2: "Repository not found"

**解决**: 检查仓库路径是否正确
```bash
ls -la DATA/raw_github_repo/bare_repo/urllib3_urllib3.git
```

### 问题 3: 运行时间过长

**解决方案**:
- 使用 `--tries 1` 减少尝试次数
- 不使用 `--randomize` 节省时间
- 先用 DAG 模式快速筛查

### 问题 4: 内存不足

**解决方案**:
- 使用 `--mode validate --level N` 分批验证特定层级
- 不使用 `--mode validate-all`

### 问题 5: Level N 失败，显示 "failed_at: prior:xxx"

**原因**: 前置层 commits 应用时发生冲突

**调试步骤**:
```bash
# 1. 查看是哪个 prior commit 失败
jq '.attempts[0].failed_at' result.json

# 2. 检查该 commit 所在的层级
jq '.levels["<commit_sha>"]' full_commit.json

# 3. 单独验证该层级
python squash_check.py \
  --mode validate \
  --level <prior_level> \
  --tries 1 \
  --verbose
```

**解决方案**: 前置层的 commits 现在按 (level, sha) 排序以保证拓扑顺序

### 问题 6: 不同尝试产生不同的 tree (deterministic_tree: false)

**原因**: Commits 之间存在顺序依赖

**示例**:
```
Commit A: 添加函数 foo()
Commit B: 调用函数 foo()

如果先应用 B 再应用 A，可能导致中间状态编译失败或产生不同的结果
```

**解决方案**: 检查 DAG 依赖关系是否完整

---

## 已知限制与注意事项

### 1. 前置层顺序依赖

**描述**: Level N 的验证依赖前置层 (Level 0 到 N-1) 的正确顺序

**影响**: 如果前置层内部有复杂依赖，可能导致失败

**解决**: 代码已按 `(level, sha)` 排序确保拓扑顺序

### 2. 大规模 DAG 性能

**问题**: Level N 需要累计 cherry-pick 所有 Level 0 到 N-1 的 commits

**耗时估算**:
- 60 层，200+ commits: 5-30 分钟（取决于 `--tries` 和 `--randomize`）

**优化建议**:
- 首次运行使用 `--tries 1`
- 分批验证，不全部使用 validate-all

### 3. Tree 相同不保证语义正确

**重要提醒**:
- `deterministic_tree: true` 只表示**文件内容相同**
- **不保证**程序的语义正确性

**建议**: 验证后运行测试
```bash
# 在验证成功后
cd squashed_repo
pytest  # 或其他测试命令
```

### 4. 临时空间需求

**需求**: 每次尝试都会 clone 仓库到临时目录

**解决**: 确保 `/tmp` 或临时目录有足够空间
```bash
df -h /tmp
```

### 5. 并发限制

**当前**: 串行执行每个层级的验证

**未来**: 可能支持并行验证多个层级

---

## 相关工具

### run_validate_all.sh

快速执行脚本，位于 `deepcommit/collect/collectors/commit_dag/run_validate_all.sh`

```bash
cd deepcommit/collect/collectors/commit_dag
chmod +x run_validate_all.sh
./run_validate_all.sh
```

---

## 更多信息

查看帮助信息:
```bash
python deepcommit/collect/collectors/commit_dag/squash_check.py --help
```
