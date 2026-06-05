"""
DeepCommit Tools Package

This package contains tools for repository analysis, milestone construction,
validation, and support utilities for the DeepCommit project.

Tools are organized by category:
- Analysis tools (分析类): Repository entity retrieval and search
- Construction tools (构建类): Milestone creation and management
- Validation tools (验证类): Quality assurance and verification
- Support tools (支持类): Utility functions and helpers
"""

# Repository entity tools
from .conflict_analysis.milestone_dependency_analyzer import (
    MilestoneDependencyAnalyzer,
)

# Conflict analysis tools
from .conflict_analysis.milestone_internal_conflict_analyzer import (
    analyze_all_milestones,
)

# DAG analysis tools
from .dag_analysis.dag_analyzer import (
    ParallelismMetrics,
    analyze_dag_parallelism_tool,
    analyze_parallelism,
    detect_cycle,
    topological_sort,
    validate_dag,
)

# Milestone analysis tools
from .milestone_analysis.milestone_statistics import (
    analyze_commit_coverage,
    analyze_milestone_size_balance,
)
from .milestone_analysis.validate_partitioning import (
    validate_milestone_partitioning,
)
from .repo_entity.repo_entity_retriever import (
    get_commit_context,
    get_commits_diff,
    get_issue_context,
    get_pr_context,
    get_release_context,
)
from .repo_entity.search_entity import search_entities

__all__ = [
    # Repository entity tools
    "get_issue_context",
    "get_pr_context",
    "get_commit_context",
    "get_release_context",
    "get_commits_diff",
    "search_entities",
    # DAG analysis tools
    "ParallelismMetrics",
    "detect_cycle",
    "topological_sort",
    "analyze_parallelism",
    "validate_dag",
    "analyze_dag_parallelism_tool",
    # Milestone analysis tools
    "analyze_commit_coverage",
    "analyze_milestone_size_balance",
    "validate_milestone_partitioning",
    # Conflict analysis tools
    "analyze_all_milestones",
    "MilestoneDependencyAnalyzer",
]
