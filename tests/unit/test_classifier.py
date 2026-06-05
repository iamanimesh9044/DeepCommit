"""Characterization tests for TestClassifier.classify (transition taxonomy).

Source under test: deepcommit.harness.test_runner.core.classifier

The classifier compares two ``{"tests": [{"nodeid", "outcome"}]}`` snapshots and
buckets every test into a transition category (``<before>_to_<after>``) plus a
side-channel ``error_outcomes`` map and aggregated ``new_tests`` / ``removed_tests``
lists. Outputs are sorted, so every assertion below pins exact lists/shapes.

All expected values were ground-truthed by executing classify() on the inputs;
they are not statically traced guesses.
"""

import json

from deepcommit.harness.test_runner.core.classifier import (
    CLASSIFICATION_CATEGORIES,
    CLASSIFICATION_TRANSITION_CATEGORIES,
    OUTCOME_MAPPINGS,
)
from deepcommit.harness.test_runner.core.classifier import (
    TestClassifier as ClassifierUnderTest,  # aliased: avoids pytest collecting the Test*-named source class
)


def R(*pairs):
    """Build a results dict from (nodeid, outcome) pairs."""
    return {"tests": [{"nodeid": n, "outcome": o} for n, o in pairs]}


def clf(framework="pytest"):
    return ClassifierUnderTest(framework=framework)


# ---------------------------------------------------------------------------
# Nominal transitions on the pass/fail axis
# ---------------------------------------------------------------------------
def test_none_to_pass_and_new_tests():
    r = clf().classify(R(("t_old", "passed")), R(("t_old", "passed"), ("t_new", "passed")))
    assert r["none_to_pass"] == ["t_new"]
    assert r["pass_to_pass"] == ["t_old"]
    # new_tests is a list of dicts; end_outcome is the NORMALIZED outcome.
    assert r["new_tests"] == [{"test_id": "t_new", "end_outcome": "pass"}]
    # removed_tests stays empty here.
    assert r["removed_tests"] == []


def test_fail_to_pass():
    r = clf().classify(R(("t1", "failed")), R(("t1", "passed")))
    assert r["fail_to_pass"] == ["t1"]
    # Near-miss: nothing leaks into the other pass-axis buckets.
    assert r["pass_to_pass"] == []
    assert r["none_to_pass"] == []
    assert r["error_to_pass"] == []


def test_fail_to_fail_consistently_failing():
    r = clf().classify(R(("t1", "failed")), R(("t1", "failed")))
    assert r["fail_to_fail"] == ["t1"]
    assert r["fail_to_pass"] == []


def test_pass_to_fail_and_removed():
    assert clf().classify(R(("t1", "passed")), R(("t1", "failed")))["pass_to_fail"] == ["t1"]
    rm = clf().classify(R(("t1", "passed")), R())
    assert rm["pass_to_none"] == ["t1"]
    # removed_tests carries the NORMALIZED start_outcome.
    assert rm["removed_tests"] == [{"test_id": "t1", "start_outcome": "pass"}]
    assert rm["new_tests"] == []


def test_removed_tests_start_outcome_is_normalized():
    # fail and skipped removals carry their normalized start_outcome verbatim.
    rm_fail = clf().classify(R(("t1", "failed")), R())
    assert rm_fail["fail_to_none"] == ["t1"]
    assert rm_fail["removed_tests"] == [{"test_id": "t1", "start_outcome": "fail"}]

    rm_skip = clf().classify(R(("t1", "skipped")), R())
    assert rm_skip["skipped_to_none"] == ["t1"]
    assert rm_skip["removed_tests"] == [{"test_id": "t1", "start_outcome": "skipped"}]


# ---------------------------------------------------------------------------
# error_to_pass: counted transition AND error side-channel (dual recording)
# ---------------------------------------------------------------------------
def test_error_to_pass_dual_recorded():
    r = clf().classify(R(("t1", "error")), R(("t1", "passed")))
    assert r["error_to_pass"] == ["t1"]
    assert r["error_outcomes"]["start"] == ["t1"]
    assert r["error_outcomes"]["start_only"] == ["t1"]
    assert r["error_outcomes"]["end"] == []
    assert r["error_outcomes"]["both"] == []
    assert r["error_outcomes"]["end_only"] == []
    # Must NOT be mis-credited as a fail_to_pass.
    assert r["fail_to_pass"] == []
    # The error path `continue`s before populating aggregated lists.
    assert r["new_tests"] == []
    assert r["removed_tests"] == []


