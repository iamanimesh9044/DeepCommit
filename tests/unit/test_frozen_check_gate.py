"""Deep characterization tests for the M3 frozen-check gate core.

Scope (Wave 1 — pure, in-memory only):
  - compute_quantity_gates   (retention / count-delta / loc-cv arithmetic)
  - compute_signal_gates     (per-milestone signal aggregation, two modes)
  - resolve_graded_milestones (whitelist / blacklist grading subset)
  - config/policy LOCKS: ANOMALY_TRANSITION_KEYS (frozen_check_gate vs diagnosis,
    skipped_to_pass excluded), RAW_M2_SIGNAL_THRESHOLDS,
    diagnosis.DEFAULT_VALIDATION_THRESHOLDS.

This file absorbs three legacy green tests: the grading test
(``resolve_graded_milestones``), the dual-threshold lock
(``RAW_M2_SIGNAL_THRESHOLDS`` / ``DEFAULT_VALIDATION_THRESHOLDS``), and the gate
half of the skipped-to-pass credit test (``ANOMALY_TRANSITION_KEYS``).

``analyze_milestone_raw_signals`` reads build_status/classification/tracker files
from a workspace and is deferred to Wave 4 (needs an fs fixture).

Every expected value below was captured by EXECUTING the function under test, not
by static tracing.
"""

from __future__ import annotations

import pytest

from deepcommit.harness.main_orch.frozen_check_gate import (
    ANOMALY_TRANSITION_KEYS,
    RAW_M2_SIGNAL_THRESHOLDS,
    UPPER_BOUND_GATES,
    Phase2Baseline,
    _slim_milestone_report,
    compute_quantity_gates,
    compute_signal_gates,
    resolve_graded_milestones,
)
from deepcommit.harness.prepare_images.build_milestone_image.diagnosis import (
    ANOMALY_TRANSITION_KEYS as DIAG_ANOMALY_KEYS,
)
from deepcommit.harness.prepare_images.build_milestone_image.diagnosis import (
    DEFAULT_VALIDATION_THRESHOLDS,
)

# Aggregate (quantity) thresholds used across the quantity-gate tests.
THRESH = {
    "commit_retention_rate": 0.85,
    "loc_retention_rate": 0.80,
    "milestone_count_delta": 0.20,
    "loc_cv": 1.50,
}

# Signal-gate ratio thresholds. The KEY names here are the *_ratio output names,
# distinct from the per-milestone signal_key names read off each report.
SIGNAL_THRESH = {
    "H_patched_completeness_ratio": 0.8,
    "H_none_error_fail_to_pass_present_ratio": 0.8,
    "H_anomaly_within_budget_ratio": 0.8,
}


def _signal_report(patched, f2p, anomaly):
    """Build one milestone's raw-signal report from three (key->subdict) inputs.

    Each of the three sub-reports must carry at least real_value / real_passed /
    value_with_waiver / passed_with_waiver — the fields the gate aggregator reads.
    """
    return {
        "H_patched_completeness": patched,
        "H_none_error_fail_to_pass_present": f2p,
        "H_anomaly_aggregate": anomaly,
    }


def _sub(real_value, real_passed, value_with_waiver, passed_with_waiver):
    return {
        "real_value": real_value,
        "real_passed": real_passed,
        "value_with_waiver": value_with_waiver,
        "passed_with_waiver": passed_with_waiver,
    }


# --------------------------------------------------------------------------- #
# compute_quantity_gates
# --------------------------------------------------------------------------- #
def test_quantity_gates_arithmetic_nominal():
    g = compute_quantity_gates(
        milestones={
            "M1": {"commits": "a;b;c", "src_loc": "100"},
            "M2": {"commits": "c;d", "src_loc": "300"},
        },
        candidate_prefix=["M1", "M2"],
        baseline=Phase2Baseline(total_commits=5, total_src_loc=500, total_milestones=4),
        thresholds=THRESH,
        upper_bound_gates=UPPER_BOUND_GATES,
        total_milestones_in_csv=4,
    )

    # commit retention: union {a,b,c,d}=4 / baseline 5 = 0.8 < 0.85 -> fail (>=)
    assert g["commit_retention_rate"]["value"] == 0.8
    assert g["commit_retention_rate"]["passed"] is False
    assert g["commit_retention_rate"]["direction"] == ">="
    assert g["commit_retention_rate"]["threshold"] == 0.85
    assert g["commit_retention_rate"]["evidence"] == {
        "candidate_commits": 4,
        "baseline_total_commits": 5,
    }

    # loc retention: (100+300)/500 = 0.8 >= 0.80 -> pass (boundary is inclusive)
    assert g["loc_retention_rate"]["value"] == 0.8
    assert g["loc_retention_rate"]["passed"] is True
    assert g["loc_retention_rate"]["evidence"] == {
        "candidate_src_loc": 400,
        "baseline_total_src_loc": 500,
    }

    # milestone_count_delta: |2-4|/4 = 0.5 <= 0.20? no -> fail (upper-bound gate)
    assert g["milestone_count_delta"]["value"] == 0.5
    assert g["milestone_count_delta"]["passed"] is False
    assert g["milestone_count_delta"]["direction"] == "<="
    assert g["milestone_count_delta"]["evidence"]["candidate_milestones"] == 2
    assert g["milestone_count_delta"]["evidence"]["baseline_scope_milestones"] == 4
    assert g["milestone_count_delta"]["evidence"]["csv_total_milestones"] == 4

    # loc_cv: pstdev([100,300]) / mean 200 = 100/200 = 0.5 <= 1.50 -> pass
    assert g["loc_cv"]["value"] == 0.5
    assert g["loc_cv"]["passed"] is True
    assert g["loc_cv"]["direction"] == "<="
    assert g["loc_cv"]["evidence"] == {
        "candidate_src_locs": [100, 300],
        "mean_src_loc": 200.0,
    }

    # exact top-level shape: only the four named gates, each with the same sub-keys
    assert set(g) == {
        "commit_retention_rate",
        "loc_retention_rate",
        "milestone_count_delta",
        "loc_cv",
    }
    for gate in g.values():
        assert set(gate) == {"value", "threshold", "passed", "direction", "evidence"}


