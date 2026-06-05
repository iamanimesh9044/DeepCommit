# Milestone拆分任务 - Agent Prompt模板 V2

**版本**: v2.0 (Hunk-based)
**创建时间**: 2025-10-26
**核心策略**: Git Hunks拆分 + 代码结构分析 + 细粒度依赖推断

> ⚠️ **DEPRECATED & DIRECTION WARNING** — 本 V2 prompt 已被 V3 (`AGENT_PROMPT_TEMPLATE_V3.md`) 取代。
> V2 的 dependencies 示例使用了**反向**的 source/target 约定(说 source 依赖 target),与 canonical 仓库约定矛盾。
> Canonical: `source_id → target_id` 表示 **source 是 prerequisite(前置), target 是 dependant(依赖者)**。
> 如果出于历史原因要参考 V2,**请将所有示例的 source/target 方向理解为反过来** — `source` 是 prerequisite, `target` 才是 dependant。新工作请用 V3。

---

## 任务说明

你的任务是将大型 milestone 智能拆分为多个功能内聚、大小合理、依赖清晰的 sub-milestones。

### 任务目标

- 将过大的 milestone 拆分成合理大小的 sub-milestones
- 确保每个 sub-milestone 功能内聚、可独立理解
- 建立清晰的依赖关系，形成有效的 sub-milestone DAG

---

## 输入数据

### 待拆分的 Milestone

- **id**: Milestone标识符（如 "M2"）
- **title**: Milestone标题
- **commits**: Commits列表（SHA列表）
- **src_loc**: 源代码变更行数
- **category**: 类别（FEATURE/BUGFIX/STABILITY等）

### 可用工具

- **MCP工具**: `get_commit_context(sha, expand=['files', 'stats', 'diff'])`
  - 获取 commit 的详细信息、文件列表、diff内容
- **MCP工具**: `calculate_commits_src_loc_tool(commits)`
  - 计算 commits 的 src_loc

---

## 核心概念：Git Hunk

### 什么是 Hunk？

**Git Hunk** 是 git diff 输出中的**连续修改区域**。每个 hunk 代表文件中一个独立的修改位置。

### 示例

```diff
diff --git a/src/file.py b/src/file.py
@@ -10,5 +10,8 @@ def foo():
     return x
+    # 新增代码
+    y = process(x)
+    return y

@@ -50,3 +53,6 @@ def bar():
     pass
+    # 另一个修改位置
+    z = compute()
```

上面有 **2 个 hunks**：
- **Hunk 1**: 修改 `foo()` 函数（第10-15行）
- **Hunk 2**: 修改 `bar()` 函数（第50-56行）

### 关键特性

| 场景 | Hunks 数量 | 说明 |
|------|-----------|------|
| 修改现有文件 | 通常多个 | 每个修改位置一个 hunk |
| 新增文件 | 只有 1 个 | 整个文件内容是一个大 hunk |
| 删除文件 | 1 个 | 整个文件删除 |

---

## 拆分流程

### Phase 1: Commit 分析与分类

**目标**: 按大小将 commits 分成两类

**步骤**:

1. **计算每个 commit 的 src_loc**
   - 使用 `calculate_commits_src_loc_tool([commit_sha])`

2. **分类**:
   - **合理大小的 commits** (< 500 src_loc): 保持不变
   - **过大的 commits** (≥ 500 src_loc): 需要拆分 ← 重点关注

**重要原则**:
- ⚠️ **Commit 是原子单元**：每个 commit 代表一个完整的代码变更，不应该被"部分使用"
- ⚠️ **小 commit 绝对不拆分**：src_loc < 50 的 commits 必须完整地分配到一个 sub-milestone
- ⚠️ **中等 commit 谨慎拆分**：50-500 src_loc 的 commits 一般也应该保持完整，除非有明确的 hunk 边界且拆分后每部分都 ≥ 50 src_loc

**输出**: 两个列表（保持不变的 commits / 需要拆分的 commits）

---

### Phase 2: Hunk-based 拆分

**目标**: 对大 commits 按照 git hunks 拆分

#### 2.1 提取 Git Hunks

**步骤**:

1. **获取 commit 的 git diff**
   - 使用 `get_commit_context(commit_sha, expand=['diff', 'files'])`

2. **解析 diff 输出，识别每个 hunk**
   - 查找 `@@` 标记（hunk header）
   - 记录每个 hunk 的：
     - 文件路径
     - 行号范围
     - 修改内容（+ 和 - 开头的行）
     - src_loc（additions + deletions）

