#!/usr/bin/env python3
"""
Build a milestone statistics TSV from classification files and raw test errors.

This script combines:
  - Per-milestone classification.json parsing
  - Dependency-based topological sorting
  - Raw error extraction from attempt summaries
  - Environment hint detection from logs

Usage:
    python build_milestone_stats.py --trial-dir <path>
    python build_milestone_stats.py --trial-dir <path> --output stats.tsv --no-env-hints

Outputs:
    - milestone_stats.tsv (or custom name via --output)
"""

from __future__ import annotations

import argparse
import csv
import heapq
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


# -----------------------------------------------------------------------------
# Column Definitions
# -----------------------------------------------------------------------------


def get_column_definitions() -> list[tuple[str, str, Any]]:
    """
    Define all output columns in order.

    Returns list of (column_name, source_type, source_key_or_components)

    source_type:
      - "summary": value from summary dict using source_key
      - "computed_sum": sum of multiple summary fields listed in source_key
      - "json_field": direct field from classification JSON object
      - "raw_errors": special handling for errors (source_key: "start" or "end")
      - "empty": empty column (separator)

    To reorder columns, simply rearrange the tuples in this list.
    """
    return [
        # Basic info
        ("mstone_id", "special", None),
        # Pass transitions
        ("pass_to_pass", "summary", "pass_to_pass"),
        ("pass_to_fail", "summary", "pass_to_fail"),
        # Fail transitions
        ("fail_to_pass", "summary", "fail_to_pass"),
        ("fail_to_fail", "summary", "fail_to_fail"),
        # New tests
        ("none_to_pass", "summary", "none_to_pass"),
        ("none_to_fail", "summary", "none_to_fail"),
        # Key incremental test summary
        ("none/fail_to_pass", "computed_sum", ["none_to_pass", "fail_to_pass"]),
        # Separator
        ("", "empty", None),
        # Totals
        ("total_before", "summary", "total_before"),
        ("total_after", "summary", "total_after"),
        ("new_total", "summary", "new_tests"),
        ("removed_total", "summary", "removed_tests"),
        ("skip_total", "computed_sum", ["skipped_to_pass", "skipped_to_fail", "skipped_to_skipped"]),
        ("to_skip_total", "computed_sum", ["pass_to_skipped", "fail_to_skipped"]),
        ("flaky_total", "summary", "flaky_total"),
        ("error_total", "raw_errors_sum", None),
        # Separator
        ("", "empty", None),
        # Skip transitions
        ("skip_to_pass", "summary", "skipped_to_pass"),
        ("skip_to_fail", "summary", "skipped_to_fail"),
        ("skip_to_skip", "summary", "skipped_to_skipped"),
        # To skip transitions
        ("pass_to_skip", "summary", "pass_to_skipped"),
        ("fail_to_skip", "summary", "fail_to_skipped"),
        # Flaky tests
        ("flaky_start", "summary", "flaky_in_start"),
        ("flaky_end", "summary", "flaky_in_end"),
        # Error tests
        ("errors_start", "raw_errors", "start"),
        ("errors_end", "raw_errors", "end"),
        # Metadata
        ("attempts", "json_field", "merged_from_attempts"),
        # Separator
        ("", "empty", None),
        # Patched tests
        ("total_patched_tests", "patched_tests", "effective_tests"),
        ("collected_patched_tests", "patched_tests", "patched_collected"),
    ]


# Build helper structures from column definitions
_COLUMN_DEFS = get_column_definitions()
COLUMNS = [col[0] for col in _COLUMN_DEFS]

# Summary fields needed for build_summary_from_classification
SUMMARY_FIELDS = [
    "pass_to_pass",
    "pass_to_fail",
    "pass_to_skipped",
    "fail_to_pass",
    "fail_to_fail",
    "fail_to_skipped",
    "skipped_to_pass",
    "skipped_to_fail",
    "skipped_to_skipped",
    "new_tests",
    "none_to_pass",
    "none_to_fail",
    "removed_tests",
    "total_before",
    "total_after",
    "flaky_total",
    "flaky_in_start",
    "flaky_in_end",
]

CLASSIFICATION_FIELDS = [
    "pass_to_pass",
    "pass_to_fail",
    "pass_to_skipped",
    "fail_to_pass",
    "fail_to_fail",
    "fail_to_skipped",
    "skipped_to_pass",
    "skipped_to_fail",
    "skipped_to_skipped",
    "new_tests",
    "removed_tests",
]


# -----------------------------------------------------------------------------
# File I/O Helpers
# -----------------------------------------------------------------------------


