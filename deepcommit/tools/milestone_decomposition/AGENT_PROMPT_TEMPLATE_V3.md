# Milestone拆分任务 - Agent Prompt模板 V3

> ⚠️ **DEPRECATED / not on active pipeline path** — 本 prompt 无任何 Python 代码 import 引用,主 pipeline `main_multi_stage.py` 用的是 `deepcommit/pipeline/prompts/stage_breakdown_v1/milestone_decompose.md`(及该目录下的 subagent / deep_analysis prompts)。
> 本文件作为独立设计 / 历史参考保留。**新工作请使用主 pipeline 的 `stage_breakdown_v1` prompts。**

**核心策略**: 两阶段拆分
- Stage 1: Commit-Level Split - 先拆分大Commits（调用Subagent）
- Stage 2: Milestone-Level Split - 再拆分Milestone（聚合成Sub-Milestones）

---

## 任务说明

你的任务是分析给定的milestones集合，识别需要拆分的大milestone，通过两阶段拆分策略将其拆分为多个功能内聚、大小合理、依赖清晰的sub-milestones。

### 核心策略：两阶段拆分

**Stage 1: Commit-Level Split（Commit级拆分）**
- 分析milestone内每个commit的特征，智能判断是否适合拆分
- 对适合拆分的commits调用Commit Split Subagent进行拆分
- 获得拆分后的commit patches集合

**Stage 2: Milestone-Level Split（Milestone级拆分）**
- 将拆分后的commit patches与未拆分的commits整合
- 基于功能相关性、文件关联、依赖关系进行语义聚合
- 生成最终的sub-milestones及其依赖关系

### 任务目标

- 将需要拆分的milestone拆分成功能内聚、大小合理的sub-milestones
- 确保每个sub-milestone功能内聚、可独立理解和测试
- 建立清晰的依赖关系，形成有效的sub-milestone DAG
- 识别可并行开发的机会，提升开发效率

---

## 输入数据

### 1. Milestones集合

数据文件路径：`{milestone_csv_path}`

**CSV格式**（每行一个milestone）:
```csv
id,title,category,commits,src_loc,loc,touched_files,start_time,end_time
M2,WebAssembly/Emscripten Support,FEATURE,"1812eac;7b3f948;...",1601,2100,"src/connection.py;...",2023-11-10T...,2023-11-15T...
```

**字段说明**:
- **id**: Milestone标识符（如"M2"）
- **title**: Milestone标题
- **category**: 类别（FEATURE/BUGFIX/STABILITY/REFACTOR等）
- **commits**: Commit SHAs列表（7位短SHA，分号分隔）
- **src_loc**: 源代码变更行数（src目录下）
- **loc**: 总代码变更行数
- **touched_files**: 修改的文件列表
- **start_time**, **end_time**: 时间范围

### 2. Dependencies关系

数据文件路径：`{dependencies_csv_path}`

**CSV格式**:
```csv
source_id,target_id
M1,M2
M2,M3
```

说明：`source_id → target_id` 表示 **source 必须在 target 之前完成**(target 依赖 source)。即 `source_id` = **prerequisite**(前置), `target_id` = **dependant**(依赖者)。例:`M1,M2` 表示 M2 依赖 M1,M2 在 M1 完成后才能开始。

### 3. 可用的工具和数据资源

**MCP工具**:
- `get_commit_context(sha, expand=['files', 'stats', 'diff'])` - 获取commit详情
- `calculate_commits_src_loc_tool(commits)` - 计算commits的src_loc

**Git仓库**:
- 路径：`{repo_path}`
- 用于commit分析、diff获取、补丁应用等操作

**配置参数**:
- `REPO_SRC_DIRS`: 源代码目录前缀（如"src/"），用于过滤src_loc计算
- `SUB_MILESTONE_TARGET_SIZE`: Sub-milestone参考大小范围（仅供参考，非强制阈值）
- `COMMIT_SPLIT_SIZE_TARGET`: 拆分后每个commit的参考大小（仅供参考，非强制阈值）

---

## 工作流程

### Phase 0: 初始化与分析

**目标**: 识别需要拆分的milestones

**步骤**:

1. **读取milestones数据**
   - 从CSV文件加载所有milestones
   - 解析每个milestone的基本信息（id, commits列表, src_loc等）

