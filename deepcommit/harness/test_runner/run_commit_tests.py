#!/usr/bin/env python3
"""
Commit Test Runner CLI

Run tests for individual commits (before/after comparison) using the unified test runner framework.

Features:
  - Automatic image building if not exists
  - Two-level merging:
    1. Within-attempt: merge multiple pytest runs (default, integration, etc.) into test.json/fix.json
    2. Cross-attempt: merge results from multiple retries with flaky test detection
  - Automatic test classification (before->after comparison)
  - Support for single commit or batch processing
  - Cleanup of commit-level images after tests

Config file format (test_config.json):
    [
      {
        "name": "default",
        "test_states": ["test", "fix"],
        "test_cmd": "pytest -n {workers} --json-report --json-report-file=/output/{output_file}",
        "description": "Normal tests"
      },
      {
        "name": "integration",
        "test_states": ["test", "fix"],
        "test_cmd": "pytest -n {workers} --json-report --json-report-file=/output/{output_file} --integration",
        "description": "Integration tests"
      }
    ]

Output Structure:
    {output_dir}/
    ├── {commit_sha}/
    │   ├── attempt_1/
    │   │   ├── test_default.json
    │   │   ├── test_integration.json
    │   │   ├── test.json              # Merged from all test_*.json
    │   │   ├── fix_default.json
    │   │   ├── fix_integration.json
    │   │   ├── fix.json               # Merged from all fix_*.json
    │   │   └── classification.json
    │   ├── attempt_2/
    │   │   └── ...
    │   ├── {commit_sha}_classification.json  # Merged from all attempts
    │   └── test_summary.json
"""

import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from deepcommit.harness.utils.pytest_report_utils import convert_pytest_report_to_summary

from .core.classifier import TestClassifier
from .core.docker import (
    DockerRunner,
    build_docker_image,
    check_image_exists,
    cleanup_docker_image,
)
from .core.merger import is_flaky, merge_outcome
from .core.types import CommitTestConfig

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)


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


