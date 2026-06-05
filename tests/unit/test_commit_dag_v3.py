"""Characterization tests for ``commit_dag_v3`` pure functions.

Targets:
  * ``parse_unified_diff`` -- ``git show --unified=0 -M -C`` text -> ``FileDiff``/``Hunk``.
  * ``topological_levels`` -- layering of a commit DAG + stuck-set (cycle) detection.

Every expected value below was ground-truthed by executing the function; the
assertions pin exact returned shapes (paths, hunk fields, line lists, level maps,
cycle lists) rather than truthiness. Set/dict iteration order is never asserted
raw -- dict equality is order-independent and the cycle-node ordering that *is*
asserted is derived from the input ``nodes`` list order, which is deterministic.
"""

from deepcommit.preprocess.commit_dag.commit_dag_v3 import (
    Edge,
    EdgeMetadata,
    FileDiff,
    Hunk,
    parse_unified_diff,
    topological_levels,
)


def _E(s, d):
    """Build an Edge with the required (no-default) EdgeMetadata field."""
    return Edge(src=s, dst=d, kind="blame_only", metadata=EdgeMetadata())


# --------------------------- dataclass contracts --------------------------- #


def test_dataclass_field_order_and_defaults():
    """Pin the dataclass constructor contract the parser/topo helpers rely on."""
    h = Hunk(1, 2, 3, 4)
    assert (h.old_start, h.old_count, h.new_start, h.new_count) == (1, 2, 3, 4)
    assert h.added_lines == [] and h.removed_lines == []

    fd = FileDiff(old_path="o", new_path="n")
    assert fd.hunks == []

    meta = EdgeMetadata()
    assert meta.file_ranges == {} and meta.verified is False

    e = _E("A", "B")
    assert (e.src, e.dst, e.kind) == ("A", "B", "blame_only")
    assert isinstance(e.metadata, EdgeMetadata) and e.types == []


def test_edge_create_classmethod_matches_constructor():
    meta = EdgeMetadata()
    e = Edge.create("A", "B", "blame_verified", meta)
    assert (e.src, e.dst, e.kind, e.metadata) == ("A", "B", "blame_verified", meta)
    assert e.types == []


# ----------------------------- parse_unified_diff -------------------------- #


def test_parse_modify_hunk():
    diff = (
        "diff --git a/foo.py b/foo.py\nindex 111..222 100644\n"
        "--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n-old line\n+new line\n"
    )
    ds = parse_unified_diff(diff)
    assert ds == [
        FileDiff(
            old_path="foo.py",
            new_path="foo.py",
            hunks=[
                Hunk(
                    old_start=1,
                    old_count=2,
                    new_start=1,
                    new_count=2,
                    added_lines=["new line"],
                    removed_lines=["old line"],
                )
            ],
        )
    ]


def test_parse_added_file_devnull():
    """``--- /dev/null`` => old_path is None (file added); omitted +N count -> 1."""
    diff = "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+hello\n"
    fd = parse_unified_diff(diff)[0]
    assert fd.old_path is None and fd.new_path == "new.py"
    assert fd.hunks == [
        Hunk(
            old_start=0,
            old_count=0,
            new_start=1,
            new_count=1,
            added_lines=["hello"],
            removed_lines=[],
        )
    ]


def test_parse_deleted_file_devnull():
    """``+++ /dev/null`` => new_path is None (file deleted)."""
    diff = (
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
    )
    fd = parse_unified_diff(diff)[0]
    assert fd.old_path == "gone.py" and fd.new_path is None
    assert fd.hunks == [
        Hunk(
            old_start=1,
            old_count=1,
            new_start=0,
            new_count=0,
            added_lines=[],
            removed_lines=["bye"],
        )
    ]


def test_parse_omitted_counts_default_one():
    """``@@ -5 +5 @@`` (no ,count on either side) => both counts default to 1."""
    diff = "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -5 +5 @@\n-x\n+y\n"
    h = parse_unified_diff(diff)[0].hunks[0]
    assert (h.old_start, h.old_count, h.new_start, h.new_count) == (5, 1, 5, 1)
    assert h.added_lines == ["y"] and h.removed_lines == ["x"]


