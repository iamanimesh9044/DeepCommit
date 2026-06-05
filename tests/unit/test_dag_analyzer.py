"""Characterization / unit tests for ``deepcommit.tools.dag_analysis.dag_analyzer``.

Scope (Wave 1, pure-core + thin CSV IO + MCP wrappers using ``tmp_path`` only):
  - ``_detect_cycles_from_graph`` — single-pass DFS cycle detector.
  - ``_compute_levels``           — parallel execution layers (Kahn-style peeling).
  - ``_find_critical_path``       — longest path (returns NODE count == max_dist + 1).
  - ``detect_cycle`` / ``topological_sort`` — thin CSV wrappers over the pure core.
  - ``analyze_parallelism``       — aggregates levels + critical path + width stats.
  - ``analyze_dag_parallelism_tool`` / ``validate_dag`` — MCP wrappers; the JSON
    save branch writes to ``tmp_path`` (ordinary local fs, no Docker/git/LLM).

Determinism notes (all empirically verified under PYTHONHASHSEED 0 and 1):
  - ``_detect_cycles_from_graph`` iterates the ``nodes`` argument, so passing a
    LIST makes the rotation/order of detected cycles deterministic. The CSV
    wrappers feed it a SET (from ``load_dependency_graph``), so the wrapper-level
    cycle rotation is hashseed-dependent -> we assert the node SET and the
    closed-loop invariant, never the exact list.
  - ``_compute_levels`` returns ``list[set]``; set equality is order-free.
  - ``_find_critical_path`` resolves ties by dict/set iteration order, so the
    middle node of a diamond is hashseed-dependent -> we assert endpoints +
    length + membership, never the exact interior.
  - A branching ``topological_sort`` is hashseed-dependent -> we assert only the
    prerequisite-before-dependant ordering invariant.
"""

import csv
import json

import pytest

from deepcommit.tools.dag_analysis.dag_analyzer import (
    _compute_levels,
    _detect_cycles_from_graph,
    _find_critical_path,
    analyze_dag_parallelism_tool,
    analyze_parallelism,
    detect_cycle,
    topological_sort,
    validate_dag,
)

# ---------------------------------------------------------------------------
# _detect_cycles_from_graph  (pass nodes as a LIST for deterministic rotation)
# ---------------------------------------------------------------------------


def test_detect_acyclic_returns_empty_list():
    result = _detect_cycles_from_graph({"A": {"B"}, "B": {"C"}}, ["A", "B", "C"])
    assert result == []
    assert isinstance(result, list)


def test_detect_self_loop():
    # A -> A is a 1-node cycle; path is [A, A] (closed on itself).
    assert _detect_cycles_from_graph({"A": {"A"}}, ["A"]) == [["A", "A"]]


def test_detect_two_cycle_list_nodes_deterministic():
    assert _detect_cycles_from_graph({"A": {"B"}, "B": {"A"}}, ["A", "B"]) == [["A", "B", "A"]]


def test_detect_two_cycle_rotation_follows_node_list_order():
    # Same graph, reversed node list -> DFS starts at B, so the cycle rotates.
    # Proves the rotation is driven by the (deterministic) list order, not luck.
    assert _detect_cycles_from_graph({"A": {"B"}, "B": {"A"}}, ["B", "A"]) == [["B", "A", "B"]]


def test_detect_three_cycle_exact_and_canonical():
    cyc = _detect_cycles_from_graph({"A": {"B"}, "B": {"C"}, "C": {"A"}}, ["A", "B", "C"])
    # Exact (deterministic because nodes is a list starting at "A"):
    assert cyc == [["A", "B", "C", "A"]]
    # Canonical invariants that any rotation must satisfy:
    assert cyc[0][0] == cyc[0][-1]  # closed loop
    assert set(cyc[0]) == {"A", "B", "C"}  # every node on the loop


