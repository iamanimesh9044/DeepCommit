#!/usr/bin/env python3
"""
Test runner for milestone testing.

This script runs tests for milestone start and end states in Docker,
collecting test results and automatically classifying them.

Supports both single milestone and batch processing modes.

Features:
  - Retry mechanism: Each milestone is tested multiple times (default: 3 retries)
  - Pass-any logic: A milestone passes if ANY retry succeeds
  - Detailed results: All retry attempts are recorded for analysis

Output Structure (with retries):
    {output_dir}/
    ├── {milestone_id}/
    │   ├── test_summary.json         # Aggregated results from all attempts
    │   ├── {milestone_id}_classification.json  # Best classification
    │   ├── attempt_1/
    │   │   ├── start.json
    │   │   ├── end.json
    │   │   └── classification.json
    │   ├── attempt_2/
    │   │   └── ...
    │   └── attempt_3/
    │       └── ...
"""

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Import shared pytest report utilities
from deepcommit.harness.utils.pytest_report_utils import (
    convert_pytest_report_to_summary,
    extract_skip_reason,
)


def merge_pytest_json_reports(report_files: List[Path], output_file: Path, verbose: bool = False) -> bool:
    """
    Merge multiple pytest-json-report results into a single report.

    This is used when test_config.json specifies multiple test runs
    (e.g., normal tests + integration tests that use exclusive flags).

    Merge Strategy:
    - For the same test (same nodeid), prioritize non-skipped results
    - If multiple non-skipped results exist, use the first one
    - Recalculate summary statistics after merging

    Args:
        report_files: List of pytest-json-report JSON files to merge
        output_file: Output file path for merged report
        verbose: Whether to print verbose output

    Returns:
        True if merge was successful, False otherwise
    """
    # Outcome priority: lower number = higher priority (prefer actual runs over skipped)
    # Note: pytest-json-report uses "xfailed"/"xpassed" (not "xfail"/"xpass") for outcomes
    OUTCOME_PRIORITY = {
        "passed": 1,
        "failed": 1,
        "error": 1,
        "xfailed": 2,  # expected failure (test ran)
        "xpassed": 2,  # unexpected pass (test ran)
        "skipped": 3,
    }

    merged_tests = {}  # nodeid -> test_result
    total_duration = 0
    last_created = 0
    all_collectors = []
    seen_collector_nodeids = set()  # for deduplication
    environment = {}
    root = ""

    valid_reports = 0

    for report_file in report_files:
        if not report_file.exists():
            if verbose:
                print(f"  ⚠️  Report file not found: {report_file}")
            continue

        try:
            with open(report_file, "r") as f:
                report = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            if verbose:
                print(f"  ⚠️  Failed to load report {report_file}: {e}")
            continue

        valid_reports += 1
        total_duration += report.get("duration", 0)
        last_created = max(last_created, report.get("created", 0))

        # Use environment and root from first valid report
        if not environment:
            environment = report.get("environment", {})
        if not root:
            root = report.get("root", "")

        # Merge collectors (deduplicate by nodeid)
        for collector in report.get("collectors", []):
            nodeid = collector.get("nodeid", "")
            if nodeid not in seen_collector_nodeids:
                seen_collector_nodeids.add(nodeid)
                all_collectors.append(collector)

        # Merge tests
        for test in report.get("tests", []):
            nodeid = test.get("nodeid", "")
            outcome = test.get("outcome", "unknown")

            if nodeid not in merged_tests:
                # First occurrence of this test
                merged_tests[nodeid] = test
            else:
                # Compare priorities - prefer non-skipped results
                existing = merged_tests[nodeid]
                existing_priority = OUTCOME_PRIORITY.get(existing.get("outcome", "unknown"), 3)
                new_priority = OUTCOME_PRIORITY.get(outcome, 3)

                if new_priority < existing_priority:
                    # New result has higher priority (lower number), replace
                    merged_tests[nodeid] = test
                    if verbose:
                        print(f"  📝 Test {nodeid}: {existing.get('outcome')} → {outcome}")

    if valid_reports == 0:
        if verbose:
            print("  ❌ No valid report files to merge")
        return False

    # Calculate summary statistics
    # Note: pytest-json-report uses "xfailed"/"xpassed" in summary, but "xfail"/"xpass" in test outcomes
    summary = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "error": 0,
        "xfailed": 0,  # pytest-json-report uses "xfailed" in summary
        "xpassed": 0,  # pytest-json-report uses "xpassed" in summary
    }

    # Mapping from outcome to summary key
    # Note: pytest-json-report uses same names for both (xfailed/xpassed)
    outcome_to_summary = {
        "passed": "passed",
        "failed": "failed",
        "skipped": "skipped",
        "error": "error",
        "xfailed": "xfailed",
        "xpassed": "xpassed",
    }

    for test in merged_tests.values():
        outcome = test.get("outcome", "unknown")
        summary_key = outcome_to_summary.get(outcome)
        if summary_key and summary_key in summary:
            summary[summary_key] += 1

    summary["total"] = len(merged_tests)
    summary["collected"] = len(merged_tests)

    # Build merged report
    merged_report = {
        "created": last_created,
        "duration": total_duration,
        "exitcode": 0,  # Will be determined by test results
        "root": root,
        "environment": environment,
        "summary": summary,
        "collectors": all_collectors,
        "tests": list(merged_tests.values()),
        "_merge_info": {
            "source_files": [str(f) for f in report_files],
            "merged_at": time.time(),
            "valid_reports": valid_reports,
            "total_reports": len(report_files),
        },
    }

    # Determine exit code based on summary
    if summary.get("error", 0) > 0:
        merged_report["exitcode"] = 1
    elif summary.get("failed", 0) > 0:
        merged_report["exitcode"] = 1

    # Write merged report
    try:
        with open(output_file, "w") as f:
            json.dump(merged_report, f, indent=2)

        if verbose:
            print(f"  ✅ Merged {valid_reports} reports → {output_file}")
            print(f"     Total tests: {summary['total']}")
            print(f"     Passed: {summary['passed']}, Failed: {summary['failed']}, Skipped: {summary['skipped']}")

        return True
    except IOError as e:
        if verbose:
            print(f"  ❌ Failed to write merged report: {e}")
        return False


def merge_outcome(outcomes: List[str]) -> str:
    """
    Merge outcomes from multiple attempts using pass-any logic.

    Priority: passed > skipped > failed/error
    - If any attempt has 'passed' → 'passed'
    - If no pass but any 'skipped' → 'skipped'
    - Otherwise → 'failed'

    Args:
        outcomes: List of outcomes from multiple attempts

    Returns:
        Merged outcome string
    """
    if "passed" in outcomes:
        return "passed"
    if "skipped" in outcomes:
        return "skipped"
    # failed, error, or other outcomes
    return "failed"


def is_flaky(outcomes: List[str]) -> bool:
    """
    Check if a test is flaky based on its outcomes across multiple attempts.

    A test is considered flaky if its outcomes are not consistent across attempts.

    Args:
        outcomes: List of outcomes from multiple attempts

    Returns:
        True if the test is flaky (inconsistent outcomes), False otherwise
    """
    if len(outcomes) <= 1:
        return False
    # Normalize outcomes: treat 'error' as 'failed' for flaky detection
    normalized = []
    for o in outcomes:
        if o in ["failed", "error"]:
            normalized.append("failed")
        else:
            normalized.append(o)
    return len(set(normalized)) > 1


