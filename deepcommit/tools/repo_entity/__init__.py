"""
Repository Entity Tools

This module provides tools for retrieving and searching repository entities including:
- Issues
- Pull Requests
- Commits
- Releases

Main components:
- repo_entity_retriever: Core retrieval functions for repository metadata
- search_entity: Search functionality across repository entities
"""

from .repo_entity_retriever import (
    get_commit_context,
    get_commits_diff,
    # _create_analyzer,
    # _get_commit_stats,
    # _extract_snippet,
    get_issue_context,
    get_pr_context,
    get_release_context,
)
from .search_entity import search_entities

__all__ = [
    # Core retrieval functions
    "get_issue_context",
    "get_pr_context",
    "get_commit_context",
    "get_release_context",
    "get_commits_diff",
    # Search functionality
    "search_entities",
    # Internal utilities (exposed for search_entity)
    # "_create_analyzer",
    # "_get_commit_stats",
    # "_extract_snippet",
]