2. **识别需要拆分的milestones**
   - 综合分析milestone的规模、复杂度、功能内聚性等维度
   - 参考指标（非硬性阈值）：src_loc、commits数量、touched_files、时间跨度等
   - 按优先级排序（通常先处理规模较大、复杂度较高的）
   - 记录需要拆分的milestone列表及拆分理由

3. **初始化工作环境**
   - 创建输出目录：`{output_dir}/milestone_splits/`
   - 为每个待拆分milestone创建子目录：`{output_dir}/milestone_splits/{milestone_id}/`

**输出**: 待拆分的milestone列表（按优先级排序）

---

### Phase 1: Commit-Level Analysis

**目标**: 识别milestone内需要拆分的commits

对于每个待拆分的milestone，执行以下步骤：

#### 1.1 获取Commits详情

对于milestone中的每个commit，收集以下信息：

**a) 使用MCP工具获取commit详情**
   - `get_commit_context(sha, expand=['files', 'stats', 'diff'])`
   - 获取：message, author, timestamp, diff, files, stats

**b) 计算commit的src_loc**
   - `calculate_commits_src_loc_tool([sha])`
   - 获取：src目录下的代码变更行数

**c) 记录完整的commit元数据**
   - SHA、message、src_loc、additions、deletions、files、timestamp等

#### 1.2 Commit拆分决策

**⚠️ 核心原则**: 优先考虑**拆分收益**和**功能独立性**，而非单纯的代码行数。

综合考虑以下因素，智能决策每个commit是否适合拆分。

对于每个commit，按以下顺序考虑：

---

**🚫 明确不拆分的情况** (满足任一即跳过)

- 规模很小且功能单一
- 单文件配置/文档修改
- 高度耦合的重构（接口+实现必须一起改）
- hotfix或原子性bug修复
- 二进制文件或自动生成的代码

---

**✅ 强烈建议拆分的情况** (满足任一即拆分)

- **Squash merge且规模较大**: 原本就是多个commits压缩而成，通常包含多个独立功能或模块

- **修改多个独立模块**: 各模块间耦合度低，可独立理解和测试

- **commit message包含多个功能主题**: 例如："add X, implement Y, fix Z"

- **超大规模的commit**: src_loc显著超出milestone内其他commits的规模

---

**🤔 边界情况：依次考虑以下因素**

**(a) 是否包含多个独立功能？** ⭐⭐⭐ (核心问题)
- commit message是否提示多个主题或功能点？
- 不同修改部分是否解决不同问题或实现不同特性？
- 各部分功能是否可以独立理解？

**(b) 模块/文件是否独立？** ⭐⭐
- 修改了几个模块？模块间耦合度如何？
- 是分散修改（多文件少量改动）还是集中修改（少文件大量改动）？
- 是否有大量新增文件可按功能分组？

**(c) 拆分边界是否清晰？** ⭐⭐ (可行性)
- 能否识别出清晰的拆分点（按文件/模块/功能）？
- 拆分后是否便于代码审查和理解？
- 是否存在高度耦合使得拆分困难或无意义？

**(d) 规模参考** ⭐
- src_loc仅作为辅助参考
- 重点关注规模是否与milestone内其他commits差异显著

---

**综合决策**:
- ✅ 如果(a)和(b)都满足，且(c)可行 → 拆分
- ❌ 如果(a)或(b)不满足，或(c)不可行（高度耦合） → 保持完整
- ⚖️ **拿不准时倾向于保持完整**（避免过度拆分）

---

#### 1.3 决策输出

对每个commit记录：
- **should_split**: true/false
- **confidence**: 0-1 (根据信号强度：强烈信号0.9+，边界情况0.6-0.8)
- **reason**: 简短理由（1-2句话）
- **split_strategy_hint**: 如果拆分，建议策略（如"按模块分组"、"按功能主题"、"按文件组"）

**Phase 1最终输出**：

根据决策结果，将commits分为两类：
- **commits_to_keep**: 保持不变的commits（附带决策理由）
- **commits_to_split**: 需要拆分的commits（附带决策信息）
  - 每个commit包含：SHA、reason、confidence、split_strategy_hint、analysis_hints

