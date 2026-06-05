from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Optional pygit2 support
try:
    import pygit2

    HAS_PYGIT2 = True
except ImportError:
    HAS_PYGIT2 = False
    pygit2 = None  # type: ignore

from deepcommit.harness.utils.src_filter import SrcFileFilter

# ---------------------------- Data Structures ---------------------------- #


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    added_lines: List[str] = field(default_factory=list)
    removed_lines: List[str] = field(default_factory=list)


@dataclass
class FileDiff:
    old_path: Optional[str]
    new_path: Optional[str]
    hunks: List[Hunk] = field(default_factory=list)


@dataclass
class EdgeMetadata:
    """Metadata for an edge, recording file and line information for dependencies."""

    file_ranges: Dict[str, List[Tuple[int, int]]] = field(default_factory=dict)  # File path -> blamed line ranges
    verified: bool = False  # Whether verified by git apply


@dataclass
class Edge:
    src: str
    dst: str
    kind: str  # 'blame_verified' | 'blame_only'
    metadata: EdgeMetadata
    types: List[str] = field(default_factory=list)  # New: semantic edge types

    @staticmethod
    def create(src: str, dst: str, kind: str, meta: EdgeMetadata) -> "Edge":
        """Create an edge with metadata."""
        return Edge(src=src, dst=dst, kind=kind, metadata=meta)


@dataclass
class Node:
    sha: str
    subject: str = ""


@dataclass
class DagResult:
    nodes: Dict[str, Node]
    edges: List[Edge]
    levels: Dict[str, int]
    external_deps: Set[str]
    cycles: List[List[str]]


# ------------------------- File Filter Patterns -------------------------- #

# Test file patterns
TEST_FILE_PATTERNS = [
    # Python
    "**/test_*.py",
    "**/*_test.py",
    "**/tests/**/*",
    "**/test/**/*",
    "**/pytest/**/*",
    # Go
    "**/*_test.go",
    # JavaScript/TypeScript
    "**/__tests__/**/*",
    "**/*.test.js",
    "**/*.test.ts",
    "**/*.test.jsx",
    "**/*.test.tsx",
    "**/*.spec.js",
    "**/*.spec.ts",
    "**/*.spec.jsx",
    "**/*.spec.tsx",
    # Ruby
    "**/*_spec.rb",
    "**/spec/**/*",
    # Rust
    "**/tests/*.rs",
    # Java/Kotlin
    "**/src/test/**/*",
    "**/*Test.java",
    "**/*Test.kt",
]

# CI/CD workflow file patterns
CI_CD_FILE_PATTERNS = [
    ".github/workflows/**/*",
    ".gitlab-ci.yml",
    ".travis.yml",
    "Jenkinsfile",
    ".circleci/**/*",
    "azure-pipelines.yml",
    ".azure-pipelines.yml",
    "**/ci/**/*",
    "**/CI/**/*",
    ".buildkite/**/*",
]

# Configuration and build file patterns
CONFIG_FILE_PATTERNS = [
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements*.txt",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".dockerignore",
    "Makefile",
    "CMakeLists.txt",
    "meson.build",
    ".editorconfig",
    ".gitignore",
    ".gitattributes",
]

# Documentation file patterns
DOC_FILE_PATTERNS = [
    "README.md",
    "README.rst",
    "README.txt",
    "README",
    "readme.md",
    "Readme.md",
    "**/docs/**/*",
    "**/doc/**/*",
    "**/documentation/**/*",
    "**/_docs/**/*",
    "**/site/**/*",
    "**/_site/**/*",
    "**/public/docs/**/*",
    "**/*.md",
    "**/*.rst",
    "**/*.adoc",
    "**/*.rdoc",
    "**/*.txt",
    "mkdocs.yml",
    "mkdocs.yaml",
    "docusaurus.config.js",
    "docusaurus.config.ts",
    "**/conf.py",
    "**/.readthedocs.yml",
    "**/.readthedocs.yaml",
    "LICENSE*",
    "COPYING*",
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    "CHANGELOG.rst",
    "HISTORY.md",
    "HISTORY.rst",
    "CHANGES.md",
    "NEWS.md",
    "NOTICE*",
    "AUTHORS*",
    "CONTRIBUTORS*",
]


# ---------------------------- Git Utilities ------------------------------ #


class GitOperations:
    """Abstraction layer for git operations, supporting both subprocess and pygit2."""

    def __init__(self, repo_path: Path, use_pygit2: bool = False):
        self.repo_path = Path(repo_path)
        self.use_pygit2 = use_pygit2 and HAS_PYGIT2

        # Fine-grained control: some operations may be faster with subprocess
        # Diff operations through subprocess are often faster due to git's internal optimizations
        self.use_pygit2_for_diff = False  # Keep diff with subprocess for now

        if self.use_pygit2:
            try:
                self.pygit2_repo = pygit2.Repository(str(repo_path))
            except Exception as e:
                print(f"Warning: Failed to initialize pygit2 repository: {e}")
                print("Falling back to subprocess...")
                self.use_pygit2 = False
                self.pygit2_repo = None
        else:
            self.pygit2_repo = None

    def _run_subprocess(self, args: List[str], check: bool = True) -> str:
        """Run git command via subprocess."""
        cmd = ["git", "-C", str(self.repo_path)] + args
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {stderr}")
        return proc.stdout.decode("utf-8", errors="replace")

    def get_parent_commit(self, commit: str) -> Optional[str]:
        """Get the first parent of a commit."""
        if self.use_pygit2 and self.pygit2_repo:
            try:
                c = self.pygit2_repo.revparse_single(commit)
                if hasattr(c, "parent_ids") and c.parent_ids:  # type: ignore[attr-defined]
                    return str(c.parent_ids[0])  # type: ignore[attr-defined]
                return None
            except Exception:
                pass

        try:
            out = self._run_subprocess(["show", "-s", "--format=%P", commit], check=False).strip()
        except Exception:
            return None
        if not out:
            return None
        return out.split()[0]

    def get_subject(self, commit: str) -> str:
        """Get the commit subject (first line of message)."""
        if self.use_pygit2 and self.pygit2_repo:
            try:
                c = self.pygit2_repo.revparse_single(commit)
                if hasattr(c, "message"):
                    return c.message.split("\n")[0].strip()  # type: ignore[attr-defined]
            except Exception:
                pass

        try:
            return self._run_subprocess(["show", "-s", "--format=%s", commit], check=False).strip()
        except Exception:
            return ""

    def get_diff(self, commit: str, unified: int = 0, detect_renames: bool = True, follow_depth: int = 2) -> str:
        """Get the diff for a commit.

        Args:
            commit: commit SHA
            unified: number of context lines
            detect_renames: whether to detect renames and copies
            follow_depth: depth of copy detection (1-3), higher = more thorough but slower (default: 2)

        Note: Currently uses subprocess even when pygit2 is enabled, as git's
        internal diff implementation is often faster than pygit2's patch generation.
        """
        if self.use_pygit2 and self.use_pygit2_for_diff and self.pygit2_repo:
            try:
                c = self.pygit2_repo.revparse_single(commit)
                if not isinstance(c, pygit2.Commit):
                    c = c.peel(pygit2.Commit)
                if c.parent_ids:
                    parent = self.pygit2_repo.get(c.parent_ids[0])
                    diff = self.pygit2_repo.diff(parent.tree, c.tree, context_lines=unified)  # type: ignore[arg-type]
                else:
                    diff = c.tree.diff_to_tree(swap=True, context_lines=unified)

                if detect_renames:
                    diff.find_similar(flags=pygit2.GIT_DIFF_FIND_RENAMES | pygit2.GIT_DIFF_FIND_COPIES)  # type: ignore[arg-type]

                return diff.patch or ""
            except Exception as e:
                print(f"Warning: pygit2 diff failed for {commit[:7]}: {e}")

        # Subprocess implementation (default, faster for diff operations)
        args = ["show", "--format=", f"-U{unified}"]
        if detect_renames:
            args.append("-M")
            for _ in range(follow_depth):
                args.append("-C")
        args.append(commit)
        return self._run_subprocess(args, check=False)

    def blame_file_range(
        self,
        commit: str,
        path: str,
        start: int,
        end: int,
        follow_moves: bool = True,
        ignore_whitespace: bool = True,
        follow_depth: int = 2,
    ) -> List[str]:
        """Blame a specific line range in a file at a commit.

        Args:
            commit: commit SHA to blame
            path: file path
            start: start line number (1-indexed)
            end: end line number (1-indexed)
            follow_moves: whether to track moves and copies
            ignore_whitespace: whether to ignore whitespace changes
            follow_depth: depth of copy detection (1-3), higher = more thorough but slower (default: 2)
                         1: same commit modified files
                         2: same commit all files (default, good balance)
                         3: cross-commit history (most accurate but slowest)
        """
        if start <= 0 or end < start:
            return []

        if self.use_pygit2 and self.pygit2_repo:
            try:
                flags = 0
                if follow_moves:
                    flags |= pygit2.GIT_BLAME_TRACK_COPIES_SAME_COMMIT_MOVES
                    flags |= pygit2.GIT_BLAME_TRACK_COPIES_SAME_COMMIT_COPIES
                if ignore_whitespace:
                    flags |= pygit2.GIT_BLAME_IGNORE_WHITESPACE

                blame = self.pygit2_repo.blame(path, newest_commit=commit, min_line=start, max_line=end, flags=flags)  # type: ignore[arg-type]

                # Optimize: directly map line numbers to commits without expanding all lines
                commits = []
                for hunk in blame:
                    # hunk.final_start_line_number is 1-indexed
                    hunk_start = hunk.final_start_line_number
                    hunk_end = hunk_start + hunk.lines_in_hunk - 1

                    # Calculate overlap with requested range [start, end]
                    overlap_start = max(hunk_start, start)
                    overlap_end = min(hunk_end, end)

                    if overlap_start <= overlap_end:
                        # Convert commit_id to string once per hunk, not per line
                        commit_str = str(hunk.final_commit_id)
                        num_lines = overlap_end - overlap_start + 1
                        commits.extend([commit_str] * num_lines)

                return commits
            except Exception as e:
                print(f"Warning: pygit2 blame failed for {path} at {commit[:7]}: {e}")

        args = ["blame"]
        if follow_moves:
            args.append("-M")
            for _ in range(follow_depth):
                args.append("-C")
        if ignore_whitespace:
            args.append("-w")
        args.extend([f"-L{start},{end}", commit, "--", path])

        out = self._run_subprocess(args, check=False)
        commits: List[str] = []
        for line in out.splitlines():
            if not line:
                continue
            tok = line.split()[0]
            if tok.startswith("^"):
                tok = tok[1:]
            commits.append(tok)
        return commits

    def run_git(self, args: List[str], check: bool = True) -> str:
        """Direct git command execution (fallback for complex operations)."""
        return self._run_subprocess(args, check=check)


