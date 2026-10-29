#!/usr/bin/env python3
"""
Milestone Testbed 构建工具
========================

功能描述
--------
为每个 milestone 创建 start 和 end 状态的 Git tags，并导出 milestone patch，
用于后续 runtime environment resolution 阶段的 testbed 构建。

核心设计："start's src + end's test"
- Start 状态: base_tag 的 src 代码 + milestone 最后一个 commit 的 test 代码
- End 状态: cherry-pick milestone 所有 commits 后的 src 代码 + milestone 最后一个 commit 的 test 代码
- 这样 start 和 end 状态共享相同的测试代码，只有 src 代码不同

使用方法
--------
基本用法（推荐，显式接口）:
    python harness/prepare_repo/build_milestone_testbed.py \\
        --milestone-csv milestones.csv \\
        --dependencies-csv dependencies.csv \\
        --config config/{repo_range}.yaml \\
        --repo-path source_repo \\
        --output-dir . \\
        --base-tag {start_version} \\
        --incremental \\
        --verify

路径推断用法（兼容旧工作流）:
    python harness/prepare_repo/build_milestone_testbed.py \\
        --milestone-csv DATA/result/{project}/milestones.csv \\
        --dependencies-csv DATA/result/{project}/dependencies.csv

    或者:
    python harness/prepare_repo/build_milestone_testbed.py \\
        --milestone-csv DATA/harness_workspace/{repo_versions}/{run_id}/milestones.csv \\
        --dependencies-csv DATA/harness_workspace/{repo_versions}/{run_id}/dependencies.csv

常用参数:
    --force-rebuild     强制重建 testbed（删除已存在的工作目录）
    --verbose           显示详细的 git 命令输出
    --verify            创建完成后验证所有 tags

路径结构与自动推断
----------------
脚本支持从 milestone-csv 路径自动推断部分参数，但这些推断只是 convenience。
对于自动化系统，推荐显式传入 `--config --repo-path --output-dir --base-tag`。

当使用路径推断时，脚本会尝试从 milestone-csv 路径推断以下参数:

1. DATA/result/ 路径结构（单层目录）:
   DATA/result/{owner}_{repo}_{start_ver}_{end_ver}_{suffix}/milestones.csv

   示例: DATA/result/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6_baseline_rerun_stage4_001/milestones.csv

   自动推断:
   - config         → config/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6.yaml
   - repo-path      → DATA/github_data/repos/apache_dubbo
   - base-tag       → dubbo-3.3.3
   - output-dir     → DATA/harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_001
   - commit-splits  → DATA/trial_space/apache_dubbo_..._001/stage4_decomp/output/commit_splits

2. DATA/harness_workspace/ 路径结构（两层目录）:
   DATA/harness_workspace/{repo_versions}/{run_id}/milestones.csv

   示例: DATA/harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_001/milestones.csv

   自动推断:
   - config         → config/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6.yaml
   - repo-path      → DATA/github_data/repos/apache_dubbo
   - base-tag       → dubbo-3.3.3
   - output-dir     → DATA/harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_001
   - commit-splits  → DATA/trial_space/apache_dubbo_..._baseline_rerun_stage4_001/stage4_decomp/output/commit_splits

Split Commits 处理
-----------------
当存在 split commits（手动拆分的大型 PR）时:
- 脚本会从 trial_space/{project}/stage4_decomp/output/commit_splits/ 读取 METADATA.json
- 自动识别 split commits 并使用原始 commit 的时间戳进行排序
- Clone 源会自动切换到 trial_dir/repo_data（包含 split commits）

⚠️  注意: 当 harness_workspace 目录名与 trial_space 目录名不匹配时（如带有 _fix1 等后缀），
自动推断会失败。此时需要手动指定 --commit-splits-dir 参数:

   示例: harness_workspace 目录为 baseline_rerun_stage4_002_fix1，
         但 trial_space 目录为 baseline_rerun_stage4_002（无后缀）

   python harness/prepare_repo/build_milestone_testbed.py \\
       --milestone-csv DATA/harness_workspace/.../baseline_rerun_stage4_002_fix1/milestones.csv \\
       --dependencies-csv DATA/harness_workspace/.../baseline_rerun_stage4_002_fix1/dependencies.csv \\
       --commit-splits-dir DATA/trial_space/..._baseline_rerun_stage4_002/stage4_decomp/output/commit_splits \\
       --force-rebuild

示例
----
1. 从 DATA/result/ 构建（自动推断所有参数）:
   python harness/prepare_repo/build_milestone_testbed.py \\
       --milestone-csv DATA/result/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6_baseline_rerun_stage4_001/milestones.csv \\
       --dependencies-csv DATA/result/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6_baseline_rerun_stage4_001/dependencies.csv \\
       --force-rebuild --verbose

2. 从 DATA/harness_workspace/ 构建:
   python harness/prepare_repo/build_milestone_testbed.py \\
       --milestone-csv DATA/harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_001/milestones.csv \\
       --dependencies-csv DATA/harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_001/dependencies.csv \\
       --force-rebuild

3. 手动指定所有参数:
   python harness/prepare_repo/build_milestone_testbed.py \\
       --milestone-csv path/to/milestones.csv \\
       --dependencies-csv path/to/dependencies.csv \\
       --config config/project.yaml \\
       --repo-path DATA/github_data/repos/owner_repo \\
       --base-tag v1.0.0 \\
       --output-dir DATA/harness_workspace/project/run_001 \\
       --commit-splits-dir DATA/trial_space/project/stage4_decomp/output/commit_splits

输出
----
构建完成后，在 output-dir 下生成:
- testbed/                  克隆的 git 仓库，包含所有 milestone tags
- milestones.csv            复制的 milestones 文件
- dependencies.csv          复制的 dependencies 文件
- metadata.json             构建元数据
- milestone_patches/        每个 milestone 的 start..end patch

说明:
- 本脚本的职责边界到 `testbed/`、`metadata.json`、refined CSV、`milestone_patches/` 为止。
- `milestone_patched_tests/*.json` 属于后续测试收集/验证阶段，不由本脚本直接生成。

每个 milestone 生成两个 tags:
- milestone-{id}-start      起始状态
- milestone-{id}-end        结束状态
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml


def compute_milestone_hash(commits: str, parents: List[str]) -> Tuple[str, str]:
    """计算 commits 和 parents 的 hash 用于一致性校验。

    Args:
        commits: 分号分隔的 commit hash 字符串
        parents: 父 milestone ID 列表

    Returns:
        Tuple of (commits_hash, parents_hash), each 16 chars
    """
    commits_hash = hashlib.sha256(commits.encode()).hexdigest()[:16]
    parents_hash = hashlib.sha256(";".join(sorted(parents)).encode()).hexdigest()[:16]
    return commits_hash, parents_hash


class _ProfileSection:
    """Small context manager used by BuildProfiler.section()."""

    def __init__(self, profiler: "BuildProfiler", name: str, metadata: Optional[Dict[str, Any]] = None):
        self.profiler = profiler
        self.name = name
        self.metadata = metadata or {}
        self.started = 0.0

    def __enter__(self):
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        metadata = dict(self.metadata)
        if exc_type is not None:
            metadata["exception"] = exc_type.__name__
        self.profiler.record(self.name, time.perf_counter() - self.started, metadata)
        return False


class BuildProfiler:
    """Collect lightweight timing data for testbed builds."""

    def __init__(
        self,
        enabled: bool = False,
        slow_git_threshold_s: float = 1.0,
        top_git_commands: int = 50,
    ):
        self.enabled = enabled
        self.slow_git_threshold_s = slow_git_threshold_s
        self.top_git_commands = top_git_commands
        self.started_at = datetime.utcnow().isoformat() + "Z"
        self.started_perf = time.perf_counter()
        self.sections: List[Dict[str, Any]] = []
        self.section_totals: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"count": 0, "total_s": 0.0, "max_s": 0.0}
        )
        self.git_totals: Dict[str, Dict[str, float]] = defaultdict(lambda: {"count": 0, "total_s": 0.0, "max_s": 0.0})
        self.slow_git_commands: List[Dict[str, Any]] = []

    def section(self, name: str, **metadata: Any) -> _ProfileSection:
        return _ProfileSection(self, name, metadata)

    def record(self, name: str, elapsed_s: float, metadata: Optional[Dict[str, Any]] = None) -> None:
        if not self.enabled:
            return
        agg = self.section_totals[name]
        agg["count"] += 1
        agg["total_s"] += elapsed_s
        agg["max_s"] = max(agg["max_s"], elapsed_s)
        event = {
            "name": name,
            "elapsed_s": round(elapsed_s, 6),
        }
        if metadata:
            event["metadata"] = metadata
        self.sections.append(event)

    def record_git(self, cmd: str, elapsed_s: float) -> None:
        if not self.enabled:
            return
        category = self._git_category(cmd)
        for key in ("__all__", category):
            agg = self.git_totals[key]
            agg["count"] += 1
            agg["total_s"] += elapsed_s
            agg["max_s"] = max(agg["max_s"], elapsed_s)

        if elapsed_s >= self.slow_git_threshold_s:
            self.slow_git_commands.append(
                {
                    "elapsed_s": round(elapsed_s, 6),
                    "category": category,
                    "cmd": cmd[:400],
                }
            )
            if len(self.slow_git_commands) > self.top_git_commands * 2:
                self.slow_git_commands = sorted(
                    self.slow_git_commands,
                    key=lambda item: item["elapsed_s"],
                    reverse=True,
                )[: self.top_git_commands]

    def _git_category(self, cmd: str) -> str:
        cmd = cmd.strip()
        if cmd.startswith("checkout ") and "--pathspec-from-file=" in cmd:
            return "checkout-pathspec-batch"
        if cmd.startswith("checkout ") and " -- " in cmd:
            return "checkout-pathspec"
        if cmd.startswith("checkout -b "):
            return "checkout-branch"
        if cmd.startswith("checkout "):
            return "checkout-ref"
        if cmd.startswith("cherry-pick "):
            return "cherry-pick"
        if cmd.startswith("merge "):
            return "merge"
        if cmd.startswith("rm -rf "):
            return "rm-non-src"
        if cmd.startswith("diff "):
            return "diff"
        if cmd.startswith("log "):
            return "log"
        if cmd.startswith("rev-parse "):
            return "rev-parse"
        if cmd.startswith("rev-list "):
            return "rev-list"
        if cmd.startswith("tag "):
            return "tag"
        if cmd.startswith("add "):
            return "add"
        if cmd.startswith("commit "):
            return "commit"
        if cmd.startswith("status "):
            return "status"
        if cmd.startswith("apply "):
            return "apply"
        if cmd.startswith("ls-tree "):
            return "ls-tree"
        if cmd.startswith("ls-files"):
            return "ls-files"
        if cmd.startswith("show "):
            return "show"
        return cmd.split(" ", 1)[0] if cmd else "unknown"

    def _format_totals(self, totals: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
        rows = []
        for name, agg in totals.items():
            count = int(agg["count"])
            total = agg["total_s"]
            rows.append(
                {
                    "name": name,
                    "count": count,
                    "total_s": round(total, 6),
                    "max_s": round(agg["max_s"], 6),
                    "avg_s": round(total / count, 6) if count else 0.0,
                }
            )
        return sorted(rows, key=lambda row: row["total_s"], reverse=True)

    def summary(self) -> Dict[str, Any]:
        slow_git = sorted(self.slow_git_commands, key=lambda item: item["elapsed_s"], reverse=True)[
            : self.top_git_commands
        ]
        return {
            "enabled": self.enabled,
            "started_at": self.started_at,
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "total_elapsed_s": round(time.perf_counter() - self.started_perf, 6),
            "section_totals": self._format_totals(self.section_totals),
            "sections": self.sections,
            "git": {
                "totals": self._format_totals(self.git_totals),
                "slow_threshold_s": self.slow_git_threshold_s,
                "slow_commands": slow_git,
            },
        }

    def write(self, path: Path) -> None:
        if not self.enabled:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.summary(), f, indent=2, ensure_ascii=False)

    def print_summary(self, path: Path, top_n: int = 8) -> None:
        if not self.enabled:
            return
        summary = self.summary()
        print(f"\n⏱️  build_profile.json → {path}")
        print(f"   total_elapsed_s={summary['total_elapsed_s']}")
        git_totals = [row for row in summary["git"]["totals"] if row["name"] != "__all__"]
        if git_totals:
            print("   Top git categories:")
            for row in git_totals[:top_n]:
                print(
                    f"      {row['name']}: total={row['total_s']}s "
                    f"count={row['count']} avg={row['avg_s']}s max={row['max_s']}s"
                )


from deepcommit.harness.utils.src_filter import SrcFileFilter  # noqa: E402

# Handle both direct execution and module import
try:
    from deepcommit.harness.prepare_repo.split_commit_mapper import SplitCommitMapper
except ModuleNotFoundError:
    from split_commit_mapper import SplitCommitMapper


class MilestoneBuildFailure(Exception):
    """Raised when a milestone fails to build, caught by --continue-on-error mode."""

    def __init__(
        self,
        milestone_id: str,
        stage: str,  # "start" | "end"
        failing_commit: str = "",
        failing_commit_subject: str = "",
        failing_files_src: Optional[List[str]] = None,
        rej_hunks: Optional[List[dict]] = None,
        error_msg: str = "",
        commit_index_in_milestone: Optional[int] = None,
        total_commits_in_milestone: Optional[int] = None,
        prior_applied_commits: Optional[List[str]] = None,
    ):
        self.milestone_id = milestone_id
        self.stage = stage
        self.failing_commit = failing_commit
        self.failing_commit_subject = failing_commit_subject
        self.failing_files_src = failing_files_src or []
        self.rej_hunks = rej_hunks or []
        self.error_msg = error_msg
        # --- P1: split-decision signals ---
        # 1-based index of the failing commit within milestone.commits
        self.commit_index_in_milestone = commit_index_in_milestone
        # total commit count in this milestone (context for the index)
        self.total_commits_in_milestone = total_commits_in_milestone
        # commits from THIS milestone that were already applied before the failure
        self.prior_applied_commits = prior_applied_commits or []
        super().__init__(error_msg or f"Milestone {milestone_id} failed at {stage} stage")


class MilestoneData:
    """Represents a milestone with its metadata."""

    def __init__(self, row: dict):
        self.id = row["id"]
        self.title = row["title"]
        self.category = row["category"]
        self.commits = row["commits"].split(";") if row["commits"] else []
        # Note: last_commit is updated after BranchCreator applies its
        # configured milestone commit order.
        self.last_commit = self.commits[-1] if self.commits else None
        # integration_test_commit: Optional commit containing additional test files
        # This is used for split commits where tests are in a separate commit
        self.integration_test_commit = row.get("integration_test_commit", "").strip() or None

    def __repr__(self):
        return f"Milestone({self.id}, commits={len(self.commits)})"


class BranchCreator:
    """Creates milestone tags with proper dependency handling."""

    def __init__(
        self,
        repo_path: Path,
        work_dir: Path,
        base_tag: str = "2.0.6",
        verbose: bool = False,
        rename_threshold: int = 50,
        force_rebuild: bool = False,
        src_dirs: Optional[List[str]] = None,
        main_branch: str = "main",
        test_dirs: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        pytest_timeout: int = 50,
        pytest_workers: str = "4",
        docker_cpus: int = 16,
        preserve_commit_order: bool = False,
        commit_splits_dir: Optional[Path] = None,
        generated_patterns: Optional[List[str]] = None,
        modifiable_test_patterns: Optional[List[str]] = None,
        continue_on_error: bool = False,
        profiler: Optional[BuildProfiler] = None,
    ):
        self.repo_path = Path(repo_path)
        self.work_dir = Path(work_dir)
        self.base_tag = base_tag
        self.verbose = verbose
        self.rename_threshold = rename_threshold
        self.force_rebuild = force_rebuild
        self.work_repo = None
        self.main_branch = main_branch
        # Actual base commit to use (may differ from base_tag if tag is on release branch)
        self.actual_base_commit: Optional[str] = None
        # Source directories to track (default: ["src/"])
        self.src_dirs = src_dirs if src_dirs else ["src/"]
        # Normalize: ensure trailing slash for directory paths
        self.src_dirs = [d if d.endswith("/") else d + "/" for d in self.src_dirs]
        # Test directories/patterns (default: ["test/"])
        self.test_dirs = test_dirs if test_dirs else ["test/"]
        # Exclude patterns (e.g., ["**/target/**", "**/__pycache__/**"])
        self.exclude_patterns = exclude_patterns if exclude_patterns else []
        # Generated code patterns (e.g., ["**/*.pb.go", "**/wire_gen.go"])
        self.generated_patterns = generated_patterns if generated_patterns else []
        # Modifiable test patterns (e.g., ["**/agents_plugin_test.go"])
        self.modifiable_test_patterns = modifiable_test_patterns if modifiable_test_patterns else []
        # Source file filter (shared utility)
        self.src_filter = SrcFileFilter(
            src_dirs=self.src_dirs,
            test_dirs=self.test_dirs,
            exclude_patterns=self.exclude_patterns,
        )
        # Pytest configuration
        self.pytest_timeout = pytest_timeout
        self.pytest_workers = pytest_workers
        # Docker resource configuration
        self.docker_cpus = docker_cpus
        # Whether to preserve commit order from CSV instead of applying topo sort.
        self.preserve_commit_order = preserve_commit_order
        # Store commit_splits_dir for inferring trial_dir/repo_data
        self.commit_splits_dir = commit_splits_dir
        # --continue-on-error mode: record failures instead of sys.exit(1)
        self.continue_on_error = continue_on_error
        # Optional build profiler for diagnosing slow M1 rebuilds
        self.profiler = profiler or BuildProfiler(enabled=False)
        # Per-milestone status records for build_report.json
        # {milestone_id: {"status": str, "failing_commit": str, ...}}
        self._milestone_statuses: Dict[str, Dict] = {}
        # Milestone IDs that failed (used to skip downstream milestones)
        self._failed_milestones: Set[str] = set()
        # Last .rej hunks captured by _apply_commit_as_patch before cleanup.
        # Populated in failure path, read by the continue-on-error failure handler.
        self._last_rej_hunks: List[dict] = []
        # Map: commit_sha -> milestone_id (lazily populated for prior_modifiers lookup)
        self._commit_to_milestone: Optional[Dict[str, str]] = None
        # Split commit mapper for original timestamp tie-breaks.
        self.split_mapper = SplitCommitMapper(commit_splits_dir)
        if commit_splits_dir and commit_splits_dir.exists():
            stats = self.split_mapper.get_stats()
            if stats["total_split_commits"] > 0:
                print(
                    f"📦 Loaded split commit mappings: {stats['total_split_commits']} split commits from {stats['total_original_commits']} original commits"
                )

    def _get_exclude_src_pathspec(self) -> str:
        """Get git pathspec to exclude source directories."""
        # e.g., "':(exclude)src/'" or "':(exclude)packages/markitdown/src/markitdown/'"
        excludes = " ".join(f"':(exclude){src_dir}'" for src_dir in self.src_dirs)
        return excludes

    # ============================================
    # Sub-milestone group utilities
    # ============================================

    def _is_sub_milestone(self, milestone_id: str) -> bool:
        """
        Check if milestone is a sub-milestone.

        Supported patterns:
        1. 'M003.1', 'M003.2' - dot notation
        2. 'milestone_003_sub-01', 'milestone_003_sub-02' - sub- notation

        Returns:
            True if the milestone is a sub-milestone
        """
        if "." in milestone_id:
            return True
        if "_sub-" in milestone_id or "_sub_" in milestone_id:
            return True
        return False

    def _get_milestone_group(self, milestone_id: str) -> Optional[str]:
        """
        Extract group prefix from sub-milestone ID.

        Examples:
            'M003.1' -> 'M003'
            'M003.2' -> 'M003'
            'milestone_003_sub-01' -> 'milestone_003'
            'milestone_003_sub-02' -> 'milestone_003'
            'M001' -> None (not a sub-milestone)

        Returns:
            The group prefix, or None if not a sub-milestone
        """
        if "." in milestone_id:
            return milestone_id.rsplit(".", 1)[0]
        match = re.match(r"^(.+?)_sub[-_].+$", milestone_id)
        if match:
            return match.group(1)
        return None

    def _get_group_milestone_ids(self, group_prefix: str) -> List[str]:
        """
        Get all milestone IDs belonging to a group.

        Args:
            group_prefix: The group prefix (e.g., 'M003')

        Returns:
            List of milestone IDs belonging to the group
        """
        if not hasattr(self, "_milestones"):
            return []
        return [mid for mid in self._milestones.keys() if self._get_milestone_group(mid) == group_prefix]

    def _collect_group_itcs(self, group_milestone_ids: List[str]) -> List[str]:
        """
        Collect all ITCs from milestones within the same group.

        ITCs don't inherit across groups - only within the same sub-milestone group.

        Args:
            group_milestone_ids: List of milestone IDs in the group

        Returns:
            List of unique ITC hashes from the group
        """
        if not hasattr(self, "_milestones"):
            return []
        itcs = []
        for mid in group_milestone_ids:
            if mid in self._milestones:
                itc = self._milestones[mid].integration_test_commit
                if itc and itc not in itcs:
                    itcs.append(itc)
        return itcs

    def _find_leaf_milestones_in_group(self, group_milestone_ids: List[str]) -> List[str]:
        """
        Find leaf milestones (no children within the group) in a sub milestone group.

        In DAG: source -> target means target depends on source.
        Leaf nodes are nodes that no other node depends on (no outgoing edges).

        Args:
            group_milestone_ids: List of milestone IDs in the group

        Returns:
            List of leaf milestone IDs (nodes with no children in the group)
        """
        if not hasattr(self, "_dependencies"):
            return group_milestone_ids

        group_set = set(group_milestone_ids)

        # Build children map (reverse of dependencies)
        # _dependencies: child -> [parents]
        # children_map: parent -> [children]
        children_map: Dict[str, List[str]] = defaultdict(list)
        for child, parents in self._dependencies.items():
            if child in group_set:
                for parent in parents:
                    if parent in group_set:
                        children_map[parent].append(child)

        # Leaf nodes have no children within the group
        return [mid for mid in group_milestone_ids if not children_map.get(mid)]

    def _get_applicable_itcs_for_sub_milestone(self, milestone: MilestoneData) -> List[str]:
        """
        Get applicable ITCs for a sub-milestone.

        Conditions for applying ITC (ALL must be satisfied):
        1. The milestone is a sub-milestone (dot or sub- notation)
        2. The group has at least one ITC
        3. The milestone is a leaf node in its group's DAG
        4. The milestone's last_commit_ts <= ITC_ts

        Args:
            milestone: The milestone being processed

        Returns:
            List of ITC hashes that should be applied
        """
        # Condition 1: Must be a sub-milestone
        if not self._is_sub_milestone(milestone.id):
            return []

        group_prefix = self._get_milestone_group(milestone.id)
        if not group_prefix:
            return []

        group_milestone_ids = self._get_group_milestone_ids(group_prefix)

        # Condition 2: Group must have ITCs
        group_itcs = self._collect_group_itcs(group_milestone_ids)
        if not group_itcs:
            return []

        # Condition 3: Must be a leaf node in the group's DAG
        leaves = self._find_leaf_milestones_in_group(group_milestone_ids)
        if milestone.id not in leaves:
            return []

        # Condition 4: Filter ITCs by timestamp
        if not milestone.last_commit:
            return []

        last_commit_ts = self._get_original_timestamp(milestone.last_commit)

        applicable = []
        for itc in group_itcs:
            itc_ts = self._get_original_timestamp(itc)
            # Apply if last_commit_ts <= ITC_ts
            if last_commit_ts <= itc_ts:
                applicable.append(itc)

        return applicable

    def _get_all_integration_test_commits(self, milestone: MilestoneData) -> List[str]:
        """
        Get all integration_test_commits for a milestone, including inherited from parents.

        For split commits, only the first milestone in a chain may have integration_test_commit
        defined. Subsequent milestones in the dependency chain should inherit it.

        Example: M003.1 -> M003.2 -> M003.3
        - M003.1 has integration_test_commit = 87a54f3
        - M003.2 and M003.3 inherit 87a54f3 from M003.1

        Returns:
            List of unique integration_test_commit hashes (from self + all ancestors)
        """
        commits = set()

        # Add this milestone's integration_test_commit if it has one
        if milestone.integration_test_commit:
            commits.add(milestone.integration_test_commit)

        # Check if we have access to milestones and dependencies
        if not hasattr(self, "_milestones") or not hasattr(self, "_dependencies"):
            return list(commits)

        # Recursively collect from parents
        parents = self._dependencies.get(milestone.id, [])
        for parent_id in parents:
            if parent_id in self._milestones:
                parent_milestone = self._milestones[parent_id]
                parent_commits = self._get_all_integration_test_commits(parent_milestone)
                commits.update(parent_commits)

        return list(commits)

    def _get_original_timestamp(self, commit: str) -> int:
        """
        Get the original timestamp for a commit.

        For split commits: returns the original commit's timestamp.
        For regular commits: returns the commit's own timestamp.

        This is used to compare commits based on their "logical" time,
        not the time they were created during splitting.

        Args:
            commit: The commit hash

        Returns:
            Unix timestamp (seconds since epoch)
        """
        # Use SplitCommitMapper.get_sort_key() which returns (original_timestamp, split_number)
        # We only need the timestamp part
        sort_key = self.split_mapper.get_sort_key(commit, self._get_commit_timestamp)
        return sort_key[0]  # Return timestamp part

    def _restore_non_src_files_for_milestone(
        self,
        milestone: MilestoneData,
        quiet: bool = True,
        remove_extra: bool = False,
    ) -> None:
        """
        Restore all non-SRC files for a milestone from last_commit and integration_test_commits.

        non-SRC files are defined as files that are NOT source files:
        - Files outside repo_src_dirs (e.g., root pom.xml, README, .gitignore)
        - Files matching test_dirs patterns (e.g., **/src/test/**)
        - Files matching exclude_patterns

        Uses original commit timestamps to decide file version priority:
        - If integration_test_commit's original timestamp >= last_commit's original timestamp:
          use the integration_test_commit's file version
        - Otherwise: use the last_commit's file version

        This ensures:
        1. Files from the same original commit (split commits) will use integration_test_commit version
        2. Files from newer original commits will override older ones
        3. Files from older integration_test_commits won't override newer last_commit files

        Args:
            milestone: The milestone being processed
            quiet: Whether to suppress output
            remove_extra: Whether to remove non-src files not in the expected set
        """
        profile_started = time.perf_counter()
        if not milestone.last_commit:
            self.profiler.record(
                "milestone.restore_non_src_files",
                time.perf_counter() - profile_started,
                {"milestone_id": milestone.id, "files": 0, "remove_extra": remove_extra},
            )
            return

        integration_commits = self._get_applicable_itcs_for_sub_milestone(milestone)

        # Step 1: Get last_commit's original timestamp (used as comparison baseline)
        last_commit_original_ts = self._get_original_timestamp(milestone.last_commit)

        if not quiet:
            is_split = self.split_mapper.is_split_commit(milestone.last_commit)
            if is_split:
                info = self.split_mapper.get_split_info(milestone.last_commit)
                print(f"      📋 last_commit {milestone.last_commit[:7]} is split from {info.original_short_hash}")
            inherited_count = len([c for c in integration_commits if c != milestone.integration_test_commit])
            sources_desc = f"{milestone.last_commit[:7]}"
            if integration_commits:
                sources_desc += f" + {len(integration_commits)} integration_test_commit(s)"
                if inherited_count > 0:
                    sources_desc += f" ({inherited_count} inherited)"

        # Step 2: Collect all non-src files from last_commit first
        files_to_restore = {}  # file_path -> commit

        result = self.run_git(
            f"ls-tree -r -z --name-only {milestone.last_commit}",
            capture=True,
            check=False,
            quiet=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            for file_path in result.stdout.split("\0"):
                if file_path and not self.src_filter.is_src_file(file_path):
                    files_to_restore[file_path] = milestone.last_commit

        # Step 3: Process integration_test_commits with timestamp comparison
        for int_commit in integration_commits:
            # Get integration_test_commit's original timestamp
            int_commit_original_ts = self._get_original_timestamp(int_commit)

            # Determine if this integration_test_commit should override last_commit's files
            # >= means: same original commit OR newer original commit should override
            should_override = int_commit_original_ts >= last_commit_original_ts

            if not quiet and self.verbose:
                is_split = self.split_mapper.is_split_commit(int_commit)
                override_reason = "will override" if should_override else "will NOT override"
                ts_relation = ">=" if should_override else "<"
                if is_split:
                    info = self.split_mapper.get_split_info(int_commit)
                    print(f"      📋 integration_test_commit {int_commit[:7]} (split from {info.original_short_hash})")
                else:
                    print(f"      📋 integration_test_commit {int_commit[:7]}")
                print(
                    f"         original_ts: {int_commit_original_ts} {ts_relation} {last_commit_original_ts} → {override_reason}"
                )

            # Get non-src files modified by integration_test_commit (not entire tree).
            # Use name-status instead of name-only so deleted files don't poison a
            # batched checkout pathspec. A deletion should remove the file from the
            # expected set when this integration commit wins the timestamp check.
            result = self.run_git(
                f"diff-tree --no-commit-id --name-status -r -M -z {int_commit}",
                capture=True,
                check=False,
                quiet=True,
            )
            if result.returncode != 0 or not result.stdout.strip():
                continue

            tokens = [token for token in result.stdout.split("\0") if token]
            token_index = 0
            while token_index < len(tokens):
                status = tokens[token_index]
                token_index += 1
                if token_index >= len(tokens):
                    continue

                status_code = status[0]

                if status_code in {"R", "C"}:
                    if token_index + 1 >= len(tokens):
                        break
                    old_path = tokens[token_index]
                    file_path = tokens[token_index + 1]
                    token_index += 2
                    if should_override and old_path != file_path and not self.src_filter.is_src_file(old_path):
                        files_to_restore.pop(old_path, None)
                else:
                    file_path = tokens[token_index]
                    token_index += 1

                if not file_path or self.src_filter.is_src_file(file_path):
                    continue

                if status_code == "D":
                    if should_override:
                        files_to_restore.pop(file_path, None)
                    continue

                if file_path not in files_to_restore:
                    # File doesn't exist in last_commit, add it.
                    files_to_restore[file_path] = int_commit
                elif should_override:
                    # File exists, but integration_test_commit's original timestamp >= last_commit's.
                    files_to_restore[file_path] = int_commit
                # else: File exists but integration_test_commit is older, keep last_commit's version

        # Step 4: Log statistics
        if not quiet:
            from_last = sum(1 for c in files_to_restore.values() if c == milestone.last_commit)
            from_int = len(files_to_restore) - from_last
            print(f"      📋 Restoring {len(files_to_restore)} non-src files from {sources_desc}")
            if from_int > 0:
                print(f"         ({from_last} from last_commit, {from_int} from integration_test_commits)")

        # Step 5: Restore files from their designated commit. Batch pathspecs
        # into one git checkout per source commit; doing this file-by-file is
        # catastrophically slow on large repos with thousands of non-src files.
        files_by_commit: Dict[str, List[str]] = defaultdict(list)
        for file_path, commit in files_to_restore.items():
            files_by_commit[commit].append(file_path)

        for commit, file_paths in files_by_commit.items():
            if not file_paths:
                continue

            pathspec_file = None
            try:
                with tempfile.NamedTemporaryFile(prefix="deepcommit_restore_", delete=False) as handle:
                    pathspec_file = Path(handle.name)
                    for file_path in file_paths:
                        handle.write(file_path.encode("utf-8"))
                        handle.write(b"\0")

                result = self.run_git(
                    f"checkout {commit} --pathspec-from-file={shlex.quote(str(pathspec_file))} --pathspec-file-nul",
                    check=False,
                    quiet=True,
                )
                if result.returncode == 0:
                    continue
            finally:
                if pathspec_file:
                    pathspec_file.unlink(missing_ok=True)

            # Fallback for older git versions or rare pathspec failures.
            for file_path in file_paths:
                escaped_path = file_path.replace("'", "'\\''")
                self.run_git(f"checkout {commit} -- '{escaped_path}'", check=False, quiet=True)

        # Step 6: Remove extra non-src files if requested
        if remove_extra:
            self._remove_extra_non_src_files(set(files_to_restore.keys()), quiet)

        self.profiler.record(
            "milestone.restore_non_src_files",
            time.perf_counter() - profile_started,
            {"milestone_id": milestone.id, "files": len(files_to_restore), "remove_extra": remove_extra},
        )

    def _remove_extra_non_src_files(self, expected_files: set, quiet: bool = True) -> None:
        """
        Remove non-src files that exist in working directory but are not in expected set.

        Args:
            expected_files: Set of file paths that should exist
            quiet: Whether to suppress output
        """
        profile_started = time.perf_counter()
        # Get current tracked files
        result = self.run_git("ls-files", capture=True, check=False, quiet=True)
        if result.returncode != 0 or not result.stdout.strip():
            self.profiler.record(
                "milestone.remove_extra_non_src_files",
                time.perf_counter() - profile_started,
                {"removed": 0, "expected_files": len(expected_files)},
            )
            return

        current_files = set(result.stdout.strip().split("\n"))

        # Find non-src files to remove
        removed_count = 0
        for file_path in current_files:
            if not file_path:
                continue
            # Only consider non-src files
            if not self.src_filter.is_src_file(file_path):
                if file_path not in expected_files:
                    escaped_path = file_path.replace("'", "'\\''")
                    self.run_git(f"rm -f '{escaped_path}'", check=False, quiet=True)
                    removed_count += 1

        if not quiet and removed_count > 0:
            print(f"      🗑️  Removed {removed_count} extra non-src files")

        self.profiler.record(
            "milestone.remove_extra_non_src_files",
            time.perf_counter() - profile_started,
            {"removed": removed_count, "expected_files": len(expected_files)},
        )

    def _update_start_state_non_src(self, milestone: MilestoneData) -> None:
        """
        Update START state to have all non-src files from last_commit and integration_test_commits.

        This implements the "start's src + end's non-src" design:
        - START state should have OLD src code (from parent milestones)
        - START state should have NEW non-src files (pom.xml, tests, configs, etc.)

        non-src files include:
        - Files outside repo_src_dirs (root pom.xml, README, .gitignore, etc.)
        - Files matching test_dirs patterns (test code)
        - Files matching exclude_patterns

        For split commits, non-src files come from:
        - last_commit: base files on main branch
        - integration_test_commits: feature-specific files (inherited from parent milestones)

        This method is called AFTER create_end_state() completes to avoid
        breaking cherry-picks. The process:
        1. Checkout the START tag
        2. Restore all non-src files from last_commit and integration_test_commits
        3. Remove extra non-src files not in the expected set
        4. Always amend the commit and force re-tag

        Args:
            milestone: The milestone being processed
        """
        start_tag = f"milestone-{milestone.id}-start"

        # Remember current position (END state)
        current_head_result = self.run_git("rev-parse HEAD", capture=True)
        current_head = current_head_result.stdout.strip()

        print(f"   🔧 Updating {start_tag} with non-src files")

        # Checkout START tag (detached HEAD)
        self.run_git(f"checkout {start_tag}")

        # Restore all non-src files from last_commit and integration_test_commits
        self._restore_non_src_files_for_milestone(milestone, quiet=False, remove_extra=True)

        # Stage all changes
        self.run_git("add .")

        # Always amend the commit (no diff check - ensures consistent state)
        commit_msg = f"Start state for {milestone.id}"
        self.run_git(f'commit --amend --allow-empty -m "{commit_msg}"', check=False)

        # Force update the tag
        self.run_git(f"tag -f {start_tag}")
        print(f"   ✅ Updated {start_tag}")

        # Return to the previous position (END state)
        self.run_git(f"checkout {current_head}")

    def _get_src_pathspec(self) -> str:
        """Get git pathspec to include only source directories."""
        # e.g., "-- src/" or "-- packages/markitdown/src/markitdown/"
        return "-- " + " ".join(self.src_dirs)

    def _apply_commit_as_patch(self, commit: str) -> bool:
        """
        Try to apply a commit as a patch when cherry-pick fails.

        This is a fallback mechanism for commits that can't be cherry-picked
        due to conflicts (e.g., commits from feature branches).

        Only applies changes to src files (filtered by src_filter).
        Non-src file changes are ignored since they'll be replaced by snapshots.

        Args:
            commit: The commit SHA to apply

        Returns:
            True if patch was applied successfully (or no src changes), False otherwise
        """
        import shlex
        import tempfile

        # Get list of files changed in this commit
        files_result = self.run_git(f"show {commit} --name-only --format=", capture=True, quiet=True)
        if files_result.returncode != 0:
            print(f"      ❌ Failed to get file list for {commit}")
            return False

        changed_files = [f.strip() for f in files_result.stdout.strip().split("\n") if f.strip()]

        # Filter to only src files using src_filter
        src_files = [f for f in changed_files if self.src_filter.is_src_file(f)]

        if not src_files:
            # No src file changes in this commit, skip
            print(f"      ℹ️  No src file changes in {commit[:7]}, skipping patch")
            return True

        # Log what we're filtering
        non_src_files = [f for f in changed_files if not self.src_filter.is_src_file(f)]
        if non_src_files:
            print(f"      ℹ️  Filtering out {len(non_src_files)} non-src files from patch")

        # Generate patch for only src files
        # Use -- to specify paths, with proper escaping for shell
        src_paths = " ".join(shlex.quote(f) for f in src_files)
        format_patch_started = time.perf_counter()
        patch_result = subprocess.run(
            f"git format-patch -1 {commit} --stdout -- {src_paths}",
            shell=True,
            cwd=self.work_repo,
            capture_output=True,
            text=True,
        )
        self.profiler.record(
            "git.format_patch_fallback",
            time.perf_counter() - format_patch_started,
            {"commit": commit[:12], "src_files": len(src_files)},
        )

        if patch_result.returncode != 0:
            print(f"      ❌ Failed to generate patch for {commit}")
            return False

        if not patch_result.stdout.strip():
            # Empty patch (might happen if all changes were filtered out)
            print("      ℹ️  Empty patch after filtering, skipping")
            return True

        # Write patch to temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
            f.write(patch_result.stdout)
            patch_file = f.name

        try:
            # Try to apply the patch with 3-way merge
            apply_result = self.run_git(f"apply --3way {patch_file}", check=False)

            if apply_result.returncode == 0:
                return True

            # If --3way failed, try without it (but with --index to stage changes)
            apply_result = self.run_git(f"apply --index {patch_file}", check=False)

            if apply_result.returncode == 0:
                return True

            # Last resort: try with --reject to see what fails
            apply_result = self.run_git(f"apply --reject {patch_file}", check=False)

            # Check if there are .rej files (only count src file rejects)
            import glob

            rej_files = glob.glob(str(self.work_repo / "**/*.rej"), recursive=True)
            if rej_files:
                # Convert reject file paths to original file paths and filter to src files
                work_repo_str = str(self.work_repo) + "/"
                src_rej_files = []
                for rej in rej_files:
                    # Remove .rej suffix and work_repo prefix to get relative path
                    rel_path = rej.replace(work_repo_str, "").replace(".rej", "")
                    if self.src_filter.is_src_file(rel_path):
                        src_rej_files.append(rej)

                if src_rej_files:
                    print(f"      ❌ Patch partially applied, {len(src_rej_files)} src reject files:")
                    for rej in src_rej_files[:3]:
                        print(f"         - {rej}")

                # Stash .rej content for diagnostics BEFORE cleanup (used by
                # --continue-on-error mode to surface hunk details in build_report.json).
                self._last_rej_hunks = []
                for rej in src_rej_files:
                    rel_path = rej.replace(work_repo_str, "").replace(".rej", "")
                    try:
                        content = Path(rej).read_text(encoding="utf-8", errors="replace")
                    except Exception as e:
                        self._last_rej_hunks.append({"file": rel_path, "error": f"read failed: {e}"})
                        continue
                    lines = content.splitlines()
                    preview = "\n".join(lines[:80])
                    if len(preview) > 6000:
                        preview = preview[:6000] + "\n... [truncated]"
                    self._last_rej_hunks.append(
                        {
                            "file": rel_path,
                            "hunk_count_hint": sum(1 for ln in lines if ln.startswith("@@")),
                            "preview": preview,
                        }
                    )

                # Clean up all reject files
                for rej in rej_files:
                    Path(rej).unlink()

                # Only fail if there are src file rejects
                return len(src_rej_files) == 0

            return apply_result.returncode == 0

        finally:
            # Clean up temp file
            Path(patch_file).unlink(missing_ok=True)

    def _get_main_ref(self) -> str:
        """Get the actual ref for main branch (local or origin/)."""
        # Try local branch first
        result = subprocess.run(
            f"git rev-parse --verify {self.main_branch}",
            shell=True,
            cwd=self.work_repo,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return self.main_branch
        # Try origin/branch
        origin_ref = f"origin/{self.main_branch}"
        result = subprocess.run(
            f"git rev-parse --verify {origin_ref}",
            shell=True,
            cwd=self.work_repo,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return origin_ref
        # Fallback to original
        return self.main_branch

    def _is_tag_on_main(self, tag: str) -> bool:
        """Check if a tag is on the main branch (is an ancestor of main)."""
        main_ref = self._get_main_ref()
        result = subprocess.run(
            f"git merge-base --is-ancestor {tag} {main_ref}",
            shell=True,
            cwd=self.work_repo,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _get_merge_base(self, tag: str) -> str:
        """Get the merge-base between a tag and main branch."""
        main_ref = self._get_main_ref()
        result = subprocess.run(
            f"git merge-base {tag} {main_ref}",
            shell=True,
            cwd=self.work_repo,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"❌ Failed to find merge-base between {tag} and {main_ref}")
            sys.exit(1)
        return result.stdout.strip()

    def _resolve_base_commit(self) -> str:
        """
        Resolve the actual base commit to use.

        If base_tag is on main branch, use it directly.
        If base_tag is on a release branch (not on main), use merge-base instead.

        Returns:
            The commit SHA to use as the base for building testbed.
        """
        if self._is_tag_on_main(self.base_tag):
            print(f"✅ Base tag '{self.base_tag}' is on {self.main_branch} branch")
            # Get the commit SHA for the tag
            result = self.run_git(f"rev-parse {self.base_tag}", capture=True)
            return result.stdout.strip()
        else:
            # Tag is on a release branch, use merge-base
            merge_base = self._get_merge_base(self.base_tag)
            merge_base_short = merge_base[:10]  # noqa: F841

            # Get commit info for display
            result = self.run_git(f"log --oneline -1 {merge_base}", capture=True)
            commit_info = result.stdout.strip()

            print(f"⚠️  Base tag '{self.base_tag}' is NOT on {self.main_branch} branch (likely on a release branch)")
            print(f"   Using merge-base instead: {commit_info}")
            print("   This ensures all milestone commits can be cherry-picked cleanly.")

            return merge_base

    def run_git(
        self, cmd: str, check: bool = True, capture: bool = False, quiet: bool = False
    ) -> subprocess.CompletedProcess:
        """Run a git command in the work repository."""
        full_cmd = f"git {cmd}"
        if self.verbose:
            print(f"  $ {full_cmd}")

        # If quiet=True, redirect stdout and stderr to /dev/null (unless capture=True)
        started = time.perf_counter()
        if quiet and not capture:
            result = subprocess.run(
                full_cmd,
                shell=True,
                cwd=self.work_repo,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        else:
            result = subprocess.run(full_cmd, shell=True, cwd=self.work_repo, capture_output=capture, text=True)
        self.profiler.record_git(cmd, time.perf_counter() - started)

        if check and result.returncode != 0:
            print(f"❌ Git command failed: {full_cmd}")
            if capture:
                print(f"Stderr: {result.stderr}")
            sys.exit(1)

        return result

    def setup_work_repository(self) -> bool:
        """
        Clone source repository to work directory.

        Returns:
            True if repository was newly created/recreated, False if using existing one
        """
        profile_started = time.perf_counter()
        print("📦 Setting up work repository...")

        try:
            if self.work_dir.exists():
                # Check if it's a valid git repository
                git_dir = self.work_dir / ".git"
                is_git_repo = git_dir.exists()

                if is_git_repo and not self.force_rebuild:
                    print(f"✅ Work directory already exists: {self.work_dir}")
                    print("   Using existing testbed (use --force-rebuild to recreate)")
                    self.work_repo = self.work_dir

                    # Verify base tag/commit exists
                    result = self.run_git(f"rev-parse --verify {self.base_tag}", capture=True, check=False)
                    if result.returncode != 0:
                        print(f"❌ Base ref '{self.base_tag}' not found in existing repository")
                        sys.exit(1)

                    return False  # Using existing repository

                # Force recreation or invalid git repo
                if self.force_rebuild:
                    print(f"⚠️  Forcing recreation of work directory: {self.work_dir}")
                else:
                    print(f"⚠️  Work directory exists but is not a valid git repository: {self.work_dir}")
                    response = input("Delete and recreate? (yes/no): ")
                    if response.lower() != "yes":
                        print("Aborted.")
                        sys.exit(1)

                rm_started = time.perf_counter()
                subprocess.run(f"rm -rf {self.work_dir}", shell=True, check=True)
                self.profiler.record(
                    "setup.rm_work_dir",
                    time.perf_counter() - rm_started,
                    {"work_dir": str(self.work_dir)},
                )

            # Determine clone source: prefer trial_dir/repo_data if available (contains split commits)
            clone_source = self.repo_path
            if self.commit_splits_dir and self.commit_splits_dir.exists():
                # Infer trial_dir/repo_data from commit_splits_dir
                # commit_splits_dir: .../trial_space/{trial_name}/stage4_decomp/output/commit_splits
                # repo_data: .../trial_space/{trial_name}/repo_data
                try:
                    # Go up 3 levels: commit_splits -> output -> stage4_decomp -> trial_dir
                    trial_dir = self.commit_splits_dir.parent.parent.parent
                    trial_repo_data = trial_dir / "repo_data"
                    if trial_repo_data.exists():
                        # Resolve symlink if it is one
                        trial_repo_data = trial_repo_data.resolve()
                        # Verify it's a valid git repo
                        git_dir = trial_repo_data / ".git"
                        if git_dir.exists():
                            clone_source = trial_repo_data
                            print("📦 Using trial_dir/repo_data as clone source (contains split commits)")
                except Exception as e:
                    if self.verbose:
                        print(f"   ⚠️  Could not infer trial_dir/repo_data: {e}")

            # Clone repository
            print(f"Cloning {clone_source} to {self.work_dir}...")
            clone_started = time.perf_counter()
            subprocess.run(f"git clone {clone_source} {self.work_dir}", shell=True, check=True)
            self.profiler.record(
                "setup.git_clone",
                time.perf_counter() - clone_started,
                {"clone_source": str(clone_source), "work_dir": str(self.work_dir)},
            )

            self.work_repo = self.work_dir

            # Verify base tag/commit exists
            result = self.run_git(f"rev-parse --verify {self.base_tag}", capture=True, check=False)
            if result.returncode != 0:
                print(f"❌ Base ref '{self.base_tag}' not found in repository")
                sys.exit(1)

            # Enable directory rename detection
            self.run_git("config merge.directoryRenames true")
            if self.verbose:
                print("   ✓ Enabled merge.directoryRenames")

            print(f"✅ Work repository ready at {self.work_dir}")
            return True  # Newly created repository
        finally:
            self.profiler.record(
                "setup_work_repository",
                time.perf_counter() - profile_started,
                {"force_rebuild": self.force_rebuild},
            )

    def apply_base_prerequisites(
        self,
        commits_to_apply: List[str],
        description: str = "base prerequisites",
    ) -> str:
        """
        Apply prerequisite commits onto base to create an enhanced base state.

        This method cherry-picks a list of commits onto the actual_base_commit,
        creating a new "enhanced base" that subsequent milestones will build upon.

        Args:
            commits_to_apply: List of commit hashes to cherry-pick in order
            description: Description for logging purposes

        Returns:
            New base commit SHA after applying all prerequisites
        """
        if not commits_to_apply:
            print(f"   ⚠️  No commits to apply for {description}")
            return self.actual_base_commit

        print(f"\n🔧 Applying {description} ({len(commits_to_apply)} commits)...")

        # Checkout actual base commit
        self.run_git(f"checkout {self.actual_base_commit}")

        # Create a temporary branch for the enhanced base
        enhanced_base_branch = "enhanced-base-temp"
        self.run_git(f"checkout -b {enhanced_base_branch}", check=False)

        # Cherry-pick each commit in order
        failed_commits = []
        for i, commit in enumerate(commits_to_apply, 1):
            short_sha = commit[:7] if len(commit) >= 7 else commit
            print(f"   [{i}/{len(commits_to_apply)}] Cherry-picking {short_sha}...")

            # Try cherry-pick with rename detection
            result = self.run_git(
                f"cherry-pick {commit} --strategy=ort -X rename-threshold={self.rename_threshold} -X patience",
                check=False,
            )

            if result.returncode != 0:
                # Check for conflicts
                src_conflicts, non_src_conflicts = self.detect_conflicts(debug=self.verbose)

                if src_conflicts:
                    print("      ❌ Conflict in src files:")
                    for f in src_conflicts[:5]:
                        print(f"         - {f}")
                    if len(src_conflicts) > 5:
                        print(f"         ... and {len(src_conflicts) - 5} more")

                    # Try to resolve by accepting theirs for src conflicts
                    # This is safe for base prerequisites since we're building on top of base
                    print("      🔄 Attempting to resolve by accepting incoming changes...")
                    for f in src_conflicts:
                        self.run_git(f"checkout --theirs -- '{f}'", check=False, quiet=True)

                if non_src_conflicts:
                    print(f"      ⚠️  Auto-resolving {len(non_src_conflicts)} non-src conflicts")
                    for f in non_src_conflicts:
                        self.run_git(f"checkout --theirs -- '{f}'", check=False, quiet=True)

                # Stage all changes and continue
                self.run_git("add .", quiet=True)

                # Check if there are still unresolved conflicts
                status_result = self.run_git("status --porcelain", capture=True, quiet=True)
                if status_result.stdout and any(
                    line.startswith("UU ") or line.startswith("AA ") or line.startswith("DD ")
                    for line in status_result.stdout.strip().split("\n")
                ):
                    print(f"      ❌ Failed to resolve conflicts for {short_sha}")
                    failed_commits.append(commit)
                    self.run_git("cherry-pick --abort", check=False, quiet=True)
                    continue

                # Complete the cherry-pick
                result = self.run_git(
                    f'commit --no-edit -m "Base prerequisite: {short_sha}"',
                    check=False,
                    quiet=True,
                )
                if result.returncode != 0:
                    # Maybe nothing to commit (empty commit)
                    self.run_git("cherry-pick --skip", check=False, quiet=True)
                    print(f"      ℹ️  Skipped {short_sha} (no changes)")
                else:
                    print(f"      ✓ Applied {short_sha} (with conflict resolution)")
            else:
                print(f"      ✓ Applied {short_sha}")

        if failed_commits:
            print(f"\n⚠️  Warning: {len(failed_commits)} commits failed to apply:")
            for c in failed_commits:
                print(f"   - {c[:7]}")
            print("   Continuing with partial base prerequisites...")

        # Get the new base commit SHA
        result = self.run_git("rev-parse HEAD", capture=True)
        new_base_sha = result.stdout.strip()

        # Create a tag for the enhanced base state
        enhanced_base_tag = "enhanced-base-state"
        self.run_git(f"tag -f {enhanced_base_tag}", check=False)

        print(f"✅ Enhanced base created: {new_base_sha[:7]}")
        print(f"   Tag: {enhanced_base_tag}")

        return new_base_sha

    def _topo_cache_path(self) -> Path:
        """Path to the topological-sort cache file (sibling of metadata.json)."""
        return self.work_dir.parent / "topo_sort_cache.json"

    @staticmethod
    def _raw_commits_hash(commits: List[str]) -> str:
        """Order-independent hash of a commits list. Cache key for topo sort."""
        canonical = ";".join(sorted(c.strip() for c in commits if c.strip()))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def _load_topo_cache(self) -> Dict[str, Dict]:
        """Load topo-sort cache. Returns {milestone_id: {raw_hash, sorted_commits}}."""
        path = self._topo_cache_path()
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") != 1:
                return {}
            return data.get("entries", {})
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_topo_cache(self, cache: Dict[str, Dict]) -> None:
        """Write cache atomically."""
        path = self._topo_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entries": cache}, f, indent=2)
        tmp.replace(path)

    def parse_milestones(self, csv_path: Path) -> Dict[str, MilestoneData]:
        """Parse milestones CSV file and apply milestone commit ordering."""
        profile_started = time.perf_counter()
        print(f"📖 Parsing milestones from {csv_path}...")
        milestones = {}

        with open(csv_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                milestone = MilestoneData(row)
                milestones[milestone.id] = milestone

        # Sort commits by topological order and update milestone.commits and last_commit
        if self.preserve_commit_order:
            print("📋 Preserving commit order from CSV (--preserve-commit-order enabled)")
            for m_id, milestone in milestones.items():
                if milestone.commits:
                    # Keep original order from CSV, just set last_commit
                    milestone.last_commit = milestone.commits[-1]
                    if self.verbose:
                        print(
                            f"   {m_id}: {len(milestone.commits)} commits (order preserved), last_commit={milestone.last_commit[:7]}"
                        )
        else:
            cache = self._load_topo_cache()
            hits = 0
            misses = 0
            cache_dirty = False
            print(f"🔄 Sorting commits by topological order... (cache: {len(cache)} entries)")
            for m_id, milestone in milestones.items():
                if not milestone.commits:
                    continue
                raw_hash = self._raw_commits_hash(milestone.commits)
                entry = cache.get(m_id)
                if entry and entry.get("raw_commits_hash") == raw_hash:
                    # Cache hit — reuse sorted order
                    sorted_commits = entry["sorted_commits"]
                    # Defensive: cached list must have same commit set as input
                    if set(sorted_commits) == set(milestone.commits):
                        milestone.commits = list(sorted_commits)
                        milestone.last_commit = milestone.commits[-1]
                        hits += 1
                        if self.verbose:
                            print(
                                f"   {m_id}: {len(milestone.commits)} commits (cached), last_commit={milestone.last_commit[:7]}"
                            )
                        continue
                    # Hash matched but content drifted — treat as miss and refresh
                # Cache miss — run topo sort
                sorted_commits = self.sort_commits_by_topo_order(milestone.commits)
                milestone.commits = sorted_commits
                milestone.last_commit = sorted_commits[-1]
                cache[m_id] = {
                    "raw_commits_hash": raw_hash,
                    "sorted_commits": list(sorted_commits),
                }
                cache_dirty = True
                misses += 1
                if self.verbose:
                    print(f"   {m_id}: {len(sorted_commits)} commits sorted, last_commit={milestone.last_commit[:7]}")
            # Drop stale entries for milestones no longer in CSV
            stale = [mid for mid in cache.keys() if mid not in milestones]
            for mid in stale:
                cache.pop(mid, None)
                cache_dirty = True
            if cache_dirty:
                self._save_topo_cache(cache)
            print(f"   Topo cache: {hits} hits, {misses} misses, {len(stale)} stale entries dropped")

        print(f"✅ Loaded {len(milestones)} milestones")
        self.profiler.record(
            "parse_milestones",
            time.perf_counter() - profile_started,
            {"milestones": len(milestones)},
        )
        return milestones

    def parse_dependencies(self, csv_path: Path) -> Dict[str, List[str]]:
        """
        Parse dependencies CSV file.

        Returns:
            Dict mapping milestone_id -> list of direct parent milestone_ids

        Note on CSV format:
            prerequisite_id,dependant_id means "target depends on source"
            (see rationale column in CSV, e.g., "M003.2 depends on M003.1" for row "M003.1,M003.2")
            So source must be completed before target, i.e., source is parent of target.
        """
        print(f"📖 Parsing dependencies from {csv_path}...")
        deps = defaultdict(list)

        with open(csv_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                source = row["prerequisite_id"]
                target = row["dependant_id"]
                # target depends on source, so source is a parent of target
                # source must be completed before target
                deps[target].append(source)

        print(f"✅ Loaded dependencies for {len(deps)} milestones")
        return dict(deps)

    def topological_sort(self, milestones: Dict[str, MilestoneData], dependencies: Dict[str, List[str]]) -> List[str]:
        """
        Perform stable topological sort on milestones.

        Returns list of milestone IDs in order they should be created.
        Uses milestone ID as tie-breaker for stability.
        """
        profile_started = time.perf_counter()
        print("🔄 Computing topological sort...")

        # Build in-degree count
        in_degree = {m_id: 0 for m_id in milestones}
        for m_id, parents in dependencies.items():
            in_degree[m_id] = len(parents)

        # Find roots (no dependencies)
        queue = sorted([m_id for m_id, deg in in_degree.items() if deg == 0])
        result = []

        # Build reverse dependency graph (children of each milestone)
        children = defaultdict(list)
        for m_id, parents in dependencies.items():
            for parent in parents:
                children[parent].append(m_id)

        while queue:
            # Process in sorted order for stability
            current = queue.pop(0)
            result.append(current)

            # Reduce in-degree of children
            for child in children.get(current, []):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

            # Keep queue sorted
            queue.sort()

        # Check for cycles
        if len(result) != len(milestones):
            print("❌ Circular dependency detected!")
            remaining = set(milestones.keys()) - set(result)
            print(f"Unprocessed milestones: {remaining}")
            sys.exit(1)

        print(f"✅ Topological order: {' → '.join(result)}")
        self.profiler.record(
            "topological_sort",
            time.perf_counter() - profile_started,
            {"milestones": len(milestones), "edges": sum(len(v) for v in dependencies.values())},
        )
        return result

    def detect_conflicts(self, debug: bool = False) -> Tuple[List[str], List[str]]:
        """
        Detect conflicted files and separate into src/ and non-src/.

        Returns:
            (src_conflicts, non_src_conflicts)
        """
        # Use git status to detect unmerged files (more reliable than diff --diff-filter=U)
        result = self.run_git("status --porcelain", check=False, capture=True)

        if debug:
            print("   DEBUG: git status --porcelain output:")
            for line in result.stdout.strip().split("\n")[:20]:
                print(f"      {line}")

        if not result.stdout.strip():
            return [], []

        # Parse unmerged files from git status
        # Unmerged files have status codes: UU, AA, DD, AU, UA, DU, UD
        unmerged_prefixes = ["UU", "AA", "DD", "AU", "UA", "DU", "UD"]
        conflicted = []

        for line in result.stdout.strip().split("\n"):
            if len(line) >= 3:
                status = line[:2]
                if status in unmerged_prefixes:
                    filepath = line[3:].strip()
                    conflicted.append(filepath)

        if debug and conflicted:
            print(f"   DEBUG: Detected {len(conflicted)} unmerged files:")
            for f in conflicted[:10]:
                print(f"      - {f}")
            if len(conflicted) > 10:
                print(f"      ... and {len(conflicted) - 10} more")

        src_conflicts = [f for f in conflicted if self.src_filter.is_src_file(f)]
        non_src_conflicts = [f for f in conflicted if not self.src_filter.is_src_file(f)]

        return src_conflicts, non_src_conflicts

    def _get_commit_timestamp(self, commit: str) -> int:
        """Get the committer timestamp of a commit as Unix epoch."""
        result = self.run_git(f"log -1 --format=%ct {commit}", capture=True, check=False, quiet=True)
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
        return 0

    def _get_main_topo_index(self) -> Dict[str, int]:
        """Build/cache an index mapping every commit reachable from main_branch to
        its position in `git log <main> --topo-order --reverse`. One git call per
        session. Used to sort milestone commits without the O(n²) merge-base storm.
        """
        if getattr(self, "_main_topo_index_cached", None) is not None:
            return self._main_topo_index_cached
        result = self.run_git(
            f"log {self.main_branch} --topo-order --reverse --format=%H",
            capture=True,
            check=False,
            quiet=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            # Fallback: empty index; callers must use legacy path
            self._main_topo_index_cached = {}
            return self._main_topo_index_cached
        all_commits = result.stdout.strip().split("\n")
        self._main_topo_index_cached = {sha: i for i, sha in enumerate(all_commits)}
        if self.verbose:
            print(f"   Built main-branch topo index: {len(self._main_topo_index_cached)} commits")
        return self._main_topo_index_cached

    def sort_commits_by_topo_order(self, commits: List[str]) -> List[str]:
        """
        Sort commits by git's topological order using ancestry relationships.

        Fast path (typical): all commits reachable from main → sort by main-branch
        topo index (1 shared git call regardless of milestone count).
        Legacy path (rare): any commit not on main → fall back to O(n²)
        pairwise `merge-base --is-ancestor`.

        Args:
            commits: List of commit SHAs

        Returns:
            List of commit SHAs sorted in topological order (ancestors first)
        """
        profile_started = time.perf_counter()
        if not commits:
            self.profiler.record("sort_commits_by_topo_order", time.perf_counter() - profile_started, {"commits": 0})
            return []
        if len(commits) == 1:
            self.profiler.record("sort_commits_by_topo_order", time.perf_counter() - profile_started, {"commits": 1})
            return list(commits)

        # Resolve all commits to full SHAs
        full_shas = []
        short_to_full = {}
        for c in commits:
            result = self.run_git(f"rev-parse {c}", capture=True, check=False, quiet=True)
            if result.returncode == 0:
                full_sha = result.stdout.strip()
                full_shas.append(full_sha)
                short_to_full[c] = full_sha
            else:
                full_shas.append(c)
                short_to_full[c] = c

        # Fast path: all commits present in main's topo index
        main_index = self._get_main_topo_index()
        if main_index and all(sha in main_index for sha in full_shas):
            sort_keys = [self.split_mapper.get_sort_key(c, self._get_commit_timestamp) for c in commits]
            # Sort by (main topo position, split tiebreak). Main position dominates
            # for any ancestor relationship; tiebreak resolves sibling determinism.
            paired = sorted(
                zip(full_shas, commits, sort_keys, strict=False),
                key=lambda t: (main_index[t[0]], t[2]),
            )
            # Preserve original short-SHA shape when available
            result_out = []
            for full_sha, orig, _ in paired:
                result_out.append(orig if orig in short_to_full else full_sha[:7])
            self.profiler.record(
                "sort_commits_by_topo_order",
                time.perf_counter() - profile_started,
                {"commits": len(commits), "mode": "main_index"},
            )
            return result_out

        # Legacy path: O(n²) — only kicks in if some commit isn't on main
        if self.verbose:
            off_main = [s for s in full_shas if s not in main_index]
            print(f"   ⚠️  {len(off_main)} commit(s) off main branch; falling back to pairwise merge-base")

        sort_keys = [self.split_mapper.get_sort_key(c, self._get_commit_timestamp) for c in commits]
        n = len(full_shas)
        in_degree = [0] * n
        adjacency = [[] for _ in range(n)]

        for i in range(n):
            for j in range(n):
                if i != j:
                    result = self.run_git(
                        f"merge-base --is-ancestor {full_shas[i]} {full_shas[j]}", capture=True, check=False, quiet=True
                    )
                    if result.returncode == 0:
                        adjacency[i].append(j)
                        in_degree[j] += 1

        # Kahn's algorithm for topological sort
        # Start with commits that have no ancestors in our set
        queue = [i for i in range(n) if in_degree[i] == 0]
        sorted_indices = []

        while queue:
            # For deterministic ordering when multiple commits have same in_degree,
            # sort by original timestamp as tie-breaker (uses original commit timestamp for split commits)
            # This ensures split commits are ordered correctly relative to other commits
            if len(queue) > 1:
                queue.sort(key=lambda i: sort_keys[i])

            idx = queue.pop(0)
            sorted_indices.append(idx)

            for neighbor in adjacency[idx]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # If we couldn't sort all commits (cycle or disconnected), add remaining
        if len(sorted_indices) < n:
            remaining = [i for i in range(n) if i not in sorted_indices]
            # Sort remaining by original timestamp for deterministic ordering
            remaining.sort(key=lambda i: sort_keys[i])
            sorted_indices.extend(remaining)

        # Convert back to original commit SHAs
        full_to_short = {v: k for k, v in short_to_full.items()}
        result = []
        for idx in sorted_indices:
            full_sha = full_shas[idx]
            if full_sha in full_to_short:
                result.append(full_to_short[full_sha])
            else:
                result.append(full_sha[:7])

        self.profiler.record(
            "sort_commits_by_topo_order",
            time.perf_counter() - profile_started,
            {"commits": len(commits), "mode": "pairwise"},
        )
        return result

    # ------------------------------------------------------------------
    # Diagnostic helpers (for --continue-on-error mode)
    # ------------------------------------------------------------------

    def _get_commit_subject(self, commit: str) -> str:
        """Return the commit subject line (best-effort; empty string on failure)."""
        try:
            result = self.run_git(f"log -1 --pretty=%s {commit}", capture=True, check=False, quiet=True)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    def _collect_rej_hunks(self, conflicting_files: List[str]) -> List[dict]:
        """Return .rej hunk previews.

        `_apply_commit_as_patch` stashes previews in `self._last_rej_hunks` right
        before deleting the actual files. We consume that stash here (and reset
        it) so each failure gets the hunks produced by its own apply attempt.

        Falls back to reading live .rej files on disk if the stash is empty
        (e.g., if the failure path wasn't through `_apply_commit_as_patch`).
        """
        if self._last_rej_hunks:
            records = list(self._last_rej_hunks)
            self._last_rej_hunks = []
            return records

        rej_records: List[dict] = []
        for src_file in conflicting_files:
            rej_path = self.work_repo / f"{src_file}.rej"
            if not rej_path.exists():
                continue
            try:
                content = rej_path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                rej_records.append({"file": src_file, "error": f"failed to read .rej: {e}"})
                continue
            lines = content.splitlines()
            preview = "\n".join(lines[:80])
            if len(preview) > 6000:
                preview = preview[:6000] + "\n... [truncated]"
            rej_records.append(
                {
                    "file": src_file,
                    "hunk_count_hint": sum(1 for ln in lines if ln.startswith("@@")),
                    "preview": preview,
                }
            )
        return rej_records

    def _build_commit_to_milestone_map(self, milestones: Dict[str, MilestoneData]) -> Dict[str, str]:
        """Build {commit_sha: milestone_id} lookup (full and short SHAs)."""
        if self._commit_to_milestone is not None:
            return self._commit_to_milestone
        mapping: Dict[str, str] = {}
        for m_id, m in milestones.items():
            for sha in m.commits:
                sha = sha.strip()
                if not sha:
                    continue
                mapping[sha] = m_id
                if len(sha) >= 7:
                    mapping[sha[:7]] = m_id
        self._commit_to_milestone = mapping
        return mapping

    def _get_prior_modifiers(
        self,
        conflicting_files: List[str],
        failing_commit: str,
        milestones: Dict[str, MilestoneData],
    ) -> Dict[str, List[dict]]:
        """For each conflicting file, list commits (in range) that previously modified it.

        Each modifier is annotated with its owning milestone (if any). This is the
        primary signal agents use to propose "add dependency" edges.
        """
        commit_map = self._build_commit_to_milestone_map(milestones)
        base = self.actual_base_commit or self.base_tag
        result: Dict[str, List[dict]] = {}

        for f in conflicting_files:
            modifiers: List[dict] = []
            # %H\x00%s\x00 — full SHA, null, subject, null (resilient to spaces)
            log_result = self.run_git(
                f'log --follow --format=%H%x00%s%x00 {base}..{failing_commit} -- "{f}"',
                capture=True,
                check=False,
                quiet=True,
            )
            if log_result.returncode != 0 or not log_result.stdout.strip():
                result[f] = []
                continue
            for raw in log_result.stdout.strip().split("\n"):
                parts = raw.rstrip("\x00").split("\x00")
                if not parts or not parts[0]:
                    continue
                sha = parts[0]
                subject = parts[1] if len(parts) > 1 else ""
                if sha == failing_commit:
                    continue  # skip the failing commit itself
                # Look up milestone by full or short SHA
                owning_milestone = commit_map.get(sha) or commit_map.get(sha[:7])
                modifiers.append(
                    {
                        "commit": sha,
                        "subject": subject,
                        "milestone_id": owning_milestone,  # None if commit not in any milestone
                    }
                )
            # Limit to top 20 to bound report size
            result[f] = modifiers[:20]
        return result

    def _cleanup_after_failure(self, milestone_id: str) -> None:
        """Reset workspace to a clean state so the next milestone can start fresh.

        NOTE: the milestone's start tag may already exist (created before the end
        state failed). We intentionally leave it alone — `--incremental` mode
        already handles the "start exists, end missing" case by rebuilding.
        """
        # Abort any in-progress cherry-pick / merge
        self.run_git("cherry-pick --abort", check=False, quiet=True)
        self.run_git("merge --abort", check=False, quiet=True)
        # Drop any uncommitted changes
        self.run_git("reset --hard HEAD", check=False, quiet=True)
        # Detach HEAD from the temp branch so we can delete it
        self.run_git("checkout --detach", check=False, quiet=True)
        # Delete the temp branch for this milestone (idempotent)
        temp_branch = f"temp-{milestone_id}-work"
        self.run_git(f"branch -D {temp_branch}", check=False, quiet=True)

    def _cleanup_existing_temp_branch(self, milestone_id: str) -> None:
        """Remove a leftover script-owned temp branch before recreating it.

        Aborted runs can leave `temp-<milestone>-work` behind. Creating the same
        branch again would fail, so clean only that exact script-owned branch.
        """
        temp_branch = f"temp-{milestone_id}-work"
        exists = self.run_git(
            f"rev-parse --verify refs/heads/{temp_branch}",
            capture=True,
            check=False,
            quiet=True,
        )
        if exists.returncode != 0:
            return

        print(f"   ⚠️  Removing leftover temp branch: {temp_branch}")
        current = self.run_git("branch --show-current", capture=True, check=False, quiet=True)
        current_branch = current.stdout.strip() if current.returncode == 0 else ""

        if current_branch == temp_branch:
            # The temp branch is script-owned. If an aborted run left us on it,
            # get back to a deletable detached state before removing it.
            self.run_git("cherry-pick --abort", check=False, quiet=True)
            self.run_git("merge --abort", check=False, quiet=True)
            self.run_git("reset --hard HEAD", check=False, quiet=True)
            self.run_git("checkout --detach", check=False, quiet=True)

        self.run_git(f"branch -D {temp_branch}", check=False, quiet=True)

    def create_start_state(self, milestone: MilestoneData, parents: List[str]) -> str:
        """
        Create start state tag for a milestone.

        Args:
            milestone: Milestone data
            parents: List of parent milestone IDs (direct dependencies)

        Returns:
            Commit SHA of the start state
        """
        tag_name = f"milestone-{milestone.id}-start"
        temp_branch = f"temp-{milestone.id}-work"
        print(f"\n Creating start state: {tag_name}")
        print(f"   Parents: {parents if parents else 'none (root)'}")

        self._cleanup_existing_temp_branch(milestone.id)

        if not parents:
            # No dependencies - start from actual base commit (may be merge-base if tag is on release branch)
            self.run_git(f"checkout {self.actual_base_commit}")
            self.run_git(f"checkout -b {temp_branch}")

        elif len(parents) == 1:
            # Single parent - checkout from parent's end tag
            parent_tag = f"milestone-{parents[0]}-end"
            self.run_git(f"checkout {parent_tag}")
            self.run_git(f"checkout -b {temp_branch}")

        else:
            # Multiple parents - need to merge
            print(f"   Merging {len(parents)} parents...")

            # Start from first parent
            first_parent_tag = f"milestone-{parents[0]}-end"
            self.run_git(f"checkout {first_parent_tag}")
            self.run_git(f"checkout -b {temp_branch}")

            # Merge remaining parents one by one
            # IMPORTANT: Each merge must be committed before the next one can proceed
            for i, parent in enumerate(parents[1:], start=2):
                parent_tag = f"milestone-{parent}-end"
                print(f"   Merging {parent_tag}...")

                result = self.run_git(
                    f"merge {parent_tag} --no-commit --no-ff --strategy=ort -X rename-threshold={self.rename_threshold} -X patience",
                    check=False,
                )

                if result.returncode != 0:
                    # Check for conflicts (enable debug for verbose output)
                    src_conflicts, non_src_conflicts = self.detect_conflicts(debug=self.verbose)

                    if src_conflicts:
                        # src/ conflicts - cannot continue
                        print("❌ ERROR: Conflicts detected in src/ directory:")
                        for f in src_conflicts:
                            print(f"     - {f}")

                        # Show all conflicts for debugging
                        if non_src_conflicts:
                            print(f"\n   Also {len(non_src_conflicts)} non-src conflicts:")
                            for f in non_src_conflicts[:5]:
                                print(f"     - {f}")
                            if len(non_src_conflicts) > 5:
                                print(f"     ... and {len(non_src_conflicts) - 5} more")

                        # Abort merge
                        self.run_git("merge --abort", check=False)

                        if self.continue_on_error:
                            raise MilestoneBuildFailure(
                                milestone_id=milestone.id,
                                stage="start",
                                failing_commit=parent_tag,  # which parent's merge caused it
                                failing_files_src=src_conflicts,
                                error_msg=f"multi-parent merge conflict at {parent_tag}",
                            )
                        print("\n   This indicates a problem in milestone dependencies.")
                        print("   Please manually resolve or fix milestone definitions.")
                        sys.exit(1)

                    if non_src_conflicts:
                        print(f"   ⚠️  Auto-resolving {len(non_src_conflicts)} non-src conflicts")
                        print("       (will be overwritten by snapshot anyway)")
                        # For non-src conflicts, accept "theirs" version (will be replaced by snapshot later)
                        for f in non_src_conflicts:
                            self.run_git(f"checkout --theirs -- '{f}'", check=False, quiet=True)
                        self.run_git("add .", quiet=True)

                # If there are more parents to merge, we need to commit this merge first
                # Otherwise git will refuse to start a new merge
                if i < len(parents):
                    self.run_git("add .", quiet=True)
                    self.run_git(
                        f"commit -m 'Intermediate merge: {parent}' --allow-empty",
                        check=False,
                        quiet=True,
                    )

        # Replace non-src/ files with milestone's snapshot
        if milestone.last_commit:
            exclude_pathspec = self._get_exclude_src_pathspec()
            print(f"   📸 Replacing non-src/ with snapshot from {milestone.last_commit[:7]}")
            self.run_git(
                f"rm -rf . {exclude_pathspec}", check=False, quiet=True
            )  # Force remove all non-src/ files first
            self.run_git(f"checkout {milestone.last_commit} -- . {exclude_pathspec}", quiet=True)

            # NOTE: Non-src files are NOT restored here to avoid breaking cherry-picks in create_end_state().
            # If we restore non-src files from last_commit here (with remove_extra=True), it can delete
            # files that will be modified by later commits, causing modify/delete conflicts.
            #
            # Instead, non-src file restoration happens AFTER create_end_state() completes - see
            # _update_start_state_non_src() which is called at the end of create_end_state().

        # Check if there are still any conflicts (should only be src/)
        src_conflicts, _ = self.detect_conflicts()
        if src_conflicts:
            print("❌ ERROR: Unexpected src/ conflicts remain:")
            for f in src_conflicts:
                print(f"     - {f}")
            self.run_git("merge --abort", check=False)
            sys.exit(1)

        # Commit start state
        self.run_git("add .")

        # Always create a commit for START state, even if no file changes.
        # This ensures START has its own commit with correct message, which is
        # important for _update_start_state_non_src() to amend later.
        # Without this, if no changes are detected, the tag would point to
        # parent's END commit with wrong message (e.g., "End state for M003.2"
        # instead of "Start state for M003.3").
        parent_list = ";".join(parents) if parents else "none"
        commit_msg = f"Start state for {milestone.id}\n\nParents: {parent_list}"

        result = self.run_git("diff --cached --quiet", check=False)
        if result.returncode == 0:
            # No file changes, but still create commit for proper tagging
            print("   ℹ️  No file changes, creating commit for proper tagging")
            self.run_git(f'commit --allow-empty -m "{commit_msg}"')
        else:
            # Commit changes
            self.run_git(f'commit -m "{commit_msg}"')

        # Get commit SHA
        result = self.run_git("rev-parse HEAD", capture=True)
        commit_sha = result.stdout.strip()

        # Create tag at current commit
        self.run_git(f"tag {tag_name}")

        print(f"✅ Start state created: {tag_name} ({commit_sha[:7]})")
        return commit_sha

    def create_end_state(self, milestone: MilestoneData) -> str:
        """
        Create end state tag for a milestone.

        Uses cherry-pick with rename detection to apply commits on top of start state.

        Args:
            milestone: Milestone data (commits are already in build order)

        Returns:
            Commit SHA of the end state
        """
        end_tag = f"milestone-{milestone.id}-end"
        temp_branch = f"temp-{milestone.id}-work"

        print(f"\n🎯 Creating end state: {end_tag}")
        print(f"   Cherry-picking {len(milestone.commits)} commits with rename detection")

        # Continue from current temp branch (already at start state)
        # No need to checkout again, we're already on temp_branch from create_start_state

        # Cherry-pick each commit with rename detection
        for i, commit in enumerate(milestone.commits, 1):
            print(f"   [{i}/{len(milestone.commits)}] Cherry-picking {commit[:7]}...")

            # Check if this is a merge commit
            parents_result = self.run_git(f"rev-list --parents -n 1 {commit}", capture=True, check=False)

            if parents_result.returncode == 0:
                parent_count = len(parents_result.stdout.strip().split()) - 1
            else:
                parent_count = 1  # Assume normal commit if check fails

            # Build cherry-pick command with advanced rename detection
            if parent_count > 1:
                # Merge commit - use first parent
                cherry_pick_cmd = f"cherry-pick {commit} --no-commit --strategy=ort -X rename-threshold={self.rename_threshold} -X patience -m 1"
                if i == 1 or self.verbose:  # Only print once to reduce noise
                    print("      ℹ️  Merge commit, using first parent")
            else:
                # Normal commit
                cherry_pick_cmd = f"cherry-pick {commit} --no-commit --strategy=ort -X rename-threshold={self.rename_threshold} -X patience"

            # Execute cherry-pick
            result = self.run_git(cherry_pick_cmd, check=False)

            if result.returncode != 0:
                # Cherry-pick failed - check for conflicts
                src_conflicts, non_src_conflicts = self.detect_conflicts()

                if src_conflicts:
                    # src/ conflicts - try fallback with git format-patch + git apply
                    print("      ⚠️  Cherry-pick conflict in src/, trying patch fallback...")
                    self.run_git("cherry-pick --abort", check=False)

                    # Try to apply the commit as a patch
                    patch_applied = self._apply_commit_as_patch(commit)

                    if not patch_applied:
                        print(f"❌ ERROR: Both cherry-pick and patch apply failed for {commit}")
                        print("   Conflicting files:")
                        for f in src_conflicts:
                            print(f"     - {f}")

                        if self.continue_on_error:
                            # Collect rej hunk info + commit subject for diagnostic report.
                            # .rej files were deleted inside _apply_commit_as_patch, but
                            # their content was stashed in self._last_rej_hunks first.
                            rej_hunks = self._collect_rej_hunks(src_conflicts)
                            subject = self._get_commit_subject(commit)
                            # Abort and clean up; raise to be handled by create_all_branches loop
                            self.run_git("cherry-pick --abort", check=False, quiet=True)
                            raise MilestoneBuildFailure(
                                milestone_id=milestone.id,
                                stage="end",
                                failing_commit=commit,
                                failing_commit_subject=subject,
                                failing_files_src=src_conflicts,
                                rej_hunks=rej_hunks,
                                error_msg="cherry-pick + patch fallback both failed",
                                commit_index_in_milestone=i,
                                total_commits_in_milestone=len(milestone.commits),
                                prior_applied_commits=list(milestone.commits[: i - 1]),
                            )
                        print(f"\n   This indicates incompatible changes in {commit}")
                        print("   Please manually resolve or fix milestone definitions.")
                        sys.exit(1)
                    else:
                        print("      ✅ Patch applied successfully")

                if non_src_conflicts:
                    print(f"      ⚠️  Non-src/ conflicts ({len(non_src_conflicts)} files)")
                    print("          (will be overwritten by snapshot)")
                    # Resolve non-src conflicts by accepting theirs (will be replaced by snapshot later)
                    # This is critical to allow subsequent cherry-picks to proceed
                    for f in non_src_conflicts:
                        self.run_git(f"checkout --theirs -- '{f}'", check=False, quiet=True)
                    self.run_git("add .", quiet=True)

            # Restore non-src/ files from milestone's snapshot
            if milestone.last_commit:
                exclude_pathspec = self._get_exclude_src_pathspec()
                # Only print detailed message for first commit
                if i == 1:
                    print(f"   📸 Restoring non-src/ from {milestone.last_commit[:7]} after each commit")

                self.run_git(
                    f"rm -rf . {exclude_pathspec}", check=False, quiet=True
                )  # Force remove all non-src/ files first
                self.run_git(f"checkout {milestone.last_commit} -- . {exclude_pathspec}", check=False, quiet=True)

                # NOTE: Test files are NOT restored here during cherry-picks.
                # Let cherry-picks handle test file changes naturally.
                # Test cleanup happens at the end after all cherry-picks are done.

            # Clean up cherry-pick state if it exists
            cherry_pick_head = self.work_repo / ".git" / "CHERRY_PICK_HEAD"
            if cherry_pick_head.exists():
                # Quit cherry-pick but keep changes
                self.run_git("cherry-pick --quit", check=False)

        # Final cleanup: restore all non-src files from last_commit and integration_test_commits
        # This ensures END state has the same non-src files as START state will have
        # non-src files include: pom.xml, tests, configs, README, etc.
        # Using remove_extra=True also removes non-src files not in the expected set
        self._restore_non_src_files_for_milestone(milestone, quiet=False, remove_extra=True)

        # Commit end state
        self.run_git("add .")

        # Check if there are changes
        result = self.run_git("diff --cached --quiet", check=False)
        if result.returncode == 0:
            print("   ℹ️  No changes from commits (milestone has no src/ changes)")
        else:
            commit_msg = f"End state for {milestone.id}\n\nCherry-picked {len(milestone.commits)} commits (build order)"
            self.run_git(f'commit -m "{commit_msg}"')

        # Get commit SHA
        result = self.run_git("rev-parse HEAD", capture=True)
        commit_sha = result.stdout.strip()

        # Create tag at current commit
        self.run_git(f"tag {end_tag}")

        # Now update START state to have non-src files from last_commit and integration_test_commits
        # This implements "start's src + end's non-src" design
        # We do this AFTER END is created to avoid breaking cherry-picks
        self._update_start_state_non_src(milestone)

        # Delete temporary branch (optional, for cleanup)
        self.run_git("checkout --detach", check=False)  # Detach from temp branch first
        self.run_git(f"branch -D {temp_branch}", check=False)

        print(f"✅ End state created: {end_tag} ({commit_sha[:7]})")
        return commit_sha

    def create_all_branches(
        self,
        milestones: Dict[str, MilestoneData],
        dependencies: Dict[str, List[str]],
        incremental: bool = False,
        existing_metadata: Optional[Dict] = None,
        milestone_csv_path: Optional[Path] = None,
        dependencies_csv_path: Optional[Path] = None,
    ) -> Tuple[List[Dict], List[str]]:
        """
        Create all milestone branches in topological order.

        Note: milestones.commits are already ordered by parse_milestones()

        Args:
            milestones: Dict of milestone_id -> MilestoneData
            dependencies: Dict of milestone_id -> list of parent milestone_ids
            incremental: If True, skip milestones with existing valid tags
            existing_metadata: Existing metadata.json content for validation
            milestone_csv_path: Path to milestones.csv (for incremental metadata save)
            dependencies_csv_path: Path to dependencies.csv (for incremental metadata save)

        Returns:
            Tuple of (results, topo_order):
            - results: List of result records for output CSV
            - topo_order: Topological order of milestone IDs
        """
        profile_started = time.perf_counter()
        # Store milestones and dependencies for use by other methods
        # (e.g., for inheriting integration_test_commit from parents)
        self._milestones = milestones
        self._dependencies = dependencies

        # Compute creation order
        topo_order = self.topological_sort(milestones, dependencies)

        results = []
        skipped_count = 0
        created_count = 0
        failed_count = 0
        skipped_due_to_parent_count = 0

        print(f"\n{'=' * 60}")
        print(f"Creating tags for {len(milestones)} milestones")
        if incremental:
            print("Mode: INCREMENTAL (will skip existing valid tags)")
        if self.continue_on_error:
            print("Mode: CONTINUE-ON-ERROR (failures recorded to build_report.json)")
        print(f"{'=' * 60}")

        for i, m_id in enumerate(topo_order, 1):
            milestone = milestones[m_id]
            parents = dependencies.get(m_id, [])
            tag_start = f"milestone-{m_id}-start"
            tag_end = f"milestone-{m_id}-end"

            # --continue-on-error: if any parent failed earlier, skip this milestone
            if self.continue_on_error and self._failed_milestones:
                blocking_parents = [p for p in parents if p in self._failed_milestones]
                if blocking_parents:
                    print(
                        f"\n[{i}/{len(topo_order)}] ⏭️  Skipping {m_id}: parent(s) failed: {', '.join(blocking_parents)}"
                    )
                    self._failed_milestones.add(m_id)
                    self._milestone_statuses[m_id] = {
                        "status": "skipped_due_to_parent",
                        "blocking_parents": blocking_parents,
                        "parents": parents,
                    }
                    skipped_due_to_parent_count += 1
                    continue

            # Incremental mode: check if we can skip this milestone
            if incremental:
                start_exists = self.verify_tag_exists(tag_start)
                end_exists = self.verify_tag_exists(tag_end)

                if start_exists and end_exists:
                    # Both tags exist, validate consistency
                    commits_str = ";".join(milestone.commits)
                    is_valid, reason = self.validate_existing_milestone(m_id, commits_str, parents, existing_metadata)

                    if not is_valid:
                        print(f"\n❌ [{i}/{len(topo_order)}] {m_id}: existing tags failed incremental validation!")
                        print(f"   Reason: {reason}")
                        print("   Use --force-rebuild to recreate all tags")
                        sys.exit(1)

                    # Valid, skip this milestone
                    print(f"\n[{i}/{len(topo_order)}] Skipping {m_id} (tags exist, {reason})")
                    start_sha = self.run_git(f"rev-parse {tag_start}", capture=True).stdout.strip()
                    end_sha = self.run_git(f"rev-parse {tag_end}", capture=True).stdout.strip()
                    results.append(
                        {
                            "milestone_id": m_id,
                            "tag_name_start": tag_start,
                            "commit_sha_start": start_sha,
                            "tag_name_end": tag_end,
                            "commit_sha_end": end_sha,
                            "parent_milestones": ";".join(parents) if parents else "",
                        }
                    )
                    self._milestone_statuses[m_id] = {
                        "status": "success_cached",
                        "start_sha": start_sha,
                        "end_sha": end_sha,
                        "parents": parents,
                    }
                    skipped_count += 1
                    continue

                elif start_exists and not end_exists:
                    # Partial state (interrupted build), need to rebuild
                    print(f"\n[{i}/{len(topo_order)}] Rebuilding {m_id} (incomplete: start exists, end missing)")
                    # Delete the start tag to rebuild cleanly
                    self.run_git(f"tag -d {tag_start}", check=False)

            print(f"\n[{i}/{len(topo_order)}] Processing {m_id}: {milestone.title}")

            try:
                # Create start state
                start_profile = time.perf_counter()
                try:
                    start_sha = self.create_start_state(milestone, parents)
                finally:
                    self.profiler.record(
                        "milestone.create_start_state",
                        time.perf_counter() - start_profile,
                        {"milestone_id": m_id, "parents": len(parents), "commits": len(milestone.commits)},
                    )

                # Create end state (this also calls _update_start_state_non_src which may amend start commit)
                end_profile = time.perf_counter()
                try:
                    end_sha = self.create_end_state(milestone)
                finally:
                    self.profiler.record(
                        "milestone.create_end_state",
                        time.perf_counter() - end_profile,
                        {"milestone_id": m_id, "commits": len(milestone.commits)},
                    )
            except MilestoneBuildFailure as e:
                # Only reachable in --continue-on-error mode (otherwise sys.exit was hit)
                failed_count += 1
                self._failed_milestones.add(m_id)

                # P0: find prior commits in range that previously modified the conflicting
                # files -> suggests candidate "add dependency" edges for the agent.
                prior_modifiers: Dict[str, List[dict]] = {}
                if e.stage == "end" and e.failing_files_src and e.failing_commit:
                    try:
                        prior_modifiers = self._get_prior_modifiers(e.failing_files_src, e.failing_commit, milestones)
                    except Exception as ex:
                        prior_modifiers = {"_error": [{"msg": f"prior_modifiers lookup failed: {ex}"}]}

                self._milestone_statuses[m_id] = {
                    "status": "conflict",
                    "stage": e.stage,
                    "failing_commit": e.failing_commit,
                    "failing_commit_subject": e.failing_commit_subject,
                    "failing_files_src": e.failing_files_src,
                    "rej_hunks": e.rej_hunks,
                    "error": e.error_msg,
                    "parents": parents,
                    # P1: split decision signals
                    "commit_index_in_milestone": e.commit_index_in_milestone,
                    "total_commits_in_milestone": e.total_commits_in_milestone,
                    "prior_applied_commits": e.prior_applied_commits,
                    # P0: add-edge decision signal
                    "prior_modifiers": prior_modifiers,
                }
                self._cleanup_after_failure(m_id)
                print(
                    f"\n❌ [{i}/{len(topo_order)}] {m_id} failed at {e.stage} stage "
                    f"(commit {e.failing_commit[:8] if e.failing_commit else '?'}); "
                    f"continuing to next milestone"
                )
                continue

            # Re-read start_sha from tag after _update_start_state_non_src() may have amended it
            start_tag = f"milestone-{m_id}-start"
            result = self.run_git(f"rev-parse {start_tag}", capture=True)
            start_sha = result.stdout.strip()

            # Record result
            result = {
                "milestone_id": m_id,
                "tag_name_start": tag_start,
                "commit_sha_start": start_sha,
                "tag_name_end": tag_end,
                "commit_sha_end": end_sha,
                "parent_milestones": ";".join(parents) if parents else "",
            }
            results.append(result)
            created_count += 1
            self._milestone_statuses[m_id] = {
                "status": "success",
                "start_sha": start_sha,
                "end_sha": end_sha,
                "parents": parents,
                "num_commits": len(milestone.commits),
            }

            # Incremental mode: save metadata after each successful milestone
            if incremental and milestone_csv_path and dependencies_csv_path:
                self._save_milestone_to_metadata(
                    m_id, milestone, result, parents, milestone_csv_path, dependencies_csv_path
                )
                print("   💾 Saved to metadata.json")

        if incremental:
            print(f"\n📊 Summary: {skipped_count} skipped, {created_count} created")
        if self.continue_on_error and (failed_count or skipped_due_to_parent_count):
            print(
                f"⚠️  {failed_count} failed, {skipped_due_to_parent_count} skipped due to parent failures "
                f"(see build_report.json for diagnostics)"
            )

        self.profiler.record(
            "create_all_branches",
            time.perf_counter() - profile_started,
            {
                "milestones": len(milestones),
                "created": created_count,
                "skipped": skipped_count,
                "failed": failed_count,
                "skipped_due_to_parent": skipped_due_to_parent_count,
            },
        )
        return results, topo_order

    def verify_tag_exists(self, tag: str) -> bool:
        """Check if a tag exists."""
        result = self.run_git(f"rev-parse --verify {tag}", capture=True, check=False)
        return result.returncode == 0

    def validate_existing_milestone(
        self,
        m_id: str,
        commits_str: str,
        parents: List[str],
        existing_metadata: Optional[Dict],
    ) -> Tuple[bool, str]:
        """校验已存在的 milestone 与 CSV 是否一致。

        Args:
            m_id: Milestone ID
            commits_str: 分号分隔的 commit hash 字符串 (从 CSV)
            parents: 父 milestone ID 列表 (从 dependencies CSV)
            existing_metadata: 已存在的 metadata.json 内容

        Returns:
            Tuple of (is_valid, reason)
            - is_valid: True 表示一致，False 表示不一致
            - reason: 校验结果说明
        """
        if existing_metadata is None:
            return False, "metadata.json missing; cannot validate existing tags"

        # 查找这个 milestone 在 metadata 中的记录
        milestones_list = existing_metadata.get("milestones", [])
        if not isinstance(milestones_list, list):
            return False, "metadata.json has invalid or missing milestones list"

        existing_entry = None
        for m in milestones_list:
            if m.get("id") == m_id:
                existing_entry = m
                break

        if existing_entry is None:
            return False, f"{m_id} missing from metadata.json; cannot validate existing tags"

        # 检查是否有 hash 字段
        existing_commits_hash = existing_entry.get("commits_hash")
        existing_parents_hash = existing_entry.get("parents_hash")

        if existing_commits_hash is None or existing_parents_hash is None:
            return False, f"{m_id} metadata missing commits_hash/parents_hash; cannot validate existing tags"

        # 计算当前 CSV 的 hash
        current_commits_hash, current_parents_hash = compute_milestone_hash(commits_str, parents)

        # 比较
        if existing_commits_hash != current_commits_hash:
            return False, f"commits changed: {existing_commits_hash} -> {current_commits_hash}"
        if existing_parents_hash != current_parents_hash:
            return False, f"parents changed: {existing_parents_hash} -> {current_parents_hash}"

        return True, "validated"

    def _create_base_metadata(self, milestone_csv_path: Path, dependencies_csv_path: Path) -> Dict:
        """创建基础 metadata 结构（不含 milestones 列表）。"""
        # Extract repo name from repo_path
        repo_name = self.repo_path.name
        if "_" in repo_name:
            parts = repo_name.split("_", 1)
            repo_full_name = f"{parts[0]}/{parts[1]}"
        else:
            repo_full_name = f"<owner/{repo_name}>"

        return {
            "repo_name": repo_full_name,
            "repo_path": str(self.repo_path.absolute()),
            "milestone_csv_source": str(milestone_csv_path.absolute()),
            "milestone_csv_copy": str((self.work_dir.parent / "milestones.csv").absolute()),
            "dependencies_csv_source": str(dependencies_csv_path.absolute()),
            "dependencies_csv_copy": str((self.work_dir.parent / "dependencies.csv").absolute()),
            "generated_time": datetime.now().isoformat(),
            "total_milestones": 0,  # Will be updated
            "total_commits": 0,  # Will be updated
            "base_tag": self.base_tag,
            "main_branch": self.main_branch,
            "rename_threshold": self.rename_threshold,
            "repo_src_dirs": self.src_dirs,
            "test_dirs": self.test_dirs,
            "exclude_patterns": self.exclude_patterns,
            "pytest_timeout": self.pytest_timeout,
            "pytest_workers": self.pytest_workers,
            "docker_cpus": self.docker_cpus,
            "generator_version": "branch_creator-1.1-incremental",
            "topological_order": {
                "full_order": [],  # Will be updated
                "independent_milestones": [],  # Will be updated
            },
            "milestones": [],
        }

    def _save_milestone_to_metadata(
        self,
        m_id: str,
        milestone: "MilestoneData",
        result: Dict,
        parents: List[str],
        milestone_csv_path: Path,
        dependencies_csv_path: Path,
    ) -> None:
        """增量更新 metadata.json，保存单个 milestone 的信息。

        Args:
            m_id: Milestone ID
            milestone: MilestoneData 对象
            result: 包含 tag 信息的结果字典
            parents: 父 milestone ID 列表
            milestone_csv_path: milestones.csv 路径
            dependencies_csv_path: dependencies.csv 路径
        """
        metadata_file = self.work_dir.parent / "metadata.json"

        # 加载现有 metadata 或创建新的
        if metadata_file.exists():
            with open(metadata_file, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        else:
            metadata = self._create_base_metadata(milestone_csv_path, dependencies_csv_path)

        # 计算 hash
        commits_str = ";".join(milestone.commits)
        commits_hash, parents_hash = compute_milestone_hash(commits_str, parents)

        # 获取 base_commit
        base_commit = ""
        if milestone.commits:
            first_commit = milestone.commits[0]
            git_result = self.run_git(f"rev-parse {first_commit}^", capture=True, check=False)
            if git_result.returncode == 0:
                base_commit = git_result.stdout.strip()

        # 构建 milestone entry
        milestone_entry = {
            "id": m_id,
            "title": milestone.title,
            "category": milestone.category,
            "commits_count": len(milestone.commits),
            "commits": commits_str,
            "commits_hash": commits_hash,
            "parents_hash": parents_hash,
            "base_commit": base_commit,
            "tag_name_start": result["tag_name_start"],
            "commit_sha_start": result["commit_sha_start"],
            "tag_name_end": result["tag_name_end"],
            "commit_sha_end": result["commit_sha_end"],
            "parent_milestones": parents,
        }

        # 更新 milestones 列表（替换或追加）
        existing_ids = {m["id"] for m in metadata.get("milestones", [])}
        if m_id in existing_ids:
            metadata["milestones"] = [milestone_entry if m["id"] == m_id else m for m in metadata["milestones"]]
        else:
            metadata.setdefault("milestones", []).append(milestone_entry)

        # 更新统计信息
        metadata["total_milestones"] = len(metadata["milestones"])
        metadata["total_commits"] = sum(m.get("commits_count", 0) for m in metadata["milestones"])
        metadata["generated_time"] = datetime.now().isoformat()

        # 保存
        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

    def _verify_non_src_matches(
        self,
        milestone: MilestoneData,
        exclude_diff_pathspec: str,
        overriding_commits: List[str],
        overriding_files: Set[str],
    ) -> bool:
        """
        Verify that non-src files in the current checkout match expected state.

        For milestones with overriding integration_test_commits:
        - Files from overriding_commits should match those commits
        - Other non-src files should match last_commit

        Args:
            milestone: The milestone being verified
            exclude_diff_pathspec: Git pathspec to exclude src directories
            overriding_commits: List of integration_test_commits that should override
            overriding_files: Set of non-src file paths modified by overriding commits

        Returns:
            True if all non-src files match expected state
        """
        if not overriding_commits:
            # Simple case: no overriding commits, just compare with last_commit
            diff_output = self.run_git(f"diff {milestone.last_commit} -- {exclude_diff_pathspec}", capture=True).stdout
            return len(diff_output) == 0

        # Complex case: check overriding files match integration_test_commits
        all_matches = True

        for f in overriding_files:
            result = self.run_git(f"ls-files -- {f}", capture=True)
            if result.stdout.strip():
                # File exists, find which overriding commit has this file (last one wins)
                source_commit = None
                for int_commit in overriding_commits:
                    check = self.run_git(f"ls-tree {int_commit} -- {f}", capture=True)
                    if check.stdout.strip():
                        source_commit = int_commit

                if source_commit:
                    diff_output = self.run_git(f"diff {source_commit} -- {f}", capture=True).stdout
                    if diff_output:
                        all_matches = False
                        break

        # Check remaining non-src files match last_commit
        if all_matches and overriding_files:
            overriding_pathspec = " ".join(f"':!{f}'" for f in overriding_files)
            diff_output = self.run_git(
                f"diff {milestone.last_commit} -- {exclude_diff_pathspec} {overriding_pathspec}",
                capture=True,
            ).stdout
            if diff_output:
                all_matches = False
        elif all_matches:
            # No overriding files from integration_test_commits (they only have src files)
            diff_output = self.run_git(f"diff {milestone.last_commit} -- {exclude_diff_pathspec}", capture=True).stdout
            all_matches = len(diff_output) == 0

        return all_matches

    def verify_milestone_tags(self, milestone: MilestoneData, tag_start: str, tag_end: str) -> Dict[str, bool]:
        """
        Verify a single milestone's tags.

        Returns dict of check_name -> passed
        """
        results = {}

        # Check 1: Tags exist
        results["start_tag_exists"] = self.verify_tag_exists(tag_start)
        results["end_tag_exists"] = self.verify_tag_exists(tag_end)

        if not (results["start_tag_exists"] and results["end_tag_exists"]):
            return results

        # Check 2: Start and end are different (unless milestone has no src/ changes)
        start_sha = self.run_git(f"rev-parse {tag_start}", capture=True).stdout.strip()
        end_sha = self.run_git(f"rev-parse {tag_end}", capture=True).stdout.strip()
        # Note: They could be same if milestone has no src/ changes
        results["start_end_different"] = start_sha != end_sha

        if not milestone.last_commit:
            # No commits, skip remaining checks
            return results

        # Check 3 & 4: Non-src/ files match expected state
        # Build exclude pathspec for diff: ':!src/' or ':!packages/markitdown/src/markitdown/'
        exclude_diff_pathspec = " ".join(f"':!{src_dir}'" for src_dir in self.src_dirs)

        # Collect integration_test_commits that should override (using new sub-milestone logic)
        integration_commits = self._get_applicable_itcs_for_sub_milestone(milestone)
        # Note: _get_applicable_itcs_for_sub_milestone already filters by timestamp,
        # so all returned ITCs are applicable (no additional filtering needed)
        overriding_commits = integration_commits

        # Get non-src files modified by overriding commits
        overriding_files: Set[str] = set()
        for int_commit in overriding_commits:
            files_output = self.run_git(f"show {int_commit} --name-only --format=''", capture=True).stdout.strip()
            for f in files_output.split("\n"):
                f = f.strip()
                if f and not self.src_filter.is_src_file(f):
                    overriding_files.add(f)

        # Check 3: Non-src/ files in start match expected state
        self.run_git(f"checkout {tag_start}", check=False)
        results["start_non_src_matches"] = self._verify_non_src_matches(
            milestone, exclude_diff_pathspec, overriding_commits, overriding_files
        )

        # Check 4: Non-src/ files in end match expected state
        self.run_git(f"checkout {tag_end}", check=False)
        results["end_non_src_matches"] = self._verify_non_src_matches(
            milestone, exclude_diff_pathspec, overriding_commits, overriding_files
        )

        # Check 5: End has src/ changes from start (or is same if no changes)
        src_pathspec = self._get_src_pathspec()
        diff_output = self.run_git(f"diff {tag_start} {tag_end} {src_pathspec}", capture=True).stdout
        # Could be empty if milestone has no src/ changes
        results["has_src_diff"] = len(diff_output) > 0

        return results

    def verify_all_tags(self, milestones: Dict[str, MilestoneData]) -> bool:
        """
        Verify all created milestone tags.

        Returns True if all verifications pass, False otherwise.
        """
        print(f"\n{'=' * 60}")
        print(f"🔍 Verifying {len(milestones)} milestones...")
        print(f"{'=' * 60}")

        all_passed = True
        failed_milestones = []

        for m_id, milestone in milestones.items():
            tag_start = f"milestone-{m_id}-start"
            tag_end = f"milestone-{m_id}-end"

            if not milestone.commits:
                continue

            results = self.verify_milestone_tags(milestone, tag_start, tag_end)

            # Check if any verification failed
            milestone_failed = False
            failed_checks = []
            for check, passed in results.items():
                if not passed:
                    failed_checks.append(check)
                    milestone_failed = True
                    all_passed = False

            # Only print if there are failures
            if milestone_failed:
                print(f"\n❌ {m_id}: {milestone.title}")
                for check in failed_checks:
                    print(f"  ❌ {check}")
                failed_milestones.append(m_id)

        print(f"\n{'=' * 60}")
        if all_passed:
            print("✅ All verifications passed!")
        else:
            print(f"❌ Verification failed for {len(failed_milestones)} milestone(s): {', '.join(failed_milestones)}")

        return all_passed

    def generate_milestone_patches(
        self,
        milestones: Dict[str, MilestoneData],
        output_dir: Path,
        repo_name: Optional[str] = None,
    ) -> None:
        """
        Generate patch files for each milestone (diff between start and end tags).

        Args:
            milestones: Dictionary of milestone data
            output_dir: Output directory (will create milestone_patches subdirectory)
            repo_name: Workspace mode only: write ``milestone_patches/{MID}/{repo_name}.patch``
        """
        profile_started = time.perf_counter()
        patches_dir = output_dir / "milestone_patches"
        patches_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n{'=' * 60}")
        print("📝 Generating milestone patches...")
        print(f"{'=' * 60}")

        for m_id in milestones:
            tag_start = f"milestone-{m_id}-start"
            tag_end = f"milestone-{m_id}-end"
            if repo_name:
                (patches_dir / m_id).mkdir(parents=True, exist_ok=True)
                patch_file = patches_dir / m_id / f"{repo_name}.patch"
            else:
                patch_file = patches_dir / f"{m_id}.patch"

            print(f"   Generating patch for {m_id}...")
            patch_started = time.perf_counter()

            # Generate diff between start and end tags
            result = self.run_git(f"diff {tag_start} {tag_end}", capture=True, check=False)

            # Write patch to file
            with open(patch_file, "w") as f:
                f.write(result.stdout)

            # Get stats for verbose output
            if self.verbose:
                stats_result = self.run_git(f"diff --stat {tag_start} {tag_end}", capture=True, check=False)
                print(f"      ✅ {patch_file.name}")
                if stats_result.stdout:
                    for line in stats_result.stdout.strip().split("\n"):
                        print(f"         {line}")
            else:
                print(f"      ✅ {patch_file.name}")
            self.profiler.record(
                "milestone.generate_patch",
                time.perf_counter() - patch_started,
                {"milestone_id": m_id, "bytes": len(result.stdout)},
            )

        print(f"\n✅ Generated {len(milestones)} patch files in: {patches_dir}")
        self.profiler.record(
            "generate_milestone_patches",
            time.perf_counter() - profile_started,
            {"milestones": len(milestones)},
        )

    def generate_metadata(
        self,
        milestones: Dict[str, MilestoneData],
        dependencies: Dict[str, List[str]],
        topo_order: List[str],
        results: List[Dict],
        milestone_csv_path: Path,
        dependencies_csv_path: Path,
        output_dir: Path,
        base_milestones: Optional[List[str]] = None,
        base_commits: Optional[List[str]] = None,
    ) -> Dict:
        """
        Generate metadata.json with topological order and milestone branches info.

        Reuses the topological sort result from create_all_branches.
        """
        profile_started = time.perf_counter()
        # Extract repo name from repo_path
        repo_name = self.repo_path.name
        if "_" in repo_name:
            parts = repo_name.split("_", 1)
            repo_full_name = f"{parts[0]}/{parts[1]}"
        else:
            repo_full_name = f"<owner/{repo_name}>"

        # Build milestone info dict for easy lookup
        # Note: commits are already in build order from parse_milestones().
        milestone_info = {}
        for m_id, milestone in milestones.items():
            milestone_info[m_id] = {
                "title": milestone.title,
                "category": milestone.category,
                "commits_count": len(milestone.commits),
                "commits": ";".join(milestone.commits),
            }

        # Identify nodes in dependency graph vs independent nodes
        nodes_in_graph = set()
        for prerequisite_id, dependant_ids in dependencies.items():
            nodes_in_graph.add(prerequisite_id)
            nodes_in_graph.update(dependant_ids)

        independent_milestones = sorted([m for m in milestones.keys() if m not in nodes_in_graph])

        # Merge milestone_branches and milestones info
        # Build a dict from results for easy lookup
        branches_dict = {}
        for result in results:
            m_id = result["milestone_id"]
            # Convert semicolon-separated string to list
            parent_str = result["parent_milestones"]
            branches_dict[m_id] = {
                "tag_name_start": result["tag_name_start"],
                "commit_sha_start": result["commit_sha_start"],
                "tag_name_end": result["tag_name_end"],
                "commit_sha_end": result["commit_sha_end"],
                "parent_milestones": parent_str.split(";") if parent_str else [],
            }

        # Merge milestone info with branch info in topological order
        merged_milestones = []
        for m_id in topo_order:
            # Get base_commit: parent of the first commit in build order.
            commits_str = milestone_info[m_id]["commits"]
            base_commit = ""
            if commits_str:
                first_commit = commits_str.split(";")[0]
                # Get parent of first commit
                result = self.run_git(f"rev-parse {first_commit}^", capture=True, check=False)
                if result.returncode == 0:
                    base_commit = result.stdout.strip()

            merged = {
                "id": m_id,
                "title": milestone_info[m_id]["title"],
                "category": milestone_info[m_id]["category"],
                "commits_count": milestone_info[m_id]["commits_count"],
                "commits": milestone_info[m_id]["commits"],
                "base_commit": base_commit,
                "tag_name_start": branches_dict[m_id]["tag_name_start"],
                "commit_sha_start": branches_dict[m_id]["commit_sha_start"],
                "tag_name_end": branches_dict[m_id]["tag_name_end"],
                "commit_sha_end": branches_dict[m_id]["commit_sha_end"],
                "parent_milestones": branches_dict[m_id]["parent_milestones"],
            }
            merged_milestones.append(merged)

        # Build metadata
        total_commits = sum(info["commits_count"] for info in milestone_info.values())

        metadata = {
            "repo_name": repo_full_name,
            "repo_path": str(self.repo_path.absolute()),
            "milestone_csv_source": str(milestone_csv_path.absolute()),
            "milestone_csv_copy": str((output_dir / "milestones.csv").absolute()),
            "dependencies_csv_source": str(dependencies_csv_path.absolute()),
            "dependencies_csv_copy": str((output_dir / "dependencies.csv").absolute()),
            "generated_time": datetime.now().isoformat(),
            "total_milestones": len(milestones),
            "total_commits": total_commits,
            "base_tag": self.base_tag,
            "main_branch": self.main_branch,
            "rename_threshold": self.rename_threshold,
            "repo_src_dirs": self.src_dirs,
            "test_dirs": self.test_dirs,
            "exclude_patterns": self.exclude_patterns,
            "pytest_timeout": self.pytest_timeout,
            "pytest_workers": self.pytest_workers,
            "docker_cpus": self.docker_cpus,
            "generator_version": "branch_creator-1.2",
            "topological_order": {
                "full_order": topo_order,
                "independent_milestones": independent_milestones,
            },
            "milestones": merged_milestones,
            "generated_patterns": self.generated_patterns,
            "modifiable_test_patterns": self.modifiable_test_patterns,
        }

        # Add base prerequisites info if provided
        if base_milestones or base_commits:
            metadata["base_prerequisites"] = {
                "milestones": base_milestones or [],
                "commits": base_commits or [],
                "enhanced_base_commit": self.actual_base_commit,
            }

        self.profiler.record(
            "generate_metadata",
            time.perf_counter() - profile_started,
            {"milestones": len(milestones), "total_commits": total_commits},
        )
        return metadata


def load_config(config_path: Path) -> dict:
    """Load configuration from a YAML file."""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _write_build_report(
    *,
    output_dir: Path,
    creator: "BranchCreator",
    topo_order: List[str],
    milestones: Dict[str, "MilestoneData"],
    dependencies: Dict[str, List[str]],
    continue_on_error: bool,
) -> Path:
    """Write a structured build_report.json capturing per-milestone outcomes.

    This file is always emitted (success or failure) so downstream agents can
    plan DAG edits without re-parsing stdout. For each milestone it records
    status (success / success_cached / conflict / skipped_due_to_parent /
    not_attempted) plus failure diagnostics (failing commit, conflicting src
    files, .rej previews) when applicable.
    """
    statuses = dict(creator._milestone_statuses)  # copy
    # Any milestone in topo_order without a status entry was not attempted
    # (e.g., script exited before reaching it in non-continue-on-error mode).
    for m_id in topo_order:
        if m_id not in statuses:
            statuses[m_id] = {
                "status": "not_attempted",
                "parents": dependencies.get(m_id, []),
            }

    # Attach basic per-milestone CSV info (title/category/commits) for context
    for m_id, record in statuses.items():
        if m_id in milestones:
            m = milestones[m_id]
            record.setdefault("title", m.title)
            record.setdefault("category", m.category)
            record.setdefault("num_commits_csv", len(m.commits))

    summary_counts: Dict[str, int] = {}
    for record in statuses.values():
        summary_counts[record["status"]] = summary_counts.get(record["status"], 0) + 1

    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mode": {
            "continue_on_error": continue_on_error,
        },
        "total_milestones": len(topo_order),
        "topological_order": topo_order,
        "summary": summary_counts,
        "failed_milestones": sorted(creator._failed_milestones),
        "milestones": {m_id: statuses[m_id] for m_id in topo_order},
    }

    report_path = output_dir / "build_report.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # Short summary to stdout
    print(f"\n📝 build_report.json → {report_path}")
    status_line = ", ".join(f"{k}={v}" for k, v in sorted(summary_counts.items()))
    print(f"   {status_line}")

    # Archive this run into testbed_runs/run_NNN/ for iteration history.
    # Each run captures the build_report plus a snapshot of the input CSVs at
    # that moment, so M3 can read prior iterations without grepping ad-hoc
    # backup filenames.
    _archive_testbed_run(output_dir=output_dir, report_path=report_path)

    return report_path


def _archive_testbed_run(*, output_dir: Path, report_path: Path) -> Optional[Path]:
    """Copy build_report.json + input CSVs into testbed_runs/run_NNN/.

    Returns the archive directory path, or None if anything goes wrong (archive
    failure must never break the main build).
    """
    try:
        runs_root = output_dir / "_testbed_runs"
        runs_root.mkdir(exist_ok=True)

        existing = [
            int(p.name[len("run_") :])
            for p in runs_root.glob("run_*")
            if p.is_dir() and p.name[len("run_") :].isdigit()
        ]
        next_n = (max(existing) + 1) if existing else 1
        run_dir = runs_root / f"run_{next_n:03d}"
        run_dir.mkdir()

        shutil.copy2(report_path, run_dir / "build_report.json")
        for name in ("milestones.csv", "dependencies.csv"):
            src = output_dir / name
            if src.exists():
                shutil.copy2(src, run_dir / name)

        print(f"   📦 archived → _testbed_runs/run_{next_n:03d}/")
        return run_dir
    except Exception as exc:  # noqa: BLE001
        print(f"   ⚠️  testbed_runs archive failed (non-fatal): {exc}")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Create milestone testbed tags and milestone patches from milestones.csv/dependencies.csv"
    )

    parser.add_argument("--milestone-csv", type=Path, default=None, help="Path to milestones CSV file")

    parser.add_argument("--dependencies-csv", type=Path, default=None, help="Path to dependencies CSV file")

    parser.add_argument(
        "--trial",
        type=str,
        default=None,
        help="Trial name or path. Auto-resolves milestones.csv, dependencies.csv, and output-dir. "
        "Searches DATA/trial_space/ for matching directory. "
        "Example: --trial django_django_4.2_5.2_v1_002_refactor_1",
    )

    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to YAML config file (e.g., config/<owner_repo>_<start>_<end>.yaml). "
        "If provided, repo_src_dirs, test_dirs, exclude, and main_branch will be loaded from this file.",
    )

    parser.add_argument(
        "--repo-path",
        type=Path,
        default=Path("DATA/github_data/repos/urllib3_urllib3"),
        help="Path to source repository. In harness workspaces, prefer passing --repo-path source_repo explicitly. "
        "If left at the default placeholder, the script may try to auto-infer from milestone-csv/config path.",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("DATA/harness_workspace/urllib3-docker/test_multi_stage"),
        help="Output directory for metadata.json, copied CSV files, and milestone_patches/. "
        "In harness workspaces, prefer passing --output-dir . explicitly. "
        "If left at the default placeholder, the script may try to auto-infer from milestone-csv path.",
    )

    parser.add_argument(
        "--work-dir",
        type=str,
        default="testbed",
        help="Work directory name (subdirectory under output-dir for cloned repo, default: testbed)",
    )

    parser.add_argument(
        "--base-tag",
        default="2.0.6",
        help="Base tag to start from. In automated workflows, prefer passing the start version explicitly. "
        "If left at the default placeholder, the script may try to auto-infer from config/milestone path.",
    )

    parser.add_argument("--verbose", action="store_true", help="Print verbose output including git commands")

    parser.add_argument("--verify", action="store_true", help="Verify created tags after creation")

    parser.add_argument(
        "--rename-threshold",
        type=int,
        default=None,
        help="Git rename detection similarity threshold (0-100). "
        "If not specified, auto-selects based on repo: 50 for huggingface_transformers, 20 for others.",
    )

    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Force recreation of testbed even if it already exists",
    )

    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Skip milestones with existing start/end tags (validates consistency with CSV)",
    )

    parser.add_argument(
        "--repo-src-dirs",
        type=str,
        nargs="+",
        default=None,
        help="Source code directories to track (overrides config file). "
        "For monorepo projects like markitdown, use: --repo-src-dirs packages/markitdown/src/markitdown",
    )

    parser.add_argument(
        "--main-branch",
        type=str,
        default=None,
        help="Name of the main branch (overrides config file). "
        "Used to detect if base_tag is on a release branch and auto-resolve merge-base.",
    )

    parser.add_argument(
        "--test-dirs",
        type=str,
        nargs="+",
        default=None,
        help="Test directory patterns (overrides config file). "
        "For projects with different test locations, use: --test-dirs 'tests/**' '**/*_test.py'",
    )

    parser.add_argument(
        "--exclude",
        type=str,
        nargs="+",
        default=None,
        help="Exclude patterns for files (overrides config file). "
        "Example: --exclude '**/target/**' '**/__pycache__/**'",
    )

    parser.add_argument(
        "--pytest-timeout",
        type=int,
        default=50,
        help="Pytest timeout per test in seconds (default: 50). "
        "For large projects like transformers, use: --pytest-timeout 120",
    )

    parser.add_argument(
        "--pytest-workers",
        type=str,
        default="4",
        help="Number of parallel pytest workers (default: 4). "
        "Use 'auto' for automatic detection or '0' for serial execution.",
    )

    parser.add_argument(
        "--docker-cpus",
        type=int,
        default=16,
        help="Number of CPUs for Docker container during evaluation (default: 16). "
        "Note: Memory is unlimited by default to avoid OOM issues.",
    )

    parser.add_argument(
        "--preserve-commit-order",
        action="store_true",
        help="Preserve commit order from CSV instead of using the script's topological sort. "
        "Use this only when the CSV order is intentionally curated.",
    )

    parser.add_argument(
        "--commit-splits-dir",
        type=Path,
        default=None,
        help="Path to commit_splits directory containing split commit metadata. "
        "When provided, split commits use their original commit timestamp as a tie-breaker. "
        "Example: DATA/trial_space/repo_name/stage4_decomp/output/commit_splits",
    )

    parser.add_argument(
        "--selected-ids-file",
        type=Path,
        default=None,
        help="Path to file containing milestone IDs to build (one per line). "
        "Only these milestones and their transitive dependencies will be built. "
        "If not provided, all milestones will be built.",
    )

    parser.add_argument(
        "--milestones-subset",
        type=str,
        nargs="+",
        default=None,
        help="Milestone IDs to build, plus their transitive dependencies. "
        "Accepts space-separated IDs and comma-separated groups. "
        "Can be combined with --selected-ids-file.",
    )

    parser.add_argument(
        "--base-milestones",
        type=str,
        nargs="+",
        default=None,
        help="Milestone IDs to pre-apply on base. These milestones' commits will be "
        "cherry-picked onto base_tag in order, creating an enhanced base state. "
        "The milestones are applied in the order specified, with each milestone's "
        "commits using the script's normal milestone commit order. These milestones "
        "will be excluded from the build set. Example: --base-milestones "
        "milestone_seed_a60e62d_1 maintenance_deps_1",
    )

    parser.add_argument(
        "--base-commits",
        type=str,
        nargs="+",
        default=None,
        help="Commit hashes to pre-apply on base. These commits will be cherry-picked "
        "onto base_tag in order. Use this for fine-grained control when you don't want "
        "to apply entire milestones. Example: --base-commits a60e62d 6dfaec0 19c2a6e",
    )

    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Do not abort on milestone cherry-pick conflicts; record the failure "
        "and continue to the next milestone. Downstream milestones whose parents "
        "failed are also skipped. A structured build_report.json is always emitted.",
    )

    parser.add_argument(
        "--profile",
        action="store_true",
        help="Collect timing data and write build_profile.json in the output directory.",
    )

    parser.add_argument(
        "--profile-output",
        type=Path,
        default=None,
        help="Optional path for profiling JSON. Implies --profile.",
    )

    parser.add_argument(
        "--profile-slow-git-threshold",
        type=float,
        default=1.0,
        help="Record individual git commands slower than this many seconds in build_profile.json.",
    )

    parser.add_argument(
        "--workspace-dir",
        type=Path,
        default=None,
        help="Multi-repo mode: DATA/github_data/workspaces/<workspace_id>/ (repos.json + commit_repos.json). "
        "Builds testbed/<repo>/ per repo with per-repo tags + testbed/manifest.json. "
        "Requires --milestone-csv, --dependencies-csv and --output-dir.",
    )

    args = parser.parse_args()

    if args.workspace_dir is not None:
        from deepcommit.harness.prepare_repo.multirepo_testbed import build_workspace_testbed

        if args.milestone_csv is None or args.dependencies_csv is None:
            parser.error("--workspace-dir requires --milestone-csv and --dependencies-csv")
        out = build_workspace_testbed(
            workspace_dir=args.workspace_dir,
            milestone_csv=args.milestone_csv,
            dependencies_csv=args.dependencies_csv,
            output_dir=args.output_dir,
            work_dir_name=args.work_dir,
            force_rebuild=args.force_rebuild,
            continue_on_error=args.continue_on_error,
            preserve_commit_order=args.preserve_commit_order,
            rename_threshold=args.rename_threshold if args.rename_threshold is not None else 20,
            verbose=args.verbose,
            workspace_id=args.workspace_dir.name,
        )
        if out["failed"]:
            print(f"\n⚠️  {len(out['failed'])} milestone(s) failed. See build_report.json for diagnostics.")
            sys.exit(2)
        return

    # Resolve --trial shortcut
    if args.trial:
        data_root = Path(os.getenv("DEEPCOMMIT_DATA_ROOT", os.getenv("AGENTBENCH_DATA_ROOT", "DATA")))
        trial_space = data_root / "trial_space"

        # Find trial directory (exact match or glob)
        trial_dir = None
        candidate = trial_space / args.trial
        if candidate.is_dir():
            trial_dir = candidate
        else:
            # Try glob matching
            matches = sorted(trial_space.glob(f"*{args.trial}*"))
            if len(matches) == 1:
                trial_dir = matches[0]
            elif len(matches) > 1:
                print(f"⚠️  Multiple trials match '{args.trial}':")
                for m in matches:
                    print(f"   {m.name}")
                print("Please specify the full trial name.")
                sys.exit(1)

        if trial_dir is None:
            print(f"❌ Trial not found: {args.trial} (searched in {trial_space})")
            sys.exit(1)

        # Resolve CSVs from trial result
        result_dir = trial_dir / "result"
        if not result_dir.exists():
            print(f"❌ No result/ directory in trial: {trial_dir}")
            sys.exit(1)

        # Resolve CSV sources. Priority: workspace copy (if exists, preserves
        # agent's iterative edits) > trial_space result (first-time bootstrap).
        # We defer the "workspace exists" check until after we compute output_dir.
        trial_name = trial_dir.name
        config_pattern = r"^(.+?_.+?_[^_]+[\d.]+_[^_]+[\d.]+)_(.+)$"
        m = re.match(config_pattern, trial_name)
        ws_milestone = None
        ws_deps = None
        if m:
            repo_range = m.group(1)
            trial_suffix = m.group(2)
            if args.output_dir == Path("DATA/harness_workspace/urllib3-docker/test_multi_stage"):
                args.output_dir = data_root / "harness_workspace" / repo_range / trial_suffix
            ws_milestone = args.output_dir / "milestones.csv"
            ws_deps = args.output_dir / "dependencies.csv"

        # Prefer workspace CSVs if they exist (iterative refinement). Otherwise
        # bootstrap from trial_space/result/.
        if args.milestone_csv is None:
            if ws_milestone and ws_milestone.exists():
                args.milestone_csv = ws_milestone
                print(f"   ↻ Using existing workspace milestones.csv: {ws_milestone}")
            else:
                args.milestone_csv = result_dir / "milestones.csv"
        if args.dependencies_csv is None:
            if ws_deps and ws_deps.exists():
                args.dependencies_csv = ws_deps
                print(f"   ↻ Using existing workspace dependencies.csv: {ws_deps}")
            else:
                args.dependencies_csv = result_dir / "dependencies.csv"

        if m:
            if args.config is None:
                inferred_config = Path("config") / f"{repo_range}.yaml"
                if inferred_config.exists():
                    args.config = inferred_config
            # Extract owner_repo and start_version from repo_range
            # e.g., django_django_4.2_5.2 -> owner_repo=django_django, start=4.2
            ver_match = re.match(r"^(.+?)_([^_]+[\d.]+)_([^_]+[\d.]+)$", repo_range)
            if ver_match:
                owner_repo = ver_match.group(1)
                start_ver = ver_match.group(2)
                # Auto-set repo-path if still default
                default_repo = Path("DATA/github_data/repos/urllib3_urllib3")
                if args.repo_path == default_repo:
                    inferred_repo = data_root / "github_data" / "repos" / owner_repo
                    if inferred_repo.exists():
                        args.repo_path = inferred_repo
                # Auto-set base-tag if still default
                if args.base_tag == "2.0.6":
                    args.base_tag = start_ver

        print(f"🔍 Resolved from --trial {trial_name}:")
        print(f"   milestone-csv:    {args.milestone_csv}")
        print(f"   dependencies-csv: {args.dependencies_csv}")
        print(f"   output-dir:       {args.output_dir}")

    # Validate required args
    if args.milestone_csv is None or args.dependencies_csv is None:
        parser.error("--milestone-csv and --dependencies-csv are required (or use --trial)")

    if not args.milestone_csv.exists():
        print(f"❌ milestones.csv not found: {args.milestone_csv}")
        sys.exit(1)
    if not args.dependencies_csv.exists():
        print(f"❌ dependencies.csv not found: {args.dependencies_csv}")
        sys.exit(1)

    # Auto-infer config path from milestone-csv if not provided
    # e.g., DATA/result/BurntSushi_ripgrep_14.1.1_15.0.0_baseline_002/milestones.csv
    #    -> config/BurntSushi_ripgrep_14.1.1_15.0.0.yaml
    # e.g., DATA/harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_001/milestones.csv
    #    -> config/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6.yaml

    # Get candidate names for config matching
    # Pattern: {owner}_{repo}_{start_ver}_{end_ver}_{suffix...}
    # Match version patterns like: 14.1.1, 2.0.6, v4.56.0, dubbo-3.3.3, etc.
    # Using [^_]+ to match versions (no underscore in version)
    config_pattern = r"^(.+?_.+?_[^_]+[\d.]+_[^_]+[\d.]+)"

    # Build list of candidate directory names to try
    candidate_names = []

    # For harness_workspace two-level structure, prioritize parent.parent (repo_versions dir)
    if "DATA/harness_workspace/" in str(args.milestone_csv):
        parent_parent_name = args.milestone_csv.parent.parent.name
        candidate_names.append(parent_parent_name)

    # Also try the immediate parent directory
    parent_dir_name = args.milestone_csv.parent.name
    candidate_names.append(parent_dir_name)

    # Find config file by trying each candidate
    config_base_match = None  # Initialize to avoid UnboundLocalError
    if args.config is None:
        for candidate in candidate_names:
            config_base_match = re.match(config_pattern, candidate)
            if config_base_match:
                config_base = config_base_match.group(1)
                inferred_config = Path("config") / f"{config_base}.yaml"
                if inferred_config.exists():
                    args.config = inferred_config
                    print(f"🔍 Auto-inferred config from milestone-csv path: {args.config}")
                    break
        else:
            # No config found - report the first candidate's expected path
            config_base_match = re.match(config_pattern, candidate_names[0])
            if config_base_match:
                config_base = config_base_match.group(1)
                inferred_config = Path("config") / f"{config_base}.yaml"
                print(f"⚠️  Could not find inferred config file: {inferred_config}")
                print("   Will use default configuration values.")
    else:
        # Config explicitly provided, still try to get config_base_match for other auto-inferences
        for candidate in candidate_names:
            config_base_match = re.match(config_pattern, candidate)
            if config_base_match:
                break

    # Auto-infer commit-splits-dir if not provided
    # e.g., DATA/result/xxx/milestones.csv -> DATA/trial_space/xxx/stage4_decomp/output/commit_splits
    # or DATA/trial_space/xxx/milestones.csv -> DATA/trial_space/xxx/stage4_decomp/output/commit_splits
    # or DATA/harness_workspace/apache_dubbo.../baseline.../milestones.csv -> DATA/trial_space/apache_dubbo..._baseline.../stage4_decomp/output/commit_splits
    if args.commit_splits_dir is None:
        milestone_parent = args.milestone_csv.parent
        milestone_parent_str = str(milestone_parent)

        # Build candidate paths
        candidate_paths = []

        # If milestone is in DATA/result/, also check DATA/trial_space/
        if "DATA/result/" in milestone_parent_str:
            trial_space_parent = Path(milestone_parent_str.replace("DATA/result/", "DATA/trial_space/"))
            candidate_paths.extend(
                [
                    trial_space_parent / "stage4_decomp" / "output" / "commit_splits",
                    trial_space_parent / "stage4_mstone" / "output" / "commit_splits",
                ]
            )

        # If milestone is in DATA/harness_workspace/, also check DATA/trial_space/
        # harness_workspace structure: DATA/harness_workspace/{repo_versions}/{run_id}/milestones.csv
        # trial_space structure: DATA/trial_space/{repo_versions}_{run_id}/stage4_decomp/output/commit_splits
        if "DATA/harness_workspace/" in milestone_parent_str:
            # Extract the two-level directory structure and combine into one
            # e.g., .../harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_001/
            #    -> .../trial_space/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6_baseline_rerun_stage4_001/
            harness_ws_idx = milestone_parent_str.find("DATA/harness_workspace/")
            if harness_ws_idx >= 0:
                prefix = milestone_parent_str[:harness_ws_idx]
                suffix = milestone_parent_str[harness_ws_idx + len("DATA/harness_workspace/") :]
                # suffix is like "apache_dubbo.../baseline_rerun_stage4_001"
                parts = suffix.rstrip("/").split("/")
                if len(parts) >= 2:
                    # Combine repo_versions and run_id with underscore
                    combined_name = "_".join(parts[:2])
                    trial_space_path = Path(prefix) / "DATA" / "trial_space" / combined_name
                    candidate_paths.extend(
                        [
                            trial_space_path / "stage4_decomp" / "output" / "commit_splits",
                            trial_space_path / "stage4_mstone" / "output" / "commit_splits",
                        ]
                    )

        # Check in the same directory as milestone-csv
        candidate_paths.extend(
            [
                milestone_parent / "stage4_decomp" / "output" / "commit_splits",
                milestone_parent / "stage4_mstone" / "output" / "commit_splits",
                milestone_parent / "commit_splits",
            ]
        )

        for candidate in candidate_paths:
            if candidate.exists() and candidate.is_dir():
                args.commit_splits_dir = candidate
                print(f"🔍 Auto-inferred commit-splits-dir: {args.commit_splits_dir}")
                break

    # Auto-infer repo-path if using default value
    # e.g., BurntSushi_ripgrep_14.1.1_15.0.0_xxx -> DATA/github_data/repos/BurntSushi_ripgrep
    # e.g., apache_dubbo_dubbo-3.3.3_dubbo-3.3.6 -> DATA/github_data/repos/apache_dubbo
    default_repo_path = Path("DATA/github_data/repos/urllib3_urllib3")
    if args.repo_path == default_repo_path and config_base_match:
        # Extract owner_repo from config_base (e.g., "BurntSushi_ripgrep_14.1.1_15.0.0")
        config_base = config_base_match.group(1)
        # Split by version pattern to get owner_repo
        # Supports: owner_repo_v1.2.3_v2.3.4 or owner_repo_name-1.2.3_name-2.3.4
        owner_repo_match = re.match(r"^(.+?)_[^_]+[\d.]+_[^_]+[\d.]+$", config_base)
        if owner_repo_match:
            owner_repo = owner_repo_match.group(1)
            inferred_repo = Path("DATA/github_data/repos") / owner_repo
            if inferred_repo.exists():
                args.repo_path = inferred_repo
                print(f"🔍 Auto-inferred repo-path: {args.repo_path}")

    # Auto-infer base-tag if using default value
    # e.g., BurntSushi_ripgrep_14.1.1_15.0.0 -> base-tag = 14.1.1
    # e.g., apache_dubbo_dubbo-3.3.3_dubbo-3.3.6 -> base-tag = dubbo-3.3.3
    if args.base_tag == "2.0.6" and config_base_match:
        config_base = config_base_match.group(1)
        # Extract start version (first version in the pattern)
        # Supports: v1.2.3, 1.2.3, dubbo-3.3.3 (version with prefix)
        version_match = re.search(r"_([^_]+[\d.]+)_[^_]+[\d.]+$", config_base)
        if version_match:
            args.base_tag = version_match.group(1)
            print(f"🔍 Auto-inferred base-tag: {args.base_tag}")

    # Auto-infer output-dir if using default value
    # e.g., DATA/result/BurntSushi_ripgrep_14.1.1_15.0.0_baseline_002/milestones.csv
    #    -> DATA/harness_workspace/BurntSushi_ripgrep_14.1.1_15.0.0/baseline_002
    # e.g., DATA/harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_001/milestones.csv
    #    -> DATA/harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_001
    default_output_dir = Path("DATA/harness_workspace/urllib3-docker/test_multi_stage")
    if args.output_dir == default_output_dir and config_base_match:
        config_base = config_base_match.group(1)
        # For harness_workspace two-level structure, use parent_dir_name as suffix
        if "DATA/harness_workspace/" in str(args.milestone_csv):
            suffix = args.milestone_csv.parent.name
        else:
            # Extract suffix (everything after the version pattern) for DATA/result/
            suffix_match = re.match(r"^.+?_.+?_[^_]+[\d.]+_[^_]+[\d.]+_(.+)$", parent_dir_name)
            suffix = suffix_match.group(1) if suffix_match else "default"
        inferred_output = Path("DATA/harness_workspace") / config_base / suffix
        args.output_dir = inferred_output
        print(f"🔍 Auto-inferred output-dir: {args.output_dir}")

    # Load config file if provided
    config = {}
    if args.config:
        if not args.config.exists():
            print(f"❌ Config file not found: {args.config}")
            sys.exit(1)
        print(f"📖 Loading configuration from: {args.config}")
        config = load_config(args.config)
        for key, value in config.items():
            print(f"   {key}: {value}")

    # Merge config with command line arguments (command line takes precedence)
    # repo_src_dirs: from args > config (required)
    if args.repo_src_dirs is not None:
        repo_src_dirs = args.repo_src_dirs
    elif "repo_src_dirs" in config:
        repo_src_dirs = config["repo_src_dirs"]
    else:
        print("❌ repo_src_dirs not found in config file and not provided via --repo-src-dirs")
        print("   Please add 'repo_src_dirs' to your config YAML or use --repo-src-dirs argument")
        sys.exit(1)

    # test_dirs: from args > config (required)
    if args.test_dirs is not None:
        test_dirs = args.test_dirs
    elif "test_dirs" in config:
        test_dirs = config["test_dirs"]
    else:
        print("❌ test_dirs not found in config file and not provided via --test-dirs")
        print("   Please add 'test_dirs' to your config YAML or use --test-dirs argument")
        sys.exit(1)

    # exclude patterns: from args > config > default (empty)
    if args.exclude is not None:
        exclude_patterns = args.exclude
    elif "exclude" in config:
        exclude_patterns = config["exclude"]
    else:
        exclude_patterns = []

    # generated_patterns: from config > default (empty)
    generated_patterns = config.get("generated_patterns", [])

    # modifiable_test_patterns: from config > default (empty)
    modifiable_test_patterns = config.get("modifiable_test_patterns", [])

    # main_branch: from args > config > default
    if args.main_branch is not None:
        main_branch = args.main_branch
    elif "main_branch" in config:
        main_branch = config["main_branch"]
    else:
        main_branch = "main"

    print("\n📋 Effective configuration:")
    print(f"   repo_src_dirs: {repo_src_dirs}")
    print(f"   test_dirs: {test_dirs}")
    print(f"   exclude_patterns: {exclude_patterns}")
    print(f"   generated_patterns: {generated_patterns}")
    print(f"   modifiable_test_patterns: {modifiable_test_patterns}")
    print(f"   main_branch: {main_branch}")

    # Combine output_dir with work_dir to get full work directory path
    output_dir = args.output_dir
    work_dir = output_dir / args.work_dir
    profile_enabled = args.profile or args.profile_output is not None
    profile_output = args.profile_output or (output_dir / "build_profile.json")
    profiler = BuildProfiler(
        enabled=profile_enabled,
        slow_git_threshold_s=args.profile_slow_git_threshold,
    )

    # Auto-select rename_threshold based on repo if not specified
    rename_threshold = args.rename_threshold
    if rename_threshold is None:
        repo_name = args.repo_path.name.lower()
        if "huggingface_transformers" in repo_name:
            rename_threshold = 50
            print("📝 Auto-selected rename-threshold=50 for huggingface_transformers (to avoid false rename detection)")
        else:
            rename_threshold = 20
            print("📝 Using default rename-threshold=20")

    # Create branch creator
    creator = BranchCreator(
        repo_path=args.repo_path,
        work_dir=work_dir,
        base_tag=args.base_tag,
        verbose=args.verbose,
        rename_threshold=rename_threshold,
        force_rebuild=args.force_rebuild,
        src_dirs=repo_src_dirs,
        main_branch=main_branch,
        test_dirs=test_dirs,
        exclude_patterns=exclude_patterns,
        pytest_timeout=args.pytest_timeout,
        pytest_workers=args.pytest_workers,
        docker_cpus=args.docker_cpus,
        preserve_commit_order=args.preserve_commit_order,
        commit_splits_dir=args.commit_splits_dir,
        generated_patterns=generated_patterns,
        modifiable_test_patterns=modifiable_test_patterns,
        continue_on_error=args.continue_on_error,
        profiler=profiler,
    )

    # Setup work repository
    is_new_repo = creator.setup_work_repository()

    # Resolve actual base commit (may differ from base_tag if tag is on release branch)
    print("\n🔍 Resolving base commit...")
    creator.actual_base_commit = creator._resolve_base_commit()

    # Copy milestone and dependencies CSV files to output directory (before creating tags)
    output_dir.mkdir(parents=True, exist_ok=True)
    print("\n📋 Copying CSV files to output directory...")

    # Copy milestones.csv
    milestone_csv_copy = output_dir / "milestones.csv"
    if args.milestone_csv.resolve() != milestone_csv_copy.resolve():
        shutil.copy(args.milestone_csv, milestone_csv_copy)
        print(f"   ✅ Copied milestones.csv to: {milestone_csv_copy}")
    else:
        print(f"   ✅ milestones.csv already at: {milestone_csv_copy}")

    # Copy dependencies.csv
    dependencies_csv_copy = output_dir / "dependencies.csv"
    if args.dependencies_csv.resolve() != dependencies_csv_copy.resolve():
        shutil.copy(args.dependencies_csv, dependencies_csv_copy)
        print(f"   ✅ Copied dependencies.csv to: {dependencies_csv_copy}")
    else:
        print(f"   ✅ dependencies.csv already at: {dependencies_csv_copy}")

    # Parse input files
    milestones = creator.parse_milestones(args.milestone_csv)
    dependencies = creator.parse_dependencies(args.dependencies_csv)

    # Apply base prerequisites if provided
    # This must happen BEFORE selected-ids-file filtering to access all milestone data
    if args.base_milestones or args.base_commits:
        print("\n🔧 Processing base prerequisites...")

        # Collect commits to apply (in order)
        commits_to_apply = []
        base_milestone_ids = []

        if args.base_milestones:
            print(f"   Base milestones: {args.base_milestones}")
            for m_id in args.base_milestones:
                if m_id not in milestones:
                    print(f"❌ ERROR: Base milestone '{m_id}' not found in milestones.csv")
                    sys.exit(1)
                m_commits = milestones[m_id].commits
                commits_to_apply.extend(m_commits)
                base_milestone_ids.append(m_id)
                print(f"      {m_id}: {len(m_commits)} commits")

        if args.base_commits:
            print(f"   Direct commits: {args.base_commits}")
            commits_to_apply.extend(args.base_commits)

        # Apply prerequisites to create enhanced base
        if commits_to_apply:
            new_base = creator.apply_base_prerequisites(
                commits_to_apply,
                description=f"base prerequisites ({len(commits_to_apply)} commits)",
            )
            creator.actual_base_commit = new_base

            # Remove base milestones from build set
            if base_milestone_ids:
                print("\n📋 Removing base milestones from build set...")
                for m_id in base_milestone_ids:
                    milestones.pop(m_id, None)
                    dependencies.pop(m_id, None)
                    print(f"   - Removed: {m_id}")

                # Also remove base milestones from other milestones' parent dependencies
                for m_id, parents in dependencies.items():
                    original_parents = list(parents)
                    for base_m_id in base_milestone_ids:
                        if base_m_id in parents:
                            parents.remove(base_m_id)
                    if len(parents) < len(original_parents):
                        removed = set(original_parents) - set(parents)
                        print(f"   - Updated {m_id}'s parents: removed {removed}")

                print(f"   Remaining milestones: {len(milestones)}")

    # Filter to subset if requested. Both CLI IDs and file IDs use the same
    # transitive dependency closure so M1 can rebuild a focused affected set.
    selected_ids: Set[str] = set()
    if args.milestones_subset:
        for raw_item in args.milestones_subset:
            for item in raw_item.split(","):
                item = item.strip()
                if item:
                    selected_ids.add(item)
        print(f"\n📋 Requested milestone subset from CLI: {len(selected_ids)} milestone(s)")

    if args.selected_ids_file:
        print(f"\n📋 Loading selected milestone IDs from {args.selected_ids_file}...")
        with open(args.selected_ids_file, "r") as f:
            file_selected_ids = set(line.strip() for line in f if line.strip())
        selected_ids.update(file_selected_ids)
        print(f"   File requested: {len(file_selected_ids)} milestone(s)")

    if selected_ids:
        print(f"   Total requested: {len(selected_ids)} milestone(s)")

        def compute_dependency_closure(dependant_ids: Set[str], deps: Dict[str, List[str]]) -> Set[str]:
            """Given target milestone IDs, return closure including all required dependencies."""
            closure = set(dependant_ids)
            queue = list(dependant_ids)
            while queue:
                current = queue.pop(0)
                for parent in deps.get(current, []):
                    if parent not in closure:
                        closure.add(parent)
                        queue.append(parent)
            return closure

        closure = compute_dependency_closure(selected_ids, dependencies)
        added_deps = closure - selected_ids
        if added_deps:
            print(f"   Added {len(added_deps)} transitive dependencies: {sorted(added_deps)}")
        print(f"   Total milestones to build: {len(closure)}")

        # Filter milestones and dependencies
        original_count = len(milestones)
        milestones = {m_id: m for m_id, m in milestones.items() if m_id in closure}
        dependencies = {
            m_id: [p for p in parents if p in closure] for m_id, parents in dependencies.items() if m_id in closure
        }

        # Validate all selected IDs exist (excluding base_milestones which were pre-applied)
        base_milestone_set = set(args.base_milestones) if args.base_milestones else set()
        missing_ids = selected_ids - set(milestones.keys()) - base_milestone_set
        if missing_ids:
            print(f"❌ ERROR: Selected milestone IDs not found in milestones.csv: {missing_ids}")
            sys.exit(1)

        # Remove base milestones from selected_ids (they were already applied to base)
        if base_milestone_set & selected_ids:
            removed_from_selected = base_milestone_set & selected_ids
            print(
                f"   ℹ️  Excluding {len(removed_from_selected)} base milestones from selection: {sorted(removed_from_selected)}"
            )

        print(f"   Filtered: {original_count} → {len(milestones)} milestones")

    def load_existing_metadata_for_incremental() -> Optional[Dict]:
        """Load metadata used to validate existing tags in --incremental mode."""
        metadata_file = output_dir / "metadata.json"
        if metadata_file.exists():
            print("\n📖 Loading existing metadata.json for validation...")
            with open(metadata_file, "r", encoding="utf-8") as f:
                return json.load(f)

        print("\n⚠️  No existing metadata.json found; existing tags cannot be validated")
        return None

    def write_build_profile() -> None:
        if not profiler.enabled:
            return
        profiler.write(profile_output)
        profiler.print_summary(profile_output)

    # Check if we need to create tags
    if not is_new_repo:
        # Check if all milestone tags already exist
        print("\n🔍 Checking if milestone tags already exist...")
        all_tags_exist = True
        missing_tags = []

        for m_id in milestones:
            tag_start = f"milestone-{m_id}-start"
            tag_end = f"milestone-{m_id}-end"

            if not creator.verify_tag_exists(tag_start):
                all_tags_exist = False
                missing_tags.append(tag_start)
            if not creator.verify_tag_exists(tag_end):
                all_tags_exist = False
                missing_tags.append(tag_end)

        if all_tags_exist and args.incremental:
            # Even if nothing is missing, --incremental must validate that
            # existing tags still match the current CSV/dependencies inputs.
            print("\n🔄 Incremental mode: all tags exist; validating against metadata.json")
            existing_metadata = load_existing_metadata_for_incremental()
            results, topo_order = creator.create_all_branches(
                milestones,
                dependencies,
                incremental=True,
                existing_metadata=existing_metadata,
                milestone_csv_path=args.milestone_csv,
                dependencies_csv_path=args.dependencies_csv,
            )
        elif all_tags_exist:
            print("✅ All milestone tags already exist. Skipping tag creation.")
            print("   Use --force-rebuild to recreate tags from scratch.")

            # Try to load repo_src_dirs and test_dirs from existing metadata.json
            metadata_file = output_dir / "metadata.json"
            if metadata_file.exists():
                print("\n📖 Loading configuration from existing metadata.json...")
                with open(metadata_file, "r", encoding="utf-8") as f:
                    existing_metadata = json.load(f)

                if "repo_src_dirs" in existing_metadata:
                    creator.src_dirs = existing_metadata["repo_src_dirs"]
                    print(f"   ✅ repo_src_dirs: {creator.src_dirs}")
                else:
                    print(f"   ⚠️  repo_src_dirs not found in metadata, using: {creator.src_dirs}")

                # Support both old "test_dir" and new "test_dirs" format
                if "test_dirs" in existing_metadata:
                    creator.test_dirs = existing_metadata["test_dirs"]
                    print(f"   ✅ test_dirs: {creator.test_dirs}")
                elif "test_dir" in existing_metadata:
                    # Backward compatibility: convert old test_dir to test_dirs list
                    creator.test_dirs = [existing_metadata["test_dir"]]
                    print(f"   ✅ test_dirs (from legacy test_dir): {creator.test_dirs}")
                else:
                    print(f"   ⚠️  test_dirs not found in metadata, using: {creator.test_dirs}")

                if "exclude_patterns" in existing_metadata:
                    creator.exclude_patterns = existing_metadata["exclude_patterns"]
                    print(f"   ✅ exclude_patterns: {creator.exclude_patterns}")
                else:
                    print(f"   ⚠️  exclude_patterns not found in metadata, using: {creator.exclude_patterns}")

                if "pytest_timeout" in existing_metadata:
                    creator.pytest_timeout = existing_metadata["pytest_timeout"]
                    print(f"   ✅ pytest_timeout: {creator.pytest_timeout}")
                else:
                    print(f"   ⚠️  pytest_timeout not found in metadata, using: {creator.pytest_timeout}")

                if "pytest_workers" in existing_metadata:
                    creator.pytest_workers = existing_metadata["pytest_workers"]
                    print(f"   ✅ pytest_workers: {creator.pytest_workers}")
                else:
                    print(f"   ⚠️  pytest_workers not found in metadata, using: {creator.pytest_workers}")

                if "docker_cpus" in existing_metadata:
                    creator.docker_cpus = existing_metadata["docker_cpus"]
                    print(f"   ✅ docker_cpus: {creator.docker_cpus}")
                else:
                    print(f"   ⚠️  docker_cpus not found in metadata, using: {creator.docker_cpus}")
            else:
                print("\n⚠️  No existing metadata.json found, using command line arguments:")
                print(f"   repo_src_dirs: {creator.src_dirs}")
                print(f"   test_dirs: {creator.test_dirs}")
                print(f"   exclude_patterns: {creator.exclude_patterns}")
                print(f"   pytest_timeout: {creator.pytest_timeout}")
                print(f"   pytest_workers: {creator.pytest_workers}")
                print(f"   docker_cpus: {creator.docker_cpus}")

            # Build results from existing tags
            results = []
            for m_id in milestones:
                tag_start = f"milestone-{m_id}-start"
                tag_end = f"milestone-{m_id}-end"
                start_sha = creator.run_git(f"rev-parse {tag_start}", capture=True).stdout.strip()
                end_sha = creator.run_git(f"rev-parse {tag_end}", capture=True).stdout.strip()
                parents = dependencies.get(m_id, [])

                results.append(
                    {
                        "milestone_id": m_id,
                        "tag_name_start": tag_start,
                        "commit_sha_start": start_sha,
                        "tag_name_end": tag_end,
                        "commit_sha_end": end_sha,
                        "parent_milestones": ";".join(parents) if parents else "",
                    }
                )

            # Get topological order
            topo_order = creator.topological_sort(milestones, dependencies)
        elif args.incremental:
            # Incremental mode: skip existing valid tags, rebuild missing ones
            print("\n🔄 Incremental mode: will skip existing valid tags, rebuild missing ones")
            print(f"   Missing tags: {len(missing_tags)}")
            for tag in missing_tags[:5]:  # Show first 5
                print(f"   - {tag}")
            if len(missing_tags) > 5:
                print(f"   ... and {len(missing_tags) - 5} more")

            # Load existing metadata for validation
            existing_metadata = load_existing_metadata_for_incremental()

            # Create all branches with incremental mode
            results, topo_order = creator.create_all_branches(
                milestones,
                dependencies,
                incremental=True,
                existing_metadata=existing_metadata,
                milestone_csv_path=args.milestone_csv,
                dependencies_csv_path=args.dependencies_csv,
            )
        else:
            print(f"⚠️  Some tags are missing: {len(missing_tags)} tags")
            for tag in missing_tags[:5]:  # Show first 5
                print(f"   - {tag}")
            if len(missing_tags) > 5:
                print(f"   ... and {len(missing_tags) - 5} more")
            print("\n❌ Cannot use existing testbed with incomplete tags.")
            print("   Use --force-rebuild to recreate all tags, or --incremental to rebuild missing ones.")
            sys.exit(1)
    else:
        # Create all branches (new repo or force rebuild)
        results, topo_order = creator.create_all_branches(
            milestones,
            dependencies,
            incremental=args.incremental,
            existing_metadata=None,
            milestone_csv_path=args.milestone_csv,
            dependencies_csv_path=args.dependencies_csv,
        )

        # Note: milestone_branches.csv is no longer generated
        # All milestone branch info is now in metadata.json

        print(f"\n{'=' * 60}")
        print(f"✅ All done! Created {len(results)} milestone tag pairs.")
        print(f"{'=' * 60}")

    # Always emit build_report.json (structured diagnostics)
    with profiler.section("write_build_report", milestones=len(milestones)):
        _write_build_report(
            output_dir=output_dir,
            creator=creator,
            topo_order=topo_order,
            milestones=milestones,
            dependencies=dependencies,
            continue_on_error=args.continue_on_error,
        )

    # Verify if requested
    if args.verify:
        with profiler.section("verify_all_tags", milestones=len(milestones)):
            verification_passed = creator.verify_all_tags(milestones)
        if not verification_passed:
            print("\n⚠️  Warning: Some verifications failed!")
            write_build_profile()
            sys.exit(1)

    # If continue-on-error recorded failures, still exit non-zero so callers
    # (CI, stage1 validator) can detect incomplete builds. build_report.json
    # has the structured details.
    if args.continue_on_error and creator._failed_milestones:
        print(f"\n⚠️  {len(creator._failed_milestones)} milestone(s) failed. See build_report.json for diagnostics.")
        write_build_profile()
        sys.exit(2)  # distinct from config errors (exit 1)

    # Generate milestone patches
    creator.generate_milestone_patches(milestones=milestones, output_dir=output_dir)

    # Generate metadata.json
    print("\n📊 Generating metadata.json...")
    metadata = creator.generate_metadata(
        milestones=milestones,
        dependencies=dependencies,
        topo_order=topo_order,
        results=results,
        milestone_csv_path=args.milestone_csv,
        dependencies_csv_path=args.dependencies_csv,
        output_dir=output_dir,
        base_milestones=args.base_milestones,
        base_commits=args.base_commits,
    )

    # Save metadata.json to output directory (e.g., baseline_rerun_stage4_002/)
    metadata_file = output_dir / "metadata.json"
    with profiler.section("save_metadata", path=str(metadata_file)):
        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

    print(f"✅ Metadata saved to: {metadata_file}")
    write_build_profile()


if __name__ == "__main__":
    main()
