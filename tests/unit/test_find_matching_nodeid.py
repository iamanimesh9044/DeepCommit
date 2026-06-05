"""Characterization tests for ``find_matching_nodeid``.

Target under test:
    deepcommit.harness.prepare_images.collect_milestone_patched_tests.find_matching_nodeid

``find_matching_nodeid(test_id, collected_nodeids: set, normalized_map=None)``
resolves a statically-extracted ``test_id`` to the concrete runtime nodeid in
``collected_nodeids``. Its result feeds ``H_patched_completeness``: a *wrong*
match silently inflates that metric, and a *missed* match silently deflates it.
So the high-value asserts here are not just "each strategy matches its own
shape" but the adversarial guarantees:

  * precedence  -- an exact id beats any fuzzy/suffix/class-insertion candidate
  * no-over-match -- a near-miss returns ``None``, never a wrong nodeid
  * shortest-wins -- ambiguous class/ginkgo/playwright/jest groups pick the
    closest (shortest) candidate deterministically

Determinism note: ``collected_nodeids`` is a ``set`` and several strategies
iterate it and return the *first* hit (S1b parametrize-prefix, S2 JUnit5, S3/S4
suffix, S4 last-component). With two or more candidates that order is
hash-seed dependent. Every test below that exercises such a strategy uses a
*single* legitimate candidate so the result is well-defined. The
``min(..., key=len)`` strategies (S4b, S6, S8, playwright, jest) are only fed
candidates of *distinct* lengths so the shortest-wins tie-break is unambiguous.
All expected values were ground-truthed by executing the real function.
"""

from deepcommit.harness.prepare_images.collect_milestone_patched_tests import (
    _convert_template_to_regex,
    find_jest_template_match,
    find_matching_nodeid,
    find_playwright_match,
)


def m(test_id, *collected, **kw):
    """Call the target with ``collected`` packed into a set."""
    return find_matching_nodeid(test_id, set(collected), **kw)


# ---------------------------------------------------------------------------
# Per-strategy: each strategy resolves its own canonical shape.
# Strategy numbering is loose; we assert the RETURNED nodeid, not which branch
# fired.
# ---------------------------------------------------------------------------


def test_s1_exact_match():
    # Exact id present -> returned verbatim, ignoring the decoy.
    assert m("a::b", "a::b", "x") == "a::b"


def test_s1a_testbed_prefix():
    # Runtime nodeid carries the Docker /testbed/ prefix; the prefixed id wins.
    assert m("test/foo.tsx::T > should", "/testbed/test/foo.tsx::T > should") == "/testbed/test/foo.tsx::T > should"


def test_s1c_jest_space_separator_normalization():
    # Jest reporter flattens " > " to a single space; with /testbed/ prefix too.
    assert m("f.tsx::describe > it > name", "/testbed/f.tsx::describe it name") == "/testbed/f.tsx::describe it name"


def test_s1c_jest_space_separator_without_testbed_prefix():
    # Same normalization, but the runtime nodeid has no /testbed/ prefix.
    assert m("f.tsx::a > b", "f.tsx::a b") == "f.tsx::a b"


def test_s1b_pytest_parametrize_prefix():
    # nodeid is the base id plus a "[...]" parametrization suffix.
    assert m("m.py::test_x", "m.py::test_x[42-Foo-0.85]") == "m.py::test_x[42-Foo-0.85]"


def test_s2_junit5_param_suffix_paren_and_bracket():
    # JUnit5 "(ParamType)[index]" suffix is stripped before comparison.
    assert m("mod::pkg.C::meth", "mod::pkg.C::meth(ParamType)[1]") == "mod::pkg.C::meth(ParamType)[1]"


def test_s2_junit5_param_suffix_paren_only():
    assert m("mod::C::m", "mod::C::m(int)") == "mod::C::m(int)"


def test_s2_junit5_param_suffix_bracket_only():
    # A bare "[1]" bracket suffix is also stripped (no parametrize-prefix decoy).
    assert m("mod::C::m", "mod::C::m[1]") == "mod::C::m[1]"