def run_git(repo: Path, args: List[str], check: bool = True) -> str:
    """Run a git command in repo and return stdout as text."""
    cmd = ["git", "-C", str(repo)] + args
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def get_parent_commit(repo: Path, commit: str) -> Optional[str]:
    try:
        out = run_git(repo, ["show", "-s", "--format=%P", commit], check=False).strip()
    except Exception:
        return None
    if not out:
        return None
    return out.split()[0]


def get_subject(repo: Path, commit: str) -> str:
    try:
        return run_git(repo, ["show", "-s", "--format=%s", commit], check=False).strip()
    except Exception:
        return ""


def parse_unified_diff(text: str) -> List[FileDiff]:
    """Parse `git show --format= --unified=0 -M -C` output into file diffs + hunks."""
    diffs: List[FileDiff] = []
    cur: Optional[FileDiff] = None
    old_path: Optional[str] = None
    new_path: Optional[str] = None

    diff_git_re = re.compile(r"^diff --git a/(.+) b/(.+)")
    old_path_re = re.compile(r"^--- (a/.+|/dev/null)")
    new_path_re = re.compile(r"^\+\+\+ (b/.+|/dev/null)")
    hunk_re = re.compile(r"^@@ -(?P<os>\d+)(,(?P<oc>\d+))? \+(?P<ns>\d+)(,(?P<nc>\d+))? @@")

    in_hunk = False
    for line in text.splitlines():
        m = diff_git_re.match(line)
        if m:
            if cur is not None:
                diffs.append(cur)
            old_path = m.group(1)
            new_path = m.group(2)
            cur = FileDiff(old_path=old_path, new_path=new_path, hunks=[])
            in_hunk = False
            continue

        if cur is None:
            continue

        if line.startswith("--- "):
            m2 = old_path_re.match(line)
            if m2:
                p = m2.group(1)
                cur.old_path = None if p == "/dev/null" else p[2:]
            in_hunk = False
            continue

        if line.startswith("+++ "):
            m3 = new_path_re.match(line)
            if m3:
                p = m3.group(1)
                cur.new_path = None if p == "/dev/null" else p[2:]
            in_hunk = False
            continue

        m4 = hunk_re.match(line)
        if m4:
            os_ = int(m4.group("os"))
            oc_ = int(m4.group("oc") or 1)
            ns_ = int(m4.group("ns"))
            nc_ = int(m4.group("nc") or 1)
            cur.hunks.append(Hunk(os_, oc_, ns_, nc_))
            in_hunk = True
            continue

        if in_hunk and cur and cur.hunks:
            if line.startswith("+") and not line.startswith("+++"):
                cur.hunks[-1].added_lines.append(line[1:])
                continue
            if line.startswith("-") and not line.startswith("---"):
                cur.hunks[-1].removed_lines.append(line[1:])
                continue

    if cur is not None:
        diffs.append(cur)
    return diffs


# ------------------------ Edge Type Classification ------------------------ #


def interval_distance(range1: Tuple[int, int], range2: Tuple[int, int]) -> int:
    """
    Calculate the minimum distance between two line ranges.

    Returns:
        0 if overlapping or adjacent, otherwise the gap size

    Examples:
        [10, 20] and [25, 30] -> 5
        [10, 20] and [15, 30] -> 0 (overlap)
        [10, 20] and [5, 8] -> 2
    """
    start1, end1 = range1
    start2, end2 = range2

    # Overlapping or adjacent
    if start1 <= end2 and start2 <= end1:
        return 0

    # range2 is after range1
    if start2 > end1:
        return start2 - end1

    # range1 is after range2
    return start1 - end2


def get_commit_hunks(commit: str, file_path: str, git_ops: GitOperations) -> List[Hunk]:
    """
    Extract hunks for a specific file from a commit's diff.

    Args:
        commit: commit SHA
        file_path: file path to extract hunks for
        git_ops: GitOperations instance

    Returns:
        List of Hunk objects for the file
    """
    try:
        diff_text = git_ops.get_diff(commit, unified=3, detect_renames=True)
        file_diffs = parse_unified_diff(diff_text)

        for fd in file_diffs:
            # Match by new_path or old_path
            if fd.new_path == file_path or fd.old_path == file_path:
                return fd.hunks
    except Exception as e:
        print(f"    Warning: Failed to get hunks for {commit[:7]}:{file_path}: {e}")

    return []


def check_rename_dependency(edge: Edge, dst_commit: str, git_ops: GitOperations) -> bool:
    """
    Check if dst_commit renames files involved in the edge.

    Uses git's native rename detection (-M -C flags).
    """
    try:
        # Get diff with rename detection
        diff_text = git_ops.get_diff(dst_commit, unified=0, detect_renames=True)
        file_diffs = parse_unified_diff(diff_text)

        blamed_files = set(edge.metadata.file_ranges.keys())

        for fd in file_diffs:
            # Check for rename/copy operations
            # Git marks these with old_path != new_path and both non-None
            if fd.old_path and fd.new_path and fd.old_path != fd.new_path:
                # If any blamed file is involved in rename
                if fd.old_path in blamed_files or fd.new_path in blamed_files:
                    return True
    except Exception:
        pass

    return False


def check_delete(edge: Edge, dst_commit: str, git_ops: GitOperations) -> bool:
    """
    Check if dst_commit predominantly deletes code from blamed ranges.

    Criteria:
        1. Removed lines > 20
        2. Removed / (Added + 1) > 3.0 (deletion dominant)
        3. >60% of deleted lines overlap with blamed ranges (OLD side)
    """
    for file_path, blamed_ranges in edge.metadata.file_ranges.items():
        dst_hunks = get_commit_hunks(dst_commit, file_path, git_ops)

        if not dst_hunks:
            continue

        removed_lines_total = 0
        removed_in_blamed = 0
        new_lines_total = 0

        for hunk in dst_hunks:
            hunk_old_start = hunk.old_start
            hunk_old_end = hunk.old_start + hunk.old_count - 1

            # Count removed and added lines
            hunk_removed = len(hunk.removed_lines)
            removed_lines_total += hunk_removed
            new_lines_total += len(hunk.added_lines)

            # Calculate overlap with blamed ranges (both in OLD side coordinates)
            for blamed_start, blamed_end in blamed_ranges:
                overlap_start = max(hunk_old_start, blamed_start)
                overlap_end = min(hunk_old_end, blamed_end)

                if overlap_start <= overlap_end and hunk.old_count > 0:
                    # Assume deletions are uniformly distributed in old range
                    overlap_ratio_in_hunk = (overlap_end - overlap_start + 1) / hunk.old_count
                    removed_in_blamed += hunk_removed * overlap_ratio_in_hunk

        # Check criteria
        if removed_lines_total < 20:
            continue

        deletion_ratio = removed_lines_total / (new_lines_total + 1)
        if deletion_ratio < 3.0:
            continue

        overlap_percentage = removed_in_blamed / removed_lines_total if removed_lines_total > 0 else 0
        if overlap_percentage > 0.6:
            return True

    return False


