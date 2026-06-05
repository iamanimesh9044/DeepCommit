#!/usr/bin/env python3
"""
Identify oversized milestones using statistical outlier detection.

This script reads commit data directly from the Git repository, updates
the src_loc field in milestones.csv, and identifies oversized milestones using
statistical outlier detection (mean + k*std).
The results serve as initial candidates for further LLM-based filtering in the main workflow.

Updated to support:
- Multiple source directories (repo_src_dirs)
- Exclude patterns for filtering out test/fixture files
"""

import argparse
import csv
import json
import re
import statistics
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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


def get_commit_src_loc(
    repo_path: Path,
    commit_sha: str,
    src_dirs: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> int:
    """
    Get source LOC (additions + deletions) for a commit.

    Args:
        repo_path: Path to git repository
        commit_sha: Commit SHA (full or short)
        src_dirs: List of source directories to include (default: ["src"])
        exclude_patterns: Glob patterns for files to exclude

    Returns:
        Total LOC (additions + deletions) for source files
    """
    if src_dirs is None:
        src_dirs = ["src"]
    if exclude_patterns is None:
        exclude_patterns = []

    try:
        # Run git show --numstat to get all file changes
        cmd = ["git", "show", "--numstat", "--format=", commit_sha]
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, check=True)

        total_loc = 0
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue

            parts = line.split("\t")
            if len(parts) < 3:
                continue

            # Handle binary files (marked with '-')
            additions = int(parts[0]) if parts[0] != "-" else 0
            deletions = int(parts[1]) if parts[1] != "-" else 0
            file_path_raw = parts[2]

            # Extract paths (handle renames)
            paths = _extract_paths(file_path_raw)

            for path in paths:
                # Step 1: Check exclude patterns first
                if exclude_patterns and SrcFileFilter.should_exclude_file(path, exclude_patterns):
                    continue

                # Step 2: Check if in source directories
                if SrcFileFilter.in_src_dirs(path, src_dirs):
                    total_loc += additions + deletions
                    break  # Count once per line even if multiple paths

        return total_loc

    except subprocess.CalledProcessError:
        # Commit might not exist in this repo
        return -1
    except Exception as e:
        print(f"Error processing commit {commit_sha}: {e}")
        return -1