def test_s3_rust_double_colon_suffix():
    # Rust style: nodeid ends with "::<test_id>".
    assert m("r3127_foo", "regression::r3127_foo") == "regression::r3127_foo"


def test_s4_python_dot_suffix():
    # Python style: nodeid ends with ".<test_id>".
    assert m("test_func", "test_module.test_func") == "test_module.test_func"


def test_s3_double_colon_suffix_for_pathlike_nodeid():
    # "path/to/test::test_name" ends with "::test_name", so the "::" suffix
    # strategy resolves it (before the last-component fallback ever runs).
    assert m("test_name", "path/to/test::test_name") == "path/to/test::test_name"


def test_s4_last_component_pure_slash_separator():
    # No "::"/"." suffix: only a "/" separates the leaf, so the last-component
    # fallback is what resolves it.
    assert m("test_name", "path/test_name") == "path/test_name"


def test_s4b_pytest_class_insertion():
    # Static extractor dropped the enclosing class; nodeid inserts it.
    assert (
        m("tests/w.py::test_render", "tests/w.py::ColorInputTest::test_render")
        == "tests/w.py::ColorInputTest::test_render"
    )


def test_s4b_class_insertion_shortest_candidate_wins():
    # Two valid class-insertion candidates -> the closest (shortest) is chosen.
    assert m("f.py::test_x", "f.py::A::test_x", "f.py::A::B::test_x") == "f.py::A::test_x"


def test_s4b_class_insertion_three_candidates_shortest_wins():
    assert m("f.py::t", "f.py::A::t", "f.py::A::B::t", "f.py::A::B::C::t") == "f.py::A::t"


def test_s5_ginkgo_hierarchical_missing_middle_containers():
    # Package + first container + leaf match; intermediate levels may be missing.
    assert (
        m("pkg::TopLevel > LeafTest", "pkg::TopLevel > Middle1 > Middle2 > LeafTest")
        == "pkg::TopLevel > Middle1 > Middle2 > LeafTest"
    )


def test_s6_ginkgo_relaxed_first_container_differs():
    # First container name differs, but the container set is a subset and the
    # leaf matches -> relaxed ginkgo match.
    assert m("pkg::ErrA > leaf", "pkg::Scan > ErrA > x > leaf") == ("pkg::Scan > ErrA > x > leaf")


def test_s6_ginkgo_relaxed_multiple_candidates_shortest_wins():
    # Two relaxed candidates of distinct length -> shortest.
    assert m("pkg::C > leaf", "pkg::X > C > leaf", "pkg::X > C > extra > leaf") == "pkg::X > C > leaf"


def test_s7_truncated_trailing_backslash_single_candidate():
    # test_id was truncated at an escaped quote -> match the unique prefix.
    assert m("pkg::helpers > returns \\", "pkg::helpers > returns clean when") == "pkg::helpers > returns clean when"


def test_s8_ginkgo_short_format_package_suffix_long_leaf():
    # Short "pkg::It::<leaf>" id; nodeid has a fully-qualified package path. Leaf
    # length > 20 so multi-candidate shortest-wins is permitted; here single.
    leaf = "should allow headless processes apply filters here ok"
    assert (
        m("persistence::It::" + leaf, "github.com/x/persistence::Artist > foo > " + leaf)
        == "github.com/x/persistence::Artist > foo > " + leaf
    )


def test_s8_ginkgo_short_format_single_candidate_short_leaf():
    # A single candidate is returned even when the leaf is short (<= 20 chars):
    # the length guard only restricts the MULTI-candidate shortest-wins path.
    assert m("persistence::It::short", "github.com/x/persistence::A > short") == "github.com/x/persistence::A > short"