def test_error_to_fail_is_side_channel_only():
    r = clf().classify(R(("t1", "error")), R(("t1", "failed")))
    assert r["error_outcomes"]["start"] == ["t1"]
    assert r["error_outcomes"]["end"] == []
    assert r["error_outcomes"]["start_only"] == ["t1"]
    # No counted transition category may fire for error->fail.
    transition_cats = {k: r[k] for k in CLASSIFICATION_TRANSITION_CATEGORIES}
    assert all(v == [] for v in transition_cats.values())


def test_none_to_error_end_only_side_channel():
    # A brand-new test that errors: end-side error, no counted transition,
    # and it does NOT show up in new_tests (error path returns early).
    r = clf().classify(R(), R(("t", "error")))
    assert r["error_outcomes"] == {
        "start": [],
        "end": ["t"],
        "both": [],
        "start_only": [],
        "end_only": ["t"],
    }
    assert r["none_to_pass"] == []
    assert r["new_tests"] == []
    transition_cats = {k: r[k] for k in CLASSIFICATION_TRANSITION_CATEGORIES}
    assert all(v == [] for v in transition_cats.values())


def test_pass_to_error_is_end_side_channel_only():
    r = clf().classify(R(("t", "passed")), R(("t", "error")))
    assert r["error_outcomes"]["end"] == ["t"]
    assert r["error_outcomes"]["end_only"] == ["t"]
    assert r["error_outcomes"]["start"] == []
    # pass->error must not be counted as pass_to_fail.
    assert r["pass_to_fail"] == []
    transition_cats = {k: r[k] for k in CLASSIFICATION_TRANSITION_CATEGORIES}
    assert all(v == [] for v in transition_cats.values())


def test_error_to_error_both_buckets():
    # before id == after id; report id resolution prefers the after side
    # (here identical), and `both` is mutually exclusive with start_only/end_only.
    r = clf().classify(R(("t", "error")), R(("t", "error")))
    assert r["error_outcomes"] == {
        "start": ["t"],
        "end": ["t"],
        "both": ["t"],
        "start_only": [],
        "end_only": [],
    }
    assert r["error_to_pass"] == []


# ---------------------------------------------------------------------------
# xfail / xpass mapping (pytest-specific outcome map)
# ---------------------------------------------------------------------------
def test_xfail_xpass_mapping():
    # xfailed (expected failure) maps to pass; xpassed (unexpected pass) maps to fail.
    assert clf().classify(R(("t1", "xfailed")), R(("t1", "passed")))["pass_to_pass"] == ["t1"]
    assert clf().classify(R(("t1", "passed")), R(("t1", "xpassed")))["pass_to_fail"] == ["t1"]


def test_xpassed_to_xfailed_is_fail_to_pass():
    # xpassed->xfailed means fail-axis -> pass-axis under the pytest map.
    r = clf().classify(R(("t1", "xpassed")), R(("t1", "xfailed")))
    assert r["fail_to_pass"] == ["t1"]


# ---------------------------------------------------------------------------
# The skipped family — skipped is a first-class normalized state, not fail.
# ---------------------------------------------------------------------------
def test_skipped_family_full_matrix():
    assert clf().classify(R(("t", "skipped")), R(("t", "passed")))["skipped_to_pass"] == ["t"]
    assert clf().classify(R(("t", "passed")), R(("t", "skipped")))["pass_to_skipped"] == ["t"]
    assert clf().classify(R(("t", "skipped")), R(("t", "skipped")))["skipped_to_skipped"] == ["t"]
    assert clf().classify(R(), R(("t", "skipped")))["none_to_skipped"] == ["t"]
    assert clf().classify(R(("t", "skipped")), R(("t", "failed")))["skipped_to_fail"] == ["t"]
    assert clf().classify(R(("t", "failed")), R(("t", "skipped")))["fail_to_skipped"] == ["t"]
    assert clf().classify(R(), R(("t", "failed")))["none_to_fail"] == ["t"]


