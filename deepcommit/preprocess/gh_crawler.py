#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
JSONL格式GitHub Release数据收集工具 - 函数式编程版本
支持增量存储和断点续传
"""

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple

try:
    from packaging.version import InvalidVersion, Version
except ImportError:
    print("Installing packaging library...")
    import subprocess

    subprocess.check_call([sys.executable, "-m", "pip", "install", "packaging"])
    from packaging.version import InvalidVersion, Version

# Add project root to sys.path for imports
if __name__ == "__main__":
    # Get project root (3 levels up from this file)
    project_root = Path(__file__).parent.parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

# Import external dependencies
if TYPE_CHECKING:
    from deepcommit.preprocess.git_api_util import GitHubAPI, clone_repo, fetch_pr_refs
    from deepcommit.utils.data.index_tool import IndexTool
    from deepcommit.utils.git.local_git_repo import LocalGitRepo
else:
    try:
        # Try relative import first (for module usage)
        from ..repo.local_git_repo import LocalGitRepo
    except ImportError:
        try:
            # Try absolute import (for direct script execution)
            from deepcommit.utils.git.local_git_repo import LocalGitRepo
        except ImportError:
            LocalGitRepo = None
            print("Warning: LocalGitRepo not available. Some features will be limited.")

    try:
        from ..git_api_util import GitHubAPI, clone_repo, fetch_pr_refs
    except ImportError:
        try:
            from deepcommit.preprocess.git_api_util import GitHubAPI, clone_repo, fetch_pr_refs
        except ImportError:
            GitHubAPI = None
            clone_repo = None
            fetch_pr_refs = None
            print("Warning: GitHubAPI not available. GitHub API features will be limited.")

    try:
        from ...utils.data.index_tool import IndexTool
    except ImportError:
        try:
            from deepcommit.utils.data.index_tool import IndexTool
        except ImportError:
            IndexTool = None
            print("Warning: IndexTool not available. Index building will be skipped.")

# Import core modules
try:
    from .collect import DataCollector, DataExporter, FilterPipeline
except ImportError:
    try:
        from deepcommit.preprocess.collect import DataCollector, DataExporter, FilterPipeline
    except ImportError:
        print("Warning: Core modules not available. Using v1 collector.")
        DataCollector = None
        FilterPipeline = None
        DataExporter = None

try:
    from tqdm import tqdm  # noqa: F401  availability probe; triggers the auto-install below on ImportError
except ImportError:
    print("Installing tqdm for progress bars...")
    import subprocess

    subprocess.check_call([sys.executable, "-m", "pip", "install", "tqdm"])


@dataclass
class CollectorConfig:
    """收集器配置"""

    owner: str
    repo: str
    token: str
    output_dir: Path
    local_repo_path: Optional[str] = None
    main_branch: str = "main"
    start_tag: Optional[str] = None
    end_tag: Optional[str] = None
    limit: Optional[int] = None
    max_workers: int = 10
    tag_filter: Optional[Callable[[str], bool]] = None
    build_index: bool = True
    # Commit文件路径过滤：只保留修改了指定目录下文件的commits
    repo_src_dirs: Optional[List[str]] = None  # e.g., ["src/", "lib/"]
    # 增量处理控制
    overwrite: bool = False  # 是否强制重新收集（忽略已有数据）
    # Tag前缀过滤：用于多产品线的 repo（如 tutanota 的 "tutanota-release-"）
    # 只有以此前缀开头的 tags 才会被处理
    tag_prefix: Optional[str] = None
    # 测试目录/文件模式：明确指定测试路径，这些文件会被排除
    # 例如: ["tests/**", "**/*_test.go", "**/src/test/java/**"]
    test_dirs: Optional[List[str]] = None
    # 额外排除模式：排除 test_dirs 覆盖不了的噪音文件
    # 例如: ["**/testdata/**", "**/fixtures/**", "**/__snapshots__/**"]
    exclude: Optional[List[str]] = None


# ============================================================================
# 注：旧架构的增量处理代码已移除（CollectorState、CollectorFiles等）
# 新架构使用 deepcommit/preprocess/collect/data_models.py 中的 CollectedData
# 如需查看历史实现，请参考 git 历史记录
# ============================================================================


def create_tag_filter(pattern: str = None, major_version: int = None) -> Optional[Callable[[str], bool]]:
    """创建tag过滤函数"""
    if not pattern and major_version is None:
        return None

    def filter_func(tag: str) -> bool:
        if pattern:
            if not re.match(pattern, tag):
                return False

        if major_version is not None:
            match = re.match(r"^v?(\d+)\.", tag)
            if match:
                tag_major = int(match.group(1))
                if tag_major != major_version:
                    return False
            else:
                return False

        return True

    return filter_func


def find_previous_tag_on_mainline(
    tag: str,
    local_repo: Optional[LocalGitRepo],
    github_api: Optional[GitHubAPI],
    tag_filter: Optional[Callable[[str], bool]] = None,
) -> Optional[str]:
    """查找主线上的前一个tag"""
    if local_repo:
        try:
            if not local_repo.is_tag_on_main_first_parent(tag):
                print(f"Warning: Tag {tag} is not on mainline first-parent chain")
                # Fallback to GitHub API
                if github_api:
                    all_tags = github_api.get_all_tags()
                    for i, (t, _) in enumerate(all_tags):
                        if t == tag and i > 0:
                            return all_tags[i - 1][0]
                return None

            prev_tag = local_repo.previous_tag_on_main(tag, tag_filter)
            return prev_tag
        except Exception as e:
            print(f"Error using LocalGitRepo for tag {tag}: {e}")

    # Fallback to GitHub API
    if github_api:
        all_tags = github_api.get_all_tags()
        for i, (t, _) in enumerate(all_tags):
            if t == tag and i > 0:
                return all_tags[i - 1][0]

    return None


def filter_tags_on_mainline(tags: List[Tuple[str, str]], local_repo: Optional[LocalGitRepo]) -> List[Tuple[str, str]]:
    """过滤出在主线上的tags"""
    if not local_repo:
        return tags

    filtered_tags = []
    for tag_name, date in tags:
        try:
            if local_repo.is_tag_on_main_first_parent(tag_name):
                filtered_tags.append((tag_name, date))
        except:  # noqa: E722
            filtered_tags.append((tag_name, date))

    print(f"Filtered {len(tags)} tags to {len(filtered_tags)} mainline tags")
    return filtered_tags


def filter_tags_by_semver_range(
    tags: List[Tuple[str, str, str]], start_tag: Optional[str], end_tag: Optional[str]
) -> List[Tuple[str, str, str]]:
    """使用语义化版本过滤tags，返回 (start_tag, end_tag] 范围内的tags

    Args:
        tags: [(tag_name, commit_sha, date), ...] 格式的tag列表
        start_tag: 起始tag（不包括），如 "v4.55.4" 或 "4.55.4"
        end_tag: 结束tag（包括），如 "v4.57.1" 或 "4.57.1"

    Returns:
        过滤后的tags列表，按版本号排序

    逻辑：
    1. 从tag名称中提取版本号（支持 vX.Y.Z 和 X.Y.Z 格式）
    2. 跳过 prerelease 和 devrelease 版本
    3. 同一版本号优先选择带 v 前缀的tag
    4. 返回在 (start_version, end_version] 范围内的tags
    """
    if not start_tag and not end_tag:
        # 如果没有指定范围，返回所有有效的语义化版本tags
        return _filter_semver_tags(tags)

    # 解析起始和结束版本
    start_version = _parse_tag_version(start_tag) if start_tag else None
    end_version = _parse_tag_version(end_tag) if end_tag else None

    if start_tag and not start_version:
        raise ValueError(f"无法解析起始tag '{start_tag}' 的版本号")
    if end_tag and not end_version:
        raise ValueError(f"无法解析结束tag '{end_tag}' 的版本号")

    # 第一步：收集所有有效的语义化版本tags
    # {Version -> (chosen_tag_name, commit_sha, date)}
    version_map = {}

    for tag_name, commit_sha, date in tags:
        version = _parse_tag_version(tag_name)
        if not version:
            continue  # 跳过无法解析的tag

        # 跳过 prerelease 和 devrelease
        if version.is_prerelease or version.is_devrelease:
            continue

        # 同一版本优先选择带 v 前缀的
        if version not in version_map:
            version_map[version] = (tag_name, commit_sha, date)
        elif tag_name.startswith("v") and not version_map[version][0].startswith("v"):
            # 新tag带v，旧tag不带v，替换
            version_map[version] = (tag_name, commit_sha, date)

    # 第二步：过滤范围
    filtered_tags = []
    for version in sorted(version_map.keys()):
        # 区间：(start_version, end_version]
        if start_version and version <= start_version:
            continue  # 小于等于起始版本，跳过
        if end_version and version > end_version:
            continue  # 大于结束版本，跳过

        filtered_tags.append(version_map[version])

    print(f"语义化版本过滤: {len(tags)} -> {len(filtered_tags)} tags")
    if start_tag and end_tag:
        print(f"  范围: ({start_tag}, {end_tag}]")
    elif start_tag:
        print(f"  范围: ({start_tag}, ∞)")
    elif end_tag:
        print(f"  范围: (-∞, {end_tag}]")

    return filtered_tags


def _parse_tag_version(tag_name: str) -> Optional[Version]:
    """从tag名称中解析版本号（灵活匹配）

    使用搜索模式从 tag 名称中提取版本号，支持任意前缀：
    - vX.Y.Z (如 v4.55.4)
    - X.Y.Z (如 4.55.4)
    - prefix-X.Y.Z (如 dubbo-3.3.5, tutanota-release-259.241213.0)

    Returns:
        Version对象，如果无法解析则返回None
    """
    # 搜索版本号模式：X.Y 或 X.Y.Z 或 X.Y.Z.W
    match = re.search(r"(\d+\.\d+(?:\.\d+)?(?:\.\d+)?)", tag_name)
    if not match:
        return None

    version_str = match.group(1)
    try:
        return Version(version_str)
    except InvalidVersion:
        return None


def _filter_semver_tags(tags: List[Tuple[str, str, str]]) -> List[Tuple[str, str, str]]:
    """过滤出所有有效的语义化版本tags（去重，去prerelease）

    用于没有指定范围时的默认行为
    """
    version_map = {}

    for tag_name, commit_sha, date in tags:
        version = _parse_tag_version(tag_name)
        if not version:
            continue

        if version.is_prerelease or version.is_devrelease:
            continue

        if version not in version_map:
            version_map[version] = (tag_name, commit_sha, date)
        elif tag_name.startswith("v") and not version_map[version][0].startswith("v"):
            version_map[version] = (tag_name, commit_sha, date)

    return [version_map[v] for v in sorted(version_map.keys())]


def get_commits_in_range(
    config: CollectorConfig, local_repo: Optional[LocalGitRepo], github_api: Optional[GitHubAPI]
) -> Optional[Set[str]]:
    """获取指定tag范围内的所有commits"""
    if not (config.start_tag or config.end_tag):
        return None

    # 优先使用LocalGitRepo
    if config.start_tag and config.end_tag and local_repo:
        print("\n[Step 0] 使用LocalGitRepo获取tag范围内的commits...")
        try:
            commits = local_repo.get_commits_between_tags(config.start_tag, config.end_tag, include_start=False)
            all_commits = set(commits)
            print(f"获取到 {len(all_commits)} 个commits")
            return all_commits
        except Exception as e:
            print(f"警告: 无法使用LocalGitRepo获取commits: {e}")

    # Fallback到GitHub API
    if github_api:
        print("\n[Step 0] 使用GitHub API获取tag范围内的commits...")
        try:
            if config.start_tag and config.end_tag:
                # 找到start_tag的前一个tag以包含start_tag的变更
                # prev_of_start = find_previous_tag_on_mainline(
                #     config.start_tag, local_repo, github_api, config.tag_filter
                # )
                # actual_start = prev_of_start if prev_of_start else config.start_tag

                commits = github_api.get_commits_between_tags(config.start_tag, config.end_tag)
                # end_tag_sha = github_api.get_tag_sha(config.end_tag)
                filter_commits = set(commits)
                # filter_commits.add(end_tag_sha)

            elif config.end_tag:
                commits = github_api.get_commits_between_tags(None, config.end_tag)
                # end_tag_sha = github_api.get_tag_sha(config.end_tag)
                filter_commits = set(commits)
                # filter_commits.add(end_tag_sha)

            elif config.start_tag:
                default_branch = github_api.get_default_branch()
                commits = github_api.get_commits_between_tags(config.start_tag, default_branch)
                filter_commits = set(commits)

            print(f"获取到 {len(filter_commits)} 个commits用于过滤")
            return filter_commits

        except Exception as e:
            print(f"警告: 无法获取tag范围内的commits: {e}")

    return None


def get_tags_to_process(
    config: CollectorConfig,
    local_repo: Optional[LocalGitRepo],
    github_api: Optional[GitHubAPI],
    filter_commits: Optional[Set[str]] = None,
) -> List[Tuple[str, str]]:
    """获取需要处理的tags（三步过滤）

    过滤步骤：
    1. tag_prefix 过滤（可选）：只保留指定前缀的 tags
    2. semver 范围过滤：只保留版本号在 (start, end] 范围内的 tags
    3. branch-out point 过滤：只保留 branch-out point 在 commits 范围内的 tags

    这三步确保返回的 tags 既在版本范围内，又与 git 拓扑相关。

    Args:
        config: 收集器配置
        local_repo: 本地 git 仓库（必须提供以进行 branch-out 检查）
        github_api: GitHub API（备用）
        filter_commits: 已废弃，不再使用

    Returns:
        过滤后的 tags 列表 [(tag_name, date), ...]
    """
    print("\n[Step 1] 获取tags...")

    if not config.start_tag or not config.end_tag:
        raise ValueError("必须同时指定 start_tag 和 end_tag")

    if not local_repo:
        raise ValueError("必须提供 local_repo 以进行 branch-out point 检查")

    # 获取所有 tags
    try:
        all_tags = local_repo.get_all_tags()  # [(name, sha, date), ...]
        print(f"从 LocalGitRepo 获取到 {len(all_tags)} 个 tags")
    except Exception as e:
        print(f"错误: 无法获取 tags: {e}")
        return []

    # 获取 commits 范围（用于 branch-out point 检查）
    try:
        commits = local_repo.get_commits_between_branch_out_points(config.start_tag, config.end_tag)
        commit_set = set(commits)
        print(f"commits 范围: {len(commits)} 个")
    except Exception as e:
        print(f"错误: 无法获取 commits 范围: {e}")
        return []

    # 解析 start/end 版本号
    start_v = _parse_tag_version(config.start_tag)
    end_v = _parse_tag_version(config.end_tag)

    if not start_v or not end_v:
        print(f"警告: 无法解析版本号 start={start_v}, end={end_v}")

    # 三步过滤
    result = []
    step1_count = 0
    step2_count = 0
    step3_count = 0

    for tag_name, _sha, date in all_tags:
        # Step 1: tag_prefix 过滤（可选）
        if config.tag_prefix and not tag_name.startswith(config.tag_prefix):
            continue
        step1_count += 1

        # Step 2: semver 范围过滤
        v = _parse_tag_version(tag_name)
        if v is None:
            continue  # 无法解析版本号，跳过
        if start_v and end_v and not (start_v < v <= end_v):
            continue  # 不在范围内，跳过
        step2_count += 1

        # Step 3: branch-out point 在 commits 范围内
        try:
            bo = local_repo.find_branch_out_point(tag_name)
            if bo not in commit_set:
                continue  # branch-out point 不在范围内，跳过
        except Exception:
            continue  # 无法找到 branch-out point，跳过
        step3_count += 1

        result.append((tag_name, date))

    # 打印过滤统计
    prefix_info = f"tag_prefix='{config.tag_prefix}'" if config.tag_prefix else "无 tag_prefix"
    print(f"三步过滤 ({prefix_info}):")
    print(f"  Step1 (tag_prefix): {len(all_tags)} -> {step1_count}")
    print(f"  Step2 (semver {start_v} < v <= {end_v}): {step1_count} -> {step2_count}")
    print(f"  Step3 (branch-out in commits): {step2_count} -> {step3_count}")
    print(f"最终结果: {len(result)} 个 tags")

    # 应用额外的 tag 过滤器（如果有）
    if config.tag_filter:
        before = len(result)
        result = [(tag, date) for tag, date in result if config.tag_filter(tag)]
        print(f"额外 Tag 过滤器: {before} -> {len(result)} tags")

    return result


def process_pr(
    pr_number: int, github_api: GitHubAPI, config: CollectorConfig = None, filter_commits: Optional[Set[str]] = None
) -> Optional[Dict[str, Any]]:
    """处理单个PR"""
    pr_detail = github_api.get_pr_details(pr_number)
    if pr_detail:
        pr_commits = github_api.get_pr_commits(pr_number)
        pr_detail["commits"] = pr_commits

        # 添加base commit是否在范围内的标记
        # 为了解决rebase的问题，使用PR的第一个commit（最早的commit）的parent来进行过滤
        if filter_commits is not None:
            base_in_range = True  # 默认值

            if pr_commits:
                # 获取第一个（最早的）commit的parent作为真正的分支起点
                try:
                    first_commit_sha = pr_commits[-1]  # PR commits通常按时间逆序排列
                    first_commit_parents = github_api.get_commit_parents(first_commit_sha)
                    if first_commit_parents:
                        # 使用第一个parent作为分支起点
                        branch_point = first_commit_parents[0]
                        base_in_range = branch_point in filter_commits
                    else:
                        # 如果没有parent，可能是初始commit，使用原来的base_sha逻辑
                        base_sha = pr_detail.get("base_sha")
                        base_in_range = base_sha in filter_commits if base_sha else True
                except Exception:
                    # 如果获取commit parents失败，fallback到原来的逻辑
                    base_sha = pr_detail.get("base_sha")
                    base_in_range = base_sha in filter_commits if base_sha else True
            else:
                # 如果没有PR commits，使用原来的base_sha逻辑
                base_sha = pr_detail.get("base_sha")
                base_in_range = base_sha in filter_commits if base_sha else True

            pr_detail["base_in_range"] = base_in_range

        return pr_detail
    return None


def normalize_path_prefix(path: str) -> str:
    """规范化路径前缀

    Args:
        path: 路径字符串，如 "src", "./src/", "src/"

    Returns:
        规范化后的路径，如 "src/"
    """
    # 移除开头的 ./
    path = path.lstrip("./")
    # 确保以 / 结尾（除非是空字符串）
    if path and not path.endswith("/"):
        path += "/"
    return path


def check_commit_matches_src_dirs(commit_data: Dict[str, Any], repo_src_dirs: Optional[List[str]]) -> bool:
    """检查commit是否修改了指定源码目录下的文件

    Args:
        commit_data: commit数据，包含files_changed字段
        repo_src_dirs: 源码目录列表，如["src/", "lib/"]。None表示不过滤

    Returns:
        True表示应该保留该commit，False表示应该过滤

    过滤规则：
    1. 如果repo_src_dirs为None或空列表，保留所有commits
    2. 如果commit的files_changed为空，且设置了repo_src_dirs，则过滤掉
    3. 如果commit至少修改了一个在repo_src_dirs下的文件，则保留
    4. 对于重命名的文件，检查新旧路径
    """
    # 如果没有设置过滤目录，保留所有commits
    if not repo_src_dirs:
        return True

    # 规范化所有源码目录路径
    normalized_dirs = [normalize_path_prefix(d) for d in repo_src_dirs]

    # 获取文件变更列表
    files_changed = commit_data.get("files_changed", [])

    # 如果没有文件变更信息，过滤掉（除非数据不完整，则保留以避免丢失重要commit）
    if not files_changed:
        # 如果数据标记为不完整，保守起见保留该commit
        if commit_data.get("data_incomplete", False):
            return True
        # 否则过滤掉
        return False

    # 检查是否有任何文件在指定的源码目录下
    for file_info in files_changed:
        # 文件路径可能在不同字段中
        file_path = file_info.get("filename") or file_info.get("path") or ""

        # 检查文件路径是否以任一源码目录开头
        for src_dir in normalized_dirs:
            if file_path.startswith(src_dir):
                return True

        # 对于重命名的文件，也检查旧路径
        previous_filename = file_info.get("previous_filename")
        if previous_filename:
            for src_dir in normalized_dirs:
                if previous_filename.startswith(src_dir):
                    return True

    # 没有找到任何匹配的文件，过滤掉
    return False


def process_commit(
    commit_sha: str,
    github_api: Optional[GitHubAPI],
    git_repo: Optional[LocalGitRepo] = None,
    commit_to_prs: Dict[str, List[int]] = None,
) -> Optional[Dict[str, Any]]:
    """处理单个commit，提取所需信息

    Args:
        commit_sha: commit的SHA值
        github_api: GitHubAPI实例
        git_repo: LocalGitRepo实例（可选）
        commit_to_prs: commit到PR的映射字典（可选）

    Returns:
        包含commit完整信息的字典
    """
    try:
        commit_data = None

        # 优先使用LocalGitRepo（如果可用）
        if git_repo:
            try:
                # 获取commit基本信息
                commit_info = git_repo.get_commit_info(commit_sha)
                # 获取文件变更信息
                files_changed = git_repo.get_commit_files(commit_sha)

                # 构建commit数据
                commit_data = {
                    "sha": commit_info["sha"],
                    "message": commit_info["message"],
                    "author": commit_info["author"],
                    "committer": commit_info["committer"],
                    "parent_shas": commit_info["parent_shas"],
                    "files_changed": files_changed,
                    "files_count": len(files_changed),
                    "additions": sum(f.get("additions", 0) for f in files_changed),
                    "deletions": sum(f.get("deletions", 0) for f in files_changed),
                }

                # 添加时间戳（转换为ISO格式）
                commit_data["author"]["time"] = datetime.fromtimestamp(
                    commit_info["author"]["time"], tz=timezone.utc
                ).isoformat()
                commit_data["committer"]["time"] = datetime.fromtimestamp(
                    commit_info["committer"]["time"], tz=timezone.utc
                ).isoformat()

                # 判断是否是merge commit
                commit_data["is_merge_commit"] = len(commit_info["parent_shas"]) > 1

            except Exception:
                # 如果LocalGitRepo失败，fallback到GitHub API
                commit_data = None

        # 使用GitHub API
        if not commit_data and github_api:
            commit_detail = github_api.get_commit_details(commit_sha)
            if commit_detail:
                # 构建commit数据
                commit_data = {
                    "sha": commit_detail["sha"],
                    "message": commit_detail.get("commit", {}).get("message", ""),
                    "author": {
                        "name": commit_detail.get("commit", {}).get("author", {}).get("name", ""),
                        "email": commit_detail.get("commit", {}).get("author", {}).get("email", ""),
                        "time": commit_detail.get("commit", {}).get("author", {}).get("date", ""),
                    },
                    "committer": {
                        "name": commit_detail.get("commit", {}).get("committer", {}).get("name", ""),
                        "email": commit_detail.get("commit", {}).get("committer", {}).get("email", ""),
                        "time": commit_detail.get("commit", {}).get("committer", {}).get("date", ""),
                    },
                    "parent_shas": [p["sha"] for p in commit_detail.get("parents", [])],
                    "files_changed": commit_detail.get("files", []),
                    "files_count": len(commit_detail.get("files", [])),
                    "additions": commit_detail.get("stats", {}).get("additions", 0),
                    "deletions": commit_detail.get("stats", {}).get("deletions", 0),
                    "is_merge_commit": len(commit_detail.get("parents", [])) > 1,
                }

        if not commit_data:
            # 如果无法获取详细信息，至少保存基本信息
            print(f"  警告: 无法获取commit {commit_sha[:8]}的详细信息，保存基本信息")
            commit_data = {
                "sha": commit_sha,
                "message": "",
                "author": {"name": "", "email": "", "time": ""},
                "committer": {"name": "", "email": "", "time": ""},
                "parent_shas": [],
                "files_changed": [],
                "files_count": 0,
                "additions": 0,
                "deletions": 0,
                "is_merge_commit": False,
                "data_incomplete": True,  # 标记数据不完整
            }

        # 添加上游引用信息（PR / issue / ticket）
        if commit_to_prs and commit_sha in commit_to_prs:
            commit_data["associated_refs"] = commit_to_prs[commit_sha]
        else:
            commit_data["associated_refs"] = []

        return commit_data

    except Exception as e:
        print(f"处理commit {commit_sha[:8]}时出错: {e}")
        # 即使出错也返回基本信息
        return {
            "sha": commit_sha,
            "message": "",
            "author": {"name": "", "email": "", "time": ""},
            "committer": {"name": "", "email": "", "time": ""},
            "parent_shas": [],
            "files_changed": [],
            "files_count": 0,
            "additions": 0,
            "deletions": 0,
            "is_merge_commit": False,
            "associated_refs": commit_to_prs.get(commit_sha, []) if commit_to_prs else [],
            "data_incomplete": True,
            "error": str(e),
        }


def collect_release_data(
    config: CollectorConfig,
    local_repo: Optional[LocalGitRepo],
    github_api: Optional[GitHubAPI],
) -> Dict[str, Any]:
    """
    收集release数据 - 主函数

    新架构：收集 → 过滤 → 增强 → 导出

    改进点：
    1. 数据收集和过滤分离
    2. merged_via_pr字段100%完整
    3. 自动生成pr_commits.jsonl和pr_conversations.jsonl
    4. 数据完整性验证
    5. 增量处理 - 检测已有数据并跳过收集
    """
    start_time = datetime.now(timezone.utc)

    print("=" * 80)
    print("数据收集流程")
    print("=" * 80)

    # 导入数据模型和收集模块
    from deepcommit.preprocess.collect.data_collector import DataCollector
    from deepcommit.preprocess.collect.data_exporter import DataExporter
    from deepcommit.preprocess.collect.data_models import CollectedData
    from deepcommit.preprocess.collect.filter_pipeline import FilterPipeline

    # Raw data存储路径
    raw_data_dir = config.output_dir / "raw_data"

    # 检查是否已有raw data（优先检查）
    skip_collection = False
    raw_data = None

    if not config.overwrite and raw_data_dir.exists():
        if CollectedData.has_jsonl_dump(raw_data_dir):
            print("\n✓ 检测到raw data，跳过收集阶段（从raw_data加载）")
            print(f"  Raw data目录: {raw_data_dir}")
            print("  提示: 使用 --overwrite 参数可以强制重新收集")
            skip_collection = True

            print("\n加载raw data...")
            raw_data = CollectedData.from_jsonl(raw_data_dir)
            print(f"  ✓ Releases: {len(raw_data.releases)}")
            print(f"  ✓ Commits: {len(raw_data.commits)}")
            print(f"  ✓ PRs: {len(raw_data.prs)}")
            print(f"  ✓ Issues: {len(raw_data.issues)}")

    # 如果没有raw data，检查是否有过滤后的数据（旧的增量模式）
    if not skip_collection and not config.overwrite and config.output_dir.exists():
        if CollectedData.has_jsonl_dump(config.output_dir):
            print("\n✓ 检测到已有数据文件，跳过收集阶段（增量模式）")
            print(f"  输出目录: {config.output_dir}")
            print("  提示: 使用 --overwrite 参数可以强制重新收集")
            skip_collection = True

            # 加载现有数据（过滤后的数据）
            print("\n加载已有数据...")
            raw_data = CollectedData.from_jsonl(config.output_dir)
            print(f"  ✓ Releases: {len(raw_data.releases)}")
            print(f"  ✓ Commits: {len(raw_data.commits)}")
            print(f"  ✓ PRs: {len(raw_data.prs)}")
            print(f"  ✓ Issues: {len(raw_data.issues)}")

    # 如果启用overwrite，删除已有数据
    if config.overwrite and config.output_dir.exists():
        import shutil

        print("\n⚠️  Overwrite模式：删除已有数据目录")
        print(f"  删除: {config.output_dir}")
        shutil.rmtree(config.output_dir)
        config.output_dir.mkdir(parents=True, exist_ok=True)

    # 如果没有跳过收集，执行正常的收集流程
    if not skip_collection:
        # 获取需要处理的 tags（三步过滤：tag_prefix -> semver -> branch-out）
        tags_to_process = get_tags_to_process(config, local_repo, github_api)
        print(f"\n将处理 {len(tags_to_process)} 个tags")

        # 阶段1: 收集原始数据
        print("\n[数据收集阶段] 开始收集原始数据...")
        collector = DataCollector(config, github_api, local_repo)
        raw_data = collector.collect_all(None, None, tags_to_process)

        # 保存raw data到磁盘
        print(f"\n保存raw data到: {raw_data_dir}")
        raw_data_dir.mkdir(parents=True, exist_ok=True)
        raw_data.to_jsonl(raw_data_dir)
        print("  ✓ 已保存raw data到磁盘")
        print(f"  ✓ Releases: {len(raw_data.releases)}")
        print(f"  ✓ Commits: {len(raw_data.commits)}")
        print(f"  ✓ PRs: {len(raw_data.prs)}")
        print(f"  ✓ Issues: {len(raw_data.issues)}")

    # 确保raw_data不为None
    if raw_data is None:
        raise RuntimeError("无法获取raw data")

    collection_time = time.time() - start_time.timestamp()
    print("\n收集统计:")
    print(f"  Releases: {len(raw_data.releases)}")
    print(f"  Commits: {len(raw_data.commits)}")
    print(f"  PRs: {len(raw_data.prs)}")
    print(f"  Issues: {len(raw_data.issues)}")
    print(f"  耗时: {collection_time:.2f}s")

    # 阶段2: 应用过滤（传入github_api用于获取missing PRs/Issues）
    filter_start = time.time()  # noqa: F841
    filter_pipeline = FilterPipeline(config, github_api)
    filtered_data = filter_pipeline.apply(raw_data)

    print("\n过滤统计:")
    print(f"  过滤Commits: {filter_pipeline.stats['filtered_commits']}")
    print(f"  过滤PRs: {filter_pipeline.stats['filtered_prs']}")
    print(f"  过滤Issues: {filter_pipeline.stats['filtered_issues']}")

    # 阶段3: 导出数据（传入local_repo用于获取PR commits详情）
    export_start = time.time()  # noqa: F841
    metadata = DataExporter(config, local_repo).export(
        filtered_data, config.output_dir, start_time, filter_pipeline.stats
    )

    # 打印最终统计
    print("\n" + "=" * 80)
    print("收集完成!")
    print(f"  数据目录: {config.output_dir}")
    print(f"  总Releases: {metadata['total_releases']}")
    print(f"  总Commits: {metadata['total_commits']}")
    print(f"  总PRs: {metadata['total_prs']}")
    print(f"  总Issues: {metadata['total_issues']}")
    print(f"  PR Commits: {metadata['total_pr_commits']}")
    print(f"  耗时: {metadata['duration_seconds']:.2f} 秒")

    if metadata.get("filtered_commits") or metadata.get("filtered_prs") or metadata.get("filtered_issues"):
        print("\n  过滤统计:")
        if metadata.get("filtered_commits"):
            print(f"    - {metadata['filtered_commits']} 个commits（未修改src目录）")
        if metadata.get("filtered_prs"):
            print(f"    - {metadata['filtered_prs']} 个PRs（merge commit不在主分支）")
        if metadata.get("filtered_issues"):
            print(f"    - {metadata['filtered_issues']} 个Issues（未被PR引用）")

    print("=" * 80)

    return metadata


def build_index(output_dir: Path):
    """构建索引"""
    if not IndexTool:
        print("警告: IndexTool不可用，跳过索引构建")
        return None

    print("\n[Step 5] 构建索引...")
    start_time = datetime.now(timezone.utc)

    index_tool = IndexTool(str(output_dir))

    try:
        index = index_tool.build_index()

        if index:
            print("\n索引构建成功！")
            print(f"  索引文件: {index_tool.index_file}")

            stats = index_tool.query_statistics()
            print("\n索引统计信息:")
            print(f"  - 总releases数: {stats['total_releases']}")
            print(f"  - 总PRs数: {stats['total_prs']}")
            print(f"  - 总issues数: {stats['total_issues']}")
            print(f"  - 主分支commits数: {stats['commits_in_releases']}")
            print(f"  - 直接提交数（非PR）: {stats['orphan_commits']}")
            print(f"  - 跨版本PRs数: {stats['multi_release_prs']}")

            end_time = datetime.now(timezone.utc)
            duration = (end_time - start_time).total_seconds()
            print(f"\n索引构建耗时: {duration:.2f} 秒")

            return index
        else:
            print("警告: 索引构建失败")
            return None

    except Exception as e:
        print(f"错误: 构建索引时发生异常: {e}")
        import traceback

        traceback.print_exc()
        return None


def _ensure_local_repo(owner: str, repo: str, provided_path: Optional[str | Path]) -> Optional[Path]:
    """
    确保本地仓库存在，如果不存在则自动克隆。
    同时确保 PR refs 已被 fetch（用于获取 PR commits 详情）。

    Args:
        owner: GitHub 仓库所有者
        repo: 仓库名称
        provided_path: 用户提供的本地仓库路径（可选）

    Returns:
        本地仓库路径，如果无法获取则返回 None
    """
    import os

    repo_name = f"{owner}_{repo}"

    # 1. 如果用户提供了路径
    if provided_path:
        provided_path = Path(provided_path).expanduser().resolve()

        # 如果路径存在且是有效的 git 仓库，直接使用
        if provided_path.exists() and (provided_path / ".git").exists():
            print(f"✓ 使用已有本地仓库: {provided_path}")
            # 确保 PR refs 已被 fetch（用于获取 PR commits 详情）
            if fetch_pr_refs:
                fetch_pr_refs(provided_path)
            return provided_path

        # 如果路径不存在，尝试克隆到该路径
        print(f"⚠ 本地仓库路径不存在: {provided_path}")
        print(f"尝试克隆仓库到: {provided_path}")

        if clone_repo and clone_repo(owner, repo, provided_path):
            # clone_repo 内部已经会调用 fetch_pr_refs
            return provided_path
        else:
            print("✗ 克隆失败")
            return None

    # 2. 如果用户没有提供路径，尝试自动查找或创建
    # 获取仓库根目录（从环境变量或默认值）
    repo_root_env = os.getenv("DEEPCOMMIT_REPO_ROOT", os.getenv("AGENTBENCH_REPO_ROOT", "DATA/github_data/repos"))
    repo_root = Path(repo_root_env).expanduser().resolve()

    # 尝试查找已存在的仓库
    auto_repo_path = repo_root / repo_name

    if auto_repo_path.exists() and (auto_repo_path / ".git").exists():
        print(f"✓ 自动发现本地仓库: {auto_repo_path}")
        # 确保 PR refs 已被 fetch（用于获取 PR commits 详情）
        if fetch_pr_refs:
            fetch_pr_refs(auto_repo_path)
        return auto_repo_path

    # 仓库不存在，询问是否自动克隆
    print(f"\n本地仓库不存在: {auto_repo_path}")
    print(f"是否自动克隆 {owner}/{repo} 到该位置？")
    print(f"  仓库URL: https://github.com/{owner}/{repo}.git")
    print(f"  目标路径: {auto_repo_path}")

    response = input("继续克隆？[Y/n]: ").strip().lower()

    if response in ("", "y", "yes"):
        if clone_repo and clone_repo(owner, repo, auto_repo_path):
            return auto_repo_path
        else:
            print("✗ 克隆失败，将不使用本地仓库")
            return None
    else:
        print("跳过克隆，将不使用本地仓库")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="JSONL格式GitHub Release数据收集工具（函数式编程版本）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
数据存储格式:
  - main_commits.jsonl: 每行一个 main branch commit 的信息
  - releases.jsonl: 每行一个release的信息
  - pull_requests.jsonl: 每行一个PR信息
  - issues.jsonl: 每行一个issue的信息
  - metadata.json: 导出后的统计元数据（位于输出目录根部）
  - raw_data/: 未过滤的原始 JSONL dump
  - .index_cache.pkl: 构建的索引缓存文件

示例:
  # 收集指定tag范围
  %(prog)s --owner urllib3 --repo urllib3 --start-tag 2.1.0 --end-tag 2.2.0

  # 收集最近10个releases
  %(prog)s --owner pytorch --repo pytorch --limit 10

  # 使用本地仓库精确查找主线tags
  %(prog)s --owner urllib3 --repo urllib3 --start-tag 2.1.0 --end-tag 2.2.0 \\
           --local-repo /path/to/urllib3.git --main-branch main

  # 只收集语义化版本的tags
  %(prog)s --owner urllib3 --repo urllib3 --tag-pattern '^v?\\d+\\.\\d+\\.\\d+$'

  # 只收集修改了src/目录下文件的commits
  %(prog)s --owner urllib3 --repo urllib3 --start-tag 2.1.0 --end-tag 2.2.0 \\
           --repo-src-dirs src/
        """,
    )

    parser.add_argument("--owner", required=True, help="仓库所有者")
    parser.add_argument("--repo", required=True, help="仓库名称")
    parser.add_argument("--start-tag", help="起始tag")
    parser.add_argument("--end-tag", help="结束tag")
    parser.add_argument("--limit", type=int, help="限制release数量")
    parser.add_argument("--output-dir", default="data", help="输出目录")
    parser.add_argument(
        "--token",
        default=os.getenv("GITHUB_CRAWLER_TOKENS", "") or os.getenv("GITHUB_TOKEN", ""),
        help="GitHub token(s)",
    )
    parser.add_argument("--workers", type=int, default=10, help="并发worker数量")
    parser.add_argument("--local-repo", help="本地git仓库路径")
    parser.add_argument("--main-branch", help="主分支名称", default="main")
    parser.add_argument("--tag-pattern", help="Tag过滤正则表达式")
    parser.add_argument("--major-version", type=int, help="只收集特定主版本号的tags")
    parser.add_argument("--no-index", action="store_true", help="不构建索引")
    parser.add_argument(
        "--repo-src-dirs",
        nargs="+",
        help="源码目录列表，只保留修改了这些目录下文件的commits（例如: --repo-src-dirs src/ lib/）",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="强制重新收集数据（忽略已有数据，删除并重新生成）",
    )
    parser.add_argument(
        "--tag-prefix",
        dest="tag_prefix",
        default=None,
        help="Tag前缀过滤（用于多产品线的 repo，如 tutanota 的 'tutanota-release-'）",
    )
    # Test dirs and exclude patterns for filtering commits
    parser.add_argument(
        "--test-dirs",
        nargs="+",
        help="测试目录/文件模式列表（glob格式，如: --test-dirs 'tests/**' '**/*_test.go' '**/src/test/java/**'）",
    )
    parser.add_argument(
        "--exclude",
        nargs="+",
        help="额外排除模式列表（glob格式，如: --exclude '**/testdata/**' '**/fixtures/**' '**/__snapshots__/**'）",
    )

    args = parser.parse_args()

    # 验证参数
    if (args.start_tag or args.end_tag) and args.limit:
        parser.error("--start-tag/--end-tag 不能与 --limit 同时使用")

    if not args.token:
        print("错误: 请提供 --token 或设置 GITHUB_CRAWLER_TOKENS 环境变量", file=sys.stderr)
        sys.exit(1)

    print("=" * 60)
    print(f"收集仓库: {args.owner}/{args.repo}")
    if args.start_tag or args.end_tag:
        print(f"Tag范围: {args.start_tag or 'start'} -> {args.end_tag or 'latest'}")
    elif args.limit:
        print(f"限制: 最近 {args.limit} 个releases")
    print(f"并发Workers: {args.workers}")

    # Tag过滤方法
    print("Tag过滤方法: 三步过滤（tag_prefix -> semver -> branch-out）")
    if args.tag_prefix:
        print(f"Tag前缀: {args.tag_prefix}")

    if args.repo_src_dirs:
        print(f"Commit路径过滤: 已启用（只保留修改以下目录的commits: {', '.join(args.repo_src_dirs)}）")
    else:
        print("Commit路径过滤: 已禁用（默认）")
    if args.overwrite:
        print("运行模式: Overwrite（强制重新收集）")
    else:
        print("运行模式: 增量处理（如果数据存在则跳过收集）")
    print("=" * 60)

    # 创建输出目录
    # CLI already provides the full path, use it directly instead of appending subdirectory
    data_dir = Path(args.output_dir)

    # 创建tag过滤器
    tag_filter = None
    if args.tag_pattern or args.major_version is not None:
        tag_filter = create_tag_filter(pattern=args.tag_pattern, major_version=args.major_version)

    # 创建配置
    config = CollectorConfig(
        owner=args.owner,
        repo=args.repo,
        token=args.token,
        output_dir=data_dir,
        local_repo_path=args.local_repo,
        main_branch=args.main_branch,
        start_tag=args.start_tag,
        end_tag=args.end_tag,
        limit=args.limit,
        max_workers=args.workers,
        tag_filter=tag_filter,
        build_index=not args.no_index,
        repo_src_dirs=args.repo_src_dirs,
        overwrite=args.overwrite,
        tag_prefix=args.tag_prefix,
        test_dirs=args.test_dirs,
        exclude=args.exclude,
    )

    # 自动处理本地仓库：检查、查找或克隆
    local_repo_path = _ensure_local_repo(args.owner, args.repo, args.local_repo)
    if local_repo_path:
        # 更新配置中的 local_repo_path
        config = CollectorConfig(
            owner=args.owner,
            repo=args.repo,
            token=args.token,
            output_dir=data_dir,
            local_repo_path=str(local_repo_path),
            main_branch=args.main_branch,
            start_tag=args.start_tag,
            end_tag=args.end_tag,
            limit=args.limit,
            max_workers=args.workers,
            tag_filter=tag_filter,
            build_index=not args.no_index,
            repo_src_dirs=args.repo_src_dirs,
            overwrite=args.overwrite,
            tag_prefix=args.tag_prefix,
            test_dirs=args.test_dirs,
            exclude=args.exclude,
        )

    # 初始化工具
    local_repo = None
    if config.local_repo_path and LocalGitRepo:
        try:
            local_repo = LocalGitRepo(config.local_repo_path, args.main_branch)
            print(f"Initialized LocalGitRepo for {config.local_repo_path}")
        except Exception as e:
            print(f"Warning: Could not initialize LocalGitRepo: {e}")

    github_api = None
    if GitHubAPI:
        github_api = GitHubAPI(args.owner, args.repo, args.token)

        # 显示 rate limit 状态
        github_api.print_rate_limit_status()
    else:
        print("Error: GitHubAPI is required but not available")
        sys.exit(1)

    # 收集数据
    metadata = collect_release_data(config, local_repo, github_api)  # noqa: F841

    # 收集完成后显示 rate limit 状态
    print("\n收集完成后的 Rate Limit 状态:")
    github_api.print_rate_limit_status()

    # 构建索引
    if config.build_index:
        build_index(config.output_dir)


if __name__ == "__main__":
    main()
