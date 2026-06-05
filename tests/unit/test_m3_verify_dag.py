"""Tests for the verify-dag verb.

Per-dag_update-event verification of the resulting CSV state: full-graph
acyclicity, candidate DAG-closure, edge reachability, and commit conservation
against the event's pre-edit ``--before`` snapshot (declared drops via
``--plan``/``--declared-drops``; ``drop_milestone`` commits expanded from
``--before``). Redundant edges are warnings, not violations.
"""

import json

from deepcommit.harness.main_orch import context as ctx
from deepcommit.harness.main_orch.verbs import verify_dag


def _ws(tmp_path, milestones: str, deps: str):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "milestones.csv").write_text(milestones, encoding="utf-8")
    (ws / "dependencies.csv").write_text(deps, encoding="utf-8")
    return ws


def _before(tmp_path, milestones: str):
    d = tmp_path / "before"
    d.mkdir()
    (d / "milestones.csv").write_text(milestones, encoding="utf-8")
    return d


M_BASE = "id,commits\nM1,a;b\nM2,c\n"
DEPS_LINE = "prerequisite_id,dependant_id\nM1,M2\n"


# ----------------------------------------------------------------------- clean
def test_clean_graph_and_conserved_commits_pass(tmp_path):
    ws = _ws(tmp_path, M_BASE, DEPS_LINE)
    before = _before(tmp_path, M_BASE)
    report, code = verify_dag.run_verify_dag(ws, before=before)
    assert code == ctx.OK
    assert report["cycles"] == []
    assert report["unreachable_violations"] == []
    assert report["commit_diff"]["undeclared_lost"] == []
    assert report["commit_diff"]["dup"] == []
    assert report["passed"] is True


# ----------------------------------------------------------------------- cycle
def test_cycle_is_hard_violation(tmp_path):
    ws = _ws(tmp_path, M_BASE, "prerequisite_id,dependant_id\nM1,M2\nM2,M1\n")
    report, code = verify_dag.run_verify_dag(ws)
    assert code == ctx.VIOLATION
    assert report["cycles"]  # non-empty cycle path
    assert report["unreachable_violations"]  # both edges close a cycle


# --------------------------------------------------------------------- closure
def test_candidate_not_dag_closed_is_hard_violation(tmp_path):
    ws = _ws(tmp_path, M_BASE, DEPS_LINE)
    report, code = verify_dag.run_verify_dag(ws, candidate=["M2"])
    assert code == ctx.VIOLATION
    assert report["unclosed"]["M2"] == ["M1"]


def test_candidate_dag_closed_passes(tmp_path):
    ws = _ws(tmp_path, M_BASE, DEPS_LINE)
    report, code = verify_dag.run_verify_dag(ws, candidate=["M1", "M2"])
    assert code == ctx.OK
    assert report["unclosed"] == {}


# ------------------------------------------------------------ commit conservation
def test_undeclared_commit_loss_is_hard_violation(tmp_path):
    ws = _ws(tmp_path, "id,commits\nM1,a\nM2,c\n", DEPS_LINE)  # 'b' vanished from M1
    before = _before(tmp_path, M_BASE)
    report, code = verify_dag.run_verify_dag(ws, before=before)
    assert code == ctx.VIOLATION
    assert report["commit_diff"]["undeclared_lost"] == ["b"]


def test_declared_drop_via_flag_is_allowed(tmp_path):
    ws = _ws(tmp_path, "id,commits\nM1,a\nM2,c\n", DEPS_LINE)
    before = _before(tmp_path, M_BASE)
    report, code = verify_dag.run_verify_dag(ws, before=before, declared_drops=["b"])
    assert code == ctx.OK
    assert report["commit_diff"]["undeclared_lost"] == []


def test_declared_drop_commit_via_plan_is_allowed(tmp_path):
    ws = _ws(tmp_path, "id,commits\nM1,a\nM2,c\n", DEPS_LINE)
    before = _before(tmp_path, M_BASE)
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "round: 1\nstarted_at: t\nsummary: s\nevents:\n"
        "  - event: dag_update\n    seq: 1\n    rationale: r\n"
        "    final_ops:\n      drop_commit:\n        - {commit_sha: b, from: M1, rationale: x}\n"
        "    m1_result: {final_status: success}\n",
        encoding="utf-8",
    )
    report, code = verify_dag.run_verify_dag(ws, before=before, plan=plan)
    assert code == ctx.OK
    assert "b" in report["commit_diff"]["declared_drops"]


