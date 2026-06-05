#!/usr/bin/env python3
"""
Symbol Extraction Module for DeepCommit
Extracts symbols from git commit changes using ast-grep on complete files
"""

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import pygit2
import yaml

from deepcommit.harness.utils.src_filter import SrcFileFilter


@dataclass
class FileChange:
    """Represents changes to a file in a commit"""

    file_path: str
    language: str
    full_content: str  # Complete file content after commit


class CommitHunkExtractor:
    """Extract changed hunks from git commits using pygit2"""

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)
        self.repo = pygit2.Repository(str(self.repo_path))

    def get_commit_changes(self, commit_sha: str) -> List[FileChange]:
        """
        Get all changed files for a commit

        Args:
            commit_sha: Git commit SHA

        Returns:
            List of FileChange objects
        """
        commit = self.repo.revparse_single(commit_sha)
        assert isinstance(commit, pygit2.Commit)
        parent_tree = commit.parents[0].tree if commit.parents else None

        # Generate diff between parent and commit
        diff = self.repo.diff(parent_tree, commit.tree)  # type: ignore[arg-type]

        file_changes = []
        for patch in diff:
            # Skip deleted files
            if patch.delta.status == pygit2.GIT_DELTA_DELETED:
                continue

            file_path = patch.delta.new_file.path

            # Get complete file content after commit
            full_content = self._get_file_content(commit, file_path)
            if full_content is None:
                continue

            # Detect language
            language = self._detect_language(file_path)

            file_changes.append(FileChange(file_path=file_path, language=language, full_content=full_content))

        return file_changes

    def get_added_lines(self, commit_sha: str, file_path: str) -> List[int]:
        """
        Get line numbers of added lines (+ prefix) in a file

        Args:
            commit_sha: Git commit SHA
            file_path: Path to file

        Returns:
            List of line numbers that were added
        """
        commit = self.repo.revparse_single(commit_sha)
        assert isinstance(commit, pygit2.Commit)
        parent_tree = commit.parents[0].tree if commit.parents else None

        # Generate diff with zero context
        diff = self.repo.diff(parent_tree, commit.tree, context_lines=0)  # type: ignore[arg-type]

        added_lines = []
        for patch in diff:
            if patch.delta.new_file.path == file_path:
                for hunk in patch.hunks:
                    for line in hunk.lines:
                        if line.origin == "+":
                            added_lines.append(line.new_lineno)
        return added_lines

    def get_changed_context_lines(self, commit_sha: str, file_path: str, context_lines: int = 3) -> List[int]:
        """
        Get line numbers of changed lines with context

        Args:
            commit_sha: Git commit SHA
            file_path: Path to file
            context_lines: Number of context lines to include (default: 3)

        Returns:
            List of line numbers including added lines and context
        """
        commit = self.repo.revparse_single(commit_sha)
        assert isinstance(commit, pygit2.Commit)
        parent_tree = commit.parents[0].tree if commit.parents else None

        # Generate diff with context
        diff = self.repo.diff(parent_tree, commit.tree, context_lines=context_lines)  # type: ignore[arg-type]

        changed_lines = []
        for patch in diff:
            if patch.delta.new_file.path == file_path:
                for hunk in patch.hunks:
                    for line in hunk.lines:
                        # Include added lines ('+') and context lines (' ')
                        if line.origin in ("+", " "):
                            changed_lines.append(line.new_lineno)
        return changed_lines

    def _get_file_content(self, commit: pygit2.Commit, file_path: str) -> Optional[str]:
        """
        Get complete file content after commit

        Args:
            commit: pygit2 Commit object
            file_path: Path to file in tree

        Returns:
            File content as string, or None if file doesn't exist
        """
        try:
            tree_entry = commit.tree[file_path]
            blob = self.repo[tree_entry.id]
            return blob.data.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
        except (KeyError, AttributeError):
            return None

    def _detect_language(self, file_path: str) -> str:
        """Detect programming language from file extension"""
        ext = Path(file_path).suffix.lower()
        language_map = {
            ".py": "python",
            ".js": "javascript",
            ".jsx": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".go": "go",
            ".rs": "rust",
            ".java": "java",
            ".c": "c",
            ".h": "c",
            ".cpp": "cpp",
            ".cc": "cpp",
            ".cxx": "cpp",
            ".hpp": "cpp",
        }
        return language_map.get(ext, "unknown")