def test_parse_pure_rename_has_paths_no_hunks():
    """`-M` 100%-similarity rename: no ---/+++/@@ lines, paths come from header."""
    diff = (
        "diff --git a/old/name.py b/new/name.py\n"
        "similarity index 100%\nrename from old/name.py\nrename to new/name.py\n"
    )
    fd = parse_unified_diff(diff)[0]
    assert fd.old_path == "old/name.py" and fd.new_path == "new/name.py"
    assert fd.hunks == []


def test_parse_header_only_no_body():
    """A lone ``diff --git`` header still yields a FileDiff with empty hunks."""
    assert parse_unified_diff("diff --git a/only.py b/only.py\n") == [
        FileDiff(old_path="only.py", new_path="only.py", hunks=[])
    ]


def test_parse_multi_file_keeps_each_filediff_separate():
    """Adds for the second file must not bleed into the first's hunk."""
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a0\n+a1\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -2,0 +3,2 @@\n+b1\n+b2\n"
    )
    ds = parse_unified_diff(diff)
    assert [d.new_path for d in ds] == ["a.py", "b.py"]
    assert ds[0].hunks[0].added_lines == ["a1"]
    assert ds[0].hunks[0].removed_lines == ["a0"]
    assert ds[1].hunks[0].added_lines == ["b1", "b2"]
    assert ds[1].hunks[0].removed_lines == []
    assert (ds[1].hunks[0].old_start, ds[1].hunks[0].old_count) == (2, 0)
    assert (ds[1].hunks[0].new_start, ds[1].hunks[0].new_count) == (3, 2)


def test_parse_multiple_hunks_in_one_file():
    diff = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-a\n+b\n@@ -10,2 +10,2 @@\n-c\n+d\n"
    fd = parse_unified_diff(diff)[0]
    assert len(fd.hunks) == 2
    assert (fd.hunks[0].old_start, fd.hunks[0].new_start) == (1, 1)
    assert fd.hunks[0].added_lines == ["b"] and fd.hunks[0].removed_lines == ["a"]
    assert (fd.hunks[1].old_start, fd.hunks[1].old_count) == (10, 2)
    assert fd.hunks[1].added_lines == ["d"] and fd.hunks[1].removed_lines == ["c"]


def test_parse_context_line_is_ignored():
    """A leading-space context line inside a hunk is neither added nor removed."""
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n context\n-removed\n+added\n"
    h = parse_unified_diff(diff)[0].hunks[0]
    assert h.added_lines == ["added"] and h.removed_lines == ["removed"]


def test_parse_triple_plus_content_line_not_treated_as_added():
    """Adversarial: a content line ``+++newval`` starts with ``+`` AND ``+++``.

    The ``not line.startswith("+++")`` guard means it is dropped, not collected
    as an added line -- proving the parser does not over-match real ``+`` adds.
    """
    diff = "diff --git a/y.py b/y.py\n--- a/y.py\n+++ b/y.py\n@@ -1 +1,2 @@\n-z\n+++newval\n"
    h = parse_unified_diff(diff)[0].hunks[0]
    assert h.added_lines == []
    assert h.removed_lines == ["z"]


def test_parse_triple_dash_and_plus_inside_hunk_skipped():
    """``---``/``+++``-prefixed content lines inside a hunk are both skipped."""
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1,3 +1,3 @@\n-removed\n+added\n+++not a header\n---not a header\n"
    )
    h = parse_unified_diff(diff)[0].hunks[0]
    assert h.added_lines == ["added"] and h.removed_lines == ["removed"]


def test_parse_add_remove_before_hunk_ignored():
    """``+``/``-`` lines appearing before the first ``@@`` (in_hunk False) are dropped."""
    diff = "diff --git a/q.py b/q.py\n--- a/q.py\n+++ b/q.py\n+premature add\n@@ -1 +1 @@\n-a\n+b\n"
    fd = parse_unified_diff(diff)[0]
    assert fd.hunks[0].added_lines == ["b"]
    assert fd.hunks[0].removed_lines == ["a"]


def test_parse_empty_returns_empty_list():
    assert parse_unified_diff("") == []


def test_parse_garbage_without_header_returns_empty_list():
    """No ``diff --git`` header => cur stays None => nothing collected."""
    assert parse_unified_diff("random text\nno header\n") == []


