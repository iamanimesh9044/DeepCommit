# Milestone拆分任务 - Agent Prompt模板

> ⚠️ **DEPRECATED** — 本 prompt 已不在 active pipeline 路径上(无 Python 代码 import,主 pipeline `main_multi_stage.py` 使用 `deepcommit/pipeline/prompts/stage_breakdown_v1/milestone_decompose.md`)。
> 仅保留作为历史 / 设计参考。**新工作请使用主 pipeline 的 stage_breakdown_v1 prompts。**

---

# 📋 第一部分：中文版Prompt

> **用途说明**：
> - 本部分为**完整的中文版任务Prompt**，供人类阅读理解
> - 用于设计、讨论、评审Milestone拆分任务的整体方案
> - 与第二部分的英文版Agent Prompt在结构上完全对应（Phase 1-6 ↔ Step 1-6）
> - 可作为编写和优化第二部分英文Prompt的设计蓝本

---

## 任务说明

将大型milestone智能拆分为多个功能内聚、大小合理、依赖清晰的sub-milestone。

**任务目标**：
- 将过大的milestone（src_loc > 500）拆分成合理大小的sub-milestones（150-500 src_loc）
- 确保每个sub-milestone功能内聚、可独立测试
- 建立清晰的依赖关系，形成有效的sub-milestone DAG
- 识别可并行开发的机会，提升开发效率

**适用场景**：
- ✅ 单个milestone的src_loc超过500行
- ✅ Milestone包含多个PR或大量commits（>10个）
- ✅ 需要改善工作量平衡性，降低CV (Coefficient of Variation)
- ✅ 希望识别可并行开发的子任务

---

## 输入数据

### 1. 待拆分的Milestone

包含以下信息：
- **id**: Milestone标识符（如"M2"）
- **title**: Milestone标题
- **commits**: Commits列表（SHA列表）
- **src_loc**: 源代码变更行数
- **commit_pr_mapping**: Commit到PR的映射关系
- **category**: 类别（FEATURE/BUGFIX/STABILITY等）
- **time_range**: 时间范围（start_time → end_time）

### 2. 可用的数据资源

Agent可以通过读取本地文件和使用工具访问以下数据资源：

**Git仓库**:
- 查询commit详情（message, author, timestamp）
- 获取commit diff和文件变更
- 计算每个commit的LoC统计
- **访问方式**: 通过MCP工具 `get_commit_context(sha)`

**PR数据**:
- **数据文件路径**:
  - `DATA/github_data/preprocessed/{repo_name}/pr_commits.jsonl` - PR到commits的映射
  - `DATA/github_data/preprocessed/{repo_name}/pulls/{pr_number}.json` - PR详情
- **访问方式**: **直接读取JSON/JSONL文件**（不使用MCP tools）
- **数据结构** (`pr_commits.jsonl`每行):
  ```json
  {
    "pr_number": 3195,
    "merge_commit_sha": "1812eac",
    "commits": ["89bdcd67", "f3a8b12c", ...],  // fine-grained commits
    "title": "Add Emscripten support",
    "state": "merged"
  }
  ```
- **数据结构** (`pulls/{pr_number}.json`):
  ```json
  {
    "number": 3195,
    "title": "Add Emscripten support",
    "commits_count": 54,
    "additions": 1500,
    "deletions": 200,
    "changed_files": 15,
    "merged_at": "2023-11-15T10:30:00Z"
  }
  ```

**Issue数据**:
- **数据文件路径**:
  - `DATA/github_data/preprocessed/{repo_name}/issues/{issue_number}.json` - Issue详情
- **访问方式**: **直接读取JSON文件**（不使用MCP tools）
- **数据结构**:
  ```json
  {
    "number": 3190,
    "title": "Support for Emscripten",
    "body": "Add WebAssembly/Emscripten support...",
    "labels": ["enhancement", "feature"],
    "state": "closed",
    "created_at": "2023-10-01T...",
    "closed_at": "2023-11-15T..."
  }
  ```
- **用途**：
  - 理解功能背景和需求上下文
  - 识别相关的PR和commits分组
  - 辅助sub-milestone的功能描述编写

**Commit DAG**:
- **数据文件路径**: `DATA/github_data/preprocessed/{repo_name}/dag/commit_dag.pkl`
- ⚠️ **重要提示**: 在拆分fine-grained commits场景下，这些细粒度commits通常**不在**Commit DAG中（它们在PR分支上），因此**不能用于**推断sub-milestones之间的依赖关系
- **替代方法**: 基于功能逻辑、时间顺序、文件修改模式进行依赖推断

**已有的Milestone数据** (`List[Milestone]`):
- 用于避免ID冲突
- 了解上下文和相关milestone

---

## 工作流程

### Phase 1: 分析阶段

**目标**：识别大commits并获取fine-grained commits

**步骤**：

1. **检查milestone内commits的基本信息**
   - 使用MCP工具`get_commit_context(sha)`获取每个commit详情
   - 计算每个commit的src_loc
   - 识别过大的commits（如 > 400 src_loc）

2. **追溯大commits的关联PR**
   - 通过commit message或commit_pr_mapping查找PR编号
   - 检查是否为squash merge（parent_count = 1且src_loc很大）

3. **获取PR的fine-grained commits**
   - **读取`pr_commits.jsonl`文件**，根据PR编号或merge_commit_sha查找对应行
   - 提取该PR的所有fine-grained commits列表
   - 用这些fine-grained commits替换原始的大merge commit
   - **如果需要PR详情**：读取`pulls/{pr_number}.json`文件获取additions、deletions等统计
   - 如果PR数据不可用，标记为需要虚拟拆分

4. **（可选）查看相关Issue背景**
   - 如果commit message或PR中提到issue编号（如#3190）
   - **读取`issues/{issue_number}.json`文件**了解功能需求背景
   - 用于后续的功能分组和描述编写

