#!/usr/bin/env python3
"""
Collect patched tests for each milestone from commits.

This script collects test modifications (added/modified/deleted) for each milestone
by aggregating patched_tests data from individual commits.

If patched_tests data is missing for some commits, it will automatically extract
symbol changes using extract_symbol_changes.py.

Usage:
    python collect_milestone_patched_tests.py <work_dir> [--repo <repo_path>] [--milestone M001]

Example:
    # Process all milestones (auto-infer repo path)
    python collect_milestone_patched_tests.py \
        /path/to/DATA/harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_002_fix2

    # Process all milestones with explicit repo path
    python collect_milestone_patched_tests.py \
        /path/to/DATA/harness_workspace/navidrome_navidrome_v0.57.0_v0.58.0/baseline_004_v3 \
        --repo /path/to/DATA/github_data/repos/navidrome_navidrome

    # Process only a single milestone
    python collect_milestone_patched_tests.py \
        /path/to/DATA/harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_002_fix2 \
        --milestone M003
"""

import argparse
import csv
import json
import logging
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set

from deepcommit.harness.test_runner.core.classifier import CLASSIFICATION_CATEGORIES

logger = logging.getLogger(__name__)

# Import extract_symbol_changes for processing missing commits
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "preprocess"))
from extract_symbol_changes import _to_dict_with_test_ids, extract_changes_batch  # noqa: E402


def infer_testbed_path(work_dir: Path) -> Optional[Path]:
    """
    Infer the testbed path from work_dir.

    work_dir: .../harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_xxx
    testbed: work_dir/testbed
    """
    testbed_path = work_dir / "testbed"
    if testbed_path.exists():
        return testbed_path
    return None


def infer_repo_path(work_dir: Path) -> Optional[Path]:
    """
    Infer the original repo path from work_dir.

    work_dir: .../harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_xxx
    repo: .../github_data/repos/apache_dubbo

    The repo_id is extracted from the work_dir parent folder name (e.g., apache_dubbo_dubbo-3.3.3_dubbo-3.3.6)
    and split to get the repo name (apache_dubbo).
    """
    # Get the parent folder name (e.g., "apache_dubbo_dubbo-3.3.3_dubbo-3.3.6")
    parent_name = work_dir.parent.name

    # Extract repo_id by finding the pattern: {owner}_{repo}_{start_version}_{end_version}
    # We need to extract {owner}_{repo}
    parts = parent_name.split("_")
    if len(parts) >= 4:
        # Try to find the version pattern (vX.X.X or X.X.X)
        # The repo_id is everything before the first version-like part
        repo_parts = []
        for _i, part in enumerate(parts):
            # Check if this looks like a version (starts with 'v' followed by digit, or is a digit)
            if (part.startswith("v") and len(part) > 1 and part[1].isdigit()) or (part[0].isdigit() and "." in part):
                break
            repo_parts.append(part)

        if repo_parts:
            repo_id = "_".join(repo_parts)
            # Look for the repo in the standard location
            # work_dir: .../harness_workspace/xxx/baseline_xxx
            # repos: .../github_data/repos/
            harness_workspace = work_dir.parent.parent
            if harness_workspace.name == "harness_workspace":
                data_root = harness_workspace.parent
                repo_path = data_root / "github_data" / "repos" / repo_id
                if repo_path.exists():
                    return repo_path

    return None


def get_default_config_path() -> Path:
    """Get the default symbol extraction config path."""
    return Path(__file__).parent.parent.parent / "preprocess" / "configs" / "symbol_extraction_config.yml"


# ============================================
# DAG dependency and timestamp utilities
# ============================================


