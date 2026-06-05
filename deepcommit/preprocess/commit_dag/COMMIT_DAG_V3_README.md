# Commit DAG v3 - Git Blame Based Dependency Analysis

## Overview

`commit_dag_v3.py` 是基于 git blame + 上下文窗口 + git apply 验证的提交依赖分析工具。在 v2 的基础上，v3 强化了重命名追踪、文件级传递性剪枝与输出结构，默认输出文件名以 `commit_dag_v3_...` 开头。

## 核心改进

### 1. 基于 Git Blame 的依赖检测

对于每个提交的每个 hunk，v3 会：
- Blame 修改的行（找出这些行最后由谁写的）
- Blame 上下文行（默认上下 5 行，可配置）
- 收集所有相关的历史提交作为候选依赖

### 1.5. 文件级传递性剪枝（File-Scoped Transitive Pruning）

v3 使用**文件级传递性剪枝**来优化验证过程，避免全局剪枝可能导致的误跳过：

- **预处理**：在分析前，构建每个文件的修改历史（file → commits）
- **文件级 intermediates**：验证 A→D 依赖时，只考虑同样修改了当前文件的中间commits
- **避免误跳过**：不会因为其他文件上的间接路径而跳过当前文件的真实依赖验证
- **更准确的依赖图**：每条边都基于特定文件，语义更清晰

**示例**：
```
文件 x.py: A, D 修改
文件 y.py: A, B, C, D 修改
已有边: A→B→C→D (y.py)

验证 A→D (x.py) 时：
- 文件级 intermediates = {} (B, C 没修改 x.py)
- 不会因为 y.py 上的 A→B→C→D 路径而跳过
- 正确建立 A→D (x.py) 边 ✓
```

### 2. 统一的上下文窗口机制

v3 使用**单一的上下文窗口参数** `--context-lines`（默认 5），直接对应 `git diff` 的 `-U` 参数：

```python
# context_lines=5 时
git show -U5 commit -- file.py

# 输出的 diff hunk 已经包含上下文
@@ -7,9 +7,9 @@   # old: 第7行开始，共9行（包含上下文）
 context line 1
 context line 2
 context line 3
 context line 4
 context line 5
-old line (modified)
+new line (modified)
 context line 6
 context line 7
 context line 8
```

**工作流程**：
1. `git show -U{context_lines}` 获取带上下文的 diff
2. Hunk header 告诉我们范围（已包含上下文）：`old_start=7, old_count=9`
3. 一次性 blame 整个范围（第 7-15 行）
4. 收集所有候选依赖

**两个上下文参数**（默认都是 5）：
- `--context-lines 5`: 用于 `git show` 和 `git blame`（发现候选依赖）
- `--patch-context-lines 5`: 用于 `git apply` 验证时的 patch 上下文（验证准确性）

**调整建议**：
- 增大 `context_lines` → 发现更多候选依赖（覆盖范围更广）
- 减小 `patch_context_lines` → 更严格的依赖验证（减少误报）
- 通常建议保持两者相同，或让 `patch_context_lines` 略小

### 3. 双层循环优化（文件级传递性剪枝）

#### 外层循环（正序）
遍历所有 commits（时间顺序：old → new）

```python
for commit in [A, B, C, D, E]:  # 正序
    # 处理每个 commit
```

#### 预处理：文件修改历史
在开始分析前，预处理每个文件被哪些commits修改过：

```python
file_commit_history = {}  # file_path -> [commits that modified it]
for commit in commit_list:
    files = get_modified_files(commit)
    for f in files:
        file_commit_history[f].append(commit)
```

#### 内层循环（倒序 + 文件级剪枝）
处理候选依赖时，按时间倒序验证，并利用**文件级传递性剪枝**：

```python
# 当前处理 E 的文件 x.py，候选依赖：{A, B, C, D}
# 按倒序验证：D, C, B, A

for candidate in [D, C, B, A]:  # 倒序
    # 文件级 intermediates：只包含修改了 x.py 的中间commits
    intermediates = {
        c for c in file_commit_history.get('x.py', [])
        if candidate_idx < index(c) < E_idx
    }

    # 检查是否存在文件级间接路径
    if has_indirect_path(candidate, E, intermediates):
        # 已有 candidate -> X -> ... -> E 的路径（X 也修改了 x.py）
        # 跳过昂贵的 git apply 验证
        continue

    # 验证直接依赖
    verify_with_git_apply(candidate, E, 'x.py')
```

**文件级剪枝 vs 全局剪枝**：

| 方案 | Intermediates 定义 | 优势 | 劣势 |
|------|-------------------|------|------|
| 全局剪枝 | 时间上所有中间commits | 可能跳过更多验证 | 可能误跳过（通过其他文件的路径） |
| 文件级剪枝 | 只包含修改当前文件的commits | 更准确，不会误跳过 | 跳过较少，但结果更可靠 |