def check_modify_overlap(edge: Edge, dst_commit: str, git_ops: GitOperations) -> bool:
    """
    Check if dst_commit modifies the same code regions as blamed ranges.

    Relaxed criteria (improved):
        1. Has any overlap with blamed ranges OR
        2. Small change (<10 total lines) that touches blamed code
    """
    for file_path, blamed_ranges in edge.metadata.file_ranges.items():
        dst_hunks = get_commit_hunks(dst_commit, file_path, git_ops)

        if not dst_hunks:
            continue

        total_blamed_lines = sum(end - start + 1 for start, end in blamed_ranges)
        overlapped_lines = 0
        new_lines = 0
        removed_lines = 0

        for hunk in dst_hunks:
            # OLD side coordinates
            hunk_old_start = hunk.old_start
            hunk_old_end = hunk.old_start + hunk.old_count - 1

            # Calculate overlap with blamed ranges (both in OLD side)
            for blamed_start, blamed_end in blamed_ranges:
                overlap_start = max(hunk_old_start, blamed_start)
                overlap_end = min(hunk_old_end, blamed_end)
                if overlap_start <= overlap_end:
                    overlapped_lines += overlap_end - overlap_start + 1

            # Count additions and deletions
            new_lines += len(hunk.added_lines)
            removed_lines += len(hunk.removed_lines)

        if total_blamed_lines == 0:
            continue

        total_changes = new_lines + removed_lines

        # Relaxed criteria: accept if has any overlap, or is a small change
        if overlapped_lines > 0:
            # Small changes (<10 lines) always count as modify if there's any overlap
            if total_changes < 10:
                return True

            # Larger changes need reasonable overlap (>20% instead of >50%)
            overlap_ratio = overlapped_lines / total_blamed_lines
            if overlap_ratio > 0.2:
                return True

    return False


def check_extend(edge: Edge, dst_commit: str, git_ops: GitOperations) -> bool:
    """
    Check if dst_commit extends functionality near blamed ranges.

    Criteria:
        1. Added lines > 10
        2. Added / (Removed + 1) > 3.0 (addition dominant)
        3. New code is within proximity (<=10 lines) of blamed ranges

    Note: Uses heuristic distance between OLD and NEW coordinates.
    Assumes local coordinate drift is small for proximity detection.
    """
    for file_path, blamed_ranges in edge.metadata.file_ranges.items():
        dst_hunks = get_commit_hunks(dst_commit, file_path, git_ops)

        if not dst_hunks:
            continue

        new_lines_total = 0
        removed_lines_total = 0
        has_proximity_addition = False

        for hunk in dst_hunks:
            new_lines = len(hunk.added_lines)
            removed_lines = len(hunk.removed_lines)

            new_lines_total += new_lines
            removed_lines_total += removed_lines

            # Only consider hunks with significant additions
            if new_lines < 5:
                continue

            # NEW side coordinates
            hunk_new_start = hunk.new_start
            hunk_new_end = hunk.new_start + hunk.new_count - 1

            # Heuristic: calculate distance between NEW side and OLD side (blamed)
            # Assumes coordinate drift is small in local regions
            for blamed_start, blamed_end in blamed_ranges:
                distance = interval_distance(
                    (blamed_start, blamed_end),
                    (hunk_new_start, hunk_new_end),  # OLD side  # NEW side (heuristic)
                )

                if distance <= 10:  # Proximity threshold
                    has_proximity_addition = True
                    break

            if has_proximity_addition:
                break

        # Check criteria
        if new_lines_total < 10:
            continue

        addition_ratio = new_lines_total / (removed_lines_total + 1)
        if addition_ratio < 3.0:
            continue

        if has_proximity_addition:
            return True

    return False


def is_complete_file_deletion(edge: Edge, dst_commit: str, git_ops: GitOperations) -> bool:
    """
    Check if the dst_commit completely deletes any file involved in the edge.

    Returns:
        True if any file in the edge was completely deleted (old_path exists, new_path is None/dev/null)
    """
    try:
        diff_text = git_ops.get_diff(dst_commit, unified=3, detect_renames=True)
        file_diffs = parse_unified_diff(diff_text)

        for file_path in edge.metadata.file_ranges.keys():
            for fd in file_diffs:
                # Check if this is a complete file deletion
                # old_path exists and new_path is None or /dev/null
                if fd.old_path == file_path and (fd.new_path is None or fd.new_path == "/dev/null"):
                    return True
    except Exception as e:
        print(f"    Warning: Failed to check file deletion for {dst_commit[:7]}: {e}")

    return False


def get_edge_change_ratio(edge: Edge, dst_commit: str, git_ops: GitOperations) -> float:
    """
    Calculate the addition/deletion ratio for an edge.

    Returns:
        Ratio of added lines to deleted lines (added / (deleted + 1))
        Returns infinity if only additions with no deletions.
    """
    total_added = 0
    total_deleted = 0

    for file_path in edge.metadata.file_ranges.keys():
        dst_hunks = get_commit_hunks(dst_commit, file_path, git_ops)
        for hunk in dst_hunks:
            total_added += len(hunk.added_lines)
            total_deleted += len(hunk.removed_lines)

    if total_deleted == 0:
        return float("inf")
    return total_added / total_deleted


def classify_edge_types(edge: Edge, dst_commit: str, git_ops: GitOperations) -> List[str]:
    """
    Classify an edge into semantic types based on diff patterns.

    Types:
        - rename_dependency: File rename/move detected
        - delete: Predominantly deletes blamed code
        - extend: Adds code near blamed regions
        - modify_overlap: Modifies same code regions (relaxed: includes small changes)

    Returns:
        List of type labels (can be multiple)

    Relaxed mutual exclusion rules:
        - Only delete and extend are mutually exclusive (logical conflict)
        - delete can coexist with modify UNLESS it's a complete file deletion
        - extend and modify can coexist UNLESS extend is dominant (ratio > 5.0)
        - rename can coexist with any type
    """
    types = []

    # Step 1: Independent detection
    is_rename = check_rename_dependency(edge, dst_commit, git_ops)
    is_del = check_delete(edge, dst_commit, git_ops)
    is_ext = check_extend(edge, dst_commit, git_ops)
    is_mod = check_modify_overlap(edge, dst_commit, git_ops)

    # Step 1.5: Check special cases
    is_complete_deletion = is_del and is_complete_file_deletion(edge, dst_commit, git_ops)

    # Check if extend is dominant (when both extend and modify are detected)
    is_extend_dominant = False
    if is_ext and is_mod:
        change_ratio = get_edge_change_ratio(edge, dst_commit, git_ops)
        # If additions dominate heavily (ratio > 5.0), it's extend-only
        is_extend_dominant = change_ratio > 5.0

    # Step 2: Apply relaxed mutual exclusion rules
    if is_rename:
        types.append("rename_dependency")

    # Only delete and extend are truly conflicting
    if is_del and is_ext:
        # If both are detected, prefer delete (more specific)
        types.append("delete")
        # For complete file deletions, don't add modify_overlap
        if is_mod and not is_complete_deletion:
            types.append("modify_overlap")
    else:
        # Normal case: add all detected types
        if is_del:
            types.append("delete")
        if is_ext:
            types.append("extend")
        # For complete file deletions or extend-dominant cases, don't add modify_overlap
        if is_mod and not is_complete_deletion and not is_extend_dominant:
            types.append("modify_overlap")

    return types if types else ["unclassified"]


