"""Tests for the apply_ops module — the canonical final_ops applier.

``apply_ops`` simulates the CSV state a ``dag_update`` event's declared
``final_ops`` (+ ``m1_result.applied_refinements``) would produce when applied
to the event's pre-edit (``--before``) state. ``ops_equivalence`` then compares
that simulated state against the *actual* recorded after-state so verify-dag can
reject a ledger whose declared ops diverge from what the CSVs really became.

Edges are ``(prerequisite_id, dependant_id)`` tuples; commits are a
``milestone_id -> [sha]`` mapping (``;``-joined cell, parsed elsewhere).
"""

from deepcommit.harness.main_orch import apply_ops


# --------------------------------------------------------------------- edge ops
def test_add_edge_adds_the_declared_edge():
    state = apply_ops.apply_ops(
        before_edges=[("A", "B")],
        before_commits={"A": ["a"], "B": ["b"], "C": ["c"]},
        final_ops={"add_edge": [{"prerequisite_id": "B", "dependant_id": "C"}]},
    )
    assert ("B", "C") in state.edges
    assert ("A", "B") in state.edges


def test_drop_edge_removes_the_declared_edge():
    state = apply_ops.apply_ops(
        before_edges=[("A", "B"), ("B", "C")],
        before_commits={"A": ["a"], "B": ["b"], "C": ["c"]},
        final_ops={"drop_edge": [{"prerequisite_id": "A", "dependant_id": "B"}]},
    )
    assert ("A", "B") not in state.edges
    assert ("B", "C") in state.edges


def test_reverse_edge_flips_direction():
    state = apply_ops.apply_ops(
        before_edges=[("A", "B")],
        before_commits={"A": ["a"], "B": ["b"]},
        final_ops={"reverse_edge": [{"prerequisite_id": "A", "dependant_id": "B"}]},
    )
    assert ("A", "B") not in state.edges
    assert ("B", "A") in state.edges


def test_applied_refinements_edges_are_also_applied():
    state = apply_ops.apply_ops(
        before_edges=[("A", "B")],
        before_commits={"A": ["a"], "B": ["b"], "C": ["c"]},
        final_ops={},
        applied_refinements={"add_edge": [{"prerequisite_id": "A", "dependant_id": "C"}]},
    )
    assert ("A", "C") in state.edges


# ------------------------------------------------------------------- commit ops
def test_reassign_commit_moves_to_declared_target():
    state = apply_ops.apply_ops(
        before_edges=[],
        before_commits={"A": ["x", "y"], "B": ["z"]},
        final_ops={"reassign_commit": [{"commit_sha": "y", "from": "A", "to": "B"}]},
    )
    assert state.commits["A"] == ["x"]
    assert "y" in state.commits["B"]


def test_drop_commit_removes_the_commit():
    state = apply_ops.apply_ops(
        before_edges=[],
        before_commits={"A": ["x", "y"]},
        final_ops={"drop_commit": [{"commit_sha": "y", "from": "A"}]},
    )
    assert state.commits["A"] == ["x"]


def test_reassign_commit_matches_a_short_sha_prefix():
    # ops cite short SHAs while milestones.csv stores full ones (prefix-tolerant).
    state = apply_ops.apply_ops(
        before_edges=[],
        before_commits={"A": ["deadbeefcafe"], "B": []},
        final_ops={"reassign_commit": [{"commit_sha": "deadbeef", "from": "A", "to": "B"}]},
    )
    assert state.commits["A"] == []
    assert state.commits["B"] == ["deadbeefcafe"]  # the FULL sha moved, not the prefix


# --------------------------------------------------------------- split / merge / drop
def test_split_milestone_extracted_commits_keeps_id_and_spawns_new():
    state = apply_ops.apply_ops(
        before_edges=[("up", "H")],
        before_commits={"up": ["u"], "H": ["h1", "feat", "h2"]},
        final_ops={
            "split_milestone": [
                {
                    "milestone_id": "H",
                    "extracted_commits": ["feat"],
                    "new_ids": ["H", "seed_feat"],
                }
            ]
        },
    )
    assert state.commits["H"] == ["h1", "h2"]
    assert state.commits["seed_feat"] == ["feat"]
    assert ("up", "H") in state.edges  # host kept its id -> inbound edge unchanged


def test_split_milestone_at_index_splits_sequentially():
    state = apply_ops.apply_ops(
        before_edges=[],
        before_commits={"H": ["c0", "c1", "c2", "c3"]},
        final_ops={"split_milestone": [{"milestone_id": "H", "at_index": 2, "new_ids": ["H", "H_b"]}]},
    )
    assert state.commits["H"] == ["c0", "c1"]
    assert state.commits["H_b"] == ["c2", "c3"]