5. **构建完整的commits列表**
   - 合并所有commits（包括fine-grained commits）
   - 为每个commit计算src_loc和文件变更信息
   - 保留commit元数据（message, author, timestamp, files_changed等）

**数据访问方式总结**：
- ✅ **Commit详情**: 使用MCP工具 `get_commit_context(sha)`
- ✅ **PR数据**: 直接读取本地JSON/JSONL文件
- ✅ **Issue数据**: 直接读取本地JSON文件
- ⚠️ **依赖关系**: 基于功能逻辑、时间顺序、文件修改推断（不使用Commit DAG）

**输出**：扩展后的commits列表，每个commit包含详细元数据

---

### Phase 2: 分组阶段

**目标**：将相关commits分组为sub-milestones

**分组策略**（根据实际情况选择）：

**策略A: 功能相关性分组** ⭐推荐
- 分析每个commit的message和diff
- 识别功能模块边界（如"基础架构"、"流式请求"、"错误处理"）
- 将实现相关功能的commits分为一组
- **适用**：Milestone功能边界清晰，commit message质量高

**策略B: 文件相似度分组**
- 计算commits之间修改文件的重叠度（Jaccard相似度）
- 使用聚类算法（如层次聚类）分组
- **适用**：代码模块化程度高，文件划分清晰

**策略C: 时间顺序分组**
- 按时间顺序划分开发阶段
- 识别时间间隔和重大变更节点
- 每个阶段作为一个sub-milestone
- **适用**：开发周期长，有明显的阶段性特征

**策略D: 混合策略**
- 结合功能、文件、时间多个维度
- 大PR按功能拆分，小PR保持完整
- **适用**：复杂milestone，需要灵活处理

**分组约束**：
- 目标大小：150-500 src_loc per sub-milestone
- 例外：初始架构可达600 src_loc（如果不可再分）
- 每个分组应该功能内聚、可独立测试

**输出**：Commits分组列表，每组附带分组理由

---

### Phase 3: 生成Sub-Milestones

**目标**：为每个分组创建sub-milestone定义

**步骤**：

1. **生成ID**
   - 格式：`{parent_id}.{序号}`
   - 示例：M2.1, M2.2, M2.3
   - 按时间或依赖顺序编号

2. **生成title**（中文，6-15字符）
   - 基于commits的共同主题
   - 示例："Emscripten基础架构"、"流式请求实现"

3. **生成mini_srs描述**（中文，100-200字符）
   - 说明实现的功能、涉及的模块、重要性
   - 模板："实现[功能]：[方法]；涉及模块：[文件]；作用：[重要性]"

4. **生成rationale**（中文，50-100字符）
   - 解释为何这些commits属于同一组
   - 强调功能相关性或依赖关系

5. **计算统计信息**
   - `loc`: 总代码行数变更
   - `src_loc`: 源代码行数变更
   - `touched_files`: 修改的文件列表
   - `start_time`, `end_time`: 时间范围
   - `category`: 类别（继承或推断）

**输出**：完整的sub-milestone定义列表

---

### Phase 4: 推断依赖关系

**目标**：确定sub-milestones之间的依赖关系

**⚠️ 重要说明**：
在拆分fine-grained commits场景下，这些细粒度commits通常不在原始的Commit DAG中（它们在PR分支上，未直接合并到主分支），因此**不能直接使用Commit DAG**来推断sub-milestones之间的依赖关系。需要基于以下方法进行推断：

**推断方法**：

1. **基于时间顺序和文件修改**（主要方法）
   - 如果A和B修改相同文件，且A的时间晚于B → A可能依赖B
   - 检查修改的代码区域：如果A修改的代码引用了B新增的代码 → A依赖B
   - 如果A和B修改相同文件的相同区域 → 存在文本冲突风险

2. **基于功能逻辑**（语义推断）
   - "错误处理"依赖"核心功能"
   - "优化增强"依赖"基础实现"
   - "集成"依赖"各个组件"
   - "测试"依赖"功能实现"

3. **基于commit message分析**
   - 检查commit message中的关键词："fix", "based on", "depends on"等
   - 识别功能演进顺序：基础 → 核心 → 增强 → 优化

**输出格式**：
```json
{
  "source_id": "M2.2",
  "target_id": "M2.1",
  "dependency_tags": "FUNCTIONAL_DEPENDENCY",
  "dependency_notes": "流式请求依赖基础架构提供的connection和fetch接口"
}
```

**dependency tags**：

- **`FUNCTIONAL_DEPENDENCY`（功能依赖）**：
  - **定义**: 基于功能逻辑、时间顺序、文件修改模式推断的依赖关系
  - **识别方式**:
    - 分析功能演进顺序（基础 → 核心 → 增强）
    - 检查文件修改时间和代码引用关系
    - 基于commit message中的依赖提示
    - 识别是否引用target新增的代码/接口
  - **影响**: 建议按顺序开发，target应先完成
  - **示例**: "M2.2流式请求依赖M2.1基础架构提供的connection和fetch接口"
  - **严重性**: ⚠️ 高 - 强烈建议遵守的依赖

- **`TEXTUAL_CONFLICT`（文本冲突）**：
  - **定义**: 两个sub-milestone修改了相同文件的相同或相近代码区域，可能产生合并冲突
  - **识别方式**: 分析commits的diff，检测文件级别和行级别的重叠
  - **影响**: 建议串行开发，或需要频繁同步代码和解决冲突
  - **示例**: "M2.2和M2.3都修改src/connection.py的ConnectionHandler类，存在冲突风险"
  - **严重性**: ⚠️ 中 - 可并行但需要额外协调成本