def merge_attempt_results(attempt_dirs: List[Path], verbose: bool = False) -> Optional[Dict[str, Any]]:
    """
    Merge test results from multiple attempts to eliminate flaky test effects.

    For each test:
    - Collect test_outcome from all attempts
    - Collect fix_outcome from all attempts
    - Merge using pass-any logic (pass > skip > fail)
    - Classify based on merged outcomes

    Args:
        attempt_dirs: List of attempt directories containing test.json and fix.json
        verbose: Whether to print verbose output

    Returns:
        Dictionary with merged classification results, or None if no valid attempts
    """
    all_test_results = []
    all_fix_results = []
    valid_attempt_dirs = []

    for attempt_dir in attempt_dirs:
        test_file = attempt_dir / "test.json"
        fix_file = attempt_dir / "fix.json"

        if test_file.exists() and fix_file.exists():
            try:
                with open(test_file) as f:
                    test_data = json.load(f)
                with open(fix_file) as f:
                    fix_data = json.load(f)
                all_test_results.append(test_data)
                all_fix_results.append(fix_data)
                valid_attempt_dirs.append(attempt_dir)
            except (json.JSONDecodeError, IOError) as e:
                if verbose:
                    logger.warning(f"Failed to load results from {attempt_dir}: {e}")

    if not all_test_results or not all_fix_results:
        return None

    # Collect outcomes for each test from all attempts
    test_outcomes_per_test: Dict[str, List[str]] = {}
    fix_outcomes_per_test: Dict[str, List[str]] = {}

    for results in all_test_results:
        for test in results.get("tests", []):
            test_id = test.get("nodeid", "")
            outcome = test.get("outcome", "unknown")
            if test_id not in test_outcomes_per_test:
                test_outcomes_per_test[test_id] = []
            test_outcomes_per_test[test_id].append(outcome)

    for results in all_fix_results:
        for test in results.get("tests", []):
            test_id = test.get("nodeid", "")
            outcome = test.get("outcome", "unknown")
            if test_id not in fix_outcomes_per_test:
                fix_outcomes_per_test[test_id] = []
            fix_outcomes_per_test[test_id].append(outcome)

    # Handle collection errors from test state
    test_failed_collectors: Set[str] = set()
    for results in all_test_results:
        for collector in results.get("collectors", []):
            if collector.get("outcome") == "failed":
                nodeid = collector.get("nodeid", "")
                if nodeid:
                    test_failed_collectors.add(nodeid)

    for test_id in fix_outcomes_per_test.keys():
        test_file = test_id.split("::")[0] if "::" in test_id else test_id
        if test_file in test_failed_collectors and test_id not in test_outcomes_per_test:
            test_outcomes_per_test[test_id] = ["error"]

    # Merge outcomes for each test using pass-any logic
    merged_test: Dict[str, str] = {
        test_id: merge_outcome(outcomes) for test_id, outcomes in test_outcomes_per_test.items()
    }
    merged_fix: Dict[str, str] = {
        test_id: merge_outcome(outcomes) for test_id, outcomes in fix_outcomes_per_test.items()
    }

    # Get all unique test IDs
    all_tests = set(merged_test.keys()) | set(merged_fix.keys())

    # Classification categories (matching TestClassifier)
    classification = {
        "pass_to_pass": [],
        "pass_to_fail": [],
        "pass_to_skipped": [],
        "fail_to_pass": [],
        "fail_to_fail": [],
        "fail_to_skipped": [],
        "skipped_to_pass": [],
        "skipped_to_fail": [],
        "skipped_to_skipped": [],
        "new_tests": [],
        "removed_tests": [],
    }

    def normalize_outcome(outcome: str) -> str:
        """Normalize outcome to: pass, fail, skipped."""
        if outcome == "skipped":
            return "skipped"
        elif outcome == "passed":
            return "pass"
        else:
            return "fail"

    for test_id in all_tests:
        test_outcome = merged_test.get(test_id, "missing")
        fix_outcome = merged_fix.get(test_id, "missing")

        if test_outcome == "missing":
            classification["new_tests"].append({"test_id": test_id, "fix_outcome": fix_outcome})
            continue

        if fix_outcome == "missing":
            classification["removed_tests"].append({"test_id": test_id, "test_outcome": test_outcome})
            continue

        # Normalize and categorize
        test_state = normalize_outcome(test_outcome)
        fix_state = normalize_outcome(fix_outcome)
        category = f"{test_state}_to_{fix_state}"
        classification[category].append(test_id)

    # Flaky test detection
    flaky_tests = []
    flaky_test_ids: Set[str] = set()

    for test_id in all_tests:
        test_outcomes = test_outcomes_per_test.get(test_id, [])
        fix_outcomes = fix_outcomes_per_test.get(test_id, [])

        test_is_flaky = is_flaky(test_outcomes)
        fix_is_flaky = is_flaky(fix_outcomes)

        if test_is_flaky or fix_is_flaky:
            flaky_in = "both" if test_is_flaky and fix_is_flaky else ("test" if test_is_flaky else "fix")

            # Find category
            category = None
            for cat_name, cat_tests in classification.items():
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
                    "test_outcomes": test_outcomes,
                    "fix_outcomes": fix_outcomes,
                    "test_merged": merged_test.get(test_id, "missing"),
                    "fix_merged": merged_fix.get(test_id, "missing"),
                }
            )
            flaky_test_ids.add(test_id)

    # Generate stable_classification (excluding flaky tests)
    stable_classification = {}
    for cat_name, cat_tests in classification.items():
        stable_tests = []
        for t in cat_tests:
            t_id = t["test_id"] if isinstance(t, dict) else t
            if t_id not in flaky_test_ids:
                stable_tests.append(t)
        stable_classification[cat_name] = stable_tests

    # Calculate statistics (flat structure matching TestClassifier)
    stats = {
        # Category counts
        "pass_to_pass": len(classification["pass_to_pass"]),
        "pass_to_fail": len(classification["pass_to_fail"]),
        "pass_to_skipped": len(classification["pass_to_skipped"]),
        "fail_to_pass": len(classification["fail_to_pass"]),
        "fail_to_fail": len(classification["fail_to_fail"]),
        "fail_to_skipped": len(classification["fail_to_skipped"]),
        "skipped_to_pass": len(classification["skipped_to_pass"]),
        "skipped_to_fail": len(classification["skipped_to_fail"]),
        "skipped_to_skipped": len(classification["skipped_to_skipped"]),
        "new_tests": len(classification["new_tests"]),
        "removed_tests": len(classification["removed_tests"]),
        # Totals
        "total_before": len(all_tests) - len(classification["new_tests"]),
        "total_after": len(all_tests) - len(classification["removed_tests"]),
        # Flaky stats
        "flaky_total": len(flaky_tests),
        "flaky_in_test": len([t for t in flaky_tests if t["flaky_in"] in ["test", "both"]]),
        "flaky_in_fix": len([t for t in flaky_tests if t["flaky_in"] in ["fix", "both"]]),
    }

    # Put summary first in output (matching single attempt structure)
    return {
        "summary": stats,
        "classification": classification,
        "stable_classification": stable_classification,
        "flaky_tests": flaky_tests,
        "merged_from_attempts": len(valid_attempt_dirs),
    }