def test_skipped_to_pass_is_not_misfiled_as_fail():
    # Guards the policy twin of error_to_pass: skip->pass is a positive signal,
    # it must land ONLY in skipped_to_pass and nowhere on the fail axis.
    r = clf().classify(R(("t", "skipped")), R(("t", "passed")))
    assert r["skipped_to_pass"] == ["t"]
    assert r["fail_to_pass"] == []
    assert r["pass_to_fail"] == []
    assert r["fail_to_fail"] == []


# ---------------------------------------------------------------------------
# go_test random-suffix severity collapse
# ---------------------------------------------------------------------------
def test_severity_collapse_fail_over_error_go_test():
    # All three random subtests normalize to pkg/TestX; the start group has both
    # fail and error -> fail wins (fail > error), so it is fail_to_pass and the
    # group does NOT register as a start error.
    r = clf("go_test").classify(
        R(("pkg/TestX/aaaaaaaa", "failed"), ("pkg/TestX/bbbbbbbb", "error")),
        R(("pkg/TestX/cccccccc", "passed")),
    )
    assert r["fail_to_pass"] == ["pkg/TestX/cccccccc"]
    assert r["error_outcomes"]["start"] == []
    assert r["error_to_pass"] == []


def test_severity_collapse_error_over_pass_go_test():
    # start group has error + pass (no fail) -> error wins (error > pass), giving
    # error_to_pass. The reported id is from the AFTER side (current test).
    r = clf("go_test").classify(
        R(("pkg/TestY/aaaaaaaa", "error"), ("pkg/TestY/bbbbbbbb", "passed")),
        R(("pkg/TestY/cccccccc", "passed")),
    )
    assert r["error_to_pass"] == ["pkg/TestY/cccccccc"]
    assert r["fail_to_pass"] == []
    # The collapsed start error is reported under the after-side id (current test).
    assert r["error_outcomes"]["start"] == ["pkg/TestY/cccccccc"]
    assert r["error_outcomes"]["start_only"] == ["pkg/TestY/cccccccc"]


def test_severity_collapse_all_skipped_go_test():
    # Only skipped outcomes in the start group -> the group is skipped.
    r = clf("go_test").classify(
        R(("pkg/TestZ/aaaaaaaa", "skip"), ("pkg/TestZ/bbbbbbbb", "skip")),
        R(("pkg/TestZ/cccccccc", "passed")),
    )
    assert r["skipped_to_pass"] == ["pkg/TestZ/cccccccc"]
    assert r["fail_to_pass"] == []


def test_go_test_memory_address_collapse():
    # ptr_0xb26440 / ptr_0x1a2b3c both normalize to ptr_0x<ADDR> (NOT random-id
    # stripping — the suffix is non-alnum after substitution), so they match.
    r = clf("go_test").classify(
        R(("pkg/TestM/ptr_0xb26440", "failed")),
        R(("pkg/TestM/ptr_0x1a2b3c", "passed")),
    )
    assert r["fail_to_pass"] == ["pkg/TestM/ptr_0x1a2b3c"]


def test_go_test_meaningful_subtest_not_collapsed():
    # Near-miss: a 7-char meaningful subtest name must NOT be stripped, so the
    # before/after match as the same full id.
    r = clf("go_test").classify(
        R(("pkg/TestX/blpopex", "failed")),
        R(("pkg/TestX/blpopex", "passed")),
    )
    assert r["fail_to_pass"] == ["pkg/TestX/blpopex"]


def test_go_test_distinct_meaningful_subtests_are_separate():
    # Adversarial: distinct meaningful subtest names are distinct tests; no
    # over-collapse into a single transition.
    r = clf("go_test").classify(
        R(("pkg/TestX/subA", "failed")),
        R(("pkg/TestX/subB", "passed")),
    )
    assert r["fail_to_none"] == ["pkg/TestX/subA"]
    assert r["none_to_pass"] == ["pkg/TestX/subB"]
    assert r["fail_to_pass"] == []