class SymbolExtractionConfig:
    """Load and manage symbol extraction configuration from YAML"""

    def __init__(self, config_path: str):
        """Load configuration from YAML file"""
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.language_extensions = self.config.get("language_extensions", {})
        self.astgrep_patterns = self.config.get("astgrep_patterns", {})
        self.regex_patterns = self.config.get("regex_patterns", {})

    def get_file_extension(self, language: str) -> str:
        """Get file extension for a language"""
        exts = self.language_extensions.get(language, [])
        return exts[0] if exts else f".{language}"

    def get_patterns(self, language: str) -> Dict:
        """Get AST-grep patterns for a language"""
        return self.astgrep_patterns.get(language, {})

    def get_regex_pattern(self, symbol_type: str, language: str) -> str:
        """Get regex pattern for extracting symbol name"""
        pattern_dict = self.regex_patterns.get(symbol_type, {})
        # Try language-specific pattern first, then default
        return pattern_dict.get(language, pattern_dict.get("default", ""))


class CommitSymbolExtractor:
    """Extract symbols from commit changes using ast-grep"""

    def __init__(self, repo_path: str, config_path: str):
        self.repo_path = repo_path
        self.hunk_extractor = CommitHunkExtractor(repo_path)
        self.config = SymbolExtractionConfig(config_path)

    def extract_commit_symbols(
        self,
        commit_sha: str,
        include_context: bool = True,
        context_lines: int = 3,
        repo_src_dirs: Optional[List[str]] = None,
        minimal_output: bool = False,
        exclude_patterns: Optional[List[str]] = None,
    ) -> Dict:
        """
        Extract symbols from changed lines in a commit

        Args:
            commit_sha: Git commit SHA
            include_context: If True, include context lines around changes (default: True)
            context_lines: Number of context lines to include (default: 3)
            repo_src_dirs: Only extract symbols from files in these directories (default: None)
                          Set to None or empty list to process all files
            minimal_output: If True, only output symbol names without types/language (default: False)
            exclude_patterns: List of glob patterns to exclude (e.g., ['**/*_test.go', 'tests/**'])

        Returns:
            Dict with extracted symbols organized by file
        """
        file_changes = self.hunk_extractor.get_commit_changes(commit_sha)

        # Normalize SHA to 7 characters
        results = {"commit": commit_sha[:7], "files": []}

        for file_change in file_changes:
            if file_change.language == "unknown":
                continue

            # Filter by exclude patterns (test_dirs + exclude)
            if exclude_patterns and SrcFileFilter.should_exclude_file(file_change.file_path, exclude_patterns):
                continue

            # Filter by source directories if specified
            if repo_src_dirs:
                # Check if file is in any of the source directories
                file_in_src = False
                for src_dir in repo_src_dirs:
                    src_dir_normalized = src_dir.rstrip("/") + "/"
                    if file_change.file_path.startswith(src_dir_normalized):
                        file_in_src = True
                        break
                if not file_in_src:
                    continue

            # Get changed lines (added + context or just added)
            if include_context:
                changed_lines = self.hunk_extractor.get_changed_context_lines(
                    commit_sha, file_change.file_path, context_lines
                )
            else:
                changed_lines = self.hunk_extractor.get_added_lines(commit_sha, file_change.file_path)

            if not changed_lines:
                continue

            # Extract symbols from complete file
            all_symbols = self._extract_symbols_from_file(file_change.full_content, file_change.language)

            # Filter symbols that are in changed lines
            changed_symbols = self._filter_symbols_in_lines(all_symbols, changed_lines)

            if changed_symbols:
                if minimal_output:
                    # Minimal format: only file_path and symbol names
                    results["files"].append(
                        {
                            "file_path": file_change.file_path,
                            "symbols": [s["name"] for s in changed_symbols],
                        }
                    )
                else:
                    # Full format: include language and symbol types
                    results["files"].append(
                        {
                            "file_path": file_change.file_path,
                            "language": file_change.language,
                            "symbols": changed_symbols,  # Already sorted in _filter_symbols_in_lines
                        }
                    )

        return results

    def _extract_symbols_from_file(self, content: str, language: str) -> List[Dict]:
        """
        Extract symbols from complete file using ast-grep

        Returns:
            List of {name, type, line} dicts
        """
        symbols = []

        # Get patterns from config for this language
        patterns = self.config.get_patterns(language)
        if not patterns:
            return []

        # Get file extension from config
        file_ext = self.config.get_file_extension(language)

        # Write to temporary file
        with tempfile.NamedTemporaryFile(mode="w", suffix=file_ext, delete=False) as f:
            f.write(content)
            temp_file = f.name

        try:
            for pattern_type, pattern in patterns.items():
                result = subprocess.run(
                    [
                        "ast-grep",
                        "run",
                        "--pattern",
                        pattern,
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
                            line_num = match.get("range", {}).get("start", {}).get("line")
                            if line_num is not None:
                                symbol_name = self._extract_symbol_name(match.get("lines", ""), pattern_type, language)
                                if symbol_name:
                                    symbols.append(
                                        {
                                            "name": symbol_name,
                                            "type": pattern_type,
                                            "line": line_num + 1,  # ast-grep uses 0-indexed lines
                                        }
                                    )
                    except json.JSONDecodeError:
                        pass

        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"Warning: ast-grep failed - {e}")
        finally:
            Path(temp_file).unlink(missing_ok=True)

        return symbols

    def _extract_symbol_name(self, line: str, pattern_type: str, language: str) -> Optional[str]:
        """
        Extract symbol name from matched line using configured regex

        Args:
            line: Matched line from ast-grep
            pattern_type: Type of pattern (function_call, method_call, etc.)
            language: Programming language

        Returns:
            Extracted symbol name or None
        """
        text = line.strip()

        # Get regex pattern from config
        regex_pattern = self.config.get_regex_pattern(pattern_type, language)
        if not regex_pattern:
            return None

        # Apply regex to extract symbol name
        m = re.search(regex_pattern, text)
        if m:
            # Return first captured group if exists, otherwise the whole match
            return m.group(1) if m.groups() else m.group(0)

        return None

    def _filter_symbols_in_lines(self, symbols: List[Dict], added_lines: List[int]) -> List[Dict]:
        """
        Filter symbols to only those in added lines and group by name

        Returns:
            List of dicts with {name: str, types: List[str]}
        """
        added_line_set = set(added_lines)
        # Use dict to aggregate types for each symbol name
        symbol_types = {}

        for symbol in symbols:
            if symbol["line"] in added_line_set:
                name = symbol["name"]
                stype = symbol["type"]
                if name not in symbol_types:
                    symbol_types[name] = []
                if stype not in symbol_types[name]:  # Avoid duplicate types
                    symbol_types[name].append(stype)

        # Convert to list of dicts and sort
        result = [{"name": name, "types": sorted(types)} for name, types in sorted(symbol_types.items())]
        return result


def extract_symbols_batch(
    repo_path: str,
    commit_shas: List[str],
    config_path: str,
    include_context: bool = True,
    context_lines: int = 3,
    repo_src_dirs: Optional[List[str]] = None,
    minimal_output: bool = False,
    verbose: bool = False,
    test_dirs: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
) -> List[Dict]:
    """
    Extract symbols from multiple commits

    Args:
        repo_path: Path to git repository
        commit_shas: List of commit SHAs
        config_path: Path to symbol extraction config YAML file
        include_context: If True, include context lines around changes (default: True)
        context_lines: Number of context lines to include (default: 3)
        repo_src_dirs: Only extract symbols from files in these directories (default: None)
                      Set to None or empty list to process all files
        minimal_output: If True, only output symbol names without types/language (default: False)
        verbose: Enable verbose output
        test_dirs: Test directory/file patterns to exclude (e.g., ['tests/**', '**/*_test.go'])
        exclude: Additional custom glob patterns to exclude

    Returns:
        List of symbol extraction results for each commit
    """
    extractor = CommitSymbolExtractor(repo_path, config_path)
    results = []

    # Build exclude patterns from test_dirs and exclude
    exclude_patterns = SrcFileFilter.build_exclude_patterns(test_dirs=test_dirs, exclude=exclude)
    if exclude_patterns and verbose:
        print(f"Excluding patterns: {exclude_patterns}")

    for i, commit_sha in enumerate(commit_shas, 1):
        if verbose:
            print(f"Processing commit {i}/{len(commit_shas)}: {commit_sha[:7]}...", end=" ")

        try:
            result = extractor.extract_commit_symbols(
                commit_sha, include_context, context_lines, repo_src_dirs, minimal_output, exclude_patterns
            )
            results.append(result)

            if verbose:
                symbol_count = sum(len(f.get("symbols", [])) for f in result.get("files", []))
                print(f"✓ ({symbol_count} symbols)")
        except Exception as e:
            if verbose:
                print(f"✗ Error: {e}")
            # Add empty result for failed commits
            results.append({"commit": commit_sha[:7], "files": []})

    return results
