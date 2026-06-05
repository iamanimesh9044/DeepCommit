"""Unit tests for the m3 verb-suite shared invariant library.

invariants.py lifts (does NOT import) the proven graph/SHA primitives from the
deprecated _orchestrator/csv_ops.py and generalizes them: full-graph
acyclicity, reachability, DAG-closure, redundant-edge detection, seq anchoring,
ambiguity-safe SHA-prefix matching, and commit conservation.
"""

from deepcommit.harness.main_orch import invariants as inv


# ---------------------------------------------------------------- reachability
def test_reaches_follows_prerequisite_to_dependant_transitively():
    edges = [("A", "B"), ("B", "C")]
    assert inv.reaches(edges, "A", "C") is True
    assert inv.reaches(edges, "A", "B") is True
    # reverse direction is not reachable
    assert inv.reaches(edges, "C", "A") is False
    # a node does not reach itself without a cycle
    assert inv.reaches(edges, "A", "A") is False


def test_reaches_unknown_nodes_are_not_reachable():
    edges = [("A", "B")]
    assert inv.reaches(edges, "A", "Z") is False
    assert inv.reaches(edges, "Z", "A") is False


# --------------------------------------------------------------- would_create_cycle
def test_would_create_cycle_detects_back_edge():
    edges = [("A", "B"), ("B", "C")]
    # adding C -> A (prereq C, dependant A) closes A->B->C->A
    assert inv.would_create_cycle(edges, "C", "A") is True
    # adding A -> C is safe (no cycle, just redundant)
    assert inv.would_create_cycle(edges, "A", "C") is False


def test_would_create_cycle_rejects_self_edge():
    assert inv.would_create_cycle([], "A", "A") is True


# ------------------------------------------------------------------- detect_cycle
def test_detect_cycle_returns_none_for_acyclic_graph():
    edges = [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")]
    assert inv.detect_cycle(edges) is None
    assert inv.is_acyclic(edges) is True


def test_detect_cycle_finds_a_cycle():
    edges = [("A", "B"), ("B", "C"), ("C", "A")]
    cycle = inv.detect_cycle(edges)
    assert cycle is not None
    # the returned path is an actual cycle over the three nodes
    assert set(cycle) >= {"A", "B", "C"}
    assert inv.is_acyclic(edges) is False


# -------------------------------------------------------------------- dag closure
def test_is_dag_closed_flags_missing_ancestor():
    edges = [("A", "B"), ("B", "C")]
    result = inv.is_dag_closed(["C"], edges)
    assert result.is_closed is False
    assert result.missing_ancestors["C"] == ["A", "B"]


def test_is_dag_closed_accepts_full_closure():
    edges = [("A", "B"), ("B", "C")]
    result = inv.is_dag_closed(["A", "B", "C"], edges)
    assert result.is_closed is True
    assert result.missing_ancestors == {}


# ----------------------------------------------------------------- redundant edge
def test_is_redundant_edge_true_when_transitive_path_exists():
    edges = [("A", "B"), ("B", "C"), ("A", "C")]
    # A->C is redundant because A->B->C already exists
    assert inv.is_redundant_edge(edges, "A", "C") is True


def test_is_redundant_edge_false_for_only_direct_path():
    edges = [("A", "B"), ("A", "C")]
    assert inv.is_redundant_edge(edges, "A", "C") is False


# ------------------------------------------------------------------ commit parse
def test_parse_commits_splits_on_semicolon_and_strips():
    assert inv.parse_commits("a1b2 ; c3d4;") == ["a1b2", "c3d4"]
    assert inv.parse_commits("") == []
    assert inv.parse_commits(None) == []


# --------------------------------------------------------------------- match_sha
def test_match_sha_matches_either_prefix_direction():
    haystack = ["abcdef1234567890", "0000000000000000"]
    assert inv.match_sha("abcdef1", haystack) == ["abcdef1234567890"]
    assert inv.match_sha("abcdef1234567890", ["abcdef1"]) == ["abcdef1"]
    assert inv.match_sha("ffff", haystack) == []


def test_match_sha_reports_ambiguous_prefix():
    haystack = ["abcd1111", "abcd2222"]
    assert len(inv.match_sha("abcd", haystack)) == 2


# ------------------------------------------------------------------ seq anchoring
def test_seq_violations_accepts_contiguous_one_based():
    assert inv.seq_violations([1, 2, 3]) == []


def test_seq_violations_flags_gap_and_repeat_and_missing():
    assert inv.seq_violations([1, 3]) != []  # gap (expected 2)
    assert inv.seq_violations([1, 2, 2]) != []  # repeat (expected 3)
    assert inv.seq_violations([2, 3]) != []  # not 1-based
    assert inv.seq_violations([1, None, 3]) != []  # missing seq value


# ----------------------------------------------------------- commit conservation
def test_commit_conservation_clean_when_all_before_present_after():
    before = {"M1": ["a", "b"], "M2": ["c"]}
    after = {"M1": ["a", "b"], "M2": ["c"]}
    result = inv.commit_conservation(before, after, declared_drops=[])
    assert result.undeclared_lost == []
    assert result.duplicated == []


def test_commit_conservation_flags_undeclared_loss():
    before = {"M1": ["a", "b"], "M2": ["c"]}
    after = {"M1": ["a"], "M2": ["c"]}  # 'b' vanished, not declared
    result = inv.commit_conservation(before, after, declared_drops=[])
    assert result.undeclared_lost == ["b"]


def test_commit_conservation_allows_declared_drop():
    before = {"M1": ["a", "b"], "M2": ["c"]}
    after = {"M1": ["a"], "M2": ["c"]}
    result = inv.commit_conservation(before, after, declared_drops=["b"])
    assert result.undeclared_lost == []


def test_commit_conservation_detects_duplication_across_milestones():
    before = {"M1": ["a"], "M2": ["b"]}
    after = {"M1": ["a", "b"], "M2": ["b"]}  # 'b' now owned twice (reassign bug)
    result = inv.commit_conservation(before, after, declared_drops=[])
    assert result.duplicated == ["b"]


def test_commit_conservation_matches_sha_prefix_for_presence():
    before = {"M1": ["abcdef1234567890"]}
    after = {"M1": ["abcdef1"]}  # stored short after edit; still the same commit
    result = inv.commit_conservation(before, after, declared_drops=[])
    assert result.undeclared_lost == []
