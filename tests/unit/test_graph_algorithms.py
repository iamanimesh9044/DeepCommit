"""Characterization / unit tests for ``deepcommit.tools.utils.graph_algorithms``.

Covers ``compute_transitive_closure`` and ``transitive_reduction`` (including the
``return_removed_edges`` tuple form, idempotency, and witness intermediates).

Determinism notes:
- All expected values were ground-truthed by executing the functions, not traced
  by hand.
- ``dict``/``set`` equality is order-insensitive, so equality assertions are
  hash-seed safe. Where iteration order *would* leak (the witness intermediate
  chosen for a removed edge), we compare canonical ``set``s of ``(source, target)``
  pairs rather than the raw list.
- ``transitive_reduction`` prints progress/stats to stdout; every call is wrapped
  in ``contextlib.redirect_stdout`` so the test output stays clean.
"""

import contextlib
import io

import pytest

from deepcommit.tools.utils.graph_algorithms import (
    compute_transitive_closure,
    get_graph_statistics,
    transitive_reduction,
)


def _reduce(g, **kw):
    """Call transitive_reduction with its chatty stdout suppressed."""
    with contextlib.redirect_stdout(io.StringIO()):
        return transitive_reduction(g, **kw)


# ---------------------------------------------------------------------------
# compute_transitive_closure
# ---------------------------------------------------------------------------


def test_closure_chain_excludes_sinks():
    # A->B->C: C is a sink (no out-edges) so it is NOT a key; only non-empty
    # reachable sets are stored.
    assert compute_transitive_closure({"A": {"B"}, "B": {"C"}}, {"A", "B", "C"}) == {
        "A": {"B", "C"},
        "B": {"C"},
    }


def test_closure_diamond_infers_nodes_when_all_nodes_omitted():
    # all_nodes omitted -> inferred from keys + every target. Diamond A->B,A->C,
    # B->D,C->D collapses A's reach to {B,C,D}.
    assert compute_transitive_closure({"A": {"B", "C"}, "B": {"D"}, "C": {"D"}}) == {
        "A": {"B", "C", "D"},
        "B": {"D"},
        "C": {"D"},
    }


def test_closure_self_loop_terminates():
    # A->A: BFS must not spin forever; A reaches itself.
    assert compute_transitive_closure({"A": {"A"}}) == {"A": {"A"}}


def test_closure_two_cycle_each_reaches_both():
    # A<->B: every node reaches the whole strongly-connected component, itself
    # included.
    assert compute_transitive_closure({"A": {"B"}, "B": {"A"}}) == {
        "A": {"A", "B"},
        "B": {"A", "B"},
    }


def test_closure_empty_graph():
    assert compute_transitive_closure({}) == {}


def test_closure_isolated_node_is_not_a_key():
    # C is in all_nodes but has no out-edges -> empty reachable set -> dropped.
    # Adversarial: proves the function does not emit C:set() just because C was
    # named in all_nodes.
    assert compute_transitive_closure({"A": {"B"}}, {"A", "B", "C"}) == {"A": {"B"}}


def test_closure_empty_all_nodes_short_circuits():
    # all_nodes=set() is falsy but NOT None, so the inference branch is skipped
    # and zero start nodes are walked -> {}. Near-miss for the `is None` guard.
    assert compute_transitive_closure({"A": {"B"}}, set()) == {}


def test_closure_node_with_empty_dep_set_is_dropped():
    # An explicit A:set() entry has no reachable nodes -> not a key.
    assert compute_transitive_closure({"A": set(), "B": {"C"}}) == {"B": {"C"}}


def test_closure_disconnected_components_stay_separate():
    # Two independent chains never bleed into each other.
    assert compute_transitive_closure({"A": {"B"}, "B": {"C"}, "X": {"Y"}, "Y": {"Z"}}) == {
        "A": {"B", "C"},
        "B": {"C"},
        "X": {"Y", "Z"},
        "Y": {"Z"},
    }


def test_closure_no_implied_edge_when_unreachable():
    # Adversarial near-miss: A->B and A->C where B does NOT reach C. The closure
    # must be exactly {B, C} for A -- no fabricated B->C link.
    assert compute_transitive_closure({"A": {"B", "C"}}) == {"A": {"B", "C"}}