def test_s9_normalized_map_direct_key_hit():
    # Go random-subtest id: test_id IS a normalized_map key; return the member.
    nm = {"github.com/x/collection/TestSafeMap": {"github.com/x/collection/TestSafeMap/eJXQ3n3q"}}
    assert (
        m("TestSafeMap", "github.com/x/collection/TestSafeMap/eJXQ3n3q", normalized_map=nm)
        == "github.com/x/collection/TestSafeMap/eJXQ3n3q"
    )


def test_s9_normalized_map_testid_needs_normalization():
    # Case 2: test_id itself carries a random suffix; normalizing it lands on a
    # key whose group resolves the runtime nodeid.
    nm = {"github.com/x/Test": {"github.com/x/Test/aaaaaaaa"}}
    assert (
        m("github.com/x/Test/bbbbbbbb", "github.com/x/Test/aaaaaaaa", normalized_map=nm) == "github.com/x/Test/aaaaaaaa"
    )


def test_s9_normalized_map_suffix_key_match():
    # Case 3: test_id is a short leaf; a key ends with "/<test_id>".
    nm = {"github.com/x/collection/TestSafeMap": {"github.com/x/collection/TestSafeMap/run1"}}
    assert m("TestSafeMap", "unrelated", normalized_map=nm) == ("github.com/x/collection/TestSafeMap/run1")


def test_s9_normalized_map_double_colon_suffix_key():
    # Case 3: a "::"-style key ending with "::<test_id>" resolves via the
    # suffix check (before the last-component fallback runs).
    nm = {"a::b::leaf": {"a::b::leaf::orig"}}
    assert m("leaf", "unrelated", normalized_map=nm) == "a::b::leaf::orig"


def test_s9_normalized_map_last_component_fallback():
    # Case 3 fallback: a separator-less key equals test_id only after the
    # last-component split (no "/" or "::" suffix), so 1071-1073 resolves it.
    nm = {"leaf": {"leaf/orig"}}
    assert m("leaf", "unrelated", normalized_map=nm) == "leaf/orig"


def test_s10_jest_template_dollar_placeholder():
    # ${name} template placeholder matched against a concrete reporter nodeid.
    assert (
        m("file.tsx::Describe > test: ${name}", "file.tsx::Describe > test: Unread")
        == "file.tsx::Describe > test: Unread"
    )


def test_s10_jest_template_printf_placeholder():
    # %s printf-style placeholder from describe.each().
    assert (
        m("file.tsx::Describe > For filter: %s", "file.tsx::Describe > For filter: Mentions")
        == "file.tsx::Describe > For filter: Mentions"
    )


def test_s11_playwright_e2e_match():
    # Playwright: strip playwright/e2e/ prefix + [Browser] suffix, match leaf.
    test_id = "playwright/e2e/right-panel/right-panel.spec.ts::should handle reporting a room"
    nodeid = (
        "right-panel/right-panel.spec.ts::right-panel/right-panel.spec.ts > "
        "RightPanel > should handle reporting a room [Chrome]"
    )
    assert m(test_id, nodeid) == nodeid


def test_s11_playwright_multiple_candidates_shortest_wins():
    # Two playwright candidates of distinct length -> shortest.
    test_id = "playwright/e2e/a.spec.ts::leaf"
    short = "a.spec.ts::a.spec.ts > D > leaf [Chrome]"
    long = "a.spec.ts::a.spec.ts > D > E > leaf [Chrome]"
    assert m(test_id, short, long) == short


# ---------------------------------------------------------------------------
# Precedence: an exact id outranks every fuzzy/suffix/class candidate.
# ---------------------------------------------------------------------------


def test_exact_match_beats_suffix_superset():
    # Both an exact id and a longer "::"-suffix superset exist; exact wins.
    assert m("a::b", "a::b", "pkg::a::b") == "a::b"


def test_exact_match_beats_class_insertion():
    # Exact id present alongside a class-inserted variant; exact wins.
    assert m("f.py::A::t", "f.py::A::t", "f.py::A::B::t") == "f.py::A::t"


