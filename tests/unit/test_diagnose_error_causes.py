"""Characterization tests for ``diagnose_error_causes`` (error-bucket taxonomy).

The four pure functions under test turn messy real tracebacks into a small
bucket taxonomy that M3 uses to separate ENV-fixable errors from DAG-timing
errors. Every expected value here was ground-truthed by executing the function
against the live source (``deepcommit/harness/main_orch/diagnose_error_causes.py``),
not by static reading.

High-value adversarial asserts:
- ``classify_cause`` first-match-wins ordering: a specific harness artifact must
  win over a generic ``AssertionError`` even when both substrings are present.
- ``_causes_in_text`` / ``cause_from_record`` drop the unittest
  "Failed to import test module" wrapper so the *real* ImportError surfaces.
- ``normalize_cause`` regex ordering: ``commit \\w+`` runs before the digit
  collapse, and quote-collapse runs before the path collapse.
"""

from __future__ import annotations

import pytest

from deepcommit.harness.main_orch.diagnose_error_causes import (
    STARTER,
    Taxonomy,
    cause_from_record,
    causes_from_log,
    classify_cause,
    normalize_cause,
)

# ---------------------------------------------------------------------------
# normalize_cause: collapse value-specifics so equal causes group together.
# ---------------------------------------------------------------------------


def test_normalize_collapses_numbers_quotes_and_paths():
    # Bare integers -> N, single-quoted strings -> 'X', /testbed paths -> <path>.
    assert normalize_cause("AssertionError: expected 5 got 7") == "AssertionError: expected N got N"
    assert normalize_cause("KeyError: 'user_id'") == "KeyError: 'X'"
    assert normalize_cause("ImportError at /testbed/pkg/mod.py now") == "ImportError at <path> now"


def test_normalize_collapses_double_quotes_and_paren_path():
    # Double-quoted strings collapse to "X"; a "(/...path)" parenthetical is dropped entirely.
    assert normalize_cause('double "quoted str" here') == 'double "X" here'
    assert normalize_cause("cleanup (/testbed/foo/bar.py) tail") == "cleanup tail"


def test_normalize_paren_path_only_collapses_to_empty():
    # A lone "(/...)" parenthetical with nothing else collapses + strips to "".
    assert normalize_cause("(/testbed/only/path)") == ""


def test_normalize_collapses_commit_sha_before_digits():
    # "commit \\w+" runs BEFORE the digit collapse, so a numeric sha -> "commit Z", not "commit N".
    assert normalize_cause("error in commit abcdef123 here") == "error in commit Z here"
    assert normalize_cause("at commit 12345 broke") == "at commit Z broke"
    assert normalize_cause("commit ABC_123") == "commit Z"


def test_normalize_word_boundary_digit_collapse():
    # Only standalone integer words become N; digits glued to letters (build3) are untouched.
    assert normalize_cause("v2 release 100 build3") == "v2 release N build3"
    assert normalize_cause("build3") == "build3"


def test_normalize_quote_collapse_precedes_path_and_number():
    # Quote-collapse runs first, so a path or number INSIDE quotes is swallowed by 'X'.
    assert normalize_cause("file '/testbed/a/b.py' missing") == "file 'X' missing"
    assert normalize_cause("got '42' items") == "got 'X' items"


def test_normalize_collapses_whitespace_and_strips():
    assert normalize_cause("   leading   and    spaces   ") == "leading and spaces"


def test_normalize_empty_is_empty():
    assert normalize_cause("") == ""


def test_normalize_truncates_to_110_chars():
    # Truncation is to exactly 110 chars, applied AFTER strip.
    assert len(normalize_cause("E" * 500)) == 110
    out = normalize_cause("  " + "A" * 120 + "   ")
    assert out == "A" * 110
    assert len(out) == 110


def test_normalize_truncation_after_substitution():
    # 108 'x' + " 12345" -> "...x N" then sliced to 110: the trailing N survives at the cut.
    out = normalize_cause("x" * 108 + " 12345")
    assert len(out) == 110
    assert out == "x" * 108 + " N"


def test_normalize_combined_signature():
    # All collapses compose in one signature.
    assert (
        normalize_cause('mixed 12 and "q" and (/testbed/x) and commit deadbeef and /testbed/y/z')
        == 'mixed N and "X" and and commit Z and <path>'
    )