def test_closure_returns_plain_dict_of_sets():
    result = compute_transitive_closure({"A": {"B"}, "B": {"C"}})
    assert type(result) is dict
    assert all(type(v) is set for v in result.values())


def test_closure_default_all_nodes_is_not_shared_across_calls():
    # all_nodes defaults to None and is rebuilt each call; prove no mutable
    # default leaks state from one invocation to the next.
    first = compute_transitive_closure({"A": {"B"}})
    second = compute_transitive_closure({"X": {"Y"}})
    assert first == {"A": {"B"}}
    assert second == {"X": {"Y"}}


# ---------------------------------------------------------------------------
# transitive_reduction
# ---------------------------------------------------------------------------


def test_reduction_drops_implied_edge():
    # A->C is implied by A->B->C, so it is removed.
    assert _reduce({"A": {"B", "C"}, "B": {"C"}}) == {"A": {"B"}, "B": {"C"}}


def test_reduction_reports_removed_edge_with_witness():
    reduced, removed = _reduce({"A": {"B", "C"}, "B": {"C"}}, return_removed_edges=True)
    assert reduced == {"A": {"B"}, "B": {"C"}}
    # Removed edge is (source, intermediate, target): A->C via B.
    assert removed == [("A", "B", "C")]


def test_reduction_keeps_non_redundant_parallel_edges():
    # Adversarial near-miss: A->B and A->C with NO path B~>C, so nothing is
    # redundant; both edges survive.
    assert _reduce({"A": {"B", "C"}}) == {"A": {"B", "C"}}


def test_reduction_self_loop_preserved():
    # A->A: the only intermediate is the target itself (skipped), so the edge is
    # never deemed redundant.
    assert _reduce({"A": {"A"}}) == {"A": {"A"}}


def test_reduction_two_cycle_preserved():
    # A<->B: each node's sole out-edge IS the target, so neither is redundant.
    assert _reduce({"A": {"B"}, "B": {"A"}}) == {"A": {"B"}, "B": {"A"}}


def test_reduction_empty_graph():
    assert _reduce({}) == {}


def test_reduction_node_with_empty_dep_set_pruned():
    # Empty out-set never produces edges; the final cleanup drops empty keys.
    assert _reduce({"A": set(), "B": {"C"}}) == {"B": {"C"}}


def test_reduction_disconnected_components():
    assert _reduce({"A": {"B", "C"}, "B": {"C"}, "X": {"Y", "Z"}, "Y": {"Z"}}) == {
        "A": {"B"},
        "B": {"C"},
        "X": {"Y"},
        "Y": {"Z"},
    }


def test_reduction_all_nodes_arg_does_not_change_result():
    # transitive_reduction only iterates dependencies.items(); an extra isolated
    # node in all_nodes (forwarded to the closure step) has no effect.
    assert _reduce({"A": {"B", "C"}, "B": {"C"}}, all_nodes={"A", "B", "C", "Z"}) == {"A": {"B"}, "B": {"C"}}


def test_reduction_is_idempotent():
    # Long chain A->B->C->D plus every implied shortcut collapses to the chain;
    # reducing the already-reduced graph is a no-op.
    g = {"A": {"B", "C", "D"}, "B": {"C", "D"}, "C": {"D"}}
    once = _reduce(g)
    assert once == {"A": {"B"}, "B": {"C"}, "C": {"D"}}
    assert _reduce(once) == once


def test_reduction_lists_every_removed_edge_with_valid_witness():
    # The chain implies three shortcuts. Compare the (source, target) pairs as a
    # set (the witness intermediate is iteration-order dependent).
    _, removed = _reduce(
        {"A": {"B", "C", "D"}, "B": {"C", "D"}, "C": {"D"}},
        return_removed_edges=True,
    )
    assert {(s, t) for s, _w, t in removed} == {("A", "C"), ("A", "D"), ("B", "D")}
    # Every witness w is a real one-hop neighbor of the source that reaches t.
    assert all(w in {"B", "C", "D"} for _s, w, _t in removed)
    # Three shortcuts -> three removed edges; no source==target witnesses leak.
    assert len(removed) == 3
    assert all(s != t for s, _w, t in removed)


