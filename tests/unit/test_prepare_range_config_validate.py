"""Unit tests for the Phase-1 config validation gate (pure core)."""

from deepcommit.preprocess.prepare_range_config.validate import classify_and_lint


def _codes(report):
    return {f.code for f in report.findings}


class TestPass:
    def test_clean_config_passes_with_no_findings(self):
        cfg = {"repo_src_dirs": ["src"], "test_dirs": ["**/tests/**"], "main_branch": "main"}
        files = ["src/app.py", "src/util.py", "tests/test_app.py"]
        r = classify_and_lint(cfg, files)
        assert r.ok
        assert r.per_language_src.get("python_src") == 2
        assert r.counts.get("test_content") == 1
        assert _codes(r) == set()


class TestErrors:
    def test_glob_in_repo_src_dirs_is_fatal(self):
        cfg = {"repo_src_dirs": ["src/*"], "test_dirs": ["**/tests/**"], "main_branch": "main"}
        r = classify_and_lint(cfg, ["src/a/app.py"])
        assert not r.ok
        assert "E_glob_in_src_dir" in _codes(r)

    def test_zero_source_is_fatal(self):
        cfg = {"repo_src_dirs": ["does_not_exist"], "test_dirs": ["**/tests/**"], "main_branch": "main"}
        r = classify_and_lint(cfg, ["src/app.py", "src/util.py"])
        assert not r.ok
        assert "E_zero_src" in _codes(r)

    def test_missing_repo_src_dirs_is_fatal(self):
        r = classify_and_lint({"test_dirs": ["**/tests/**"]}, ["src/app.py"])
        assert not r.ok
        assert "E_no_src_dirs" in _codes(r)

    def test_obsolete_struct_field_fixes_is_fatal(self):
        cfg = {"repo_src_dirs": ["src"], "test_dirs": ["**/tests/**"], "struct_field_fixes": [{"x": 1}]}
        r = classify_and_lint(cfg, ["src/app.py"])
        assert not r.ok
        assert "E_obsolete_key" in _codes(r)


class TestWarnings:
    def test_root_anchored_test_pattern_misses_nested_tests(self):
        # "tests/**" is root-anchored; nested crate tests get misclassified as source.
        cfg = {"repo_src_dirs": ["crates"], "test_dirs": ["tests/**"], "main_branch": "main"}
        files = ["crates/core/src/lib.rs", "crates/core/tests/it.rs", "crates/cli/tests/main.rs"]
        r = classify_and_lint(cfg, files)
        assert r.ok  # advisory, not blocking
        assert "W_test_looks_like_src" in _codes(r)

    def test_phase3_field_warns(self):
        cfg = {"repo_src_dirs": ["src"], "test_dirs": ["**/tests/**"], "build_command": "make"}
        r = classify_and_lint(cfg, ["src/app.py"])
        assert "W_phase3_field" in _codes(r)

    def test_unknown_key_warns(self):
        cfg = {"repo_src_dirs": ["src"], "test_dirs": ["**/tests/**"], "frobnicate": True}
        r = classify_and_lint(cfg, ["src/app.py"])
        assert "W_unknown_key" in _codes(r)

    def test_exclude_alias_warns(self):
        cfg = {"repo_src_dirs": ["src"], "test_dirs": ["**/tests/**"], "exclude_patterns": ["x/**"]}
        r = classify_and_lint(cfg, ["src/app.py"])
        assert "W_exclude_alias" in _codes(r)

    def test_source_like_outside_boundary_warns(self):
        cfg = {"repo_src_dirs": ["src"], "test_dirs": ["**/tests/**"], "main_branch": "main"}
        files = ["src/app.py", "tools/gen.go", "scripts/build.go"]
        r = classify_and_lint(cfg, files)
        assert "W_src_like_outside_boundary" in _codes(r)

    def test_unrecognised_src_extension_warns(self):
        cfg = {"repo_src_dirs": ["app"], "test_dirs": ["**/tests/**"], "main_branch": "main"}
        files = ["app/Main.scala", "app/widget.vue", "app/real.py"]
        r = classify_and_lint(cfg, files)
        assert "W_unrecognised_src_ext" in _codes(r)

    def test_main_branch_unresolved_warns(self):
        cfg = {"repo_src_dirs": ["src"], "test_dirs": ["**/tests/**"], "main_branch": "develop"}
        r = classify_and_lint(cfg, ["src/app.py"], main_branch_resolves=False)
        assert "W_main_branch_unresolved" in _codes(r)
        # resolves True / None → no warning
        assert "W_main_branch_unresolved" not in _codes(
            classify_and_lint(cfg, ["src/app.py"], main_branch_resolves=True)
        )
