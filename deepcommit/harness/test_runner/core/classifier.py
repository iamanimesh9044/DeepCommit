"""
Test result classifier for comparing results between states.

Supports multiple test frameworks (pytest, go_test, maven, cargo, jest, mocha)
by using a standardized report format.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

from deepcommit.harness.utils.test_id_normalizer import TestIdNormalizer

logger = logging.getLogger(__name__)

NORMALIZED_OUTCOME_STATES = ("pass", "fail", "skipped")

CLASSIFICATION_TRANSITION_CATEGORIES = (
    "pass_to_pass",
    "pass_to_fail",
    "pass_to_skipped",
    "fail_to_pass",
    "fail_to_fail",
    "fail_to_skipped",
    "skipped_to_pass",
    "skipped_to_fail",
    "skipped_to_skipped",
    "none_to_pass",
    "none_to_fail",
    "none_to_skipped",
    # error_to_pass: the test errored at the milestone baseline (e.g. its src
    # symbol/feature was absent so collection/setup raised) and passes at end.
    # Credited as a counted transition exactly like none_to_pass — the commit
    # made a previously-unrunnable test pass. It is ALSO kept in
    # error_outcomes.start, so H_error_within_budget still sees the real start
    # error. Every OTHER error transition (error->error/fail/skipped, *->error)
    # stays side-channel only (in error_outcomes, never a counted category).
    "error_to_pass",
    "pass_to_none",
    "fail_to_none",
    "skipped_to_none",
)

CLASSIFICATION_CATEGORIES = (
    *CLASSIFICATION_TRANSITION_CATEGORIES,
    # Aggregated lists (for backward compatibility)
    "new_tests",
    "removed_tests",
)

# Framework-specific outcome mappings to normalized outcomes
OUTCOME_MAPPINGS = {
    "pytest": {
        "passed": "pass",
        "failed": "fail",
        "skipped": "skipped",
        "error": "error",
        "errored": "error",
        "xfailed": "pass",  # Expected failure = success
        "xpassed": "fail",  # Unexpected pass = failure
    },
    "go_test": {
        "pass": "pass",
        "fail": "fail",
        "skip": "skipped",
        "passed": "pass",
        "failed": "fail",
        "skipped": "skipped",
    },
    "maven": {
        "passed": "pass",
        "failed": "fail",
        "skipped": "skipped",
        "error": "error",
        "errored": "error",
    },
    "cargo": {
        "ok": "pass",
        "passed": "pass",
        "FAILED": "fail",
        "failed": "fail",
        "ignored": "skipped",
        "skipped": "skipped",
    },
    "jest": {
        "passed": "pass",
        "failed": "fail",
        "pending": "skipped",
        "skipped": "skipped",
    },
    "mocha": {
        "passed": "pass",
        "failed": "fail",
        "pending": "skipped",
        "skipped": "skipped",
    },
}


class TestClassifier:
    """
    Classifies test result changes between two states.

    Supports multiple test frameworks (pytest, go_test, maven, cargo, jest, mocha)
    by normalizing framework-specific outcomes to standard outcomes.

    Categories:
    - pass_to_pass: Tests that consistently pass
    - pass_to_fail: Tests that passed before but fail now
    - pass_to_skipped: Tests that passed before but are skipped now
    - fail_to_pass: Tests that failed before but pass now
    - fail_to_fail: Tests that consistently fail
    - fail_to_skipped: Tests that failed before but are skipped now
    - skipped_to_pass: Tests that were skipped before but pass now
    - skipped_to_fail: Tests that were skipped before but fail now
    - skipped_to_skipped: Tests that are skipped in both states
    - new_tests: Tests that only exist in the after state
    - removed_tests: Tests that only exist in the before state
    - error_outcomes: Side-channel for tests with setup/collection/runtime errors.
      Errors are intentionally excluded from pass/fail/skipped transitions so
      they don't pollute fail_to_fail.
    """

    def __init__(self, framework: str = "pytest", enable_test_id_normalization: bool = True):
        """
        Initialize classifier with framework-specific outcome mappings.

        Args:
            framework: Test framework name (pytest, go_test, maven, cargo, jest, mocha)
            enable_test_id_normalization: Whether to normalize test IDs before comparison.
                This helps handle parameterized/fuzz tests with random subtest IDs.
                Default is True for go_test framework.
        """
        self.framework = framework
        self.outcome_map = OUTCOME_MAPPINGS.get(framework, OUTCOME_MAPPINGS["pytest"])
        self.enable_test_id_normalization = enable_test_id_normalization
        self.normalizer = TestIdNormalizer(framework=framework, enable_normalization=enable_test_id_normalization)

    def classify(self, before_results: Dict[str, Any], after_results: Dict[str, Any]) -> Dict[str, Any]:
        """
        Classify test changes between two states.

        Args:
            before_results: Test results from the before state
            after_results: Test results from the after state

        Returns:
            Dict with classification categories as keys and test IDs as values
        """
        # Build outcome maps (original test ID -> outcome)
        before_outcomes = self._build_outcome_map(before_results)
        after_outcomes = self._build_outcome_map(after_results)

        # Build normalized outcome maps for matching
        # normalized_id -> (original_id, outcome)
        before_normalized, before_norm_to_orig = self._build_normalized_outcome_map(before_outcomes)
        after_normalized, after_norm_to_orig = self._build_normalized_outcome_map(after_outcomes)

        # Get all normalized test IDs for comparison
        all_normalized_tests = set(before_normalized.keys()) | set(after_normalized.keys())

        # Classify each test
        classification: Dict[str, Any] = {category: [] for category in CLASSIFICATION_CATEGORIES}
        error_outcomes: Dict[str, List[str]] = {
            "start": [],
            "end": [],
            "both": [],
            "start_only": [],
            "end_only": [],
        }
        classification["error_outcomes"] = error_outcomes

        for norm_test_id in all_normalized_tests:
            before_outcome = before_normalized.get(norm_test_id)
            after_outcome = after_normalized.get(norm_test_id)

            # Get original test IDs for reporting
            # Prefer after_id if available (it's the "current" test), otherwise use before_id
            if norm_test_id in after_norm_to_orig:
                report_test_id = after_norm_to_orig[norm_test_id]
            else:
                report_test_id = before_norm_to_orig.get(norm_test_id, norm_test_id)

            before_state = self._normalize_outcome(before_outcome) if before_outcome is not None else None
            after_state = self._normalize_outcome(after_outcome) if after_outcome is not None else None
            if before_state == "error" or after_state == "error":
                self._record_error_outcome(error_outcomes, report_test_id, before_state, after_state)
                # error->pass is ALSO surfaced as a counted transition (parallel to
                # none_to_pass): a start error means the test could not run at the
                # milestone baseline and the commit made it pass. It stays in
                # error_outcomes.start too, so H_error_within_budget still counts the
                # real start error; every other error transition remains side-channel.
                if before_state == "error" and after_state == "pass":
                    classification["error_to_pass"].append(report_test_id)
                continue

            category = self._categorize_normalized(before_state, after_state)
            classification[category].append(report_test_id)

            # Also populate aggregated lists
            if before_outcome is None:
                classification["new_tests"].append({"test_id": report_test_id, "end_outcome": after_outcome})
            elif after_outcome is None:
                classification["removed_tests"].append({"test_id": report_test_id, "start_outcome": before_outcome})

        # Sort lists for consistent output
        for category in CLASSIFICATION_CATEGORIES:
            if category in ("new_tests", "removed_tests"):
                # These are lists of dicts, sort by test_id
                classification[category].sort(key=lambda x: x["test_id"])
            else:
                # These are lists of strings (test IDs)
                classification[category].sort()
        for key in error_outcomes:
            error_outcomes[key].sort()

        return classification

    @staticmethod
    def _record_error_outcome(
        error_outcomes: Dict[str, List[str]],
        test_id: str,
        before_state: Optional[str],
        after_state: Optional[str],
    ) -> None:
        if before_state == "error":
            error_outcomes["start"].append(test_id)
        if after_state == "error":
            error_outcomes["end"].append(test_id)
        if before_state == "error" and after_state == "error":
            error_outcomes["both"].append(test_id)
        elif before_state == "error":
            error_outcomes["start_only"].append(test_id)
        elif after_state == "error":
            error_outcomes["end_only"].append(test_id)

    def _build_normalized_outcome_map(self, outcome_map: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
        """
        Build a normalized outcome map for test ID matching.

        When multiple tests normalize to the same ID (e.g., parameterized tests),
        choose the most severe normalized outcome. Assertion failure outranks
        error because it means the test body ran and failed. Error remains a
        side-channel so setup/import crashes don't pollute F2F transitions.

        Args:
            outcome_map: Original test ID -> outcome mapping

        Returns:
            Tuple of (normalized_id -> outcome, normalized_id -> original_id)
        """
        normalized_outcomes: Dict[str, str] = {}
        normalized_to_original: Dict[str, str] = {}
        normalized_all_outcomes: Dict[str, List[str]] = {}

        for test_id, outcome in outcome_map.items():
            norm_id = self.normalizer.normalize(test_id)

            if norm_id not in normalized_all_outcomes:
                normalized_all_outcomes[norm_id] = []
                normalized_to_original[norm_id] = test_id  # Keep first original ID

            normalized_all_outcomes[norm_id].append(outcome)

        # Determine final outcome for each normalized ID
        for norm_id, outcomes in normalized_all_outcomes.items():
            # Normalize all outcomes first
            norm_outcomes = [self._normalize_outcome(o) for o in outcomes]

            # Severity order: fail > error > pass > skipped.
            if "fail" in norm_outcomes:
                normalized_outcomes[norm_id] = "fail"
            elif "error" in norm_outcomes:
                normalized_outcomes[norm_id] = "error"
            elif "pass" in norm_outcomes:
                normalized_outcomes[norm_id] = "pass"
            elif all(o == "skipped" for o in norm_outcomes):
                normalized_outcomes[norm_id] = "skipped"
            else:
                normalized_outcomes[norm_id] = "fail"

        return normalized_outcomes, normalized_to_original

    def _build_outcome_map(self, results: Dict[str, Any]) -> Dict[str, str]:
        """
        Build a map of test ID to outcome.

        Args:
            results: Test results dict

        Returns:
            Dict mapping test ID to outcome
        """
        outcome_map = {}
        for test in results.get("tests", []):
            test_id = test.get("nodeid")
            if test_id:
                outcome_map[test_id] = test.get("outcome", "unknown")
        return outcome_map

    def _categorize_normalized(self, before_state: Optional[str], after_state: Optional[str]) -> str:
        """
        Categorize a single test based on normalized non-error states.

        Args:
            before_state: Normalized state in before side (None if absent)
            after_state: Normalized state in after side (None if absent)

        Returns:
            Category string (e.g., "pass_to_fail", "none_to_pass", "fail_to_none")
        """
        if before_state is None:
            return f"none_to_{after_state}"
        if after_state is None:
            return f"{before_state}_to_none"
        return f"{before_state}_to_{after_state}"

    def _normalize_outcome(self, outcome: str) -> str:
        """
        Normalize outcome to one of: pass, fail, skipped, error.

        Uses framework-specific outcome mappings to handle different
        test frameworks (pytest, go_test, maven, cargo, jest, mocha).

        Args:
            outcome: Raw test outcome string

        Returns:
            Normalized state: "pass", "fail", "skipped", or "error"
        """
        if outcome is None:
            return "fail"
        outcome_text = str(outcome)
        # Use framework-specific mapping if available
        normalized = self.outcome_map.get(outcome_text)
        if normalized:
            return normalized

        # Fallback logic for unknown outcomes
        outcome_lower = outcome_text.lower().replace("-", "_").replace(" ", "_")
        if outcome_lower in ("skipped", "skip", "ignored", "pending"):
            return "skipped"
        elif outcome_lower in ("passed", "pass", "ok", "xfail", "xfailed"):
            return "pass"
        elif outcome_lower in (
            "error",
            "errored",
            "erroring",
            "crashed",
            "collection_error",
            "setup_error",
            "import_error",
            "runtime_error",
        ):
            return "error"
        else:
            # failed, xpass, FAILED, genuinely unknown outcomes, etc. count as fail
            return "fail"

    def _is_pass(self, outcome: str) -> bool:
        """
        Check if an outcome is considered a pass.

        Args:
            outcome: Test outcome string

        Returns:
            True if outcome is a pass
        """
        return outcome in ("passed", "xfail")  # xfail is expected failure, counts as pass

    def classify_from_files(self, before_file: Path, after_file: Path) -> Dict[str, Any]:
        """
        Classify test changes from result files.

        Supports multiple test frameworks by using the unified report parser.
        Also handles pre-parsed JSON files in standardized format.

        Args:
            before_file: Path to before state results JSON
            after_file: Path to after state results JSON

        Returns:
            Classification dict
        """
        # Import here to avoid circular dependency

        # Try to load as standardized JSON format first
        # (merged reports already have "tests" array with "nodeid" and "outcome")
        before_results = self._load_or_parse(before_file)
        after_results = self._load_or_parse(after_file)

        return self.classify(before_results, after_results)

    def _load_or_parse(self, file_path: Path) -> Dict[str, Any]:
        """
        Load a file as standardized JSON, or parse it using framework-specific parser.

        If the file is already in standardized format (has "tests" array with
        "nodeid" and "outcome"), use it directly. Otherwise, use the framework
        parser to convert from raw format.

        Args:
            file_path: Path to test results file

        Returns:
            Standardized test results dict
        """
        from .report_parser import parse_test_report

        if not file_path.exists():
            return {"tests": [], "summary": {}}

        # Try loading as JSON first
        if file_path.suffix == ".json":
            try:
                with open(file_path) as f:
                    data = json.load(f)
                # Check if it's already in standardized format
                if "tests" in data and isinstance(data["tests"], list):
                    # Verify it has the expected structure
                    if not data["tests"] or ("nodeid" in data["tests"][0] and "outcome" in data["tests"][0]):
                        return cast(Dict[str, Any], data)
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

        # Fall back to framework-specific parsing
        return parse_test_report(file_path, self.framework)

    def generate_summary(self, classification: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate a summary of the classification.

        Args:
            classification: Classification dict from classify()

        Returns:
            Summary dict with counts (flat structure, no nested 'counts' key)
        """
        # Flat counts for transition categories only. error_outcomes is a
        # side-channel because H_error_within_budget evaluates per-state error
        # counts, not pairwise before/after categories.
        summary = {k: len(classification.get(k, [])) for k in CLASSIFICATION_CATEGORIES}
        error_outcomes = classification.get("error_outcomes", {})
        if not isinstance(error_outcomes, dict):
            error_outcomes = {}
        summary["start_error"] = len(error_outcomes.get("start", []))
        summary["end_error"] = len(error_outcomes.get("end", []))
        summary["error_overlap"] = len(error_outcomes.get("both", []))
        summary["start_only_error"] = len(error_outcomes.get("start_only", []))
        summary["end_only_error"] = len(error_outcomes.get("end_only", []))

        # Calculate totals
        before_states = set(NORMALIZED_OUTCOME_STATES)
        after_states = set(NORMALIZED_OUTCOME_STATES)
        summary["total_before"] = sum(
            count
            for key, count in summary.items()
            if isinstance(key, str) and "_to_" in key and key.split("_to_", 1)[0] in before_states
        )

        summary["total_after"] = sum(
            count
            for key, count in summary.items()
            if isinstance(key, str) and "_to_" in key and key.split("_to_", 1)[1] in after_states
        )

        return summary

    def save_classification(
        self, classification: Dict[str, List[str]], output_path: Path, include_summary: bool = True
    ):
        """
        Save classification to a JSON file.

        Args:
            classification: Classification dict
            output_path: Output file path
            include_summary: Whether to include summary in output
        """
        output: Dict[str, Any] = dict(classification)

        if include_summary:
            output["summary"] = self.generate_summary(classification)

        with open(output_path, "w") as f:
            json.dump(output, f, indent=2)
