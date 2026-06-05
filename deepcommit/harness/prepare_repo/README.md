# Milestone Tag Creator

## 概述

为每个 milestone 创建起始（start）和结束（end）状态的 Git 标签（tag），用于 milestone 的独立开发和测试。

**核心技术**：
- 使用 `cherry-pick` + ORT策略应用commits（取代旧的git apply方式）
- 配置化重命名检测（`--rename-threshold`，默认20%）
- 智能testbed复用（自动检测并复用现有tags）
- 自动生成metadata.json和milestone patches

**输出文件**：
- Git tags: `milestone-{id}-start` 和 `milestone-{id}-end`
- `metadata.json`: 完整的元数据和拓扑排序信息
- `milestone_patches/`: 每个milestone的patch文件
- CSV副本: milestones.csv 和 dependencies.csv

### 快速开始

```bash
# 1. 准备输入文件
#    - milestones.csv: milestone 定义
#    - dependencies.csv: milestone 依赖关系

# 2. 运行创建工具
python -m harness.prepare_repo.build_milestone_testbed \
    --milestone-csv DATA/result/test_multi_stage/milestones.csv \
    --dependencies-csv DATA/result/test_multi_stage/dependencies.csv \
    --output-dir DATA/result/test_multi_stage \
    # 可选：覆盖 src 目录（默认从项目根 conf/repo_src_dirs.json 按 repo 名读取）
    # --repo-src-dirs src,packages/markitdown \
    --verify

# 3. 查看创建的 tags
cd DATA/result/test_multi_stage/testbed
git tag | grep milestone-

# 4. 查看生成的输出文件
cat DATA/result/test_multi_stage/metadata.json           # 完整的元数据
ls DATA/result/test_multi_stage/milestone_patches/       # 每个milestone的patch文件
```

## 核心概念

### Start状态 vs End状态

**Start状态** (`milestone-{id}-start`)：
- **src/目录**：包含所有前置依赖（传递依赖）的src patches累积
- **非src/目录**：该milestone最后一个commit的snapshot
- **用途**：milestone开发的初始状态

**End状态** (`milestone-{id}-end`)：
- **src/目录**：start的src/ + 该milestone自己的src patches
- **非src/目录**：该milestone最后一个commit的snapshot（同start）
- **用途**：milestone完成的状态，也是子milestone的base点

### 依赖处理

- **单父节点**：直接从父的end状态checkout
- **多父节点**：merge所有父的end状态
  - src/冲突：立即报错退出（需要人工介入）
  - 非src/冲突：自动解决（会被snapshot覆盖）

## 算法流程

### 1. 准备阶段
1. Clone源仓库到工作目录
2. 验证起始tag（start tag）存在
3. 解析milestones.csv和dependencies.csv

### 2. 计算拓扑排序
- 对milestone进行拓扑排序（按依赖关系）
- 使用milestone ID作为tie-breaker保证稳定性

### 3. 创建标签（按拓扑序）

对每个milestone M：

#### 3.1 创建Start状态
```
创建临时工作分支: temp-{M}-work

IF 无父节点:
    git checkout {BASE_TAG}
    git checkout -b temp-{M}-work

ELIF 单父节点 P:
    git checkout milestone-{P}-end  # checkout tag
    git checkout -b temp-{M}-work

ELSE 多父节点 [P1, P2, ...]:
    git checkout milestone-{P1}-end  # checkout tag
    git checkout -b temp-{M}-work
    FOR P in [P2, P3, ...]:
        git merge milestone-{P}-end --no-commit  # merge tag
        IF 有src/冲突:
            git merge --abort
            报错退出
        ENDIF
    ENDFOR

# 替换非src/文件
git checkout {M.last_commit} -- . ':!src/'
git add .
git commit -m "Start state for {M}"

# 创建start tag
git tag milestone-{M}-start
记录 start_commit_sha
```