def test_reduction_full_chain_collapses_to_spine_many_removed():
    # 5-node "complete chain" (every node points to all later nodes) reduces to
    # the spine A->B->C->D->E, removing 6 redundant edges. >3 removals exercises
    # the "... and N more" stdout-summary branch.
    g = {
        "A": {"B", "C", "D", "E"},
        "B": {"C", "D", "E"},
        "C": {"D", "E"},
        "D": {"E"},
    }
    reduced, removed = _reduce(g, return_removed_edges=True)
    assert reduced == {"A": {"B"}, "B": {"C"}, "C": {"D"}, "D": {"E"}}
    assert {(s, t) for s, _w, t in removed} == {
        ("A", "C"),
        ("A", "D"),
        ("A", "E"),
        ("B", "D"),
        ("B", "E"),
        ("C", "E"),
    }
    assert len(removed) == 6


def test_reduction_return_shapes():
    # No flag -> a bare dict.
    bare = _reduce({"A": {"B"}})
    assert type(bare) is dict

    # With the flag -> a 2-tuple (dict, list) where each removed edge is a 3-tuple.
    result = _reduce({"A": {"B", "C"}, "B": {"C"}}, return_removed_edges=True)
    assert type(result) is tuple
    assert len(result) == 2
    reduced, removed = result
    assert type(reduced) is dict  # plain dict, not the internal defaultdict
    assert type(removed) is list
    assert all(isinstance(e, tuple) and len(e) == 3 for e in removed)


def test_reduction_empty_graph_returns_plain_dict_not_defaultdict():
    # The cleanup comprehension rebuilds a plain dict even when nothing remains;
    # a leaked defaultdict would auto-vivify on missing-key access.
    reduced = _reduce({})
    assert type(reduced) is dict
    with pytest.raises(KeyError):
        reduced["missing"]


def test_reduction_witness_is_a_pair_set_not_the_full_triple():
    # Two candidate intermediates (B and D) both reach C, but exactly one edge
    # A->C is removed (the loop breaks on the first witness). Pin the stable part
    # -- which (source, target) is dropped -- not the order-dependent witness.
    _, removed = _reduce(
        {"A": {"B", "D", "C"}, "B": {"C"}, "D": {"C"}},
        return_removed_edges=True,
    )
    assert {(s, t) for s, _w, t in removed} == {("A", "C")}
    assert len(removed) == 1


def test_reduction_skips_intermediate_equal_to_target():
    # Adversarial near-miss for the `if intermediate == target: continue` guard
    # (source line 130-131). A's only out-edge is B, and B is also the only
    # candidate intermediate -- but it equals the target, so it is skipped and
    # A->B is NEVER falsely removed. A missing guard would let an edge witness
    # itself and vanish.
    reduced, removed = _reduce({"A": {"B"}, "B": set()}, return_removed_edges=True)
    assert reduced == {"A": {"B"}}
    assert removed == []


def test_reduction_overremoves_minimal_two_node_cycle_chord_observed_behavior():
    # Minimal reproduction of the cyclic over-removal bug (smaller than the 3-node
    # example below): A->{B,C} with a back-edge B->A. A->C is dropped because the
    # closure wraps B->A->C, so C becomes unreachable from A. Characterize the
    # ACTUAL buggy output so any future fix must update this test.
    g = {"A": {"B", "C"}, "B": {"A"}}
    assert _reduce(g) == {"A": {"B"}, "B": {"A"}}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Appendix A (same root cause as the 3-node case): transitive_reduction "
        "over-removes on a 2-node cycle with a chord. For A->{B,C}, B->A the edge "
        "A->C is deleted (closure wraps B->A->C), so closure(reduce(g)) drops C "
        "from A's reach and the invariant closure(reduce(g)) == closure(g) fails."
    ),
)
def test_reduction_preserves_reachability_on_minimal_cycle_chord():
    g = {"A": {"B", "C"}, "B": {"A"}}
    assert compute_transitive_closure(_reduce(g)) == compute_transitive_closure(g)


def test_reduction_closure_invariant_holds_on_every_dag_case():
    # Cross-check: for the DAG cases the suite reduces, the reduction must always
    # preserve the transitive closure. This guards against any future change that
    # over-removes on acyclic input (the documented bug is cycle-only).
    dags = [
        {"A": {"B", "C"}, "B": {"C"}},
        {"A": {"B", "C", "D"}, "B": {"C", "D"}, "C": {"D"}},
        {"A": {"B", "C", "D", "E"}, "B": {"C", "D", "E"}, "C": {"D", "E"}, "D": {"E"}},
        {"A": {"B", "C"}, "B": {"C"}, "X": {"Y", "Z"}, "Y": {"Z"}},
        {"A": {"B", "C"}},
    ]
    for g in dags:
        assert compute_transitive_closure(_reduce(g)) == compute_transitive_closure(g)


