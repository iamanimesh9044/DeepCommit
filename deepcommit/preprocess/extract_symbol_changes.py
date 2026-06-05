#!/usr/bin/env python3
"""
Symbol Change Extractor using ast-grep

Extracts added/modified/deleted symbols (classes, functions, methods) from commits
using ast-grep for multi-language AST parsing.

Similar to harness/commit_level/extract_commit_test_changes.py but:
- Uses ast-grep instead of Python AST
- Supports multiple languages (Python, Java, Go, Rust, TypeScript, etc.)
- Configuration-driven via symbol_extraction_config.yml

Output format:
{
  "sha": "abc1234",
  "short_sha": "abc1234",
  "files": [
    {
      "file_path": "src/Foo.java",
      "old_path": null,
      "status": "M",
      "language": "java",
      "symbols_added": [{"name": "newMethod", "type": "method"}],
      "symbols_modified": [{"name": "existingMethod", "type": "method"}],
      "symbols_deleted": [{"name": "oldMethod", "type": "method"}]
    }
  ]
}
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pygit2
import yaml


def matches_any_pattern(file_path: str, patterns: List[str]) -> bool:
    """Check if file path matches any of the glob patterns."""
    for pattern in patterns:
        if fnmatch.fnmatch(file_path, pattern):
            return True
        # Also try matching just the filename
        if fnmatch.fnmatch(Path(file_path).name, pattern):
            return True
    return False


def normalize_whitespace(content: str) -> str:
    """Normalize whitespace for semantic comparison.

    This helps detect when code is semantically identical but formatted differently
    (e.g., rustfmt changes). We normalize by:
    1. Removing leading/trailing whitespace from each line
    2. Collapsing multiple spaces/tabs into single space
    3. Removing empty lines
    """
    lines = []
    for line in content.splitlines():
        # Strip leading/trailing whitespace
        stripped = line.strip()
        if stripped:  # Skip empty lines
            # Collapse multiple whitespace into single space
            normalized = re.sub(r"\s+", " ", stripped)
            lines.append(normalized)
    return "\n".join(lines)


@dataclass
class SymbolInfo:
    """Information about a symbol including line range."""

    name: str
    symbol_type: str
    start_line: int
    end_line: int
    mod_test_name: Optional[str] = None  # Actual mod test/tests name if inside such block

    def __hash__(self):
        return hash((self.name, self.symbol_type))

    def __eq__(self, other):
        if not isinstance(other, SymbolInfo):
            return False
        return self.name == other.name and self.symbol_type == other.symbol_type


@dataclass
class GinkgoNode:
    """A Ginkgo BDD node (Describe, Context, When, It, etc.)."""

    node_type: str  # "Describe", "Context", "When", "It", etc.
    description: str
    start_line: int
    end_line: int
    children: List["GinkgoNode"] = field(default_factory=list)


@dataclass
class GinkgoTestCase:
    """A Ginkgo test case with full hierarchy path."""

    hierarchy: List[str]  # ["Extractor", "Error Checking", "does not throw..."]
    start_line: int
    end_line: int

    @property
    def full_path(self) -> str:
        """Return full hierarchy path joined by ' > '."""
        return " > ".join(self.hierarchy)


@dataclass
class JestTestCase:
    """A Jest/Vitest BDD test case with full hierarchy path."""

    hierarchy: List[str]  # ["<ActivityPanel />", "clears the error icon..."]
    start_line: int
    end_line: int

    @property
    def full_path(self) -> str:
        """Return full hierarchy path joined by ' > ' (matching Jest reporter format)."""
        return " > ".join(self.hierarchy)


@dataclass
class FileSymbolChanges:
    """Symbol changes for a single file."""

    file_path: str
    old_path: Optional[str]
    status: str  # 'A' (added), 'M' (modified), 'D' (deleted), 'R' (renamed)
    language: str
    symbols_added: List[Dict] = field(default_factory=list)
    symbols_modified: List[Dict] = field(default_factory=list)
    symbols_deleted: List[Dict] = field(default_factory=list)
    # All class/interface symbols in the file (for finding containing class of methods)
    all_classes: List[Dict] = field(default_factory=list)
    # Ginkgo BDD test cases (for Go test files)
    ginkgo_tests_before: List[GinkgoTestCase] = field(default_factory=list)
    ginkgo_tests_after: List[GinkgoTestCase] = field(default_factory=list)
    # Jest/Vitest BDD test cases (for JavaScript/TypeScript test files)
    jest_tests_before: List[JestTestCase] = field(default_factory=list)
    jest_tests_after: List[JestTestCase] = field(default_factory=list)


@dataclass
class CommitSymbolChanges:
    """Symbol changes for a commit."""

    sha: str
    short_sha: str
    files: List[FileSymbolChanges] = field(default_factory=list)
    # Go module name (e.g., "github.com/navidrome/navidrome") for generating full test IDs
    go_module_name: Optional[str] = None
    # Mapping from package path to suite test function name (e.g., "core/scrobbler" -> "TestAgents")
    go_suite_functions: Dict[str, str] = field(default_factory=dict)


class SymbolExtractionConfig:
    """Load and manage symbol extraction configuration from YAML."""

    def __init__(self, config_path: str):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.language_extensions = self.config.get("language_extensions", {})
        self.definition_patterns = self.config.get("definition_patterns", {})

        # Build extension to language map
        self._ext_to_lang = {}
        for lang, exts in self.language_extensions.items():
            for ext in exts:
                self._ext_to_lang[ext] = lang

    def get_language(self, file_path: str) -> str:
        """Detect language from file extension."""
        ext = Path(file_path).suffix.lower()
        return self._ext_to_lang.get(ext, "unknown")

    def get_file_extension(self, language: str) -> str:
        """Get file extension for a language."""
        exts = self.language_extensions.get(language, [])
        return exts[0] if exts else f".{language}"

    def get_definition_patterns(self, language: str) -> Dict[str, Tuple[str, str]]:
        """Get definition patterns for a language.

        Returns:
            Dict mapping pattern_name to (ast_pattern, regex_pattern) tuple
        """
        lang_patterns = self.definition_patterns.get(language, {})
        # Convert list format [ast_pattern, regex_pattern] to tuple
        return {
            name: (patterns[0], patterns[1])
            for name, patterns in lang_patterns.items()
            if isinstance(patterns, list) and len(patterns) >= 2
        }

    def has_patterns_for_language(self, language: str) -> bool:
        """Check if we have definition patterns for a language."""
        return language in self.definition_patterns


class CommitSymbolChangeExtractor:
    """Extract symbol changes from commits using ast-grep."""

    def __init__(self, repo_path: str, config_path: str):
        self.repo_path = Path(repo_path)
        self.repo = pygit2.Repository(str(self.repo_path))
        self.config = SymbolExtractionConfig(config_path)
        # Store config directory for finding rule files
        self.config_dir = Path(config_path).parent
        self.rust_rules_path = self.config_dir / "rust_test_rules.yml"
        self.rust_macro_rules_path = self.config_dir / "rust_macro_test_rules.yml"
        self.go_rules_path = self.config_dir / "go_test_rules.yml"
        self.go_ginkgo_rules_path = self.config_dir / "go_ginkgo_rules.yml"
        self.js_jest_rules_path = self.config_dir / "js_jest_rules.yml"
        # Cache for Go module name
        self._go_module_name: Optional[str] = None

    def get_go_module_name(self) -> Optional[str]:
        """Get the Go module name from go.mod file.

        Returns:
            Module name like "github.com/navidrome/navidrome" or None if not found
        """
        if self._go_module_name is not None:
            return self._go_module_name if self._go_module_name else None

        go_mod_path = self.repo_path / "go.mod"
        if not go_mod_path.exists():
            self._go_module_name = ""
            return None

        try:
            with open(go_mod_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("module "):
                        self._go_module_name = line[7:].strip()
                        return self._go_module_name
        except Exception:
            pass

        self._go_module_name = ""
        return None

    def find_go_suite_function(self, pkg_path: str) -> Optional[str]:
        """Find the Go test suite entry function name for a package.

        Looks for *_suite_test.go files in the package directory and extracts
        the Test* function name (e.g., TestCore, TestAgents, TestNativeApi).

        Args:
            pkg_path: Package path relative to repo root (e.g., "core/scrobbler")

        Returns:
            Test function name like "TestAgents" or None if not found
        """
        pkg_dir = self.repo_path / pkg_path
        if not pkg_dir.exists() or not pkg_dir.is_dir():
            return None

        # Look for *_suite_test.go files
        suite_files = list(pkg_dir.glob("*_suite_test.go"))
        if not suite_files:
            return None

        # Parse the first suite file to find Test* function
        for suite_file in suite_files:
            try:
                with open(suite_file, "r") as f:
                    content = f.read()
                # Look for func Test*(t *testing.T) pattern
                match = re.search(r"func\s+(Test\w+)\s*\(\s*t\s+\*testing\.T\s*\)", content)
                if match:
                    return match.group(1)
            except Exception:
                pass

        return None

    def extract_commit_changes(
        self,
        commit_sha: str,
        include_patterns: Optional[List[str]] = None,
    ) -> CommitSymbolChanges:
        """Extract symbol changes for a commit.

        Args:
            commit_sha: Git commit SHA
            include_patterns: If provided, only process files matching these glob patterns
                             e.g., ["**/test/**", "*Test.java", "test_*.py"]
        """
        commit = self.repo.revparse_single(commit_sha)
        assert isinstance(commit, pygit2.Commit)

        full_sha = str(commit.id)
        short_sha = full_sha[:7]

        parent = commit.parents[0] if commit.parents else None
        parent_tree = parent.tree if parent else None

        diff = self.repo.diff(parent_tree, commit.tree)
        # Enable rename detection (similar to git's -M flag)
        diff.find_similar()

        result = CommitSymbolChanges(
            sha=full_sha,
            short_sha=short_sha,
            go_module_name=self.get_go_module_name(),
            go_suite_functions={},
        )

        # Track Go packages that have test file changes (for finding suite functions later)
        go_test_packages: Set[str] = set()

        for patch in diff:
            file_change = self._analyze_file_patch(commit, parent, patch, include_patterns)
            if file_change:
                # For Go test files, always include them even without symbol changes
                # because Ginkgo It()/Describe() changes don't affect function signatures
                is_go_test_file = file_change.file_path.endswith("_test.go")
                # For JS/TS test files, always include them even without symbol changes
                # because Jest describe()/it() changes don't affect function signatures
                is_js_test_file = _is_js_test_file(file_change.file_path)
                has_symbol_changes = self._has_changes(file_change)
                has_jest_tests = file_change.jest_tests_before or file_change.jest_tests_after

                if has_symbol_changes or is_go_test_file or (is_js_test_file and has_jest_tests):
                    result.files.append(file_change)
                    # Track Go test file packages for suite function lookup
                    if is_go_test_file:
                        pkg_path = os.path.dirname(file_change.file_path)
                        if pkg_path:
                            go_test_packages.add(pkg_path)

        # Find suite functions for all Go packages with test file changes
        for pkg_path in go_test_packages:
            suite_fn = self.find_go_suite_function(pkg_path)
            if suite_fn:
                result.go_suite_functions[pkg_path] = suite_fn

        return result

    def _has_changes(self, file_change: FileSymbolChanges) -> bool:
        """Check if file has any symbol changes."""
        return bool(file_change.symbols_added or file_change.symbols_modified or file_change.symbols_deleted)

    def _analyze_file_patch(
        self,
        commit: pygit2.Commit,
        parent: Optional[pygit2.Commit],
        patch: pygit2.Patch,
        include_patterns: Optional[List[str]] = None,
    ) -> Optional[FileSymbolChanges]:
        """Analyze a single file patch for symbol changes."""
        delta = patch.delta
        status = self._get_status_char(delta.status)

        new_path = delta.new_file.path
        old_path = delta.old_file.path if status == "R" else None

        # Filter by include patterns if provided
        if include_patterns:
            if not matches_any_pattern(new_path, include_patterns):
                return None

        language = self.config.get_language(new_path)
        if language == "unknown":
            return None

        # Check if we have patterns for this language
        if not self.config.has_patterns_for_language(language):
            return None

        file_change = FileSymbolChanges(
            file_path=new_path,
            old_path=old_path,
            status=status,
            language=language,
        )

        if status == "A":
            # File added - all symbols are new
            content_after = self._get_file_content(commit, new_path)
            if content_after:
                symbols_after = self._extract_symbols(content_after, language, file_path=new_path)
                file_change.symbols_added = [
                    {"name": s.name, "type": s.symbol_type, "mod_test_name": s.mod_test_name} for s in symbols_after
                ]
                # Extract Ginkgo test cases for added Go test files
                if language == "go" and new_path.endswith("_test.go"):
                    file_change.ginkgo_tests_after = self.extract_ginkgo_test_cases(content_after)
                # Extract Jest test cases for added JavaScript/TypeScript test files
                if language in ("javascript", "typescript") and _is_js_test_file(new_path):
                    file_ext = Path(new_path).suffix
                    file_change.jest_tests_after = self.extract_jest_test_cases(content_after, file_ext)

        elif status == "D":
            # File deleted - all symbols are deleted
            if parent:
                content_before = self._get_file_content(parent, new_path)
                if content_before:
                    symbols_before = self._extract_symbols(content_before, language, file_path=new_path)
                    file_change.symbols_deleted = [
                        {"name": s.name, "type": s.symbol_type, "mod_test_name": s.mod_test_name}
                        for s in symbols_before
                    ]
                    # Extract Ginkgo test cases for deleted Go test files
                    if language == "go" and new_path.endswith("_test.go"):
                        file_change.ginkgo_tests_before = self.extract_ginkgo_test_cases(content_before)
                    # Extract Jest test cases for deleted JavaScript/TypeScript test files
                    if language in ("javascript", "typescript") and _is_js_test_file(new_path):
                        file_ext = Path(new_path).suffix
                        file_change.jest_tests_before = self.extract_jest_test_cases(content_before, file_ext)

        elif status == "R":
            # File renamed
            self._compare_files(file_change, commit, parent, new_path, old_path, language, patch)

        elif status in ("M", "C"):
            # File modified or copied
            self._compare_files(file_change, commit, parent, new_path, new_path, language, patch)

        return file_change

    def _compare_files(
        self,
        file_change: FileSymbolChanges,
        commit: pygit2.Commit,
        parent: Optional[pygit2.Commit],
        new_path: str,
        old_path: Optional[str],
        language: str,
        patch: pygit2.Patch,
    ) -> None:
        """Compare symbols between two versions of a file."""
        content_after = self._get_file_content(commit, new_path)
        content_before = None
        if parent and old_path:
            content_before = self._get_file_content(parent, old_path)

        symbols_after = []
        symbols_before = []

        if content_after:
            symbols_after = self._extract_symbols(content_after, language, file_path=new_path)
        if content_before:
            symbols_before = self._extract_symbols(content_before, language, file_path=old_path)

        # Store all class/interface symbols from the file (for finding containing class)
        file_change.all_classes = [
            {
                "name": s.name,
                "type": s.symbol_type,
                "start_line": s.start_line,
                "end_line": s.end_line,
            }
            for s in symbols_after
            if s.symbol_type.startswith("class") or s.symbol_type.startswith("interface")
        ]

        # Fallback: if no class found by ast-grep (e.g., annotated classes), use regex
        if not file_change.all_classes and content_after and language == "java":
            file_change.all_classes = self._extract_java_classes_by_regex(content_after)

        # Extract Ginkgo BDD test cases for Go test files
        if language == "go" and new_path.endswith("_test.go"):
            if content_after:
                file_change.ginkgo_tests_after = self.extract_ginkgo_test_cases(content_after)
            if content_before:
                file_change.ginkgo_tests_before = self.extract_ginkgo_test_cases(content_before)

        # Extract Jest/Vitest BDD test cases for JavaScript/TypeScript test files
        if language in ("javascript", "typescript") and _is_js_test_file(new_path):
            file_ext = Path(new_path).suffix
            if content_after:
                file_change.jest_tests_after = self.extract_jest_test_cases(content_after, file_ext)
            if content_before:
                file_change.jest_tests_before = self.extract_jest_test_cases(content_before, file_ext)

        # Build name -> symbol maps
        after_by_name = {s.name: s for s in symbols_after}
        before_by_name = {s.name: s for s in symbols_before}

        after_names = set(after_by_name.keys())
        before_names = set(before_by_name.keys())

        # Added: in after but not in before
        added_names = after_names - before_names
        file_change.symbols_added = [
            {
                "name": name,
                "type": after_by_name[name].symbol_type,
                "start_line": after_by_name[name].start_line,
                "end_line": after_by_name[name].end_line,
                "mod_test_name": after_by_name[name].mod_test_name,
            }
            for name in sorted(added_names)
        ]

        # Deleted: in before but not in after
        deleted_names = before_names - after_names
        file_change.symbols_deleted = [
            {
                "name": name,
                "type": before_by_name[name].symbol_type,
                "start_line": before_by_name[name].start_line,
                "end_line": before_by_name[name].end_line,
                "mod_test_name": before_by_name[name].mod_test_name,
            }
            for name in sorted(deleted_names)
        ]

        # Modified: in both, check if diff overlaps with symbol range
        common_names = after_names & before_names
        if common_names and content_after:
            added_lines, deleted_lines = self._get_modified_lines(patch, language)
            modified_names = []

            # Split content into lines for content comparison
            content_after_lines = content_after.splitlines() if content_after else []
            content_before_lines = content_before.splitlines() if content_before else []

            for name in common_names:
                is_modified = False

                # Check added lines against new file symbol ranges
                symbol_after = after_by_name[name]
                for line_no in added_lines:
                    if symbol_after.start_line <= line_no <= symbol_after.end_line:
                        is_modified = True
                        break

                # Check deleted lines against old file symbol ranges
                if not is_modified and name in before_by_name:
                    symbol_before = before_by_name[name]
                    for line_no in deleted_lines:
                        if symbol_before.start_line <= line_no <= symbol_before.end_line:
                            is_modified = True
                            break

                # Verify by comparing actual content (to handle pygit2 line origin bugs
                # and rustfmt-only changes)
                if is_modified and name in before_by_name:
                    symbol_before = before_by_name[name]
                    # Extract symbol content from both versions
                    try:
                        old_content = "\n".join(
                            content_before_lines[symbol_before.start_line - 1 : symbol_before.end_line]
                        )
                        new_content = "\n".join(
                            content_after_lines[symbol_after.start_line - 1 : symbol_after.end_line]
                        )
                        # If content is identical, it's a false positive
                        if old_content == new_content:
                            is_modified = False
                        # Also check with whitespace normalization (catches rustfmt changes)
                        elif normalize_whitespace(old_content) == normalize_whitespace(new_content):
                            is_modified = False
                    except (IndexError, TypeError):
                        pass  # Keep is_modified as True if we can't compare

                if is_modified:
                    modified_names.append(name)

            file_change.symbols_modified = [
                {
                    "name": name,
                    "type": after_by_name[name].symbol_type,
                    "start_line": after_by_name[name].start_line,
                    "end_line": after_by_name[name].end_line,
                    "mod_test_name": after_by_name[name].mod_test_name,
                }
                for name in sorted(modified_names)
            ]

    def _get_modified_lines(self, patch: pygit2.Patch, language: str) -> Tuple[Set[int], Set[int]]:
        """Get line numbers that were added or deleted.

        Filters out comment-only changes to avoid false positives.

        Returns:
            Tuple of (added_lines_in_new_file, deleted_lines_in_old_file)
        """
        # Collect all changed lines (both + and -)
        added_lines = []  # (new_lineno, content)
        removed_lines = []  # (old_lineno, content)

        for hunk in patch.hunks:
            for line in hunk.lines:
                content = line.content.strip()
                if line.origin == "+":
                    added_lines.append((line.new_lineno, content))
                elif line.origin == "-":
                    removed_lines.append((line.old_lineno, content))

        # Check if ALL changes are comment-only
        all_changes_are_comments = True
        for _, content in added_lines:
            if not self._is_comment_line(content, language):
                all_changes_are_comments = False
                break

        if all_changes_are_comments:
            for _, content in removed_lines:
                if not self._is_comment_line(content, language):
                    all_changes_are_comments = False
                    break

        # If all changes are comments, return empty sets (no real modifications)
        if all_changes_are_comments and (added_lines or removed_lines):
            return set(), set()

        # Return both added and deleted line numbers
        added_line_nos = {line_no for line_no, _ in added_lines}
        deleted_line_nos = {line_no for line_no, _ in removed_lines}

        return added_line_nos, deleted_line_nos

    def _extract_java_classes_by_regex(self, content: str) -> List[Dict]:
        """Extract Java class declarations using regex (fallback for annotated classes).

        This handles cases where ast-grep pattern matching fails due to annotations
        like @SuppressWarnings, @Deprecated, etc. before the class declaration.
        """
        classes = []
        lines = content.split("\n")

        # Track brace depth to find class end
        class_pattern = re.compile(
            r"^\s*(?:public\s+|private\s+|protected\s+)?(?:abstract\s+|final\s+|static\s+)*"
            r"(?:class|interface|enum)\s+(\w+)"
        )

        i = 0
        while i < len(lines):
            line = lines[i]
            match = class_pattern.search(line)
            if match:
                class_name = match.group(1)
                start_line = i + 1  # 1-indexed

                # Find the end of the class by counting braces
                brace_count = 0
                started = False
                end_line = start_line

                for j in range(i, len(lines)):
                    for char in lines[j]:
                        if char == "{":
                            brace_count += 1
                            started = True
                        elif char == "}":
                            brace_count -= 1

                    if started and brace_count == 0:
                        end_line = j + 1  # 1-indexed
                        break

                classes.append(
                    {
                        "name": class_name,
                        "type": "class",
                        "start_line": start_line,
                        "end_line": end_line,
                    }
                )

                # Skip to the end of this class to avoid nested class confusion
                i = end_line
            else:
                i += 1

        return classes

    def _is_comment_line(self, line: str, language: str) -> bool:
        """Check if a line is a comment line."""
        line = line.strip()
        if not line:
            return True  # Empty lines are considered neutral

        if language == "java":
            # Java comments: //, /*, */, *, /**
            return (
                line.startswith("//")
                or line.startswith("/*")
                or line.startswith("*/")
                or line.startswith("*")
                or line.endswith("*/")
            )
        elif language == "python":
            # Python comments: #, """, '''
            return (
                line.startswith("#")
                or line.startswith('"""')
                or line.startswith("'''")
                or line.endswith('"""')
                or line.endswith("'''")
            )
        elif language == "go":
            # Go comments: //, /* */
            return line.startswith("//") or line.startswith("/*") or line.startswith("*/") or line.startswith("*")
        elif language in ("javascript", "typescript"):
            # JS/TS comments: //, /* */
            return line.startswith("//") or line.startswith("/*") or line.startswith("*/") or line.startswith("*")
        elif language == "rust":
            # Rust comments: //, /* */, ///
            return line.startswith("//") or line.startswith("/*") or line.startswith("*/") or line.startswith("*")

        return False

    def _get_status_char(self, status: int) -> str:
        """Convert pygit2 status to single character."""
        status_map = {
            pygit2.GIT_DELTA_ADDED: "A",
            pygit2.GIT_DELTA_DELETED: "D",
            pygit2.GIT_DELTA_MODIFIED: "M",
            pygit2.GIT_DELTA_RENAMED: "R",
            pygit2.GIT_DELTA_COPIED: "C",
        }
        return status_map.get(status, "M")

    def _get_file_content(self, commit: pygit2.Commit, file_path: str) -> Optional[str]:
        """Get file content from a commit."""
        try:
            tree_entry = commit.tree[file_path]
            blob = self.repo[tree_entry.id]
            return blob.data.decode("utf-8", errors="replace")
        except (KeyError, AttributeError):
            return None

    def _extract_symbols(
        self, content: str, language: str, def_types: Set[str] = None, file_path: str = None
    ) -> List[SymbolInfo]:
        """Extract symbol definitions from file content using ast-grep."""
        symbols = []

        # Get patterns for this language from config
        lang_patterns = self.config.get_definition_patterns(language)
        if not lang_patterns:
            return []

        file_ext = self.config.get_file_extension(language)

        with tempfile.NamedTemporaryFile(mode="w", suffix=file_ext, delete=False) as f:
            f.write(content)
            temp_file = f.name

        try:
            for pattern_type, (ast_pattern, regex_pattern) in lang_patterns.items():
                result = subprocess.run(
                    [
                        "ast-grep",
                        "run",
                        "--pattern",
                        ast_pattern,
                        "--lang",
                        language,
                        "--json=compact",
                        temp_file,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

                if result.returncode == 0 and result.stdout.strip():
                    try:
                        matches = json.loads(result.stdout)
                        for match in matches:
                            start_line = match.get("range", {}).get("start", {}).get("line")
                            end_line = match.get("range", {}).get("end", {}).get("line")

                            if start_line is not None:
                                if end_line is None or end_line < start_line:
                                    end_line = start_line

                                # Extract symbol name using regex
                                text = match.get("lines", "")
                                symbol_name = self._extract_name_with_regex(text, regex_pattern)

                                if symbol_name:
                                    symbols.append(
                                        SymbolInfo(
                                            name=symbol_name,
                                            symbol_type=pattern_type,
                                            start_line=start_line + 1,  # 0-indexed to 1-indexed
                                            end_line=end_line + 1,
                                        )
                                    )
                    except json.JSONDecodeError:
                        pass

        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"Warning: ast-grep failed - {e}", file=sys.stderr)
        finally:
            Path(temp_file).unlink(missing_ok=True)

        # For Rust, also extract tests using YAML rules (handles #[test] attribute)
        if language == "rust":
            existing_keys = {(s.name, s.start_line) for s in symbols}

            # Extract standard #[test] functions
            rust_test_symbols = self._extract_rust_tests_with_rules(content, file_path)
            for test_sym in rust_test_symbols:
                key = (test_sym.name, test_sym.start_line)
                if key not in existing_keys:
                    symbols.append(test_sym)
                    existing_keys.add(key)
                else:
                    # Update existing symbol to use test type instead of function type
                    for i, s in enumerate(symbols):
                        if s.name == test_sym.name and s.start_line == test_sym.start_line:
                            # Replace with test type if current type is generic function
                            if s.symbol_type in ("function", "function_pub", "function_async", "function_pub_async"):
                                symbols[i] = test_sym
                            break

            # Extract macro-based tests (rgtest!, test_case!, etc.)
            macro_test_symbols = self._extract_rust_macro_tests(content)
            for test_sym in macro_test_symbols:
                key = (test_sym.name, test_sym.start_line)
                if key not in existing_keys:
                    symbols.append(test_sym)
                    existing_keys.add(key)

        # For Go, extract test functions using YAML rules
        elif language == "go":
            existing_keys = {(s.name, s.start_line) for s in symbols}

            # Extract Go test functions (Test*, Benchmark*, Example*, Fuzz*)
            go_test_symbols = self._extract_go_tests_with_rules(content)
            for test_sym in go_test_symbols:
                key = (test_sym.name, test_sym.start_line)
                if key not in existing_keys:
                    symbols.append(test_sym)
                    existing_keys.add(key)
                else:
                    # Update existing symbol to use test type instead of generic function type
                    for i, s in enumerate(symbols):
                        if s.name == test_sym.name and s.start_line == test_sym.start_line:
                            # Replace with test type if current type is generic function/method
                            if s.symbol_type in ("function", "method"):
                                symbols[i] = test_sym
                            break

            # Note: We do NOT extract Ginkgo BDD test cases (It, Describe, etc.) here
            # because go test reports only the suite entry functions (TestCore, TestTagLib, etc.)
            # The standard Go test detection above already captures these entry functions.

        return symbols

    def _extract_name_with_regex(self, text: str, regex_pattern: str) -> Optional[str]:
        """Extract symbol name from text using regex."""
        m = re.search(regex_pattern, text)
        if m:
            return m.group(1) if m.groups() else m.group(0)
        return None

    def _count_braces_excluding_strings(self, line: str) -> tuple:
        """Count { and } braces, excluding those inside string literals and comments.

        This is important for Rust code where string literals often contain
        format specifiers like "{path}" or escaped braces like "{{host}}".

        Args:
            line: A single line of Rust source code

        Returns:
            Tuple of (open_brace_count, close_brace_count)
        """
        # Remove line comments (// ...)
        if "//" in line:
            # Be careful not to remove // inside strings
            # Simple heuristic: find // that's not inside a string
            in_string = False
            comment_start = -1
            i = 0
            while i < len(line):
                if line[i] == '"' and (i == 0 or line[i - 1] != "\\"):
                    in_string = not in_string
                elif not in_string and i < len(line) - 1 and line[i : i + 2] == "//":
                    comment_start = i
                    break
                i += 1
            if comment_start >= 0:
                line = line[:comment_start]

        # Remove string literals
        # Handle raw strings r"...", r#"..."#, r##"..."##, etc.
        line = re.sub(r'r#+"[^"]*"#+', "", line)
        line = re.sub(r'r"[^"]*"', "", line)
        # Handle regular strings "..." (with escaped quotes)
        line = re.sub(r'"(?:[^"\\]|\\.)*"', "", line)
        # Handle char literals '...'
        line = re.sub(r"'(?:[^'\\]|\\.)'", "", line)

        return line.count("{"), line.count("}")

    def _get_mod_test_name(self, content: str, target_line: int) -> Optional[str]:
        """Get the actual test module name if line is inside a #[cfg(test)] mod block.

        Scans from beginning to find enclosing #[cfg(test)] mod blocks
        by tracking brace depth and preserving the actual module name.

        Args:
            content: The full source code content
            target_line: 1-indexed line number to check

        Returns:
            The actual module name (e.g., 'tests', 'test_expand_dots', 'test_is_windows_device_path')
            if inside a #[cfg(test)] mod block, None otherwise
        """
        lines = content.split("\n")
        if target_line < 1 or target_line > len(lines):
            return None

        # Track test module blocks with their actual names
        # Stack of tuples: (depth, mod_name)
        mod_test_stack = []
        brace_depth = 0

        # Track if we just saw #[cfg(test)]
        has_cfg_test = False

        # Pattern to match #[cfg(test)] or #[cfg_attr(test, ...)]
        # This matches various forms:
        # - #[cfg(test)]
        # - #[cfg(all(test, ...))]
        # - #[cfg_attr(test, ...)]
        cfg_test_pattern = re.compile(r"#\[\s*cfg(?:_attr)?\s*\([^)]*\btest\b[^)]*\)\s*\]")

        # Pattern to match mod XXX {  (captures any module name)
        mod_pattern = re.compile(r"\bmod\s+(\w+)\s*\{")

        for i, line in enumerate(lines):
            line_num = i + 1  # 1-indexed
            stripped = line.strip()

            # Check for #[cfg(test)] or #[cfg_attr(test, ...)]
            if cfg_test_pattern.search(stripped):
                has_cfg_test = True
                continue

            # Check for mod XXX { on this line
            match = mod_pattern.search(line)
            if match and has_cfg_test:
                # Extract the actual module name (can be 'tests', 'test_expand_dots', etc.)
                actual_mod_name = match.group(1)

                # Count braces excluding those in strings/comments
                open_braces, close_braces = self._count_braces_excluding_strings(line)

                # Push to stack with actual module name
                mod_test_stack.append((brace_depth + 1, actual_mod_name))

                brace_depth += open_braces - close_braces
                has_cfg_test = False  # Reset flag after processing
            else:
                # Not a test module declaration on this line
                # Reset flag if we see a non-attribute line that's not empty
                if stripped and not stripped.startswith("#"):
                    has_cfg_test = False

                # Count braces excluding those in strings/comments
                open_braces, close_braces = self._count_braces_excluding_strings(line)

                # Handle closing braces - check if we're exiting blocks
                new_depth = brace_depth + open_braces - close_braces

                # Pop stack when exiting blocks
                while mod_test_stack and new_depth < mod_test_stack[-1][0]:
                    mod_test_stack.pop()

                brace_depth = new_depth

            # Check if target line is reached
            if line_num == target_line:
                # Return the innermost test module name if inside one
                if mod_test_stack:
                    return mod_test_stack[-1][1]
                return None

        return None

    def _get_enclosing_mod_name(self, content: str, target_line: int) -> Optional[str]:
        """Get the enclosing module name for any mod block (not just #[cfg(test)]).

        This is used for integration tests where modules don't need #[cfg(test)]
        since the entire tests/ directory is already test code.

        For integration tests (tests/*.rs):
        - Top-level tests: returns None (cargo test outputs just the function name)
        - Tests inside mod xxx { }: returns "xxx" (cargo test outputs "xxx::fn_name")

        Args:
            content: The full source code content
            target_line: 1-indexed line number to check

        Returns:
            The enclosing module name if inside a mod block, None if at top level
        """
        lines = content.split("\n")
        if target_line < 1 or target_line > len(lines):
            return None

        # Track module blocks with their names and brace depths
        # Stack of tuples: (depth, mod_name)
        mod_stack = []
        brace_depth = 0

        # Pattern to match mod XXX { (captures any module name)
        mod_pattern = re.compile(r"\bmod\s+(\w+)\s*\{")

        for i, line in enumerate(lines):
            line_num = i + 1  # 1-indexed

            # Check for mod XXX { on this line
            match = mod_pattern.search(line)
            if match:
                mod_name = match.group(1)

                # Count braces excluding those in strings/comments
                open_braces, close_braces = self._count_braces_excluding_strings(line)

                # Push to stack with module name
                mod_stack.append((brace_depth + 1, mod_name))

                brace_depth += open_braces - close_braces
            else:
                # Not a module declaration on this line
                # Count braces excluding those in strings/comments
                open_braces, close_braces = self._count_braces_excluding_strings(line)

                # Handle closing braces - check if we're exiting blocks
                new_depth = brace_depth + open_braces - close_braces

                # Pop stack when exiting blocks
                while mod_stack and new_depth < mod_stack[-1][0]:
                    mod_stack.pop()

                brace_depth = new_depth

            # Check if target line is reached
            if line_num == target_line:
                # Return the innermost module name if inside one
                if mod_stack:
                    return mod_stack[-1][1]
                return None

        return None

    def _extract_rust_tests_with_rules(self, content: str, file_path: str = None) -> List[SymbolInfo]:
        """Extract Rust test functions using YAML rules with 'follows' relation.

        This handles the case where #[test] attribute is a sibling node to the function,
        which can't be matched with simple patterns in ast-grep.

        Args:
            content: The source code content
            file_path: Optional file path to determine if this is an integration test file

        Returns:
            List of SymbolInfo for detected test functions
        """
        if not self.rust_rules_path.exists():
            return []

        symbols = []

        # Check if this is an integration test file (tests/*.rs)
        # For integration tests, we use enclosing mod name (any mod block)
        # For unit tests (src/*.rs), we use mod test name (only #[cfg(test)] blocks)
        is_integration_test = file_path and _is_rust_integration_test(file_path)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
            f.write(content)
            temp_file = f.name

        try:
            result = subprocess.run(
                [
                    "ast-grep",
                    "scan",
                    "--rule",
                    str(self.rust_rules_path),
                    "--json=compact",
                    temp_file,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0 and result.stdout.strip():
                try:
                    matches = json.loads(result.stdout)
                    for match in matches:
                        rule_id = match.get("ruleId", "test_fn")
                        start_line = match.get("range", {}).get("start", {}).get("line")
                        end_line = match.get("range", {}).get("end", {}).get("line")

                        if start_line is not None:
                            if end_line is None or end_line < start_line:
                                end_line = start_line

                            # Extract function name from matched text
                            text = match.get("text", "") or match.get("lines", "")
                            # Pattern: fn name(...) or pub fn name(...)
                            fn_match = re.search(r"\bfn\s+(\w+)\s*[<\(]", text)
                            if fn_match:
                                symbol_name = fn_match.group(1)
                                # ast-grep line numbers are 0-indexed, convert to 1-indexed
                                target_line = start_line + 1

                                # For integration tests, use enclosing mod name (any mod block)
                                # For unit tests, use mod test name (only #[cfg(test)] blocks)
                                if is_integration_test:
                                    mod_test_name = self._get_enclosing_mod_name(content, target_line)
                                else:
                                    mod_test_name = self._get_mod_test_name(content, target_line)

                                symbols.append(
                                    SymbolInfo(
                                        name=symbol_name,
                                        symbol_type=rule_id,  # e.g., "test_fn", "tokio_test"
                                        start_line=target_line,
                                        end_line=end_line + 1,
                                        mod_test_name=mod_test_name,
                                    )
                                )
                except json.JSONDecodeError:
                    pass
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"Warning: ast-grep rule scan failed - {e}", file=sys.stderr)
        finally:
            Path(temp_file).unlink(missing_ok=True)

        return symbols

    def _extract_rust_macro_tests(self, content: str) -> List[SymbolInfo]:
        """Extract Rust test functions defined via macros like rgtest!, test_case!, etc.

        These macros generate #[test] functions but aren't detected by standard patterns
        since ast-grep sees the macro invocation, not the expanded code.

        Returns:
            List of SymbolInfo for detected macro-based test functions
        """
        if not self.rust_macro_rules_path.exists():
            return []

        symbols = []

        with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
            f.write(content)
            temp_file = f.name

        try:
            result = subprocess.run(
                [
                    "ast-grep",
                    "scan",
                    "--rule",
                    str(self.rust_macro_rules_path),
                    "--json=compact",
                    temp_file,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0 and result.stdout.strip():
                try:
                    matches = json.loads(result.stdout)
                    for match in matches:
                        rule_id = match.get("ruleId", "macro_test")
                        start_line = match.get("range", {}).get("start", {}).get("line")
                        end_line = match.get("range", {}).get("end", {}).get("line")

                        if start_line is not None:
                            if end_line is None or end_line < start_line:
                                end_line = start_line

                            # Extract test name from macro invocation
                            # Pattern: rgtest!(test_name, |...|) or test_case!(test_name, ...)
                            text = match.get("text", "") or match.get("lines", "")

                            # Try different macro patterns
                            name_match = None
                            # rgtest!(name, |...|)
                            if not name_match:
                                name_match = re.search(r"rgtest!\s*\(\s*(\w+)\s*,", text)
                            # test_case!(name, ...)
                            if not name_match:
                                name_match = re.search(r"test_case!\s*\(\s*(\w+)\s*,", text)
                            # quickcheck!(fn name(...))
                            if not name_match:
                                name_match = re.search(r"quickcheck!\s*\(\s*fn\s+(\w+)", text)
                            # proptest!(fn name(...))
                            if not name_match:
                                name_match = re.search(r"proptest!\s*\{[^}]*fn\s+(\w+)", text)
                            # criterion_group!(group_name, ...)
                            if not name_match:
                                name_match = re.search(r"criterion_group!\s*\(\s*(\w+)\s*,", text)

                            if name_match:
                                symbol_name = name_match.group(1)
                                symbols.append(
                                    SymbolInfo(
                                        name=symbol_name,
                                        symbol_type=rule_id,
                                        start_line=start_line + 1,
                                        end_line=end_line + 1,
                                    )
                                )
                except json.JSONDecodeError:
                    pass
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"Warning: ast-grep macro rule scan failed - {e}", file=sys.stderr)
        finally:
            Path(temp_file).unlink(missing_ok=True)

        return symbols

    def _extract_go_tests_with_rules(self, content: str) -> List[SymbolInfo]:
        """Extract Go test functions using YAML rules.

        Uses ast-grep rules to detect:
        - Test functions: func TestXxx(t *testing.T)
        - Benchmark functions: func BenchmarkXxx(b *testing.B)
        - Example functions: func ExampleXxx()
        - Fuzz functions: func FuzzXxx(f *testing.F)

        Returns:
            List of SymbolInfo for detected test functions
        """
        if not self.go_rules_path.exists():
            return []

        symbols = []

        with tempfile.NamedTemporaryFile(mode="w", suffix=".go", delete=False) as f:
            f.write(content)
            temp_file = f.name

        try:
            result = subprocess.run(
                [
                    "ast-grep",
                    "scan",
                    "--rule",
                    str(self.go_rules_path),
                    "--json=compact",
                    temp_file,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0 and result.stdout.strip():
                try:
                    matches = json.loads(result.stdout)
                    for match in matches:
                        rule_id = match.get("ruleId", "go_test_fn")
                        start_line = match.get("range", {}).get("start", {}).get("line")
                        end_line = match.get("range", {}).get("end", {}).get("line")

                        if start_line is not None:
                            if end_line is None or end_line < start_line:
                                end_line = start_line

                            # Extract function name from matched text
                            text = match.get("text", "") or match.get("lines", "")
                            # Pattern: func TestName(... or func BenchmarkName(... etc
                            fn_match = re.search(r"\bfunc\s+(\w+)\s*\(", text)
                            if fn_match:
                                symbol_name = fn_match.group(1)
                                # Skip TestMain as it's setup/teardown, not a test
                                if symbol_name == "TestMain":
                                    continue
                                symbols.append(
                                    SymbolInfo(
                                        name=symbol_name,
                                        symbol_type=rule_id,
                                        start_line=start_line + 1,
                                        end_line=end_line + 1,
                                    )
                                )
                except json.JSONDecodeError:
                    pass
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"Warning: ast-grep Go rule scan failed - {e}", file=sys.stderr)
        finally:
            Path(temp_file).unlink(missing_ok=True)

        return symbols

    def _extract_ginkgo_tests(self, content: str) -> List[SymbolInfo]:
        """Extract Ginkgo BDD test cases from Go test files.

        Detects Ginkgo patterns:
        - It(), FIt(), PIt() - test cases
        - Specify(), FSpecify(), PSpecify() - test case aliases
        - Entry(), FEntry(), PEntry() - table-driven test entries
        - Describe(), Context(), When() - test groupings (also tracked)
        - DescribeTable() - table-driven test suites

        Returns:
            List of SymbolInfo for detected Ginkgo test cases
        """
        if not self.go_ginkgo_rules_path.exists():
            return []

        symbols = []

        with tempfile.NamedTemporaryFile(mode="w", suffix=".go", delete=False) as f:
            f.write(content)
            temp_file = f.name

        try:
            result = subprocess.run(
                [
                    "ast-grep",
                    "scan",
                    "--rule",
                    str(self.go_ginkgo_rules_path),
                    "--json=compact",
                    temp_file,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0 and result.stdout.strip():
                try:
                    matches = json.loads(result.stdout)
                    for match in matches:
                        rule_id = match.get("ruleId", "ginkgo_it")
                        start_line = match.get("range", {}).get("start", {}).get("line")
                        end_line = match.get("range", {}).get("end", {}).get("line")

                        if start_line is not None:
                            if end_line is None or end_line < start_line:
                                end_line = start_line

                            # Extract test description from matched text
                            text = match.get("text", "") or match.get("lines", "")

                            # Extract the description string from patterns like:
                            # It("description", func() {...})
                            # Describe("description", func() {...})
                            # Entry("description", args...)
                            desc_match = re.search(
                                r'(?:It|Specify|Entry|Describe|Context|When|DescribeTable|FIt|PIt|FSpecify|PSpecify|FEntry|PEntry|FDescribe|PDescribe|FContext|PContext|FDescribeTable|PDescribeTable)\s*\(\s*["`]([^"`]+)["`]',
                                text,
                            )

                            if desc_match:
                                # Use description as the test name
                                # Sanitize: replace spaces and special chars with underscores
                                description = desc_match.group(1)
                                # Keep it readable but safe for IDs
                                symbol_name = description[:80]  # Truncate long descriptions

                                symbols.append(
                                    SymbolInfo(
                                        name=symbol_name,
                                        symbol_type=rule_id,
                                        start_line=start_line + 1,
                                        end_line=end_line + 1,
                                    )
                                )
                except json.JSONDecodeError:
                    pass
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"Warning: ast-grep Ginkgo rule scan failed - {e}", file=sys.stderr)
        finally:
            Path(temp_file).unlink(missing_ok=True)

        return symbols

    def extract_ginkgo_test_cases(self, content: str) -> List[GinkgoTestCase]:
        """Extract Ginkgo BDD test cases with full hierarchy paths.

        Parses Ginkgo test files to extract complete test paths like:
        "Extractor > Error Checking > does not throw a SIGSEGV error..."

        This is needed because go test reports Ginkgo tests with full hierarchy
        in format: {package}::{Describe > Context > ... > It}

        Args:
            content: Go test file content

        Returns:
            List of GinkgoTestCase with full hierarchy paths
        """
        if not self.go_ginkgo_rules_path.exists():
            return []

        # Extract all Ginkgo nodes (Describe, Context, When, It, etc.)
        nodes = self._extract_all_ginkgo_nodes(content)
        if not nodes:
            return []

        # Build hierarchy tree and extract test cases
        return self._build_ginkgo_test_cases_from_nodes(nodes)

    def _extract_all_ginkgo_nodes(self, content: str) -> List[Dict]:
        """Extract all Ginkgo BDD nodes with their line ranges.

        Returns a list of dicts with:
        - node_type: "Describe", "Context", "When", "It", etc.
        - description: The test description string
        - start_line: Start line (1-indexed)
        - end_line: End line (1-indexed)
        """
        nodes = []

        with tempfile.NamedTemporaryFile(mode="w", suffix=".go", delete=False) as f:
            f.write(content)
            temp_file = f.name

        try:
            result = subprocess.run(
                [
                    "ast-grep",
                    "scan",
                    "--rule",
                    str(self.go_ginkgo_rules_path),
                    "--json=compact",
                    temp_file,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0 and result.stdout.strip():
                try:
                    matches = json.loads(result.stdout)
                    for match in matches:
                        rule_id = match.get("ruleId", "")
                        start_line = match.get("range", {}).get("start", {}).get("line")
                        end_line = match.get("range", {}).get("end", {}).get("line")

                        if start_line is not None:
                            if end_line is None or end_line < start_line:
                                end_line = start_line

                            # Extract description from matched text
                            text = match.get("text", "") or match.get("lines", "")

                            # Determine node type from rule_id
                            node_type = self._rule_id_to_node_type(rule_id)

                            # Extract description string
                            desc_match = re.search(
                                r"(?:It|Specify|Entry|Describe|Context|When|DescribeTable|"
                                r"FIt|PIt|FSpecify|PSpecify|FEntry|PEntry|FDescribe|PDescribe|"
                                r'FContext|PContext|FDescribeTable|PDescribeTable)\s*\(\s*["`]([^"`]+)["`]',
                                text,
                            )

                            if desc_match:
                                description = desc_match.group(1)
                                nodes.append(
                                    {
                                        "node_type": node_type,
                                        "description": description,
                                        "start_line": start_line + 1,  # 0-indexed to 1-indexed
                                        "end_line": end_line + 1,
                                    }
                                )
                except json.JSONDecodeError:
                    pass
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"Warning: ast-grep Ginkgo extraction failed - {e}", file=sys.stderr)
        finally:
            Path(temp_file).unlink(missing_ok=True)

        return nodes

    def _rule_id_to_node_type(self, rule_id: str) -> str:
        """Convert ast-grep rule ID to Ginkgo node type."""
        # Map rule IDs to node types
        # e.g., "ginkgo_it" -> "It", "ginkgo_describe" -> "Describe"
        mapping = {
            "ginkgo_it": "It",
            "ginkgo_fit": "It",
            "ginkgo_pit": "It",
            "ginkgo_specify": "It",  # Specify is alias for It
            "ginkgo_fspecify": "It",
            "ginkgo_pspecify": "It",
            "ginkgo_describe": "Describe",
            "ginkgo_fdescribe": "Describe",
            "ginkgo_pdescribe": "Describe",
            "ginkgo_context": "Context",
            "ginkgo_fcontext": "Context",
            "ginkgo_pcontext": "Context",
            "ginkgo_when": "When",
            "ginkgo_entry": "Entry",
            "ginkgo_fentry": "Entry",
            "ginkgo_pentry": "Entry",
            "ginkgo_describe_table": "DescribeTable",
            "ginkgo_fdescribe_table": "DescribeTable",
            "ginkgo_pdescribe_table": "DescribeTable",
        }
        return mapping.get(rule_id, "Unknown")

    def _build_ginkgo_test_cases_from_nodes(self, nodes: List[Dict]) -> List[GinkgoTestCase]:
        """Build GinkgoTestCase objects with full hierarchy from flat node list.

        Uses line ranges to determine parent-child relationships.
        A node A is parent of node B if A's range contains B's range.
        """
        if not nodes:
            return []

        # Sort nodes by start_line, then by range size (larger first for containers)
        sorted_nodes = sorted(nodes, key=lambda n: (n["start_line"], -(n["end_line"] - n["start_line"])))

        test_cases = []

        # Container types that form the hierarchy
        container_types = {"Describe", "Context", "When", "DescribeTable"}
        # Leaf types that are actual test cases
        leaf_types = {"It", "Entry"}

        for node in sorted_nodes:
            if node["node_type"] not in leaf_types:
                continue

            # This is a test case (It or Entry)
            # Find all container ancestors by checking which containers contain this node
            hierarchy = []

            for potential_parent in sorted_nodes:
                if potential_parent["node_type"] not in container_types:
                    continue

                # Check if potential_parent contains this node
                if (
                    potential_parent["start_line"] <= node["start_line"]
                    and potential_parent["end_line"] >= node["end_line"]
                ):
                    hierarchy.append(
                        {
                            "description": potential_parent["description"],
                            "node_type": potential_parent["node_type"],
                            "start_line": potential_parent["start_line"],
                        }
                    )

            # Sort hierarchy by start_line (outermost first)
            hierarchy.sort(key=lambda h: h["start_line"])

            # Build the full path: [Describe, Context, ..., It description]
            # Note: Ginkgo adds "when " prefix to When() block descriptions in reports
            full_hierarchy = []
            for h in hierarchy:
                desc = h["description"]
                if h["node_type"] == "When":
                    desc = "when " + desc
                full_hierarchy.append(desc)
            full_hierarchy.append(node["description"])

            test_cases.append(
                GinkgoTestCase(
                    hierarchy=full_hierarchy,
                    start_line=node["start_line"],
                    end_line=node["end_line"],
                )
            )

        return test_cases

    # ========== Jest/Vitest BDD Test Extraction Methods ==========

    def extract_jest_test_cases(self, content: str, file_ext: str = ".js") -> List[JestTestCase]:
        """Extract Jest/Vitest BDD test cases with full hierarchy paths.

        Parses Jest/Vitest test files to extract complete test paths like:
        "<ActivityPanel /> > clears the error icon after opening the panel"

        Args:
            content: JavaScript/TypeScript test file content
            file_ext: File extension for ast-grep language detection

        Returns:
            List of JestTestCase with full hierarchy paths
        """
        if not self.js_jest_rules_path.exists():
            return []

        # Extract all Jest nodes (describe, it, test, etc.)
        nodes = self._extract_all_jest_nodes(content, file_ext)
        if not nodes:
            return []

        # Build hierarchy tree and extract test cases
        return self._build_jest_test_cases_from_nodes(nodes)

    def _extract_all_jest_nodes(self, content: str, file_ext: str = ".js") -> List[Dict]:
        """Extract all Jest/Vitest BDD nodes with their line ranges.

        Returns a list of dicts with:
        - node_type: "describe", "it", "test", etc.
        - description: The test description string
        - start_line: Start line (1-indexed)
        - end_line: End line (1-indexed)
        """
        nodes = []

        # Determine suffix and ast-grep language based on file extension
        ext_config = {
            ".js": (".js", "javascript"),
            ".jsx": (".jsx", "javascript"),  # ast-grep uses javascript for jsx
            ".ts": (".ts", "typescript"),
            ".tsx": (".tsx", "tsx"),
        }
        suffix, ast_lang = ext_config.get(file_ext, (".js", "javascript"))

        with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as f:
            f.write(content)
            temp_file = f.name

        # Generate inline rules with correct language for this file type
        jest_patterns = [
            ("jest_describe", "describe($DESC, $$$)"),
            ("jest_describe_only", "describe.only($DESC, $$$)"),
            ("jest_describe_skip", "describe.skip($DESC, $$$)"),
            ("jest_it", "it($DESC, $$$)"),
            ("jest_it_only", "it.only($DESC, $$$)"),
            ("jest_it_skip", "it.skip($DESC, $$$)"),
            ("jest_it_todo", "it.todo($DESC)"),
            ("jest_test", "test($DESC, $$$)"),
            ("jest_test_only", "test.only($DESC, $$$)"),
            ("jest_test_skip", "test.skip($DESC, $$$)"),
            ("jest_test_todo", "test.todo($DESC)"),
            ("jest_fit", "fit($DESC, $$$)"),
            ("jest_xit", "xit($DESC, $$$)"),
            ("jest_fdescribe", "fdescribe($DESC, $$$)"),
            ("jest_xdescribe", "xdescribe($DESC, $$$)"),
            # .each patterns for parameterized tests (curried call syntax)
            # describe.each(cases)("description", fn) / it.each(cases)("description", fn)
            ("jest_describe_each", "describe.each($CASES)($DESC, $$$)"),
            ("jest_describe_each_only", "describe.each($CASES).only($DESC, $$$)"),
            ("jest_describe_each_skip", "describe.each($CASES).skip($DESC, $$$)"),
            ("jest_it_each", "it.each($CASES)($DESC, $$$)"),
            ("jest_it_each_only", "it.each($CASES).only($DESC, $$$)"),
            ("jest_it_each_skip", "it.each($CASES).skip($DESC, $$$)"),
            ("jest_test_each", "test.each($CASES)($DESC, $$$)"),
            ("jest_test_each_only", "test.each($CASES).only($DESC, $$$)"),
            ("jest_test_each_skip", "test.each($CASES).skip($DESC, $$$)"),
        ]

        inline_rules = "\n---\n".join(
            f"id: {rule_id}\nlanguage: {ast_lang}\nrule:\n  pattern: {pattern}" for rule_id, pattern in jest_patterns
        )

        try:
            result = subprocess.run(
                [
                    "ast-grep",
                    "scan",
                    "--inline-rules",
                    inline_rules,
                    "--json",
                    temp_file,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0 and result.stdout.strip():
                try:
                    matches = json.loads(result.stdout)
                    for match in matches:
                        rule_id = match.get("ruleId", "")
                        start_line = match.get("range", {}).get("start", {}).get("line")
                        end_line = match.get("range", {}).get("end", {}).get("line")

                        if start_line is not None:
                            if end_line is None or end_line < start_line:
                                end_line = start_line

                            # Extract description from matched text
                            text = match.get("text", "") or match.get("lines", "")

                            # Determine node type from rule_id
                            node_type = self._jest_rule_id_to_node_type(rule_id)

                            # Extract description string (first argument, usually a string literal)
                            # Patterns: describe('...', it('...', test('...',
                            # Handle single quotes, double quotes, and backticks
                            # Also handle escaped quotes within the string
                            desc_match = None
                            # Try single quotes first (handles escaped single quotes)
                            desc_match = re.search(
                                r"(?:describe|it|test|fdescribe|xdescribe|fit|xit)"
                                r"(?:\.only|\.skip|\.todo|\.concurrent|\.each\([^)]*\))?"
                                r"\s*\(\s*'((?:[^'\\]|\\.)*)'",
                                text,
                            )
                            if not desc_match:
                                # Try double quotes (handles escaped double quotes)
                                desc_match = re.search(
                                    r"(?:describe|it|test|fdescribe|xdescribe|fit|xit)"
                                    r"(?:\.only|\.skip|\.todo|\.concurrent|\.each\([^)]*\))?"
                                    r'\s*\(\s*"((?:[^"\\]|\\.)*)"',
                                    text,
                                )
                            if not desc_match:
                                # Try backticks (template literals)
                                desc_match = re.search(
                                    r"(?:describe|it|test|fdescribe|xdescribe|fit|xit)"
                                    r"(?:\.only|\.skip|\.todo|\.concurrent|\.each\([^)]*\))?"
                                    r"\s*\(\s*`((?:[^`\\]|\\.)*)`",
                                    text,
                                )

                            if desc_match:
                                description = desc_match.group(1)
                                nodes.append(
                                    {
                                        "node_type": node_type,
                                        "description": description,
                                        "start_line": start_line + 1,  # 0-indexed to 1-indexed
                                        "end_line": end_line + 1,
                                    }
                                )
                except json.JSONDecodeError:
                    pass
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"Warning: ast-grep Jest extraction failed - {e}", file=sys.stderr)
        finally:
            Path(temp_file).unlink(missing_ok=True)

        return nodes

    def _jest_rule_id_to_node_type(self, rule_id: str) -> str:
        """Convert ast-grep rule ID to Jest node type."""
        # Map rule IDs to node types
        mapping = {
            # describe variants -> "describe" (container)
            "jest_describe": "describe",
            "jest_describe_only": "describe",
            "jest_describe_skip": "describe",
            "jest_fdescribe": "describe",
            "jest_xdescribe": "describe",
            "jest_describe_each": "describe",
            "jest_describe_each_only": "describe",
            "jest_describe_each_skip": "describe",
            # it variants -> "it" (leaf test)
            "jest_it": "it",
            "jest_it_only": "it",
            "jest_it_skip": "it",
            "jest_it_todo": "it",
            "jest_fit": "it",
            "jest_xit": "it",
            "jest_it_each": "it",
            "jest_it_each_only": "it",
            "jest_it_each_skip": "it",
            # test variants -> "test" (leaf test, alias for it)
            "jest_test": "test",
            "jest_test_only": "test",
            "jest_test_skip": "test",
            "jest_test_todo": "test",
            "jest_test_concurrent": "test",
            "jest_test_concurrent_only": "test",
            "jest_test_concurrent_skip": "test",
            "jest_test_each": "test",
            "jest_test_each_only": "test",
            "jest_test_each_skip": "test",
        }
        return mapping.get(rule_id, "unknown")

    def _build_jest_test_cases_from_nodes(self, nodes: List[Dict]) -> List[JestTestCase]:
        """Build JestTestCase objects with full hierarchy from flat node list.

        Uses line ranges to determine parent-child relationships.
        A node A is parent of node B if A's range contains B's range.
        """
        if not nodes:
            return []

        # Sort nodes by start_line, then by range size (larger first for containers)
        sorted_nodes = sorted(nodes, key=lambda n: (n["start_line"], -(n["end_line"] - n["start_line"])))

        test_cases = []

        # Container types that form the hierarchy
        container_types = {"describe"}
        # Leaf types that are actual test cases
        leaf_types = {"it", "test"}

        for node in sorted_nodes:
            if node["node_type"] not in leaf_types:
                continue

            # This is a test case (it or test)
            # Find all container ancestors by checking which containers contain this node
            hierarchy = []

            for potential_parent in sorted_nodes:
                if potential_parent["node_type"] not in container_types:
                    continue

                # Check if potential_parent contains this node
                if (
                    potential_parent["start_line"] <= node["start_line"]
                    and potential_parent["end_line"] >= node["end_line"]
                ):
                    hierarchy.append(
                        {
                            "description": potential_parent["description"],
                            "node_type": potential_parent["node_type"],
                            "start_line": potential_parent["start_line"],
                        }
                    )

            # Sort hierarchy by start_line (outermost first)
            hierarchy.sort(key=lambda h: h["start_line"])

            # Build the full path: [describe, ..., it/test description]
            full_hierarchy = [h["description"] for h in hierarchy]
            full_hierarchy.append(node["description"])

            test_cases.append(
                JestTestCase(
                    hierarchy=full_hierarchy,
                    start_line=node["start_line"],
                    end_line=node["end_line"],
                )
            )

        return test_cases


def extract_changes_batch(
    repo_path: str,
    commit_shas: List[str],
    config_path: str,
    output_dir: Optional[str] = None,
    include_patterns: Optional[List[str]] = None,
    verbose: bool = False,
    with_test_ids: bool = False,
) -> List[CommitSymbolChanges]:
    """Extract symbol changes for multiple commits.

    Args:
        repo_path: Path to git repository
        commit_shas: List of commit SHAs to analyze
        config_path: Path to symbol_extraction_config.yml
        output_dir: Directory to save results (one file per commit)
        include_patterns: If provided, only process files matching these glob patterns
                         e.g., ["**/test/**", "*Test.java", "test_*.py"]
        verbose: Enable verbose output
        with_test_ids: Include Java test IDs in output
    """
    extractor = CommitSymbolChangeExtractor(repo_path, config_path)
    results = []

    # Choose serialization function based on with_test_ids flag
    to_dict_fn = _to_dict_with_test_ids if with_test_ids else _to_dict

    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    for i, commit_sha in enumerate(commit_shas, 1):
        if verbose:
            print(f"Processing {i}/{len(commit_shas)}: {commit_sha[:7]}...", end=" ", file=sys.stderr)

        try:
            result = extractor.extract_commit_changes(commit_sha, include_patterns)
            results.append(result)

            if output_dir:
                output_file = Path(output_dir) / f"{result.short_sha}_symbol_changes.json"
                with open(output_file, "w") as f:
                    json.dump(to_dict_fn(result), f, indent=2)

            if verbose:
                total_added = sum(len(fc.symbols_added) for fc in result.files)
                total_modified = sum(len(fc.symbols_modified) for fc in result.files)
                total_deleted = sum(len(fc.symbols_deleted) for fc in result.files)
                print(f"✓ (+{total_added} ~{total_modified} -{total_deleted})", file=sys.stderr)

        except Exception as e:
            if verbose:
                print(f"✗ Error: {e}", file=sys.stderr)
            results.append(CommitSymbolChanges(sha=commit_sha, short_sha=commit_sha[:7]))

    return results


def _extract_java_test_id(file_path: str, class_name: str, method_name: Optional[str] = None) -> Optional[str]:
    """Extract Java test identifier in format: module::package.ClassName::methodName

    Only processes test files (src/test/java), not main source files.

    Args:
        file_path: Java file path like "dubbo-registry/dubbo-registry-api/src/test/java/org/apache/dubbo/registry/client/ServiceDiscoveryRegistryTest.java"
        class_name: Class name like "ServiceDiscoveryRegistryTest"
        method_name: Optional method name like "testConcurrencySubscribe"

    Returns:
        Test ID like "dubbo-registry/dubbo-registry-api::org.apache.dubbo.registry.client.ServiceDiscoveryRegistryTest::testConcurrencySubscribe"
        or None if not a valid Java test file
    """
    if not file_path.endswith(".java"):
        return None

    # Only process test files (src/test/java), skip main source files
    test_marker = "/src/test/java/"

    if test_marker not in file_path:
        return None

    marker_pos = file_path.find(test_marker)

    # Extract module (everything before src/test/java)
    module = file_path[:marker_pos]

    # Extract package path (everything after the marker, minus the filename)
    package_path = file_path[marker_pos + len(test_marker) :]
    package_dir = "/".join(package_path.split("/")[:-1])
    package_name = package_dir.replace("/", ".")

    # Build the test ID
    fqcn = f"{package_name}.{class_name}" if package_name else class_name

    if method_name:
        return f"{module}::{fqcn}::{method_name}"
    else:
        return f"{module}::{fqcn}"


def _extract_rust_test_id(file_path: str, fn_name: str, mod_test_name: Optional[str] = None) -> Optional[str]:
    """Extract Rust test identifier in format: module_path::fn_name

    Handles both unit tests (in src/) and integration tests (in tests/).

    Rust test naming conventions:
    - Integration tests (tests/*.rs at crate root): Only function name (e.g., "test_foo")
    - Integration tests (tests/subdir/*.rs): Only function name
    - Unit tests inside mod test/tests: module::path::{test|tests}::fn_name
    - Unit tests outside mod test/tests (standalone): module::path::fn_name
    - Unit tests (crates/X/src/**/*.rs): Similar, but consider workspace structure

    For workspace crates (crates/*/):
    - Standard: crates/foo/src/bar.rs -> bar::{test|tests}::fn_name (if in mod test/tests)
    - Non-standard (like ripgrep): crates/core/flags/defs.rs -> flags::defs::{test|tests}::fn_name
      where main.rs is at crates/core/main.rs (not crates/core/src/main.rs)

    Args:
        file_path: Rust file path like "src/lib.rs" or "tests/integration_test.rs"
        fn_name: Test function name like "test_basic"
        mod_test_name: Actual mod test/tests name ('test' or 'tests'), or None if standalone

    Returns:
        Test ID like "tests::test_basic" or "module::test::fn_name"
        or None if not a valid Rust file
    """
    if not file_path.endswith(".rs"):
        return None

    # Integration tests: tests/*.rs at any level
    # For integration tests, cargo test output format depends on:
    # 1. File location relative to tests/ directory
    # 2. Whether the test is inside a mod block
    #
    # Examples:
    # - tests/test_signature.rs::test_foo -> "test_foo" (direct file in tests/)
    # - tests/commands/date/format.rs::test_foo -> "commands::date::format::test_foo" (subdirectory)
    # - tests/test_parser.rs::mod record::test_foo -> "record::test_foo" (inside mod block)
    # Check for /tests/ directory (but not /src/tests/ which would be unit tests)
    if _is_rust_integration_test(file_path):
        # Extract the path relative to tests/ directory
        if "/tests/" in file_path:
            rel_path = file_path.split("/tests/")[-1]
        elif file_path.startswith("tests/"):
            rel_path = file_path[6:]  # Remove "tests/"
        else:
            rel_path = file_path

        # Remove .rs extension and convert to module notation
        module_path = rel_path.replace(".rs", "").replace("/", "::")

        # Handle mod.rs files: str_/mod -> str_ (mod.rs represents its parent directory)
        if module_path.endswith("::mod"):
            module_path = module_path[:-5]  # Remove "::mod"

        # Check if file is directly in tests/ (no subdirectory)
        # e.g., tests/test_signature.rs -> "test_signature" (single component)
        is_direct_test_file = "/" not in rel_path

        if is_direct_test_file:
            # For files directly in tests/, cargo test doesn't use file name as prefix
            # Only use enclosing mod name if inside a mod block
            if mod_test_name:
                return f"{mod_test_name}::{fn_name}"
            else:
                return fn_name
        else:
            # For files in subdirectories (e.g., tests/commands/date/format.rs)
            # Use the subdirectory path as module prefix
            if mod_test_name:
                return f"{module_path}::{mod_test_name}::{fn_name}"
            else:
                return f"{module_path}::{fn_name}"

    # Unit tests handling
    # Need to determine the module path from the file path

    # Case 1: Standard src/ directory structure
    if "/src/" in file_path or file_path.startswith("src/"):
        # Extract module path relative to src/
        if "/src/" in file_path:
            # Handle crates/foo/src/bar.rs -> bar (module name within crate)
            rel_path = file_path.split("/src/")[-1]
        else:
            rel_path = file_path[4:]  # Remove "src/"

        # Convert path to module notation
        mod_path = rel_path.replace(".rs", "").replace("/", "::")

        # Handle lib.rs and main.rs (crate root)
        if mod_path == "lib" or mod_path == "main":
            if mod_test_name:
                return f"{mod_test_name}::{fn_name}"
            else:
                return fn_name

        # Handle mod.rs files
        if mod_path.endswith("::mod"):
            mod_path = mod_path[:-5]  # Remove "::mod"

        if mod_test_name:
            return f"{mod_path}::{mod_test_name}::{fn_name}"
        else:
            return f"{mod_path}::{fn_name}"

    # Case 2: Non-standard workspace crate structure (like ripgrep)
    # Example: crates/core/flags/defs.rs where main.rs is at crates/core/main.rs
    # The module path is flags::defs (relative to the crate root which is crates/core/)
    if "/crates/" in file_path or file_path.startswith("crates/"):
        return _extract_rust_workspace_test_id(file_path, fn_name, mod_test_name)

    # Default: just use the filename and function name (likely a simple project structure)
    file_name = os.path.basename(file_path).replace(".rs", "")
    if file_name in ("lib", "main"):
        if mod_test_name:
            return f"{mod_test_name}::{fn_name}"
        else:
            return fn_name
    if mod_test_name:
        return f"{file_name}::{mod_test_name}::{fn_name}"
    else:
        return f"{file_name}::{fn_name}"


def _is_rust_integration_test(file_path: str) -> bool:
    """Check if a Rust file is an integration test.

    Integration tests are in the tests/ directory at the crate root,
    NOT in src/tests/ (which would be a unit test module).

    Examples:
    - tests/foo.rs -> True (integration test)
    - crates/ignore/tests/foo.rs -> True (integration test in workspace)
    - src/tests/foo.rs -> False (unit test module)
    - crates/foo/src/tests.rs -> False (unit test module)
    """
    # Normalize path separators
    path = file_path.replace("\\", "/")

    # First, check if this is under src/ - then it's NOT an integration test
    # This handles src/tests/*.rs which is a unit test module
    if path.startswith("src/") or "/src/" in path:
        return False

    # Pattern 1: tests/*.rs at repo root
    if path.startswith("tests/"):
        return True

    # Pattern 2: crates/foo/tests/*.rs (workspace crate integration tests)
    # The tests/ must be directly under the crate directory, not under src/
    if "/tests/" in path:
        return True

    return False


def _extract_rust_workspace_test_id(file_path: str, fn_name: str, mod_test_name: Optional[str] = None) -> str:
    """Extract test ID for Rust files in a workspace crate with non-standard structure.

    Handles cases like ripgrep where:
    - crates/core/main.rs is the crate entry (not crates/core/src/main.rs)
    - crates/core/flags/defs.rs has module path flags::defs

    Args:
        file_path: File path like "crates/core/flags/defs.rs"
        fn_name: Test function name
        mod_test_name: Actual mod test/tests name ('test' or 'tests'), or None if standalone

    Returns:
        Test ID like "flags::defs::test::fn_name" (if in mod test)
        or "flags::defs::fn_name" (if standalone test)
    """
    path = file_path.replace("\\", "/")

    # Find the crate root within workspace
    # Pattern: crates/{crate_name}/...
    parts = path.split("/")
    try:
        crates_idx = parts.index("crates")
        if crates_idx + 2 < len(parts):
            # crate_name is at crates_idx + 1
            # Module path starts from crates_idx + 2
            crate_root_idx = crates_idx + 2

            # Check if there's a src/ directory in the path
            if "src" in parts[crate_root_idx:]:
                src_idx = parts.index("src", crate_root_idx)
                # Module path is after src/
                module_parts = parts[src_idx + 1 :]
            else:
                # Non-standard structure: module path starts right after crate name
                # e.g., crates/core/flags/defs.rs -> module_parts = ["flags", "defs.rs"]
                module_parts = parts[crate_root_idx:]

            # Convert to module path
            if module_parts:
                # Remove .rs extension from the last part
                module_parts[-1] = module_parts[-1].replace(".rs", "")

                # Handle mod.rs
                if module_parts[-1] == "mod":
                    module_parts = module_parts[:-1]

                # Handle main.rs and lib.rs at module root
                if len(module_parts) == 1 and module_parts[0] in ("main", "lib"):
                    if mod_test_name:
                        return f"{mod_test_name}::{fn_name}"
                    else:
                        return fn_name

                mod_path = "::".join(module_parts)
                if mod_test_name:
                    return f"{mod_path}::{mod_test_name}::{fn_name}"
                else:
                    return f"{mod_path}::{fn_name}"

    except (ValueError, IndexError):
        pass

    # Fallback: use filename
    file_name = os.path.basename(file_path).replace(".rs", "")
    if mod_test_name:
        return f"{file_name}::{mod_test_name}::{fn_name}"
    else:
        return f"{file_name}::{fn_name}"


def _extract_go_test_id(file_path: str, fn_name: str, module_name: Optional[str] = None) -> Optional[str]:
    """Extract Go test identifier in format: module/package_path/TestName

    This format matches `go test -json` output which reports tests as:
    module/package_path/TestFunctionName (e.g., "github.com/navidrome/navidrome/core/TestCore")

    Args:
        file_path: Go test file path like "core/library_test.go" or "adapters/taglib/taglib_test.go"
        fn_name: Test function name like "TestCore" or "TestTagLib"
        module_name: Go module name from go.mod (e.g., "github.com/navidrome/navidrome")

    Returns:
        Test ID like "github.com/navidrome/navidrome/core/TestCore"
        Returns None if not a valid Go test file
    """
    if not file_path.endswith("_test.go"):
        return None

    # Extract package path from file path (directory part)
    dir_path = os.path.dirname(file_path)
    if dir_path:
        # Use the full directory path as package path
        pkg_path = dir_path
    else:
        # File is in root directory
        pkg_path = ""

    # Build the full test ID
    if module_name:
        if pkg_path:
            return f"{module_name}/{pkg_path}/{fn_name}"
        else:
            return f"{module_name}/{fn_name}"
    else:
        if pkg_path:
            return f"{pkg_path}/{fn_name}"
        else:
            return fn_name


def _extract_go_package_path(file_path: str, module_name: Optional[str] = None) -> str:
    """Extract Go package path from file path.

    For Ginkgo test IDs, we need the package path in format:
    module/package (e.g., "github.com/navidrome/navidrome/adapters/taglib")

    Args:
        file_path: Go file path like "adapters/taglib/taglib_test.go"
        module_name: Go module name from go.mod (e.g., "github.com/navidrome/navidrome")

    Returns:
        Package path like "github.com/navidrome/navidrome/adapters/taglib"
    """
    # Extract directory path from file path
    dir_path = os.path.dirname(file_path)

    # Build full package path
    if module_name:
        if dir_path:
            return f"{module_name}/{dir_path}"
        else:
            return module_name
    else:
        return dir_path if dir_path else "."


def _derive_go_suite_test_id(
    file_path: str,
    module_name: Optional[str] = None,
    suite_functions: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Derive the Go test suite ID for a test file.

    For Ginkgo BDD projects, test files like core/library_test.go contain
    Describe/It blocks that are run by the suite entry function (TestCore).

    This function uses the suite_functions mapping if available, otherwise
    falls back to deriving the name from the package name.

    Args:
        file_path: Go test file path like "core/library_test.go"
        module_name: Go module name from go.mod (e.g., "github.com/navidrome/navidrome")
        suite_functions: Mapping from package path to suite function name

    Returns:
        Test ID like "github.com/navidrome/navidrome/core/TestCore"
        Returns None if not a valid Go test file
    """
    if not file_path.endswith("_test.go"):
        return None

    # Extract package path from file path (directory part)
    dir_path = os.path.dirname(file_path)
    if not dir_path or dir_path == ".":
        return None  # Can't derive suite name for root package

    # Try to get the actual suite function name from the mapping
    if suite_functions and dir_path in suite_functions:
        suite_name = suite_functions[dir_path]
    else:
        # Fall back to deriving from package name
        # e.g., "core" -> "TestCore", "persistence" -> "TestPersistence"
        pkg_name = os.path.basename(dir_path)
        suite_name = "Test" + pkg_name.capitalize()

    # Build the full test ID
    if module_name:
        return f"{module_name}/{dir_path}/{suite_name}"
    else:
        return f"{dir_path}/{suite_name}"


def _is_go_test_file(file_path: str) -> bool:
    """Check if a file is a Go test file.

    Go test files must end with _test.go.
    """
    return file_path.endswith("_test.go")


def _is_js_test_file(file_path: str) -> bool:
    """Check if a file is a JavaScript/TypeScript test file.

    Common patterns:
    - *.test.js, *.test.jsx, *.test.ts, *.test.tsx
    - *.spec.js, *.spec.jsx, *.spec.ts, *.spec.tsx
    - *-test.js, *-test.jsx, *-test.ts, *-test.tsx (hyphen variant)
    - *-spec.js, *-spec.jsx, *-spec.ts, *-spec.tsx (hyphen variant)
    """
    test_patterns = (
        ".test.js",
        ".test.jsx",
        ".test.ts",
        ".test.tsx",
        ".spec.js",
        ".spec.jsx",
        ".spec.ts",
        ".spec.tsx",
        "-test.js",
        "-test.jsx",
        "-test.ts",
        "-test.tsx",
        "-spec.js",
        "-spec.jsx",
        "-spec.ts",
        "-spec.tsx",
    )
    return any(file_path.endswith(pattern) for pattern in test_patterns)


def _extract_ginkgo_test_id(file_path: str, description: str, symbol_type: str) -> Optional[str]:
    """Extract Ginkgo BDD test identifier in format: package::type::description

    Args:
        file_path: Go test file path like "pkg/foo/bar_test.go"
        description: Test description from It("description", ...) etc.
        symbol_type: The Ginkgo rule type (e.g., "ginkgo_it", "ginkgo_describe")

    Returns:
        Test ID like "bar::It::should handle errors" or None if not a valid Go test file
    """
    if not file_path.endswith("_test.go"):
        return None

    # Extract package name from file path
    dir_path = os.path.dirname(file_path)
    if dir_path:
        pkg_name = os.path.basename(dir_path)
    else:
        pkg_name = os.path.basename(file_path).replace("_test.go", "")

    if not pkg_name or pkg_name == ".":
        pkg_name = "main"

    # Determine test type from symbol_type
    type_map = {
        "ginkgo_it": "It",
        "ginkgo_fit": "FIt",
        "ginkgo_pit": "PIt",
        "ginkgo_specify": "Specify",
        "ginkgo_fspecify": "FSpecify",
        "ginkgo_pspecify": "PSpecify",
        "ginkgo_entry": "Entry",
        "ginkgo_fentry": "FEntry",
        "ginkgo_pentry": "PEntry",
        "ginkgo_describe": "Describe",
        "ginkgo_fdescribe": "FDescribe",
        "ginkgo_pdescribe": "PDescribe",
        "ginkgo_context": "Context",
        "ginkgo_fcontext": "FContext",
        "ginkgo_pcontext": "PContext",
        "ginkgo_when": "When",
        "ginkgo_describe_table": "DescribeTable",
        "ginkgo_fdescribe_table": "FDescribeTable",
        "ginkgo_pdescribe_table": "PDescribeTable",
    }
    test_type = type_map.get(symbol_type, "Ginkgo")

    # Sanitize description for use in test ID
    # Replace problematic characters with underscores
    safe_desc = description.replace("::", "_").replace("/", "_")

    return f"{pkg_name}::{test_type}::{safe_desc}"


def _is_ginkgo_test_type(symbol_type: str) -> bool:
    """Check if a symbol type is a Ginkgo BDD test type."""
    return symbol_type.startswith("ginkgo_")


def _to_dict(result: CommitSymbolChanges) -> Dict:
    """Convert CommitSymbolChanges to dict for JSON serialization."""
    return {
        "sha": result.sha,
        "short_sha": result.short_sha,
        "files": [
            {
                "file_path": f.file_path,
                "old_path": f.old_path,
                "status": f.status,
                "language": f.language,
                "symbols_added": f.symbols_added,
                "symbols_modified": f.symbols_modified,
                "symbols_deleted": f.symbols_deleted,
            }
            for f in result.files
        ],
    }


def _is_test_symbol(sym: Dict, language: str) -> bool:
    """Check if a symbol is a test based on language-specific conventions.

    Args:
        sym: Symbol dict with 'name' and 'type' keys
        language: Programming language

    Returns:
        True if the symbol is a test method/function
    """
    sym_type = sym["type"]
    sym_name = sym["name"]

    if language == "java":
        # Java: JUnit 5 test annotations (@Test, @ParameterizedTest, @RepeatedTest, @TestFactory, @TestTemplate)
        java_test_prefixes = (
            "test_method",  # @Test
            "parameterized_test",  # @ParameterizedTest
            "repeated_test",  # @RepeatedTest
            "test_factory",  # @TestFactory
            "test_template",  # @TestTemplate
        )
        return sym_type.startswith(java_test_prefixes)

    elif language == "python":
        # Python pytest: test_* functions or methods in Test* classes
        if sym_type == "function" or sym_type == "method":
            return sym_name.startswith("test_") or sym_name.startswith("test")
        elif sym_type == "class":
            return sym_name.startswith("Test")
        return False

    elif language == "go":
        # Go testing conventions:
        # - Test functions: func TestXxx(t *testing.T) - must have uppercase after "Test"
        # - Benchmark functions: func BenchmarkXxx(b *testing.B) - must have uppercase after "Benchmark"
        # - Example functions: func ExampleXxx() - for documentation tests
        # - Fuzz functions: func FuzzXxx(f *testing.F) - Go 1.18+ fuzzing
        # - TestMain: func TestMain(m *testing.M) - NOT a test, it's setup/teardown
        #
        # Note: For Ginkgo BDD projects, we detect the suite entry functions (e.g., TestCore)
        # which are standard Go test functions. We do NOT detect individual It()/Describe() blocks
        # because go test reports only the suite entry functions.

        if sym_type in ("function", "method", "go_test_fn", "go_benchmark_fn", "go_example_fn", "go_fuzz_fn"):
            # TestMain is special - it's the test runner setup, not a test itself
            if sym_name == "TestMain":
                return False

            # Test functions: TestXxx where Xxx starts with uppercase
            if sym_name.startswith("Test"):
                if len(sym_name) == 4:
                    return False  # "Test" alone is not valid
                return sym_name[4].isupper()

            # Benchmark functions: BenchmarkXxx
            if sym_name.startswith("Benchmark"):
                if len(sym_name) == 9:
                    return False  # "Benchmark" alone is not valid
                return sym_name[9].isupper()

            # Example functions: ExampleXxx (can be just "Example" for package example)
            if sym_name.startswith("Example"):
                if len(sym_name) == 7:
                    return True  # "Example" alone is valid (package-level example)
                return sym_name[7].isupper() or sym_name[7] == "_"

            # Fuzz functions: FuzzXxx (Go 1.18+)
            if sym_name.startswith("Fuzz"):
                if len(sym_name) == 4:
                    return False  # "Fuzz" alone is not valid
                return sym_name[4].isupper()

        return False

    elif language == "rust":
        # Rust: attribute-based test detection
        rust_test_prefixes = (
            "test_fn",  # #[test]
            "test_should_panic",  # #[test] #[should_panic]
            "test_ignore",  # #[test] #[ignore]
            "test_attr_before",  # #[...] #[test]
            "tokio_test",  # #[tokio::test]
            "async_std_test",  # #[async_std::test]
            "actix_test",  # #[actix_rt::test]
            "rstest",  # #[rstest]
            "bench_fn",  # #[bench]
            # Macro-based tests
            "rgtest_macro",  # rgtest!()
            "test_case_macro",  # test_case!()
            "quickcheck_macro",  # quickcheck!()
            "proptest_macro",  # proptest!()
            "criterion_group_macro",  # criterion_group!()
        )
        if sym_type.startswith(rust_test_prefixes):
            return True

        # Fallback: naming convention for functions not matched by attribute patterns
        # This handles tests in tests/ directory or mod tests that might not match patterns
        if sym_type in ("function", "function_pub", "function_async", "function_pub_async"):
            return sym_name.startswith("test_")

        return False

    elif language in ("javascript", "typescript"):
        # JS/TS: test(), it(), describe() - harder to detect without context
        # Use naming convention as fallback
        if sym_type in ("function", "method"):
            return sym_name.startswith("test") or sym_name.endswith("Test")
        return False

    return False


def _to_dict_with_test_ids(result: CommitSymbolChanges) -> Dict:
    """Convert CommitSymbolChanges to dict with test IDs only.

    Uses language-specific test detection:
    - Java: @Test annotated methods only
    - Python: test_* functions, Test* classes
    - Go: Test* functions
    - Rust: test_* functions
    - JS/TS: test* functions

    Output format: module::package.ClassName::methodName (for Java)
    """
    test_ids_added = []
    test_ids_modified = []
    test_ids_deleted = []

    for f in result.files:
        language = f.language

        # Java-specific handling with module::package.Class::method format
        if language == "java":
            # Use all class symbols from the file (not just changed ones)
            all_symbols = f.symbols_added + f.symbols_modified + f.symbols_deleted
            class_symbols = f.all_classes if f.all_classes else []

            def find_containing_class(method_sym: Dict) -> Optional[str]:
                """Find the class that contains this method based on line numbers."""
                method_start = method_sym.get("start_line", 0)
                method_end = method_sym.get("end_line", 0)

                # Find the smallest enclosing class
                best_class = None
                best_range = float("inf")

                for cls in class_symbols:  # noqa: B023
                    cls_start = cls.get("start_line", 0)
                    cls_end = cls.get("end_line", 0)
                    # Check if method is within class range
                    if cls_start <= method_start and method_end <= cls_end:
                        range_size = cls_end - cls_start
                        if range_size < best_range:
                            best_range = range_size
                            best_class = cls["name"]

                # Fallback: extract from file name if no class found
                if not best_class:
                    file_name = f.file_path.split("/")[-1]  # noqa: B023
                    if file_name.endswith(".java"):
                        best_class = file_name[:-5]

                return best_class

            # Check if this file has any @Test methods
            has_test_methods = any(_is_test_symbol(sym, "java") for sym in all_symbols)

            if not has_test_methods:
                continue  # Skip files without @Test methods

            # Process test methods only
            for sym in f.symbols_added:
                if _is_test_symbol(sym, "java"):
                    class_name = find_containing_class(sym)
                    if class_name:
                        test_id = _extract_java_test_id(f.file_path, class_name, sym["name"])
                        if test_id:
                            test_ids_added.append(test_id)

            for sym in f.symbols_modified:
                if _is_test_symbol(sym, "java"):
                    class_name = find_containing_class(sym)
                    if class_name:
                        test_id = _extract_java_test_id(f.file_path, class_name, sym["name"])
                        if test_id:
                            test_ids_modified.append(test_id)

            for sym in f.symbols_deleted:
                if _is_test_symbol(sym, "java"):
                    class_name = find_containing_class(sym)
                    if class_name:
                        test_id = _extract_java_test_id(f.file_path, class_name, sym["name"])
                        if test_id:
                            test_ids_deleted.append(test_id)

        # Python handling - include class name in test_id for pytest nodeid matching
        elif language == "python":
            # Use all class symbols from the file for finding containing class
            class_symbols = f.all_classes if f.all_classes else []

            def find_containing_class_py(method_sym: Dict) -> Optional[str]:
                """Find the class that contains this method based on line numbers."""
                method_start = method_sym.get("start_line", 0)
                method_end = method_sym.get("end_line", 0)

                # Find the smallest enclosing class
                best_class = None
                best_range = float("inf")

                for cls in class_symbols:  # noqa: B023
                    cls_start = cls.get("start_line", 0)
                    cls_end = cls.get("end_line", 0)
                    # Check if method is within class range
                    if cls_start <= method_start and method_end <= cls_end:
                        range_size = cls_end - cls_start
                        if range_size < best_range:
                            best_range = range_size
                            best_class = cls.get("name")

                return best_class

            # Only process function/method symbols, not classes themselves
            # (pytest nodeids are file::Class::method, not file::Class::Class)
            for sym in f.symbols_added:
                if _is_test_symbol(sym, "python") and sym["type"] in ("function", "method"):
                    class_name = find_containing_class_py(sym)
                    if class_name:
                        test_ids_added.append(f"{f.file_path}::{class_name}::{sym['name']}")
                    else:
                        test_ids_added.append(f"{f.file_path}::{sym['name']}")

            for sym in f.symbols_modified:
                if _is_test_symbol(sym, "python") and sym["type"] in ("function", "method"):
                    class_name = find_containing_class_py(sym)
                    if class_name:
                        test_ids_modified.append(f"{f.file_path}::{class_name}::{sym['name']}")
                    else:
                        test_ids_modified.append(f"{f.file_path}::{sym['name']}")

            for sym in f.symbols_deleted:
                if _is_test_symbol(sym, "python") and sym["type"] in ("function", "method"):
                    class_name = find_containing_class_py(sym)
                    if class_name:
                        test_ids_deleted.append(f"{f.file_path}::{class_name}::{sym['name']}")
                    else:
                        test_ids_deleted.append(f"{f.file_path}::{sym['name']}")

        # Go handling (standard Go tests - Ginkgo suite entries are detected as standard Test* functions)
        elif language == "go":
            # Only process _test.go files for Go tests
            if not _is_go_test_file(f.file_path):
                # For renamed files, also check old_path
                if not (f.old_path and _is_go_test_file(f.old_path)):
                    continue

            # Get module name for generating full test IDs
            go_module = result.go_module_name

            # Check if this file has Ginkgo BDD tests
            has_ginkgo_tests = f.ginkgo_tests_before or f.ginkgo_tests_after

            # Check if this is a Ginkgo project (any file in the commit has Ginkgo tests)
            # This is used to filter out suite entry functions like TestLocal, TestCore, etc.
            is_ginkgo_project = any(
                gf.ginkgo_tests_before or gf.ginkgo_tests_after for gf in result.files if gf.language == "go"
            )

            if has_ginkgo_tests:
                # Ginkgo BDD project - generate test IDs with full hierarchy
                # Format: module/package::Describe > Context > It description
                pkg_path = _extract_go_package_path(f.file_path, go_module)

                # Build sets for comparison using full_path as key
                before_paths = {tc.full_path: tc for tc in f.ginkgo_tests_before}
                after_paths = {tc.full_path: tc for tc in f.ginkgo_tests_after}

                before_keys = set(before_paths.keys())
                after_keys = set(after_paths.keys())

                # Added tests: in after but not in before
                for path in sorted(after_keys - before_keys):
                    test_id = f"{pkg_path}::{path}"
                    test_ids_added.append(test_id)

                # Deleted tests: in before but not in after
                for path in sorted(before_keys - after_keys):
                    # For deleted, use old_path if file was renamed
                    if f.old_path:
                        old_pkg_path = _extract_go_package_path(f.old_path, go_module)
                        test_id = f"{old_pkg_path}::{path}"
                    else:
                        test_id = f"{pkg_path}::{path}"
                    test_ids_deleted.append(test_id)

                # Modified tests: in both, check if line range changed (indicates content change)
                for path in sorted(before_keys & after_keys):
                    tc_before = before_paths[path]
                    tc_after = after_paths[path]
                    # Consider modified if line numbers changed (suggests surrounding code changed)
                    if tc_before.start_line != tc_after.start_line or tc_before.end_line != tc_after.end_line:
                        test_id = f"{pkg_path}::{path}"
                        test_ids_modified.append(test_id)

            elif is_ginkgo_project:
                # Ginkgo project but this file has no Ginkgo tests (e.g., *_suite_test.go)
                # Skip Test* functions as they are likely suite entry points (e.g., TestLocal, TestCore)
                # These functions just call RunSpecs() and are not actual test cases
                pass

            else:
                # Standard Go test project - use Test* function detection
                for sym in f.symbols_added:
                    if _is_test_symbol(sym, "go"):
                        test_id = _extract_go_test_id(f.file_path, sym["name"], go_module)
                        if test_id:
                            test_ids_added.append(test_id)

                for sym in f.symbols_modified:
                    if _is_test_symbol(sym, "go"):
                        test_id = _extract_go_test_id(f.file_path, sym["name"], go_module)
                        if test_id:
                            test_ids_modified.append(test_id)

                for sym in f.symbols_deleted:
                    if _is_test_symbol(sym, "go"):
                        # For deleted symbols, use old_path if available (handles renames)
                        path_for_id = f.old_path if f.old_path else f.file_path
                        test_id = _extract_go_test_id(path_for_id, sym["name"], go_module)
                        if test_id:
                            test_ids_deleted.append(test_id)

        # Rust handling
        elif language == "rust":
            for sym in f.symbols_added:
                if _is_test_symbol(sym, "rust"):
                    test_id = _extract_rust_test_id(f.file_path, sym["name"], sym.get("mod_test_name"))
                    if test_id:
                        test_ids_added.append(test_id)

            for sym in f.symbols_modified:
                if _is_test_symbol(sym, "rust"):
                    test_id = _extract_rust_test_id(f.file_path, sym["name"], sym.get("mod_test_name"))
                    if test_id:
                        test_ids_modified.append(test_id)

            for sym in f.symbols_deleted:
                if _is_test_symbol(sym, "rust"):
                    # For deleted symbols, use old_path if available (handles renames)
                    path_for_id = f.old_path if f.old_path else f.file_path
                    test_id = _extract_rust_test_id(path_for_id, sym["name"], sym.get("mod_test_name"))
                    if test_id:
                        test_ids_deleted.append(test_id)

        # JavaScript/TypeScript handling
        elif language in ("javascript", "typescript"):
            # Only process test files for JavaScript/TypeScript tests
            if not _is_js_test_file(f.file_path):
                # For renamed files, also check old_path
                if not (f.old_path and _is_js_test_file(f.old_path)):
                    continue

            # Check if this file has Jest BDD tests
            has_jest_tests = f.jest_tests_before or f.jest_tests_after

            if has_jest_tests:
                # Jest BDD project - generate test IDs with full hierarchy
                # Format: file_path::describe > ... > it/test description

                # Build sets for comparison using full_path as key
                before_paths = {tc.full_path: tc for tc in f.jest_tests_before}
                after_paths = {tc.full_path: tc for tc in f.jest_tests_after}

                before_keys = set(before_paths.keys())
                after_keys = set(after_paths.keys())

                # Added tests: in after but not in before
                for path in sorted(after_keys - before_keys):
                    test_id = f"{f.file_path}::{path}"
                    test_ids_added.append(test_id)

                # Deleted tests: in before but not in after
                for path in sorted(before_keys - after_keys):
                    # For deleted, use old_path if file was renamed
                    if f.old_path:
                        test_id = f"{f.old_path}::{path}"
                    else:
                        test_id = f"{f.file_path}::{path}"
                    test_ids_deleted.append(test_id)

                # Modified tests: in both, check if line range changed (indicates content change)
                for path in sorted(before_keys & after_keys):
                    tc_before = before_paths[path]
                    tc_after = after_paths[path]
                    # Consider modified if line numbers changed (suggests surrounding code changed)
                    if tc_before.start_line != tc_after.start_line or tc_before.end_line != tc_after.end_line:
                        test_id = f"{f.file_path}::{path}"
                        test_ids_modified.append(test_id)

            else:
                # Fallback: No Jest tests detected, use standard symbol-based detection
                for sym in f.symbols_added:
                    if _is_test_symbol(sym, language):
                        test_ids_added.append(f"{f.file_path}::{sym['name']}")

                for sym in f.symbols_modified:
                    if _is_test_symbol(sym, language):
                        test_ids_modified.append(f"{f.file_path}::{sym['name']}")

                for sym in f.symbols_deleted:
                    if _is_test_symbol(sym, language):
                        test_ids_deleted.append(f"{f.file_path}::{sym['name']}")

    # Deduplicate test IDs (same test may be matched by multiple patterns)
    test_ids_added = sorted(set(test_ids_added))
    test_ids_modified = sorted(set(test_ids_modified))
    test_ids_deleted = sorted(set(test_ids_deleted))

    return {
        "sha": result.sha,
        "short_sha": result.short_sha,
        "summary": {
            "added": len(test_ids_added),
            "modified": len(test_ids_modified),
            "deleted": len(test_ids_deleted),
            "total": len(test_ids_added) + len(test_ids_modified) + len(test_ids_deleted),
        },
        "test_ids": {
            "added": test_ids_added,
            "modified": test_ids_modified,
            "deleted": test_ids_deleted,
        },
    }


def load_commits_from_file(file_path: str) -> List[str]:
    """Load commit SHAs from a file (plain text or JSONL)."""
    commits = []
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                data = json.loads(line)
                # Only treat as JSONL if it's a dict with 'sha' key
                if isinstance(data, dict):
                    sha = data.get("sha")
                    if sha:
                        commits.append(sha)
                else:
                    # Not a dict (e.g., parsed as int), treat as plain text
                    commits.append(line.split()[0])
            except json.JSONDecodeError:
                commits.append(line.split()[0])
    return commits


def load_test_patterns_from_config(config_path: str) -> List[str]:
    """Load test file patterns from repo config YAML.

    Expected config format:
        test_dirs:
          - "**/src/test/java/**"
          - "**/*Test.java"
          - "test_*.py"
    """
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    patterns = config.get("test_dirs", [])
    if not patterns:
        # Fallback to common patterns if not specified
        print(f"Warning: No 'test_dirs' found in {config_path}", file=sys.stderr)

    return patterns


@dataclass
class SplitCommitInfo:
    """Information about a split commit."""

    original_sha: str
    original_short_sha: str
    split_sha: str
    split_short_sha: str
    split_order: int
    split_message: Optional[str] = None


def parse_split_metadata(metadata_path: str) -> List[SplitCommitInfo]:
    """Parse a METADATA.json file and extract split commit information.

    Handles two different metadata formats:
    1. Format with 'commits' array (contains sha, files)
    2. Format with 'split_commits' array (contains sha, sequence)

    Args:
        metadata_path: Path to METADATA.json file

    Returns:
        List of SplitCommitInfo objects
    """
    with open(metadata_path, "r") as f:
        metadata = json.load(f)

    result = []

    # Get original commit info
    original = metadata.get("original_commit", {})
    original_sha = original.get("sha", "")
    original_short_sha = original.get("short_sha", original_sha[:7] if original_sha else "")

    # Try format 1: 'commits' array
    commits = metadata.get("commits", [])
    if commits:
        for commit in commits:
            result.append(
                SplitCommitInfo(
                    original_sha=original_sha,
                    original_short_sha=original_short_sha,
                    split_sha=commit.get("sha", ""),
                    split_short_sha=commit.get("short_sha", ""),
                    split_order=commit.get("order", 0),
                    split_message=commit.get("message"),
                )
            )
        return result

    # Try format 2: 'split_commits' array
    split_commits = metadata.get("split_commits", [])
    if split_commits:
        for commit in split_commits:
            result.append(
                SplitCommitInfo(
                    original_sha=original_sha,
                    original_short_sha=original_short_sha,
                    split_sha=commit.get("sha", ""),
                    split_short_sha=commit.get("short_sha", ""),
                    split_order=commit.get("sequence", 0),
                    split_message=commit.get("message"),
                )
            )
        return result

    return result


def discover_split_commits(commit_splits_dir: str) -> Dict[str, List[SplitCommitInfo]]:
    """Discover all split commits from a commit_splits directory.

    Args:
        commit_splits_dir: Path to commit_splits directory containing split-* subdirectories

    Returns:
        Dict mapping original_sha to list of SplitCommitInfo
    """
    splits_path = Path(commit_splits_dir)
    result = {}

    if not splits_path.exists():
        print(f"Warning: commit_splits directory not found: {commit_splits_dir}", file=sys.stderr)
        return result

    # Find all split-* directories
    for split_dir in sorted(splits_path.iterdir()):
        if not split_dir.is_dir() or not split_dir.name.startswith("split-"):
            continue

        # Look for METADATA.json (case-insensitive)
        metadata_file = None
        for name in ["METADATA.json", "metadata.json"]:
            candidate = split_dir / name
            if candidate.exists():
                metadata_file = candidate
                break

        if not metadata_file:
            continue

        try:
            split_infos = parse_split_metadata(str(metadata_file))
            for info in split_infos:
                if info.original_sha not in result:
                    result[info.original_sha] = []
                result[info.original_sha].append(info)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Warning: Failed to parse {metadata_file}: {e}", file=sys.stderr)
            continue

    return result


@dataclass
class SplitCommitTestChanges:
    """Test changes aggregated from split commits."""

    original_sha: str
    original_short_sha: str
    split_commits: List[Dict]  # List of {sha, short_sha, order, message}
    test_changes: List[Dict]  # Aggregated test file changes


def extract_split_commits_test_changes(
    repo_path: str,
    commit_splits_dir: str,
    config_path: str,
    test_patterns: Optional[List[str]] = None,
    output_dir: Optional[str] = None,
    verbose: bool = False,
) -> List[CommitSymbolChanges]:
    """Extract test changes from split commits.

    This function:
    1. Discovers all split commits from the commit_splits directory
    2. For each split commit, extracts test file changes
    3. Outputs one file per split commit (same format as regular commits)

    Args:
        repo_path: Path to the git repository
        commit_splits_dir: Path to commit_splits directory
        config_path: Path to symbol_extraction_config.yml
        test_patterns: Glob patterns for test files (e.g., ["**/src/test/java/**", "*Test.java"])
        output_dir: Directory to save results (one file per split commit)
        verbose: Enable verbose output

    Returns:
        List of CommitSymbolChanges, one per split commit
    """
    # Default test patterns if not provided
    if not test_patterns:
        test_patterns = [
            "**/src/test/java/**",
            "**/src/test/resources/**",
            "**/*Test.java",
            "**/*Tests.java",
            "**/*TestCase.java",
            "**/test_*.py",
            "**/*_test.py",
            "**/test/**",
            "**/*_test.go",
            "**/*_test.rs",
        ]

    # Discover split commits
    if verbose:
        print(f"Discovering split commits in {commit_splits_dir}...", file=sys.stderr)

    split_commits_map = discover_split_commits(commit_splits_dir)

    if not split_commits_map:
        print("No split commits found.", file=sys.stderr)
        return []

    if verbose:
        total_splits = sum(len(v) for v in split_commits_map.values())
        print(
            f"Found {len(split_commits_map)} original commits with {total_splits} split commits",
            file=sys.stderr,
        )

    # Initialize extractor
    extractor = CommitSymbolChangeExtractor(repo_path, config_path)

    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    results = []

    for original_sha, split_infos in split_commits_map.items():
        if verbose:
            print(f"\nProcessing original commit {original_sha[:7]}...", file=sys.stderr)

        for info in sorted(split_infos, key=lambda x: x.split_order):
            if verbose:
                print(
                    f"  Analyzing split commit {info.split_order}: {info.split_short_sha}...",
                    end=" ",
                    file=sys.stderr,
                )

            try:
                changes = extractor.extract_commit_changes(info.split_sha, test_patterns)
                results.append(changes)

                # Save to file if output_dir specified
                if output_dir:
                    output_file = Path(output_dir) / f"{changes.short_sha}_symbol_changes.json"
                    with open(output_file, "w") as f:
                        json.dump(_to_dict(changes), f, indent=2)

                if verbose:
                    total_added = sum(len(fc.symbols_added) for fc in changes.files)
                    total_modified = sum(len(fc.symbols_modified) for fc in changes.files)
                    total_deleted = sum(len(fc.symbols_deleted) for fc in changes.files)
                    print(f"✓ (+{total_added} ~{total_modified} -{total_deleted})", file=sys.stderr)

            except Exception as e:
                if verbose:
                    print(f"✗ Error: {e}", file=sys.stderr)
                # Still add an empty result for tracking
                results.append(CommitSymbolChanges(sha=info.split_sha, short_sha=info.split_short_sha))

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Extract symbol changes (added/modified/deleted) from commits using ast-grep"
    )
    parser.add_argument("--repo-path", required=True, help="Path to git repository")
    parser.add_argument(
        "--config-path",
        default=str(Path(__file__).parent / "configs" / "symbol_extraction_config.yml"),
        help="Path to symbol_extraction_config.yml",
    )
    parser.add_argument("--commits", nargs="+", help="Commit SHAs to analyze")
    parser.add_argument("--commits-file", help="File containing commit SHAs")
    parser.add_argument("--output-dir", help="Directory to save results")
    parser.add_argument(
        "--repo-config",
        help="Path to repo config YAML (loads test_dirs as test patterns)",
    )
    parser.add_argument(
        "--test-patterns",
        nargs="+",
        help="Only process test files matching these glob patterns (overrides --repo-config)",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    parser.add_argument(
        "--with-test-ids",
        action="store_true",
        help="Include Java test IDs in format: module::package.ClassName::methodName",
    )

    # Split commits test extraction mode
    parser.add_argument(
        "--split-commits-dir",
        help="Path to commit_splits directory (e.g., stage4_decomp/output/commit_splits). "
        "When specified, extracts test changes from split commits instead of regular commits.",
    )

    args = parser.parse_args()

    # Determine test patterns
    test_patterns = args.test_patterns
    if not test_patterns and args.repo_config:
        test_patterns = load_test_patterns_from_config(args.repo_config)
        if args.verbose and test_patterns:
            print(f"Loaded {len(test_patterns)} test patterns from {args.repo_config}", file=sys.stderr)

    # Mode: Split commits test extraction
    if args.split_commits_dir:
        results = extract_split_commits_test_changes(
            repo_path=args.repo_path,
            commit_splits_dir=args.split_commits_dir,
            config_path=args.config_path,
            test_patterns=test_patterns,
            output_dir=args.output_dir,
            verbose=args.verbose,
        )

        if args.output_dir:
            if args.verbose:
                print(
                    f"\n✓ Saved {len(results)} results to {args.output_dir}",
                    file=sys.stderr,
                )
        else:
            output = [_to_dict(r) for r in results]
            print(json.dumps(output, indent=2))

        return 0

    # Mode: Regular commit analysis
    # Collect commits
    commits = []
    if args.commits:
        commits.extend(args.commits)
    if args.commits_file:
        commits.extend(load_commits_from_file(args.commits_file))

    if not commits:
        print("Error: No commits specified. Use --commits or --commits-file", file=sys.stderr)
        return 1

    # Extract changes
    results = extract_changes_batch(
        repo_path=args.repo_path,
        commit_shas=commits,
        config_path=args.config_path,
        output_dir=args.output_dir,
        include_patterns=test_patterns,
        verbose=args.verbose,
        with_test_ids=args.with_test_ids,
    )

    # Output
    if args.output_dir:
        if args.verbose:
            print(f"\n✓ Saved {len(results)} results to {args.output_dir}", file=sys.stderr)
    else:
        to_dict_fn = _to_dict_with_test_ids if args.with_test_ids else _to_dict
        output = [to_dict_fn(r) for r in results]
        print(json.dumps(output, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