- **`REGRESSION`（回归风险）**：
  - **定义**: 一个sub-milestone的修改可能影响另一个sub-milestone已实现的功能
  - **识别方式**: 检测是否修改了其他sub-milestone依赖的基础模块或核心逻辑
  - **影响**: 需要回归测试，确保后续修改不破坏已有功能；建议在依赖方完成后再修改基础模块
  - **示例**: "M2.5修改基础架构的错误处理机制，可能影响M2.2-M2.4已实现的功能，需回归测试"
  - **严重性**: ⚠️ 中-低 - 可并行但需加强测试

**标签组合**：
- 一个依赖关系可以有多个标签，如：`["FUNCTIONAL_DEPENDENCY", "TEXTUAL_CONFLICT"]`
- 优先级：`FUNCTIONAL_DEPENDENCY` > `TEXTUAL_CONFLICT` > `REGRESSION`
- 如果只有`TEXTUAL_CONFLICT`或`REGRESSION`，不构成DAG中的强依赖边，但需要在dependency_notes中说明风险

**输出**：依赖关系列表（形成有效DAG，仅包含FUNCTIONAL_DEPENDENCY类型的边）

---

### Phase 5: 验证与优化

**目标**：验证拆分质量并进行必要的优化

**验证检查项**：

| 检查类别 | 检查项 | 失败处理 |
|---------|--------|---------|
| **完整性** | 所有commits被包含 | 找到遗漏commits，补充到相关sub-milestone |
| **完整性** | 无重复commits | 移除重复，优先保留在功能最相关的sub-milestone中 |
| **完整性** | src_loc总和≈原值(±5%) | 重新计算，检查是否有commits被错误过滤 |
| **平衡性** | CV < 1.0 | 拆分最大sub-milestone或合并最小的 |
| **平衡性** | 所有sub-milestone在50-600 src_loc | 调整分组 |
| **依赖性** | DAG无环 | 重新分析依赖，打破循环依赖 |
| **内聚性** | 每个sub-milestone功能单一 | 重新分组，分离不相关commits |

**优化迭代**：
- 第1次失败 → 自动调整分组
- 第2次失败 → 尝试不同分组策略
- 第3次失败 → 输出警告，请求人工review
- ⚠️ 最多迭代3次，避免无限循环

**输出**：验证状态报告和任何警告信息

---

### Phase 6: 生成报告

**目标**：生成完整的JSON格式拆分报告

**报告内容**：

#### 1. Sub-Milestones列表
```json
{
  "id": "M2.1",
  "parent_id": "M2",
  "title": "Emscripten基础架构",
  "commits": ["89bdcd67", ...],
  "mini_srs": "建立Emscripten模块的初始架构...",
  "rationale": "这些commits共同建立了基础架构",
  "loc": 650,
  "src_loc": 470,
  "touched_files": ["file1.py", ...],
  "category": "FEATURE",
  "start_time": "2023-11-10T19:32:03+00:00",
  "end_time": "2023-11-10T19:32:03+00:00"
}
```

#### 2. 依赖关系图
```json
{
  "source_id": "M2.2",
  "target_id": "M2.1",
  "dependency_tags": "CODE_DEPENDENCY",
  "dependency_notes": "流式请求依赖基础架构"
}
```

#### 3. 统计摘要
- 拆分前后的对比（src_loc分布、CV改善）
- Sub-milestones数量和大小统计

#### 4. 警告和建议
- 仍然较大的sub-milestone
- 潜在的并行开发机会
- 需要特别关注的依赖关系

**输出格式**：JSON格式，包含上述所有内容（参考第二部分Step 6的详细schema）

**输出**：完整的拆分报告JSON文件

---

<!-- 以下内容已注释，保留备用

<!-- ---

## 关键特性

1. **智能处理Squash Merge**: 自动从PR获取fine-grained commits，还原真实的开发历史
2. **多策略智能分组**: 支持功能、文件、时间等多种分组策略，灵活适应不同场景
3. **精确依赖推断**: 基于commit DAG自动识别依赖关系，避免人为错误
4. **全面质量验证**: 自动检查完整性、平衡性、有效性，确保拆分质量
5. **并行机会识别**: 分析拓扑结构，识别可并行开发的sub-milestone，提升效率 -->

---

# 🤖 第二部分：Agent Prompt正文

以下内容可直接作为Agent的任务输入，无需修改。

---

# Task: Split Large Milestone into Sub-Milestones

You are an AI agent specialized in analyzing Git repository history and decomposing large software milestones into smaller, manageable sub-milestones.

## Mission

Analyze the given milestone and split it into sub-milestones that are:
1. **Functionally coherent** - Each sub-milestone has a clear, focused purpose
2. **Reasonably sized** - 150-500 src_loc per sub-milestone (ideally)
3. **Properly ordered** - Clear dependency relationships based on commit DAG
4. **Parallelizable** - Identify opportunities for concurrent development

## Input Data

**Milestone ID**: {milestone_id}
**Title**: {milestone_title}
**Current src_loc**: {milestone_src_loc} lines
**Total LoC**: {milestone_loc} lines
**Commits**: {milestone_commits}
**Commit-PR Mapping**: {commit_pr_mapping}
**Category**: {milestone_category}
**Time Range**: {milestone_start_time} → {milestone_end_time}

**Configuration**:
- Max src_loc per sub-milestone: {config_max_src_loc}
- Min src_loc per sub-milestone: {config_min_src_loc}
- Split strategy: {config_split_strategy}

**Available Resources**:

1. **Git Repository**: Use MCP tool `get_commit_context(sha)`
   - Access commit details: message, author, timestamp, diff, stats