def merge_attempt_results(attempt_dirs: List[Path], verbose: bool = False) -> Dict[str, Any]:
    """
    Merge test results from multiple attempts to eliminate flaky test effects.

    For each test:
    - Collect start_outcome from all attempts
    - Collect end_outcome from all attempts
    - Merge using pass-any logic (pass > skip > fail)
    - Classify based on merged outcomes

    Args:
        attempt_dirs: List of attempt directories containing start.json and end.json
        verbose: Whether to print verbose output

    Returns:
        Dictionary with merged classification results, or None if no valid attempts
    """
    # Load all start and end results from successful attempts
    all_start_results = []
    all_end_results = []
    valid_attempt_dirs = []

    for attempt_dir in attempt_dirs:
        start_file = attempt_dir / "start.json"
        end_file = attempt_dir / "end.json"

        if start_file.exists() and end_file.exists():
            try:
                with open(start_file) as f:
                    start_data = json.load(f)
                with open(end_file) as f:
                    end_data = json.load(f)
                all_start_results.append(start_data)
                all_end_results.append(end_data)
                valid_attempt_dirs.append(attempt_dir)
            except (json.JSONDecodeError, IOError) as e:
                if verbose:
                    print(f"  Warning: Failed to load results from {attempt_dir}: {e}")

    if not all_start_results or not all_end_results:
        return None

    # Collect outcomes for each test from all attempts
    # test_id -> list of outcomes
    start_outcomes_per_test: Dict[str, List[str]] = {}
    end_outcomes_per_test: Dict[str, List[str]] = {}

    # Also collect test details for [ENV-PATCH] handling (from first valid attempt)
    start_tests_dict: Dict[str, Dict] = {}
    end_tests_dict: Dict[str, Dict] = {}

    for _i, results in enumerate(all_start_results):
        for test in results.get("tests", []):
            test_id = test.get("nodeid", "")
            outcome = test.get("outcome", "unknown")
            if test_id not in start_outcomes_per_test:
                start_outcomes_per_test[test_id] = []
            start_outcomes_per_test[test_id].append(outcome)
            # Keep first occurrence for test details
            if test_id not in start_tests_dict:
                start_tests_dict[test_id] = test

    for _i, results in enumerate(all_end_results):
        for test in results.get("tests", []):
            test_id = test.get("nodeid", "")
            outcome = test.get("outcome", "unknown")
            if test_id not in end_outcomes_per_test:
                end_outcomes_per_test[test_id] = []
            end_outcomes_per_test[test_id].append(outcome)
            # Keep first occurrence for test details
            if test_id not in end_tests_dict:
                end_tests_dict[test_id] = test

    # Handle collection errors from start (merge from all attempts)
    start_failed_collectors: Set[str] = set()
    for results in all_start_results:
        for collector in results.get("collectors", []):
            if collector.get("outcome") == "failed":
                nodeid = collector.get("nodeid", "")
                if nodeid:
                    start_failed_collectors.add(nodeid)

    # For tests from failed collectors, mark them as 'error' in start if not already present
    for test_id in end_outcomes_per_test.keys():
        test_file = test_id.split("::")[0] if "::" in test_id else test_id
        if test_file in start_failed_collectors and test_id not in start_outcomes_per_test:
            start_outcomes_per_test[test_id] = ["error"]

    # Merge outcomes for each test
    merged_start: Dict[str, str] = {
        test_id: merge_outcome(outcomes) for test_id, outcomes in start_outcomes_per_test.items()
    }
    merged_end: Dict[str, str] = {
        test_id: merge_outcome(outcomes) for test_id, outcomes in end_outcomes_per_test.items()
    }

    # Get all unique test IDs
    all_tests = set(merged_start.keys()) | set(merged_end.keys())

    # Helper function to check for [ENV-PATCH] or [COMMIT-FIX] skips
    def is_env_patch_skip(test: Dict) -> bool:
        reason = extract_skip_reason(test)
        # Match both [ENV-PATCH] and [COMMIT-FIX {commit_sha}] prefixes
        return "[ENV-PATCH]" in reason or "[COMMIT-FIX" in reason

    # Classification categories
    classification = {
        "pass_to_pass": [],
        "fail_to_pass": [],
        "fail_to_fail": [],
        "pass_to_fail": [],
        "new_tests": [],
        "removed_tests": [],
        "skipped": [],
        "env_patch_skip_to_pass": [],
        "env_patch_skip_to_fail": [],
        "env_patch_skip_to_skip": [],
    }

    for test_id in all_tests:
        start_outcome = merged_start.get(test_id, "missing")
        end_outcome = merged_end.get(test_id, "missing")

        # Handle missing tests
        if start_outcome == "missing":
            classification["new_tests"].append({"test_id": test_id, "end_outcome": end_outcome})
            continue

        if end_outcome == "missing":
            classification["removed_tests"].append({"test_id": test_id, "start_outcome": start_outcome})
            continue

        # Handle [ENV-PATCH] skipped tests in start
        if start_outcome == "skipped":
            start_test = start_tests_dict.get(test_id)
            if start_test and is_env_patch_skip(start_test):
                skip_reason = extract_skip_reason(start_test)

                if end_outcome == "skipped":
                    classification["env_patch_skip_to_skip"].append(
                        {
                            "test_id": test_id,
                            "start_reason": skip_reason,
                            "end_reason": extract_skip_reason(end_tests_dict.get(test_id, {})),
                        }
                    )
                elif end_outcome == "passed":
                    classification["env_patch_skip_to_pass"].append(
                        {"test_id": test_id, "start_reason": skip_reason, "end_outcome": end_outcome}
                    )
                else:
                    classification["env_patch_skip_to_fail"].append(
                        {"test_id": test_id, "start_reason": skip_reason, "end_outcome": end_outcome}
                    )
                continue

        # Normal skipped tests (both skipped, not [ENV-PATCH])
        if start_outcome == "skipped" and end_outcome == "skipped":
            classification["skipped"].append(test_id)
            continue

        # Normalize outcomes to pass/fail
        start_pass = start_outcome == "passed"
        end_pass = end_outcome == "passed"

        # Classify based on transition
        if start_pass and end_pass:
            classification["pass_to_pass"].append(test_id)
        elif not start_pass and end_pass:
            classification["fail_to_pass"].append(test_id)
        elif not start_pass and not end_pass:
            classification["fail_to_fail"].append(test_id)
        elif start_pass and not end_pass:
            classification["pass_to_fail"].append(test_id)

    # ===== Flaky Test Detection =====
    # Detect flaky tests by checking if outcomes are inconsistent across attempts
    flaky_tests = []
    flaky_test_ids = set()

    for test_id in all_tests:
        start_outcomes = start_outcomes_per_test.get(test_id, [])
        end_outcomes = end_outcomes_per_test.get(test_id, [])

        start_is_flaky = is_flaky(start_outcomes)
        end_is_flaky = is_flaky(end_outcomes)

        if start_is_flaky or end_is_flaky:
            # Determine flaky_in value
            if start_is_flaky and end_is_flaky:
                flaky_in = "both"
            elif start_is_flaky:
                flaky_in = "start"
            else:
                flaky_in = "end"

            # Determine which category this test belongs to
            category = None
            for cat_name, cat_tests in classification.items():
                # Handle both string lists and dict lists
                for t in cat_tests:
                    t_id = t["test_id"] if isinstance(t, dict) else t
                    if t_id == test_id:
                        category = cat_name
                        break
                if category:
                    break

            flaky_tests.append(
                {
                    "test_id": test_id,
                    "category": category,
                    "flaky_in": flaky_in,
                    "start_outcomes": start_outcomes,
                    "end_outcomes": end_outcomes,
                    "start_merged": merged_start.get(test_id, "missing"),
                    "end_merged": merged_end.get(test_id, "missing"),
                }
            )
            flaky_test_ids.add(test_id)

    # ===== Generate stable_classification (excluding flaky tests) =====
    stable_classification = {}
    for cat_name, cat_tests in classification.items():
        stable_tests = []
        for t in cat_tests:
            t_id = t["test_id"] if isinstance(t, dict) else t
            if t_id not in flaky_test_ids:
                stable_tests.append(t)
        stable_classification[cat_name] = stable_tests

    # Calculate statistics
    stats = {
        "total_tests": len(all_tests),
        "pass_to_pass": len(classification["pass_to_pass"]),
        "fail_to_pass": len(classification["fail_to_pass"]),
        "fail_to_fail": len(classification["fail_to_fail"]),
        "pass_to_fail": len(classification["pass_to_fail"]),
        "new_tests": len(classification["new_tests"]),
        "removed_tests": len(classification["removed_tests"]),
        "skipped": len(classification["skipped"]),
        "env_patch_skip_to_pass": len(classification["env_patch_skip_to_pass"]),
        "env_patch_skip_to_fail": len(classification["env_patch_skip_to_fail"]),
        "env_patch_skip_to_skip": len(classification["env_patch_skip_to_skip"]),
        # Flaky statistics
        "flaky_total": len(flaky_tests),
        "flaky_in_start": len([t for t in flaky_tests if t["flaky_in"] in ["start", "both"]]),
        "flaky_in_end": len([t for t in flaky_tests if t["flaky_in"] in ["end", "both"]]),
        "flaky_in_both": len([t for t in flaky_tests if t["flaky_in"] == "both"]),
        # Stable statistics
        "stable_pass_to_pass": len(stable_classification["pass_to_pass"]),
        "stable_fail_to_pass": len(stable_classification["fail_to_pass"]),
        "stable_fail_to_fail": len(stable_classification["fail_to_fail"]),
        "stable_pass_to_fail": len(stable_classification["pass_to_fail"]),
    }

    # Generate summary
    summary_lines = [
        "",
        "=" * 60,
        f"Test Classification Summary (merged from {len(valid_attempt_dirs)} attempts)",
        "=" * 60,
        f"Total tests: {stats['total_tests']}",
        "",
        f"✅ pass→pass (start): {stats['pass_to_pass']}",
        f"✨ fail→pass (fixed): {stats['fail_to_pass']}",
        f"❌ fail→fail (unrelated): {stats['fail_to_fail']}",
        f"⚠️  pass→fail (regression): {stats['pass_to_fail']}",
        "",
        f"🆕 New tests: {stats['new_tests']}",
        f"🗑️  Removed tests: {stats['removed_tests']}",
        f"⏭️  Skipped: {stats['skipped']}",
        "",
        f"🔧 [ENV-PATCH] skip→pass: {stats['env_patch_skip_to_pass']}",
        f"🔧 [ENV-PATCH] skip→fail: {stats['env_patch_skip_to_fail']}",
        f"🔧 [ENV-PATCH] skip→skip: {stats['env_patch_skip_to_skip']}",
        "",
        f"🎲 Flaky tests: {stats['flaky_total']} (start: {stats['flaky_in_start']}, end: {stats['flaky_in_end']}, both: {stats['flaky_in_both']})",
        f"📊 Stable fail→pass: {stats['stable_fail_to_pass']} (used for SRS & eval)",
        "=" * 60,
    ]

    return {
        "classification": classification,
        "stable_classification": stable_classification,
        "flaky_tests": flaky_tests,
        "statistics": stats,
        "summary": "\n".join(summary_lines),
        "merged_from_attempts": len(valid_attempt_dirs),
    }


