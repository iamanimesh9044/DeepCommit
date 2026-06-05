"""
Source file filter utilities.

Provides a reusable SrcFileFilter class for determining whether a file path
is a source file based on configuration from metadata.json.

Configuration Assumptions
=========================

The filter expects three configuration fields from metadata.json:

1. repo_src_dirs (required): List[str]
   - Simple directory paths, NOT glob patterns
   - Used with prefix matching (str.startswith)
   - Trailing slash is optional (will be normalized)
   - Examples: ["src/"], ["crates/"], ["core/", "gateway/", "rest/"]

2. test_dirs (required): List[str]
   - Glob patterns using gitwildmatch syntax (same as git/gitignore)
   - Matched from repository root directory
   - Examples: ["tests/**", "crates/*/tests/**", "**/test_*.py"]

3. exclude_patterns (optional): List[str]
   - Glob patterns using gitwildmatch syntax (same as git/gitignore)
   - Matched from repository root directory
   - Examples: ["crates/*/examples/**", "**/testdata/**"]

4. generated_patterns (optional): List[str]
   - Glob patterns for generated code files (e.g., protobuf, wire)
   - These files are excluded from agent modification but included in snapshots
   - Used by should_include_in_snapshot() to ensure generated code is available for compilation
   - Examples: ["**/*.pb.go", "**/wire_gen.go"]

5. modifiable_test_patterns (optional): List[str]
   - Glob patterns for test files that the agent is allowed to modify
   - These test files are included in snapshots (not filtered out)
   - Use case: test files with //go:build ignore that need to be enabled
   - Examples: ["**/agents_plugin_test.go", "**/integration_test.go"]
"""

from functools import lru_cache
from typing import List, Literal, Optional, Tuple

import pathspec

FileClass = Literal[
    "python_src",
    "java_src",
    "go_src",
    "js_src",
    "rust_src",
    "rust_inline_test",
    "test_content",
    "test_infrastructure",
    "non_src_non_test",
]


# Map filename extensions to the corresponding `{lang}_src` FileClass.
# Unknown extensions in src dirs fall through to `non_src_non_test` (conservative).
_LANGUAGE_SRC_BY_EXTENSION = {
    ".py": "python_src",
    ".java": "java_src",
    ".kt": "java_src",  # Kotlin uses Java tooling for milestone purposes
    ".go": "go_src",
    ".js": "js_src",
    ".jsx": "js_src",
    ".ts": "js_src",
    ".tsx": "js_src",
    ".mjs": "js_src",
    ".cjs": "js_src",
    ".rs": "rust_src",
}


# Sensible defaults when metadata.json does not specify test_infra_patterns.
# Covers the most common test-infrastructure file shapes across Python/JS/Go/Rust/Java.
DEFAULT_TEST_INFRA_PATTERNS: List[str] = [
    "**/conftest.py",
    "**/runtests.py",
    "**/setup_test_environment.py",
    "**/test_helpers/**",
    "**/jest.setup.js",
    "**/jest.setup.ts",
    "**/jest.config.js",
    "**/jest.config.ts",
    "**/setup.go",
    "**/test_helper.rb",
    "**/build.rs",
]