def test_parse_orphan_body_without_header_ignored():
    """---/+++/@@/content with no preceding header produce no FileDiff at all."""
    diff = "--- a/orphan.py\n+++ b/orphan.py\n@@ -1 +1 @@\n-x\n+y\n"
    assert parse_unified_diff(diff) == []


# ----------------------------- topological_levels -------------------------- #


def test_topo_empty():
    assert topological_levels([], []) == ({}, [])


def test_topo_chain():
    level, cycles = topological_levels(["A", "B", "C"], [_E("A", "B"), _E("B", "C")])
    assert level == {"A": 0, "B": 1, "C": 2}
    assert cycles == []


def test_topo_diamond():
    level, cycles = topological_levels(
        ["A", "B", "C", "D"],
        [_E("A", "B"), _E("A", "C"), _E("B", "D"), _E("C", "D")],
    )
    assert level == {"A": 0, "B": 1, "C": 1, "D": 2}
    assert cycles == []


def test_topo_isolated_node_is_level_zero():
    level, cycles = topological_levels(["A", "B", "C"], [_E("A", "B")])
    assert level == {"A": 0, "B": 1, "C": 0}
    assert cycles == []


def test_topo_all_isolated():
    level, cycles = topological_levels(["A", "B", "C"], [])
    assert level == {"A": 0, "B": 0, "C": 0}
    assert cycles == []


def test_topo_two_cycle_returns_stuck_nodeset():
    level, cycles = topological_levels(["A", "B"], [_E("A", "B"), _E("B", "A")])
    assert level == {"A": 0, "B": 0}
    assert cycles == [["A", "B"]]


def test_topo_three_cycle_stuck_set_in_node_order():
    """Cycle node order follows the input ``nodes`` list, not the edge order."""
    level, cycles = topological_levels(["A", "B", "C"], [_E("C", "A"), _E("A", "B"), _E("B", "C")])
    assert level == {"A": 0, "B": 0, "C": 0}
    assert cycles == [["A", "B", "C"]]


def test_topo_partial_cycle_only_stuck_nodes_reported():
    """A reaches a node feeding a B<->C cycle: only B,C are stuck."""
    level, cycles = topological_levels(["A", "B", "C"], [_E("A", "B"), _E("B", "C"), _E("C", "B")])
    assert level == {"A": 0, "B": 0, "C": 0}
    assert cycles == [["B", "C"]]


def test_topo_self_loop_drags_dependent_into_stuck_set():
    """Adversarial: ``A->A`` self-loop keeps indeg(A)>0 so A never frees B."""
    level, cycles = topological_levels(["A", "B"], [_E("A", "A"), _E("A", "B")])
    assert level == {"A": 0, "B": 0}
    assert cycles == [["A", "B"]]


def test_topo_unknown_dst_edge_ignored():
    """An edge to a node not in ``nodes`` is dropped (no KeyError, no level)."""
    level, cycles = topological_levels(["A", "B"], [_E("A", "B"), _E("B", "Z")])
    assert level == {"A": 0, "B": 1}
    assert "Z" not in level
    assert cycles == []


def test_topo_unknown_src_edge_ignored():
    level, cycles = topological_levels(["A", "B"], [_E("Z", "A"), _E("A", "B")])
    assert level == {"A": 0, "B": 1}
    assert cycles == []


def test_topo_duplicate_edges_still_resolve():
    """Adversarial: a doubled ``A->B`` edge inflates indeg(B) to 2 but B still frees."""
    level, cycles = topological_levels(["A", "B"], [_E("A", "B"), _E("A", "B")])
    assert level == {"A": 0, "B": 1}
    assert cycles == []


# ----------------- parse_unified_diff: augmented depth (review) ------------- #


def test_parse_multi_file_exact_shape():
    """Exact-shape equality for the whole two-file parse (stronger than per-field)."""
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a0\n+a1\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -2,0 +3,2 @@\n+b1\n+b2\n"
    )
    assert parse_unified_diff(diff) == [
        FileDiff(
            old_path="a.py",
            new_path="a.py",
            hunks=[
                Hunk(
                    old_start=1,
                    old_count=1,
                    new_start=1,
                    new_count=1,
                    added_lines=["a1"],
                    removed_lines=["a0"],
                )
            ],
        ),
        FileDiff(
            old_path="b.py",
            new_path="b.py",
            hunks=[
                Hunk(
                    old_start=2,
                    old_count=0,
                    new_start=3,
                    new_count=2,
                    added_lines=["b1", "b2"],
                    removed_lines=[],
                )
            ],
        ),
    ]