# =============================================================================
# Helper Functions
# =============================================================================


def get_dockerfile_path(commit_sha: str, work_dir: Path, dockerfile_arg: Optional[str] = None) -> Optional[str]:
    """Get dockerfile path for a commit."""
    if dockerfile_arg:
        return dockerfile_arg

    commit_short = commit_sha[:7]
    default_path = work_dir / "commit_level" / "dockerfiles" / commit_short / "Dockerfile"

    if default_path.exists():
        return str(default_path)
    return None


def get_switch_cmd(state: str, commit_sha: str, test_dir: str = "test/") -> str:
    """Get git command to switch to a specific state."""
    if state == "test":
        return f"git checkout {commit_sha}^ && git checkout {commit_sha} -- {test_dir}"
    elif state == "fix":
        return f"git checkout {commit_sha}"
    else:
        raise ValueError(f"Unknown state: {state}")


def get_default_test_cmd(language: str, test_framework: str, workers: int, output_file: str) -> Optional[str]:
    """Get default test command based on language and test framework."""
    if language == "python" and test_framework == "pytest":
        parts = ["pytest"]
        if workers > 0:
            parts.append(f"-n {workers}")
        parts.append(f"--json-report --json-report-file=/output/{output_file}")
        return " ".join(parts)
    return None


