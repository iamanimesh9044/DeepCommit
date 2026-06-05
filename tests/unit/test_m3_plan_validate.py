"""Tests for the plan-validate verb — full plan_schema.md validation of a round
ledger + rejected_suggestions schema.

0 = valid · 2 = schema/seq/append-only/missing-field violation · 1 = input invalid.
"""

import copy
import json

from deepcommit.harness.main_orch import context as ctx
from deepcommit.harness.main_orch.verbs import plan_validate as pv

from . import m3_fixtures as fx


def _base_plan(round_n=3):
    return {
        "round": round_n,
        "started_at": "2026-01-01T00:00:00Z",
        "summary": "test round",
        "events": [
            {
                "event": "dag_update",
                "seq": 1,
                "rationale": "r",
                "final_ops": {"add_edge": [{"prerequisite_id": "A", "dependant_id": "B", "rationale": "x"}]},
                "m1_result": {
                    "final_status": "success",
                    "milestones_success_count": 1,
                    "milestones_conflict_count": 0,
                    "milestones_skipped_count": 0,
                    "run_id": "run_1",
                },
            },
            {
                "event": "env_refine",
                "seq": 2,
                "rationale": "r",
                "milestones": ["M1"],
                "mode": "validate_only",
                "result": {"M1": {"failed_gates": [], "attempt_n": 1}},
            },
            {
                "event": "freeze_check",
                "seq": 3,
                "rationale": "r",
                "candidate_prefix": ["A", "B"],
                "script_gates_passed": "7/7",
                "skill3_risk": {"level": "low", "concerns": []},
                "fork_decision": "FREEZE",
                "decision_summary": "ok",
                "newly_frozen": ["A", "B"],
            },
        ],
    }


def _run(tmp_path, plan, round_n=3, **kw):
    ws = fx.base_workspace(tmp_path)
    fx.write_plan(ws, round_n, plan)
    return pv.run_plan_validate(ws, round_n, **kw)


def test_valid_plan_passes(tmp_path):
    report, code = _run(tmp_path, _base_plan())
    assert code == ctx.OK
    assert report["violations"] == []


def test_missing_top_level_field_is_violation(tmp_path):
    plan = _base_plan()
    del plan["summary"]
    report, code = _run(tmp_path, plan)
    assert code == ctx.VIOLATION
    assert any("summary" in v for v in report["violations"])


def test_seq_gap_is_violation(tmp_path):
    plan = _base_plan()
    plan["events"][1]["seq"] = 3  # 1,3,3 — not contiguous
    report, code = _run(tmp_path, plan)
    assert code == ctx.VIOLATION
    assert any("seq" in v for v in report["violations"])


def test_dag_update_missing_m1_result_is_violation(tmp_path):
    plan = _base_plan()
    del plan["events"][0]["m1_result"]
    report, code = _run(tmp_path, plan)
    assert code == ctx.VIOLATION
    assert any("m1_result" in v for v in report["violations"])


def test_env_refine_missing_result_is_violation(tmp_path):
    plan = _base_plan()
    del plan["events"][1]["result"]
    report, code = _run(tmp_path, plan)
    assert code == ctx.VIOLATION
    assert any("result" in v for v in report["violations"])


def test_unknown_event_type_is_violation(tmp_path):
    plan = _base_plan()
    plan["events"][0]["event"] = "bogus"
    report, code = _run(tmp_path, plan)
    assert code == ctx.VIOLATION
    assert any("bogus" in v for v in report["violations"])


def test_unknown_final_ops_op_type_is_violation(tmp_path):
    plan = _base_plan()
    plan["events"][0]["final_ops"] = {"frobnicate": [{}]}
    report, code = _run(tmp_path, plan)
    assert code == ctx.VIOLATION
    assert any("frobnicate" in v for v in report["violations"])


def test_recorded_failing_verify_dag_is_violation(tmp_path):
    plan = _base_plan()
    plan["events"][0]["verify_dag"] = {"passed": False}
    report, code = _run(tmp_path, plan)
    assert code == ctx.VIOLATION
    assert any("verify-dag" in v.lower() for v in report["violations"])


def test_absent_verify_dag_is_warning_by_default_violation_when_required(tmp_path):
    plan = _base_plan()  # dag_update has no verify_dag record
    report, code = _run(tmp_path, plan)
    assert code == ctx.OK  # default: not required (Phase-2 convention)
    assert any("verify-dag" in w.lower() for w in report["warnings"])

    report2, code2 = _run(tmp_path, copy.deepcopy(plan), require_verify_dag=True)
    assert code2 == ctx.VIOLATION