**输出**: 每个 commit 的 hunks 列表

#### 2.2 Hunk 分类

将 hunks 按文件类型分类：

**修改文件的 hunks**:
- 直接作为拆分单元
- 每个 hunk 代表文件中一个独立的修改位置
- 通常大小合理，无需进一步拆分

**新增文件的 hunks**:
- 如果文件 < 300 行：保持整体
- 如果文件 > 300 行：需要进一步拆分 → 进入 Phase 2.3

**删除文件的 hunks**:
- 保持整体，不需要拆分

**注意事项**:
- **修改文件的大 hunk**: 通常保持原样（hunk 本身有内聚性，是一个连续的修改区域）
- **新增文件的大 hunk**: 需要按代码结构进一步拆分（见 Phase 2.3）

#### 2.3 大文件按代码结构拆分

**目标**: 对新增的大文件，按功能划分成多个 sub-hunks

**步骤**:

1. **读取文件内容**
   - 从 git diff 中提取（+ 开头的行）
   - 或使用 `git show <commit>:<file_path>`

2. **识别代码结构**
   - **Python**: 识别 `class` 和 `def` 定义
   - **JavaScript**: 识别 `class`, `function`, `const/let`
   - 记录每个结构的行号范围和大小

3. **按功能聚合**
   - 将相关的 class 和 method 分组
   - 将辅助函数归到主函数
   - 目标：每个 sub-hunk 在 150-300 src_loc

**输出**: Sub-hunks 列表，每个包含：
- **type**: "sub_hunk"
- **base_commit**: 原始 commit SHA
- **file**: 文件路径
- **function_group**: 功能组名称（如 "HTTPConnection class"）
- **line_range**: 行号范围 `{"start": 开始行, "end": 结束行}`
- **src_loc**: 该 sub-hunk 的代码行数

**示例**:
```json
{
  "type": "sub_hunk",
  "base_commit": "abc123",
  "file": "src/fetch.py",
  "function_group": "HTTPConnection",
  "line_range": {"start": 10, "end": 150},
  "src_loc": 140
}
```

#### 2.4 汇总拆分单元

**拆分单元类型**:

1. **完整 commit** (合理大小的 commits，无需拆分)
2. **Hunk** (修改文件的独立修改位置)
3. **New file hunk** (新增的小文件)
4. **Sub-hunk** (新增大文件的功能片段)

**输出**: 所有拆分单元的完整列表

---

### Phase 3: 语义聚合

**目标**: 将拆分单元按功能聚合成 sub-milestones

**主要策略: 功能相关性**
- 分析 commit message 和文件路径
- 识别功能主题（如 "基础架构", "错误处理", "API实现"）
- 将实现相同功能的单元分组

**辅助考虑**:
- **文件内聚性**: 修改相关文件的单元尽量分组
- **时间顺序**: 参考开发阶段（初始 → 核心 → 增强 → 修复）

**聚合约束**:
- 目标大小：150-500 src_loc per sub-milestone
- 例外：初始架构可达 600 src_loc（如不可再分）
- 功能内聚：每个 sub-milestone 应该有清晰的功能描述

**输出**: Sub-milestones 列表，每个包含：
- id (如 "M2.1")
- title
- units (包含的拆分单元)
- total_src_loc
- mini_srs (简短功能描述)
- category

---

### Phase 4: 依赖推断

**目标**: 推断 sub-milestones 之间的依赖关系

V2 支持三种依赖类型：

#### 4.1 Textual 依赖（行级依赖）

**规则**: 修改某行需要先创建该行

**检测方法**:
- 识别每个 sub-milestone 创建的代码行（新增文件或 hunk 中的 + 行）
- 识别每个 sub-milestone 修改的代码行（hunk 中修改的位置）
- 如果 B 修改了 A 创建的行 → **B depends on A**

**示例**:
```
Sub_A: 新增 connection.py 第 10-50 行
Sub_B: 修改 connection.py 第 30-35 行
→ B depends on A (textual dependency)
```

#### 4.2 Def-before-use 依赖（符号级依赖）

**规则**: 调用某个符号需要先定义该符号

**检测方法**:
- 识别每个 sub-milestone 定义的符号（class, function）
- 识别每个 sub-milestone 使用的符号（调用、引用）
- 如果 B 使用 A 定义的符号 → **B depends on A**

**示例**:
```
Sub_A: 定义 class HTTPConnection
Sub_B: 调用 HTTPConnection()
→ B depends on A (def-use dependency)
```