def blame_commits_for_range(
    repo: Path, commit: str, path: str, start: int, end: int, follow_depth: int = 2
) -> List[str]:
    """Return list of commit ids that last touched lines [start,end] in `commit`.

    commit: the revision to blame (i.e., parent commit of the change)
    path: file path at that revision (old side path)
    follow_depth: depth of copy detection (1-3), higher = more thorough but slower (default: 2)
    """
    if start <= 0 or end < start:
        return []

    args = ["blame", "-M"]
    for _ in range(follow_depth):
        args.append("-C")
    args.extend(["-w", f"-L{start},{end}", commit, "--", path])

    out = run_git(repo, args, check=False)
    commits: List[str] = []
    for line in out.splitlines():
        if not line:
            continue
        tok = line.split()[0]
        if tok.startswith("^"):
            tok = tok[1:]
        commits.append(tok)
    return commits


def get_file_line_count(repo: Path, commit: str, path: str) -> int:
    """Get the total line count of a file at a specific commit."""
    try:
        out = run_git(repo, ["show", f"{commit}:{path}"], check=False)
        return len(out.splitlines())
    except Exception:
        return 0


def map_prefix_to_commits(prefixes: List[str], allowed: Set[str]) -> Set[str]:
    """Map short SHAs from blame to full SHAs limited to `allowed` set.

    Handles both cases:
    - full is longer than p: full.startswith(p)
    - p is longer than full: p.startswith(full)
    """
    res: Set[str] = set()
    for p in prefixes:
        for full in allowed:
            if full.startswith(p) or p.startswith(full):
                res.add(full)
    return res


# ------------------------ Patch Verification ------------------------- #


