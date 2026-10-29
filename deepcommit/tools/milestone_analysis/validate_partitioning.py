"""Validate milestone partitioning by detecting cycles in inferred dependencies."""

from typing import Any, Dict

from ..conflict_analysis.milestone_dependency_analyzer import MilestoneDependencyAnalyzer
from ..dag_analysis.dag_analyzer import _detect_cycles_from_graph


def validate_milestone_partitioning(
    milestone_csv_path: str,
    commit_dag_path: str,
    split_metadata_dir: str = None,
    repo_path: str = None,
    resolver=None,
) -> Dict[str, Any]:
    """Validate milestone partitioning by inferring dependencies from commit DAG and detecting cycles.

    This function helps determine if milestone partitioning is reasonable by:
    1. Inferring dependencies ONLY from commit DAG (no existing dependencies needed)
    2. Detecting cycles in the inferred dependency graph
    3. If cycles exist, it indicates milestone partitioning issues

    Args:
        milestone_csv_path: Path to milestone CSV file
        commit_dag_path: Path to commit DAG JSON file
        split_metadata_dir: Optional path to split commit metadata directory
        repo_path: Optional Git repository path for precise blame-based edge mapping
        resolver: Optional multi-repo WorkspaceResolver (split blame runs in each commit's own repo)

    Returns:
        Dictionary containing:
        - is_valid_partitioning: Boolean - whether milestones can form a DAG (no cycles)
        - inferred_dependencies: List of [source, target] dependency pairs
        - inferred_dependencies_count: Number of inferred dependencies
        - cycles: List of cycles detected in inferred dependencies
        - cycles_count: Number of cycles found
        - cycle_edge_details: For each cycle, statistics about edges
        - problematic_milestones: Set of milestone IDs involved in cycles
        - recommendation: Suggestions for fixing partitioning issues
    """
    analyzer = MilestoneDependencyAnalyzer(
        commit_dag_path=commit_dag_path,
        milestone_csv_path=milestone_csv_path,
        dependencies_csv_path=None,  # No existing dependencies needed
        split_metadata_dir=split_metadata_dir,
        repo_path=repo_path,
        resolver=resolver,
    )

    # Infer dependencies from commit DAG
    inferred_deps = analyzer.infer_dependencies_from_commits(do_transitive_reduction=True)

    # Convert to edge list
    inferred_edges = []
    for source, targets in inferred_deps.items():
        for target in targets:
            inferred_edges.append([source, target])

    # Detect cycles in inferred dependencies
    all_nodes = set(analyzer.milestones.keys())
    cycles = _detect_cycles_from_graph(dict(inferred_deps), all_nodes)

    # Find all milestones involved in cycles
    problematic_milestones = set()
    for cycle in cycles:
        problematic_milestones.update(cycle)

    # Analyze edges in cycles to provide more context
    cycle_edge_details = []
    for cycle in cycles:
        edges_in_cycle = []
        # For each consecutive pair in the cycle
        for i in range(len(cycle) - 1):
            source = cycle[i]
            target = cycle[i + 1]

            # Count how many commit dependencies exist for this edge
            commit_dep_count = 0
            if source in inferred_deps and target in inferred_deps[source]:
                # Count commit-level dependencies
                for commit_a in analyzer.milestones[source]["commits"]:
                    for commit_b in analyzer.milestones[target]["commits"]:
                        if commit_b in analyzer.transitive_closure.get(commit_a, set()):
                            commit_dep_count += 1

            edges_in_cycle.append({"source": source, "target": target, "commit_dependency_count": commit_dep_count})

        cycle_edge_details.append({"cycle_path": cycle, "edges": edges_in_cycle})

    # Generate recommendation
    recommendation = ""
    if cycles:
        recommendation = (
            f"⚠️ Milestone partitioning has issues: {len(cycles)} cycle(s) detected.\n"
            f"Problematic milestones: {', '.join(sorted(problematic_milestones))}\n\n"
            "Recommendations:\n"
            "1. Review the commits in these milestones - they likely have interleaved development\n"
            "2. Consider merging or re-partitioning milestones that form cycles\n"
            "3. Use check_dependency tool to analyze specific milestone pairs for details"
        )
    else:
        recommendation = "✓ Milestone partitioning is valid - no cycles detected in inferred dependencies."

    return {
        "is_valid_partitioning": len(cycles) == 0,
        "inferred_dependencies": inferred_edges,
        "inferred_dependencies_count": len(inferred_edges),
        "cycles": cycles,
        "cycles_count": len(cycles),
        "cycle_edge_details": cycle_edge_details,
        "problematic_milestones": sorted(list(problematic_milestones)),
        "recommendation": recommendation,
    }