def test_merge_milestone_combines_commits_and_remaps_edges():
    state = apply_ops.apply_ops(
        before_edges=[("up", "X"), ("Y", "down")],
        before_commits={"up": ["u"], "X": ["x"], "Y": ["y"], "down": ["d"]},
        final_ops={"merge_milestone": [{"merge_ids": ["X", "Y"], "new_id": "XY"}]},
    )
    assert "X" not in state.commits and "Y" not in state.commits
    assert set(state.commits["XY"]) == {"x", "y"}
    assert ("up", "XY") in state.edges
    assert ("XY", "down") in state.edges


def test_drop_milestone_removes_it_and_its_edges():
    state = apply_ops.apply_ops(
        before_edges=[("A", "D"), ("D", "C")],
        before_commits={"A": ["a"], "D": ["d"], "C": ["c"]},
        final_ops={"drop_milestone": [{"milestone_id": "D"}]},
    )
    assert "D" not in state.commits
    assert ("A", "D") not in state.edges
    assert ("D", "C") not in state.edges


# ---------------------------------------------------------------- equivalence cmp
def test_ops_equivalence_clean_when_simulated_matches_actual():
    state = apply_ops.apply_ops(
        before_edges=[("A", "B")],
        before_commits={"A": ["a"], "B": ["b"]},
        final_ops={"add_edge": [{"prerequisite_id": "B", "dependant_id": "A"}]},
    )
    # actual after-state == what the op implies
    result = apply_ops.ops_equivalence(
        state,
        actual_edges=[("A", "B"), ("B", "A")],
        actual_commits={"A": ["a"], "B": ["b"]},
    )
    assert result.ok


def test_ops_equivalence_flags_undeclared_edge_in_actual():
    state = apply_ops.apply_ops(
        before_edges=[("A", "B")],
        before_commits={"A": ["a"], "B": ["b"], "C": ["c"]},
        final_ops={"add_edge": [{"prerequisite_id": "A", "dependant_id": "C"}]},
    )
    # actual has an extra A->B... wait that's declared; actual adds B->C nobody declared
    result = apply_ops.ops_equivalence(
        state,
        actual_edges=[("A", "B"), ("A", "C"), ("B", "C")],
        actual_commits={"A": ["a"], "B": ["b"], "C": ["c"]},
    )
    assert not result.ok
    assert ("B", "C") in result.edges_only_in_actual


def test_ops_equivalence_flags_declared_edge_missing_from_actual():
    state = apply_ops.apply_ops(
        before_edges=[("A", "B")],
        before_commits={"A": ["a"], "B": ["b"], "C": ["c"]},
        final_ops={"add_edge": [{"prerequisite_id": "A", "dependant_id": "C"}]},
    )
    # the declared A->C never made it into the actual CSV
    result = apply_ops.ops_equivalence(
        state,
        actual_edges=[("A", "B")],
        actual_commits={"A": ["a"], "B": ["b"], "C": ["c"]},
    )
    assert not result.ok
    assert ("A", "C") in result.edges_only_in_declared


def test_ops_equivalence_flags_reassign_to_wrong_target():
    state = apply_ops.apply_ops(
        before_edges=[],
        before_commits={"A": ["x", "y"], "B": [], "C": []},
        final_ops={"reassign_commit": [{"commit_sha": "y", "from": "A", "to": "B"}]},
    )
    # actual moved y to C instead of the declared B
    result = apply_ops.ops_equivalence(
        state,
        actual_edges=[],
        actual_commits={"A": ["x"], "B": [], "C": ["y"]},
    )
    assert not result.ok
    assert "B" in result.ownership_mismatches or "C" in result.ownership_mismatches


def test_ops_equivalence_flags_wrong_split_new_id():
    state = apply_ops.apply_ops(
        before_edges=[],
        before_commits={"H": ["h1", "feat"]},
        final_ops={
            "split_milestone": [{"milestone_id": "H", "extracted_commits": ["feat"], "new_ids": ["H", "seed_feat"]}]
        },
    )
    # actual created seed_WRONG instead of the declared seed_feat
    result = apply_ops.ops_equivalence(
        state,
        actual_edges=[],
        actual_commits={"H": ["h1"], "seed_WRONG": ["feat"]},
    )
    assert not result.ok
    assert "seed_feat" in result.milestones_only_in_declared
    assert "seed_WRONG" in result.milestones_only_in_actual


# ----------------------------------------------------------- apply_plan (cumulative)
# Fold a whole round's dag_update events from one baseline (round-start = a canonical
# run archive). A revert event resets the running state to its declared restore point
# (also a canonical archive, resolved via restore_loader), so the final state depends
# only on the LAST revert's restore point + the events after it.
def _dag(seq, **kw):
    return {"event": "dag_update", "seq": seq, **kw}