def load_commit_data_from_repo(
    repo_path: Path,
    commits: List[str],
    src_dirs: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> Dict[str, int]:
    """
    Load commit LOC data directly from Git repository.

    Args:
        repo_path: Path to git repository
        commits: List of commit SHAs to process
        src_dirs: List of source directories to include
        exclude_patterns: Glob patterns for files to exclude

    Returns:
        Dictionary mapping commit SHA to total LOC
    """
    commit_loc = {}

    for commit in commits:
        loc = get_commit_src_loc(repo_path, commit, src_dirs, exclude_patterns)
        if loc >= 0:
            commit_loc[commit] = loc
        else:
            print(f"Warning: Commit {commit} not found in repository")

    return commit_loc


def calculate_cv(values: List[int]) -> float:
    """
    Calculate coefficient of variation (CV) = std / mean.

    Args:
        values: List of numeric values

    Returns:
        CV value, or 0 if mean is 0 or values is empty
    """
    if not values or len(values) <= 1:
        return 0.0

    mean_val = statistics.mean(values)
    if mean_val == 0:
        return 0.0

    std_val = statistics.stdev(values)
    return std_val / mean_val


def identify_extreme_milestones(
    milestone_data: List[Dict], k: float = 1.0, identify_small: bool = False
) -> Tuple[List[Dict], Dict]:
    """
    Identify oversized or undersized milestones using statistical outlier detection.

    Args:
        milestone_data: List of milestone dicts with 'milestone_id', 'src_loc', etc.
        k: Standard deviation multiplier for outlier detection (default: 1.0)
        identify_small: If True, identify small milestones (mean - k*std);
                       If False, identify large milestones (mean + k*std)

    Returns:
        Tuple of (extreme_milestones, statistics_dict)
        - extreme_milestones: List of oversized/undersized milestone dicts
        - statistics_dict: Global statistics including threshold
    """
    if not milestone_data:
        return [], {}

    # Extract src_loc values
    src_locs = [m["total_src_loc"] for m in milestone_data if m["total_src_loc"] > 0]

    if not src_locs:
        return [], {}

    # Calculate global statistics
    mean_src_loc = statistics.mean(src_locs)
    std_src_loc = statistics.stdev(src_locs) if len(src_locs) > 1 else 0

    # Calculate global milestone-level CV
    cv_milestones = calculate_cv(src_locs)

    # Statistical outlier detection
    if identify_small:
        threshold = max(0, mean_src_loc - k * std_src_loc)  # Ensure non-negative
    else:
        threshold = mean_src_loc + k * std_src_loc

    # Identify extreme milestones
    extreme_milestones = []
    for m in milestone_data:
        src_loc = m["total_src_loc"]
        if src_loc > 0:
            if identify_small and src_loc < threshold:
                extreme_milestones.append(m.copy())
            elif not identify_small and src_loc > threshold:
                extreme_milestones.append(m.copy())

    # Sort by src_loc (ascending for small, descending for large)
    extreme_milestones.sort(key=lambda x: x["total_src_loc"], reverse=not identify_small)

    # Return statistics
    stats = {
        "total_milestones": len(milestone_data),
        "mean_src_loc": mean_src_loc,
        "std_src_loc": std_src_loc,
        "cv_milestones": cv_milestones,
        "threshold": threshold,
        "k_value": k,
        "identify_small": identify_small,
        "extreme_milestones_count": len(extreme_milestones),
    }

    return extreme_milestones, stats


def update_milestone_src_loc(
    csv_path: Path,
    repo_path: Path,
    src_dirs: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> List[Dict]:
    """
    Update src_loc field in milestones.csv and collect milestone data for analysis.

    Args:
        csv_path: Path to milestones.csv
        repo_path: Path to git repository
        src_dirs: List of source directories to include (default: ["src"])
        exclude_patterns: Glob patterns for files to exclude

    Returns:
        List of milestone data, each containing:
        - milestone_id: Milestone ID
        - milestone_title: Milestone title
        - total_src_loc: Total src_loc
        - num_commits: Number of commits
        - commits: List of commit SHAs
        - commit_src_locs: Dict mapping commit SHA to src_loc
    """
    if src_dirs is None:
        src_dirs = ["src"]
    if exclude_patterns is None:
        exclude_patterns = []

    # Read all rows from CSV
    rows = []
    milestone_data = []

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames

        if fieldnames is None:
            raise ValueError("CSV file has no header row")

        for row in reader:
            # Parse commits field (semicolon-separated)
            commits_str = row.get("commits", "")
            if not commits_str:
                row["src_loc"] = "0"
                rows.append(row)
                continue

            # Split commits by semicolon
            commits = [c.strip() for c in commits_str.split(";") if c.strip()]

            # Calculate src_loc for each commit
            total_src_loc = 0
            commit_src_locs = {}
            missing_commits = []

            for commit in commits:
                loc = get_commit_src_loc(repo_path, commit, src_dirs, exclude_patterns)
                if loc >= 0:
                    total_src_loc += loc
                    commit_src_locs[commit] = loc
                else:
                    missing_commits.append(commit)

            # Update src_loc field
            row["src_loc"] = str(total_src_loc)

            # Log missing commits if any
            if missing_commits:
                print(f"Warning: Missing commits in {row['id']}: {', '.join(missing_commits)}")

            rows.append(row)

            # Collect milestone data for analysis
            milestone_info = {
                "milestone_id": row["id"],
                "milestone_title": row.get("title", ""),
                "total_src_loc": total_src_loc,
                "num_commits": len(commits),
                "commits": commits,
                "commit_src_locs": commit_src_locs,
            }
            milestone_data.append(milestone_info)

    # Write updated data back to CSV
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nSuccessfully updated {len(rows)} milestones in {csv_path}")

    return milestone_data


def main():
    """Main function to identify candidate milestones for decomposition."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Identify oversized milestones using statistical outlier detection (mean + k*std)"
    )
    parser.add_argument(
        "--milestone-dir",
        type=str,
        default="output",
        help="Parent directory containing milestones.csv (default: output)",
    )
    parser.add_argument("--repo-path", type=str, required=True, help="Path to git repository (required)")
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
        "--k-value",
        type=float,
        default=None,
        help="Standard deviation multiplier for outlier detection (default: 1.0 for large, 0.5 for small)",
    )
    parser.add_argument("--small", action="store_true", help="Identify undersized milestones instead of oversized ones")
    parser.add_argument("--output-json", type=str, help="Output JSON file for milestone candidates (optional)")
    args = parser.parse_args()

    # Set default k_value based on --small flag
    if args.k_value is None:
        args.k_value = 0.5 if args.small else 1.0

    # Parse src_dirs and exclude_patterns
    src_dirs = [d.strip() for d in args.repo_src_dirs.split(",") if d.strip()]
    exclude_patterns = [p.strip() for p in args.exclude_patterns.split(",") if p.strip()]

    # Define paths
    script_dir = Path(__file__).parent
    project_root = script_dir.parent.parent.parent  # deepcommit/tools/milestone_balancing -> project root

    csv_path = project_root / args.milestone_dir / "milestones.csv"
    repo_path = Path(args.repo_path).resolve()

    # Verify paths exist
    if not csv_path.exists():
        print(f"Error: CSV file not found: {csv_path}")
        return

    if not repo_path.exists():
        print(f"Error: Repository path not found: {repo_path}")
        return

    if not (repo_path / ".git").exists():
        print(f"Error: Not a git repository: {repo_path}")
        return

    print(f"Repository: {repo_path}")
    print(f"Source directories: {src_dirs}")
    if exclude_patterns:
        print(f"Exclude patterns: {exclude_patterns}")
    print(f"Milestone CSV: {csv_path}")
    print(f"K value (std multiplier): {args.k_value}")
    print()

    print("Updating milestone src_loc from Git repository...")
    milestone_data = update_milestone_src_loc(csv_path, repo_path, src_dirs, exclude_patterns)

    print("\nAnalyzing milestone size distribution...")
    extreme_milestones, stats = identify_extreme_milestones(milestone_data, k=args.k_value, identify_small=args.small)

    # Display global statistics
    print(f"\n{'=' * 80}")
    print("Global Milestone Statistics:")
    print(f"{'=' * 80}")
    print(f"Total milestones: {stats['total_milestones']}")
    print(f"Mean src_loc: {stats['mean_src_loc']:.1f}")
    print(f"Std src_loc: {stats['std_src_loc']:.1f}")
    print(f"CV (milestone-level): {stats['cv_milestones']:.3f}")

    if stats["identify_small"]:
        print(f"Threshold: {stats['threshold']:.1f} (mean - {stats['k_value']}*std)")
    else:
        print(f"Threshold: {stats['threshold']:.1f} (mean + {stats['k_value']}*std)")
    print()

    # Display results
    print(f"{'=' * 80}")
    if stats["identify_small"]:
        print("Undersized Milestones (src_loc < threshold):")
    else:
        print("Oversized Milestones (src_loc > threshold):")
    print(f"{'=' * 80}\n")

    if not extreme_milestones:
        if stats["identify_small"]:
            print("No undersized milestones found.")
        else:
            print("No oversized milestones found.")
    else:
        for m in extreme_milestones:
            # Sort commit_src_locs by value (descending for large, ascending for small)
            sorted_items = sorted(m["commit_src_locs"].items(), key=lambda x: x[1], reverse=not stats["identify_small"])

            # For small milestones, show all commits
            # For large milestones, show top 5
            if stats["identify_small"]:
                display_commits = dict(sorted_items)
                remaining_count = 0
            else:
                top_commits = dict(sorted_items[:5])
                remaining_count = len(sorted_items) - 5
                display_commits = top_commits

            print(f"Milestone: {m['milestone_id']} - {m['milestone_title']}")
            print(f"  Total src_loc: {m['total_src_loc']}")
            print(f"  Commits: {m['num_commits']}")

            if not stats["identify_small"] and remaining_count > 0:
                print(f"  Top {len(display_commits)} commits (+ {remaining_count} more): {display_commits}")
            else:
                print(f"  Commit src_locs: {display_commits}")
            print()

    # Optionally save to JSON
    if args.output_json:
        output_path = Path(args.output_json)
        output_data = {"statistics": stats, "extreme_milestones": extreme_milestones}
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2)

        milestone_type = "undersized" if stats["identify_small"] else "oversized"
        print(f"\nSaved {len(extreme_milestones)} {milestone_type} milestones to: {output_path}")

    print("\nDone!")


if __name__ == "__main__":
    main()
