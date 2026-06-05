#!/usr/bin/env python3
"""
Milestone Dependency Analyzer

统一的 milestone 依赖关系分析工具，基于 commit DAG 提供完整的依赖分析能力：

功能层级：
- Level 1: Commit 级别依赖查询
- Level 2: Milestone 点对点检查
- Level 3: 批量依赖推断
- Level 4: 依赖丰富与比较

主要功能：
1. 检查两个 milestone 之间的依赖关系（点对点）
2. 从 commit DAG 推断所有 milestone 依赖（批量推断）
3. 比较推断依赖与现有依赖，发现缺失/冗余（依赖丰富）
4. 导出丰富后的依赖关系

核心思想：
如果 milestone M1 中的 commit A 依赖 milestone M2 中的 commit B，
那么 M1 应该依赖 M2。
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ...utils.data.commit_dag_loader import CommitDAGLoader
from ...utils.data.data_loader import (
    load_dependency_graph,
    load_milestones,
    save_dependency_graph,
)

# Import from dag_analyzer
from ..dag_analysis.dag_analyzer import _detect_cycles_from_graph

# Import utilities
from ..utils.commit_utils import normalize_commit_sha
from ..utils.graph_algorithms import (
    compute_transitive_closure,
    transitive_reduction,
)


class MilestoneDependencyAnalyzer:
    """
    Milestone 依赖关系分析器

    基于 commit DAG 提供完整的 milestone 依赖分析功能：
    - 点对点检查：检查两个 milestone 之间的依赖关系
    - 批量推断：从 commit DAG 推断所有 milestone 依赖
    - 依赖丰富：比较推断依赖与现有依赖，发现缺失/冗余

    Usage:
        # 场景 1: 只需要检查两个 milestone
        analyzer = MilestoneDependencyAnalyzer(
            commit_dag_path="commit_dag.json",
            milestone_csv_path="milestones.csv"
        )
        result = analyzer.check_milestone_dependency("M1", "M2")

        # 场景 2: 批量推断所有依赖
        analyzer = MilestoneDependencyAnalyzer(
            commit_dag_path="commit_dag.json",
            milestone_csv_path="milestones.csv"
        )
        inferred_deps = analyzer.infer_dependencies_from_commits()

        # 场景 3: 完整的依赖丰富分析
        analyzer = MilestoneDependencyAnalyzer(
            commit_dag_path="commit_dag.json",
            milestone_csv_path="milestones.csv",
            dependencies_csv_path="dependencies.csv"
        )
        result = analyzer.enrich_dependencies()
    """

    def __init__(
        self,
        commit_dag_path: str,
        milestone_csv_path: str,
        dependencies_csv_path: Optional[str] = None,
        detect_redundant: bool = False,
        split_metadata_dir: Optional[str] = None,
        repo_path: Optional[str] = None,
    ):
        """
        初始化依赖分析器

        Args:
            commit_dag_path: commit_dag.json 文件路径
            milestone_csv_path: milestone CSV 文件路径
            dependencies_csv_path: 现有依赖关系 CSV（可选，仅在需要比较时提供）
            detect_redundant: 是否检测冗余的传递依赖（默认为 False）
            split_metadata_dir: split commit 元数据目录（可选，包含 split-* 子目录）
            repo_path: Git 仓库路径（可选，用于 split commit 的精确 blame 映射）
        """
        self.commit_dag_path = Path(commit_dag_path)
        self.milestone_csv_path = Path(milestone_csv_path)
        self.dependencies_csv_path = Path(dependencies_csv_path) if dependencies_csv_path else None
        self.detect_redundant = detect_redundant
        self.split_metadata_dir = split_metadata_dir
        self.repo_path = repo_path

        # 使用 CommitDAGLoader 加载 commit DAG
        print(f"🔄 Loading commit DAG from {commit_dag_path}...", file=sys.stderr)
        self.dag_loader = CommitDAGLoader(str(commit_dag_path))
        print(
            f"✅ Loaded {len(self.dag_loader.nodes)} commits with {len(self.dag_loader.edges_map)} dependencies",
            file=sys.stderr,
        )

        # 应用 split commit 元数据（如果提供）
        if split_metadata_dir:
            print(f"🔄 Applying split commit metadata from {split_metadata_dir}...", file=sys.stderr)
            split_count = self.dag_loader.apply_split_metadata(
                split_metadata_dir,
                repo_path=repo_path,
                use_blame=repo_path is not None,
            )
            if split_count > 0:
                print(
                    f"✅ Applied {split_count} split commits (blame={'enabled' if repo_path else 'disabled'}), DAG now has {len(self.dag_loader.nodes)} nodes",
                    file=sys.stderr,
                )

        # 构建依赖图：commit -> [依赖的 commits]
        self.dependency_graph = self.dag_loader.get_forward_dependency_graph(normalize=False)

        # 预计算传递闭包（核心优化：所有查询复用此结果）
        print("🔄 Computing transitive closure for dependency optimization...", file=sys.stderr)
        self.transitive_closure = compute_transitive_closure(self.dependency_graph)
        print(f"✅ Transitive closure computed for {len(self.transitive_closure)} commits", file=sys.stderr)

        # 加载 milestones
        print(f"🔄 Loading milestones from {milestone_csv_path}...", file=sys.stderr)
        self.milestones = load_milestones(str(milestone_csv_path))

        # 适配字段名
        for milestone_data in self.milestones.values():
            if "commits" in milestone_data and "all_commits" not in milestone_data:
                milestone_data["all_commits"] = milestone_data["commits"]

        print(f"✅ Loaded {len(self.milestones)} milestones", file=sys.stderr)

        # 建立映射
        self.commit_to_milestone = self._build_commit_to_milestone_map()
        self.milestone_commits = self._build_milestone_commits_map()

        # 分析 commit 覆盖情况
        self.invalid_commits = self._identify_invalid_commits()
        self.commits_not_in_milestones = self._identify_commits_not_in_milestones()

        # 加载现有依赖（如果提供）
        self.existing_dependencies = None
        if self.dependencies_csv_path:
            print(f"🔄 Loading existing dependencies from {dependencies_csv_path}...", file=sys.stderr)
            self.existing_dependencies = load_dependency_graph(str(dependencies_csv_path))
            print(
                f"✅ Loaded {sum(len(deps) for deps in self.existing_dependencies.values())} existing dependencies",
                file=sys.stderr,
            )

        # 记录各阶段的环信息
        self.existing_cycles = []
        self.inferred_cycles = []
        self.enriched_cycles = []

        # 检测现有依赖中的循环（早期发现问题）
        if self.existing_dependencies:
            all_nodes = set(self.milestones.keys())
            self.existing_cycles = _detect_cycles_from_graph(self.existing_dependencies, all_nodes)
            if self.existing_cycles:
                print(
                    f"⚠️  WARNING: Found {len(self.existing_cycles)} cycle(s) in existing dependencies", file=sys.stderr
                )

        print("✅ Analyzer initialization complete\n", file=sys.stderr)

    # ============================================================
    # 辅助方法：数据结构构建
    # ============================================================

    def _build_commit_to_milestone_map(self) -> Dict[str, str]:
        """
        建立 commit -> milestone 的映射
        只包含在 commit DAG 中实际存在的 commits

        Returns:
            commit_to_milestone[commit_sha] = milestone_id
        """
        commit_to_milestone = {}

        # 获取 DAG 中所有的 commits
        dag_commits = set(normalize_commit_sha(c) for c in self.dag_loader.nodes.keys())

        # 统计信息
        total_commits = 0
        valid_commits = 0

        for milestone_id, milestone_data in self.milestones.items():
            for commit_sha in milestone_data["all_commits"]:
                total_commits += 1
                normalized_sha = normalize_commit_sha(commit_sha)

                # 只添加在 DAG 中存在的 commits
                if normalized_sha not in dag_commits:
                    continue

                commit_to_milestone[normalized_sha] = milestone_id
                valid_commits += 1

        print(f"Built commit-to-milestone map with {len(commit_to_milestone)} commits", file=sys.stderr)

        # 如果有跳过的 commits，显示统计信息
        if total_commits > valid_commits:
            skipped = total_commits - valid_commits
            print(f"  (Skipped {skipped} invalid commits not in DAG)", file=sys.stderr)

        return commit_to_milestone

    def _build_milestone_commits_map(self) -> Dict[str, Set[str]]:
        """
        建立 milestone -> commits 的映射

        Returns:
            milestone_commits[milestone_id] = {commit_shas}
        """
        milestone_commits = {}
        for milestone_id, milestone_data in self.milestones.items():
            commits = set()
            for commit_sha in milestone_data["all_commits"]:
                normalized_sha = normalize_commit_sha(commit_sha)
                commits.add(normalized_sha)
            milestone_commits[milestone_id] = commits

        return milestone_commits

    def _identify_invalid_commits(self) -> Dict[str, List[str]]:
        """
        识别在 milestone 中但不在 commit DAG 中的 commits

        Returns:
            {milestone_id: [invalid_commit_shas]}
        """
        dag_commits = set(normalize_commit_sha(c) for c in self.dag_loader.nodes.keys())
        invalid_commits = {}

        for milestone_id, milestone_data in self.milestones.items():
            invalid_list = []
            for commit_sha in milestone_data["all_commits"]:
                normalized_sha = normalize_commit_sha(commit_sha)
                if normalized_sha not in dag_commits:
                    invalid_list.append(commit_sha)

            if invalid_list:
                invalid_commits[milestone_id] = invalid_list

        total_invalid = sum(len(commits) for commits in invalid_commits.values())
        if total_invalid > 0:
            print(f"Found {total_invalid} invalid commits across {len(invalid_commits)} milestones", file=sys.stderr)

        return invalid_commits

    def _identify_commits_not_in_milestones(self) -> List[str]:
        """
        识别在 commit DAG 中但不在任何 milestone 中的 commits

        Returns:
            [commit_shas]
        """
        dag_commits = set(normalize_commit_sha(c) for c in self.dag_loader.nodes.keys())
        milestone_commits = set(self.commit_to_milestone.keys())
        commits_not_in_milestones = sorted(dag_commits - milestone_commits)

        if commits_not_in_milestones:
            print(f"Found {len(commits_not_in_milestones)} commits in DAG but not in any milestone", file=sys.stderr)

        return commits_not_in_milestones

    # ============================================================
    # Level 1: Commit 级别依赖查询
    # ============================================================

    def get_all_dependencies(self, commit: str) -> Set[str]:
        """
        获取一个 commit 的所有传递依赖

        使用预计算的传递闭包，查询复杂度为 O(1)。

        Args:
            commit: commit SHA (支持短 SHA 或完整 SHA)

        Returns:
            所有依赖的 commits 集合（包括直接和传递依赖）

        Example:
            >>> deps = analyzer.get_all_dependencies("abc123")
            >>> print(f"Found {len(deps)} dependencies")
        """
        return self.transitive_closure.get(commit, set()).copy()

    def check_dependency_between_commits(self, commits_a: List[str], commits_b: List[str]) -> Tuple[bool, bool, Dict]:
        """
        检查两组 commits 之间的依赖关系

        Args:
            commits_a: 第一组 commits
            commits_b: 第二组 commits

        Returns:
            (a_depends_on_b, b_depends_on_a, details)
            - a_depends_on_b: A 是否依赖 B
            - b_depends_on_a: B 是否依赖 A
            - details: 详细信息，包括具体的依赖边
        """
        # 标准化 commits
        normalized_a = [self.dag_loader._normalize_sha(c) for c in commits_a]
        normalized_b = [self.dag_loader._normalize_sha(c) for c in commits_b]

        # 过滤掉未找到的 commits
        valid_commits_a = {c for c in normalized_a if c}
        valid_commits_b = {c for c in normalized_b if c}

        # 检查 A 是否依赖 B
        a_depends_on_b = False
        a_to_b_deps = []

        for commit_a in valid_commits_a:
            all_deps = self.get_all_dependencies(commit_a)
            deps_in_b = all_deps.intersection(valid_commits_b)
            if deps_in_b:
                a_depends_on_b = True
                for dep in deps_in_b:
                    a_to_b_deps.append({"from": commit_a[:7], "to": dep[:7], "from_full": commit_a, "to_full": dep})

        # 检查 B 是否依赖 A
        b_depends_on_a = False
        b_to_a_deps = []

        for commit_b in valid_commits_b:
            all_deps = self.get_all_dependencies(commit_b)
            deps_in_a = all_deps.intersection(valid_commits_a)
            if deps_in_a:
                b_depends_on_a = True
                for dep in deps_in_a:
                    b_to_a_deps.append({"from": commit_b[:7], "to": dep[:7], "from_full": commit_b, "to_full": dep})

        details = {
            "commits_a_count": len(valid_commits_a),
            "commits_b_count": len(valid_commits_b),
            "a_to_b_dependencies": a_to_b_deps,
            "b_to_a_dependencies": b_to_a_deps,
            "a_to_b_count": len(a_to_b_deps),
            "b_to_a_count": len(b_to_a_deps),
        }

        return a_depends_on_b, b_depends_on_a, details

    # ============================================================
    # Level 2: Milestone 点对点检查
    # ============================================================

    def check_milestone_dependency(self, milestone_a_id: str, milestone_b_id: str) -> Dict:
        """
        检查两个 milestone 之间的依赖关系

        Args:
            milestone_a_id: milestone A 的 ID
            milestone_b_id: milestone B 的 ID

        Returns:
            依赖关系分析结果字典
        """
        if milestone_a_id not in self.milestones:
            return {"error": f"Milestone {milestone_a_id} not found", "dependency_type": "unknown"}

        if milestone_b_id not in self.milestones:
            return {"error": f"Milestone {milestone_b_id} not found", "dependency_type": "unknown"}

        milestone_a = self.milestones[milestone_a_id]
        milestone_b = self.milestones[milestone_b_id]

        commits_a = milestone_a["all_commits"]
        commits_b = milestone_b["all_commits"]

        a_depends_on_b, b_depends_on_a, details = self.check_dependency_between_commits(commits_a, commits_b)

        # 确定依赖类型
        if a_depends_on_b and b_depends_on_a:
            dependency_type = "bidirectional"
            direction = f"{milestone_a_id} ↔ {milestone_b_id}"
            description = f"双向依赖：{milestone_a_id} 和 {milestone_b_id} 互相依赖"
        elif a_depends_on_b:
            dependency_type = "unidirectional"
            direction = f"{milestone_b_id} → {milestone_a_id}"
            description = f"单向依赖：{milestone_a_id} 依赖 {milestone_b_id}"
        elif b_depends_on_a:
            dependency_type = "unidirectional"
            direction = f"{milestone_a_id} → {milestone_b_id}"
            description = f"单向依赖：{milestone_b_id} 依赖 {milestone_a_id}"
        else:
            dependency_type = "none"
            direction = f"{milestone_a_id} ∥ {milestone_b_id}"
            description = f"无依赖：{milestone_a_id} 和 {milestone_b_id} 可以并行"

        return {
            "milestone_a": {
                "id": milestone_a_id,
                "title": milestone_a["title"],
                "category": milestone_a["category"],
                "commits_count": len(commits_a),
            },
            "milestone_b": {
                "id": milestone_b_id,
                "title": milestone_b["title"],
                "category": milestone_b["category"],
                "commits_count": len(commits_b),
            },
            "dependency_type": dependency_type,
            "direction": direction,
            "description": description,
            "a_depends_on_b": a_depends_on_b,
            "b_depends_on_a": b_depends_on_a,
            "details": details,
        }

    def check_all_milestone_pairs(self) -> List[Dict]:
        """
        检查所有 milestone 对之间的依赖关系

        这是 check_milestone_list_dependencies 的便捷包装，检查所有 milestones。

        Returns:
            所有 milestone 对的依赖关系分析结果列表
        """
        return self.check_milestone_list_dependencies(list(self.milestones.keys()))

    def check_milestone_list_dependencies(self, milestone_ids: List[str]) -> List[Dict]:
        """
        检查指定 milestone 列表中所有两两之间的依赖关系

        Args:
            milestone_ids: 要检查的 milestone ID 列表

        Returns:
            所有 milestone 对的依赖关系分析结果列表
        """
        results = []

        # 验证所有 milestone 是否存在
        missing_milestones = [m for m in milestone_ids if m not in self.milestones]
        if missing_milestones:
            print(f"⚠️  警告：以下 milestone 不存在: {', '.join(missing_milestones)}", file=sys.stderr)

        # 过滤出存在的 milestones
        valid_milestone_ids = [m for m in milestone_ids if m in self.milestones]

        if len(valid_milestone_ids) < 2:
            print("❌ 错误：至少需要 2 个有效的 milestone", file=sys.stderr)
            return results

        # 检查所有两两组合
        for i, milestone_a_id in enumerate(valid_milestone_ids):
            for milestone_b_id in valid_milestone_ids[i + 1 :]:
                result = self.check_milestone_dependency(milestone_a_id, milestone_b_id)
                results.append(result)

        return results

    # ============================================================
    # Level 3: 批量依赖推断
    # ============================================================

    def infer_dependencies_from_commits(self, do_transitive_reduction: bool = True) -> Dict[str, Set[str]]:
        """
        从 commit DAG 推断所有 milestone 之间的依赖关系

        核心逻辑：
        1. 使用预计算的传递闭包（self.transitive_closure）← 关键优化
        2. 遍历每个 milestone 的 commits，查找其依赖
        3. 根据依赖 commits 所属的 milestone 推断 milestone 依赖
        4. 可选：对结果进传行递约简

        性能优化：
        - ✅ 使用预计算的传递闭包，避免重复计算
        - ✅ commit 依赖查询从 O(V) BFS → O(1) 字典查询

        Args:
            do_transitive_reduction: 是否对结果做传递约简（默认 True）

        Returns:
            依赖关系图 {source_milestone: {target_milestones}}
        """
        print("\n" + "=" * 60, file=sys.stderr)
        print("INFERRING MILESTONE DEPENDENCIES FROM COMMIT DAG", file=sys.stderr)
        print("=" * 60, file=sys.stderr)

        print(f"Using pre-computed transitive closure: {len(self.transitive_closure)} commits", file=sys.stderr)

        # 统计信息
        milestone_commits = set(self.commit_to_milestone.keys())
        total_commits = len(self.dag_loader.nodes)

        print(f"Milestone commits: {len(milestone_commits)}", file=sys.stderr)
        print(f"Total commits in DAG: {total_commits}", file=sys.stderr)
        if total_commits > 0:
            coverage = len(milestone_commits) / total_commits * 100
            print(f"Coverage: {coverage:.1f}%", file=sys.stderr)

        # 推断 milestone 依赖（包含所有传递依赖）
        inferred_deps_full = defaultdict(set)

        # 统计信息
        total_commit_pairs = 0
        cross_milestone_pairs = 0
        same_milestone_pairs = 0
        unknown_commit_pairs = 0

        # 遍历每个 milestone 中的每个 commit
        for src_milestone_id, milestone_data in self.milestones.items():
            for src_commit in milestone_data["all_commits"]:
                src_commit_norm = normalize_commit_sha(src_commit)

                # ✅ 关键优化：直接从预计算的传递闭包查询（O(1)）
                dst_commits = self.transitive_closure.get(src_commit_norm, set())

                for dst_commit_norm in dst_commits:
                    total_commit_pairs += 1

                    # 查找目标 commit 所属的 milestone
                    dst_milestone_id = self.commit_to_milestone.get(dst_commit_norm)

                    if not dst_milestone_id:
                        unknown_commit_pairs += 1
                        continue

                    if src_milestone_id == dst_milestone_id:
                        same_milestone_pairs += 1
                        continue

                    # 跨 milestone 的依赖
                    inferred_deps_full[src_milestone_id].add(dst_milestone_id)
                    cross_milestone_pairs += 1

        print("\nDependency Statistics:", file=sys.stderr)
        print(f"  Total commit pairs (with transitive): {total_commit_pairs}", file=sys.stderr)
        print(f"  Same-milestone pairs: {same_milestone_pairs}", file=sys.stderr)
        print(f"  Cross-milestone pairs: {cross_milestone_pairs}", file=sys.stderr)
        print(f"  Unknown commit pairs: {unknown_commit_pairs}", file=sys.stderr)
        print(
            f"  Inferred milestone dependencies: {sum(len(deps) for deps in inferred_deps_full.values())} (before reduction)",
            file=sys.stderr,
        )

        # 检测推断依赖中的循环
        all_nodes = set(self.milestones.keys())
        self.inferred_cycles = _detect_cycles_from_graph(dict(inferred_deps_full), all_nodes)

        # 传递约简（如果需要）
        if do_transitive_reduction:
            if self.inferred_cycles:
                print("\n⚠️  CRITICAL: Skipping transitive reduction due to cycles", file=sys.stderr)
                print("  Transitive reduction is only valid for DAGs", file=sys.stderr)
                print(f"  Found {len(self.inferred_cycles)} cycle(s)", file=sys.stderr)
                return dict(inferred_deps_full)
            else:
                print("\nApplying transitive reduction...", file=sys.stderr)
                inferred_deps_reduced = transitive_reduction(dict(inferred_deps_full), all_nodes)

                reduced_count = sum(len(deps) for deps in inferred_deps_reduced.values())
                original_count = sum(len(deps) for deps in inferred_deps_full.values())
                print(f"  After reduction: {reduced_count} dependencies", file=sys.stderr)
                print(f"  Removed {original_count - reduced_count} redundant edges", file=sys.stderr)

                return inferred_deps_reduced

        return dict(inferred_deps_full)

    # ============================================================
    # Level 4: 依赖丰富与比较
    # ============================================================

    def enrich_dependencies(self) -> Dict:
        """
        比较推断依赖与现有依赖，发现缺失/冗余/一致的依赖

        需要在初始化时提供 dependencies_csv_path。

        Returns:
            包含详细比较结果的字典

        Raises:
            ValueError: 如果未提供现有依赖文件
        """
        if self.existing_dependencies is None:
            raise ValueError(
                "Cannot enrich dependencies without existing dependencies. "
                "Please provide dependencies_csv_path when initializing."
            )

        print("\n" + "=" * 80, file=sys.stderr)
        print("MILESTONE DEPENDENCY ENRICHMENT", file=sys.stderr)
        print("=" * 80, file=sys.stderr)

        # 1. 推断依赖
        inferred_dependencies = self.infer_dependencies_from_commits(do_transitive_reduction=True)

        # 2. 比较现有依赖和推断依赖
        print("\n" + "=" * 60, file=sys.stderr)
        print("COMPARING EXISTING VS INFERRED DEPENDENCIES", file=sys.stderr)
        print("=" * 60, file=sys.stderr)

        all_existing = set()
        for source, targets in self.existing_dependencies.items():
            for target in targets:
                all_existing.add((source, target))

        all_inferred = set()
        for source, targets in inferred_dependencies.items():
            for target in targets:
                all_inferred.add((source, target))

        # 分类依赖关系
        confirmed = all_existing & all_inferred
        missing = all_inferred - all_existing
        extra = all_existing - all_inferred

        print(f"Existing dependencies: {len(all_existing)}", file=sys.stderr)
        print(f"Inferred dependencies: {len(all_inferred)}", file=sys.stderr)
        print(f"✓ Confirmed (in both): {len(confirmed)}", file=sys.stderr)
        print(f"+ Missing (should add): {len(missing)}", file=sys.stderr)
        print(f"? Extra (need review): {len(extra)}", file=sys.stderr)

        # 3. 生成丰富后的依赖
        enriched_dependencies = defaultdict(set)

        # 添加所有推断出的依赖
        for source, targets in inferred_dependencies.items():
            enriched_dependencies[source].update(targets)

        # 保留现有的依赖
        for source, targets in self.existing_dependencies.items():
            enriched_dependencies[source].update(targets)

        # 检测合并后依赖中的循环
        all_nodes = set(self.milestones.keys())
        self.enriched_cycles = _detect_cycles_from_graph(dict(enriched_dependencies), all_nodes)

        # 4. 识别冗余的传递依赖（可选）
        redundant_deps = []
        if self.detect_redundant:
            print("\n" + "=" * 60, file=sys.stderr)
            print("DETECTING REDUNDANT TRANSITIVE DEPENDENCIES", file=sys.stderr)
            print("=" * 60, file=sys.stderr)

            # 检查是否有环路 - 传递约简算法要求输入必须是 DAG
            if self.enriched_cycles:
                print("⚠️  WARNING: Graph contains cycles - skipping transitive reduction", file=sys.stderr)
                print("   Transitive reduction requires a DAG (acyclic graph)", file=sys.stderr)
                print(f"   Found {len(self.enriched_cycles)} cycle(s) in the dependency graph", file=sys.stderr)
            else:
                # 使用传递约简识别冗余边（仅在无环时执行）
                _, redundant_deps = transitive_reduction(
                    dict(enriched_dependencies), all_nodes, return_removed_edges=True
                )

                if redundant_deps:
                    print(f"Found {len(redundant_deps)} redundant dependencies", file=sys.stderr)
                    for source, intermediate, target in redundant_deps[:5]:
                        print(f"  {source} -> {target} (via {intermediate})", file=sys.stderr)
                    if len(redundant_deps) > 5:
                        print(f"  ... and {len(redundant_deps) - 5} more", file=sys.stderr)
                else:
                    print("No redundant dependencies found", file=sys.stderr)

        # 5. 统计总结
        print("\n" + "=" * 60, file=sys.stderr)
        print("SUMMARY", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print(f"Total milestones: {len(self.milestones)}", file=sys.stderr)
        print(f"Enriched dependencies: {sum(len(deps) for deps in enriched_dependencies.values())}", file=sys.stderr)
        print(f"Dependencies added: {len(missing)}", file=sys.stderr)
        print(f"Existing dependencies: {len(all_existing)}", file=sys.stderr)
        print(f"Dependencies needing review: {len(extra)}", file=sys.stderr)
        print("\nCycle Detection:", file=sys.stderr)
        print(f"  Existing: {len(self.existing_cycles)} cycle(s)", file=sys.stderr)
        print(f"  Inferred: {len(self.inferred_cycles)} cycle(s)", file=sys.stderr)
        print(f"  Enriched: {len(self.enriched_cycles)} cycle(s)", file=sys.stderr)
        if self.enriched_cycles:
            print("⚠️  WARNING: Dependency graph contains cycles!", file=sys.stderr)

        # 转换 redundant_deps 格式：从 (source, intermediate, target) 到 [source, target]
        # 只报告在 existing 中的冗余边（inferred 中的冗余边不需要报告，因为我们本来就不会添加它们）
        all_redundant_edges_set = {(source, target) for source, _, target in redundant_deps}
        redundant_edges = [[source, target] for source, _, target in redundant_deps if (source, target) in all_existing]

        # 从 extra 和 missing 中排除所有 redundant edges（包括 inferred 中的）
        # - 冗余边不应该算作 extra（已存在但可传递到达）
        # - 冗余边也不应该算作 missing（需要添加但可传递到达，所以不需要显式添加）
        extra = extra - all_redundant_edges_set
        missing = missing - all_redundant_edges_set

        return {
            "enriched_dependencies": dict(enriched_dependencies),
            "existing_dependencies": dict(self.existing_dependencies),
            "inferred_dependencies": dict(inferred_dependencies),
            "missing_dependencies": list(missing),
            "extra_dependencies": list(extra),
            "redundant_dependencies": redundant_edges,
            "cycles": {
                "existing_cycles": self.existing_cycles,
                "inferred_cycles": self.inferred_cycles,
                "enriched_cycles": self.enriched_cycles,
            },
            "summary": {
                "total_milestones": len(self.milestones),
                "original_dependencies_count": len(all_existing),
                "inferred_dependencies_count": len(all_inferred),
                "missing_dependencies_count": len(missing),
                "extra_dependencies_count": len(extra),
                "all_dependencies_count": sum(len(deps) for deps in enriched_dependencies.values()),
                "existing_cycles_count": len(self.existing_cycles),
                "inferred_cycles_count": len(self.inferred_cycles),
                "all_cycles_count": len(self.enriched_cycles),
                "is_valid_dag": len(self.enriched_cycles) == 0,
            },
        }

    # ============================================================
    # Level 5: 可选依赖分析
    # ============================================================

    def _filter_transitive_optional_edges(
        self, inferred_edges: Set[Tuple[str, str]], optional_edges: Set[Tuple[str, str]]
    ) -> Tuple[Set[Tuple[str, str]], Set[Tuple[str, str]]]:
        """
        从 optional_edges 中过滤掉可以通过 inferred_edges 传递到达的边。

        如果存在路径 A -> ... -> B (通过 inferred_edges，至少 2 跳)，
        则 optional_edges 中的 A -> B 是传递边，应该被移除。

        Args:
            inferred_edges: 推断出的强制依赖边集合
            optional_edges: 待过滤的可选依赖边集合

        Returns:
            (non_transitive_edges, transitive_edges) 元组
        """
        from collections import deque

        # 构建 inferred 依赖图
        inferred_graph = defaultdict(set)
        for source, target in inferred_edges:
            inferred_graph[source].add(target)

        # 检查从 start 到 end 是否存在至少 min_length 条边的路径
        def can_reach_via_path(start: str, end: str, min_length: int = 2) -> bool:
            if start == end:
                return False

            # BFS with path length tracking
            queue = deque([(start, 0)])  # (current_node, path_length)
            visited = {start: 0}  # node -> minimum path length seen

            while queue:
                current, path_length = queue.popleft()

                # 探索邻居
                for neighbor in inferred_graph.get(current, set()):
                    new_path_length = path_length + 1

                    # 如果到达目标且路径长度足够
                    if neighbor == end and new_path_length >= min_length:
                        return True

                    # 继续探索（避免重复访问更长路径）
                    if neighbor not in visited or visited[neighbor] > new_path_length:
                        visited[neighbor] = new_path_length
                        queue.append((neighbor, new_path_length))

            return False

        # 分离传递边和非传递边
        non_transitive = set()
        transitive = set()

        for source, target in optional_edges:
            if can_reach_via_path(source, target, min_length=2):
                transitive.add((source, target))
            else:
                non_transitive.add((source, target))

        return non_transitive, transitive

    def compute_optional_dependencies(self, filter_transitive: bool = True) -> Dict:
        """
        计算所有可选的、非冗余的 milestone 依赖关系

        返回所有满足时间约束但非强制、非冗余的依赖边。
        这些边可以被添加到依赖图中而不违反时间顺序或引入冗余。

        时间约束定义：
            如果存在依赖 A -> B，则必须满足：
            B 的最大 layer >= A 的最小 layer
            （即 B 不能完全发生在 A 之前）

        计算逻辑：
            1. 找出所有满足时间约束的 milestone 对（时间可行集）
            2. 排除从 commit DAG 推断出的依赖（强制依赖）
            3. 排除 inferred 依赖的逆向路径（防止循环）
            4. 排除冗余的传递依赖（可通过其他路径到达）
            5. 剩余的边即为可选依赖（可安全添加）
            6. [可选] 过滤掉可通过 inferred 传递到达的边

        Args:
            filter_transitive: 是否过滤掉可以通过 inferred edges 传递到达的可选边（默认 True）

        Returns:
            {
                "summary": 统计摘要,
                "inferred_dependencies": 从 commit DAG 推断的边（强制的）,
                "redundant_dependencies": 冗余的传递边,
                "optional_dependencies": 可选的依赖边（已过滤传递边）,
                "transitive_optional_dependencies": 被过滤的传递边（仅当 filter_transitive=True）
            }

        Example:
            >>> result = analyzer.compute_optional_dependencies()
            >>> print(f"Found {result['summary']['optional_count']} optional dependencies")
        """
        print("\n" + "=" * 80, file=sys.stderr)
        print("COMPUTING OPTIONAL MILESTONE DEPENDENCIES", file=sys.stderr)
        print("=" * 80, file=sys.stderr)

        # 1. 获取每个 milestone 的时间范围（使用 DAG 层级作为代理）
        print("\n📅 Computing milestone time ranges...", file=sys.stderr)
        milestone_time_ranges = {}

        for milestone_id, milestone_data in self.milestones.items():
            commits = milestone_data["all_commits"]
            layers = []
            commit_layers = {}

            for commit_sha in commits:
                normalized_sha = normalize_commit_sha(commit_sha)
                node_data = self.dag_loader.nodes.get(normalized_sha)
                if node_data and "layer" in node_data:
                    layer = node_data["layer"]
                    layers.append(layer)
                    commit_layers[normalized_sha] = layer

            if layers:
                min_layer = min(layers)
                max_layer = max(layers)
                min_commits = [sha for sha, layer in commit_layers.items() if layer == min_layer]
                max_commits = [sha for sha, layer in commit_layers.items() if layer == max_layer]

                milestone_time_ranges[milestone_id] = {
                    "min_layer": min_layer,
                    "max_layer": max_layer,
                    "layer_span": max_layer - min_layer,
                    "commit_count": len(layers),
                    "min_commits": min_commits[:3],  # 只保存前3个
                    "max_commits": max_commits[:3],  # 只保存前3个
                }

        print(f"✓ Computed layer ranges for {len(milestone_time_ranges)} milestones", file=sys.stderr)
        print("  Note: Using DAG 'layer' field as proxy for temporal order", file=sys.stderr)

        # 2. 找出所有满足时间约束的 milestone 对
        print("\n🔍 Finding time-feasible milestone pairs...", file=sys.stderr)
        time_feasible = []

        for milestone_a in self.milestones.keys():
            for milestone_b in self.milestones.keys():
                if milestone_a == milestone_b:
                    continue

                # 检查时间约束：A -> B 意味着 B 的最大 layer >= A 的最小 layer
                if milestone_a in milestone_time_ranges and milestone_b in milestone_time_ranges:
                    a_min_layer = milestone_time_ranges[milestone_a]["min_layer"]
                    b_max_layer = milestone_time_ranges[milestone_b]["max_layer"]

                    # B 的最大 layer >= A 的最小 layer
                    # 这意味着 B 不能完全发生在 A 之前（拓扑顺序）
                    if b_max_layer >= a_min_layer:
                        time_feasible.append((milestone_a, milestone_b))

        time_feasible_set = set(time_feasible)
        print(f"✓ Found {len(time_feasible_set)} time-feasible pairs", file=sys.stderr)

        # 3. 获取 inferred dependencies（从 commit DAG 推断的强制依赖）
        print("\n🔗 Computing inferred dependencies from commit DAG...", file=sys.stderr)
        inferred_deps = self.infer_dependencies_from_commits(do_transitive_reduction=True)
        inferred_edges = set()
        for source, targets in inferred_deps.items():
            for target in targets:
                inferred_edges.add((source, target))

        print(f"✓ Found {len(inferred_edges)} inferred dependencies", file=sys.stderr)

        # 3.5. 计算 inferred dependencies 的传递闭包，用于识别逆向边
        print("\n🔄 Computing transitive closure of inferred dependencies...", file=sys.stderr)
        inferred_closure = compute_transitive_closure(inferred_deps, set(self.milestones.keys()))

        # 构建逆向边集合：如果 inferred 中存在路径 B -> ... -> A，则 (A, B) 是逆向边
        reverse_edges = set()
        for source in self.milestones.keys():
            for target in self.milestones.keys():
                if source == target:
                    continue
                # 如果 target 可以到达 source (在 inferred 的传递闭包中)
                # 那么 source -> target 是逆向边
                if source in inferred_closure.get(target, set()):
                    reverse_edges.add((source, target))

        print(f"✓ Found {len(reverse_edges)} reverse edges to exclude", file=sys.stderr)

        # 4. 计算 redundant edges（基于当前的依赖图）
        print("\n♻️  Computing redundant dependencies...", file=sys.stderr)
        redundant_edges = set()

        if self.existing_dependencies:
            # 合并 inferred 和 existing 依赖
            enriched_dependencies = defaultdict(set)
            for source, targets in inferred_deps.items():
                enriched_dependencies[source].update(targets)
            for source, targets in self.existing_dependencies.items():
                enriched_dependencies[source].update(targets)

            all_nodes = set(self.milestones.keys())

            # 使用传递约简识别冗余边
            _, redundant_tuples = transitive_reduction(
                dict(enriched_dependencies), all_nodes, return_removed_edges=True
            )
            redundant_edges = set((source, target) for source, _, target in redundant_tuples)
            print(f"✓ Found {len(redundant_edges)} redundant dependencies", file=sys.stderr)
        else:
            print("⚠️  No existing dependencies provided, skipping redundancy check", file=sys.stderr)

        # 5. 计算可选依赖：time_feasible - inferred - reverse - redundant
        print("\n🎯 Computing optional dependencies...", file=sys.stderr)
        optional_edges = time_feasible_set - inferred_edges - reverse_edges - redundant_edges
        print(f"✓ Found {len(optional_edges)} optional dependencies (before transitive filtering)", file=sys.stderr)

        # 6. 过滤传递边（可选）
        transitive_optional_edges = set()
        if filter_transitive:
            print("\n🔄 Filtering transitive optional edges...", file=sys.stderr)
            optional_edges, transitive_optional_edges = self._filter_transitive_optional_edges(
                inferred_edges, optional_edges
            )
            print(f"✓ Filtered out {len(transitive_optional_edges)} transitive edges", file=sys.stderr)
            print(f"✓ Remaining optional dependencies: {len(optional_edges)}", file=sys.stderr)

        # 7. 统计和输出
        print("\n" + "=" * 60, file=sys.stderr)
        print("SUMMARY", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print(f"Time-feasible pairs:            {len(time_feasible_set)}", file=sys.stderr)
        print(f"  - Inferred (forced):          {len(inferred_edges)}", file=sys.stderr)
        print(f"  - Reverse (conflicts):        {len(reverse_edges)}", file=sys.stderr)
        print(f"  - Redundant (transitive):     {len(redundant_edges)}", file=sys.stderr)
        if filter_transitive:
            print(f"  - Transitive optional:        {len(transitive_optional_edges)}", file=sys.stderr)
        print(f"  - Optional (final):           {len(optional_edges)}", file=sys.stderr)

        # 显示一些示例
        if optional_edges:
            print("\nExample optional dependencies:", file=sys.stderr)
            for source, target in sorted(optional_edges)[:5]:
                print(f"  {source} -> {target}", file=sys.stderr)
            if len(optional_edges) > 5:
                print(f"  ... and {len(optional_edges) - 5} more", file=sys.stderr)

        # 构建返回结果，summary 放在最前面
        result = {
            "summary": {
                "total_milestones": len(self.milestones),
                "time_feasible_count": len(time_feasible_set),
                "inferred_count": len(inferred_edges),
                "reverse_count": len(reverse_edges),
                "redundant_count": len(redundant_edges),
                "optional_count": len(optional_edges),
            },
            "inferred_dependencies": sorted(inferred_edges),
            "redundant_dependencies": sorted(redundant_edges),
            "optional_dependencies": sorted(optional_edges),
        }

        # 如果过滤了传递边，添加到结果中
        if filter_transitive:
            result["summary"]["transitive_optional_count"] = len(transitive_optional_edges)
            result["transitive_optional_dependencies"] = sorted(transitive_optional_edges)

        return result

    def get_inferred_and_optional_dependencies(
        self, filter_transitive: bool = True
    ) -> Tuple[List[List[str]], List[List[str]]]:
        """
        便捷方法：直接获取 inferred 和 optional 依赖的数组形式。

        不需要提供 dependencies.csv，只基于 commit DAG 和 milestones。

        Args:
            filter_transitive: 是否过滤掉可以通过 inferred edges 传递到达的可选边（默认 True）

        Returns:
            (inferred_dependencies_list, optional_dependencies_list)
            - inferred_dependencies_list: [[source, target], ...] 推断依赖列表
            - optional_dependencies_list: [[source, target], ...] 可选依赖列表（已过滤传递边）

        Example:
            >>> analyzer = MilestoneDependencyAnalyzer(
            ...     commit_dag_path="commit_dag.json",
            ...     milestone_csv_path="milestones.csv"
            ... )
            >>> inferred, optional = analyzer.get_inferred_and_optional_dependencies()
            >>> print(f"Inferred: {len(inferred)}, Optional: {len(optional)}")
        """
        result = self.compute_optional_dependencies(filter_transitive=filter_transitive)

        # 转换为列表格式
        inferred_list = [[src, tgt] for src, tgt in result["inferred_dependencies"]]
        optional_list = [[src, tgt] for src, tgt in result["optional_dependencies"]]

        return inferred_list, optional_list

    # ============================================================
    # 辅助方法：打印和导出
    # ============================================================

    def print_dependency_result(self, result: Dict) -> None:
        """打印依赖关系分析结果"""
        print("=" * 80, file=sys.stderr)
        print(f"Milestone A: {result['milestone_a']['id']} - {result['milestone_a']['title']}", file=sys.stderr)
        print(f"  Category: {result['milestone_a']['category']}", file=sys.stderr)
        print(f"  Commits: {result['milestone_a']['commits_count']}", file=sys.stderr)
        print(file=sys.stderr)
        print(f"Milestone B: {result['milestone_b']['id']} - {result['milestone_b']['title']}", file=sys.stderr)
        print(f"  Category: {result['milestone_b']['category']}", file=sys.stderr)
        print(f"  Commits: {result['milestone_b']['commits_count']}", file=sys.stderr)
        print(file=sys.stderr)
        print(f"Dependency Type: {result['dependency_type'].upper()}", file=sys.stderr)
        print(f"Direction: {result['direction']}", file=sys.stderr)
        print(f"Description: {result['description']}", file=sys.stderr)
        print(file=sys.stderr)

        details = result.get("details", {})
        if details.get("a_to_b_count", 0) > 0:
            print(f"A → B Dependencies: {details['a_to_b_count']}", file=sys.stderr)
            for dep in details["a_to_b_dependencies"][:5]:
                print(f"  {dep['from']} → {dep['to']}", file=sys.stderr)
            if details["a_to_b_count"] > 5:
                print(f"  ... and {details['a_to_b_count'] - 5} more", file=sys.stderr)
            print(file=sys.stderr)

        if details.get("b_to_a_count", 0) > 0:
            print(f"B → A Dependencies: {details['b_to_a_count']}", file=sys.stderr)
            for dep in details["b_to_a_dependencies"][:5]:
                print(f"  {dep['from']} → {dep['to']}", file=sys.stderr)
            if details["b_to_a_count"] > 5:
                print(f"  ... and {details['b_to_a_count'] - 5} more", file=sys.stderr)
            print(file=sys.stderr)

    def export_enriched_dependencies(self, output_path: str, validation_results: Dict):
        """
        导出丰富后的依赖关系到 CSV 文件

        Args:
            output_path: 输出文件路径
            validation_results: 验证结果（来自 enrich_dependencies）
        """
        enriched_deps = validation_results["enriched_dependencies"]
        save_dependency_graph(enriched_deps, output_path, sort=True)

    def export_filtered_milestones(self, output_path: str):
        """
        导出过滤后的 milestones CSV（移除 invalid commits）

        Args:
            output_path: 输出文件路径
        """
        import csv

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        # 获取 DAG 中的有效 commits
        dag_commits = set(normalize_commit_sha(c) for c in self.dag_loader.nodes.keys())

        # 读取原始 CSV 的字段名
        with open(self.milestone_csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames

        # 写入过滤后的数据
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for _milestone_id, milestone_data in self.milestones.items():
                # 过滤 commits
                original_commits = milestone_data["all_commits"]
                valid_commits = [commit for commit in original_commits if normalize_commit_sha(commit) in dag_commits]

                # 构建输出行
                row = dict(milestone_data)
                row["commits"] = ";".join(valid_commits)

                # 移除内部字段
                if "all_commits" in row:
                    del row["all_commits"]

                writer.writerow(row)

        # 统计信息
        total_original = sum(len(m["all_commits"]) for m in self.milestones.values())
        total_valid = sum(
            sum(1 for c in m["all_commits"] if normalize_commit_sha(c) in dag_commits) for m in self.milestones.values()
        )
        removed = total_original - total_valid

        print(f"✓ Filtered milestones exported to: {output_path}", file=sys.stderr)
        print(f"  Total commits: {total_original} → {total_valid} (removed {removed})", file=sys.stderr)

    def export_detailed_analysis_json(self, output_path: str, validation_results: Dict):
        """
        导出详细的 JSON 分析结果

        Args:
            output_path: 输出文件路径
            validation_results: 验证结果
        """
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        # Ensure metadata is first, summary is second by building dict in order
        output_data = {}
        output_data["metadata"] = {
            "milestone_csv": str(self.milestone_csv_path),
            "dependencies_csv": str(self.dependencies_csv_path) if self.dependencies_csv_path else None,
            "commit_dag_json": str(self.commit_dag_path),
        }
        output_data["summary"] = validation_results["summary"]

        output_data["cycles"] = validation_results.get("cycles", {})
        output_data["commit_coverage"] = {
            "total_commits_in_dag": len(self.dag_loader.nodes),
            "total_commits_in_milestones": sum(len(m["all_commits"]) for m in self.milestones.values()),
            "valid_commits_in_milestones": len(self.commit_to_milestone),
            "invalid_commits_count": sum(len(c) for c in self.invalid_commits.values()),
            "commits_not_in_milestones_count": len(self.commits_not_in_milestones),
            "invalid_commits_by_milestone": self.invalid_commits,
            "commits_not_in_milestones": self.commits_not_in_milestones,
        }

        # Helper function to convert adjacency list to edge list
        def adjacency_to_edge_list(adj_dict):
            """Convert adjacency list dict to edge list format"""
            edges = []
            for source, targets in adj_dict.items():
                for target in targets:
                    edges.append([source, target])
            return edges

        # All dependency analysis in one section (all in edge list format)
        output_data["analysis"] = {
            "original_dependencies": adjacency_to_edge_list(validation_results["existing_dependencies"]),
            "inferred_dependencies": adjacency_to_edge_list(validation_results["inferred_dependencies"]),
            "missing_dependencies": validation_results["missing_dependencies"],
            "extra_dependencies": validation_results["extra_dependencies"],
            "redundant_dependencies": validation_results["redundant_dependencies"],
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)

        print(f"✓ Detailed JSON analysis exported to: {output_path}", file=sys.stderr)


# ============================================================
# CLI 入口
# ============================================================


def main():
    parser = argparse.ArgumentParser(
        description="Milestone Dependency Analyzer - Based on Commit DAG",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
This tool analyzes milestone dependencies based on commit-level dependencies.

Core Logic:
  If commit A (in milestone M1) depends on commit B (in milestone M2),
  then M1 should depend on M2.

Usage Examples:
  # 1. Check specific milestone pair
  python -m deepcommit.tools.conflict_analysis.milestone_dependency_analyzer \\
    --commit-dag commit_dag.json --milestones milestones.csv \\
    --check M1 M2

  # 2. Infer all dependencies
  python -m deepcommit.tools.conflict_analysis.milestone_dependency_analyzer \\
    --commit-dag commit_dag.json --milestones milestones.csv \\
    --infer

  # 3. Enrich existing dependencies
  python -m deepcommit.tools.conflict_analysis.milestone_dependency_analyzer \\
    --commit-dag commit_dag.json --milestones milestones.csv \\
    --dependencies dependencies.csv --enrich

  # 4. Get inferred and optional dependencies (simple output)
  python -m deepcommit.tools.conflict_analysis.milestone_dependency_analyzer \\
    --commit-dag commit_dag.json --milestones milestones.csv \\
    --get-optional
        """,
    )

    parser.add_argument("--commit-dag", "-c", required=True, help="Path to commit DAG JSON file")
    parser.add_argument("--milestones", "-m", required=True, help="Path to milestones CSV file")
    parser.add_argument("--dependencies", "-d", help="Path to existing dependencies CSV file")
    parser.add_argument("--check", nargs=2, metavar=("M1", "M2"), help="Check dependency between two milestones")
    parser.add_argument("--infer", action="store_true", help="Infer all milestone dependencies")
    parser.add_argument("--enrich", action="store_true", help="Enrich dependencies (requires --dependencies)")
    parser.add_argument(
        "--get-optional",
        action="store_true",
        help="Get inferred and optional dependencies (simple output, no dependencies.csv needed)",
    )
    parser.add_argument("--output-dir", "-o", help="Output directory for results")
    parser.add_argument("--detect-redundant", action="store_true", help="Detect redundant transitive dependencies")
    parser.add_argument(
        "--no-filter-transitive",
        action="store_true",
        help="Disable transitive edge filtering for optional dependencies",
    )

    args = parser.parse_args()

    # 创建分析器
    analyzer = MilestoneDependencyAnalyzer(
        commit_dag_path=args.commit_dag,
        milestone_csv_path=args.milestones,
        dependencies_csv_path=args.dependencies,
        detect_redundant=args.detect_redundant,
    )

    # 执行操作
    if args.check:
        m1, m2 = args.check
        result = analyzer.check_milestone_dependency(m1, m2)
        analyzer.print_dependency_result(result)

    elif args.infer:
        inferred = analyzer.infer_dependencies_from_commits()
        print(f"\nInferred {sum(len(v) for v in inferred.values())} dependencies", file=sys.stderr)

        if args.output_dir:
            os.makedirs(args.output_dir, exist_ok=True)
            output_path = os.path.join(args.output_dir, "inferred_dependencies.csv")
            save_dependency_graph(inferred, output_path)
            print(f"Saved to {output_path}", file=sys.stderr)

    elif args.enrich:
        if not args.dependencies:
            print("Error: --enrich requires --dependencies", file=sys.stderr)
            sys.exit(1)

        results = analyzer.enrich_dependencies()

        if args.output_dir:
            os.makedirs(args.output_dir, exist_ok=True)
            analyzer.export_enriched_dependencies(os.path.join(args.output_dir, "dependencies_enriched.csv"), results)
            analyzer.export_filtered_milestones(os.path.join(args.output_dir, "milestones_filtered.csv"))
            analyzer.export_detailed_analysis_json(os.path.join(args.output_dir, "analysis_details.json"), results)

    elif args.get_optional:
        # 简单输出：inferred 和 optional 依赖
        filter_transitive = not args.no_filter_transitive
        inferred_list, optional_list = analyzer.get_inferred_and_optional_dependencies(
            filter_transitive=filter_transitive
        )

        print("\n" + "=" * 80, file=sys.stderr)
        print("INFERRED AND OPTIONAL DEPENDENCIES", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        print(f"\n📌 Inferred Dependencies: {len(inferred_list)}", file=sys.stderr)
        print(json.dumps(inferred_list, indent=2), file=sys.stderr)

        print(f"\n🎯 Optional Dependencies: {len(optional_list)}", file=sys.stderr)
        print(json.dumps(optional_list, indent=2), file=sys.stderr)

        if args.output_dir:
            os.makedirs(args.output_dir, exist_ok=True)
            output_file = os.path.join(args.output_dir, "dependencies_simple.json")
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump({"inferred_dependencies": inferred_list, "optional_dependencies": optional_list}, f, indent=2)
            print(f"\n✓ Saved to: {output_file}", file=sys.stderr)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