class SrcFileFilter:
    """Filter for identifying source files vs test/excluded files.

    Uses pathspec library with gitwildmatch pattern (same as git/gitignore).

    Pattern syntax (same as gitignore):
    - `tests/**` matches files under root `tests/` directory only
    - `**/tests/**` matches `tests/` directory at any level
    - `crates/*/tests/**` matches `crates/X/tests/` but not `crates/tests/`
    - `*` matches anything except `/`
    - `**` matches anything including `/`

    Example:
        filter = SrcFileFilter(
            src_dirs=["crates"],
            test_dirs=["tests/**", "crates/*/tests/**", "crates/*/benches/**"],
            exclude_patterns=["crates/*/examples/**"]
        )
        filter.is_src_file("crates/printer/src/json.rs")  # True
        filter.is_src_file("crates/printer/tests/test.rs")  # False
        filter.is_src_file("tests/integration.rs")  # False (test file)
    """

    def __init__(
        self,
        src_dirs: List[str],
        test_dirs: List[str],
        exclude_patterns: Optional[List[str]] = None,
        generated_patterns: Optional[List[str]] = None,
        modifiable_test_patterns: Optional[List[str]] = None,
        test_infra_patterns: Optional[List[str]] = None,
    ):
        """Initialize the filter.

        Args:
            src_dirs: Source directories (e.g., ["src/", "crates/"]). Required.
            test_dirs: Test directory/file patterns (e.g., ["tests/**", "**/test_*.py"]). Required.
            exclude_patterns: Patterns to exclude (e.g., ["**/examples/**"]). Optional.
            generated_patterns: Patterns for generated code files (e.g., ["**/*.pb.go"]). Optional.
            modifiable_test_patterns: Patterns for test files agent can modify (e.g., ["**/plugin_test.go"]). Optional.
            test_infra_patterns: Test-infrastructure file patterns that should be classified as `test_infrastructure`
                instead of `test_content` (e.g. ["**/conftest.py", "**/runtests.py"]). Used by ``classify()`` only.
                If None or empty, falls back to ``DEFAULT_TEST_INFRA_PATTERNS``.
        """
        # Source directories - normalize to have trailing slash
        self.src_dirs = [d if d.endswith("/") else d + "/" for d in src_dirs]

        # Test directories/patterns
        self.test_dirs = test_dirs

        # Exclude patterns
        self.exclude_patterns = exclude_patterns if exclude_patterns else []

        # Generated code patterns (for snapshot inclusion)
        self.generated_patterns = generated_patterns if generated_patterns else []

        # Modifiable test patterns (test files that should be included in snapshot)
        self.modifiable_test_patterns = modifiable_test_patterns if modifiable_test_patterns else []

        # Test infrastructure patterns (test files modifiable per HOW-layer rules).
        # Falls back to a sensible default if not explicitly provided.
        self.test_infra_patterns = test_infra_patterns if test_infra_patterns else list(DEFAULT_TEST_INFRA_PATTERNS)

        # Create pathspec matchers using gitwildmatch (same as git)
        self._test_spec = pathspec.PathSpec.from_lines("gitwildmatch", self.test_dirs) if self.test_dirs else None

        self._exclude_spec = (
            pathspec.PathSpec.from_lines("gitwildmatch", self.exclude_patterns) if self.exclude_patterns else None
        )

        self._generated_spec = (
            pathspec.PathSpec.from_lines("gitwildmatch", self.generated_patterns) if self.generated_patterns else None
        )

        self._modifiable_test_spec = (
            pathspec.PathSpec.from_lines("gitwildmatch", self.modifiable_test_patterns)
            if self.modifiable_test_patterns
            else None
        )

        self._test_infra_spec = (
            pathspec.PathSpec.from_lines("gitwildmatch", self.test_infra_patterns) if self.test_infra_patterns else None
        )

    def match_pattern(self, filepath: str, pattern: str) -> bool:
        """Match filepath against a glob pattern.

        Patterns are matched from the root directory (like git pathspec).

        Args:
            filepath: File path to check (e.g., "crates/core/tests/test.rs")
            pattern: Glob pattern (e.g., "tests/**", "crates/*/tests/**")

        Returns:
            True if filepath matches the pattern
        """
        spec = pathspec.PathSpec.from_lines("gitwildmatch", [pattern])
        return spec.match_file(filepath)

    def is_excluded(self, filepath: str) -> bool:
        """Check if a file path matches any exclude pattern.

        Args:
            filepath: File path to check

        Returns:
            True if the file should be excluded
        """
        if self._exclude_spec is None:
            return False
        return self._exclude_spec.match_file(filepath)

    def is_test_file(self, filepath: str) -> bool:
        """Check if a file path matches any test directory pattern.

        Args:
            filepath: File path to check

        Returns:
            True if the file is a test file
        """
        if self._test_spec is None:
            return False
        return self._test_spec.match_file(filepath)

    def is_src_file(self, filepath: str) -> bool:
        """Check if a file path is a source file.

        A file is considered a source file if:
        1. It is within any of the source directories
        2. It is NOT matched by any exclude pattern
        3. It is NOT a test file

        Args:
            filepath: File path to check

        Returns:
            True if the file is a source file
        """
        # First check if file is in any src directory
        in_src = any(filepath.startswith(src_dir) for src_dir in self.src_dirs)
        if not in_src:
            return False

        # Then check if it's excluded
        if self.is_excluded(filepath):
            return False

        # Also exclude test files
        if self.is_test_file(filepath):
            return False

        return True

    def is_test_infra(self, filepath: str) -> bool:
        """Return True iff ``filepath`` matches a ``test_infra_patterns`` entry.

        Test-infrastructure files (e.g. ``conftest.py``, ``tests/runtests.py``,
        ``jest.setup.js``) live under test directories but are HOW-layer:
        scaffolding hooks, runner harness, setup-only fixtures. They are
        modifiable; only Test Content (assertion bodies, parametrize data,
        assertion-target fixture models) is immutable.
        """
        if self._test_infra_spec is None:
            return False
        return self._test_infra_spec.match_file(filepath)

    def classify(self, filepath: str) -> FileClass:
        """Classify a file path into one of nine ``FileClass`` labels.

        Used by ``evaluate_per_class_thresholds`` to pick the right numstat
        threshold (or skip thresholding entirely for unchecked classes). See
        ``module2_lint_class_threshold_plan.md`` for the semantics of each
        label.

        Classification order (first match wins):

        1. ``test_infrastructure`` — matches ``test_infra_patterns`` (e.g.
           ``**/conftest.py``). Always wins regardless of whether the path
           lives under ``test_dirs`` or elsewhere.
        2. ``test_content`` — matches ``test_dirs`` and not classified as
           test_infrastructure.
        3. ``non_src_non_test`` (excluded) — matches ``exclude_patterns``.
        4. ``{lang}_src`` — under a ``src_dirs`` directory; language inferred
           by file extension. Unknown extensions in src dirs fall through to
           ``non_src_non_test`` (conservative — pipeline does not threshold
           files it doesn't recognize as source).
        5. ``non_src_non_test`` — anything else (e.g. ``/tmp/sitecustomize.py``,
           new Dockerfile-added config files outside src/test dirs).

        Note on ``rust_inline_test``: file-level classify cannot distinguish
        Rust src from inline ``#[cfg(test)]`` ranges in the same file —
        ``classify()`` returns ``rust_src`` for any ``.rs`` file under src
        dirs, and ``evaluate_per_class_thresholds`` performs hunk-level
        refinement via ``find_test_code_ranges`` to split into ``rust_src``
        vs ``rust_inline_test`` per hunk.
        """
        # 1. Test infrastructure takes precedence over test_content classification.
        if self.is_test_infra(filepath):
            return "test_infrastructure"

        # 2. Test directory matches (but not infrastructure) → content.
        if self.is_test_file(filepath):
            return "test_content"

        # 3. Excluded paths fall outside the threshold-checked classes.
        if self.is_excluded(filepath):
            return "non_src_non_test"

        # 4. Inside src dirs: language by extension.
        in_src = any(filepath.startswith(src_dir) for src_dir in self.src_dirs)
        if in_src:
            for ext, label in _LANGUAGE_SRC_BY_EXTENSION.items():
                if filepath.endswith(ext):
                    return label  # type: ignore[return-value]
            # Unknown extension under src — conservative: unchecked.
            return "non_src_non_test"

        # 5. Outside src and test dirs (e.g. /tmp/sitecustomize.py).
        return "non_src_non_test"

    def is_generated_file(self, filepath: str) -> bool:
        """Check if a file path matches any generated code pattern.

        Generated files (e.g., protobuf, wire) should be:
        - Excluded from agent modification (via exclude_patterns)
        - But included in snapshots for compilation

        Args:
            filepath: File path to check

        Returns:
            True if the file is a generated code file
        """
        if self._generated_spec is None:
            return False
        return self._generated_spec.match_file(filepath)

    def is_modifiable_test_file(self, filepath: str) -> bool:
        """Check if a file path matches any modifiable test pattern.

        Modifiable test files are test files that:
        - The agent is allowed to modify (e.g., to remove //go:build ignore)
        - Should be included in snapshots (not filtered out)

        Args:
            filepath: File path to check

        Returns:
            True if the file is a modifiable test file
        """
        if self._modifiable_test_spec is None:
            return False
        return self._modifiable_test_spec.match_file(filepath)

    def should_include_in_snapshot(self, filepath: str) -> bool:
        """Check if a file should be included in the evaluation snapshot.

        A file should be included if:
        1. It is a source file (is_src_file), OR
        2. It is a generated code file (is_generated_file) AND in a source directory, OR
        3. It is a modifiable test file (is_modifiable_test_file) AND in a source directory

        This ensures:
        - Generated files like .pb.go are included for compilation
        - Test files that agent needs to modify are included

        Args:
            filepath: File path to check

        Returns:
            True if the file should be included in the snapshot
        """
        # Include regular source files
        if self.is_src_file(filepath):
            return True

        # Check if file is in a source directory (needed for generated/modifiable test files)
        in_src = any(filepath.startswith(src_dir) for src_dir in self.src_dirs)
        if not in_src:
            return False

        # Include generated files in source directories (must not be a test file)
        if self.is_generated_file(filepath):
            if not self.is_test_file(filepath):
                return True

        # Include modifiable test files in source directories
        if self.is_modifiable_test_file(filepath):
            return True

        return False

    # ==================== Static Methods for deepcommit ====================
    # These methods provide standalone functionality without requiring
    # full SrcFileFilter instantiation, useful for preprocessing pipelines.

    @staticmethod
    def build_exclude_patterns(
        test_dirs: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
    ) -> List[str]:
        """Combine test_dirs and exclude patterns into a unified list.

        This is useful for preprocessing pipelines that need a single list
        of patterns to filter out test files and other excluded files.

        Args:
            test_dirs: Test directory/file patterns (e.g., ["tests/**", "**/test_*.py"])
            exclude: Additional exclude patterns (e.g., ["**/locale/**", "**/static/**"])

        Returns:
            Combined list of all exclude patterns
        """
        all_patterns: List[str] = []
        if test_dirs:
            all_patterns.extend(test_dirs)
        if exclude:
            all_patterns.extend(exclude)
        return all_patterns

    @staticmethod
    @lru_cache(maxsize=8192)
    def _compile_spec(patterns: Tuple[str, ...]) -> pathspec.PathSpec:
        """Compile (and cache) a gitwildmatch PathSpec from a tuple of patterns.

        Cached so the standalone ``should_exclude_file`` can be called in
        per-file hot loops (preprocess / milestone enrichment) without
        recompiling the spec each call.
        """
        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)

    @staticmethod
    def should_exclude_file(filepath: str, exclude_patterns: List[str]) -> bool:
        """Check if a file path matches any exclude pattern.

        Uses pathspec library with gitwildmatch (same as git/gitignore). This is
        the single canonical exclude/test glob engine — every Phase 1-3 consumer
        routes through it.

        Args:
            filepath: File path to check (repo-relative)
            exclude_patterns: List of glob patterns to match against

        Returns:
            True if the file should be excluded, False otherwise
        """
        if not exclude_patterns:
            return False
        return SrcFileFilter._compile_spec(tuple(exclude_patterns)).match_file(filepath)

    @staticmethod
    def in_src_dirs(filepath: str, src_dirs: List[str]) -> bool:
        """Canonical S-norm source-dir membership (trailing-slash anchored).

        Mirrors the instance src-membership rule (``__init__`` normalizes each
        ``src_dir`` to a trailing slash, then ``is_src_file`` does
        ``startswith``). Use this everywhere a standalone ``startswith`` src
        check is needed so preprocess (Phase 1) and harness (Phase 3) agree on
        the boundary — a bare ``startswith("core")`` would wrongly match
        ``core_extra/...``.
        """
        return any(filepath.startswith(d.rstrip("/") + "/") for d in src_dirs)
