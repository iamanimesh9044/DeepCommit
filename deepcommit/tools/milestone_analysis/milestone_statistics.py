#!/usr/bin/env python3
"""Milestone statistics analysis tools"""

import json
import statistics
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

# Add parent directory to path for imports
_parent_dir = Path(__file__).parent.parent
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

# Try relative import first, fall back to absolute import
try:
    from ...utils.data.data_loader import load_main_commits, load_milestones
    from ..utils import resolve_repo_path_with_fallback
except ImportError:
    # For direct script execution
    from utils.data_loader import load_main_commits, load_milestones
    from utils.repo_utils import resolve_repo_path_with_fallback


@dataclass
class SplitInfo:
    """Information about a decomposed commit."""

    original_sha: str
    branch: str
    sub_commits: List[str] = field(default_factory=list)
    sub_commit_messages: Dict[str, str] = field(default_factory=dict)


def detect_commit_splits_from_metadata(split_metadata_dir: str) -> Dict[str, SplitInfo]:
    """Load decomposed commits from trial's commit_splits metadata directory.

    Args:
        split_metadata_dir: Path to the commit_splits directory containing split-* subdirs
                           (typically at trial_space/<trial>/stage4_decomp/output/commit_splits/)

    Returns:
        Dict mapping original commit SHA (7-char) to SplitInfo
    """
    if not split_metadata_dir:
        return {}

    splits_path = Path(split_metadata_dir)
    if not splits_path.exists():
        return {}

    splits: Dict[str, SplitInfo] = {}

    try:
        # Iterate over split-* directories
        for split_dir in splits_path.iterdir():
            if not split_dir.is_dir():
                continue

            # Handle both "split-<sha>" and "<sha>" naming conventions
            dir_name = split_dir.name
            if dir_name.startswith("split-"):
                original_sha = dir_name.replace("split-", "")[:7]
            else:
                original_sha = dir_name[:7]

            # Read METADATA.json
            metadata_file = split_dir / "METADATA.json"
            if not metadata_file.exists():
                continue

            with open(metadata_file, "r", encoding="utf-8") as f:
                metadata = json.load(f)

            # Extract sub-commits from various possible locations
            # Support multiple METADATA.json formats:
            # 1. split_result.src_commits (preferred)
            # 2. split_result.commits (legacy)
            # 3. top-level "commits" (newest format)
            # 4. top-level "split_commits" (another format)
            split_result = metadata.get("split_result", {})
            src_commits = (
                split_result.get("src_commits", [])
                or split_result.get("commits", [])
                or metadata.get("commits", [])
                or metadata.get("split_commits", [])
            )

            sub_commits = []
            sub_commit_messages = {}
            for commit_info in src_commits:
                # Support both short_hash/short_sha and hash/sha field names
                sha = (
                    commit_info.get("short_hash")
                    or commit_info.get("short_sha")
                    or commit_info.get("hash", "")
                    or commit_info.get("sha", "")
                )[:7]
                message = commit_info.get("message", "").split("\n")[0]  # First line only
                if sha:
                    sub_commits.append(sha)
                    sub_commit_messages[sha] = message

            if sub_commits:
                splits[original_sha] = SplitInfo(
                    original_sha=original_sha,
                    branch=split_result.get("working_branch", f"split-{original_sha}"),
                    sub_commits=sub_commits,
                    sub_commit_messages=sub_commit_messages,
                )

    except (json.JSONDecodeError, IOError, Exception):
        pass

    return splits