def test_subagent_log_naming(tmp_path):
    ws = fx.base_workspace(tmp_path)
    fx.write_plan(ws, 3, _base_plan())
    fx.write_subagent_log(ws, 3, "m1_seq01_dag_update.md")  # conforming
    fx.write_subagent_log(ws, 3, "skill1_seqpre1_c7fc9f2_1.md")  # conforming (Step-1 pre form)
    fx.write_subagent_log(ws, 3, "skill1_maint_015.md")  # typed but no seq anchor -> warning
    fx.write_subagent_log(ws, 3, "notes.md")  # foreign file -> violation
    report, code = pv.run_plan_validate(ws, 3)
    assert code == ctx.VIOLATION
    assert any("notes.md" in v for v in report["violations"])
    assert any("skill1_maint_015.md" in w for w in report["warnings"])


def test_rejected_suggestions_schema(tmp_path):
    ws = fx.base_workspace(tmp_path)
    fx.write_plan(ws, 3, _base_plan())
    fx.write_rejected(ws, 3, {"rejected": [{"claim": "c", "reason": "r", "verified_at": "t"}]})  # no decided_by
    report, code = pv.run_plan_validate(ws, 3)
    assert code == ctx.VIOLATION
    assert any("decided_by" in v for v in report["violations"])


def test_valid_rejected_suggestions_passes(tmp_path):
    ws = fx.base_workspace(tmp_path)
    fx.write_plan(ws, 3, _base_plan())
    fx.write_rejected(
        ws, 3, {"rejected": [{"claim": "c", "reason": "r", "verified_at": "t", "decided_by": "m3_main_agent"}]}
    )
    report, code = pv.run_plan_validate(ws, 3)
    assert code == ctx.OK


def _plan_with_revert(restore_from="run_000", with_final_ops=False, with_restore=True):
    """seq1 normal dag_update + seq2 a first-class revert (restore from a checkpoint)."""
    revert = {"reverts_seqs": [1]}
    if with_restore:
        revert["restore_from"] = restore_from
    seq2 = {
        "event": "dag_update",
        "seq": 2,
        "rationale": "revert",
        "revert": revert,
        "m1_result": {"final_status": "success", "run_id": "run_2"},
    }
    if with_final_ops:
        seq2["final_ops"] = {"add_edge": [{"prerequisite_id": "A", "dependant_id": "B", "rationale": "x"}]}
    plan = _base_plan()
    plan["baseline_run"] = "run_000"
    plan["events"] = [plan["events"][0], seq2]
    return plan


def test_revert_event_without_final_ops_passes(tmp_path):
    report, code = _run(tmp_path, _plan_with_revert())
    assert code == ctx.OK, report["violations"]
    assert report["violations"] == []


def test_revert_event_missing_restore_from_is_violation(tmp_path):
    report, code = _run(tmp_path, _plan_with_revert(with_restore=False))
    assert code == ctx.VIOLATION
    assert any("restore_from" in v for v in report["violations"])


def test_revert_event_carrying_final_ops_is_violation(tmp_path):
    report, code = _run(tmp_path, _plan_with_revert(with_final_ops=True))
    assert code == ctx.VIOLATION
    assert any("final_ops" in v for v in report["violations"])


def test_baseline_run_string_is_accepted(tmp_path):
    plan = _base_plan()
    plan["baseline_run"] = "run_016"
    report, code = _run(tmp_path, plan)
    assert code == ctx.OK


def test_baseline_run_non_string_is_violation(tmp_path):
    plan = _base_plan()
    plan["baseline_run"] = ["run_016"]  # must be a scalar run id
    report, code = _run(tmp_path, plan)
    assert code == ctx.VIOLATION
    assert any("baseline_run" in v for v in report["violations"])


def test_missing_plan_is_input_invalid(tmp_path):
    ws = fx.base_workspace(tmp_path)
    assert pv.main(["--workspace", str(ws), "--round", "9"]) == ctx.INPUT_INVALID


def test_main_emits_json(tmp_path, capsys):
    ws = fx.base_workspace(tmp_path)
    fx.write_plan(ws, 3, _base_plan())
    code = pv.main(["--workspace", str(ws), "--round", "3"])
    assert code == ctx.OK
    assert json.loads(capsys.readouterr().out)["verb"] == "plan-validate"
