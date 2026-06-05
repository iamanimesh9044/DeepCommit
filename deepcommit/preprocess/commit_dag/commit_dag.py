from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from deepcommit.harness.utils.src_filter import SrcFileFilter

# Optional pygit2 support
try:
    import pygit2

    HAS_PYGIT2 = True
except ImportError:
    HAS_PYGIT2 = False
    pygit2 = None  # type: ignore


# ---------------------------- Data Structures ---------------------------- #


class PerformanceProfiler:
    """Performance profiling utility to track operation timings."""

    def __init__(self):
        self.timings: Dict[str, List[float]] = defaultdict(list)
        self.counters: Dict[str, int] = defaultdict(int)
        self.start_times: Dict[str, float] = {}

    def start(self, operation: str) -> None:
        """Start timing an operation."""
        self.start_times[operation] = time.time()

    def stop(self, operation: str) -> None:
        """Stop timing an operation and record the duration."""
        if operation in self.start_times:
            duration = time.time() - self.start_times[operation]
            self.timings[operation].append(duration)
            del self.start_times[operation]

    def count(self, counter: str, value: int = 1) -> None:
        """Increment a counter."""
        self.counters[counter] += value

    def get_stats(self, operation: str) -> Dict[str, float]:
        """Get statistics for an operation."""
        times = self.timings.get(operation, [])
        if not times:
            return {"count": 0, "total": 0, "avg": 0, "min": 0, "max": 0}

        return {
            "count": len(times),
            "total": sum(times),
            "avg": sum(times) / len(times),
            "min": min(times),
            "max": max(times),
        }

    def print_report(self) -> None:
        """Print a detailed performance report."""
        print("\n" + "=" * 80)
        print("PERFORMANCE PROFILING REPORT")
        print("=" * 80)

        # Group operations by category
        categories = {
            "Git Operations": ["get_subject", "get_parent", "get_diff", "blame_file_range", "read_file_at_commit"],
            "Patch Testing": [
                "patch_test_pair",
                "patch_test_clone",
                "patch_test_cherry_pick",
                "patch_apply_file",
                "patch_get_subtree",
            ],
            "DAG Construction": ["parse_diff", "detect_container", "build_edges", "transitive_reduction"],
        }

        for category, operations in categories.items():
            category_total = 0
            category_count = 0
            has_data = False

            print(f"\n{category}:")
            print("-" * 80)

            for op in operations:
                stats = self.get_stats(op)
                if stats["count"] > 0:
                    has_data = True
                    category_total += stats["total"]
                    category_count += stats["count"]
                    print(
                        f"  {op:30s}: {stats['count']:6d} calls, "
                        f"{stats['total']:8.2f}s total, "
                        f"{stats['avg'] * 1000:7.2f}ms avg, "
                        f"{stats['min'] * 1000:7.2f}ms min, "
                        f"{stats['max'] * 1000:7.2f}ms max"
                    )

            if has_data:
                print(f"  {'SUBTOTAL':30s}: {category_count:6d} calls, {category_total:8.2f}s total")

        # Print counters
        if self.counters:
            print("\nCounters:")
            print("-" * 80)
            for counter, value in sorted(self.counters.items()):
                print(f"  {counter:50s}: {value:10d}")

        # Print total time
        all_times = [t for times in self.timings.values() for t in times]
        if all_times:
            total_time = sum(all_times)
            print(f"\n{'Total measured time':30s}: {total_time:8.2f}s")

        print("=" * 80 + "\n")


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

    file: str  # File path involved in the dependency
    line_range: Optional[Tuple[int, int]] = None  # For 'line' type: (start, end)
    # TODO: Add class_name and class_line attributes for 'container' type dependencies
    class_name: Optional[str] = None  # For 'container' type: class name
    class_line: Optional[int] = None  # For 'container' type: class definition line number


