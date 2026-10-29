from __future__ import annotations

import csv
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from pipeline.utils.pipeline_logger import PipelineLoggerAdapter

from deepcommit.harness.utils.src_filter import SrcFileFilter
from deepcommit.multirepo.resolver import UnknownCommit, WorkspaceResolver

from ..context import TrialContext
from ..types import StageResult

_NEW_FIELDS = [
    "start_time",
    "end_time",
    "loc",
    "src_loc",
    "additions",
    "deletions",
    "src_additions",
    "src_deletions",
    "touched_test_files",
    "touched_src_files",
]

# Default test directory prefixes (used when test_patterns not provided)
_TEST_DIR_PREFIXES = ("test/", "tests/", "dummyserver/")

# Common test file patterns for different languages
_DEFAULT_TEST_PATTERNS = [
    "test/**",
    "tests/**",
    "dummyserver/**",
    "**/*_test.go",  # Go test files
    "**/*_test.py",  # Python test files
    "**/test_*.py",  # Python test files (alternative)
    "**/*.test.js",  # JavaScript test files
    "**/*.test.jsx",  # React test files
    "**/*.test.ts",  # TypeScript test files
    "**/*.test.tsx",  # React TypeScript test files
    "**/*.spec.js",  # JavaScript spec files
    "**/*.spec.ts",  # TypeScript spec files
    "**/__tests__/**",  # Jest convention
    "**/__snapshots__/**",  # Jest snapshots
    "**/testdata/**",  # Test data directories
    "**/fixtures/**",  # Test fixtures
]


def _should_exclude_file(path: str, exclude_patterns: List[str]) -> bool:
    """Delegate to the single canonical glob engine."""
    return SrcFileFilter.should_exclude_file(path, exclude_patterns)


@dataclass(frozen=True)
class CommitStats:
    commit: str
    committed_at: Optional[datetime]
    committed_at_raw: str
    loc: int
    src_loc: int
    additions: int
    deletions: int
    src_additions: int
    src_deletions: int
    test_files: Set[str]
    src_files: Set[str]


class CommitAnalyzer:
    """Lightweight helper to collect per-commit statistics with caching."""

    def __init__(
        self,
        repo_path: Path,
        logger: logging.Logger | PipelineLoggerAdapter,
        repo_src_dirs: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        test_patterns: Optional[List[str]] = None,
        resolver: Optional["WorkspaceResolver"] = None,
    ):
        self._repo_path = repo_path
        # multi-repo: pick the checkout per commit and prefix paths with the repo name (merged-tree paths)
        self._resolver = resolver if resolver is not None and resolver.multi else None
        self._logger = logger
        self._cache: Dict[str, CommitStats] = {}
        # Use provided source directories or fallback to ["src"]
        self._src_dirs = repo_src_dirs if repo_src_dirs else ["src"]
        # Use provided exclude patterns directly (combined from test_dirs + exclude)
        self._exclude_patterns: List[str] = []
        if exclude_patterns:
            self._exclude_patterns.extend([str(p) for p in exclude_patterns if str(p).strip()])
        # Use provided test patterns or fallback to defaults
        self._test_patterns = test_patterns if test_patterns else _DEFAULT_TEST_PATTERNS

    def get(self, commit: str) -> Optional[CommitStats]:
        commit = commit.strip()
        if not commit:
            return None
        if commit in self._cache:
            return self._cache[commit]

        stats = self._compute(commit)
        if stats is not None:
            self._cache[commit] = stats
        return stats

    def _compute(self, commit: str) -> Optional[CommitStats]:
        repo_path = self._repo_path
        path_prefix = ""
        if self._resolver is not None:
            try:
                repo_name = self._resolver.repo_for(commit)
            except UnknownCommit:
                self._logger.error("Commit %s is not in any workspace repo", commit)
                return None
            repo_path = self._resolver.repos[repo_name].checkout_path
            path_prefix = f"{repo_name}/"
        cmd = [
            "git",
            "-C",
            str(repo_path),
            "show",
            commit,
            "--numstat",
            "--format=%cI",
        ]

        try:
            proc = subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip().splitlines()
            err_msg = stderr[0] if stderr else str(exc)
            self._logger.error("Failed to read commit %s: %s", commit, err_msg)
            return None

        lines = proc.stdout.splitlines()
        if not lines:
            self._logger.warning("Commit %s returned no data", commit)
            return None

        timestamp_raw = lines[0].strip()
        committed_at: Optional[datetime] = None
        if timestamp_raw:
            try:
                committed_at = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
            except ValueError:
                self._logger.warning("Could not parse commit timestamp for %s: %s", commit, timestamp_raw)

        total_loc = 0
        src_loc = 0
        total_additions = 0
        total_deletions = 0
        src_additions = 0
        src_deletions = 0
        test_paths: Set[str] = set()
        src_paths: Set[str] = set()

        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            columns = line.split("\t")
            if len(columns) != 3:
                continue

            added_raw, removed_raw, path_raw = columns
            additions = _safe_int(added_raw)
            deletions = _safe_int(removed_raw)
            path_candidates = _extract_paths(path_raw)
            normalized_candidates = [
                path_prefix + normalized
                for candidate in path_candidates
                if (normalized := _normalize_path(candidate))
            ]

            # IMPORTANT: Detect test files BEFORE exclude check
            # Test file detection should not be affected by exclude_patterns
            # because exclude_patterns often contains test directories for LOC filtering
            for normalized in normalized_candidates:
                if _is_test_path(normalized, self._test_patterns):
                    test_paths.add(normalized)

            # Apply exclude patterns only for LOC calculation
            if self._exclude_patterns and any(
                _should_exclude_file(candidate, self._exclude_patterns) for candidate in normalized_candidates
            ):
                continue

            change_magnitude = additions + deletions
            total_loc += change_magnitude
            total_additions += additions
            total_deletions += deletions

            for normalized in normalized_candidates:
                # Skip test files from src_files tracking
                # (test files are already tracked in test_paths)
                is_test = _is_test_path(normalized, self._test_patterns)
                if is_test:
                    continue

                # Check if path starts with any of the configured source directories
                is_src_file = SrcFileFilter.in_src_dirs(normalized, self._src_dirs)
                if is_src_file:
                    src_loc += change_magnitude
                    src_additions += additions
                    src_deletions += deletions
                    src_paths.add(normalized)

        return CommitStats(
            commit=commit,
            committed_at=committed_at,
            committed_at_raw=timestamp_raw,
            loc=total_loc,
            src_loc=src_loc,
            additions=total_additions,
            deletions=total_deletions,
            src_additions=src_additions,
            src_deletions=src_deletions,
            test_files=test_paths,
            src_files=src_paths,
        )