def test_normalize_empty_single_quotes_still_collapse():
    # The quote regex is '[^']*' (zero-or-more), so even empty '' collapses to 'X'.
    assert normalize_cause("empty '' quotes") == "empty 'X' quotes"
    assert normalize_cause('empty "" quotes') == 'empty "X" quotes'


def test_normalize_drops_multiple_paren_paths():
    # Every "(/...path)" parenthetical is dropped, not just the first; surrounding text survives.
    assert normalize_cause("a (/testbed/x) b (/testbed/y) c") == "a b c"


def test_normalize_paren_without_leading_slash_is_kept():
    # The drop rule is "(/...)" specifically; a parenthetical NOT starting with "/" is preserved.
    assert normalize_cause("keep (notapath) here") == "keep (notapath) here"


def test_normalize_commit_matches_as_substring_inside_word():
    # "commit \\w+" is unanchored, so it fires on the "commit abc123" tail INSIDE "precommit ...".
    assert normalize_cause("precommit abc123 done") == "precommit Z done"


def test_normalize_collapses_newlines_and_tabs_as_whitespace():
    # \\s+ collapse folds newlines and tabs, not just spaces, into a single space.
    assert normalize_cause("line1\nline2\tline3") == "line1 line2 line3"


def test_normalize_testbed_path_with_line_number_collapses_whole_token():
    # "/testbed/\\S+" is non-greedy on whitespace only, so a trailing ":123" is part of the token.
    assert normalize_cause("see /testbed/a/b.py:123 line") == "see <path> line"
    # Standalone integers around a path each collapse independently.
    assert normalize_cause("error 5 at /testbed/x.py and 6") == "error N at <path> and N"


# ---------------------------------------------------------------------------
# classify_cause: (bucket, category), first-match-wins ordering.
# ---------------------------------------------------------------------------


def test_classify_returns_exact_two_tuple_shape():
    res = classify_cause("OperationalError: no such table: app_user")
    assert isinstance(res, tuple)
    assert len(res) == 2
    assert res == ("ENV_DB", "ENV_FIXABLE")


def test_classify_env_db_bucket():
    assert classify_cause("OperationalError: no such table: app_user") == ("ENV_DB", "ENV_FIXABLE")
    assert classify_cause("django.db.utils: schema editor failed") == ("ENV_DB", "ENV_FIXABLE")
    assert classify_cause("IntegrityError: dup key") == ("ENV_DB", "ENV_FIXABLE")
    assert classify_cause("NotSupportedError: nope") == ("ENV_DB", "ENV_FIXABLE")
    assert classify_cause("DatabaseError: down") == ("ENV_DB", "ENV_FIXABLE")


def test_classify_env_app_bucket():
    assert classify_cause("AppRegistryNotReady: Apps aren't loaded yet") == ("ENV_APP", "ENV_FIXABLE")
    assert classify_cause("RuntimeError: no installed app named foo") == ("ENV_APP", "ENV_FIXABLE")


def test_classify_timing_post_ms_bucket():
    assert classify_cause("Foo introduced post-milestone") == ("TIMING_POST_MS", "TIMING_DAG")


def test_classify_import_missing_bucket():
    assert classify_cause("ImportError: cannot import name 'Foo'") == ("IMPORT_MISSING", "TIMING_DAG")
    assert classify_cause("ModuleNotFoundError: No module named x") == ("IMPORT_MISSING", "TIMING_DAG")
    assert classify_cause("Failed to import test module") == ("IMPORT_MISSING", "TIMING_DAG")


def test_classify_api_kwarg_bucket():
    assert classify_cause("TypeError: __init__() got an unexpected keyword") == ("API_KWARG", "TIMING_DAG")
    assert classify_cause("TypeError: foo() takes no arguments") == ("API_KWARG", "TIMING_DAG")
    assert classify_cause("TypeError: missing positional argument") == ("API_KWARG", "TIMING_DAG")


def test_classify_api_attr_bucket():
    assert classify_cause("AttributeError: 'M' object has no attribute 'x'") == ("API_ATTR", "TIMING_DAG")


def test_classify_test_assert_bucket():
    assert classify_cause("AssertionError: 1 != 2") == ("TEST_ASSERT", "TEST_FAIL")