def test_parse_crlf_line_endings_stripped_from_paths_and_content():
    r"""``splitlines()`` splits on ``\r\n``: no trailing ``\r`` leaks into paths/content."""
    diff = "diff --git a/foo.py b/foo.py\r\n--- a/foo.py\r\n+++ b/foo.py\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n"
    assert parse_unified_diff(diff) == [
        FileDiff(
            old_path="foo.py",
            new_path="foo.py",
            hunks=[
                Hunk(
                    old_start=1,
                    old_count=1,
                    new_start=1,
                    new_count=1,
                    added_lines=["new"],
                    removed_lines=["old"],
                )
            ],
        )
    ]


def test_parse_hunk_with_section_heading_after_at_at():
    """A function-context heading after the closing ``@@`` is ignored (regex unanchored)."""
    diff = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@ def foo():\n-a\n+b\n"
    h = parse_unified_diff(diff)[0].hunks[0]
    assert (h.old_start, h.old_count, h.new_start, h.new_count) == (1, 2, 1, 2)
    assert h.added_lines == ["b"] and h.removed_lines == ["a"]


def test_parse_header_greedy_capture_picks_last_b_slash():
    """Characterization: greedy ``(.+) (.+)`` on ``a/x b/y b/z`` splits at the *last* ``b/``."""
    fd = parse_unified_diff("diff --git a/x b/y b/z\n")[0]
    assert fd.old_path == "x b/y" and fd.new_path == "z"
    assert fd.hunks == []


def test_parse_old_path_keeps_trailing_tab_metadata():
    r"""Characterization: ``--- a/f.py\t(date)`` -> path captures the tab suffix verbatim."""
    diff = "diff --git a/f.py b/f.py\n--- a/f.py\t(date)\n+++ b/f.py\t(date)\n"
    fd = parse_unified_diff(diff)[0]
    assert fd.old_path == "f.py\t(date)"
    assert fd.new_path == "f.py\t(date)"


def test_parse_paths_with_spaces_in_old_and_new_lines():
    """``--- a/old one.py`` / ``+++ b/new one.py`` keep the embedded space."""
    diff = "diff --git a/x b/x\n--- a/old one.py\n+++ b/new one.py\n"
    fd = parse_unified_diff(diff)[0]
    assert fd.old_path == "old one.py" and fd.new_path == "new one.py"


def test_parse_nonmatching_old_path_line_keeps_header_path():
    """``--- garbage`` (no ``a/``, not ``/dev/null``) leaves old_path from the header."""
    diff = "diff --git a/x.py b/x.py\n--- garbage\n@@ -1 +1 @@\n-a\n"
    fd = parse_unified_diff(diff)[0]
    assert fd.old_path == "x.py" and fd.new_path == "x.py"
    assert fd.hunks[0].removed_lines == ["a"]


def test_parse_nonmatching_new_path_line_keeps_header_path():
    """``+++ garbage`` (no ``b/``, not ``/dev/null``) leaves new_path from the header."""
    diff = "diff --git a/x.py b/x.py\n+++ garbage\n@@ -1 +1 @@\n+a\n"
    fd = parse_unified_diff(diff)[0]
    assert fd.old_path == "x.py" and fd.new_path == "x.py"
    assert fd.hunks[0].added_lines == ["a"]


def test_parse_bare_plus_and_minus_collect_empty_strings():
    """A lone ``+`` / ``-`` (empty added/removed content) collects the empty string."""
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+\n-\n"
    h = parse_unified_diff(diff)[0].hunks[0]
    assert h.added_lines == [""] and h.removed_lines == [""]


def test_parse_blank_unprefixed_line_in_hunk_ignored():
    """A fully blank line (no +/-/space prefix) inside a hunk is dropped, not collected."""
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n\n+a\n"
    h = parse_unified_diff(diff)[0].hunks[0]
    assert h.added_lines == ["a"] and h.removed_lines == []