def test_parametrize_prefix_beats_junit5_suffix():
    # For "m.py::t" with candidate "m.py::t[1]" the parametrize-prefix strategy
    # fires before the JUnit5 strategy; both would return the same nodeid here,
    # so this pins the resolved value rather than the firing branch.
    assert m("m.py::t", "m.py::t[1]") == "m.py::t[1]"


# ---------------------------------------------------------------------------
# No-over-match: near-misses MUST return None, never a wrong nodeid. These are
# the asserts that keep H_patched_completeness from silent inflation.
# ---------------------------------------------------------------------------


def test_parametrize_prefix_requires_bracket_not_substring():
    # "m.py::test" is a prefix of "m.py::test_extra", but the next char is not
    # '[' so it is not a parametrization, and nothing else legitimately matches.
    assert m("m.py::test", "m.py::test_extra") is None


def test_parametrize_prefix_underscore_continuation_no_overmatch():
    # "m.py::test_x" is a prefix of "m.py::test_x_long" (next char '_', not '[').
    assert m("m.py::test_x", "m.py::test_x_long") is None


def test_junit5_nonempty_residual_after_strip_no_overmatch():
    # Stripping the param suffix from "mod::C::mX" leaves "mod::C::mX" which is
    # not equal to "mod::C::m" -> no match.
    assert m("mod::C::m", "mod::C::mX") is None


def test_rust_suffix_requires_full_double_colon_boundary():
    # endswith("::foo") is False for "...::barfoo": the "::" boundary guards it.
    assert m("foo", "regression::barfoo") is None


def test_rust_suffix_not_a_bare_substring():
    # "a::foobar" contains "foo" but does not end with "::foo".
    assert m("foo", "a::foobar") is None


def test_python_dot_suffix_requires_dot_boundary():
    # "mod.xfunc" does not end with ".func".
    assert m("func", "mod.xfunc") is None


def test_last_component_must_equal_not_contain():
    # parts[-1] is "other_name", not "name".
    assert m("name", "path/to/test::other_name") is None


def test_class_insertion_does_not_cross_files():
    # Right leaf, wrong file -> class-insertion must not reach across files.
    assert m("f.py::test_x", "g.py::A::test_x") is None


def test_s8_short_leaf_multiple_candidates_returns_none():
    # Short leaf (<= 20 chars) with MULTIPLE package-suffix candidates: the
    # length guard suppresses the ambiguous shortest-wins -> None (no over-match).
    assert (
        m(
            "persistence::It::shortleaf",
            "github.com/x/persistence::A > shortleaf",
            "github.com/x/persistence::B > shortleaf",
        )
        is None
    )


# ---------------------------------------------------------------------------
# Degenerate / empty inputs.
# ---------------------------------------------------------------------------


def test_empty_collected_set_returns_none():
    assert m("a::b") is None


def test_no_match_returns_none():
    assert m("totally_absent", "a::b", "c::d") is None


def test_empty_test_id_exact_match():
    # An empty test_id present in the set is an exact hit and returns "".
    assert m("", "") == ""


def test_empty_test_id_absent_returns_none():
    assert m("", "a::b") is None


def test_normalized_map_none_default_does_not_crash():
    # Default normalized_map=None: strategies 9 are skipped, fall through to None.
    assert m("Leaf", "x/y/Other") is None


# ---------------------------------------------------------------------------
# Branch coverage: skip-decoys inside the ginkgo / short-format / E2E loops.
# Each test mixes one legitimate match with decoys that exercise the per-nodeid
# guard (`continue`) branches, proving a decoy does not derail the real match.
# ---------------------------------------------------------------------------


def test_s5_ginkgo_skips_decoys_and_finds_real_match():
    # Decoys: no "::"/" > " separators, and a wrong-package nodeid. The real
    # candidate (matching package + first container + leaf) still resolves.
    assert (
        m(
            "pkg::TopLevel > LeafTest",
            "pkg::TopLevel > Mid > LeafTest",
            "plainnodeid",
            "other::TopLevel > LeafTest",
        )
        == "pkg::TopLevel > Mid > LeafTest"
    )


