#!/usr/bin/env python3
"""
Source Lines of Code (src_loc) Calculator for Commits

This module provides functionality to calculate the source code lines changed
in commits, using the existing get_commit_loc method from LocalGitRepo.
"""

from typing import Any, Dict, List, Optional, Union

from deepcommit.utils.git.local_git_repo import LocalGitRepo


def calculate_commits_src_loc(
    commits: List[str],
    repo_path: str,
    path_prefix: Optional[Union[str, List[str]]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Calculate source lines of code (src_loc) for given commits.

    This function analyzes commits and calculates the lines of code changed,
    optionally filtered by path prefix(es) and exclude patterns.

    Args:
        commits: List of commit SHAs to analyze (can be full or 7-char SHAs)
        repo_path: Path to the git repository (bare or regular)
        path_prefix: Optional path prefix(es) to filter files
                    Can be a single string or a list of strings
                    Examples:
                      - "src/" - only count files in src directory
                      - ["src/", "lib/"] - count files in src or lib directories
        exclude_patterns: Optional exclude patterns to filter out files
                         Supports glob patterns like ["**/tests/**", "**/*.md"]

    Returns:
        Dictionary containing:
        - total_src_loc: Total source code lines changed (additions + deletions)
        - total_additions: Total lines added
        - total_deletions: Total lines deleted
        - valid_commits: Number of valid commits analyzed
        - invalid_commits: Number of commits that don't exist
        - commits: Per-commit details with loc breakdown
        - path_prefix: The path prefix(es) used for filtering (if any)
        - exclude_patterns: The exclude patterns used for filtering (if any)

    Example:
        # Calculate src_loc for specific commits with single prefix
        result = calculate_commits_src_loc(
            commits=["1812eac", "def456"],
            repo_path="/path/to/urllib3_urllib3",
            path_prefix="src/"
        )
        print(f"Total src_loc: {result['total_src_loc']}")

        # Calculate src_loc with multiple prefixes
        result = calculate_commits_src_loc(
            commits=["1812eac", "def456"],
            repo_path="/path/to/urllib3_urllib3",
            path_prefix=["src/", "lib/"]
        )

        # With exclude patterns
        result = calculate_commits_src_loc(
            commits=["abc123"],
            repo_path="/path/to/repo",
            exclude_patterns=["**/tests/**", "**/*.md"]
        )
    """
    repo = LocalGitRepo(repo_path)

    # Initialize aggregated statistics
    total_additions = 0
    total_deletions = 0
    total_src_loc = 0
    valid_commits = 0
    invalid_commits = 0

    # Per-commit details
    commit_details = []

    for commit_sha in commits:
        # Get commit loc info using existing method
        loc_info = repo.get_commit_loc(commit_sha, path_prefix=path_prefix, exclude_patterns=exclude_patterns)

        if loc_info.get("exists", False):
            # Valid commit
            additions = loc_info["additions"]
            deletions = loc_info["deletions"]
            commit_total = loc_info["total"]

            total_additions += additions
            total_deletions += deletions
            total_src_loc += commit_total
            valid_commits += 1

            commit_details.append(
                {
                    "sha": commit_sha[:7] if len(commit_sha) > 7 else commit_sha,
                    "full_sha": commit_sha,
                    "exists": True,
                    "additions": additions,
                    "deletions": deletions,
                    "total": commit_total,
                }
            )
        else:
            # Invalid commit
            invalid_commits += 1
            commit_details.append(
                {
                    "sha": commit_sha[:7] if len(commit_sha) > 7 else commit_sha,
                    "full_sha": commit_sha,
                    "exists": False,
                    "additions": 0,
                    "deletions": 0,
                    "total": 0,
                    "error": "Commit does not exist",
                }
            )

    # Build result
    result = {
        "total_src_loc": total_src_loc,
        "total_additions": total_additions,
        "total_deletions": total_deletions,
        "valid_commits": valid_commits,
        "invalid_commits": invalid_commits,
        "commits": commit_details,
        "path_prefix": path_prefix,
        "exclude_patterns": exclude_patterns,
    }

    return result