def test_classify_unmatched_is_other():
    # A real exception that matches no rule falls through to OTHER/OTHER.
    assert classify_cause("ValueError: something nobody bucketed") == ("OTHER", "OTHER")
    assert classify_cause("") == ("OTHER", "OTHER")


def test_classify_is_case_insensitive():
    # Rule keys are lowercase; the input is lowercased before matching.
    assert classify_cause("NO SUCH TABLE: FOO") == ("ENV_DB", "ENV_FIXABLE")


def test_classify_matches_on_substring_not_word_boundary():
    # Keys are plain substrings: "no module named" glued inside another token still matches.
    assert classify_cause("xxno module namedxx") == ("IMPORT_MISSING", "TIMING_DAG")


def test_classify_whitespace_only_is_other():
    # A whitespace-only / unmatched string falls through to OTHER/OTHER.
    assert classify_cause("   ") == ("OTHER", "OTHER")
    assert classify_cause("ValueError: nothing here matches a rule") == ("OTHER", "OTHER")


def test_classify_env_db_all_six_keys():
    # Every ENV_DB substring key independently maps to (ENV_DB, ENV_FIXABLE).
    for key in (
        "schema editor",
        "operationalerror",
        "notsupportederror",
        "integrityerror",
        "databaseerror",
        "no such table",
    ):
        assert classify_cause(f"prefix {key} suffix") == ("ENV_DB", "ENV_FIXABLE"), key


def test_classify_env_app_all_keys():
    for key in ("no installed app", "aren't loaded", "appregistrynotready", "apps aren't loaded"):
        assert classify_cause(f"prefix {key} suffix") == ("ENV_APP", "ENV_FIXABLE"), key


def test_classify_import_missing_all_three_keys():
    for key in ("cannot import name", "no module named", "failed to import"):
        assert classify_cause(f"prefix {key} suffix") == ("IMPORT_MISSING", "TIMING_DAG"), key


def test_classify_api_kwarg_all_four_keys():
    for key in ("unexpected keyword", "__init__()", "positional argument", "takes no arguments"):
        assert classify_cause(f"prefix {key} suffix") == ("API_KWARG", "TIMING_DAG"), key


def test_classify_api_attr_both_keys():
    # Both "no attribute" and the longer "has no attribute" key resolve to API_ATTR.
    assert classify_cause("xxno attributexx") == ("API_ATTR", "TIMING_DAG")
    assert classify_cause("object has no attribute foo") == ("API_ATTR", "TIMING_DAG")


# --- the high-value ordering / near-miss asserts (specific-rule wins) -------


def test_classify_order_harness_artifact_wins_over_assertion():
    # Matches BOTH a harness-artifact key and assertionerror; HARNESS_ARTIFACT is listed
    # first in STARTER_RULES (specific-first), so it must win.
    sig = "[env-patch] outcome preserved as error; AssertionError: subtest not picklable"
    assert classify_cause(sig) == ("HARNESS_ARTIFACT", "HARNESS_ARTIFACT")


def test_classify_order_harness_artifact_wins_over_import():
    # "not picklable" (HARNESS_ARTIFACT) precedes "failed to import" (IMPORT_MISSING).
    assert classify_cause("Failed to import: subtest not picklable") == (
        "HARNESS_ARTIFACT",
        "HARNESS_ARTIFACT",
    )


def test_classify_order_env_db_wins_over_api_attr():
    # ENV_DB ("integrityerror") precedes API_ATTR ("no attribute").
    assert classify_cause("IntegrityError ... object has no attribute foo") == ("ENV_DB", "ENV_FIXABLE")


def test_classify_order_env_app_wins_over_assertion():
    # ENV_APP ("no installed app") precedes TEST_ASSERT ("assertionerror").
    assert classify_cause("AssertionError: no installed app named foo") == ("ENV_APP", "ENV_FIXABLE")


def test_classify_order_api_kwarg_wins_over_api_attr():
    # API_KWARG ("__init__()") precedes API_ATTR ("no attribute").
    assert classify_cause("__init__() error: object has no attribute z") == ("API_KWARG", "TIMING_DAG")


def test_classify_order_api_attr_wins_over_assertion():
    # API_ATTR ("has no attribute") precedes TEST_ASSERT ("assertionerror").
    assert classify_cause("AssertionError ... has no attribute") == ("API_ATTR", "TIMING_DAG")