2. **PR Data**: **Read local JSON/JSONL files directly** (DO NOT use MCP tools)
   - `DATA/github_data/preprocessed/{repo_name}/pr_commits.jsonl` - PR to commits mapping
   - `DATA/github_data/preprocessed/{repo_name}/pulls/{pr_number}.json` - PR details
   - Data structure (`pr_commits.jsonl` per line):
     ```json
     {
       "pr_number": 3195,
       "merge_commit_sha": "1812eac",
       "commits": ["89bdcd67", "f3a8b12c", ...],
       "title": "Add Emscripten support",
       "state": "merged"
     }
     ```

3. **Issue Data**: **Read local JSON files directly** (DO NOT use MCP tools)
   - `DATA/github_data/preprocessed/{repo_name}/issues/{issue_number}.json`
   - Data structure:
     ```json
     {
       "number": 3190,
       "title": "Support for Emscripten",
       "body": "Add WebAssembly/Emscripten support...",
       "labels": ["enhancement", "feature"],
       "state": "closed"
     }
     ```

4. **Commit DAG**: ⚠️ **Not applicable for fine-grained commits splitting**
   - Fine-grained commits from PR branches typically do NOT exist in the main branch Commit DAG
   - Dependency inference should be based on functional logic, temporal order, and file modifications

## Execution Steps

### Step 1: Analysis

1. **Examine each commit in the milestone**
   - Use MCP tool `get_commit_context(sha)` to get commit details
   - Calculate src_loc for each commit
   - Identify large commits (e.g., > 400 src_loc)

2. **Identify merge commits and their associated PRs**
   - Check commit message or commit_pr_mapping for PR number
   - Detect squash merges (parent_count = 1 and large src_loc)

3. **Retrieve fine-grained commits from PR data**
   - **Read `pr_commits.jsonl` file**, search by PR number or merge_commit_sha
   - Extract the list of fine-grained commits for that PR
   - Replace the large merge commit with these fine-grained commits
   - **If need PR details**: Read `pulls/{pr_number}.json` for additions/deletions stats
   - If PR data unavailable, mark for virtual split

4. **Extract commit messages and identify functional themes**
   - Analyze commit messages for keywords (e.g., "connection", "streaming", "error")
   - Group related functionality

5. **Analyze file modification patterns**
   - Track which files each commit modifies
   - Identify file overlap between commits