#### 3.2 创建End状态
```
# 继续在temp-{M}-work分支上工作

# 按 author date 排序 commits（时间顺序应用）
sorted_commits = sort_by_author_date(M.commits)

# 使用 cherry-pick 应用该milestone的commits（按时间顺序）
FOR commit in sorted_commits:
    # 使用 cherry-pick with rename detection
    IF commit is merge commit:
        git cherry-pick {commit} --no-commit --strategy=ort \
            -X rename-threshold={THRESHOLD} -X patience -m 1
    ELSE:
        git cherry-pick {commit} --no-commit --strategy=ort \
            -X rename-threshold={THRESHOLD} -X patience
    ENDIF

    IF cherry-pick失败:
        检查冲突
        IF 有src/冲突:
            报错退出（显示冲突文件）
        ENDIF
        IF 有非src/冲突:
            记录警告（将被snapshot覆盖）
        ENDIF
    ENDIF

    # 每次cherry-pick后恢复非src/文件
    git rm -rf . ':(exclude)src/'
    git checkout {M.last_commit} -- . ':(exclude)src/'

    # 清理cherry-pick状态
    git cherry-pick --quit
ENDFOR

git add .
git commit -m "End state for {M}"

# 创建end tag
git tag milestone-{M}-end
记录 end_commit_sha

# 删除临时分支
git checkout --detach
git branch -D temp-{M}-work
```

## 输入文件

### milestones.csv
```csv
id,title,category,commits,mini_srs,confidence_score,start_time,end_time,loc,src_loc,touched_test_files,touched_src_files
M001,HTTP/2 Protocol Support,major_feature,23f2287;f9d37ad;0628b6b;5239265;be24439,...
```

关键字段：
- `id`: Milestone ID
- `commits`: 分号分隔的commit SHA列表

### dependencies.csv
```csv
prerequisite_id,dependant_id,dependency_primary_type,dependency_secondary_types,dependency_strength,dependency_notes,confidence_score
M002,M015,FUNC,,weak,Inferred from commit DAG,0.85
```

关键字段:
- `prerequisite_id`: 前置条件(必须先完成)
- `dependant_id`: 依赖者(在 prerequisite 完成后才能开始)

> Legacy CSVs 可能仍用 `source_id` / `target_id` 列名。Reader 兼容两套(同方向,只是 header 不同)。新写入用 canonical 名。

**依赖语义**：
- `M002,M015` 表示：先做 M002，再做 M015（M002 → M015）
- M002 是 M015 的父节点（parent）
- M015 依赖 M002

## 输出文件

### metadata.json
包含所有milestone的完整信息:
- 仓库信息(repo_name, repo_path, base_tag)
- 拓扑排序结果(topological_order, independent_milestones)
- 每个milestone的详细信息:
  - 基本信息(id, title, category, commits)
  - Tag信息(tag_name_start, tag_name_end)
  - Commit SHA(commit_sha_start, commit_sha_end)
  - 依赖关系(parent_milestones)
- 生成时间和工具版本

### milestone_patches/
包含每个milestone的patch文件:
- `M001.patch`: M001的start到end的完整diff
- `M002.patch`: M002的start到end的完整diff
- 可用于验证或手动应用changes

### milestones.csv 和 dependencies.csv
输入文件的副本,保存在output目录中

## 命令行参数

### 必需参数
- `--milestone-csv PATH`: Milestone CSV 文件路径
- `--dependencies-csv PATH`: 依赖关系 CSV 文件路径

### 可选参数
- `--repo-path PATH`: 源仓库路径（默认：`DATA/github_data/repos/urllib3_urllib3`）
- `--output-dir PATH`: 输出目录路径（默认：`DATA/harness_workspace/urllib3-docker/test_multi_stage`）
- `--work-dir NAME`: 工作目录名称，output-dir的子目录（默认：`testbed`）
- `--base-tag TAG`: 基准版本 tag（默认：`2.0.6`）
- `--rename-threshold N`: Git重命名检测阈值0-100（默认：`20`，越低检测越激进）
- `--force-rebuild`: 强制重新创建testbed（即使已存在）
- `--verbose`: 显示详细输出，包括所有 git 命令
- `--verify`: 创建完成后自动验证所有 tags

