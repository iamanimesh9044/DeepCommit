"""Tests for the freeze-apply verb — atomic FREEZE write to _frozen_prefix.json.

Precondition: a passing frozen_check (overall_passed && is_dag_closed) whose
candidate_prefix EQUALS --candidate. Invariants (write refused on violation):
candidate DAG-closed, candidate ⊆ scope, resulting frozen set top-down.

0 = applied · 2 = invariant violation (no write) · 1 = input invalid.
"""

import json

from deepcommit.harness.main_orch import context as ctx
from deepcommit.harness.main_orch.verbs import freeze as fa

from . import m3_fixtures as fx

# graph: A -> B -> C  (A is root)
EDGES = [("A", "B"), ("B", "C")]


def _ws(tmp_path, frozen=None, scope=("A", "B", "C")):
    ws = fx.base_workspace(tmp_path, mids=("A", "B", "C"), edges=EDGES)
    if frozen is not None:
        fx.write_frozen_prefix(ws, frozen=frozen, scope=scope, round_n=1)
    return ws


def test_valid_freeze_applies_and_appends_history(tmp_path):
    ws = _ws(tmp_path, frozen=["A"], scope=["A", "B", "C"])
    fc = fx.write_frozen_check_full(ws, candidate=["A", "B"], scope=["A", "B", "C"])
    report, code = fa.run_freeze_apply(
        ws,
        round_n=3,
        candidate=["A", "B"],
        frozen_check=fc,
        decision="FREEZE",
        reasoning="sub-loop converged",
    )
    assert code == ctx.OK
    fp = ctx.read_frozen_prefix(ws)
    assert fp["frozen_milestones"] == ["A", "B"]
    assert fp["frozen_round"] == 3
    assert fp["history"][-1]["decision"] == "FREEZE"
    assert fp["history"][-1]["added_milestones"] == ["B"]  # A was already frozen
    assert report["backup"] is not None  # existing prefix backed up


def test_fresh_workspace_creates_prefix(tmp_path):
    ws = _ws(tmp_path, frozen=None)  # no _frozen_prefix.json
    fc = fx.write_frozen_check_full(ws, candidate=["A"], scope=["A", "B", "C"])
    report, code = fa.run_freeze_apply(
        ws, round_n=1, candidate=["A"], frozen_check=fc, decision="FREEZE", reasoning="first freeze"
    )
    assert code == ctx.OK
    fp = ctx.read_frozen_prefix(ws)
    assert fp["frozen_milestones"] == ["A"]
    assert len(fp["history"]) == 1


def test_non_passing_frozen_check_is_input_invalid(tmp_path):
    ws = _ws(tmp_path, frozen=["A"])
    fc = fx.write_frozen_check_full(ws, candidate=["A", "B"], scope=["A", "B", "C"], passed=False)
    code = fa.freeze_apply_main(
        [
            "--workspace",
            str(ws),
            "--round",
            "3",
            "--candidate",
            "A,B",
            "--frozen-check",
            str(fc),
            "--decision",
            "FREEZE",
            "--reasoning",
            "x",
        ]
    )
    assert code == ctx.INPUT_INVALID
    # write refused
    assert ctx.read_frozen_prefix(ws)["frozen_milestones"] == ["A"]


def test_candidate_mismatch_with_report_is_input_invalid(tmp_path):
    ws = _ws(tmp_path, frozen=["A"])
    fc = fx.write_frozen_check_full(ws, candidate=["A", "B"], scope=["A", "B", "C"])
    code = fa.freeze_apply_main(
        [
            "--workspace",
            str(ws),
            "--round",
            "3",
            "--candidate",
            "A,B,C",
            "--frozen-check",
            str(fc),
            "--decision",
            "FREEZE",
            "--reasoning",
            "x",
        ]
    )
    assert code == ctx.INPUT_INVALID