def read_json(path: Path) -> dict[str, Any] | None:
    """Read JSON file, return None on error."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def load_milestone_ids(milestones_csv: Path) -> list[str]:
    """Load milestone IDs from milestones.csv."""
    with milestones_csv.open() as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "id" not in reader.fieldnames:
            raise ValueError(f"milestones.csv missing 'id' column: {milestones_csv}")
        return [row["id"] for row in reader if row.get("id")]


# -----------------------------------------------------------------------------
# Topological Sort
# -----------------------------------------------------------------------------


def toposort(milestone_ids: list[str], dependencies_csv: Path) -> list[str]:
    """
    Topologically sort milestones based on dependencies.
    Returns milestones in dependency order (dependencies come first).
    """
    index = {mid: i for i, mid in enumerate(milestone_ids)}
    indeg = {mid: 0 for mid in milestone_ids}
    adj: dict[str, list[str]] = defaultdict(list)

    with dependencies_csv.open() as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "prerequisite_id" not in reader.fieldnames or "dependant_id" not in reader.fieldnames:
            raise ValueError(f"dependencies.csv missing prerequisite_id/dependant_id columns")
        for row in reader:
            src, tgt = row.get("prerequisite_id"), row.get("dependant_id")
            if not src or not tgt:
                continue
            if src not in indeg or tgt not in indeg:
                continue
            adj[src].append(tgt)
            indeg[tgt] += 1

    # Kahn's algorithm with deterministic tie-breaker
    heap: list[tuple[int, str]] = [(index[mid], mid) for mid, d in indeg.items() if d == 0]
    heapq.heapify(heap)
    order: list[str] = []

    while heap:
        _, node = heapq.heappop(heap)
        order.append(node)
        for nxt in adj.get(node, []):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                heapq.heappush(heap, (index[nxt], nxt))

    # Handle cycles: append remaining nodes in original order
    cycle_nodes = [mid for mid, d in indeg.items() if d != 0]
    if cycle_nodes:
        cycle_nodes.sort(key=lambda x: index.get(x, 0))
        order.extend(cycle_nodes)

    return order


# -----------------------------------------------------------------------------
# Summary Extraction from Classification Files
# -----------------------------------------------------------------------------


def build_summary_from_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Build summary from legacy 'statistics' field."""
    return {
        "pass_to_pass": stats.get("pass_to_pass", ""),
        "pass_to_fail": stats.get("pass_to_fail", ""),
        "pass_to_skipped": "",
        "fail_to_pass": stats.get("fail_to_pass", ""),
        "fail_to_fail": stats.get("fail_to_fail", ""),
        "fail_to_skipped": "",
        "skipped_to_pass": stats.get("env_patch_skip_to_pass", ""),
        "skipped_to_fail": stats.get("env_patch_skip_to_fail", ""),
        "skipped_to_skipped": stats.get("env_patch_skip_to_skip", stats.get("skipped", "")),
        "none_to_pass": stats.get("none_to_pass", ""),
        "none_to_fail": stats.get("none_to_fail", ""),
        "total_before": "",
        "total_after": "",
        "new_tests": stats.get("new_tests", ""),
        "removed_tests": stats.get("removed_tests", ""),
        "flaky_total": stats.get("flaky_total", ""),
        "flaky_in_start": stats.get("flaky_in_baseline", ""),
        "flaky_in_end": stats.get("flaky_in_final", ""),
    }


def build_summary_from_classification(classification: dict[str, Any]) -> dict[str, Any]:
    """Build summary by counting items in classification lists."""
    summary: dict[str, Any] = {}
    for key in SUMMARY_FIELDS:
        if key in ("none_to_pass", "none_to_fail"):
            # These are computed from new_tests
            continue
        items = classification.get(key, [])
        summary[key] = len(items) if isinstance(items, list) else ""

    # Compute none_to_pass and none_to_fail from new_tests
    new_tests = classification.get("new_tests", [])
    none_to_pass = 0
    none_to_fail = 0
    if isinstance(new_tests, list):
        for item in new_tests:
            if isinstance(item, dict):
                outcome = item.get("end_outcome", "")
                if outcome == "passed":
                    none_to_pass += 1
                elif outcome == "failed":
                    none_to_fail += 1
    summary["none_to_pass"] = none_to_pass
    summary["none_to_fail"] = none_to_fail

    return summary


def compute_none_to_pass_fail(classification: dict[str, Any]) -> tuple[int, int]:
    """Compute none_to_pass and none_to_fail from new_tests in classification."""
    new_tests = classification.get("new_tests", [])
    none_to_pass = 0
    none_to_fail = 0
    if isinstance(new_tests, list):
        for item in new_tests:
            if isinstance(item, dict):
                outcome = item.get("end_outcome", "")
                if outcome == "passed":
                    none_to_pass += 1
                elif outcome == "failed":
                    none_to_fail += 1
    return none_to_pass, none_to_fail