def test_apply_plan_folds_consecutive_events_from_baseline():
    base_edges = [("A", "B")]
    base_commits = {"A": ["a"], "B": ["b"], "C": ["c"]}
    events = [
        _dag(1, final_ops={"add_edge": [{"prerequisite_id": "B", "dependant_id": "C"}]}),
        _dag(2, final_ops={"add_edge": [{"prerequisite_id": "A", "dependant_id": "C"}]}),
    ]
    state = apply_ops.apply_plan(base_edges, base_commits, events, restore_loader=lambda ref: ([], {}))
    assert ("A", "B") in state.edges and ("B", "C") in state.edges and ("A", "C") in state.edges


def test_apply_plan_applies_event_refinements_too():
    events = [
        _dag(
            1,
            final_ops={"add_edge": [{"prerequisite_id": "A", "dependant_id": "B"}]},
            m1_result={"applied_refinements": {"add_edge": [{"prerequisite_id": "A", "dependant_id": "C"}]}},
        )
    ]
    state = apply_ops.apply_plan([], {"A": ["a"], "B": ["b"], "C": ["c"]}, events, restore_loader=lambda r: ([], {}))
    assert ("A", "B") in state.edges and ("A", "C") in state.edges  # final_ops + applied_refinements


def test_apply_plan_skips_non_dag_update_events():
    events = [
        {"event": "env_refine", "seq": 1, "mode": "surgical"},
        {"event": "freeze_check", "seq": 2},
        _dag(3, final_ops={"add_edge": [{"prerequisite_id": "A", "dependant_id": "B"}]}),
    ]
    state = apply_ops.apply_plan([], {"A": ["a"], "B": ["b"]}, events, restore_loader=lambda r: ([], {}))
    assert state.edges == {("A", "B")}


def test_apply_plan_revert_resets_state_to_restore_point():
    base_edges = [("A", "B")]
    base_commits = {"A": ["a"], "B": ["b"], "C": ["c"]}
    restore = {"run_000": ([("A", "B")], {"A": ["a"], "B": ["b"], "C": ["c"]})}
    events = [
        _dag(1, final_ops={"add_edge": [{"prerequisite_id": "B", "dependant_id": "C"}]}),
        _dag(2, revert={"restore_from": "run_000", "reverts_seqs": [1]}),
    ]
    state = apply_ops.apply_plan(base_edges, base_commits, events, restore_loader=lambda r: restore[r])
    assert ("B", "C") not in state.edges  # seq1's edge undone by the revert
    assert state.edges == {("A", "B")}


def test_apply_plan_last_revert_plus_following_events_decide_final():
    restore = {"run_x": ([("A", "B")], {"A": ["a"], "B": ["b"]})}
    events = [
        _dag(1, final_ops={"add_edge": [{"prerequisite_id": "A", "dependant_id": "B"}]}),
        _dag(2, revert={"restore_from": "run_x"}),  # reset to {A->B}
        _dag(3, final_ops={"drop_edge": [{"prerequisite_id": "A", "dependant_id": "B"}]}),  # then drop it
    ]
    state = apply_ops.apply_plan([], {"A": ["a"], "B": ["b"]}, events, restore_loader=lambda r: restore[r])
    assert state.edges == set()  # restore gave A->B, seq3 dropped it


def test_apply_plan_revert_with_no_following_events_equals_restore_state():
    restore = {"run_y": ([("X", "Y")], {"X": ["x"], "Y": ["y"]})}
    events = [
        _dag(1, final_ops={"add_edge": [{"prerequisite_id": "P", "dependant_id": "Q"}]}),
        _dag(2, revert={"restore_from": "run_y"}),
    ]
    state = apply_ops.apply_plan([], {"P": [], "Q": []}, events, restore_loader=lambda r: restore[r])
    assert state.edges == {("X", "Y")}
    assert state.commits == {"X": ["x"], "Y": ["y"]}


def test_apply_plan_through_seq_stops_early():
    events = [
        _dag(1, final_ops={"add_edge": [{"prerequisite_id": "A", "dependant_id": "B"}]}),
        _dag(2, final_ops={"add_edge": [{"prerequisite_id": "B", "dependant_id": "C"}]}),
    ]
    state = apply_ops.apply_plan(
        [], {"A": ["a"], "B": ["b"], "C": ["c"]}, events, restore_loader=lambda r: ([], {}), through_seq=1
    )
    assert ("A", "B") in state.edges and ("B", "C") not in state.edges