def _safe_int(value: str) -> int:
    return int(value) if value.isdigit() else 0


def _is_test_path(path: str, test_patterns: Optional[List[str]] = None) -> bool:
    """Check if a path is a test file.

    Args:
        path: File path to check
        test_patterns: Optional list of glob patterns for test files.
                      If None, uses _DEFAULT_TEST_PATTERNS.
    """
    patterns = test_patterns if test_patterns else _DEFAULT_TEST_PATTERNS
    return SrcFileFilter.should_exclude_file(path, patterns)


def _normalize_path(path: str) -> str:
    path = path.strip().replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    return path


def _extract_paths(raw: str) -> Tuple[str, ...]:
    """Handle rename syntax such as 'a -> b' or 'dir/{old => new}/file.py'."""
    raw = raw.strip()
    if " -> " in raw:
        return (raw.split(" -> ", 1)[1].strip(),)

    if " => " in raw:
        # First handle {... => ...} syntax
        def _brace_repl(match: re.Match[str]) -> str:
            return match.group(2)

        replaced = re.sub(r"\{([^{}]+?) => ([^{}]+?)\}", _brace_repl, raw)
        if " => " in replaced:
            return (replaced.split(" => ", 1)[1].strip(),)
        return (replaced.strip(),)

    return (raw,)


def _ensure_field_order(fieldnames: List[str]) -> List[str]:
    names = list(fieldnames)
    # Remove deprecated 'touched_files' column (replaced by touched_test_files + touched_src_files)
    if "touched_files" in names:
        names.remove("touched_files")
    for field in _NEW_FIELDS:
        if field not in names:
            names.append(field)
    return names


def _format_paths(paths: Iterable[str]) -> str:
    unique = sorted({p for p in paths if p})
    return ";".join(unique)


def _setup_logger(logger: Optional[logging.Logger]) -> logging.Logger:
    if logger is not None:
        return logger
    logger = logging.getLogger("milestone_enrichment")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def milestone_repos(commits: List[str], resolver: WorkspaceResolver) -> Set[str]:
    """Repo names touched by a list of commits (unknown commits are ignored)."""
    out: Set[str] = set()
    for c in commits:
        try:
            out.add(resolver.repo_for(c))
        except UnknownCommit:
            continue
    return out