def test_quantity_gates_single_milestone_zero_cv():
    # A single milestone has pstdev 0 -> loc_cv == 0.0; count-delta |1-1|/1 == 0.
    g = compute_quantity_gates(
        milestones={"M1": {"commits": "a;b", "src_loc": "50"}},
        candidate_prefix=["M1"],
        baseline=Phase2Baseline(total_commits=2, total_src_loc=50, total_milestones=1),
        thresholds=THRESH,
        upper_bound_gates=UPPER_BOUND_GATES,
        total_milestones_in_csv=1,
    )
    assert g["loc_cv"]["value"] == 0.0
    assert g["loc_cv"]["passed"] is True
    assert g["commit_retention_rate"]["value"] == 1.0
    assert g["loc_retention_rate"]["value"] == 1.0
    assert g["milestone_count_delta"]["value"] == 0.0
    assert g["milestone_count_delta"]["passed"] is True


def test_quantity_gates_empty_candidate_is_all_zero_not_crash():
    # Degenerate: no milestones selected. pstdev/mean guard -> 0.0, not ZeroDivision.
    g = compute_quantity_gates(
        milestones={},
        candidate_prefix=[],
        baseline=Phase2Baseline(total_commits=5, total_src_loc=100, total_milestones=3),
        thresholds=THRESH,
        upper_bound_gates=UPPER_BOUND_GATES,
        total_milestones_in_csv=3,
    )
    assert g["commit_retention_rate"]["value"] == 0.0
    assert g["commit_retention_rate"]["passed"] is False
    assert g["loc_retention_rate"]["value"] == 0.0
    assert g["loc_cv"]["value"] == 0.0
    assert g["loc_cv"]["evidence"] == {"candidate_src_locs": [], "mean_src_loc": 0.0}
    # |0 - 3| / 3 = 1.0 -> way over the 0.20 upper bound.
    assert g["milestone_count_delta"]["value"] == 1.0
    assert g["milestone_count_delta"]["passed"] is False


def test_quantity_gates_loc_fallback_and_csv_total_default():
    # 'loc' is read when 'src_loc' is absent; total_milestones_in_csv=None falls
    # back to baseline.total_milestones for the count-delta denominator.
    g = compute_quantity_gates(
        milestones={"M1": {"commits": "a", "loc": "200"}},
        candidate_prefix=["M1"],
        baseline=Phase2Baseline(total_commits=1, total_src_loc=400, total_milestones=2),
        thresholds=THRESH,
        upper_bound_gates=UPPER_BOUND_GATES,
        total_milestones_in_csv=None,
    )
    assert g["loc_retention_rate"]["value"] == 0.5
    assert g["loc_retention_rate"]["evidence"]["candidate_src_loc"] == 200
    # |1 - 2| / 2 = 0.5; denominator is baseline.total_milestones (2) since csv=None.
    assert g["milestone_count_delta"]["value"] == 0.5
    assert g["milestone_count_delta"]["evidence"]["csv_total_milestones"] == 2


def test_quantity_gates_commit_string_parsing_and_commitless_milestone():
    # Commit strings are ';'-split, stripped, and de-duped across milestones.
    # An empty / absent commits field contributes nothing (no crash, no phantom id).
    g = compute_quantity_gates(
        milestones={
            "M1": {"commits": "a; b ;;", "src_loc": "100"},  # whitespace + empty segs
            "M2": {"commits": "", "src_loc": "100"},  # empty -> no commits
            "M3": {"src_loc": "0"},  # commits key absent entirely
        },
        candidate_prefix=["M1", "M2", "M3"],
        baseline=Phase2Baseline(total_commits=4, total_src_loc=200, total_milestones=3),
        thresholds=THRESH,
        upper_bound_gates=UPPER_BOUND_GATES,
        total_milestones_in_csv=3,
    )
    # union of commit ids is {a, b} = 2 (NOT 4: blanks dropped, 'a;b' not double-counted).
    assert g["commit_retention_rate"]["evidence"]["candidate_commits"] == 2
    assert g["commit_retention_rate"]["value"] == 0.5
    assert g["loc_cv"]["evidence"]["candidate_src_locs"] == [100, 100, 0]