def test_s6_ginkgo_relaxed_skips_decoys_and_finds_real_match():
    # First container differs (defeats S5), so S6 runs. Decoys: no separators,
    # wrong package. The subset+leaf candidate wins.
    assert (
        m(
            "pkg::C > leaf",
            "pkg::X > C > leaf",
            "nocolons",
            "wrong::X > C > leaf",
        )
        == "pkg::X > C > leaf"
    )


def test_s7_truncated_backslash_skips_colonless_decoy():
    # Truncated (trailing backslash) id; a colon-less decoy is skipped, the
    # unique prefix candidate resolves.
    assert m("pkg::helpers > x \\", "pkg::helpers > x clean", "nocolonshere") == "pkg::helpers > x clean"


def test_s8_short_format_skips_colonless_decoy():
    # Short "pkg::It::leaf" id; a colon-less decoy is skipped, single real
    # candidate resolves even with a short leaf.
    assert (
        m(
            "persistence::It::short",
            "github.com/x/persistence::A > short",
            "no_colon_node",
        )
        == "github.com/x/persistence::A > short"
    )


def test_s8_short_format_long_leaf_multiple_candidates_shortest_wins():
    # Long leaf (> 20 chars) unlocks the multi-candidate shortest-wins path.
    # Candidates of distinct length keep the min() tie-break deterministic.
    leaf = "should allow headless apply filters here okay"
    short = "github.com/x/persistence::A > " + leaf
    long = "github.com/x/persistence::A > B > " + leaf
    assert m("persistence::It::" + leaf, short, long) == short


def test_s9_normalized_map_case1_true_direct_key_no_earlier_strategy():
    # test_id IS a full normalized key, and no earlier strategy can match the
    # collected nodeids -> Case 1 returns the group member directly.
    nm = {"github.com/x/coll/TestSafeMap": {"github.com/x/coll/TestSafeMap/run1"}}
    assert (
        m("github.com/x/coll/TestSafeMap", "totally/unrelated::nope", normalized_map=nm)
        == "github.com/x/coll/TestSafeMap/run1"
    )


def test_playwright_skips_decoys_and_finds_real_match():
    # E2E: a colon-less decoy (1145 continue) and a wrong-path decoy (1159
    # continue) are skipped; the matching spec resolves.
    test_id = "playwright/e2e/a.spec.ts::leaf"
    real = "a.spec.ts::a.spec.ts > D > leaf [Chrome]"
    assert m(test_id, real, "no_colon_pw", "other.spec.ts::other > leaf [Chrome]") == real


def test_jest_template_without_file_part_matches_hierarchy_only():
    # test_id has no "::" -> file_part == "" -> the jest matcher builds a
    # file-less pattern and matches on hierarchy alone.
    assert m("Describe > val ${name}", "Describe > val Unread") == "Describe > val Unread"


# ---------------------------------------------------------------------------
# ADVERSARIAL AUGMENTATION (reviewer pass).
# Deeper branch reach, regex-escape no-over-match, direct helper-function
# coverage, and degenerate inputs that the author block did not pin.
# Every expected value below was ground-truthed by executing the real
# function and re-checked stable across PYTHONHASHSEED in {0,1,2,3}; each
# set-iterating / shortest-wins strategy is fed a SINGLE legitimate
# candidate (or distinct-length candidates) so the result is well-defined.
# ---------------------------------------------------------------------------


# --- Line 922 reach: S5 node_parts < 2 `continue` (the author marked this
#     dead; it is in fact reachable by putting " > " in the PACKAGE part so the
#     node passes the line-911 `" > " in nodeid` guard, but node_hierarchy
#     after "::" splits to a single part). ---------------------------------


