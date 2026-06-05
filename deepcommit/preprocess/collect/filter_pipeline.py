#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
过滤管道 - 统一的数据过滤逻辑
"""

import time
from typing import Dict, Set

from deepcommit.harness.utils.src_filter import SrcFileFilter

from .data_models import CollectedData, IssueData, PRData


class FilterPipeline:
    """过滤管道 - 统一的过滤逻辑"""

    def __init__(self, config, github_api=None):
        """
        初始化过滤管道

        Args:
            config: CollectorConfig对象
            github_api: GitHubAPI实例（用于获取missing PRs/Issues）
        """
        self.config = config
        self.github_api = github_api
        self.stats = {
            "original_commits": 0,
            "filtered_commits": 0,
            "filtered_commits_by_exclude": 0,
            "original_prs": 0,
            "filtered_prs": 0,
            "fetched_prs": 0,
            "original_issues": 0,
            "filtered_issues": 0,
            "fetched_issues": 0,
        }
        # Build exclude patterns from test_dirs + exclude
        self._exclude_patterns = SrcFileFilter.build_exclude_patterns(
            test_dirs=getattr(config, "test_dirs", None),
            exclude=getattr(config, "exclude", None),
        )

    def apply(self, data: CollectedData) -> CollectedData:
        """
        应用所有过滤器

        新逻辑（更严格）：
        1. 过滤commits（src_dirs）
        2. 为commits添加merged_via_pr字段
        3. 只保留merged_via_pr中提及的PRs
        4. 只保留被保留PRs引用的Issues

        Args:
            data: 原始数据

        Returns:
            过滤后的数据
        """
        import time

        print("\n[过滤阶段] 开始应用过滤器...")
        stage_start = time.time()

        self.stats["original_commits"] = len(data.commits)
        self.stats["original_prs"] = len(data.prs)
        self.stats["original_issues"] = len(data.issues)

        # 1. 过滤commits（src_dirs）
        if self.config.repo_src_dirs:
            step_start = time.time()
            print(f"\n  [1/8] 过滤commits（只保留修改了 {', '.join(self.config.repo_src_dirs)} 的commits）...")
            data = self._filter_commits_by_src_dirs(data)
            step_time = time.time() - step_start
            print(
                f"  ✓ 保留 {len(data.commits)} 个commits (过滤了 {self.stats['filtered_commits']} 个) - 耗时: {step_time:.2f}s"
            )
        else:
            print("\n  [1/8] 跳过commits过滤（未配置repo_src_dirs）")

        # 2. 过滤commits（test_dirs + exclude）- 移除只修改被排除文件的commits
        if self._exclude_patterns:
            step_start = time.time()
            print("\n  [2/8] 过滤commits（排除只修改 test_dirs/exclude 匹配文件的commits）...")
            print(f"        共 {len(self._exclude_patterns)} 个排除模式")
            data = self._filter_commits_by_exclude_patterns(data)
            step_time = time.time() - step_start
            print(
                f"  ✓ 保留 {len(data.commits)} 个commits (排除了 {self.stats['filtered_commits_by_exclude']} 个) - 耗时: {step_time:.2f}s"
            )
        else:
            print("\n  [2/8] 跳过exclude过滤（未配置 test_dirs/exclude）")

        # 3. 收集可能的merged_via_pr，并检测missing PRs
        step_start = time.time()
        print("\n  [3/8] 收集merged_via_pr并检测missing PRs...")
        merged_via_prs = self._collect_merged_via_prs(data)
        print(f"  - 发现 {len(merged_via_prs)} 个merged_via_pr")

        missing_prs = merged_via_prs - set(data.prs.keys())
        if missing_prs:
            print(f"  - 发现 {len(missing_prs)} 个missing PRs: {sorted(missing_prs)}")
            fetched_prs = self._fetch_missing_prs(missing_prs)
            if fetched_prs:
                for pr_num, pr_data in fetched_prs.items():
                    data.prs[pr_num] = pr_data
                self.stats["fetched_prs"] = len(fetched_prs)
                print(f"  ✓ 从GitHub获取 {len(fetched_prs)} 个PRs")
        else:
            print("  ✓ 没有missing PRs")
        step_time = time.time() - step_start
        print(f"  耗时: {step_time:.2f}s")

        # 4. 为commits添加merged_via_pr字段
        step_start = time.time()
        print("\n  [4/8] 为commits添加merged_via_pr字段...")
        data = self._add_merged_via_pr_to_commits(data)
        added_count = sum(1 for c in data.commits.values() if c.merged_via_pr)
        print(f"  ✓ {added_count} 个commits添加了merged_via_pr - 耗时: {time.time() - step_start:.2f}s")

        # 5. 过滤PRs（只保留merged_via_pr中的PRs）
        step_start = time.time()
        print("\n  [5/8] 过滤PRs（只保留merged_via_pr中提及的PRs）...")
        data = self._filter_prs_by_merged_via_pr(data, merged_via_prs)
        step_time = time.time() - step_start
        print(f"  ✓ 保留 {len(data.prs)} 个PRs (过滤了 {self.stats['filtered_prs']} 个) - 耗时: {step_time:.2f}s")

        # 6. 检测并获取missing Issues（被保留的PR引用但不在数据中）
        step_start = time.time()
        print("\n  [6/8] 检测missing Issues...")
        missing_issues = self._find_missing_issues(data)
        if missing_issues:
            print(f"  - 发现 {len(missing_issues)} 个missing Issues: {sorted(list(missing_issues))[:10]}")
            fetched_issues = self._fetch_missing_issues(missing_issues)
            if fetched_issues:
                for issue_num, issue_data in fetched_issues.items():
                    data.issues[issue_num] = issue_data
                self.stats["fetched_issues"] = len(fetched_issues)
                print(f"  ✓ 从GitHub获取 {len(fetched_issues)} 个Issues")
        else:
            print("  ✓ 没有missing Issues")
        step_time = time.time() - step_start
        print(f"  耗时: {step_time:.2f}s")

        # 7. 过滤孤立的issues（只保留被保留的PR引用的Issues）
        step_start = time.time()
        print("\n  [7/8] 过滤孤立的Issues（只保留被保留的PR引用的Issues）...")
        data = self._filter_orphaned_issues(data)
        step_time = time.time() - step_start
        print(
            f"  ✓ 保留 {len(data.issues)} 个Issues (过滤了 {self.stats['filtered_issues']} 个) - 耗时: {step_time:.2f}s"
        )

        stage_time = time.time() - stage_start
        print(f"\n[过滤阶段] 完成！总耗时: {stage_time:.2f}s")

        return data

    def _filter_commits_by_src_dirs(self, data: CollectedData) -> CollectedData:
        """
        过滤不符合src_dirs的commits，并且只保留src/目录的文件变更

        新逻辑：
        1. 过滤files_changed，只保留src/目录的文件
        2. 如果过滤后没有文件，丢弃该commit
        3. 重新计算additions和deletions
        """
        if not self.config.repo_src_dirs:
            return data

        filtered_commits = {}
        for sha, commit in data.commits.items():
            # 过滤files_changed，只保留src/目录的文件
            filtered_files = self._filter_files_by_src_dirs(commit.files_changed)

            # 如果过滤后没有文件，丢弃该commit
            if not filtered_files:
                self.stats["filtered_commits"] += 1
                continue

            # 更新commit的files_changed和统计信息
            commit.files_changed = filtered_files
            commit.additions = sum(f.get("additions", 0) for f in filtered_files)
            commit.deletions = sum(f.get("deletions", 0) for f in filtered_files)

            filtered_commits[sha] = commit

        data.commits = filtered_commits
        return data

    def _filter_files_by_src_dirs(self, files_changed: list) -> list:
        """
        过滤文件列表，只保留src/目录下的文件

        Args:
            files_changed: 文件变更列表

        Returns:
            过滤后的文件列表（只包含src/目录的文件）
        """
        if not self.config.repo_src_dirs:
            return files_changed

        if not files_changed:
            return []

        filtered_files = []
        for file_info in files_changed:
            file_path = file_info.get("path", "") if isinstance(file_info, dict) else str(file_info)

            # 检查文件路径是否在指定目录下 (canonical S-norm membership)
            if SrcFileFilter.in_src_dirs(file_path, self.config.repo_src_dirs):
                filtered_files.append(file_info)

        return filtered_files

    def _filter_commits_by_exclude_patterns(self, data: CollectedData) -> CollectedData:
        """
        过滤只修改被排除文件的commits，并从保留的commits中移除被排除的文件

        逻辑：
        1. 对每个commit的files_changed，移除匹配exclude patterns的文件
        2. 如果过滤后没有文件，丢弃该commit
        3. 重新计算additions和deletions
        """
        if not self._exclude_patterns:
            return data

        filtered_commits = {}
        for sha, commit in data.commits.items():
            # 过滤files_changed，移除匹配exclude patterns的文件
            filtered_files = self._filter_files_by_exclude_patterns(commit.files_changed)

            # 如果过滤后没有文件，丢弃该commit
            if not filtered_files:
                self.stats["filtered_commits_by_exclude"] += 1
                continue

            # 更新commit的files_changed和统计信息
            commit.files_changed = filtered_files
            commit.additions = sum(f.get("additions", 0) for f in filtered_files)
            commit.deletions = sum(f.get("deletions", 0) for f in filtered_files)

            filtered_commits[sha] = commit

        data.commits = filtered_commits
        return data

    def _filter_files_by_exclude_patterns(self, files_changed: list) -> list:
        """
        过滤文件列表，移除匹配exclude patterns的文件

        Args:
            files_changed: 文件变更列表

        Returns:
            过滤后的文件列表（不包含被排除的文件）
        """
        if not self._exclude_patterns:
            return files_changed

        if not files_changed:
            return []

        filtered_files = []
        for file_info in files_changed:
            file_path = file_info.get("path", "") if isinstance(file_info, dict) else str(file_info)

            # 如果文件不匹配任何exclude pattern，保留它
            if not SrcFileFilter.should_exclude_file(file_path, self._exclude_patterns):
                filtered_files.append(file_info)

        return filtered_files

    def _normalize_path_prefix(self, path: str) -> str:
        """
        规范化路径前缀

        Examples:
            "src" -> "src/"
            "src/" -> "src/"
            "src/foo" -> "src/"
        """
        if not path:
            return ""

        # 移除前导/后trailing的空格和斜杠
        path = path.strip().strip("/")

        # 如果路径包含子路径，只取第一层
        if "/" in path:
            path = path.split("/")[0]

        # 添加trailing slash
        return path + "/"

    def _collect_merged_via_prs(self, data: CollectedData) -> Set[int]:
        """
        收集所有可能的merged_via_pr

        方法：
        1. 检查每个PR的merge_commit_sha是否在commits中
        2. 如果是，说明这个PR被merge到了main分支

        Returns:
            所有应该保留的PR编号集合
        """
        merged_via_prs = set()

        for pr_num, pr in data.prs.items():
            # 检查PR的merge_commit_sha是否在commits中
            if pr.merged and pr.merge_commit_sha and pr.merge_commit_sha in data.commits:
                merged_via_prs.add(pr_num)

        return merged_via_prs

    def _add_merged_via_pr_to_commits(self, data: CollectedData) -> CollectedData:
        """
        为commits添加merged_via_pr字段

        方法：
        1. 遍历所有PRs
        2. 如果PR的merge_commit_sha在commits中，为该commit添加merged_via_pr字段

        Returns:
            更新后的数据
        """
        for pr_num, pr in data.prs.items():
            if pr.merged and pr.merge_commit_sha and pr.merge_commit_sha in data.commits:
                data.commits[pr.merge_commit_sha].merged_via_pr = pr_num

        return data

    def _filter_prs_by_merged_via_pr(self, data: CollectedData, merged_via_prs: Set[int]) -> CollectedData:
        """
        过滤PRs - 只保留merged_via_pr中提及的PRs

        新逻辑（更严格）：
        只保留在commits的merged_via_pr字段中出现的PRs
        这确保PR和Commit数据完全对齐

        Args:
            data: 数据集合
            merged_via_prs: merged_via_pr的PR编号集合

        Returns:
            过滤后的数据
        """
        filtered_prs = {}

        for num, pr in data.prs.items():
            # 只保留在merged_via_prs中的PRs
            if num in merged_via_prs:
                filtered_prs[num] = pr
            else:
                self.stats["filtered_prs"] += 1

        data.prs = filtered_prs
        return data

    def _filter_orphaned_issues(self, data: CollectedData) -> CollectedData:
        """过滤没有被任何PR或Commit引用的issues

        对于 GitHub 项目：issues 通过 PR 的 related_issues 引用
        对于 Trac 项目（如 Django）：issues 通过 commit 的 associated_refs 引用
        （associated_refs 对于 Trac 项目存储的是 ticket 编号）
        """
        # 收集所有被PR引用的issues
        referenced_issues = set()
        for pr in data.prs.values():
            referenced_issues.update(pr.related_issues)

        # 收集所有被Commit引用的issues（对于 Trac 项目）
        for commit in data.commits.values():
            referenced_issues.update(commit.associated_refs)

        # 只保留被引用的issues
        filtered_issues = {}
        for num, issue in data.issues.items():
            if num in referenced_issues:
                filtered_issues[num] = issue
            else:
                self.stats["filtered_issues"] += 1

        data.issues = filtered_issues
        return data

    def _find_missing_prs(self, data: CollectedData) -> Set[int]:
        """
        查找missing PRs

        场景：commits有merged_via_pr字段，但PR不在数据中
        原因：PR的merge commit可能没有修改src/，被过滤了

        Returns:
            missing PR numbers集合
        """
        missing_prs = set()

        for commit in data.commits.values():
            # 检查merged_via_pr字段
            if commit.merged_via_pr and commit.merged_via_pr not in data.prs:
                missing_prs.add(commit.merged_via_pr)

        return missing_prs

    def _fetch_missing_prs(self, pr_numbers: Set[int]) -> Dict[int, PRData]:
        """
        从GitHub API获取missing PRs

        Args:
            pr_numbers: PR编号集合

        Returns:
            PR编号 -> PRData的字典
        """
        if not self.github_api:
            print("  ⚠️  GitHubAPI不可用，无法获取missing PRs")
            return {}

        fetched_prs = {}

        for pr_number in sorted(pr_numbers):
            try:
                print(f"    获取PR #{pr_number}...")
                pr_detail = self.github_api.get_pr_details(pr_number)
                if pr_detail:
                    # 构建PRData对象
                    pr_data = PRData(
                        number=pr_detail["number"],
                        title=pr_detail["title"],
                        body=pr_detail.get("body", ""),
                        state=pr_detail["state"],
                        merged=pr_detail.get("merged", False),
                        merge_commit_sha=pr_detail.get("merge_commit_sha"),
                        base_ref=pr_detail.get("base_ref", ""),
                        base_sha=pr_detail.get("base_sha"),
                        head_ref=pr_detail.get("head_ref", ""),
                        head_sha=pr_detail.get("head_sha"),
                        commits=pr_detail.get("commits", []),
                        related_issues=pr_detail.get("related_issues", []),
                        files_changed=pr_detail.get("files_changed", []),
                        files_count=pr_detail.get("files_count", 0),
                        additions=pr_detail.get("additions", 0),
                        deletions=pr_detail.get("deletions", 0),
                        author=pr_detail.get("author"),
                        created_at=pr_detail.get("created_at"),
                        updated_at=pr_detail.get("updated_at"),
                        merged_at=pr_detail.get("merged_at"),
                        closed_at=pr_detail.get("closed_at"),
                        comments=pr_detail.get("comments", []),
                        review_comments=pr_detail.get("review_comments", []),
                    )
                    fetched_prs[pr_number] = pr_data

                time.sleep(0.5)  # 避免rate limit

            except Exception as e:
                print(f"    ⚠️  获取PR #{pr_number}失败: {e}")

        return fetched_prs

    def _find_missing_issues(self, data: CollectedData) -> Set[int]:
        """
        查找missing Issues

        场景：PR引用了issue，但issue不在数据中
        原因：可能是跨范围引用

        Returns:
            missing issue numbers集合
        """
        # 收集所有被PR引用的issue numbers
        referenced_issues = set()
        for pr in data.prs.values():
            referenced_issues.update(pr.related_issues)

        # 找出不在数据中的issues
        existing_issues = set(data.issues.keys())
        missing_issues = referenced_issues - existing_issues

        return missing_issues

    def _fetch_missing_issues(self, issue_numbers: Set[int]) -> Dict[int, IssueData]:
        """
        从GitHub API获取missing Issues

        Args:
            issue_numbers: Issue编号集合

        Returns:
            Issue编号 -> IssueData的字典
        """
        if not self.github_api:
            print("  ⚠️  GitHubAPI不可用，无法获取missing Issues")
            return {}

        fetched_issues = {}

        for issue_number in sorted(issue_numbers):
            try:
                print(f"    获取Issue #{issue_number}...")
                issue_detail = self.github_api.get_issue_details(issue_number)
                if issue_detail:
                    # 构建IssueData对象
                    issue_data = IssueData(
                        number=issue_detail["number"],
                        title=issue_detail["title"],
                        body=issue_detail.get("body", ""),
                        state=issue_detail["state"],
                        author=issue_detail.get("author"),
                        labels=issue_detail.get("labels", []),
                        created_at=issue_detail.get("created_at"),
                        updated_at=issue_detail.get("updated_at"),
                        closed_at=issue_detail.get("closed_at"),
                        comments_count=issue_detail.get("comments_count", 0),
                    )
                    fetched_issues[issue_number] = issue_data

                time.sleep(0.5)  # 避免rate limit

            except Exception as e:
                print(f"    ⚠️  获取Issue #{issue_number}失败: {e}")

        return fetched_issues