def test_quantity_gates_missing_threshold_key_raises():
    # A threshold dict missing any required key is a programming error, surfaced loudly.
    with pytest.raises(KeyError):
        compute_quantity_gates(
            milestones={"M1": {"commits": "a", "src_loc": "1"}},
            candidate_prefix=["M1"],
            baseline=Phase2Baseline(total_commits=1, total_src_loc=1, total_milestones=1),
            thresholds={"commit_retention_rate": 0.5},  # rest missing
            upper_bound_gates=UPPER_BOUND_GATES,
            total_milestones_in_csv=1,
        )


def test_quantity_gates_unknown_candidate_id_raises():
    with pytest.raises(KeyError):
        compute_quantity_gates(
            milestones={},
            candidate_prefix=["MX"],
            baseline=Phase2Baseline(total_commits=1, total_src_loc=1, total_milestones=1),
            thresholds=THRESH,
            upper_bound_gates=UPPER_BOUND_GATES,
            total_milestones_in_csv=1,
        )


def test_quantity_gate_upper_bound_boundary_is_inclusive():
    # "<=" gates pass at exact equality (value == threshold). loc_cv and
    # milestone_count_delta both land precisely on their threshold here.
    thresh = {
        "commit_retention_rate": 0.0,
        "loc_retention_rate": 0.0,
        "milestone_count_delta": 0.5,  # |2-4|/4 == 0.5 exactly
        "loc_cv": 0.5,  # pstdev([100,300])/200 == 0.5 exactly
    }
    g = compute_quantity_gates(
        milestones={
            "M1": {"commits": "a", "src_loc": "100"},
            "M2": {"commits": "b", "src_loc": "300"},
        },
        candidate_prefix=["M1", "M2"],
        baseline=Phase2Baseline(total_commits=2, total_src_loc=400, total_milestones=4),
        thresholds=thresh,
        upper_bound_gates=UPPER_BOUND_GATES,
        total_milestones_in_csv=4,
    )
    assert g["loc_cv"]["value"] == 0.5
    assert g["loc_cv"]["passed"] is True  # 0.5 <= 0.5 -> inclusive
    assert g["milestone_count_delta"]["value"] == 0.5
    assert g["milestone_count_delta"]["passed"] is True  # 0.5 <= 0.5 -> inclusive


def test_quantity_gate_upper_bound_decides_on_unrounded_value():
    # ADVERSARIAL near-miss (twin of the signal-gate unrounded test): the upper-bound
    # count-delta is |1-2|/3 = 0.33333... It DISPLAYS as 0.333333 (rounded to 6dp),
    # which would naively pass a "<= 0.3333331" reading — but the pass decision uses
    # the FULL-precision 0.3333333..., which is > 0.3333331, so it must FAIL.
    thresh = {
        "commit_retention_rate": 0.0,
        "loc_retention_rate": 0.0,
        "milestone_count_delta": 0.3333331,
        "loc_cv": 100.0,
    }
    g = compute_quantity_gates(
        milestones={
            "M1": {"commits": "a", "src_loc": "1"},
            "M2": {"commits": "b", "src_loc": "1"},
            "M3": {"commits": "c", "src_loc": "1"},
        },
        candidate_prefix=["M1"],
        baseline=Phase2Baseline(total_commits=1, total_src_loc=1, total_milestones=2),
        thresholds=thresh,
        upper_bound_gates=UPPER_BOUND_GATES,
        total_milestones_in_csv=3,
    )
    mcd = g["milestone_count_delta"]
    assert mcd["value"] == 0.333333  # displayed (rounded)
    assert mcd["passed"] is False  # decided on 0.3333333... > 0.3333331


def test_quantity_gate_lower_bound_decides_on_unrounded_value():
    # Twin near-miss for a ">=" gate: commit retention 2/3 = 0.66666...; displays
    # 0.666667 yet must PASS a 0.6666661 threshold (real 0.66666... >= it) AND must
    # FAIL a 0.6666667 threshold (real 0.666666... < 0.6666667).
    base = dict(
        milestones={
            "M1": {"commits": "a;b", "src_loc": "1"},
        },
        candidate_prefix=["M1"],
        baseline=Phase2Baseline(total_commits=3, total_src_loc=1, total_milestones=1),
        upper_bound_gates=UPPER_BOUND_GATES,
        total_milestones_in_csv=1,
    )
    pass_thr = {
        "commit_retention_rate": 0.6666661,
        "loc_retention_rate": 0.0,
        "milestone_count_delta": 1.0,
        "loc_cv": 1.0,
    }
    fail_thr = {
        "commit_retention_rate": 0.6666667,
        "loc_retention_rate": 0.0,
        "milestone_count_delta": 1.0,
        "loc_cv": 1.0,
    }
    g_pass = compute_quantity_gates(thresholds=pass_thr, **base)
    g_fail = compute_quantity_gates(thresholds=fail_thr, **base)
    assert g_pass["commit_retention_rate"]["value"] == 0.666667  # displayed (rounded)
    assert g_pass["commit_retention_rate"]["passed"] is True  # 0.66666... >= 0.6666661
    assert g_fail["commit_retention_rate"]["passed"] is False  # 0.66666... < 0.6666667