class TestClassifier:
    """Classifies test results by comparing start and end states."""

    def __init__(self, start_file: Path, end_file: Path, verbose: bool = False):
        self.start_file = start_file
        self.end_file = end_file
        self.verbose = verbose

        self.start_results = None
        self.end_results = None

    def log(self, msg: str):
        """Print log message."""
        if self.verbose:
            print(msg)

    def load_results(self) -> bool:
        """Load test result JSON files."""
        try:
            with open(self.start_file, "r") as f:
                self.start_results = json.load(f)
            self.log(f"✅ Loaded start results from {self.start_file}")

            with open(self.end_file, "r") as f:
                self.end_results = json.load(f)
            self.log(f"✅ Loaded end results from {self.end_file}")

            return True
        except Exception as e:
            print(f"❌ Error loading test results: {e}")
            return False

    def extract_test_outcomes(self, results: Dict) -> Dict[str, str]:
        """
        Extract test outcomes from pytest-json-report format.

        Returns:
            Dictionary mapping test_id -> outcome (passed/failed/skipped/error)
        """
        outcomes = {}

        if "tests" not in results:
            return outcomes

        for test in results["tests"]:
            test_id = test.get("nodeid", "")
            outcome = test.get("outcome", "unknown")
            outcomes[test_id] = outcome

        return outcomes

    def is_env_patch_skip(self, test: Dict) -> bool:
        """
        Check if a test was skipped due to [ENV-PATCH] or [COMMIT-FIX].

        Args:
            test: Test result dictionary

        Returns:
            True if skip reason contains [ENV-PATCH] or [COMMIT-FIX] prefix
        """
        reason = extract_skip_reason(test)
        # Match both [ENV-PATCH] and [COMMIT-FIX {commit_sha}] prefixes
        return "[ENV-PATCH]" in reason or "[COMMIT-FIX" in reason

    def extract_failed_collectors(self, results: Dict) -> Set[str]:
        """
        Extract failed collectors (test files with collection errors) from pytest-json-report format.

        Returns:
            Set of test file paths that failed to collect (e.g., 'test/test_http2_connection.py')
        """
        failed_collectors = set()

        if "collectors" not in results:
            return failed_collectors

        for collector in results["collectors"]:
            if collector.get("outcome") == "failed":
                nodeid = collector.get("nodeid", "")
                if nodeid:  # nodeid is the file path for failed collectors
                    failed_collectors.add(nodeid)

        return failed_collectors

    def classify_tests(self) -> Dict[str, Any]:
        """
        Classify tests by comparing start and end outcomes.

        Handles collection errors: If a test file failed to collect in start
        but tests from that file passed in end, those are counted as ERROR→PASS.

        Returns:
            Dictionary with classified test categories and statistics.
        """
        start_outcomes = self.extract_test_outcomes(self.start_results)
        end_outcomes = self.extract_test_outcomes(self.end_results)

        # Get failed collectors (test files with collection errors) from start
        start_failed_collectors = self.extract_failed_collectors(self.start_results)

        # For tests from failed collectors, mark them as 'error' in start
        for test_id in end_outcomes.keys():
            # Extract the test file path from test_id (e.g., 'test/foo.py::TestClass::test_method' -> 'test/foo.py')
            test_file = test_id.split("::")[0] if "::" in test_id else test_id

            # If this test's file had a collection error in start, mark it as error
            if test_file in start_failed_collectors and test_id not in start_outcomes:
                start_outcomes[test_id] = "error"
                self.log(f"Collection error in start for: {test_id}")

        # Get all unique test IDs
        all_tests = set(start_outcomes.keys()) | set(end_outcomes.keys())

        # Build test lookup dictionaries
        start_tests_dict = {t.get("nodeid", ""): t for t in self.start_results.get("tests", [])}
        end_tests_dict = {t.get("nodeid", ""): t for t in self.end_results.get("tests", [])}

        # Classification categories
        classification = {
            "pass_to_pass": [],  # Start tests that still pass
            "fail_to_pass": [],  # Milestone fixes ✨
            "fail_to_fail": [],  # Unrelated failures
            "pass_to_fail": [],  # Regressions ⚠️
            "new_tests": [],  # Tests only in end
            "removed_tests": [],  # Tests only in start
            "skipped": [],  # Tests skipped in both (normal skips)
            "env_patch_skip_to_pass": [],  # [ENV-PATCH] skip → pass (feature added)
            "env_patch_skip_to_fail": [],  # [ENV-PATCH] skip → fail (feature added but broken)
            "env_patch_skip_to_skip": [],  # [ENV-PATCH] skip → skip (still missing or skipped)
        }

        for test_id in all_tests:
            start_outcome = start_outcomes.get(test_id, "missing")
            end_outcome = end_outcomes.get(test_id, "missing")

            # Handle missing tests
            if start_outcome == "missing":
                classification["new_tests"].append({"test_id": test_id, "end_outcome": end_outcome})
                continue

            if end_outcome == "missing":
                classification["removed_tests"].append({"test_id": test_id, "start_outcome": start_outcome})
                continue

            # Handle skipped tests - check for [ENV-PATCH] in start
            if start_outcome == "skipped":
                start_test = start_tests_dict.get(test_id)
                if start_test and self.is_env_patch_skip(start_test):
                    # Start was [ENV-PATCH] skipped - categorize by end outcome
                    skip_reason = extract_skip_reason(start_test)

                    if end_outcome == "skipped":
                        # skip → skip: Still skipped in end (feature still missing or other skip reason)
                        classification["env_patch_skip_to_skip"].append(
                            {
                                "test_id": test_id,
                                "start_reason": skip_reason,
                                "end_reason": extract_skip_reason(end_tests_dict.get(test_id, {})),
                            }
                        )
                    elif end_outcome in ["passed"]:
                        # skip → pass: Feature was added and tests pass
                        classification["env_patch_skip_to_pass"].append(
                            {"test_id": test_id, "start_reason": skip_reason, "end_outcome": end_outcome}
                        )
                    else:
                        # skip → fail: Feature was added but tests fail
                        classification["env_patch_skip_to_fail"].append(
                            {"test_id": test_id, "start_reason": skip_reason, "end_outcome": end_outcome}
                        )
                    continue

            # Normal skipped tests (both skipped, not [ENV-PATCH])
            if start_outcome == "skipped" and end_outcome == "skipped":
                classification["skipped"].append(test_id)
                continue

            # Normalize outcomes to pass/fail
            start_pass = start_outcome in ["passed"]
            end_pass = end_outcome in ["passed"]

            # Classify based on transition
            if start_pass and end_pass:
                classification["pass_to_pass"].append(test_id)
            elif not start_pass and end_pass:
                classification["fail_to_pass"].append(test_id)
            elif not start_pass and not end_pass:
                classification["fail_to_fail"].append(test_id)
            elif start_pass and not end_pass:
                classification["pass_to_fail"].append(test_id)

        # Calculate statistics
        stats = {
            "total_tests": len(all_tests),
            "pass_to_pass": len(classification["pass_to_pass"]),
            "fail_to_pass": len(classification["fail_to_pass"]),
            "fail_to_fail": len(classification["fail_to_fail"]),
            "pass_to_fail": len(classification["pass_to_fail"]),
            "new_tests": len(classification["new_tests"]),
            "removed_tests": len(classification["removed_tests"]),
            "skipped": len(classification["skipped"]),
            "env_patch_skip_to_pass": len(classification["env_patch_skip_to_pass"]),
            "env_patch_skip_to_fail": len(classification["env_patch_skip_to_fail"]),
            "env_patch_skip_to_skip": len(classification["env_patch_skip_to_skip"]),
        }

        return {"classification": classification, "statistics": stats, "summary": self._generate_summary(stats)}

    def _generate_summary(self, stats: Dict[str, int]) -> str:
        """Generate a human-readable summary."""
        lines = [
            "",
            "=" * 60,
            "Test Classification Summary",
            "=" * 60,
            f"Total tests: {stats['total_tests']}",
            "",
            f"✅ pass→pass (start): {stats['pass_to_pass']}",
            f"✨ fail→pass (fixed): {stats['fail_to_pass']}",
            f"❌ fail→fail (unrelated): {stats['fail_to_fail']}",
            f"⚠️  pass→fail (regression): {stats['pass_to_fail']}",
            "",
            f"🆕 New tests: {stats['new_tests']}",
            f"🗑️  Removed tests: {stats['removed_tests']}",
            f"⏭️  Skipped: {stats['skipped']}",
            "",
            f"🔧 [ENV-PATCH] skip→pass: {stats['env_patch_skip_to_pass']}",
            f"🔧 [ENV-PATCH] skip→fail: {stats['env_patch_skip_to_fail']}",
            f"🔧 [ENV-PATCH] skip→skip: {stats['env_patch_skip_to_skip']}",
            "=" * 60,
        ]
        return "\n".join(lines)