**提示**: 可以使用简化方法（正则表达式识别 class/def）或完整方法（AST 分析）

#### 4.3 File Import 依赖（辅助）

**规则**: 文件 A import 文件 B → A depends on B

**检测方法**:
- 检查文件内容中的 import 语句
- 匹配 import 的模块名与其他 sub-milestone 的文件

**示例**:
```
Sub_A: 包含 connection.py
Sub_B: 包含 fetch.py，其中有 "from .connection import ..."
→ B depends on A (import dependency)
```

#### 4.4 合并依赖

- 合并所有三种依赖
- 去重（如果两个 sub-milestone 有多种依赖关系，合并成一个依赖记录）
- 标记依赖类型（TEXTUAL, DEF_USE, FILE_IMPORT）

**输出**: 依赖关系列表,每个包含(canonical convention — source 是前置, target 是依赖者):
- source_id (**prerequisite** / 前置 — 必须先完成的 sub-milestone)
- target_id (**dependant** / 依赖者 — 在 source 完成后才能开始)
- dependency_tags (依赖类型)
- dependency_notes (依赖说明)

> ⚠️ 本 V2 文件后续示例中的 CSV/JSON 数据**方向是反的**(详见文件顶部 DEPRECATED 警告)。读取时请反向理解,新工作请用 V3。

---

### Phase 5: 验证与优化

**目标**: 验证拆分质量，确保符合要求

#### 验证检查项

**1. 完整性检查**
- **Src_loc 守恒**: 拆分后的总 src_loc 应接近原始 milestone（±5% 容差）
- **Commits 覆盖**: 所有原始 commits 都被包含在某个 sub-milestone 中

**如果 src_loc 差异过大**:
- 检查是否正确计算了每个 hunk 的 src_loc（additions + deletions）
- 检查是否遗漏了某些 hunks
- 如果差异 < 10%，仍可接受

**2. 平衡性检查**
- **大小分布**: 计算 sub-milestones 的平均大小和标准差
- **CV (变异系数)**: CV < 1.0 表示大小较均衡
- **极值检查**: 最大 sub < 700, 最小 sub > 30

**3. DAG 有效性检查**
- 使用拓扑排序验证依赖图无环
- 如果检测到环，找出环中的节点并报告

**如果检测到循环依赖**:
- 检查是否是同一个 commit 的不同 hunks（这种不应该有依赖关系）
- **优先方案**：将有循环依赖的 sub-milestones 合并成一个
- **备选方案**：调整 sub-milestones 的边界（重新分配完整的 commits）
- ⚠️ **禁止方案**：拆分小 commit（< 50 src_loc）来打破循环
- 最后手段：重新评估依赖推断的准确性（可能存在误判）

#### 优化策略

如果验证失败：
- **第1次失败**: 自动调整（如合并过小的 sub-milestones）
- **第2次失败**: 尝试不同的聚合策略
- **第3次失败**: 输出警告和建议，继续生成报告

**输出**: 验证报告（通过/失败 + 详细信息）

---

### Phase 6: 生成报告

**目标**: 生成完整的 JSON 报告

**输出文件**:
- **文件名**: `{milestone_id}_split_report.json`
  - 示例: `M2_split_report.json`
- **保存位置**: 当前工作目录或用户指定的输出目录

**报告结构**:

```json
{
  "original_milestone": {
    "id": "M2",
    "title": "...",
    "src_loc": 1200,
    "commits": [...]
  },

  "sub_milestones": [
    {
      "id": "M2.1",
      "parent_id": "M2",
      "title": "Core Infrastructure",
      "commits": [
        {
          "sha": "abc123",
          "split_type": "complete",
          "src_loc": 150
        },
        {
          "sha": "def456",
          "split_type": "partial",
          "files": ["src/core.py", "src/utils.py"],
          "hunks": [
            {
              "file": "src/core.py",
              "description": "Add base class",
              "line_range": {"start": 50, "end": 150},
              "src_loc": 80
            },
            {
              "file": "src/utils.py",
              "description": "Add utility functions",
              "line_range": {"start": 1, "end": 45},
              "src_loc": 40
            }
          ],
          "src_loc": 120
        }
      ],
      "total_src_loc": 270,
      "mini_srs": "Implement core infrastructure module with base classes and utilities",
      "rationale": "Aggregate architecture-related hunks into a foundational module",
      "category": "FEATURE"
    }
  ],

  "dependencies": [
    {
      "source_id": "M2.2",
      "target_id": "M2.1",
      "dependency_tags": "TEXTUAL,DEF_USE",
      "dependency_notes": "M2.2 modifies lines created by M2.1 and uses HTTPConnection"
    }
  ],

  "split_summary": {
    "num_sub_milestones": 5,
    "original_src_loc": 1200,
    "split_src_loc_total": 1195,
    "conservation_rate": 0.996,
    "max_sub_src_loc": 350,
    "avg_sub_src_loc": 239,
    "cv": 0.42
  },

  "split_strategy_used": "Hunk-based + Code Structure + Semantic Grouping",

  "warnings": [
    "M2.3 has 350 src_loc, slightly large but acceptable"
  ]
}
```