def test_s5_node_hierarchy_single_part_continue_then_real_match():
    # test_id package itself contains " > " ("a > b"); the decoy nodeid shares
    # that package but its post-"::" hierarchy has no " > " (-> node_parts < 2,
    # line-922 continue), while the real candidate still resolves via S5.
    assert (
        m(
            "a > b::TopLevel > LeafTest",
            "a > b::soloLeaf",  # passes 911 (" > " in pkg) but node_parts<2 -> 922 continue
            "a > b::TopLevel > Mid > LeafTest",  # real S5 match
        )
        == "a > b::TopLevel > Mid > LeafTest"
    )


def test_s5_node_hierarchy_single_part_decoy_only_returns_none():
    # Only the line-922 decoy is present (no real candidate anywhere) -> None.
    # Guards against the continue accidentally returning the decoy.
    assert m("a > b::TopLevel > LeafTest", "a > b::soloLeaf") is None


# --- S6 subset semantics: a leaf-matching candidate whose container set is NOT
#     a superset of the test containers must be rejected. We isolate S6 by
#     giving a DIFFERENT package suffix so S8 (package-suffix + leaf) cannot
#     fire either -> the only honest answer is None. -----------------------


def test_s6_container_not_subset_and_pkg_suffix_differs_returns_none():
    # test containers {"MustHave"} are not a subset of node containers {"Other"};
    # packages also differ ("pkgA" vs "pkgB") so S8 cannot rescue it -> None.
    assert m("pkgA::MustHave > leaf", "pkgB::Other > leaf") is None


# --- S8 ignores container hierarchy (package-suffix + leaf only). Pin this
#     explicitly: a single short-leaf candidate matches even when the
#     intermediate container differs, but a package-suffix MISMATCH is None. -


def test_s8_matches_on_pkg_suffix_and_leaf_ignoring_containers():
    # Container differs ("MustHave" vs "Other") yet package suffix ("pkg") and
    # leaf ("leaf") match with a single candidate -> S8 resolves it.
    assert m("pkg::MustHave > leaf", "pkg::Other > leaf") == "pkg::Other > leaf"


def test_s8_pkg_suffix_mismatch_long_leaf_returns_none():
    # Long leaf (> 20) but the package suffix differs ("persistence" vs
    # "OTHER") -> no candidate, None (no over-match on leaf alone).
    leaf = "this is a sufficiently long leaf name over twenty chars"
    assert m("persistence::It::" + leaf, "github.com/x/OTHER::Artist > " + leaf) is None


def test_s8_slash_qualified_node_package_suffix_match():
    # node_package has a "/"-qualified path; S8 compares only the last segment
    # ("persistence") against the bare test package suffix. Long leaf permits
    # the path; single candidate keeps it deterministic.
    leaf = "this is a sufficiently long leaf name over twenty chars"
    node = "github.com/navidrome/navidrome/persistence::Artist > " + leaf
    assert m("persistence::It::" + leaf, node) == node


# --- Jest template matcher: second-separator (simple space) branch, /testbed/
#     file-prefix branch, regex-escaping no-over-match, and no-match -> None. -


def test_jest_template_simple_space_separator_branch():
    # Jest reporter flattens " > " to a single space; the " > " separator regex
    # fails to match, so the simple-space separator (2nd entry) resolves it.
    assert m("f.tsx::D > test: ${name}", "f.tsx::D test: Unread") == "f.tsx::D test: Unread"


def test_jest_template_testbed_file_prefix_branch():
    # file_pattern allows an optional /testbed/ prefix on the spec path.
    assert m("f.tsx::D > v ${x}", "/testbed/f.tsx::D > v hello") == "/testbed/f.tsx::D > v hello"


def test_jest_template_escapes_regex_specials_in_literals():
    # Literal "." and "+" in the test name are re.escape'd, so they match
    # themselves -- not "any char" / "one-or-more". A concrete reporter line
    # with the same literals + a placeholder value resolves.
    assert m("f.tsx::v1.0+build ${x}", "f.tsx::v1.0+build ZZ") == "f.tsx::v1.0+build ZZ"


