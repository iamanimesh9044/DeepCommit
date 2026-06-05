#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GitHub API工具类 - 处理所有与GitHub API相关的操作
"""

import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from requests.adapters import HTTPAdapter

# GitHub关键词用于识别PR解决的issue
PR_KEYWORDS = {
    "close",
    "closes",
    "closed",
    "fix",
    "fixes",
    "fixed",
    "resolve",
    "resolves",
    "resolved",
    "address",
    "addresses",
    "addressed",
    "implement",
    "implements",
    "implemented",
    "related",
    "relates",
    "related to",
    "refs",
    "ref",
    "references",
    "see",
    "for",
    "re",
}


class GitHubAPI:
    """GitHub API操作封装类"""

    API = "https://api.github.com"

    def __init__(self, owner: str, repo: str, token: str):
        self.owner = owner
        self.repo = repo

        # 解析 token（支持逗号分隔的多个 token）
        self.tokens = [t.strip() for t in token.split(",") if t.strip()]
        self.current_token_index = 0

        # 为每个 token 创建 session
        self.sessions = [self._make_session(t) for t in self.tokens]

        if len(self.tokens) > 1:
            print(f"✓ GitHubAPI 初始化: 使用 {len(self.tokens)} 个 token(s)")
        else:
            print("✓ GitHubAPI 初始化: 使用 1 个 token")

    @property
    def session(self):
        """获取当前 token 对应的 session"""
        return self.sessions[self.current_token_index]

    def _make_session(self, token: str) -> requests.Session:
        """创建HTTP会话"""
        s = requests.Session()
        s.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": "release-collector",
            }
        )
        if token:
            s.headers["Authorization"] = f"Bearer {token}"

        # 启用连接池
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=3)
        s.mount("https://", adapter)
        return s

    def _rotate_token(self) -> bool:
        """切换到下一个 token"""
        if len(self.tokens) == 1:
            return False  # 只有一个 token，无法切换

        old_index = self.current_token_index
        self.current_token_index = (self.current_token_index + 1) % len(self.tokens)
        print(f"⚠️  Token #{old_index + 1} 达到 rate limit，切换到 Token #{self.current_token_index + 1}")
        return True

    def _get_json(self, url: str, params: dict = {}, retries: int = 3) -> requests.Response:  # noqa: B006
        """发送GET请求并处理速率限制 - 支持多 token 自动切换"""
        for i in range(retries):
            try:
                r = self.session.get(url, params=params, timeout=30)
                if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
                    # 尝试切换到下一个 token
                    if self._rotate_token():
                        continue  # 用新 token 重试
                    else:
                        # 只有一个 token，需要等待
                        reset = int(r.headers.get("X-RateLimit-Reset", "0") or 0)
                        sleep_s = max(reset - int(time.time()) + 1, 1)
                        print(f"速率限制，等待 {sleep_s} 秒...")
                        time.sleep(sleep_s)
                        continue
                if r.status_code >= 500:
                    time.sleep(1.5 * (i + 1))
                    continue
                r.raise_for_status()
                return r
            except requests.exceptions.ConnectionError:
                if i < retries - 1:
                    time.sleep(2 * (i + 1))
                    continue
                raise
        r.raise_for_status()
        return r

    def print_rate_limit_status(self):
        """打印所有 token 的 rate limit 状态"""
        print("\n" + "=" * 70)
        print("GitHub API Rate Limit 状态:")
        print("=" * 70)

        for i in range(len(self.sessions)):
            # 临时切换到该 token
            old_index = self.current_token_index
            self.current_token_index = i

            try:
                url = f"{self.API}/rate_limit"
                r = self.session.get(url, timeout=10)
                info = r.json()

                if "rate" in info:
                    rate = info["rate"]
                    remaining = rate["remaining"]
                    limit = rate["limit"]
                    reset_time = time.strftime("%H:%M:%S", time.localtime(rate["reset"]))

                    # 状态标记
                    if remaining > 1000:
                        status = "✓"
                    elif remaining > 100:
                        status = "⚠️"
                    else:
                        status = "✗"

                    current_mark = " (当前)" if i == old_index else ""
                    print(f"  Token #{i + 1}{current_mark}: {status} {remaining}/{limit} (重置: {reset_time})")
                else:
                    print(f"  Token #{i + 1}: 无法获取状态")
            except Exception as e:
                print(f"  Token #{i + 1}: 获取失败 ({e})")
            finally:
                # 恢复原 token
                self.current_token_index = old_index

        print("=" * 70 + "\n")

    def get_tag_sha(self, tag: Optional[str]) -> Optional[str]:
        """获取tag对应的commit SHA"""
        tag_url = f"{self.API}/repos/{self.owner}/{self.repo}/git/refs/tags/{tag}"
        try:
            ref = self._get_json(tag_url).json()
            sha = ref["object"]["sha"]

            # 检查是否是annotated tag
            tag_obj_url = f"{self.API}/repos/{self.owner}/{self.repo}/git/tags/{sha}"
            tag_obj = self._get_json(tag_obj_url).json()

            if tag_obj.get("object", {}).get("type") == "commit":
                return tag_obj["object"]["sha"]
            else:
                return sha
        except:  # noqa: E722
            # 尝试通过release获取
            release_url = f"{self.API}/repos/{self.owner}/{self.repo}/releases/tags/{tag}"
            try:
                release = self._get_json(release_url).json()
                return release["target_commitish"]
            except:  # noqa: E722
                raise ValueError(f"无法找到tag: {tag}")  # noqa: B904

    def get_prs_for_commit(self, sha: str) -> List[int]:
        """获取与commit关联的PR编号"""
        url = f"{self.API}/repos/{self.owner}/{self.repo}/commits/{sha}/pulls"
        r = self._get_json(url)
        prs = r.json()
        return [pr["number"] for pr in prs if pr.get("merged_at")]

    def get_all_tags(self, filter_by_commits: Optional[Set[str]] = None) -> List[Tuple[str, str]]:
        """获取所有tags，可选择基于commit集合过滤

        Returns:
            [(tag_name, date), ...] 按日期排序
        """
        tags = []
        url = f"{self.API}/repos/{self.owner}/{self.repo}/tags"

        # 首先收集所有tags
        all_tag_data = []
        while url:
            r = self._get_json(url, params={"per_page": 100} if "?" not in url else {})
            tag_data = r.json()
            all_tag_data.extend(tag_data)

            if r.links and "next" in r.links:
                url = r.links["next"]["url"]
            else:
                url = None

        # 如果需要过滤
        if filter_by_commits:
            print(f"基于 {len(filter_by_commits)} 个commits过滤tags...")
            filtered_tag_data = []
            for tag in all_tag_data:
                # 获取tag对应的实际commit SHA
                commit_sha = tag["commit"]["sha"]
                try:
                    # 对于annotated tag，需要获取实际的commit SHA
                    tag_obj_url = f"{self.API}/repos/{self.owner}/{self.repo}/git/tags/{commit_sha}"
                    tag_obj = self._get_json(tag_obj_url).json()
                    if tag_obj.get("object", {}).get("type") == "commit":
                        actual_commit_sha = tag_obj["object"]["sha"]
                    else:
                        actual_commit_sha = commit_sha
                except:  # noqa: E722
                    actual_commit_sha = commit_sha

                # 检查commit是否在过滤集合中
                if actual_commit_sha in filter_by_commits:
                    filtered_tag_data.append(tag)

            print(f"过滤后保留 {len(filtered_tag_data)} 个tags（原有 {len(all_tag_data)} 个）")
            all_tag_data = filtered_tag_data

        # 使用进度条获取每个tag的日期
        try:
            from tqdm import tqdm

            with tqdm(total=len(all_tag_data), desc="获取tag日期") as pbar:
                for tag in all_tag_data:
                    # 获取tag的日期
                    commit_url = f"{self.API}/repos/{self.owner}/{self.repo}/commits/{tag['commit']['sha']}"
                    commit = self._get_json(commit_url).json()
                    date = commit["commit"]["committer"]["date"]
                    tags.append((tag["name"], date))
                    pbar.update(1)
        except ImportError:
            # 如果没有tqdm，正常处理
            for tag in all_tag_data:
                commit_url = f"{self.API}/repos/{self.owner}/{self.repo}/commits/{tag['commit']['sha']}"
                commit = self._get_json(commit_url).json()
                date = commit["commit"]["committer"]["date"]
                tags.append((tag["name"], date))

        # 按日期排序（从旧到新）
        tags.sort(key=lambda x: x[1])
        return tags

    def get_tag_shas(self, tag: str) -> tuple[str, str]:
        """获取tag的SHA和它指向的commit SHA

        Returns:
            (tag_sha, commit_sha) tuple
            - tag_sha: tag对象本身的SHA（annotated tag）或commit SHA（lightweight tag）
            - commit_sha: tag指向的commit SHA
        """
        tag_url = f"{self.API}/repos/{self.owner}/{self.repo}/git/refs/tags/{tag}"
        try:
            ref = self._get_json(tag_url).json()
            tag_sha = ref["object"]["sha"]
            tag_type = ref["object"]["type"]

            if tag_type == "commit":
                # Lightweight tag: 直接指向commit
                return tag_sha, tag_sha
            else:
                # Annotated tag: 需要获取tag对象来找到实际的commit
                tag_obj_url = f"{self.API}/repos/{self.owner}/{self.repo}/git/tags/{tag_sha}"
                tag_obj = self._get_json(tag_obj_url).json()
                commit_sha = tag_obj["object"]["sha"]
                return tag_sha, commit_sha
        except Exception as e:
            # 如果获取失败，返回空字符串
            print(f"Warning: Failed to get tag SHAs for {tag}: {e}")
            return "", ""

    def get_release_info(self, tag: str) -> Dict[str, Any]:
        """获取release信息，包括release notes和tag SHAs"""
        # 获取tag的SHA信息
        tag_sha, commit_sha = self.get_tag_shas(tag)

        # 尝试获取release
        release_url = f"{self.API}/repos/{self.owner}/{self.repo}/releases/tags/{tag}"
        try:
            release = self._get_json(release_url).json()
            return {
                "tag": tag,
                "tag_sha": tag_sha,
                "commit_sha": commit_sha,
                "name": release.get("name", ""),
                "body": release.get("body", ""),  # release notes
                "draft": release.get("draft", False),
                "prerelease": release.get("prerelease", False),
                "created_at": release.get("created_at"),
                "published_at": release.get("published_at"),
                "author": release.get("author", {}).get("login") if release.get("author") else None,
                "url": release.get("html_url"),
            }
        except:  # noqa: E722
            # 如果没有release，尝试获取tag信息
            tag_url = f"{self.API}/repos/{self.owner}/{self.repo}/git/refs/tags/{tag}"
            try:
                ref = self._get_json(tag_url).json()
                sha = ref["object"]["sha"]

                # 获取tag对象详情
                tag_obj_url = f"{self.API}/repos/{self.owner}/{self.repo}/git/tags/{sha}"
                tag_obj = self._get_json(tag_obj_url).json()

                return {
                    "tag": tag,
                    "tag_sha": tag_sha,
                    "commit_sha": commit_sha,
                    "name": tag,
                    "body": tag_obj.get("message", ""),  # tag message as release notes
                    "draft": False,
                    "prerelease": False,
                    "created_at": None,
                    "published_at": None,
                    "author": tag_obj.get("tagger", {}).get("name") if tag_obj.get("tagger") else None,
                    "url": f"https://github.com/{self.owner}/{self.repo}/releases/tag/{tag}",
                }
            except:  # noqa: E722
                return {
                    "tag": tag,
                    "tag_sha": tag_sha,
                    "commit_sha": commit_sha,
                    "name": tag,
                    "body": "",
                    "draft": False,
                    "prerelease": False,
                    "created_at": None,
                    "published_at": None,
                    "author": None,
                    "url": f"https://github.com/{self.owner}/{self.repo}/releases/tag/{tag}",
                }

    def get_default_branch(self) -> str:
        """获取仓库的默认分支"""
        url = f"{self.API}/repos/{self.owner}/{self.repo}"
        r = self._get_json(url)
        return r.json().get("default_branch", "main")

    def get_commits_between_tags(self, from_tag: Optional[str], to_tag: Optional[str]) -> List[str]:
        """获取两个tag之间的commits

        Args:
            from_tag: 起始tag，可以为None表示从头开始
            to_tag: 结束tag，也可以是分支名
        """
        # 获取tag的commit SHA
        from_sha = self.get_tag_sha(from_tag) if from_tag else None

        # to_tag可能是tag也可能是分支名
        try:
            to_sha = self.get_tag_sha(to_tag)
        except:  # noqa: E722
            # 如果不是tag，可能是分支名
            to_sha = to_tag

        if from_sha:
            url = f"{self.API}/repos/{self.owner}/{self.repo}/compare/{from_sha}...{to_sha}"
        else:
            # 如果没有起始tag，获取到目标tag的所有commits
            url = f"{self.API}/repos/{self.owner}/{self.repo}/commits"
            params = {"sha": to_sha, "per_page": 100}

        commits = []

        if from_sha:
            # 使用compare API
            while url:
                r = self._get_json(url, params={"per_page": 100} if "?" not in url else {})
                data = r.json()
                commits.extend([c["sha"] for c in data.get("commits", [])])

                if r.links and "next" in r.links:
                    url = r.links["next"]["url"]
                else:
                    url = None
        else:
            # 使用commits API
            while url:
                r = self._get_json(url, params=params if "?" not in url else {})
                data = r.json()
                commits.extend([c["sha"] for c in data])

                if r.links and "next" in r.links:
                    url = r.links["next"]["url"]
                    params = None  # 后续请求不需要params
                else:
                    url = None

        return commits

    def get_pr_commits(self, pr_number: int) -> List[str]:
        """获取PR包含的commits"""
        url = f"{self.API}/repos/{self.owner}/{self.repo}/pulls/{pr_number}/commits"
        commits = []

        while url:
            r = self._get_json(url, params={"per_page": 100} if "?" not in url else {})
            data = r.json()
            commits.extend([c["sha"] for c in data])

            if r.links and "next" in r.links:
                url = r.links["next"]["url"]
            else:
                url = None

        return commits

    def get_commit_parents(self, commit_sha: str) -> List[str]:
        """获取指定commit的parent commits列表"""
        url = f"{self.API}/repos/{self.owner}/{self.repo}/commits/{commit_sha}"
        r = self._get_json(url)
        data = r.json()

        parents = data.get("parents", [])
        return [parent["sha"] for parent in parents]

    def get_pr_conversations(self, pr_number: int) -> Dict[str, Any]:
        """获取PR的所有对话（评论、reviews、review comments）"""
        conversations = {"comments": [], "reviews": [], "review_comments": []}

        # 获取issue comments（一般讨论）
        url = f"{self.API}/repos/{self.owner}/{self.repo}/issues/{pr_number}/comments"
        while url:
            r = self._get_json(url, params={"per_page": 100} if "?" not in url else {})
            comments = r.json()
            for comment in comments:
                conversations["comments"].append(
                    {
                        "id": comment["id"],
                        "user": comment["user"]["login"] if comment.get("user") else None,
                        "created_at": comment["created_at"],
                        "updated_at": comment["updated_at"],
                        "body": comment["body"],
                        "author_association": comment.get("author_association"),
                        "reactions": comment.get("reactions", {}),
                    }
                )
            if r.links and "next" in r.links:
                url = r.links["next"]["url"]
            else:
                url = None

        # 获取PR reviews
        url = f"{self.API}/repos/{self.owner}/{self.repo}/pulls/{pr_number}/reviews"
        while url:
            r = self._get_json(url, params={"per_page": 100} if "?" not in url else {})
            reviews = r.json()
            for review in reviews:
                conversations["reviews"].append(
                    {
                        "id": review["id"],
                        "user": review["user"]["login"] if review.get("user") else None,
                        "state": review["state"],  # APPROVED, CHANGES_REQUESTED, COMMENTED
                        "submitted_at": review.get("submitted_at"),
                        "body": review.get("body", ""),
                        "author_association": review.get("author_association"),
                    }
                )
            if r.links and "next" in r.links:
                url = r.links["next"]["url"]
            else:
                url = None

        # 获取review comments（代码行内评论）
        url = f"{self.API}/repos/{self.owner}/{self.repo}/pulls/{pr_number}/comments"
        while url:
            r = self._get_json(url, params={"per_page": 100} if "?" not in url else {})
            comments = r.json()
            for comment in comments:
                conversations["review_comments"].append(
                    {
                        "id": comment["id"],
                        "user": comment["user"]["login"] if comment.get("user") else None,
                        "path": comment.get("path"),
                        "line": comment.get("line"),
                        "side": comment.get("side"),  # LEFT or RIGHT
                        "start_line": comment.get("start_line"),
                        "original_line": comment.get("original_line"),
                        "created_at": comment["created_at"],
                        "updated_at": comment["updated_at"],
                        "body": comment["body"],
                        "diff_hunk": comment.get("diff_hunk"),
                        "author_association": comment.get("author_association"),
                        "in_reply_to_id": comment.get("in_reply_to_id"),
                    }
                )
            if r.links and "next" in r.links:
                url = r.links["next"]["url"]
            else:
                url = None

        return conversations

    def get_pr_events(self, pr_number: int) -> List[Dict[str, Any]]:
        """获取PR的所有事件"""
        events = []
        url = f"{self.API}/repos/{self.owner}/{self.repo}/issues/{pr_number}/events"  # PR events使用issues端点

        while url:
            r = self._get_json(url, params={"per_page": 100} if "?" not in url else {})
            pr_events = r.json()
            for event in pr_events:
                event_data = {
                    "id": event["id"],
                    "event": event["event"],
                    "created_at": event["created_at"],
                    "actor": event.get("actor", {}).get("login") if event.get("actor") else None,
                }

                # 添加事件特定数据
                if event["event"] in ["labeled", "unlabeled"]:
                    event_data["label"] = event.get("label", {}).get("name") if event.get("label") else None
                elif event["event"] in ["assigned", "unassigned"]:
                    event_data["assignee"] = event.get("assignee", {}).get("login") if event.get("assignee") else None
                elif event["event"] in ["review_requested", "review_request_removed"]:
                    if event.get("requested_reviewer"):
                        event_data["reviewer"] = event["requested_reviewer"].get("login")
                    elif event.get("requested_team"):
                        event_data["team"] = event["requested_team"].get("name")
                elif event["event"] in ["milestoned", "demilestoned"]:
                    event_data["milestone"] = (
                        event.get("milestone", {}).get("title") if event.get("milestone") else None
                    )
                elif event["event"] == "renamed":
                    event_data["rename"] = event.get("rename", {})
                elif event["event"] == "merged":
                    event_data["commit_id"] = event.get("commit_id")
                    event_data["commit_url"] = event.get("commit_url")
                elif event["event"] == "closed":
                    if event.get("commit_id"):
                        event_data["commit_id"] = event["commit_id"]
                        event_data["commit_url"] = event.get("commit_url")
                elif event["event"] == "referenced":
                    event_data["commit_id"] = event.get("commit_id")
                elif event["event"] == "head_ref_force_pushed":
                    event_data["before"] = event.get("before")
                    event_data["after"] = event.get("after")

                events.append(event_data)

            if r.links and "next" in r.links:
                url = r.links["next"]["url"]
            else:
                url = None

        return events

    def get_pr_details(self, pr_number: int) -> Dict[str, Any]:
        """获取PR详细信息（包含conversations和events）"""
        url = f"{self.API}/repos/{self.owner}/{self.repo}/pulls/{pr_number}"
        try:
            pr = self._get_json(url).json()

            # 提取关联的issues（从 body 和 title 中提取）
            related_issues = self._extract_issue_references(pr.get("body", ""))
            related_issues.extend(self._extract_issue_references(pr.get("title", "")))

            # 通过 Timeline API 获取显式关联的 issues
            closing_issues = self.get_pr_closing_issues(pr_number)
            related_issues.extend(closing_issues)

            # 获取conversations、events和commits
            conversations = self.get_pr_conversations(pr_number)
            events = self.get_pr_events(pr_number)
            commits = self.get_pr_commits(pr_number)

            return {
                "number": pr_number,
                "title": pr.get("title", ""),
                "body": pr.get("body", ""),
                "state": pr.get("state"),
                "created_at": pr.get("created_at"),
                "updated_at": pr.get("updated_at"),
                "closed_at": pr.get("closed_at"),
                "merged_at": pr.get("merged_at"),
                "merge_commit_sha": pr.get("merge_commit_sha"),
                "base_sha": pr.get("base", {}).get("sha") if pr.get("base") else None,
                "base_ref": pr.get("base", {}).get("ref") if pr.get("base") else None,
                "head_sha": pr.get("head", {}).get("sha") if pr.get("head") else None,
                "head_ref": pr.get("head", {}).get("ref") if pr.get("head") else None,
                "user": pr.get("user", {}).get("login") if pr.get("user") else None,
                "author_association": pr.get("author_association"),
                "url": pr.get("html_url"),
                "merged": pr.get("merged", False),
                "merged_by": pr.get("merged_by", {}).get("login") if pr.get("merged_by") else None,
                "additions": pr.get("additions", 0),
                "deletions": pr.get("deletions", 0),
                "changed_files": pr.get("changed_files", 0),
                "draft": pr.get("draft", False),
                "mergeable": pr.get("mergeable"),
                "mergeable_state": pr.get("mergeable_state"),
                "rebaseable": pr.get("rebaseable"),
                "maintainer_can_modify": pr.get("maintainer_can_modify", False),
                "labels": [label["name"] for label in pr.get("labels", [])],
                "assignees": [a["login"] for a in pr.get("assignees", [])],
                "requested_reviewers": [r["login"] for r in pr.get("requested_reviewers", [])],
                "requested_teams": [t["name"] for t in pr.get("requested_teams", [])],
                "milestone": pr.get("milestone", {}).get("title") if pr.get("milestone") else None,
                "related_issues": list(set(related_issues)),  # 去重
                "conversations": conversations,
                "events": events,
                "comments_count": pr.get("comments", 0),
                "review_comments_count": pr.get("review_comments", 0),
                "commits": commits,  # PR包含的commit SHA列表
                "commits_count": len(commits),
            }
        except Exception as e:
            print(f"错误获取PR #{pr_number}: {e}")
            return {}

    def get_issue_conversations(self, issue_number: int) -> Dict[str, Any]:
        """获取issue的所有对话"""
        conversations = {"comments": [], "reactions": []}

        # 获取issue comments
        url = f"{self.API}/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments"
        while url:
            r = self._get_json(url, params={"per_page": 100} if "?" not in url else {})
            comments = r.json()
            for comment in comments:
                conversations["comments"].append(
                    {
                        "id": comment["id"],
                        "user": comment["user"]["login"] if comment.get("user") else None,
                        "author_association": comment.get("author_association"),
                        "created_at": comment["created_at"],
                        "updated_at": comment["updated_at"],
                        "body": comment["body"],
                        "reactions": comment.get("reactions", {}),
                    }
                )
            if r.links and "next" in r.links:
                url = r.links["next"]["url"]
            else:
                url = None

        # 获取issue reactions
        try:
            url = f"{self.API}/repos/{self.owner}/{self.repo}/issues/{issue_number}/reactions"
            r = self._get_json(url, params={"per_page": 100})
            reactions = r.json()
            for reaction in reactions:
                conversations["reactions"].append(
                    {
                        "user": reaction["user"]["login"] if reaction.get("user") else None,
                        "content": reaction["content"],
                        "created_at": reaction["created_at"],
                    }
                )
        except:  # noqa: E722
            pass  # Reactions API可能不可用

        return conversations

    def get_issue_events(self, issue_number: int) -> List[Dict[str, Any]]:
        """获取issue的所有事件"""
        events = []
        url = f"{self.API}/repos/{self.owner}/{self.repo}/issues/{issue_number}/events"

        while url:
            r = self._get_json(url, params={"per_page": 100} if "?" not in url else {})
            issue_events = r.json()
            for event in issue_events:
                event_data = {
                    "id": event["id"],
                    "event": event["event"],
                    "created_at": event["created_at"],
                    "actor": event.get("actor", {}).get("login") if event.get("actor") else None,
                }

                # 添加事件特定数据
                if event["event"] in ["labeled", "unlabeled"]:
                    event_data["label"] = event.get("label", {}).get("name") if event.get("label") else None
                elif event["event"] in ["assigned", "unassigned"]:
                    event_data["assignee"] = event.get("assignee", {}).get("login") if event.get("assignee") else None
                elif event["event"] in ["milestoned", "demilestoned"]:
                    event_data["milestone"] = (
                        event.get("milestone", {}).get("title") if event.get("milestone") else None
                    )
                elif event["event"] == "renamed":
                    event_data["rename"] = event.get("rename", {})
                elif event["event"] == "closed":
                    if event.get("commit_id"):
                        event_data["commit_id"] = event["commit_id"]
                        event_data["commit_url"] = event.get("commit_url")
                    event_data["state_reason"] = event.get("state_reason")  # completed, not_planned, reopened
                elif event["event"] == "reopened":
                    pass  # 基本信息已包含
                elif event["event"] == "cross-referenced":
                    if event.get("source"):
                        event_data["source"] = {
                            "type": event["source"].get("type"),
                            "issue": (
                                event["source"].get("issue", {}).get("number") if event["source"].get("issue") else None
                            ),
                        }
                elif event["event"] == "referenced":
                    event_data["commit_id"] = event.get("commit_id")
                elif event["event"] == "locked":
                    event_data["lock_reason"] = event.get("lock_reason")
                elif event["event"] == "transferred":
                    event_data["transferred_to"] = event.get("transferred_to")

                events.append(event_data)

            if r.links and "next" in r.links:
                url = r.links["next"]["url"]
            else:
                url = None

        return events

    def get_issue_or_pr_type(self, number: int) -> str:
        """
        判断编号是 PR、Issue 还是不存在

        GitHub 的 Issue 和 PR 共享同一个编号空间。
        /issues/{number} API 对两者都返回数据，但 PR 会有 pull_request 字段。

        Args:
            number: GitHub issue/PR 编号

        Returns:
            "pr" | "issue" | "not_found"
        """
        url = f"{self.API}/repos/{self.owner}/{self.repo}/issues/{number}"
        try:
            resp = self._get_json(url)
            data = resp.json()

            if "pull_request" in data:
                return "pr"
            else:
                return "issue"
        except Exception as e:
            error_msg = str(e)
            if "404" in error_msg:
                return "not_found"
            # 其他错误（如 rate limit）重新抛出
            raise

    def get_issue_details(self, issue_number: int) -> Dict[str, Any]:
        """获取issue详细信息（包含conversations和events）"""
        url = f"{self.API}/repos/{self.owner}/{self.repo}/issues/{issue_number}"
        try:
            issue = self._get_json(url).json()

            # 检查是否是PR（GitHub的issues API也返回PR）
            if "pull_request" in issue:
                return {}

            # 获取conversations和events
            conversations = self.get_issue_conversations(issue_number)
            events = self.get_issue_events(issue_number)

            return {
                "number": issue_number,
                "title": issue.get("title", ""),
                "body": issue.get("body", ""),
                "state": issue.get("state"),
                "state_reason": issue.get("state_reason"),  # completed, not_planned, reopened
                "created_at": issue.get("created_at"),
                "updated_at": issue.get("updated_at"),
                "closed_at": issue.get("closed_at"),
                "user": issue.get("user", {}).get("login") if issue.get("user") else None,
                "author_association": issue.get("author_association"),
                "url": issue.get("html_url"),
                "labels": [label["name"] for label in issue.get("labels", [])],
                "assignees": [a["login"] for a in issue.get("assignees", [])],
                "milestone": issue.get("milestone", {}).get("title") if issue.get("milestone") else None,
                "closed_by": issue.get("closed_by", {}).get("login") if issue.get("closed_by") else None,
                "locked": issue.get("locked", False),
                "active_lock_reason": issue.get("active_lock_reason"),
                "comments_count": issue.get("comments", 0),
                "reactions": issue.get("reactions", {}),
                "conversations": conversations,
                "events": events,
            }
        except Exception as e:
            print(f"错误获取issue #{issue_number}: {e}")
            return {}

    def _extract_issue_references(self, text: str) -> List[int]:
        """从文本中提取本仓库的issue引用"""
        if not text:
            return []

        issues = set()

        # 移除HTML注释
        text = re.sub(r"(?s)<!--.*?-->", "", text)

        # 策略：采用白名单方式，只保留本仓库的引用

        # 1. 找出所有 owner/repo#123 格式的引用
        cross_repo_refs = re.findall(r"([\w-]+)/([\w-]+)#(\d+)", text)
        for owner, repo, issue_num in cross_repo_refs:
            # 只保留本仓库的引用（大小写不敏感）
            if owner.lower() == self.owner.lower() and repo.lower() == self.repo.lower():
                issues.add(int(issue_num))

        # 2. 找出所有完整URL格式的引用
        # https://github.com/owner/repo/issues/123 或 /pull/123
        url_pattern = r"https?://github\.com/([\w-]+)/([\w-]+)/(?:issues|pull)/(\d+)"
        url_refs = re.findall(url_pattern, text)
        for owner, repo, issue_num in url_refs:
            # 只保留本仓库的引用（大小写不敏感）
            if owner.lower() == self.owner.lower() and repo.lower() == self.repo.lower():
                issues.add(int(issue_num))

        # 3. 处理简单的 #123 格式 - 改进的过滤逻辑
        # 先创建一个副本，移除已经被 owner/repo#123 匹配的部分
        text_clean = text
        # 移除所有 owner/repo#123 格式（包括本仓库的），避免重复匹配
        text_clean = re.sub(r"[\w-]+/[\w-]+#\d+", "", text_clean)

        # 移除所有其他仓库的URL内容，避免提取其中的 #123 引用
        # 这包括链接文本如: <a href="https://github.com/other/repo/issues/123">#123</a>
        # 或者 href="https://redirect.github.com/other/repo/issues/123">#123</a>
        other_repo_url_pattern = (
            r'href=["\']https?://(?:redirect\.)?github\.com/([\w-]+)/([\w-]+)/(?:issues|pull)/\d+["\'][^>]*>#\d+</a>'
        )

        def url_filter(match):
            owner, repo = match.group(1), match.group(2)
            # 如果是其他仓库的URL，移除整个链接
            if not (owner.lower() == self.owner.lower() and repo.lower() == self.repo.lower()):
                return ""
            # 如果是本仓库的URL，保留
            return match.group(0)

        text_clean = re.sub(other_repo_url_pattern, url_filter, text_clean)

        # 也移除commit URLs中的引用
        commit_url_pattern = r'href=["\']https?://(?:redirect\.)?github\.com/([\w-]+)/([\w-]+)/(?:commit|commits)/[a-f0-9]+["\'][^>]*>\([^)]*#\d+[^)]*\)</a>'
        text_clean = re.sub(commit_url_pattern, url_filter, text_clean)

        # 匹配独立的 #123（不被其他字符包围）
        simple_refs = re.findall(r"(?<![/\w#])#(\d+)(?!\w)", text_clean)
        issues.update(int(ref) for ref in simple_refs)

        # 4. 匹配关键词 + #123 格式（fixes #123, closes #123等）
        # 支持关键词后跟冒号、空格或换行
        keyword_pattern = r"(?:" + "|".join(PR_KEYWORDS) + r")[\s:]+#(\d+)"
        keyword_refs = re.findall(keyword_pattern, text_clean, re.IGNORECASE)
        issues.update(int(ref) for ref in keyword_refs)

        # 5. 匹配关键词 + owner/repo#123 格式（只保留本仓库的）
        keyword_cross_pattern = r"(?:" + "|".join(PR_KEYWORDS) + r")[\s:]+([\w-]+)/([\w-]+)#(\d+)"
        keyword_cross_refs = re.findall(keyword_cross_pattern, text, re.IGNORECASE)
        for owner, repo, issue_num in keyword_cross_refs:
            if owner.lower() == self.owner.lower() and repo.lower() == self.repo.lower():
                issues.add(int(issue_num))

        # 6. 匹配多个连续的 issue 引用（如 "fixes #123, #456, #789"）
        multi_issue_pattern = r"(?:" + "|".join(PR_KEYWORDS) + r")[\s:]+(?:#\d+[,\s]+)*#(\d+)"
        multi_refs = re.findall(multi_issue_pattern, text_clean, re.IGNORECASE)
        issues.update(int(ref) for ref in multi_refs)

        # 7. 匹配 Markdown 链接中的 issue 引用 [#123](url) 或 [Issue #123](url)
        md_link_pattern = r"\[(?:Issue\s*)?#(\d+)\]\([^)]+\)"
        md_refs = re.findall(md_link_pattern, text_clean, re.IGNORECASE)
        issues.update(int(ref) for ref in md_refs)

        return list(issues)

    def get_pr_closing_issues(self, pr_number: int) -> List[int]:
        """
        通过 Timeline API 获取 PR 显式关闭的 issues

        Args:
            pr_number: PR 编号

        Returns:
            关闭的 issue 编号列表
        """
        closing_issues = set()

        # 使用 Timeline API 获取 PR 的事件历史
        url = f"{self.API}/repos/{self.owner}/{self.repo}/issues/{pr_number}/timeline"

        try:
            while url:
                r = self._get_json(url, params={"per_page": 100} if "?" not in url else {})
                events = r.json()

                for event in events:
                    # 查找 "connected" 或 "cross-referenced" 事件
                    event_type = event.get("event")

                    if event_type == "connected":
                        # 获取关联的 issue
                        source = event.get("source", {})
                        issue = source.get("issue", {})
                        if issue:
                            issue_number = issue.get("number")
                            if issue_number:
                                closing_issues.add(issue_number)

                    elif event_type == "cross-referenced":
                        # 检查是否是 PR 关闭 issue 的引用
                        source = event.get("source", {})
                        issue = source.get("issue", {})
                        if issue and issue.get("pull_request"):
                            # 这是一个 PR 引用，跳过
                            continue
                        if issue:
                            issue_number = issue.get("number")
                            if issue_number:
                                closing_issues.add(issue_number)

                # 获取下一页
                if "next" in r.links:
                    url = r.links["next"]["url"]
                else:
                    url = None

        except Exception:
            # Timeline API 可能对某些仓库不可用，静默失败
            pass

        return list(closing_issues)


def clone_repo(owner: str, repo: str, target_path) -> bool:
    """
    克隆 GitHub 仓库

    Args:
        owner: 仓库所有者
        repo: 仓库名称
        target_path: 目标路径（Path 对象或字符串）

    Returns:
        是否克隆成功
    """
    import subprocess
    from pathlib import Path

    repo_url = f"https://github.com/{owner}/{repo}.git"
    target_path = Path(target_path)

    try:
        # 创建父目录
        target_path.parent.mkdir(parents=True, exist_ok=True)

        print("\n开始克隆仓库...")
        print(f"  URL: {repo_url}")
        print(f"  目标: {target_path}")
        print("  (这可能需要几分钟时间)")

        # 执行克隆（使用完整仓库，不是 bare）
        result = subprocess.run(  # noqa: F841
            ["git", "clone", repo_url, str(target_path)], capture_output=True, text=True, check=True
        )

        print(f"✓ 仓库克隆成功: {target_path}")

        # 克隆成功后，自动 fetch PR refs
        fetch_pr_refs(target_path)

        return True

    except subprocess.CalledProcessError as e:
        print("✗ 克隆失败:")
        print(f"  错误: {e.stderr}")
        return False
    except Exception as e:
        print(f"✗ 克隆时发生错误: {e}")
        return False


def fetch_pr_refs(repo_path, verbose: bool = True) -> bool:
    """
    Fetch PR refs 到本地仓库

    GitHub 的 PR 分支存储在 refs/pull/*/head，标准 git clone 不会获取这些 refs。
    此函数将 PR refs fetch 到 refs/remotes/origin/pr/*，使得 PR 分支上的 commits 可访问。

    Args:
        repo_path: 本地仓库路径（Path 对象或字符串）
        verbose: 是否输出详细信息

    Returns:
        是否成功
    """
    import subprocess
    from pathlib import Path

    repo_path = Path(repo_path)

    if not (repo_path / ".git").exists():
        if verbose:
            print(f"✗ 不是有效的 git 仓库: {repo_path}")
        return False

    try:
        if verbose:
            print("  Fetching PR refs...")

        # Fetch 所有 PR refs
        result = subprocess.run(
            ["git", "-C", str(repo_path), "fetch", "origin", "+refs/pull/*/head:refs/remotes/origin/pr/*"],
            capture_output=True,
            text=True,
            timeout=300,  # 5 分钟超时
        )

        if result.returncode == 0:
            if verbose:
                print("  ✓ PR refs fetch 完成")
            return True
        else:
            # 某些仓库可能没有 PR，这不是错误
            if "couldn't find remote ref" in result.stderr.lower():
                if verbose:
                    print("  ✓ 没有 PR refs（仓库可能没有使用 PR）")
                return True
            else:
                if verbose:
                    print(f"  ⚠ Fetch PR refs 警告: {result.stderr.strip()}")
                return False

    except subprocess.TimeoutExpired:
        if verbose:
            print("  ⚠ Fetch PR refs 超时（可能仓库 PR 数量很多）")
        return False
    except Exception as e:
        if verbose:
            print(f"  ⚠ Fetch PR refs 失败: {e}")
        return False