**Commits 字段要求**:

每个 sub-milestone 的 `commits` 数组中，每个 commit 对象必须包含：

1. **Complete commits** (`split_type: "complete"`):
   ```json
   {
     "sha": "abc123",
     "split_type": "complete",
     "src_loc": 150
   }
   ```

2. **Partial commits** (`split_type: "partial"`) - ⚠️ 必须包含详细信息:
   ```json
   {
     "sha": "def456",
     "split_type": "partial",
     "files": ["src/core.py", "src/utils.py"],  // ← 必需
     "hunks": [  // ← 必需
       {
         "file": "src/core.py",
         "description": "Add base class",
         "line_range": {"start": 50, "end": 150},  // ← 当同一文件被拆分时必需
         "src_loc": 80
       },
       {
         "file": "src/utils.py",
         "description": "Add helper functions",
         "src_loc": 40
       }
     ],
     "src_loc": 120
   }
   ```

**重要**：
- ⚠️ 所有 `split_type: "partial"` 的 commits **必须**包含 `files` 和 `hunks` 字段
- `files` 数组列出该 sub-milestone 包含的文件
- `hunks` 数组详细说明每个文件的具体修改内容和 src_loc
- ⚠️ **line_range 字段要求**:
  - 当同一个文件的不同部分被拆分到多个 sub-milestones 时，**必须**在每个 hunk 中添加 `line_range` 字段
  - `line_range` 格式: `{"start": N, "end": M}`，其中 N 和 M 是行号（从 1 开始）
  - `line_range` 用于明确区分同一文件的哪些行属于哪个 sub-milestone
  - ⚠️ 行号范围**只能**出现在结构化的 `line_range` 字段中，**不能**仅写在 description 文本里（如 "lines 1-82"）
  - 如果文件没有被跨 sub-milestones 拆分，则 `line_range` 字段可选
- 不允许只有 `description` 而没有 `files` 和 `hunks` 的 partial commits

---

## 使用示例：M2 拆分流程

### 输入

```
Milestone M2:
- 5 commits: ['abc123', 'def456', 'ghi789', 'jkl012', 'mno345']
- 1,200 src_loc
```

### Phase 1: Commit 分类

```
abc123: 100 src_loc → 保持不变
def456: 250 src_loc → 保持不变
ghi789: 600 src_loc → 需要拆分 ← 唯一需要拆分的
jkl012: 150 src_loc → 保持不变
mno345: 100 src_loc → 保持不变
```

### Phase 2: Hunk 拆分

**对 ghi789 (600 src_loc) 拆分**:

```
ghi789 contains:
  ├─ src/connection.py (modified file): 3 hunks
  │   ├─ hunk 0: modify __init__() (80 src_loc)
  │   ├─ hunk 1: modify connect() (100 src_loc)
  │   └─ hunk 2: add close() (70 src_loc)
  └─ src/fetch.py (new file, 350 src_loc): 1 giant hunk
      → File too large, split by code structure:
      ├─ sub-hunk 0: FetchRequest class (200 src_loc)
      └─ sub-hunk 1: helper functions (150 src_loc)

Split result: 9 units
  1. abc123 (complete commit)
  2. def456 (complete commit)
  3. ghi789:connection.py:hunk0
  4. ghi789:connection.py:hunk1
  5. ghi789:connection.py:hunk2
  6. ghi789:fetch.py:FetchRequest
  7. ghi789:fetch.py:helpers
  8. jkl012 (complete commit)
  9. mno345 (complete commit)
```

### Phase 3: 语义聚合

```
Group by functionality:
  M2.1 "Connection Management": [abc123, ghi789:connection.py:*] → 280 src_loc
  M2.2 "Request Handling": [ghi789:fetch.py:FetchRequest] → 200 src_loc
  M2.3 "Helper Utilities": [ghi789:fetch.py:helpers, mno345] → 250 src_loc
  M2.4 "Integration Tests": [def456, jkl012] → 400 src_loc

Result: 4 sub-milestones
```

