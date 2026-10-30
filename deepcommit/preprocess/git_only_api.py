"""Platform-free stand-in for ``GitHubAPI``: answers everything from a local clone.

Used for repositories that are not on GitHub (Bitbucket, GitLab, self-hosted) with ``gh_crawler.py --platform git``.
Commits, tags and parents come from git. Pull requests are rebuilt from merge commits whose subject names a pull
request: Bitbucket ``Merged in <branch> (pull request #N)`` and GitHub ``Merge pull request #N from ...``; after
``linearize.py`` the same subject sits on a single squashed commit, which is then the whole pull request. Issues,
comments, labels and reviews do not exist locally and come back empty.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_PR_SUBJECT = re.compile(r"\((?:pull request|PR) #(\d+)\)|^Merge pull request #(\d+)", re.IGNORECASE)


class GitOnlyAPI:
    def __init__(self, owner: str, repo: str, token: str = "", repo_path: str | Path = ".", main_branch: str = "main"):
        self.owner, self.repo, self.main_branch = owner, repo, main_branch
        self.path = Path(repo_path)
        self._prs: Optional[Dict[int, str]] = None

    def _git(self, *args: str) -> str:
        r = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True, text=True, check=True)
        return r.stdout.strip()

    def print_rate_limit_status(self):
        print("git-only platform: no API, no rate limit")

    def get_default_branch(self) -> str:
        return self.main_branch

    def get_tag_sha(self, tag: Optional[str]) -> Optional[str]:
        if not tag:
            return None
        try:
            return self._git("rev-parse", f"{tag}^{{commit}}")
        except subprocess.CalledProcessError:
            raise ValueError(f"tag not found: {tag}") from None

    def get_tag_shas(self, tag: str) -> Tuple[str, str]:
        sha = self.get_tag_sha(tag)
        return sha, sha

    def get_all_tags(self, filter_by_commits: Optional[Set[str]] = None) -> List[Tuple[str, str]]:
        out = self._git("for-each-ref", "--format=%(refname:short)\t%(*objectname)%(objectname)\t%(creatordate:iso-strict)", "refs/tags")
        tags = []
        for line in out.splitlines():
            name, sha, date = line.split("\t")
            if filter_by_commits and self.get_tag_sha(name) not in filter_by_commits:
                continue
            tags.append((name, date))
        return sorted(tags, key=lambda t: t[1])

    def get_release_info(self, tag: str) -> Dict[str, Any]:
        sha = self.get_tag_sha(tag) or ""
        date = self._git("log", "-1", "--format=%cI", sha) if sha else ""
        return {
            "tag": tag,
            "tag_sha": sha,
            "commit_sha": sha,
            "name": tag,
            "body": "",
            "draft": False,
            "prerelease": False,
            "created_at": date,
            "published_at": date,
            "author": self._git("log", "-1", "--format=%an", sha) if sha else None,
            "url": "",
        }

    def get_commits_between_tags(self, from_tag: Optional[str], to_tag: Optional[str]) -> List[str]:
        end = self.get_tag_sha(to_tag) if to_tag in self._tag_names() else to_tag
        rng = f"{self.get_tag_sha(from_tag)}..{end}" if from_tag else end
        return self._git("rev-list", rng).splitlines()

    def _tag_names(self) -> Set[str]:
        return set(self._git("tag").splitlines())

    def get_commit_parents(self, commit_sha: str) -> List[str]:
        return self._git("rev-list", "--parents", "-n", "1", commit_sha).split()[1:]

    def get_commit_details(self, commit_sha: str) -> Dict[str, Any]:
        fmt = "%H%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%P%x00%B"
        h, an, ae, ad, cn, ce, cd, parents, msg = self._git("show", "-s", f"--format={fmt}", commit_sha).split("\x00", 8)
        return {
            "sha": h,
            "commit": {"message": msg, "author": {"name": an, "email": ae, "date": ad}, "committer": {"name": cn, "email": ce, "date": cd}},
            "parents": [{"sha": p} for p in parents.split()],
            "files": [],
        }

    def _pr_index(self) -> Dict[int, str]:
        """PR number -> merge commit sha."""
        if self._prs is None:
            self._prs = {}
            for line in self._git("log", "--first-parent", "--format=%H%x00%s", self.main_branch).splitlines():
                sha, subject = line.split("\x00", 1)
                m = _PR_SUBJECT.search(subject)
                if m:
                    self._prs[int(m.group(1) or m.group(2))] = sha
        return self._prs

    def get_prs_for_commit(self, sha: str) -> List[int]:
        return [n for n, merge in self._pr_index().items() if sha in self.get_pr_commits(n)]

    def get_pr_commits(self, pr_number: int) -> List[str]:
        merge = self._pr_index().get(pr_number)
        if not merge:
            return []
        parents = self.get_commit_parents(merge)
        if len(parents) < 2:  # linearized or squash-merged: the commit is the whole pull request
            return [merge]
        return self._git("rev-list", f"{parents[0]}..{parents[1]}").splitlines()

    def get_pr_details(self, pr_number: int) -> Dict[str, Any]:
        merge = self._pr_index().get(pr_number)
        if not merge:
            return {}
        parents = self.get_commit_parents(merge)
        message = self._git("show", "-s", "--format=%B", merge)
        body_lines = [ln for ln in message.splitlines()[1:] if ln.strip() and not ln.startswith("Approved-by")]
        date = self._git("show", "-s", "--format=%cI", merge)
        commits = self.get_pr_commits(pr_number)
        return {
            "number": pr_number,
            "title": body_lines[0] if body_lines else message.splitlines()[0],
            "body": "\n".join(body_lines),
            "state": "closed",
            "created_at": date,
            "updated_at": date,
            "closed_at": date,
            "merged_at": date,
            "merge_commit_sha": merge,
            "base_sha": parents[0] if parents else None,
            "base_ref": self.main_branch,
            "head_sha": parents[1] if len(parents) > 1 else merge,
            "head_ref": None,
            "user": self._git("show", "-s", "--format=%an", merge),
            "url": "",
            "merged": True,
            "labels": [],
            "related_issues": [],
            "conversations": {},
            "events": [],
            "commits": commits,
            "commits_count": len(commits),
        }

    def get_pr_conversations(self, pr_number: int) -> Dict[str, Any]:
        return {}

    def get_pr_events(self, pr_number: int) -> List[Dict[str, Any]]:
        return []

    def get_pr_closing_issues(self, pr_number: int) -> List[int]:
        return []

    def get_issue_or_pr_type(self, number: int) -> str:
        return "pr" if number in self._pr_index() else "not_found"

    def get_issue_details(self, issue_number: int) -> Dict[str, Any]:
        return {}

    def get_issue_conversations(self, issue_number: int) -> Dict[str, Any]:
        return {}

    def get_issue_events(self, issue_number: int) -> List[Dict[str, Any]]:
        return []
