"""Tests for the preflight verb — pre-operation go/no-go gate.

Validates deliverables (Dockerfiles/build_status/test_results/CSVs/testbed/frozen
pins) for cleanliness + content-hash freshness + cross-file consistency.
Fail-closed: unverifiable evidence → a finding, never silent `safe`.
Verdict: safe | blocked | needs_rebuild. 0 safe · 2 blocked/needs_rebuild · 1 input invalid.
"""

import json

from deepcommit.harness.main_orch import context as ctx
from deepcommit.harness.main_orch.verbs import preflight as pf

from . import m3_fixtures as fx


def _clean_mid(ws, mid, **bs):
    """Recipe + build_status (no input hashes) + classification — passes C2/C3 but C1
    will flag 'freshness unverifiable' (no recorded hashes). Use where a stronger
    finding dominates the verdict."""
    fx.write_recipe(ws, mid)
    fx.write_build_status(ws, mid, **bs)
    fx.write_classification(ws, mid, latest_attempt=bs.get("latest_attempt", "attempt_1"))


def _safe_ws(tmp_path, mids=("M1",)):
    """A workspace that passes every gate (host clean is None → a warning only)."""
    ws = fx.base_workspace(tmp_path, mids=mids)
    for mid in mids:
        fx.write_fresh_recipe_and_status(ws, mid, built=True)
    fx.write_frozen_prefix(ws, frozen=list(mids), scope=list(mids))
    fx.write_m1_run(ws)
    return ws


def test_preflight_clean_workspace_is_safe(tmp_path):
    ws = _safe_ws(tmp_path)
    report, code = pf.run_preflight(ws)
    assert code == ctx.OK
    assert report["verdict"] == "safe"
    assert report["summary"]["n_violations"] == 0 and report["summary"]["n_stale"] == 0


