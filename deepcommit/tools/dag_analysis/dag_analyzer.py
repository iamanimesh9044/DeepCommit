"""
DAG Analysis Tool

This module provides comprehensive functionality for analyzing directed acyclic graphs (DAGs):
- Cycle detection and validation
- Parallelism metrics and analysis
- Topological sorting
- Critical path analysis
"""

import json
import statistics
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Import the shared data loader from utils
from ...utils.data.data_loader import load_dependency_graph


@dataclass
class ParallelismMetrics:
    """Metrics for measuring DAG parallelism."""

    critical_path_length: int
    critical_path_nodes: List[str]
    maximum_width: int
    total_nodes: int
    total_edges: int
    levels: List[Set[str]]
    level_widths: List[int]
    level_widths_stats: Dict[str, float]  # Statistical distribution of level widths


def _calculate_degrees(graph: Dict[str, Set[str]], nodes: Set[str]) -> Tuple[Dict[str, int], Dict[str, int]]:
    """Calculate in-degree and out-degree for all nodes."""
    in_degree = {node: 0 for node in nodes}
    out_degree = {node: 0 for node in nodes}

    for node in nodes:
        if node in graph:
            out_degree[node] = len(graph[node])
            for target in graph[node]:
                in_degree[target] += 1

    return in_degree, out_degree


def _detect_cycles_from_graph(graph: Dict[str, Set[str]], nodes: Set[str]) -> List[List[str]]:
    """
    Detect cycles in a dependency graph using efficient single-pass DFS.

    Args:
        graph: Adjacency list representing the dependency graph
        nodes: Set of all nodes in the graph

    Returns:
        List of detected cycles (each cycle is a list of nodes)
    """
    cycles = []
    visited = set()
    rec_stack = set()
    path = []

    def dfs(node: str) -> bool:
        """DFS to detect cycles"""
        visited.add(node)
        rec_stack.add(node)
        path.append(node)

        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                if dfs(neighbor):
                    return True
            elif neighbor in rec_stack:
                # Found a cycle
                cycle_start = path.index(neighbor)
                cycle = path[cycle_start:] + [neighbor]
                cycles.append(cycle)
                return True

        path.pop()
        rec_stack.remove(node)
        return False

    for node in nodes:
        if node not in visited:
            dfs(node)

    return cycles


def detect_cycle(csv_path: str) -> Tuple[bool, Optional[List[List[str]]]]:
    """
    Detect cycles in a DAG from CSV file.

    Args:
        csv_path: Path to the CSV file

    Returns:
        Tuple of (has_cycle, cycles) where cycles is list of detected cycles or None
    """
    graph, nodes = load_dependency_graph(csv_path, return_nodes=True)
    cycles = _detect_cycles_from_graph(graph, nodes)
    has_cycle = len(cycles) > 0

    return has_cycle, cycles if has_cycle else None


