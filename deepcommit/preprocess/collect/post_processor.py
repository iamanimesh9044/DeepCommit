#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
后处理器 - 处理收集后的数据（SHA 标准化、验证、DAG 丰富、patch 提取）
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from deepcommit.harness.utils.src_filter import SrcFileFilter


class PostProcessor:
    """数据后处理器 - 处理 SHA 标准化、验证、DAG 丰富、patch 提取"""

    def __init__(
        self,
        output_dir: Path,
        repo_path: Optional[Path] = None,
        verbose: bool = False,
        test_dirs: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
    ):
        """
        初始化后处理器

        Args:
            output_dir: 输出目录（包含 main_commits.jsonl 等文件）
            repo_path: 本地 git 仓库路径
            verbose: 是否输出详细日志
            test_dirs: 测试目录/文件模式列表 (e.g., ["tests/**", "**/*_test.go"])
            exclude: 额外的排除模式列表 (e.g., ["**/testdata/**"])
        """
        self.output_dir = Path(output_dir)
        self.repo_path = Path(repo_path) if repo_path else None
        self.verbose = verbose
        self._exclude_patterns = SrcFileFilter.build_exclude_patterns(test_dirs=test_dirs, exclude=exclude)

    def process_all(
        self,
        dag_path: Optional[Path] = None,
        repo_src_dirs: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        执行所有后处理步骤

        Args:
            dag_path: DAG 文件路径（用于丰富 main_commits）
            repo_src_dirs: 源代码目录列表（用于 patch 提取）

        Returns:
            处理结果统计
        """
        results = {}

        # Step 1: Normalize SHA to 7 characters
        print()
        print("=" * 80)
        print("Normalizing SHA to 7 characters...")
        print("=" * 80)
        sha_stats = self.normalize_sha()
        results["sha_normalization"] = sha_stats

        # Step 2: Enrich main_commits with DAG data (if provided)
        if dag_path:
            print()
            print("=" * 80)
            print("Enriching main_commits.jsonl with DAG statistics...")
            print("=" * 80)
            print(f"DAG file: {dag_path}")

            main_commits_file = self.output_dir / "main_commits.jsonl"
            if not main_commits_file.exists():
                print(f"Warning: {main_commits_file} not found, skipping DAG enrichment")
            else:
                dag_stats = self.enrich_with_dag(dag_path)
                results["dag_enrichment"] = dag_stats

        # Step 3: Extract patches (if repo_path provided)
        if self.repo_path:
            print()
            print("=" * 80)
            print("Extracting commit patches...")
            print("=" * 80)
            print(f"Repository: {self.repo_path}")
            if repo_src_dirs:
                print(f"Path filters: {', '.join(repo_src_dirs)}")

            main_commits_file = self.output_dir / "main_commits.jsonl"
            if main_commits_file.exists():
                patch_stats = self.extract_patches(repo_src_dirs)
                results["patch_extraction"] = patch_stats
            else:
                print(f"Warning: {main_commits_file} not found, skipping patch extraction")

        # Step 4: Validate SHA uniqueness
        if self.repo_path:
            print()
            print("=" * 80)
            print("Validating 7-character SHA uniqueness...")
            print("=" * 80)
            validation_result = self.validate_sha_uniqueness()
            results["sha_validation"] = validation_result

            # Update metadata with validation results
            metadata_file = self.output_dir / "metadata.json"
            self.update_metadata_with_validation(metadata_file, validation_result)

        return results

    def normalize_sha(self) -> Dict[str, int]:
        """
        Normalize all SHA values in pull_requests.jsonl, releases.jsonl, and pr_commits.jsonl to 7 characters.

        Returns:
            Statistics dictionary
        """
        stats = {"prs": 0, "releases": 0, "pr_commits": 0}

        # Process pull_requests.jsonl
        pr_file = self.output_dir / "pull_requests.jsonl"
        if pr_file.exists():
            if self.verbose:
                print(f"Processing {pr_file}...")

            prs = []
            with open(pr_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        pr = json.loads(line)

                        # Truncate SHA fields
                        if "merge_commit_sha" in pr and pr["merge_commit_sha"]:
                            pr["merge_commit_sha"] = self._truncate_sha(pr["merge_commit_sha"])
                        if "base_sha" in pr and pr["base_sha"]:
                            pr["base_sha"] = self._truncate_sha(pr["base_sha"])
                        if "head_sha" in pr and pr["head_sha"]:
                            pr["head_sha"] = self._truncate_sha(pr["head_sha"])

                        # Truncate commits list and rename to pr_commits
                        if "commits" in pr and pr["commits"]:
                            pr["pr_commits"] = [self._truncate_sha(sha) for sha in pr["commits"]]
                            del pr["commits"]

                        prs.append(pr)

            # Write back
            with open(pr_file, "w", encoding="utf-8") as f:
                for pr in prs:
                    f.write(json.dumps(pr, ensure_ascii=False) + "\n")

            stats["prs"] = len(prs)
            if self.verbose:
                print(f"✓ Processed {len(prs)} PRs")

        # Process releases.jsonl
        release_file = self.output_dir / "releases.jsonl"
        if release_file.exists():
            if self.verbose:
                print(f"Processing {release_file}...")

            releases = []
            with open(release_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        release = json.loads(line)

                        # Truncate commits list
                        if "commits" in release and release["commits"]:
                            release["commits"] = [self._truncate_sha(sha) for sha in release["commits"]]

                        releases.append(release)

            # Write back
            with open(release_file, "w", encoding="utf-8") as f:
                for release in releases:
                    f.write(json.dumps(release, ensure_ascii=False) + "\n")

            stats["releases"] = len(releases)
            if self.verbose:
                print(f"✓ Processed {len(releases)} releases")

        # Process pr_commits.jsonl
        pr_commits_file = self.output_dir / "pr_commits.jsonl"
        if pr_commits_file.exists():
            if self.verbose:
                print(f"Processing {pr_commits_file}...")

            pr_commits = []
            with open(pr_commits_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        pr_commit = json.loads(line)

                        # Truncate commit_sha field
                        if "commit_sha" in pr_commit and pr_commit["commit_sha"]:
                            pr_commit["commit_sha"] = self._truncate_sha(pr_commit["commit_sha"])

                        pr_commits.append(pr_commit)

            # Write back
            with open(pr_commits_file, "w", encoding="utf-8") as f:
                for pr_commit in pr_commits:
                    f.write(json.dumps(pr_commit, ensure_ascii=False) + "\n")

            stats["pr_commits"] = len(pr_commits)
            if self.verbose:
                print(f"✓ Processed {len(pr_commits)} PR commits")

        print()
        print("=" * 80)
        print("✓ SHA normalization completed!")
        print("=" * 80)
        if pr_file.exists():
            print(f"Updated: {pr_file}")
        if release_file.exists():
            print(f"Updated: {release_file}")
        if pr_commits_file.exists():
            print(f"Updated: {pr_commits_file}")
        print("=" * 80)

        return stats

    def enrich_with_dag(self, dag_path: Path) -> Dict[str, int]:
        """
        Enrich main_commits.jsonl with DAG statistics.

        Args:
            dag_path: Path to commit DAG JSON file

        Returns:
            Statistics dictionary
        """
        main_commits_file = self.output_dir / "main_commits.jsonl"

        # Load DAG nodes
        dag_nodes = self._load_dag_nodes(dag_path)
        if not dag_nodes:
            print("Warning: No DAG nodes loaded, skipping enrichment")
            return {"enriched": 0}

        # Load commits
        commits = []
        with open(main_commits_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    commits.append(json.loads(line))

        # Refine and enrich commits
        enriched_commits = []
        for commit in commits:
            refined = self._refine_commit(commit, dag_nodes)
            enriched_commits.append(refined)

        # Write back
        with open(main_commits_file, "w", encoding="utf-8") as f:
            for commit in enriched_commits:
                f.write(json.dumps(commit, ensure_ascii=False) + "\n")

        enriched_count = sum(1 for c in enriched_commits if "commit_dag_stat" in c)
        print(f"✓ Enriched {enriched_count}/{len(enriched_commits)} commits with DAG statistics")

        return {"total": len(enriched_commits), "enriched": enriched_count}

    def extract_patches(self, path_prefixes: Optional[List[str]] = None) -> Dict[str, int]:
        """
        Extract git patches for all commits in main_commits.jsonl.

        Args:
            path_prefixes: List of path prefixes to filter (e.g., ['src/', 'test/'])

        Returns:
            Statistics dictionary
        """
        main_commits_file = self.output_dir / "main_commits.jsonl"
        patches_dir = self.output_dir / "patches"
        patches_dir.mkdir(exist_ok=True)

        # Load commits
        commits = []
        with open(main_commits_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    commit = json.loads(line)
                    commits.append(commit)

        # Log exclude patterns if any
        if self._exclude_patterns:
            print(f"Excluding patterns: {self._exclude_patterns}")

        # Extract patches
        success_count = 0
        for commit in commits:
            commit_sha = commit["sha"]
            patch = self._get_commit_patch(commit_sha, path_prefixes, self._exclude_patterns)

            if patch:
                # Use 7-character SHA for patch filename
                short_sha = commit_sha[:7]
                patch_file = patches_dir / f"{short_sha}.patch"
                with open(patch_file, "w", encoding="utf-8") as f:
                    f.write(patch)
                success_count += 1

        print(f"✓ Extracted {success_count}/{len(commits)} patches to {patches_dir}")

        return {"total": len(commits), "extracted": success_count}

    def validate_sha_uniqueness(self) -> Dict[str, Any]:
        """
        Validate that 7-character SHA prefixes uniquely identify commits.

        Only main_commits conflicts are considered critical (cause validation to fail).
        PR commits conflicts are reported as warnings but don't fail validation.

        Returns:
            Dictionary with validation results
        """
        # Collect SHAs separately: main_commits (critical) vs others (non-critical)
        main_shas = set()
        other_shas = set()

        # From main_commits.jsonl (critical)
        main_commits_file = self.output_dir / "main_commits.jsonl"
        if main_commits_file.exists():
            with open(main_commits_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        commit = json.loads(line)
                        main_shas.add(commit["sha"])

        # From pull_requests.jsonl (non-critical for pr_commits)
        pr_file = self.output_dir / "pull_requests.jsonl"
        if pr_file.exists():
            with open(pr_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        pr = json.loads(line)
                        # merge_commit_sha is critical (it's on main branch)
                        if pr.get("merge_commit_sha"):
                            main_shas.add(pr["merge_commit_sha"])
                        # base_sha, head_sha, pr_commits are non-critical
                        if pr.get("base_sha"):
                            other_shas.add(pr["base_sha"])
                        if pr.get("head_sha"):
                            other_shas.add(pr["head_sha"])
                        for sha in pr.get("pr_commits", []):
                            other_shas.add(sha)

        # From releases.jsonl (critical - these are on main branch)
        release_file = self.output_dir / "releases.jsonl"
        if release_file.exists():
            with open(release_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        release = json.loads(line)
                        for sha in release.get("commits", []):
                            main_shas.add(sha)

        # Remove duplicates from other_shas that are already in main_shas
        other_shas = other_shas - main_shas
        all_shas = main_shas | other_shas

        print(f"Collected {len(all_shas)} unique 7-character SHAs from data files")
        print(f"  - Main commits (critical): {len(main_shas)}")
        print(f"  - PR commits (non-critical): {len(other_shas)}")

        # Get all commits from git repository and check for conflicts
        try:
            result = subprocess.run(
                ["git", "rev-list", "--all"], cwd=self.repo_path, capture_output=True, text=True, check=True
            )
            all_full_shas = result.stdout.strip().split("\n")
            print(f"Found {len(all_full_shas)} total commits in repository")

            # Build a mapping of 7-char prefix to full SHAs
            prefix_to_full = {}
            for full_sha in all_full_shas:
                prefix = full_sha[:7]
                if prefix not in prefix_to_full:
                    prefix_to_full[prefix] = []
                prefix_to_full[prefix].append(full_sha)

            # Check for conflicts
            conflicts = {}
            for prefix, full_shas in prefix_to_full.items():
                if len(full_shas) > 1:
                    conflicts[prefix] = full_shas

            # Check conflicts separately for main vs other
            main_conflicts = {}
            other_conflicts = {}
            for short_sha in main_shas:
                if short_sha in conflicts:
                    main_conflicts[short_sha] = conflicts[short_sha]
            for short_sha in other_shas:
                if short_sha in conflicts:
                    other_conflicts[short_sha] = conflicts[short_sha]

            print()
            print("=" * 80)
            print("Validation Results:")
            print("=" * 80)
            print(f"Total unique 7-char SHAs in our data: {len(all_shas)}")
            print(f"Total commits in repository: {len(all_full_shas)}")
            print(f"Total 7-char prefix conflicts in repository: {len(conflicts)}")
            print(f"Conflicts in main commits (critical): {len(main_conflicts)}")
            print(f"Conflicts in PR commits (warning): {len(other_conflicts)}")

            # Only fail if main_commits have conflicts
            validation_passed = len(main_conflicts) == 0

            validation_result = {
                "sha_length": 7,
                "unique_shas_in_data": len(all_shas),
                "main_shas_count": len(main_shas),
                "other_shas_count": len(other_shas),
                "total_commits_in_repo": len(all_full_shas),
                "total_prefix_conflicts_in_repo": len(conflicts),
                "main_conflicts": len(main_conflicts),
                "other_conflicts": len(other_conflicts),
                "validation_passed": validation_passed,
            }

            # Report main conflicts (critical)
            if main_conflicts:
                print()
                print("❌ CRITICAL: SHA conflicts in main commits!")
                print("=" * 80)
                conflict_examples = []
                for short_sha, full_shas in list(main_conflicts.items())[:5]:
                    print(f"  {short_sha} -> {len(full_shas)} commits:")
                    conflict_examples.append(
                        {"short_sha": short_sha, "conflict_count": len(full_shas), "full_shas": full_shas[:3]}
                    )
                    for full_sha in full_shas[:3]:
                        print(f"    - {full_sha}")
                    if len(full_shas) > 3:
                        print(f"    ... and {len(full_shas) - 3} more")
                if len(main_conflicts) > 5:
                    print(f"  ... and {len(main_conflicts) - 5} more conflicts")
                validation_result["main_conflict_examples"] = conflict_examples

            # Report other conflicts (warning only)
            if other_conflicts:
                print()
                print("⚠️  WARNING: SHA conflicts in PR commits (non-critical):")
                print("=" * 80)
                warning_examples = []
                for short_sha, full_shas in list(other_conflicts.items())[:5]:
                    print(f"  {short_sha} -> {len(full_shas)} commits:")
                    warning_examples.append(
                        {"short_sha": short_sha, "conflict_count": len(full_shas), "full_shas": full_shas[:3]}
                    )
                    for full_sha in full_shas[:3]:
                        print(f"    - {full_sha}")
                    if len(full_shas) > 3:
                        print(f"    ... and {len(full_shas) - 3} more")
                if len(other_conflicts) > 5:
                    print(f"  ... and {len(other_conflicts) - 5} more conflicts")
                validation_result["other_conflict_examples"] = warning_examples

            print()
            print("=" * 80)
            if validation_passed:
                print("✓ Validation PASSED: All main commit SHAs are unique!")
                if other_conflicts:
                    print(f"  (Note: {len(other_conflicts)} PR commit conflicts ignored)")
            else:
                print("✗ Validation FAILED: Main commit SHAs have conflicts")
            print("=" * 80)

            return validation_result

        except subprocess.CalledProcessError as e:
            print(f"Error running git command: {e}")
            return {"sha_length": 7, "validation_passed": False, "error": str(e)}

    def update_metadata_with_validation(self, metadata_file: Path, validation_result: Dict[str, Any]):
        """
        Update metadata.json with SHA validation results.

        Args:
            metadata_file: Path to metadata.json
            validation_result: Validation result from validate_sha_uniqueness
        """
        if not metadata_file.exists():
            print(f"Warning: metadata.json not found at {metadata_file}")
            return

        # Read existing metadata
        with open(metadata_file, "r", encoding="utf-8") as f:
            metadata = json.load(f)

        # Add validation results
        metadata["sha_validation"] = validation_result

        # Write back
        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        print(f"✓ Updated metadata with SHA validation results: {metadata_file}")

    # Helper methods

    @staticmethod
    def _truncate_sha(sha: str) -> str:
        """Truncate SHA to 7 characters."""
        if sha:
            return sha[:7]
        return sha

    def _get_commit_patch(
        self,
        commit_sha: str,
        path_prefixes: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
    ) -> str:
        """
        Get git patch for a specific commit, optionally filtered by path prefixes.

        Args:
            commit_sha: Commit SHA (full or short)
            path_prefixes: List of path prefixes to filter (e.g., ['src/', 'test/'])
            exclude_patterns: List of glob patterns to exclude (e.g., ['**/*_test.go'])

        Returns:
            Git patch as string
        """
        try:
            cmd = ["git", "show", "--format=fuller", commit_sha]
            # Add path filters and exclusions
            if path_prefixes or exclude_patterns:
                cmd.append("--")
                if path_prefixes:
                    cmd.extend(path_prefixes)
                else:
                    # If only exclusions, we need to include all files first
                    cmd.append(".")
                # Add exclusion patterns using git pathspec magic
                if exclude_patterns:
                    for pattern in exclude_patterns:
                        # Use git pathspec glob exclusion syntax: :(exclude,glob)pattern
                        cmd.append(f":(exclude,glob){pattern}")
            # Use bytes mode and decode with errors="replace" to handle non-UTF-8 content
            result = subprocess.run(cmd, cwd=self.repo_path, capture_output=True, check=True)
            return result.stdout.decode("utf-8", errors="replace")
        except subprocess.CalledProcessError as e:
            if self.verbose:
                print(f"Warning: Error getting patch for {commit_sha}: {e}", file=sys.stderr)
            return ""

    @staticmethod
    def _load_dag_nodes(dag_file: Path) -> Dict[str, Dict[str, Any]]:
        """
        Load DAG nodes from JSON file.

        Args:
            dag_file: Path to commit DAG JSON file

        Returns:
            Dictionary mapping SHA (7 chars) to DAG node data
        """
        if not dag_file.exists():
            print(f"Warning: DAG file not found: {dag_file}")
            return {}

        with open(dag_file, "r", encoding="utf-8") as f:
            dag_data = json.load(f)

        nodes = dag_data.get("nodes", {})

        # Convert full SHA keys to 7-char keys
        short_nodes = {}
        for full_sha, node_data in nodes.items():
            short_sha = full_sha[:7]
            short_nodes[short_sha] = node_data

        return short_nodes

    @staticmethod
    def _refine_commit(commit: Dict[str, Any], dag_nodes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """
        Refine commit data to match main_commits_refined.jsonl format.

        Args:
            commit: Original commit data
            dag_nodes: DAG nodes mapping (7-char SHA -> node data)

        Returns:
            Refined commit data
        """
        refined = {}

        # 1. SHA -> short SHA (7 chars)
        full_sha = commit["sha"]
        short_sha = full_sha[:7]
        refined["sha"] = short_sha

        # 2. message
        refined["message"] = commit["message"]

        # 3. author field flattening
        if "author" in commit and isinstance(commit["author"], dict):
            refined["author"] = commit["author"].get("name", "")

        # 4. time field - use Committer Date (fallback to Author Date)
        if "committer" in commit and isinstance(commit["committer"], dict):
            refined["time"] = commit["committer"].get("time", "")
        elif "author" in commit and isinstance(commit["author"], dict):
            refined["time"] = commit["author"].get("time", "")

        # 5. PR related fields
        if "merged_via_pr" in commit:
            refined["merged_via_pr"] = commit["merged_via_pr"]

        # 6. files_changed (remove status and old_path fields)
        if "files_changed" in commit:
            files_changed = []
            for file_item in commit["files_changed"]:
                refined_file = {k: v for k, v in file_item.items() if k not in ("status", "old_path")}
                files_changed.append(refined_file)
            refined["files_changed"] = files_changed

        # 7. Add commit_dag_stat if DAG data available
        if dag_nodes:
            dag_stats = dag_nodes.get(short_sha)
            if dag_stats:
                refined["commit_dag_stat"] = {
                    "layer": dag_stats.get("layer"),
                    "in_degree": dag_stats.get("in_degree"),
                    "out_degree": dag_stats.get("out_degree"),
                    "ancestors_count": dag_stats.get("ancestors_count"),
                    "descendants_count": dag_stats.get("descendants_count"),
                }

        return refined