### Phase 4: 依赖推断

```
Textual dependencies:
  M2.2 modifies code created by M2.1 in connection.py
  → M2.2 depends on M2.1

Def-use dependencies:
  M2.3 calls FetchRequest class defined in M2.2
  → M2.3 depends on M2.2

Import dependencies:
  M2.4 imports from M2.1 (connection.py)
  → M2.4 depends on M2.1

Dependency graph:
  M2.1 → M2.2 → M2.3
    ↓
  M2.4
```

### Phase 5: 验证

```
✓ Src_loc conservation: 1200 ≈ 1130 (94%)
✓ CV: 0.35 < 1.0
✓ Valid DAG: no cycles
✓ Max sub: 400 < 600
```

### 最终输出 (JSON)

完整的报告文件保存为 `M2_split_report.json`:

```json
{
  "split_metadata": {
    "version": "V2.0",
    "split_date": "2025-10-26",
    "milestone_id": "M2",
    "total_commits": 5,
    "splitter": "claude-sonnet-4.5"
  },
  "original_milestone": {
    "id": "M2",
    "title": "WebAssembly/Emscripten Platform Support",
    "commits": ["abc123", "def456", "ghi789", "jkl012", "mno345"],
    "src_loc": 1200,
    "category": "FEATURE",
    "description": "Enable urllib3 to run in WebAssembly/Emscripten environments"
  },
  "sub_milestones": [
    {
      "id": "M2.1",
      "title": "Connection Management",
      "description": "Core connection infrastructure with timeout handling and retry logic",
      "category": "FEATURE",
      "mini_srs": "Implement the foundational connection management system including HTTPConnection base class with init, connect, and close methods. Handle connection timeouts, retry logic, and proper cleanup.",
      "commits": [
        {
          "sha": "abc123",
          "split_type": "complete",
          "src_loc": 100
        },
        {
          "sha": "ghi789",
          "split_type": "partial",
          "files": ["src/connection.py"],
          "hunks": [
            {
              "file": "src/connection.py",
              "description": "Modify __init__() method to support new parameters",
              "line_range": {"start": 15, "end": 45},
              "src_loc": 80
            },
            {
              "file": "src/connection.py",
              "description": "Modify connect() method with retry logic",
              "line_range": {"start": 78, "end": 120},
              "src_loc": 100
            }
          ],
          "src_loc": 180
        }
      ],
      "total_src_loc": 280
    },
    {
      "id": "M2.2",
      "title": "Request Handling",
      "description": "Fetch request implementation with header and body handling",
      "category": "FEATURE",
      "mini_srs": "Create the FetchRequest class to handle HTTP requests in Emscripten environment. Support custom headers, request body serialization, and proper content-type handling.",
      "commits": [
        {
          "sha": "ghi789",
          "split_type": "partial",
          "files": ["src/fetch.py"],
          "hunks": [
            {
              "file": "src/fetch.py",
              "description": "Main fetch request class with headers and body handling",
              "line_range": {"start": 1, "end": 200},
              "src_loc": 200
            }
          ],
          "src_loc": 200
        }
      ],
      "total_src_loc": 200
    },
    {
      "id": "M2.3",
      "title": "Helper Utilities",
      "description": "Utility functions for URL parsing and header processing",
      "category": "FEATURE",
      "mini_srs": "Implement helper utilities including URL parser, header normalizer, and common utility functions. Add small utility functions for general use.",
      "commits": [
        {
          "sha": "ghi789",
          "split_type": "partial",
          "files": ["src/fetch.py"],
          "hunks": [
            {
              "file": "src/fetch.py",
              "description": "Helper functions for URL parsing and header processing",
              "line_range": {"start": 201, "end": 350},
              "src_loc": 150
            }
          ],
          "src_loc": 150
        },
        {
          "sha": "mno345",
          "split_type": "complete",
          "src_loc": 100
        }
      ],
      "total_src_loc": 250
    },
    {
      "id": "M2.4",
      "title": "Integration Tests",
      "description": "Comprehensive test suite and edge cases",
      "category": "FEATURE",
      "mini_srs": "Add comprehensive integration tests covering connection lifecycle, request/response handling, and edge cases. Include tests for timeout behavior, error handling, and retry logic.",
      "commits": [
        {
          "sha": "def456",
          "split_type": "complete",
          "src_loc": 250
        },
        {
          "sha": "jkl012",
          "split_type": "complete",
          "src_loc": 150
        },
        {
          "sha": "ghi789",
          "split_type": "partial",
          "files": ["src/connection.py"],
          "hunks": [
            {
              "file": "src/connection.py",
              "description": "Add close() method for cleanup",
              "line_range": {"start": 145, "end": 180},
              "src_loc": 70
            }
          ],
          "src_loc": 70
        }
      ],
      "total_src_loc": 470
    }
  ],
  "dependencies": [
    {
      "source_id": "M2.2",
      "target_id": "M2.1",
      "type": "def-use",
      "description": "FetchRequest uses connection classes defined in M2.1"
    },
    {
      "source_id": "M2.3",
      "target_id": "M2.2",
      "type": "def-use",
      "description": "Helper functions process FetchRequest objects from M2.2"
    },
    {
      "source_id": "M2.4",
      "target_id": "M2.1",
      "type": "import",
      "description": "Tests import connection classes from M2.1"
    },
    {
      "source_id": "M2.4",
      "target_id": "M2.2",
      "type": "import",
      "description": "Tests import FetchRequest from M2.2"
    },
    {
      "source_id": "M2.4",
      "target_id": "M2.3",
      "type": "import",
      "description": "Tests use helper utilities from M2.3"
    }
  ],
  "split_summary": {
    "total_sub_milestones": 4,
    "total_src_loc": 1200,
    "original_src_loc": 1200,
    "src_loc_delta": 0,
    "src_loc_conservation_rate": 1.0,
    "size_distribution": {
      "min": 200,
      "max": 470,
      "mean": 300,
      "median": 265,
      "std_dev": 110.5
    },
    "validation": {
      "src_loc_conserved": true,
      "all_commits_included": true,
      "dag_valid": true,
      "balance_acceptable": true,
      "cv": 0.37
    },
    "split_approach": "Hybrid hunk-based and semantic aggregation. Large commit ghi789 (600 src_loc) was split by hunks and sub-hunks based on file structure and code boundaries. Connection-related hunks aggregated into M2.1, FetchRequest class into M2.2, helper functions into M2.3. Tests and close() method aggregated into M2.4 based on functional dependencies."
  },
  "notes": [
    "Commit ghi789 was the primary split target (600 src_loc)",
    "src/connection.py split into 3 hunks distributed across M2.1 (init, connect) and M2.4 (close)",
    "src/fetch.py split into 2 sub-hunks: M2.2 (FetchRequest class, 200 src_loc) and M2.3 (helpers, 150 src_loc)",
    "Tests (def456, jkl012) aggregated with close() method into M2.4 for coherent testing milestone",
    "Dependency inference used def-use and import analysis",
    "CV of 0.37 indicates good balance across sub-milestones",
    "All sub-milestones within acceptable size range (200-470 src_loc)"
  ]
}
```