class MilestoneTestRunner:
    """Runs tests for milestone start, end, and optionally original states with retry support."""

    def __init__(
        self,
        milestone_id: str,
        output_dir: Path,
        docker_image: str = None,
        repo_id: str = None,
        test_scenario: str = None,
        verbose: bool = False,
        workers: int = 30,
        timeout: int = 30,
        log_file: Path = None,
        classify: bool = True,
        classification_output: Path = None,
        max_retries: int = 3,
        test_dir: str = None,
        pytest_extra_args: str = None,
        use_switch_state_script: bool = False,
        run_configs: List[Dict[str, Any]] = None,
        include_original: bool = False,
        base_commit: str = None,
    ):
        self.milestone_id = milestone_id

        # Determine docker image name
        if docker_image:
            self.docker_image = docker_image
        elif repo_id and test_scenario:
            # Use format: repo_id/test_scenario/milestone:latest (all lowercase for Docker)
            self.docker_image = f"{repo_id}/{test_scenario}/{milestone_id}:latest".lower()
        else:
            raise ValueError(
                "Either docker_image OR both repo_id and test_scenario must be provided. "
                f"Got: docker_image={docker_image}, repo_id={repo_id}, test_scenario={test_scenario}"
            )

        # Output directory is now required
        self.output_dir = output_dir

        self.verbose = verbose  # Whether to show Docker output in terminal
        self.workers = workers  # Number of parallel pytest workers
        self.timeout = timeout  # Timeout per test in seconds
        self.classify = classify  # Whether to auto-classify test results
        self.max_retries = max_retries  # Maximum number of test retries
        self.test_dir = test_dir  # Test directory (None = auto-discover)
        self.use_switch_state_script = use_switch_state_script  # Use switch-state.sh instead of git checkout
        self.include_original = include_original  # Whether to test at base_commit (original state)
        self.base_commit = base_commit  # Parent of first milestone commit (for original state)

        # Run configurations (list-based format)
        # Each config: {"name": "...", "pytest_args": "...", "description": "..."}
        if run_configs:
            self.run_configs = run_configs
        elif pytest_extra_args:
            # Backward compatibility: convert single pytest_extra_args to run_configs
            self.run_configs = [{"name": "default", "pytest_args": pytest_extra_args}]
        else:
            # Default: single run with no extra args
            self.run_configs = [{"name": "default", "pytest_args": ""}]

        # Classification output file
        if classification_output is None:
            self.classification_output = self.output_dir / f"{milestone_id}_classification.json"
        else:
            self.classification_output = classification_output

        # Log file for Docker output
        if log_file is None:
            self.log_file = self.output_dir / f"{milestone_id}_docker.log"
        else:
            self.log_file = log_file

        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def log(self, msg: str):
        """Print log message."""
        if self.verbose:
            print(f"[{self.milestone_id}] {msg}")

    def _check_existing_attempt(self, attempt_num: int, attempt_dir: Path) -> Optional[Dict[str, Any]]:
        """
        Check if a valid attempt result already exists.

        An attempt is considered valid if:
        1. The attempt directory exists
        2. classification.json exists and is valid JSON
        3. statistics.total_tests field exists and is > 0

        Args:
            attempt_num: The attempt number
            attempt_dir: The attempt directory path

        Returns:
            Attempt result dict if valid existing result found, None otherwise
        """
        classification_file = attempt_dir / "classification.json"

        if not classification_file.exists():
            return None

        try:
            with open(classification_file, "r") as f:
                classification = json.load(f)

            # Check if statistics.total_tests exists and is > 0
            statistics = classification.get("statistics", {})
            total_tests = statistics.get("total_tests", 0)

            if total_tests > 0:
                self.log(f"  Found valid existing attempt {attempt_num}: {total_tests} tests")
                return {
                    "attempt": attempt_num,
                    "status": "success",
                    "duration": 0,  # Unknown duration for cached results
                    "statistics": statistics,
                    "classification_file": str(classification_file.relative_to(self.output_dir)),
                    "cached": True,
                }
        except (json.JSONDecodeError, IOError, KeyError) as e:
            self.log(f"  Invalid existing attempt {attempt_num}: {e}")

        return None

    def run_docker_command(self, bash_script: str) -> subprocess.CompletedProcess:
        """Run a bash script inside Docker container."""

        # Prepare docker run command
        # Use --init to properly reap zombie child processes
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--init",
            "-v",
            f"{self.output_dir.absolute()}:/output",
            self.docker_image,
            "bash",
            "-c",
            bash_script,
        ]

        # Print key information if verbose
        if self.verbose:
            print(f"\n{'=' * 60}")
            print(f"Running tests for {self.milestone_id}")
            print(f"{'=' * 60}")
            print(f"Docker image: {self.docker_image}")
            print(f"Output dir:   {self.output_dir}")
            print(f"Log file:     {self.log_file}")
            print(f"Workers:      {self.workers}")
            print(f"Timeout:      {self.timeout}s")
            print(f"{'=' * 60}\n")

        # Open log file for writing (always save to file)
        with open(self.log_file, "w", encoding="utf-8") as log_f:
            # Write header
            log_f.write(f"=== Docker Test Run for {self.milestone_id} ===\n")
            log_f.write(f"Time: {__import__('datetime').datetime.now()}\n")
            log_f.write(f"Command: {' '.join(docker_cmd[:6])}...\n")
            log_f.write("=" * 80 + "\n\n")
            log_f.flush()

            if self.verbose:
                # Verbose mode: stream output to both terminal and file
                process = subprocess.Popen(
                    docker_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
                )

                for line in process.stdout:
                    print(line, end="")
                    log_f.write(line)
                    log_f.flush()

                process.wait()
                result = subprocess.CompletedProcess(
                    args=docker_cmd, returncode=process.returncode, stdout="", stderr=""
                )
            else:
                # Quiet mode: only save to file, no terminal output
                result = subprocess.run(docker_cmd, capture_output=True, text=True)
                log_f.write(result.stdout)
                if result.stderr:
                    log_f.write("\n=== STDERR ===\n")
                    log_f.write(result.stderr)

        return result

    def _generate_pytest_run_script(self, run_config: Dict[str, Any], state: str, output_file: str) -> str:
        """
        Generate pytest run command for a single run configuration.

        Args:
            run_config: Run configuration dict with 'name' and 'pytest_args'
            state: 'original', 'start', or 'end'
            output_file: Output JSON file name

        Returns:
            Bash commands to run pytest for this configuration
        """
        run_name = run_config.get("name", "default")
        pytest_args = run_config.get("pytest_args", "")

        return f"""
# Run {state} tests ({run_name})
echo "Running {state} tests ({run_name})..."
rm -f /output/{output_file}
pytest -v -n {self.workers} --continue-on-collection-errors --timeout={self.timeout} --json-report --json-report-file=/output/{output_file} {pytest_args} {self.test_dir or ""} || true

# If pytest failed completely, create an empty report
if [ ! -f /output/{output_file} ]; then
    echo "⚠️  {state.capitalize()} tests ({run_name}) failed to generate report"
    echo "Creating empty {output_file}..."
    TIMESTAMP=$(date +%s)
    cat > /output/{output_file} <<EOF
{{
  "created": ${{TIMESTAMP}}.0,
  "duration": 0,
  "exitcode": 5,
  "root": "/testbed",
  "environment": {{}},
  "summary": {{"collected": 0, "total": 0, "error": 1}},
  "collectors": [],
  "tests": []
}}
EOF
fi
"""

    def _run_single_attempt(self, attempt_num: int, attempt_dir: Path) -> Dict[str, Any]:
        """
        Run a single test attempt for the milestone.

        Supports multiple pytest runs per attempt (e.g., normal + integration tests).
        If multiple runs are configured, each run's results are saved separately
        and then merged into a single start.json/end.json.

        Args:
            attempt_num: Attempt number (1-indexed)
            attempt_dir: Output directory for this attempt

        Returns:
            Dictionary with attempt result
        """
        milestone_tag_start = f"milestone-{self.milestone_id}-start"
        milestone_tag_end = f"milestone-{self.milestone_id}-end"

        self.log(f"  Attempt {attempt_num}/{self.max_retries}...")

        # Create attempt directory
        attempt_dir.mkdir(parents=True, exist_ok=True)

        # Determine state switching commands based on config
        if self.use_switch_state_script:
            switch_to_start = "switch-state.sh start"
            switch_to_end = "switch-state.sh end"
            switch_to_original = "switch-state.sh original" if self.include_original else None
        else:
            switch_to_start = f"git checkout {milestone_tag_start}"
            switch_to_end = f"git checkout {milestone_tag_end}"
            switch_to_original = (
                f"git checkout {self.base_commit}" if self.include_original and self.base_commit else None
            )

        # Check if we need multiple runs
        num_runs = len(self.run_configs)
        needs_merge = num_runs > 1

        if needs_merge:
            self.log(f"     Multi-run mode: {num_runs} pytest runs configured")
            for i, rc in enumerate(self.run_configs):
                self.log(f"       Run {i + 1}: {rc.get('name', 'unnamed')} ({rc.get('pytest_args', 'no extra args')})")

        # Generate pytest run commands for original state (if requested)
        original_run_scripts = []
        original_output_files = []
        if self.include_original and self.base_commit:
            for run_config in self.run_configs:
                run_name = run_config.get("name", "default")
                if needs_merge:
                    output_file = f"original_{run_name}.json"
                else:
                    output_file = "original.json"
                original_output_files.append(output_file)
                original_run_scripts.append(self._generate_pytest_run_script(run_config, "original", output_file))

        # Generate pytest run commands for start state
        start_run_scripts = []
        start_output_files = []
        for run_config in self.run_configs:
            run_name = run_config.get("name", "default")
            if needs_merge:
                output_file = f"start_{run_name}.json"
            else:
                output_file = "start.json"
            start_output_files.append(output_file)
            start_run_scripts.append(self._generate_pytest_run_script(run_config, "start", output_file))

        # Generate pytest run commands for end state
        end_run_scripts = []
        end_output_files = []
        for run_config in self.run_configs:
            run_name = run_config.get("name", "default")
            if needs_merge:
                output_file = f"end_{run_name}.json"
            else:
                output_file = "end.json"
            end_output_files.append(output_file)
            end_run_scripts.append(self._generate_pytest_run_script(run_config, "end", output_file))

        # Prepare bash script to run inside container
        original_runs = "\n".join(original_run_scripts) if original_run_scripts else ""
        start_runs = "\n".join(start_run_scripts)
        end_runs = "\n".join(end_run_scripts)

        # Build original state testing section (only if enabled and base_commit available)
        original_section = ""
        if self.include_original and self.base_commit and switch_to_original:
            original_section = f"""
# === ORIGINAL STATE (base_commit: {self.base_commit}) ===
echo "Switching to original state (base_commit)..."
{switch_to_original}

# Run tests for original state
echo "Running tests for original state..."
cd /testbed

{original_runs}
"""

        bash_script = f"""
set -e

echo "=== Testing {self.milestone_id} (Attempt {attempt_num}) ==="

# Install pytest plugins if not already installed
echo "Installing pytest plugins..."
pip install pytest-xdist pytest-timeout pytest-json-report >/dev/null 2>&1 || true

# PYTHONPATH should be set in Dockerfile via runtime.python_path config
# Fallback to current working directory if not set
if [ -z "$PYTHONPATH" ]; then
    WORKDIR=$(pwd)
    echo "⚠️  PYTHONPATH not set in Docker image, using fallback: $WORKDIR"
    export PYTHONPATH=$WORKDIR
fi
{original_section}
# === START STATE (milestone-start tag) ===
echo "Switching to start state..."
{switch_to_start}

# Run tests for start state
echo "Running tests for start state..."
cd /testbed

{start_runs}

# === END STATE (milestone-end tag) ===
echo "Switching to end state..."
{switch_to_end}

# Run tests for end state
echo "Running tests for end state..."
cd /testbed

{end_runs}

echo "=== Tests completed for {self.milestone_id} (Attempt {attempt_num}) ==="
"""

        # Temporarily change output_dir and log_file for this attempt
        original_output_dir = self.output_dir
        original_log_file = self.log_file
        self.output_dir = attempt_dir
        self.log_file = attempt_dir / f"{self.milestone_id}_docker.log"

        start_time = time.time()

        # Run the docker command
        result = self.run_docker_command(bash_script)

        duration = time.time() - start_time

        # Restore original paths
        self.output_dir = original_output_dir
        self.log_file = original_log_file

        if result.returncode != 0:
            self.log(f"  ❌ Attempt {attempt_num} failed: Docker command error")
            return {
                "attempt": attempt_num,
                "status": "failed",
                "error": f"Docker command failed with exit code {result.returncode}",
                "duration": duration,
            }

        # Merge results if multiple runs were configured
        if needs_merge:
            # Merge original reports (if include_original is enabled)
            if self.include_original and original_output_files:
                self.log(f"     Merging {num_runs} original reports...")
                original_report_files = [attempt_dir / f for f in original_output_files]
                original_merged = attempt_dir / "original.json"
                if not merge_pytest_json_reports(original_report_files, original_merged, verbose=self.verbose):
                    self.log("  ⚠️  Warning: Could not merge original reports (continuing anyway)")
                    # Don't fail the whole attempt, just continue without original

            self.log(f"     Merging {num_runs} start reports...")
            start_report_files = [attempt_dir / f for f in start_output_files]
            start_merged = attempt_dir / "start.json"
            if not merge_pytest_json_reports(start_report_files, start_merged, verbose=self.verbose):
                self.log(f"  ❌ Attempt {attempt_num} failed: Could not merge start reports")
                return {
                    "attempt": attempt_num,
                    "status": "failed",
                    "error": "Failed to merge start pytest reports",
                    "duration": duration,
                }

            self.log(f"     Merging {num_runs} end reports...")
            end_report_files = [attempt_dir / f for f in end_output_files]
            end_merged = attempt_dir / "end.json"
            if not merge_pytest_json_reports(end_report_files, end_merged, verbose=self.verbose):
                self.log(f"  ❌ Attempt {attempt_num} failed: Could not merge end reports")
                return {
                    "attempt": attempt_num,
                    "status": "failed",
                    "error": "Failed to merge end pytest reports",
                    "duration": duration,
                }

        # Load test results
        start_file = attempt_dir / "start.json"
        end_file = attempt_dir / "end.json"
        original_file = attempt_dir / "original.json" if self.include_original else None

        if not start_file.exists() or not end_file.exists():
            self.log(f"  ❌ Attempt {attempt_num} failed: Missing result files")
            return {
                "attempt": attempt_num,
                "status": "failed",
                "error": f"Missing output files: start={start_file.exists()}, end={end_file.exists()}",
                "duration": duration,
            }

        # Classify test results (start vs end)
        classifier = TestClassifier(start_file=start_file, end_file=end_file, verbose=False)

        if not classifier.load_results():
            self.log(f"  ❌ Attempt {attempt_num} failed: Cannot load results")
            return {
                "attempt": attempt_num,
                "status": "failed",
                "error": "Failed to load test results",
                "duration": duration,
            }

        classification_results = classifier.classify_tests()

        # Save classification for this attempt
        classification_file = attempt_dir / "classification.json"
        with open(classification_file, "w") as f:
            json.dump(classification_results, f, indent=2)

        # Generate detailed summary files with fail/skip reasons
        start_summary_file = attempt_dir / "start_summary.json"
        end_summary_file = attempt_dir / "end_summary.json"
        try:
            convert_pytest_report_to_summary(start_file, start_summary_file)
            convert_pytest_report_to_summary(end_file, end_summary_file)
            self.log("     Generated start_summary.json and end_summary.json")
        except Exception as e:
            self.log(f"     Warning: Failed to generate summary files: {e}")

        # Generate original summary if include_original is enabled
        if self.include_original and original_file and original_file.exists():
            original_summary_file = attempt_dir / "original_summary.json"
            try:
                convert_pytest_report_to_summary(original_file, original_summary_file)
                self.log("     Generated original_summary.json")
            except Exception as e:
                self.log(f"     Warning: Failed to generate original summary: {e}")

        self.log(f"  ✅ Attempt {attempt_num} success: {classification_results['statistics']['total_tests']} tests")
        self.log(f"     pass→pass: {classification_results['statistics']['pass_to_pass']}")
        self.log(f"     fail→pass: {classification_results['statistics']['fail_to_pass']}")
        self.log(f"     pass→fail: {classification_results['statistics']['pass_to_fail']}")

        return {
            "attempt": attempt_num,
            "status": "success",
            "duration": duration,
            "statistics": classification_results["statistics"],
            "classification_file": str(classification_file.relative_to(self.output_dir)),
            "run_configs": [rc.get("name") for rc in self.run_configs],
        }

    def run_milestone_tests(self) -> Dict[str, Any]:
        """
        Run tests for both start and end states of a milestone with retry logic.

        The final result is determined by combining all attempts:
        - If ANY attempt succeeds, the milestone is considered SUCCESS
        - Statistics are aggregated from the best (last successful) attempt

        Returns:
            Dictionary with test results and metadata.
        """
        self.log(f"Starting test run for {self.milestone_id} (max_retries={self.max_retries})")

        # Run test attempts with retries
        all_attempts = []
        successful_attempts = []
        total_duration = 0

        for attempt_num in range(1, self.max_retries + 1):
            attempt_dir = self.output_dir / f"attempt_{attempt_num}"

            # Check if this attempt already has valid results (skip if exists)
            existing_result = self._check_existing_attempt(attempt_num, attempt_dir)
            if existing_result:
                print(f"  ⏭️  Attempt {attempt_num}/{self.max_retries} SKIPPED (reusing existing results)")
                all_attempts.append(existing_result)
                total_duration += existing_result.get("duration", 0)
                if existing_result["status"] == "success":
                    successful_attempts.append(existing_result)
                continue

            attempt_result = self._run_single_attempt(attempt_num, attempt_dir)

            all_attempts.append(attempt_result)
            total_duration += attempt_result.get("duration", 0)

            if attempt_result["status"] == "success":
                successful_attempts.append(attempt_result)
                print(f"  ✅ Attempt {attempt_num}/{self.max_retries} PASSED")
            else:
                print(
                    f"  ❌ Attempt {attempt_num}/{self.max_retries} FAILED: {attempt_result.get('error', 'Unknown error')}"
                )

        # Determine final status: SUCCESS if ANY attempt passed
        if successful_attempts:
            final_status = "success"

            # Merge results from all successful attempts to eliminate flaky test effects
            successful_attempt_dirs = [
                self.output_dir / f"attempt_{attempt['attempt']}" for attempt in successful_attempts
            ]
            merged_classification = merge_attempt_results(successful_attempt_dirs, verbose=self.verbose)

            if merged_classification:
                # Save merged classification to main output
                with open(self.classification_output, "w") as f:
                    json.dump(merged_classification, f, indent=2)

                # Print summary
                print(merged_classification["summary"])
                print(
                    f"\n📊 Merged classification (from {merged_classification['merged_from_attempts']} attempts) "
                    f"saved to: {self.classification_output}"
                )

                merged_statistics = merged_classification["statistics"]
            else:
                # Fallback to last successful attempt if merge fails
                best_result = successful_attempts[-1]
                best_attempt_dir = self.output_dir / f"attempt_{best_result['attempt']}"
                best_classification_file = best_attempt_dir / "classification.json"

                with open(best_classification_file, "r") as f:
                    best_classification = json.load(f)

                with open(self.classification_output, "w") as f:
                    json.dump(best_classification, f, indent=2)

                print(best_classification["summary"])
                print(
                    f"\n📊 Classification (from attempt {best_result['attempt']}) saved to: {self.classification_output}"
                )

                merged_statistics = best_result["statistics"]

            # Save aggregated summary
            summary_file = self.output_dir / "test_summary.json"
            with open(summary_file, "w") as f:
                json.dump(
                    {
                        "milestone_id": self.milestone_id,
                        "final_status": final_status,
                        "total_attempts": len(all_attempts),
                        "successful_attempts": len(successful_attempts),
                        "failed_attempts": len(all_attempts) - len(successful_attempts),
                        "total_duration": total_duration,
                        "merged_statistics": merged_statistics,
                        "all_attempts": all_attempts,
                    },
                    f,
                    indent=2,
                )

            print(f"\n✅ FINAL: SUCCESS ({len(successful_attempts)}/{len(all_attempts)} attempts passed)")
            print(f"   Merged statistics from {len(successful_attempts)} successful attempts:")
            print(f"   Total tests: {merged_statistics['total_tests']}")
            print(f"   pass→pass: {merged_statistics['pass_to_pass']}")
            print(f"   fail→pass: {merged_statistics['fail_to_pass']}")
            print(f"   pass→fail: {merged_statistics['pass_to_fail']}")

            # Prepare result metadata
            result = {
                "milestone_id": self.milestone_id,
                "status": final_status,
                "total_attempts": len(all_attempts),
                "successful_attempts": len(successful_attempts),
                "total_duration": total_duration,
                "output_dir": str(self.output_dir),
                "summary_file": str(summary_file),
            }

            # Add classification info
            if self.classify:
                result["classification"] = {
                    "status": "success",
                    "output_file": str(self.classification_output),
                    "statistics": merged_statistics,
                    "has_regressions": merged_statistics["pass_to_fail"] > 0,
                }

                if merged_statistics["pass_to_fail"] > 0:
                    print(f"\n⚠️  WARNING: Found {merged_statistics['pass_to_fail']} regression(s)!")

            return result
        else:
            # All attempts failed
            final_status = "failed"
            last_error = all_attempts[-1].get("error", "Unknown error")

            # Save aggregated failure summary
            summary_file = self.output_dir / "test_summary.json"
            with open(summary_file, "w") as f:
                json.dump(
                    {
                        "milestone_id": self.milestone_id,
                        "final_status": final_status,
                        "total_attempts": len(all_attempts),
                        "successful_attempts": 0,
                        "failed_attempts": len(all_attempts),
                        "total_duration": total_duration,
                        "all_attempts": all_attempts,
                        "last_error": last_error,
                    },
                    f,
                    indent=2,
                )

            print(f"\n❌ FINAL: FAILED (all {len(all_attempts)} attempts failed)")
            print(f"   Last error: {last_error}")

            return {
                "milestone_id": self.milestone_id,
                "status": final_status,
                "error": f"All {len(all_attempts)} attempts failed. Last error: {last_error}",
                "total_attempts": len(all_attempts),
                "successful_attempts": 0,
                "total_duration": total_duration,
                "output_dir": str(self.output_dir),
                "summary_file": str(summary_file),
            }