def test_pytest_does_not_normalize_go_random_suffix():
    # Adversarial framework guard: under pytest the random suffixes are NOT
    # stripped, so the two ids are different tests (removed + new), not a match.
    r = clf("pytest").classify(
        R(("pkg/TestX/aaaaaaaa", "failed")),
        R(("pkg/TestX/cccccccc", "passed")),
    )
    assert r["fail_to_none"] == ["pkg/TestX/aaaaaaaa"]
    assert r["none_to_pass"] == ["pkg/TestX/cccccccc"]
    assert r["fail_to_pass"] == []


# ---------------------------------------------------------------------------
# Outcome normalization fallbacks (_normalize_outcome)
# ---------------------------------------------------------------------------
def test_normalize_outcome_fallback_buckets():
    c = clf()
    assert c._normalize_outcome("ok") == "pass"
    assert c._normalize_outcome("xfail") == "pass"
    assert c._normalize_outcome("pending") == "skipped"
    assert c._normalize_outcome("ignored") == "skipped"
    assert c._normalize_outcome("collection_error") == "error"
    assert c._normalize_outcome("import_error") == "error"
    assert c._normalize_outcome("crashed") == "error"
    # Genuinely unknown and explicit None both fall through to "fail".
    assert c._normalize_outcome("totally_unknown") == "fail"
    assert c._normalize_outcome(None) == "fail"
    # Adversarial: uppercase non-mapped token is lowercased; UPPER_SKIP is not a
    # skip token, so it is the "unknown -> fail" default.
    assert c._normalize_outcome("UPPER_SKIP") == "fail"


def test_is_pass_helper():
    # Legacy helper kept for backward compatibility: only raw "passed"/"xfail"
    # count as a pass; "failed" and normalized aliases do not.
    c = clf()
    assert c._is_pass("passed") is True
    assert c._is_pass("xfail") is True
    assert c._is_pass("failed") is False
    assert c._is_pass("pass") is False


def test_missing_outcome_field_defaults_to_fail():
    # A test entry with no "outcome" key -> "unknown" -> normalized to fail.
    before = {"tests": [{"nodeid": "t1"}]}
    after = R(("t1", "passed"))
    r = clf().classify(before, after)
    assert r["fail_to_pass"] == ["t1"]


def test_explicit_none_outcome_value_is_fail():
    before = {"tests": [{"nodeid": "t1", "outcome": None}]}
    r = clf().classify(before, R(("t1", "passed")))
    assert r["fail_to_pass"] == ["t1"]


def test_entry_without_nodeid_is_dropped():
    # No nodeid -> dropped from the outcome map entirely.
    before = {"tests": [{"outcome": "passed"}, {"nodeid": "t1", "outcome": "passed"}]}
    after = R(("t1", "passed"))
    r = clf().classify(before, after)
    assert r["pass_to_pass"] == ["t1"]
    assert r["new_tests"] == []
    assert r["removed_tests"] == []


# Snapshot of the pytest map, used by the framework-fallback assertion below.
OUTCOME_MAPPINGS_PYTEST = {
    "passed": "pass",
    "failed": "fail",
    "skipped": "skipped",
    "error": "error",
    "errored": "error",
    "xfailed": "pass",
    "xpassed": "fail",
}


def test_unknown_framework_falls_back_to_pytest_map():
    c = ClassifierUnderTest(framework="totally_made_up")
    assert c.outcome_map == OUTCOME_MAPPINGS_PYTEST
    # xfailed mapping (a pytest-specific key) is therefore available.
    assert c._normalize_outcome("xfailed") == "pass"


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------
def test_empty_inputs_produce_all_empty_categories():
    r = clf().classify(R(), R())
    for category in CLASSIFICATION_CATEGORIES:
        assert r[category] == [], category
    assert r["error_outcomes"] == {
        "start": [],
        "end": [],
        "both": [],
        "start_only": [],
        "end_only": [],
    }


def test_missing_tests_key_is_tolerated():
    r = clf().classify({}, {})
    for category in CLASSIFICATION_CATEGORIES:
        assert r[category] == [], category


def test_classification_has_exactly_expected_keys():
    r = clf().classify(R(), R())
    expected = set(CLASSIFICATION_CATEGORIES) | {"error_outcomes"}
    assert set(r.keys()) == expected


