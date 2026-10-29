#!/usr/bin/env python3
"""
Milestone Test Runner CLI

Run tests for milestone states (original, start, end) using the unified test runner framework.

Features:
  - Automatic image building if not exists
  - Two-level merging:
    1. Within-attempt: merge multiple pytest runs (default, integration, etc.) into start.json/end.json
    2. Cross-attempt: merge results from multiple retries with flaky test detection
  - Automatic test classification (start->end comparison)
  - Support for single milestone or batch processing

Config file format (test_config.json):
    [
      {
        "name": "default",
        "test_states": ["start", "end"],
        "test_cmd": "pytest -n {workers} --json-report --json-report-file=/output/{output_file}",
        "description": "Normal tests"
      },
      {
        "name": "integration",
        "test_states": ["end"],
        "test_cmd": "pytest -n {workers} --json-report --json-report-file=/output/{output_file} --integration",
        "description": "Integration tests"
      }
    ]

Output Structure:
    {output_dir}/
    ├── {milestone_id}/
    │   ├── benchmark/                # default --output-scope benchmark
    │   │   ├── replicate_1/
    │   │   │   ├── start_default.json
    │   │   │   ├── start_integration.json  (if configured)
    │   │   │   ├── start.json              # Merged from all start_*.json
    │   │   │   ├── end_default.json
    │   │   │   ├── end_integration.json    (if configured)
    │   │   │   ├── end.json                # Merged from all end_*.json
    │   │   │   ├── original.json           (if --include-original)
    │   │   │   └── classification.json
    │   │   ├── replicate_2/
    │   │   │   └── ...
    │   │   ├── {milestone_id}_classification.json  # Merged from benchmark replicates
    │   │   ├── test_summary.json
    │   │   └── f2p_tests/             # Created by extract_f2p_tests.py
    │
    │   └── refinement/
    │       └── attempt_N/             # --output-scope refinement, Module 2 evidence only

Usage:
    # Single milestone
    python -m harness.test_runner.run_milestone_tests M001 \\
        --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2

    # Multiple milestones
    python -m harness.test_runner.run_milestone_tests M001 M002 M003 \\
        --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2

    # All milestones
    python -m harness.test_runner.run_milestone_tests --all \\
        --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2

    # With custom settings
    python -m harness.test_runner.run_milestone_tests M001 \\
        --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2 \\
        --workers 16 --timeout 60 --replicates 5

    # Force rerun (disable caching)
    python -m harness.test_runner.run_milestone_tests M001 \\
        --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2 \\
        --no-cache

Caching Behavior:
    By default, the runner caches completed benchmark replicates or
    refinement attempts:
    - If benchmark/replicate_N/ or refinement/attempt_N/ contains valid
      start.json and end.json, it will be skipped
    - Use --no-cache to force rerun all runs

    Example scenarios:
    | Previous run          | Current run           | Behavior                                      |
    |-----------------------|-----------------------|-----------------------------------------------|
    | --replicates 1        | --replicates 1        | Skip replicate_1 (cached)                    |
    | --replicates 1        | --replicates 3        | Skip replicate_1 (cached), run 2 and 3       |
    | --replicates 3        | --replicates 1        | Skip replicate_1 (cached)                    |
    | --replicates 1        | --replicates 3 --no-cache | Rerun all 3 replicates                  |
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .core.classifier import CLASSIFICATION_CATEGORIES, TestClassifier
from .core.docker import (
    DockerRunner,
    build_docker_image,
    check_image_exists,
)
from .core.report_parser import (
    FRAMEWORK_CONFIG,
    convert_to_summary,
    get_file_extension,
    namespace_test_ids,
    parse_test_report,
)
from .core.test_executor import get_workspace_switch_cmd
from .core.types import MilestoneTestConfig

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

OUTPUT_SCOPE_BENCHMARK = "benchmark"
OUTPUT_SCOPE_REFINEMENT = "refinement"
OUTPUT_SCOPES = (OUTPUT_SCOPE_BENCHMARK, OUTPUT_SCOPE_REFINEMENT)


def run_dir_parent(milestone_output_dir: Path, output_scope: str) -> Path:
    if output_scope == OUTPUT_SCOPE_REFINEMENT:
        return milestone_output_dir / "refinement"
    return milestone_output_dir / "benchmark"


def run_dir_name(output_scope: str, run_number: int) -> str:
    if output_scope == OUTPUT_SCOPE_REFINEMENT:
        return f"attempt_{run_number}"
    return f"replicate_{run_number}"


# =============================================================================
# Pytest Report Merging (within-attempt)
# =============================================================================


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
    OUTCOME_PRIORITY = {
        "passed": 1,
        "failed": 1,
        "error": 1,
        "xfailed": 2,
        "xpassed": 2,
        "skipped": 3,
    }

    merged_tests = {}  # nodeid -> test_result
    total_duration = 0
    last_created = 0
    all_collectors = []
    seen_collector_nodeids = set()
    environment = {}
    root = ""
    valid_reports = 0

    for report_file in report_files:
        if not report_file.exists():
            if verbose:
                logger.warning(f"Report file not found: {report_file}")
            continue

        try:
            with open(report_file, "r") as f:
                report = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            if verbose:
                logger.warning(f"Failed to load report {report_file}: {e}")
            continue

        valid_reports += 1
        total_duration += report.get("duration", 0)
        last_created = max(last_created, report.get("created", 0))

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
                merged_tests[nodeid] = test
            else:
                existing = merged_tests[nodeid]
                existing_priority = OUTCOME_PRIORITY.get(existing.get("outcome", "unknown"), 3)
                new_priority = OUTCOME_PRIORITY.get(outcome, 3)

                if new_priority < existing_priority:
                    merged_tests[nodeid] = test

    if valid_reports == 0:
        if verbose:
            logger.warning("No valid report files to merge")
        return False

    # Calculate summary statistics
    summary = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "error": 0,
        "xfailed": 0,
        "xpassed": 0,
    }

    for test in merged_tests.values():
        outcome = test.get("outcome", "unknown")
        if outcome in summary:
            summary[outcome] += 1

    summary["total"] = len(merged_tests)
    summary["collected"] = len(merged_tests)

    # Build merged report
    merged_report = {
        "created": last_created,
        "duration": total_duration,
        "exitcode": 1 if summary.get("failed", 0) > 0 or summary.get("error", 0) > 0 else 0,
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

    try:
        with open(output_file, "w") as f:
            json.dump(merged_report, f, indent=2)
        return True
    except IOError as e:
        if verbose:
            logger.error(f"Failed to write merged report: {e}")
        return False


# =============================================================================
# Cross-attempt Result Merging
# =============================================================================


def _copy_classification_list(value: Any) -> List[Any]:
    if not isinstance(value, list):
        return []
    copied = []
    for item in value:
        if isinstance(item, dict):
            copied.append(dict(item))
        else:
            copied.append(item)
    return copied


def _copy_error_outcomes(value: Any) -> Dict[str, List[str]]:
    copied = {
        "start": [],
        "end": [],
        "both": [],
        "start_only": [],
        "end_only": [],
    }
    if not isinstance(value, dict):
        return copied
    for key in copied:
        copied[key] = [item for item in value.get(key, []) if isinstance(item, str)]
    return copied


def single_attempt_classification_result(attempt_dir: Path, verbose: bool = False) -> Optional[Dict[str, Any]]:
    """
    Build the milestone-level classification artifact from one attempt.

    A single successful attempt is already classified by run_single_attempt().
    Re-parsing start.json/end.json through cross-attempt merge can drift for
    framework-specific mixed reports, so reuse the attempt classification as the
    source of truth and wrap it in the milestone-level schema.
    """
    classification_file = attempt_dir / "classification.json"
    if not classification_file.exists():
        if verbose:
            logger.warning(f"Classification file not found for single attempt: {classification_file}")
        return None

    try:
        with open(classification_file) as f:
            attempt_classification = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        if verbose:
            logger.warning(f"Failed to load single-attempt classification {classification_file}: {e}")
        return None

    summary = attempt_classification.get("summary")
    if not isinstance(summary, dict):
        if verbose:
            logger.warning(f"Single-attempt classification missing summary: {classification_file}")
        return None
    if not any(category in attempt_classification for category in CLASSIFICATION_CATEGORIES):
        if verbose:
            logger.warning(f"Single-attempt classification missing category lists: {classification_file}")
        return None

    classification = {
        category: _copy_classification_list(attempt_classification.get(category, []))
        for category in CLASSIFICATION_CATEGORIES
    }
    classification["error_outcomes"] = _copy_error_outcomes(attempt_classification.get("error_outcomes"))
    summary = dict(summary)
    computed_summary = TestClassifier().generate_summary(classification)
    for category, values in classification.items():
        if category == "error_outcomes":
            continue
        summary.setdefault(category, len(values))
    for key, value in computed_summary.items():
        summary.setdefault(key, value)
    stable_classification = {
        category: _copy_classification_list(values)
        for category, values in classification.items()
        if category != "error_outcomes"
    }

    return {
        "summary": summary,
        "classification": classification,
        "stable_classification": stable_classification,
        "flaky_tests": [],
        "merged_from_attempts": 1,
        "source_attempt": attempt_dir.name,
    }


def merge_attempt_results(
    attempt_dirs: List[Path],
    verbose: bool = False,
    framework: str = "pytest",
) -> Optional[Dict[str, Any]]:
    """
    Merge test results from multiple attempts to eliminate flaky test effects.

    For each test:
    - Collect start_outcome from all attempts
    - Collect end_outcome from all attempts
    - Merge using severity priority (fail > error > pass > skipped)
    - Classify based on merged outcomes

    Supports multiple test frameworks (pytest, go_test, maven, cargo, jest, mocha).

    Args:
        attempt_dirs: List of attempt directories containing start.json and end.json
        verbose: Whether to print verbose output
        framework: Test framework name (determines how to parse report files)

    Returns:
        Dictionary with merged classification results, or None if no valid attempts
    """
    all_start_results = []
    all_end_results = []
    valid_attempt_dirs = []

    for attempt_dir in attempt_dirs:
        start_file = attempt_dir / "start.json"
        end_file = attempt_dir / "end.json"

        if start_file.exists() and end_file.exists():
            try:
                # Use unified parser for framework-agnostic file reading
                start_data = parse_test_report(start_file, framework)
                end_data = parse_test_report(end_file, framework)
                all_start_results.append(start_data)
                all_end_results.append(end_data)
                valid_attempt_dirs.append(attempt_dir)
            except (json.JSONDecodeError, IOError) as e:
                if verbose:
                    logger.warning(f"Failed to load results from {attempt_dir}: {e}")

    if not all_start_results or not all_end_results:
        return None

    classifier = TestClassifier(framework=framework)

    def normalize_outcome(outcome: str) -> str:
        """Normalize raw framework outcomes to pass/fail/skipped/error."""
        return classifier._normalize_outcome(outcome)

    def merge_outcomes_to_state(outcomes: List[str]) -> str:
        """Merge raw outcomes using fail > error > pass > skipped priority."""
        normalized = [normalize_outcome(outcome) for outcome in outcomes]
        if not normalized:
            return "unknown"
        if "fail" in normalized:
            return "fail"
        if "error" in normalized:
            return "error"
        if "pass" in normalized:
            return "pass"
        if "skipped" in normalized:
            return "skipped"
        return "fail"

    def is_flaky_outcome_set(outcomes: List[str], seen_attempts: Set[str], expected_attempts: Set[str]) -> bool:
        if not outcomes:
            return False
        if seen_attempts != expected_attempts:
            return True
        if len(outcomes) <= 1:
            return False
        normalized = [normalize_outcome(outcome) for outcome in outcomes]
        return len(set(normalized)) > 1

    # Collect outcomes for each test from all attempts
    start_outcomes_per_test: Dict[str, List[str]] = {}
    end_outcomes_per_test: Dict[str, List[str]] = {}
    start_attempts_per_test: Dict[str, Set[str]] = {}
    end_attempts_per_test: Dict[str, Set[str]] = {}
    expected_attempt_names = {attempt_dir.name for attempt_dir in valid_attempt_dirs}

    for attempt_dir, results in zip(valid_attempt_dirs, all_start_results, strict=False):
        attempt_name = attempt_dir.name
        for test in results.get("tests", []):
            test_id = test.get("nodeid", "")
            if not test_id:
                continue
            outcome = test.get("outcome", "unknown")
            if test_id not in start_outcomes_per_test:
                start_outcomes_per_test[test_id] = []
            start_outcomes_per_test[test_id].append(outcome)
            start_attempts_per_test.setdefault(test_id, set()).add(attempt_name)

    for attempt_dir, results in zip(valid_attempt_dirs, all_end_results, strict=False):
        attempt_name = attempt_dir.name
        for test in results.get("tests", []):
            test_id = test.get("nodeid", "")
            if not test_id:
                continue
            outcome = test.get("outcome", "unknown")
            if test_id not in end_outcomes_per_test:
                end_outcomes_per_test[test_id] = []
            end_outcomes_per_test[test_id].append(outcome)
            end_attempts_per_test.setdefault(test_id, set()).add(attempt_name)

    # Handle collection errors from start state
    start_failed_collectors: Set[str] = set()
    for results in all_start_results:
        for collector in results.get("collectors", []):
            if collector.get("outcome") == "failed":
                nodeid = collector.get("nodeid", "")
                if nodeid:
                    start_failed_collectors.add(nodeid)

    for test_id in end_outcomes_per_test.keys():
        test_file = test_id.split("::")[0] if "::" in test_id else test_id
        if test_file in start_failed_collectors and test_id not in start_outcomes_per_test:
            start_outcomes_per_test[test_id] = ["error"]
            start_attempts_per_test[test_id] = set(expected_attempt_names)

    # Merge outcomes for each test using fail > error > pass > skipped priority.
    merged_start: Dict[str, str] = {
        test_id: merge_outcomes_to_state(outcomes) for test_id, outcomes in start_outcomes_per_test.items()
    }
    merged_end: Dict[str, str] = {
        test_id: merge_outcomes_to_state(outcomes) for test_id, outcomes in end_outcomes_per_test.items()
    }

    # Get all unique test IDs
    all_tests = set(merged_start.keys()) | set(merged_end.keys())

    # Classification categories (matching TestClassifier)
    classification = {category: [] for category in CLASSIFICATION_CATEGORIES}
    error_outcomes = {
        "start": [],
        "end": [],
        "both": [],
        "start_only": [],
        "end_only": [],
    }
    classification["error_outcomes"] = error_outcomes

    for test_id in all_tests:
        start_outcome = merged_start.get(test_id, "missing")
        end_outcome = merged_end.get(test_id, "missing")
        start_state = None if start_outcome == "missing" else normalize_outcome(start_outcome)
        end_state = None if end_outcome == "missing" else normalize_outcome(end_outcome)

        if start_state == "error" or end_state == "error":
            TestClassifier._record_error_outcome(error_outcomes, test_id, start_state, end_state)
            continue

        if start_outcome == "missing":
            # New test: none_to_pass, none_to_fail, or none_to_skipped
            category = f"none_to_{end_state}"
            classification[category].append(test_id)
            # Also add to aggregated list
            classification["new_tests"].append({"test_id": test_id, "end_outcome": end_outcome})
            continue

        if end_outcome == "missing":
            # Removed test: pass_to_none, fail_to_none, or skipped_to_none
            category = f"{start_state}_to_none"
            classification[category].append(test_id)
            # Also add to aggregated list
            classification["removed_tests"].append({"test_id": test_id, "start_outcome": start_outcome})
            continue

        category = f"{start_state}_to_{end_state}"
        classification[category].append(test_id)

    # Flaky test detection
    flaky_tests = []
    flaky_test_ids: Set[str] = set()

    for test_id in all_tests:
        start_outcomes = start_outcomes_per_test.get(test_id, [])
        end_outcomes = end_outcomes_per_test.get(test_id, [])
        start_attempts = start_attempts_per_test.get(test_id, set())
        end_attempts = end_attempts_per_test.get(test_id, set())

        start_is_flaky = is_flaky_outcome_set(start_outcomes, start_attempts, expected_attempt_names)
        end_is_flaky = is_flaky_outcome_set(end_outcomes, end_attempts, expected_attempt_names)

        if start_is_flaky or end_is_flaky:
            flaky_in = "both" if start_is_flaky and end_is_flaky else ("start" if start_is_flaky else "end")
            missing_start_attempts = sorted(expected_attempt_names - start_attempts)
            missing_end_attempts = sorted(expected_attempt_names - end_attempts)

            # Find category
            category = None
            for cat_name in CLASSIFICATION_CATEGORIES:
                cat_tests = classification.get(cat_name, [])
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
                    "start_attempts": sorted(start_attempts),
                    "end_attempts": sorted(end_attempts),
                    "missing_start_attempts": missing_start_attempts,
                    "missing_end_attempts": missing_end_attempts,
                    "start_merged": merged_start.get(test_id, "missing"),
                    "end_merged": merged_end.get(test_id, "missing"),
                }
            )
            flaky_test_ids.add(test_id)

    # Generate stable_classification (excluding flaky tests)
    stable_classification = {}
    for cat_name in CLASSIFICATION_CATEGORIES:
        cat_tests = classification.get(cat_name, [])
        stable_tests = []
        for t in cat_tests:
            t_id = t["test_id"] if isinstance(t, dict) else t
            if t_id not in flaky_test_ids:
                stable_tests.append(t)
        stable_classification[cat_name] = stable_tests

    # Calculate statistics (flat structure matching TestClassifier)
    stats = classifier.generate_summary(classification)
    stats.update(
        {
            "flaky_total": len(flaky_tests),
            "flaky_in_start": len([t for t in flaky_tests if t["flaky_in"] in ["start", "both"]]),
            "flaky_in_end": len([t for t in flaky_tests if t["flaky_in"] in ["end", "both"]]),
        }
    )

    # Put summary first in output (matching single attempt structure)
    return {
        "summary": stats,
        "classification": classification,
        "stable_classification": stable_classification,
        "flaky_tests": flaky_tests,
        "merged_from_attempts": len(valid_attempt_dirs),
    }


# =============================================================================
# Cache Helper Functions
# =============================================================================


def _file_sha256(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def write_attempt_manifest(
    attempt_dir: Path,
    *,
    source: str,
    milestone_id: str,
    attempt_num: int,
    image_name: str,
    dockerfile_dir: Path,
) -> None:
    attempt_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source": source,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "milestone_id": milestone_id,
        "attempt": attempt_num,
        "status": "started",
        "image_name": image_name,
        "inputs": {
            "dockerfile_sha256": _file_sha256(dockerfile_dir / "Dockerfile"),
            "test_config_sha256": _file_sha256(dockerfile_dir / "test_config.json"),
        },
    }
    (attempt_dir / "attempt_manifest.json").write_text(json.dumps(manifest, indent=2))


def _compact_report_metadata(path: Path) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "path": path.name,
        "exists": path.exists(),
        "sha256": _file_sha256(path) if path.exists() else None,
    }
    if not path.exists():
        return metadata
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        metadata["parse_error"] = str(e)
        return metadata
    if not isinstance(payload, dict):
        metadata["parse_error"] = "not a JSON object"
        return metadata
    summary = payload.get("summary")
    if isinstance(summary, dict):
        metadata["summary"] = {
            key: summary.get(key)
            for key in ("total", "collected", "passed", "failed", "skipped", "error")
            if key in summary
        }
    if "_framework" in payload:
        metadata["framework"] = payload.get("_framework")
    if "_returncode" in payload:
        metadata["returncode"] = payload.get("_returncode")
    if "_run_returncodes" in payload:
        metadata["run_returncodes"] = payload.get("_run_returncodes")
    tests = payload.get("tests")
    if isinstance(tests, list):
        metadata["tests_count"] = len(tests)
        metadata["synthetic_error_count"] = sum(
            1
            for test in tests
            if isinstance(test, dict)
            and str(test.get("nodeid", "")).startswith("__")
            and ("fatal" in str(test.get("nodeid", "")) or "timeout" in str(test.get("nodeid", "")))
        )
    return metadata


def update_attempt_manifest(
    attempt_dir: Path,
    *,
    result: Dict[str, Any],
    duration: float,
    config: MilestoneTestConfig,
) -> None:
    manifest_path = attempt_dir / "attempt_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}

    report_names = [
        "start.json",
        "end.json",
        "classification.json",
        "start_summary.json",
        "end_summary.json",
        "original.json",
        "original_summary.json",
    ]
    mode_reports = sorted(
        path.name
        for path in attempt_dir.glob("*.json")
        if path.name not in set(report_names) | {"attempt_manifest.json"}
    )
    manifest.update(
        {
            "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": result.get("status"),
            "duration": duration,
            "statistics": result.get("statistics"),
            "classification_file": result.get("classification_file"),
            "run_configs": result.get("run_configs") or [mode.name for mode in config.modes],
            "error": result.get("error"),
            "report_files": {
                name: _compact_report_metadata(attempt_dir / name)
                for name in report_names
                if (attempt_dir / name).exists()
            },
            "mode_reports": [_compact_report_metadata(attempt_dir / name) for name in mode_reports],
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2))


def check_attempt_completed(attempt_dir: Path) -> bool:
    """
    Check if an attempt has been completed successfully.

    An attempt is considered complete if both start.json and end.json exist
    and contain valid JSON data.

    Args:
        attempt_dir: Path to the attempt directory

    Returns:
        True if attempt is complete, False otherwise
    """
    if not attempt_dir.exists():
        return False

    start_file = attempt_dir / "start.json"
    end_file = attempt_dir / "end.json"

    if not start_file.exists() or not end_file.exists():
        return False

    # Validate that the files contain valid JSON
    try:
        with open(start_file) as f:
            start_data = json.load(f)
        with open(end_file) as f:
            end_data = json.load(f)

        # Basic sanity check: ensure they have test data
        if "tests" not in start_data or "tests" not in end_data:
            return False

        return True
    except (json.JSONDecodeError, IOError):
        return False


def load_cached_attempt_result(
    attempt_dir: Path,
    attempt_num: int,
    milestone_id: str,
    framework: str = "pytest",
) -> Optional[Dict[str, Any]]:
    """
    Load cached result from a completed attempt.

    Args:
        attempt_dir: Path to the attempt directory
        attempt_num: Attempt number
        milestone_id: Milestone ID for logging
        framework: Test framework name

    Returns:
        Dictionary with attempt result, or None if loading fails
    """
    classification_file = attempt_dir / "classification.json"

    if not classification_file.exists():
        return None

    try:
        with open(classification_file) as f:
            classification = json.load(f)

        summary = classification.get("summary", {})

        logger.info(
            f"[{milestone_id}] Attempt {attempt_num} (cached): "
            f"fail_to_pass={summary.get('fail_to_pass', 0)}, "
            f"pass_to_fail={summary.get('pass_to_fail', 0)}"
        )

        return {
            "attempt": attempt_num,
            "status": "success",
            "statistics": summary,
            "classification_file": str(classification_file.name),
            "cached": True,
        }
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"[{milestone_id}] Failed to load cached result from {attempt_dir}: {e}")
        return None


# =============================================================================
# Helper Functions
# =============================================================================


def discover_milestones(work_dir: Path) -> List[str]:
    """
    Discover available milestones from dockerfiles directory.

    Args:
        work_dir: Working directory containing dockerfiles/

    Returns:
        Sorted list of milestone IDs
    """
    dockerfiles_dir = work_dir / "dockerfiles"
    if not dockerfiles_dir.exists():
        return []

    milestones = []
    for item in dockerfiles_dir.iterdir():
        if not item.is_dir():
            continue
        # Check for required files. Milestone ids may be M001, maint_015,
        # or repo-specific labels supplied by metadata.
        if (item / "Dockerfile").exists() or (item / "test_config.json").exists():
            milestones.append(item.name)

    return sorted(milestones)


def load_base_commits(work_dir: Path) -> Dict[str, str]:
    """
    Load base commits from metadata.json.

    Args:
        work_dir: Working directory

    Returns:
        Dict mapping milestone ID to base commit SHA
    """
    metadata_path = work_dir / "metadata.json"
    if not metadata_path.exists():
        return {}

    with open(metadata_path) as f:
        metadata = json.load(f)

    base_commits = {}
    for milestone in metadata.get("milestones", []):
        milestone_id = milestone.get("id")
        base_commit = milestone.get("base_commit")
        if milestone_id and base_commit:
            base_commits[milestone_id] = base_commit

    return base_commits


def infer_repo_info(work_dir: Path) -> tuple:
    """
    Infer repo_id and test_scenario from work_dir path.

    Expected path format: .../harness_workspace/{repo_id}/{test_scenario}/...

    Returns:
        Tuple of (repo_id, test_scenario)
    """
    parts = work_dir.resolve().parts
    try:
        idx = parts.index("harness_workspace")
        repo_id = parts[idx + 1]
        test_scenario = parts[idx + 2]
        return repo_id, test_scenario
    except (ValueError, IndexError):
        return None, None


def get_switch_cmd(state: str, milestone_id: str, base_commit: Optional[str] = None) -> str:
    """
    Get git command to switch to a specific milestone state.

    Uses 'git checkout -f' to forcefully discard local changes before switching.
    This is necessary because Dockerfiles may apply compilation patches that modify
    tracked files, and these modifications would otherwise block state switching.

    Args:
        state: State name ("original", "start", "end")
        milestone_id: Milestone ID (e.g., "M001")
        base_commit: Base commit SHA for original state

    Returns:
        Git checkout command with -f flag
    """
    if state == "original":
        if not base_commit:
            raise ValueError("base_commit required for original state")
        return f"git checkout -f {base_commit}"
    elif state == "start":
        return f"git checkout -f milestone-{milestone_id}-start"
    elif state == "end":
        return f"git checkout -f milestone-{milestone_id}-end"
    else:
        raise ValueError(f"Unknown state: {state}")


def build_workspace_state_script(
    state: str, milestone_id: str, repos: List[str], mode: Any, test_cmd: str, post_script: str = ""
) -> str:
    """Script for one (state, suite) run in a multi-root workspace image (all repos switched, suite runs in its workdir)."""
    workdir = mode.workdir or f"/workspace/{mode.repo}"
    return f"""