def test_detect_back_edge_to_mid_node_excludes_acyclic_prefix():
    # A -> B -> C -> B. The cycle is B<->C; the acyclic prefix A is NOT in it.
    # Proves the detector slices the path at the back-edge target, not at the root.
    assert _detect_cycles_from_graph({"A": {"B"}, "B": {"C"}, "C": {"B"}}, ["A", "B", "C"]) == [["B", "C", "B"]]


def test_detect_diamond_is_not_a_cycle():
    # Adversarial near-miss: D is reached via both B and C (a cross edge to an
    # already-visited node), but D is NOT on the recursion stack -> no cycle.
    result = _detect_cycles_from_graph({"A": {"B", "C"}, "B": {"D"}, "C": {"D"}}, ["A", "B", "C", "D"])
    assert result == []


def test_detect_dangling_target_no_cycle():
    # "B" is a neighbor with no outgoing edges and is not a graph key.
    assert _detect_cycles_from_graph({"A": {"B"}}, ["A", "B"]) == []


def test_detect_two_disconnected_cycles_both_found():
    # Two independent 2-cycles; both are reported, ordered by the node list.
    result = _detect_cycles_from_graph(
        {"A": {"B"}, "B": {"A"}, "C": {"D"}, "D": {"C"}},
        ["A", "B", "C", "D"],
    )
    assert result == [["A", "B", "A"], ["C", "D", "C"]]


def test_detect_empty_graph_and_nodes():
    assert _detect_cycles_from_graph({}, []) == []


# ---------------------------------------------------------------------------
# _compute_levels  (parallel execution layers; takes a SET of nodes)
# ---------------------------------------------------------------------------


def test_levels_chain():
    assert _compute_levels({"A": {"B"}, "B": {"C"}}, {"A", "B", "C"}) == [{"A"}, {"B"}, {"C"}]


def test_levels_diamond_packs_parallel_nodes():
    levels = _compute_levels({"A": {"B", "C"}, "B": {"D"}, "C": {"D"}}, {"A", "B", "C", "D"})
    # B and C are independent and share level 1 (set equality is order-free).
    assert levels == [{"A"}, {"B", "C"}, {"D"}]


def test_levels_isolated_node_joins_root_layer():
    # C has no edges -> it is a root and lands in level 0 alongside A.
    assert _compute_levels({"A": {"B"}}, {"A", "B", "C"}) == [{"A", "C"}, {"B"}]


def test_levels_uneven_branch_depth():
    # A -> {B, E}; B -> C -> D. E terminates early, the B-chain extends deeper.
    levels = _compute_levels({"A": {"B", "E"}, "B": {"C"}, "C": {"D"}}, {"A", "B", "C", "D", "E"})
    assert levels == [{"A"}, {"B", "E"}, {"C"}, {"D"}]


def test_levels_single_node():
    assert _compute_levels({}, {"A"}) == [{"A"}]


def test_levels_empty():
    assert _compute_levels({}, set()) == []


def test_levels_does_not_mutate_caller_node_set():
    nodes = {"A", "B", "C"}
    _compute_levels({"A": {"B"}, "B": {"C"}}, nodes)
    assert nodes == {"A", "B", "C"}  # internal copy() must protect the caller


def test_levels_raise_on_full_cycle():
    with pytest.raises(ValueError, match="[Cc]ycle"):
        _compute_levels({"A": {"B"}, "B": {"A"}}, {"A", "B"})


def test_levels_raise_on_downstream_cycle():
    # A is a clean root; B<->C form a cycle that can never reach in-degree 0.
    with pytest.raises(ValueError, match="[Cc]ycle"):
        _compute_levels({"A": {"B"}, "B": {"C"}, "C": {"B"}}, {"A", "B", "C"})


# ---------------------------------------------------------------------------
# _find_critical_path  (longest path; returns (node_count, path_nodes))
# ---------------------------------------------------------------------------


def test_critical_path_chain_counts_nodes_not_edges():
    length, path = _find_critical_path({"A": {"B"}, "B": {"C"}}, {"A", "B", "C"})
    assert (length, path) == (3, ["A", "B", "C"])  # 3 nodes / 2 edges