# ---------------------------------------------------------------------------
# cause_from_record: deepest (last) real exception across sections, wrappers dropped.
# ---------------------------------------------------------------------------


def test_cause_from_record_returns_last_exception():
    # pytest/unittest print the triggering exception last; the last line wins.
    rec = {"call": {"longrepr": "E   ValueError: early\nE   AssertionError: the real one"}}
    assert cause_from_record(rec) == "AssertionError: the real one"


def test_cause_from_record_drops_unittest_import_wrapper():
    # "Failed to import test module" is the unittest wrapper; the real ImportError follows it.
    rec = {
        "setup": {
            "crash": {
                "message": (
                    "ImportError: Failed to import test module: pkg\nModuleNotFoundError: No module named 'pkg.sub'"
                )
            }
        }
    }
    assert cause_from_record(rec) == "ModuleNotFoundError: No module named 'pkg.sub'"


def test_cause_from_record_wrapper_only_returns_none():
    # When the wrapper is the ONLY exception line, it is dropped -> no cause remains.
    rec = {"call": {"crash": {"message": "ImportError: Failed to import test module: pkg"}}}
    assert cause_from_record(rec) is None


def test_cause_from_record_none_when_no_exception_text():
    assert cause_from_record({}) is None
    assert cause_from_record({"call": {"longrepr": "all green, nothing raised"}}) is None


def test_cause_from_record_section_order_teardown_last():
    # Blobs are joined in call->setup->teardown order; the last exception line overall wins.
    rec = {
        "call": {"longrepr": "E   ValueError: call_exc"},
        "setup": {"longrepr": "E   KeyError: setup_exc"},
        "teardown": {"longrepr": "E   TypeError: teardown_exc"},
    }
    assert cause_from_record(rec) == "TypeError: teardown_exc"


def test_cause_from_record_crash_message_appended_after_longrepr():
    # Within a section, longrepr is appended before crash.message, so crash wins as "last".
    rec = {"call": {"longrepr": "E   ValueError: lr", "crash": {"message": "KeyError: cr"}}}
    assert cause_from_record(rec) == "KeyError: cr"


def test_cause_from_record_falsy_longrepr_and_empty_crash_ignored():
    assert cause_from_record({"call": {"longrepr": ""}}) is None
    assert cause_from_record({"call": {"crash": {"message": ""}}}) is None
    assert cause_from_record({"call": {"crash": {"no_message": "x"}}}) is None


def test_cause_from_record_non_mapping_section_ignored():
    # A section that is not a Mapping is skipped entirely.
    assert cause_from_record({"call": "not a mapping"}) is None


def test_cause_from_record_coerces_non_str_longrepr():
    # str([...]) starts with "[" not "E"/whitespace+type, so the regex finds no exception.
    assert cause_from_record({"call": {"longrepr": ["E   ValueError: in list"]}}) is None
    assert cause_from_record({"call": {"longrepr": 123}}) is None


def test_cause_from_record_dotted_exception_type():
    # Dotted exception names are captured whole.
    rec = {"call": {"longrepr": "django.db.utils.IntegrityError: dup key"}}
    assert cause_from_record(rec) == "django.db.utils.IntegrityError: dup key"


def test_cause_from_record_only_scans_call_setup_teardown():
    # Sections other than call/setup/teardown (e.g. pytest "collect") are never inspected.
    rec = {"collect": {"longrepr": "E   ValueError: in collect phase"}}
    assert cause_from_record(rec) is None


def test_cause_from_record_dict_longrepr_coerced_finds_no_exception():
    # A dict longrepr is str()-coerced to "{...}" — the regex anchors fail, so no cause surfaces.
    rec = {"call": {"longrepr": {"reprcrash": {"message": "ValueError: hidden in dict"}}}}
    assert cause_from_record(rec) is None


def test_cause_from_record_indented_E_prefix_does_not_match():
    # The "E   " prefix is anchored at column 0 via "^(?:E\\s+)?"; an INDENTED E line is not a
    # valid exception line because the bare "E" type does not carry an Error/Exception suffix.
    rec = {"call": {"longrepr": "    E   ValueError: indented under E"}}
    assert cause_from_record(rec) is None


