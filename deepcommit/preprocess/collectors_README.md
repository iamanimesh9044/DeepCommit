# GitHub Data Collection Scripts

这个目录包含了用于收集GitHub仓库数据的脚本和模块。

## 目录结构

```
collectors/
├── gh_crawler.py           # 主要的GitHub数据收集器（入口点）
├── revert_detector.py      # 回滚提交检测工具
├── core/                   # 核心模块（模块化架构）
│   ├── data_collector.py   # 数据收集器
│   ├── data_exporter.py    # 数据导出器
│   ├── data_models.py      # 数据模型定义
│   └── filter_pipeline.py  # 过滤管道
├── commit_dag/             # Commit DAG 相关工具
└── README.md               # 本文档
```

## 主要工具

### 1. gh_crawler.py - GitHub 数据收集器

这是主要的数据收集入口点，用于从GitHub API收集releases、PRs、issues等信息。

#### 基本用法

```bash
# 收集指定tag范围的数据
python -m deepcommit.collect.collectors.gh_crawler \
    --owner urllib3 \
    --repo urllib3 \
    --start-tag 2.0.6 \
    --end-tag 2.3.0 \
    --local-repo DATA/github_data/repos/urllib3_urllib3 \
    --output-dir DATA/github_data/preprocessed/urllib3_urllib3_2.0.6_2.3.0

# 收集最近N个releases
python -m deepcommit.collect.collectors.gh_crawler \
    --owner pytorch \
    --repo pytorch \
    --limit 10 \
    --output-dir DATA/github_data/preprocessed/pytorch_latest_10

# 只收集修改了特定目录的commits
python -m deepcommit.collect.collectors.gh_crawler \
    --owner urllib3 \
    --repo urllib3 \
    --start-tag 2.0.6 \
    --end-tag 2.3.0 \
    --local-repo DATA/github_data/repos/urllib3_urllib3 \
    --output-dir DATA/github_data/preprocessed/urllib3_urllib3_2.0.6_2.3.0 \
    --repo-src-dirs src/urllib3/
```

#### 主要参数

- `--owner`: GitHub仓库所有者（必需）
- `--repo`: 仓库名称（必需）
- `--start-tag`: 起始tag（可选，与--end-tag配合使用）
- `--end-tag`: 结束tag（可选，与--start-tag配合使用）
- `--limit`: 限制收集的release数量（可选，与tag范围互斥）
- `--local-repo`: 本地Git仓库路径（推荐，用于精确的主线分析）
- `--output-dir`: 输出目录（默认：data）
- `--token`: GitHub token（默认从环境变量GITHUB_TOKEN读取）
- `--workers`: 并发worker数量（默认：10）
- `--main-branch`: 主分支名称（默认：main）
- `--repo-src-dirs`: 源码目录过滤（默认：src/，只保留修改了这些目录的commits）
- `--no-index`: 不构建索引
- `--overwrite`: 强制重新收集数据

#### 输出文件

收集器会在输出目录生成以下文件：

```
output_dir/
├── releases.jsonl          # 每行一个release的信息
├── pull_requests.jsonl     # 每行一个PR的信息
├── issues.jsonl            # 每行一个issue的信息
├── metadata.json           # 收集任务的元数据
└── .index_cache.pkl        # 索引缓存文件（可选）
```

### 2. revert_detector.py - 回滚检测工具

基于代码变更模式自动识别回滚提交，不依赖commit message。

#### 用法

```bash
# 检测指定commits中的回滚对
python deepcommit/collect/collectors/revert_detector.py \
    --repo /path/to/repo \
    --commits abc123 def456 ghi789 \
    --method combined \
    --verbose

# 使用不同的检测方法
python deepcommit/collect/collectors/revert_detector.py \
    --repo /path/to/repo \
    --commits abc123 def456 \
    --method numstat      # 快速，基于行数统计

python deepcommit/collect/collectors/revert_detector.py \
    --repo /path/to/repo \
    --commits abc123 def456 \
    --method hash         # 精确，基于文件哈希
```

