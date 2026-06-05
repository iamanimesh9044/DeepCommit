"""
Pytest Report Parsing Utilities

This module provides utilities for extracting information from pytest-json-report
format JSON files, including:
- Failure messages
- Skip reasons
- Report summarization

Used by both validate_base_image.py and run_milestone_tests.py
"""

import ast
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def extract_fail_message(test: Dict[str, Any]) -> str:
    """
    Extract failure message from a pytest test result.

    Looks for the crash message in the call phase of the test.

    Args:
        test: Test result dictionary from pytest-json-report

    Returns:
        Failure message string, or "Unknown error" if not found
    """
    call_info = test.get("call", {})
    crash_info = call_info.get("crash", {})
    message = crash_info.get("message", "")

    if message:
        return message

    # Fallback: try to get from longrepr
    longrepr = call_info.get("longrepr", "")
    if isinstance(longrepr, str) and longrepr:
        # Truncate long repr to reasonable length
        return longrepr[:500] if len(longrepr) > 500 else longrepr

    return "Unknown error"


def extract_skip_reason(test: Dict[str, Any]) -> str:
    """
    Extract skip reason from a pytest test result.

    Skip reasons can be found in:
    1. setup.longrepr (for skip markers evaluated at collection)
    2. call.longrepr (for skips during test execution)

    The longrepr can be:
    - A string directly containing the reason
    - A tuple/list like ('/path/file.py', line_num, 'Skipped: reason')

    Args:
        test: Test result dictionary from pytest-json-report

    Returns:
        Skip reason string, or empty string if not found
    """
    # Check setup.longrepr first (most common for skip markers)
    setup_info = test.get("setup", {})
    longrepr = setup_info.get("longrepr", "")

    # Check call.longrepr if setup doesn't have it
    if not longrepr:
        call_info = test.get("call", {})
        longrepr = call_info.get("longrepr", "")

    if not longrepr:
        return ""

    # Handle list/tuple format: ('/path/file.py', line_num, 'Skipped: reason')
    if isinstance(longrepr, (list, tuple)) and len(longrepr) >= 3:
        return str(longrepr[2])

    return str(longrepr)


def extract_skip_reason_message(test: Dict[str, Any]) -> str:
    """
    Extract a clean skip reason message, stripping file/line info.

    This extracts just the message part from skip reasons, which is useful
    for grouping tests by their skip reason.

    Args:
        test: Test result dictionary from pytest-json-report

    Returns:
        Clean skip reason message
    """
    reason = extract_skip_reason(test)

    if not reason:
        return "Unknown reason"

    # Try to parse tuple format like:
    # "('/path/file.py', 22, 'Skipped: message')"
    if reason.startswith("(") and reason.endswith(")"):
        try:
            parsed = ast.literal_eval(reason)
            if isinstance(parsed, tuple) and len(parsed) >= 3:
                message = str(parsed[2])
                # Remove "Skipped: " prefix if present
                if message.startswith("Skipped: "):
                    message = message[9:]
                return message
        except (ValueError, SyntaxError):
            pass  # Keep original if parsing fails

    # For string format, remove "Skipped: " prefix if present
    if reason.startswith("Skipped: "):
        return reason[9:]

    return reason