# --------------------------------------------------------------------------- #
# compute_signal_gates
# --------------------------------------------------------------------------- #
def test_signal_gates_aggregation_modes_nominal():
    ok = _sub(1, True, 1, True)
    reports = {
        "M1": _signal_report(_sub(1.0, True, 1.0, True), ok, ok),
        "M2": _signal_report(_sub(0.9, False, 0.9, False), ok, ok),
    }
    g = compute_signal_gates(reports, SIGNAL_THRESH)

    # patched uses value_mean -> (1.0 + 0.9)/2 = 0.95; others use pass_count_ratio.
    pc = g["H_patched_completeness_ratio"]
    assert pc["value_real"] == 0.95
    assert pc["aggregation_mode"] == "value_mean"
    assert pc["passed"] is True
    assert pc["direction"] == ">="
    assert pc["threshold"] == 0.8
    assert pc["evidence"]["real_passed_milestones"] == ["M1"]
    assert pc["evidence"]["real_failed_milestones"] == ["M2"]
    assert pc["evidence"]["pass_count_ratio"] == 0.5
    assert pc["evidence"]["value_mean"] == 0.95
    # one milestone failed raw eval -> the "Raw evaluation failed" note appears.
    assert pc["note"] == "Raw evaluation failed for 1/2 milestone(s) after ignoring waivers."

    f2p = g["H_none_error_fail_to_pass_present_ratio"]
    assert f2p["value_real"] == 1.0
    assert f2p["aggregation_mode"] == "pass_count_ratio"
    assert f2p["evidence"]["value_mean"] is None  # pass_count_ratio never computes a mean
    assert "note" not in f2p  # all passed, waiver == real -> no note

    anom = g["H_anomaly_within_budget_ratio"]
    assert anom["value_real"] == 1.0
    assert anom["aggregation_mode"] == "pass_count_ratio"

    # exact top-level shape.
    assert set(g) == {
        "H_patched_completeness_ratio",
        "H_none_error_fail_to_pass_present_ratio",
        "H_anomaly_within_budget_ratio",
    }


def test_signal_gate_full_subkey_shape():
    ok = _sub(1, True, 1, True)
    reports = {"M1": _signal_report(_sub(1.0, True, 1.0, True), ok, ok)}
    pc = compute_signal_gates(reports, SIGNAL_THRESH)["H_patched_completeness_ratio"]
    assert set(pc) == {
        "value_with_waiver",
        "value_real",
        "threshold",
        "passed",
        "direction",
        "aggregation_mode",
        "evidence",
    }
    assert set(pc["evidence"]) == {
        "milestones_total",
        "real_passed_count",
        "real_failed_count",
        "real_passed_milestones",
        "real_failed_milestones",
        "pass_count_ratio",
        "value_mean",
        "with_waiver",
    }
    assert set(pc["evidence"]["with_waiver"]) == {
        "available",
        "unavailable_milestones",
        "passed_milestones",
        "pass_count_ratio",
        "value_mean",
    }


def test_signal_gate_waiver_unavailable_marks_value_none():
    # If any milestone has passed_with_waiver=None, the waiver aggregate is
    # unavailable: value_with_waiver=None and the evidence is a stub (no ratios).
    sub = _sub(1.0, True, None, None)
    reports = {"M1": _signal_report(sub, sub, sub)}
    pc = compute_signal_gates(reports, {**SIGNAL_THRESH, "H_patched_completeness_ratio": 1.0})[
        "H_patched_completeness_ratio"
    ]
    assert pc["value_with_waiver"] is None
    assert pc["value_real"] == 1.0
    assert pc["passed"] is True
    assert pc["evidence"]["with_waiver"] == {
        "available": False,
        "unavailable_milestones": ["M1"],
        "passed_milestones": None,
    }
    # No "differ" note: value_with_waiver is None so the differ branch is skipped.
    assert "note" not in pc


def test_signal_gate_value_mean_falls_back_when_no_numeric_real_value():
    # value_mean mode but every real_value is non-numeric (None) -> value_mean
    # stays None and the gate compares pass_count_ratio instead.
    reports = {
        "M1": _signal_report(_sub(None, True, None, True), _sub(None, True, None, True), _sub(None, True, None, True)),
        "M2": _signal_report(
            _sub(None, False, None, False), _sub(None, False, None, False), _sub(None, False, None, False)
        ),
    }
    thresh = {
        "H_patched_completeness_ratio": 0.4,
        "H_none_error_fail_to_pass_present_ratio": 0.4,
        "H_anomaly_within_budget_ratio": 0.4,
    }
    pc = compute_signal_gates(reports, thresh)["H_patched_completeness_ratio"]
    assert pc["aggregation_mode"] == "value_mean"  # mode unchanged...
    assert pc["evidence"]["value_mean"] is None  # ...but no numeric mean was formed
    assert pc["value_real"] == 0.5  # so it compares pass_count_ratio (1 of 2 passed)
    assert pc["passed"] is True


def test_signal_gate_waiver_differ_note():
    # pass_count_ratio gate, all milestones pass raw eval, but one fails with-waiver
    # -> waiver ratio differs from raw ratio -> the "differ" note (not the "failed" note).
    reports = {
        "M1": _signal_report(_sub(5, True, 5, True), _sub(5, True, 5, True), _sub(5, True, 5, True)),
        "M2": _signal_report(_sub(5, True, 5, False), _sub(5, True, 5, False), _sub(5, True, 5, False)),
    }
    thresh = {
        "H_patched_completeness_ratio": 0.4,
        "H_none_error_fail_to_pass_present_ratio": 0.4,
        "H_anomaly_within_budget_ratio": 0.4,
    }
    f2p = compute_signal_gates(reports, thresh)["H_none_error_fail_to_pass_present_ratio"]
    assert f2p["value_real"] == 1.0
    assert f2p["value_with_waiver"] == 0.5
    assert f2p["note"] == "Waiver-adjusted and raw pass ratios differ."