@dataclass
class Edge:
    src: str
    dst: str
    kind: List[str]  # List of: 'line' | 'container' | 'file_direct' | 'file_indirect'
    metadata: Dict[str, List[EdgeMetadata]] = field(default_factory=dict)  # kind -> [metadata]

    @staticmethod
    def create(src: str, dst: str, kind: str, meta: Optional[EdgeMetadata] = None) -> "Edge":
        """Create an edge with a single kind and optional metadata."""
        metadata = {kind: [meta]} if meta else {}
        return Edge(src=src, dst=dst, kind=[kind], metadata=metadata)

    def add_kind(self, kind: str, meta: Optional[EdgeMetadata] = None) -> None:
        """Add a new kind with optional metadata."""
        if kind not in self.kind:
            self.kind.append(kind)
        if meta:
            self.metadata.setdefault(kind, []).append(meta)

    def get_files(self, kind: Optional[str] = None) -> Set[str]:
        """Get all files involved in this edge, optionally filtered by kind."""
        if kind:
            return {m.file for m in self.metadata.get(kind, [])}
        return {m.file for mlist in self.metadata.values() for m in mlist}

    def get_line_ranges(self, file: str) -> List[Tuple[int, int]]:
        """Get all line ranges for a specific file in 'line' type dependencies."""
        ranges = []
        for m in self.metadata.get("line", []):
            if m.file == file and m.line_range:
                ranges.append(m.line_range)
        return ranges


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
    # README files
    "README.md",
    "README.rst",
    "README.txt",
    "README",
    "readme.md",
    "Readme.md",
    # Documentation directories
    "**/docs/**/*",
    "**/doc/**/*",
    "**/documentation/**/*",
    "**/_docs/**/*",
    "**/site/**/*",
    "**/_site/**/*",
    "**/public/docs/**/*",
    # Markdown and other doc formats
    "**/*.md",
    "**/*.rst",
    "**/*.adoc",
    "**/*.rdoc",
    "**/*.txt",
    # Documentation tool configs
    "mkdocs.yml",
    "mkdocs.yaml",
    "docusaurus.config.js",
    "docusaurus.config.ts",
    "**/conf.py",  # Sphinx
    "**/.readthedocs.yml",
    "**/.readthedocs.yaml",
    # License and contribution files
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

    def __init__(self, repo_path: Path, use_pygit2: bool = False, profiler: Optional[PerformanceProfiler] = None):
        self.repo_path = Path(repo_path)
        self.use_pygit2 = use_pygit2 and HAS_PYGIT2
        self.profiler = profiler

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

    def _timed_call(self, operation: str, func, *args, **kwargs):
        """Helper to time a function call."""
        if self.profiler:
            self.profiler.start(operation)
        try:
            result = func(*args, **kwargs)
            return result
        finally:
            if self.profiler:
                self.profiler.stop(operation)

    def _run_subprocess(self, args: List[str], check: bool = True) -> str:
        """Run git command via subprocess."""
        cmd = ["git", "-C", str(self.repo_path)] + args
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def get_parent_commit(self, commit: str) -> Optional[str]:
        """Get the first parent of a commit."""
        if self.use_pygit2 and self.pygit2_repo:
            try:
                c = self.pygit2_repo.revparse_single(commit)
                if hasattr(c, "parent_ids") and c.parent_ids:
                    return str(c.parent_ids[0])
                return None
            except Exception:
                # Fallback to subprocess
                pass

        # Subprocess implementation
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
                    return c.message.split("\n")[0].strip()
            except Exception:
                pass

        # Subprocess implementation
        try:
            return self._run_subprocess(["show", "-s", "--format=%s", commit], check=False).strip()
        except Exception:
            return ""

    def get_diff(self, commit: str, unified: int = 0, detect_renames: bool = True) -> str:
        """Get the diff for a commit.

        Note: Currently uses subprocess even when pygit2 is enabled, as git's
        internal diff implementation is often faster than pygit2's patch generation.
        """
        if self.use_pygit2 and self.use_pygit2_for_diff and self.pygit2_repo:
            try:
                c = self.pygit2_repo.revparse_single(commit)
                if c.parent_ids:
                    parent = self.pygit2_repo.get(c.parent_ids[0])
                    diff = self.pygit2_repo.diff(parent.tree, c.tree, context_lines=unified)
                else:
                    # Initial commit: diff against empty tree
                    diff = c.tree.diff_to_tree(swap=True, context_lines=unified)

                if detect_renames:
                    diff.find_similar(flags=pygit2.GIT_DIFF_FIND_RENAMES | pygit2.GIT_DIFF_FIND_COPIES)

                return diff.patch or ""
            except Exception as e:
                print(f"Warning: pygit2 diff failed for {commit[:7]}: {e}")
                # Fallback to subprocess

        # Subprocess implementation (default, faster for diff operations)
        args = ["show", "--format=", f"--unified={unified}"]
        if detect_renames:
            args.extend(["-M", "-C"])
        args.append(commit)
        return self._run_subprocess(args, check=False)

    def blame_file_range(
        self, commit: str, path: str, start: int, end: int, follow_moves: bool = True, ignore_whitespace: bool = True
    ) -> List[str]:
        """Blame a specific line range in a file at a commit."""
        if start <= 0 or end < start:
            return []

        if self.use_pygit2 and self.pygit2_repo:
            try:
                # pygit2 blame API
                flags = 0
                if follow_moves:
                    flags |= pygit2.GIT_BLAME_TRACK_COPIES_SAME_COMMIT_MOVES
                    flags |= pygit2.GIT_BLAME_TRACK_COPIES_SAME_COMMIT_COPIES
                if ignore_whitespace:
                    flags |= pygit2.GIT_BLAME_IGNORE_WHITESPACE

                blame = self.pygit2_repo.blame(path, newest_commit=commit, min_line=start, max_line=end, flags=flags)

                # Optimize: directly map line numbers to commits without expanding all lines
                commits = []
                current_line = start  # noqa: F841

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
                # Fallback to subprocess

        # Subprocess implementation
        args = ["blame"]
        if follow_moves:
            args.extend(["-M", "-C"])
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

    def read_file_at_commit(self, commit: str, path: str) -> Optional[List[str]]:
        """Read file contents at a specific commit."""
        if self.use_pygit2 and self.pygit2_repo:
            try:
                c = self.pygit2_repo.revparse_single(commit)
                tree = c.tree
                entry = tree / path
                if entry and entry.type_str == "blob":
                    blob = self.pygit2_repo.get(entry.id)
                    content = blob.data.decode("utf-8", errors="replace")
                    return content.splitlines()
            except Exception as e:
                print(f"Warning: pygit2 read file failed for {path} at {commit[:7]}: {e}")
                # Fallback to subprocess

        # Subprocess implementation
        out = self._run_subprocess(["show", f"{commit}:{path}"], check=False)
        if out == "":
            return None
        return out.splitlines()

    def find_file_provider_commit(self, path: str) -> Optional[str]:
        """Find commit that added the file (across renames)."""
        # This is complex to implement in pygit2, use subprocess
        out = self._run_subprocess(
            [
                "log",
                "--follow",
                "--diff-filter=A",
                "-1",
                "--format=%H",
                "--",
                path,
            ],
            check=False,
        ).strip()
        return out or None

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
    # First parent only (prototype)
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

    # Patterns
    diff_git_re = re.compile(r"^diff --git a/(.+) b/(.+)")
    old_path_re = re.compile(r"^--- (a/.+|/dev/null)")
    new_path_re = re.compile(r"^\+\+\+ (b/.+|/dev/null)")
    hunk_re = re.compile(r"^@@ -(?P<os>\d+)(,(?P<oc>\d+))? \+(?P<ns>\d+)(,(?P<nc>\d+))? @@")

    in_hunk = False
    for line in text.splitlines():
        m = diff_git_re.match(line)
        if m:
            # start new file block
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
            # Record changed lines (unified=0, so only +/- lines appear)
            if line.startswith("+") and not line.startswith("+++"):
                cur.hunks[-1].added_lines.append(line[1:])
                continue
            if line.startswith("-") and not line.startswith("---"):
                cur.hunks[-1].removed_lines.append(line[1:])
                continue
            # Any other line ends the hunk in U=0 context
            # but keep it simple: do nothing

    if cur is not None:
        diffs.append(cur)
    return diffs


def blame_commits_for_range(repo: Path, commit: str, path: str, start: int, end: int) -> List[str]:
    """Return list of commit ids that last touched lines [start,end] in `commit`.

    commit: the revision to blame (i.e., parent commit of the change)
    path: file path at that revision (old side path)
    """
    if start <= 0 or end < start:
        return []
    out = run_git(
        repo,
        [
            "blame",
            "-M",
            "-C",
            "-w",
            f"-L{start},{end}",
            commit,
            "--",
            path,
        ],
        check=False,
    )
    commits: List[str] = []
    for line in out.splitlines():
        if not line:
            continue
        # First token is commit (may start with ^ for boundary)
        tok = line.split()[0]
        if tok.startswith("^"):
            tok = tok[1:]
        commits.append(tok)
    return commits


def find_file_provider_commit(repo: Path, path: str) -> Optional[str]:
    """Find commit that added the file (across renames)."""
    out = run_git(
        repo,
        [
            "log",
            "--follow",
            "--diff-filter=A",
            "-1",
            "--format=%H",
            "--",
            path,
        ],
        check=False,
    ).strip()
    return out or None


