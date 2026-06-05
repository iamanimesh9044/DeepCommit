"""Commit-level DAG prototype builder.

This module provides a CLI and helpers to build a commit-level dependency DAG
from a given commit list within a Git repo, using:

- Line-level blame edges (who last changed the old-side lines you modify)
- Container edges for pure additions (e.g., class-level provider for new methods)
- File-level patch-based edges (via bidirectional cherry-pick conflict detection)

It is intentionally lightweight and uses Git CLI commands via subprocess.
"""

from .commit_dag import build_commit_dag, main

__all__ = ["build_commit_dag", "main"]