# ---------------------------------------------------------------------------
# Sorted, deterministic output
# ---------------------------------------------------------------------------
def test_outputs_are_sorted():
    before = R(("zzz", "passed"), ("aaa", "passed"), ("mmm", "failed"), ("bbb", "skipped"))
    after = R(("zzz", "failed"), ("aaa", "passed"), ("mmm", "passed"), ("ccc", "passed"))
    r = clf().classify(before, after)
    assert r["pass_to_pass"] == ["aaa"]
    assert r["pass_to_fail"] == ["zzz"]
    assert r["fail_to_pass"] == ["mmm"]
    assert r["skipped_to_none"] == ["bbb"]
    assert r["none_to_pass"] == ["ccc"]
    # dict lists are sorted by test_id.
    assert r["new_tests"] == [{"test_id": "ccc", "end_outcome": "pass"}]
    assert r["removed_tests"] == [{"test_id": "bbb", "start_outcome": "skipped"}]


def test_multiple_tests_same_category_sorted():
    before = R(("c", "passed"), ("a", "passed"), ("b", "passed"))
    after = R(("c", "passed"), ("a", "passed"), ("b", "passed"))
    r = clf().classify(before, after)
    assert r["pass_to_pass"] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# generate_summary
# ---------------------------------------------------------------------------
def test_generate_summary_counts_and_error_sidechannel():
    c = clf()
    cls = c.classify(R(("t1", "passed"), ("t2", "error")), R(("t1", "passed"), ("t2", "passed")))
    s = c.generate_summary(cls)
    assert s["pass_to_pass"] == 1
    # counted transition (parallel to none_to_pass)
    assert s["error_to_pass"] == 1
    # error side-channel still recorded
    assert s["start_error"] == 1
    assert s["end_error"] == 0
    assert s["error_overlap"] == 0
    assert s["start_only_error"] == 1
    assert s["end_only_error"] == 0


def test_generate_summary_totals_prefix_suffix_semantics():
    # total_before sums categories whose BEFORE side is pass/fail/skipped;
    # total_after sums categories whose AFTER side is pass/fail/skipped.
    # none_to_* (prefix none) is excluded from total_before;
    # *_to_none (suffix none) is excluded from total_after;
    # error_to_pass (prefix error) is excluded from total_before but its
    # suffix "pass" IS counted in total_after.
    before = R(
        ("pp", "passed"),
        ("pf", "passed"),
        ("ff", "failed"),
        ("sk", "skipped"),
        ("p_none", "passed"),
        ("err_start", "error"),
    )
    after = R(
        ("pp", "passed"),
        ("pf", "failed"),
        ("ff", "failed"),
        ("sk", "skipped"),
        ("new_pass", "passed"),
        ("err_start", "passed"),
    )
    c = clf()
    s = c.generate_summary(c.classify(before, after))
    # before contributors: pass_to_pass, pass_to_fail, fail_to_fail,
    # skipped_to_skipped, pass_to_none = 5
    assert s["total_before"] == 5
    # after contributors: pass_to_pass, pass_to_fail, fail_to_fail,
    # skipped_to_skipped, none_to_pass, error_to_pass = 6
    assert s["total_after"] == 6


def test_generate_summary_empty_classification():
    s = clf().generate_summary(clf().classify(R(), R()))
    for category in CLASSIFICATION_CATEGORIES:
        assert s[category] == 0, category
    assert s["start_error"] == 0
    assert s["end_error"] == 0
    assert s["error_overlap"] == 0
    assert s["start_only_error"] == 0
    assert s["end_only_error"] == 0
    assert s["total_before"] == 0
    assert s["total_after"] == 0


def test_generate_summary_tolerates_malformed_error_outcomes():
    # error_outcomes that is not a dict must not crash; counts default to 0.
    s = clf().generate_summary({"error_outcomes": "not_a_dict"})
    assert s["start_error"] == 0
    assert s["end_error"] == 0
    assert s["pass_to_pass"] == 0


def test_generate_summary_missing_categories_default_zero():
    # generate_summary uses .get(...) so a sparse dict yields zeros, not errors.
    s = clf().generate_summary({})
    for category in CLASSIFICATION_CATEGORIES:
        assert s[category] == 0, category
    assert s["start_error"] == 0


