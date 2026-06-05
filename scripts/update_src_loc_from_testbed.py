#!/usr/bin/env python3
"""Update src_loc, src_additions, src_deletions in milestones.csv based on testbed git diff."""

import argparse
import csv
import subprocess
from pathlib import Path


def get_src_stats(testbed_path: Path, milestone_id: str) -> tuple[int, int, int] | None:
    """Calculate src stats from git diff between start and end tags.

    Returns: (src_additions, src_deletions, src_loc) or None if tags not found.
    """
    start_tag = f"milestone-{milestone_id}-start"
    end_tag = f"milestone-{milestone_id}-end"

    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", start_tag, end_tag],
            cwd=testbed_path,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None

    src_add, src_del = 0, 0
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        add, delete, filepath = parts
        # Skip test files
        if "test" in filepath.lower() or filepath.endswith("_test.go"):
            continue
        # Skip docs
        if filepath.endswith(".md") or filepath.startswith("doc"):
            continue
        try:
            src_add += int(add)
            src_del += int(delete)
        except ValueError:
            continue

    return src_add, src_del, src_add + src_del


def main():
    parser = argparse.ArgumentParser(description="Update src stats in milestones.csv")
    parser.add_argument("csv_path", help="Path to milestones.csv")
    args = parser.parse_args()

    csv_path = Path(args.csv_path)
    testbed_path = csv_path.parent / "testbed"

    if not testbed_path.exists():
        print(f"Error: testbed not found at {testbed_path}")
        return 1

    # Read CSV
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames

    # Update src stats
    updated = 0
    for row in rows:
        milestone_id = row["id"]
        stats = get_src_stats(testbed_path, milestone_id)
        if stats is not None:
            src_add, src_del, src_loc = stats
            old = (
                f"loc={row.get('src_loc', '')}, add={row.get('src_additions', '')}, del={row.get('src_deletions', '')}"
            )
            row["src_loc"] = str(src_loc)
            row["src_additions"] = str(src_add)
            row["src_deletions"] = str(src_del)
            print(f"{milestone_id}: {old} -> loc={src_loc}, add={src_add}, del={src_del}")
            updated += 1
        else:
            print(f"{milestone_id}: tags not found, skipped")

    # Write CSV
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nUpdated {updated} milestones")
    return 0


if __name__ == "__main__":
    exit(main())