def extract_summary(obj: dict[str, Any]) -> dict[str, Any]:
    """Extract summary from classification JSON object."""
    summary = obj.get("summary")
    if isinstance(summary, dict):
        # If summary exists but doesn't have none_to_pass/none_to_fail, compute them
        if "none_to_pass" not in summary or "none_to_fail" not in summary:
            classification = obj.get("stable_classification") or obj.get("classification") or {}
            none_to_pass, none_to_fail = compute_none_to_pass_fail(classification)
            summary = dict(summary)  # Make a copy to avoid modifying original
            summary["none_to_pass"] = none_to_pass
            summary["none_to_fail"] = none_to_fail
        return summary

    # Fallback to statistics field
    stats = obj.get("statistics")
    if isinstance(stats, dict):
        return build_summary_from_stats(stats)

    # Last resort: count from stable_classification
    return build_summary_from_classification(obj.get("stable_classification") or {})


# -----------------------------------------------------------------------------
# Patched Tests Extraction
# -----------------------------------------------------------------------------


def load_patched_tests_summary(patched_tests_dir: Path, milestone_id: str) -> dict[str, Any]:
    """
    Load patched tests summary for a milestone from milestone_patched_tests/{milestone_id}.json.

    Returns dict with total_tests and patched_collected, or empty dict if not found.
    """
    patched_file = patched_tests_dir / f"{milestone_id}.json"
    if not patched_file.exists():
        return {}

    obj = read_json(patched_file)
    if not obj:
        return {}

    summary = obj.get("summary", {})
    return {
        "effective_tests": summary.get("effective_tests", 0),
        "patched_collected": summary.get("patched_collected", 0),
    }


# -----------------------------------------------------------------------------
# Raw Error Extraction
# -----------------------------------------------------------------------------


def extract_error_items(results: dict[str, Any]) -> list[dict[str, str]]:
    """Extract error items from test results."""
    items: Any = results.get("error") or results.get("errors")
    if not isinstance(items, list):
        return []

    out: list[dict[str, str]] = []
    for it in items:
        if isinstance(it, dict):
            nodeid = str(it.get("nodeid") or it.get("test_id") or it.get("id") or "")
            message = str(it.get("message") or it.get("error") or it.get("exception") or "")
        else:
            nodeid = ""
            message = str(it)
        out.append({"nodeid": nodeid, "message": message})
    return out


def collect_raw_errors(test_results_dir: Path, milestone_id: str) -> tuple[int, int]:
    """
    Collect unique raw error counts from attempt summaries.
    Returns (start_unique_error_count, end_unique_error_count).
    """
    ms_dir = test_results_dir / milestone_id
    if not ms_dir.exists():
        return 0, 0

    start_nodeids: set[str] = set()
    end_nodeids: set[str] = set()

    for attempt_dir in sorted(ms_dir.glob("attempt_*")):
        for phase, fname in [("start", "start_summary.json"), ("end", "end_summary.json")]:
            p = attempt_dir / fname
            obj = read_json(p)
            if not obj:
                continue
            items = extract_error_items(obj.get("results", {}) or {})
            for item in items:
                nodeid = item.get("nodeid", "")
                if nodeid:
                    if phase == "start":
                        start_nodeids.add(nodeid)
                    else:
                        end_nodeids.add(nodeid)

    return len(start_nodeids), len(end_nodeids)


# -----------------------------------------------------------------------------
# Main Processing
# -----------------------------------------------------------------------------


def safe_int(x: Any) -> int:
    """Convert value to int, returning 0 on failure."""
    try:
        return int(float(x))
    except Exception:
        return 0


def build_row(
    milestone_id: str,
    obj: dict[str, Any] | None,
    summary: dict[str, Any],
    raw_errors: tuple[int, int],
    patched_tests: dict[str, Any] | None = None,
) -> dict[str, str]:
    """
    Build a row dict based on column definitions.
    All column logic is centralized here.
    """
    row: dict[str, str] = {}
    start_errors, end_errors = raw_errors
    patched_tests = patched_tests or {}

    for col_name, source_type, source_key in _COLUMN_DEFS:
        if source_type == "special":
            # Special fields like mstone_id
            if col_name == "mstone_id":
                row[col_name] = milestone_id
        elif source_type == "summary":
            # Direct value from summary dict
            row[col_name] = str(summary.get(source_key, ""))
        elif source_type == "computed_sum":
            # Sum of multiple summary fields
            # Only compute sum if at least one field has actual data
            values = [summary.get(key) for key in source_key]
            has_data = any(v not in (None, "", 0) or (v == 0 and key in summary) for v, key in zip(values, source_key))
            if has_data:
                total = sum(safe_int(v) for v in values)
                row[col_name] = str(total)
            else:
                row[col_name] = ""
        elif source_type == "json_field":
            # Direct field from classification JSON
            row[col_name] = str(obj.get(source_key, "") if obj else "")
        elif source_type == "raw_errors":
            # Raw error counts
            if source_key == "start":
                row[col_name] = str(start_errors)
            elif source_key == "end":
                row[col_name] = str(end_errors)
        elif source_type == "raw_errors_sum":
            # Sum of raw error counts
            row[col_name] = str(start_errors + end_errors)
        elif source_type == "patched_tests":
            # Patched tests data
            value = patched_tests.get(source_key)
            row[col_name] = str(value) if value is not None else ""
        elif source_type == "empty":
            # Empty column (separator)
            row[col_name] = ""

    return row