def test_signal_gates_empty_reports_is_zero_and_available():
    # No milestones: ratios are 0.0 (empty denominator guard), and the waiver
    # aggregate is "available" (an empty unavailable-list is not unavailable).
    g = compute_signal_gates(
        {},
        {
            "H_patched_completeness_ratio": 0.0,
            "H_none_error_fail_to_pass_present_ratio": 0.0,
            "H_anomaly_within_budget_ratio": 0.0,
        },
    )
    pc = g["H_patched_completeness_ratio"]
    assert pc["value_real"] == 0.0
    assert pc["value_with_waiver"] == 0.0
    assert pc["passed"] is True  # 0.0 >= 0.0
    assert pc["evidence"]["milestones_total"] == 0
    assert pc["evidence"]["real_passed_milestones"] == []
    assert pc["evidence"]["with_waiver"]["available"] is True
    assert pc["evidence"]["with_waiver"]["passed_milestones"] == []
    assert "note" not in pc


def test_signal_gate_passed_uses_unrounded_value_real():
    # Near-miss / anti-over-match: reported value_real is rounded to 6 dp, but the
    # pass decision compares the FULL-precision value. value_mean = 1/3 displays as
    # 0.333333 yet must FAIL a 0.3333335 threshold.
    reports = {
        "M1": _signal_report(_sub(0.0, True, None, None), _sub(0.0, True, None, None), _sub(0.0, True, None, None)),
        "M2": _signal_report(_sub(0.0, True, None, None), _sub(0.0, True, None, None), _sub(0.0, True, None, None)),
        "M3": _signal_report(_sub(1.0, True, None, None), _sub(1.0, True, None, None), _sub(1.0, True, None, None)),
    }
    thresh = {
        "H_patched_completeness_ratio": 0.3333335,
        "H_none_error_fail_to_pass_present_ratio": 0.0,
        "H_anomaly_within_budget_ratio": 0.0,
    }
    pc = compute_signal_gates(reports, thresh)["H_patched_completeness_ratio"]
    assert pc["value_real"] == 0.333333  # displayed (rounded)
    assert pc["passed"] is False  # decided on 0.3333... < 0.3333335


def test_signal_gate_value_mean_mode_waiver_differ_note():
    # value_mean mode: ALL milestones pass raw eval (so the "failed" note is NOT
    # taken), but the waiver-adjusted MEAN (0.7) differs from the raw MEAN (0.95)
    # -> the "differ" note fires. This is a distinct branch from the
    # pass_count_ratio differ note: here the divergence is in the value_mean of
    # value_with_waiver, not in a flipped passed_with_waiver flag.
    reports = {
        "M1": _signal_report(_sub(1.0, True, 0.8, True), _sub(1, True, 1, True), _sub(1, True, 1, True)),
        "M2": _signal_report(_sub(0.9, True, 0.6, True), _sub(1, True, 1, True), _sub(1, True, 1, True)),
    }
    thresh = {
        "H_patched_completeness_ratio": 0.5,
        "H_none_error_fail_to_pass_present_ratio": 0.5,
        "H_anomaly_within_budget_ratio": 0.5,
    }
    pc = compute_signal_gates(reports, thresh)["H_patched_completeness_ratio"]
    assert pc["value_real"] == 0.95  # mean(1.0, 0.9)
    assert pc["value_with_waiver"] == 0.7  # mean(0.8, 0.6) — value_mean over waivers
    assert pc["evidence"]["with_waiver"]["value_mean"] == 0.7
    assert pc["evidence"]["with_waiver"]["pass_count_ratio"] == 1.0  # all passed_with_waiver
    assert pc["note"] == "Waiver-adjusted and raw pass ratios differ."


def test_signal_gate_value_mean_equal_waiver_emits_no_note():
    # value_mean mode where raw mean == waiver mean (both exactly 1/3): even though
    # both DISPLAY as the same rounded 0.333333, the differ-note compares the
    # UNROUNDED locals, finds them equal, and emits no note.
    reports = {
        "M1": _signal_report(_sub(1.0, True, 1.0, True), _sub(1, True, 1, True), _sub(1, True, 1, True)),
        "M2": _signal_report(_sub(0.0, True, 0.0, True), _sub(1, True, 1, True), _sub(1, True, 1, True)),
        "M3": _signal_report(_sub(0.0, True, 0.0, True), _sub(1, True, 1, True), _sub(1, True, 1, True)),
    }
    thresh = {
        "H_patched_completeness_ratio": 0.0,
        "H_none_error_fail_to_pass_present_ratio": 0.0,
        "H_anomaly_within_budget_ratio": 0.0,
    }
    pc = compute_signal_gates(reports, thresh)["H_patched_completeness_ratio"]
    assert pc["value_real"] == 0.333333
    assert pc["value_with_waiver"] == 0.333333
    assert "note" not in pc  # unrounded 1/3 == 1/3 -> no spurious differ note