def run_milestone_enrichment(
    milestones_csv: Path | str,
    repo_path: Path | str,
    logger: Optional[logging.Logger | PipelineLoggerAdapter] = None,
    repo_src_dirs: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
    test_patterns: Optional[List[str]] = None,
    resolver: Optional[WorkspaceResolver] = None,
) -> Dict[str, object]:
    """Augment milestones.csv in-place with timing, size, and file coverage metrics.

    Args:
        milestones_csv: Path to milestones CSV file
        repo_path: Path to repository
        logger: Optional logger instance
        repo_src_dirs: List of source directory paths (e.g., ["packages/markitdown/src/markitdown"])
                      Defaults to ["src"] if not provided
        exclude_patterns: Combined exclude patterns (from test_dirs + exclude)
        test_patterns: Test file patterns for detecting test files (e.g., ["**/*_test.go"])
                      Defaults to _DEFAULT_TEST_PATTERNS if not provided
    """

    if logger is None:
        logger = logging.getLogger("milestone_enrichment")
        logger.setLevel(logging.INFO)

    milestones_path = Path(milestones_csv).resolve()
    repo_path = Path(repo_path).resolve()

    if not milestones_path.exists():
        msg = f"milestones.csv not found at {milestones_path}"
        logger.error(msg)
        return {"status": "error", "error": msg}

    if not repo_path.exists():
        msg = f"Repository path not found at {repo_path}"
        logger.error(msg)
        return {"status": "error", "error": msg}

    analyzer = CommitAnalyzer(
        repo_path,
        logger,
        repo_src_dirs=repo_src_dirs,
        exclude_patterns=exclude_patterns,
        test_patterns=test_patterns,
        resolver=resolver,
    )
    multi = resolver is not None and resolver.multi
    updated_rows: List[Dict[str, str]] = []
    commit_misses: List[str] = []

    with milestones_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            msg = "milestones.csv has no header"
            logger.error(msg)
            return {"status": "error", "error": msg}
        fieldnames = _ensure_field_order(list(reader.fieldnames))
        if multi and "repos" not in fieldnames:
            fieldnames.append("repos")

        for row in reader:
            commits_raw = row.get("commits", "") or ""
            commits = [c.strip() for c in commits_raw.split(";") if c.strip()]

            timestamps: List[Tuple[datetime, str]] = []
            total_loc = 0
            total_src_loc = 0
            total_additions = 0
            total_deletions = 0
            total_src_additions = 0
            total_src_deletions = 0
            all_test_files: Set[str] = set()
            all_src_files: Set[str] = set()

            for commit in commits:
                stats = analyzer.get(commit)
                if stats is None:
                    commit_misses.append(commit)
                    continue
                if stats.committed_at is not None:
                    timestamps.append((stats.committed_at, stats.committed_at_raw))
                total_loc += stats.loc
                total_src_loc += stats.src_loc
                total_additions += stats.additions
                total_deletions += stats.deletions
                total_src_additions += stats.src_additions
                total_src_deletions += stats.src_deletions
                all_test_files.update(stats.test_files)
                all_src_files.update(stats.src_files)

            if timestamps:
                timestamps.sort(key=lambda item: item[0])
                row["start_time"] = timestamps[0][1]
                row["end_time"] = timestamps[-1][1]
            else:
                row["start_time"] = ""
                row["end_time"] = ""

            if multi:
                row["repos"] = ";".join(sorted(milestone_repos(commits, resolver)))

            row["loc"] = str(total_loc)
            row["src_loc"] = str(total_src_loc)
            row["additions"] = str(total_additions)
            row["deletions"] = str(total_deletions)
            row["src_additions"] = str(total_src_additions)
            row["src_deletions"] = str(total_src_deletions)
            row["touched_test_files"] = _format_paths(all_test_files)
            row["touched_src_files"] = _format_paths(all_src_files)

            # Remove deprecated 'touched_files' column
            row.pop("touched_files", None)

            updated_rows.append(row)

    try:
        with milestones_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(updated_rows)
    except PermissionError as exc:
        msg = f"Permission denied while writing {milestones_path}: {exc}"
        logger.error(msg)
        return {"status": "error", "error": msg}
    except OSError as exc:
        msg = f"Failed to write {milestones_path}: {exc}"
        logger.error(msg)
        return {"status": "error", "error": msg}

    artifacts: Dict[str, object] = {
        "milestones_csv": str(milestones_path),
        "milestones_enriched": len(updated_rows),
        "missing_commits": sorted(set(commit_misses)),
        "new_fields": list(_NEW_FIELDS),
    }

    if commit_misses:
        logger.warning("Commit stats missing for %d commits", len(set(commit_misses)))
    logger.info(
        "Enriched %d milestones with timing, LOC, and path metadata",
        len(updated_rows),
    )

    return {"status": "ok", **artifacts}


def enrich_milestones(ctx: TrialContext, logger: logging.Logger) -> StageResult:
    print()
    logger.info("[6/8] Enhance milestones with repository metrics")

    milestones_path = Path(ctx.to_abs_path(ctx.result_path)) / "milestones.csv"
    repo_path = Path(ctx.to_abs_path(ctx.get_repo()))

    result = run_milestone_enrichment(
        milestones_path,
        repo_path,
        logger=logger,
        repo_src_dirs=ctx.get_repo_src_dirs(),
        exclude_patterns=ctx.get_repo_exclude_patterns(),
        test_patterns=ctx.test_dirs,  # Use test_dirs for test file detection
        resolver=WorkspaceResolver.from_dir(ctx.workspace_dir) if ctx.is_multi_repo else None,
    )
    if result.get("status") != "ok":
        return StageResult(status="error", error=str(result.get("error", "Unknown enrichment error")))

    artifacts = {k: v for k, v in result.items() if k != "status"}
    return StageResult(status="ok", artifacts=artifacts)


__all__ = ["enrich_milestones", "run_milestone_enrichment"]