---

### Phase 2: Commit-Level Split

**目标**: 对需要拆分的commits，调用专门的Commit Split Subagent进行拆分

#### 2.1 调用Commit Split Subagent

**⚠️ 重要说明**:
这一步需要为每个待拆分的commit启动一个独立的Subagent来执行拆分任务。Subagent使用 `GIT_COMMIT_SPLIT_AGENT_PROMPT.md` 作为其任务prompt模板，该模板定义了完整的commit拆分流程。

**执行方式**:

对于每个需要拆分的commit（来自Phase 1的`commits_to_split`列表），执行以下操作：

**a) 准备Subagent的输入参数**

对每个待拆分commit（来自Phase 1的`commits_to_split`列表），组装参数：

**从全局配置获取**:
- `repo_path`: Git仓库路径（输入参数）
- `focus_areas`: 使用`REPO_SRC_DIRS`（如"src/"）
- `size_target`: 参考`COMMIT_SPLIT_SIZE_TARGET`，可根据commit规模微调

**从Phase 1决策结果获取**:
- `target_commit`: commit SHA
- `split_reason`: 拆分理由
- `split_strategy_hint`: 拆分策略提示
- `analysis_hints`: 辅助信息（is_squash_merge, module_count, functional_themes）

**生成运行时参数**:
- `working_branch`: 工作分支名（如"split-{commit_sha[:7]}"）
- `output_path`: 输出目录（如"{output_dir}/milestone_splits/{milestone_id}/commit_splits/{commit_sha[:7]}/"）

**b) 调用Subagent**

启动一个新的agent实例，使用 `GIT_COMMIT_SPLIT_AGENT_PROMPT.md` 作为其prompt模板，并将上述参数填充到模板中的占位符.

**c) 执行要求**

- ⚠️ 必须等待Subagent完成后再继续（顺序执行各个commits的拆分）
- ⚠️ 验证输出文件完整性（METADATA.json、COMMITS.txt、patches/目录）
- ⚠️ 如果Subagent执行失败，记录错误并将该commit标记为"拆分失败，保持原样"

Subagent将在`output_path`生成：METADATA.json、COMMITS.txt、patches/目录。详细格式见Phase 2.2。

#### 2.2 收集Subagent输出

**输出目录结构**:
```
{output_dir}/milestone_splits/M2/commit_splits/1812eac/
├── METADATA.json          # 拆分元数据
├── COMMITS.txt           # 拆分后的commits列表（SHA + message）
└── patches/              # Patch files目录
    ├── abc123d.patch
    ├── def456e.patch
    └── ...
```

**构建Split Commit对象**
   - 每个拆分后的commit记录为一个"虚拟commit"
   - 包含：patch_file路径、src_loc、files、message
   - 标记为来自原始commit的拆分：`base_commit: "1812eac"`

#### 2.3 整合Commits列表

**整合逻辑**:

将保持不变的commits和拆分后的commits整合成统一列表：

**a) 添加未拆分的commits**
   - 类型标记为"original"
   - 保留完整元数据：sha, message, src_loc, files, timestamp等

**b) 添加拆分后的commits**
   - 从每个原始commit的METADATA.json中读取拆分结果
   - 类型标记为"split"
   - 记录：base_commit（原始SHA）、split_sha、patch_file路径、完整元数据
   - 继承原始commit的timestamp
   - 验证：所有splits的src_loc总和应接近原始commit的src_loc

**c) 排序和去重**
   - 按timestamp排序
   - 确保无重复commits

**输出**:
- 完整的commits列表（原始commits + 拆分后的commits）
- 每个commit标记类型（original or split）和完整元数据

---

### Phase 3: Sub-Milestone Generation（Sub-Milestone生成）

**目标**: 将commits按功能相关性分组，生成功能内聚、大小适中的sub-milestones，并输出为CSV文件

**核心任务**:
- 分析commits的语义和功能关系
- 按功能主题将commits分组
- 为每个分组生成正式的sub-milestone定义（ID、Title、描述、统计信息）
- 输出结构化的sub_milestones.csv文件

#### 3.1 分组策略

**核心目标**: 将commits聚合成功能内聚、可独立理解的sub-milestones

**分组时需要考虑的因素**:

- **a) 功能相关性** ⭐ 最重要 - 识别功能主题，将实现相关功能的commits分组
- **b) 时间/演进顺序** - 考虑开发顺序和功能演进阶段
- **c) 文件/模块结构** - 高度模块化代码可按模块分组
- **d) 大小平衡** - 参考目标大小，但优先保证功能内聚（允许合理例外）
- **e) 依赖关系** - 识别并行机会，避免过度串行化

**核心原则**:
- ✅ 优先保证功能内聚和可理解性
- ✅ 基于语义理解，而非机械规则
- ✅ Split commits可分散到不同sub-milestones（基于功能相关性）

---

#### 3.2 执行分组

**分组步骤**:

**a) 理解commits的语义**
   - 阅读所有commit messages，理解每个commit的目的和功能
   - 分析修改的文件和代码，理解技术实现
   - 识别功能主题（如："基础架构"、"连接管理"、"测试基础设施"）

**b) 按语义相关性分组**
   - 将功能相关的commits分为一组
   - 每组应有清晰的功能主题和目的
   - 来自同一原始commit的splits可以分散到不同组（基于功能相关性）

**c) 调整分组**
   - 合并过小的组（功能相关且规模较小）
   - 拆分过大的组（按子功能进一步划分）
   - 参考目标大小，但优先保证功能内聚性

---

#### 3.3 生成Sub-Milestone定义

为每个分组生成正式的sub-milestone定义。

**a) 生成Sub-Milestone ID**

格式：`{parent_id}.{序号}`

**编号规则**:
- 按时间顺序或依赖顺序编号
- 示例：M2.1, M2.2, M2.3, ...

**b) 生成Title和描述**

**Title** (6-15字符):
- 基于分组主题和主要功能
- 示例：
  - "Emscripten基础架构"
  - "流式请求实现"
  - "错误处理增强"

**mini_srs** (100-200字符):
- 模板：实现[功能]：[方法]；涉及模块：[文件]；作用：[重要性]
- 示例：
  - "建立Emscripten模块的初始架构和基础类：connection.py(186行), fetch.py(376行); 这是整个Emscripten支持的基石; Main modules: src/urllib3/contrib/emscripten/"

**rationale** (50-100字符):
- 解释为何这些commits属于同一组
- 示例：
  - "这些commits共同建立了HTTP/2的基础协议支持，是后续功能的前提"

**c) 计算统计信息**

为每个sub-milestone计算：
- **total_src_loc**: 所有commits的src_loc总和
- **loc**: 所有commits的总代码变更行数（additions + deletions）
- **touched_files**: 所有commits涉及的文件集合（去重）
- **start_time**: 最早的commit时间
- **end_time**: 最晚的commit时间
- **category**: 继承父milestone的类别

---

#### 3.4 生成Sub-Milestones CSV

**输出文件**: `{output_dir}/milestone_splits/{milestone_id}/sub_milestones.csv`

**CSV格式**:
```csv
id,parent_id,title,category,commits,total_src_loc,loc,touched_files,start_time,end_time,mini_srs,rationale
M2.1,M2,Emscripten基础架构,FEATURE,"7b3f948;abc123d;def456e",401,550,"pyproject.toml;src/urllib3/contrib/emscripten/connection.py;...",2023-11-10T19:32:03+00:00,2023-11-10T19:32:03+00:00,"建立Emscripten模块的初始架构和基础类：connection.py(186行), ...","这些commits共同建立了Emscripten支持的基础架构"
```

**CSV字段**:
- **id**: Sub-milestone标识符（如"M2.1"）
- **parent_id**: 父milestone的id
- **title**: Sub-milestone标题
- **category**: 继承父milestone的类别
- **commits**: Commits SHA列表（7位短SHA，包含original和split，分号分隔）
- **total_src_loc**: 所有commits的src_loc总和
- **loc**: 所有commits的总代码变更行数（additions + deletions）
- **touched_files**: 所有commits涉及的文件集合（分号分隔）
- **start_time**: 最早的commit时间
- **end_time**: 最晚的commit时间
- **mini_srs**: Sub-milestone功能描述（100-200字符）
- **rationale**: 分组理由（50-100字符）