def test_generate_summary_aggregated_list_counts_do_not_pollute_totals():
    # new_tests/removed_tests are list-of-dict categories: generate_summary
    # len()s them, and because their keys contain no "_to_" they are excluded
    # from total_before/total_after. Pin the exact counts AND the totals.
    c = clf()
    cls = c.classify(
        R(("old", "passed"), ("rm", "failed")),
        R(("old", "passed"), ("n1", "passed"), ("n2", "skipped")),
    )
    s = c.generate_summary(cls)
    assert s["new_tests"] == 2
    assert s["removed_tests"] == 1
    # contributors with a pass/fail/skipped BEFORE side: pass_to_pass(old),
    # fail_to_none(rm) -> 2.
    assert s["total_before"] == 2
    # contributors with a pass/fail/skipped AFTER side: pass_to_pass(old),
    # none_to_pass(n1), none_to_skipped(n2) -> 3.
    assert s["total_after"] == 3


# ---------------------------------------------------------------------------
# Multi-test error matrix: only error->pass is a counted category; every other
# error transition is side-channel only, so total_before stays 0 here.
# ---------------------------------------------------------------------------
def test_error_to_fail_and_error_to_pass_only_pass_is_counted():
    c = clf()
    r = c.classify(R(("a", "error"), ("b", "error")), R(("a", "passed"), ("b", "failed")))
    assert r["error_to_pass"] == ["a"]
    # error->fail (b) produces NO counted transition at all.
    transition_cats = {k: r[k] for k in CLASSIFICATION_TRANSITION_CATEGORIES if k != "error_to_pass"}
    assert all(v == [] for v in transition_cats.values())
    assert r["error_outcomes"] == {
        "start": ["a", "b"],
        "end": [],
        "both": [],
        "start_only": ["a", "b"],
        "end_only": [],
    }
    s = c.generate_summary(r)
    # error_to_pass prefix "error" is NOT a before-state; error->fail has no
    # category -> total_before is 0 even though two tests started as errors.
    assert s["total_before"] == 0
    # error_to_pass suffix "pass" IS an after-state -> total_after is 1.
    assert s["total_after"] == 1
    assert s["start_error"] == 2


# ---------------------------------------------------------------------------
# Report-id resolution: prefer the after-side original id when a test exists on
# both sides; fall back to the before-side id when the test was removed.
# (go_test normalization lets the before/after originals differ while still
# matching on the normalized id.)
# ---------------------------------------------------------------------------
def test_report_id_prefers_after_side_when_present_both_sides():
    r = clf("go_test").classify(
        R(("pkg/T/aaaaaaaa", "passed")),
        R(("pkg/T/zzzzzzzz", "passed")),
    )
    # both originals normalize to pkg/T; the reported id is the AFTER-side one.
    assert r["pass_to_pass"] == ["pkg/T/zzzzzzzz"]


def test_report_id_falls_back_to_before_side_for_removed_test():
    r = clf("go_test").classify(
        R(("pkg/T/aaaaaaaa", "failed")),
        R(),
    )
    assert r["fail_to_none"] == ["pkg/T/aaaaaaaa"]
    assert r["removed_tests"] == [{"test_id": "pkg/T/aaaaaaaa", "start_outcome": "fail"}]


# ---------------------------------------------------------------------------
# Severity collapse on the AFTER side (the existing suite only covers the start
# side). The after group's collapsed outcome drives the transition, and the
# reported id is the first original id seen in that group.
# ---------------------------------------------------------------------------
def test_after_side_collapse_fail_over_pass_go_test():
    r = clf("go_test").classify(
        R(("pkg/T/aaaaaaaa", "passed")),
        R(("pkg/T/bbbbbbbb", "passed"), ("pkg/T/cccccccc", "failed")),
    )
    # after group has pass + fail -> fail wins -> pass_to_fail.
    assert r["pass_to_fail"] == ["pkg/T/bbbbbbbb"]
    assert r["fail_to_pass"] == []
    assert r["error_outcomes"]["end"] == []