# --------------------------------------------------------------- C2 structural
def test_preflight_missing_dockerfile_is_blocked(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fx.write_build_status(ws, "M1")  # build_status only — no Dockerfile/test_config
    fx.write_frozen_prefix(ws, frozen=["M1"], scope=["M1"])
    report, code = pf.run_preflight(ws)
    assert code == ctx.VIOLATION and report["verdict"] == "blocked"
    assert any(f["check"] == "structural" for f in report["findings"])


def test_preflight_dangling_edge_is_blocked(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",), edges=[("M1", "GHOST")])
    _clean_mid(ws, "M1")
    fx.write_frozen_prefix(ws, frozen=["M1"], scope=["M1"])
    report, _ = pf.run_preflight(ws)
    assert report["verdict"] == "blocked"
    assert any(f["check"] == "structural" and "GHOST" in str(f["detail"]) for f in report["findings"])


def test_preflight_c2_checks_out_of_scope_milestones(tmp_path):
    # M2 is in milestones.csv but NOT in the operating scope and has no dockerfiles/ —
    # C2 must still flag it (workspace well-formedness covers ALL CSV ids).
    ws = fx.base_workspace(tmp_path, mids=("M1", "M2"))
    fx.write_fresh_recipe_and_status(ws, "M1", built=True)  # M2 has nothing
    fx.write_frozen_prefix(ws, frozen=["M1"], scope=["M1"])  # scope excludes M2
    fx.write_m1_run(ws)
    report, _ = pf.run_preflight(ws, scope=["M1"])
    assert report["verdict"] == "blocked"
    assert any(f["milestone_id"] == "M2" and f["check"] == "structural" for f in report["findings"])


# ----------------------------------------------------- C3′ rebuild_crashed
def test_preflight_rebuild_crashed_is_violation_with_restore_source(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fx.write_recipe(ws, "M1")
    fx.write_build_status(
        ws, "M1", built=False, failed_gates=["B_tests_ran_successfully"], tests_ran=False, latest_attempt="attempt_3"
    )
    fx.write_build_run_archive(ws, "M1", 2, built=True)  # last known-good
    fx.write_frozen_prefix(ws, frozen=["M1"], scope=["M1"])
    report, code = pf.run_preflight(ws)
    assert code == ctx.VIOLATION and report["verdict"] == "blocked"
    rc = [f for f in report["findings"] if f["check"] == "rebuild_crashed"]
    assert rc and rc[0]["restore_source"] and "attempt_2" in rc[0]["restore_source"]


# --------------------------------------------------------- C3 pointer integrity
def test_preflight_tests_ran_but_classification_missing_is_blocked(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fx.write_recipe(ws, "M1")
    fx.write_build_status(ws, "M1", built=True, tests_ran=True, latest_attempt="attempt_4")  # no classification written
    fx.write_frozen_prefix(ws, frozen=["M1"], scope=["M1"])
    report, _ = pf.run_preflight(ws)
    assert report["verdict"] == "blocked"
    assert any(f["check"] == "classification_pointer" for f in report["findings"])


def test_preflight_tests_ran_but_no_latest_attempt_is_blocked(tmp_path):
    # B_tests_ran_successfully passed but artifact_paths.latest_attempt is empty → no
    # classification evidence (the C3 case that used to be skipped).
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fx.write_recipe(ws, "M1")
    fx.write_build_status(ws, "M1", built=True, tests_ran=True, latest_attempt="")
    fx.write_frozen_prefix(ws, frozen=["M1"], scope=["M1"])
    report, _ = pf.run_preflight(ws)
    assert report["verdict"] == "blocked"
    assert any(f["check"] == "classification_pointer" for f in report["findings"])


# ------------------------------------------------------ C1 recipe freshness
def test_preflight_stale_recipe_hash_is_needs_rebuild(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fx.write_recipe(ws, "M1", dockerfile="FROM current\n")
    fx.write_build_status(ws, "M1", built=True, inputs={"dockerfile_sha256": "stale-does-not-match"})
    fx.write_classification(ws, "M1")
    fx.write_frozen_prefix(ws, frozen=["M1"], scope=["M1"])
    fx.write_m1_run(ws)
    report, code = pf.run_preflight(ws)
    assert code == ctx.VIOLATION and report["verdict"] == "needs_rebuild"
    assert any(f["check"] == "recipe_freshness" for f in report["findings"])


def test_preflight_absent_input_hashes_is_needs_rebuild(tmp_path):
    # build_status with NO `inputs` → freshness unverifiable → fail-closed (needs_rebuild).
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fx.write_recipe(ws, "M1")
    fx.write_build_status(ws, "M1", built=True)  # no inputs
    fx.write_classification(ws, "M1")
    fx.write_frozen_prefix(ws, frozen=["M1"], scope=["M1"])
    fx.write_m1_run(ws)
    report, _ = pf.run_preflight(ws)
    assert report["verdict"] == "needs_rebuild"
    assert any(f["check"] == "recipe_freshness" and "unverifiable" in str(f["detail"]) for f in report["findings"])


# ------------------------------------------------------ C6 frozen-recipe drift
def test_preflight_frozen_recipe_drift_is_blocked(tmp_path):
    ws = _safe_ws(tmp_path)
    # current M1 Dockerfile differs from the pinned archive
    pinned = ws / "_build_runs" / "M1" / "attempt_5" / "after_agent" / "Dockerfile"
    pinned.parent.mkdir(parents=True, exist_ok=True)
    pinned.write_text("FROM totally-different\n", encoding="utf-8")
    fx.write_frozen_prefix(ws, frozen=["M1"], scope=["M1"], checkpoints={"milestones": {"M1": "attempt_5"}})
    report, _ = pf.run_preflight(ws)
    assert report["verdict"] == "blocked"
    assert any(f["check"] == "frozen_recipe_drift" and f["severity"] == "violation" for f in report["findings"])


def test_preflight_missing_pin_archive_is_needs_rebuild(tmp_path):
    # pin references attempt_99 whose archived Dockerfile does not exist → drift
    # UNVERIFIABLE → fail-closed (needs_rebuild), NOT silent safe.
    ws = _safe_ws(tmp_path)
    fx.write_frozen_prefix(ws, frozen=["M1"], scope=["M1"], checkpoints={"milestones": {"M1": "attempt_99"}})
    report, _ = pf.run_preflight(ws)
    assert report["verdict"] == "needs_rebuild"
    assert any(f["check"] == "frozen_recipe_drift" and "unverifiable" in str(f["detail"]) for f in report["findings"])


# ------------------------------------------------------------- C5 M1 currency
def test_preflight_m1_archive_csv_lag_is_needs_rebuild(tmp_path):
    ws = _safe_ws(tmp_path)
    (ws / "_testbed_runs" / "run_001" / "milestones.csv").write_text("id,commits\nM1,DIFFERENT\n", encoding="utf-8")
    report, _ = pf.run_preflight(ws)
    assert report["verdict"] == "needs_rebuild"
    assert any(f["check"] == "m1_currency" for f in report["findings"])


def test_preflight_no_m1_evidence_is_needs_rebuild(tmp_path):
    # No _testbed_runs at all → M1 currency unverifiable → fail-closed (regression guard:
    # the old precondition returned NEEDS_SANITY_BUILD here, not ADMIT).
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    fx.write_fresh_recipe_and_status(ws, "M1", built=True)
    fx.write_frozen_prefix(ws, frozen=["M1"], scope=["M1"])
    report, code = pf.run_preflight(ws)
    assert code == ctx.VIOLATION and report["verdict"] == "needs_rebuild"
    assert any(f["check"] == "m1_currency" and "unverifiable" in str(f["detail"]) for f in report["findings"])


# ------------------------------------------------------------- C7 waiver schema
def test_preflight_bad_waiver_is_warning_not_violation(tmp_path):
    ws = _safe_ws(tmp_path)
    fx.write_waivers(ws, "M1", bound_waivers=[{"reason_code": "x"}])  # missing anchor/rationale
    report, _ = pf.run_preflight(ws)
    assert any(f["check"] == "waiver_schema" and f["severity"] == "warning" for f in report["findings"])
    assert report["summary"]["n_violations"] == 0  # a bad waiver never blocks


def test_preflight_non_list_waivers_does_not_crash(tmp_path):
    ws = _safe_ws(tmp_path)
    (ws / "dockerfiles" / "M1" / "module2_waivers.json").write_text('{"bound_waivers": 5}', encoding="utf-8")
    report, _ = pf.run_preflight(ws)  # must not raise
    assert any(f["check"] == "waiver_schema" and f["severity"] == "warning" for f in report["findings"])
    assert report["summary"]["n_violations"] == 0


# ----------------------------------------------------------------- C4 host clean
def test_preflight_host_dirty_is_blocked(tmp_path, monkeypatch):
    ws = _safe_ws(tmp_path)
    monkeypatch.setattr(pf, "host_testbed_clean", lambda _ws: False)
    report, _ = pf.run_preflight(ws)
    assert report["verdict"] == "blocked"
    assert any(f["check"] == "host_testbed" and f["severity"] == "violation" for f in report["findings"])


def test_preflight_host_inconclusive_is_warning_not_blocking(tmp_path):
    ws = _safe_ws(tmp_path)  # no testbed/ dir → host_testbed_clean returns None
    report, code = pf.run_preflight(ws)
    assert code == ctx.OK and report["verdict"] == "safe"
    assert any(f["check"] == "host_testbed" and f["severity"] == "warning" for f in report["findings"])


def test_preflight_no_fallback_command_field(tmp_path):
    ws = _safe_ws(tmp_path)
    report, _ = pf.run_preflight(ws)
    assert "fallback_command" not in report  # the remedy is the caller's concern


def test_preflight_main_malformed_workspace_returns_input_invalid():
    assert pf.main(["--workspace", "/no/such/ws"]) == ctx.INPUT_INVALID


def test_preflight_main_emits_json(tmp_path, capsys):
    ws = _safe_ws(tmp_path)
    code = pf.main(["--workspace", str(ws), "--scope", "M1"])
    assert code == ctx.OK
    assert json.loads(capsys.readouterr().out)["verb"] == "preflight"
