"""
Common data loading utilities for various analysis tools.

This module provides centralized functions for loading milestone, PR, and commit data
to avoid code duplication across different analysis scripts.
"""

import csv
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple, Union, overload

from ...tools.utils.commit_utils import normalize_commit_sha


def load_milestones(csv_path: str) -> Dict[str, Dict]:
    """
    Load milestone data from CSV file.

    Supports both old and new CSV formats:
    - Old format: core_pr_commits, main_commits, start_date, end_date, core_prs, all_prs
    - New format: commits, start_time, end_time, mini_srs, loc, src_loc, touched_test_files, touched_src_files

    Args:
        csv_path: Path to the milestone CSV file

    Returns:
        Dictionary mapping milestone_id to milestone information
    """
    milestones = {}
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            milestone_id = row["id"]

            # Parse commits from different columns
            commits = []
            if row.get("core_pr_commits"):
                commits.extend([c.strip() for c in row["core_pr_commits"].split(";") if c.strip()])
            if row.get("main_commits"):
                commits.extend([c.strip() for c in row["main_commits"].split(";") if c.strip()])
            if row.get("commits"):
                commits.extend([c.strip() for c in row["commits"].split(";") if c.strip()])
            # Also include integration_test_commit if present
            if row.get("integration_test_commit") and row["integration_test_commit"].strip():
                commits.append(row["integration_test_commit"].strip())

            # Detect format version by checking for new format fields
            is_new_format = "start_time" in row

            # Build milestone data
            milestone_data = {
                "title": row.get("title", ""),
                "category": row.get("category", ""),
                "commits": list(set(normalize_commit_sha(c) for c in commits)),  # Normalize SHA and remove duplicates
            }

            # Add format-specific fields
            if is_new_format:
                # New format fields
                milestone_data.update(
                    {
                        "mini_srs": row.get("mini_srs", ""),
                        "start_time": row.get("start_time", ""),
                        "end_time": row.get("end_time", ""),
                        "loc": int(row["loc"]) if row.get("loc") and row["loc"].strip() else 0,
                        "src_loc": int(row["src_loc"]) if row.get("src_loc") and row["src_loc"].strip() else 0,
                        "touched_test_files": [
                            f.strip() for f in row.get("touched_test_files", "").split(";") if f.strip()
                        ],
                        "touched_src_files": [
                            f.strip() for f in row.get("touched_src_files", "").split(";") if f.strip()
                        ],
                    }
                )
            else:
                # Old format fields
                milestone_data.update(
                    {
                        "start_date": row.get("start_date", ""),
                        "end_date": row.get("end_date", ""),
                        "core_prs": row.get("core_prs", "").split(";") if row.get("core_prs") else [],
                        "all_prs": row.get("all_prs", "").split(";") if row.get("all_prs") else [],
                    }
                )

            milestones[milestone_id] = milestone_data

    return milestones


def load_main_commits(main_commits_file: str) -> Set[str]:
    """
    Load all main branch commits from JSONL file.

    Args:
        main_commits_file: Path to the main commits JSONL file

    Returns:
        Set of commit SHAs (7-char prefix)
    """
    main_commits = set()

    if not Path(main_commits_file).exists():
        print(f"Warning: Main commits file not found: {main_commits_file}")
        return main_commits

    with open(main_commits_file, "r") as f:
        for line in f:
            if line.strip():
                try:
                    commit = json.loads(line)
                    main_commits.add(commit["sha"][:7])
                except (json.JSONDecodeError, KeyError) as e:
                    print(f"Error parsing main commit: {e}")

    print(f"Loaded {len(main_commits)} main commits")
    return main_commits


def load_pr_commits(pr_file: str) -> Tuple[Set[str], Dict[int, List[str]]]:
    """
    Load PR commits from pull requests JSONL file.

    Args:
        pr_file: Path to the pull requests JSONL file

    Returns:
        Tuple of (all_pr_commits_set, pr_to_commits_mapping)
    """
    pr_commits = set()
    pr_to_commits = {}

    if not Path(pr_file).exists():
        print(f"Warning: Pull requests file not found: {pr_file}")
        return pr_commits, pr_to_commits

    with open(pr_file, "r") as f:
        for line in f:
            if line.strip():
                try:
                    pr = json.loads(line)
                    pr_number = pr.get("number")

                    # Extract commits from PR
                    commits = pr.get("commits", [])
                    if commits:
                        # Handle both dict and string formats
                        commit_shas = []
                        for c in commits:
                            if isinstance(c, dict) and "sha" in c:
                                commit_shas.append(c["sha"][:7])
                            elif isinstance(c, str):
                                commit_shas.append(c[:7])

                        pr_commits.update(commit_shas)
                        if pr_number and commit_shas:
                            pr_to_commits[pr_number] = commit_shas

                except (json.JSONDecodeError, KeyError) as e:
                    print(f"Error parsing PR: {e}")

    print(f"Loaded {len(pr_commits)} PR commits from {len(pr_to_commits)} PRs")
    return pr_commits, pr_to_commits