def detect_commit_splits(repo_path: str) -> Dict[str, SplitInfo]:
    """Detect decomposed commits by scanning for split-* branches in repo.

    NOTE: This is a fallback method. Prefer using detect_commit_splits_from_metadata()
    with the trial's commit_splits directory for accurate per-trial split detection.

    Args:
        repo_path: Path to the git repository

    Returns:
        Dict mapping original commit SHA (7-char) to SplitInfo
    """
    if not repo_path or not Path(repo_path).exists():
        return {}

    splits: Dict[str, SplitInfo] = {}

    try:
        # List all branches matching split-* pattern
        result = subprocess.run(
            ["git", "branch", "-a"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {}

        # Find split branches
        split_branches = []
        for line in result.stdout.splitlines():
            branch = line.strip().lstrip("* ")
            if branch.startswith("split-"):
                split_branches.append(branch)

        # Determine the base branch once (try develop, main, master, then use HEAD)
        base_branch = None
        for candidate in ["develop", "main", "master"]:
            check_base = subprocess.run(
                ["git", "rev-parse", "--verify", candidate],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if check_base.returncode == 0:
                base_branch = candidate
                break

        # If no standard branch found, try to get the default branch from HEAD
        if not base_branch:
            result = subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                # refs/remotes/origin/HEAD -> refs/remotes/origin/master
                ref = result.stdout.strip()
                base_branch = ref.split("/")[-1]

        # For each split branch, get the sub-commits
        for branch in split_branches:
            # Extract original SHA from branch name (split-<sha>)
            original_sha = branch.replace("split-", "")[:7]

            # Try to find the merge-base between split branch and base branch
            # This handles cases where the base branch has a non-standard name
            actual_base = base_branch
            if not actual_base:
                # Last resort: find common ancestor with any local branch
                local_branches = [
                    line.strip().lstrip("* ")
                    for line in subprocess.run(
                        ["git", "branch"],
                        cwd=repo_path,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    ).stdout.splitlines()
                    if not line.strip().lstrip("* ").startswith("split-")
                ]
                if local_branches:
                    actual_base = local_branches[0]

            if not actual_base:
                continue

            # Get commits unique to split branch
            result = subprocess.run(
                ["git", "log", "--oneline", f"{branch}", "--not", actual_base],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                continue

            sub_commits = []
            sub_commit_messages = {}
            for line in result.stdout.strip().splitlines():
                if line:
                    parts = line.split(" ", 1)
                    sha = parts[0][:7]
                    message = parts[1] if len(parts) > 1 else ""
                    sub_commits.append(sha)
                    sub_commit_messages[sha] = message

            if sub_commits:
                # Reverse to get chronological order (oldest first)
                sub_commits = list(reversed(sub_commits))
                splits[original_sha] = SplitInfo(
                    original_sha=original_sha,
                    branch=branch,
                    sub_commits=sub_commits,
                    sub_commit_messages=sub_commit_messages,
                )

    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass

    return splits


def analyze_commit_coverage(
    csv_file: str,
    main_commits_file: str = "",
    repo_path: str = "",
    split_metadata_dir: str = "",
    save_results: bool = False,
    output_dir: str = "",
) -> Dict:
    """分析里程碑的commit覆盖率（支持子commit识别）

    Args:
        csv_file: 里程碑CSV文件路径
        main_commits_file: main commits文件路径
        repo_path: git仓库路径（用于检测split分支，作为fallback）
        split_metadata_dir: trial的commit_splits元数据目录（优先使用）
                           典型路径: trial_space/<trial>/stage4_decomp/output/commit_splits/
        save_results: 是否保存结果到文件
        output_dir: 结果输出目录，默认为csv_file所在目录/analysis_results

    Returns:
        包含覆盖率分析结果的字典，包括：
        - main_coverage: 原始覆盖率（不考虑sub-commits）
        - effective_coverage: 真实覆盖率（考虑sub-commits后的覆盖率）
        - decomposed_commits: 被分解的commits详情
    """
    # 加载外部数据源
    all_main_commits = load_main_commits(main_commits_file) if main_commits_file else set()

    # 使用 data_loader 加载里程碑数据（commits已规范化为7位SHA）
    milestones = load_milestones(csv_file)

    # 检测 split commits（仅使用 metadata 目录，不再 fallback 到 repo 分支扫描）
    # NOTE: Removed fallback to repo branch scanning because:
    # - commit_splits_reference is for reference only (milestones may keep original commits)
    # - Only explicit commit_splits directory should trigger split detection
    splits: Dict[str, SplitInfo] = {}
    if split_metadata_dir:
        splits = detect_commit_splits_from_metadata(split_metadata_dir)

    # 构建 sub-commit -> original commit 的反向映射
    sub_to_original: Dict[str, str] = {}
    for original_sha, split_info in splits.items():
        for sub_sha in split_info.sub_commits:
            sub_to_original[sub_sha] = original_sha

    # 统计所有commits 并检测重叠
    milestone_commits = set()
    commit_to_milestones: Dict[str, List[str]] = defaultdict(list)
    for milestone_id, milestone_info in milestones.items():
        for commit in milestone_info["commits"]:
            milestone_commits.add(commit)
            commit_to_milestones[commit].append(milestone_id)

    # 分类 main commits: regular vs decomposed
    regular_commits = set()
    decomposed_commits = set()
    for commit in all_main_commits:
        if commit in splits:
            decomposed_commits.add(commit)
        else:
            regular_commits.add(commit)

    # 计算 regular commits 覆盖率
    regular_covered = regular_commits & milestone_commits
    regular_uncovered = regular_commits - milestone_commits

    # 计算 decomposed commits 覆盖率
    decomposed_details = []
    decomposed_fully_covered = 0
    decomposed_partially_covered = 0
    decomposed_not_covered = 0
    total_sub_commits = 0
    covered_sub_commits = 0

    for original_sha in decomposed_commits:
        split_info = splits[original_sha]
        sub_commits = split_info.sub_commits
        total_sub_commits += len(sub_commits)

        # 检查每个 sub-commit 的覆盖情况
        sub_coverage = []
        for sub_sha in sub_commits:
            # 尝试匹配（支持不同长度的SHA）
            matched_milestone = None
            for ms_commit in milestone_commits:
                if sub_sha.startswith(ms_commit) or ms_commit.startswith(sub_sha):
                    matched_milestone = commit_to_milestones.get(ms_commit, [None])[0]
                    break
            is_covered = matched_milestone is not None
            if is_covered:
                covered_sub_commits += 1
            sub_coverage.append(
                {
                    "sha": sub_sha,
                    "message": split_info.sub_commit_messages.get(sub_sha, ""),
                    "covered": is_covered,
                    "milestone": matched_milestone,
                }
            )

        # 判断覆盖状态
        covered_count = sum(1 for s in sub_coverage if s["covered"])
        if covered_count == len(sub_commits):
            status = "fully_covered"
            decomposed_fully_covered += 1
        elif covered_count > 0:
            status = "partially_covered"
            decomposed_partially_covered += 1
        else:
            status = "not_covered"
            decomposed_not_covered += 1

        decomposed_details.append(
            {
                "original_sha": original_sha,
                "branch": split_info.branch,
                "status": status,
                "sub_commits_total": len(sub_commits),
                "sub_commits_covered": covered_count,
                "sub_commits": sub_coverage,
            }
        )

    # 计算真实覆盖率（effective coverage）
    # regular: 直接覆盖的
    # decomposed: fully_covered 的算作覆盖
    effectively_covered = len(regular_covered) + decomposed_fully_covered
    effective_coverage = (effectively_covered / len(all_main_commits) * 100) if all_main_commits else None

    # 原始覆盖率（不考虑 sub-commits）
    raw_covered = milestone_commits & all_main_commits
    raw_coverage = (len(raw_covered) / len(all_main_commits) * 100) if all_main_commits else None

    # 识别 invalid commits（不在 main_commits 中，也不是 sub-commits）
    invalid_commits = {}
    valid_sub_commits = set(sub_to_original.keys())
    if all_main_commits:
        for milestone_id, milestone_info in milestones.items():
            invalid = [
                c
                for c in milestone_info["commits"]
                if c not in all_main_commits
                and c not in valid_sub_commits
                # 也检查前缀匹配
                and not any(c.startswith(sub[:7]) or sub.startswith(c[:7]) for sub in valid_sub_commits)
            ]
            if invalid:
                invalid_commits[milestone_id] = invalid

    # 识别 overlapping commits（在多个 milestone 中出现的）
    overlapping_commits = {commit: mids for commit, mids in commit_to_milestones.items() if len(mids) > 1}

    # 返回分析结果
    result = {
        # 总览
        "summary": {
            "total_main_commits": len(all_main_commits),
            "regular_commits": len(regular_commits),
            "decomposed_commits": len(decomposed_commits),
            "total_sub_commits": total_sub_commits,
        },
        # 覆盖率
        "coverage": {
            "raw_coverage": raw_coverage,  # 原始覆盖率（不考虑sub-commits）
            "effective_coverage": effective_coverage,  # 真实覆盖率（考虑sub-commits）
            "regular": {
                "total": len(regular_commits),
                "covered": len(regular_covered),
                "uncovered": len(regular_uncovered),
                "percentage": (len(regular_covered) / len(regular_commits) * 100) if regular_commits else 100.0,
            },
            "decomposed": {
                "total": len(decomposed_commits),
                "fully_covered": decomposed_fully_covered,
                "partially_covered": decomposed_partially_covered,
                "not_covered": decomposed_not_covered,
            },
            "sub_commits": {
                "total": total_sub_commits,
                "covered": covered_sub_commits,
                "percentage": (covered_sub_commits / total_sub_commits * 100) if total_sub_commits else 100.0,
            },
        },
        # 被分解的commits详情
        "decomposed_details": decomposed_details,
        # 未覆盖的commits
        "uncovered": {
            "regular_commits": list(regular_uncovered),
            "partial_decomposed": [d for d in decomposed_details if d["status"] == "partially_covered"],
            "not_covered_decomposed": [d for d in decomposed_details if d["status"] == "not_covered"],
        },
        # 兼容旧格式
        "main_coverage": raw_coverage,
        "total_main_commits": len(all_main_commits),
        "invalid_commits": invalid_commits,
        "overlapping_commits": overlapping_commits,
    }

    # Save results if requested
    if save_results:
        csv_path_obj = Path(csv_file)

        # Use provided output_dir or default to csv_file parent/analysis_results
        if output_dir:
            out_dir = Path(output_dir)
        else:
            out_dir = csv_path_obj.parent / "analysis_results"

        out_dir.mkdir(parents=True, exist_ok=True)

        # Use fixed filename for milestone coverage analysis
        output_file = out_dir / "milestone_coverage_analysis.json"

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        result["output_file"] = str(output_file)

    return result


def analyze_milestone_size_balance(
    csv_file: str,
    repo_path: str = "",
    save_results: bool = False,
    output_dir: str = "",
    repo_src_dir: Optional[Union[str, List[str]]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> Dict:
    """分析里程碑规模平衡性

    Args:
        csv_file: 里程碑CSV文件路径
        repo_path: 本地bare repo路径（用于LoC统计）
        save_results: 是否保存结果到文件
        output_dir: 结果输出目录，默认为csv_file所在目录/analysis_results
        repo_src_dir: 可选的源码目录路径前缀，只统计该目录下的文件变更
                     可以是单个字符串或字符串列表
                     例如: "src/" 只统计 src 目录下的文件
                          ["src/", "lib/"] 统计 src 或 lib 目录下的文件
        exclude_patterns: 可选的排除模式列表，用于排除匹配的文件
                         支持glob模式，例如: ["**/tests/**", "**/*.md"]

    Returns:
        包含规模平衡性分析结果的字典，包括：
        - milestones: 里程碑列表
        - total_count: 里程碑总数
        - valid_milestone_count: 有效里程碑数量（commit数量 > 0）
        - size_stats: 基本规模统计（均值、中位数、标准差、变异系数等）
        - loc_stats: LoC统计（如果提供了repo_path）
    """
    repo_path = resolve_repo_path_with_fallback(repo_path)

    # 使用 data_loader 加载里程碑数据（commits已规范化为7位SHA）
    milestones = load_milestones(csv_file)

    # 计算里程碑大小统计
    milestone_sizes = [len(m["commits"]) for m in milestones.values()]

    # 统计有效里程碑数量（commit数量 > 0）
    valid_milestone_count = sum(1 for size in milestone_sizes if size > 0)

    mean_size = 0
    median_size = 0
    std_dev = 0
    cv = None
    loc_stats = None

    if milestone_sizes:
        mean_size = statistics.mean(milestone_sizes)
        median_size = statistics.median(milestone_sizes)

        if len(milestone_sizes) > 1:
            std_dev = statistics.stdev(milestone_sizes)
        else:
            std_dev = 0

        # 计算变异系数
        if mean_size > 0:
            cv = std_dev / mean_size

        # 基于LoC的统计分析
        if repo_path and Path(repo_path).exists():
            milestone_commits_list = [m["commits"] for m in milestones.values()]
            loc_stats = _calculate_loc_stats(
                milestone_commits_list, repo_path, path_prefix=repo_src_dir, exclude_patterns=exclude_patterns
            )

    # 返回分析结果
    result = {
        # "milestones": [
        #     {"id": mid, **info} for mid, info in milestones.items()
        # ],  # 转换为列表格式用于JSON序列化
        "total_count": len(milestones),
        "valid_milestone_count": valid_milestone_count,
        "size_stats": {
            "mean": mean_size,
            "median": median_size,
            "std_dev": std_dev,
            "min": min(milestone_sizes) if milestone_sizes else 0,
            "max": max(milestone_sizes) if milestone_sizes else 0,
            "cv": cv,
        },
    }

    # 添加LoC统计（如果有）
    if loc_stats:
        result["loc_stats"] = loc_stats

    # Save results if requested
    if save_results:
        csv_path_obj = Path(csv_file)

        # Use provided output_dir or default to csv_file parent/analysis_results
        if output_dir:
            out_dir = Path(output_dir)
        else:
            out_dir = csv_path_obj.parent / "analysis_results"

        out_dir.mkdir(parents=True, exist_ok=True)

        # Use fixed filename for milestone balance analysis
        output_file = out_dir / "milestone_balance_analysis.json"

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        result["output_file"] = str(output_file)
        result["output_message"] = f"Results saved to {output_file}"

    return result


def _calculate_loc_stats(
    milestone_commits: List[List[str]],
    repo_path: str,
    path_prefix: Optional[Union[str, List[str]]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> Dict[str, float]:
    """计算里程碑的LoC统计信息

    Args:
        milestone_commits: 每个里程碑的commit列表
        repo_path: 本地bare repo路径
        path_prefix: 可选的路径前缀过滤器，只统计该路径下的文件变更
                    可以是单个字符串或字符串列表
                    例如: "src/" 只统计 src 目录下的文件
                         ["src/", "lib/"] 统计 src 或 lib 目录下的文件
        exclude_patterns: 可选的排除模式列表，用于排除匹配的文件
                         支持glob模式，例如: ["**/tests/**", "**/*.md"]

    Returns:
        包含LoC统计结果的字典，包括：
        - loc_mean/median/std/min/max: LoC基本统计
        - loc_cv: LoC变异系数（衡量工作量平衡性）
        - path_prefix: 如果指定了路径前缀，返回使用的前缀
        - exclude_patterns: 如果指定了排除模式，返回使用的模式
    """
    if not repo_path or not Path(repo_path).exists():
        return {}

    try:
        # 导入LocalGitRepo
        import sys

        sys.path.append(str(Path(__file__).parent.parent.parent))  # 添加deepcommit目录到路径
        from deepcommit.utils.git.local_git_repo import LocalGitRepo

        repo = LocalGitRepo(repo_path)

        # 计算每个里程碑的LoC
        milestone_locs = []

        for commits in milestone_commits:
            total_loc = 0
            valid_commits = 0  # 实际存在的commits数量

            for commit in commits:
                if commit:  # 确保commit不是空字符串
                    loc_info = repo.get_commit_loc(commit, path_prefix=path_prefix, exclude_patterns=exclude_patterns)

                    # 检查commit是否存在
                    if loc_info.get("exists", False):
                        total_loc += loc_info["total"]
                        valid_commits += 1

            # 只有有有效commits的里程碑才纳入计算
            if valid_commits > 0:
                milestone_locs.append(total_loc)

        # 过滤掉LoC为0的里程碑（可能是空里程碑或获取失败）
        valid_locs = [loc for loc in milestone_locs if loc > 0]

        if len(valid_locs) < 2:
            return {}

        # 计算各种统计量
        results = {
            "loc_mean": statistics.mean(valid_locs),
            "loc_median": statistics.median(valid_locs),
            "loc_std": statistics.stdev(valid_locs),
            "loc_min": min(valid_locs),
            "loc_max": max(valid_locs),
        }

        # 计算LoC的变异系数（直接衡量工作量分布的平衡性）
        loc_mean = statistics.mean(valid_locs)
        if loc_mean > 0:
            results["loc_cv"] = results["loc_std"] / loc_mean

        # 如果指定了路径前缀，记录到结果中
        if path_prefix is not None:
            results["path_prefix"] = path_prefix

        # 如果指定了排除模式，记录到结果中
        if exclude_patterns is not None:
            results["exclude_patterns"] = exclude_patterns

        return results

    except Exception:
        return {}


if __name__ == "__main__":
    # 文件路径（默认项目根；可通过命令行参数覆盖）
    base_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    csv_file = f"{base_dir}/DATA/result/full_dag_v1.1/cc_think/milestones.csv"
    main_commits_file = f"{base_dir}/DATA/github_data/preprocessed/urllib3_urllib3_2.0.6_2.3.0/main_commits.jsonl"

    # 本地bare repo路径（从环境变量获取或使用默认值）
    repo_path = f"{base_dir}/DATA/github_data/repos/urllib3_urllib3"

    # 运行覆盖率分析
    print("运行Commit覆盖率分析...")
    coverage_results = analyze_commit_coverage(csv_file, main_commits_file, save_results=True)

    # 运行规模平衡性分析
    print("运行规模平衡性分析...")
    balance_results = analyze_milestone_size_balance(csv_file, repo_path, save_results=True)
    # print("\n\n")
