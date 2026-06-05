#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
数据模型定义
用于gh_crawler的数据收集、过滤、增强流程
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class CommitData:
    """Commit数据模型"""

    sha: str
    message: str
    author: Dict[str, Any]
    committer: Dict[str, Any]
    parent_shas: List[str]
    files_changed: List[Dict[str, Any]]
    additions: int
    deletions: int
    is_merge_commit: bool
    # 增强字段
    merged_via_pr: Optional[int] = None  # 如果是merge commit，对应的PR编号
    associated_refs: List[int] = field(default_factory=list)  # 关联的上游引用（PR、Issue、ticket 等）
    data_incomplete: bool = False  # 数据是否不完整

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CommitData":
        """从字典创建"""
        return cls(
            sha=data["sha"],
            message=data["message"],
            author=data["author"],
            committer=data["committer"],
            parent_shas=data.get("parent_shas", []),
            files_changed=data.get("files_changed", []),
            additions=data.get("additions", 0),
            deletions=data.get("deletions", 0),
            is_merge_commit=data.get("is_merge_commit", False),
            merged_via_pr=data.get("merged_via_pr"),
            associated_refs=data.get("associated_refs", data.get("associated_prs", [])),
            data_incomplete=data.get("data_incomplete", False),
        )


@dataclass
class PRData:
    """PR数据模型"""

    number: int
    title: str
    body: str
    state: str
    merged: bool
    merge_commit_sha: Optional[str]
    base_ref: str
    base_sha: Optional[str]
    head_ref: str
    head_sha: Optional[str]
    commits: List[str]  # commit SHA列表
    related_issues: List[int]
    files_changed: List[Dict[str, Any]]
    files_count: int
    additions: int
    deletions: int
    author: Optional[str]  # 保留兼容性，实际应该用user
    created_at: Optional[str]
    updated_at: Optional[str]
    merged_at: Optional[str]
    closed_at: Optional[str]
    # 新增10个字段（版本2新增）
    user: Optional[str] = None  # PR作者用户名
    author_association: Optional[str] = None  # 作者角色（MEMBER/CONTRIBUTOR等）
    merged_by: Optional[str] = None  # 合并者用户名
    comments_count: int = 0  # 讨论评论数
    review_comments_count: int = 0  # 代码审查评论数
    labels: List[str] = field(default_factory=list)  # PR标签
    assignees: List[str] = field(default_factory=list)  # 负责人
    requested_reviewers: List[str] = field(default_factory=list)  # 请求审查者
    milestone: Optional[str] = None  # 里程碑
    draft: bool = False  # 是否草稿PR
    # 额外字段
    comments: List[Dict[str, Any]] = field(default_factory=list)
    review_comments: List[Dict[str, Any]] = field(default_factory=list)
    meets_threshold: bool = True
    base_in_range: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PRData":
        """从字典创建"""
        return cls(
            number=data["number"],
            title=data["title"],
            body=data.get("body", ""),
            state=data["state"],
            merged=data.get("merged", False),
            merge_commit_sha=data.get("merge_commit_sha"),
            base_ref=data.get("base_ref", ""),
            base_sha=data.get("base_sha"),
            head_ref=data.get("head_ref", ""),
            head_sha=data.get("head_sha"),
            commits=data.get("commits", []),
            related_issues=data.get("related_issues", []),
            files_changed=data.get("files_changed", []),
            files_count=data.get("files_count", 0),
            additions=data.get("additions", 0),
            deletions=data.get("deletions", 0),
            author=data.get("author"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            merged_at=data.get("merged_at"),
            closed_at=data.get("closed_at"),
            # 新增10个字段
            user=data.get("user"),
            author_association=data.get("author_association"),
            merged_by=data.get("merged_by"),
            comments_count=data.get("comments_count", 0),
            review_comments_count=data.get("review_comments_count", 0),
            labels=data.get("labels", []),
            assignees=data.get("assignees", []),
            requested_reviewers=data.get("requested_reviewers", []),
            milestone=data.get("milestone"),
            draft=data.get("draft", False),
            # 额外字段
            comments=data.get("comments", []),
            review_comments=data.get("review_comments", []),
            meets_threshold=data.get("meets_threshold", True),
            base_in_range=data.get("base_in_range", True),
        )


@dataclass
class IssueData:
    """Issue数据模型"""

    number: int
    title: str
    body: str
    state: str
    author: Optional[str]  # 保留兼容性，实际应该用user
    labels: List[str]
    created_at: Optional[str]
    updated_at: Optional[str]
    closed_at: Optional[str]
    comments_count: int = 0
    # 新增12个字段（版本2新增）
    user: Optional[str] = None  # Issue创建者用户名
    author_association: Optional[str] = None  # 创建者角色（MEMBER/CONTRIBUTOR/NONE等）
    url: Optional[str] = None  # Issue完整URL
    state_reason: Optional[str] = None  # 关闭原因（completed/not_planned等）
    assignees: List[str] = field(default_factory=list)  # 负责人列表
    milestone: Optional[str] = None  # 关联里程碑
    closed_by: Optional[str] = None  # 关闭者用户名
    locked: bool = False  # 是否锁定
    active_lock_reason: Optional[str] = None  # 锁定原因
    reactions: Dict[str, Any] = field(default_factory=dict)  # Emoji反应统计
    conversations: Dict[str, Any] = field(default_factory=dict)  # 评论和反应详情
    events: List[Dict[str, Any]] = field(default_factory=list)  # Issue事件历史

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IssueData":
        """从字典创建"""
        return cls(
            number=data["number"],
            title=data["title"],
            body=data.get("body", ""),
            state=data["state"],
            author=data.get("author"),
            labels=data.get("labels", []),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            closed_at=data.get("closed_at"),
            comments_count=data.get("comments_count", 0),
            # 新增12个字段
            user=data.get("user"),
            author_association=data.get("author_association"),
            url=data.get("url"),
            state_reason=data.get("state_reason"),
            assignees=data.get("assignees", []),
            milestone=data.get("milestone"),
            closed_by=data.get("closed_by"),
            locked=data.get("locked", False),
            active_lock_reason=data.get("active_lock_reason"),
            reactions=data.get("reactions", {}),
            conversations=data.get("conversations", {}),
            events=data.get("events", []),
        )


@dataclass
class ReleaseData:
    """Release数据模型（简化版本 - 只保存元数据）"""

    # Tag信息
    tag: str
    tag_sha: str  # tag对象的SHA
    commit_sha: str  # tag指向的commit SHA

    # Release元数据
    name: str
    release_notes: str
    author: Optional[str]
    url: str
    created_at: Optional[str]
    published_at: Optional[str]  # 发布时间
    draft: bool = False
    prerelease: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReleaseData":
        """从字典创建"""
        return cls(
            tag=data["tag"],
            tag_sha=data["tag_sha"],
            commit_sha=data["commit_sha"],
            name=data["name"],
            release_notes=data.get("release_notes", ""),
            author=data.get("author"),
            url=data["url"],
            created_at=data.get("created_at"),
            published_at=data.get("published_at"),
            draft=data.get("draft", False),
            prerelease=data.get("prerelease", False),
        )


@dataclass
class CollectedData:
    """收集的完整数据"""

    REQUIRED_JSONL_FILES = (
        "main_commits.jsonl",
        "pull_requests.jsonl",
        "issues.jsonl",
        "releases.jsonl",
    )

    commits: Dict[str, CommitData] = field(default_factory=dict)
    prs: Dict[int, PRData] = field(default_factory=dict)
    issues: Dict[int, IssueData] = field(default_factory=dict)
    releases: List[ReleaseData] = field(default_factory=list)

    @classmethod
    def has_jsonl_dump(cls, output_dir: Path) -> bool:
        """Return True when the standard JSONL dump layout exists on disk.

        This intentionally checks only for file presence. Empty files are valid
        for repositories/ranges that legitimately have zero PRs or issues.
        """
        output_dir = Path(output_dir)
        return all((output_dir / filename).exists() for filename in cls.REQUIRED_JSONL_FILES)

    def to_jsonl(self, output_dir: Path):
        """保存为JSONL文件"""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # 保存commits
        with open(output_dir / "main_commits.jsonl", "w", encoding="utf-8") as f:
            for commit in self.commits.values():
                f.write(json.dumps(commit.to_dict(), ensure_ascii=False, default=str) + "\n")

        # 保存PRs
        with open(output_dir / "pull_requests.jsonl", "w", encoding="utf-8") as f:
            for pr in self.prs.values():
                f.write(json.dumps(pr.to_dict(), ensure_ascii=False, default=str) + "\n")

        # 保存issues
        with open(output_dir / "issues.jsonl", "w", encoding="utf-8") as f:
            for issue in self.issues.values():
                f.write(json.dumps(issue.to_dict(), ensure_ascii=False, default=str) + "\n")

        # 保存releases
        with open(output_dir / "releases.jsonl", "w", encoding="utf-8") as f:
            for release in self.releases:
                f.write(json.dumps(release.to_dict(), ensure_ascii=False, default=str) + "\n")

    @classmethod
    def from_jsonl(cls, output_dir: Path) -> "CollectedData":
        """从JSONL文件加载"""
        output_dir = Path(output_dir)
        data = cls()

        # 加载commits
        commits_file = output_dir / "main_commits.jsonl"
        if commits_file.exists():
            with open(commits_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        commit_dict = json.loads(line)
                        commit = CommitData.from_dict(commit_dict)
                        data.commits[commit.sha] = commit
                    except Exception:
                        continue

        # 加载PRs
        prs_file = output_dir / "pull_requests.jsonl"
        if prs_file.exists():
            with open(prs_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        pr_dict = json.loads(line)
                        pr = PRData.from_dict(pr_dict)
                        data.prs[pr.number] = pr
                    except Exception:
                        continue

        # 加载issues
        issues_file = output_dir / "issues.jsonl"
        if issues_file.exists():
            with open(issues_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        issue_dict = json.loads(line)
                        issue = IssueData.from_dict(issue_dict)
                        data.issues[issue.number] = issue
                    except Exception:
                        continue

        # 加载releases
        releases_file = output_dir / "releases.jsonl"
        if releases_file.exists():
            with open(releases_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        release_dict = json.loads(line)
                        release = ReleaseData.from_dict(release_dict)
                        data.releases.append(release)
                    except Exception:
                        continue

        return data

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            "total_commits": len(self.commits),
            "total_prs": len(self.prs),
            "total_issues": len(self.issues),
            "total_releases": len(self.releases),
        }


class DataIntegrityError(Exception):
    """数据完整性错误"""

    pass