def test_signal_gate_value_mean_waiver_skips_non_numeric_values():
    # value_mean over value_with_waiver ignores non-numeric (None) entries: with
    # one numeric waiver value (0.8) and one None, the waiver mean is 0.8 (mean of
    # the single numeric), NOT 0.4 (it does not treat None as 0).
    reports = {
        "M1": _signal_report(_sub(1.0, True, 0.8, True), _sub(1, True, 1, True), _sub(1, True, 1, True)),
        "M2": _signal_report(_sub(0.6, True, None, True), _sub(1, True, 1, True), _sub(1, True, 1, True)),
    }
    thresh = {
        "H_patched_completeness_ratio": 0.5,
        "H_none_error_fail_to_pass_present_ratio": 0.5,
        "H_anomaly_within_budget_ratio": 0.5,
    }
    pc = compute_signal_gates(reports, thresh)["H_patched_completeness_ratio"]
    # waiver available (both passed_with_waiver True), value_mean over numeric only.
    assert pc["evidence"]["with_waiver"]["available"] is True
    assert pc["evidence"]["with_waiver"]["value_mean"] == 0.8  # mean([0.8]) — None skipped
    assert pc["value_with_waiver"] == 0.8
    assert pc["value_real"] == 0.8  # mean(1.0, 0.6)


def test_signal_gates_missing_ratio_threshold_key_raises():
    ok = _sub(1, True, 1, True)
    reports = {"M1": _signal_report(ok, ok, ok)}
    with pytest.raises(KeyError):
        compute_signal_gates(reports, {"H_patched_completeness_ratio": 0.5})


# --------------------------------------------------------------------------- #
# resolve_graded_milestones  (replaces test_frozen_check_grading.py)
# --------------------------------------------------------------------------- #
def test_grading_whitelist_keeps_only_selected_in_candidate_order():
    r = resolve_graded_milestones(["M1", "M2", "M3"], ["M3", "M1"], None)
    # candidate order is preserved, NOT whitelist order.
    assert r["graded"] == ["M1", "M3"]
    assert r["excluded"] == ["M2"]
    assert r["deselected"] == ["M2"]
    assert r["non_graded_applied"] == []
    assert r["exclusion_used"] is True
    assert set(r) == {"graded", "excluded", "non_graded_applied", "deselected", "exclusion_used"}


def test_grading_blacklist_removes_listed():
    r = resolve_graded_milestones(["M1", "M2", "M3"], None, ["M2"])
    assert r["graded"] == ["M1", "M3"]
    assert r["excluded"] == ["M2"]
    assert r["non_graded_applied"] == ["M2"]
    assert r["deselected"] == []
    assert r["exclusion_used"] is True


def test_grading_no_exclusions_is_identity():
    r = resolve_graded_milestones(["M1", "M2"], None, None)
    assert r["graded"] == ["M1", "M2"]
    assert r["excluded"] == []
    assert r["non_graded_applied"] == []
    assert r["deselected"] == []
    assert r["exclusion_used"] is False


def test_grading_empty_whitelist_grades_nothing_but_is_active():
    # selected=[] (not None) is a real, if drastic, exclusion: grade nobody.
    r = resolve_graded_milestones(["M1", "M2"], [], None)
    assert r["graded"] == []
    assert r["excluded"] == ["M1", "M2"]
    assert r["deselected"] == ["M1", "M2"]
    assert r["exclusion_used"] is True


def test_grading_whitelist_and_blacklist_compose():
    r = resolve_graded_milestones(["M1", "M2", "M3"], ["M1", "M2"], ["M2"])
    # graded = (in whitelist) AND (not blacklisted) = {M1,M2} - {M2} = {M1}.
    assert r["graded"] == ["M1"]
    # M2 is both blacklisted and (trivially) graded-removed; M3 is deselected.
    assert r["excluded"] == ["M2", "M3"]
    assert r["non_graded_applied"] == ["M2"]
    assert r["deselected"] == ["M3"]
    assert r["exclusion_used"] is True


def test_grading_strips_whitespace_and_drops_empty_entries():
    # Adversarial input: padded ids match after strip; whitespace-only / empty
    # entries are dropped from BOTH lists (so a blank blacklist does nothing).
    r = resolve_graded_milestones(["M1", "M2"], ["  M1 ", ""], ["  "])
    assert r["graded"] == ["M1"]
    assert r["excluded"] == ["M2"]
    assert r["deselected"] == ["M2"]
    assert r["non_graded_applied"] == []  # "  " stripped to empty -> not applied


def test_grading_whitelist_id_not_in_candidate_is_ignored():
    # A whitelist entry that names a milestone absent from the candidate prefix
    # neither adds it nor errors; it is simply inert.
    r = resolve_graded_milestones(["M1"], ["M1", "MX"], None)
    assert r["graded"] == ["M1"]
    assert r["excluded"] == []
    assert r["deselected"] == []
    assert r["exclusion_used"] is True