#### 检测方法

- `numstat`: 基于添加/删除行数的反向匹配（快速）
- `hash`: 基于文件内容哈希验证（精确）
- `combined`: 组合方法，先快速筛选再精确验证（推荐）

## 完整工作流程

### 收集新仓库数据

**方式 1：自动克隆（推荐，v2.1+）**

```bash
# 直接运行收集器，工具会自动处理本地仓库
python -m deepcommit.collect.collectors.gh_crawler \
    --owner urllib3 \
    --repo urllib3 \
    --start-tag 2.0.6 \
    --end-tag 2.3.0 \
    --output-dir DATA/github_data/preprocessed/urllib3_urllib3_2.0.6_2.3.0 \
    --repo-src-dirs src/urllib3/

# 如果本地仓库不存在，工具会：
# 1. 在 $DEEPCOMMIT_REPO_ROOT/urllib3_urllib3 下查找
# 2. 如果没找到，询问是否自动克隆
# 3. 克隆后自动使用
```

**方式 2：手动准备仓库**

```bash
# 1. 准备本地Git仓库（强烈推荐，可以提供更精确的分析）
# 注意：必须使用完整仓库（不是 bare repo），因为 commit DAG 等工具需要工作目录
cd DATA/github_data/repos
git clone https://github.com/urllib3/urllib3.git urllib3_urllib3

# 2. 运行数据收集器
cd /path/to/project
python -m deepcommit.collect.collectors.gh_crawler \
    --owner urllib3 \
    --repo urllib3 \
    --start-tag 2.0.6 \
    --end-tag 2.3.0 \
    --local-repo DATA/github_data/repos/urllib3_urllib3 \
    --output-dir DATA/github_data/preprocessed/urllib3_urllib3_2.0.6_2.3.0 \
    --repo-src-dirs src/urllib3/

# 3. 验证数据完整性
python scripts/test_preprocessed_data.py \
    DATA/github_data/preprocessed/urllib3_urllib3_2.0.6_2.3.0 \
    --local-repo DATA/github_data/repos/urllib3_urllib3
```

**关于本地仓库的说明：**

- **是否必需？** 本地仓库是可选的，但**强烈推荐**准备
- **自动克隆功能（v2.1+）：**
  - 如果未提供 `--local-repo` 参数，工具会自动在 `$DEEPCOMMIT_REPO_ROOT`（默认：`DATA/github_data/repos`）下查找仓库
  - 如果找不到，会询问是否自动克隆仓库
  - 如果提供了 `--local-repo` 但路径不存在，会自动克隆到指定路径
  - 自动克隆使用完整仓库（非 bare），符合后续分析需求
- **不提供本地仓库的影响：**
  - 会 fallback 到 GitHub API 获取数据
  - 无法精确判断 tag 是否在主线（first-parent chain）上
  - 可能遗漏部分数据或精确度降低
  - 后续的 commit DAG 生成步骤**必须**有本地仓库才能运行
- **为什么需要完整仓库？**
  - commit DAG 工具需要执行 `git blame`、`git apply` 等操作
  - 需要工作目录来检出文件和执行分析
  - **不要**使用 `git clone --bare`，必须使用普通 `git clone`

### 补充缺失的PR refs

如果发现某些PR commits缺失，可以使用以下脚本补充：

```bash
# 补充缺失的PR refs到Git仓库
python scripts/fetch_missing_pr_refs.py \
    DATA/github_data/repos/urllib3_urllib3.git \
    --data-dir DATA/github_data/preprocessed/urllib3_urllib3_2.0.6_2.3.0
```

### 改进现有数据

使用 `improve_preprocessed_data.py` 脚本可以：
- 检测并补充缺失的PRs和Issues
- 自动fetch新获取PRs的Git refs
- 重新过滤源码目录