def test_after_side_collapse_error_over_pass_go_test():
    r = clf("go_test").classify(
        R(("pkg/T/aaaaaaaa", "passed")),
        R(("pkg/T/bbbbbbbb", "passed"), ("pkg/T/cccccccc", "error")),
    )
    # after group has pass + error (no fail) -> error wins -> end-side error,
    # and NO counted transition (pass->error is side-channel only).
    assert r["error_outcomes"] == {
        "start": [],
        "end": ["pkg/T/bbbbbbbb"],
        "both": [],
        "start_only": [],
        "end_only": ["pkg/T/bbbbbbbb"],
    }
    transition_cats = {k: r[k] for k in CLASSIFICATION_TRANSITION_CATEGORIES}
    assert all(v == [] for v in transition_cats.values())


# ---------------------------------------------------------------------------
# Duplicate nodeid in one snapshot: _build_outcome_map is a plain dict, so the
# LAST entry for a nodeid wins (no severity collapse at this layer — that only
# happens across normalized ids).
# ---------------------------------------------------------------------------
def test_duplicate_nodeid_last_write_wins():
    # ("t1","passed") then ("t1","failed") -> outcome map keeps "failed".
    r = clf().classify(R(("t1", "passed"), ("t1", "failed")), R(("t1", "passed")))
    assert r["fail_to_pass"] == ["t1"]
    assert r["pass_to_pass"] == []
    # Reversed order -> last is "passed".
    r2 = clf().classify(R(("t1", "failed"), ("t1", "passed")), R(("t1", "passed")))
    assert r2["pass_to_pass"] == ["t1"]
    assert r2["fail_to_pass"] == []


# ---------------------------------------------------------------------------
# Per-framework outcome maps (only pytest + go_test were exercised before).
# ---------------------------------------------------------------------------
def test_framework_outcome_maps_are_selected_per_framework():
    for fw in ("pytest", "go_test", "maven", "cargo", "jest", "mocha"):
        assert ClassifierUnderTest(framework=fw).outcome_map is OUTCOME_MAPPINGS[fw]


def test_maven_error_transition_is_dual_recorded():
    # maven maps "errored"->error; error->pass is the dual-recorded transition.
    r = ClassifierUnderTest("maven").classify(R(("t", "errored")), R(("t", "passed")))
    assert r["error_to_pass"] == ["t"]
    assert r["error_outcomes"]["start"] == ["t"]


def test_cargo_ignored_and_ok_normalization():
    # cargo: "ignored"->skipped, "ok"->pass; FAILED (uppercase) mapped directly.
    r = ClassifierUnderTest("cargo").classify(R(("t", "ignored")), R(("t", "ok")))
    assert r["skipped_to_pass"] == ["t"]
    r2 = ClassifierUnderTest("cargo").classify(R(("t", "ok")), R(("t", "FAILED")))
    assert r2["pass_to_fail"] == ["t"]


def test_jest_and_mocha_pending_is_skipped():
    # jest/mocha map "pending"->skipped (not fail).
    assert ClassifierUnderTest("jest").classify(R(("t", "pending")), R(("t", "passed")))["skipped_to_pass"] == ["t"]
    assert ClassifierUnderTest("mocha").classify(R(("t", "passed")), R(("t", "pending")))["pass_to_skipped"] == ["t"]


def test_go_test_error_resolves_via_fallback_not_map():
    # go_test's map has NO "error" key; "error" reaches "error" only through the
    # _normalize_outcome fallback. This is load-bearing for the go_test error
    # collapse tests elsewhere in this file.
    assert "error" not in OUTCOME_MAPPINGS["go_test"]
    assert ClassifierUnderTest("go_test")._normalize_outcome("error") == "error"


# ---------------------------------------------------------------------------
# Direct unit coverage of the private helpers used transitively above.
# ---------------------------------------------------------------------------
def test_categorize_normalized_directly():
    c = clf()
    assert c._categorize_normalized("pass", "fail") == "pass_to_fail"
    assert c._categorize_normalized("skipped", "skipped") == "skipped_to_skipped"
    assert c._categorize_normalized(None, "pass") == "none_to_pass"
    assert c._categorize_normalized("fail", None) == "fail_to_none"


def test_build_outcome_map_directly():
    c = clf()
    assert c._build_outcome_map(R(("a", "passed"), ("b", "failed"))) == {
        "a": "passed",
        "b": "failed",
    }
    # empty / missing tests key.
    assert c._build_outcome_map({}) == {}
    assert c._build_outcome_map({"tests": []}) == {}
    # entries without nodeid are dropped; missing outcome defaults to "unknown".
    assert c._build_outcome_map({"tests": [{"outcome": "passed"}, {"nodeid": "x"}]}) == {"x": "unknown"}