def convert_pytest_report_to_summary(json_report_path: Path, output_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Convert pytest JSON report to a readable summary.

    This function parses a pytest-json-report format file and produces
    a structured summary with:
    - Overall statistics (passed, failed, skipped, error counts)
    - Failed tests with their failure messages
    - Skipped tests grouped by reason
    - Passed tests list

    Args:
        json_report_path: Path to pytest JSON report
        output_path: Optional path to save summary JSON

    Returns:
        Summary dict with structure:
        {
            "duration": float,
            "summary": {"total": int, "passed": int, "failed": int, ...},
            "results": {
                "failed": [{"nodeid": str, "message": str}, ...],
                "skipped": [{"reason": str, "count": int, "tests": [...]}, ...],
                "passed": [nodeid, ...]
            }
        }
    """
    with open(json_report_path) as f:
        data = json.load(f)

    summary = {
        "summary": data.get("summary", {}),
        "duration": data.get("duration", 0),
        "passed": [],
        "failed": [],
        "skipped": [],
        "error": [],
        "xfailed": [],
        "xpassed": [],
    }

    for test in data.get("tests", []):
        nodeid = test.get("nodeid", "unknown")
        outcome = test.get("outcome", "unknown")
        duration = test.get("call", {}).get("duration", 0)

        test_info = {
            "nodeid": nodeid,
            "duration": round(duration, 4),
        }

        if outcome == "passed":
            summary["passed"].append(test_info)
        elif outcome == "failed":
            test_info["reason"] = extract_fail_message(test)
            summary["failed"].append(test_info)
        elif outcome == "skipped":
            test_info["reason"] = extract_skip_reason(test)
            summary["skipped"].append(test_info)
        elif outcome == "xfailed":
            test_info["reason"] = extract_skip_reason(test) or extract_fail_message(test)
            summary["xfailed"].append(test_info)
        elif outcome == "xpassed":
            test_info["reason"] = extract_skip_reason(test)
            summary["xpassed"].append(test_info)
        else:
            # error or other outcomes
            test_info["outcome"] = outcome
            test_info["reason"] = _extract_error_message(test)
            summary["error"].append(test_info)

    # Group skipped tests by reason message (ignoring file and line number)
    skip_reasons: Dict[str, List[str]] = {}
    for test in summary["skipped"]:
        reason = test.get("reason", "Unknown reason")
        message = _extract_clean_skip_message(reason)

        if message not in skip_reasons:
            skip_reasons[message] = []
        skip_reasons[message].append(test["nodeid"])

    # Convert failed to flat list with {nodeid, message}
    failed_list = [
        {"nodeid": test["nodeid"], "message": test.get("reason", "Unknown error")} for test in summary["failed"]
    ]

    # Convert skipped to list format sorted by count descending
    skipped_by_reason = [
        {"reason": reason, "count": len(tests), "tests": tests}
        for reason, tests in sorted(skip_reasons.items(), key=lambda x: -len(x[1]))
    ]

    # Convert error to flat list with {nodeid, message}
    error_list = [
        {"nodeid": test["nodeid"], "message": test.get("reason", "Unknown error")} for test in summary["error"]
    ]

    # Convert xfailed to flat list with {nodeid, message}
    xfailed_list = [{"nodeid": test["nodeid"], "message": test.get("reason", "")} for test in summary["xfailed"]]

    # Convert xpassed to flat list with {nodeid, message}
    xpassed_list = [{"nodeid": test["nodeid"], "message": test.get("reason", "")} for test in summary["xpassed"]]

    # Pytest records collection-time failures (import errors, model-config
    # errors, etc.) in a separate `collectors` array -- these never reach
    # the `tests` array, so they'd otherwise be lost when building the
    # summary. Group them by exception message so a milestone with 1000+
    # broken collectors doesn't blow up the summary file.
    collection_errors = _group_collection_failures(data.get("collectors", []))

    # Aggregate tests[outcome=error] by normalized crash signature. When env
    # setup blows up (e.g. pytest-django DB init), a single root cause can
    # produce 10000+ identical entries in the flat error list; the grouped
    # view gives a meta-agent (or human) a bounded top-K view of root causes.
    # results.error is kept flat for backward compat with existing consumers.
    error_groups = _group_test_errors([t for t in data.get("tests", []) if t.get("outcome") == "error"])

    # Create compact summary with new structure
    # Order: collection_errors -> error_groups -> failed -> error -> ... -> passed
    compact_summary = {
        "duration": round(summary["duration"], 2),
        "summary": {
            "total": summary["summary"].get("total", 0),
            "passed": summary["summary"].get("passed", 0),
            "failed": summary["summary"].get("failed", 0),
            "skipped": summary["summary"].get("skipped", 0),
            "error": summary["summary"].get("error", 0),
            "xfailed": summary["summary"].get("xfailed", 0),
            "xpassed": summary["summary"].get("xpassed", 0),
            "collected": summary["summary"].get("collected", 0),
            "collection_errors": sum(g["count"] for g in collection_errors),
            "error_groups": len(error_groups),
        },
        "results": {
            "collection_errors": collection_errors,
            "error_groups": error_groups,
            "failed": failed_list,
            "error": error_list,
            "xfailed": xfailed_list,
            "xpassed": xpassed_list,
            "skipped": skipped_by_reason,
            "passed": [t["nodeid"] for t in summary["passed"]],
        },
    }

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(compact_summary, f, indent=2)

    return compact_summary


def _extract_error_message(test: Dict[str, Any]) -> str:
    """
    Extract error message from a pytest test result.

    Error outcomes typically occur in setup or teardown phases,
    so we check multiple locations for the error message.

    Args:
        test: Test result dictionary from pytest-json-report

    Returns:
        Error message string
    """
    # Check setup phase first (common for fixture errors)
    # Note: setup.outcome can be "failed" even when test.outcome is "error"
    setup_info = test.get("setup", {})
    crash_info = setup_info.get("crash", {})
    message = crash_info.get("message", "")
    if message:
        return f"[setup] {message}"
    longrepr = setup_info.get("longrepr", "")
    if longrepr and setup_info.get("outcome") in ("failed", "error"):
        msg = str(longrepr)[:500] if len(str(longrepr)) > 500 else str(longrepr)
        return f"[setup] {msg}"

    # Check teardown phase
    teardown_info = test.get("teardown", {})
    crash_info = teardown_info.get("crash", {})
    message = crash_info.get("message", "")
    if message:
        return f"[teardown] {message}"
    longrepr = teardown_info.get("longrepr", "")
    if longrepr and teardown_info.get("outcome") in ("failed", "error"):
        msg = str(longrepr)[:500] if len(str(longrepr)) > 500 else str(longrepr)
        return f"[teardown] {msg}"

    # Fallback to call phase
    call_info = test.get("call", {})
    crash_info = call_info.get("crash", {})
    message = crash_info.get("message", "")
    if message:
        return message

    longrepr = call_info.get("longrepr", "")
    if longrepr:
        return str(longrepr)[:500] if len(str(longrepr)) > 500 else str(longrepr)

    return "Unknown error"


def ensure_pytest_summary_current(summary_path: Path, report_path: Path) -> bool:
    """Regenerate `summary_path` from `report_path` if it's missing the new fields.

    The agent's own exploration sometimes overwrites the per-attempt summary
    files with a hand-rolled dict that lacks the grouped-error fields this
    module emits. Rather than trust whoever wrote the file last, use the
    presence of ``summary.error_groups`` as a new-format marker: if absent,
    regenerate from the raw pytest-json-report next to it.

    Returns True if regenerated (or the file was missing/malformed and got
    written fresh), False if the existing summary was already current.
    """
    summary_path = Path(summary_path)
    report_path = Path(report_path)
    if not report_path.exists():
        return False

    if summary_path.exists():
        try:
            data = json.loads(summary_path.read_text())
            if "error_groups" in data.get("summary", {}):
                return False
        except (json.JSONDecodeError, OSError):
            pass  # treat as regenerate-worthy

    convert_pytest_report_to_summary(report_path, summary_path)
    return True


def _group_collection_failures(collectors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group pytest collection failures by exception message.

    Pytest records ``collectors`` entries per module/class collected. When a
    module fails to import (e.g. ImportError for a 5.x-only symbol in a 4.2
    checkout, or a pytest-django ``INSTALLED_APPS`` misconfiguration), the
    whole file's tests never reach the ``tests`` array and are otherwise
    silently dropped from the summary.

    Groups share shape with ``skipped_by_reason`` (reason/count/tests) so a
    milestone with 1000 broken modules stays cheap, and include one sample
    ``longrepr`` (truncated) so the reader can see the full traceback without
    opening the 10 MB full report.
    """
    groups: Dict[str, Dict[str, Any]] = {}
    for collector in collectors:
        if collector.get("outcome") != "failed":
            continue
        longrepr = str(collector.get("longrepr", ""))
        # Last non-empty line usually carries the exception class + message.
        # Strip pytest's leading `E   ` marker if present.
        lines = [ln.strip() for ln in longrepr.split("\n") if ln.strip()]
        last = lines[-1] if lines else "collection failed"
        if last.startswith("E "):
            last = last[2:].lstrip()
        key = last[:300]
        if key not in groups:
            groups[key] = {
                "reason": key,
                "count": 0,
                "tests": [],
                "longrepr": longrepr[:1500],
            }
        groups[key]["count"] += 1
        groups[key]["tests"].append(collector.get("nodeid", ""))

    # Sort by count desc (most impactful first), matching skipped_by_reason.
    return sorted(groups.values(), key=lambda g: -g["count"])


_NORMALIZERS = [
    # Python repr patterns like "type object 'FooTest'" / "'Bar' object has no attribute"
    (re.compile(r"object '[A-Z]\w+'"), "object '<CLASS>'"),
    (re.compile(r"'[A-Z]\w+' object"), "'<CLASS>' object"),
    # Temp dirs: /tmp/pytest-of-user/xyz123/
    (re.compile(r"/tmp/[^\s'\"]+"), "/tmp/<PATH>"),
    # Memory addresses
    (re.compile(r"0x[0-9a-fA-F]+"), "0x<ADDR>"),
    # UUIDs
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "<UUID>"),
    # Line-number fragments that sometimes leak into messages
    (re.compile(r":\d+:"), ":<N>:"),
]