## 使用示例

```bash
# 基本使用
python -m harness.prepare_repo.build_milestone_testbed \
    --milestone-csv DATA/result/test_multi_stage/milestones.csv \
    --dependencies-csv DATA/result/test_multi_stage/dependencies.csv \
    --output-dir DATA/result/test_multi_stage

# 创建并自动验证
python -m harness.prepare_repo.build_milestone_testbed \
    --milestone-csv DATA/result/test_multi_stage/milestones.csv \
    --dependencies-csv DATA/result/test_multi_stage/dependencies.csv \
    --output-dir DATA/result/test_multi_stage \
    --verify

# 详细模式（显示所有 git 命令）
python -m harness.prepare_repo.build_milestone_testbed \
    --milestone-csv DATA/result/test_multi_stage/milestones.csv \
    --dependencies-csv DATA/result/test_multi_stage/dependencies.csv \
    --output-dir DATA/result/test_multi_stage \
    --verbose

# 自定义所有路径
python -m harness.prepare_repo.build_milestone_testbed \
    --repo-path /path/to/source/repo \
    --work-dir testbed \
    --milestone-csv /path/to/milestones.csv \
    --dependencies-csv /path/to/dependencies.csv \
    --output-dir /path/to/output \
    --base-tag 2.0.6 \
    --verify

# 调整重命名检测阈值（处理大量文件重命名的场景）
python -m harness.prepare_repo.build_milestone_testbed \
    --milestone-csv DATA/result/test_multi_stage/milestones.csv \
    --dependencies-csv DATA/result/test_multi_stage/dependencies.csv \
    --output-dir DATA/result/test_multi_stage \
    --rename-threshold 10 \
    --verbose

# 强制重建（即使testbed已存在）
python -m harness.prepare_repo.build_milestone_testbed \
    --milestone-csv DATA/result/test_multi_stage/milestones.csv \
    --dependencies-csv DATA/result/test_multi_stage/dependencies.csv \
    --output-dir DATA/result/test_multi_stage \
    --force-rebuild \
    --verify
```

## 错误处理

| 错误类型 | 处理方式 |
|---------|---------|
| 起始tag不存在 | 报错退出 |
| Commit SHA不存在 | 报错退出 |
| src/目录冲突（merge） | 报错退出（需要人工分析） |
| src/目录冲突（cherry-pick） | 报错退出（显示冲突文件） |
| 非src/冲突 | 警告（会被snapshot覆盖） |
| cherry-pick失败 | 报错退出 |
| 循环依赖 | 报错退出 |

所有错误都会输出详细信息，包括：
- 相关milestone ID
- 冲突文件列表
- 错误发生的步骤

## 验证方法

### 自动验证（推荐）
使用 `--verify` 参数可以在创建完成后自动验证所有 tags：
```bash
python -m harness.prepare_repo.build_milestone_testbed \
    --milestone-csv DATA/result/test_multi_stage/milestones.csv \
    --dependencies-csv DATA/result/test_multi_stage/dependencies.csv \
    --output-dir DATA/result/test_multi_stage \
    --verify
```

自动验证会检查每个 milestone：

1. **Tags 存在性**
   - ✅ `milestone-{id}-start` tag 存在
   - ✅ `milestone-{id}-end` tag 存在

2. **Start 和 End 状态差异**
   - ✅ Start 和 End 的 commit SHA 不同（除非 milestone 没有 src/ 改动）

3. **非 src/ 文件一致性**
   - ✅ Start 状态的非 src/ 文件与 `last_commit` 一致
   - ✅ End 状态的非 src/ 文件与 `last_commit` 一致

4. **src/ 变更检测**
   - ✅ End 相对 Start 有 src/ 目录的差异（如果 milestone 有 src/ 改动）