def test_drop_milestone_expands_commits_from_before(tmp_path):
    # M2 dropped entirely; its commit 'c' must be expanded from --before as declared.
    ws = _ws(tmp_path, "id,commits\nM1,a;b\n", "prerequisite_id,dependant_id\n")
    before = _before(tmp_path, M_BASE)
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "events:\n  - event: dag_update\n    seq: 1\n    rationale: r\n"
        "    final_ops:\n      drop_milestone:\n        - {milestone_id: M2, cascaded_edges: [], rationale: x}\n"
        "    m1_result: {final_status: success}\n",
        encoding="utf-8",
    )
    report, code = verify_dag.run_verify_dag(ws, before=before, plan=plan)
    assert code == ctx.OK
    assert report["commit_diff"]["undeclared_lost"] == []
    assert "c" in report["commit_diff"]["declared_drops"]


# ------------------------------------------------------------------- duplication
def test_commit_duplication_is_hard_violation(tmp_path):
    ws = _ws(tmp_path, "id,commits\nM1,a;b\nM2,b\n", DEPS_LINE)  # 'b' owned twice
    before = _before(tmp_path, M_BASE)
    report, code = verify_dag.run_verify_dag(ws, before=before)
    assert code == ctx.VIOLATION
    assert report["commit_diff"]["dup"] == ["b"]


# --------------------------------------------------------------- redundant (soft)
def test_redundant_edge_is_warning_not_violation(tmp_path):
    ws = _ws(
        tmp_path,
        "id,commits\nM1,a\nM2,b\nM3,c\n",
        "prerequisite_id,dependant_id\nM1,M2\nM2,M3\nM1,M3\n",
    )
    report, code = verify_dag.run_verify_dag(ws)
    assert code == ctx.OK
    assert ["M1", "M3"] in report["warnings"]["redundant_edges"]
    assert report["passed"] is True


# --------------------------------------------------------------------- CLI / exit
def test_main_missing_workspace_returns_input_invalid(capsys):
    code = verify_dag.main(["--workspace", "/no/such/ws"])
    assert code == ctx.INPUT_INVALID


def test_main_writes_report_and_exit_code(tmp_path):
    ws = _ws(tmp_path, M_BASE, "prerequisite_id,dependant_id\nM1,M2\nM2,M1\n")
    out = tmp_path / "report.json"
    code = verify_dag.main(["--workspace", str(ws), "--output", str(out)])
    assert code == ctx.VIOLATION
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["cycles"]


# ------------------------------------------------------ strict-ops equivalence
# Enforces that the actual before->after CSV diff equals the plan's declared
# final_ops (+ applied_refinements): catches wrong/missing edges, reassign to the
# wrong milestone, and split/merge that produced the wrong ids — the defects the
# structural + conservation checks let through.
M_ABC = "id,commits\nA,a\nB,b\nC,c\n"
DEPS_AB = "prerequisite_id,dependant_id\nA,B\n"
ADD_BC_PLAN = (
    "events:\n  - event: dag_update\n    seq: 1\n    rationale: r\n"
    "    final_ops:\n      add_edge:\n        - {prerequisite_id: B, dependant_id: C, rationale: x}\n"
    "    m1_result: {final_status: success}\n"
)


def _before_full(tmp_path, milestones: str, deps: str):
    d = tmp_path / "before_full"
    d.mkdir()
    (d / "milestones.csv").write_text(milestones, encoding="utf-8")
    (d / "dependencies.csv").write_text(deps, encoding="utf-8")
    return d


def _plan(tmp_path, body: str):
    p = tmp_path / "plan.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_strict_ops_passes_when_actual_matches_declared(tmp_path):
    ws = _ws(tmp_path, M_ABC, "prerequisite_id,dependant_id\nA,B\nB,C\n")  # B->C added
    before = _before_full(tmp_path, M_ABC, DEPS_AB)
    plan = _plan(tmp_path, ADD_BC_PLAN)
    report, code = verify_dag.run_verify_dag(ws, before=before, plan=plan, seq=1, strict_ops=True)
    assert code == ctx.OK
    assert report["ops_equivalence"]["passed"] is True


def test_strict_ops_flags_declared_edge_not_applied(tmp_path):
    ws = _ws(tmp_path, M_ABC, DEPS_AB)  # plan said add B->C but actual still only has A->B
    before = _before_full(tmp_path, M_ABC, DEPS_AB)
    plan = _plan(tmp_path, ADD_BC_PLAN)
    report, code = verify_dag.run_verify_dag(ws, before=before, plan=plan, seq=1, strict_ops=True)
    assert code == ctx.VIOLATION
    assert "ops_equivalence" in report["violations"]
    assert ["B", "C"] in report["ops_equivalence"]["edges_only_in_declared"]


def test_strict_ops_flags_undeclared_edge_in_actual(tmp_path):
    ws = _ws(tmp_path, M_ABC, "prerequisite_id,dependant_id\nA,B\nA,C\n")  # A->C nobody declared
    before = _before_full(tmp_path, M_ABC, DEPS_AB)
    plan = _plan(
        tmp_path,
        "events:\n  - event: dag_update\n    seq: 1\n    rationale: r\n"
        "    final_ops: {}\n    m1_result: {final_status: success}\n",
    )
    report, code = verify_dag.run_verify_dag(ws, before=before, plan=plan, seq=1, strict_ops=True)
    assert code == ctx.VIOLATION
    assert ["A", "C"] in report["ops_equivalence"]["edges_only_in_actual"]