**Phase 3输出**:
- ✅ `sub_milestones.csv` - Sub-milestone定义文件
- ✅ 内存中的完整sub-milestone对象列表（包含详细commit元数据，用于后续Phase）

---

### Phase 4: Dependency Inference（依赖推断）

**目标**: 确定同一milestone内sub-milestones之间的依赖关系

**⚠️ 重要范围说明**:
- **仅推断同一milestone内的sub-milestones之间的依赖**
  - 例如：M2.1, M2.2, M2.3之间的依赖关系
- **不推断跨milestone的依赖**
  - 例如：不推断M2.1依赖M3.1这样的关系
  - 跨milestone依赖已在原始milestone依赖关系中定义

---

#### 4.1 依赖推断方法

依次考虑以下三种依赖类型，综合判断sub-milestones之间的依赖关系：

---

**类型1: 代码冲突依赖** ⚠️ 强依赖（必须遵守执行顺序）

**识别条件**: 两个sub-milestones修改了同一文件的**同一代码区域**（overlapping hunks）

**判断方法**:
- 分析每个sub-milestone包含的commits修改的文件和代码行范围
- 检查是否存在重叠的代码区域（同一文件的相同或相邻代码行）
- 如果M2.2修改了M2.1修改过的相同代码区域 → M2.2依赖M2.1（必须按时间顺序）

**示例**:
```
M2.1: 修改 connection.py 第100-150行（添加基础连接逻辑）
M2.2: 修改 connection.py 第120-140行（增强连接逻辑）
→ M2.2依赖M2.1（代码区域重叠，必须按顺序应用）
```

**依赖标签**: `TEXTUAL_CONFLICT`

---

**类型2: 功能定义-使用依赖** ⭐ 核心依赖（def-before-use）

**识别条件**: M2.1定义/实现了某个功能/接口/类，M2.2使用了这个功能

**判断方法**:
- 分析M2.1的commits: 识别新增的类、函数、接口、模块
- 分析M2.2的commits: 识别引用/调用/import的外部功能
- 如果M2.2使用了M2.1定义的功能 → M2.2依赖M2.1

**如何识别"使用"关系**:
- **方法a) 文件级分析**: M2.2的代码中import/引用了M2.1新增的模块
- **方法b) 语义理解**:
  - M2.1: "实现HTTPConnection基类"
  - M2.2: "实现基于HTTPConnection的请求处理"
  - → 明显M2.2使用了M2.1定义的HTTPConnection
- **方法c) 时间+文件组合**:
  - M2.1新增文件A，M2.2修改文件B且B可能使用A的功能

**示例**:
```
M2.1: 新增 src/connection.py (定义HTTPConnection类)
M2.2: 新增 src/request.py (使用HTTPConnection发送请求)
→ M2.2依赖M2.1（def-before-use）

M2.1: 实现基础架构和配置
M2.3: 实现错误处理（使用M2.1的配置系统）
→ M2.3依赖M2.1（使用了配置功能）
```

**依赖标签**: `FUNCTIONAL_DEPENDENCY`

---

**类型3: 功能演进的语义依赖** (语义层面的合理顺序)

**识别条件**: 基于功能的自然演进顺序和开发逻辑

**判断方法**:
理解每个sub-milestone的功能主题，识别自然的演进顺序：

**常见演进模式**:
- **基础设施 → 核心功能 → 增强功能 → 优化**
  - 示例: "基础架构" → "连接管理" → "重试机制" → "性能优化"

- **接口定义 → 实现 → 测试**
  - 示例: "API接口设计" → "HTTP实现" → "集成测试"

- **核心功能 → 错误处理**
  - 如果错误处理需要了解核心功能的错误类型和处理方式

- **同步实现 → 异步实现**
  - 异步版本通常基于同步版本改进

**注意**:
- ⚠️ 这种依赖不是强制的，需要根据实际情况判断
- 不要生搬硬套模式（如"错误处理一定依赖核心功能"）
- 重点关注: 如果B的实现需要理解或基于A的设计/架构 → B可能依赖A

**示例**:
```
M2.1: Emscripten基础架构（定义整体架构）
M2.2: 流式请求实现（基于M2.1的架构设计）
→ M2.2可能依赖M2.1（需要理解架构设计）

M2.3: 同步连接实现
M2.4: 异步连接实现（基于M2.3改进）
→ M2.4可能依赖M2.3（基于同步版本）
```