```bash
python scripts/improve_preprocessed_data.py \
    DATA/github_data/preprocessed/urllib3_urllib3_2.0.6_2.3.0_old \
    --output-dir DATA/github_data/preprocessed/urllib3_urllib3_2.0.6_2.3.0_v2 \
    --src-dirs src/urllib3/ \
    --local-repo DATA/github_data/repos/urllib3_urllib3.git \
    --yes
```

## 环境配置

### GitHub Token

确保设置了有效的GitHub token：

```bash
export GITHUB_TOKEN="your_github_token_here"
```

或者在项目根目录的 `.envrc` 文件中配置：

```bash
export GITHUB_TOKEN="your_github_token_here"
```

### Python依赖

主要依赖包：
- `requests` - GitHub API调用
- `pygit2` - Git仓库操作
- `tqdm` - 进度条显示

## 数据质量检查

使用测试脚本验证收集的数据：

```bash
python scripts/test_preprocessed_data.py \
    DATA/github_data/preprocessed/urllib3_urllib3_2.0.6_2.3.0 \
    --local-repo DATA/github_data/repos/urllib3_urllib3.git
```

关键指标：
- `missing_main_commits`: 应该为 0
- `missing_pr_commits`: 应该为 0
- `pr_commit_association`: 应该为 100%

## 故障排除

### GitHub API 限制

如果遇到API速率限制：
1. 确保使用了有效的GitHub token
2. 检查token的权限和速率限制
3. 考虑降低 `--workers` 参数值

### 缺失的PR commits

如果测试发现缺失的PR commits：
1. 运行 `scripts/fetch_missing_pr_refs.py` 补充
2. 或者使用 `scripts/improve_preprocessed_data.py` 重新处理

### 本地仓库问题

如果本地仓库损坏或过时：
1. 删除旧仓库
2. 重新克隆：`git clone https://github.com/owner/repo.git owner_repo`（使用完整仓库，不是 bare）
3. 重新运行收集器

## 进阶使用

### 批量收集多个仓库

```bash
#!/bin/bash
repos=(
    "urllib3 urllib3 2.0.6 2.3.0"
    "psf requests 2.28.0 2.31.0"
    "pallets flask 2.0.0 2.3.0"
)

# 先克隆所有仓库（如果还没有）
for repo_config in "${repos[@]}"; do
    read -r owner repo start_tag end_tag <<< "$repo_config"
    repo_dir="DATA/github_data/repos/${owner}_${repo}"

    if [ ! -d "$repo_dir" ]; then
        echo "Cloning ${owner}/${repo}..."
        git clone "https://github.com/${owner}/${repo}.git" "$repo_dir"
    fi
done

# 收集数据
for repo_config in "${repos[@]}"; do
    read -r owner repo start_tag end_tag <<< "$repo_config"

    echo "Processing: ${owner}/${repo} (${start_tag} -> ${end_tag})"

    python -m deepcommit.collect.collectors.gh_crawler \
        --owner "$owner" \
        --repo "$repo" \
        --start-tag "$start_tag" \
        --end-tag "$end_tag" \
        --local-repo "DATA/github_data/repos/${owner}_${repo}" \
        --output-dir "DATA/github_data/preprocessed/${owner}_${repo}_${start_tag}_${end_tag}"
done
```

### 自定义过滤规则

通过修改 `core/filter_pipeline.py` 可以实现自定义过滤逻辑。

## 相关文档

- 项目根目录的文档：
  - `AUTO_FETCH_PR_REFS_SUMMARY.md` - PR refs自动获取功能说明
  - `TESTING_GUIDE.md` - 测试指南
  - `DATA_VALIDATION_REPORT.md` - 数据验证报告

## 贡献指南

如需修改或扩展功能，请：
1. 优先修改 `core/` 模块中的相关代码
2. 为新功能添加测试
3. 更新相关文档