**优化效果示例**：
```
Commit list: A -> B -> C -> D -> E

文件 x.py:
- A: 修改了 x.py
- E: 修改了 x.py
- B, C, D: 没有修改 x.py

文件 y.py:
- A, B, C, D, E: 都修改了 y.py
- 已建立边: A→B→C→D→E (在 y.py 上)

处理 E 的 x.py 时：
1. 候选依赖: {A} (通过 git blame 找到)
2. 计算文件级 intermediates: {} (B, C, D 都没修改 x.py)
3. has_indirect_path(A, E, {}) = False
4. 执行 git apply 验证 A→E 在 x.py 上的依赖
5. **结果**：正确建立 A→E (x.py) 边

如果使用全局方案：
1. 全局 intermediates: {B, C, D}
2. has_indirect_path(A, E, {B,C,D}) = True (通过 y.py 的路径)
3. **跳过验证** ❌
4. 丢失了 A→E 在 x.py 上的依赖信息
```

### 4. Git Apply 双向验证（优化版）

对于每个候选依赖 `src -> dst`，使用**优化的双向 patch 应用测试**：

```bash
# 1. 获取 src 的 parent commit
parent=$(git rev-parse src^)

# 2. 测试顺序 1：parent → src → dst
git checkout parent
git apply <patch_from_src>  # 应用 src 的 patch
git commit
git apply <patch_from_dst>  # 应用 dst 的 patch
git commit

# 如果 src→dst 失败，立即判定有依赖，节省后续测试
if [ $? -ne 0 ]; then
    echo "Has dependency (src→dst failed)"
    exit 0
fi

file_hash_1=$(git ls-tree HEAD file.py | awk '{print $3}')

# 3. 测试顺序 2：parent → dst → src（仅当 src→dst 成功时）
git checkout parent
git apply <patch_from_dst>  # 应用 dst 的 patch
git commit
git apply <patch_from_src>  # 应用 src 的 patch
git commit

# 如果 dst→src 失败，判定有依赖
if [ $? -ne 0 ]; then
    echo "Has dependency (only src→dst succeeds)"
    exit 0
fi

file_hash_2=$(git ls-tree HEAD file.py | awk '{print $3}')

# 4. 比较文件 hash
if [[ "$file_hash_1" == "$file_hash_2" ]]; then
    # 两个顺序结果相同 → 无依赖（可交换）
    echo "No dependency"
else
    # 两个顺序结果不同 → 有依赖（顺序影响结果）
    echo "Has dependency (hashes differ)"
fi
```

**判断逻辑（优化后）**：
1. ❌ **src→dst 失败** → **有依赖**（立即返回，节省 50% 时间）
2. ✅ src→dst 成功，继续测试：
   - ❌ dst→src 失败 → **有依赖**
   - ✅ dst→src 成功 + hash 相同 → **无依赖**（commutative）
   - ❌ dst→src 成功 + hash 不同 → **有依赖**（顺序影响结果）

**性能优化**：大多数情况下，如果 dst 依赖 src，src→dst 会失败，此时只需一次测试即可判定，节省约 50% 的验证时间。

这比 v1 的单向测试更准确，避免了"应用成功就认为无依赖"的误判。

## 边类型（Edge Types）

v3 定义了 2 种边类型：

| 类型 | 说明 | 颜色（DOT） |
|------|------|------------|
| `blame_verified` | Git blame 找到 + git apply 验证通过（依赖确认） | Green |
| `blame_only` | Git blame 找到但未验证（禁用 verify_with_patch 时） | Blue |

**重要说明**：
1. **验证失败不添加边**：当 git apply 验证失败时（表示无依赖，可并行），不会添加任何边
2. **传递性剪枝**：当发现文件级传递性路径时（如 A→B→C，其中 A、B、C 都修改了同一文件），v3 会：
   - 建立边 A→B 和 B→C
   - 跳过 A→C 的验证（通过传递性剪枝优化）
   - **不添加 A→C 边**（因为已有间接路径）
3. **只保留真实依赖**：最终的 DAG 只包含验证通过的依赖边（`blame_verified`）或未验证的候选边（`blame_only`），没有误报边

如果需要显式的传递性约简，可以使用 `--reduce-transitive` 选项（通常不需要，因为已通过文件级剪枝优化）。

## 使用方法

### 基本用法

```bash
python commit_dag_v3.py \
  --repo /path/to/repo \
  --commit-file commits.txt \
  --outdir output/
```

### 自定义上下文窗口

