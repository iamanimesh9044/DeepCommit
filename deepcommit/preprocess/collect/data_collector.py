#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
数据收集器 - 负责从GitHub API收集原始数据，不做任何过滤
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from tqdm import tqdm

from .data_models import CollectedData, CommitData, IssueData, PRData, ReleaseData

# Trac 项目配置
TRAC_PROJECTS = {
    ("django", "django"): "https://code.djangoproject.com",
}


class DataCollector:
    """数据收集器 - 只负责收集原始数据，不做任何过滤"""

    def __init__(self, config, github_api, git_repo=None):
        """
        初始化数据收集器

        Args:
            config: CollectorConfig对象
            github_api: GitHubAPI实例
            git_repo: LocalGitRepo实例（可选）
        """
        self.config = config
        self.github_api = github_api
        self.git_repo = git_repo
        self.stats = {"collected_commits": 0, "collected_prs": 0, "collected_issues": 0, "collected_releases": 0}

        # 检测是否是 Trac 项目
        self.trac_collector = None
        project_key = (config.owner.lower(), config.repo.lower())
        if project_key in TRAC_PROJECTS:
            print(f"  ✓ 检测到 Trac 项目: {config.owner}/{config.repo}")
            trac_url = TRAC_PROJECTS[project_key]
            print(f"    Trac URL: {trac_url}")

            try:
                from ..trac_crawler import TracCollector

                self.trac_collector = TracCollector(trac_url, max_workers=config.max_workers)
                print("    ✓ Trac 收集器初始化成功")
            except ImportError as e:
                print(f"    ✗ 无法导入 TracCollector: {e}")
                print("    将使用 GitHub Issues API")
        else:
            print("  使用 GitHub Issues API")

    def _extract_pr_numbers_from_commit_message(self, message: str) -> List[int]:
        """从commit message中提取PR编号或Trac ticket编号

        GitHub 格式：
        - "Merge pull request #123 from ..."
        - "Fix bug (#456)"
        - "Update docs (PR #789)"

        Trac 格式 (Django):
        - "Fixed #36213 -- Added support..."
        - "Refs #34619 -- Updated docs"
        """
        import re

        # 如果是 Trac 项目，使用 Trac ticket 提取器
        if self.trac_collector:
            from ..trac_crawler import extract_trac_tickets

            return extract_trac_tickets(message)

        # 否则使用默认的 #数字 提取
        pr_numbers = []
        matches = re.findall(r"#(\d+)", message)
        for match in matches:
            pr_numbers.append(int(match))
        return pr_numbers

    def collect_all(
        self,
        filter_commits: Optional[Set[str]] = None,
        all_tags: Optional[List[Tuple[str, str]]] = None,
        tags_to_process: Optional[List[Tuple[str, str]]] = None,
    ) -> CollectedData:
        """
        收集所有数据，不做任何过滤

        Args:
            filter_commits: 范围内的commit SHA集合（用于PR base判断）
            all_tags: 所有tags列表
            tags_to_process: 要处理的tags列表

        Returns:
            CollectedData对象
        """
        data = CollectedData()

        print("\n[数据收集阶段] 开始收集原始数据...")

        # Step 0: 如果配置了start_tag和end_tag，直接获取范围内的main commits
        all_main_commits_sha = set()
        all_refs_set = set()  # 改名：所有 #数字 引用（可能是 PR 或 Issue）
        commit_to_prs = {}

        if self.config.start_tag and self.config.end_tag:
            print("\n  [0/6] 获取tag范围内的main commits...")
            all_main_commits_sha, commit_to_prs = self._get_main_commits_in_range()
            print(f"  ✓ 主分支commits: {len(all_main_commits_sha)}")

            # 从commit_to_prs中提取所有引用编号（可能是 PR 或 Issue）
            for ref_list in commit_to_prs.values():
                all_refs_set.update(ref_list)
            print(f"  ✓ 从commits中提取的引用编号: {len(all_refs_set)}")

        # Step 1: 收集releases元数据
        print("\n  [1/6] 收集releases元数据...")
        if tags_to_process:
            data.releases = self._collect_releases_metadata(tags_to_process)
        print(f"  ✓ 收集 {len(data.releases)} 个releases")

        # Step 1.5: 分类引用（区分 PR 和 Issue）
        pr_numbers, issue_numbers_from_commits, not_found = self._classify_references(all_refs_set)

        # Step 2: 收集所有PRs详情（只对确认是 PR 的编号）
        print("\n  [2/6] 收集PRs详情...")
        data.prs, issues_from_prs = self._collect_prs(pr_numbers, filter_commits)
        print(f"  ✓ 收集 {len(data.prs)} 个PRs")

        # Step 3: 收集所有issues详情（合并 commit 引用的 + PR 引用的）
        print("\n  [3/6] 收集Issues详情...")
        all_issues_set = issue_numbers_from_commits | issues_from_prs
        data.issues = self._collect_issues(all_issues_set)
        print(f"  ✓ 收集 {len(data.issues)} 个Issues")

        # Step 4: 收集所有commits详情（不过滤）
        print("\n  [4/6] 收集Commits详情...")
        data.commits = self._collect_commits(all_main_commits_sha, commit_to_prs)
        print(f"  ✓ 收集 {len(data.commits)} 个Commits详情")

        self.stats["collected_commits"] = len(data.commits)
        self.stats["collected_prs"] = len(data.prs)
        self.stats["collected_issues"] = len(data.issues)
        self.stats["collected_releases"] = len(data.releases)

        return data

    def _get_main_commits_in_range(self) -> Tuple[Set[str], Dict[str, List[int]]]:
        """获取start_tag到end_tag之间main分支上的所有commits

        使用merge-base方法：
        1. 找到start_tag与main的分叉点A
        2. 找到end_tag与main的分叉点B
        3. 获取A到B之间main上的commits（沿first-parent）

        Returns:
            (commit_sha集合, commit_sha -> PR编号列表的映射)
        """
        all_commits = set()
        commit_to_prs = {}

        # 优先使用LocalGitRepo（支持merge-base）
        if self.git_repo:
            try:
                # 使用 branch-out point 方法获取 commits
                # 该方法在所有场景下都能正确工作：
                # - 当 tag 在 main first-parent 上时，返回 tag 本身
                # - 当 tag 在 release 分支上时，返回正确的分叉点
                commits = self.git_repo.get_commits_between_branch_out_points(
                    self.config.start_tag, self.config.end_tag
                )
                print(f"    使用 LocalGitRepo 获取到 {len(commits)} 个 main commits")

                # 当 start 和 end 有相同的 branch-out point 时，回退到 tag-to-tag 方法
                # 这种情况下，所有 release 都是从同一个 main 分支点分叉的
                if len(commits) == 0:
                    print("    注意: start/end 有相同的 branch-out point，使用 tag 之间的所有 commits")
                    commits = self.git_repo.get_commits_between_tags(self.config.start_tag, self.config.end_tag)
                    print(f"    使用 get_commits_between_tags 获取到 {len(commits)} 个 commits")

                all_commits = set(commits)

                # 从commits的message中提取PR编号
                for commit_sha in tqdm(all_commits, desc="  提取PR编号", leave=False):
                    try:
                        commit_info = self.git_repo.get_commit_info(commit_sha)
                        pr_numbers = self._extract_pr_numbers_from_commit_message(commit_info["message"])
                        if pr_numbers:
                            commit_to_prs[commit_sha] = pr_numbers
                    except Exception:
                        pass

                print(f"    从 {len(all_commits)} 个commits中提取到 {len(commit_to_prs)} 个有PR关联的commits")
                return all_commits, commit_to_prs

            except Exception as e:
                print(f"    警告: LocalGitRepo方法失败: {e}")
                print("    回退到GitHub API方法...")

        # Fallback到GitHub API（不支持merge-base，只能用tag之间的commits）
        if self.github_api:
            try:
                commits = self.github_api.get_commits_between_tags(self.config.start_tag, self.config.end_tag)
                all_commits = set(commits)
                print(f"    使用GitHub API获取到 {len(all_commits)} 个commits")

                # 从commits的message中提取PR编号（需要先获取commit详情）
                print("    从commits中提取PR编号...")
                for commit_sha in tqdm(list(all_commits)[:100], desc="  提取PR编号（限100个）", leave=False):
                    try:
                        commit_detail = self.github_api.get_commit_details(commit_sha)
                        if commit_detail:
                            message = commit_detail.get("commit", {}).get("message", "")
                            pr_numbers = self._extract_pr_numbers_from_commit_message(message)
                            if pr_numbers:
                                commit_to_prs[commit_sha] = pr_numbers
                    except Exception:
                        pass

                print(f"    提取到 {len(commit_to_prs)} 个有PR关联的commits")
                return all_commits, commit_to_prs

            except Exception as e:
                print(f"    警告: GitHub API方法也失败: {e}")

        return all_commits, commit_to_prs

    def _classify_references(self, ref_numbers: Set[int]) -> Tuple[Set[int], Set[int], Set[int]]:
        """
        分类引用编号：区分 PR、Issue 和不存在的编号

        GitHub 的 Issue 和 PR 共享同一个编号空间。通过 /issues/{number} API
        可以判断编号是 PR（有 pull_request 字段）还是 Issue。

        对于 Trac 项目，所有引用都是 Issues（Trac 没有 PR 概念）。

        Args:
            ref_numbers: 从 commit message 提取的所有 #数字 引用

        Returns:
            (pr_numbers, issue_numbers, not_found_numbers)
        """
        if not ref_numbers:
            return set(), set(), set()

        # 如果是 Trac 项目，所有引用都是 Issues
        if self.trac_collector:
            print("\n  [1.5/6] Trac 项目：所有引用都视为 Issues")
            print(f"  ✓ 共 {len(ref_numbers)} 个 Issue 引用")
            return set(), ref_numbers, set()

        # GitHub 项目需要分类
        if not self.github_api:
            return set(), set(), ref_numbers.copy()

        pr_numbers = set()
        issue_numbers = set()
        not_found = set()

        print("\n  [1.5/6] 分类引用编号（区分 PR/Issue）...")

        with tqdm(total=len(ref_numbers), desc="  分类引用") as pbar:
            with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
                futures = {executor.submit(self.github_api.get_issue_or_pr_type, num): num for num in ref_numbers}

                for future in as_completed(futures):
                    num = futures[future]
                    try:
                        result = future.result()
                        if result == "pr":
                            pr_numbers.add(num)
                        elif result == "issue":
                            issue_numbers.add(num)
                        else:
                            not_found.add(num)
                    except Exception as e:
                        pbar.write(f"  分类 #{num} 时出错: {e}")
                        not_found.add(num)
                    pbar.update(1)

        print(f"  ✓ 分类完成: PRs={len(pr_numbers)}, Issues={len(issue_numbers)}, NotFound={len(not_found)}")

        return pr_numbers, issue_numbers, not_found

    def _collect_releases_metadata(self, tags_to_process: List[Tuple[str, str]]) -> List[ReleaseData]:
        """收集releases元数据（不收集commits）

        Returns:
            releases列表
        """
        releases = []

        for tag, _date in tqdm(tags_to_process, desc="  处理releases"):
            # 获取release信息（包含tag_sha和commit_sha）
            release_info = self._get_release_info(tag)

            # 构建release数据（只保存元数据）
            release = ReleaseData(
                tag=tag,
                tag_sha=release_info["tag_sha"],
                commit_sha=release_info["commit_sha"],
                name=release_info["name"],
                release_notes=release_info["body"],
                author=release_info["author"],
                url=release_info["url"],
                created_at=release_info["created_at"],
                published_at=release_info["published_at"],
                draft=release_info["draft"],
                prerelease=release_info["prerelease"],
            )
            releases.append(release)

        return releases

    def _collect_releases(
        self, tags_to_process: List[Tuple[str, str]]
    ) -> Tuple[List[ReleaseData], Set[str], Set[int], Dict[str, List[int]]]:
        """
        收集releases数据（已废弃，保留用于向后兼容）

        Returns:
            (releases列表, 空集合, 空集合, 空字典) - 保持接口兼容性
        """
        releases = self._collect_releases_metadata(tags_to_process)
        # 返回空的 commits/prs 数据以保持接口兼容
        return releases, set(), set(), {}

    def _get_release_info(self, tag: str) -> Dict[str, Any]:
        """获取release信息"""
        if self.github_api:
            return self.github_api.get_release_info(tag)
        else:
            return {
                "tag_sha": "",
                "commit_sha": "",
                "name": tag,
                "body": "",
                "author": None,
                "url": f"https://github.com/{self.config.owner}/{self.config.repo}/releases/tag/{tag}",
                "created_at": None,
                "published_at": None,
                "draft": False,
                "prerelease": False,
            }

    def _get_commits_between_tags(self, prev_tag: Optional[str], tag: str) -> List[str]:
        """获取两个tag之间的commits"""
        commits = []
        if self.git_repo:
            try:
                if prev_tag:
                    commits = self.git_repo.get_commits_between_tags(prev_tag, tag, include_start=False)
                else:
                    commits = self.git_repo.get_commits_between_tags(None, tag, include_start=False)
            except Exception:
                if self.github_api:
                    commits = self.github_api.get_commits_between_tags(prev_tag, tag)
        elif self.github_api:
            commits = self.github_api.get_commits_between_tags(prev_tag, tag)
        return commits

    def _collect_prs(
        self, pr_numbers: Set[int], filter_commits: Optional[Set[str]] = None
    ) -> Tuple[Dict[int, PRData], Set[int]]:
        """
        收集所有PRs详情（不过滤）

        Returns:
            (PRs字典, 所有issues编号集合)
        """
        prs = {}
        all_issues = set()

        if not pr_numbers or not self.github_api:
            return prs, all_issues

        with tqdm(total=len(pr_numbers), desc="  处理PRs") as pbar:
            with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
                futures = {executor.submit(self._process_pr, pr_num, filter_commits): pr_num for pr_num in pr_numbers}

                for future in as_completed(futures):
                    pr_num = futures[future]
                    try:
                        pr_data = future.result()
                        if pr_data:
                            prs[pr_num] = pr_data
                            all_issues.update(pr_data.related_issues)
                    except Exception as e:
                        pbar.write(f"  错误处理PR #{pr_num}: {e}")
                    pbar.update(1)

        return prs, all_issues

    def _process_pr(self, pr_number: int, filter_commits: Optional[Set[str]] = None) -> Optional[PRData]:
        """处理单个PR"""
        try:
            pr_detail = self.github_api.get_pr_details(pr_number)
            if not pr_detail:
                return None

            # 获取PR commits
            pr_commits = pr_detail.get("commits", [])

            # 判断base是否在范围内
            base_in_range = True
            if filter_commits is not None:
                base_sha = pr_detail.get("base_sha")
                base_in_range = base_sha in filter_commits if base_sha else True

            # 创建PRData对象
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
                commits=pr_commits,
                related_issues=pr_detail.get("related_issues", []),
                files_changed=pr_detail.get("files_changed", []),
                files_count=pr_detail.get("changed_files", 0),  # 修复：使用changed_files
                additions=pr_detail.get("additions", 0),
                deletions=pr_detail.get("deletions", 0),
                author=pr_detail.get("user"),  # 修复：使用user字段
                created_at=pr_detail.get("created_at"),
                updated_at=pr_detail.get("updated_at"),
                merged_at=pr_detail.get("merged_at"),
                closed_at=pr_detail.get("closed_at"),
                # 新增10个字段
                user=pr_detail.get("user"),
                author_association=pr_detail.get("author_association"),
                merged_by=pr_detail.get("merged_by"),
                comments_count=pr_detail.get("comments_count", 0),
                review_comments_count=pr_detail.get("review_comments_count", 0),
                labels=pr_detail.get("labels", []),
                assignees=pr_detail.get("assignees", []),
                requested_reviewers=pr_detail.get("requested_reviewers", []),
                milestone=pr_detail.get("milestone"),
                draft=pr_detail.get("draft", False),
                # 从conversations中提取comments和review_comments
                comments=(
                    pr_detail.get("conversations", {}).get("comments", [])
                    if "conversations" in pr_detail
                    else pr_detail.get("comments", [])
                ),
                review_comments=(
                    pr_detail.get("conversations", {}).get("review_comments", [])
                    if "conversations" in pr_detail
                    else pr_detail.get("review_comments", [])
                ),
                meets_threshold=pr_detail.get("meets_threshold", True),
                base_in_range=base_in_range,
            )

            return pr_data
        except Exception:
            return None

    def _collect_issues(self, issue_numbers: Set[int]) -> Dict[int, IssueData]:
        """收集所有issues详情（不过滤）"""
        issues = {}

        if not issue_numbers:
            return issues

        # 如果是 Trac 项目，使用 Trac 收集器
        if self.trac_collector:
            print("  使用 Trac API 收集 Issues...")
            return self.trac_collector.collect_issues(issue_numbers)

        # 否则使用 GitHub API
        if not self.github_api:
            return issues

        with tqdm(total=len(issue_numbers), desc="  处理Issues") as pbar:
            with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
                futures = {
                    executor.submit(self.github_api.get_issue_details, issue_num): issue_num
                    for issue_num in issue_numbers
                }

                for future in as_completed(futures):
                    issue_num = futures[future]
                    try:
                        issue_detail = future.result()
                        if issue_detail:
                            issue_data = IssueData(
                                number=issue_detail["number"],
                                title=issue_detail["title"],
                                body=issue_detail.get("body", ""),
                                state=issue_detail["state"],
                                author=issue_detail.get("user"),  # 修复：使用user字段
                                labels=issue_detail.get("labels", []),
                                created_at=issue_detail.get("created_at"),
                                updated_at=issue_detail.get("updated_at"),
                                closed_at=issue_detail.get("closed_at"),
                                comments_count=issue_detail.get("comments_count", 0),
                                # 新增12个字段
                                user=issue_detail.get("user"),
                                author_association=issue_detail.get("author_association"),
                                url=issue_detail.get("url"),
                                state_reason=issue_detail.get("state_reason"),
                                assignees=issue_detail.get("assignees", []),
                                milestone=issue_detail.get("milestone"),
                                closed_by=issue_detail.get("closed_by"),
                                locked=issue_detail.get("locked", False),
                                active_lock_reason=issue_detail.get("active_lock_reason"),
                                reactions=issue_detail.get("reactions", {}),
                                conversations=issue_detail.get("conversations", {}),
                                events=issue_detail.get("events", []),
                            )
                            issues[issue_num] = issue_data
                    except Exception as e:
                        pbar.write(f"  错误处理Issue #{issue_num}: {e}")
                    pbar.update(1)

        return issues

    def _collect_commits(self, commit_shas: Set[str], commit_to_prs: Dict[str, List[int]]) -> Dict[str, CommitData]:
        """收集所有commits详情（不过滤）"""
        commits = {}

        if not commit_shas:
            return commits

        with tqdm(total=len(commit_shas), desc="  处理Commits") as pbar:
            with ThreadPoolExecutor(max_workers=min(10, self.config.max_workers)) as executor:
                futures = {executor.submit(self._process_commit, sha, commit_to_prs): sha for sha in commit_shas}

                for future in as_completed(futures):
                    sha = futures[future]
                    try:
                        commit_data = future.result()
                        if commit_data:
                            commits[sha] = commit_data
                    except Exception as e:
                        pbar.write(f"  错误处理Commit {sha[:8]}: {e}")
                    pbar.update(1)

        return commits

    def _process_commit(self, commit_sha: str, commit_to_prs: Dict[str, List[int]]) -> Optional[CommitData]:
        """处理单个commit"""
        try:
            commit_info = None

            # 优先使用LocalGitRepo
            if self.git_repo:
                try:
                    commit_info = self.git_repo.get_commit_info(commit_sha)
                    files_changed = self.git_repo.get_commit_files(commit_sha)

                    # 转换时间格式
                    author_time = datetime.fromtimestamp(commit_info["author"]["time"], tz=timezone.utc).isoformat()
                    committer_time = datetime.fromtimestamp(
                        commit_info["committer"]["time"], tz=timezone.utc
                    ).isoformat()

                    commit_data = CommitData(
                        sha=commit_info["sha"],
                        message=commit_info["message"],
                        author={
                            "name": commit_info["author"]["name"],
                            "email": commit_info["author"]["email"],
                            "time": author_time,
                        },
                        committer={
                            "name": commit_info["committer"]["name"],
                            "email": commit_info["committer"]["email"],
                            "time": committer_time,
                        },
                        parent_shas=commit_info["parent_shas"],
                        files_changed=files_changed,
                        additions=sum(f.get("additions", 0) for f in files_changed),
                        deletions=sum(f.get("deletions", 0) for f in files_changed),
                        is_merge_commit=len(commit_info["parent_shas"]) > 1,
                        associated_refs=commit_to_prs.get(commit_sha, []),
                    )
                    return commit_data
                except Exception:
                    pass

            # 使用GitHub API
            if self.github_api:
                commit_detail = self.github_api.get_commit_details(commit_sha)
                if commit_detail:
                    commit_data = CommitData(
                        sha=commit_detail["sha"],
                        message=commit_detail.get("commit", {}).get("message", ""),
                        author={
                            "name": commit_detail.get("commit", {}).get("author", {}).get("name", ""),
                            "email": commit_detail.get("commit", {}).get("author", {}).get("email", ""),
                            "time": commit_detail.get("commit", {}).get("author", {}).get("date", ""),
                        },
                        committer={
                            "name": commit_detail.get("commit", {}).get("committer", {}).get("name", ""),
                            "email": commit_detail.get("commit", {}).get("committer", {}).get("email", ""),
                            "time": commit_detail.get("commit", {}).get("committer", {}).get("date", ""),
                        },
                        parent_shas=[p["sha"] for p in commit_detail.get("parents", [])],
                        files_changed=commit_detail.get("files", []),
                        additions=commit_detail.get("stats", {}).get("additions", 0),
                        deletions=commit_detail.get("stats", {}).get("deletions", 0),
                        is_merge_commit=len(commit_detail.get("parents", [])) > 1,
                        associated_refs=commit_to_prs.get(commit_sha, []),
                        data_incomplete=not commit_detail.get("files"),
                    )
                    return commit_data

            return None
        except Exception:
            return None