def process_milestone(
    test_results_dir: Path,
    milestone_id: str,
    patched_tests_dir: Path | None = None,
) -> tuple[dict[str, str], bool]:
    """
    Process a single milestone and return its stats row.
    Returns (row, has_classification) tuple.
    """
    classification_path = test_results_dir / milestone_id / f"{milestone_id}_classification.json"

    # Load patched tests data if available
    patched_tests = {}
    if patched_tests_dir and patched_tests_dir.exists():
        patched_tests = load_patched_tests_summary(patched_tests_dir, milestone_id)

    # If no classification file, return empty row with just mstone_id
    if not classification_path.exists():
        row = build_row(milestone_id, None, {}, (0, 0), patched_tests)
        return row, False

    obj = read_json(classification_path)
    if not obj:
        row = build_row(milestone_id, None, {}, (0, 0), patched_tests)
        return row, False

    # Extract summary and collect raw errors
    summary = extract_summary(obj)
    raw_errors = collect_raw_errors(test_results_dir, milestone_id)

    # Build row using centralized column definitions
    row = build_row(milestone_id, obj, summary, raw_errors, patched_tests)

    return row, True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build milestone statistics TSV with topological sorting and error counts."
    )
    parser.add_argument(
        "--trial-dir",
        type=Path,
        required=True,
        help="Trial directory containing milestones.csv, dependencies.csv, and test_results/",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("milestone_stats.tsv"),
        help="Output TSV file (relative to --trial-dir if not absolute). Default: milestone_stats.tsv",
    )
    args = parser.parse_args()

    # Resolve paths
    trial_dir = args.trial_dir.expanduser().resolve()
    milestones_csv = trial_dir / "milestones.csv"
    dependencies_csv = trial_dir / "dependencies.csv"
    test_results_dir = trial_dir / "test_results"
    patched_tests_dir = trial_dir / "milestone_patched_tests"
    output_path = args.output if args.output.is_absolute() else (trial_dir / args.output)

    # Validate inputs
    if not milestones_csv.exists():
        raise SystemExit(f"Error: missing {milestones_csv}")
    if not dependencies_csv.exists():
        raise SystemExit(f"Error: missing {dependencies_csv}")
    if not test_results_dir.exists():
        raise SystemExit(f"Error: missing {test_results_dir}")

    # Load and sort milestones
    milestone_ids = load_milestone_ids(milestones_csv)
    if not milestone_ids:
        raise SystemExit(f"Error: no milestone IDs found in {milestones_csv}")

    sorted_ids = toposort(milestone_ids, dependencies_csv)

    # Process all milestones
    rows_with_data: list[dict[str, str]] = []
    rows_without_data: list[dict[str, str]] = []
    found_count = 0
    missing_count = 0

    for mid in sorted_ids:
        row, has_classification = process_milestone(test_results_dir, mid, patched_tests_dir)
        if has_classification:
            rows_with_data.append(row)
            found_count += 1
        else:
            rows_without_data.append(row)
            missing_count += 1

    # Combine rows: data rows first, then empty rows
    rows = rows_with_data + rows_without_data

    # Write output
    csv.field_size_limit(50 * 1024 * 1024)
    with output_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    print(f"Wrote {output_path}")
    print(f"  Milestones: {found_count} found, {missing_count} missing classification files")

    # Check for selected_milestone_ids.txt and generate selected_milestone_stats.tsv
    selected_ids_path = trial_dir / "selected_milestone_ids.txt"
    if selected_ids_path.exists():
        # Read selected milestone IDs
        selected_ids = set()
        with selected_ids_path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    selected_ids.add(line)

        # Filter rows for selected milestones
        selected_rows = [row for row in rows if row.get("mstone_id") in selected_ids]

        # Write selected_milestone_stats.tsv
        selected_output_path = trial_dir / "selected_milestone_stats.tsv"
        with selected_output_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS, delimiter="\t")
            writer.writeheader()
            for row in selected_rows:
                writer.writerow(row)

        print(f"Wrote {selected_output_path}")
        print(f"  Selected milestones: {len(selected_rows)} of {len(selected_ids)} requested")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