def test_critical_path_two_node():
    assert _find_critical_path({"A": {"B"}}, {"A", "B"}) == (2, ["A", "B"])


def test_critical_path_diamond_picks_a_longest_branch():
    length, path = _find_critical_path({"A": {"B", "C"}, "B": {"D"}, "C": {"D"}}, {"A", "B", "C", "D"})
    assert length == 3  # A -> (B|C) -> D
    assert path[0] == "A"
    assert path[-1] == "D"
    assert len(path) == 3
    # Interior tie is broken by iteration order (hashseed-dependent) -> membership only.
    assert path[1] in {"B", "C"}


def test_critical_path_uneven_picks_the_deeper_chain():
    # A -> {B, E}; B -> C -> D (depth 4) vs A -> E (depth 2). Deeper chain wins.
    assert _find_critical_path({"A": {"B", "E"}, "B": {"C"}, "C": {"D"}}, {"A", "B", "C", "D", "E"}) == (
        4,
        ["A", "B", "C", "D"],
    )


def test_critical_path_ignores_disconnected_isolated_nodes():
    # X and Y are isolated; the longest path is still the A-B-C chain.
    length, path = _find_critical_path({"A": {"B"}, "B": {"C"}}, {"A", "B", "C", "X", "Y"})
    assert (length, path) == (3, ["A", "B", "C"])


def test_critical_path_single_node_quirk():
    # Documented quirk (Appendix-style): with no edges, max_dist stays 0, so the
    # `d > max_dist` branch never fires and end_node is never set. length is
    # max_dist + 1 == 1 but the reconstructed path is EMPTY. Pinned so any future
    # refactor that "fixes" this is a conscious, reviewed change.
    assert _find_critical_path({}, {"A"}) == (1, [])


def test_critical_path_multiple_isolated_nodes_same_quirk():
    # Same quirk generalises to several edgeless nodes: still (1, []).
    assert _find_critical_path({}, {"A", "B", "C"}) == (1, [])


def test_critical_path_empty_graph_empty_nodes():
    assert _find_critical_path({}, set()) == (1, [])


# ---------------------------------------------------------------------------
# CSV wrappers  (thin IO over the pure core; replaces legacy cycle-detector test)
# ---------------------------------------------------------------------------


def _write_deps(tmp_path, rows, header=("prerequisite_id", "dependant_id")):
    p = tmp_path / "dependencies.csv"
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(list(header))
        w.writerows(rows)
    return str(p)


def test_detect_cycle_csv_acyclic(tmp_path):
    has_cycle, cycles = detect_cycle(_write_deps(tmp_path, [("M1", "M2"), ("M2", "M3")]))
    assert has_cycle is False
    assert cycles is None


def test_detect_cycle_csv_with_cycle(tmp_path):
    has_cycle, cycles = detect_cycle(_write_deps(tmp_path, [("M1", "M2"), ("M2", "M1")]))
    assert has_cycle is True
    assert cycles is not None and len(cycles) == 1
    # Rotation is hashseed-dependent (loader yields a set) -> canonical checks only.
    only = cycles[0]
    assert len(only) == 3  # exactly Mx -> My -> Mx (2 distinct nodes + closing repeat)
    assert only[0] == only[-1]  # closed loop
    assert set(only) == {"M1", "M2"}


def test_detect_cycle_csv_legacy_header(tmp_path):
    # Loader also accepts the legacy source_id/target_id header.
    has_cycle, cycles = detect_cycle(
        _write_deps(tmp_path, [("M1", "M2"), ("M2", "M3")], header=("source_id", "target_id"))
    )
    assert has_cycle is False
    assert cycles is None


def test_detect_cycle_csv_missing_file(tmp_path):
    # Missing file -> loader returns an empty graph -> no cycle, no list.
    missing = str(tmp_path / "nope.csv")
    assert detect_cycle(missing) == (False, None)


