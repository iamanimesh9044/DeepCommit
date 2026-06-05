"""Unit tests for extract_f2p_tests.detect_status / detect_issues (F2P status classifier).

Pure ``dict -> str`` classifier (no IO), so it lives in tests/unit even though it
is landed during Wave 2. The high-value cases are the pytest exit-code semantics
(exitcode 1 = "ran but some failed" = NORMAL, not an error) and the check
ordering: empty -> error -> warning -> ok.
"""

from deepcommit.harness.filter_tests.extract_f2p_tests import detect_issues, detect_status


def test_status_empty_when_no_tests_collected():
    assert detect_status({"total_tests": 0}, None, None) == "empty"


def test_status_empty_wins_over_error_exitcode():
    # Ordering: total_tests == 0 is checked first, so "empty" beats a >=2 exitcode.
    assert detect_status({"total_tests": 0}, {"exitcode": 3}, {"exitcode": 5}) == "empty"


def test_exitcode_1_is_not_an_error():
    # The key semantic: pytest exitcode 1 == "tests ran but some failed" == NORMAL.
    assert detect_status({"total_tests": 10}, {"exitcode": 0}, {"exitcode": 1}) == "ok"


def test_exitcode_ge_2_is_error():
    for code in (2, 3, 4, 5):
        assert detect_status({"total_tests": 10}, {"exitcode": 0}, {"exitcode": code}) == "error"
    # the baseline side triggers it too
    assert detect_status({"total_tests": 10}, {"exitcode": 3}, {"exitcode": 0}) == "error"


def test_error_beats_warning():
    # A real error AND high regression -> error (error is checked before warning).
    stats = {"total_tests": 10, "pass_to_fail": 9}
    assert detect_status(stats, None, {"exitcode": 3}) == "error"


def test_warning_on_high_regression_rate():
    # 1/10 = 10% > 5% -> warning
    assert detect_status({"total_tests": 10, "pass_to_fail": 1}, None, None) == "warning"


def test_low_regression_rate_is_ok():
    # 1/100 = 1% <= 5% -> ok (near-miss for the >0.05 threshold)
    assert detect_status({"total_tests": 100, "pass_to_fail": 1}, None, None) == "ok"


def test_no_regression_is_ok():
    assert detect_status({"total_tests": 10, "pass_to_fail": 0}, None, None) == "ok"


def test_none_baseline_final_default_exitcode_zero():
    # missing baseline/final -> exitcode defaults to 0 (not an error)
    assert detect_status({"total_tests": 5, "pass_to_fail": 0}, None, None) == "ok"


def test_detect_issues_messages_for_status():
    assert "No tests collected" in detect_issues({"total_tests": 0}, "empty")[0]
    assert any("execution error" in m for m in detect_issues({}, "error"))
    assert any("High regression: 9" in m for m in detect_issues({"pass_to_fail": 9}, "warning"))