**依赖标签**: `SEMANTIC_DEPENDENCY`

---

**综合判断原则**:

1. **类型1优先**: 如果存在代码冲突依赖，必须标记（强制顺序）
2. **类型2次之**: def-before-use是核心依赖，需要仔细识别
3. **类型3谨慎**: 语义依赖不要过度推断，确实需要才标记
4. **避免过度串行化**: 如果不确定是否有依赖，倾向于不标记（保留并行机会）

---

#### 4.2 依赖类型和标签

**`TEXTUAL_CONFLICT`（代码冲突依赖）**:
- 修改同一文件的同一代码区域（overlapping hunks）
- 严重性：⚠️ 最高 - 强依赖，必须按时间顺序执行

**`FUNCTIONAL_DEPENDENCY`（功能定义-使用依赖）**:
- def-before-use关系，基于功能逻辑和调用关系推断
- 严重性：⭐ 高 - 核心依赖，强烈建议遵守

**`SEMANTIC_DEPENDENCY`（语义依赖）**:
- 基于功能演进模式的语义层面依赖
- 严重性：💡 中 - 合理顺序建议，可根据实际情况调整

#### 4.3 处理Split Commits的依赖

**特殊情况**:
- 来自同一原始commit的split commits之间通常没有依赖关系
- 但如果它们被分配到不同sub-milestones，需要检查：
  - 是否修改相同文件的不同部分？
  - 是否存在函数调用关系？

**示例**:
```
原始commit 1812eac 被拆分为12个commits:
- abc123d (基础架构) → M2.1
- def456e (connection实现) → M2.1
- 5098905 (fetch实现) → M2.2
- ...

依赖关系：
M2.2依赖M2.1 （因为fetch使用了connection）
但abc123d和def456e之间没有依赖（都在M2.1内）
```

#### 4.4 生成Dependencies CSV

**输出文件**: `{output_dir}/milestone_splits/{milestone_id}/sub_dependencies.csv`

**重要**: 该CSV仅包含同一milestone内的sub-milestones之间的依赖关系（如M2.1→M2.2）

**CSV格式**:
```csv
source_id,target_id,dependency_tags,dependency_notes
M2.1,M2.2,FUNCTIONAL_DEPENDENCY,"M2.2的流式请求实现依赖M2.1提供的connection和fetch基础接口"
M2.1,M2.3,FUNCTIONAL_DEPENDENCY,"M2.3的错误处理依赖M2.1的基础架构"
M2.2,M2.4,FUNCTIONAL_DEPENDENCY,"M2.4的集成测试依赖M2.2的流式请求实现"
```

**字段说明**(canonical convention — `source` 是前置, `target` 是依赖者):
- `source_id`: **prerequisite**(前置 / 必须先完成的 sub-milestone)
- `target_id`: **dependant**(依赖者 / 在 source 完成后才能开始)
- `dependency_tags`: 依赖类型标签（可以是多个，分号分隔）
- `dependency_notes`: 依赖关系说明,描述 target **依赖** source 的具体原因

**Phase 4输出**:
- ✅ `sub_dependencies.csv` - Sub-milestone依赖关系文件
- ✅ 内存中的依赖图数据结构（用于Phase 5验证DAG有效性）

---

### Phase 5: Validation（验证）

**目标**: 验证拆分质量

#### 5.1 完整性检查

**Src_loc守恒**:
- 计算原始milestone的src_loc
- 计算所有sub-milestones的src_loc总和
- 计算守恒率 = 拆分后总和 / 原始值
- 检查守恒率是否在合理范围内（通常0.95-1.05）

**Commits覆盖**:
- 所有原始commits都被包含（作为original或base_commit）
- 无重复commits

**Split完整性**:
- 所有被拆分的commits，其splits的总src_loc应接近原始commit的src_loc
- 验证patch files存在且可读

#### 5.2 平衡性检查

检查sub-milestones的大小分布是否合理：