set -e
echo ">>> Switching all repos to {state} state..."
{get_workspace_switch_cmd(state, milestone_id, repos)}
# Apply compilation patches if script exists
if [ -x /usr/local/bin/apply_patches.sh ]; then
    echo ">>> Applying compilation patches..."
    /usr/local/bin/apply_patches.sh
fi
cd {workdir}
echo ">>> Running {mode.name} tests ({mode.repo}) for {state} state..."
{test_cmd} || true
echo ">>> Done with {state}/{mode.name}"
{post_script}
"""


def get_default_test_cmd(
    workers: int,
    timeout: int,
    output_file: str,
    framework: str = "pytest",
) -> str:
    """
    Get default test command for a given framework.

    Args:
        workers: Number of parallel workers
        timeout: Test timeout in seconds
        output_file: Output file name for test results
        framework: Test framework name (pytest, go_test, maven, cargo, jest, mocha)

    Returns:
        Test command string
    """
    # Framework-specific default commands
    commands = {
        "pytest": f"pytest -n {workers} --timeout={timeout} --json-report --json-report-file=/output/{output_file}",
        "unittest": f"python -m pytest -n {workers} --timeout={timeout} --json-report --json-report-file=/output/{output_file}",
        "go_test": f"go test -json -timeout {timeout}s -parallel {workers} ./... 2>&1 | tee /output/{output_file}",
        "maven": f"mvn test -Dmaven.test.failure.ignore=true -Dsurefire.timeout={timeout} 2>&1 | tee /output/{output_file}",
        "gradle": f"gradle test --continue -Dtest.parallel=true -Dtest.maxParallelForks={workers} 2>&1 | tee /output/{output_file}",
        "cargo": f"cargo test --no-fail-fast -- --test-threads={workers} 2>&1 | tee /output/{output_file}",
        "jest": f"npx jest --json --outputFile=/output/{output_file} --testTimeout={timeout * 1000} --maxWorkers={workers}",
        "mocha": f"npx mocha --reporter json --timeout {timeout * 1000} --parallel --jobs {workers} > /output/{output_file}",
    }

    return commands.get(framework, commands["pytest"])


# =============================================================================
# Single Attempt Runner
# =============================================================================


def run_single_attempt(
    attempt_num: int,
    attempt_dir: Path,
    milestone_id: str,
    runner: DockerRunner,
    config: MilestoneTestConfig,
    base_commit: Optional[str],
    workers: int,
    timeout: int,
    verbose: bool,
    framework: str = "pytest",
    workers_override: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Run a single test attempt for the milestone.

    Returns:
        Dictionary with attempt result
    """
    attempt_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"[{milestone_id}] Attempt {attempt_num}...")

    # Get all unique states from config
    all_states = set(config.get_all_states())

    # Add original if configured
    if config.include_original and base_commit:
        all_states.add("original")

    # Run each (state, mode) pair
    # Store tuples of (Path, framework) to support per-mode framework overrides
    state_mode_files: Dict[str, List[Tuple[Path, str]]] = {state: [] for state in all_states}
    workspace_repos = config.workspace_repos() if config.is_workspace else []
    file_repo: Dict[Path, Optional[str]] = {}  # report path -> suite repo (id namespace)

    # Determine file extension based on framework
    file_ext = get_file_extension(framework)

    for state, mode in config.get_all_state_mode_pairs():
        # Use mode-specific framework if specified, otherwise use global framework
        mode_framework = mode.framework or framework
        mode_file_ext = get_file_extension(mode_framework)
        output_file = f"{state}_{mode.name}{mode_file_ext}"
        if mode.report_file:
            output_file = mode.report_file.format(state=state)

        # Build test command
        if mode.test_cmd:
            test_cmd = mode.test_cmd.format(
                workers=workers,
                timeout=timeout,
                output_file=output_file,
                milestone_id=milestone_id,
            )
        else:
            test_cmd = get_default_test_cmd(workers, timeout, output_file, framework)

        # Workers-override: regex-replace `--workers N` / `-n N` / `--parallel N`
        # in hard-coded test_cmd strings (e.g. Django runtests wrappers that don't
        # use the {workers} template placeholder). Used by retry logic to halve
        # parallelism on timeout.
        if workers_override is not None:
            n = max(1, int(workers_override))
            original_test_cmd = test_cmd
            test_cmd = re.sub(r"--workers\s+\d+", f"--workers {n}", test_cmd)
            test_cmd = re.sub(r"(?<!\w)-n\s+\d+", f"-n {n}", test_cmd)
            test_cmd = re.sub(r"--parallel\s+\d+", f"--parallel {n}", test_cmd)
            if test_cmd != original_test_cmd:
                print(f"  [workers-override] test_cmd parallelism rewritten to {n}")

        # Build switch command
        switch_cmd = get_switch_cmd(state, milestone_id, base_commit)

        # Build surefire collection script for Maven projects
        # This collects XML reports with module path prefixes for method-level granularity
        surefire_collect_script = ""
        if framework in ("maven", "gradle"):
            surefire_archive_name = f"{state}_surefire_reports.tar.gz"
            surefire_collect_script = f"""
echo ">>> Collecting Surefire XML reports for {state} state..."
mkdir -p /tmp/surefire_reports
# Find all surefire-reports directories and copy with module structure
find /testbed -path "*/target/surefire-reports" -type d | while read dir; do
    # Extract module path relative to /testbed
    module_path=$(dirname "$dir" | sed 's|/testbed/||' | sed 's|/target||')
    if [ -n "$module_path" ]; then
        mkdir -p "/tmp/surefire_reports/$module_path"
        cp -f "$dir"/TEST-*.xml "/tmp/surefire_reports/$module_path/" 2>/dev/null || true
    fi
done
# Create archive if any reports were collected
if [ -n "$(find /tmp/surefire_reports -name 'TEST-*.xml' 2>/dev/null)" ]; then
    cd /tmp && tar -czf /output/{surefire_archive_name} surefire_reports/
    echo ">>> Surefire reports archived to {surefire_archive_name}"
else
    echo ">>> No Surefire XML reports found"
fi
rm -rf /tmp/surefire_reports
"""

        # Build script
        # After git checkout, call apply_patches.sh if it exists to re-apply compilation fixes
        script = f"""
set -e
cd /testbed
echo ">>> Switching to {state} state..."
{switch_cmd}
# Apply compilation patches if script exists
if [ -x /usr/local/bin/apply_patches.sh ]; then
    echo ">>> Applying compilation patches..."
    /usr/local/bin/apply_patches.sh
fi
# Force recompilation of test files for Rust projects to ensure new tests are included
if [ -f Cargo.toml ] || [ -f cargo.toml ]; then
    echo ">>> Touching test files to force recompilation..."
    find . -path '*/tests/*.rs' -type f -exec touch {{}} \\; 2>/dev/null || true
    find . -path '*/src/*test*.rs' -type f -exec touch {{}} \\; 2>/dev/null || true
    echo ">>> Test files touched, forcing recompilation"
fi
echo ">>> Running {mode.name} tests for {state} state..."
{test_cmd} || true
echo ">>> Done with {state}/{mode.name}"
{surefire_collect_script}
"""

        if workspace_repos:
            script = build_workspace_state_script(
                state, milestone_id, workspace_repos, mode, test_cmd, surefire_collect_script
            )

        # Run in container
        returncode, stdout, stderr = runner.run(
            script,
            timeout=timeout * 60,
            extra_volumes={str(attempt_dir.absolute()): "/output"},
        )

        if verbose:
            logger.debug(f"[{milestone_id}] {state}_{mode.name}: returncode={returncode}")

        output_path = attempt_dir / output_file
        if output_path.exists():
            state_mode_files[state].append((output_path, mode_framework))
            file_repo[output_path] = mode.repo

    # Run original state if configured (with default mode only)
    if (
        config.include_original
        and base_commit
        and not workspace_repos  # workspace mode: no per-repo "original" state
        and "original" not in [s for s, m in config.get_all_state_mode_pairs()]
    ):
        output_file = f"original_default{file_ext}"
        test_cmd = get_default_test_cmd(workers, timeout, output_file, framework)
        switch_cmd = get_switch_cmd("original", milestone_id, base_commit)

        script = f"""
set -e
cd /testbed
echo ">>> Switching to original state..."
{switch_cmd}
# Apply compilation patches if script exists
if [ -x /usr/local/bin/apply_patches.sh ]; then
    echo ">>> Applying compilation patches..."
    /usr/local/bin/apply_patches.sh
fi
# Force recompilation of test files for Rust projects to ensure new tests are included
if [ -f Cargo.toml ] || [ -f cargo.toml ]; then
    echo ">>> Touching test files to force recompilation..."
    find . -path '*/tests/*.rs' -type f -exec touch {{}} \\; 2>/dev/null || true
    find . -path '*/src/*test*.rs' -type f -exec touch {{}} \\; 2>/dev/null || true
    echo ">>> Test files touched, forcing recompilation"
fi
echo ">>> Running tests for original state..."
{test_cmd} || true
echo ">>> Done with original state"
"""
        runner.run(
            script,
            timeout=timeout * 60,
            extra_volumes={str(attempt_dir.absolute()): "/output"},
        )

        output_path = attempt_dir / output_file
        if output_path.exists():
            state_mode_files["original"].append((output_path, framework))

    # Merge results per state (within-attempt merging)
    # mode_files is now List[Tuple[Path, str]] where str is the framework
    for state in all_states:
        mode_file_tuples = state_mode_files.get(state, [])
        if mode_file_tuples:
            merged_path = attempt_dir / f"{state}.json"
            if len(mode_file_tuples) == 1:
                # Single mode: parse and convert to standardized JSON format
                # This is important for non-JSON frameworks (maven, cargo, etc.)
                file_path, file_framework = mode_file_tuples[0]
                try:
                    parsed = namespace_test_ids(parse_test_report(file_path, file_framework), file_repo.get(file_path))
                    with open(merged_path, "w") as f:
                        json.dump(parsed, f, indent=2)
                except Exception as e:
                    logger.warning(f"[{milestone_id}] Failed to parse {state} report: {e}")
            else:
                # Multiple modes with potentially different frameworks
                # Parse each file with its own framework, then merge standardized results
                all_parsed_tests = []
                total_duration = 0.0
                summary_totals = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
                source_reports = []

                for file_path, file_framework in mode_file_tuples:
                    try:
                        parsed = namespace_test_ids(
                            parse_test_report(file_path, file_framework), file_repo.get(file_path)
                        )
                        all_parsed_tests.extend(parsed.get("tests", []))
                        total_duration += parsed.get("duration", 0)
                        for key in summary_totals:
                            summary_totals[key] += parsed.get("summary", {}).get(key, 0)
                        source_reports.append(
                            {
                                "path": str(file_path),
                                "framework": file_framework,
                                "summary": parsed.get("summary", {}),
                                "returncode": parsed.get("_returncode"),
                                "run_returncodes": parsed.get("_run_returncodes"),
                                "synthetic_error_count": _compact_report_metadata(file_path).get(
                                    "synthetic_error_count",
                                    0,
                                ),
                            }
                        )
                    except Exception as e:
                        logger.warning(f"[{milestone_id}] Failed to parse {file_path}: {e}")
                        source_reports.append(
                            {
                                "path": str(file_path),
                                "framework": file_framework,
                                "parse_error": str(e),
                            }
                        )

                # Build merged result
                summary_totals["total"] = len(all_parsed_tests)
                merged_result = {
                    "tests": all_parsed_tests,
                    "collectors": [],
                    "summary": summary_totals,
                    "duration": total_duration,
                    "_framework": "mixed",
                    "_merge_info": {
                        "source_files": [str(fp) for fp, _ in mode_file_tuples],
                        "frameworks": [fw for _, fw in mode_file_tuples],
                        "source_reports": source_reports,
                    },
                }
                with open(merged_path, "w") as f:
                    json.dump(merged_result, f, indent=2)

    # Verify both start.json and end.json exist
    start_file = attempt_dir / "start.json"
    end_file = attempt_dir / "end.json"

    if not start_file.exists() or not end_file.exists():
        return {
            "attempt": attempt_num,
            "status": "error",
            "error": f"Missing output files: start={start_file.exists()}, end={end_file.exists()}",
        }

    # Generate summary files with fail/skip reasons
    try:
        convert_to_summary(start_file, attempt_dir / "start_summary.json", framework)
        convert_to_summary(end_file, attempt_dir / "end_summary.json", framework)
        if config.include_original and (attempt_dir / "original.json").exists():
            convert_to_summary(attempt_dir / "original.json", attempt_dir / "original_summary.json", framework)
    except Exception as e:
        logger.warning(f"[{milestone_id}] Failed to generate summary files: {e}")

    # Classify results for this attempt (using framework-aware classifier)
    classifier = TestClassifier(framework=framework)
    classification = classifier.classify_from_files(start_file, end_file)
    summary = classifier.generate_summary(classification)

    # Put summary first in output
    classification_result = {"summary": summary, **classification}

    # Save classification for this attempt
    classification_file = attempt_dir / "classification.json"
    with open(classification_file, "w") as f:
        json.dump(classification_result, f, indent=2)

    logger.info(
        f"[{milestone_id}] Attempt {attempt_num} success: "
        f"fail_to_pass={summary['fail_to_pass']}, "
        f"pass_to_fail={summary['pass_to_fail']}"
    )

    return {
        "attempt": attempt_num,
        "status": "success",
        "statistics": summary,
        "classification_file": str(classification_file.name),
        "run_configs": [m.name for m in config.modes],
    }