def test_grading_excluded_keeps_candidate_order_but_applied_lists_are_sorted():
    # DETERMINISM + ORDERING contract (the author's tests never separated these
    # because they used single-element or already-sorted inputs):
    #   - graded / excluded preserve CANDIDATE order (a stable, position-faithful view)
    #   - non_graded_applied / deselected are SORTED (canonical audit view)
    # Candidate is deliberately NOT in sorted order: M3, M1, M2.
    candidate = ["M3", "M1", "M2"]
    # Blacklist M3 and M1 (given out of candidate order): excluded follows the
    # candidate sequence (M3 before M1) while non_graded_applied is sorted (M1, M3).
    r = resolve_graded_milestones(candidate, None, ["M1", "M3"])
    assert r["graded"] == ["M2"]
    assert r["excluded"] == ["M3", "M1"]  # candidate order, NOT sorted
    assert r["non_graded_applied"] == ["M1", "M3"]  # SORTED, NOT candidate order
    assert r["deselected"] == []
    assert r["exclusion_used"] is True

    # Whitelist-only on the same unsorted candidate: deselected is sorted too.
    r2 = resolve_graded_milestones(candidate, ["M2"], None)
    assert r2["graded"] == ["M2"]
    assert r2["excluded"] == ["M3", "M1"]  # candidate order
    assert r2["deselected"] == ["M1", "M3"]  # SORTED
    assert r2["non_graded_applied"] == []


def test_grading_duplicate_blacklist_entries_collapse_in_applied():
    # ng is built as a set, so a blacklist that lists the same id twice yields a
    # single non_graded_applied entry (no duplicate in the canonical view).
    r = resolve_graded_milestones(["M1", "M2"], None, ["M2", "M2", " M2 "])
    assert r["graded"] == ["M1"]
    assert r["excluded"] == ["M2"]
    assert r["non_graded_applied"] == ["M2"]  # de-duped, stripped


# --------------------------------------------------------------------------- #
# policy lock: skipped_to_pass is NOT an anomaly (twin of error_to_pass)
# --------------------------------------------------------------------------- #
def test_skipped_to_pass_excluded_from_anomaly_keys_both_modules():
    assert "skipped_to_pass" not in ANOMALY_TRANSITION_KEYS
    assert "skipped_to_pass" not in DIAG_ANOMALY_KEYS
    # error_to_pass is likewise credited, never penalised.
    assert "error_to_pass" not in ANOMALY_TRANSITION_KEYS
    assert "error_to_pass" not in DIAG_ANOMALY_KEYS


def test_anomaly_keys_are_in_sync_across_modules():
    # The two copies (one tuple, one list) must remain identical — single policy.
    assert set(ANOMALY_TRANSITION_KEYS) == set(DIAG_ANOMALY_KEYS)
    # Stronger: same membership AND same order (so a reorder is also caught).
    assert list(ANOMALY_TRANSITION_KEYS) == list(DIAG_ANOMALY_KEYS)
    assert len(ANOMALY_TRANSITION_KEYS) == 9


def test_genuinely_bad_transitions_are_penalised():
    assert {"pass_to_fail", "none_to_fail", "fail_to_none"} <= set(ANOMALY_TRANSITION_KEYS)
    # Lock the exact policy set so silently adding/removing an anomaly breaks CI.
    assert set(ANOMALY_TRANSITION_KEYS) == {
        "pass_to_fail",
        "pass_to_skipped",
        "fail_to_skipped",
        "skipped_to_fail",
        "none_to_fail",
        "none_to_skipped",
        "pass_to_none",
        "fail_to_none",
        "skipped_to_none",
    }


# --------------------------------------------------------------------------- #
# config lock: anti-cheat thresholds (replaces test_gate_dual_threshold.py)
# --------------------------------------------------------------------------- #
def test_raw_m2_signal_thresholds_locked():
    assert RAW_M2_SIGNAL_THRESHOLDS == {
        "H_patched_completeness": 1.00,
        "H_none_error_fail_to_pass_present": 1.00,
        "H_anomaly_aggregate": 0.02,
    }
    # Dict-equality is BLIND to int/float ({...:1.0} == {...:1}); lock the type
    # explicitly. In the RAW (M2 signal) copy these are float RATIOS, not the
    # integer presence count that DEFAULT_VALIDATION_THRESHOLDS uses.
    assert type(RAW_M2_SIGNAL_THRESHOLDS["H_patched_completeness"]) is float
    assert type(RAW_M2_SIGNAL_THRESHOLDS["H_none_error_fail_to_pass_present"]) is float
    assert type(RAW_M2_SIGNAL_THRESHOLDS["H_anomaly_aggregate"]) is float
    # Exact key set (no silent additions/removals).
    assert set(RAW_M2_SIGNAL_THRESHOLDS) == {
        "H_patched_completeness",
        "H_none_error_fail_to_pass_present",
        "H_anomaly_aggregate",
    }


def test_default_validation_thresholds_hard_bars_locked():
    t = DEFAULT_VALIDATION_THRESHOLDS
    assert t["H_patched_completeness"] == 1.00
    assert t["H_pass_to_pass_majority"] == 0.75
    assert t["H_f2f_not_above_base"] == 0.15  # patched-scoped, not whole-suite
    assert t["H_error_within_budget"] == 0.10  # ratio, not count
    assert t["H_total_not_below_base"] == 0.20
    # H_none_error_fail_to_pass_present is the integer presence count 1 (>= 1 F2P/N2P).
    # CRITICAL: `== 1` is True for BOTH int(1) and float(1.0) (1 == 1.0), so the
    # equality alone does NOT lock the type. The whole point of the author's
    # discrepancy note is that this must be int here while the RAW copy is float.
    # Assert the type so a regression to 1.0 breaks CI.
    assert t["H_none_error_fail_to_pass_present"] == 1
    assert type(t["H_none_error_fail_to_pass_present"]) is int
    # And the same key in the RAW copy is genuinely a DIFFERENT type (float),
    # proving the two policies are intentionally divergent on type.
    assert type(RAW_M2_SIGNAL_THRESHOLDS["H_none_error_fail_to_pass_present"]) is float
    # anomaly budget must match the frozen-check M2 copy exactly (single policy).
    assert t["H_anomaly_aggregate"] == RAW_M2_SIGNAL_THRESHOLDS["H_anomaly_aggregate"] == 0.02
    assert t["module2_acceptance_mode"] == "hard_gate"


