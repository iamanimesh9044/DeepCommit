#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
数据导出器 - 生成所有输出文件
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from deepcommit.harness.utils.src_filter import SrcFileFilter

from .data_models import CollectedData


class DataExporter:
    """数据导出器 - 生成所有输出文件"""

    def __init__(self, config, git_repo=None):
        """
        初始化数据导出器

        Args:
            config: CollectorConfig对象
            git_repo: LocalGitRepo实例（用于获取PR commits详情）
        """
        self.config = config
        self.git_repo = git_repo

    def export(
        self, data: CollectedData, output_dir: Path, start_time: datetime, filter_stats: Dict[str, int]
    ) -> Dict[str, Any]:
        """
        导出所有数据文件

        Args:
            data: 要导出的数据
            output_dir: 输出目录
            start_time: 收集开始时间
            filter_stats: 过滤统计信息

        Returns:
            元数据字典
        """
        print("\n[导出阶段] 开始导出数据...")

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # 1. 导出标准文件
        print("\n  [1/6] 导出main_commits.jsonl...")
        self._export_main_commits(data, output_dir / "main_commits.jsonl")
        print(f"  ✓ {len(data.commits)} 个commits")

        print("\n  [2/6] 导出pull_requests.jsonl...")
        self._export_pull_requests(data, output_dir / "pull_requests.jsonl")
        print(f"  ✓ {len(data.prs)} 个PRs")

        print("\n  [3/6] 导出issues.jsonl...")
        self._export_issues(data, output_dir / "issues.jsonl")
        print(f"  ✓ {len(data.issues)} 个Issues")

        print("\n  [4/6] 导出releases.jsonl...")
        self._export_releases(data, output_dir / "releases.jsonl")
        print(f"  ✓ {len(data.releases)} 个Releases")

        # 2. 导出派生文件（新增）
        print("\n  [5/6] 导出pr_commits.jsonl...")
        pr_commits_count = self._export_pr_commits(data, output_dir / "pr_commits.jsonl")
        print(f"  ✓ {pr_commits_count} 个PR commits")

        # 6. 导出pr_conversations（增量模式下如果已存在则跳过，保留原文件）
        print("\n  [6/6] 导出pr_conversations.jsonl...")
        pr_conversations_file = output_dir / "pr_conversations.jsonl"
        if pr_conversations_file.exists():
            print("  ✓ pr_conversations.jsonl 已存在，保留原文件（避免丢失comments）")
        else:
            self._export_pr_conversations(data, pr_conversations_file)
            print(f"  ✓ {len(data.prs)} 个PR conversations")

        # 3. 生成并导出元数据
        metadata = self._generate_metadata(data, start_time, filter_stats, pr_commits_count)
        self._export_metadata(metadata, output_dir / "metadata.json")

        return metadata

    def _export_main_commits(self, data: CollectedData, file_path: Path):
        """导出main_commits.jsonl"""
        # 按提交时间排序
        sorted_commits = sorted(
            data.commits.values(),
            key=lambda c: c.committer.get("time", ""),
        )

        with open(file_path, "w", encoding="utf-8") as f:
            for commit in sorted_commits:
                commit_dict = commit.to_dict()
                f.write(json.dumps(commit_dict, ensure_ascii=False, default=str) + "\n")

    def _export_pull_requests(self, data: CollectedData, file_path: Path):
        """导出pull_requests.jsonl - 只保留元数据和索引，删除大字段"""
        # 按PR编号排序
        sorted_prs = sorted(data.prs.values(), key=lambda p: p.number)

        with open(file_path, "w", encoding="utf-8") as f:
            for pr in sorted_prs:
                # 清理PR数据，只保留元数据
                clean_pr = self._clean_pr_data(pr)
                f.write(json.dumps(clean_pr, ensure_ascii=False, default=str) + "\n")

    def _export_issues(self, data: CollectedData, file_path: Path):
        """导出issues.jsonl"""
        # 按Issue编号排序
        sorted_issues = sorted(data.issues.values(), key=lambda i: i.number)

        with open(file_path, "w", encoding="utf-8") as f:
            for issue in sorted_issues:
                f.write(json.dumps(issue.to_dict(), ensure_ascii=False, default=str) + "\n")

    def _export_releases(self, data: CollectedData, file_path: Path):
        """导出releases.jsonl"""
        with open(file_path, "w", encoding="utf-8") as f:
            for release in data.releases:
                f.write(json.dumps(release.to_dict(), ensure_ascii=False, default=str) + "\n")

    def _export_pr_commits(self, data: CollectedData, file_path: Path) -> int:
        """
        导出pr_commits.jsonl

        这个文件包含所有PR的commits信息，并应用src/过滤

        Returns:
            导出的PR commits数量
        """
        pr_commits_count = 0

        with open(file_path, "w", encoding="utf-8") as f:
            for pr_num, pr in data.prs.items():
                for commit_sha in pr.commits:
                    # 获取commit详情
                    commit_info = self._get_commit_info_for_pr(commit_sha, data)
                    if not commit_info:
                        continue

                    # 应用src/过滤
                    filtered_files = self._filter_files_by_src_dirs(commit_info.get("files_changed", []))

                    # 如果过滤后没有文件，跳过
                    if not filtered_files:
                        continue

                    # 构建pr_commit数据
                    pr_commit = {
                        "pr_number": pr_num,
                        "commit_sha": commit_sha,
                        "message": commit_info.get("message", ""),
                        "author": commit_info.get("author"),
                        "committer": commit_info.get("committer"),
                        "files_changed": filtered_files,
                        "files_count": len(filtered_files),
                        "additions": sum(f.get("additions", 0) for f in filtered_files),
                        "deletions": sum(f.get("deletions", 0) for f in filtered_files),
                    }

                    f.write(json.dumps(pr_commit, ensure_ascii=False, default=str) + "\n")
                    pr_commits_count += 1

        return pr_commits_count

    def _export_pr_conversations(self, data: CollectedData, file_path: Path):
        """
        导出pr_conversations.jsonl

        这个文件包含PR的标题、描述、评论等对话信息
        便于做对话分析和文本挖掘
        """
        conversations = []

        for pr_num, pr in data.prs.items():
            conversation = {
                "pr_number": pr_num,
                "title": pr.title,
                "body": pr.body,
                "author": pr.author,
                "created_at": pr.created_at,
                "merged_at": pr.merged_at,
                "comments": pr.comments,
                "review_comments": pr.review_comments,
                "comments_count": len(pr.comments),
                "review_comments_count": len(pr.review_comments),
            }
            conversations.append(conversation)

        # 按PR编号排序
        conversations.sort(key=lambda x: x["pr_number"])

        with open(file_path, "w", encoding="utf-8") as f:
            for conv in conversations:
                f.write(json.dumps(conv, ensure_ascii=False, default=str) + "\n")

    def _generate_metadata(
        self, data: CollectedData, start_time: datetime, filter_stats: Dict[str, int], pr_commits_count: int
    ) -> Dict[str, Any]:
        """生成元数据"""
        end_time = datetime.now(timezone.utc)

        # 计算统计信息
        commits_with_merged_via_pr = sum(1 for c in data.commits.values() if c.merged_via_pr)

        metadata = {
            "repository": f"{self.config.owner}/{self.config.repo}",
            "collection_start": start_time.isoformat(),
            "collection_end": end_time.isoformat(),
            "duration_seconds": (end_time - start_time).total_seconds(),
            "start_tag": self.config.start_tag,
            "end_tag": self.config.end_tag,
            # 基本统计
            "total_releases": len(data.releases),
            "main_commits": len(data.commits),
            "total_commits": len(data.commits),
            "total_prs": len(data.prs),
            "total_issues": len(data.issues),
            "total_pr_commits": pr_commits_count,
            # new_* 字段表示"最终保存的数量"（兼容improve脚本）
            "new_prs": len(data.prs),  # 保存的PRs数量
            "new_issues": len(data.issues),  # 保存的Issues数量
            "new_commits": len(data.commits),  # 保存的Commits数量
            # 增强统计
            "commits_with_merged_via_pr": commits_with_merged_via_pr,
            # 过滤统计（兼容测试脚本）
            # 注意：测试脚本公式 expected = new_* - filtered_*_count + fetched_*_count
            # 所以需要确保 new_* + filtered_* = original_*
            "original_main_commits_count": filter_stats.get("original_commits", len(data.commits)),
            "filtered_main_commits_count": filter_stats.get("filtered_commits", 0),
            "original_prs_count": filter_stats.get("original_prs", len(data.prs)),
            "filtered_prs_count": filter_stats.get("filtered_prs", 0),
            "fetched_prs_count": 0,  # v2版本不支持从GitHub获取missing PRs
            "original_issues_count": filter_stats.get("original_issues", len(data.issues)),
            "filtered_issues_count": filter_stats.get("filtered_issues", 0),
            "fetched_issues_count": 0,  # v2版本不支持从GitHub获取missing Issues
            # 配置信息
            "repo_src_dirs": self.config.repo_src_dirs,
            "main_branch": self.config.main_branch,
            # 数据版本
            "data_version": "2.0",
            "generator": "gh_crawler_v2",
            "files_filtered": bool(self.config.repo_src_dirs),
            "prs_aligned": True,
            "issues_aligned": True,
        }

        return metadata

    def _export_metadata(self, metadata: Dict[str, Any], file_path: Path):
        """导出元数据"""
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False, default=str)

    def _clean_pr_data(self, pr) -> Dict[str, Any]:
        """
        清理PR数据，只保留元数据和索引

        删除大字段：files_changed, comments, review_comments
        """
        return {
            "number": pr.number,
            "title": pr.title,
            "body": pr.body,
            "state": pr.state,
            "created_at": pr.created_at,
            "updated_at": pr.updated_at,
            "closed_at": pr.closed_at,
            "merged_at": pr.merged_at,
            "merge_commit_sha": pr.merge_commit_sha,
            "base_sha": pr.base_sha,
            "base_ref": pr.base_ref,
            "head_sha": pr.head_sha,
            "head_ref": pr.head_ref,
            "author": pr.author,  # 兼容性字段（deprecated，使用user）
            "url": f"https://github.com/{self.config.owner}/{self.config.repo}/pull/{pr.number}",
            "merged": pr.merged,
            # 统计信息（原始值）
            "additions": pr.additions,
            "deletions": pr.deletions,
            "changed_files": pr.files_count,
            # 索引信息
            "related_issues": pr.related_issues,
            "commits": pr.commits,
            "commits_count": len(pr.commits),
            # 新增10个字段（版本2）
            "user": pr.user,
            "author_association": pr.author_association,
            "merged_by": pr.merged_by,
            "comments_count": pr.comments_count,
            "review_comments_count": pr.review_comments_count,
            "labels": pr.labels,
            "assignees": pr.assignees,
            "requested_reviewers": pr.requested_reviewers,
            "milestone": pr.milestone,
            "draft": pr.draft,
            # 删除大字段: files_changed, comments, review_comments
        }

    def _get_commit_info_for_pr(self, commit_sha: str, data: CollectedData) -> Dict:
        """
        获取commit详情（用于pr_commits）

        优先级：
        1. data.commits（main commits）
        2. LocalGitRepo（PR分支commits）
        """
        # 首先尝试从main_commits获取
        if commit_sha in data.commits:
            commit = data.commits[commit_sha]
            return {
                "message": commit.message,
                "author": commit.author,
                "committer": commit.committer,
                "files_changed": commit.files_changed,
            }

        # 如果不在main_commits中，尝试从LocalGitRepo获取
        if self.git_repo:
            try:
                commit_info = self.git_repo.get_commit_info(commit_sha)
                files_changed = self.git_repo.get_commit_files(commit_sha)

                # 转换时间格式
                from datetime import datetime, timezone

                author_time = datetime.fromtimestamp(commit_info["author"]["time"], tz=timezone.utc).isoformat()
                committer_time = datetime.fromtimestamp(commit_info["committer"]["time"], tz=timezone.utc).isoformat()

                return {
                    "message": commit_info["message"],
                    "author": {
                        "name": commit_info["author"]["name"],
                        "email": commit_info["author"]["email"],
                        "time": author_time,
                    },
                    "committer": {
                        "name": commit_info["committer"]["name"],
                        "email": commit_info["committer"]["email"],
                        "time": committer_time,
                    },
                    "files_changed": files_changed,
                }
            except Exception:
                pass

        # 无法获取，返回None
        return None

    def _filter_files_by_src_dirs(self, files: list) -> list:
        """
        过滤文件列表，只保留src/目录下的文件

        注意：这个方法和FilterPipeline._filter_files_by_src_dirs保持一致

        Args:
            files: 文件变更列表

        Returns:
            过滤后的文件列表（只包含src/目录的文件）
        """
        if not self.config.repo_src_dirs:
            return files

        if not files:
            return []

        filtered_files = []
        for file_info in files:
            file_path = file_info.get("path", "") if isinstance(file_info, dict) else str(file_info)

            # 检查文件路径是否在指定目录下 (canonical S-norm membership)
            if SrcFileFilter.in_src_dirs(file_path, self.config.repo_src_dirs):
                filtered_files.append(file_info)

        return filtered_files