def topological_sort(csv_path: str) -> Optional[List[str]]:
    """
    Perform topological sort on the DAG.

    Args:
        csv_path: Path to the CSV file

    Returns:
        Topologically sorted list of nodes if DAG is acyclic, None if cycle exists
    """
    graph, nodes = load_dependency_graph(csv_path, return_nodes=True)
    has_cycle, _ = detect_cycle(csv_path)

    if has_cycle:
        return None

    in_degree = {node: 0 for node in nodes}

    for node in graph:
        for neighbor in graph[node]:
            in_degree[neighbor] += 1

    queue = [node for node in nodes if in_degree[node] == 0]
    result = []

    while queue:
        node = queue.pop(0)
        result.append(node)

        if node in graph:
            for neighbor in graph[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

    return result if len(result) == len(nodes) else None


def _compute_levels(graph: Dict[str, Set[str]], nodes: Set[str]) -> List[Set[str]]:
    """
    Compute the levels of nodes in the DAG.
    Nodes in the same level can be executed in parallel.
    """
    in_degree, _ = _calculate_degrees(graph, nodes)
    levels = []
    remaining_nodes = nodes.copy()
    temp_in_degree = in_degree.copy()

    while remaining_nodes:
        # Find all nodes with no incoming edges
        current_level = {node for node in remaining_nodes if temp_in_degree[node] == 0}

        if not current_level:
            raise ValueError("Cycle detected in graph")

        levels.append(current_level)

        # Remove current level nodes and update degrees
        for node in current_level:
            remaining_nodes.remove(node)
            if node in graph:
                for target in graph[node]:
                    temp_in_degree[target] -= 1

    return levels


def _find_critical_path(graph: Dict[str, Set[str]], nodes: Set[str]) -> Tuple[int, List[str]]:
    """
    Find the critical path (longest path) in the DAG.
    Returns: (path_length, path_nodes)
    """
    in_degree, _ = _calculate_degrees(graph, nodes)

    # Topological sort
    topo_order = []
    temp_in_degree = in_degree.copy()
    queue = deque([n for n in nodes if temp_in_degree[n] == 0])

    while queue:
        node = queue.popleft()
        topo_order.append(node)
        if node in graph:
            for neighbor in graph[node]:
                temp_in_degree[neighbor] -= 1
                if temp_in_degree[neighbor] == 0:
                    queue.append(neighbor)

    # Dynamic programming to find longest path
    dist = {node: 0 for node in nodes}
    parent = {node: None for node in nodes}

    for node in topo_order:
        if node in graph:
            for neighbor in graph[node]:
                if dist[neighbor] < dist[node] + 1:
                    dist[neighbor] = dist[node] + 1
                    parent[neighbor] = node

    # Find the node with maximum distance
    max_dist = 0
    end_node = None
    for node, d in dist.items():
        if d > max_dist:
            max_dist = d
            end_node = node

    # Reconstruct path
    path = []
    current = end_node
    while current is not None:
        path.append(current)
        current = parent[current]
    path.reverse()

    return max_dist + 1, path  # +1 because we count nodes, not edges


def analyze_parallelism(csv_path: str) -> ParallelismMetrics:
    """
    Compute comprehensive parallelism metrics for the DAG.

    Args:
        csv_path: Path to the CSV file

    Returns:
        ParallelismMetrics dataclass with all metrics
    """
    graph, nodes = load_dependency_graph(csv_path, return_nodes=True)

    levels = _compute_levels(graph, nodes)
    critical_length, critical_path = _find_critical_path(graph, nodes)

    # Calculate metrics
    level_widths = [len(level) for level in levels]
    maximum_width = max(level_widths) if level_widths else 0
    total_nodes = len(nodes)
    total_edges = sum(len(targets) for targets in graph.values())

    # Calculate statistical distribution of level widths
    if level_widths:
        mean_width = statistics.mean(level_widths)
        median_width = statistics.median(level_widths)
        std_dev = statistics.stdev(level_widths) if len(level_widths) > 1 else 0.0
        min_width = min(level_widths)
        max_width = max(level_widths)
        # Coefficient of variation: measures relative variability
        variance_coefficient = (std_dev / mean_width) if mean_width > 0 else 0.0

        level_widths_stats = {
            "mean": mean_width,
            "median": median_width,
            "std_dev": std_dev,
            "min": min_width,
            "max": max_width,
            "variance_coefficient": variance_coefficient,
        }
    else:
        level_widths_stats = {
            "mean": 0.0,
            "median": 0.0,
            "std_dev": 0.0,
            "min": 0,
            "max": 0,
            "variance_coefficient": 0.0,
        }

    return ParallelismMetrics(
        critical_path_length=critical_length,
        critical_path_nodes=critical_path,
        maximum_width=maximum_width,
        total_nodes=total_nodes,
        total_edges=total_edges,
        levels=levels,
        level_widths=level_widths,
        level_widths_stats=level_widths_stats,
    )


# MCP Tool wrapper functions


def analyze_dag_parallelism_tool(csv_path: str, save_results: bool = False, output_dir: str = None) -> Dict[str, Any]:
    """
    MCP tool function to analyze DAG parallelism.

    Args:
        csv_path: Path to the CSV file
        save_results: Whether to save results to file (default: False)
        output_dir: Output directory for results (default: csv_path parent/analysis_results)

    Returns:
        Dictionary with parallelism metrics
    """
    metrics = analyze_parallelism(csv_path)
    result = {
        "critical_path_length": metrics.critical_path_length,
        "critical_path_nodes": metrics.critical_path_nodes,
        "maximum_width": metrics.maximum_width,
        "total_nodes": metrics.total_nodes,
        "total_edges": metrics.total_edges,
        "levels": [list(level) for level in metrics.levels],
        "level_widths": metrics.level_widths,
        "level_widths_stats": metrics.level_widths_stats,
    }

    # Save results if requested
    if save_results:
        csv_path_obj = Path(csv_path)

        # Use provided output_dir or default to csv_path parent/analysis_results
        if output_dir:
            out_dir = Path(output_dir)
        else:
            out_dir = csv_path_obj.parent / "analysis_results"

        out_dir.mkdir(parents=True, exist_ok=True)

        # Use fixed filename for DAG parallelism analysis
        output_file = out_dir / "dag_parallelism_analysis.json"

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        result["output_file"] = str(output_file)
        result["output_message"] = f"Results saved to {output_file}"

    return result


def validate_dag(csv_path: str, save_results: bool = False, output_dir: str = None) -> Dict[str, Any]:
    """
    MCP tool function to validate if a graph is a proper DAG.

    Args:
        csv_path: Path to the CSV file
        save_results: Whether to save results to file (default: False)
        output_dir: Output directory for results (default: csv_path parent/analysis_results)

    Returns:
        Dictionary with validation results
    """
    has_cycle, cycles = detect_cycle(csv_path)
    graph, nodes = load_dependency_graph(csv_path, return_nodes=True)
    edge_count = sum(len(targets) for targets in graph.values())

    result = {
        "is_valid_dag": not has_cycle,
        "has_cycle": has_cycle,
        "node_count": len(nodes),
        "edge_count": edge_count,
    }

    if has_cycle:
        result["cycles"] = cycles
        result["cycle_count"] = len(cycles)
    else:
        topo_sort = topological_sort(csv_path)
        if topo_sort:
            result["topological_order"] = topo_sort

    # Save results if requested
    if save_results:
        csv_path_obj = Path(csv_path)

        # Use provided output_dir or default to csv_path parent/analysis_results
        if output_dir:
            out_dir = Path(output_dir)
        else:
            out_dir = csv_path_obj.parent / "analysis_results"

        out_dir.mkdir(parents=True, exist_ok=True)

        # Use fixed filename for DAG cycle validation
        output_file = out_dir / "dag_cycle_validation.json"

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        result["output_file"] = str(output_file)
        result["output_message"] = f"Results saved to {output_file}"

    return result