# ---------------------------------------------------------------------------
# Bug pin: transitive reduction does not preserve reachability on cyclic graphs.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Appendix A: transitive_reduction over-removes edges on cyclic graphs. "
        "For A->{B,C}, B->C, C->A both of A's out-edges are deleted because the "
        "full closure (which wraps the cycle through the edge under test) marks "
        "each as redundant. The reduced graph loses A's reachability, so the "
        "transitive-closure invariant closure(reduce(g)) == closure(g) fails."
    ),
)
def test_reduction_preserves_reachability_on_cyclic_graph():
    g = {"A": {"B", "C"}, "B": {"C"}, "C": {"A"}}
    reduced = _reduce(g)
    assert compute_transitive_closure(reduced) == compute_transitive_closure(g)


def test_reduction_preserves_reachability_on_dag():
    # The correct case (no cycles): reduction must preserve the closure. This is
    # the green counterpart to the xfail above, proving the invariant holds for
    # the algorithm's intended (acyclic) domain.
    g = {"A": {"B", "C", "D"}, "B": {"C", "D"}, "C": {"D"}}
    reduced = _reduce(g)
    assert compute_transitive_closure(reduced) == compute_transitive_closure(g)


def test_reduction_overremoves_cyclic_chord_observed_behavior():
    # Characterize the *actual* (buggy) output so a future fix is forced to
    # update this test rather than silently changing behavior.
    g = {"A": {"B", "C"}, "B": {"C"}, "C": {"A"}}
    assert _reduce(g) == {"B": {"C"}, "C": {"A"}}


# ---------------------------------------------------------------------------
# get_graph_statistics
#
# Pure (no IO/Docker/git/LLM); covers source lines 181-203. Every dict is pinned
# in full (all eight keys) so no metric can silently drift. Floats are exact
# (small rationals) and equality on the whole dict is hash-seed safe.
# ---------------------------------------------------------------------------


def test_statistics_chain_with_explicit_all_nodes():
    # A->B->C, all_nodes given. 3 nodes, 2 edges, no isolated nodes.
    assert get_graph_statistics({"A": {"B"}, "B": {"C"}}, {"A", "B", "C"}) == {
        "total_nodes": 3,
        "nodes_with_outgoing_edges": 2,
        "total_edges": 2,
        "avg_out_degree": 2 / 3,
        "density": 2 / 6,
        "isolated_nodes": 0,
        "max_in_degree": 1,
        "max_out_degree": 1,
    }


def test_statistics_infers_all_nodes_when_omitted():
    # all_nodes omitted -> inferred from keys + targets. Diamond-ish graph:
    # A->{B,C}, B->C. C has in-degree 2 (from A and B).
    assert get_graph_statistics({"A": {"B", "C"}, "B": {"C"}}) == {
        "total_nodes": 3,
        "nodes_with_outgoing_edges": 2,
        "total_edges": 3,
        "avg_out_degree": 3 / 3,
        "density": 3 / 6,
        "isolated_nodes": 0,
        "max_in_degree": 2,
        "max_out_degree": 2,
    }


def test_statistics_empty_graph_hits_all_zero_fallback_branches():
    # Empty deps + inferred (empty) all_nodes: every guarded ternary takes its
    # else-branch. avg_out_degree and density fall back to literal 0 (int), and
    # max_in/out_degree fall back to 0 because the degree dicts are empty.
    assert get_graph_statistics({}) == {
        "total_nodes": 0,
        "nodes_with_outgoing_edges": 0,
        "total_edges": 0,
        "avg_out_degree": 0,
        "density": 0,
        "isolated_nodes": 0,
        "max_in_degree": 0,
        "max_out_degree": 0,
    }