def read_file_at_rev(repo: Path, rev: str, path: str) -> Optional[List[str]]:
    out = run_git(repo, ["show", f"{rev}:{path}"], check=False)
    if out == "":
        return None
    return out.splitlines()


def detect_python_container_class(lines: List[str], insertion_after_line: int) -> Optional[Tuple[int, str]]:
    """Heuristically find the class declaration above the insertion point.

    Returns (line_number, class_name) if found. `insertion_after_line` is 1-based
    line number in the OLD file where new lines will be inserted after.
    """
    # If insertion is at top (0), there is no container above.
    if insertion_after_line <= 0:
        return None
    i = min(len(lines), insertion_after_line)
    class_re = re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b")
    for ln in range(i, 0, -1):
        m = class_re.match(lines[ln - 1])
        if m:
            return ln, m.group(1)
    return None


def is_python_file(path: str) -> bool:
    return path.endswith(".py")


def _read_commits_from_file(file_path: Path) -> List[str]:
    """Read commits from a file.

    Supports:
    - Plain text: one SHA per non-empty line
    - JSONL: one JSON object per line with a 'sha' (or common variants) field
    """
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
                # common keys in our datasets
                for key in ("sha", "commit", "commit_sha", "id", "oid"):
                    val = obj.get(key)
                    if isinstance(val, str) and val:
                        commits.append(val)
                        break
                else:
                    # fallback: ignore line if no suitable key
                    continue
            except json.JSONDecodeError:
                # if a line isn't JSON, fall back to treating it as plain SHA
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


