"""
Conflict Analysis Module

This module provides tools for detecting and analyzing git cherry-pick conflicts
using commit DAG relationships.
"""

# Commit DAG-based Analysis (optimized using commit dependency graph)
from .milestone_dependency_analyzer import MilestoneDependencyAnalyzer
from .milestone_internal_conflict_analyzer import (
    analyze_all_milestones,
    analyze_milestone_completeness,
)

# Alias for backward compatibility
analyze_milestone_conflicts_dag_based = analyze_all_milestones

# from deepcommit.utils.data.commit_dag_loader import CommitDAGLoader

__all__ = [
    "analyze_milestone_conflicts_dag_based",  # DAG-based milestone dependency completeness checker
    "analyze_all_milestones",
    "analyze_milestone_completeness",
    "MilestoneDependencyAnalyzer",  # Unified milestone dependency analyzer
]