class BatchMilestoneRunner:
    """Batch processing for multiple milestones."""

    def __init__(
        self,
        work_dir: Path,
        project_root: Path,
        verbose: bool = False,
        workers: int = 30,
        timeout: int = 30,
        classify: bool = True,
        parallel: int = 1,
        max_retries: int = 3,
        include_original: bool = False,
    ):
        self.work_dir = Path(work_dir).resolve()
        self.project_root = project_root

        # Support dockerfiles/ or env_data/ directory names
        if (self.work_dir / "dockerfiles").exists():
            self.dockerfiles_dir = self.work_dir / "dockerfiles"
        elif (self.work_dir / "env_data").exists():
            self.dockerfiles_dir = self.work_dir / "env_data"
        else:
            self.dockerfiles_dir = self.work_dir / "dockerfiles"  # Default

        self.testbed_dir = self.work_dir / "testbed"

        # Support test_results/ or test_data/ directory names
        if (self.work_dir / "test_results").exists():
            self.test_results_dir = self.work_dir / "test_results"
        elif (self.work_dir / "test_data").exists():
            self.test_results_dir = self.work_dir / "test_data"
        else:
            self.test_results_dir = self.work_dir / "test_results"  # Default

        self.verbose = verbose
        self.workers = workers
        self.timeout = timeout
        self.classify = classify
        self.parallel = parallel  # Number of milestones to process in parallel
        self.max_retries = max_retries  # Maximum number of test retries
        self.include_original = include_original  # Whether to test at base_commit (original state)

        # Load test_dir and base_commits from metadata.json if available
        self.test_dir = None
        self.base_commits = {}  # milestone_id -> base_commit (parent of first milestone commit)
        metadata_file = self.work_dir / "metadata.json"
        if metadata_file.exists():
            try:
                with open(metadata_file) as f:
                    metadata = json.load(f)
                self.test_dir = metadata.get("test_dir")
                if self.test_dir:
                    print(f"📋 Using test_dir from metadata.json: {self.test_dir}")

                # Load base_commits for each milestone
                for milestone_data in metadata.get("milestones", []):
                    milestone_id = milestone_data.get("id")
                    base_commit = milestone_data.get("base_commit")
                    if milestone_id and base_commit:
                        self.base_commits[milestone_id] = base_commit

                if self.base_commits and self.include_original:
                    print(f"📋 Loaded base_commits for {len(self.base_commits)} milestones")
            except Exception as e:
                print(f"⚠️  Failed to read metadata.json: {e}")

        # Track results
        self.results = {"success": [], "failed": [], "skipped": []}

    def _load_milestone_test_config(self, milestone: str) -> List[Dict[str, Any]]:
        """
        Load test configuration for a specific milestone from test_config.json.

        The new format is a list of run configurations:
        [
            {"name": "default", "pytest_args": "", "description": "Normal tests"},
            {"name": "integration", "pytest_args": "--integration", "description": "Integration tests"}
        ]

        For backward compatibility, the old dict format is also supported:
        {"pytest_extra_args": "--integration", ...}
        This will be converted to: [{"name": "default", "pytest_args": "--integration"}]

        Args:
            milestone: Milestone ID (e.g., M011)

        Returns:
            List of run configurations. Each config has:
            - name: Run name (for file naming)
            - pytest_args: Extra pytest arguments for this run
            - description: Optional description
        """
        config_file = self.dockerfiles_dir / milestone / "test_config.json"
        if config_file.exists():
            try:
                with open(config_file) as f:
                    config = json.load(f)

                # New format: list of run configurations
                if isinstance(config, list):
                    if self.verbose:
                        print(f"📋 Loaded test config for {milestone}: {len(config)} run(s)")
                        for run_config in config:
                            print(f"   - {run_config.get('name', 'unnamed')}: {run_config.get('pytest_args', '')}")
                    return config

                # Old format: dict with pytest_extra_args
                # Convert to new format for backward compatibility
                if isinstance(config, dict):
                    pytest_args = config.get("pytest_extra_args", "")
                    use_switch_state = config.get("use_switch_state_script", False)
                    converted = [
                        {
                            "name": "default",
                            "pytest_args": pytest_args,
                            "use_switch_state_script": use_switch_state,
                            "_converted_from_old_format": True,
                        }
                    ]
                    if self.verbose:
                        print(f"📋 Loaded test config for {milestone} (old format): pytest_args={pytest_args}")
                    return converted

            except (json.JSONDecodeError, IOError) as e:
                print(f"⚠️  Failed to load test_config.json for {milestone}: {e}")

        # No config file: return default single run with no extra args
        return [{"name": "default", "pytest_args": ""}]

    def log(self, msg: str, level: str = "INFO"):
        """Log message with level."""
        colors = {"INFO": "\033[0;34m", "SUCCESS": "\033[0;32m", "WARNING": "\033[1;33m", "ERROR": "\033[0;31m"}
        reset = "\033[0m"
        prefix = {"INFO": "[INFO]", "SUCCESS": "[✓]", "WARNING": "[⚠]", "ERROR": "[✗]"}
        print(f"{colors.get(level, '')}{prefix.get(level, '[?]')}{reset} {msg}")

    def find_milestones(self) -> List[str]:
        """Find all available milestones from dockerfiles/env_data directory."""
        if not self.dockerfiles_dir.exists():
            return []

        milestones = []
        for dockerfile_dir in sorted(self.dockerfiles_dir.iterdir()):
            # Skip Base directory (base image) and only include directories with Dockerfile
            if dockerfile_dir.is_dir() and dockerfile_dir.name != "Base" and (dockerfile_dir / "Dockerfile").exists():
                milestones.append(dockerfile_dir.name)

        return milestones

    def run_single_milestone(self, milestone: str) -> bool:
        """Run tests for a single milestone."""
        self.log(f"Running tests for {milestone}...")

        # Determine output directory for this milestone
        output_dir = self.test_results_dir / milestone

        # Infer repo_id and test_scenario from work_dir
        repo_id, test_scenario = self._infer_repo_and_scenario()

        # Load milestone-specific test config (new list-based format)
        run_configs = self._load_milestone_test_config(milestone)

        # Determine use_switch_state_script from first config (applies to all runs)
        use_switch_state_script = run_configs[0].get("use_switch_state_script", False) if run_configs else False

        # Get base_commit for this milestone (if include_original is enabled)
        base_commit = self.base_commits.get(milestone) if self.include_original else None
        if self.include_original and not base_commit:
            self.log(f"Warning: --include-original requested but no base_commit found for {milestone}", "WARNING")

        # Create runner with run_configs
        runner = MilestoneTestRunner(
            milestone_id=milestone,
            docker_image=None,  # Will be inferred
            repo_id=repo_id,
            test_scenario=test_scenario,
            output_dir=output_dir,
            verbose=self.verbose,
            workers=self.workers,
            timeout=self.timeout,
            classify=self.classify,
            max_retries=self.max_retries,
            test_dir=self.test_dir,
            run_configs=run_configs,  # New: pass list of run configs
            use_switch_state_script=use_switch_state_script,
            include_original=self.include_original,
            base_commit=base_commit,
        )

        # Run tests
        result = runner.run_milestone_tests()

        if result["status"] == "success":
            self.log(f"Tests completed for {milestone}", "SUCCESS")
            # Check for regressions
            if result.get("classification", {}).get("has_regressions", False):
                self.log(f"WARNING: {milestone} has regressions!", "WARNING")
            return True
        else:
            self.log(f"Tests failed for {milestone}", "ERROR")
            return False

    def _infer_repo_and_scenario(self) -> tuple:
        """Infer repo_id and test_scenario from work_dir path."""
        path_parts = self.work_dir.parts

        # Try to find workspace directory (support both harness_workspace and srs_workspace)
        for workspace_name in ["harness_workspace", "srs_workspace"]:
            try:
                workspace_idx = path_parts.index(workspace_name)
                if len(path_parts) > workspace_idx + 2:
                    repo_id = path_parts[workspace_idx + 1]
                    test_scenario = path_parts[workspace_idx + 2]
                    return repo_id, test_scenario
            except (ValueError, IndexError):
                continue

        return None, None

    def process_milestone(self, milestone: str) -> bool:
        """Process a single milestone (run tests)."""
        print(f"\n{'=' * 60}")
        self.log(f"Processing {milestone}")
        print(f"{'=' * 60}")

        # Run tests
        if not self.run_single_milestone(milestone):
            self.results["failed"].append(milestone)
            return False

        self.results["success"].append(milestone)
        return True

    def show_summary(self):
        """Show processing summary."""
        print(f"\n{'=' * 60}")
        print("Batch Processing Summary")
        print(f"{'=' * 60}")

        if self.results["success"]:
            self.log(f"Successful ({len(self.results['success'])}): {', '.join(self.results['success'])}", "SUCCESS")

        if self.results["failed"]:
            self.log(f"Failed ({len(self.results['failed'])}): {', '.join(self.results['failed'])}", "ERROR")

        if self.results["skipped"]:
            self.log(f"Skipped ({len(self.results['skipped'])}): {', '.join(self.results['skipped'])}", "WARNING")

        print(f"{'=' * 60}")
        print(f"Results directory: {self.test_results_dir}")
        print(f"{'=' * 60}")

    def run(self, milestones: List[str]) -> bool:
        """Run the complete pipeline for multiple milestones."""
        if not milestones:
            self.log("No milestones to process", "ERROR")
            return False

        # Create results directory
        self.test_results_dir.mkdir(parents=True, exist_ok=True)

        start_time = time.time()

        # Process milestones (parallel or sequential)
        if self.parallel > 1:
            self.log(f"Running {len(milestones)} milestones with parallelism={self.parallel}")
            self._run_parallel(milestones)
        else:
            self.log(f"Running {len(milestones)} milestones sequentially")
            for milestone in milestones:
                self.process_milestone(milestone)

        elapsed = time.time() - start_time

        # Show summary
        self.show_summary()
        self.log(f"Total time: {elapsed:.1f} seconds")

        return len(self.results["failed"]) == 0

    def _run_parallel(self, milestones: List[str]):
        """Run milestones in parallel using ThreadPoolExecutor."""
        with ThreadPoolExecutor(max_workers=self.parallel) as executor:
            # Submit all tasks
            future_to_milestone = {
                executor.submit(self._process_milestone_safe, milestone): milestone for milestone in milestones
            }

            # Process completed tasks
            for future in as_completed(future_to_milestone):
                milestone = future_to_milestone[future]
                try:
                    success = future.result()
                    if not success:
                        self.log(f"Failed to process {milestone}", "ERROR")
                except Exception as e:
                    self.log(f"Exception processing {milestone}: {e}", "ERROR")
                    self.results["failed"].append(milestone)

    def _process_milestone_safe(self, milestone: str) -> bool:
        """
        Thread-safe wrapper for process_milestone.
        Returns True on success, False on failure.
        """
        try:
            return self.process_milestone(milestone)
        except Exception as e:
            self.log(f"Error processing {milestone}: {e}", "ERROR")
            self.results["failed"].append(milestone)
            return False