def _normalize_crash_message(msg: str) -> str:
    """Strip per-test variable parts so the same root cause collapses to one key.

    Variables that typically differ test-to-test but share a root cause:
      - concrete class names (SMTPBackendTests vs CookieTests → '<CLASS>')
      - temp directories, memory addresses, UUIDs, line numbers in messages

    Keeps error type ("AttributeError: ..."), the missing symbol
    ("has no attribute 'enterClassContext'"), and any literal strings
    that are root-cause specific.
    """
    s = msg or ""
    for pattern, replacement in _NORMALIZERS:
        s = pattern.sub(replacement, s)
    return s.strip()[:300]


def _group_test_errors(errored_tests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate ``tests[outcome=error]`` entries by normalized crash signature.

    When env setup blows up catastrophically (e.g. pytest-django DB init
    failing on every test), ``tests[error]`` can balloon to tens of thousands
    of identical entries. Callers reading ``results.error`` flat have to dedup
    themselves and summary files grow unbounded. This helper collapses them
    to unique ``(error_class + normalized_message + file_path)`` signatures
    — lineno deliberately excluded because it tends to drift across rows that
    share a root cause.

    Returned group shape mirrors ``skipped_by_reason`` / ``collection_errors``
    so consumers can treat all three uniformly.

    Invariant: ``sum(g["count"] for g in result) == len(errored_tests)``. Tests
    without usable crash data fall into a single "_unclassified" bucket rather
    than being silently dropped — this keeps the group totals reconcilable
    with ``summary.error`` and guarantees no error info is ever lost (worst
    case: a group says "unclassified, read longrepr in full report").
    """
    groups: Dict[tuple, Dict[str, Any]] = {}
    unclassified_nodeids: List[str] = []
    for t in errored_tests:
        # Prefer setup (fixture crashes — the common D2 case); fall back to call/teardown
        crash = None
        phase = None
        for phase_name in ("setup", "call", "teardown"):
            c = t.get(phase_name, {}).get("crash")
            if c:
                crash, phase = c, phase_name
                break
        if not crash:
            # No crash dict in any phase — could be a pytest-xdist worker
            # replacement, a plugin-defined non-standard outcome, or a
            # truncated report. Preserve the nodeid in a catch-all bucket
            # so downstream can read longrepr from the full pytest report.
            unclassified_nodeids.append(t.get("nodeid", ""))
            continue

        raw_msg = str(crash.get("message", ""))
        normalized = _normalize_crash_message(raw_msg)
        path = crash.get("path", "")
        key = (normalized, path)

        if key not in groups:
            groups[key] = {
                "reason": normalized,
                "location": f"{path}:{crash.get('lineno', 0)}" if path else "",
                "phase": phase,
                "count": 0,
                "tests": [],
                "sample_message": raw_msg[:500],  # keep one original for reference
            }
        groups[key]["count"] += 1
        groups[key]["tests"].append(t.get("nodeid", ""))

    result = sorted(groups.values(), key=lambda g: -g["count"])

    if unclassified_nodeids:
        # Always append at the end (lowest priority) — these need per-test
        # drilldown into start.json / end.json to get their longrepr.
        result.append(
            {
                "reason": "_unclassified (no crash data in setup/call/teardown)",
                "location": "",
                "phase": None,
                "count": len(unclassified_nodeids),
                "tests": unclassified_nodeids,
                "sample_message": "",
            }
        )

    return result


def _extract_clean_skip_message(reason: str) -> str:
    """
    Extract clean skip message from a reason string.

    Internal helper for convert_pytest_report_to_summary.

    Handles formats like:
    - "('/path/file.py', 22, 'Skipped: message')"
    - "[gw5] linux -- Python 3.9.25...\n('/path/...', 1834, 'Skipped: ...')"

    Args:
        reason: Raw skip reason string

    Returns:
        Clean skip message
    """
    if not reason:
        return "Unknown reason"

    message = reason

    # Handle pytest-xdist worker prefix: "[gw5] linux -- Python 3.9.25...\n..."
    if reason.startswith("[gw") and "\n" in reason:
        # Extract the part after the newline
        parts = reason.split("\n", 1)
        if len(parts) > 1:
            reason = parts[1].strip()
            message = reason

    # Try to parse tuple format like:
    # "('/path/file.py', 22, 'Skipped: message')"
    if reason.startswith("(") and reason.endswith(")"):
        try:
            parsed = ast.literal_eval(reason)
            if isinstance(parsed, tuple) and len(parsed) >= 3:
                message = str(parsed[2])
                # Remove "Skipped: " prefix if present
                if message.startswith("Skipped: "):
                    message = message[9:]
        except (ValueError, SyntaxError):
            pass  # Keep original if parsing fails

    return message


def group_tests_by_skip_reason(tests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Group skipped tests by their skip reason.

    Args:
        tests: List of test result dicts with 'nodeid' and skip info

    Returns:
        List of {"reason": str, "count": int, "tests": [nodeid, ...]} dicts,
        sorted by count descending
    """
    skip_reasons: Dict[str, List[str]] = {}

    for test in tests:
        nodeid = test.get("nodeid", "unknown")
        reason = extract_skip_reason_message(test)

        if reason not in skip_reasons:
            skip_reasons[reason] = []
        skip_reasons[reason].append(nodeid)

    return [
        {"reason": reason, "count": len(nodeids), "tests": nodeids}
        for reason, nodeids in sorted(skip_reasons.items(), key=lambda x: -len(x[1]))
    ]


def group_tests_by_fail_message(tests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Group failed tests by their failure message.

    Args:
        tests: List of test result dicts with 'nodeid' and fail info

    Returns:
        List of {"message": str, "count": int, "tests": [nodeid, ...]} dicts,
        sorted by count descending
    """
    fail_messages: Dict[str, List[str]] = {}

    for test in tests:
        nodeid = test.get("nodeid", "unknown")
        message = extract_fail_message(test)
        # Truncate long messages for grouping
        message_key = message[:200] if len(message) > 200 else message

        if message_key not in fail_messages:
            fail_messages[message_key] = []
        fail_messages[message_key].append(nodeid)

    return [
        {"message": message, "count": len(nodeids), "tests": nodeids}
        for message, nodeids in sorted(fail_messages.items(), key=lambda x: -len(x[1]))
    ]