def test_strict_ops_flags_reassign_to_wrong_target(tmp_path):
    # plan: reassign y A->B ; actual moved y to C instead
    ws = _ws(tmp_path, "id,commits\nA,x\nB,\nC,y\n", "prerequisite_id,dependant_id\n")
    before = _before_full(tmp_path, "id,commits\nA,x;y\nB,\nC,\n", "prerequisite_id,dependant_id\n")
    plan = _plan(
        tmp_path,
        "events:\n  - event: dag_update\n    seq: 1\n    rationale: r\n"
        "    final_ops:\n      reassign_commit:\n        - {commit_sha: y, from: A, to: B, rationale: x}\n"
        "    m1_result: {final_status: success}\n",
    )
    report, code = verify_dag.run_verify_dag(ws, before=before, plan=plan, seq=1, strict_ops=True)
    assert code == ctx.VIOLATION
    assert "ops_equivalence" in report["violations"]
    assert report["ops_equivalence"]["ownership_mismatches"]


def test_strict_ops_flags_wrong_split_new_id(tmp_path):
    # plan: split H -> [H, seed_feat] extracting feat ; actual created seed_WRONG
    ws = _ws(tmp_path, "id,commits\nH,h1\nseed_WRONG,feat\n", "prerequisite_id,dependant_id\n")
    before = _before_full(tmp_path, "id,commits\nH,h1;feat\n", "prerequisite_id,dependant_id\n")
    plan = _plan(
        tmp_path,
        "events:\n  - event: dag_update\n    seq: 1\n    rationale: r\n"
        "    final_ops:\n      split_milestone:\n"
        "        - {milestone_id: H, extracted_commits: [feat], new_ids: [H, seed_feat], rationale: x}\n"
        "    m1_result: {final_status: success}\n",
    )
    report, code = verify_dag.run_verify_dag(ws, before=before, plan=plan, seq=1, strict_ops=True)
    assert code == ctx.VIOLATION
    assert "seed_feat" in report["ops_equivalence"]["milestones_only_in_declared"]
    assert "seed_WRONG" in report["ops_equivalence"]["milestones_only_in_actual"]


def test_strict_ops_requires_before_plan_seq(tmp_path):
    ws = _ws(tmp_path, M_ABC, DEPS_AB)
    code = verify_dag.main(["--workspace", str(ws), "--before", str(ws), "--strict-ops"])  # no --plan/--seq
    assert code == ctx.INPUT_INVALID


def test_without_strict_ops_equivalence_is_not_checked(tmp_path):
    ws = _ws(tmp_path, M_ABC, "prerequisite_id,dependant_id\nA,B\nA,C\n")  # undeclared A->C
    before = _before_full(tmp_path, M_ABC, DEPS_AB)
    plan = _plan(
        tmp_path,
        "events:\n  - event: dag_update\n    seq: 1\n    rationale: r\n"
        "    final_ops: {}\n    m1_result: {final_status: success}\n",
    )
    report, code = verify_dag.run_verify_dag(ws, before=before, plan=plan, seq=1)  # strict_ops defaults off
    assert code == ctx.OK
    assert "ops_equivalence" not in report


# -------------------------------------------------------- cumulative (whole round)
# Fold ALL dag_update events from one round-start baseline (a canonical run archive
# referenced by `baseline_run`); a revert resets to its `restore_from` checkpoint.
# No per-event snapshot needed; the after-state is just the current workspace CSVs.
def _ws_with_runs(tmp_path, milestones, deps, runs):
    ws = tmp_path / "ws_cum"
    ws.mkdir()
    (ws / "milestones.csv").write_text(milestones, encoding="utf-8")
    (ws / "dependencies.csv").write_text(deps, encoding="utf-8")
    for run_id, (ms, dp) in runs.items():
        d = ws / "_testbed_runs" / run_id
        d.mkdir(parents=True)
        (d / "milestones.csv").write_text(ms, encoding="utf-8")
        (d / "dependencies.csv").write_text(dp, encoding="utf-8")
    return ws


def test_cumulative_clean_when_current_matches_folded_plan(tmp_path):
    ws = _ws_with_runs(tmp_path, M_ABC, "prerequisite_id,dependant_id\nA,B\nB,C\n", {"run_000": (M_ABC, DEPS_AB)})
    plan = _plan(
        tmp_path,
        "baseline_run: run_000\nevents:\n  - event: dag_update\n    seq: 1\n    rationale: r\n"
        "    final_ops:\n      add_edge:\n        - {prerequisite_id: B, dependant_id: C, rationale: x}\n"
        "    m1_result: {final_status: success}\n",
    )
    report, code = verify_dag.run_verify_dag(ws, plan=plan, cumulative=True)
    assert code == ctx.OK
    assert report["ops_equivalence"]["passed"] is True