def load_milestone_commits(work_dir: Path, milestone_id: str) -> List[str]:
    """Load commits for a specific milestone from milestones.csv."""
    milestones_csv = work_dir / "milestones.csv"
    if not milestones_csv.exists():
        raise FileNotFoundError(f"milestones.csv not found: {milestones_csv}")

    with open(milestones_csv, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("id") == milestone_id:
                commits_str = row.get("commits", "") or row.get("core_pr_commits", "")
                if commits_str:
                    return [c.strip() for c in commits_str.split(";") if c.strip()]

    raise ValueError(f"Milestone {milestone_id} not found in milestones.csv")


def infer_repo_info(work_dir: Path) -> Optional[str]:
    """Infer repo_id from work_dir path."""
    parts = work_dir.resolve().parts
    try:
        idx = parts.index("harness_workspace")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return None


# =============================================================================
# Single Attempt Runner
# =============================================================================


def run_single_attempt(
    attempt_num: int,
    attempt_dir: Path,
    commit_sha: str,
    runner: DockerRunner,
    config: CommitTestConfig,
    language: str,
    test_framework: str,
    test_dir: str,
    workers: int,
    timeout: int,
    verbose: bool,
) -> Dict[str, Any]:
    """
    Run a single test attempt for the commit.

    Returns:
        Dictionary with attempt result
    """
    short_sha = commit_sha[:7]
    attempt_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"[{short_sha}] Attempt {attempt_num}...")

    # Get all unique states from config
    all_states = set()
    for mode in config.modes:
        all_states.update(mode.test_states)

    # Run each (state, mode) pair
    state_mode_files: Dict[str, List[Path]] = {state: [] for state in all_states}

    for state, mode in config.get_all_state_mode_pairs():
        output_file = f"{state}_{mode.name}.json"

        # Build test command
        if mode.test_cmd:
            test_cmd = mode.test_cmd.format(
                workers=workers,
                timeout=timeout,
                output_file=output_file,
            )
        else:
            test_cmd = get_default_test_cmd(language, test_framework, workers, output_file)
            if test_cmd is None:
                return {
                    "attempt": attempt_num,
                    "status": "error",
                    "error": f"No default test command for {language}/{test_framework}",
                }

        # Build switch command
        switch_cmd = get_switch_cmd(state, commit_sha, test_dir)

        # Build script
        script = f"""
set -e
cd /testbed
echo ">>> Switching to {state} state..."
{switch_cmd}
echo ">>> Running {mode.name} tests..."
{test_cmd} || true
echo ">>> Done"
"""

        # Run in container
        returncode, stdout, stderr = runner.run(
            script,
            timeout=timeout * 60,
            extra_volumes={str(attempt_dir.absolute()): "/output"},
        )

        if verbose:
            logger.debug(f"[{short_sha}] {state}_{mode.name}: returncode={returncode}")

        output_path = attempt_dir / output_file
        if output_path.exists():
            state_mode_files[state].append(output_path)

    # Merge results per state (within-attempt merging)
    for state in all_states:
        mode_files = state_mode_files[state]
        if mode_files:
            merged_path = attempt_dir / f"{state}.json"
            if len(mode_files) == 1:
                # Only one mode, just copy
                import shutil

                shutil.copy(mode_files[0], merged_path)
            else:
                # Multiple modes, merge them
                if not merge_pytest_json_reports(mode_files, merged_path, verbose):
                    logger.warning(f"[{short_sha}] Failed to merge {state} reports")

    # Verify both test.json and fix.json exist
    test_file = attempt_dir / "test.json"
    fix_file = attempt_dir / "fix.json"

    if not test_file.exists() or not fix_file.exists():
        return {
            "attempt": attempt_num,
            "status": "error",
            "error": f"Missing output files: test={test_file.exists()}, fix={fix_file.exists()}",
        }

    # Generate summary files with fail/skip reasons
    test_summary_file = attempt_dir / "test_summary.json"
    fix_summary_file = attempt_dir / "fix_summary.json"
    try:
        convert_pytest_report_to_summary(test_file, test_summary_file)
        convert_pytest_report_to_summary(fix_file, fix_summary_file)
        if verbose:
            logger.debug(f"[{short_sha}] Generated test_summary.json and fix_summary.json")
    except Exception as e:
        logger.warning(f"[{short_sha}] Failed to generate summary files: {e}")

    # Classify results for this attempt
    classifier = TestClassifier()
    classification = classifier.classify_from_files(test_file, fix_file)
    summary = classifier.generate_summary(classification)

    # Put summary first in output
    classification_result = {"summary": summary, **classification}

    # Save classification for this attempt
    classification_file = attempt_dir / "classification.json"
    with open(classification_file, "w") as f:
        json.dump(classification_result, f, indent=2)

    logger.info(
        f"[{short_sha}] Attempt {attempt_num} success: "
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
# Main Commit Runner
# =============================================================================


def run_single_commit(
    commit_sha: str,
    work_dir: Path,
    repo_path: Path,
    output_dir: Path,
    repo_id: str,
    dockerfile_arg: Optional[str],
    image_name_arg: Optional[str],
    language: str,
    test_framework: str,
    test_dir: str,
    workers: int,
    timeout: int,
    max_retries: int,
    verbose: bool,
) -> Dict[str, Any]:
    """Run tests for a single commit using Docker with retry support."""
    short_sha = commit_sha[:7]
    commit_output_dir = output_dir / short_sha
    commit_output_dir.mkdir(parents=True, exist_ok=True)

    # Use provided image name or generate one
    if image_name_arg:
        image_name = image_name_arg
        dockerfile_path = None
    else:
        dockerfile_path = get_dockerfile_path(commit_sha, work_dir, dockerfile_arg)
        if not dockerfile_path:
            return {
                "status": "error",
                "commit_sha": commit_sha,
                "short_sha": short_sha,
                "error": f"No dockerfile found for commit {short_sha}",
            }
        image_name = f"{repo_id}/commit-{short_sha}:latest".lower()

    # Build image if not exists
    if not check_image_exists(image_name):
        if image_name_arg:
            return {
                "status": "error",
                "commit_sha": commit_sha,
                "short_sha": short_sha,
                "error": f"Specified image not found: {image_name}",
            }

        logger.info(f"[{short_sha}] Building image {image_name}...")

        build_context = work_dir / "testbed"
        if not build_context.exists():
            return {
                "status": "error",
                "commit_sha": commit_sha,
                "short_sha": short_sha,
                "error": f"Build context not found: {build_context}",
            }

        if not build_docker_image(dockerfile_path, image_name, build_context, verbose):
            return {
                "status": "error",
                "commit_sha": commit_sha,
                "short_sha": short_sha,
                "error": f"Failed to build image {image_name}",
            }

    # Create Docker runner
    runner = DockerRunner(image_name)

    # Load test config
    commit_config_dir = work_dir / "commit_level" / "dockerfiles" / short_sha
    config_path = commit_config_dir / "test_config.json"

    if config_path.exists():
        config = CommitTestConfig.from_file(config_path)
        logger.info(f"[{short_sha}] Loaded config with {len(config.modes)} mode(s)")
    else:
        config = CommitTestConfig.default()
        logger.info(f"[{short_sha}] Using default config")

    # Run test attempts with retries
    all_attempts = []
    successful_attempts = []
    total_duration = 0
    start_time = time.time()

    for attempt_num in range(1, max_retries + 1):
        attempt_dir = commit_output_dir / f"attempt_{attempt_num}"
        attempt_start = time.time()

        attempt_result = run_single_attempt(
            attempt_num=attempt_num,
            attempt_dir=attempt_dir,
            commit_sha=commit_sha,
            runner=runner,
            config=config,
            language=language,
            test_framework=test_framework,
            test_dir=test_dir,
            workers=workers,
            timeout=timeout,
            verbose=verbose,
        )

        attempt_result["duration"] = time.time() - attempt_start
        all_attempts.append(attempt_result)
        total_duration += attempt_result["duration"]

        if attempt_result["status"] == "success":
            successful_attempts.append(attempt_result)

    elapsed = time.time() - start_time

    # Cleanup image after test (only if we built it)
    if not image_name_arg:
        cleanup_docker_image(image_name, verbose)

    # Determine final status
    if successful_attempts:
        final_status = "success"

        # Merge results from all successful attempts (cross-attempt merging)
        successful_attempt_dirs = [
            commit_output_dir / f"attempt_{attempt['attempt']}" for attempt in successful_attempts
        ]
        merged_classification = merge_attempt_results(successful_attempt_dirs, verbose)

        if merged_classification:
            # Save merged classification
            classification_file = commit_output_dir / f"{short_sha}_classification.json"
            with open(classification_file, "w") as f:
                json.dump(merged_classification, f, indent=2)

            merged_summary = merged_classification["summary"]
        else:
            # Fallback to last successful attempt
            best_result = successful_attempts[-1]
            merged_summary = best_result["statistics"]

        # Save test summary
        summary_file = commit_output_dir / "test_summary.json"
        with open(summary_file, "w") as f:
            json.dump(
                {
                    "commit_sha": commit_sha,
                    "short_sha": short_sha,
                    "final_status": final_status,
                    "total_attempts": len(all_attempts),
                    "successful_attempts": len(successful_attempts),
                    "failed_attempts": len(all_attempts) - len(successful_attempts),
                    "total_duration": total_duration,
                    "summary": merged_summary,
                    "all_attempts": all_attempts,
                },
                f,
                indent=2,
            )

        return {
            "status": final_status,
            "commit_sha": commit_sha,
            "short_sha": short_sha,
            "elapsed_seconds": round(elapsed, 2),
            "total_attempts": len(all_attempts),
            "successful_attempts": len(successful_attempts),
            "summary": merged_summary,
        }
    else:
        # All attempts failed
        last_error = all_attempts[-1].get("error", "Unknown error")

        summary_file = commit_output_dir / "test_summary.json"
        with open(summary_file, "w") as f:
            json.dump(
                {
                    "commit_sha": commit_sha,
                    "short_sha": short_sha,
                    "final_status": "failed",
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

        return {
            "status": "error",
            "commit_sha": commit_sha,
            "short_sha": short_sha,
            "elapsed_seconds": round(elapsed, 2),
            "error": f"All {len(all_attempts)} attempts failed. Last error: {last_error}",
        }


# =============================================================================
# CLI
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Run commit-level tests using the unified test runner framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test specific commits
  python -m harness.test_runner.run_commit_tests \\
      --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2 \\
      --repo-path DATA/github_data/repos/urllib3_urllib3 \\
      --commits 23f2287 f9d37ad

  # Test all commits in a milestone
  python -m harness.test_runner.run_commit_tests \\
      --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2 \\
      --repo-path DATA/github_data/repos/urllib3_urllib3 \\
      --milestone-id M001
        """,
    )

    # Required paths
    parser.add_argument(
        "--work-dir", type=Path, required=True, help="Working directory (contains commit_level/, dockerfiles/, etc.)"
    )
    parser.add_argument("--repo-path", type=Path, required=True, help="Path to repository")
    parser.add_argument(
        "--output-dir", type=Path, default=None, help="Output directory (default: {work_dir}/commit_level/test_results)"
    )

    # Commit selection
    commit_group = parser.add_mutually_exclusive_group(required=True)
    commit_group.add_argument("--commits", nargs="+", help="Specific commit SHA(s) to test")
    commit_group.add_argument("--milestone-id", help="Test all commits in this milestone")

    # Docker configuration
    parser.add_argument("--dockerfile", help="Path to Dockerfile")
    parser.add_argument("--image-name", help="Docker image name to use")

    # Language and framework
    parser.add_argument("--language", default="python", help="Programming language")
    parser.add_argument("--test-framework", default="pytest", help="Test framework")
    parser.add_argument("--test-dir", default="test/", help="Test directory path")

    # Test configuration
    parser.add_argument("-n", "--workers", type=int, default=30, help="Parallel pytest workers")
    parser.add_argument("--timeout", type=int, default=300, help="Timeout in seconds")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retry attempts")

    # Execution options
    parser.add_argument("-p", "--parallel", type=int, default=1, help="Parallel commits")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    work_dir = args.work_dir.resolve()
    if not work_dir.exists():
        print(f"Error: Work directory not found: {work_dir}", file=sys.stderr)
        return 1

    repo_path = args.repo_path.resolve()
    if not repo_path.exists():
        print(f"Error: Repository path not found: {repo_path}", file=sys.stderr)
        return 1

    output_dir = args.output_dir or (work_dir / "commit_level" / "test_results")
    output_dir.mkdir(parents=True, exist_ok=True)

    repo_id = infer_repo_info(work_dir)
    if not repo_id:
        print("Error: Cannot infer repo_id from work_dir path", file=sys.stderr)
        return 1

    # Determine commits
    if args.milestone_id:
        try:
            commits = load_milestone_commits(work_dir, args.milestone_id)
            print(f"Found {len(commits)} commits in milestone {args.milestone_id}")
        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    else:
        commits = args.commits

    if not commits:
        print("Error: No commits to test", file=sys.stderr)
        return 1

    print(f"Repository: {repo_id}")
    print(f"Language: {args.language}, Test framework: {args.test_framework}")
    print(f"Test directory: {args.test_dir}")
    print(f"Output directory: {output_dir}")
    print(f"Workers: {args.workers}, Timeout: {args.timeout}s, Max retries: {args.max_retries}")
    print(f"Commits to test: {len(commits)}")
    print()

    # Run tests
    all_results = {}
    start_time = time.time()

    from concurrent.futures import ThreadPoolExecutor, as_completed

    if args.parallel > 1 and len(commits) > 1:
        print(f"Running {len(commits)} commits in parallel (max {args.parallel} concurrent)")

        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = {}
            for commit_sha in commits:
                future = executor.submit(
                    run_single_commit,
                    commit_sha=commit_sha,
                    work_dir=work_dir,
                    repo_path=repo_path,
                    output_dir=output_dir,
                    repo_id=repo_id,
                    dockerfile_arg=args.dockerfile,
                    image_name_arg=args.image_name,
                    language=args.language,
                    test_framework=args.test_framework,
                    test_dir=args.test_dir,
                    workers=args.workers,
                    timeout=args.timeout,
                    max_retries=args.max_retries,
                    verbose=args.verbose,
                )
                futures[future] = commit_sha

            for future in as_completed(futures):
                commit_sha = futures[future]
                short_sha = commit_sha[:7]
                try:
                    result = future.result()
                    all_results[commit_sha] = result
                    status = "OK" if result["status"] == "success" else "FAILED"
                    print(f"[{short_sha}] {status}")
                except Exception as e:
                    all_results[commit_sha] = {"status": "error", "error": str(e)}
                    print(f"[{short_sha}] ERROR: {e}")
    else:
        for i, commit_sha in enumerate(commits):
            short_sha = commit_sha[:7]
            print(f"[{i + 1}/{len(commits)}] Testing commit {short_sha}...")

            result = run_single_commit(
                commit_sha=commit_sha,
                work_dir=work_dir,
                repo_path=repo_path,
                output_dir=output_dir,
                repo_id=repo_id,
                dockerfile_arg=args.dockerfile,
                image_name_arg=args.image_name,
                language=args.language,
                test_framework=args.test_framework,
                test_dir=args.test_dir,
                workers=args.workers,
                timeout=args.timeout,
                max_retries=args.max_retries,
                verbose=args.verbose,
            )
            all_results[commit_sha] = result

            status = "OK" if result["status"] == "success" else "FAILED"
            elapsed = result.get("elapsed_seconds", 0)
            print(f"[{short_sha}] {status} ({elapsed:.1f}s)")

    # Summary
    total_time = time.time() - start_time
    success_count = sum(1 for r in all_results.values() if r["status"] == "success")
    error_count = len(all_results) - success_count

    print()
    print("=" * 60)
    print(f"Summary: {success_count} succeeded, {error_count} failed")
    print(f"Total time: {total_time:.1f}s")

    summary_path = output_dir / "batch_summary.json"
    summary = {
        "total_commits": len(commits),
        "success": success_count,
        "failed": error_count,
        "total_time_seconds": round(total_time, 2),
        "commits": all_results,
    }
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"Summary saved to: {summary_path}")

    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
