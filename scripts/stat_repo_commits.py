#!/usr/bin/env python3
"""
统计 preprocessed 数据的 commits 变更量和 Top3 连通子图覆盖率

用法:
    python scripts/stat_repo_commits.py [repo_name]

    不带参数: 统计所有 repo
    带参数: 统计指定 repo (如 navidrome_navidrome_v0.57.0_v0.58.0)
"""

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


def find_connected_components(nodes: set, edges: list) -> list[set]:
    """使用 Union-Find 找到所有连通分量"""
    parent = {node: node for node in nodes}

    def find(x):
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src in nodes and tgt in nodes:
            union(src, tgt)

    components = defaultdict(set)
    for node in nodes:
        components[find(node)].add(node)

    return list(components.values())


def stat_single_repo(base_path: Path, repo_name: str) -> dict | None:
    """统计单个 repo 的数据"""
    repo_dir = base_path / repo_name
    commits_path = repo_dir / "main_commits.jsonl"
    dag_path = repo_dir / "commit_dag.json"

    if not commits_path.exists():
        return None

    # 读取 commits
    commits_data = {}
    with open(commits_path) as f:
        for line in f:
            commit = json.loads(line)
            commits_data[commit["sha"]] = commit

    # 统计总变更量
    total_add, total_del, total_files = 0, 0, 0
    for commit in commits_data.values():
        for fc in commit.get("files_changed", []):
            total_add += fc.get("additions", 0)
            total_del += fc.get("deletions", 0)
            total_files += 1

    total_change = total_add + total_del

    # 统计 Top3 连通子图
    top3_nodes, top3_add, top3_del, top3_files = 0, 0, 0, 0
    if dag_path.exists():
        with open(dag_path) as f:
            dag = json.load(f)
        nodes = set(dag.get("nodes", {}).keys())
        edges = dag.get("edges_dag", [])
        if nodes:
            components = find_connected_components(nodes, edges)
            sorted_components = sorted(components, key=len, reverse=True)
            top3 = sorted_components[:3]
            top3_shas = set()
            for comp in top3:
                top3_shas.update(comp)
                top3_nodes += len(comp)

            for sha in top3_shas:
                if sha in commits_data:
                    for fc in commits_data[sha].get("files_changed", []):
                        top3_add += fc.get("additions", 0)
                        top3_del += fc.get("deletions", 0)
                        top3_files += 1

    top3_total = top3_add + top3_del
    ratio = (top3_total / total_change * 100) if total_change > 0 else 0

    return {
        "repo": repo_name,
        "commits": len(commits_data),
        "net": total_add - total_del,
        "total": total_change,
        "files": total_files,
        "top3_nodes": top3_nodes,
        "top3_net": top3_add - top3_del,
        "top3_total": top3_total,
        "top3_files": top3_files,
        "ratio": ratio,
    }


def print_stats(stats: list[dict], sort_by: str = "total"):
    """打印统计结果"""
    if sort_by:
        stats = sorted(stats, key=lambda x: x.get(sort_by, 0), reverse=True)

    print(
        f"{'Repo':<55} | {'Commits':>7} | {'Net':>8} | {'Total':>8} | {'Files':>6} | {'Top3':>5} | {'T3 Net':>8} | {'T3 Total':>9} | {'T3 Files':>8} | {'Ratio':>6}"
    )
    print("-" * 140)
    for s in stats:
        print(
            f"{s['repo']:<55} | {s['commits']:>7} | {s['net']:>8} | {s['total']:>8} | {s['files']:>6} | {s['top3_nodes']:>5} | {s['top3_net']:>8} | {s['top3_total']:>9} | {s['top3_files']:>8} | {s['ratio']:>5.1f}%"
        )


def main():
    parser = argparse.ArgumentParser(description="统计 preprocessed 数据的 commits 变更量")
    parser.add_argument("repo_name", nargs="?", help="指定 repo 名称，不指定则统计所有")
    parser.add_argument("--sort", default="total", help="排序字段 (total, commits, ratio, net)")
    parser.add_argument("--csv", help="输出到 CSV 文件")
    parser.add_argument("--exclude", nargs="*", default=[], help="排除的 repo 列表")
    args = parser.parse_args()

    base_path = Path(__file__).parent.parent / "DATA/github_data/preprocessed"

    if not base_path.exists():
        print(f"Error: {base_path} not found")
        return

    stats = []

    if args.repo_name:
        # 统计单个 repo
        result = stat_single_repo(base_path, args.repo_name)
        if result:
            stats.append(result)
        else:
            print(f"Error: {args.repo_name} not found or no data")
            return
    else:
        # 统计所有 repo
        for repo_dir in sorted(base_path.iterdir()):
            if not repo_dir.is_dir():
                continue
            if repo_dir.name in args.exclude:
                continue
            result = stat_single_repo(base_path, repo_dir.name)
            if result:
                stats.append(result)

    if not stats:
        print("No data found")
        return

    print_stats(stats, args.sort)

    if args.csv:
        output_path = Path(args.csv)
        fieldnames = [
            "repo",
            "commits",
            "net",
            "total",
            "files",
            "top3_nodes",
            "top3_net",
            "top3_total",
            "top3_files",
            "ratio",
        ]
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for s in stats:
                s["ratio"] = f"{s['ratio']:.1f}%"
                writer.writerow(s)
        print(f"\nCSV saved to {output_path}")


if __name__ == "__main__":
    main()