def test_default_validation_thresholds_full_scalar_shape_locked():
    # Exhaustive lock of every SCALAR (non-dict) policy value. The hard-bars test
    # above pins the load-bearing few; this one freezes the ENTIRE flat policy so
    # an accidentally-edited soft-warning bar or a renamed/dropped key breaks CI.
    # Nested dicts (large_src_append / src_function_replaced / per_class_thresholds)
    # are deliberately excluded here — they are numstat-lint policy, locked by the
    # lint test suite, not the frozen-check gate suite.
    scalars = {k: v for k, v in DEFAULT_VALIDATION_THRESHOLDS.items() if not isinstance(v, dict)}
    assert scalars == {
        "H_patched_completeness": 1.00,
        "H_patched_clean_signal": 0.30,
        "H_pass_to_pass_majority": 0.75,
        "H_none_error_fail_to_pass_present": 1,
        "H_total_not_below_base": 0.20,
        "H_f2f_not_above_base": 0.15,
        "H_error_within_budget": 0.10,
        "H_s2s_not_above_base": 0.05,
        "H_anomaly_aggregate": 0.02,
        "H_pass_to_pass_majority_warn": 0.85,
        "H_anomaly_aggregate_warn": 0.01,
        "H_error_within_budget_warn": 0.0,
        "H_f2f_whole_warn": 0.05,
        "H_total_not_below_base_warn": 0.10,
        "module2_acceptance_mode": "hard_gate",
        "module2_validation_timeout_seconds": 3600,
        "module2_validation_retries": 3,
    }
    # Every soft-warning bar is strictly looser than (or equal to, for f2f which is
    # a separate whole-suite metric) its hard counterpart — a sanity invariant.
    assert (
        DEFAULT_VALIDATION_THRESHOLDS["H_pass_to_pass_majority_warn"]
        > DEFAULT_VALIDATION_THRESHOLDS["H_pass_to_pass_majority"]
    )
    assert (
        DEFAULT_VALIDATION_THRESHOLDS["H_anomaly_aggregate_warn"] < DEFAULT_VALIDATION_THRESHOLDS["H_anomaly_aggregate"]
    )
    assert (
        DEFAULT_VALIDATION_THRESHOLDS["H_error_within_budget_warn"]
        < DEFAULT_VALIDATION_THRESHOLDS["H_error_within_budget"]
    )
    # The nested numstat-policy dicts are present (so a wholesale deletion is caught)
    # but their internals are out of this suite's scope.
    assert isinstance(DEFAULT_VALIDATION_THRESHOLDS["large_src_append"], dict)
    assert isinstance(DEFAULT_VALIDATION_THRESHOLDS["src_function_replaced"], dict)
    assert isinstance(DEFAULT_VALIDATION_THRESHOLDS["per_class_thresholds"], dict)


def test_upper_bound_gates_locked():
    # Only count-delta and loc-cv are "<=" gates; retention rates are ">=".
    assert UPPER_BOUND_GATES == frozenset({"milestone_count_delta", "loc_cv"})


def test_slim_milestone_report_keeps_only_index_and_recomputed_values():
    # report["milestones"][mid] is slimmed to the freeze index (latest_attempt, the
    # pin's sole input) + recomputed real_value/real_passed; the bulk (paths,
    # waivers, errors, with-waiver sub-values) — all duplicated in build_status — is dropped.
    full = {
        "build_status_path": "/x/build_status.json",
        "latest_attempt": "attempt_9",
        "classification_path": "/x/classification.json",
        "errors": [],
        "waivers": {"present": True, "bound_waivers_count": 3},
        "H_patched_completeness": {
            "real_value": 0.9,
            "real_passed": True,
            "value_with_waiver": 1.0,
            "raw_effective": 10,
        },
        "H_none_error_fail_to_pass_present": {"real_value": 3, "real_passed": True, "fail_to_pass": 2},
        "H_anomaly_aggregate": {"real_value": 0.01, "real_passed": True, "anomaly_count": 1},
    }
    slim = _slim_milestone_report(full)
    assert set(slim) == {"latest_attempt", "real_value", "real_passed"}
    assert slim["latest_attempt"] == "attempt_9"  # the freeze pin reads this
    assert slim["real_value"] == {
        "H_patched_completeness": 0.9,
        "H_none_error_fail_to_pass_present": 3,
        "H_anomaly_aggregate": 0.01,
    }
    assert slim["real_passed"]["H_patched_completeness"] is True
    assert "build_status_path" not in slim and "waivers" not in slim and "value_with_waiver" not in slim["real_value"]