def test_cause_from_record_partial_section_last_wins():
    # If a later section has no exception text, the only exception (an earlier section) survives.
    rec = {
        "call": {"longrepr": "E   ValueError: real cause"},
        "teardown": {"longrepr": "everything cleaned up fine"},
    }
    assert cause_from_record(rec) == "ValueError: real cause"


def test_cause_from_record_bare_type_without_message():
    # A type with the required suffix but no message is returned bare (no colon).
    assert cause_from_record({"call": {"longrepr": "RuntimeError"}}) == "RuntimeError"


# --- regex boundary near-misses (via cause_from_record's text extraction) ---


def test_cause_from_record_requires_exception_suffix():
    # The type MUST end in Error/Exception/Warning/NotFound; "Failure"/"ValueX" do not match.
    assert cause_from_record({"call": {"longrepr": "Failure: text"}}) is None
    assert cause_from_record({"call": {"longrepr": "ValueX: text"}}) is None


def test_cause_from_record_accepts_all_suffixes():
    assert cause_from_record({"call": {"longrepr": "DeprecationWarning"}}) == "DeprecationWarning"
    assert cause_from_record({"call": {"longrepr": "CustomException"}}) == "CustomException"
    assert cause_from_record({"call": {"longrepr": "HTTPNotFound: 404"}}) == "HTTPNotFound: 404"


def test_cause_from_record_colon_without_message_does_not_match():
    # "ValueError:" (colon, no message) requires whitespace after the colon to capture a
    # message; with none, the leftover colon breaks the end-of-line anchor -> no match.
    assert cause_from_record({"call": {"longrepr": "ValueError:"}}) is None
    assert cause_from_record({"call": {"longrepr": "ValueError:    "}}) is None


# ---------------------------------------------------------------------------
# causes_from_log: frequency of normalized causes in a raw stdout log.
# ---------------------------------------------------------------------------


def test_causes_from_log_counts_normalized_duplicates(tmp_path):
    log = tmp_path / "end_default.json.log"
    log.write_text(
        "noise line\nValueError: failed at row 1\nmore noise\nValueError: failed at row 2\n",
        encoding="utf-8",
    )
    counter = causes_from_log(log)
    # rows 1 and 2 collapse to one normalized cause counted twice.
    assert dict(counter) == {"ValueError: failed at row N": 2}
    assert counter["ValueError: failed at row N"] == 2


def test_causes_from_log_normalizes_quotes(tmp_path):
    log = tmp_path / "n.log"
    log.write_text("KeyError: 'a'\nKeyError: 'b'\n", encoding="utf-8")
    assert dict(causes_from_log(log)) == {"KeyError: 'X'": 2}


def test_causes_from_log_drops_unittest_wrapper(tmp_path):
    log = tmp_path / "w.log"
    log.write_text(
        "ImportError: Failed to import test module: pkg\nModuleNotFoundError: No module named pkg.sub\n",
        encoding="utf-8",
    )
    assert dict(causes_from_log(log)) == {"ModuleNotFoundError: No module named pkg.sub": 1}


def test_causes_from_log_missing_file_is_empty(tmp_path):
    # Missing file -> empty Counter (Counter() == {}).
    counter = causes_from_log(tmp_path / "nope.log")
    assert counter == {}
    from collections import Counter as _Counter

    assert isinstance(counter, _Counter)


def test_causes_from_log_directory_path_is_empty(tmp_path):
    # Reading a directory raises OSError, which is caught -> empty Counter.
    assert causes_from_log(tmp_path) == {}


def test_causes_from_log_respects_byte_limit(tmp_path):
    # limit_bytes truncates the read; the cut falls mid-second-line so only the first survives.
    log = tmp_path / "big.log"
    log.write_text("ValueError: aaaa\nKeyError: bbbb\n", encoding="utf-8")
    assert dict(causes_from_log(log, limit_bytes=18)) == {"ValueError: aaaa": 1}


def test_causes_from_log_decodes_latin1(tmp_path):
    # The log is read as latin-1 with errors=replace; a non-ASCII message decodes fine.
    log = tmp_path / "lat.log"
    log.write_bytes("ValueError: caf\xe9 crash\n".encode("latin-1"))
    assert dict(causes_from_log(log)) == {"ValueError: caf\xe9 crash": 1}


def test_causes_from_log_empty_file_is_empty(tmp_path):
    log = tmp_path / "empty.log"
    log.write_text("", encoding="utf-8")
    assert causes_from_log(log) == {}


