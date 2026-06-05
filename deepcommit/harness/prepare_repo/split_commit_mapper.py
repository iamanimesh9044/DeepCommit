"""
Split Commit Mapper - Maps split commits to their original commits for proper timestamp ordering.

When commits are split during preprocessing, the split commits get new timestamps (the time of splitting),
which breaks chronological ordering when mixed with other commits that modify the same files.

This module provides utilities to:
1. Load split commit mappings from METADATA.json files
2. Get proper sort keys that use original commit timestamps for cross-commit comparison
3. Use split commit's own timestamp for ordering within the same original commit
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple


@dataclass
class SplitCommitInfo:
    """Information about a split commit."""

    original_hash: str
    original_short_hash: str
    split_hash: str
    split_short_hash: str
    number: int  # Order within the split (1, 2, 3, ...)


class SplitCommitMapper:
    """Handles mapping between split commits and their original commits."""

    def __init__(self, commit_splits_dir: Optional[Path] = None):
        """
        Initialize the mapper.

        Args:
            commit_splits_dir: Path to the commit_splits directory containing METADATA.json files.
                              If None, no mappings will be loaded.
        """
        self.commit_splits_dir = commit_splits_dir

        # Mapping: split_hash (short or full) -> SplitCommitInfo
        self.split_info: Dict[str, SplitCommitInfo] = {}

        # Cache for original commit timestamps (fetched from git)
        self._original_timestamps: Dict[str, int] = {}

        if commit_splits_dir and commit_splits_dir.exists():
            self._load_mappings()

    def _load_mappings(self):
        """Load all split commit mappings from METADATA.json files.

        Supports two METADATA.json formats:
        - Format v1 (baseline_rerun_stage4_001): split_result.src_commits with hash/short_hash/number
        - Format v2 (baseline_rerun_stage4_002): split_commits with sha/short_sha/sequence
        """
        if not self.commit_splits_dir:
            return

        for subdir in self.commit_splits_dir.iterdir():
            if not subdir.is_dir():
                continue

            metadata_file = subdir / "METADATA.json"
            if not metadata_file.exists():
                continue

            try:
                with open(metadata_file, "r") as f:
                    metadata = json.load(f)

                original = metadata.get("original_commit", {})
                # Support both "hash" (v1) and "sha" (v2) field names
                original_hash = original.get("hash", "") or original.get("sha", "")
                original_short = original.get("short_hash", "") or original.get("short_sha", "")

                # Try v1 format first: split_result.src_commits
                split_result = metadata.get("split_result", {})
                src_commits = split_result.get("src_commits", [])

                # Fall back to v2 format: split_commits
                if not src_commits:
                    src_commits = metadata.get("split_commits", [])

                for commit_info in src_commits:
                    # Support both field naming conventions
                    split_hash = commit_info.get("hash", "") or commit_info.get("sha", "")
                    split_short = commit_info.get("short_hash", "") or commit_info.get("short_sha", "")
                    number = commit_info.get("number", 0) or commit_info.get("sequence", 0)

                    info = SplitCommitInfo(
                        original_hash=original_hash,
                        original_short_hash=original_short,
                        split_hash=split_hash,
                        split_short_hash=split_short,
                        number=number,
                    )

                    # Store mappings for both full and short hashes
                    if split_hash:
                        self.split_info[split_hash] = info
                        self.split_info[split_hash[:10]] = info
                        self.split_info[split_hash[:7]] = info
                    if split_short:
                        self.split_info[split_short] = info

                # Also load integration_test_commit if present
                # This is a separate commit containing only test files from the original commit
                int_test_commit = split_result.get("integration_test_commit", {})
                if int_test_commit:
                    int_hash = int_test_commit.get("hash", "") or int_test_commit.get("sha", "")
                    int_short = int_test_commit.get("short_hash", "") or int_test_commit.get("short_sha", "")
                    int_number = int_test_commit.get("number", 0) or int_test_commit.get("sequence", 0)

                    if int_hash or int_short:
                        int_info = SplitCommitInfo(
                            original_hash=original_hash,
                            original_short_hash=original_short,
                            split_hash=int_hash,
                            split_short_hash=int_short,
                            number=int_number,
                        )

                        # Store mappings for both full and short hashes
                        if int_hash:
                            self.split_info[int_hash] = int_info
                            self.split_info[int_hash[:10]] = int_info
                            self.split_info[int_hash[:7]] = int_info
                        if int_short:
                            self.split_info[int_short] = int_info

            except (json.JSONDecodeError, KeyError) as e:
                print(f"   Warning: Failed to parse {metadata_file}: {e}")

    def is_split_commit(self, commit_hash: str) -> bool:
        """Check if a commit is a split commit."""
        return (
            commit_hash in self.split_info or commit_hash[:10] in self.split_info or commit_hash[:7] in self.split_info
        )

    def get_split_info(self, commit_hash: str) -> Optional[SplitCommitInfo]:
        """Get split commit info for a commit hash."""
        # Try different hash lengths
        for hash_variant in [commit_hash, commit_hash[:10], commit_hash[:7]]:
            if hash_variant in self.split_info:
                return self.split_info[hash_variant]
        return None

    def get_original_hash(self, commit_hash: str) -> Optional[str]:
        """Get the original commit hash for a split commit."""
        info = self.get_split_info(commit_hash)
        return info.original_hash if info else None

    def get_sort_key(self, commit_hash: str, get_timestamp_func) -> Tuple[int, int]:
        """
        Get sorting key for a commit.

        The key is a tuple (primary_timestamp, secondary_key) where:
        - For regular commits: (commit_timestamp, 0)
        - For split commits: (original_commit_timestamp, split_number)

        This ensures:
        1. Split commits are ordered by their original commit's timestamp when compared with other commits
        2. Split commits from the same original are ordered by their split number

        Args:
            commit_hash: The commit hash to get sort key for
            get_timestamp_func: Function that takes a commit hash and returns its timestamp

        Returns:
            Tuple of (primary_timestamp, secondary_key)
        """
        info = self.get_split_info(commit_hash)

        if info:
            # This is a split commit
            # Use original commit's timestamp as primary key
            original_hash = info.original_hash
            if original_hash not in self._original_timestamps:
                self._original_timestamps[original_hash] = get_timestamp_func(original_hash)

            primary_timestamp = self._original_timestamps[original_hash]
            # Use split number as secondary key (for ordering within same original)
            secondary_key = info.number

            return (primary_timestamp, secondary_key)
        else:
            # Regular commit - use its own timestamp
            timestamp = get_timestamp_func(commit_hash)
            return (timestamp, 0)

    def get_stats(self) -> Dict:
        """Get statistics about loaded mappings."""
        # Count unique original commits
        original_hashes = set()
        for info in self.split_info.values():
            original_hashes.add(info.original_hash)

        return {
            "total_split_commits": len(set(info.split_hash for info in self.split_info.values())),
            "total_original_commits": len(original_hashes),
            "original_commits": list(original_hashes),
        }


def find_commit_splits_dir(trial_space_base: Path, repo_name: str) -> Optional[Path]:
    """
    Find the commit_splits directory for a given repository.

    Args:
        trial_space_base: Base path to trial_space directory
        repo_name: Repository name pattern (e.g., "apache_dubbo_dubbo-3.3.3_dubbo-3.3.6")

    Returns:
        Path to commit_splits directory, or None if not found
    """
    # Try to find matching trial directory
    for trial_dir in trial_space_base.iterdir():
        if not trial_dir.is_dir():
            continue
        if repo_name in trial_dir.name:
            commit_splits = trial_dir / "stage4_decomp" / "output" / "commit_splits"
            if commit_splits.exists():
                return commit_splits

    return None
