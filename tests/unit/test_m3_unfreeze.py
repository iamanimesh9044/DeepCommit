"""Tests for the unfreeze verb — remove frozen MIDs (must stay DAG-closed) and
append an UNFREEZE history entry. Freeze membership is reversible by design.

0 = applied · 2 = invariant violation (removal breaks closure) · 1 = input invalid.
"""

import json

from deepcommit.harness.main_orch import context as ctx
from deepcommit.harness.main_orch.verbs import freeze as uf

from . import m3_fixtures as fx

EDGES = [("A", "B"), ("B", "C")]


def _ws(tmp_path, frozen):
    ws = fx.base_workspace(tmp_path, mids=("A", "B", "C"), edges=EDGES)
    fx.write_frozen_prefix(ws, frozen=frozen, scope=["A", "B", "C"], round_n=5)
    return ws


def test_valid_unfreeze_of_leaf_applies(tmp_path):
    ws = _ws(tmp_path, frozen=["A", "B", "C"])
    report, code = uf.run_unfreeze(ws, round_n=7, remove=["C"], reasoning="needs surgery")
    assert code == ctx.OK
    fp = ctx.read_frozen_prefix(ws)
    assert fp["frozen_milestones"] == ["A", "B"]
    assert fp["history"][-1]["decision"] == "UNFREEZE"
    assert fp["history"][-1]["removed_milestones"] == ["C"]
    assert report["backup"] is not None


def test_unfreeze_breaking_closure_is_violation(tmp_path):
    # removing A while B,C (frozen) depend on it breaks top-down closure
    ws = _ws(tmp_path, frozen=["A", "B", "C"])
    report, code = uf.run_unfreeze(ws, round_n=7, remove=["A"], reasoning="x")
    assert code == ctx.VIOLATION
    assert report["applied"] is False
    # write refused
    assert ctx.read_frozen_prefix(ws)["frozen_milestones"] == ["A", "B", "C"]


def test_removing_unfrozen_id_is_input_invalid(tmp_path):
    ws = _ws(tmp_path, frozen=["A"])
    code = uf.unfreeze_main(["--workspace", str(ws), "--round", "7", "--remove", "Z", "--reasoning", "x"])
    assert code == ctx.INPUT_INVALID


def test_main_missing_args_is_input_invalid(tmp_path):
    ws = _ws(tmp_path, frozen=["A", "B", "C"])
    code = uf.unfreeze_main(["--workspace", str(ws), "--round", "7", "--remove", "C"])  # no --reasoning
    assert code == ctx.INPUT_INVALID


def test_main_emits_json(tmp_path, capsys):
    ws = _ws(tmp_path, frozen=["A", "B", "C"])
    code = uf.unfreeze_main(["--workspace", str(ws), "--round", "7", "--remove", "C", "--reasoning", "x"])
    assert code == ctx.OK
    assert json.loads(capsys.readouterr().out)["verb"] == "unfreeze"