```bash
# 使用 10 行 blame 上下文 + 5 行 patch 上下文
python commit_dag_v3.py \
  --repo /path/to/repo \
  --commit-file commits.txt \
  --context-lines 10 \
  --patch-context-lines 5 \
  --outdir output/

# 不使用 blame 上下文（仅修改行） + 1 行 patch 上下文（严格验证）
python commit_dag_v3.py \
  --repo /path/to/repo \
  --commit-file commits.txt \
  --context-lines 0 \
  --patch-context-lines 1 \
  --outdir output/

# 大 blame 上下文 + 小 patch 上下文（发现多，验证严）
python commit_dag_v3.py \
  --repo /path/to/repo \
  --commit-file commits.txt \
  --context-lines 10 \
  --patch-context-lines 1 \
  --outdir output/
```

### 禁用 Git Apply 验证

```bash
# 仅使用 git blame（更快但可能有误报）
python commit_dag_v3.py \
  --repo /path/to/repo \
  --commit-file commits.txt \
  --no-verify-with-patch \
  --outdir output/
```

### 过滤特定目录

```bash
# 只分析 src/ 目录下的文件
python commit_dag_v3.py \
  --repo /path/to/repo \
  --commit-file commits.txt \
  --repo-src-dir src/ \
  --exclude-test-files \
  --exclude-doc-files \
  --outdir output/
```

### 传递性约简

```bash
# 移除传递性冗余边
python commit_dag_v3.py \
  --repo /path/to/repo \
  --commit-file commits.txt \
  --reduce-transitive \
  --outdir output/
```

## 输出格式

### JSON 输出

```json
{
  "nodes": {
    "abc123...": { "sha": "abc123...", "subject": "Add feature X" }
  },
  "edges": [
    {
      "src": "abc123...",
      "dst": "def456...",
      "kind": "blame_verified",
      "metadata": {
        "file": "src/foo.py",
        "blame_line_ranges": [[10, 15], [5, 9]],
        "verified": true
      }
    }
  ],
  "levels": { "abc123...": 0, "def456...": 1 },
  "external_deps": [],
  "cycles": []
}
```

### DOT 输出（Graphviz）

```bash
# 生成可视化图
dot -Tpng output/repo_name/commit_dag_v3_ctx_5.dot -o dag.png
```

## 优化统计

运行后会输出优化统计：

```
[V2] DAG construction complete:
  - Total nodes: 50
  - Total edges: 120
  - Filtered commits: 5

[Optimization Statistics]
  - Total candidate dependencies: 200
  - Verified with git apply: 120
  - Skipped (transitive): 80
  - Skip rate: 40.0%
```

**Skip rate** 表示通过传递性剪枝节省的 git apply 调用比例。

## 性能考虑

### Git Blame 性能
- Git blame 对大文件可能较慢
- 使用 `-M -C` 选项跟踪移动/复制（更准确但稍慢）
- 上下文窗口越大，blame 调用越多

### Git Apply 性能
- Git apply 需要 checkout 和 patch 应用
- 通过传递性剪枝可以显著减少调用次数
- 建议在独立的临时 clone 中运行（已实现）

### 优化建议

1. **限制上下文窗口**：对于大型仓库，使用较小的上下文（如 3-5 行）
2. **启用传递性约简**：使用 `--reduce-transitive` 减少边数
3. **过滤无关文件**：使用 `--exclude-test-files` 等选项
4. **限制源码目录**：使用 `--repo-src-dir` 只分析核心代码

## 与 v1 的对比

| 特性 | v1 | v2 |
|------|----|----|
| 依赖检测基础 | Line blame + Container + Patch file deps | Git blame + Context window |
| 上下文感知 | ❌ | ✅ (可配置) |
| 验证方式 | Cherry-pick 双向测试 | Git apply 双向测试（优化版） |
| 传递性剪枝 | ❌ | ✅ (文件级倒序 + 路径检查) |
| 边类型 | 4 种 (line, container, file_direct, file_indirect) | 2 种 (verified, only) |
| 误报处理 | 可能包含误报边 | 验证失败直接跳过（无误报边） |
| 性能优化 | 基础 | 高级（文件级剪枝 + 早期返回） |

## 示例场景

### 场景 1：修改函数实现

```python
# Commit A
def calculate(x):
    return x * 2

# Commit B (修改实现)
def calculate(x):
    return x * 3  # 修改第 2 行
```

**v3 分析**：
- Blame 第 2 行 → 找到 commit A
- Blame 上下文（第 1, 3 行）→ 找到 commit A
- Verify: 尝试将 B 的 patch 应用到 A → 失败（依赖确认）
- **结果**：A → B (blame_verified)

### 场景 2：添加新方法到类（可并行场景）

