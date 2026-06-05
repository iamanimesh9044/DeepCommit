"""Deep characterization tests for the Dockerfile linter.

Target: deepcommit.harness.prepare_images.build_milestone_image.dockerfile_lint

Scope (Wave 1, pure / fs-via-tmp_path only):
  - lint_dockerfile (incl. the missing-Dockerfile path)
  - the per-command rule fns: _check_no_checkout_file, _check_no_delete
  - the file-level rule fn: _check_patch_committed_file_level
  - the warn-level rule fn: _check_indirect_src_modify (heredoc + inline -c)
  - supporting pure helpers that drive the rules: _normalize_path,
    _path_touches_dir, _split_shell_commands, _parse_run_instructions,
    _extract_python_src_writes
  - format_violations

Every expected value below was ground-truthed by EXECUTING the function, not
traced statically. The removed symbols _is_modify_command / _command_touches_dir
are deliberately NOT imported.
"""

import sys
from pathlib import Path

from deepcommit.harness.prepare_images.build_milestone_image import (
    dockerfile_lint as dl,
)
from deepcommit.harness.prepare_images.build_milestone_image.dockerfile_lint import (
    LintViolation,
    _check_indirect_src_modify,
    _check_no_checkout_file,
    _check_no_delete,
    _check_patch_committed_file_level,
    _cli_main,
    _extract_python_src_writes,
    _normalize_path,
    _parse_run_instructions,
    _path_touches_dir,
    _split_shell_commands,
    format_violations,
    lint_dockerfile,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_dockerfile(tmp_path, body):
    """Write a Dockerfile with a standard FROM header + body, return its Path."""
    p = tmp_path / "Dockerfile"
    p.write_text("FROM python:3.11\n" + body + "\n")
    return p


def _lint(tmp_path, body, src_dirs=("src/urllib3",), test_dir="tests"):
    """Lint a body-only Dockerfile; src body always lands on line 2."""
    p = _write_dockerfile(tmp_path, body)
    return lint_dockerfile(p, list(src_dirs), test_dir, [])


def _ri(args, line, raw=None):
    """Build a single parsed RUN instruction dict."""
    return {"args": args, "line": line, "raw": raw if raw is not None else args}


# ---------------------------------------------------------------------------
# LintViolation dataclass shape
# ---------------------------------------------------------------------------


def test_lint_violation_field_order_and_types():
    v = LintViolation(
        rule="no_delete",
        severity="block",
        line=3,
        command="rm x",
        detail="d",
    )
    assert (v.rule, v.severity, v.line, v.command, v.detail) == (
        "no_delete",
        "block",
        3,
        "rm x",
        "d",
    )
    # positional construction must follow the declared field order
    assert v == LintViolation("no_delete", "block", 3, "rm x", "d")


# ---------------------------------------------------------------------------
# _check_no_checkout_file
# ---------------------------------------------------------------------------


def test_check_no_checkout_file_blocks_with_exact_shape():
    v = _check_no_checkout_file("git checkout HEAD~1 -- src/x.py", 5)
    assert isinstance(v, LintViolation)
    assert v.rule == "no_checkout_file"
    assert v.severity == "block"
    assert v.line == 5
    assert v.command == "git checkout HEAD~1 -- src/x.py"
    assert v.detail == (
        "'git checkout HEAD~1 -- src/x.py' checks out a file from "
        "another commit, changing the milestone's code state.  "
        "If this symbol is missing, it likely indicates a DAG dependency "
        "issue (missing edge), not an environment problem."
    )


def test_check_no_checkout_file_plain_branch_is_allowed():
    # No `-- <path>` => not a file checkout => no violation.
    assert _check_no_checkout_file("git checkout main", 5) is None


def test_check_no_checkout_file_plain_tag_is_allowed():
    assert _check_no_checkout_file("git checkout milestone-M001-start", 5) is None


def test_check_no_checkout_file_unrelated_command_is_none():
    assert _check_no_checkout_file("pip install -e .", 1) is None


def test_check_no_checkout_file_detail_interpolates_ref_and_path():
    v = _check_no_checkout_file("git checkout abc123 -- a/b/c.py", 9)
    assert v is not None
    assert v.detail.startswith("'git checkout abc123 -- a/b/c.py' checks out a file")


def test_check_no_checkout_file_command_truncated_to_200():
    long_cmd = "git checkout HEAD -- " + "x" * 300
    v = _check_no_checkout_file(long_cmd, 1)
    assert v is not None
    assert len(v.command) == 200


def test_check_no_checkout_file_extra_whitespace_still_blocks_and_detail_normalizes():
    # Adversarial spacing: multiple spaces around `checkout`, the `--`, and the
    # path. The rule still matches (regex tolerates \s+) AND the detail message
    # is rebuilt from the captured \S+ groups, so it collapses to single spaces
    # regardless of the raw command's spacing. `command` keeps the raw spacing;
    # `detail` is canonical.
    v = _check_no_checkout_file("git  checkout  HEAD~1  --  src/x.py", 5)
    assert v is not None
    assert v.command == "git  checkout  HEAD~1  --  src/x.py"
    assert v.detail == (
        "'git checkout HEAD~1 -- src/x.py' checks out a file from "
        "another commit, changing the milestone's code state.  "
        "If this symbol is missing, it likely indicates a DAG dependency "
        "issue (missing edge), not an environment problem."
    )


def test_check_no_checkout_file_two_dashes_without_path_is_allowed():
    # `git checkout <ref> --` with no following path token does NOT match the
    # `(\S+)\s+--\s+(\S+)` shape (no second \S+ after `--`) => no violation.
    assert _check_no_checkout_file("git checkout HEAD --", 5) is None


# ---------------------------------------------------------------------------
# _check_no_delete  (rule + allowlist)
# ---------------------------------------------------------------------------


def test_check_no_delete_blocks_src_delete_exact_shape():
    v = _check_no_delete("rm src/foo.py", 3)
    assert isinstance(v, LintViolation)
    assert v.rule == "no_delete"
    assert v.severity == "block"
    assert v.line == 3
    assert v.command == "rm src/foo.py"
    assert v.detail == (
        "Deleting files is forbidden. File deletion can break test "
        "discovery and changes the milestone's code state. "
        "If a file causes compilation errors, comment out its contents "
        "(whole-function comments) instead."
    )


def test_check_no_delete_blocks_git_rm():
    v = _check_no_delete("git rm src/foo.py", 3)
    assert v is not None and v.rule == "no_delete" and v.severity == "block"


def test_check_no_delete_allowlist_testbed_no_trailing_slash():
    assert _check_no_delete("rm -rf /testbed", 3) is None


def test_check_no_delete_testbed_with_trailing_slash_still_blocks():
    # Adversarial near-miss: the allowlist anchors on `/testbed$` (no trailing
    # slash), so `rm -rf /testbed/` falls THROUGH and blocks. Proves the rule
    # does not over-allow.
    v = _check_no_delete("rm -rf /testbed/", 3)
    assert v is not None and v.rule == "no_delete"


def test_check_no_delete_allowlist_tmp():
    assert _check_no_delete("rm -rf /tmp/x", 3) is None


def test_check_no_delete_allowlist_output():
    assert _check_no_delete("rm -rf /output/foo", 3) is None


def test_check_no_delete_allowlist_pycache():
    assert _check_no_delete("rm -rf src/urllib3/__pycache__", 3) is None


def test_check_no_delete_allowlist_pyc():
    assert _check_no_delete("rm -f foo.pyc", 3) is None


def test_check_no_delete_allowlist_node_modules():
    assert _check_no_delete("rm -rf node_modules", 3) is None


def test_check_no_delete_allowlist_pyo():
    # `.pyo` is the third alternation in the cache/build allowlist (source L356).
    assert _check_no_delete("rm -rf foo.pyo", 3) is None


def test_check_no_delete_allowlist_egg_info():
    # `.egg-info` alternation in the cache/build allowlist (source L356).
    assert _check_no_delete("rm -rf src/foo.egg-info", 3) is None


def test_check_no_delete_allowlist_dot_cache():
    # `.cache` alternation in the cache/build allowlist (source L356).
    assert _check_no_delete("rm -rf /home/user/.cache", 3) is None


def test_check_no_delete_find_with_cache_token_allowed_by_l356_not_l360():
    # DEAD-CODE WITNESS for source L358-360 (`find ... (cache) ... (-delete|rm)`).
    # Any string that matches the L359 regex necessarily contains one of
    # (__pycache__|.pyc|.cache), all of which ALSO match the broader L356 cache
    # allowlist, which returns None FIRST. So L360 is unreachable. This input
    # exercises the only way such a command could arrive (`rm` token present so
    # the rule body is entered) and confirms it is allowlisted at L356, never
    # L360 — proving the allow is correct even though L360 is dead.
    assert _check_no_delete("find . -name __pycache__ -exec rm -rf {} +", 3) is None


def test_check_no_delete_no_rm_token_returns_none():
    # The rule's guard is `\brm\s`: no rm token => never enters the rule body.
    assert _check_no_delete("pip install x", 3) is None
    assert _check_no_delete("rmdir foo", 3) is None
    assert _check_no_delete("rm", 3) is None  # `rm` with no trailing space


def test_check_no_delete_find_delete_non_cache_has_no_rm_token():
    # `find ... -delete` without an `rm` token never enters the rule body, so it
    # is silently allowed regardless of target. Documents the guard's reach.
    assert _check_no_delete('find . -name "*.py" -delete', 3) is None


def test_check_no_delete_command_truncated_to_200():
    long_cmd = "rm " + "x" * 300
    v = _check_no_delete(long_cmd, 1)
    assert v is not None
    assert len(v.command) == 200


# ---------------------------------------------------------------------------
# _check_patch_committed_file_level  (warn)
# ---------------------------------------------------------------------------


def test_patch_committed_no_patch_returns_none():
    assert _check_patch_committed_file_level([_ri("echo hi", 1)]) is None


def test_patch_committed_all_present_returns_none():
    instrs = [
        _ri("patch -p1 < x.diff", 3),
        _ri("git add -A && git commit -m x && git tag -f T HEAD", 6),
    ]
    assert _check_patch_committed_file_level(instrs) is None


def test_patch_committed_missing_all_three_exact_shape():
    v = _check_patch_committed_file_level([_ri("sed -i s/a/b/ f.py", 4)])
    assert isinstance(v, LintViolation)
    assert v.rule == "patch_not_committed"
    assert v.severity == "warn"
    assert v.line == 4
    assert v.command == "(RUN block with sed/patch)"
    assert v.detail == (
        "Dockerfile contains sed/patch at L4 but is missing "
        "git add -A + git commit -m '[ENV-PATCH] ...' + git tag -f <TAG> HEAD "
        "anywhere in the file.  Patches will be lost when switching between "
        "START/END states.  Add: git add -A && git commit -m '[ENV-PATCH] ...' "
        "&& git tag -f <TAG> HEAD"
    )


def test_patch_committed_points_at_first_location_and_counts_more():
    instrs = [_ri("sed -i a f", 2), _ri("sed --in-place b g", 5)]
    v = _check_patch_committed_file_level(instrs)
    assert v is not None
    assert v.line == 2
    assert "at L2 (and 1 more)" in v.detail


def test_patch_committed_partial_lists_only_missing_pieces():
    # has git add + git commit, missing only tag -f
    instrs = [_ri("sed -i a f", 2), _ri("git add -A && git commit -m x", 4)]
    v = _check_patch_committed_file_level(instrs)
    assert v is not None
    assert "git tag -f <TAG> HEAD" in v.detail
    assert "git add -A +" not in v.detail
    assert "git commit -m '[ENV-PATCH] ...' +" not in v.detail


def test_patch_committed_recognizes_patch_p_directive():
    # `patch -p<N>` (not sed) also triggers the rule.
    v = _check_patch_committed_file_level([_ri("patch -p0 < a.diff", 7)])
    assert v is not None and v.line == 7


# ---------------------------------------------------------------------------
# _check_indirect_src_modify  (warn)
# ---------------------------------------------------------------------------


def test_indirect_src_modify_empty_src_dirs_returns_empty():
    instrs = [
        _ri(
            "cat > /tmp/p.py << EOF",
            2,
            raw="RUN cat > /tmp/p.py << EOF\nopen('/testbed/src/urllib3/x.py','w')\nEOF",
        ),
        _ri("python3 /tmp/p.py", 5),
    ]
    assert _check_indirect_src_modify(instrs, []) == []


def test_indirect_src_modify_heredoc_executed_flags_warn(tmp_path):
    body = (
        "RUN cat > /tmp/patch.py << EOF\n"
        "from pathlib import Path\n"
        "Path('/testbed/src/urllib3/connection.py').write_text('x')\n"
        "EOF\n"
        "RUN python3 /tmp/patch.py"
    )
    vs = _lint(tmp_path, body)
    assert len(vs) == 1
    v = vs[0]
    assert v.rule == "indirect_src_modify"
    assert v.severity == "warn"
    assert v.line == 2
    assert v.command == "cat > /tmp/patch.py << EOF ...; python /tmp/patch.py"
    assert v.detail == (
        "Indirect source modification via heredoc-generated Python script "
        "'/tmp/patch.py', which writes 'src/urllib3/connection.py'. Review "
        "this as a source shim; if the missing symbol is a DAG/root-cause "
        "issue, prefer a verified bound waiver over implementing framework "
        "behavior."
    )


def test_indirect_src_modify_heredoc_not_executed_is_silent(tmp_path):
    # Near-miss: the heredoc writes src, but the script is never run with python.
    body = "RUN cat > /tmp/patch.py << EOF\nPath('/testbed/src/urllib3/connection.py').write_text('x')\nEOF"
    assert _lint(tmp_path, body) == []


def test_indirect_src_modify_inline_dash_c_flags_warn(tmp_path):
    body = "RUN python3 -c \"open('/testbed/src/urllib3/x.py','w').write('z')\""
    vs = _lint(tmp_path, body)
    assert len(vs) == 1
    v = vs[0]
    assert v.rule == "indirect_src_modify"
    assert v.severity == "warn"
    assert v.line == 2
    assert v.detail == ("Indirect source modification via inline interpreter command, which writes 'src/urllib3/x.py'.")


def test_indirect_src_modify_inline_writes_non_src_is_silent(tmp_path):
    body = "RUN python3 -c \"open('/testbed/docs/readme.md','w').write('z')\""
    assert _lint(tmp_path, body) == []


def test_indirect_src_modify_heredoc_non_tmp_py_target_skipped():
    # Heredoc whose target is not /tmp/*.py is ignored (line 290-291 `continue`),
    # even if its body writes protected src.
    raw_etc = "RUN cat > /etc/conf.py << EOF\nopen('/testbed/src/urllib3/x.py','w')\nEOF"
    instrs_etc = [{"args": "cat > /etc/conf.py << EOF", "line": 2, "raw": raw_etc}]
    assert _check_indirect_src_modify(instrs_etc, ["src/urllib3"]) == []

    # Target under /tmp/ but not .py: also skipped.
    raw_txt = "RUN cat > /tmp/note.txt << EOF\nopen('/testbed/src/urllib3/x.py','w')\nEOF"
    instrs_txt = [{"args": "cat > /tmp/note.txt << EOF", "line": 2, "raw": raw_txt}]
    assert _check_indirect_src_modify(instrs_txt, ["src/urllib3"]) == []


def test_indirect_src_modify_heredoc_dedups_repeated_key():
    # Two identical executed heredocs at the same line writing the same src path
    # produce ONE violation (the `seen` set drops the duplicate, line 296-297).
    hd_raw = "RUN cat > /tmp/p.py << EOF\nopen('/testbed/src/urllib3/x.py','w')\nEOF"
    instrs = [
        {"args": "cat > /tmp/p.py << EOF", "line": 2, "raw": hd_raw},
        {"args": "cat > /tmp/p.py << EOF", "line": 2, "raw": hd_raw},
        {"args": "python3 /tmp/p.py", "line": 9, "raw": "python3 /tmp/p.py"},
    ]
    vs = _check_indirect_src_modify(instrs, ["src/urllib3"])
    assert len(vs) == 1
    assert vs[0].rule == "indirect_src_modify"


def test_indirect_src_modify_inline_dedups_repeated_key():
    # Two identical inline commands at the same line writing the same src path
    # produce ONE violation (the `seen` set drops the duplicate, line 316-317).
    inline = "python3 -c \"open('/testbed/src/urllib3/x.py','w')\""
    instrs = [
        {"args": inline, "line": 2, "raw": inline},
        {"args": inline, "line": 2, "raw": inline},
    ]
    vs = _check_indirect_src_modify(instrs, ["src/urllib3"])
    assert len(vs) == 1
    assert vs[0].rule == "indirect_src_modify"


def test_indirect_src_modify_inline_bash_dash_c_flags_warn(tmp_path):
    # Source L313 alternation is `(?:python3?|bash)\s+-c\s+`: a `bash -c` shell
    # that runs python writing src is flagged the same as `python -c`. Pins that
    # `bash`, not just `python`, triggers the inline interpreter path.
    body = "RUN bash -c \"open('/testbed/src/urllib3/x.py','w').write('z')\""
    vs = _lint(tmp_path, body)
    assert len(vs) == 1
    v = vs[0]
    assert v.rule == "indirect_src_modify"
    assert v.severity == "warn"
    assert v.line == 2
    assert v.detail == ("Indirect source modification via inline interpreter command, which writes 'src/urllib3/x.py'.")


def test_indirect_src_modify_multipath_order_is_body_order_not_hashed():
    # DETERMINISM: a single executed heredoc that writes THREE protected paths
    # across TWO src dirs yields three violations whose order matches the
    # script's body line order (the `touched` accumulator is a list with
    # membership dedup, never a set), so it is independent of PYTHONHASHSEED.
    script = "\n".join(
        [
            "open('/testbed/src/a/x.py','w')",
            "open('/testbed/src/b/y.py','w')",
            "open('/testbed/src/a/z.py','w')",
        ]
    )
    raw = "RUN cat > /tmp/p.py << EOF\n" + script + "\nEOF"
    instrs = [
        {"args": "cat > /tmp/p.py << EOF", "line": 2, "raw": raw},
        {"args": "python3 /tmp/p.py", "line": 9, "raw": "python3 /tmp/p.py"},
    ]
    vs = _check_indirect_src_modify(instrs, ["src/a", "src/b"])
    touched = [v.detail.split("'")[3] for v in vs]
    assert touched == ["src/a/x.py", "src/b/y.py", "src/a/z.py"]
    # every violation shares the executing line + heredoc command shape
    assert all(v.line == 2 for v in vs)
    assert all(v.command == "cat > /tmp/p.py << EOF ...; python /tmp/p.py" for v in vs)


# ---------------------------------------------------------------------------
# _extract_python_src_writes  (drives indirect rule)
# ---------------------------------------------------------------------------


def test_extract_writes_read_mode_not_flagged():
    assert _extract_python_src_writes("open('/testbed/src/urllib3/x.py','r')", ["src/urllib3"]) == []


def test_extract_writes_write_and_append_modes_flagged():
    assert _extract_python_src_writes("open('/testbed/src/urllib3/x.py','w')", ["src/urllib3"]) == ["src/urllib3/x.py"]
    assert _extract_python_src_writes("open('/testbed/src/urllib3/x.py','a')", ["src/urllib3"]) == ["src/urllib3/x.py"]


def test_extract_writes_variable_mapped_write_text():
    script = 'p = Path("/testbed/src/urllib3/conn.py")\np.write_text("x")'
    assert _extract_python_src_writes(script, ["src/urllib3"]) == ["src/urllib3/conn.py"]


def test_extract_writes_direct_path_write_text():
    assert _extract_python_src_writes('Path("src/urllib3/a.py").write_text("x")', ["src/urllib3"]) == [
        "src/urllib3/a.py"
    ]


def test_extract_writes_direct_path_write_bytes():
    # The write_ regex is `write_(?:text|bytes)`; pin the bytes arm explicitly.
    assert _extract_python_src_writes('Path("src/urllib3/a.py").write_bytes(b"x")', ["src/urllib3"]) == [
        "src/urllib3/a.py"
    ]


def test_extract_writes_variable_mapped_open():
    # A bare-string assignment `p = "/testbed/src/urllib3/x.py"` populates the
    # variable_paths map (source L204-210), then `open(p, "w")` resolves p via
    # variable_paths.get (source L219) — distinct from the Path()-assignment arm.
    script = "\n".join(["p = '/testbed/src/urllib3/x.py'", "open(p, 'w')"])
    assert _extract_python_src_writes(script, ["src/urllib3"]) == ["src/urllib3/x.py"]


def test_extract_writes_variable_open_read_mode_not_flagged():
    # Same variable mapping, but read mode => the mutating-mode guard (L216)
    # short-circuits before add_if_src, so nothing is flagged.
    script = "\n".join(["p = '/testbed/src/urllib3/x.py'", "open(p, 'r')"])
    assert _extract_python_src_writes(script, ["src/urllib3"]) == []


def test_extract_writes_dedups_same_path_across_lines():
    # Two writes to the same protected path collapse to a single entry
    # (membership check on `touched`), preserving first-seen order.
    script = "\n".join(
        [
            "open('/testbed/src/urllib3/x.py','w')",
            "open('/testbed/src/urllib3/x.py','a')",
        ]
    )
    assert _extract_python_src_writes(script, ["src/urllib3"]) == ["src/urllib3/x.py"]


def test_extract_writes_non_src_path_returns_empty():
    assert _extract_python_src_writes("open('/testbed/docs/readme.md','w')", ["src/urllib3"]) == []


def test_extract_writes_empty_src_dirs_returns_empty():
    assert _extract_python_src_writes("open('/testbed/src/urllib3/x.py','w')", []) == []


# ---------------------------------------------------------------------------
# Supporting pure helpers
# ---------------------------------------------------------------------------


def test_normalize_path_strips_testbed_dotslash_and_quotes():
    assert _normalize_path("/testbed/src/x.py") == "src/x.py"
    assert _normalize_path("./src/x.py") == "src/x.py"
    assert _normalize_path("'src/x.py'") == "src/x.py"
    assert _normalize_path('  "src/x.py"  ') == "src/x.py"


def test_normalize_path_degenerate_inputs():
    # Whitespace-only collapses to empty; bare /testbed/ leaves nothing after the
    # prefix strip; the testbed strip is anchored so a non-leading /testbed/ stays.
    assert _normalize_path("   ") == ""
    assert _normalize_path("/testbed/") == ""
    assert _normalize_path("a/testbed/b") == "a/testbed/b"
    # only the FIRST leading ./ is stripped (single re.sub of `^\./`)
    assert _normalize_path("././x.py") == "./x.py"


def test_path_touches_dir_boundary_semantics():
    assert _path_touches_dir("src/urllib3/x.py", ["src/urllib3"]) == "src/urllib3/x.py"
    assert _path_touches_dir("src/urllib3", ["src/urllib3"]) == "src/urllib3"
    assert _path_touches_dir("docs/x.py", ["src/urllib3"]) is None
    # adversarial near-miss: prefix match without a path separator must NOT match
    assert _path_touches_dir("src/urllib3extra/x.py", ["src/urllib3"]) is None


def test_path_touches_dir_empty_and_trailing_slash_dir_entries():
    # An empty/blank dir entry is skipped (the `if d and ...` guard, source L135),
    # so it never spuriously matches; a later real entry still matches.
    assert _path_touches_dir("src/x.py", ["", "src"]) == "src/x.py"
    assert _path_touches_dir("src/x.py", ["   "]) is None
    # Trailing-slash dir entries are normalized (strip("/").rstrip("/")) before
    # comparison, so "src/" behaves identically to "src".
    assert _path_touches_dir("src/x.py", ["src/"]) == "src/x.py"
    # The path argument is itself normalized: /testbed/ prefix is stripped first.
    assert _path_touches_dir("/testbed/src/x.py", ["src"]) == "src/x.py"


def test_split_shell_commands():
    assert _split_shell_commands("a && b &&  c") == ["a", "b", "c"]
    assert _split_shell_commands("   ") == []
    assert _split_shell_commands("solo") == ["solo"]


def test_split_shell_commands_only_splits_on_double_ampersand():
    # The splitter intentionally splits ONLY on `&&` — not `;`, `|`, or a single
    # `&`. This is load-bearing: a `rm` joined by `;` stays in one command token.
    assert _split_shell_commands("a ; b") == ["a ; b"]
    assert _split_shell_commands("a | b") == ["a | b"]
    assert _split_shell_commands("a & b") == ["a & b"]
    # trailing && yields no empty trailing token (empty parts are dropped)
    assert _split_shell_commands("a && ") == ["a"]


def test_parse_run_instructions_skips_comments_and_joins_continuations():
    content = "FROM x\n# comment\nRUN echo a && \\\n    echo b\nCOPY . .\nRUN pip install"
    ris = _parse_run_instructions(content)
    assert [(r["line"], r["args"]) for r in ris] == [
        (3, "echo a &&  echo b"),
        (6, "pip install"),
    ]


def test_parse_run_instructions_case_insensitive_and_empty():
    assert _parse_run_instructions("") == []
    ris = _parse_run_instructions("run echo hi")
    assert len(ris) == 1
    assert ris[0]["args"] == "echo hi"


def test_parse_run_instructions_requires_separator_after_run():
    # `RUN` must be followed by whitespace (regex `^RUN\s+`); a token like
    # `RUNNER`/`RUNX` is NOT a RUN instruction and is skipped entirely.
    assert _parse_run_instructions("RUNNER echo hi") == []
    # A tab separator counts as \s+.
    ris = _parse_run_instructions("RUN\techo hi")
    assert [(r["line"], r["args"]) for r in ris] == [(1, "echo hi")]


def test_parse_run_instructions_dangling_backslash_at_eof():
    # A continuation backslash with no following line: the while-loop's
    # `i + 1 < len(lines)` guard stops, and the trailing `\` is kept in args
    # verbatim (no continuation join happens).
    ris = _parse_run_instructions("RUN echo a \\")
    assert [(r["line"], r["args"]) for r in ris] == [(1, "echo a \\")]


def test_parse_run_instructions_captures_heredoc_body_in_raw_only():
    # For a cat-heredoc RUN, `args` holds only the first line's command while
    # `raw` preserves the full instruction including the heredoc body and the
    # closing tag. The continuation logic is suppressed when heredoc tags exist.
    content = "FROM x\nRUN cat > /tmp/p.py << EOF\nimport os\nos.write()\nEOF\nRUN python3 /tmp/p.py"
    ris = _parse_run_instructions(content)
    assert [(r["line"], r["args"]) for r in ris] == [
        (2, "cat > /tmp/p.py << EOF"),
        (6, "python3 /tmp/p.py"),
    ]
    assert ris[0]["raw"] == "RUN cat > /tmp/p.py << EOF\nimport os\nos.write()\nEOF"
    assert ris[1]["raw"] == "RUN python3 /tmp/p.py"


# ---------------------------------------------------------------------------
# lint_dockerfile  (end-to-end via tmp_path)
# ---------------------------------------------------------------------------


def test_lint_clean_dockerfile(tmp_path):
    assert _lint(tmp_path, "RUN pip install -e .") == []


def test_lint_checkout_blocks(tmp_path):
    vs = _lint(tmp_path, "RUN git checkout HEAD~1 -- src/urllib3/connection.py")
    assert [(v.rule, v.severity, v.line) for v in vs] == [("no_checkout_file", "block", 2)]


def test_lint_allowed_tmp_cleanup(tmp_path):
    assert _lint(tmp_path, "RUN rm -rf /tmp/build_artifacts") == []


def test_lint_delete_blocks(tmp_path):
    vs = _lint(tmp_path, "RUN rm src/urllib3/foo.py")
    assert [(v.rule, v.severity, v.line) for v in vs] == [("no_delete", "block", 2)]


def test_lint_missing_file_exact_shape():
    vs = lint_dockerfile(Path("/nonexistent/Dockerfile"), ["src"], "tests", [])
    assert len(vs) == 1
    v = vs[0]
    assert v.rule == "missing_dockerfile"
    assert v.severity == "block"
    assert v.line == 0
    assert v.command == ""
    assert v.detail == "Dockerfile not found: /nonexistent/Dockerfile"


def test_lint_combined_violation_ordering_is_deterministic(tmp_path):
    # Per-command rules in source order (checkout, then delete) on each line,
    # then the file-level patch warn last.
    body = "RUN git checkout HEAD~1 -- src/urllib3/a.py && rm src/urllib3/b.py\nRUN sed -i s/x/y/ src/urllib3/c.py"
    vs = _lint(tmp_path, body)
    assert [(v.rule, v.severity, v.line) for v in vs] == [
        ("no_checkout_file", "block", 2),
        ("no_delete", "block", 2),
        ("patch_not_committed", "warn", 3),
    ]


def test_lint_two_run_lines_keep_distinct_line_numbers(tmp_path):
    body = "RUN rm src/a.py\nRUN git checkout X -- src/b.py"
    vs = _lint(tmp_path, body, src_dirs=("src",))
    assert [(v.rule, v.line) for v in vs] == [
        ("no_delete", 2),
        ("no_checkout_file", 3),
    ]


def test_lint_semicolon_joined_rm_stays_in_one_command_and_blocks(tmp_path):
    # `_split_shell_commands` does NOT split on `;`, so `echo hi ; rm src/...`
    # is a single command token whose `\brm\s` guard still fires => one block.
    # The whole token (echo + rm) is reported as the violating command.
    vs = _lint(tmp_path, "RUN echo hi ; rm src/urllib3/x.py")
    assert [(v.rule, v.severity, v.line, v.command) for v in vs] == [
        ("no_delete", "block", 2, "echo hi ; rm src/urllib3/x.py"),
    ]


def test_lint_multiple_blockers_same_line_full_shape(tmp_path):
    # Two checkout-file commands && joined on one RUN line: BOTH block, in
    # source order, both pointing at the same physical line.
    body = "RUN git checkout A -- src/urllib3/a.py && git checkout B -- src/urllib3/b.py"
    vs = _lint(tmp_path, body)
    assert [(v.rule, v.severity, v.line, v.command) for v in vs] == [
        ("no_checkout_file", "block", 2, "git checkout A -- src/urllib3/a.py"),
        ("no_checkout_file", "block", 2, "git checkout B -- src/urllib3/b.py"),
    ]


def test_lint_warn_only_dockerfile_has_no_blockers(tmp_path):
    # A lone uncommitted sed produces exactly one warn-level violation and zero
    # blockers — the canonical "passes the block gate but flags audit evidence".
    vs = _lint(tmp_path, "RUN sed -i s/a/b/ src/urllib3/x.py")
    assert [(v.rule, v.severity) for v in vs] == [("patch_not_committed", "warn")]
    assert [v for v in vs if v.severity == "block"] == []


def test_lint_passes_args_through_allowed_tags_unused(tmp_path):
    # allowed_tags is reserved/unused; a non-empty value must not change output.
    p = _write_dockerfile(tmp_path, "RUN git checkout milestone-start -- src/urllib3/x.py")
    vs = lint_dockerfile(p, ["src/urllib3"], "tests", ["milestone-start", "milestone-end"])
    assert [(v.rule, v.severity, v.line) for v in vs] == [("no_checkout_file", "block", 2)]


# ---------------------------------------------------------------------------
# format_violations
# ---------------------------------------------------------------------------


def test_format_violations_empty_is_empty_string():
    assert format_violations([]) == ""


def test_format_violations_blockers_first_then_warnings():
    vb = LintViolation("no_delete", "block", 3, "rm src/foo.py", "del detail")
    vw = LintViolation("patch_not_committed", "warn", 7, "(RUN block with sed/patch)", "warn detail")
    out = format_violations([vb, vw])
    assert out == (
        "Dockerfile lint: 1 error(s), 1 warning(s)\n\n"
        "  [ERROR] L3 no_delete\n"
        "    del detail\n"
        "    Command: rm src/foo.py\n\n"
        "  [WARNING] L7 patch_not_committed\n"
        "    warn detail\n\n"
        "Fix the ERROR violations and regenerate the Dockerfile. "
        "See the DOCKERFILE VERIFICATION CHECKLIST in the original prompt."
    )


def test_format_violations_warn_only_uses_warn_footer():
    vw = LintViolation("patch_not_committed", "warn", 7, "(RUN block with sed/patch)", "warn detail")
    out = format_violations([vw])
    assert out.startswith("Dockerfile lint: 0 error(s), 1 warning(s)")
    assert "audit evidence for risky environment" in out
    # the sentinel placeholder command must be suppressed from the output
    assert "Command:" not in out


def test_format_violations_truncates_long_command_with_ellipsis():
    long_cmd = "rm " + "y" * 300
    v = LintViolation("no_delete", "block", 1, long_cmd, "d")
    out = format_violations([v])
    assert "Command: rm " + "y" * (150 - 3) + "..." in out


def test_format_violations_command_exactly_150_has_no_ellipsis():
    # Boundary: at exactly len==150 the `len > 150` guard is False, so NO
    # ellipsis is appended and the full command is shown verbatim.
    cmd150 = "rm " + "y" * 147  # len == 150
    assert len(cmd150) == 150
    v = LintViolation("no_delete", "block", 1, cmd150, "d")
    out = format_violations([v])
    assert "..." not in out
    assert f"    Command: {cmd150}\n" in out


def test_format_violations_empty_command_suppresses_command_line():
    # The missing_dockerfile shape carries command="": the `if v.command and ...`
    # guard suppresses the Command line entirely, but rule/detail still render.
    v = LintViolation("missing_dockerfile", "block", 0, "", "Dockerfile not found: /x")
    out = format_violations([v])
    assert out == (
        "Dockerfile lint: 1 error(s), 0 warning(s)\n\n"
        "  [ERROR] L0 missing_dockerfile\n"
        "    Dockerfile not found: /x\n\n"
        "Fix the ERROR violations and regenerate the Dockerfile. "
        "See the DOCKERFILE VERIFICATION CHECKLIST in the original prompt."
    )


def test_format_violations_two_blockers_one_warning_exact_full_shape():
    # Interleaved input must be regrouped: all blockers first (in input order),
    # then all warnings. Header counts reflect 2 errors + 1 warning.
    vb1 = LintViolation("no_checkout_file", "block", 2, "git checkout A -- f", "d1")
    vw = LintViolation("patch_not_committed", "warn", 9, "(RUN block with sed/patch)", "dw")
    vb2 = LintViolation("no_delete", "block", 4, "rm g", "d2")
    out = format_violations([vb1, vw, vb2])
    assert out == (
        "Dockerfile lint: 2 error(s), 1 warning(s)\n\n"
        "  [ERROR] L2 no_checkout_file\n"
        "    d1\n"
        "    Command: git checkout A -- f\n\n"
        "  [ERROR] L4 no_delete\n"
        "    d2\n"
        "    Command: rm g\n\n"
        "  [WARNING] L9 patch_not_committed\n"
        "    dw\n\n"
        "Fix the ERROR violations and regenerate the Dockerfile. "
        "See the DOCKERFILE VERIFICATION CHECKLIST in the original prompt."
    )


# ---------------------------------------------------------------------------
# _cli_main  (argparse + exit codes; pure, driven via monkeypatched argv)
# ---------------------------------------------------------------------------
#
# NOTE: _cli_main is in-process testable — argparse over a synthetic argv, a
# tmp_path Dockerfile, and capsys for stdout. It needs no Docker/git/LLM, so it
# is in Wave-1 scope despite being the "CLI entrypoint".


def test_cli_main_clean_returns_0_and_prints_clean(tmp_path, monkeypatch, capsys):
    p = tmp_path / "Dockerfile"
    p.write_text("FROM x\nRUN pip install -e .\n")
    monkeypatch.setattr(sys, "argv", ["prog", str(p)])
    rc = _cli_main()
    assert rc == 0
    assert capsys.readouterr().out == "clean: no violations.\n"


def test_cli_main_block_returns_2_and_prints_error_report(tmp_path, monkeypatch, capsys):
    p = tmp_path / "Dockerfile"
    p.write_text("FROM x\nRUN rm src/x.py\n")
    monkeypatch.setattr(sys, "argv", ["prog", str(p), "--repo-src-dirs", "src"])
    rc = _cli_main()
    assert rc == 2
    out = capsys.readouterr().out
    assert out.startswith("Dockerfile lint: 1 error(s), 0 warning(s)")
    assert "[ERROR] L2 no_delete" in out
    assert "clean: no violations." not in out


def test_cli_main_warn_only_returns_1(tmp_path, monkeypatch, capsys):
    p = tmp_path / "Dockerfile"
    p.write_text("FROM x\nRUN sed -i s/a/b/ src/x.py\n")
    monkeypatch.setattr(sys, "argv", ["prog", str(p), "--repo-src-dirs", "src"])
    rc = _cli_main()
    assert rc == 1
    out = capsys.readouterr().out
    assert out.startswith("Dockerfile lint: 0 error(s), 1 warning(s)")
    assert "[WARNING] L2 patch_not_committed" in out


def test_cli_main_missing_dockerfile_returns_2(tmp_path, monkeypatch, capsys):
    # A nonexistent path yields the missing_dockerfile block violation => rc 2.
    missing = tmp_path / "nope" / "Dockerfile"
    monkeypatch.setattr(sys, "argv", ["prog", str(missing)])
    rc = _cli_main()
    assert rc == 2
    out = capsys.readouterr().out
    assert "missing_dockerfile" in out
    assert f"Dockerfile not found: {missing}" in out


def test_cli_main_defaults_test_dir_and_empty_src_dirs(tmp_path, monkeypatch, capsys):
    # With no --repo-src-dirs and default --test-dir, src_dirs is [] so the
    # indirect-src warn never fires; a clean file still returns 0.
    p = tmp_path / "Dockerfile"
    p.write_text("FROM x\nRUN echo hi\n")
    monkeypatch.setattr(sys, "argv", ["prog", str(p)])
    rc = _cli_main()
    assert rc == 0
    assert capsys.readouterr().out == "clean: no violations.\n"


def test_cli_main_is_exported_callable():
    # Guard against accidental rename/removal of the CLI entrypoint symbol.
    assert callable(dl._cli_main)