验证失败时会显示：
- ❌ 失败的检查项
- 相关的 milestone ID 和 title
- 最后以非 0 退出码退出（便于 CI/CD 集成）

### 手动验证
在工作目录中进行手动验证：

```bash
# 进入工作目录
cd DATA/harness_workspace/urllib3-docker/test_multi_stage/testbed

# 1. 检查所有标签已创建
git tag | grep "milestone-"

# 2. 检查某个 milestone 的 start 状态
git checkout milestone-M007-start
git log -1
git diff 2.0.6 -- src/  # 应该有前置依赖的改动

# 3. 检查 end 状态
git checkout milestone-M007-end
git diff milestone-M007-start -- src/  # 应该是该 milestone 自己的改动

# 4. 检查非 src/ 文件
git checkout milestone-M007-start
git diff {M007.last_commit} -- ':!src/'  # 应该无差异

git checkout milestone-M007-end
git diff {M007.last_commit} -- ':!src/'  # 应该无差异

# 5. 验证临时分支已清理
git branch | grep "temp-"  # 应该为空
```

## 技术细节

### Commit 时间排序
在应用 milestone 的 commits 时，使用 **author date** 而非 commit SHA 顺序：
- 获取每个 commit 的 `%at`（author timestamp）
- 按时间从早到晚排序
- 确保 patches 按实际开发时间顺序应用
- 避免因 commit 顺序导致的依赖问题

```python
# 实现示例
for commit in commits:
    timestamp = git show -s --format=%at {commit}
    commit_times.append((timestamp, commit))

sorted_commits = [c for _, c in sorted(commit_times)]
```

### Cherry-pick 策略
使用 `git cherry-pick` with ORT策略和rename detection应用commits：
- **ORT策略**：使用 `--strategy=ort` 提供更好的合并性能和准确性
- **重命名检测**：使用 `-X rename-threshold=N` 检测文件重命名（默认20%）
- **Patience算法**：使用 `-X patience` 提供更准确的diff算法
- **Merge commit处理**：使用 `-m 1` 选择第一个父节点
- **每次恢复snapshot**：cherry-pick后立即恢复非src/文件，确保正确性

```bash
# 示例 - 普通commit
git cherry-pick abc1234 --no-commit --strategy=ort \
    -X rename-threshold=20 -X patience

# 示例 - merge commit
git cherry-pick abc1234 --no-commit --strategy=ort \
    -X rename-threshold=20 -X patience -m 1
```

### Git Pathspec 语法
- `src/`: 包含 src/ 目录
- `:!src/`: 排除 src/ 目录
- `. ':!src/'`: 所有文件除了 src/

### 拓扑排序稳定性
使用 milestone ID 作为 tie-breaker，确保：
- 同一层级的 milestone 按 ID 字典序排列
- 每次运行结果一致

### Merge 策略
- 使用 `--no-ff` 确保保留 merge 历史
- 使用 `--no-commit` 以便在 commit 前处理冲突
- 检测 src/ 冲突后立即 `git merge --abort`

### Tag vs Branch 设计选择
使用 tag 而非 branch 的优势：
- **语义准确**：start/end 是里程碑标记，不是开发分支
- **清晰简洁**：`git tag` 列表不会混入临时分支
- **不可变性**：tag 语义上表示固定的历史点
- **操作支持**：tag 可以直接用于 checkout、merge、diff 等所有操作
- **临时分支清理**：工作完成后删除临时分支，保持仓库整洁

### 智能Testbed复用
工具具有智能检测和复用现有testbed的能力：
- **自动检测**：检查所有milestone tags是否存在
- **完整复用**：如果所有tags存在，跳过tag创建，仅生成metadata和patches
- **部分重建检测**：如果部分tags缺失，提示使用 `--force-rebuild`
- **强制重建**：使用 `--force-rebuild` 参数可强制删除并重建整个testbed
- **性能优化**：复用现有tags可以节省大量时间，特别是大型项目