def test_causes_from_log_multiple_distinct_causes_exact_shape(tmp_path):
    # Two ValueErrors (same after number-collapse) + one KeyError -> exact two-key Counter.
    log = tmp_path / "mixed.log"
    log.write_text(
        "ValueError: row 1 bad\nKeyError: 'k'\nValueError: row 2 bad\n",
        encoding="utf-8",
    )
    assert dict(causes_from_log(log)) == {
        "ValueError: row N bad": 2,
        "KeyError: 'X'": 1,
    }


def test_causes_from_log_wrapper_only_yields_empty(tmp_path):
    # A log whose only exception line is the dropped unittest wrapper -> empty Counter.
    log = tmp_path / "wrap.log"
    log.write_text("ImportError: Failed to import test module: pkg\n", encoding="utf-8")
    assert dict(causes_from_log(log)) == {}


def test_causes_from_log_indented_E_lines_are_not_counted(tmp_path):
    # Indented "E   Type" lines (column>0) never match the col-0-anchored E prefix.
    log = tmp_path / "indent.log"
    log.write_text("    E   ValueError: indented\nKeyError: real\n", encoding="utf-8")
    assert dict(causes_from_log(log)) == {"KeyError: real": 1}


# ---------------------------------------------------------------------------
# Determinism guard: bucket classification of a mixed log is order-stable.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sig", "expected"),
    [
        ("OperationalError: no such table", ("ENV_DB", "ENV_FIXABLE")),
        ("ImportError: cannot import name", ("IMPORT_MISSING", "TIMING_DAG")),
        ("AssertionError: boom", ("TEST_ASSERT", "TEST_FAIL")),
        ("ValueError: nobody", ("OTHER", "OTHER")),
    ],
)
def test_classify_parametrized_buckets_stable(sig, expected):
    assert classify_cause(sig) == expected


# ---------------------------------------------------------------------------
# Taxonomy value + starter rename (Task 1): classify via an explicit ruleset.
# ---------------------------------------------------------------------------


def test_taxonomy_classify_matches_starter_default():
    assert (
        classify_cause("OperationalError: no such table")
        == STARTER.classify("OperationalError: no such table")
        == ("ENV_DB", "ENV_FIXABLE")
    )


def test_taxonomy_custom_rules_override_starter_ordering():
    tax = Taxonomy(rules=(("MY_DB", ("no such table",)),), categories={"MY_DB": "ENV_FIXABLE"})
    assert tax.classify("OperationalError: no such table") == ("MY_DB", "ENV_FIXABLE")


def test_taxonomy_unknown_bucket_category_is_other():
    tax = Taxonomy(rules=(("X", ("zzz",)),), categories={})
    assert tax.classify("zzz here") == ("X", "OTHER")


# ---------------------------------------------------------------------------
# load_taxonomy + --print-starter (Task 2): workspace-curated, starter fallback.
# ---------------------------------------------------------------------------


def test_load_taxonomy_absent_returns_starter(tmp_path):
    from deepcommit.harness.main_orch.diagnose_error_causes import load_taxonomy

    assert load_taxonomy(tmp_path) is STARTER


def test_load_taxonomy_layers_workspace_first_then_starter(tmp_path):
    from deepcommit.harness.main_orch.diagnose_error_causes import load_taxonomy

    (tmp_path / "_error_taxonomy.yaml").write_text(
        "inherit_starter: true\nrules:\n"
        "  - {bucket: MY_DB, category: ENV_FIXABLE, patterns: ['no such table'], grounded_by: 'm1'}\n",
        encoding="utf-8",
    )
    tax = load_taxonomy(tmp_path)
    assert tax.classify("no such table") == ("MY_DB", "ENV_FIXABLE")  # workspace wins
    assert tax.classify("AssertionError: x") == ("TEST_ASSERT", "TEST_FAIL")  # starter still applies
    assert "MY_DB" in tax.grounded_buckets


