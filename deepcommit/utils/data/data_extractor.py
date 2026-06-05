"""
Data extraction interfaces for release, PR, and issue information from collected data directories.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


@dataclass
class ReleaseInfo:
    """Information about a release (simplified version - metadata only)."""

    # Tag信息
    tag: str
    tag_sha: str
    commit_sha: str

    # Release元数据
    name: str
    release_notes: str
    author: Optional[str]
    url: str
    created_at: Optional[str]
    published_at: Optional[str]  # 发布时间
    draft: bool
    prerelease: bool


@dataclass
class PullRequestInfo:
    """Information about a pull request."""

    number: int
    title: str
    body: str
    state: str
    created_at: str
    updated_at: str
    closed_at: Optional[str]
    merged_at: Optional[str]
    merge_commit_sha: Optional[str]
    user: str
    author_association: str
    url: str
    merged: bool
    merged_by: Optional[str] = None
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    draft: bool = False
    labels: Optional[List[str]] = None
    assignees: Optional[List[str]] = None
    requested_reviewers: Optional[List[str]] = None
    requested_teams: Optional[List[str]] = None
    milestone: Optional[str] = None
    related_issues: Optional[List[int]] = None
    conversations: Optional[Dict[str, Any]] = None
    events: Optional[List[Dict[str, Any]]] = None
    comments_count: int = 0
    review_comments_count: int = 0
    commits_count: int = 0
    commits: Optional[List[str]] = None
    # Git reference fields
    base_sha: Optional[str] = None
    base_ref: Optional[str] = None
    head_sha: Optional[str] = None
    head_ref: Optional[str] = None
    author: Optional[str] = None

    def __post_init__(self):
        """Initialize list fields if they are None."""
        if self.labels is None:
            self.labels = []
        if self.assignees is None:
            self.assignees = []
        if self.requested_reviewers is None:
            self.requested_reviewers = []
        if self.requested_teams is None:
            self.requested_teams = []
        if self.related_issues is None:
            self.related_issues = []
        if self.conversations is None:
            self.conversations = {}
        if self.events is None:
            self.events = []
        if self.commits is None:
            self.commits = []


@dataclass
class IssueInfo:
    """Information about an issue."""

    number: int
    title: str
    body: str
    state: str
    state_reason: Optional[str]
    created_at: str
    updated_at: str
    closed_at: Optional[str]
    user: str
    author_association: str
    url: str
    labels: List[str]
    assignees: List[str]
    milestone: Optional[str]
    closed_by: Optional[str]
    locked: bool
    active_lock_reason: Optional[str]
    comments_count: int
    reactions: Dict[str, Any]
    conversations: Dict[str, Any]
    events: List[Dict[str, Any]]


@dataclass
class DataDirectoryInfo:
    """Metadata about a data directory."""

    repository: str
    collection_start: str
    collection_end: str
    duration_seconds: float
    start_tag: str
    end_tag: str
    total_releases: int
    total_commits: int
    total_prs: int
    total_issues: int
    new_releases: int
    new_prs: int
    new_issues: int


class DataExtractor:
    """Extract release, PR, and issue information from data directories."""

    def __init__(self, data_dir: str):
        """
        Initialize with a data directory path.

        Args:
            data_dir: Path to the data directory (e.g., /path/to/urllib3_urllib3_2.1.0_2.3.0)
        """
        self.data_dir = Path(data_dir)
        if not self.data_dir.exists():
            raise ValueError(f"Data directory does not exist: {data_dir}")

    def get_metadata(self) -> DataDirectoryInfo:
        """Get metadata information about the data directory."""
        metadata_file = self.data_dir / "metadata.json"
        if not metadata_file.exists():
            raise FileNotFoundError(f"Metadata file not found: {metadata_file}")

        with open(metadata_file, "r") as f:
            data = json.load(f)

        return DataDirectoryInfo(**data)

    def get_releases(self) -> List[ReleaseInfo]:
        """Get all release information."""
        releases_file = self.data_dir / "releases.jsonl"
        if not releases_file.exists():
            raise FileNotFoundError(f"Releases file not found: {releases_file}")

        releases = []
        with open(releases_file, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    releases.append(ReleaseInfo(**data))

        return releases

    def get_release_by_tag(self, tag: str) -> Optional[ReleaseInfo]:
        """Get a specific release by tag."""
        releases = self.get_releases()
        for release in releases:
            if release.tag == tag:
                return release
        return None

    def get_pull_requests(self) -> List[PullRequestInfo]:
        """Get all pull request information."""
        prs_file = self.data_dir / "pull_requests.jsonl"
        if not prs_file.exists():
            raise FileNotFoundError(f"Pull requests file not found: {prs_file}")

        prs = []
        # Get the field names from the dataclass
        pr_fields = {field.name for field in PullRequestInfo.__dataclass_fields__.values()}

        with open(prs_file, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    # Filter out unknown fields that aren't in the dataclass
                    filtered_data = {k: v for k, v in data.items() if k in pr_fields}
                    prs.append(PullRequestInfo(**filtered_data))

        return prs

    def get_pull_request_by_number(self, number: int) -> Optional[PullRequestInfo]:
        """Get a specific pull request by number."""
        prs = self.get_pull_requests()
        for pr in prs:
            if pr.number == number:
                return pr
        return None

    def get_issues(self) -> List[IssueInfo]:
        """Get all issue information."""
        issues_file = self.data_dir / "issues.jsonl"
        if not issues_file.exists():
            raise FileNotFoundError(f"Issues file not found: {issues_file}")

        issues = []
        # Get the field names from the dataclass
        issue_fields = {field.name for field in IssueInfo.__dataclass_fields__.values()}

        with open(issues_file, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    # Filter out unknown fields that aren't in the dataclass
                    filtered_data = {k: v for k, v in data.items() if k in issue_fields}
                    issues.append(IssueInfo(**filtered_data))

        return issues

    def get_issue_by_number(self, number: int) -> Optional[IssueInfo]:
        """Get a specific issue by number."""
        issues = self.get_issues()
        for issue in issues:
            if issue.number == number:
                return issue
        return None

    def iter_releases(self) -> Iterator[ReleaseInfo]:
        """Iterate over releases without loading all into memory."""
        releases_file = self.data_dir / "releases.jsonl"
        if not releases_file.exists():
            raise FileNotFoundError(f"Releases file not found: {releases_file}")

        with open(releases_file, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    yield ReleaseInfo(**data)

    def iter_pull_requests(self) -> Iterator[PullRequestInfo]:
        """Iterate over pull requests without loading all into memory."""
        prs_file = self.data_dir / "pull_requests.jsonl"
        if not prs_file.exists():
            raise FileNotFoundError(f"Pull requests file not found: {prs_file}")

        with open(prs_file, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    yield PullRequestInfo(**data)

    def iter_issues(self) -> Iterator[IssueInfo]:
        """Iterate over issues without loading all into memory."""
        issues_file = self.data_dir / "issues.jsonl"
        if not issues_file.exists():
            raise FileNotFoundError(f"Issues file not found: {issues_file}")

        with open(issues_file, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    yield IssueInfo(**data)

    def get_releases_between_dates(self, start_date: str, end_date: str) -> List[ReleaseInfo]:
        """Get releases between two dates (ISO format)."""
        start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))

        releases = []
        for release in self.iter_releases():
            release_dt = datetime.fromisoformat(release.date.replace("Z", "+00:00"))
            if start_dt <= release_dt <= end_dt:
                releases.append(release)

        return releases

    def get_pull_requests_by_state(self, state: str) -> List[PullRequestInfo]:
        """Get pull requests filtered by state (open, closed, merged)."""
        prs = []
        for pr in self.iter_pull_requests():
            if pr.state == state:
                prs.append(pr)

        return prs

    def get_issues_by_state(self, state: str) -> List[IssueInfo]:
        """Get issues filtered by state (open, closed)."""
        issues = []
        for issue in self.iter_issues():
            if issue.state == state:
                issues.append(issue)

        return issues

    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of all data in the directory."""
        metadata = self.get_metadata()
        releases = self.get_releases()

        # Count PR states
        pr_states = {"open": 0, "closed": 0, "merged": 0}
        for pr in self.iter_pull_requests():
            if pr.merged:
                pr_states["merged"] += 1
            else:
                pr_states[pr.state] += 1

        # Count issue states
        issue_states = {"open": 0, "closed": 0}
        for issue in self.iter_issues():
            issue_states[issue.state] += 1

        return {
            "repository": metadata.repository,
            "collection_period": {
                "start": metadata.collection_start,
                "end": metadata.collection_end,
                "duration_seconds": metadata.duration_seconds,
            },
            "tag_range": {"start": metadata.start_tag, "end": metadata.end_tag},
            "releases": {"total": len(releases), "tags": [r.tag for r in releases]},
            "pull_requests": {"total": metadata.total_prs, "by_state": pr_states},
            "issues": {"total": metadata.total_issues, "by_state": issue_states},
        }


def extract_from_directory(data_dir: str) -> DataExtractor:
    """
    Convenience function to create a DataExtractor instance.

    Args:
        data_dir: Path to the data directory

    Returns:
        DataExtractor instance
    """
    return DataExtractor(data_dir)
