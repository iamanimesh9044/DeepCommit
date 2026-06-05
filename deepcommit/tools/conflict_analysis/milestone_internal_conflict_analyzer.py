#!/usr/bin/env python3
"""
Milestone Conflict Analyzer V2 - Dependency Completeness Checker (Functional Design)

基于 commit DAG 的 milestone 内部依赖完整性检查（函数式设计）。

核心理念：
Milestone 应该是"依赖完整"的原子单元。如果 milestone 包含 commits A 和 B，
且存在依赖路径 A → ... → B，那么路径上的所有中间节点都必须在这个 milestone 内。

检测逻辑：
1. 对于 milestone 中按时间排序的任意两个 commits A 和 B（A 在 B 前）
2. 如果存在依赖路径 A → ... → B
3. 检查路径上的所有中间节点是否都在 milestone 内
4. 如果有节点不在 milestone 内 → 报告为 incomplete_dependency_chain

设计理念：
- 函数式设计，每个步骤都是独立的纯函数
- 内部完整性检查：milestone 内的依赖链必须完整
- 不依赖全局状态，易于测试和复用
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pygit2

from deepcommit.utils.data.commit_dag_loader import CommitDAGLoader
from deepcommit.utils.data.data_loader import load_dependency_graph, load_milestones
from deepcommit.utils.git.local_git_repo import LocalGitRepo


def sort_commits_by_time(repo: LocalGitRepo, commits: List[str]) -> List[str]:
    """按时间排序 commits"""
    commit_times = {}
    for commit_sha in commits:
        try:
            commit = repo.repo.revparse_single(commit_sha[:7]).peel(pygit2.Commit)
            commit_times[commit_sha] = commit.commit_time
        except:  # noqa: E722
            pass

    return sorted(commits, key=lambda x: commit_times.get(x, 0))


def get_required_frontier_commits(milestone: Dict, dag: CommitDAGLoader) -> set:
    """
    方向1：基于 commit DAG 递归查找所有需要的前置 commits

    使用反向传递闭包快速查找所有依赖的 commits。
    从 milestone 的所有 commits 出发，查找所有它们依赖的节点，
    排除当前 milestone 内的 commits。

    时间复杂度：O(n) vs 原来的 O(n × (V + E))

    Args:
        milestone: milestone 数据（需要包含 'commits' 字段）
        dag: CommitDAGLoader 实例

    Returns:
        所有需要的前置 commits 的集合（短 SHA）
    """
    # 获取反向传递闭包（第一次调用会计算并缓存）
    reverse_closure = dag.get_reverse_transitive_closure()

    commits = milestone.get("commits", [])
    milestone_commits = set(c[:7] for c in commits)

    required_commits = set()

    # 对于 milestone 中的每个 commit，查找它依赖的所有节点
    for commit in milestone_commits:
        # reverse_closure[commit] 包含所有能到达 commit 的节点（即 commit 依赖的节点）
        dependencies = reverse_closure.get(commit, set())
        # 排除 milestone 内的节点
        required_commits.update(dependencies - milestone_commits)

    return required_commits


def get_all_prerequisite_milestones(milestone_id: str, milestone_dependencies: Dict[str, set]) -> set:
    """
    递归获取 milestone 的所有前置 milestones

    Args:
        milestone_id: milestone ID
        milestone_dependencies: milestone 依赖关系字典

    Returns:
        所有前置 milestone IDs 的集合
    """
    prerequisites = set()
    to_visit = [milestone_id]
    visited = set()

    while to_visit:
        current = to_visit.pop(0)

        if current in visited:
            continue
        visited.add(current)

        # 获取直接前置 milestones
        direct_prerequisites = milestone_dependencies.get(current, [])
        for prereq in direct_prerequisites:
            if prereq not in prerequisites:
                prerequisites.add(prereq)
                to_visit.append(prereq)

    return prerequisites


def get_provided_frontier_commits(
    milestone_id: str,
    milestones: Dict[str, Dict],
    milestone_dependencies: Dict[str, set],
) -> set:
    """
    方向2：基于 milestone dependencies，获取前置 milestones 提供的 commits

    Args:
        milestone_id: 当前 milestone ID
        milestones: 所有 milestones 字典
        milestone_dependencies: milestone 依赖关系

    Returns:
        前置 milestones 提供的 commits 集合（短 SHA），排除当前 milestone 中的 commits
    """
    current_milestone = milestones[milestone_id]
    # 获取所有前置 milestones
    prerequisite_milestones = get_all_prerequisite_milestones(milestone_id, milestone_dependencies)

    # 收集前置 milestones 中的所有 commits
    provided_commits = set()
    for prereq_id in prerequisite_milestones:
        if prereq_id in milestones:
            prereq_milestone = milestones[prereq_id]
            for commit_sha in prereq_milestone.get("commits", []):
                provided_commits.add(commit_sha[:7])

    # 排除当前 milestone 中的 commits
    current_commits = set(c[:7] for c in current_milestone.get("commits", []))
    provided_commits = provided_commits - current_commits

    return provided_commits


def check_dependency_path_completeness(
    sorted_commits: List[str],
    dag: CommitDAGLoader,
) -> List[Dict]:
    """
    检查 milestone 内部的依赖路径完整性

    对于 milestone 中按时间排序的任意两个 commits A 和 B（A 在 B 前），
    如果存在依赖路径 A → ... → B，检查路径上的所有中间节点是否都在 milestone 内。

    Args:
        sorted_commits: 按时间排序的 commits 列表
        dag: CommitDAGLoader 实例

    Returns:
        不完整依赖链列表，每个元素包含：
        - source_commit: 源 commit（短 SHA）
        - target_commit: 目标 commit（短 SHA）
        - source_position: 源 commit 在排序列表中的位置
        - target_position: 目标 commit 在排序列表中的位置
        - missing_intermediate_nodes: 缺失的中间节点列表
        - missing_count: 缺失节点数量
    """
    incomplete_chains = []

    # 构建 milestone commits 集合（用于快速查找）
    milestone_commit_set = set(c[:7] for c in sorted_commits)

    print(f"  📊 Checking dependency completeness for {len(sorted_commits)} commits...", file=sys.stderr)

    for i in range(len(sorted_commits)):
        commit_a = sorted_commits[i]

        for j in range(i + 1, len(sorted_commits)):
            commit_b = sorted_commits[j]

            # 检查是否存在 A → B 的依赖路径（使用传递闭包，O(1) 查询）
            if dag.has_dependency_path_fast(commit_a, commit_b):
                # 获取路径上的所有节点（使用传递闭包，O(V) vs 指数级）
                path_nodes = dag.get_intermediate_nodes_fast(commit_a, commit_b)

                # 检查中间节点（排除 A 和 B）
                missing_intermediate = []
                for node in path_nodes:
                    node_short = node[:7]
                    # 跳过 A 和 B 本身
                    if node_short == commit_a[:7] or node_short == commit_b[:7]:
                        continue

                    # 检查节点是否在 milestone 内
                    if node_short not in milestone_commit_set:
                        missing_intermediate.append(node_short)

                if missing_intermediate:
                    conflict_info = {
                        # "milestone_id": milestone_id,
                        # "milestone_title": milestone_title,
                        "source_commit": commit_a[:7],
                        "target_commit": commit_b[:7],
                        "source_position": i,
                        "target_position": j,
                        "missing_intermediate_nodes": sorted(set(missing_intermediate)),
                        "missing_count": len(set(missing_intermediate)),
                    }
                    incomplete_chains.append(conflict_info)
                    print(
                        f"    ⚠️  Incomplete chain: {commit_a[:7]} → {commit_b[:7]}, "
                        f"missing {len(set(missing_intermediate))} intermediate nodes",
                        file=sys.stderr,
                    )

    return incomplete_chains


def analyze_frontier_prerequisites(
    milestone_id: str,
    # milestone: Dict,
    milestones: Dict[str, Dict],
    dag: CommitDAGLoader,
    milestone_dependencies: Optional[Dict[str, set]] = None,
) -> Dict:
    """
    分析 frontier prerequisites，对比需要的和声明的前置依赖

    Args:
        milestone_id: milestone ID
        milestone: 当前 milestone 数据
        dag: CommitDAGLoader 实例
        milestones: 所有 milestones 字典
        milestone_dependencies: milestone 依赖关系（可选）

    Returns:
        frontier prerequisites 分析结果
    """
    milestone = milestones[milestone_id]
    # 方向1：需要的前置 commits（总是计算）
    required_frontier = get_required_frontier_commits(milestone, dag)

    result = {
        "required_frontier": {
            "commits": sorted(required_frontier),
            "count": len(required_frontier),
        }
    }

    # 如果有 dependencies 文件，计算方向2和 missing frontier
    if milestone_dependencies:
        provided_frontier = get_provided_frontier_commits(milestone_id, milestones, milestone_dependencies)
        missing_frontier = required_frontier - provided_frontier

        result.update(
            {
                "provided_frontier": {
                    "commits": sorted(provided_frontier),
                    "count": len(provided_frontier),
                },
                "missing_frontier": {
                    "commits": sorted(missing_frontier),
                    "count": len(missing_frontier),
                },
                # "coverage_rate": (
                #     len(provided_frontier) / len(required_frontier)
                #     if required_frontier
                #     else 1.0
                # ),
            }
        )

    return result


def analyze_milestone_completeness(
    milestone_id: str,
    milestone: Dict,
    dag: CommitDAGLoader,
    repo: LocalGitRepo,
    milestones: Dict[str, Dict],
    milestone_dependencies: Optional[Dict[str, set]] = None,
) -> Dict:
    """
    分析单个 milestone 的内部依赖完整性和 frontier prerequisites

    Args:
        milestone_id: milestone ID
        milestone: milestone 数据
        dag: CommitDAGLoader 实例
        repo: LocalGitRepo 实例
        milestones: 所有 milestones 字典
        milestone_dependencies: milestone 依赖关系（可选）

    Returns:
        分析结果字典
    """
    print(f"\n🔍 Analyzing milestone: {milestone_id}", file=sys.stderr)
    milestone = milestones[milestone_id]
    commits = milestone["commits"]

    # 1. 检查 commit 存在性（基于 DAG）
    valid_commits = [c for c in commits if c[:7] in dag.nodes]
    invalid_commits = [c for c in commits if c not in valid_commits]

    if invalid_commits:
        print(f"  ⚠️  Warning: {len(invalid_commits)} commits not found in DAG", file=sys.stderr)

    # 2. 按时间排序
    sorted_commits = sort_commits_by_time(repo, valid_commits)

    # 3. 检查依赖路径完整性
    incomplete_chains = check_dependency_path_completeness(sorted_commits, dag)

    # 提取所有唯一的缺失 commits
    missing_commits = set()
    for chain in incomplete_chains:
        missing_commits.update(chain["missing_intermediate_nodes"])
    missing_commits = sorted(missing_commits)  # 排序以保持一致性

    print("  ✅ Results:", file=sys.stderr)
    print(f"    - Total commits: {len(sorted_commits)}", file=sys.stderr)
    print(f"    - Incomplete dependency chains: {len(incomplete_chains)} ⚠️", file=sys.stderr)
    print(f"    - Missing commits (unique): {len(missing_commits)} ⚠️", file=sys.stderr)

    # 4. 分析 frontier prerequisites
    print("  🔍 Analyzing frontier prerequisites...", file=sys.stderr)
    frontier_analysis = analyze_frontier_prerequisites(milestone_id, milestones, dag, milestone_dependencies)

    print(f"    - Required frontier commits: {frontier_analysis['required_frontier']['count']}", file=sys.stderr)
    if milestone_dependencies:
        print(f"    - Provided frontier commits: {frontier_analysis['provided_frontier']['count']}", file=sys.stderr)
        print(f"    - Missing frontier commits: {frontier_analysis['missing_frontier']['count']} ⚠️", file=sys.stderr)

    return {
        # "milestone_id": milestone_id,
        "title": milestone["title"],
        "total_commits": len(commits),
        "valid_commits": len(valid_commits),
        "invalid_commits": invalid_commits,
        "missing_commits": missing_commits,
        "incomplete_chains": incomplete_chains,
        "conflict_count": len(incomplete_chains),
        "frontier_prerequisites": frontier_analysis,
    }


def analyze_all_milestones(
    repo_path: str,
    milestone_csv_path: str,
    commit_dag_path: str,
    dependencies_csv_path: Optional[str] = None,
    milestone_ids: Optional[List[str]] = None,
    main_branch: str = "main",
    split_metadata_dir: Optional[str] = None,
) -> Dict:
    """
    分析所有或指定的 milestones

    Args:
        repo_path: Git 仓库路径
        milestone_csv_path: milestone CSV 文件路径
        commit_dag_path: commit_dag.json 文件路径
        main_branch: 主分支名称
        milestone_ids: 要分析的 milestone IDs，None 表示分析所有
        dependencies_csv_path: milestone dependencies CSV 文件路径（可选）
        split_metadata_dir: split commit 元数据目录（可选，包含 split-* 子目录）

    Returns:
        完整的分析结果
    """
    # 加载数据
    print(f"📊 Loading commit DAG from {commit_dag_path}...", file=sys.stderr)
    dag = CommitDAGLoader(commit_dag_path)
    print(f"   ✓ Loaded {len(dag.nodes)} commits, {len(dag.edges_map)} with dependencies", file=sys.stderr)

    # 应用 split commit 元数据（如果提供）
    if split_metadata_dir:
        print(f"📊 Applying split commit metadata from {split_metadata_dir}...", file=sys.stderr)
        split_count = dag.apply_split_metadata(
            split_metadata_dir,
            repo_path=repo_path,
            use_blame=True,
        )
        if split_count > 0:
            print(
                f"   ✓ Applied {split_count} split commits (blame=enabled), DAG now has {len(dag.nodes)} nodes",
                file=sys.stderr,
            )

    print(f"📋 Loading milestones from {milestone_csv_path}...", file=sys.stderr)
    milestones = load_milestones(milestone_csv_path)
    print(f"   ✓ Loaded {len(milestones)} milestones", file=sys.stderr)

    # 加载 milestone dependencies（如果提供）
    milestone_dependencies = None
    if dependencies_csv_path and os.path.exists(dependencies_csv_path):
        print(f"📋 Loading milestone dependencies from {dependencies_csv_path}...", file=sys.stderr)
        milestone_dependencies = load_dependency_graph(dependencies_csv_path)

    print("🔧 Initializing repository...", file=sys.stderr)
    repo = LocalGitRepo(repo_path, main_branch)

    # 确定要分析的 milestones
    if milestone_ids is None:
        milestone_ids = list(milestones.keys())

    # 预计算传递闭包（一次性操作，显著提升多 milestone 分析性能）
    print(f"\n🚀 Preparing for analysis of {len(milestone_ids)} milestones...", file=sys.stderr)
    dag.get_transitive_closure()
    dag.get_reverse_transitive_closure()

    print("\n🚀 Starting V2 dependency completeness analysis...", file=sys.stderr)

    # 分析每个 milestone
    results = {}
    total_conflicts = 0
    milestones_with_conflicts = 0

    for milestone_id in milestone_ids:
        if milestone_id not in milestones:
            print(f"  ⚠️  Milestone {milestone_id} not found", file=sys.stderr)
            continue

        result = analyze_milestone_completeness(
            milestone_id, milestones[milestone_id], dag, repo, milestones, milestone_dependencies
        )

        results[milestone_id] = result

        # 更新统计
        if result["conflict_count"] > 0:
            milestones_with_conflicts += 1
            total_conflicts += result["conflict_count"]

    # 构建完整结果 (metadata first, summary second)
    analysis_results = {}
    analysis_results["metadata"] = {
        "repo_path": repo_path,
        "milestone_csv": str(milestone_csv_path),
        "commit_dag": str(commit_dag_path),
        "analysis_time": datetime.now().isoformat(),
        "main_branch": main_branch,
        "version": "v2.0 (Dependency Completeness Checker - Functional)",
    }
    analysis_results["summary"] = {
        "total_milestones_analyzed": len(milestone_ids),
        "milestones_with_incomplete_chains": milestones_with_conflicts,
        "total_incomplete_chains": total_conflicts,
    }
    analysis_results["milestones"] = results

    print("\n✅ V2 Analysis complete!", file=sys.stderr)
    print(f"  📊 Total incomplete dependency chains: {total_conflicts}", file=sys.stderr)
    print(f"  📊 Milestones with issues: {milestones_with_conflicts}", file=sys.stderr)

    return analysis_results


def generate_conflict_summary(analysis_results: Dict, output_dir: Path) -> None:
    """生成冲突汇总 JSON"""
    conflicts = []

    for milestone_id, result in analysis_results["milestones"].items():
        for conflict in result.get("incomplete_chains", []):
            conflict_summary = {
                "milestone": milestone_id,
                "title": result["title"],
                "source": conflict["source_commit"],
                "target": conflict["target_commit"],
                "missing_nodes": conflict["missing_intermediate_nodes"],
                "missing_count": conflict["missing_count"],
            }
            conflicts.append(conflict_summary)

    summary_report = {
        "metadata": analysis_results["metadata"],
        "summary": analysis_results["summary"],
        "incomplete_chains": conflicts,
    }

    summary_path = output_dir / "conflict_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_report, f, indent=2, ensure_ascii=False)
    print(f"  ✅ Conflict summary: {summary_path}", file=sys.stderr)


def generate_reports(analysis_results: Dict, output_dir: Path) -> None:
    """
    生成分析报告文件

    Args:
        analysis_results: 分析结果
        output_dir: 输出目录
    """
    print(f"\n📝 Generating conflict report in {output_dir}...", file=sys.stderr)

    output_dir.mkdir(parents=True, exist_ok=True)

    # 只生成冲突摘要 JSON（包含有冲突的 milestones）
    generate_conflict_summary(analysis_results, output_dir)

    print("✅ Report generated", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="V2: Milestone dependency completeness checker (functional design)")
    parser.add_argument("--repo", "-r", required=True, help="Path to git repository (work tree)")
    parser.add_argument("--milestones", "-m", required=True, help="Path to milestones CSV file")
    parser.add_argument("--dag", "-d", required=True, help="Path to commit_dag.json file")
    parser.add_argument("--dependencies", help="Path to milestone dependencies CSV file (optional)")
    parser.add_argument("--branch", "-b", default="main", help="Main branch name (default: main)")
    parser.add_argument("--sample", "-s", nargs="+", help="Sample specific milestones to analyze")
    parser.add_argument("--output", "-o", help="Output directory path")

    args = parser.parse_args()

    # 验证文件存在
    if not os.path.exists(args.repo):
        print(f"❌ Error: Repository not found: {args.repo}", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(args.milestones):
        print(f"❌ Error: Milestones CSV not found: {args.milestones}", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(args.dag):
        print(f"❌ Error: Commit DAG JSON not found: {args.dag}", file=sys.stderr)
        sys.exit(1)

    # 确定输出目录
    if args.output:
        output_dir = Path(args.output)
    else:
        milestone_dir = Path(args.milestones).parent
        output_dir = milestone_dir / "analysis_results" / "milestone_conflict_result_v3"

    # 运行分析
    if args.sample:
        print(f"📌 Analyzing specified milestones: {', '.join(args.sample)}", file=sys.stderr)
    else:
        print("📌 Analyzing all milestones...", file=sys.stderr)

    analysis_results = analyze_all_milestones(
        args.repo, args.milestones, args.dag, args.branch, args.sample, args.dependencies
    )

    # 生成报告
    generate_reports(analysis_results, output_dir)

    print(f"\n✨ Conflict analysis results saved to: {output_dir}", file=sys.stderr)
    print("   📂 File: conflict_summary.json (metadata + summary + conflict details)", file=sys.stderr)


if __name__ == "__main__":
    main()