def test_load_taxonomy_inherit_false_drops_starter(tmp_path):
    from deepcommit.harness.main_orch.diagnose_error_causes import load_taxonomy

    (tmp_path / "_error_taxonomy.yaml").write_text(
        "inherit_starter: false\nrules:\n  - {bucket: ONLY, category: OTHER, patterns: ['zzz']}\n",
        encoding="utf-8",
    )
    tax = load_taxonomy(tmp_path)
    assert tax.classify("AssertionError: x") == ("OTHER", "OTHER")  # starter ignored
    assert tax.classify("zzz") == ("ONLY", "OTHER")
    assert "ONLY" not in tax.grounded_buckets  # no grounded_by


def test_load_taxonomy_malformed_falls_back_to_starter(tmp_path):
    from deepcommit.harness.main_orch.diagnose_error_causes import load_taxonomy

    (tmp_path / "_error_taxonomy.yaml").write_text("not: a list: [", encoding="utf-8")
    assert load_taxonomy(tmp_path) is STARTER


def test_print_starter_roundtrips_through_load(tmp_path):
    from deepcommit.harness.main_orch.diagnose_error_causes import _starter_as_yaml, load_taxonomy

    (tmp_path / "_error_taxonomy.yaml").write_text(_starter_as_yaml(), encoding="utf-8")
    assert load_taxonomy(tmp_path).classify("OperationalError: no such table") == ("ENV_DB", "ENV_FIXABLE")


# ---------------------------------------------------------------------------
# Honesty + self-audit fields (Task 3): dominant_fraction / other_pct / taxonomy stub.
# ---------------------------------------------------------------------------


def _write_end(tmp_path, mid, tests):
    import json

    (tmp_path / "dockerfiles" / mid).mkdir(parents=True)
    (tmp_path / "dockerfiles" / mid / "build_status.json").write_text(
        json.dumps({"artifact_paths": {"latest_attempt": "attempt_1"}})
    )
    d = tmp_path / "test_results" / mid / "refinement" / "attempt_1"
    d.mkdir(parents=True)
    (d / "end.json").write_text(json.dumps({"_framework": "pytest", "tests": tests}))


def test_analyze_milestone_emits_dominant_fraction_and_other(tmp_path):
    from deepcommit.harness.main_orch.diagnose_error_causes import analyze_milestone

    _write_end(
        tmp_path,
        "m1",
        [
            {"outcome": "error", "call": {"longrepr": "E   OperationalError: no such table"}},
            {"outcome": "error", "call": {"longrepr": "E   ValueError: nobody buckets this"}},
        ],
    )
    res = analyze_milestone(tmp_path, "m1")
    assert 0.0 <= res["dominant_fraction"] <= 1.0
    assert res["other_count"] == 1
    assert any("ValueError" in c for c in res["top_other_causes"])


def test_analyze_scope_emits_taxonomy_audit_stub(tmp_path):
    from deepcommit.harness.main_orch.diagnose_error_causes import analyze_scope, load_taxonomy

    _write_end(tmp_path, "m1", [{"outcome": "error", "call": {"longrepr": "E   ValueError: x"}}])
    rep = analyze_scope(tmp_path, ["m1"], load_taxonomy(tmp_path))
    assert rep["other_pct"] == 100.0
    t = rep["taxonomy"]
    assert t["source"] == "starter" and t["rule_count"] >= 8
    assert len(t["rules_digest"]) >= 8 and t["inherit_starter"] is True
    assert isinstance(t["unused_rules"], list) and isinstance(t["ungrounded_rules"], list)


# ---------------------------------------------------------------------------
# CLI wiring (Task 4): --print-starter + load workspace taxonomy.
# ---------------------------------------------------------------------------


def test_cli_print_starter_emits_yaml(capsys):
    from deepcommit.harness.main_orch.diagnose_error_causes import main

    assert main(["--print-starter", "."]) == 0
    out = capsys.readouterr().out
    assert "inherit_starter" in out and "rules:" in out


def test_cli_uses_workspace_taxonomy(tmp_path, capsys):
    from deepcommit.harness.main_orch.diagnose_error_causes import main

    _write_end(tmp_path, "m1", [{"outcome": "error", "call": {"longrepr": "E   ValueError: x"}}])
    (tmp_path / "_error_taxonomy.yaml").write_text(
        "rules:\n  - {bucket: MINE, category: TEST_FAIL, patterns: ['valueerror'], grounded_by: 'm1'}\n",
        encoding="utf-8",
    )
    assert main([str(tmp_path), "--all"]) == 0
    assert "workspace" in capsys.readouterr().out