def unique(iterable: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for x in iterable:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def map_prefix_to_commits(prefixes: List[str], allowed: Set[str]) -> Set[str]:
    """Map short SHAs from blame to full SHAs limited to `allowed` set.

    If a prefix matches multiple allowed commits, keep all that match.
    """
    res: Set[str] = set()
    for p in prefixes:
        for full in allowed:
            if full.startswith(p):
                res.add(full)
    return res


def should_exclude_file(path: str, exclude_patterns: List[str]) -> bool:
    """Check if a file path matches any of the exclude patterns.

    Delegates to the single canonical glob engine
    ``SrcFileFilter.should_exclude_file`` (pathspec/gitwildmatch). The
    hand-rolled ``_glob_match`` engine that used to live here was removed during
    the classification unification.
    """
    return SrcFileFilter.should_exclude_file(path, exclude_patterns)


# ------------------------ DAG Construction Logic ------------------------- #


def build_commit_dag(
    repo: Path,
    commits: List[str],
    include_external: bool = False,
    include_patch_file_deps: bool = False,
    reduce_transitive: bool = False,
    exclude_test_files: bool = False,
    exclude_ci_files: bool = False,
    exclude_config_files: bool = False,
    exclude_doc_files: bool = False,
    exclude_patterns: Optional[List[str]] = None,
    repo_src_dir: Optional[str] = None,
    include_line_edges: bool = True,
    include_container_edges: bool = True,
    use_pygit2: bool = False,
) -> DagResult:
    """Build a commit-level DAG from the given commit list.

    - repo: path to a Git repository
    - commits: ordered list of commit SHAs (old -> new suggested)
    - include_external: whether to keep edges pointing to commits outside `commits`
    - include_patch_file_deps: whether to enable patch-based file dependency detection via bidirectional cherry-picks
    - reduce_transitive: whether to remove transitive redundant edges
    - exclude_test_files: whether to exclude test files from dependency analysis
    - exclude_ci_files: whether to exclude CI/CD files from dependency analysis
    - exclude_config_files: whether to exclude configuration/build files from dependency analysis
    - exclude_doc_files: whether to exclude documentation files from dependency analysis
    - exclude_patterns: additional custom glob patterns to exclude
    - repo_src_dir: if specified, only analyze files under this directory (relative to repo root)
    - include_line_edges: whether to include line-level dependency edges (default: True)
    - include_container_edges: whether to include container-level dependency edges (default: True)
    - use_pygit2: whether to use pygit2 for git operations (default: False, uses subprocess)
    """
    if not shutil.which("git"):
        raise RuntimeError("git command not found in PATH")

    repo = Path(repo)
    commit_list = commits
    commit_set = set(commit_list)
    order_index = {c: i for i, c in enumerate(commit_list)}

    # Initialize performance profiler
    profiler = PerformanceProfiler()
    overall_start = time.time()

    # Initialize git operations backend
    profiler.start("init_git_ops")
    git_ops = GitOperations(repo, use_pygit2=use_pygit2, profiler=profiler)
    if use_pygit2 and not git_ops.use_pygit2:
        print("Note: pygit2 requested but not available or failed to initialize, using subprocess")
    profiler.stop("init_git_ops")

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

    # nodes
    profiler.start("init_nodes")
    nodes: Dict[str, Node] = {}
    for c in commit_list:
        profiler.start("get_subject")
        subject = git_ops.get_subject(c)
        profiler.stop("get_subject")
        nodes[c] = Node(sha=c, subject=subject)
    profiler.stop("init_nodes")
    profiler.count("total_commits", len(commit_list))

    edges: List[Edge] = []
    external_deps: Set[str] = set()

    # Track commits that have at least one analyzed file
    commits_with_analyzed_files: Set[str] = set()

    # Optional helpers for patch-based dependency checks
    changed_files_cache: Dict[str, Set[str]] = {}

    class _PatchTester:
        def __init__(self, repo_path: Path):
            self.repo_path = repo_path
            self.tmpdir = Path(tempfile.mkdtemp(prefix="patchdep_"))
            # local working clone to allow cherry-pick operations
            rc = subprocess.run(
                ["git", "clone", str(repo_path), str(self.tmpdir / "repo"), "--no-tags", "--quiet"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if rc.returncode != 0:
                raise RuntimeError(f"clone failed for patch-dep checks: {rc.stderr.strip()}")
            self.work = self.tmpdir / "repo"

            # Record initial state (the default branch after clone)
            code, initial_branch, _ = self._git(["rev-parse", "--abbrev-ref", "HEAD"])
            self.initial_branch = initial_branch.strip() if code == 0 else "main"
            code, initial_commit, _ = self._git(["rev-parse", "HEAD"])
            self.initial_commit = initial_commit.strip() if code == 0 else None

        def cleanup(self):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

        def _git(self, args: List[str]) -> Tuple[int, str, str]:
            p = subprocess.run(
                ["git", "-C", str(self.work), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            return p.returncode, p.stdout, p.stderr

        def _reset_to_initial_state(self):
            """Reset to initial clean state after clone.

            This ensures that all operations start from a known, clean state
            and properly cleanup any temporary branches or uncommitted changes.

            The "initial state" is the state immediately after cloning the repo,
            i.e., the commit that the default branch (usually main/master) pointed
            to when the clone completed.
            """
            # Checkout initial branch
            self._git(["checkout", self.initial_branch])
            # Reset to the recorded initial commit (not HEAD, which may have moved!)
            self._git(["reset", "--hard", self.initial_commit])
            # Clean untracked files and directories
            self._git(["clean", "-fd"])

        def apply_file_patch(self, commit: str, file_path: str, check_only: bool = False) -> Tuple[bool, Optional[str]]:
            """Extract and apply patch for a specific file from a commit.

            Args:
                commit: The commit to extract patch from
                file_path: The file path to extract patch for
                check_only: If True, only check if patch can be applied without actually applying

            Returns:
                Tuple of (success: bool, error_msg: Optional[str])
                - success: True if patch can be applied (or applied successfully)
                - error_msg: Error message if failed, None if successful
            """
            # Extract the patch for the specific file
            code, patch_content, err = self._git(["show", commit, "--", file_path])
            if code != 0:
                return False, f"failed to extract patch from {commit[:7]} for {file_path}"

            if not patch_content.strip():
                # No changes to this file in this commit
                return True, None

            # Apply the patch
            import subprocess

            apply_args = ["git", "-C", str(self.work), "apply", "--3way", "--unidiff-zero"]
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

        def get_subtree_hash(self, tree_sha: str, subdir: str) -> Optional[str]:
            """Get the hash of a subdirectory tree.

            Args:
                tree_sha: The root tree SHA
                subdir: The subdirectory path (e.g., "src/")

            Returns:
                The subtree SHA if found, None otherwise
            """
            if not tree_sha or not subdir:
                return None

            # Normalize path (remove trailing /)
            normalized_path = subdir.rstrip("/")

            # git ls-tree <tree> <path>
            code, out, err = self._git(["ls-tree", tree_sha, normalized_path])
            if code != 0:
                return None

            # Output format: <mode> <type> <hash>\t<path>
            # Example: 040000 tree abc123def456\tsrc
            lines = out.strip().split("\n")
            if not lines or not lines[0]:
                return None

            parts = lines[0].split()
            if len(parts) >= 3 and parts[1] == "tree":
                return parts[2]

            return None

        def test_pair(
            self, a: str, b: str, analyzed_files: Optional[Set[str]] = None, analyzed_dir: Optional[str] = None
        ) -> Dict[str, Any]:
            """Test if commits a and b can be parallelized by attempting bidirectional cherry-picks.

            Directly cherry-picks b onto a, and a onto b (no base commit).

            Args:
                a: base commit SHA
                b: commit to cherry-pick onto a
                analyzed_files: if provided, only check conflicts in these files (e.g., files in repo_src_dir)
                analyzed_dir: if provided, compare subtrees of this directory instead of full trees

            Returns:
                - a_then_b: result of cherry-picking b onto a
                - b_then_a: result of cherry-picking a onto b
                - can_parallelize: True if both succeed and subtrees match (when analyzed_dir is set)
                - has_dependency: True if there's an ordering dependency
            """

            def attempt_cherry_pick(base_commit: str, pick_commit: str) -> Dict[str, Any]:
                res: Dict[str, Any] = {"ok": False, "tree": None, "conflict_files": []}

                # Checkout base commit
                c0, o0, e0 = self._git(["checkout", "-B", "pairtest", base_commit])
                if c0 != 0:
                    res["error"] = f"checkout {base_commit} failed: {e0.strip()}"
                    self._reset_to_initial_state()
                    return res

                # Try cherry-pick
                c1, _o1, _e1 = self._git(["cherry-pick", "-n", pick_commit])
                if c1 != 0:
                    # Cherry-pick failed, list conflicted files
                    c2, out2, _e2 = self._git(["diff", "--name-only", "--diff-filter=U"])
                    cf = [x for x in out2.splitlines() if x.strip()] if c2 == 0 else []
                    res["conflict_files"] = cf

                    print(f"    Cherry-pick {pick_commit[:7]} onto {base_commit[:7]} failed")
                    print(f"      Conflicts: {cf}")

                    # If analyzed_dir is specified, check if conflicts are only in non-analyzed directory
                    if analyzed_dir is not None:
                        # Normalize analyzed_dir
                        normalized_analyzed_dir = analyzed_dir.rstrip("/") + "/"
                        # Filter conflicts to only those in analyzed directory
                        analyzed_conflicts = [f for f in cf if f.startswith(normalized_analyzed_dir)]
                        print(f"      analyzed_dir: {analyzed_dir}")
                        print(f"      Conflicts in {analyzed_dir}: {analyzed_conflicts}")
                        print(
                            f"      Conflicts outside {analyzed_dir}: {[f for f in cf if not f.startswith(normalized_analyzed_dir)]}"
                        )

                        if not analyzed_conflicts:
                            # All conflicts are outside analyzed directory, treat as success for our purposes
                            # Resolve non-analyzed directory conflicts by accepting pick_commit's version
                            print(f"      Resolving non-{analyzed_dir} conflicts...")
                            for conflict_file in cf:
                                if not conflict_file.startswith(normalized_analyzed_dir):
                                    # Use --theirs (pick_commit's version) for non-analyzed files
                                    self._git(["checkout", "--theirs", conflict_file])
                                    self._git(["add", conflict_file])

                            res["ok"] = True
                            # Get tree for comparison (should succeed now)
                            c3, tree, _e3 = self._git(["write-tree"])
                            if c3 == 0:
                                res["tree"] = tree.strip()
                                print(f"      → Tree after resolution: {tree.strip()}")

                    # Reset to initial state
                    self._reset_to_initial_state()
                    return res

                # Cherry-pick succeeded, get tree
                print(f"    Cherry-pick {pick_commit[:7]} onto {base_commit[:7]} succeeded")
                c3, tree, _e3 = self._git(["write-tree"])
                res["ok"] = c3 == 0
                res["tree"] = tree.strip() if c3 == 0 else None
                print(f"      → Tree: {res['tree']}")
                # Reset to initial state
                self._reset_to_initial_state()
                return res

            # Try a -> b (cherry-pick b onto a)
            r_a_then_b = attempt_cherry_pick(a, b)
            # Try b -> a (cherry-pick a onto b)
            r_b_then_a = attempt_cherry_pick(b, a)

            result: Dict[str, Any] = {
                "a_then_b": r_a_then_b,
                "b_then_a": r_b_then_a,
                "can_parallelize": False,
                "has_dependency": False,
            }

            a_ok = r_a_then_b.get("ok", False)
            b_ok = r_b_then_a.get("ok", False)

            # DEBUG
            print(f"DEBUG: test_pair {a[:7]} ⇄ {b[:7]}")
            print(
                f"  analyzed_files: {analyzed_files if analyzed_files and len(analyzed_files) <= 5 else f'{len(analyzed_files)} files' if analyzed_files else None}"
            )
            print(f"  analyzed_dir: {analyzed_dir}")
            print(f"  a_ok: {a_ok}, b_ok: {b_ok}")

            if a_ok and b_ok:
                # Both directions succeed
                if analyzed_files is not None and analyzed_dir is not None:
                    # With analyzed_files: compare subtrees of the analyzed directory
                    tree_a = r_a_then_b.get("tree")
                    tree_b = r_b_then_a.get("tree")

                    if tree_a and tree_b:
                        # Get subtrees for the analyzed directory
                        subtree_a = self.get_subtree_hash(tree_a, analyzed_dir)
                        subtree_b = self.get_subtree_hash(tree_b, analyzed_dir)

                        # DEBUG
                        print("  DEBUG: Comparing subtrees")
                        print(f"    analyzed_dir: {analyzed_dir}")
                        print(f"    tree_a: {tree_a}, subtree_a: {subtree_a}")
                        print(f"    tree_b: {tree_b}, subtree_b: {subtree_b}")

                        if subtree_a and subtree_b and subtree_a == subtree_b:
                            # Subtrees match -> can parallelize
                            print("    → Subtrees match, can parallelize ✓")
                            result["can_parallelize"] = True
                        else:
                            # Subtrees differ or missing -> has dependency (order matters)
                            print("    → Subtrees differ, has dependency ✗")
                            result["has_dependency"] = True
                    else:
                        # Tree hash missing, cannot compare
                        # Conservative: assume dependency
                        result["has_dependency"] = True
                else:
                    # Without filtering: check if full trees match
                    trees_match = r_a_then_b.get("tree") == r_b_then_a.get("tree")
                    if trees_match:
                        # Can parallelize (no edge)
                        result["can_parallelize"] = True
                    else:
                        # Both succeed but different trees -> time-ordered dependency
                        result["has_dependency"] = True
            # elif a_ok or b_ok:
            elif a_ok:
                # Only one direction succeeds -> has dependency
                result["has_dependency"] = True
            else:
                # case 1:
                #   b_ok == True: (a not depends on b, and there are missing deps.
                #   Thus b is parellal to a's dependencies + a).
                # case 2:
                #   Both fail -> no edge (no dependency)
                pass

            return result

        def can_apply_patch_to_commit(self, patch_commit: str, file_path: str, target_commit: str) -> bool:
            """Check if a specific file's patch from patch_commit can be applied to target_commit.

            Args:
                patch_commit: The commit containing the patch to test
                file_path: The specific file path to extract patch for
                target_commit: The commit to apply the patch to

            Returns:
                True if the patch can be applied without conflicts, False otherwise
            """
            # Checkout target commit
            c0, _o0, e0 = self._git(["checkout", "-B", "patchtest", target_commit])
            if c0 != 0:
                print(f"    Failed to checkout {target_commit[:7]}: {e0.strip()}")
                self._reset_to_initial_state()
                return False

            # Use utility function to check if patch can be applied
            success, error_msg = self.apply_file_patch(patch_commit, file_path, check_only=True)

            if not success:
                if error_msg:
                    print(f"    Patch from {patch_commit[:7]} cannot apply to {target_commit[:7]} on {file_path}")
                    print(f"      Error: {error_msg}")
            else:
                print(f"    Patch from {patch_commit[:7]} can apply to {target_commit[:7]} on {file_path}")

            # Reset to initial state
            self._reset_to_initial_state()
            return success

        def get_file_tree_hash(self, commit: str, file_path: str) -> Optional[str]:
            """Get the blob hash of a specific file at a given commit.

            Args:
                commit: The commit SHA
                file_path: The file path

            Returns:
                The blob hash if the file exists, None otherwise
            """
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

        def test_pair_on_parent(
            self, parent_commit: str, commit_a: str, commit_b: str, file_path: str
        ) -> Dict[str, Any]:
            """Test if commits A and B can be parallelized by cherry-picking both onto parent in both orders.

            This tests whether A and B's changes to file_path are commutative by:
            1. Cherry-picking A then B onto parent
            2. Cherry-picking B then A onto parent
            3. Comparing the resulting file blob hashes

            Args:
                parent_commit: The base commit (typically B's parent in commit order)
                commit_a: First commit to test
                commit_b: Second commit to test
                file_path: The specific file to check

            Returns:
                Dict with:
                - a_then_b: result of A→B order
                - b_then_a: result of B→A order
                - can_parallelize: True if both succeed and file hashes match
                - has_dependency: True if there's an ordering dependency
            """

            def attempt_cherry_pick_sequence(base: str, first: str, second: str) -> Dict[str, Any]:
                """Attempt to apply patches from first then second onto base, checking only file_path."""
                res: Dict[str, Any] = {"ok": False, "file_hash": None}

                # Checkout base
                c0, _o0, e0 = self._git(["checkout", "-B", "seqtest", base])
                if c0 != 0:
                    res["error"] = f"checkout {base[:7]} failed: {e0.strip()}"
                    self._reset_to_initial_state()
                    return res

                # Apply first commit's patch using utility function
                success1, error1 = self.apply_file_patch(first, file_path, check_only=False)
                if not success1:
                    res["error"] = error1 or f"apply patch from {first[:7]} failed on {file_path}"
                    self._reset_to_initial_state()
                    return res

                # Commit the first change
                self._git(["add", file_path])
                c_commit1, _o_commit1, _e_commit1 = self._git(["commit", "-m", f"temp: {first[:7]}"])
                if c_commit1 != 0:
                    res["error"] = "failed to commit first change"
                    self._reset_to_initial_state()
                    return res

                # Apply second commit's patch using utility function
                success2, error2 = self.apply_file_patch(second, file_path, check_only=False)
                if not success2:
                    res["error"] = error2 or f"apply patch from {second[:7]} failed on {file_path}"
                    self._reset_to_initial_state()
                    return res

                # Commit the second change
                self._git(["add", file_path])
                c_commit2, _o_commit2, _e_commit2 = self._git(["commit", "-m", f"temp: {second[:7]}"])
                if c_commit2 != 0:
                    res["error"] = "failed to commit second change"
                    self._reset_to_initial_state()
                    return res

                # Get final commit hash
                c_final, final_out, _ = self._git(["rev-parse", "HEAD"])
                final_commit = final_out.strip() if c_final == 0 else None

                # Get file hash at final state
                if final_commit:
                    file_hash = self.get_file_tree_hash(final_commit, file_path)
                    res["ok"] = True
                    res["file_hash"] = file_hash

                # Reset to initial state
                self._reset_to_initial_state()
                return res

            # Try A then B
            print(f"  Testing {commit_a[:7]}→{commit_b[:7]} on parent {parent_commit[:7]} for file {file_path}")
            r_a_then_b = attempt_cherry_pick_sequence(parent_commit, commit_a, commit_b)

            # Try B then A
            print(f"  Testing {commit_b[:7]}→{commit_a[:7]} on parent {parent_commit[:7]} for file {file_path}")
            r_b_then_a = attempt_cherry_pick_sequence(parent_commit, commit_b, commit_a)

            result: Dict[str, Any] = {
                "a_then_b": r_a_then_b,
                "b_then_a": r_b_then_a,
                "can_parallelize": False,
                "has_dependency": False,
            }

            a_ok = r_a_then_b.get("ok", False)
            b_ok = r_b_then_a.get("ok", False)

            print(f"  Results: a_then_b={a_ok}, b_then_a={b_ok}")

            if a_ok and b_ok:
                # Both directions succeed, check if file hashes match
                hash_a = r_a_then_b.get("file_hash")
                hash_b = r_b_then_a.get("file_hash")

                print(f"  File hashes: a_then_b={hash_a}, b_then_a={hash_b}")

                if hash_a and hash_b and hash_a == hash_b:
                    # File hashes match → can parallelize (no edge needed)
                    print("  → File hashes match, can parallelize ✓")
                    result["can_parallelize"] = True
                else:
                    # File hashes differ → has dependency (order matters)
                    print("  → File hashes differ, has dependency ✗")
                    result["has_dependency"] = True
            elif a_ok:
                # Only A→B succeeds → B depends on A (add edge B→A in commit order)
                print("  → Only a_then_b succeeds, has dependency ✗")
                result["has_dependency"] = True
            else:
                # Both fail or only B→A succeeds → no dependency
                print("  → No dependency detected")
                pass

            return result

    def _get_changed_files(cmt: str) -> Set[str]:
        if cmt in changed_files_cache:
            return changed_files_cache[cmt]
        out = run_git(repo, ["show", "--name-only", "--format=", cmt], check=False)
        files = set([ln.strip() for ln in out.splitlines() if ln.strip()])
        changed_files_cache[cmt] = files
        return files

    patch_tester: Optional[_PatchTester] = _PatchTester(repo) if include_patch_file_deps else None

    # Build set of analyzed files for conflict checking
    # This will be populated as we process commits
    analyzed_files_cache: Set[str] = set()

    for j in commit_list:  # commit_list is in temporal order
        profiler.start("process_commit")

        profiler.start("get_parent")
        parent = git_ops.get_parent_commit(j)
        profiler.stop("get_parent")

        # diffs at j
        profiler.start("get_diff")
        show_out = git_ops.get_diff(j, unified=0, detect_renames=True)
        profiler.stop("get_diff")

        profiler.start("parse_diff")
        file_diffs = parse_unified_diff(show_out)
        profiler.stop("parse_diff")
        profiler.count("total_file_diffs", len(file_diffs))

        # per-file analyze
        for fd in file_diffs:
            # path for old side blame
            old_path = fd.old_path or fd.new_path
            if old_path is None:
                continue

            # Skip files matching exclude patterns
            if all_exclude_patterns and should_exclude_file(old_path, all_exclude_patterns):
                continue

            # Skip files not in the specified source directory
            if repo_src_dir is not None:
                # Normalize paths for comparison
                normalized_src_dir = repo_src_dir.rstrip("/") + "/"
                if not old_path.startswith(normalized_src_dir):
                    continue

            # Mark that this commit has at least one analyzed file
            commits_with_analyzed_files.add(j)
            # Add this file to analyzed files cache
            analyzed_files_cache.add(old_path)

            # Phase 1: Hunk-level line/container dependency detection
            for h in fd.hunks:
                if h.old_count > 0 and parent and include_line_edges:
                    # modification/deletion: blame old side lines at parent
                    old_start = h.old_start
                    old_end = h.old_start + h.old_count - 1
                    profiler.start("blame_file_range")
                    blame_ids = git_ops.blame_file_range(parent, old_path, old_start, old_end)
                    profiler.stop("blame_file_range")
                    profiler.count("blame_operations")
                    blame_ids = [b for b in blame_ids if b]
                    if not blame_ids:
                        pass
                    mapped = map_prefix_to_commits(blame_ids, commit_set)
                    if not mapped and include_external:
                        external_deps.update(blame_ids)
                    for i in mapped:
                        # Guard: ignore self loops; also typically i should be before j
                        if i != j:
                            meta = EdgeMetadata(file=old_path, line_range=(old_start, old_end))
                            edges.append(Edge.create(src=i, dst=j, kind="line", meta=meta))
                elif h.old_count == 0 and parent and include_container_edges:
                    # pure addition: try container provider
                    # insertion occurs after old_start in parent file
                    # read parent version of file
                    profiler.start("read_file_at_commit")
                    lines = git_ops.read_file_at_commit(parent, old_path)
                    profiler.stop("read_file_at_commit")
                    provider_commit: Optional[str] = None
                    if lines is not None and is_python_file(old_path):
                        # Only consider container edges when the hunk actually adds a method
                        adds_method = any(re.match(r"^\s+def\s+[A-Za-z_][A-Za-z0-9_]*\(", ln) for ln in h.added_lines)
                        if not adds_method:
                            continue

                        profiler.start("detect_container")
                        found = detect_python_container_class(lines, h.old_start)
                        profiler.stop("detect_container")
                        if found is not None:
                            class_ln, class_name = found
                            profiler.start("blame_file_range")
                            blame_ids = git_ops.blame_file_range(parent, old_path, class_ln, class_ln)
                            profiler.stop("blame_file_range")
                            profiler.count("blame_operations")
                            blame_ids = [b for b in blame_ids if b]
                            mapped = map_prefix_to_commits(blame_ids, commit_set)
                            if mapped:
                                provider_commit = sorted(mapped, key=lambda x: order_index.get(x, -1))[0]
                            elif include_external and blame_ids:
                                external_deps.update(blame_ids)

                    # Only add container edge when a meaningful provider was found
                    if provider_commit and provider_commit != j and found is not None:
                        class_ln, class_name = found
                        meta = EdgeMetadata(file=old_path, class_name=class_name, class_line=class_ln)
                        edges.append(Edge.create(src=provider_commit, dst=j, kind="container", meta=meta))

            # Phase 2: File-level patch-based dependency detection
            # New approach: For each file modified by commit j, check if j's patch
            # can be applied to earlier commits that modified the same file.
            # Two types of dependencies are detected:
            # 1. Indirect dependency (file_indirect): j's patch cannot apply to ci at all
            #    -> suggests j depends on intermediate commits between ci and j
            # 2. Direct dependency (file_direct): j's patch can apply to ci, but
            #    when cherry-picked in both orders on ci's parent, the file hashes differ
            #    -> commits have a direct ordering dependency
            if include_patch_file_deps and patch_tester is not None:
                path_to_check = old_path
                j_idx = order_index.get(j, 0)

                print(f"\n[Phase 2] Checking file-level deps for {j[:7]} on {path_to_check}")

                # Collect all commits that touched this file before j
                for k_idx in range(j_idx - 1, -1, -1):
                    ci = commit_list[k_idx]
                    if ci == j:
                        continue
                    changed_ci = _get_changed_files(ci)
                    if path_to_check not in changed_ci:
                        continue

                    print(f"  Checking {ci[:7]} (earlier commit that modified {path_to_check})")

                    # Step 1: Check if j's patch on this file can be applied to ci
                    print("  Step 1: Can j's patch apply to ci?")
                    can_apply = patch_tester.can_apply_patch_to_commit(j, path_to_check, ci)
                    if not can_apply:
                        print("    → Cannot apply, adding indirect dependency edge")
                        # Indirect dependency: patch cannot apply at all
                        # This suggests j depends on intermediate commits between ci and j
                        meta = EdgeMetadata(file=path_to_check)
                        edges.append(Edge.create(src=ci, dst=j, kind="file_indirect", meta=meta))
                        continue
                    print("    → Can apply, proceeding to Step 2")

                    # Step 2: Get ci's parent
                    ci_parent = get_parent_commit(repo, ci)
                    if not ci_parent:
                        print("    → ci has no parent, skipping")
                        continue

                    print(f"  Step 2: Testing on ci's parent {ci_parent[:7]}")

                    # Step 3: Test if ci and j can be cherry-picked in both orders on ci_parent
                    # and compare the resulting file blob hashes
                    outcome = patch_tester.test_pair_on_parent(ci_parent, ci, j, path_to_check)

                    can_parallelize = outcome.get("can_parallelize", False)
                    has_dependency = outcome.get("has_dependency", False)

                    # Add edge based on results:
                    # - can_parallelize=True: file hashes match -> no edge (commits are commutative)
                    # - has_dependency=True: file hashes differ or only one direction succeeds -> direct dependency
                    if has_dependency:
                        print(f"  → Adding direct dependency edge {ci[:7]} -> {j[:7]} on {path_to_check}")
                        meta = EdgeMetadata(file=path_to_check)
                        edges.append(Edge.create(src=ci, dst=j, kind="file_direct", meta=meta))
                    elif can_parallelize:
                        print("  → No edge needed (can parallelize)")
                    else:
                        print("  → No edge (both cherry-picks failed)")

        # End of per-commit processing
        profiler.stop("process_commit")

    # Identify commits that were completely filtered (no files analyzed)
    filtered_commits = commit_set - commits_with_analyzed_files

    # Filter edges: remove edges involving filtered commits
    edges = [e for e in edges if e.src not in filtered_commits and e.dst not in filtered_commits]

    # dedup edges: merge edges with same src->dst but different kinds
    edge_map: Dict[Tuple[str, str], Edge] = {}
    for e in edges:
        key = (e.src, e.dst)
        if key in edge_map:
            # Merge kinds and metadata
            for k in e.kind:
                edge_map[key].add_kind(k)
            for kind, meta_list in e.metadata.items():
                edge_map[key].metadata.setdefault(kind, []).extend(meta_list)
        else:
            edge_map[key] = e
    dedup_edges = list(edge_map.values())

    # Apply transitive reduction if requested
    if reduce_transitive:
        profiler.start("transitive_reduction")
        original_count = len(dedup_edges)
        dedup_edges = transitive_reduction(commit_list, dedup_edges)
        profiler.stop("transitive_reduction")
        print(
            f"[INFO] Transitive reduction: {original_count} edges -> {len(dedup_edges)} edges (removed {original_count - len(dedup_edges)} redundant edges)"
        )

    # Update commit_list to exclude filtered commits for topological sorting
    active_commits = [c for c in commit_list if c not in filtered_commits]

    # compute layers via Kahn (only for active commits)
    profiler.start("topological_levels")
    levels, cycles = topological_levels(active_commits, dedup_edges)
    profiler.stop("topological_levels")

    # Remove filtered commits from nodes
    for commit in filtered_commits:
        nodes.pop(commit, None)

    # cleanup patch tester if any
    if patch_tester is not None:
        patch_tester.cleanup()

    # Print performance report
    overall_elapsed = time.time() - overall_start
    profiler.count("total_edges", len(dedup_edges))
    profiler.count("filtered_commits", len(filtered_commits))
    profiler.count("active_commits", len(active_commits))

    print(f"\n{'=' * 80}")
    print(f"Overall elapsed time: {overall_elapsed:.2f}s ({overall_elapsed / 60:.2f}m)")
    print(f"{'=' * 80}")
    profiler.print_report()

    return DagResult(nodes=nodes, edges=dedup_edges, levels=levels, external_deps=external_deps, cycles=cycles)


def transitive_reduction(nodes: List[str], edges: List[Edge]) -> List[Edge]:
    """Remove transitive edges from the DAG.

    If there exists A->B, B->C, and A->C, then A->C is redundant and removed.
    This performs a transitive reduction while preserving edge kinds.

    Important: file_direct edges are always preserved, as they represent
    direct file-level dependencies that are semantically important.
    """
    from collections import defaultdict, deque

    # Build adjacency list (ignore kinds for reachability)
    adj: Dict[str, List[str]] = defaultdict(list)  # node -> [targets]

    for e in edges:
        if e.dst not in adj[e.src]:
            adj[e.src].append(e.dst)

    # For each edge, check if there's an alternate path
    non_redundant: List[Edge] = []

    for e in edges:
        # Always preserve file_direct edges - they represent important direct dependencies
        if "file_direct" in e.kind:
            non_redundant.append(e)
            continue

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
                    # Found alternate path
                    has_alternate_path = True
                    break

                if v not in visited:
                    visited.add(v)
                    q.append(v)

        if not has_alternate_path:
            non_redundant.append(e)

    return non_redundant


def topological_levels(nodes: List[str], edges: List[Edge]) -> Tuple[Dict[str, int], List[List[str]]]:
    """Compute layering; returns (node->level, cycles).

    If cycles are detected, returns empty levels for nodes in cycles and reports cycles.
    """
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
        # very simple cycle extraction: nodes with indeg>0
        cyc_nodes = [n for n, d in indeg.items() if d > 0]
        if cyc_nodes:
            cycles.append(cyc_nodes)
    # Fill missing levels with 0 for disconnected nodes
    for n in nodes:
        if n not in level:
            level[n] = 0
    return level, cycles


# ------------------------------- CLI ------------------------------------- #


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build a commit-level dependency DAG (prototype)")
    p.add_argument("--repo", required=True, help="Path to a Git repo")
    p.add_argument("--commits", nargs="*", default=None, help="Commit SHAs (ordered, e.g., old->new)")
    p.add_argument("--commit-file", default=None, help="File with one commit SHA per line (ordered)")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of commits taken from --commit-file or --commits (from the start)",
    )
    p.add_argument("--include-external", action="store_true", help="Include external edges' sources in report")
    p.add_argument(
        "--patch-file-deps",
        action="store_true",
        help="Enable patch-based file dependency detection (pairwise commute/conflict checks)",
    )
    p.add_argument(
        "--reduce-transitive",
        action="store_true",
        help="Remove transitive redundant edges (if A->B, B->C, A->C exists, remove A->C)",
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
        help="Additional glob patterns to exclude (e.g., 'docs/**/*' '*.md')",
    )
    p.add_argument(
        "--repo-src-dir",
        default=None,
        help="Only analyze files under this directory (relative to repo root, e.g., 'src/' or 'lib/')",
    )
    p.add_argument(
        "--no-line-edges",
        action="store_true",
        help="Exclude line-level dependency edges (git blame based, may have false positives)",
    )
    p.add_argument(
        "--no-container-edges",
        action="store_true",
        help="Exclude container-level dependency edges (class/method additions)",
    )
    p.add_argument(
        "--use-pygit2",
        action="store_true",
        help="Use pygit2 library for git operations instead of subprocess (faster, requires pygit2 installed)",
    )
    p.add_argument("--out", default=None, help="Path to write JSON output (overrides --outdir)")
    p.add_argument("--dot", default=None, help="Path to write Graphviz DOT output (overrides --outdir)")
    p.add_argument("--outdir", default=None, help="Output directory for JSON and DOT files")
    p.add_argument("--print-levels", action="store_true", help="Print parallel layers to stdout")
    return p.parse_args(argv)


def to_json(dr: DagResult) -> Dict:
    def serialize_metadata_list(kind: str, mlist: List[EdgeMetadata]) -> List[Dict]:
        """Serialize metadata list, merging line ranges for the same file."""
        if kind == "line":
            # Group by file and merge line ranges
            file_to_ranges: Dict[str, List[Tuple[int, int]]] = {}
            for m in mlist:
                if m.line_range:
                    file_to_ranges.setdefault(m.file, []).append(m.line_range)

            # Return one entry per file with all line ranges
            return [
                {"file": file, "line_ranges": [list(r) for r in ranges]}
                for file, ranges in sorted(file_to_ranges.items())
            ]
        else:
            # For other types (container, file_direct, file_indirect), serialize individually
            result = []
            for m in mlist:
                entry = {"file": m.file}
                if m.class_name is not None:
                    entry["class_name"] = m.class_name
                if m.class_line is not None:
                    entry["class_line"] = m.class_line
                result.append(entry)
            return result

    return {
        "nodes": {k: {"sha": v.sha, "subject": v.subject} for k, v in dr.nodes.items()},
        "edges": [
            {
                "src": e.src,
                "dst": e.dst,
                "kind": e.kind,
                "metadata": {k: serialize_metadata_list(k, mlist) for k, mlist in e.metadata.items()},
            }
            for e in dr.edges
        ],
        "levels": dr.levels,
        "external_deps": sorted(list(dr.external_deps)),
        "cycles": dr.cycles,
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

    color_map = {
        "line": "black",
        "container": "blue",
        "file_direct": "purple",
        "file_indirect": "orange",
    }

    for e in dr.edges:
        # For edges with multiple kinds, use the first kind's color and show all kinds in label
        kinds_str = "+".join(e.kind)
        primary_kind = e.kind[0] if e.kind else "unknown"
        color = color_map.get(primary_kind, "black")
        lines.append(f'  "{e.src[:7]}" -> "{e.dst[:7]}" [color={color}, label="{kinds_str}"]; ')
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

    dr = build_commit_dag(
        repo=repo,
        commits=commits,
        include_external=args.include_external,
        include_patch_file_deps=args.patch_file_deps,
        reduce_transitive=args.reduce_transitive,
        exclude_test_files=args.exclude_test_files,
        exclude_ci_files=args.exclude_ci_files,
        exclude_config_files=args.exclude_config_files,
        exclude_doc_files=args.exclude_doc_files,
        exclude_patterns=args.exclude_patterns,
        repo_src_dir=args.repo_src_dir,
        include_line_edges=not args.no_line_edges,
        include_container_edges=not args.no_container_edges,
        use_pygit2=args.use_pygit2,
    )

    # Determine output file paths
    json_out = args.out
    dot_out = args.dot

    if args.outdir:
        # Extract repo directory name and remove .git suffix
        repo_name = repo.name
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]

        # Create subdirectory under outdir with repo name
        outdir = Path(args.outdir) / repo_name
        outdir.mkdir(parents=True, exist_ok=True)

        # Auto-generate basename
        if args.limit:
            basename = f"commit_dag_limit_{args.limit}"
        else:
            basename = "commit_dag"

        # Override paths if --out/--dot not explicitly provided
        if not json_out:
            json_out = str(outdir / f"{basename}.json")
        if not dot_out:
            dot_out = str(outdir / f"{basename}.dot")

    # Write JSON output
    data = to_json(dr)
    if json_out:
        Path(json_out).write_text(json.dumps(data, indent=2))
        print(f"Wrote JSON to {json_out}")
    else:
        print(json.dumps(data, indent=2))

    # Write DOT output
    if dot_out:
        Path(dot_out).write_text(to_dot(dr))
        print(f"Wrote DOT to {dot_out}")

    if args.print_levels:
        # Print layers: nodes grouped by level index
        lvl2nodes: Dict[int, List[str]] = {}
        for n, lvl in dr.levels.items():
            lvl2nodes.setdefault(lvl, []).append(n)
        for lvl in sorted(lvl2nodes.keys()):
            row = " ".join(sorted([n[:7] for n in lvl2nodes[lvl]]))
            print(f"level {lvl}: {row}")


if __name__ == "__main__":
    main()