# =============================================================================
# Main Milestone Runner
# =============================================================================


def run_single_milestone(
    milestone_id: str,
    work_dir: Path,
    output_dir: Path,
    repo_id: str,
    test_scenario: str,
    base_commit: Optional[str],
    workers: int,
    timeout: int,
    max_retries: int,
    include_original: bool,
    verbose: bool,
    framework: str = "pytest",
    force_rebuild_image: bool = False,
    use_cache: bool = True,
    attempt_offset: int = 0,
    attempt_source: str = "pipeline",
    output_scope: str = OUTPUT_SCOPE_BENCHMARK,
    workers_override: Optional[int] = None,
    no_network: bool = False,
) -> Dict[str, Any]:
    """
    Run tests for a single milestone using Docker with retry support.

    Supports multiple test frameworks (pytest, go_test, maven, cargo, jest, mocha).

    Args:
        force_rebuild_image: If True, rebuild the Docker image even if it already exists.
        use_cache: If True (default), skip already completed runs and reuse their results.
                   If False, rerun all runs from scratch.
        attempt_offset: Write run directories starting at attempt/replicate_{attempt_offset + 1}.
        attempt_source: Metadata source written to attempt_manifest.json.
        output_scope: "benchmark" writes benchmark/replicate_N plus benchmark final files.
                      "refinement" writes refinement/attempt_N only.
    """
    if output_scope not in OUTPUT_SCOPES:
        raise ValueError(f"Unsupported output_scope: {output_scope}")

    milestone_output_dir = output_dir / milestone_id
    milestone_output_dir.mkdir(parents=True, exist_ok=True)
    run_output_dir = run_dir_parent(milestone_output_dir, output_scope)
    run_output_dir.mkdir(parents=True, exist_ok=True)

    dockerfile_dir = work_dir / "dockerfiles" / milestone_id

    # Determine image name
    image_name = f"{repo_id}/{test_scenario}/{milestone_id}:latest".lower()
    # # Determine image name (sanitize for valid Docker tag)
    # # Docker image names must match: [a-z0-9]+(?:[._-][a-z0-9]+)*
    # import re
    # raw_name = f"{repo_id}_{test_scenario}_{milestone_id}".lower()
    # # Replace any non-alphanumeric chars with underscore, collapse multiples
    # sanitized = re.sub(r'[^a-z0-9]+', '_', raw_name).strip('_')
    # image_name = f"{sanitized}:latest"

    # Build image if not exists or force rebuild
    image_exists = check_image_exists(image_name)
    if force_rebuild_image or not image_exists:
        dockerfile_path = dockerfile_dir / "Dockerfile"
        if not dockerfile_path.exists():
            return {
                "status": "error",
                "milestone_id": milestone_id,
                "error": f"Dockerfile not found: {dockerfile_path}",
            }

        if force_rebuild_image and image_exists:
            logger.info(f"[{milestone_id}] Force rebuilding image {image_name}...")
        else:
            logger.info(f"[{milestone_id}] Building image {image_name}...")

        build_context = work_dir / "testbed"
        if not build_context.exists():
            return {
                "status": "error",
                "milestone_id": milestone_id,
                "error": f"Build context not found: {build_context}",
            }

        if not build_docker_image(str(dockerfile_path), image_name, build_context, verbose):
            return {
                "status": "error",
                "milestone_id": milestone_id,
                "error": f"Failed to build image {image_name}",
            }

    # Load test config first (needed to check Docker socket requirements)
    config_path = dockerfile_dir / "test_config.json"
    if config_path.exists():
        config = MilestoneTestConfig.from_file(config_path, include_original)
        logger.info(f"[{milestone_id}] Loaded config with {len(config.modes)} mode(s)")
    else:
        config = MilestoneTestConfig.default(include_original)
        logger.info(f"[{milestone_id}] Using default config")

    # Create Docker runner (with Docker socket if e2e tests need it)
    enable_docker_socket = config.requires_docker_socket_any()
    if enable_docker_socket:
        logger.info(f"[{milestone_id}] Enabling Docker socket mounting for e2e tests")
    # For e2e tests with testcontainers, we need:
    # - Docker socket mounting for testcontainers to create containers
    # - Host network so the test can reach containers spawned by testcontainers
    # - TESTCONTAINERS_RYUK_DISABLED=true to prevent Ryuk connection issues
    # Idle watchdog: kill container when CPU stays below threshold for N seconds.
    # Catches in-container deadlocks (e.g., Python multiprocessing futex) that
    # would otherwise wait the full validation timeout.
    idle_timeout_seconds: Optional[int] = None
    idle_env = os.environ.get("HARNESS_DOCKER_IDLE_TIMEOUT_SECONDS")
    if idle_env:
        try:
            idle_timeout_seconds = max(60, int(idle_env))
        except ValueError:
            logger.warning(f"Invalid HARNESS_DOCKER_IDLE_TIMEOUT_SECONDS={idle_env!r}; ignoring")
    runner = DockerRunner(
        image_name,
        network_none=no_network,
        enable_docker_socket=enable_docker_socket,
        use_host_network=enable_docker_socket,  # Use host network when testcontainers enabled
        extra_env={"TESTCONTAINERS_RYUK_DISABLED": "true"} if enable_docker_socket else {},
        run_label=f"{attempt_source}_{milestone_id}",
        idle_timeout_seconds=idle_timeout_seconds,
    )

    # Run test attempts/replicates.
    all_attempts = []
    successful_attempts = []
    total_duration = 0
    start_time = time.time()
    cached_count = 0

    for retry_index in range(1, max_retries + 1):
        attempt_num = attempt_offset + retry_index
        attempt_dir = run_output_dir / run_dir_name(output_scope, attempt_num)

        # Check cache: skip if attempt is already completed
        if use_cache and check_attempt_completed(attempt_dir):
            cached_result = load_cached_attempt_result(attempt_dir, attempt_num, milestone_id, framework)
            if cached_result:
                cached_result["duration"] = 0  # No time spent on cached attempts
                update_attempt_manifest(
                    attempt_dir,
                    result=cached_result,
                    duration=0,
                    config=config,
                )
                all_attempts.append(cached_result)
                successful_attempts.append(cached_result)
                cached_count += 1
                continue
            else:
                # Classification file missing or invalid, need to regenerate
                logger.info(
                    f"[{milestone_id}] Attempt {attempt_num} has data but missing classification, regenerating..."
                )

        write_attempt_manifest(
            attempt_dir,
            source=attempt_source,
            milestone_id=milestone_id,
            attempt_num=attempt_num,
            image_name=image_name,
            dockerfile_dir=dockerfile_dir,
        )
        attempt_start = time.time()

        attempt_result = run_single_attempt(
            attempt_num=attempt_num,
            attempt_dir=attempt_dir,
            milestone_id=milestone_id,
            runner=runner,
            config=config,
            base_commit=base_commit,
            workers=workers,
            timeout=timeout,
            verbose=verbose,
            framework=framework,
            workers_override=workers_override,
        )

        attempt_result["duration"] = time.time() - attempt_start
        update_attempt_manifest(
            attempt_dir,
            result=attempt_result,
            duration=attempt_result["duration"],
            config=config,
        )
        all_attempts.append(attempt_result)
        total_duration += attempt_result["duration"]

        if attempt_result["status"] == "success":
            successful_attempts.append(attempt_result)

    # Log cache statistics
    if cached_count > 0:
        logger.info(
            f"[{milestone_id}] Used {cached_count} cached attempt(s), ran {len(all_attempts) - cached_count} new attempt(s)"
        )

    elapsed = time.time() - start_time

    # Determine final status
    if successful_attempts:
        final_status = "success"

        # Merge results from all successful attempts (cross-attempt merging)
        successful_attempt_dirs = [
            run_output_dir / run_dir_name(output_scope, attempt["attempt"]) for attempt in successful_attempts
        ]
        if len(successful_attempt_dirs) == 1:
            merged_classification = single_attempt_classification_result(successful_attempt_dirs[0], verbose)
            if not merged_classification:
                merged_classification = merge_attempt_results(successful_attempt_dirs, verbose, framework)
        else:
            merged_classification = merge_attempt_results(successful_attempt_dirs, verbose, framework)

        if merged_classification:
            # Save merged classification only for post-loop benchmark output.
            classification_file = run_output_dir / f"{milestone_id}_classification.json"
            if output_scope == OUTPUT_SCOPE_BENCHMARK:
                with open(classification_file, "w") as f:
                    json.dump(merged_classification, f, indent=2)

            merged_summary = merged_classification["summary"]
        else:
            # Fallback to last successful attempt
            best_result = successful_attempts[-1]
            merged_summary = best_result["statistics"]

        summary_payload = {
            "milestone_id": milestone_id,
            "final_status": final_status,
            "output_scope": output_scope,
            "total_runs": len(all_attempts),
            "successful_runs": len(successful_attempts),
            "failed_runs": len(all_attempts) - len(successful_attempts),
            "cached_runs": cached_count,
            "total_attempts": len(all_attempts),
            "successful_attempts": len(successful_attempts),
            "failed_attempts": len(all_attempts) - len(successful_attempts),
            "cached_attempts": cached_count,
            "total_duration": total_duration,
            "summary": merged_summary,
            "all_attempts": all_attempts,
        }
        if output_scope == OUTPUT_SCOPE_BENCHMARK:
            summary_payload["all_replicates"] = all_attempts
            summary_file = run_output_dir / "test_summary.json"
            with open(summary_file, "w") as f:
                json.dump(summary_payload, f, indent=2)

        return {
            "status": final_status,
            "milestone_id": milestone_id,
            "elapsed_seconds": round(elapsed, 2),
            "total_attempts": len(all_attempts),
            "successful_attempts": len(successful_attempts),
            "output_scope": output_scope,
            "summary": merged_summary,
        }
    else:
        # All attempts failed
        last_error = all_attempts[-1].get("error", "Unknown error") if all_attempts else "No attempts were made"

        if output_scope == OUTPUT_SCOPE_BENCHMARK:
            summary_file = run_output_dir / "test_summary.json"
            with open(summary_file, "w") as f:
                json.dump(
                    {
                        "milestone_id": milestone_id,
                        "final_status": "failed",
                        "output_scope": output_scope,
                        "total_runs": len(all_attempts),
                        "successful_runs": 0,
                        "failed_runs": len(all_attempts),
                        "cached_runs": cached_count,
                        "total_attempts": len(all_attempts),
                        "successful_attempts": 0,
                        "failed_attempts": len(all_attempts),
                        "cached_attempts": cached_count,
                        "total_duration": total_duration,
                        "all_attempts": all_attempts,
                        "all_replicates": all_attempts,
                        "last_error": last_error,
                    },
                    f,
                    indent=2,
                )

        return {
            "status": "error",
            "milestone_id": milestone_id,
            "elapsed_seconds": round(elapsed, 2),
            "output_scope": output_scope,
            "error": f"All {len(all_attempts)} attempts failed. Last error: {last_error}",
        }