```python
# Commit A
class User:
    def __init__(self):
        pass

# Commit B (添加新方法)
class User:
    def __init__(self):
        pass

    def login(self):  # 新增
        pass
```

**v3 分析**：
- Blame 上下文（class User 行）→ 找到 commit A
- Verify: 尝试双向 patch 应用
  - A→B: 成功（在 A 的基础上添加 login 方法）
  - B→A: 成功（在 B 的基础上...实际上 A 没有新增内容）
  - 文件 hash: 相同（两个顺序结果一致）
- **结果**：验证失败（无依赖），**不添加边** ✓
- **结论**：A 和 B 可以并行，B 只是在类中添加新方法，不依赖 A 的具体实现

### 场景 3：传递性依赖链（文件级剪枝）

```
Commit list: A -> B -> C -> D

文件 foo.py:
A: 定义函数 foo()
B: 修改 foo() 内部逻辑
C: 修改 foo() 返回值
D: 修改 foo() 参数

所有commits都只修改了 foo.py
```

**v3 分析处理 D 的 foo.py**：
1. Blame D 的修改行 → 找到候选 {A, B, C}
2. 文件级 intermediates for each candidate:
   - 验证 C → D：intermediates = {} (C和D之间没有修改foo.py的commits)
     → 验证成功，建立边 C → D
   - 验证 B → D：intermediates = {C} (C修改了foo.py)
     → 检查路径，发现 B → C → D，**跳过验证**
   - 验证 A → D：intermediates = {B, C} (B, C都修改了foo.py)
     → 检查路径，发现 A → B → C → D，**跳过验证**
3. **结果**：只验证 1 次，跳过 2 次（节省 66% 的验证）

**对比场景**：如果B, C修改了其他文件，D还修改了foo.py：
```
Commit list: A -> B -> C -> D

文件 foo.py: A, D 修改
文件 bar.py: B, C 修改
```

**v3 分析处理 D 的 foo.py**：
1. Blame D 的 foo.py → 找到候选 {A}
2. 文件级 intermediates: {} (B, C 没有修改 foo.py)
3. **必须验证 A → D**（不会被误跳过）
4. **结果**：正确建立 A → D (foo.py) 边

如果使用全局剪枝，intermediates = {B, C}，如果 A→B→C→D 在 bar.py 上有路径，会错误地跳过 A→D 在 foo.py 上的验证。

## 双向验证详细示例

### 为什么需要双向验证？

**单向验证的问题**：
```python
# Commit A:
def foo():
    return 1

# Commit B:
def foo():
    return 2  # 修改返回值
```

单向测试 A → B：
- 尝试将 B 的 patch (`return 1` → `return 2`) 应用到 A
- 成功！（因为 A 确实有 `return 1`）
- **错误结论**：B 不依赖 A ❌

实际上 B 明显依赖并修改了 A 的代码！

**双向验证的解决**：
```bash
# 获取 A 的 parent（假设是空文件）
parent_commit

# 测试 1：parent → A → B
# 结果：foo() 返回 2

# 测试 2：parent → B → A
# B 的 patch 无法应用到 parent（因为 parent 没有 foo）
# → 只有一个方向成功 → 依赖存在 ✓
```

### 真实案例

#### 案例 1：修改同一函数的不同部分

```python
# Parent: 空
# A: 添加函数框架
def calculate(x):
    return x

# B: 修改返回值
def calculate(x):
    return x * 2
```

**双向测试**：
- parent → A → B: 成功，`return x * 2`
- parent → B → A: 失败（B 的 patch 找不到 `return x`）
- **结论**：B 依赖 A ✓

#### 案例 2：独立修改不同函数

```python
# Parent:
def foo():
    pass
def bar():
    pass

# A: 修改 foo
def foo():
    return 1

# B: 修改 bar
def bar():
    return 2
```

**双向测试**：
- parent → A → B: 成功，foo 返回 1，bar 返回 2
- parent → B → A: 成功，foo 返回 1，bar 返回 2
- 文件 hash 相同！
- **结论**：A 和 B 无依赖（可并行）✓

#### 案例 3：同一行的连续修改

```python
# Parent:
result = x * 1

# A:
result = x * 2

# B:
result = x * 3
```

**双向测试**：
- parent → A → B: 成功，`result = x * 3`
- parent → B → A: 成功，`result = x * 2`
- 文件 hash **不同**！
- **结论**：B 依赖 A（顺序影响结果）✓

## 未来改进

- [ ] 支持并行 git blame 调用
- [ ] 缓存 blame 结果和验证结果
- [ ] 更智能的上下文窗口（基于代码结构）
- [ ] 支持更多语言的容器检测
- [ ] 增量分析（只分析新增 commits）
- [ ] 优化文件级别的批量 patch 测试