6. **(Optional) Check related issues for functional context**
   - If commit/PR mentions issue number (e.g., #3190)
   - **Read `issues/{issue_number}.json` file** to understand feature requirements
   - Use for grouping and description writing

7. **Build complete commits list**
   - Merge all commits (including fine-grained ones)
   - Calculate metadata for each commit

**Data Access Summary**:
- ✅ **Commit details**: Use MCP tool `get_commit_context(sha)`
- ✅ **PR data**: Read local JSON/JSONL files directly
- ✅ **Issue data**: Read local JSON files directly
- ⚠️ **Dependencies**: Infer from functional logic, temporal order, and file modifications (NOT from Commit DAG)

**Expected output**: List of all commits (including fine-grained ones) with metadata

### Step 2: Grouping

Group related commits into sub-milestones using one of these strategies:

**Strategy A: Functional Coherence** (Recommended)
- Analyze commit messages for common keywords/themes
- Group commits implementing the same feature or functional area
- Example: "connection", "response", "error handling", "optimization"

**Strategy B: File-based Clustering**
- Calculate file overlap between commits
- Group commits modifying similar file sets
- Use Jaccard similarity threshold > 0.3

**Strategy C: Temporal Evolution**
- Identify development phases from commit timeline
- Group by: initial setup → core implementation → enhancements → fixes

**Strategy D: PR Boundaries**
- If multiple PRs with independent features
- Each PR becomes one sub-milestone (if size appropriate)

**Grouping constraints**:
- Target: 150-500 src_loc per group
- Exception: Initial architecture can be up to 600 src_loc if indivisible
- Each group should be testable independently
- Minimum 2 commits per group (unless single large PR)

**Expected output**: List of commit groups with rationale for grouping

### Step 3: Sub-Milestone Generation

For each group, create a sub-milestone with:

**1. ID**: `{parent_id}.{number}`
- Sequential numbering based on temporal or dependency order
- Example: M2.1, M2.2, M2.3

**2. Title** (Chinese, 6-15 characters)
- Concise and descriptive
- Examples:
  - "Emscripten基础架构"
  - "流式请求实现"
  - "HTTP/2协议支持"
  - "错误处理机制"

**3. mini_srs** (Chinese, 100-200 characters)
- Summarize what this sub-milestone accomplishes
- Template: "实现[功能]: [方法/细节]; 涉及模块: [文件]; 作用: [重要性]"
- Example: "建立Emscripten模块的初始架构和基础类：connection.py(186行), fetch.py(376行); 这是整个Emscripten支持的基石; Main modules: src/urllib3/contrib/emscripten/"

**4. rationale** (Chinese, 50-100 characters)
- Explain why these commits belong together
- Example: "这些commits共同建立了流式请求的核心功能，依赖基础架构"

**5. Metadata** (computed):
```json
{
  "commits": ["sha1", "sha2", ...],
  "loc": 650,
  "src_loc": 470,
  "touched_files": ["file1.py", "file2.py", ...],
  "start_time": "2023-11-10T19:32:03+00:00",
  "end_time": "2023-11-10T19:32:03+00:00",
  "category": "FEATURE"
}
```

**Expected output**: Complete sub-milestone definitions

### Step 4: Dependency Inference

Determine dependencies between sub-milestones:

**⚠️ Important Note**:
When splitting fine-grained commits, these commits typically do NOT exist in the original Commit DAG (they're on PR branches, not directly merged to main). Therefore, **DO NOT use Commit DAG** to infer sub-milestone dependencies. Instead, use the following methods:

**Methods**:

1. **Time-based and file modification analysis** (primary method)
   - If A and B modify the same files AND A's time > B's time → A may depend on B
   - Check code regions: if A modifies code that references B's new additions → A depends on B
   - If A and B modify the same regions in same files → textual conflict risk

2. **Functional logic** (semantic inference)
   - "Error handling" depends on "Core functionality"
   - "Optimization" depends on "Base implementation"
   - "Integration" depends on "All components"
   - "Tests" depend on "Feature implementation"

3. **Commit message analysis**
   - Check for keywords: "fix", "based on", "depends on"
   - Identify functional evolution: base → core → enhancement → optimization

**Output format**:
```json
{
  "source_id": "M2.2",
  "target_id": "M2.1",
  "dependency_tags": "FUNCTIONAL_DEPENDENCY",
  "dependency_notes": "Streaming implementation depends on base architecture's connection and fetch interfaces"
}
```

**Dependency Tags**:

- **`FUNCTIONAL_DEPENDENCY`** (Functional Dependency):
  - **Definition**: Dependency inferred from functional logic, temporal order, and file modification patterns
  - **Detection**:
    - Analyze functional evolution sequence (base → core → enhancement)
    - Check file modification timestamps and code reference patterns
    - Look for dependency hints in commit messages
    - Identify if source references code/interfaces added by target
  - **Impact**: Recommend sequential development, target should complete first
  - **Example**: "M2.2 streaming depends on M2.1 base architecture's connection and fetch interfaces"
  - **Severity**: ⚠️ High - Strongly recommended dependency

- **`TEXTUAL_CONFLICT`** (Textual Conflict):
  - **Definition**: Two sub-milestones modify the same or adjacent code regions, may cause merge conflicts
  - **Detection**: Analyze commit diffs, detect file-level and line-level overlaps
  - **Impact**: Recommend sequential development, or require frequent synchronization
  - **Example**: "M2.2 and M2.3 both modify ConnectionHandler class in src/connection.py"
  - **Severity**: ⚠️ Medium - Can parallelize but needs extra coordination

- **`REGRESSION`** (Regression Risk):
  - **Definition**: One sub-milestone's changes may affect another's implemented functionality
  - **Detection**: Check if modifying base modules or core logic that others depend on
  - **Impact**: Requires regression testing; recommend modifying base after dependents complete
  - **Example**: "M2.5 modifies error handling in base architecture, may affect M2.2-M2.4 features"
  - **Severity**: ⚠️ Medium-Low - Can parallelize but needs thorough testing

**Tag Combinations**:
- One dependency can have multiple tags, e.g., `["FUNCTIONAL_DEPENDENCY", "TEXTUAL_CONFLICT"]`
- Priority: `FUNCTIONAL_DEPENDENCY` > `TEXTUAL_CONFLICT` > `REGRESSION`
- If only `TEXTUAL_CONFLICT` or `REGRESSION`, not a hard edge in DAG, but note risk in dependency_notes

**Expected output**: List of dependencies forming a valid DAG (only FUNCTIONAL_DEPENDENCY edges)

### Step 5: Validation

Verify split quality:

**Completeness check**:
- [ ] All original commits included in sub-milestones
- [ ] No duplicate commits
- [ ] Sum of sub-milestone src_loc ≈ original (±5% acceptable)

**Balance check**:
- [ ] All sub-milestones within 50-600 src_loc
- [ ] Coefficient of Variation (CV) < 1.0
- [ ] Max/min ratio < 10

**Dependency check**:
- [ ] Dependency graph is acyclic (valid DAG)
- [ ] All dependencies based on functional logic and temporal order
- [ ] No missing implicit dependencies

**Coherence check**:
- [ ] Each sub-milestone has clear purpose
- [ ] Title accurately reflects commits
- [ ] Commits within group are functionally related

If any check fails, revise the grouping and try again.

**Expected output**: Validation status and any warnings

### Step 6: Generate Report

Create comprehensive split report in JSON format:

```json
{
  "original_milestone": {
    "id": "...",
    "title": "...",
    "src_loc": 0,
    "commits": [...]
  },

  "sub_milestones": [
    {
      "id": "...",
      "parent_id": "...",
      "title": "...",
      "commits": [...],
      "mini_srs": "...",
      "rationale": "...",
      "category": "...",
      "loc": 0,
      "src_loc": 0,
      "touched_files": [...],
      "start_time": "...",
      "end_time": "..."
    }
  ],

  "dependencies": [
    {
      "source_id": "...",
      "target_id": "...",
      "dependency_type": "sequential",
      "reason": "..."
    }
  ],

  "split_summary": {
    "num_sub_milestones": 0,
    "original_src_loc": 0,
    "max_sub_src_loc": 0,
    "min_sub_src_loc": 0,
    "avg_sub_src_loc": 0.0,
    "cv": 0.0,
    "cv_improvement": "从X降至Y (改善Z%)"
  },

  "split_strategy_used": "...",
  "split_reasoning": "...",

  "warnings": [
    "..."
  ]
}
```

## Special Cases

### Case 1: Squash Merge (Recovering Fine-Grained Commits)

If a merge commit is a squash of many commits:

**Option A: Read from local PR data** ⭐ Recommended
```python
# Read pr_commits.jsonl to get fine-grained commits
import json

# Method 1: Search by PR number
pr_number = 3195
with open('DATA/github_data/preprocessed/{repo_name}/pr_commits.jsonl', 'r') as f:
    for line in f:
        pr_data = json.loads(line)
        if pr_data['pr_number'] == pr_number:
            fine_grained_commits = pr_data['commits']
            break

# Method 2: Search by merge_commit_sha
merge_sha = "1812eac"
with open('DATA/github_data/preprocessed/{repo_name}/pr_commits.jsonl', 'r') as f:
    for line in f:
        pr_data = json.loads(line)
        if pr_data.get('merge_commit_sha', '').startswith(merge_sha):
            fine_grained_commits = pr_data['commits']
            break
```

**Option B: Virtual split** (if PR data unavailable)
1. Analyze merge commit's diff using `get_commit_context(sha)`
2. Group files by module/directory
3. Create sub-milestones based on file groups
4. Mark as `"split_type": "virtual"` in output
5. Add warning about virtual split

### Case 2: Single Huge Commit

If one commit has 800+ src_loc:

**Option A**: Virtual split by file modules (if independent)
**Option B**: Keep as single sub-milestone, mark as "indivisible"

Add rationale explaining the decision.

### Case 3: Multiple Independent PRs

If milestone contains several PRs with different features:

- Split large PRs into multiple sub-milestones
- Keep small PRs as single sub-milestones
- Group trivial fixes into one "fixes and enhancements" sub-milestone

## Success Criteria

Your split is successful if:
- ✅ Largest sub-milestone < 600 src_loc (prefer < 500)
- ✅ CV of sub-milestone sizes < 1.0
- ✅ Dependency graph is a valid DAG
- ✅ Each sub-milestone is testable independently
- ✅ Identified parallel development opportunities (if exist)

## Output Instructions

Return ONLY the JSON report as specified in Step 6. Do not include explanatory text before or after the JSON.

---

# 💡 第三部分：使用建议和最佳实践

## 拆分策略选择指南

### 何时使用功能内聚策略（Strategy A）

**适用场景**：
- Milestone包含明确的功能模块（如M2的架构、流式、错误处理等）
- Commit messages质量高，能清晰表达功能意图
- 需要按功能阶段进行迭代开发

**优点**：
- 功能边界清晰，易于理解和测试
- 符合开发人员的思维模式
- 便于代码审查和文档编写

**实施建议**：
1. 提取commit message中的关键词（connection, streaming, error, integration等）
2. 识别功能演进阶段（基础 → 核心 → 增强 → 优化）
3. 将相关功能的commits归为一组

### 何时使用文件聚类策略（Strategy B）

**适用场景**：
- Commit messages不够清晰或不一致
- 代码库模块化程度高，文件划分清晰
- 需要基于代码结构进行拆分

**优点**：
- 客观性强，不依赖commit message质量
- 自动化程度高
- 适合大规模重构或模块化改造

**实施建议**：
1. 计算commits之间的文件重叠度（Jaccard相似度）
2. 使用聚类算法（如层次聚类）分组
3. 设置相似度阈值（建议0.3-0.5）

### 何时使用时间演进策略（Strategy C）

**适用场景**：
- Milestone跨越较长时间（如M3的9个月）
- 开发过程有明显的阶段性特征
- 存在长时间间隔或重大方向调整

**优点**：
- 符合实际开发时间线
- 易于追踪功能演进历史
- 便于识别开发阶段和里程碑

**实施建议**：
1. 绘制commits的时间分布图
2. 识别时间间隔（如1个月以上的空白期）
3. 按阶段划分：初始实现 → 功能扩展 → 优化完善

### 何时使用PR边界策略（Strategy D）

**适用场景**：
- Milestone包含多个独立的PR
- 每个PR实现不同的功能模块
- PR大小合适（200-600 src_loc）

**优点**：
- 实施最简单，边界天然清晰
- 符合实际的代码审查流程
- 保持了原始PR的完整性

**实施建议**：
1. 统计每个PR的src_loc
2. 大PR（>600 LoC）进一步拆分
3. 小PR（<100 LoC）可以合并

## 拆分粒度控制

### 目标范围

- **理想范围**: 150-500 src_loc
- **可接受范围**: 50-600 src_loc
- **例外情况**: 初始架构可达600+ LoC（如M2.1的470 LoC）

### 过大sub-milestone的处理

**当sub-milestone > 600 src_loc时**：

1. **再次拆分**：
   - 检查是否包含多个功能模块
   - 按文件或子功能进一步拆分

2. **接受并标注**：
   - 如果是不可分割的初始架构
   - 在rationale中说明为何不能再分
   - 添加warning提示开发者重点关注

### 过小sub-milestone的处理

**当sub-milestone < 50 src_loc时**：

1. **合并相关sub-milestone**：
   - 找到功能相关的其他小sub-milestone
   - 合并为一个更大的单元

2. **保留并标注**：
   - 如果是独立的重要功能（如hotfix）
   - 在rationale中说明其重要性

## Squash Merge处理最佳实践

### 识别Squash Merge

**特征**：
- Commit message包含PR引用（如"#3195"）
- Parent count = 1
- PR中显示commits_count >> 1

### 恢复Fine-Grained Commits

**方法1: 从本地文件读取** ⭐推荐
```python
# 读取 pr_commits.jsonl 文件
import json

# 方法A: 根据PR编号查找
pr_number = 3195
with open('DATA/github_data/preprocessed/urllib3_urllib3_2.0.6_2.3.0/pr_commits.jsonl', 'r') as f:
    for line in f:
        pr_data = json.loads(line)
        if pr_data['pr_number'] == pr_number:
            fine_grained_commits = pr_data['commits']
            print(f"Found {len(fine_grained_commits)} commits for PR #{pr_number}")
            break

# 方法B: 根据merge_commit_sha查找
merge_commit = "1812eac"
with open('DATA/github_data/preprocessed/urllib3_urllib3_2.0.6_2.3.0/pr_commits.jsonl', 'r') as f:
    for line in f:
        pr_data = json.loads(line)
        if pr_data.get('merge_commit_sha', '').startswith(merge_commit):
            fine_grained_commits = pr_data['commits']
            pr_number = pr_data['pr_number']
            break
```

**方法2: 虚拟拆分**（当PR数据不可用时）
- 分析merge commit的完整diff
- 按文件或模块分组
- 标注为`"split_type": "virtual"`
- 在warnings中说明

**数据文件位置**：
- `pr_commits.jsonl`: `DATA/github_data/preprocessed/{repo_name}/pr_commits.jsonl`
- PR详情: `DATA/github_data/preprocessed/{repo_name}/pulls/{pr_number}.json`

### 案例：M2的PR #3195处理

**背景**：
- Merge commit: `1812eac`
- 包含54个fine-grained commits
- 数据在本地文件中

**处理步骤**：
1. 从`pr_commits.jsonl`读取PR #3195的54个commits列表
2. 分析commits message识别功能阶段
3. 按阶段分组：基础(8个) → 流式(7个) → 错误(7个) → 集成(13个) → Worker(9个) → 响应(10个)
4. 生成6个sub-milestones

## 依赖关系推断技巧

### 依赖推断方法（适用于Fine-Grained Commits拆分场景）

**⚠️ 重要提示**：
在拆分fine-grained commits场景下，这些细粒度commits通常不在原始的Commit DAG中（它们在PR分支上），因此**不能使用Commit DAG**来推断依赖关系。需要使用以下方法：

**方法1: 基于时间顺序和文件修改**
```python
def infer_dependency_by_time_and_files(sub_a, sub_b):
    """检查A是否依赖B（基于时间和文件修改）"""
    # 获取文件重叠
    files_a = set(sub_a.touched_files)
    files_b = set(sub_b.touched_files)
    common_files = files_a & files_b

    if not common_files:
        return False  # 无文件重叠，无依赖

    # 检查时间顺序
    if sub_a.start_time > sub_b.end_time:
        return True  # A在B之后修改相同文件，可能依赖B

    return False
```

**方法2: 基于功能逻辑推断**
- 分析commit messages和功能描述
- 识别功能演进顺序：基础 → 核心 → 增强 → 优化
- 示例：
  - "错误处理" 依赖 "核心功能"
  - "集成测试" 依赖 "功能实现"
  - "性能优化" 依赖 "基础实现"

**方法3: 基于代码引用分析**
- 检查A的commits是否引用了B新增的类/函数/接口
- 分析import语句和函数调用关系
- 需要结合commit diff分析

### 识别可并行的Sub-Milestones

**条件**：
- Sub-milestone A和B没有直接或间接依赖关系
- 它们位于依赖图的同一层（拓扑层次相同）

**示例 - M3的并行机会**：
```
Layer 0: [M3.1]            ← 基础协议
Layer 1: [M3.2, M3.3]      ← 请求体 和 探测可以并行！

M3.2和M3.3都依赖M3.1，但彼此独立
→ 可以在M3.1完成后同时开发
→ 潜在加速: 1.5x
```

### 避免过度依赖

**常见问题**：
- 将所有sub-milestone串联成一条链
- 忽略潜在的并行机会

**解决方法**：
1. 仅在确实存在数据依赖时才创建dependency
2. 检查是否可以通过接口约定实现并行
3. 询问："B真的需要A完全完成吗？还是只需要A的接口定义？"

## 命名规范

### Title命名原则

**格式**: 6-15个中文字符，简洁描述核心功能

**示例**：
- ✅ "Emscripten基础架构"
- ✅ "流式请求实现"
- ✅ "HTTP/2协议支持"
- ✅ "错误处理机制"
- ❌ "实现Emscripten的connection和fetch基础类" (太长)
- ❌ "修复" (太笼统)

### mini_srs编写原则

**格式**: 100-200字，包含what + how + why

**模板**：
```
实现[功能名称]: [具体做法/关键技术];
涉及模块: [主要文件];
作用: [在整个milestone中的位置和重要性];
Main modules: [英文模块路径];
Test coverage: [测试文件]
```

**示例**：
```
建立Emscripten模块的初始架构和基础类：connection.py初版(186行),
fetch.py框架(376行), response.py初版(85行); 这是整个Emscripten支持
的基石，包含核心数据结构和接口定义; Main modules:
src/urllib3/contrib/emscripten/; Test coverage:
test/contrib/emscripten/test_emscripten.py
```

### rationale编写原则

**格式**: 50-100字，解释为何这些commits属于同一组

**关键点**：
- 功能相关性
- 依赖关系
- 演进阶段

**示例**：
- "这些commits共同建立了HTTP/2的基础协议支持，是后续功能的前提"
- "基于基础架构，实现核心的流式请求功能，使其真正可用"
- "在核心功能基础上添加完整的错误处理机制，提升代码健壮性"

## 质量验证清单

### 拆分前检查

- [ ] 确认milestone确实需要拆分（src_loc > 500）
- [ ] 确认有足够的commits可以分组（>= 3个）
- [ ] 确认可以访问PR数据（用于获取fine-grained commits）
- [ ] 确认了解milestone的功能目标

### 拆分中检查

- [ ] 每个sub-milestone都有明确的功能目标
- [ ] Sub-milestones之间没有明显的功能重叠
- [ ] 大小分布相对均衡（CV < 1.0）
- [ ] 依赖关系符合功能逻辑

### 拆分后验证

- [ ] 所有原始commits都被包含，无遗漏
- [ ] 依赖图是有效的DAG，无环
- [ ] 每个sub-milestone的title和描述准确
- [ ] 识别出至少一个并行机会（如果存在）
- [ ] 最大sub-milestone < 600 src_loc

## 常见问题处理

### Q1: 如何处理测试文件？

**建议**：
- 测试commits随功能commits一起归入相应的sub-milestone
- 不要单独创建"测试"sub-milestone
- 在mini_srs中提及"Test coverage"

### Q2: 如何处理文档commits？

**建议**：
- 如果是功能文档，随功能一起
- 如果是通用文档（如README），合并到最后的"完善"sub-milestone

### Q3: 如何处理hotfix commits？

**建议**：
- 小的hotfix（<50 LoC）合并到"修复和增强"sub-milestone
- 大的hotfix（>100 LoC）可以单独成为sub-milestone

### Q4: 初始架构太大怎么办？

**示例**: M2.1有470 src_loc

**建议**：
1. 尝试按模块拆分（如connection, fetch, response）
2. 如果模块高度耦合，不可拆分，则接受并标注
3. 在rationale中说明："初始架构包含多个高度耦合的核心模块，不宜拆分"

### Q5: 两个sub-milestone应该并行还是串行？

**决策流程**（适用于Fine-Grained Commits拆分场景）：
```
1. 检查功能逻辑 → B需要A的结果吗？
   - 需要 → 串行
   - 不需要 → 继续

2. 检查时间顺序 → A在B之后且修改相同文件吗？
   - 是 → A可能依赖B，建议串行
   - 否 → 继续

3. 检查文件overlap → 修改相同文件的相同区域吗？
   - 是 → 可能有冲突，建议串行
   - 否 → 可并行

注：在fine-grained commits场景下，不能用Commit DAG验证依赖关系
```

## 成功案例参考

### M2拆分案例

**原始状态**:
- 1个milestone，1,601 src_loc
- 8个commits（包含2个大型squash merge）
- 主要PR: #3195 (54 commits), #3427 (68 commits)

**拆分结果**:
- 8个sub-milestones (M2.1-M2.8)
- 最大: M2.1 (470 src_loc) - 初始架构
- 最小: M2.8 (100 src_loc) - 测试和修复
- CV: 0.54 (改善68%)
- 并行机会: M2.5可与M2.2-M2.4部分并行

**关键决策**:
1. PR #3195的54个commits按功能演进分为6个阶段
2. PR #3427独立成为M2.7（Promise集成）
3. 其他小修复合并为M2.8
4. 接受M2.1较大（470 LoC），因为是不可分割的初始架构

### M3拆分案例

**原始状态**:
- 1个milestone，1,001 src_loc
- 11个commits，跨越9个月
- 多个PR，功能边界清晰

**拆分结果**:
- 3个sub-milestones (M3.1-M3.3)
- M3.1: HTTP/2核心协议 (288 src_loc)
- M3.2: 请求体和隧道 (565 src_loc)
- M3.3: 探测和缓存 (148 src_loc)
- CV: 0.49
- 并行机会: M3.2和M3.3可完全并行

**关键决策**:
1. 按功能层次拆分：基础 → 完整性 → 优化
2. 识别M3.2和M3.3的并行机会（加速1.5x）
3. 保持每个sub-milestone功能完整且可测试

## 工具使用提示

### 数据访问方式

**MCP工具**（用于Git数据获取）:

1. **get_commit_context(sha)**:
   - 用途: 获取commit的详细信息
   - 返回: message, author, timestamp, diff, stats (additions/deletions), files
   - 示例: `get_commit_context("89bdcd67")`

2. **check_dependency(commits_a, commits_b)**:
   - ⚠️ **不适用于fine-grained commits拆分场景**
   - 原因: Fine-grained commits不在主分支Commit DAG中
   - 替代方案: 使用功能逻辑、时间顺序、文件修改模式推断依赖

**本地文件读取**（用于PR和Issue数据）:

3. **读取PR数据**:
   ```python
   # pr_commits.jsonl - PR到commits的映射
   with open('DATA/github_data/preprocessed/{repo_name}/pr_commits.jsonl', 'r') as f:
       for line in f:
           pr_data = json.loads(line)
           # pr_data包含: pr_number, merge_commit_sha, commits, title, state

   # pulls/{pr_number}.json - PR详情
   with open(f'DATA/github_data/preprocessed/{repo_name}/pulls/{pr_number}.json', 'r') as f:
       pr_detail = json.load(f)
       # pr_detail包含: number, title, commits_count, additions, deletions
   ```

4. **读取Issue数据**:
   ```python
   # issues/{issue_number}.json - Issue详情
   with open(f'DATA/github_data/preprocessed/{repo_name}/issues/{issue_number}.json', 'r') as f:
       issue = json.load(f)
       # issue包含: number, title, body, labels, state
   ```

**重要提醒**：
- ✅ Commit详情 → 使用MCP工具 `get_commit_context(sha)`
- ✅ PR和Issue数据 → 直接读取本地JSON/JSONL文件
- ⚠️ 依赖关系 → 基于功能逻辑、时间顺序、文件修改推断（**不使用Commit DAG**）
- ❌ 不要使用`search_entities`或`check_dependency`等MCP工具（不适用于fine-grained commits场景）

### 调试建议

**如果拆分结果不理想**:

1. **Sub-milestones太大**: 增加分组数量，降低每组的commits数
2. **Sub-milestones太小**: 减少分组数量，合并相关组
3. **依赖关系复杂**: 重新审视分组逻辑，确保功能内聚
4. **CV仍然很高**: 考虑将大sub-milestone进一步拆分

## 性能优化

### 处理大型milestone（>2000 src_loc）

**建议**:
1. 优先使用PR边界策略快速分组
2. 对大PR再应用功能内聚策略
3. 并行处理多个commits的分析任务

### 处理大量commits（>50个）

**建议**:
1. 使用聚类算法自动分组
2. 设置合理的分组数量（如5-10个）
3. 批量计算commits的相似度矩阵

---

**文档版本**: v2.1
**创建时间**: 2025-10-21
**最后更新**: 2025-10-22
**更新说明**: 针对fine-grained commits拆分场景，移除Commit DAG依赖，改用功能逻辑、时间顺序、文件修改模式推断依赖关系
**维护者**: DeepCommit Team