def test_statistics_single_node_all_nodes_density_zero_branch():
    # Empty deps but all_nodes={'A'}: total_nodes=1, so the density `len>1` guard
    # is False -> density 0 (int). avg_out_degree divides by 1 -> 0.0 (float).
    # The lone node is isolated. Near-miss separating the two distinct guards
    # (avg uses `if all_nodes`, density uses `if len(all_nodes) > 1`).
    stats = get_graph_statistics({}, {"A"})
    assert stats == {
        "total_nodes": 1,
        "nodes_with_outgoing_edges": 0,
        "total_edges": 0,
        "avg_out_degree": 0.0,
        "density": 0,
        "isolated_nodes": 1,
        "max_in_degree": 0,
        "max_out_degree": 0,
    }
    # Pin the exact types: avg is a float (division happened), density is the int
    # literal 0 (the guard short-circuited before any division).
    assert type(stats["avg_out_degree"]) is float
    assert type(stats["density"]) is int


def test_statistics_isolated_nodes_counted():
    # A->B with an extra node Z named only in all_nodes -> Z is isolated.
    assert get_graph_statistics({"A": {"B"}}, {"A", "B", "Z"}) == {
        "total_nodes": 3,
        "nodes_with_outgoing_edges": 1,
        "total_edges": 1,
        "avg_out_degree": 1 / 3,
        "density": 1 / 6,
        "isolated_nodes": 1,
        "max_in_degree": 1,
        "max_out_degree": 1,
    }


def test_statistics_self_loop_density_is_zero_when_single_node():
    # A->A: one node, one edge. density guard `len>1` is False -> 0 even though
    # an edge exists. A is both source and target so it is NOT isolated.
    assert get_graph_statistics({"A": {"A"}}) == {
        "total_nodes": 1,
        "nodes_with_outgoing_edges": 1,
        "total_edges": 1,
        "avg_out_degree": 1.0,
        "density": 0,
        "isolated_nodes": 0,
        "max_in_degree": 1,
        "max_out_degree": 1,
    }


def test_statistics_fully_connected_triangle_density_one():
    # Complete directed graph on 3 nodes: 6 edges = max possible (n*(n-1)) ->
    # density exactly 1.0. Exercises the max_in/out_degree max() branches with a
    # populated degree dict.
    assert get_graph_statistics({"A": {"B", "C"}, "B": {"A", "C"}, "C": {"A", "B"}}) == {
        "total_nodes": 3,
        "nodes_with_outgoing_edges": 3,
        "total_edges": 6,
        "avg_out_degree": 6 / 3,
        "density": 1.0,
        "isolated_nodes": 0,
        "max_in_degree": 2,
        "max_out_degree": 2,
    }


def test_statistics_empty_dep_set_key_counts_as_having_outgoing_but_zero_edges():
    # A:set() is a key (counts toward nodes_with_outgoing_edges) but contributes
    # no edges. Adversarial near-miss: nodes_with_outgoing_edges (len of deps) and
    # total_edges (sum of out-set sizes) are distinct metrics and must not be
    # conflated. max_out_degree over {A: 0} is 0.
    assert get_graph_statistics({"A": set()}) == {
        "total_nodes": 1,
        "nodes_with_outgoing_edges": 1,
        "total_edges": 0,
        "avg_out_degree": 0.0,
        "density": 0,
        "isolated_nodes": 0,
        "max_in_degree": 0,
        "max_out_degree": 0,
    }


def test_statistics_returns_plain_dict_with_exact_key_set():
    # Pin the exact public shape: a plain dict with precisely these eight keys,
    # nothing more, nothing less.
    stats = get_graph_statistics({"A": {"B"}, "B": {"C"}})
    assert type(stats) is dict
    assert set(stats) == {
        "total_nodes",
        "nodes_with_outgoing_edges",
        "total_edges",
        "avg_out_degree",
        "density",
        "isolated_nodes",
        "max_in_degree",
        "max_out_degree",
    }


def test_statistics_skewed_degrees_pick_distinct_max_in_and_out():
    # Adversarial: the node with the highest out-degree (A, 3) is different from
    # the node with the highest in-degree (D, 2). Pins that max_in_degree and
    # max_out_degree are computed independently, not from the same node.
    g = {"A": {"B", "C", "D"}, "B": {"D"}}
    assert get_graph_statistics(g) == {
        "total_nodes": 4,
        "nodes_with_outgoing_edges": 2,
        "total_edges": 4,
        "avg_out_degree": 4 / 4,
        "density": 4 / 12,
        "isolated_nodes": 0,
        "max_in_degree": 2,
        "max_out_degree": 3,
    }
