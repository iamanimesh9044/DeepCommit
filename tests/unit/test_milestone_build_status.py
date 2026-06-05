"""M2 build_status verdict — `built` is decided by HARD gates only.

These lock the gate PARTITION (HARD ∪ SOFT == QUALITY_GATES, disjoint) and the
pure verdict helpers (`_collect_failed_gates`, `_collect_soft_gate_warnings`) that
the `evaluate_build_status` monolith delegates to. Docker / git / LLM I/O is the
imperative shell and is NOT exercised here — only the deterministic core.

The model: a SOFT gate failing is a non-blocking signal (it surfaces in
`raw_metrics.soft_warnings` for the agent / M3 to triage TIMING_DAG pollution vs a
real env defect); only HARD gates + the B_* build-run gates block `built`.
"""

from deepcommit.harness.prepare_images.build_milestone_image.diagnosis import (
    BUILD_RUN_GATES,
    HARD_QUALITY_GATES,
    QUALITY_GATES,
    SOFT_QUALITY_GATES,
    _collect_failed_gates,
    _collect_soft_gate_warnings,
)
from deepcommit.harness.prepare_images.build_milestone_image.diagnosis import (
    DEFAULT_VALIDATION_THRESHOLDS as T,
)


def _passing_build_run():
    return {g: {"passed": True} for g in BUILD_RUN_GATES}


def _passing_quality():
    return {g: {"passed": True, "value": 0.0, "threshold": 0.0} for g in QUALITY_GATES}


# --- partition invariant ---------------------------------------------------


def test_hard_soft_partition_covers_quality_gates():
    assert set(HARD_QUALITY_GATES) | set(SOFT_QUALITY_GATES) == set(QUALITY_GATES)
    assert not (set(HARD_QUALITY_GATES) & set(SOFT_QUALITY_GATES))


def test_hard_soft_membership_locked():
    assert set(HARD_QUALITY_GATES) == {
        "H_patched_completeness",
        "H_patched_clean_signal",
        "H_none_error_fail_to_pass_present",
        "H_error_within_budget",
        "H_total_not_below_base",
    }
    assert set(SOFT_QUALITY_GATES) == {
        "H_f2f_not_above_base",
        "H_pass_to_pass_majority",
        "H_s2s_not_above_base",
        "H_anomaly_aggregate",
    }


# --- _collect_failed_gates: built is hard-only -----------------------------


def test_all_pass_no_failed_gates():
    assert _collect_failed_gates(_passing_build_run(), _passing_quality()) == []


def test_soft_gate_failure_does_not_block_built():
    q = _passing_quality()
    for soft in SOFT_QUALITY_GATES:
        q[soft] = {"passed": False, "value": 1.0, "threshold": 0.0}
    # built = not failed_gates → True even with every SOFT gate failing
    assert _collect_failed_gates(_passing_build_run(), q) == []


def test_hard_quality_gate_failure_blocks_built():
    q = _passing_quality()
    q["H_total_not_below_base"] = {"passed": False, "value": 0, "threshold": 0.2}
    assert "H_total_not_below_base" in _collect_failed_gates(_passing_build_run(), q)


def test_build_run_gate_failure_blocks_built():
    br = _passing_build_run()
    br["B_image_exists"] = {"passed": False}
    assert "B_image_exists" in _collect_failed_gates(br, _passing_quality())


def test_unevaluated_hard_gate_is_skipped():
    q = _passing_quality()
    q["H_patched_completeness"] = {"passed": False, "evaluated": False}
    assert "H_patched_completeness" not in _collect_failed_gates(_passing_build_run(), q)


def test_h_error_is_hard_blocking_when_over_ratio():
    # H_error is HARD: a gate dict with passed=False (ratio > 0.10) blocks built.
    q = _passing_quality()
    q["H_error_within_budget"] = {"passed": False, "value": 0.5, "threshold": 0.10}
    assert "H_error_within_budget" in _collect_failed_gates(_passing_build_run(), q)


# --- _collect_soft_gate_warnings -------------------------------------------


def test_soft_warning_emitted_for_failing_soft_gate():
    q = _passing_quality()
    q["H_s2s_not_above_base"] = {"passed": False, "value": 0.3, "threshold": 0.05}
    warnings = _collect_soft_gate_warnings(q)
    by_gate = {w["gate"]: w for w in warnings}
    assert "H_s2s_not_above_base" in by_gate
    assert by_gate["H_s2s_not_above_base"]["value"] == 0.3
    assert by_gate["H_s2s_not_above_base"]["soft_threshold"] == 0.05


def test_no_soft_warning_for_passing_soft_gate():
    assert _collect_soft_gate_warnings(_passing_quality()) == []


def test_unevaluated_soft_gate_yields_no_warning():
    q = _passing_quality()
    q["H_f2f_not_above_base"] = {"passed": False, "evaluated": False}
    assert _collect_soft_gate_warnings(q) == []


def test_hard_gate_failure_is_not_downgraded_to_soft_warning():
    # HARD gate failures block built; they must NOT also appear as soft warnings.
    q = _passing_quality()
    q["H_error_within_budget"] = {"passed": False, "value": 0.5, "threshold": 0.10}
    q["H_total_not_below_base"] = {"passed": False, "value": 0, "threshold": 0.2}
    assert _collect_soft_gate_warnings(q) == []


# --- H_error dual threshold (policy) ---------------------------------------


def test_h_error_hard_threshold_is_010():
    assert T["H_error_within_budget"] == 0.10


def test_h_error_soft_threshold_is_strict_zero():
    # SOFT line = 0 → ANY residual error ratio > 0 warns (non-blocking), so the
    # agent never silently passes a milestone that still errors.
    assert T["H_error_within_budget_warn"] == 0.0
    assert T["H_error_within_budget_warn"] < T["H_error_within_budget"]