def test_topological_sort_orders_prereqs_first(tmp_path):
    order = topological_sort(_write_deps(tmp_path, [("M1", "M2"), ("M2", "M3")]))
    assert order == ["M1", "M2", "M3"]


def test_topological_sort_branching_respects_dependency_invariant(tmp_path):
    # M1 -> M2 and M1 -> M3. Sibling order is hashseed-dependent; only the
    # prerequisite-before-dependant invariant is guaranteed.
    order = topological_sort(_write_deps(tmp_path, [("M1", "M2"), ("M1", "M3")]))
    assert order is not None
    assert sorted(order) == ["M1", "M2", "M3"]
    assert order.index("M1") < order.index("M2")
    assert order.index("M1") < order.index("M3")


def test_topological_sort_none_on_cycle(tmp_path):
    assert topological_sort(_write_deps(tmp_path, [("M1", "M2"), ("M2", "M1")])) is None


def test_topological_sort_missing_file_returns_empty_list(tmp_path):
    # Documented behaviour: a missing file yields an empty graph, so the sort
    # returns [] (len(result) == len(nodes) == 0), NOT None. Pinned to lock the
    # distinction between "no DAG" ([]) and "cyclic DAG" (None).
    missing = str(tmp_path / "nope.csv")
    assert topological_sort(missing) == []


def test_detect_cycle_csv_two_disjoint_cycles_counts_both(tmp_path):
    # Two independent 2-cycles fed through the SET-based loader. Rotation/order is
    # hashseed-dependent, so we canonicalise to a set-of-frozensets of the
    # node-content of each detected cycle and assert the exact pair.
    has_cycle, cycles = detect_cycle(_write_deps(tmp_path, [("M1", "M2"), ("M2", "M1"), ("M3", "M4"), ("M4", "M3")]))
    assert has_cycle is True
    assert cycles is not None and len(cycles) == 2
    for c in cycles:
        assert c[0] == c[-1]  # each is a closed loop
        assert len(c) == 3  # Mx -> My -> Mx
    contents = {frozenset(c) for c in cycles}
    assert contents == {frozenset({"M1", "M2"}), frozenset({"M3", "M4"})}


# ---------------------------------------------------------------------------
# analyze_parallelism  (aggregates levels + critical path + width statistics)
# ---------------------------------------------------------------------------