def load_conflict_pairs(csv_path: str) -> List[Dict]:
    """
    Load conflict pairs from CSV generated by step1_detect_conflicts.

    Args:
        csv_path: Path to the conflict pairs CSV file

    Returns:
        List of conflict pair dictionaries
    """
    conflict_pairs = []

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("base_commit") and row.get("next_commit"):
                conflict_pairs.append(row)

    return conflict_pairs


@overload
def load_dependency_graph(csv_path: str, return_nodes: bool = False) -> Dict[str, Set[str]]: ...


@overload
def load_dependency_graph(csv_path: str, return_nodes: bool = True) -> Tuple[Dict[str, Set[str]], Set[str]]: ...


def load_dependency_graph(
    csv_path: str, return_nodes: bool = False
) -> Union[Dict[str, Set[str]], Tuple[Dict[str, Set[str]], Set[str]]]:
    """
    从 CSV 文件加载依赖关系图

    CSV 格式要求(canonical convention):
    - 必须包含 prerequisite_id 和 dependant_id 列(也兼容 legacy source_id/target_id)
    - `prerequisite_id → dependant_id` 表示 prerequisite 必须在 dependant 之前完成
    - prerequisite_id = 前置(upstream), dependant_id = 依赖者(downstream)

    Args:
        csv_path: 依赖关系 CSV 文件路径
        return_nodes: 是否返回所有节点集合（默认 False）

    Returns:
        当 return_nodes=False 时: dependencies[source] = {targets that depend on source}
        当 return_nodes=True 时: (dependencies, all_nodes)

    Example:
        >>> deps = load_dependency_graph("dependencies.csv")
        >>> # If CSV has rows (M1,M2), (M1,M3) — M2 and M3 both depend on M1
        >>> print(deps["M1"])  # {M2, M3} = downstream milestones that depend on M1
        {'M2', 'M3'}

        >>> deps, nodes = load_dependency_graph("dependencies.csv", return_nodes=True)
        >>> print(nodes)  # All nodes in the graph
        {'M1', 'M2', 'M3', 'M4'}
    """
    dependencies = defaultdict(set)
    nodes = set()

    if not os.path.exists(csv_path):
        print(f"Warning: Dependencies file not found: {csv_path}")
        return (dependencies, nodes) if return_nodes else dependencies

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        # Detect column convention: new (prerequisite_id/dependant_id) or
        # legacy (source_id/target_id). Both encode the same direction
        # (prerequisite → dependant); only the header changed.
        header = reader.fieldnames or []
        if "prerequisite_id" in header and "dependant_id" in header:
            prereq_col, dep_col = "prerequisite_id", "dependant_id"
        elif "source_id" in header and "target_id" in header:
            prereq_col, dep_col = "source_id", "target_id"
        else:
            raise ValueError(
                f"dependencies CSV missing required columns "
                f"(prerequisite_id/dependant_id or legacy source_id/target_id): {csv_path}"
            )
        for row in reader:
            source = row[prereq_col]
            target = row[dep_col]
            dependencies[source].add(target)

            # Collect all nodes (both sources and targets)
            nodes.add(source)
            nodes.add(target)

    total_deps = sum(len(targets) for targets in dependencies.values())
    print(f"Loaded {total_deps} dependencies from {csv_path}")

    if return_nodes:
        print(f"  Total nodes: {len(nodes)}")
        return dependencies, nodes
    return dependencies


def save_dependency_graph(dependencies: Dict[str, Set[str]], output_path: str, sort: bool = True) -> None:
    """
    将依赖关系图保存到 CSV 文件(用新 header: prerequisite_id / dependant_id)

    Args:
        dependencies: 依赖关系图(key 是 prerequisite milestone id,value 是该 prerequisite 的所有 dependants)
        output_path: 输出文件路径
        sort: 是否按 prerequisite_id 和 dependant_id 排序（默认 True）
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
        fieldnames = ["prerequisite_id", "dependant_id"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        # 生成所有边
        edges = []
        for prerequisite_id, dependants in dependencies.items():
            for dependant_id in dependants:
                edges.append((prerequisite_id, dependant_id))

        # 排序（如果需要）
        if sort:
            edges.sort()

        # 写入
        for prerequisite_id, dependant_id in edges:
            writer.writerow(
                {
                    "prerequisite_id": prerequisite_id,
                    "dependant_id": dependant_id,
                }
            )

    total_deps = len(edges)
    print(f"✓ Dependencies exported to: {output_path}")
    print(f"  Total dependencies: {total_deps}")
