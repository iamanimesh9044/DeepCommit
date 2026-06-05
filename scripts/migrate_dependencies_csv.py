#!/usr/bin/env python3
"""Migrate legacy dependencies.csv column names → canonical.

Renames `source_id` → `prerequisite_id` and `target_id` → `dependant_id` in
every `dependencies.csv` under a given root (default: DATA/).

Direction is unchanged — `source` and `target` already encoded the canonical
"source = prerequisite, target = dependant" direction. Only the header names
are migrated to remove ambiguity. Other column types in the same CSV
(e.g. `dependency_primary_type`) are untouched.

Behaviors:
- Idempotent: a CSV already using canonical headers is left as-is.
- Mixed: a CSV with `prerequisite_id` AND `target_id` (improbable) is reported
  and skipped to avoid double-rename.
- Empty / missing header: reported and skipped.
- A `*.bak` backup is written next to each file before in-place edit unless
  `--no-backup` is passed.

Usage:
    python scripts/migrate_dependencies_csv.py [--root DATA] [--dry-run] [--no-backup]
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

LEGACY_PREREQ = "source_id"
LEGACY_DEP = "target_id"
NEW_PREREQ = "prerequisite_id"
NEW_DEP = "dependant_id"


def migrate_one(path: Path, *, dry_run: bool, backup: bool) -> str:
    """Return one of: 'skipped_no_change', 'migrated', 'skipped_mixed', 'skipped_no_header', 'error'."""
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            rows = list(reader)
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR reading {path}: {exc}", file=sys.stderr)
        return "error"

    if not rows:
        return "skipped_no_header"

    header = rows[0]
    has_legacy_prereq = LEGACY_PREREQ in header
    has_legacy_dep = LEGACY_DEP in header
    has_new_prereq = NEW_PREREQ in header
    has_new_dep = NEW_DEP in header

    if not (has_legacy_prereq or has_legacy_dep):
        return "skipped_no_change"

    if (has_new_prereq and has_legacy_prereq) or (has_new_dep and has_legacy_dep):
        print(f"  MIXED HEADER (refusing to migrate): {path}", file=sys.stderr)
        return "skipped_mixed"

    new_header = [
        NEW_PREREQ if c == LEGACY_PREREQ else NEW_DEP if c == LEGACY_DEP else c
        for c in header
    ]

    if dry_run:
        return "migrated"

    if backup:
        bak_path = path.with_suffix(path.suffix + ".bak")
        if bak_path.exists():
            # Don't overwrite an existing .bak (could be from prior migration or
            # other tool). Skip backup; the migration still proceeds.
            print(f"  NOTE existing .bak found, skipping backup: {bak_path}", file=sys.stderr)
        else:
            try:
                shutil.copy2(path, bak_path)
            except (PermissionError, OSError) as exc:
                print(f"  WARN backup failed for {path}: {exc} — skipping (no rewrite)", file=sys.stderr)
                return "error"

    rows[0] = new_header
    try:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(rows)
    except (PermissionError, OSError) as exc:
        print(f"  WARN write failed for {path}: {exc}", file=sys.stderr)
        return "error"
    return "migrated"


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate dependencies.csv headers to canonical names")
    parser.add_argument("--root", default="DATA", help="Root dir to scan (default: DATA)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be migrated without writing")
    parser.add_argument("--no-backup", action="store_true", help="Skip .bak files (default: write backup)")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"Root not found: {root}", file=sys.stderr)
        return 1

    counts = {
        "migrated": 0,
        "skipped_no_change": 0,
        "skipped_mixed": 0,
        "skipped_no_header": 0,
        "error": 0,
    }

    files = list(root.rglob("dependencies.csv"))
    print(f"Found {len(files)} dependencies.csv files under {root}")

    for path in files:
        result = migrate_one(path, dry_run=args.dry_run, backup=not args.no_backup)
        counts[result] += 1
        if result == "migrated":
            mark = "[DRY]" if args.dry_run else "[OK]"
            print(f"  {mark} migrated: {path}")

    print()
    print("Summary:")
    for k, v in counts.items():
        print(f"  {k:25s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