# =============================================================================
# CLI
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Run milestone tests using the unified test runner framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single milestone (Python/pytest - default)
  python -m harness.test_runner.run_milestone_tests M001 \\
      --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2

  # Multiple milestones
  python -m harness.test_runner.run_milestone_tests M001 M002 M003 \\
      --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2

  # All milestones in parallel
  python -m harness.test_runner.run_milestone_tests --all \\
      --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2 \\
      --parallel 4

  # Go project (go test)
  python -m harness.test_runner.run_milestone_tests M001 \\
      --work-dir DATA/harness_workspace/go_project/test_scenario \\
      --language go --test-framework go_test

  # Java/Maven project
  python -m harness.test_runner.run_milestone_tests M001 \\
      --work-dir DATA/harness_workspace/java_project/test_scenario \\
      --language java --test-framework maven

  # Rust/Cargo project
  python -m harness.test_runner.run_milestone_tests M001 \\
      --work-dir DATA/harness_workspace/rust_project/test_scenario \\
      --language rust --test-framework cargo

  # JavaScript/Jest project
  python -m harness.test_runner.run_milestone_tests M001 \\
      --work-dir DATA/harness_workspace/js_project/test_scenario \\
      --language javascript --test-framework jest

  # With original state testing
  python -m harness.test_runner.run_milestone_tests --milestone-id M001 \\
      --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2 \\
      --include-original

  # Post-loop benchmark sampling
  python -m harness.test_runner.run_milestone_tests --milestone-id M001 \\
      --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2 \\
      --workers 16 --timeout 60 --replicates 3

  # Module 2 refinement evidence
  python -m harness.test_runner.run_milestone_tests --milestone-id M001 \\
      --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2 \\
      --output-scope refinement --max-retries 1 --attempt-offset 2 --no-cache
        """,
    )

    # Positional arguments
    parser.add_argument("milestones", nargs="*", help="Milestone ID(s) to test (e.g., M001, M002)")

    # Named milestone argument (alternative to positional)
    parser.add_argument(
        "--milestone-id",
        type=str,
        default=None,
        help="Single milestone ID to test (alternative to positional argument)",
    )

    # Mode selection
    parser.add_argument("--all", action="store_true", help="Run tests for all available milestones")

    # Required paths
    parser.add_argument(
        "--work-dir", type=Path, required=True, help="Working directory (contains dockerfiles/, metadata.json, etc.)"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None, help="Output directory (default: {work_dir}/test_results)"
    )

    # Test configuration
    parser.add_argument("-n", "--workers", type=int, default=30, help="Number of parallel test workers (default: 30)")
    parser.add_argument("--timeout", type=int, default=300, help="Timeout for test execution in seconds (default: 300)")
    parser.add_argument(
        "--workers-override",
        type=int,
        default=None,
        help=(
            "If set, regex-replace `--workers N` and `-n N` flags in the rendered test_cmd. "
            "Used by retry logic to halve parallelism after a timeout so in-container "
            "multiprocessing deadlocks don't re-occur (8 -> 4 -> 2 -> 1)."
        ),
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Compatibility alias for number of runs when --replicates is not supplied (default: 3)",
    )
    parser.add_argument(
        "--replicates",
        type=int,
        default=None,
        help="Number of benchmark replicates to run (recommended for post-loop benchmark sampling)",
    )
    parser.add_argument("--include-original", action="store_true", help="Also test at original state (base_commit)")
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Run test containers with --network none (offline; deps must be resolved at image build time)",
    )
    parser.add_argument(
        "--force-rebuild-image",
        action="store_true",
        help="Force rebuild Docker image even if it already exists",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable caching: rerun all attempts even if they already completed",
    )
    parser.add_argument(
        "--attempt-offset",
        type=int,
        default=0,
        help=(
            "Offset attempt numbering by this amount. With --max-retries 1 and --attempt-offset 3, writes attempt_4."
        ),
    )
    parser.add_argument(
        "--attempt-source",
        choices=["pipeline", "agent_self_check"],
        default="pipeline",
        help="Metadata source written to attempt_manifest.json (default: pipeline)",
    )
    parser.add_argument(
        "--output-scope",
        choices=OUTPUT_SCOPES,
        default=OUTPUT_SCOPE_BENCHMARK,
        help=(
            "Output namespace: benchmark writes benchmark/replicate_N plus benchmark final files; "
            "refinement writes refinement/attempt_N only (default: benchmark)"
        ),
    )

    # Language and framework options
    parser.add_argument(
        "--language",
        type=str,
        default="python",
        help="Programming language (default: python). Options: python, java, javascript, go, rust",
    )
    parser.add_argument(
        "--test-framework",
        type=str,
        default="pytest",
        help=(
            "Test framework (default: pytest). Options: "
            "pytest, unittest (Python); maven, gradle (Java); "
            "jest, mocha (JavaScript); go_test (Go); cargo (Rust)"
        ),
    )

    # Execution options
    parser.add_argument(
        "-p", "--parallel", type=int, default=1, help="Number of milestones to process in parallel (default: 1)"
    )

    # Output options
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")

    args = parser.parse_args()

    # Set logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate work_dir
    work_dir = args.work_dir.resolve()
    if not work_dir.exists():
        print(f"Error: Work directory not found: {work_dir}", file=sys.stderr)
        return 1

    # Set output directory
    output_dir = args.output_dir or (work_dir / "test_results")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Determine milestones to run
    if args.all:
        milestones = discover_milestones(work_dir)
        if not milestones:
            print(f"Error: No milestones found in {work_dir / 'dockerfiles'}", file=sys.stderr)
            return 1
        print(f"Found {len(milestones)} milestones: {', '.join(milestones)}")
    elif args.milestone_id:
        # Named argument takes precedence if positional is empty
        milestones = [args.milestone_id]
    elif args.milestones:
        milestones = args.milestones
    else:
        milestones = discover_milestones(work_dir)
        if not milestones:
            print(f"Error: No milestones found in {work_dir / 'dockerfiles'}", file=sys.stderr)
            return 1
        print(f"No milestone ID supplied; running all {len(milestones)} milestone(s): {', '.join(milestones)}")

    # Load base commits
    base_commits = load_base_commits(work_dir)

    # Infer repo info
    repo_id, test_scenario = infer_repo_info(work_dir)
    if not repo_id:
        print("Error: Cannot infer repo_id from work_dir path", file=sys.stderr)
        return 1

    # Validate framework
    framework = args.test_framework
    if framework not in FRAMEWORK_CONFIG:
        print(f"Error: Unsupported test framework '{framework}'", file=sys.stderr)
        print(f"Available frameworks: {', '.join(FRAMEWORK_CONFIG.keys())}", file=sys.stderr)
        return 1
    if args.attempt_offset < 0:
        print("Error: --attempt-offset must be >= 0", file=sys.stderr)
        return 1
    run_count = args.replicates if args.replicates is not None else args.max_retries
    if run_count < 1:
        print("Error: --replicates/--max-retries must be >= 1", file=sys.stderr)
        return 1

    print(f"Repository: {repo_id}")
    print(f"Test scenario: {test_scenario}")
    print(f"Language: {args.language}, Framework: {framework}")
    print(f"Output directory: {output_dir}")
    print(f"Workers: {args.workers}, Timeout: {args.timeout}s, Runs: {run_count}")
    print(f"Output scope: {args.output_scope}")
    if args.attempt_offset:
        print(f"Attempt offset: {args.attempt_offset}")
    print(f"Attempt source: {args.attempt_source}")
    if args.include_original:
        print("Include original: Yes")
    if args.force_rebuild_image:
        print("Force rebuild image: Yes")
    if args.no_cache:
        print("Cache disabled: Yes (will rerun all attempts)")
    else:
        print("Cache enabled: Yes (will skip completed attempts)")
    print()

    # Run tests
    all_results = {}
    start_time = time.time()

    if args.parallel > 1 and len(milestones) > 1:
        # Parallel execution
        print(f"Running {len(milestones)} milestones in parallel (max {args.parallel} concurrent)")

        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = {}
            for milestone_id in milestones:
                base_commit = base_commits.get(milestone_id)

                future = executor.submit(
                    run_single_milestone,
                    milestone_id=milestone_id,
                    work_dir=work_dir,
                    output_dir=output_dir,
                    repo_id=repo_id,
                    test_scenario=test_scenario,
                    base_commit=base_commit,
                    workers=args.workers,
                    timeout=args.timeout,
                    max_retries=run_count,
                    include_original=args.include_original,
                    verbose=args.verbose,
                    framework=framework,
                    force_rebuild_image=args.force_rebuild_image,
                    use_cache=not args.no_cache,
                    attempt_offset=args.attempt_offset,
                    attempt_source=args.attempt_source,
                    output_scope=args.output_scope,
                    workers_override=args.workers_override,
                    no_network=args.no_network,
                )
                futures[future] = milestone_id

            for future in as_completed(futures):
                milestone_id = futures[future]
                try:
                    result = future.result()
                    all_results[milestone_id] = result
                    status = "OK" if result["status"] == "success" else "FAILED"
                    print(f"[{milestone_id}] {status}")
                except Exception as e:
                    all_results[milestone_id] = {"status": "error", "error": str(e)}
                    print(f"[{milestone_id}] ERROR: {e}")
    else:
        # Sequential execution
        for i, milestone_id in enumerate(milestones):
            print(f"[{i + 1}/{len(milestones)}] Testing {milestone_id}...")
            base_commit = base_commits.get(milestone_id)

            result = run_single_milestone(
                milestone_id=milestone_id,
                work_dir=work_dir,
                output_dir=output_dir,
                repo_id=repo_id,
                test_scenario=test_scenario,
                base_commit=base_commit,
                workers=args.workers,
                timeout=args.timeout,
                max_retries=run_count,
                include_original=args.include_original,
                verbose=args.verbose,
                framework=framework,
                force_rebuild_image=args.force_rebuild_image,
                use_cache=not args.no_cache,
                attempt_offset=args.attempt_offset,
                attempt_source=args.attempt_source,
                output_scope=args.output_scope,
                workers_override=args.workers_override,
                no_network=args.no_network,
            )
            all_results[milestone_id] = result

            status = "OK" if result["status"] == "success" else "FAILED"
            elapsed = result.get("elapsed_seconds", 0)
            print(f"[{milestone_id}] {status} ({elapsed:.1f}s)")

    # Summary
    total_time = time.time() - start_time
    success_count = sum(1 for r in all_results.values() if r["status"] == "success")
    error_count = len(all_results) - success_count

    print()
    print("=" * 60)
    print(f"Summary: {success_count} succeeded, {error_count} failed")
    print(f"Total time: {total_time:.1f}s")

    # Save overall benchmark summary. Refinement runs are attempt-scoped and
    # must not overwrite post-loop benchmark artifacts.
    if args.output_scope == OUTPUT_SCOPE_BENCHMARK:
        summary_dir = output_dir / "benchmark"
        summary_dir.mkdir(parents=True, exist_ok=True)
        summary_path = summary_dir / "batch_summary.json"
        summary = {
            "total_milestones": len(milestones),
            "success": success_count,
            "failed": error_count,
            "total_time_seconds": round(total_time, 2),
            "language": args.language,
            "test_framework": framework,
            "output_scope": args.output_scope,
            "milestones": all_results,
        }
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        print(f"Summary saved to: {summary_path}")
    else:
        print("Refinement scope: benchmark batch_summary.json not written")

    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