def test_cumulative_flags_undeclared_edge(tmp_path):
    ws = _ws_with_runs(tmp_path, M_ABC, "prerequisite_id,dependant_id\nA,B\nA,C\n", {"run_000": (M_ABC, DEPS_AB)})
    plan = _plan(
        tmp_path,
        "baseline_run: run_000\nevents:\n  - event: dag_update\n    seq: 1\n    rationale: r\n"
        "    final_ops: {}\n    m1_result: {final_status: success}\n",
    )
    report, code = verify_dag.run_verify_dag(ws, plan=plan, cumulative=True)
    assert code == ctx.VIOLATION
    assert "ops_equivalence" in report["violations"]
    assert ["A", "C"] in report["ops_equivalence"]["edges_only_in_actual"]


def test_cumulative_revert_resets_to_checkpoint(tmp_path):
    # seq1 adds B->C, seq2 reverts to run_000 (baseline) -> current must equal baseline
    ws = _ws_with_runs(tmp_path, M_ABC, DEPS_AB, {"run_000": (M_ABC, DEPS_AB)})
    plan = _plan(
        tmp_path,
        "baseline_run: run_000\nevents:\n"
        "  - event: dag_update\n    seq: 1\n    rationale: r\n"
        "    final_ops:\n      add_edge:\n        - {prerequisite_id: B, dependant_id: C, rationale: x}\n"
        "    m1_result: {final_status: success}\n"
        "  - event: dag_update\n    seq: 2\n    rationale: revert\n"
        "    revert: {restore_from: run_000, reverts_seqs: [1]}\n"
        "    m1_result: {final_status: success}\n",
    )
    report, code = verify_dag.run_verify_dag(ws, plan=plan, cumulative=True)
    assert code == ctx.OK
    assert report["ops_equivalence"]["passed"] is True


def test_cumulative_through_seq_verifies_partial(tmp_path):
    # current == state after ONLY seq1 (B->C); verifying through seq1 should pass
    ws = _ws_with_runs(tmp_path, M_ABC, "prerequisite_id,dependant_id\nA,B\nB,C\n", {"run_000": (M_ABC, DEPS_AB)})
    plan = _plan(
        tmp_path,
        "baseline_run: run_000\nevents:\n"
        "  - event: dag_update\n    seq: 1\n    rationale: r\n"
        "    final_ops:\n      add_edge:\n        - {prerequisite_id: B, dependant_id: C, rationale: x}\n"
        "    m1_result: {final_status: success}\n"
        "  - event: dag_update\n    seq: 2\n    rationale: r\n"
        "    final_ops:\n      add_edge:\n        - {prerequisite_id: A, dependant_id: C, rationale: x}\n"
        "    m1_result: {final_status: success}\n",
    )
    report, code = verify_dag.run_verify_dag(ws, plan=plan, cumulative=True, through_seq=1)
    assert code == ctx.OK


def test_cumulative_requires_baseline_run(tmp_path):
    ws = _ws_with_runs(tmp_path, M_ABC, DEPS_AB, {"run_000": (M_ABC, DEPS_AB)})
    plan = _plan(  # no baseline_run
        tmp_path,
        "events:\n  - event: dag_update\n    seq: 1\n    rationale: r\n    final_ops: {}\n    m1_result: {final_status: success}\n",
    )
    code = verify_dag.main(["--workspace", str(ws), "--plan", str(plan), "--cumulative"])
    assert code == ctx.INPUT_INVALID


def test_cumulative_fails_closed_when_checkpoint_archive_incomplete(tmp_path):
    # baseline run_000 archive is missing dependencies.csv -> cannot verify -> input invalid
    ws = tmp_path / "ws_fc"
    ws.mkdir()
    (ws / "milestones.csv").write_text(M_ABC, encoding="utf-8")
    (ws / "dependencies.csv").write_text(DEPS_AB, encoding="utf-8")
    d = ws / "_testbed_runs" / "run_000"
    d.mkdir(parents=True)
    (d / "milestones.csv").write_text(M_ABC, encoding="utf-8")  # NO dependencies.csv
    plan = _plan(
        tmp_path,
        "baseline_run: run_000\nevents:\n  - event: dag_update\n    seq: 1\n    rationale: r\n    final_ops: {}\n    m1_result: {final_status: success}\n",
    )
    code = verify_dag.main(["--workspace", str(ws), "--plan", str(plan), "--cumulative"])
    assert code == ctx.INPUT_INVALID