---

## 重要提示

### 关于 Hunk 拆分

- **修改文件的 hunks**: 通常大小合理，直接使用
- **新增文件**: 只有一个巨大的 hunk，需要特殊处理
- **Hunk header 格式**: `@@ -10,5 +10,8 @@` 表示旧文件第10行开始5行，新文件第10行开始8行

### 关于代码结构识别

- **Python**: 查找 `class ClassName:` 和 `def function_name():`
- **简化方法**: 使用正则表达式
- **完整方法**: 使用 AST 解析（更准确但更复杂）

### 关于依赖推断

- **优先级**: Def-use > Import > Textual
- **Textual 依赖**: 最完整但最复杂，可选使用
- **Def-use 依赖**: 覆盖大多数实际依赖，推荐使用
- **Import 依赖**: 最简单最可靠，必须使用

### 关于 Src_loc 守恒

- **为什么不完全相等**: Git diff 的计算方式略有差异
- **可接受范围**: ±5% 容差
- **常见问题**: Hunk 包含上下文行（空格开头的行）

---

## 成功标准

拆分成功需满足：

- ✅ **Src_loc 守恒**: 拆分后总 src_loc 在原始的 95%-105% 范围内
- ✅ **大小合理**: 所有 sub-milestones 在 50-600 src_loc 范围内
- ✅ **平衡性**: CV < 1.0（标准差/平均值）
- ✅ **DAG 有效**: 依赖图无环
- ✅ **功能清晰**: 每个 sub-milestone 有明确的功能描述
- ✅ **依赖合理**: 不存在过度依赖或孤立节点

---

**文档版本**: v2.0 (Hunk-based)
**维护者**: DeepCommit Team
**最后更新**: 2025-10-26