def test_analyze_parallelism_diamond_full_shape(tmp_path):
    # Diamond A -> {B, C} -> D. Levels [{A}, {B,C}, {D}] -> widths [1, 2, 1].
    m = analyze_parallelism(_write_deps(tmp_path, [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")]))
    assert m.critical_path_length == 3
    # Interior diamond node is hashseed-dependent -> endpoints + length only.
    assert m.critical_path_nodes[0] == "A"
    assert m.critical_path_nodes[-1] == "D"
    assert len(m.critical_path_nodes) == 3
    assert m.critical_path_nodes[1] in {"B", "C"}
    assert m.maximum_width == 2
    assert m.total_nodes == 4
    assert m.total_edges == 4
    # levels is list[set]; set equality is order-free.
    assert m.levels == [{"A"}, {"B", "C"}, {"D"}]
    assert m.level_widths == [1, 2, 1]
    # Width-distribution stats are exact (computed over [1, 2, 1]).
    s = m.level_widths_stats
    assert s["mean"] == pytest.approx(4 / 3)
    assert s["median"] == 1
    assert s["min"] == 1
    assert s["max"] == 2
    assert s["std_dev"] == pytest.approx(0.5773502691896257)
    assert s["variance_coefficient"] == pytest.approx(s["std_dev"] / (4 / 3))


def test_analyze_parallelism_chain_zero_variance(tmp_path):
    # Linear chain A -> B -> C. Every level has width 1, so std_dev and the
    # coefficient of variation are exactly 0.0 (the len==1 / mean==0 guards).
    m = analyze_parallelism(_write_deps(tmp_path, [("A", "B"), ("B", "C")]))
    assert m.critical_path_length == 3
    assert m.critical_path_nodes == ["A", "B", "C"]
    assert m.maximum_width == 1
    assert m.total_nodes == 3
    assert m.total_edges == 2
    assert m.level_widths == [1, 1, 1]
    s = m.level_widths_stats
    assert s["mean"] == 1
    assert s["median"] == 1
    assert s["std_dev"] == 0.0
    assert s["min"] == 1
    assert s["max"] == 1
    assert s["variance_coefficient"] == 0.0


def test_analyze_parallelism_raises_on_cyclic_graph(tmp_path):
    # _compute_levels raises ValueError on a cycle, and analyze_parallelism
    # propagates it (it does not pre-check acyclicity).
    with pytest.raises(ValueError, match="[Cc]ycle"):
        analyze_parallelism(_write_deps(tmp_path, [("M1", "M2"), ("M2", "M1")]))


def test_analyze_parallelism_empty_graph_zero_default_stats(tmp_path):
    # Empty graph (missing file -> empty graph): levels == [] drives the
    # `else` zero-defaults branch of level_widths_stats. critical_path_length is
    # 1 (the edgeless max_dist + 1 quirk, generalised to zero nodes).
    m = analyze_parallelism(str(tmp_path / "nope.csv"))
    assert m.critical_path_length == 1
    assert m.critical_path_nodes == []
    assert m.maximum_width == 0
    assert m.total_nodes == 0
    assert m.total_edges == 0
    assert m.levels == []
    assert m.level_widths == []
    assert m.level_widths_stats == {
        "mean": 0.0,
        "median": 0.0,
        "std_dev": 0.0,
        "min": 0,
        "max": 0,
        "variance_coefficient": 0.0,
    }


# ---------------------------------------------------------------------------
# analyze_dag_parallelism_tool  (MCP wrapper: compute dict + optional JSON save)
# ---------------------------------------------------------------------------


def test_analyze_dag_tool_no_save_returns_plain_dict(tmp_path):
    r = analyze_dag_parallelism_tool(_write_deps(tmp_path, [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")]))
    # No save -> no output_file / output_message keys.
    assert set(r.keys()) == {
        "critical_path_length",
        "critical_path_nodes",
        "maximum_width",
        "total_nodes",
        "total_edges",
        "levels",
        "level_widths",
        "level_widths_stats",
    }
    assert r["critical_path_length"] == 3
    assert r["maximum_width"] == 2
    assert r["total_nodes"] == 4
    assert r["total_edges"] == 4
    # levels is serialised as list[list]; canonicalise inner lists to sets.
    assert [set(level) for level in r["levels"]] == [{"A"}, {"B", "C"}, {"D"}]
    assert r["level_widths"] == [1, 2, 1]


def test_analyze_dag_tool_save_explicit_output_dir_writes_json(tmp_path):
    csv_path = _write_deps(tmp_path, [("A", "B"), ("B", "C")])
    out_dir = tmp_path / "explicit_out"
    r = analyze_dag_parallelism_tool(csv_path, save_results=True, output_dir=str(out_dir))
    expected = out_dir / "dag_parallelism_analysis.json"
    assert r["output_file"] == str(expected)
    assert r["output_message"] == f"Results saved to {expected}"
    assert expected.is_file()
    # The persisted JSON must round-trip to the compute payload.
    with expected.open(encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded["critical_path_length"] == 3
    assert loaded["total_nodes"] == 3
    assert loaded["level_widths"] == [1, 1, 1]
    # output_file / output_message are added to the return dict, NOT to the file.
    assert "output_file" not in loaded
    assert "output_message" not in loaded


def test_analyze_dag_tool_save_default_output_dir_is_csv_parent(tmp_path):
    # output_dir omitted -> defaults to <csv parent>/analysis_results.
    csv_path = _write_deps(tmp_path, [("A", "B"), ("B", "C")])
    r = analyze_dag_parallelism_tool(csv_path, save_results=True)
    expected = tmp_path / "analysis_results" / "dag_parallelism_analysis.json"
    assert r["output_file"] == str(expected)
    assert expected.is_file()


# ---------------------------------------------------------------------------
# validate_dag  (MCP wrapper: cycle verdict + topo order / cycle list, + save)
# ---------------------------------------------------------------------------


def test_validate_dag_acyclic_includes_topological_order(tmp_path):
    r = validate_dag(_write_deps(tmp_path, [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")]))
    assert r["is_valid_dag"] is True
    assert r["has_cycle"] is False
    assert r["node_count"] == 4
    assert r["edge_count"] == 4
    # No cycle -> cycle keys absent, topo order present.
    assert "cycles" not in r
    assert "cycle_count" not in r
    assert "topological_order" in r
    # Sibling order is hashseed-dependent -> canonicalise to sorted membership.
    assert sorted(r["topological_order"]) == ["A", "B", "C", "D"]


def test_validate_dag_cyclic_reports_cycles_and_no_topo(tmp_path):
    r = validate_dag(_write_deps(tmp_path, [("M1", "M2"), ("M2", "M1")]))
    assert r["is_valid_dag"] is False
    assert r["has_cycle"] is True
    assert r["node_count"] == 2
    assert r["edge_count"] == 2
    assert r["cycle_count"] == 1
    # Cyclic -> no topological order key.
    assert "topological_order" not in r
    only = r["cycles"][0]
    assert len(only) == 3
    assert only[0] == only[-1]
    assert set(only) == {"M1", "M2"}


def test_validate_dag_empty_graph_valid_but_no_topo_key(tmp_path):
    # Near-miss branch: an empty graph has no cycle, so is_valid_dag is True, but
    # topological_sort returns a falsy [] -> the `if topo_sort:` guard SKIPS the
    # topological_order key. Pins the "valid DAG with zero nodes omits topo" path.
    missing = str(tmp_path / "nope.csv")
    r = validate_dag(missing)
    assert r == {
        "is_valid_dag": True,
        "has_cycle": False,
        "node_count": 0,
        "edge_count": 0,
    }


def test_validate_dag_save_writes_validation_json(tmp_path):
    csv_path = _write_deps(tmp_path, [("A", "B"), ("B", "C")])
    out_dir = tmp_path / "vout"
    r = validate_dag(csv_path, save_results=True, output_dir=str(out_dir))
    expected = out_dir / "dag_cycle_validation.json"
    assert r["output_file"] == str(expected)
    assert r["output_message"] == f"Results saved to {expected}"
    assert expected.is_file()
    with expected.open(encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded["is_valid_dag"] is True
    assert loaded["node_count"] == 3
    assert loaded["edge_count"] == 2
    assert sorted(loaded["topological_order"]) == ["A", "B", "C"]
    assert "output_file" not in loaded
    assert "output_message" not in loaded


def test_validate_dag_save_default_output_dir_is_csv_parent(tmp_path):
    # output_dir omitted -> defaults to <csv parent>/analysis_results.
    csv_path = _write_deps(tmp_path, [("A", "B"), ("B", "C")])
    r = validate_dag(csv_path, save_results=True)
    expected = tmp_path / "analysis_results" / "dag_cycle_validation.json"
    assert r["output_file"] == str(expected)
    assert expected.is_file()


def test_validate_dag_save_cyclic_persists_cycles(tmp_path):
    csv_path = _write_deps(tmp_path, [("M1", "M2"), ("M2", "M1")])
    out_dir = tmp_path / "vout_cyc"
    r = validate_dag(csv_path, save_results=True, output_dir=str(out_dir))
    expected = out_dir / "dag_cycle_validation.json"
    assert r["output_file"] == str(expected)
    assert r["cycle_count"] == 1
    assert expected.is_file()
    with expected.open(encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded["is_valid_dag"] is False
    assert loaded["has_cycle"] is True
    assert loaded["cycle_count"] == 1
    assert set(loaded["cycles"][0]) == {"M1", "M2"}