- 收集所有sub-milestones的src_loc
- 计算均值、标准差、CV（变异系数 = 标准差/均值）
- 检查CV是否合理（通常 < 1.0表示分布较均衡）
- 检查是否存在极端大小的sub-milestone
- 参考范围但允许合理例外（如核心架构可以较大）

#### 5.3 DAG有效性检查

验证依赖图是否为有效的DAG（无环图）：

- 构建依赖关系图
- 使用拓扑排序验证是否存在环
- 如果存在环，标记为验证失败

#### 5.4 如果验证失败

**第1次失败**: 自动调整
- 合并过小的sub-milestones
- 重新分配导致循环依赖的commits

**第2次失败**: 尝试不同策略
- 切换分组方法（功能语义 → 文件模块 → 时间顺序）
- 调整分组粒度（更细或更粗）

**第3次失败**: 输出警告
- 生成验证报告，标记问题
- 继续生成输出，但警告用户需要人工review

#### 5.5 成功标准

拆分成功需满足以下标准：

- ✅ **Commit拆分完整**: 所有需要拆分的commits成功拆分，patch files生成正确
- ✅ **Src_loc守恒**: 拆分后总src_loc与原始milestone基本一致（通常在95%-105%范围）
- ✅ **大小合理**: Sub-milestones大小适中，功能内聚，可独立理解
- ✅ **平衡性**: Sub-milestones大小分布相对均衡（CV通常 < 1.0）
- ✅ **DAG有效**: 依赖图无环
- ✅ **功能清晰**: 每个sub-milestone有明确的功能描述
- ✅ **Split完整性**: 所有split commits有对应的patch files和元数据

**输出**: 验证报告（通过/失败 + 详细信息）

---

### Phase 6: Report Generation（报告生成）

**目标**: 生成拆分报告和验证报告

#### 6.1 拆分报告 (`{milestone_id}_split_report.json`)

**包含内容**:
- **original_milestone**: 原始milestone基本信息（id, title, src_loc, commits数量等）
- **commit_split_summary**: commit级拆分汇总
  - 分析总数、保持/拆分数量
  - 每个被拆分commit的拆分详情（原始SHA、拆分数量、src_loc匹配情况、输出目录）
- **split_summary**: 拆分总结统计
  - sub-milestone数量
  - 原始/拆分后src_loc及守恒率
  - 大小分布统计（max, min, mean, median, std_dev, CV）
  - 依赖关系数量
- **warnings**: 警告信息列表
- **notes**: 拆分说明和备注

#### 6.2 验证报告 (`{milestone_id}_verification.json`)

**包含内容**:
- **completeness_checks**: 完整性检查（src_loc守恒、commits覆盖、split完整性）
- **balance_checks**: 平衡性检查（大小分布、CV值）
- **dependency_checks**: 依赖检查（DAG有效性、环检测）
- **split_integrity_checks**: Split commits完整性（patch files存在、元数据完整、loc匹配）
- **overall_status**: 总体状态（PASSED/FAILED/WARNING）
- **warnings**: 警告列表
- **errors**: 错误列表

**输出**:
- ✅ `{milestone_id}_split_report.json` - 拆分报告
- ✅ `{milestone_id}_verification.json` - 验证报告

---

## 输出清单

最终应生成以下文件：

```
{output_dir}/milestone_splits/{milestone_id}/
├── commit_splits/                              # Phase 2: Commit级拆分结果
│   ├── {commit_sha_1}/
│   │   ├── METADATA.json
│   │   ├── COMMITS.txt
│   │   └── patches/*.patch
│   └── {commit_sha_2}/
│       └── ...
├── sub_milestones.csv                          # Phase 3: Sub-milestone定义
├── sub_dependencies.csv                        # Phase 4: Sub-milestone依赖关系
├── {milestone_id}_split_report.json           # Phase 6: 最终拆分报告
└── {milestone_id}_verification.json           # Phase 5+6: 验证报告
```

**文件说明**:
- **commit_splits/**: 每个拆分commit的patch files和元数据
- **sub_milestones.csv**: Sub-milestone基本信息（id, title, commits, src_loc等）
- **sub_dependencies.csv**: 同一milestone内sub-milestones之间的依赖关系（如M2.1→M2.2）
- **split_report.json**: 完整的拆分报告（包含详细commit元数据）
- **verification.json**: 拆分质量验证报告