def test_jest_template_dot_is_literal_not_wildcard_no_overmatch():
    # The "." in literal "v1.0" must NOT act as a regex wildcard: "vX0" differs
    # at the literal char, so there is no match -> None (no over-match).
    assert m("f.tsx::v1.0 ${x}", "f.tsx::vX0 ZZ") is None


def test_jest_template_no_match_returns_none():
    # Placeholder present but no nodeid satisfies the literal segments -> None.
    assert m("f.tsx::D > ${x}", "g.tsx::OTHER > zzz") is None


# --- _convert_template_to_regex: exact output shape (the helper that powers
#     S10). Pins both placeholder kinds and the literal-escaping contract. --


def test_convert_template_dollar_placeholder_exact_shape():
    assert _convert_template_to_regex("should work: ${name}") == r"should\ work:\ [^>]+?"


def test_convert_template_printf_placeholder_exact_shape():
    assert _convert_template_to_regex("For filter: %s") == r"For\ filter:\ [^>]+?"


def test_convert_template_escapes_regex_specials_in_literal():
    # Regex metacharacters "." and "+" in the literal run are escaped; the
    # placeholder becomes the non-separator wildcard.
    assert _convert_template_to_regex("a.b+c ${x}") == r"a\.b\+c\ [^>]+?"


def test_convert_template_pure_literal_no_placeholder():
    # No placeholder at all -> the whole segment is escaped literal text.
    assert _convert_template_to_regex("plain text") == r"plain\ text"


# --- find_jest_template_match called directly (its own degenerate inputs). --


def test_find_jest_template_match_fileless_hierarchy_only():
    assert find_jest_template_match("Describe > val ${name}", {"Describe > val Unread"}) == "Describe > val Unread"


def test_find_jest_template_match_no_candidate_returns_none():
    assert find_jest_template_match("D > ${x}", {"E > zzz"}) is None


def test_find_jest_template_match_literal_only_pattern():
    # A test_id with no placeholder still compiles to a literal regex and can
    # match an identical nodeid (callable directly, bypassing the S10 guard).
    assert find_jest_template_match("D > exact", {"D > exact"}) == "D > exact"


# --- find_playwright_match called directly: degenerate / rejection branches. -


def test_find_playwright_match_no_double_colon_returns_none():
    # test_id without "::" -> early None (line ~1115).
    assert find_playwright_match("nocolon", {"a.spec.ts::a.spec.ts > leaf [Chrome]"}) is None


def test_find_playwright_match_non_playwright_path_returns_none():
    # File path is neither playwright/e2e/ nor *.spec.{ts,js} -> None.
    assert find_playwright_match("src/foo.py::leaf", {"src/foo.py::leaf [Chrome]"}) is None


def test_find_playwright_match_leaf_name_differs_returns_none():
    # Path matches and browser suffix strips, but the leaf test names differ
    # ("leafX" vs "leafY") -> None (no over-match on the spec path alone).
    assert m("playwright/e2e/a.spec.ts::leafX", "a.spec.ts::a.spec.ts > D > leafY [Chrome]") is None


def test_find_playwright_match_spec_js_extension_resolves():
    # The *.spec.js branch of is_playwright_path (not just *.spec.ts).
    test_id = "playwright/e2e/a.spec.js::leaf"
    node = "a.spec.js::a.spec.js > D > leaf [firefox]"
    assert m(test_id, node) == node


# --- S9 normalized-map: degenerate / no-over-match around the map branch. ---


def test_s9_normalized_map_empty_dict_falls_through_to_none():
    # An empty normalized_map is falsy -> the whole S9 block is skipped and no
    # later strategy matches -> None.
    assert m("TestSafeMap", "x/y/Other", normalized_map={}) is None


def test_s9_normalized_map_no_key_matches_returns_none():
    # Non-empty map but neither a direct key, a normalized key, nor a suffix
    # key matches "absent" -> None (does not leak an arbitrary group member).
    nm = {"github.com/x/coll/TestSafeMap": {"github.com/x/coll/TestSafeMap/run1"}}
    assert m("absent", "unrelated", normalized_map=nm) is None