def main():
    parser = argparse.ArgumentParser(
        description="Run milestone tests in Docker and automatically classify results (supports single and batch modes)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single milestone mode
  python run_milestone_tests.py M001 --output-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/test_results

  # Batch mode: Multiple milestones
  python run_milestone_tests.py M001 M002 M003 --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2

  # Batch mode: All milestones
  python run_milestone_tests.py --all --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2

  # Batch mode: All milestones in parallel (4 at a time)
  python run_milestone_tests.py --all --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2 -p 4

  # Single milestone with verbose output
  python run_milestone_tests.py M001 --output-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/test_results -v

  # Custom workers and timeout
  python run_milestone_tests.py M001 --output-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/test_results -n 16 --timeout 60

  # Using custom docker image (single mode only)
  python run_milestone_tests.py M001 --output-dir ./test_results --docker-image my-custom-image:latest

Modes:
  Single: Provide one milestone + --output-dir (detailed output)
  Batch:  Provide multiple milestones or --all + --work-dir (summary output)

Note:
  - Single mode: Uses --output-dir, outputs to {output_dir}/{milestone_id}/
  - Batch mode: Uses --work-dir, outputs to {work_dir}/test_results/{milestone_id}/
  - repo_id and test_scenario are automatically inferred from paths
        """,
    )
    parser.add_argument("milestones", nargs="*", help="Milestone ID(s) (e.g., M001, M002, M003)")
    parser.add_argument("--all", action="store_true", help="Run tests for all available milestones (batch mode)")
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Working directory for batch mode (contains dockerfiles/env_data/ and testbed/)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for single mode (format: .../test_results, will create {output_dir}/{milestone_id})",
    )
    parser.add_argument(
        "--docker-image", default=None, help="Docker image to use (single mode only, overrides auto-inference)"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show real-time Docker output (logs always saved to file)"
    )
    parser.add_argument("-n", "--workers", type=int, default=30, help="Number of parallel pytest workers (default: 30)")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout per test in seconds (default: 30)")
    parser.add_argument(
        "-p",
        "--parallel",
        type=int,
        default=1,
        help="Number of milestones to process in parallel (batch mode only, default: 1 for sequential)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum number of test retries per milestone (default: 3). Uses pass-any logic.",
    )
    parser.add_argument(
        "--include-original",
        action="store_true",
        help="Also test at original state (base_commit = parent of first milestone commit). "
        "Outputs original.json and original_summary.json in addition to start.json and end.json.",
    )
    parser.add_argument(
        "--no-classify", action="store_true", help="Disable automatic test classification (enabled by default)"
    )
    parser.add_argument(
        "--classification-output",
        type=Path,
        default=None,
        help="Output file for classification results (single mode only, default: {output_dir}/{milestone_id}_classification.json)",
    )

    args = parser.parse_args()

    # Determine mode based on arguments
    is_batch_mode = bool(args.all or args.work_dir or len(args.milestones) > 1)

    # Batch mode
    if is_batch_mode:
        if not args.work_dir:
            print("❌ Batch mode requires --work-dir", file=sys.stderr)
            print(
                "   Use --work-dir to specify the working directory containing dockerfiles/env_data/ and testbed/",
                file=sys.stderr,
            )
            sys.exit(1)

        batch_runner = BatchMilestoneRunner(
            work_dir=args.work_dir,
            project_root=Path.cwd(),
            verbose=args.verbose,
            workers=args.workers,
            timeout=args.timeout,
            classify=not args.no_classify,
            parallel=args.parallel,
            max_retries=args.max_retries,
            include_original=args.include_original,
        )

        # Determine which milestones to run
        if args.all:
            milestones = batch_runner.find_milestones()
            if not milestones:
                print("❌ No milestones found", file=sys.stderr)
                sys.exit(1)
            print(f"📋 Found {len(milestones)} milestones: {', '.join(milestones)}\n")
        else:
            milestones = args.milestones

        # Run batch processing
        success = batch_runner.run(milestones)
        sys.exit(0 if success else 1)

    # Single milestone mode
    if not args.milestones:
        parser.print_help()
        sys.exit(1)

    if len(args.milestones) > 1:
        print("❌ Multiple milestones require batch mode (use --work-dir instead of --output-dir)", file=sys.stderr)
        sys.exit(1)

    milestone_id = args.milestones[0]

    if not args.output_dir:
        print("❌ Single milestone mode requires --output-dir", file=sys.stderr)
        sys.exit(1)

    # Infer repo_id and test_scenario from output_dir path (unless docker_image is provided)
    repo_id = None
    test_scenario = None

    if not args.docker_image:
        # Expected format: .../harness_workspace/{repo_id}/{test_scenario}/...
        #              or: .../srs_workspace/{repo_id}/{test_scenario}/...
        output_path = args.output_dir.resolve()
        path_parts = output_path.parts

        # Try to find workspace directory (support both harness_workspace and srs_workspace)
        workspace_found = False
        for workspace_name in ["harness_workspace", "srs_workspace"]:
            try:
                workspace_idx = path_parts.index(workspace_name)
                if len(path_parts) > workspace_idx + 2:
                    repo_id = path_parts[workspace_idx + 1]
                    test_scenario = path_parts[workspace_idx + 2]
                    print("📋 Inferred from output_dir:")
                    print(f"   repo_id: {repo_id}")
                    print(f"   test_scenario: {test_scenario}")
                    print()
                    workspace_found = True
                    break
            except ValueError:
                continue

        if not workspace_found:
            print(f"❌ Cannot infer repo_id and test_scenario from output_dir: {output_path}", file=sys.stderr)
            print("   Expected format: .../harness_workspace/{repo_id}/{test_scenario}/...", file=sys.stderr)
            print("                or: .../srs_workspace/{repo_id}/{test_scenario}/...", file=sys.stderr)
            return 1

    # Auto-append milestone_id to output_dir
    output_dir = args.output_dir / milestone_id

    # Create runner
    runner = MilestoneTestRunner(
        milestone_id=milestone_id,
        docker_image=args.docker_image,
        repo_id=repo_id,
        test_scenario=test_scenario,
        output_dir=output_dir,
        verbose=args.verbose,
        workers=args.workers,
        timeout=args.timeout,
        classify=not args.no_classify,
        classification_output=args.classification_output,
        max_retries=args.max_retries,
    )

    # Run tests
    result = runner.run_milestone_tests()

    # Print result metadata
    print("\n" + "=" * 60)
    print("Test Run Summary")
    print("=" * 60)
    print(json.dumps(result, indent=2))
    print("=" * 60)

    if result["status"] == "success":
        print(f"\n✅ Successfully tested {milestone_id}")
        print(f"   Start: {result.get('start_file', 'N/A')}")
        print(f"   End: {result.get('end_file', 'N/A')}")

        # Check classification results
        if "classification" in result:
            if result["classification"]["status"] == "success":
                print(f"   Classification: {result['classification']['output_file']}")

                # Exit with appropriate code based on regressions
                if result["classification"].get("has_regressions", False):
                    print("\n⚠️  WARNING: Exiting with code 2 due to regressions")
                    sys.exit(2)

        sys.exit(0)
    else:
        print(f"\n❌ Failed to test {milestone_id}")
        sys.exit(1)


if __name__ == "__main__":
    main()
