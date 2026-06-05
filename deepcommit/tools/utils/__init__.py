"""
Utility functions for the tools module
"""

from .commit_utils import (
    batch_normalize_commits,
    find_sha_in_set,
    normalize_commit_sha,
    sha_match,
    validate_commit_sha,
)
from .graph_algorithms import (
    compute_transitive_closure,
    get_graph_statistics,
    transitive_reduction,
)
from .repo_utils import (
    get_repo_path_from_env,
    resolve_data_dir,
    resolve_repo_id,
    resolve_repo_path,
    resolve_repo_path_with_fallback,
    sanitize_repo_id,
)

__all__ = [
    # Repo utils
    "sanitize_repo_id",
    "resolve_repo_id",
    "resolve_repo_path",
    "resolve_data_dir",
    "get_repo_path_from_env",
    "resolve_repo_path_with_fallback",
    # Commit utils
    "normalize_commit_sha",
    "sha_match",
    "find_sha_in_set",
    "batch_normalize_commits",
    "validate_commit_sha",
    # Graph algorithms
    "compute_transitive_closure",
    "transitive_reduction",
    "get_graph_statistics",
]