class PatchVerifier:
    """Helper class to verify dependencies using git apply."""

    def __init__(self, repo_path: Path, patch_context_lines: int = 5):
        self.repo_path = repo_path
        self.patch_context_lines = patch_context_lines
        self.tmpdir = Path(tempfile.mkdtemp(prefix="patchverify_"))
        # Clone repo for patch testing using local shared clone (fastest)
        # --shared: share objects with source repo (no copying, only refs and working tree)
        # --local: force local protocol (no network access)
        # This is safe because we only read objects, never write new ones
        rc = subprocess.run(
            ["git", "clone", "--shared", "--local", str(repo_path), str(self.tmpdir / "repo"), "--quiet"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if rc.returncode != 0:
            raise RuntimeError(f"local clone failed for patch verification: {rc.stderr.strip()}")
        self.work = self.tmpdir / "repo"

        # Record initial state
        code, initial_branch, _ = self._git(["rev-parse", "--abbrev-ref", "HEAD"])
        self.initial_branch = initial_branch.strip() if code == 0 else "main"
        code, initial_commit, _ = self._git(["rev-parse", "HEAD"])
        self.initial_commit = initial_commit.strip() if code == 0 else None

    def cleanup(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _git(self, args: List[str]) -> Tuple[int, str, str]:
        p = subprocess.run(["git", "-C", str(self.work), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Use errors="replace" to handle non-UTF-8 content (e.g., binary files in commits)
        return p.returncode, p.stdout.decode("utf-8", errors="replace"), p.stderr.decode("utf-8", errors="replace")

    def _reset_to_initial_state(self):
        """Reset to initial clean state after clone."""
        self._git(["checkout", self.initial_branch])
        self._git(["reset", "--hard", self.initial_commit])  # type: ignore[arg-type]
        self._git(["clean", "-fd"])

    def get_file_blob_hash(self, commit: str, file_path: str) -> Optional[str]:
        """Get the blob hash of a specific file at a given commit."""
        # git ls-tree <commit> <file_path>
        code, out, _err = self._git(["ls-tree", commit, file_path])
        if code != 0 or not out.strip():
            return None

        # Output format: <mode> <type> <hash>\t<path>
        # Example: 100644 blob abc123def456\tpath/to/file.py
        parts = out.strip().split()
        if len(parts) >= 3 and parts[1] == "blob":
            return parts[2]

        return None

    def apply_file_patch(self, commit: str, file_path: str, check_only: bool = False) -> Tuple[bool, Optional[str]]:
        """Extract and apply patch for a specific file from a commit.

        Args:
            commit: The commit to extract patch from
            file_path: The file path to extract patch for
            check_only: If True, only check if patch can be applied without actually applying

        Returns:
            Tuple of (success: bool, error_msg: Optional[str])
        """
        # Extract the patch for the specific file with context lines
        code, patch_content, err = self._git(
            ["show", f"-U{self.patch_context_lines}", commit, "--", file_path]  # Control context lines
        )
        if code != 0:
            return False, f"failed to extract patch from {commit[:7]} for {file_path}"

        if not patch_content.strip():
            # No changes to this file in this commit
            return True, None

        # Apply the patch
        apply_args = ["git", "-C", str(self.work), "apply", "--3way"]
        if check_only:
            apply_args.append("--check")

        proc = subprocess.run(
            apply_args, input=patch_content, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

        if proc.returncode != 0:
            return False, f"apply patch from {commit[:7]} failed: {proc.stderr.strip()[:200]}"

        # IMPORTANT: --3way returns exit code 0 even with conflicts
        # We must check stderr for "with conflicts" to detect them
        if "with conflicts" in proc.stderr.lower():
            return False, f"apply patch from {commit[:7]} has conflicts (3-way merge failed)"

        return True, None

    def verify_dependency(self, src_commit: str, dst_commit: str, file_path: str) -> bool:
        """Verify if dst_commit depends on src_commit using bidirectional patch application.

        This method tests whether src and dst can be applied in any order on src's parent:
        1. Apply src then dst on src's parent
        2. Apply dst then src on src's parent
        3. Compare the resulting file blob hashes

        Returns:
            True if dependency exists (either direction fails OR file hashes differ)
            False if no dependency (both succeed and file hashes match)
        """
        # Get src's parent commit
        code, parent_out, _ = self._git(["rev-parse", f"{src_commit}^"])
        if code != 0:
            print(f"    Warning: {src_commit[:7]} has no parent, cannot verify")
            return False

        parent_commit = parent_out.strip()

        def attempt_sequence(base: str, first: str, second: str) -> Dict[str, Any]:
            """Attempt to apply patches in sequence and get final file hash."""
            res: Dict[str, Any] = {"ok": False, "file_hash": None}

            # Checkout base
            c0, _o0, e0 = self._git(["checkout", "-B", "seqtest", base])
            if c0 != 0:
                res["error"] = f"checkout {base[:7]} failed: {e0.strip()}"
                self._reset_to_initial_state()
                return res

            # Apply first patch
            success1, error1 = self.apply_file_patch(first, file_path, check_only=False)
            if not success1:
                res["error"] = error1
                self._reset_to_initial_state()
                return res

            # Commit first change
            self._git(["add", file_path])
            c1, _o1, _e1 = self._git(["commit", "-m", f"temp: {first[:7]}"])
            if c1 != 0:
                res["error"] = "failed to commit first change"
                self._reset_to_initial_state()
                return res

            # Apply second patch
            success2, error2 = self.apply_file_patch(second, file_path, check_only=False)
            if not success2:
                res["error"] = error2
                self._reset_to_initial_state()
                return res

            # Commit second change
            self._git(["add", file_path])
            c2, _o2, _e2 = self._git(["commit", "-m", f"temp: {second[:7]}"])
            if c2 != 0:
                res["error"] = "failed to commit second change"
                self._reset_to_initial_state()
                return res

            # Get final file hash
            c_final, final_commit, _ = self._git(["rev-parse", "HEAD"])
            if c_final == 0:
                file_hash = self.get_file_blob_hash(final_commit.strip(), file_path)
                res["ok"] = True
                res["file_hash"] = file_hash

            # Reset to initial state
            self._reset_to_initial_state()
            return res

        # Try src → dst on parent
        print(f"      Testing {src_commit[:7]}→{dst_commit[:7]} on parent {parent_commit[:7]}")
        r_src_then_dst = attempt_sequence(parent_commit, src_commit, dst_commit)

        src_ok = r_src_then_dst.get("ok", False)

        # If src→dst fails, dst definitely depends on src
        if not src_ok:
            print("      → src→dst failed, dependency exists")
            return True

        # src→dst succeeded, now try dst→src to check if they're commutative
        print(f"      Testing {dst_commit[:7]}→{src_commit[:7]} on parent {parent_commit[:7]}")
        r_dst_then_src = attempt_sequence(parent_commit, dst_commit, src_commit)

        dst_ok = r_dst_then_src.get("ok", False)

        # Analyze results
        if dst_ok:
            # Both directions succeed, check if file hashes match
            hash_src_dst = r_src_then_dst.get("file_hash")
            hash_dst_src = r_dst_then_src.get("file_hash")

            print(f"      File hashes: src→dst={hash_src_dst}, dst→src={hash_dst_src}")

            if hash_src_dst and hash_dst_src and hash_src_dst == hash_dst_src:
                # File hashes match → no dependency (commutative)
                print("      → Hashes match, no dependency")
                return False
            else:
                # File hashes differ → has dependency (order matters)
                print("      → Hashes differ, dependency exists")
                return True
        else:
            # src→dst succeeds but dst→src fails → dst depends on src
            print("      → Only src→dst succeeds, dependency exists")
            return True


# ------------------------ DAG Construction Logic ------------------------- #


def has_indirect_path(src: str, dst: str, adj: Dict[str, Set[str]], intermediates: Set[str]) -> bool:
    """Check if there's a path from src to dst through intermediate nodes.

    Args:
        src: source commit
        dst: destination commit
        adj: adjacency list of current edges
        intermediates: set of commits that are between src and dst in chronological order

    Returns:
        True if there's a path from src to dst through any intermediate nodes
    """
    from collections import deque

    if src not in adj:
        return False

    # BFS to find path from src to dst that goes through at least one intermediate
    visited = {src}
    queue = deque([(src, False)])  # (node, has_passed_through_intermediate)

    while queue:
        node, passed_intermediate = queue.popleft()

        for neighbor in adj.get(node, set()):
            if neighbor == dst:
                # Found path to dst - valid if we passed through an intermediate
                if passed_intermediate:
                    return True
                continue

            if neighbor not in visited:
                visited.add(neighbor)
                # Mark if this neighbor is an intermediate or we've already passed one
                is_intermediate = neighbor in intermediates
                queue.append((neighbor, passed_intermediate or is_intermediate))

    return False


def build_commit_dag_v3(
    repo: Path,
    commits: List[str],
    context_lines: int = 5,
    patch_context_lines: int = 5,
    verify_with_patch: bool = True,
    include_external: bool = False,
    reduce_transitive: bool = False,
    exclude_test_files: bool = False,
    exclude_ci_files: bool = False,
    exclude_config_files: bool = False,
    exclude_doc_files: bool = False,
    exclude_patterns: Optional[List[str]] = None,
    repo_src_dirs: Optional[List[str]] = None,
    use_pygit2: bool = False,
    ignore_whitespace: bool = False,
    follow_depth: int = 2,
) -> DagResult:
    """Build a commit-level DAG using git blame with context window + git apply verification.

    Args:
        repo: path to a Git repository
        commits: ordered list of commit SHAs (old -> new suggested)
        context_lines: number of context lines to blame around each hunk (default: 5)
        patch_context_lines: number of context lines in patches for git apply verification (default: 5)
        verify_with_patch: whether to verify dependencies using git apply (default: True)
        include_external: whether to keep edges pointing to commits outside `commits`
        reduce_transitive: whether to remove transitive redundant edges
        exclude_test_files: whether to exclude test files from dependency analysis
        exclude_ci_files: whether to exclude CI/CD files from dependency analysis
        exclude_config_files: whether to exclude configuration/build files from dependency analysis
        exclude_doc_files: whether to exclude documentation files from dependency analysis
        exclude_patterns: additional custom glob patterns to exclude
        repo_src_dirs: if specified, only analyze files under these directories (relative to repo root)
        use_pygit2: whether to use pygit2 for git operations (default: False, uses subprocess)
        ignore_whitespace: whether to ignore whitespace changes in git blame (default: False)
        follow_depth: depth of copy detection (1-3), higher = more thorough but slower (default: 2)
    """
    if not shutil.which("git"):
        raise RuntimeError("git command not found in PATH")

    repo = Path(repo)
    commit_list = commits
    commit_set = set(commit_list)
    order_index = {c: i for i, c in enumerate(commit_list)}

    # Initialize git operations backend
    git_ops = GitOperations(repo, use_pygit2=use_pygit2)
    if use_pygit2 and not git_ops.use_pygit2:
        print("Note: pygit2 requested but not available or failed to initialize, using subprocess")

    # Build exclude patterns list
    all_exclude_patterns: List[str] = []
    if exclude_test_files:
        all_exclude_patterns.extend(TEST_FILE_PATTERNS)
    if exclude_ci_files:
        all_exclude_patterns.extend(CI_CD_FILE_PATTERNS)
    if exclude_config_files:
        all_exclude_patterns.extend(CONFIG_FILE_PATTERNS)
    if exclude_doc_files:
        all_exclude_patterns.extend(DOC_FILE_PATTERNS)
    if exclude_patterns:
        all_exclude_patterns.extend(exclude_patterns)

    # Helper function to check if file is in any of the source directories
    def is_in_source_dirs(file_path: str) -> bool:
        """Check if file_path is under any of the specified source directories."""
        if not repo_src_dirs:
            return True  # No filter, include all files
        for src_dir in repo_src_dirs:
            normalized_src_dir = src_dir.rstrip("/") + "/"
            if file_path.startswith(normalized_src_dir):
                return True
        return False

    # Initialize nodes
    nodes: Dict[str, Node] = {c: Node(sha=c, subject=git_ops.get_subject(c)) for c in commit_list}

    edges: List[Edge] = []
    external_deps: Set[str] = set()

    # Track commits that have at least one analyzed file
    commits_with_analyzed_files: Set[str] = set()

    # Initialize patch verifier if needed
    patch_verifier: Optional[PatchVerifier] = (
        PatchVerifier(repo, patch_context_lines=patch_context_lines) if verify_with_patch else None
    )

    # Build adjacency list to track all edges created so far (for transitive pruning)
    from collections import defaultdict

    adj: Dict[str, Set[str]] = defaultdict(set)  # src -> {dst1, dst2, ...}

    # Statistics for optimization
    skipped_verifications = 0
    total_verifications = 0

    # Preprocess: Build file modification history (file_path -> list of commits that modified it)
    # This is used for file-scoped transitive pruning
    print("\n[V3] Preprocessing file modification history...")
    file_commit_history: Dict[str, List[str]] = defaultdict(list)
    # Track files that were CREATED (not just modified) in range, and their creators
    file_creator_map: Dict[str, str] = {}  # file_path -> creator_commit (only for new files)

    for commit in commit_list:
        # Get file status (A=added, M=modified, D=deleted, etc.)
        show_out_pre = git_ops.run_git(["diff-tree", "--no-commit-id", "--name-status", "-r", commit], check=False)
        for line in show_out_pre.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            status, file_path = parts[0].strip(), parts[1].strip()

            # Apply same filters as in main loop
            if all_exclude_patterns and SrcFileFilter.should_exclude_file(file_path, all_exclude_patterns):
                continue
            if not is_in_source_dirs(file_path):
                continue

            file_commit_history[file_path].append(commit)

            # Track file providers (status 'A' means file was added/created, covering new files and rename targets)
            if status.startswith("A") and file_path not in file_creator_map:
                file_creator_map[file_path] = commit

    print(f"[V3] Preprocessed {len(file_commit_history)} files with modification history")

    # Build file rename mapping to track file history across renames
    print("\n[V3] Building file rename mapping...")
    file_rename_map: Dict[str, Set[str]] = defaultdict(set)  # current_path -> all_historical_paths

    for commit in commit_list:
        # Use -M and -C to detect renames and copies
        args = ["show", "--format=", "--name-status", "-M"]
        for _ in range(follow_depth):
            args.append("-C")
        args.append(commit)
        show_out = git_ops.run_git(args, check=False)

        for line in show_out.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                status = parts[0]
                # Detect renames (R) and copies (C): R100, R090, C100, etc.
                if status.startswith("R") or status.startswith("C"):
                    old_path = parts[1]
                    new_path = parts[2]

                    # Apply same filters
                    if all_exclude_patterns:
                        if SrcFileFilter.should_exclude_file(old_path, all_exclude_patterns):
                            continue
                        if SrcFileFilter.should_exclude_file(new_path, all_exclude_patterns):
                            continue
                    if not is_in_source_dirs(old_path) or not is_in_source_dirs(new_path):
                        continue

                    # Record mapping from new path to old path
                    file_rename_map[new_path].add(old_path)
                    # Transitivity: if old path has history, inherit it
                    if old_path in file_rename_map:
                        file_rename_map[new_path].update(file_rename_map[old_path])

    print(
        f"[V3] Found {sum(len(v) for v in file_rename_map.values())} rename relationships for {len(file_rename_map)} files"
    )

    # Helper function to get complete file history including renamed paths
    def get_file_history(path: str) -> List[str]:
        """Get all commits that modified this file or its historical paths."""
        result = list(file_commit_history.get(path, []))

        # Add commits from all historical paths
        for old_path in file_rename_map.get(path, set()):
            result.extend(file_commit_history.get(old_path, []))

        # Remove duplicates while preserving order
        seen = set()
        unique_result = []
        for c in result:
            if c not in seen:
                seen.add(c)
                unique_result.append(c)

        return unique_result

    # Outer loop: Iterate through all commits (in chronological order: old -> new)
    print(
        f"\n[V3] Building commit DAG with context_lines={context_lines}, "
        f"patch_context_lines={patch_context_lines}, verify_with_patch={verify_with_patch}"
    )

    for j_idx, j in enumerate(commit_list):
        parent = git_ops.get_parent_commit(j)
        if not parent:
            continue

        print(f"\n[{j_idx + 1}/{len(commit_list)}] Processing commit {j[:7]}")

        # Get diff for commit j with context lines
        # Using -U{context_lines} so hunk ranges already include context
        show_out = git_ops.get_diff(j, unified=context_lines, detect_renames=True, follow_depth=follow_depth)
        file_diffs = parse_unified_diff(show_out)

        # Track if this commit has any analyzed files
        commit_has_analyzed_files = False

        # Process each file
        for fd in file_diffs:
            old_path = fd.old_path or fd.new_path
            if old_path is None:
                continue

            # Skip files matching exclude patterns
            if all_exclude_patterns and SrcFileFilter.should_exclude_file(old_path, all_exclude_patterns):
                continue

            # Skip files not in the specified source directories
            if not is_in_source_dirs(old_path):
                continue

            # Mark that this commit has analyzed files
            commit_has_analyzed_files = True
            print(f"  File: {old_path}")

            # Execute git blame for all hunks and collect candidate dependencies
            # Group by source commit: src_commit -> line_ranges
            candidate_deps: Dict[str, List[Tuple[int, int]]] = {}

            for hunk in fd.hunks:
                # Blame the entire hunk range (includes modifications + context)
                if hunk.old_count > 0:
                    old_start = hunk.old_start
                    old_end = hunk.old_start + hunk.old_count - 1

                    blame_ids = git_ops.blame_file_range(
                        parent,
                        old_path,
                        old_start,
                        old_end,
                        follow_moves=True,
                        ignore_whitespace=ignore_whitespace,
                        follow_depth=follow_depth,
                    )
                    mapped = map_prefix_to_commits(blame_ids, commit_set)

                    if not mapped and include_external:
                        external_deps.update(blame_ids)

                    # PATH PROVIDER DEPENDENCY (Structural Dependency)
                    # If the file was created/renamed-to in a previous commit within our list,
                    # we have a structural dependency on that provider commit.
                    # This handles cases like: A (Add f) -> B (Rename f->g) -> C (Mod g)
                    # - C depends on B (provider of g)
                    # - B depends on A (provider of f)
                    if old_path in file_creator_map:
                        provider = file_creator_map[old_path]
                        provider_idx = order_index.get(provider, -1)

                        # Only if provider is valid, strictly earlier, and not self
                        if provider != j and provider_idx >= 0 and provider_idx < j_idx:
                            # Add structural edge directly
                            # We use a special meta to indicate this is a file-level dependency
                            provider_meta = EdgeMetadata(file_ranges={old_path: []}, verified=True)
                            provider_edge = Edge.create(src=provider, dst=j, kind="path_provider", meta=provider_meta)
                            provider_edge.types = ["path_provider"]
                            edges.append(provider_edge)
                            adj[provider].add(j)
                            # print(f"    Added structural edge: {provider[:7]} -> {j[:7]} (provider of {old_path})")

                    for i in mapped:
                        if i != j:  # Skip self-loops
                            if i not in candidate_deps:
                                candidate_deps[i] = []
                            candidate_deps[i].append((old_start, old_end))

            print(f"    Found {len(candidate_deps)} candidate dependencies via git blame")

            # Inner loop: Verify each candidate dependency with git apply
            # Sort candidates in reverse chronological order (most recent first)
            # This allows us to skip transitive dependencies early
            sorted_candidates = sorted(candidate_deps.items(), key=lambda x: order_index.get(x[0], -1), reverse=True)

            for i, line_ranges in sorted_candidates:
                i_idx = order_index.get(i, -1)

                # Check for transitive dependency: if there's already a path i -> ... -> j
                # through commits between i and j, we can skip verification
                # File-scoped intermediates: only commits that modified the current file
                # This is more accurate than global intermediates which could include
                # commits that modified other files but not this one
                # Use get_file_history to include commits from renamed file paths
                intermediates = {c for c in get_file_history(old_path) if i_idx < order_index.get(c, -1) < j_idx}

                if intermediates and has_indirect_path(i, j, adj, intermediates):
                    skipped_verifications += 1
                    print(f"    Skipping {i[:7]} -> {j[:7]}: indirect path exists through intermediates (file-scoped)")
                    # Skip verification and don't add any edge (already have indirect path)
                    continue

                # Verify dependency with git apply if enabled
                if verify_with_patch and patch_verifier is not None:
                    total_verifications += 1
                    print(f"    Verifying dependency {i[:7]} -> {j[:7]} on {old_path}")
                    verified = patch_verifier.verify_dependency(i, j, old_path)
                    if verified:
                        print("      ✓ Verified: dependency exists")
                        kind = "blame_verified"
                    else:
                        print("      ✗ Not verified: no dependency (can be parallel)")
                        # No dependency, skip adding edge
                        continue
                else:
                    # No verification, just use blame result
                    kind = "blame_only"
                    verified = False

                # Create edge metadata
                meta = EdgeMetadata(file_ranges={old_path: line_ranges}, verified=verified)

                # Create edge
                new_edge = Edge.create(src=i, dst=j, kind=kind, meta=meta)

                # Classify edge types (new feature)
                try:
                    edge_types = classify_edge_types(new_edge, j, git_ops)
                    new_edge.types = edge_types
                    print(f"    Added edge: {i[:7]} -> {j[:7]} (kind={kind}, types={edge_types})")
                except Exception as e:
                    print(f"    Warning: Failed to classify edge types: {e}")
                    new_edge.types = ["unclassified"]
                    print(f"    Added edge: {i[:7]} -> {j[:7]} (kind={kind}, types=unclassified)")

                edges.append(new_edge)

                # Update adjacency list for transitive pruning
                adj[i].add(j)

        if commit_has_analyzed_files:
            commits_with_analyzed_files.add(j)

    # Filter out commits with no analyzed files
    filtered_commits = commit_set - commits_with_analyzed_files
    edges = [e for e in edges if e.src not in filtered_commits and e.dst not in filtered_commits]

    # Deduplicate edges with same src->dst
    edge_map: Dict[Tuple[str, str], Edge] = {}
    for e in edges:
        key = (e.src, e.dst)
        if key in edge_map:
            # Merge metadata
            existing = edge_map[key]

            # Merge file_ranges: preserve file -> ranges mapping
            for file, ranges in e.metadata.file_ranges.items():
                if file in existing.metadata.file_ranges:
                    # Same file: extend line ranges
                    existing.metadata.file_ranges[file].extend(ranges)
                else:
                    # New file: add to dictionary
                    existing.metadata.file_ranges[file] = ranges

            # Upgrade kind: verified > only. path_provider is structural, kept as is or merged.
            kind_priority = {"blame_verified": 3, "path_provider": 2, "blame_only": 1}
            if kind_priority.get(e.kind, 0) > kind_priority.get(existing.kind, 0):
                existing.kind = e.kind
            if e.metadata.verified:
                existing.metadata.verified = True

            # Merge types: take union and deduplicate
            existing_types_set = set(existing.types)
            new_types_set = set(e.types)
            merged_types = list(existing_types_set | new_types_set)
            existing.types = merged_types
        else:
            edge_map[key] = e
    dedup_edges = list(edge_map.values())

    # Apply transitive reduction if requested
    if reduce_transitive:
        original_count = len(dedup_edges)
        dedup_edges = transitive_reduction(commit_list, dedup_edges)
        print(
            f"[INFO] Transitive reduction: {original_count} edges -> {len(dedup_edges)} edges "
            f"(removed {original_count - len(dedup_edges)} redundant edges)"
        )

    # Update commit_list to exclude filtered commits for topological sorting
    active_commits = [c for c in commit_list if c not in filtered_commits]

    # Compute topological levels
    levels, cycles = topological_levels(active_commits, dedup_edges)

    # Remove filtered commits from nodes
    for commit in filtered_commits:
        nodes.pop(commit, None)

    # Cleanup patch verifier
    if patch_verifier is not None:
        patch_verifier.cleanup()

    print("\n[V3] DAG construction complete:")
    print(f"  - Total nodes: {len(nodes)}")
    print(f"  - Total edges: {len(dedup_edges)}")
    print(f"  - Filtered commits: {len(filtered_commits)}")
    if verify_with_patch:
        total_candidates = total_verifications + skipped_verifications
        print("\n[Optimization Statistics]")
        print(f"  - Total candidate dependencies: {total_candidates}")
        print(f"  - Verified with git apply: {total_verifications}")
        print(f"  - Skipped (transitive): {skipped_verifications}")
        if total_candidates > 0:
            print(f"  - Skip rate: {skipped_verifications / total_candidates * 100:.1f}%")

    return DagResult(nodes=nodes, edges=dedup_edges, levels=levels, external_deps=external_deps, cycles=cycles)


def transitive_reduction(nodes: List[str], edges: List[Edge]) -> List[Edge]:
    """Remove transitive edges from the DAG."""
    from collections import defaultdict, deque

    # Build adjacency list
    adj: Dict[str, List[str]] = defaultdict(list)
    for e in edges:
        if e.dst not in adj[e.src]:
            adj[e.src].append(e.dst)

    # For each edge, check if there's an alternate path
    non_redundant: List[Edge] = []

    for e in edges:
        src, dst = e.src, e.dst

        # BFS to check if there's a path from src to dst without using the direct edge
        has_alternate_path = False
        visited = {src}
        q = deque([src])

        while q and not has_alternate_path:
            u = q.popleft()
            for v in adj[u]:
                # Skip the direct edge we're testing
                if u == src and v == dst:
                    continue

                if v == dst:
                    has_alternate_path = True
                    break

                if v not in visited:
                    visited.add(v)
                    q.append(v)

        if not has_alternate_path:
            non_redundant.append(e)

    return non_redundant


def topological_levels(nodes: List[str], edges: List[Edge]) -> Tuple[Dict[str, int], List[List[str]]]:
    """Compute layering; returns (node->level, cycles)."""
    from collections import defaultdict, deque

    indeg = {n: 0 for n in nodes}
    adj = defaultdict(list)
    for e in edges:
        if e.src not in indeg or e.dst not in indeg:
            continue
        adj[e.src].append(e.dst)
        indeg[e.dst] += 1

    q = deque([n for n, d in indeg.items() if d == 0])
    level = {n: 0 for n in q}

    visited = []
    while q:
        u = q.popleft()
        visited.append(u)
        for v in adj[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                level[v] = level[u] + 1
                q.append(v)

    cycles: List[List[str]] = []
    if len(visited) != len(nodes):
        cyc_nodes = [n for n, d in indeg.items() if d > 0]
        if cyc_nodes:
            cycles.append(cyc_nodes)

    for n in nodes:
        if n not in level:
            level[n] = 0

    return level, cycles


# ------------------------ Helper Functions ------------------------ #


def _read_commits_from_file(file_path: Path) -> List[str]:
    """Read commits from a file (plain text or JSONL)."""
    commits: List[str] = []
    lines = file_path.read_text().splitlines()
    is_jsonl = file_path.suffix.lower() in {".jsonl", ".json"}
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if is_jsonl:
            try:
                obj: Dict[str, Any] = json.loads(line)
                for key in ("sha", "commit", "commit_sha", "id", "oid"):
                    val = obj.get(key)
                    if isinstance(val, str) and val:
                        commits.append(val)
                        break
            except json.JSONDecodeError:
                if line:
                    commits.append(line)
        else:
            commits.append(line)
    return commits


def normalize_to_list(
    values: Optional[List[str]], file_path: Optional[Path] = None, limit: Optional[int] = None
) -> List[str]:
    commits: List[str] = []
    if values:
        commits = list(values)
    elif file_path:
        commits = _read_commits_from_file(file_path)
    if limit is not None and limit >= 0:
        commits = commits[:limit]
    return commits


# ------------------------------- CLI ------------------------------------- #


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build a commit-level dependency DAG using git blame v3")
    p.add_argument("--repo", required=True, help="Path to a Git repo")
    p.add_argument("--commits", nargs="*", default=None, help="Commit SHAs (ordered, e.g., old->new)")
    p.add_argument("--commit-file", default=None, help="File with one commit SHA per line (ordered)")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of commits taken from --commit-file or --commits",
    )
    p.add_argument(
        "--context-lines",
        type=int,
        default=5,
        help="Number of context lines to blame around each hunk (default: 5)",
    )
    p.add_argument(
        "--patch-context-lines",
        type=int,
        default=5,
        help="Number of context lines in patches for git apply verification (default: 5)",
    )
    p.add_argument(
        "--verify-with-patch",
        action="store_true",
        default=True,
        help="Verify dependencies using git apply (default: True)",
    )
    p.add_argument(
        "--no-verify-with-patch",
        action="store_false",
        dest="verify_with_patch",
        help="Disable git apply verification",
    )
    p.add_argument("--include-external", action="store_true", help="Include external edges' sources in report")
    p.add_argument(
        "--reduce-transitive",
        action="store_true",
        help="Remove transitive redundant edges",
    )
    p.add_argument(
        "--exclude-test-files",
        action="store_true",
        help="Exclude test files from dependency analysis",
    )
    p.add_argument(
        "--exclude-ci-files",
        action="store_true",
        help="Exclude CI/CD workflow files from dependency analysis",
    )
    p.add_argument(
        "--exclude-config-files",
        action="store_true",
        help="Exclude configuration and build files from dependency analysis",
    )
    p.add_argument(
        "--exclude-doc-files",
        action="store_true",
        help="Exclude documentation files from dependency analysis",
    )
    p.add_argument(
        "--exclude-patterns",
        nargs="*",
        default=None,
        help="Additional glob patterns to exclude",
    )
    p.add_argument(
        "--repo-src-dirs",
        nargs="*",
        default=None,
        help="Only analyze files under these directories (relative to repo root). Multiple directories can be specified.",
    )
    p.add_argument(
        "--use-pygit2",
        action="store_true",
        help="Use pygit2 library for git operations instead of subprocess (faster, requires pygit2 installed)",
    )
    p.add_argument(
        "--ignore-whitespace",
        action="store_true",
        help="Ignore whitespace changes in git blame (default: False, whitespace changes are tracked)",
    )
    p.add_argument(
        "--follow-depth",
        type=int,
        default=2,
        choices=[1, 2, 3],
        help="Depth of copy/move detection (1-3, default: 2). Higher = more accurate but slower. "
        "1: same commit modified files, 2: same commit all files (recommended), 3: cross-commit history",
    )
    p.add_argument("--out", default=None, help="Path to write JSON output (overrides --outdir)")
    p.add_argument("--dot", default=None, help="Path to write Graphviz DOT output (overrides --outdir)")
    p.add_argument("--outdir", default=None, help="Output directory for JSON and DOT files")
    p.add_argument("--print-levels", action="store_true", help="Print parallel layers to stdout")
    return p.parse_args(argv)


def to_json(dr: DagResult) -> Dict:
    def serialize_edge(e: Edge) -> Dict:
        return {
            "src": e.src,
            "dst": e.dst,
            "kind": e.kind,
            "types": e.types,  # New: semantic edge types
            "metadata": {
                "file_ranges": {file: [list(r) for r in ranges] for file, ranges in e.metadata.file_ranges.items()},
                "verified": e.metadata.verified,
            },
        }

    return {
        "nodes": {k: {"sha": v.sha, "subject": v.subject} for k, v in dr.nodes.items()},
        "edges": [serialize_edge(e) for e in dr.edges],
        "levels": dr.levels,
        "external_deps": sorted(list(dr.external_deps)),
        "cycles": dr.cycles,
    }


def compute_topological_order(nodes: Dict[str, Node], edges: List[Edge]) -> List[str]:
    """Compute topological order using Kahn's algorithm."""
    # Build adjacency list and in-degree table
    adj = {node: [] for node in nodes}
    in_degree = {node: 0 for node in nodes}

    for edge in edges:
        adj[edge.src].append(edge.dst)
        in_degree[edge.dst] += 1

    # Kahn's algorithm
    queue = [node for node in nodes if in_degree[node] == 0]
    result = []

    while queue:
        # Sort to ensure consistent output
        queue.sort()
        node = queue.pop(0)
        result.append(node)

        for neighbor in adj[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    return result


def count_ancestors(node: str, adj_in: Dict[str, Set[str]]) -> int:
    """DFS to count all ancestors (predecessors)."""
    visited = set()

    def dfs(n: str):
        if n in visited:
            return
        visited.add(n)
        for pred in adj_in.get(n, set()):
            dfs(pred)

    for pred in adj_in.get(node, set()):
        dfs(pred)

    return len(visited)


def count_descendants(node: str, adj_out: Dict[str, Set[str]]) -> int:
    """DFS to count all descendants (successors)."""
    visited = set()

    def dfs(n: str):
        if n in visited:
            return
        visited.add(n)
        for succ in adj_out.get(n, set()):
            dfs(succ)

    for succ in adj_out.get(node, set()):
        dfs(succ)

    return len(visited)


def to_enhanced_json(dr: DagResult, context_lines: int = 3, repo_src_dirs: Optional[List[str]] = None) -> Dict:
    """
    Generate enhanced JSON format with topological order, degree info, and ancestor/descendant counts.

    Args:
        dr: DagResult from build_commit_dag_v3
        context_lines: Number of context lines for textual dependency detection
        repo_src_dirs: Source directory filters (if any)

    New format:
    {
      "meta": {...},
      "nodes": {...},  # with in_degree, out_degree, ancestors_count, descendants_count
      "edges_dag": [...],
      "layers": {...},
      "topo_order": [...]
    }
    """
    # 1. Build adjacency lists
    adj_out = {}  # node -> set of successors
    adj_in = {}  # node -> set of predecessors

    for node in dr.nodes:
        adj_out[node] = set()
        adj_in[node] = set()

    for edge in dr.edges:
        adj_out[edge.src].add(edge.dst)
        adj_in[edge.dst].add(edge.src)

    # 2. Compute topological order
    topo_order = compute_topological_order(dr.nodes, dr.edges)

    # 3. Compute ancestors and descendants counts
    ancestors_count = {}
    descendants_count = {}

    for node in dr.nodes:
        ancestors_count[node] = count_ancestors(node, adj_in)
        descendants_count[node] = count_descendants(node, adj_out)

    # 4. Build nodes data (with degree and ancestor/descendant info)
    nodes_data = {}
    for sha, node in dr.nodes.items():
        short_sha = sha[:7]
        nodes_data[short_sha] = {
            "subject": node.subject,
            "layer": dr.levels.get(sha, -1),
            "in_degree": len(adj_in[sha]),
            "out_degree": len(adj_out[sha]),
            "ancestors_count": ancestors_count[sha],
            "descendants_count": descendants_count[sha],
        }

    # 5. Build edges_dag (only essential fields, use short SHA)
    edges_data = []
    for edge in dr.edges:
        edges_data.append(
            {
                "source": edge.src[:7],
                "target": edge.dst[:7],
                "files_ref": sorted(edge.metadata.file_ranges.keys()),  # Extract file list
                "edge_types": edge.types,  # New: semantic edge types
            }
        )

    # 6. Build layers (group by level, store only IDs)
    layers_data = {}
    for sha, level in dr.levels.items():
        level_key = str(level)
        if level_key not in layers_data:
            layers_data[level_key] = []
        layers_data[level_key].append(sha[:7])

    # Sort nodes in each layer for consistency
    for level_key in layers_data:
        layers_data[level_key].sort()

    # 7. Build meta information
    # Format analysis_scope as array of directories
    if repo_src_dirs:
        # Ensure trailing slash for clarity
        scope_dirs = [d if d.endswith("/") else f"{d}/" for d in repo_src_dirs]
    else:
        scope_dirs = None

    meta = {
        "layers_count": len(set(dr.levels.values())) if dr.levels else 0,
        "nodes_count": len(dr.nodes),
        "edges_count": len(dr.edges),
        "constraints": {"acyclic": len(dr.cycles) == 0, "layer_increasing": True, "directed": True},
        "dag_info": {
            "description": f"Textual commit dependency DAG where 'source -> target' means target depends on source. Execute commits following topo_order or layer by layer (0 to {len(set(dr.levels.values())) - 1}).",
            "detection_fuzz": context_lines,
            "detection_note": f"Dependencies detected with {context_lines}-line context tolerance. Larger fuzz = more lenient (may include false dependencies), smaller = stricter (may miss weak dependencies).",
            "analysis_scope": scope_dirs,
        },
    }

    # 8. Return enhanced format
    return {
        "meta": meta,
        "nodes": nodes_data,
        "edges_dag": edges_data,
        "layers": layers_data,
        "topo_order": [sha[:7] for sha in topo_order],
    }


def to_dot(dr: DagResult) -> str:
    lines = ["digraph commits {", "  rankdir=LR;"]
    # group by level
    lvl2nodes: Dict[int, List[str]] = {}
    for n, lvl in dr.levels.items():
        lvl2nodes.setdefault(lvl, []).append(n)
    for lvl, ns in sorted(lvl2nodes.items()):
        lines.append(f'  subgraph cluster_level_{lvl} {{ label="level {lvl}"; color=lightgrey;')
        for n in ns:
            subj = dr.nodes[n].subject.replace('"', "'") if n in dr.nodes else ""
            lines.append(f'    "{n[:7]}" [label="{n[:7]}: {subj}"]; ')
        lines.append("  }")

    for e in dr.edges:
        lines.append(f'  "{e.src[:7]}" -> "{e.dst[:7]}"; ')
    lines.append("}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> None:
    args = _parse_args(argv)
    repo = Path(args.repo)
    if not (repo / ".git").exists() and not (repo.is_dir() and (repo / "HEAD").exists()):
        raise SystemExit(f"--repo {repo} does not look like a Git repository")

    commits = normalize_to_list(args.commits, Path(args.commit_file) if args.commit_file else None, limit=args.limit)
    if not commits:
        raise SystemExit("Provide --commits or --commit-file")

    dr = build_commit_dag_v3(
        repo=repo,
        commits=commits,
        context_lines=args.context_lines,
        patch_context_lines=args.patch_context_lines,
        verify_with_patch=args.verify_with_patch,
        include_external=args.include_external,
        reduce_transitive=args.reduce_transitive,
        exclude_test_files=args.exclude_test_files,
        exclude_ci_files=args.exclude_ci_files,
        exclude_config_files=args.exclude_config_files,
        exclude_doc_files=args.exclude_doc_files,
        exclude_patterns=args.exclude_patterns,
        repo_src_dirs=args.repo_src_dirs,
        use_pygit2=args.use_pygit2,
        ignore_whitespace=args.ignore_whitespace,
        follow_depth=args.follow_depth,
    )

    # Determine output file paths
    json_out = args.out
    dot_out = args.dot

    if args.outdir:
        repo_name = repo.name
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]

        outdir = Path(args.outdir) / repo_name
        outdir.mkdir(parents=True, exist_ok=True)

        if args.limit:
            basename = f"commit_dag_v3_limit_{args.limit}_ctx_{args.context_lines}_pctx_{args.patch_context_lines}"
        else:
            basename = f"commit_dag_v3_ctx_{args.context_lines}_pctx_{args.patch_context_lines}"

        if not json_out:
            json_out = str(outdir / f"{basename}.json")
        if not dot_out:
            dot_out = str(outdir / f"{basename}.dot")

    # Write JSON output (original format)
    data_v3 = to_json(dr)
    if json_out:
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(json_out).write_text(json.dumps(data_v3, indent=2))
        print(f"Wrote original JSON to {json_out}")

        # Write enhanced JSON format
        enhanced_out = json_out.replace(".json", "_enhanced.json")
        data_enhanced = to_enhanced_json(dr, context_lines=args.context_lines, repo_src_dirs=args.repo_src_dirs)
        Path(enhanced_out).write_text(json.dumps(data_enhanced, indent=2))
        print(f"Wrote enhanced JSON to {enhanced_out}")
    else:
        print(json.dumps(data_v3, indent=2))

    # Write DOT output
    if dot_out:
        Path(dot_out).parent.mkdir(parents=True, exist_ok=True)
        Path(dot_out).write_text(to_dot(dr))
        print(f"Wrote DOT to {dot_out}")

    if args.print_levels:
        lvl2nodes: Dict[int, List[str]] = {}
        for n, lvl in dr.levels.items():
            lvl2nodes.setdefault(lvl, []).append(n)
        for lvl in sorted(lvl2nodes.keys()):
            row = " ".join(sorted([n[:7] for n in lvl2nodes[lvl]]))
            print(f"level {lvl}: {row}")


if __name__ == "__main__":
    main()