def test_parse_malformed_hunk_header_creates_no_hunk():
    """``@@ -1 +1`` lacks the closing ``@@`` so no Hunk is created; the body is dropped."""
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1\n+a\n"
    assert parse_unified_diff(diff) == [FileDiff(old_path="x.py", new_path="x.py", hunks=[])]


def test_parse_orphan_old_path_line_before_real_header_dropped():
    """A ``--- a/orphan.py`` before any ``diff --git`` is dropped; the real header parses clean."""
    diff = "--- a/orphan.py\ndiff --git a/real.py b/real.py\n@@ -1 +1 @@\n+x\n"
    assert parse_unified_diff(diff) == [
        FileDiff(
            old_path="real.py",
            new_path="real.py",
            hunks=[
                Hunk(
                    old_start=1,
                    old_count=1,
                    new_start=1,
                    new_count=1,
                    added_lines=["x"],
                    removed_lines=[],
                )
            ],
        )
    ]


# ----------------- topological_levels: augmented depth (review) ------------ #


def test_topo_level_is_longest_path_via_last_predecessor():
    """``C`` (preds ``A`` lvl0 and ``B`` lvl1) frees on its *last* pred => level 2, not 1."""
    level, cycles = topological_levels(["A", "B", "C"], [_E("A", "B"), _E("B", "C"), _E("A", "C")])
    assert level == {"A": 0, "B": 1, "C": 2}
    assert cycles == []


def test_topo_long_arm_dominates_short_shortcut():
    """``A->B->C->D`` plus shortcut ``A->D``: D takes the longest path level (3), not 1."""
    level, cycles = topological_levels(
        ["A", "B", "C", "D"],
        [_E("A", "B"), _E("B", "C"), _E("C", "D"), _E("A", "D")],
    )
    assert level == {"A": 0, "B": 1, "C": 2, "D": 3}
    assert cycles == []


def test_topo_duplicate_node_in_nodes_list_deduped():
    """``nodes`` containing ``A`` twice still yields a single ``A`` key (dict dedup)."""
    level, cycles = topological_levels(["A", "A", "B"], [_E("A", "B")])
    assert level == {"A": 0, "B": 1}
    assert cycles == []


def test_topo_single_node_self_loop_is_its_own_cycle():
    """Adversarial: a lone node with ``A->A`` never reaches indeg 0 => stuck set ``[A]``."""
    level, cycles = topological_levels(["A"], [_E("A", "A")])
    assert level == {"A": 0}
    assert cycles == [["A"]]


def test_topo_self_loop_on_unknown_node_dropped():
    """A self-loop ``Z->Z`` where ``Z`` is not in ``nodes`` is dropped: no cycle."""
    level, cycles = topological_levels(["A"], [_E("Z", "Z")])
    assert level == {"A": 0}
    assert cycles == []


def test_topo_disjoint_cycles_merge_into_single_stuck_list():
    """Two independent 2-cycles are reported as ONE combined stuck list, not two."""
    level, cycles = topological_levels(
        ["A", "B", "C", "D"],
        [_E("A", "B"), _E("B", "A"), _E("C", "D"), _E("D", "C")],
    )
    assert level == {"A": 0, "B": 0, "C": 0, "D": 0}
    assert cycles == [["A", "B", "C", "D"]]


def test_topo_acyclic_and_cyclic_components_coexist():
    """An acyclic ``X->Y`` chain levels normally while a separate ``A<->B`` cycle is stuck."""
    level, cycles = topological_levels(
        ["X", "Y", "A", "B"],
        [_E("X", "Y"), _E("A", "B"), _E("B", "A")],
    )
    assert level == {"X": 0, "Y": 1, "A": 0, "B": 0}
    assert cycles == [["A", "B"]]


def test_topo_cycle_stuck_set_order_follows_nodes_list_not_edges():
    """Stuck-set order = ``nodes`` list order (``C,B,A``), independent of edge order."""
    level, cycles = topological_levels(["C", "B", "A"], [_E("A", "B"), _E("B", "C"), _E("C", "A")])
    assert level == {"C": 0, "B": 0, "A": 0}
    assert cycles == [["C", "B", "A"]]


def test_topo_empty_nodes_with_edges_yields_empty_result():
    """Adversarial: edges referencing nonexistent nodes with empty ``nodes`` => ``({}, [])``."""
    assert topological_levels([], [_E("A", "B")]) == ({}, [])
