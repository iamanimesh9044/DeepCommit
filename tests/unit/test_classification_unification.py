"""Regression guards for the Phase 1-3 source/test classification unification.

Every glob/prefix boundary classifier now routes through the single canonical
engine ``SrcFileFilter`` (pathspec/gitwildmatch + S-norm src membership). The
four hand-rolled ``_glob_match`` copies were deleted. See
docs/refactor/classification_unification.md.
"""

from deepcommit.harness.utils.src_filter import SrcFileFilter


class TestCanonicalExcludeEngine:
    def test_gitignore_directory_subtree_semantics(self):
        """A pattern matching a *directory* excludes its whole subtree.

        This is the one behavior that diverged from the old hand-rolled engine
        (it only matched the final path segment). On element-web this is the
        197-case ``foo.spec.ts/`` snapshot-dir behavior.
        """
        case = "a/feature.spec.ts/snap-linux.png"
        assert SrcFileFilter.should_exclude_file(case, ["**/*.spec.*"]) is True

    def test_root_anchored_and_nested_cases(self):
        patterns = ["**/tests/**/*", "**/*_test.go", "**/testdata/**", "README.md", "**/*.md"]
        for p in ["tests/foo.py", "tests/b/foo.py", "a/tests/foo.py", "foo_test.go", "a/foo_test.go"]:
            assert SrcFileFilter.should_exclude_file(p, patterns) is True, p
        for p in ["testdata/foo", "a/testdata/foo", "README.md", "docs/x.md"]:
            assert SrcFileFilter.should_exclude_file(p, patterns) is True, p
        assert SrcFileFilter.should_exclude_file("src/main.go", patterns) is False

    def test_empty_patterns(self):
        assert SrcFileFilter.should_exclude_file("any/path.py", []) is False


class TestSNormSrcMembership:
    def test_bare_prefix_does_not_match_sibling(self):
        # The footgun S-norm fixes: bare "core" must not match "core_extra/".
        assert SrcFileFilter.in_src_dirs("core/x.go", ["core"]) is True
        assert SrcFileFilter.in_src_dirs("core_extra/x.go", ["core"]) is False

    def test_trailing_slash_configs(self):
        assert SrcFileFilter.in_src_dirs("src/a.ts", ["src/", "res/css/"]) is True
        assert SrcFileFilter.in_src_dirs("res/css/a.pcss", ["src/", "res/css/"]) is True
        assert SrcFileFilter.in_src_dirs("res/other/a", ["src/", "res/css/"]) is False


class TestConvergedConsumersDelegate:
    """Production classifiers must produce identical verdicts to SrcFileFilter."""

    SAMPLE = [
        "src/main.py",
        "a/feature.spec.ts/snap.png",
        "tests/test_x.py",
        "pkg/foo_test.go",
        "docs/readme.md",
        "core_extra/x.go",
    ]
    PATTERNS = ["**/tests/**", "**/*_test.go", "**/*.spec.*", "**/*.md"]

    def test_commit_dag_should_exclude_file_matches_canonical(self):
        from deepcommit.preprocess.commit_dag.commit_dag import should_exclude_file

        for p in self.SAMPLE:
            assert should_exclude_file(p, self.PATTERNS) == SrcFileFilter.should_exclude_file(p, self.PATTERNS), p

    def test_milestone_enrichment_helpers_delegate(self):
        from deepcommit.pipeline.stages import milestone_enrichment as me

        for p in self.SAMPLE:
            assert me._should_exclude_file(p, self.PATTERNS) == SrcFileFilter.should_exclude_file(p, self.PATTERNS), p
            assert me._is_test_path(p, self.PATTERNS) == SrcFileFilter.should_exclude_file(p, self.PATTERNS), p


class TestNoHandRolledEngineRemains:
    """Guard: the duplicate ``_glob_match`` engine must not be re-introduced."""

    def test_modules_have_no_glob_match(self):
        import importlib

        for mod_name in [
            "deepcommit.preprocess.commit_dag.commit_dag",
            "deepcommit.pipeline.stages.milestone_enrichment",
            "deepcommit.utils.git.local_git_repo",
            "deepcommit.tools.milestone_balancing.identify_imbalances",
            "deepcommit.tools.milestone_balancing.generate_split_metadata",
        ]:
            mod = importlib.import_module(mod_name)
            assert not hasattr(mod, "_glob_match"), f"{mod_name} re-introduced a hand-rolled _glob_match"