### 输出文件生成
工具生成三类输出文件：
1. **metadata.json**：包含完整的元数据信息
   - 仓库信息、拓扑排序、milestone详细信息
   - 可用于后续工具链的输入
2. **milestone_patches/**：每个milestone的patch文件
   - 包含start到end的完整diff
   - 可用于验证或手动应用changes
3. **CSV文件副本**：milestones.csv和dependencies.csv的备份
   - 保存在output目录，方便追踪输入数据

## 常见问题

### 工作目录已存在怎么办？

**场景1：所有milestone tags都存在**
工具会自动检测并复用现有tags：
```
✅ Work directory already exists: /path/to/testbed
   Using existing testbed (use --force-rebuild to recreate)
✅ All milestone tags already exist. Skipping tag creation.
   Use --force-rebuild to recreate tags from scratch.
```
这种情况下会：
- 跳过tag创建，直接使用现有tags
- 仍然生成metadata.json和milestone_patches/
- 可以使用 `--force-rebuild` 强制重建

**场景2：部分tags缺失**
会报错并要求重建：
```
⚠️  Some tags are missing: 5 tags
   - milestone-M001-start
   ...
❌ Cannot use existing testbed with incomplete tags.
   Use --force-rebuild to recreate all tags.
```
解决：使用 `--force-rebuild` 完全重建testbed

**场景3：无效的git仓库**
会提示删除重建：
```
⚠️  Work directory exists but is not a valid git repository
Delete and recreate? (yes/no):
```
- 输入 `yes` 删除重建（推荐）
- 输入 `no` 退出（保留现有数据）

### cherry-pick 失败怎么办？
当看到以下错误时：
```
❌ ERROR: Conflicts detected in src/ directory:
     - src/urllib3/connection.py
   This indicates incompatible changes in abc1234
```

可能原因：
1. **文件重命名问题**：某个 commit 修改了重命名后的文件，但重命名操作在另一个 milestone
   - 解决：检查 dependencies.csv，确保重命名操作所在的 milestone 是依赖项
   - 可尝试调整 `--rename-threshold` 参数（默认20，降低可检测更多重命名）

2. **代码冲突**：多个 milestone 修改了同一行代码
   - 解决：审查 milestone 划分，考虑合并冲突的 milestones

3. **应用顺序问题**：commits没有按正确顺序应用
   - 工具已自动按author date排序，检查commits的时间戳

### src/ 目录 merge 冲突怎么办？
这是严重错误，表示 milestone 依赖关系有问题：
```
❌ ERROR: Conflicts detected in src/ directory:
     - src/urllib3/connection.py
```

解决步骤：
1. 检查哪些 milestones 修改了冲突文件
2. 确定它们之间的依赖关系
3. 更新 dependencies.csv 添加缺失的依赖
4. 重新运行工具

### 验证失败怎么办？
使用 `--verify` 时如果看到 ❌：
1. 查看具体失败的检查项
2. 手动检查对应的 tag 状态
3. 可能需要调整 milestone 定义或依赖关系
4. 删除工作目录重新运行

### 如何清理之前的运行结果？
```bash
# 方法1：使用 --force-rebuild 参数（推荐）
python -m harness.prepare_repo.build_milestone_testbed \
    --milestone-csv DATA/result/test_multi_stage/milestones.csv \
    --dependencies-csv DATA/result/test_multi_stage/dependencies.csv \
    --output-dir DATA/result/test_multi_stage \
    --force-rebuild \
    --verify

# 方法2：手动删除工作目录
rm -rf DATA/result/test_multi_stage/testbed

# 然后重新运行
python -m harness.prepare_repo.build_milestone_testbed \
    --milestone-csv DATA/result/test_multi_stage/milestones.csv \
    --dependencies-csv DATA/result/test_multi_stage/dependencies.csv \
    --output-dir DATA/result/test_multi_stage \
    --verify
```
