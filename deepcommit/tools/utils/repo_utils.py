"""
Repository-related utility functions for DeepCommit tools.

This module provides centralized functions for repository path resolution,
repo ID handling, and other common repository operations used across various tools.
"""

import os
from pathlib import Path
from typing import Optional

# Environment variables for default paths
DEFAULT_REPO_ENV_VAR = "DEFAULT_BARE_REPO_DIR"
DEFAULT_BARE_REPO_ROOT = os.getenv("DEEPCOMMIT_REPO_ROOT", os.getenv("AGENTBENCH_REPO_ROOT"))
DEFAULT_RELEASE_DATA_ROOT = os.getenv("DEEPCOMMIT_PREPROCESSED_ROOT", os.getenv("AGENTBENCH_PREPROCESSED_ROOT"))


def sanitize_repo_id(repo_id: str) -> str:
    """Normalize repo identifier to match local directory names."""
    return repo_id.strip().strip("/").replace("/", "_")


def resolve_repo_id(repo_id: Optional[str] = None) -> str:
    """Return the repo identifier, preferring explicit input then env override."""
    if repo_id:
        return repo_id

    env_repo_id = os.getenv(DEFAULT_REPO_ENV_VAR)
    if not env_repo_id:
        raise RuntimeError(
            f"Repository identifier not provided. Set {DEFAULT_REPO_ENV_VAR} or pass repo_id explicitly."
        )

    return env_repo_id


def resolve_repo_path(repo_id: str) -> Optional[str]:
    """Resolve the local repository path for the given repo identifier."""
    candidates = []

    repo_path = Path(repo_id).expanduser()
    if repo_path.exists():
        candidates.append(repo_path)
    elif repo_path.with_suffix(".git").exists():
        candidates.append(repo_path.with_suffix(".git"))

    if DEFAULT_BARE_REPO_ROOT:
        root = Path(DEFAULT_BARE_REPO_ROOT).expanduser()
        sanitized = sanitize_repo_id(repo_id)
        for suffix in ("", ".git"):
            candidate = root / f"{sanitized}{suffix}"
            if candidate.exists():
                candidates.append(candidate)

    # Prefer the most specific (deepest) candidate path.
    for candidate in sorted({c.resolve() for c in candidates if c.exists()}, key=lambda p: (-len(str(p)), str(p))):
        return str(candidate)
    return None


def resolve_data_dir(repo_id: str) -> Optional[str]:
    """Resolve the release/issue data directory for the given repo identifier."""
    candidates = []

    repo_path = Path(repo_id).expanduser()
    if repo_path.exists():
        # Try sibling data directories
        parent = repo_path.parent
        potential = parent / "data"
        if potential.exists():
            sanitized = sanitize_repo_id(repo_id)
            for match in potential.glob(f"{sanitized}*"):
                if match.is_dir():
                    candidates.append(match)

    if DEFAULT_RELEASE_DATA_ROOT:
        root = Path(DEFAULT_RELEASE_DATA_ROOT).expanduser()
        if root.exists():
            sanitized = sanitize_repo_id(repo_id)
            for match in sorted(root.glob(f"{sanitized}*")):
                if match.is_dir():
                    candidates.append(match)

            # Bare repository paths typically end with <owner>_<repo>.git; capture that prefix.
            base_name = repo_path.stem if repo_path.suffix == ".git" else repo_path.name
            if base_name:
                for match in sorted(root.glob(f"{base_name}*")):
                    if match.is_dir():
                        candidates.append(match)

    for candidate in sorted({c.resolve() for c in candidates if c.exists()}, key=lambda p: (-len(str(p)), str(p))):
        return str(candidate)
    return None


def get_repo_path_from_env() -> str:
    """Get repository path from DEEPCOMMIT_REPO_ROOT environment variable."""
    repo_path = DEFAULT_BARE_REPO_ROOT
    if not repo_path:
        raise RuntimeError("Repository path not provided. Set DEEPCOMMIT_REPO_ROOT environment variable.")
    return repo_path


def resolve_repo_path_with_fallback(repo_path: Optional[str] = None) -> str:
    """
    Resolve repository path with fallback to environment variable.

    Args:
        repo_path: Optional explicit repository path

    Returns:
        Resolved repository path

    Raises:
        RuntimeError: If no repository path can be resolved
    """
    if repo_path:
        return repo_path

    return get_repo_path_from_env()
