"""Tests for the dashboard verb — Step-1 broad survey of each milestone's latest
state. Pure read-only: exit 0 always (exit 1 only on a malformed workspace).
"""

import json
import os

from deepcommit.harness.main_orch import context as ctx
from deepcommit.harness.main_orch.verbs import dashboard

from . import m3_fixtures as fx


def test_dashboard_surveys_latest_state_per_milestone(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1", "M2"))
    fx.write_build_status(ws, "M1", built=True, unbound=0)
    fx.write_build_status(ws, "M2", built=False, failed_gates=["H_error_within_budget"], unbound=7)
    fx.make_attempts(ws, "M1", 1)
    fx.make_attempts(ws, "M2", 6)
    fx.write_frozen_prefix(ws, frozen=["M1"], scope=["M1", "M2"])

    report, code = dashboard.run_dashboard(ws)
    assert code == ctx.OK
    rows = {r["milestone_id"]: r for r in report["rows"]}

    assert rows["M1"]["frozen"] is True
    assert rows["M1"]["built"] is True
    assert rows["M2"]["frozen"] is False
    assert rows["M2"]["built"] is False
    assert rows["M2"]["unbound_count"] == 7
    # all 9 H gates' values surfaced per row
    assert set(rows["M1"]["gate_values"]) == set(fx.H_GATES)
    # the failed gate shows up in the hard/soft partition (no separate failed_gates field)
    assert "H_error_within_budget" in (rows["M2"]["hard_failed"] + rows["M2"]["soft_failed"])


def test_dashboard_scope_defaults_to_frozen_prefix_scope(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1", "M2", "M3"))
    for m in ("M1", "M2", "M3"):
        fx.write_build_status(ws, m)
    fx.write_frozen_prefix(ws, frozen=["M1"], scope=["M1", "M2"])  # M3 out of scope
    report, _ = dashboard.run_dashboard(ws)
    assert report["scope_source"] == "frozen_prefix_scope"
    assert {r["milestone_id"] for r in report["rows"]} == {"M1", "M2"}


def test_dashboard_scope_falls_back_to_dockerfiles_dirs(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1", "M2"))
    fx.write_build_status(ws, "M1")
    fx.write_build_status(ws, "M2")
    # a stray non-dir file in dockerfiles/ must be ignored
    (ws / "dockerfiles" / "diagnosis_summary.json").write_text("{}", encoding="utf-8")
    report, _ = dashboard.run_dashboard(ws)
    assert report["scope_source"] == "dockerfiles"
    assert {r["milestone_id"] for r in report["rows"]} == {"M1", "M2"}


def test_dashboard_explicit_scope_wins(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1", "M2"))
    fx.write_build_status(ws, "M1")
    fx.write_build_status(ws, "M2")
    fx.write_frozen_prefix(ws, frozen=[], scope=["M1", "M2"])
    report, _ = dashboard.run_dashboard(ws, scope=["M1"])
    assert report["scope_source"] == "scope_arg"
    assert {r["milestone_id"] for r in report["rows"]} == {"M1"}


def test_dashboard_partitions_hard_vs_soft_gates(tmp_path):
    # New-style data: failed_gates holds only the HARD failure; the SOFT gate is
    # recorded passed=False but does NOT block built.
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fx.write_build_status(
        ws,
        "M1",
        built=False,
        failed_gates=["H_total_not_below_base"],
        gate_overrides={
            "H_total_not_below_base": (False, 0.0),
            "H_f2f_not_above_base": (False, 0.4),
        },
    )
    row = dashboard.run_dashboard(ws, scope=["M1"])[0]["rows"][0]
    assert row["hard_failed"] == ["H_total_not_below_base"]
    assert row["soft_failed"] == ["H_f2f_not_above_base"]


def test_dashboard_soft_only_failure_keeps_hard_clean(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fx.write_build_status(
        ws,
        "M1",
        built=True,
        failed_gates=[],
        gate_overrides={
            "H_s2s_not_above_base": (False, 0.3),
            "H_anomaly_aggregate": (False, 0.5),
        },
    )
    row = dashboard.run_dashboard(ws, scope=["M1"])[0]["rows"][0]
    assert row["hard_failed"] == []
    assert set(row["soft_failed"]) == {"H_s2s_not_above_base", "H_anomaly_aggregate"}


def test_dashboard_repartitions_legacy_flat_failed_gates(tmp_path):
    # OLD data: the flat failed_gates lists ALL failing gates (hard+soft). The
    # dashboard must re-derive the split from each gate's recorded `passed`.
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    flat = ["H_total_not_below_base", "H_f2f_not_above_base", "H_s2s_not_above_base"]
    fx.write_build_status(ws, "M1", built=False, failed_gates=flat, gate_overrides={g: (False, 1.0) for g in flat})
    row = dashboard.run_dashboard(ws, scope=["M1"])[0]["rows"][0]
    assert row["hard_failed"] == ["H_total_not_below_base"]
    assert set(row["soft_failed"]) == {"H_f2f_not_above_base", "H_s2s_not_above_base"}


def test_dashboard_h_error_sub_bar_ratio_shows_soft(tmp_path):
    # H_error is HARD, but a recorded ratio in (0, 0.10] would not block under the
    # 0.10 bar → display it as soft (old data was graded at the stricter 0 line).
    ws = fx.base_workspace(tmp_path, mids=("collapse", "residual"))
    fx.write_build_status(
        ws,
        "collapse",
        built=False,
        failed_gates=["H_error_within_budget"],
        gate_overrides={"H_error_within_budget": (False, 1.0)},
    )
    fx.write_build_status(
        ws,
        "residual",
        built=False,
        failed_gates=["H_error_within_budget"],
        gate_overrides={"H_error_within_budget": (False, 0.02)},
    )
    rows = {r["milestone_id"]: r for r in dashboard.run_dashboard(ws, scope=["collapse", "residual"])[0]["rows"]}
    assert rows["collapse"]["hard_failed"] == ["H_error_within_budget"]
    assert rows["collapse"]["soft_failed"] == []
    assert rows["residual"]["hard_failed"] == []
    assert rows["residual"]["soft_failed"] == ["H_error_within_budget"]


def test_dashboard_folds_recorded_soft_warnings(tmp_path):
    # H_error PASSED the hard bar (ratio 0.05) but the strict-0 soft warning fired —
    # the dashboard surfaces it in soft_failed though it is not a gate failure.
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fx.write_build_status(
        ws,
        "M1",
        built=True,
        failed_gates=[],
        gate_overrides={"H_error_within_budget": (True, 0.05)},
        soft_warnings=[{"gate": "H_error_within_budget", "value": 0.05, "soft_threshold": 0.0}],
    )
    row = dashboard.run_dashboard(ws, scope=["M1"])[0]["rows"][0]
    assert row["hard_failed"] == []
    assert "H_error_within_budget" in row["soft_failed"]


def test_dashboard_build_run_gate_failure_is_hard(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fx.write_build_status(ws, "M1", built=False, failed_gates=["B_image_exists"])
    row = dashboard.run_dashboard(ws, scope=["M1"])[0]["rows"][0]
    assert "B_image_exists" in row["hard_failed"]
    assert "B_image_exists" not in row["soft_failed"]


def test_dashboard_missing_build_status_has_null_hard_soft(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    row = dashboard.run_dashboard(ws, scope=["M1"])[0]["rows"][0]
    assert row["hard_failed"] is None
    assert row["soft_failed"] is None


def test_dashboard_handles_missing_build_status(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    # no build_status written for M1
    report, code = dashboard.run_dashboard(ws, scope=["M1"])
    assert code == ctx.OK
    row = report["rows"][0]
    assert row["build_status_present"] is False
    assert row["built"] is None


# ----------------------------------------------------- absorbed consistency (ex-audit)
def test_dashboard_flags_fresh_consistency_divergence(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fc = fx.write_frozen_check_report(ws, {"M1": {"H_patched_completeness": 0.95}})
    fx.write_build_status(ws, "M1", gate_overrides={"H_patched_completeness": (False, 0.80)})
    # build_status FRESH (newer) vs frozen_check => a fresh divergence is a real problem
    os.utime(fc, (1000, 1000))
    os.utime(ws / "dockerfiles" / "M1" / "build_status.json", (2000, 2000))
    report = dashboard.run_dashboard(ws, scope=["M1"], frozen_check=str(fc))[0]
    row = report["rows"][0]
    assert row["consistency"]["status"] == "divergent"
    assert any(d["gate"] == "H_patched_completeness" for d in row["consistency"]["fields"])
    assert report["summary"]["n_consistency_divergent"] == 1


def test_dashboard_no_consistency_flag_when_build_status_predates_frozen_check(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fc = fx.write_frozen_check_report(ws, {"M1": {"H_patched_completeness": 0.95}})
    fx.write_build_status(ws, "M1", gate_overrides={"H_patched_completeness": (False, 0.80)})
    # build_status OLDER than frozen_check => expected lag, NOT flagged
    os.utime(ws / "dockerfiles" / "M1" / "build_status.json", (1000, 1000))
    os.utime(fc, (2000, 2000))
    row = dashboard.run_dashboard(ws, scope=["M1"], frozen_check=str(fc))[0]["rows"][0]
    assert "consistency" not in row


def test_dashboard_no_consistency_flag_when_values_agree(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fc = fx.write_frozen_check_report(ws, {"M1": {"H_patched_completeness": 0.80}})
    fx.write_build_status(ws, "M1", gate_overrides={"H_patched_completeness": (True, 0.80)})
    row = dashboard.run_dashboard(ws, scope=["M1"], frozen_check=str(fc))[0]["rows"][0]
    assert "consistency" not in row


def test_dashboard_consistency_reads_slim_frozen_check_shape(tmp_path):
    # production (slim) frozen_check shape: per-MS {latest_attempt, real_value:{gate:v}}
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fc = fx.write_frozen_check_report(ws, {"M1": {"H_patched_completeness": 0.95}}, slim=True)
    fx.write_build_status(ws, "M1", gate_overrides={"H_patched_completeness": (False, 0.80)})
    os.utime(fc, (1000, 1000))
    os.utime(ws / "dockerfiles" / "M1" / "build_status.json", (2000, 2000))
    row = dashboard.run_dashboard(ws, scope=["M1"], frozen_check=str(fc))[0]["rows"][0]
    assert row["consistency"]["status"] == "divergent"
    assert any(d["gate"] == "H_patched_completeness" for d in row["consistency"]["fields"])


def test_dashboard_consistency_ignores_non_patched_gate_divergence(tmp_path):
    # H_patched agrees; a non-patched gate (H_f2f) disagrees — must NOT be flagged,
    # because build_status.value and frozen_check.real_value are different computations there.
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fc = fx.write_frozen_check_report(
        ws, {"M1": {"H_patched_completeness": 0.80, "H_f2f_not_above_base": 0.99}}, slim=True
    )
    fx.write_build_status(
        ws,
        "M1",
        gate_overrides={"H_patched_completeness": (True, 0.80), "H_f2f_not_above_base": (False, 0.10)},
    )
    os.utime(fc, (1000, 1000))
    os.utime(ws / "dockerfiles" / "M1" / "build_status.json", (2000, 2000))
    row = dashboard.run_dashboard(ws, scope=["M1"], frozen_check=str(fc))[0]["rows"][0]
    assert "consistency" not in row


def test_dashboard_main_malformed_workspace_returns_input_invalid():
    assert dashboard.main(["--workspace", "/no/such/ws"]) == ctx.INPUT_INVALID


def test_dashboard_main_emits_json(tmp_path, capsys):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fx.write_build_status(ws, "M1")
    code = dashboard.main(["--workspace", str(ws), "--scope", "M1"])
    assert code == ctx.OK
    out = capsys.readouterr().out
    assert json.loads(out)["rows"][0]["milestone_id"] == "M1"
