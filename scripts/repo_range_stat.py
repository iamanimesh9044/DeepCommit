#!/usr/bin/env python3
"""
Repo range statistics (reusable).

Input: a list of preprocessed repo-range directory names (under DATA/github_data/preprocessed).
Output: a table (markdown/csv/json) summarizing total changes and Top-K connected-component coverage.

Example (as a library):
    from scripts.repo_range_stat import compute_repo_range_stats, format_stats_table
    rows = compute_repo_range_stats(["urllib3_urllib3_2.0.6_2.3.0"])
    print(format_stats_table(rows))

Example (CLI):
    python scripts/repo_range_stat.py urllib3_urllib3_2.0.6_2.3.0
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


def _find_repo_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        if (parent / "pyproject.toml").exists() or (parent / "DATA").exists():
            return parent
    return current


def resolve_preprocessed_base_path(base_path: str | Path | None = None) -> Path:
    """
    Resolve base path for preprocessed data.

    Defaults to: <repo_root>/DATA/github_data/preprocessed
    """
    if base_path is not None:
        return Path(base_path).expanduser().resolve()
    return _find_repo_root() / "DATA" / "github_data" / "preprocessed"


def find_connected_components(nodes: set[str], edges: Iterable[dict[str, Any]]) -> list[set[str]]:
    parent: dict[str, str] = {node: node for node in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for edge in edges:
        src, tgt = edge.get("source"), edge.get("target")
        if not isinstance(src, str) or not isinstance(tgt, str):
            continue
        if src in nodes and tgt in nodes:
            union(src, tgt)

    components: dict[str, set[str]] = defaultdict(set)
    for node in nodes:
        components[find(node)].add(node)
    return list(components.values())


def _load_commits_by_sha(commits_path: Path) -> dict[str, dict[str, Any]]:
    commits_data: dict[str, dict[str, Any]] = {}
    with commits_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            commit = json.loads(line)
            sha = commit.get("sha")
            if isinstance(sha, str) and sha:
                commits_data[sha] = commit
    return commits_data


def _sum_file_changes(commits: Iterable[dict[str, Any]]) -> tuple[int, int, int]:
    total_additions = 0
    total_deletions = 0
    total_files = 0
    for commit in commits:
        for fc in commit.get("files_changed", []) or []:
            if not isinstance(fc, dict):
                continue
            total_additions += int(fc.get("additions", 0) or 0)
            total_deletions += int(fc.get("deletions", 0) or 0)
            total_files += 1
    return total_additions, total_deletions, total_files


def compute_repo_range_stat(
    repo_name: str,
    *,
    base_path: str | Path | None = None,
    top_k_components: int = 3,
) -> dict[str, Any]:
    """
    Compute statistics for one preprocessed repo-range directory.

    Returns a dict with keys usable by `format_stats_table`.
    """
    resolved_base = resolve_preprocessed_base_path(base_path)
    repo_dir = resolved_base / repo_name
    commits_path = repo_dir / "main_commits.jsonl"
    dag_path = repo_dir / "commit_dag.json"

    row: dict[str, Any] = {
        "repo": repo_name,
        "commit_count": 0,
        "dag_max_depth": 0,
        "net_loc": 0,
        "total_change": 0,
        "files": 0,
        "topk_nodes": 0,
        "topk_net_loc": 0,
        "topk_total_change": 0,
        "topk_files": 0,
        "topk_coverage_pct": 0.0,
        "error": "",
    }

    if not repo_dir.exists():
        row["error"] = f"missing repo dir: {repo_dir}"
        return row
    if not commits_path.exists():
        row["error"] = f"missing commits: {commits_path}"
        return row
    if not dag_path.exists():
        row["error"] = f"missing dag: {dag_path}"
        return row

    commits_data = _load_commits_by_sha(commits_path)
    row["commit_count"] = len(commits_data)
    total_add, total_del, total_files = _sum_file_changes(commits_data.values())
    total_change = total_add + total_del
    row["net_loc"] = total_add - total_del
    row["total_change"] = total_change
    row["files"] = total_files

    with dag_path.open() as f:
        dag = json.load(f)

    # Compute max depth from layers
    layers = dag.get("layers") or {}
    if layers:
        row["dag_max_depth"] = max(int(k) for k in layers.keys())

    nodes = set((dag.get("nodes") or {}).keys())
    edges = dag.get("edges_dag") or []
    if not isinstance(edges, list):
        edges = []

    components = find_connected_components(nodes, edges)
    components.sort(key=len, reverse=True)
    top_components = components[: max(0, int(top_k_components))]

    top_shas: set[str] = set()
    top_nodes = 0
    for comp in top_components:
        top_shas.update(comp)
        top_nodes += len(comp)
    row["topk_nodes"] = top_nodes

    top_add, top_del, top_files = _sum_file_changes(commits_data[sha] for sha in top_shas if sha in commits_data)
    top_total = top_add + top_del
    row["topk_net_loc"] = top_add - top_del
    row["topk_total_change"] = top_total
    row["topk_files"] = top_files
    row["topk_coverage_pct"] = (top_total / total_change * 100.0) if total_change > 0 else 0.0

    return row


def compute_repo_range_stats(
    repo_names: list[str],
    *,
    base_path: str | Path | None = None,
    top_k_components: int = 3,
) -> list[dict[str, Any]]:
    return [
        compute_repo_range_stat(name, base_path=base_path, top_k_components=top_k_components) for name in repo_names
    ]


def format_stats_table(
    rows: list[dict[str, Any]],
    *,
    table_format: str = "markdown",
    top_k_components: int = 3,
) -> str:
    """
    Format rows returned by compute_repo_range_stats.

    table_format: "markdown" | "csv" | "json"
    """
    table_format = table_format.lower()
    if table_format == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2)

    k = max(0, int(top_k_components))
    columns: list[tuple[str, str]] = [
        ("repo", "repo"),
        ("commit_count", "commits"),
        ("dag_max_depth", "dag_depth"),
        ("net_loc", "net_loc"),
        ("total_change", "total_change"),
        ("files", "files"),
        ("topk_nodes", f"top{k}_nodes"),
        ("topk_net_loc", f"top{k}_net"),
        ("topk_total_change", f"top{k}_total"),
        ("topk_files", f"top{k}_files"),
        ("topk_coverage_pct", f"top{k}_cov%"),
        ("error", "error"),
    ]

    if table_format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([header for _, header in columns])
        for row in rows:
            writer.writerow(
                [
                    (
                        row.get(key, "")
                        if key != "topk_coverage_pct"
                        else (f"{float(row.get(key, 0.0)):.1f}" if row.get(key, "") != "" else "")
                    )
                    for key, _ in columns
                ]
            )
        return buf.getvalue().rstrip("\n")

    # markdown
    headers = [header for _, header in columns]
    matrix: list[list[str]] = []
    for row in rows:
        rendered: list[str] = []
        for key, _ in columns:
            val = row.get(key, "")
            if key == "topk_coverage_pct" and val != "":
                rendered.append(f"{float(val):.1f}")
            else:
                rendered.append(str(val))
        matrix.append(rendered)

    widths = [len(h) for h in headers]
    for r in matrix:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    sep = "| " + " | ".join("-" * w for w in widths) + " |"
    lines = [fmt_row(headers), sep, *[fmt_row(r) for r in matrix]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute repo-range statistics and print a table.")
    parser.add_argument("repo_names", nargs="+", help="Repo-range dir names under preprocessed base path")
    parser.add_argument(
        "--base-path",
        default=None,
        help="Preprocessed base path (default: <repo_root>/DATA/github_data/preprocessed)",
    )
    parser.add_argument("--top-k", type=int, default=3, help="Top-K connected components to aggregate (default: 3)")
    parser.add_argument(
        "--format",
        dest="table_format",
        default="markdown",
        choices=["markdown", "csv", "json"],
        help="Output format (default: markdown)",
    )
    args = parser.parse_args(argv)

    rows = compute_repo_range_stats(args.repo_names, base_path=args.base_path, top_k_components=args.top_k)
    print(format_stats_table(rows, table_format=args.table_format, top_k_components=args.top_k))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
