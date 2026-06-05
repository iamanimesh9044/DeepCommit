#!/usr/bin/env python3
"""
Generate METADATA.json for split commits based on branch name.

This script analyzes a split commit branch and generates structured metadata
including separate tracking of source commits and integration test commit.

Updated to support:
- Multiple source directories (repo_src_dirs)
- Exclude patterns for filtering out test/fixture files

Usage:
    python generate_split_metadata.py \
        --repo-path /path/to/repo \
        --branch-name split-1812eac \
        --repo-src-dirs src/,lib/

Author: Generated for milestone decomposition workflow
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from deepcommit.harness.utils.src_filter import SrcFileFilter


def _extract_paths(raw: str) -> Tuple[str, ...]:
    """Handle rename syntax such as 'a -> b' or 'dir/{old => new}/file.py'."""
    raw = raw.strip()
    if " -> " in raw:
        return (raw.split(" -> ", 1)[1].strip(),)

    if " => " in raw:

        def _brace_repl(match: re.Match[str]) -> str:
            return match.group(2)

        replaced = re.sub(r"\{([^{}]+?) => ([^{}]+?)\}", _brace_repl, raw)
        if " => " in replaced:
            return (replaced.split(" => ", 1)[1].strip(),)
        return (replaced.strip(),)

    return (raw,)


def run_git_command(cmd: list[str], cwd: Path) -> str:
    """Run a git command and return output."""
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Error running git command: {' '.join(cmd)}", file=sys.stderr)
        print(f"Error: {e.stderr}", file=sys.stderr)
        sys.exit(1)


def get_commit_src_loc(
    repo_path: Path,
    commit_sha: str,
    src_dirs: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> dict[str, int]:
    """Get source LOC (additions + deletions) for a commit.

    Args:
        repo_path: Path to git repository
        commit_sha: Commit SHA
        src_dirs: List of source directories to include (default: ["src"])
        exclude_patterns: Glob patterns for files to exclude

    Returns:
        Dict with additions, deletions, total_loc
    """
    if src_dirs is None:
        src_dirs = ["src"]
    if exclude_patterns is None:
        exclude_patterns = []

    cmd = ["git", "show", "--numstat", "--format=", commit_sha]
    result = run_git_command(cmd, repo_path)

    additions = 0
    deletions = 0

    for line in result.split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue

        add = int(parts[0]) if parts[0] != "-" else 0
        delete = int(parts[1]) if parts[1] != "-" else 0
        file_path_raw = parts[2]

        # Extract paths (handle renames)
        paths = _extract_paths(file_path_raw)

        for path in paths:
            # Step 1: Check exclude patterns first
            if exclude_patterns and SrcFileFilter.should_exclude_file(path, exclude_patterns):
                continue

            # Step 2: Check if in source directories
            if SrcFileFilter.in_src_dirs(path, src_dirs):
                additions += add
                deletions += delete
                break  # Count once per line

    return {"additions": additions, "deletions": deletions, "total_loc": additions + deletions}


def get_commit_files(repo_path: Path, commit_sha: str) -> list[str]:
    """Get list of files changed in a commit."""
    cmd = ["git", "show", "--name-only", "--format=", commit_sha]
    result = run_git_command(cmd, repo_path)
    return [f for f in result.split("\n") if f]


def get_commit_info(
    repo_path: Path,
    commit_sha: str,
    src_dirs: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> dict:
    """Get complete information for a commit."""
    # Get full hash
    full_hash = run_git_command(["git", "rev-parse", commit_sha], repo_path)

    # Get short hash
    short_hash = run_git_command(["git", "rev-parse", "--short", commit_sha], repo_path)

    # Get commit message
    message = run_git_command(["git", "log", "-1", "--format=%B", commit_sha], repo_path)

    # Get src stats
    src_stats = get_commit_src_loc(repo_path, commit_sha, src_dirs, exclude_patterns)

    # Get files
    files = get_commit_files(repo_path, commit_sha)

    return {"sha": full_hash, "short_sha": short_hash, "message": message, "src_stats": src_stats, "files": files}


def is_test_commit(commit_info: dict) -> bool:
    """Determine if a commit is an integration test commit."""
    # Check if message starts with "test:"
    message = commit_info["message"].strip()
    if not message.startswith("test:"):
        return False

    # Check if src_stats.total_loc is 0 (no src changes)
    if commit_info["src_stats"]["total_loc"] != 0:
        return False

    return True


def generate_metadata(
    repo_path: Path,
    branch_name: str,
    src_dirs: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
    output_dir: Path | None = None,
) -> dict:
    """Generate METADATA.json for a split commit branch.

    Args:
        repo_path: Path to git repository
        branch_name: Name of the split branch (e.g., split-1812eac)
        src_dirs: List of source directories to include (default: ["src"])
        exclude_patterns: Glob patterns for files to exclude
        output_dir: Output directory for METADATA.json
    """
    if src_dirs is None:
        src_dirs = ["src"]
    if exclude_patterns is None:
        exclude_patterns = []

    # Get original commit info from branch name
    # Assumes branch name format: split-{commit_hash}
    if not branch_name.startswith("split-"):
        print(f"Error: Branch name must start with 'split-', got: {branch_name}", file=sys.stderr)
        sys.exit(1)

    original_commit_short = branch_name[6:]  # Remove "split-" prefix

    # Get original commit full hash and info
    original_hash = run_git_command(["git", "rev-parse", original_commit_short], repo_path)

    original_parent = run_git_command(["git", "rev-parse", f"{original_commit_short}^"], repo_path)

    original_message = run_git_command(["git", "log", "-1", "--format=%B", original_commit_short], repo_path)

    # Get all commits on the split branch (from parent to HEAD)
    commit_list = run_git_command(
        ["git", "rev-list", "--reverse", f"{original_parent}..{branch_name}"], repo_path
    ).split("\n")

    if not commit_list or commit_list == [""]:
        print(f"Error: No commits found on branch {branch_name}", file=sys.stderr)
        sys.exit(1)

    # Analyze each commit
    src_commits = []
    integration_test_commit = None

    for i, commit_sha in enumerate(commit_list, start=1):
        commit_info = get_commit_info(repo_path, commit_sha, src_dirs, exclude_patterns)
        commit_info["number"] = i
        commit_info["patch_file"] = f"patches/{commit_info['short_hash']}.patch"

        # Determine if test commit
        if is_test_commit(commit_info):
            if integration_test_commit is not None:
                print(
                    f"Warning: Multiple test commits found! First: {integration_test_commit['short_hash']}, "
                    f"Second: {commit_info['short_hash']}",
                    file=sys.stderr,
                )
            integration_test_commit = commit_info
        else:
            src_commits.append(commit_info)

    # Verify test commit is last (if exists)
    if integration_test_commit is not None:
        if integration_test_commit["number"] != len(commit_list):
            print(
                f"Warning: Test commit {integration_test_commit['short_hash']} is not the last commit! "
                f"Position: {integration_test_commit['number']}/{len(commit_list)}",
                file=sys.stderr,
            )

    # Build dependency edges (sequential dependencies between src commits)
    dependency_edges = []
    for i in range(len(src_commits) - 1):
        source_sha = src_commits[i]["short_sha"]
        target_sha = src_commits[i + 1]["short_sha"]
        dependency_edges.append([source_sha, target_sha])

    # Build metadata structure
    # Use consistent field naming: sha (full), short_sha (7-char)
    metadata = {
        "version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "original_commit": {
            "sha": original_hash,
            "short_sha": original_commit_short,
            "parent_sha": original_parent,
            "message": original_message,
        },
        "split_result": {
            "working_branch": branch_name,
            "total_src_commits": len(src_commits),
            "has_integration_test_commit": integration_test_commit is not None,
            "src_commits": src_commits,
            "integration_test_commit": integration_test_commit,
            "dependency_edges": dependency_edges,
        },
    }

    # Determine output directory
    if output_dir is None:
        output_dir = Path.cwd() / "output" / branch_name

    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "METADATA.json"

    # Write metadata
    with open(output_file, "w") as f:
        json.dump(metadata, f, indent=2)

    print(f"Generated metadata: {output_file}")
    print(f"   - Src commits: {len(src_commits)}")
    print(f"   - Integration test commit: {'Yes' if integration_test_commit else 'No'}")

    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate METADATA.json for split commits based on branch name",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate metadata for split-1812eac branch
  python generate_split_metadata.py --repo-path /path/to/repo --branch-name split-1812eac

  # Multiple source directories
  python generate_split_metadata.py --repo-path /path/to/repo --branch-name split-1812eac --repo-src-dirs src/,lib/

  # With exclude patterns
  python generate_split_metadata.py --repo-path /path/to/repo --branch-name split-1812eac --exclude-patterns "tests/**,**/test_*.py"

  # Custom output directory
  python generate_split_metadata.py --repo-path /path/to/repo --branch-name split-1812eac --output-dir /path/to/output
        """,
    )

    parser.add_argument("--repo-path", type=Path, required=True, help="Path to the Git repository")

    parser.add_argument(
        "--branch-name",
        type=str,
        help="Name of the split branch (e.g., split-1812eac). If not provided, uses current branch.",
    )

    parser.add_argument(
        "--repo-src-dirs",
        type=str,
        default="src",
        help="Comma-separated list of source directories to include (default: src)",
    )

    parser.add_argument(
        "--exclude-patterns",
        type=str,
        default="",
        help="Comma-separated glob patterns for files to exclude (e.g., 'tests/**,**/test_*.py')",
    )

    parser.add_argument(
        "--output-dir", type=Path, help="Output directory for METADATA.json (default: output/{branch_name}/)"
    )

    args = parser.parse_args()

    # Validate repo path
    if not args.repo_path.exists():
        print(f"Error: Repository path does not exist: {args.repo_path}", file=sys.stderr)
        sys.exit(1)

    if not (args.repo_path / ".git").exists():
        print(f"Error: Not a git repository: {args.repo_path}", file=sys.stderr)
        sys.exit(1)

    # Parse src_dirs and exclude_patterns
    src_dirs = [d.strip() for d in args.repo_src_dirs.split(",") if d.strip()]
    exclude_patterns = [p.strip() for p in args.exclude_patterns.split(",") if p.strip()]

    # Get branch name if not provided
    branch_name = args.branch_name
    if branch_name is None:
        branch_name = run_git_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], args.repo_path)
        print(f"Using current branch: {branch_name}")

    print(f"Repository: {args.repo_path}")
    print(f"Source directories: {src_dirs}")
    if exclude_patterns:
        print(f"Exclude patterns: {exclude_patterns}")

    # Generate metadata
    generate_metadata(
        repo_path=args.repo_path,
        branch_name=branch_name,
        src_dirs=src_dirs,
        exclude_patterns=exclude_patterns,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