def load_dependencies_as_parents(work_dir: Path) -> Dict[str, List[str]]:
    """
    Load dependencies from dependencies.csv as parent mapping.

    In DAG: prerequisite_id -> dependant_id means target depends on source.
    So source is parent of target.

    Args:
        work_dir: Path to the work directory

    Returns:
        Dict mapping milestone_id to list of parent milestone IDs
        Example: {"M003.2": ["M003.1"], "M003.3": ["M003.2"]}
    """
    parents: Dict[str, List[str]] = defaultdict(list)
    dep_file = work_dir / "dependencies.csv"
    if not dep_file.exists():
        return dict(parents)

    with open(dep_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            src = row.get("prerequisite_id", "").strip()
            tgt = row.get("dependant_id", "").strip()
            if src and tgt:
                # source -> target means target depends on source
                # so source is parent of target
                parents[tgt].append(src)

    return dict(parents)


# ============================================
# Sub-milestone group utilities
# ============================================


def is_sub_milestone(milestone_id: str) -> bool:
    """
    Check if milestone is a sub-milestone.

    Supported patterns:
    1. 'M003.1', 'M003.2' - dot notation
    2. 'milestone_003_sub-01', 'milestone_003_sub-02' - sub- notation

    Returns:
        True if the milestone is a sub-milestone
    """
    # Pattern 1: contains '.'
    if "." in milestone_id:
        return True

    # Pattern 2: contains '_sub-' or '_sub_'
    if "_sub-" in milestone_id or "_sub_" in milestone_id:
        return True

    return False


def get_milestone_group(milestone_id: str) -> Optional[str]:
    """
    Extract group prefix from sub-milestone ID.

    Examples:
        'M003.1' -> 'M003'
        'M003.2' -> 'M003'
        'milestone_003_sub-01' -> 'milestone_003'
        'milestone_003_sub-02' -> 'milestone_003'
        'M001' -> None (not a sub-milestone)

    Returns:
        The group prefix, or None if not a sub-milestone
    """
    # Pattern 1: dot notation (e.g., 'M003.1' -> 'M003')
    if "." in milestone_id:
        return milestone_id.rsplit(".", 1)[0]

    # Pattern 2: _sub- or _sub_ notation (e.g., 'milestone_003_sub-01' -> 'milestone_003')
    match = re.match(r"^(.+?)_sub[-_].+$", milestone_id)
    if match:
        return match.group(1)

    return None


def get_group_milestones(
    group_prefix: str,
    milestones_dict: Dict[str, dict],
) -> List[str]:
    """
    Get all milestone IDs belonging to a group.

    Args:
        group_prefix: The group prefix (e.g., 'M003')
        milestones_dict: Dict mapping milestone_id to milestone row dict

    Returns:
        List of milestone IDs belonging to the group
    """
    return [mid for mid in milestones_dict.keys() if get_milestone_group(mid) == group_prefix]


def collect_group_itcs(
    group_milestone_ids: List[str],
    milestones_dict: Dict[str, dict],
) -> List[str]:
    """
    Collect all ITCs from milestones within the same group.

    ITCs don't inherit across groups - only within the same sub-milestone group.

    Args:
        group_milestone_ids: List of milestone IDs in the group
        milestones_dict: Dict mapping milestone_id to milestone row dict

    Returns:
        List of unique ITC hashes from the group
    """
    itcs = []
    for mid in group_milestone_ids:
        if mid in milestones_dict:
            itc = milestones_dict[mid].get("integration_test_commit", "").strip()
            if itc and itc not in itcs:
                itcs.append(itc)
    return itcs


def find_leaf_milestones_in_group(
    group_milestone_ids: List[str],
    parents_map: Dict[str, List[str]],
) -> List[str]:
    """
    Find leaf milestones (no children within the group) in a sub milestone group.

    In DAG: source -> target means target depends on source.
    Leaf nodes are nodes that no other node depends on (no outgoing edges).

    Args:
        group_milestone_ids: List of milestone IDs in the group
        parents_map: Dict mapping milestone_id to list of parent IDs

    Returns:
        List of leaf milestone IDs (nodes with no children in the group)
    """
    group_set = set(group_milestone_ids)

    # Build children map (reverse of parents_map)
    # parents_map: child -> [parents]
    # children_map: parent -> [children]
    children_map: Dict[str, List[str]] = defaultdict(list)
    for child, parents in parents_map.items():
        if child in group_set:
            for parent in parents:
                if parent in group_set:
                    children_map[parent].append(child)

    # Leaf nodes have no children within the group
    leaves = [mid for mid in group_milestone_ids if not children_map.get(mid)]
    return leaves


def get_all_inherited_itcs(
    milestone_id: str,
    milestones_dict: Dict[str, dict],
    parents_map: Dict[str, List[str]],
    visited: Optional[Set[str]] = None,
) -> List[str]:
    """
    Recursively collect all integration_test_commits from milestone and its ancestors.

    Args:
        milestone_id: The milestone ID to collect ITCs for
        milestones_dict: Dict mapping milestone_id to milestone row dict
        parents_map: Dict mapping milestone_id to list of parent IDs
        visited: Set of already visited milestone IDs (for cycle detection)

    Returns:
        List of unique integration_test_commit hashes
    """
    if visited is None:
        visited = set()

    if milestone_id in visited:
        return []
    visited.add(milestone_id)

    itcs = set()

    # Add this milestone's ITC if it has one
    if milestone_id in milestones_dict:
        itc = milestones_dict[milestone_id].get("integration_test_commit", "").strip()
        if itc:
            itcs.add(itc)

    # Recursively collect from parents
    for parent_id in parents_map.get(milestone_id, []):
        parent_itcs = get_all_inherited_itcs(parent_id, milestones_dict, parents_map, visited)
        itcs.update(parent_itcs)

    return list(itcs)


def get_commit_timestamp(commit: str, testbed_path: Path) -> int:
    """
    Get the commit timestamp from git.

    Args:
        commit: The commit hash
        testbed_path: Path to the git repository

    Returns:
        Unix timestamp (seconds since epoch)
    """
    import subprocess

    result = subprocess.run(
        ["git", "log", "-1", "--format=%ct", commit],
        cwd=testbed_path,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return int(result.stdout.strip())
    return 0


def get_original_timestamp(
    commit: str,
    testbed_path: Path,
    split_mapper: Optional["SplitCommitMapper"] = None,
    timestamp_cache: Optional[Dict[str, int]] = None,
    repo_path: Optional[Path] = None,
) -> int:
    """
    Get the original timestamp for a commit.

    For split commits: returns the original commit's timestamp from the original repo.
    For regular commits: returns the commit's own timestamp.

    Args:
        commit: The commit hash
        testbed_path: Path to the testbed git repository
        split_mapper: Optional SplitCommitMapper for split commit detection
        timestamp_cache: Optional cache for timestamps
        repo_path: Optional path to the original repo (for looking up original commit timestamps)

    Returns:
        Unix timestamp (seconds since epoch)
    """
    if timestamp_cache is None:
        timestamp_cache = {}

    # Check if this is a split commit
    if split_mapper and split_mapper.is_split_commit(commit):
        info = split_mapper.get_split_info(commit)
        if info and info.original_hash:
            original_hash = info.original_hash
            if original_hash not in timestamp_cache:
                # For split commits, look up original commit timestamp from the original repo
                # The original commit doesn't exist in testbed (it was split)
                lookup_path = repo_path if repo_path else testbed_path
                timestamp_cache[original_hash] = get_commit_timestamp(original_hash, lookup_path)
            return timestamp_cache[original_hash]

    # Regular commit or split mapper not available
    if commit not in timestamp_cache:
        timestamp_cache[commit] = get_commit_timestamp(commit, testbed_path)
    return timestamp_cache[commit]


def get_applicable_itcs(
    milestone_id: str,
    last_commit: str,
    milestones_dict: Dict[str, dict],
    parents_map: Dict[str, List[str]],
    testbed_path: Path,
    split_mapper: Optional["SplitCommitMapper"] = None,
    timestamp_cache: Optional[Dict[str, int]] = None,
    repo_path: Optional[Path] = None,
    verbose: bool = False,
) -> List[str]:
    """
    Get integration_test_commits that should be applied for a milestone.

    Follows the same logic as build_milestone_testbed.py:
    1. Recursively collect all ITCs from milestone and ancestors
    2. Filter by timestamp: only apply ITC if itc_original_ts >= last_commit_original_ts

    Args:
        milestone_id: The milestone ID
        last_commit: The milestone's last commit (sorted by original timestamp)
        milestones_dict: Dict mapping milestone_id to milestone row dict
        parents_map: Dict mapping milestone_id to list of parent IDs
        testbed_path: Path to the git repository
        split_mapper: Optional SplitCommitMapper for split commit handling
        timestamp_cache: Optional cache for timestamps
        repo_path: Optional path to the original repo (for looking up original commit timestamps)
        verbose: Whether to print debug info

    Returns:
        List of ITC hashes that should be applied (pass timestamp filter)
    """
    if timestamp_cache is None:
        timestamp_cache = {}

    # Get all inherited ITCs
    all_itcs = get_all_inherited_itcs(milestone_id, milestones_dict, parents_map)

    if not all_itcs:
        return []

    # Get last_commit's original timestamp
    last_commit_ts = get_original_timestamp(last_commit, testbed_path, split_mapper, timestamp_cache, repo_path)

    # Filter ITCs by timestamp
    applicable = []
    for itc in all_itcs:
        itc_ts = get_original_timestamp(itc, testbed_path, split_mapper, timestamp_cache, repo_path)

        # Apply if ITC's original timestamp >= last_commit's original timestamp
        if itc_ts >= last_commit_ts:
            applicable.append(itc)
            if verbose:
                print(f"    ITC {itc[:7]}: ts={itc_ts} >= {last_commit_ts} → apply")
        else:
            if verbose:
                print(f"    ITC {itc[:7]}: ts={itc_ts} < {last_commit_ts} → skip")

    return applicable


def get_applicable_itcs_for_sub_milestone(
    milestone_id: str,
    milestones_dict: Dict[str, dict],
    parents_map: Dict[str, List[str]],
    testbed_path: Path,
    split_mapper: Optional["SplitCommitMapper"] = None,
    timestamp_cache: Optional[Dict[str, int]] = None,
    repo_path: Optional[Path] = None,
    verbose: bool = False,
) -> List[str]:
    """
    Get applicable ITCs for a sub-milestone.

    Conditions for applying ITC (ALL must be satisfied):
    1. The milestone is a sub-milestone (dot or sub- notation)
    2. The group has at least one ITC
    3. The milestone is a leaf node in its group's DAG
    4. The milestone's last_commit_ts <= ITC_ts

    Args:
        milestone_id: The milestone ID
        milestones_dict: Dict mapping milestone_id to milestone row dict
        parents_map: Dict mapping milestone_id to list of parent IDs
        testbed_path: Path to the git repository
        split_mapper: Optional SplitCommitMapper for split commit handling
        timestamp_cache: Optional cache for timestamps
        repo_path: Optional path to the original repo (for looking up original commit timestamps)
        verbose: Whether to print debug info

    Returns:
        List of ITC hashes that should be applied
    """
    if timestamp_cache is None:
        timestamp_cache = {}

    # Condition 1: Must be a sub-milestone
    if not is_sub_milestone(milestone_id):
        return []

    group_prefix = get_milestone_group(milestone_id)
    if not group_prefix:
        return []

    group_milestone_ids = get_group_milestones(group_prefix, milestones_dict)

    # Condition 2: Group must have ITCs
    group_itcs = collect_group_itcs(group_milestone_ids, milestones_dict)
    if not group_itcs:
        return []  # No ITCs in group → skip this logic entirely

    # Condition 3: Must be a leaf node in the group's DAG
    leaves = find_leaf_milestones_in_group(group_milestone_ids, parents_map)
    if milestone_id not in leaves:
        if verbose:
            print(f"    {milestone_id}: not a leaf node in group {group_prefix} → no ITCs")
        return []

    # Get this milestone's last commit (by original timestamp)
    commits_str = milestones_dict.get(milestone_id, {}).get("commits", "")
    commits = [c.strip() for c in commits_str.split(";") if c.strip()]

    if not commits:
        return []

    last_commit = get_last_commit_sorted(commits, testbed_path, split_mapper, timestamp_cache, repo_path)

    if not last_commit:
        return []

    last_commit_ts = get_original_timestamp(last_commit, testbed_path, split_mapper, timestamp_cache, repo_path)

    # Condition 4: Filter ITCs by timestamp (last_commit_ts <= ITC_ts)
    applicable = []
    for itc in group_itcs:
        itc_ts = get_original_timestamp(itc, testbed_path, split_mapper, timestamp_cache, repo_path)

        if last_commit_ts <= itc_ts:
            applicable.append(itc)
            if verbose:
                print(f"    {milestone_id} (leaf): ts={last_commit_ts} <= itc_ts={itc_ts} → apply {itc[:7]}")
        elif verbose:
            print(f"    {milestone_id} (leaf): ts={last_commit_ts} > itc_ts={itc_ts} → skip {itc[:7]}")

    return applicable


def get_last_commit_sorted(
    commits: List[str],
    testbed_path: Path,
    split_mapper: Optional["SplitCommitMapper"] = None,
    timestamp_cache: Optional[Dict[str, int]] = None,
    repo_path: Optional[Path] = None,
) -> Optional[str]:
    """
    Get the last commit from a list, sorted by original timestamp.

    Args:
        commits: List of commit hashes
        testbed_path: Path to the git repository
        split_mapper: Optional SplitCommitMapper for split commit handling
        timestamp_cache: Optional cache for timestamps
        repo_path: Optional path to the original repo (for looking up original commit timestamps)

    Returns:
        The commit with the highest original timestamp, or None if empty
    """
    if not commits:
        return None

    if timestamp_cache is None:
        timestamp_cache = {}

    def get_sort_key(commit: str) -> tuple:
        ts = get_original_timestamp(commit, testbed_path, split_mapper, timestamp_cache, repo_path)
        # For split commits, use split number as secondary key
        split_num = 0
        if split_mapper and split_mapper.is_split_commit(commit):
            info = split_mapper.get_split_info(commit)
            if info:
                split_num = info.number
        return (ts, split_num)

    return max(commits, key=get_sort_key)


# Try to import SplitCommitMapper
# Add project root to path for imports
_project_root = Path(__file__).parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

try:
    from deepcommit.harness.prepare_repo.split_commit_mapper import SplitCommitMapper
except ImportError as e:
    print(f"Warning: Could not import SplitCommitMapper: {e}", file=sys.stderr)
    SplitCommitMapper = None  # type: ignore

try:
    from deepcommit.harness.utils.test_id_normalizer import TestIdNormalizer
except ImportError as e:
    print(f"Warning: Could not import TestIdNormalizer: {e}", file=sys.stderr)
    TestIdNormalizer = None  # type: ignore


def extract_commits_patched_tests(
    commits: List[str], testbed_path: Path, output_dir: Path, config_path: Path, verbose: bool = True
) -> int:
    """
    Extract symbol changes (patched tests) for commits.

    Args:
        commits: List of commit SHAs to process
        testbed_path: Path to the testbed repository
        output_dir: Directory to save results
        config_path: Path to symbol_extraction_config.yml
        verbose: Enable verbose output

    Returns:
        Number of commits successfully processed
    """
    if not commits:
        return 0

    if verbose:
        print(f"\nExtracting patched tests for {len(commits)} commits...")

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        results = extract_changes_batch(
            repo_path=str(testbed_path),
            commit_shas=commits,
            config_path=str(config_path),
            output_dir=None,  # We'll save manually with test_ids format
            include_patterns=None,  # Process all files to detect tests
            verbose=verbose,
            with_test_ids=True,
        )

        # Save results with test_ids format
        saved_count = 0
        for result in results:
            output_file = output_dir / f"{result.short_sha}_symbol_changes.json"
            with open(output_file, "w") as f:
                json.dump(_to_dict_with_test_ids(result), f, indent=2)
            saved_count += 1

        if verbose:
            print(f"  Saved {saved_count} symbol change files to {output_dir}")

        return saved_count

    except Exception as e:
        print(f"Error extracting symbol changes: {e}", file=sys.stderr)
        return 0


def _attempt_number(path: Path) -> int:
    try:
        return int(path.name.split("_", 1)[1])
    except (IndexError, ValueError):
        return -1


def _latest_refinement_classification(milestone_id: str, work_dir: Path) -> Optional[Path]:
    refinement_dir = work_dir / "test_results" / milestone_id / "refinement"
    attempt_dirs = sorted(
        (path for path in refinement_dir.glob("attempt_*") if path.is_dir()),
        key=_attempt_number,
    )
    for attempt_dir in reversed(attempt_dirs):
        classification_file = attempt_dir / "classification.json"
        if classification_file.exists():
            return classification_file
    return None


def load_classification(milestone_id: str, work_dir: Path) -> Optional[dict]:
    """
    Load classification data for patched-test intersection.

    Module 2 reads only the latest refinement artifact:
    work_dir/test_results/{milestone_id}/refinement/attempt_N/classification.json.

    Root benchmark final files
    work_dir/test_results/{milestone_id}/{milestone_id}_classification.json
    are intentionally not used here; those belong to post-loop F2P/SRS
    consumers, not Module 2 refinement.

    Returns:
        The classification data dict with keys:
        - summary: counts for each transition type
        - classification: full classification (including flaky)
        - stable_classification: classification excluding flaky tests
        - flaky_tests: list of flaky test info

        Returns None if file doesn't exist or can't be parsed.
    """
    classification_file = _latest_refinement_classification(milestone_id, work_dir)
    if classification_file is None or not classification_file.exists():
        return None

    try:
        with open(classification_file, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def get_collected_nodeids_from_classification(classification_data: dict) -> Set[str]:
    """
    Extract all collected nodeids from classification data.

    Collects nodeids from stable_classification when available; refinement
    attempt files fall back to their top-level category lists.
    """
    nodeids = set()
    stable = classification_data.get("stable_classification")
    if not isinstance(stable, dict) or not any(category in stable for category in CLASSIFICATION_CATEGORIES):
        fallback = classification_data.get("classification")
        if isinstance(fallback, dict):
            stable = fallback
        else:
            stable = classification_data

    for category in CLASSIFICATION_CATEGORIES:
        category_data = stable.get(category, [])
        # category_data should be a list of test_id strings
        if isinstance(category_data, list):
            for item in category_data:
                if isinstance(item, str):
                    nodeids.add(item)

    # Also include flaky tests (list of dicts with "test_id" key)
    for flaky_test in classification_data.get("flaky_tests", []):
        if isinstance(flaky_test, dict):
            test_id = flaky_test.get("test_id")
            if test_id:
                nodeids.add(test_id)

    return nodeids


def load_patched_tests(commit: str, work_dir: Path) -> Optional[dict]:
    """
    Load patched tests for a commit from work_dir/commit-level/patched_tests/.

    Uses short SHA (7 chars) for filename lookup.
    """
    short_sha = commit[:7]
    filename = f"{short_sha}_symbol_changes.json"

    patched_tests_path = work_dir / "commit-level" / "patched_tests" / filename
    if patched_tests_path.exists():
        with open(patched_tests_path, "r") as f:
            return json.load(f)

    return None


def normalize_nodeids(nodeids: Set[str], framework: str = "go_test") -> Dict[str, Set[str]]:
    """
    Normalize nodeids and return a mapping from normalized_id -> {original_nodeids}.

    This helps match test IDs with random subtest suffixes to their parent test.

    Example:
        Input: {"TestSafeMap/eJXQ3n3q", "TestSafeMap/VWyqLRLs", "TestOther"}
        Output: {
            "TestSafeMap": {"TestSafeMap/eJXQ3n3q", "TestSafeMap/VWyqLRLs"},
            "TestOther": {"TestOther"}
        }

    Args:
        nodeids: Set of test nodeids from classification
        framework: Test framework name (go_test, pytest, etc.)

    Returns:
        Dict mapping normalized ID to set of original nodeids
    """
    if TestIdNormalizer is None:
        # Fallback: 1:1 mapping when normalizer not available
        return {nid: {nid} for nid in nodeids}

    normalizer = TestIdNormalizer(framework=framework)
    normalized_map: Dict[str, Set[str]] = {}

    for nodeid in nodeids:
        norm_id = normalizer.normalize(nodeid)
        if norm_id not in normalized_map:
            normalized_map[norm_id] = set()
        normalized_map[norm_id].add(nodeid)

    return normalized_map


def find_matching_nodeid(
    test_id: str, collected_nodeids: Set[str], normalized_map: Optional[Dict[str, Set[str]]] = None
) -> Optional[str]:
    """
    Find a matching nodeid for a given test_id.

    Supports multiple matching strategies:
    1. Exact match: test_id == nodeid
    2. JUnit 5 parameterized test: nodeid with (ParamType)[index] suffix matches test_id
    3. Suffix match: nodeid ends with ::test_id (for Rust tests like regression::r3127_xxx)
    4. Suffix match: nodeid ends with .test_id (for Python tests like test_module.test_func)
    5. Contains match: test_id is contained in nodeid as a component
    6. Ginkgo hierarchical match: package + first container + leaf test name match
       (handles missing intermediate container levels)
    9. Normalized match: use normalized_map to match test_id with random subtest suffixes
       (e.g., TestSafeMap matches TestSafeMap/eJXQ3n3q)

    Args:
        test_id: The test ID from symbol extraction (e.g., "r3127_gitignore_allow_unclosed_class")
        collected_nodeids: Set of nodeids from test results (e.g., "regression::r3127_gitignore_allow_unclosed_class")
        normalized_map: Optional pre-computed normalized mapping from normalize_nodeids()

    Returns:
        The matching nodeid if found, None otherwise
    """
    # Strategy 1: Exact match
    if test_id in collected_nodeids:
        return test_id

    # Strategy 1a: Match with /testbed/ prefix
    # Handles cases where nodeid has /testbed/ prefix from Docker container:
    #   test_id:  "test/unit-tests/foo-test.tsx::Test > should work"
    #   nodeid:   "/testbed/test/unit-tests/foo-test.tsx::Test > should work"
    testbed_prefixed = f"/testbed/{test_id}"
    if testbed_prefixed in collected_nodeids:
        return testbed_prefixed

    # Strategy 1c: Jest hierarchy separator normalization
    # Handles cases where test results use space instead of " > " as hierarchy separator:
    #   test_id:  "test/unit-tests/foo-test.tsx::describe > it > test name"
    #   nodeid:   "/testbed/test/unit-tests/foo-test.tsx::describe it test name"
    # Convert " > " to space and try matching with/without /testbed/ prefix
    if " > " in test_id:
        space_separated = test_id.replace(" > ", " ")
        if space_separated in collected_nodeids:
            return space_separated
        testbed_space_separated = f"/testbed/{space_separated}"
        if testbed_space_separated in collected_nodeids:
            return testbed_space_separated

    # Strategy 1b: Pytest parametrized test prefix match
    # Handles cases where test_id is the base test name and nodeid has parametrization suffix
    # Example:
    #   test_id:  "sklearn/tree/tests/test_tree.py::test_missing_value_is_predictive"
    #   nodeid:   "sklearn/tree/tests/test_tree.py::test_missing_value_is_predictive[42-DecisionTreeClassifier-0.85]"
    # Match if nodeid starts with test_id followed by '[' (parametrization indicator)
    for nodeid in collected_nodeids:
        if nodeid.startswith(test_id + "["):
            return nodeid

    # Strategy 2: JUnit 5 parameterized test match
    # Handles cases where nodeid has parameterized test suffix:
    #   test_id:  "module::pkg.Class::methodName"
    #   nodeid:   "module::pkg.Class::methodName(ParamType)[1]"
    # Pattern matches: (ParamType)[params], (arg1, arg2), [any-params]
    parameterized_pattern = re.compile(r"(\([^)]*\))?(\[[^\]]*\])?$")
    for nodeid in collected_nodeids:
        # Strip parameterized suffix and compare
        base_nodeid = parameterized_pattern.sub("", nodeid)
        if base_nodeid == test_id:
            return nodeid

    # Strategy 3 & 4: Suffix match with :: or .
    for nodeid in collected_nodeids:
        # Rust-style: module::test_name
        if nodeid.endswith(f"::{test_id}"):
            return nodeid
        # Python-style: module.test_name or class.test_name
        if nodeid.endswith(f".{test_id}"):
            return nodeid

    # Strategy 4: Check if test_id matches the last component of nodeid
    for nodeid in collected_nodeids:
        # Split by common separators and check the last part
        # Handle patterns like: module::submodule::test_name, path/to/test::test_name
        parts = nodeid.replace("::", "/").replace(".", "/").split("/")
        if parts and parts[-1] == test_id:
            return nodeid

    # Strategy 4b: Pytest class-prefix insertion match
    # Handles cases where a Python git symbol extractor produces a module-level
    # test_id (only the function name is captured) but the pytest nodeid
    # includes the enclosing class between the file and the leaf:
    #   test_id:  "tests/forms_tests/widget_tests/test_colorinput.py::test_render"
    #   nodeid:   "tests/forms_tests/widget_tests/test_colorinput.py::ColorInputTest::test_render"
    if "::" in test_id:
        file_part, _, test_name = test_id.rpartition("::")
        if file_part and test_name and "::" not in test_name:
            file_prefix = file_part + "::"
            leaf_suffix = "::" + test_name
            candidates = [
                n for n in collected_nodeids if n.startswith(file_prefix) and n.endswith(leaf_suffix) and n != test_id
            ]
            if len(candidates) == 1:
                return candidates[0]
            if candidates:
                return min(candidates, key=len)

    # Strategy 5: Ginkgo hierarchical match
    # Handles cases where test_id is missing intermediate container levels
    # Example:
    #   test_id:  "pkg::TopLevel > LeafTest"
    #   nodeid:   "pkg::TopLevel > Middle1 > Middle2 > LeafTest"
    # We match if:
    #   - Package prefix matches (before ::)
    #   - First container after :: matches
    #   - Leaf test name (last " > " segment) matches
    if "::" in test_id and " > " in test_id:
        test_package, test_hierarchy = test_id.split("::", 1)
        test_parts = [p.strip() for p in test_hierarchy.split(" > ")]
        if len(test_parts) >= 2:
            test_first_container = test_parts[0]
            test_leaf = test_parts[-1]

            for nodeid in collected_nodeids:
                if "::" not in nodeid or " > " not in nodeid:
                    continue

                node_package, node_hierarchy = nodeid.split("::", 1)

                # Package must match exactly
                if node_package != test_package:
                    continue

                node_parts = [p.strip() for p in node_hierarchy.split(" > ")]
                if len(node_parts) < 2:
                    continue

                node_first_container = node_parts[0]
                node_leaf = node_parts[-1]

                # First container and leaf must match
                if node_first_container == test_first_container and node_leaf == test_leaf:
                    return nodeid

    # Strategy 6: Ginkgo relaxed match - package + leaf only
    # For cases where even the first container name differs
    # Example:
    #   test_id:  "pkg::Error Handling - Multi-Library > should preserve error"
    #   nodeid:   "pkg::Scanner - Multi-Library > Error Handling - Multi-Library > ... > should preserve error"
    if "::" in test_id and " > " in test_id:
        test_package, test_hierarchy = test_id.split("::", 1)
        test_parts = [p.strip() for p in test_hierarchy.split(" > ")]
        test_leaf = test_parts[-1]

        # Also extract any intermediate container names from test_id
        test_containers = set(test_parts[:-1])  # All parts except leaf

        candidates = []
        for nodeid in collected_nodeids:
            if "::" not in nodeid or " > " not in nodeid:
                continue

            node_package, node_hierarchy = nodeid.split("::", 1)

            # Package must match exactly
            if node_package != test_package:
                continue

            node_parts = [p.strip() for p in node_hierarchy.split(" > ")]
            node_leaf = node_parts[-1]

            # Leaf must match exactly
            if node_leaf != test_leaf:
                continue

            # Check if all test containers appear somewhere in nodeid hierarchy
            node_containers = set(node_parts[:-1])
            if test_containers.issubset(node_containers):
                candidates.append(nodeid)

        # If exactly one candidate, return it
        if len(candidates) == 1:
            return candidates[0]
        # If multiple candidates, prefer the shortest one (closest match)
        if candidates:
            return min(candidates, key=len)

    # Strategy 7: Handle truncated test IDs (ending with backslash from quote escaping issues)
    # Example:
    #   test_id:  "pkg::helpers > returns \"  (truncated due to quote issue)
    #   nodeid:   "pkg::helpers > returns "clean" when..."
    if test_id.endswith("\\"):
        prefix = test_id.rstrip("\\")
        if "::" in prefix and " > " in prefix:
            test_package = prefix.split("::")[0]
            candidates = []
            for nodeid in collected_nodeids:
                if "::" not in nodeid:
                    continue
                node_package = nodeid.split("::")[0]
                if node_package == test_package and nodeid.startswith(prefix):
                    candidates.append(nodeid)

            # Only match if there's exactly one candidate (to avoid ambiguity)
            if len(candidates) == 1:
                return candidates[0]

    # Strategy 8: Ginkgo short format match - package suffix + test leaf name
    # For test_ids extracted with short format like "persistence::It::should allow..."
    # Match against full nodeids like "github.com/navidrome/navidrome/persistence::... > should allow..."
    # Example:
    #   test_id:  "persistence::It::should allow headless processes to apply explicit library_id filters"
    #   nodeid:   "github.com/navidrome/navidrome/persistence::ArtistRepository > ... > should allow headless processes to apply explicit library_id filters"
    if "::" in test_id:
        parts = test_id.split("::")
        if len(parts) >= 2:
            # Extract package suffix (e.g., "persistence" from "persistence::It::...")
            test_package_suffix = parts[0].split("/")[-1] if "/" in parts[0] else parts[0]
            # Extract test leaf name (last part after any :: or >)
            test_leaf_raw = parts[-1]
            if " > " in test_leaf_raw:
                test_leaf = test_leaf_raw.split(" > ")[-1].strip()
            else:
                test_leaf = test_leaf_raw.strip()

            if test_leaf:
                candidates = []
                for nodeid in collected_nodeids:
                    if "::" not in nodeid:
                        continue

                    node_package = nodeid.split("::")[0]
                    node_package_suffix = node_package.split("/")[-1] if "/" in node_package else node_package

                    # Package suffix must match
                    if node_package_suffix != test_package_suffix:
                        continue

                    # Extract nodeid leaf (last part after >)
                    node_rest = nodeid.split("::", 1)[1] if "::" in nodeid else nodeid
                    if " > " in node_rest:
                        node_leaf = node_rest.split(" > ")[-1].strip()
                    else:
                        node_leaf = node_rest.strip()

                    # Test leaf must match exactly
                    if node_leaf == test_leaf:
                        candidates.append(nodeid)

                # If exactly one candidate, return it
                if len(candidates) == 1:
                    return candidates[0]
                # If multiple candidates, prefer the shortest one (closest match)
                # But only if test_leaf is long enough to be unique (avoid matching generic names)
                if candidates and len(test_leaf) > 20:
                    return min(candidates, key=len)

    # Strategy 9: Normalized match for parameterized tests with random IDs
    # Uses pre-computed normalized_map to match test_id with subtests that have random suffixes
    # Example:
    #   test_id:     "github.com/zeromicro/go-zero/core/collection/TestSafeMap"
    #   nodeid:      "github.com/zeromicro/go-zero/core/collection/TestSafeMap/eJXQ3n3q"
    #   normalized:  "github.com/zeromicro/go-zero/core/collection/TestSafeMap" -> {"...TestSafeMap/eJXQ3n3q", "...TestSafeMap/VWyqLRLs"}
    if normalized_map:
        # Case 1: test_id directly matches a normalized key
        if test_id in normalized_map:
            # Return the first (or any) matching nodeid from the group
            return next(iter(normalized_map[test_id]))

        # Case 2: test_id itself needs normalization before matching
        if TestIdNormalizer is not None:
            normalizer = TestIdNormalizer(framework="go_test")
            norm_test_id = normalizer.normalize(test_id)
            if norm_test_id != test_id and norm_test_id in normalized_map:
                return next(iter(normalized_map[norm_test_id]))

        # Case 3: Try suffix matching with normalized keys
        # This handles cases where test_id is a short name like "TestSafeMap"
        # and normalized_map has full paths like "github.com/.../TestSafeMap"
        for norm_key, original_nodeids in normalized_map.items():
            # Check if norm_key ends with /test_id or ::test_id
            if norm_key.endswith(f"/{test_id}") or norm_key.endswith(f"::{test_id}"):
                return next(iter(original_nodeids))
            # Check if the last component matches
            parts = norm_key.replace("::", "/").split("/")
            if parts and parts[-1] == test_id:
                return next(iter(original_nodeids))

    # Strategy 10: Jest template string placeholder matching
    # Handles cases where test_id contains ${varName} or %s/%d/etc. placeholders from .each():
    #   test_id:  "file.tsx::Describe > For filter: %s > should work: ${name}"
    #   nodeid:   "file.tsx::Describe > For filter: Mentions > should work: Unread"
    # Convert placeholders to regex patterns and match against collected nodeids
    if "${" in test_id or re.search(r"%[sd]", test_id):
        match = find_jest_template_match(test_id, collected_nodeids)
        if match:
            return match

    # Strategy 11: Playwright E2E test matching
    # Handles format differences between static extraction and Playwright JSON reporter:
    #   Static:  "playwright/e2e/right-panel/right-panel.spec.ts::should handle reporting a room"
    #   Runtime: "right-panel/right-panel.spec.ts::right-panel/right-panel.spec.ts > RightPanel > ... > should handle reporting a room [Chrome]"
    # Key differences:
    #   - Path prefix: Static has "playwright/e2e/", runtime doesn't
    #   - Describe hierarchy: Static only has test name, runtime has full describe() blocks
    #   - Browser suffix: Runtime has [Chrome], [Firefox], [WebKit]
    match = find_playwright_match(test_id, collected_nodeids)
    if match:
        return match

    return None


def find_playwright_match(test_id: str, collected_nodeids: Set[str]) -> Optional[str]:
    """
    Find a matching nodeid for Playwright E2E test IDs.

    Handles the format difference between static extraction and Playwright JSON reporter:
    - Static:  "playwright/e2e/path/file.spec.ts::test name"
    - Runtime: "path/file.spec.ts::path/file.spec.ts > describe > ... > test name [Browser]"

    Args:
        test_id: Test ID from static extraction
        collected_nodeids: Set of nodeids from test results

    Returns:
        Matching nodeid if found, None otherwise
    """
    if "::" not in test_id:
        return None

    file_path, test_name = test_id.split("::", 1)

    # Check if this looks like a Playwright test
    # Patterns: playwright/e2e/..., *.spec.ts, *.spec.js
    is_playwright_path = (
        "playwright/e2e/" in file_path or file_path.endswith(".spec.ts") or file_path.endswith(".spec.js")
    )

    if not is_playwright_path:
        return None

    # Normalize file path: remove playwright/e2e/ prefix
    normalized_path = file_path
    for prefix in ["playwright/e2e/", "playwright/"]:
        if normalized_path.startswith(prefix):
            normalized_path = normalized_path[len(prefix) :]
            break

    # Extract the leaf test name (in case static extraction includes some hierarchy)
    leaf_test_name = test_name.split(" > ")[-1].strip() if " > " in test_name else test_name.strip()

    # Browser suffixes to strip
    browser_suffixes = [" [Chrome]", " [Firefox]", " [WebKit]", " [chromium]", " [firefox]", " [webkit]"]

    candidates = []
    for nodeid in collected_nodeids:
        if "::" not in nodeid:
            continue

        node_file_path, node_hierarchy = nodeid.split("::", 1)

        # Check if file paths match (ignoring prefix differences)
        # Also check if one ends with the other (handles /testbed/ prefix)
        path_match = (
            node_file_path == normalized_path
            or node_file_path.endswith(f"/{normalized_path}")
            or normalized_path.endswith(f"/{node_file_path}")
            or node_file_path.endswith(normalized_path)
        )

        if not path_match:
            continue

        # Strip browser suffix from nodeid hierarchy
        node_test_part = node_hierarchy
        for suffix in browser_suffixes:
            if node_test_part.endswith(suffix):
                node_test_part = node_test_part[: -len(suffix)]
                break

        # Extract leaf test name from hierarchy
        # Format: "file.spec.ts > Describe > ... > test name"
        node_leaf_name = node_test_part.split(" > ")[-1].strip() if " > " in node_test_part else node_test_part.strip()

        # Match if leaf test names are equal
        if node_leaf_name == leaf_test_name:
            candidates.append(nodeid)

    # Return the first candidate (or shortest if multiple)
    if len(candidates) == 1:
        return candidates[0]
    elif candidates:
        # Prefer shortest match (most specific)
        return min(candidates, key=len)


def _convert_template_to_regex(segment: str) -> str:
    """
    Convert a test name segment with template placeholders to a regex pattern.

    Handles:
    - ${varName} -> [^>]+? (match any chars except hierarchy separator)
    - %s, %d, %i, %f, %j, %o, %# -> [^>]+? (Jest printf-style placeholders)

    Example:
        "should work: ${name}" -> "should work: [^>]+?"
        "For filter: %s" -> "For filter: [^>]+?"
    """
    # Pattern to match all placeholder types
    placeholder_pattern = re.compile(r"(\$\{[^}]+\}|%[sdijfo#])")

    # Split by placeholders, keeping the delimiters
    parts = placeholder_pattern.split(segment)

    result_parts = []
    for part in parts:
        if placeholder_pattern.match(part):
            # This is a placeholder - replace with wildcard
            result_parts.append(r"[^>]+?")
        else:
            # This is literal text - escape for regex
            result_parts.append(re.escape(part))

    return "".join(result_parts)


def find_jest_template_match(test_id: str, collected_nodeids: Set[str]) -> Optional[str]:
    """
    Find a matching nodeid for a test_id containing Jest template placeholders.

    Handles two types of placeholders:
    1. ${varName} - JavaScript template literal placeholders
    2. %s, %d, %i, %f, %j, %o, %# - Jest .each() printf-style placeholders

    Args:
        test_id: Test ID with placeholders (e.g., "Describe > For %s > test ${name}")
        collected_nodeids: Set of actual nodeids from test results

    Returns:
        First matching nodeid, or None if no match found
    """
    # Build regex pattern from test_id
    # 1. Escape all regex special characters in the literal parts
    # 2. Replace placeholders with regex patterns

    # Split test_id into file path and test hierarchy
    if "::" in test_id:
        file_part, hierarchy_part = test_id.split("::", 1)
    else:
        file_part = ""
        hierarchy_part = test_id

    # Process the hierarchy part to build a flexible regex pattern
    # Split by " > " to get individual segments
    segments = hierarchy_part.split(" > ")

    # Build pattern for each segment
    segment_patterns = []
    for segment in segments:
        # Check if this segment contains placeholders
        has_placeholder = "${" in segment or re.search(r"%[sdijfo#]", segment)

        if has_placeholder:
            # Convert placeholders to regex pattern
            # Strategy: Split by placeholders, escape literal parts, join with wildcard patterns
            pattern = _convert_template_to_regex(segment)
            segment_patterns.append(("placeholder", pattern))
        else:
            # Literal segment - escape for regex
            segment_patterns.append(("literal", re.escape(segment)))

    # Build the full pattern
    # For placeholder segments, we allow flexible matching
    # For literal segments, we require exact match

    # Strategy: Match segments that allow extra " > X > " segments in between
    # This handles cases where describe.each adds intermediate container names
    pattern_parts = []
    for seg_type, pattern in segment_patterns:
        if seg_type == "placeholder":
            # For placeholder segments, match the pattern
            pattern_parts.append(f"(?:{pattern})")
        else:
            # For literal segments, require exact match
            pattern_parts.append(pattern)

    # Try two separator styles:
    # 1. " > " separator (extracted test_id style)
    # 2. " " separator (Jest reporter style - actual nodeids use space)
    separators_to_try = [
        r" > (?:[^>]+ > )*?",  # Flexible " > " separator
        r" ",  # Simple space separator (Jest reporter format)
    ]

    # Add file path pattern if present
    if file_part:
        # Allow /testbed/ prefix and normalize path
        file_pattern = r"(?:/testbed/)?" + re.escape(file_part)
    else:
        file_pattern = None

    for separator in separators_to_try:
        hierarchy_pattern = separator.join(pattern_parts)

        if file_pattern:
            full_pattern = file_pattern + r"::" + hierarchy_pattern
        else:
            full_pattern = hierarchy_pattern

        # Compile and match
        try:
            regex = re.compile(full_pattern)
            matches = []
            for nodeid in collected_nodeids:
                if regex.search(nodeid):
                    matches.append(nodeid)

            if matches:
                # Return the shortest match (most specific)
                return min(matches, key=len)
        except re.error:
            # If regex compilation fails, try next separator
            continue

    return None


def build_test_id_mapping(
    all_patched: Set[str], collected_nodeids: Set[str], normalize: bool = False, framework: str = "go_test"
) -> dict:
    """
    Build a mapping from test_ids to their matching nodeids.

    Args:
        all_patched: Set of patched test IDs from symbol extraction
        collected_nodeids: Set of nodeids from test classification
        normalize: Whether to use normalized matching for parameterized tests
        framework: Test framework name (go_test, pytest, etc.)

    Returns:
        {
            "matched": {test_id: nodeid, ...},
            "unmatched": set of test_ids with no match
        }
    """
    # Pre-compute normalized map if normalization is enabled
    normalized_map = None
    if normalize:
        normalized_map = normalize_nodeids(collected_nodeids, framework)

    matched = {}
    unmatched = set()

    for test_id in all_patched:
        nodeid = find_matching_nodeid(test_id, collected_nodeids, normalized_map)
        if nodeid:
            matched[test_id] = nodeid
        else:
            unmatched.add(test_id)

    return {"matched": matched, "unmatched": unmatched}


def collect_milestone_tests(
    milestone_id: str,
    commits: list[str],
    work_dir: Path,
    normalize_test_ids: bool = False,
    framework: str = "go_test",
    layer: str = "both",
) -> dict:
    """
    Collect all patched tests for a milestone from its commits.

    Two-layer pipeline:
      - Layer 1 (commit diff aggregation): builds ``test_ids`` (added /
        modified / deleted / effective) from per-commit ``patched_tests``
        files. Pure git-history computation, no test-results dependency.
      - Layer 2 (classification intersection): reads the latest
        ``test_results/{mid}/refinement/attempt_N/classification.json`` and
        intersects ``effective`` with collected nodeids → populates
        ``collected`` + per-category ``stable_classification``.

    The two layers exist because Layer 2 reflects "the test outcome on the
    most recent agent attempt" and goes stale whenever tests re-run, while
    Layer 1 only changes when commits/dependencies change. Run them
    independently to avoid recomputing Layer 1 inside per-milestone retry
    loops.

    ``layer`` accepts:
      - ``"both"`` (default, backward-compatible): full pipeline.
      - ``"1"``: only Layer 1; ``collected`` returns the empty placeholder.
        Use this once per scenario at the start of Module 2.
      - ``"2"``: only Layer 2; assumes Layer 1 already ran (per-commit
        ``patched_tests`` files exist). Caller must have invoked
        ``--layer 1`` (or ``both``) earlier. Use this after a milestone's
        new tests have run, to refresh just that milestone's intersection.
    """
    result = {
        "milestone_id": milestone_id,
        "commits": commits,
        "commits_found": [],
        "commits_not_found": [],
        "test_ids": {"added": set(), "modified": set(), "deleted": set()},
    }

    for commit in commits:
        patched_tests = load_patched_tests(commit, work_dir)

        if patched_tests is None:
            result["commits_not_found"].append(commit)
            continue

        result["commits_found"].append(commit)

        # Aggregate to milestone level
        commit_test_ids = patched_tests.get("test_ids", {})
        for test_id in commit_test_ids.get("added", []):
            result["test_ids"]["added"].add(test_id)
        for test_id in commit_test_ids.get("modified", []):
            result["test_ids"]["modified"].add(test_id)
        for test_id in commit_test_ids.get("deleted", []):
            result["test_ids"]["deleted"].add(test_id)

    # Calculate effective_test_ids: deduplicated (added ∪ modified) - deleted
    # This excludes tests that appear in deleted (e.g., renamed tests where old name is in deleted)
    effective_test_ids = (result["test_ids"]["added"] | result["test_ids"]["modified"]) - result["test_ids"]["deleted"]

    # Convert sets to sorted lists for JSON serialization
    result["test_ids"]["added"] = sorted(result["test_ids"]["added"])
    result["test_ids"]["modified"] = sorted(result["test_ids"]["modified"])
    result["test_ids"]["deleted"] = sorted(result["test_ids"]["deleted"])
    result["test_ids"]["effective"] = sorted(effective_test_ids)

    # Layer 1 short-circuit: caller asked for aggregation only.
    # Critically, MUST preserve any pre-existing Layer-2 fields written by a
    # prior run (collected.*, stable_classification, flaky_tests). Otherwise
    # running Layer 1 at scenario entry would clobber valid classification
    # data that's still fresh, defeating the whole "don't disturb Layer 2"
    # design. We only refresh the L1-owned slots: test_ids and the L1
    # subset of summary.
    output_path = work_dir / "milestone_patched_tests" / f"{milestone_id}.json"
    prior_result: Optional[dict] = None
    if output_path.exists():
        try:
            prior = json.loads(output_path.read_text())
            if isinstance(prior, dict):
                prior_result = prior
        except (json.JSONDecodeError, OSError):
            prior_result = None

    prior_test_ids = prior_result.get("test_ids") if prior_result else None
    prior_effective = set()
    if isinstance(prior_test_ids, dict):
        prior_effective = set(prior_test_ids.get("effective", []) or [])
    if not prior_effective and prior_result and isinstance(prior_result.get("collected"), dict):
        prior_collected_tests = prior_result["collected"]
        prior_effective = set(prior_collected_tests.get("patched_in_results", []) or [])
        prior_effective.update(prior_collected_tests.get("patched_not_in_results", []) or [])

    current_effective = set(result["test_ids"].get("effective", []) or [])
    should_use_prior_layer1 = (layer == "2" and prior_effective) or (
        layer == "1" and not current_effective and prior_effective
    )
    if should_use_prior_layer1 and isinstance(prior_test_ids, dict):
        # A scenario-level Layer-1 refresh should not erase an existing
        # non-empty Layer-1 artifact just because symbol extraction produced
        # empty per-commit test_ids this time. Layer 2 depends on the
        # previously established Layer-1 effective set instead of recomputing
        # it during the per-attempt classification refresh.
        result["test_ids"] = {
            "added": sorted(prior_test_ids.get("added", []) or []),
            "modified": sorted(prior_test_ids.get("modified", []) or []),
            "deleted": sorted(prior_test_ids.get("deleted", []) or []),
            "effective": sorted(prior_effective),
        }
        effective_test_ids = prior_effective

    prior_collected: dict = {
        "patched_in_results": [],
        "patched_not_in_results": sorted(effective_test_ids),
        "test_id_to_nodeid": {},
        "stable_classification": {},
        "flaky_tests": [],
    }
    prior_layer2_summary: dict = {
        "patched_collected": 0,
        "patched_not_collected": len(effective_test_ids),
        "stable_classification": {},
        "flaky_tests": 0,
    }
    if layer == "1" and prior_result:
        if isinstance(prior_result.get("collected"), dict):
            prior_collected = prior_result["collected"]
        for k in ("patched_collected", "patched_not_collected", "stable_classification", "flaky_tests"):
            if k in prior_result.get("summary", {}):
                prior_layer2_summary[k] = prior_result["summary"][k]

    if layer == "1":
        result["collected"] = prior_collected
        result["summary"] = {
            "total_commits": len(commits),
            "commits_found": len(result["commits_found"]),
            "commits_not_found": len(result["commits_not_found"]),
            "tests_added": len(result["test_ids"]["added"]),
            "tests_modified": len(result["test_ids"]["modified"]),
            "tests_deleted": len(result["test_ids"]["deleted"]),
            "effective_tests": len(result["test_ids"]["effective"]),
            **prior_layer2_summary,
            "layer_1_refreshed": True,
        }
        return result

    # Load classification data from the latest Module 2 refinement attempt.
    classification_data = load_classification(milestone_id, work_dir)

    if classification_data is None:
        # No classification data available
        result["collected"] = {
            "patched_in_results": [],
            "patched_not_in_results": sorted(effective_test_ids),
            "test_id_to_nodeid": {},
            "stable_classification": {},
            "flaky_tests": [],
        }
        result["summary"] = {
            "total_commits": len(commits),
            "commits_found": len(result["commits_found"]),
            "commits_not_found": len(result["commits_not_found"]),
            "tests_added": len(result["test_ids"]["added"]),
            "tests_modified": len(result["test_ids"]["modified"]),
            "tests_deleted": len(result["test_ids"]["deleted"]),
            "effective_tests": len(result["test_ids"]["effective"]),
            "patched_collected": 0,
            "patched_not_collected": len(effective_test_ids),
            "stable_classification": {},
            "flaky_tests": 0,
        }
        return result

    # Get all collected nodeids from classification
    collected_tests = get_collected_nodeids_from_classification(classification_data)

    # Build mapping from test_ids to matching nodeids
    mapping = build_test_id_mapping(
        effective_test_ids, collected_tests, normalize=normalize_test_ids, framework=framework
    )
    test_id_to_nodeid = mapping["matched"]  # {test_id: nodeid}
    patched_collected = set(test_id_to_nodeid.keys())  # test_ids that matched
    patched_not_collected = mapping["unmatched"]  # test_ids with no match

    # Build reverse mapping: nodeid -> test_id (for patched tests only)
    nodeid_to_test_id = {v: k for k, v in test_id_to_nodeid.items()}

    # Extract stable classification for patched tests only. Refinement
    # classification.json stores categories at the top level; keep support for
    # a nested classification dict because tests and future runners may wrap it.
    stable_class = classification_data.get("stable_classification")
    if not isinstance(stable_class, dict) or not any(
        category in stable_class for category in CLASSIFICATION_CATEGORIES
    ):
        fallback = classification_data.get("classification")
        stable_class = fallback if isinstance(fallback, dict) else classification_data

    # Filter stable_classification to only include patched tests
    patched_stable_classification = {}
    for category in CLASSIFICATION_CATEGORIES:
        category_tests = []
        for item in stable_class.get(category, []):
            # Items should be test_id strings
            if isinstance(item, str) and item in nodeid_to_test_id:
                category_tests.append(nodeid_to_test_id[item])
        if category_tests:
            patched_stable_classification[category] = sorted(category_tests)

    # Filter flaky_tests to only include patched tests
    patched_flaky_tests = []
    for flaky_test in classification_data.get("flaky_tests", []):
        nodeid = flaky_test.get("test_id")
        if nodeid and nodeid in nodeid_to_test_id:
            patched_flaky_tests.append(
                {
                    "test_id": nodeid_to_test_id[nodeid],
                    "nodeid": nodeid,
                    "category": flaky_test.get("category"),
                }
            )

    result["collected"] = {
        "patched_in_results": sorted(patched_collected),
        "patched_not_in_results": sorted(patched_not_collected),
        "test_id_to_nodeid": {k: v for k, v in sorted(test_id_to_nodeid.items())},
        "stable_classification": patched_stable_classification,
        "flaky_tests": patched_flaky_tests,
    }

    # Build summary counts for stable_classification
    stable_classification_summary = {category: len(tests) for category, tests in patched_stable_classification.items()}

    result["summary"] = {
        "total_commits": len(commits),
        "commits_found": len(result["commits_found"]),
        "commits_not_found": len(result["commits_not_found"]),
        "tests_added": len(result["test_ids"]["added"]),
        "tests_modified": len(result["test_ids"]["modified"]),
        "tests_deleted": len(result["test_ids"]["deleted"]),
        "effective_tests": len(result["test_ids"]["effective"]),
        "patched_collected": len(patched_collected),
        "patched_not_collected": len(patched_not_collected),
        "stable_classification": stable_classification_summary,
        "flaky_tests": len(patched_flaky_tests),
    }

    return result


def ensure_patched_tests(work_dir: Path) -> Path:
    """Ensure ``milestone_patched_tests/{mid}.json`` exists for every milestone.

    Idempotent: if every milestone in ``metadata.json`` already has a
    Layer-1 patched-tests file, this is a no-op. Otherwise runs Layer 1
    once for the whole scenario (commit-diff symbol extraction +
    per-milestone effective test_id aggregation).

    Layer 2 (classification intersection) is intentionally not precomputed
    here — it would go stale the moment a milestone's tests re-run, and
    is refreshed per-milestone after each test attempt.

    Returns the patched_tests directory path. Raises ``RuntimeError`` if
    the underlying Layer-1 subprocess fails.
    """
    patched_tests_dir = work_dir / "milestone_patched_tests"
    metadata_path = work_dir / "metadata.json"

    if patched_tests_dir.is_dir() and metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text())
            milestone_ids = [m["id"] for m in metadata.get("milestones", [])]
            if milestone_ids and all((patched_tests_dir / f"{mid}.json").is_file() for mid in milestone_ids):
                logger.info(
                    f"Patched tests up-to-date for {len(milestone_ids)} milestones; skipping Layer 1 extraction"
                )
                return patched_tests_dir
        except (json.JSONDecodeError, OSError):
            pass

    logger.info("Generating patched tests (Layer 1) for all milestones...")
    start = time.time()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "deepcommit.harness.prepare_images.collect_milestone_patched_tests",
            str(work_dir),
            "--layer",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"collect_milestone_patched_tests --layer 1 failed "
            f"(exit {result.returncode}). stderr tail: {(result.stderr or '')[-500:]}"
        )
    logger.info(f"Patched tests Layer 1 done in {time.time() - start:.1f}s")
    return patched_tests_dir


def main():
    parser = argparse.ArgumentParser(description="Collect patched tests for each milestone from commits")
    parser.add_argument(
        "work_dir", type=str, help="Path to the work directory (e.g., .../harness_workspace/repo_id/run_name)"
    )
    parser.add_argument(
        "--repo", "-r", type=str, help="Path to the original git repository (for looking up original commit timestamps)"
    )
    parser.add_argument("--milestone", "-m", type=str, help="Process only the specified milestone (e.g., M001, M003.2)")
    parser.add_argument(
        "--skip-extraction", action="store_true", help="Skip extraction step (use existing patched_tests data only)"
    )
    parser.add_argument(
        "--commit-splits",
        type=str,
        help="Path to commit_splits directory for split commit timestamp mapping. "
        "If not specified, will look in work_dir/commit-level/commit_splits/",
    )
    parser.add_argument(
        "--normalize-test-ids",
        action="store_true",
        help="Normalize test IDs to handle parameterized tests with random subtest suffixes "
        "(e.g., TestSafeMap/eJXQ3n3q -> TestSafeMap)",
    )
    parser.add_argument(
        "--framework",
        type=str,
        default="go_test",
        choices=["go_test", "pytest", "maven", "cargo", "jest", "mocha"],
        help="Test framework for normalization (default: go_test)",
    )
    parser.add_argument(
        "--layer",
        type=str,
        choices=["1", "2", "both"],
        default="both",
        help=(
            "Pipeline layer to run. '1' = commit-diff extraction + milestone "
            "aggregation (no classification intersection — useful at scenario "
            "entry, runs once for all milestones). '2' = classification "
            "intersection only, assuming Layer 1 already ran (use after a "
            "milestone's tests re-ran, to refresh that milestone alone — "
            "implies --skip-extraction). 'both' (default) = full pipeline, "
            "backward-compatible."
        ),
    )
    args = parser.parse_args()

    # Layer 2 implies skip-extraction: the per-commit patched_tests files
    # are assumed to exist from a prior Layer-1/both run.
    if args.layer == "2":
        args.skip_extraction = True

    work_dir = Path(args.work_dir).resolve()

    if not work_dir.exists():
        print(f"Error: work_dir does not exist: {work_dir}", file=sys.stderr)
        sys.exit(1)

    milestones_csv = work_dir / "milestones.csv"
    if not milestones_csv.exists():
        print(f"Error: milestones.csv not found: {milestones_csv}", file=sys.stderr)
        sys.exit(1)

    print(f"Work dir: {work_dir}")

    # Print normalization settings
    if args.normalize_test_ids:
        print(f"Test ID normalization: enabled (framework: {args.framework})")
    else:
        print("Test ID normalization: disabled")

    # Get repo path (explicit or inferred)
    repo_path: Optional[Path] = None
    if args.repo:
        repo_path = Path(args.repo).resolve()
        if not repo_path.exists():
            print(f"Error: repo path does not exist: {repo_path}", file=sys.stderr)
            sys.exit(1)
        print(f"Repo path: {repo_path}")
    else:
        repo_path = infer_repo_path(work_dir)
        if repo_path:
            print(f"Repo path (inferred): {repo_path}")
        else:
            print("Warning: Could not infer repo path. Split commit timestamps may be incorrect.")

    # Create output directory
    output_dir = work_dir / "milestone_patched_tests"
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {output_dir}")

    # Read milestones.csv (first pass: collect raw data)
    milestones_raw: List[dict] = []
    with open(milestones_csv, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            milestones_raw.append(dict(row))

    # Build milestones_dict for lookup
    milestones_dict: Dict[str, dict] = {row["id"]: row for row in milestones_raw}

    # Load dependencies as parent mapping
    parents_map = load_dependencies_as_parents(work_dir)

    # Get testbed path for timestamp lookup
    testbed_path = infer_testbed_path(work_dir)

    # Initialize SplitCommitMapper if available
    split_mapper = None
    if args.commit_splits:
        commit_splits_dir = Path(args.commit_splits).resolve()
    else:
        commit_splits_dir = work_dir / "commit-level" / "commit_splits"

    if SplitCommitMapper is not None and commit_splits_dir.exists():
        split_mapper = SplitCommitMapper(commit_splits_dir)
        print(f"Loaded SplitCommitMapper from {commit_splits_dir}")
    elif args.commit_splits:
        print(f"Warning: commit_splits directory not found: {commit_splits_dir}", file=sys.stderr)

    # Timestamp cache for performance
    timestamp_cache: Dict[str, int] = {}

    # Build final milestones list with applicable ITCs (using timestamp filter)
    milestones: List[tuple] = []
    all_commits: Set[str] = set()
    itc_assignments: Dict[str, List[str]] = {}  # For logging

    for row in milestones_raw:
        milestone_id = row["id"]
        commits_str = row.get("commits", "")
        commits = [c.strip() for c in commits_str.split(";") if c.strip()]

        # Get applicable ITCs
        # For sub-milestones: use group-based logic (only leaf nodes with timestamp filter)
        # For non-sub-milestones: no ITC inheritance
        applicable_itcs: List[str] = []
        if testbed_path:
            if is_sub_milestone(milestone_id):
                # New logic for sub-milestones: group-based ITC with leaf node + timestamp filter
                applicable_itcs = get_applicable_itcs_for_sub_milestone(
                    milestone_id=milestone_id,
                    milestones_dict=milestones_dict,
                    parents_map=parents_map,
                    testbed_path=testbed_path,
                    split_mapper=split_mapper,
                    timestamp_cache=timestamp_cache,
                    repo_path=repo_path,
                    verbose=False,
                )
            # Non-sub-milestones: no ITC inheritance (applicable_itcs remains empty)

        # Add applicable ITCs to commits
        for itc in applicable_itcs:
            if itc not in commits:
                commits.append(itc)

        if applicable_itcs:
            itc_assignments[milestone_id] = applicable_itcs

        milestones.append((milestone_id, commits))
        all_commits.update(commits)

    print(f"\nFound {len(milestones)} milestones with {len(all_commits)} unique commits")

    # Filter to single milestone if specified
    if args.milestone:
        filtered = [(mid, commits) for mid, commits in milestones if mid == args.milestone]
        if not filtered:
            print(f"Error: Milestone '{args.milestone}' not found in milestones.csv", file=sys.stderr)
            available = [mid for mid, _ in milestones]
            print(
                f"Available milestones: {', '.join(available[:10])}{'...' if len(available) > 10 else ''}",
                file=sys.stderr,
            )
            sys.exit(1)
        milestones = filtered
        # Update all_commits to only include commits for this milestone
        all_commits = set()
        for _, commits in milestones:
            all_commits.update(commits)
        print(f"Filtered to milestone: {args.milestone} ({len(all_commits)} commits)")

    # Log ITC assignments for visibility
    if itc_assignments:
        print("ITC assignments (sub-milestone leaf nodes with timestamp filter):")
        for mid, itcs in sorted(itc_assignments.items()):
            print(f"  {mid}: {', '.join(itcs)}")

    # Step 3: Extract patched tests for all commits using extract_symbol_changes.py
    if not args.skip_extraction:
        testbed_path = infer_testbed_path(work_dir)
        if testbed_path:
            config_path = get_default_config_path()
            if config_path.exists():
                # Output to work_dir/commit-level/patched_tests
                patched_tests_dir = work_dir / "commit-level" / "patched_tests"
                extract_commits_patched_tests(
                    commits=list(all_commits),
                    testbed_path=testbed_path,
                    output_dir=patched_tests_dir,
                    config_path=config_path,
                    verbose=True,
                )
            else:
                print(f"Error: Config not found: {config_path}", file=sys.stderr)
                sys.exit(1)
        else:
            print(f"Error: testbed not found in {work_dir}", file=sys.stderr)
            print("  Use --skip-extraction to use existing patched_tests data only.")
            sys.exit(1)

    # Process each milestone
    total_stats = {
        "milestones_processed": 0,
        "milestones_with_tests": 0,
        "total_commits": 0,
        "commits_found": 0,
        "commits_not_found": 0,
    }

    for milestone_id, commits in milestones:
        result = collect_milestone_tests(
            milestone_id,
            commits,
            work_dir,
            normalize_test_ids=args.normalize_test_ids,
            framework=args.framework,
            layer=args.layer,
        )

        # Save to file
        output_file = output_dir / f"{milestone_id}.json"
        with open(output_file, "w") as f:
            json.dump(result, f, indent=2)

        # Update stats
        total_stats["milestones_processed"] += 1
        if result["summary"]["effective_tests"] > 0:
            total_stats["milestones_with_tests"] += 1
        total_stats["total_commits"] += result["summary"]["total_commits"]
        total_stats["commits_found"] += result["summary"]["commits_found"]
        total_stats["commits_not_found"] += result["summary"]["commits_not_found"]

        # Print progress with status info
        tests_info = f"added={result['summary']['tests_added']}, modified={result['summary']['tests_modified']}, deleted={result['summary']['tests_deleted']}, effective={result['summary']['effective_tests']}"
        collected_info = f"collected: {result['summary']['patched_collected']}/{result['summary']['effective_tests']}"

        # Status classification summary
        stable_class = result["summary"]["stable_classification"]
        status_info = ""
        if result["summary"]["patched_collected"] > 0:
            parts = []
            if stable_class.get("fail_to_pass", 0) > 0:
                parts.append(f"f2p={stable_class['fail_to_pass']}")
            if stable_class.get("pass_to_fail", 0) > 0:
                parts.append(f"p2f={stable_class['pass_to_fail']}")
            if stable_class.get("fail_to_fail", 0) > 0:
                parts.append(f"f2f={stable_class['fail_to_fail']}")
            if stable_class.get("pass_to_pass", 0) > 0:
                parts.append(f"p2p={stable_class['pass_to_pass']}")
            if stable_class.get("none_to_pass", 0) > 0:
                parts.append(f"n2p={stable_class['none_to_pass']}")
            if stable_class.get("error_to_pass", 0) > 0:
                parts.append(f"e2p={stable_class['error_to_pass']}")
            if stable_class.get("new_tests", 0) > 0:
                parts.append(f"new={stable_class['new_tests']}")
            flaky_count = result["summary"]["flaky_tests"]
            if flaky_count > 0:
                parts.append(f"flaky={flaky_count}")
            if parts:
                status_info = f" [{', '.join(parts)}]"

        print(f"  {milestone_id}: {tests_info} ({collected_info}){status_info}")

    # Print summary
    print(f"\n{'=' * 60}")
    print("Summary:")
    print(f"  Milestones processed: {total_stats['milestones_processed']}")
    print(f"  Milestones with tests: {total_stats['milestones_with_tests']}")
    print(f"  Total commits: {total_stats['total_commits']}")
    print(f"  Commits found: {total_stats['commits_found']}")
    print(f"  Commits not found: {total_stats['commits_not_found']}")
    print(f"\nOutput saved to: {output_dir}")


if __name__ == "__main__":
    main()
