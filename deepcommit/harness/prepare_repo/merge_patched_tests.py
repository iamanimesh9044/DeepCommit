#!/usr/bin/env python3
"""
Merge commit-level patched tests into milestone-level test changes.

This script reads commit-level patched_tests (symbol_changes.json files) and
milestones.csv, then merges them into milestone-level test_changes.json files.

Usage:
    python harness/prepare_repo/merge_patched_tests.py \
        --patched-tests-dir DATA/github_data/preprocessed/{project}/patched_tests \
        --milestones-csv DATA/harness_workspace/{project}/{run}/milestones.csv \
        --output-dir DATA/harness_workspace/{project}/{run}/patched_tests
"""

import argparse
import csv
import json
import logging
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def load_milestones(milestones_csv: Path) -> dict[str, list[str]]:
    """
    Load milestones.csv and return a mapping from milestone_id to list of commits.

    Args:
        milestones_csv: Path to the milestones.csv file

    Returns:
        Dict mapping milestone_id -> list of commit short hashes
    """
    milestones = {}
    with open(milestones_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            milestone_id = row["id"]
            commits_str = row.get("commits", "")
            if commits_str:
                commits = [c.strip() for c in commits_str.split(";") if c.strip()]
            else:
                commits = []
            milestones[milestone_id] = commits
    return milestones


def load_commit_patched_tests(patched_tests_dir: Path, commit_hash: str) -> dict[str, Any] | None:
    """
    Load a single commit's patched tests file.

    Args:
        patched_tests_dir: Directory containing {commit}_symbol_changes.json files
        commit_hash: Short commit hash

    Returns:
        Parsed JSON data or None if file not found
    """
    # Try to find the file with the given commit hash prefix
    pattern = f"{commit_hash}*_symbol_changes.json"
    matches = list(patched_tests_dir.glob(pattern))

    if not matches:
        logger.warning(f"No patched tests file found for commit {commit_hash}")
        return None

    if len(matches) > 1:
        logger.warning(f"Multiple files match commit {commit_hash}, using first: {matches[0]}")

    file_path = matches[0]
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_test_class_info(file_path: str) -> tuple[str, str] | None:
    """
    Extract module path and fully qualified Java class name from file path.

    Examples:
        dubbo-plugin/.../src/test/java/org/apache/dubbo/spring/security/ObjectMapperCodecTest.java
        -> ("dubbo-plugin/dubbo-spring-security", "org.apache.dubbo.spring.security.ObjectMapperCodecTest")

    Args:
        file_path: The file path from symbol_changes.json

    Returns:
        Tuple of (module_path, class_name) or None if not a valid test file
    """
    # Find the src/test/java/ marker
    markers = ["src/test/java/", "src/test/groovy/", "src/test/scala/"]

    for marker in markers:
        if marker in file_path:
            # Extract module path (everything before src/test/java/)
            idx = file_path.index(marker)
            module_path = file_path[:idx].rstrip("/")

            # Extract the part after the marker
            class_path = file_path[idx + len(marker) :]

            # Remove file extension
            for ext in [".java", ".groovy", ".scala", ".kt"]:
                if class_path.endswith(ext):
                    class_path = class_path[: -len(ext)]
                    break

            # Convert path separators to dots
            class_name = class_path.replace("/", ".")
            return (module_path, class_name)

    return None


def merge_symbols(existing: list[dict], new: list[dict]) -> list[dict]:
    """
    Merge two lists of symbols, deduplicating by (name, type).

    Args:
        existing: Existing list of symbol dicts
        new: New list of symbol dicts to merge

    Returns:
        Merged and deduplicated list
    """
    seen = set()
    result = []

    for symbol in existing + new:
        key = (symbol.get("name"), symbol.get("type"))
        if key not in seen:
            seen.add(key)
            result.append(symbol)

    return result


def merge_file_changes(files_by_path: dict[str, dict], commit_data: dict, commit_hash: str) -> None:
    """
    Merge file changes from a commit into the accumulated files_by_path dict.

    Args:
        files_by_path: Dict mapping file_path -> merged file info (mutated in place)
        commit_data: The commit's patched tests data
        commit_hash: The commit hash for tracking
    """
    for file_info in commit_data.get("files", []):
        file_path = file_info.get("file_path")
        if not file_path:
            continue

        if file_path not in files_by_path:
            # Initialize new file entry
            files_by_path[file_path] = {
                "file_path": file_path,
                "language": file_info.get("language"),
                "symbols_added": [],
                "symbols_modified": [],
                "symbols_deleted": [],
                "source_commits": [],
            }

        existing = files_by_path[file_path]

        # Merge symbols
        existing["symbols_added"] = merge_symbols(existing["symbols_added"], file_info.get("symbols_added", []))
        existing["symbols_modified"] = merge_symbols(
            existing["symbols_modified"], file_info.get("symbols_modified", [])
        )
        existing["symbols_deleted"] = merge_symbols(existing["symbols_deleted"], file_info.get("symbols_deleted", []))

        # Track source commits
        if commit_hash not in existing["source_commits"]:
            existing["source_commits"].append(commit_hash)


def merge_milestone_patched_tests(
    patched_tests_dir: Path,
    milestone_id: str,
    commits: list[str],
) -> dict[str, Any]:
    """
    Merge all commit-level patched tests for a milestone.

    Args:
        patched_tests_dir: Directory containing commit-level patched tests
        milestone_id: The milestone ID
        commits: List of commit hashes in this milestone

    Returns:
        Merged milestone-level test changes data
    """
    files_by_path: dict[str, dict] = {}
    processed_commits = []

    for commit_hash in commits:
        commit_data = load_commit_patched_tests(patched_tests_dir, commit_hash)
        if commit_data is None:
            continue

        merge_file_changes(files_by_path, commit_data, commit_hash)
        processed_commits.append(commit_hash)

    # Convert to list and sort by file path
    files_list = sorted(files_by_path.values(), key=lambda x: x["file_path"])

    # Extract test class info with module paths
    test_classes = []
    seen = set()
    for file_info in files_list:
        result = extract_test_class_info(file_info["file_path"])
        if result:
            module_path, class_name = result
            key = (module_path, class_name)
            if key not in seen:
                seen.add(key)
                test_classes.append({"module": module_path, "class": class_name})

    # Sort by module then class name
    test_classes.sort(key=lambda x: (x["module"], x["class"]))

    return {
        "milestone_id": milestone_id,
        "commits": processed_commits,
        "test_classes": test_classes,
        "files": files_list,
    }


def main():
    parser = argparse.ArgumentParser(description="Merge commit-level patched tests into milestone-level test changes")
    parser.add_argument(
        "--patched-tests-dir",
        type=Path,
        required=True,
        help="Directory containing commit-level {commit}_symbol_changes.json files",
    )
    parser.add_argument(
        "--milestones-csv",
        type=Path,
        required=True,
        help="Path to milestones.csv file",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Output directory for milestone-level test changes",
    )
    parser.add_argument(
        "--milestone",
        type=str,
        help="Process only a specific milestone (optional)",
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.patched_tests_dir.is_dir():
        logger.error(f"Patched tests directory not found: {args.patched_tests_dir}")
        return 1

    if not args.milestones_csv.is_file():
        logger.error(f"Milestones CSV not found: {args.milestones_csv}")
        return 1

    # Create output directory
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Load milestones
    milestones = load_milestones(args.milestones_csv)
    logger.info(f"Loaded {len(milestones)} milestones from {args.milestones_csv}")

    # Filter to specific milestone if requested
    if args.milestone:
        if args.milestone not in milestones:
            logger.error(f"Milestone {args.milestone} not found in CSV")
            return 1
        milestones = {args.milestone: milestones[args.milestone]}

    # Process each milestone
    for milestone_id, commits in milestones.items():
        if not commits:
            logger.warning(f"Milestone {milestone_id} has no commits, skipping")
            continue

        logger.info(f"Processing milestone {milestone_id} with {len(commits)} commits")

        merged_data = merge_milestone_patched_tests(args.patched_tests_dir, milestone_id, commits)

        # Write output
        output_file = args.output_dir / f"{milestone_id}_test_changes.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(merged_data, f, indent=2)

        logger.info(
            f"  -> {output_file.name}: {len(merged_data['test_classes'])} test classes, "
            f"{len(merged_data['files'])} files, {len(merged_data['commits'])} commits"
        )

    logger.info("Done!")
    return 0


if __name__ == "__main__":
    exit(main())