# ---------------------------------------------------------------------------
# Filesystem entry points (tmp_path only — no Docker/git/report_parser needed
# for the standardized-JSON and missing-file branches).
# ---------------------------------------------------------------------------
def test_save_classification_roundtrip_with_summary(tmp_path):
    c = clf()
    cls = c.classify(R(("t", "failed")), R(("t", "passed")))
    out = tmp_path / "cls.json"
    c.save_classification(cls, out)
    loaded = json.loads(out.read_text())
    assert loaded["fail_to_pass"] == ["t"]
    assert "summary" in loaded
    assert loaded["summary"]["fail_to_pass"] == 1
    assert loaded["summary"]["total_after"] == 1


def test_save_classification_without_summary(tmp_path):
    c = clf()
    cls = c.classify(R(("t", "passed")), R(("t", "passed")))
    out = tmp_path / "cls.json"
    c.save_classification(cls, out, include_summary=False)
    loaded = json.loads(out.read_text())
    assert "summary" not in loaded
    assert loaded["pass_to_pass"] == ["t"]


def test_load_or_parse_missing_file_returns_empty(tmp_path):
    c = clf()
    assert c._load_or_parse(tmp_path / "does_not_exist.json") == {"tests": [], "summary": {}}


def test_load_or_parse_standardized_json_passthrough(tmp_path):
    c = clf()
    f = tmp_path / "std.json"
    payload = {"tests": [{"nodeid": "t", "outcome": "passed"}], "summary": {}}
    f.write_text(json.dumps(payload))
    assert c._load_or_parse(f) == payload


def test_load_or_parse_empty_tests_list_is_standardized(tmp_path):
    # An empty "tests" list is treated as already-standardized (the structure
    # check short-circuits on the empty list), so it is returned verbatim.
    c = clf()
    f = tmp_path / "empty.json"
    f.write_text(json.dumps({"tests": []}))
    assert c._load_or_parse(f) == {"tests": []}


def test_load_or_parse_non_standard_json_falls_back_to_parser(tmp_path):
    # Valid JSON whose first test lacks "outcome" is NOT standardized; it falls
    # through to parse_test_report, which fills outcome with "unknown".
    c = clf()
    f = tmp_path / "nonstd.json"
    f.write_text(json.dumps({"tests": [{"nodeid": "t"}]}))
    parsed = c._load_or_parse(f)
    assert {"nodeid": "t", "outcome": "unknown"} in parsed["tests"]


def test_classify_from_files_standardized_json(tmp_path):
    c = clf()
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    before.write_text(json.dumps({"tests": [{"nodeid": "t", "outcome": "failed"}]}))
    after.write_text(json.dumps({"tests": [{"nodeid": "t", "outcome": "passed"}]}))
    r = c.classify_from_files(before, after)
    assert r["fail_to_pass"] == ["t"]


def test_classify_from_files_missing_files_yield_empty(tmp_path):
    c = clf()
    r = c.classify_from_files(tmp_path / "nope_b.json", tmp_path / "nope_a.json")
    for category in CLASSIFICATION_CATEGORIES:
        assert r[category] == [], category


# ---------------------------------------------------------------------------
# Cross-seed determinism of the error side-channel ordering: feed deliberately
# unsorted ids and pin the sorted result (proves the sort, not iteration order).
# ---------------------------------------------------------------------------
def test_error_outcomes_lists_are_sorted():
    r = clf().classify(
        R(("z", "error"), ("a", "error"), ("m", "error")),
        R(("z", "error"), ("a", "passed"), ("m", "error")),
    )
    assert r["error_outcomes"]["start"] == ["a", "m", "z"]
    assert r["error_outcomes"]["end"] == ["m", "z"]
    assert r["error_outcomes"]["both"] == ["m", "z"]
    assert r["error_outcomes"]["start_only"] == ["a"]
    assert r["error_outcomes"]["end_only"] == []
    assert r["error_to_pass"] == ["a"]