def test_decision_must_be_freeze(tmp_path):
    ws = _ws(tmp_path, frozen=["A"])
    fc = fx.write_frozen_check_full(ws, candidate=["A", "B"], scope=["A", "B", "C"])
    code = fa.freeze_apply_main(
        [
            "--workspace",
            str(ws),
            "--round",
            "3",
            "--candidate",
            "A,B",
            "--frozen-check",
            str(fc),
            "--decision",
            "WAIT",
            "--reasoning",
            "x",
        ]
    )
    assert code == ctx.INPUT_INVALID


def test_candidate_not_dag_closed_is_violation(tmp_path):
    # candidate [B] needs A; report claims closed but freeze-apply verifies independently
    ws = _ws(tmp_path, frozen=[])
    fc = fx.write_frozen_check_full(ws, candidate=["B"], scope=["A", "B", "C"], closed=True)
    report, code = fa.run_freeze_apply(
        ws, round_n=2, candidate=["B"], frozen_check=fc, decision="FREEZE", reasoning="x"
    )
    assert code == ctx.VIOLATION
    assert report["applied"] is False


def test_candidate_not_in_scope_is_violation(tmp_path):
    ws = _ws(tmp_path, frozen=[])
    fc = fx.write_frozen_check_full(ws, candidate=["A", "B"], scope=["A"])  # B not in scope
    report, code = fa.run_freeze_apply(
        ws, round_n=2, candidate=["A", "B"], frozen_check=fc, decision="FREEZE", reasoning="x"
    )
    assert code == ctx.VIOLATION
    assert any("scope" in str(v) for v in report["violations"])


def test_freeze_apply_pins_recipe_checkpoints(tmp_path):
    ws = _ws(tmp_path, frozen=[])
    fx.write_build_report(ws, run="run_005")  # creates _testbed_runs/run_005
    fc = fx.write_frozen_check_full(
        ws,
        candidate=["A", "B"],
        scope=["A", "B", "C"],
        milestones={"A": {"latest_attempt": "attempt_7"}, "B": {"latest_attempt": "attempt_3"}},
        generated_at="2026-06-03T00:00:00Z",
    )
    report, code = fa.run_freeze_apply(
        ws, round_n=2, candidate=["A", "B"], frozen_check=fc, decision="FREEZE", reasoning="x"
    )
    assert code == ctx.OK
    cp = ctx.read_frozen_prefix(ws)["frozen_checkpoints"]
    # frozen_at is the freeze-apply moment (UTC ISO 'Z'), NOT the frozen_check's generated_at
    assert "T" in cp["frozen_at"] and cp["frozen_at"].endswith("Z")
    assert cp["frozen_at"] != "2026-06-03T00:00:00Z"
    assert cp["testbed_run"] == "run_005"  # global M1 sync checkpoint
    assert cp["milestones"] == {"A": "attempt_7", "B": "attempt_3"}  # per-MID recipe attempt


def test_main_missing_required_args_is_input_invalid(tmp_path):
    ws = _ws(tmp_path, frozen=["A"])
    # missing --frozen-check
    code = fa.freeze_apply_main(
        ["--workspace", str(ws), "--round", "3", "--candidate", "A", "--decision", "FREEZE", "--reasoning", "x"]
    )
    assert code == ctx.INPUT_INVALID


def test_main_emits_json(tmp_path, capsys):
    ws = _ws(tmp_path, frozen=["A"])
    fc = fx.write_frozen_check_full(ws, candidate=["A", "B"], scope=["A", "B", "C"])
    code = fa.freeze_apply_main(
        [
            "--workspace",
            str(ws),
            "--round",
            "3",
            "--candidate",
            "A,B",
            "--frozen-check",
            str(fc),
            "--decision",
            "FREEZE",
            "--reasoning",
            "x",
        ]
    )
    assert code == ctx.OK
    assert json.loads(capsys.readouterr().out)["verb"] == "freeze-apply"
