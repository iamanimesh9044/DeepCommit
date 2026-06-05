#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
统一的索引构建和查询工具类
结合了build_index_simple.py和query_index.py的功能
"""

import json
import pickle
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


class IndexTool:
    """索引构建和查询工具类"""

    def __init__(self, data_dir: str):
        """
        初始化索引工具

        Args:
            data_dir: 数据目录路径
        """
        self.data_dir = Path(data_dir)
        self.index_file = self.data_dir / ".index_cache.pkl"
        self.index = None

        # 标记是否已经尝试过加载索引，避免重复的日志输出
        self._index_load_attempted = False

    # ============== 内部辅助方法 ==============

    def _ensure_index_loaded(self) -> bool:
        """确保索引被加载；若不存在则回退到空索引并返回False。"""
        if self.index is not None:
            # index 为 {} 时意味着已经确认索引不存在
            return bool(self.index)

        if not self._index_load_attempted:
            self._index_load_attempted = True
            loaded = self.load_index()
            if loaded:
                return True

        # 若索引文件不存在，避免后续访问 None.get 抛异常
        if self.index is None:
            self.index = {}
        return False

    # ============== 索引构建功能 ==============

    def build_index(self) -> Dict:
        """构建索引"""
        # 文件路径
        releases_file = self.data_dir / "releases.jsonl"
        prs_file = self.data_dir / "pull_requests.jsonl"
        issues_file = self.data_dir / "issues.jsonl"

        # 初始化索引结构
        self.index = {
            # 原始数据
            "releases": {},
            "prs": {},
            "issues": {},
            # 正向映射
            "release_to_commits": defaultdict(list),
            "release_to_prs": defaultdict(list),
            "pr_to_commits": defaultdict(list),
            "pr_to_issues": defaultdict(list),
            # 反向映射
            "commit_to_releases": defaultdict(set),
            "commit_to_prs": defaultdict(set),
            "issue_to_prs": defaultdict(set),
            "pr_to_releases": defaultdict(set),
            # 额外映射
            "merge_commit_to_pr": {},
            "pr_merge_commits": {},  # pr_number -> merge_commit_sha
            "commit_to_merge_pr": {},  # commit_sha -> pr_number (if it was merged via this PR)
        }

        # 1. 加载releases
        print("加载releases...")
        if releases_file.exists():
            with open(releases_file, "r") as f:
                for line in f:
                    data = json.loads(line)
                    tag = data["tag"]

                    # 存储原始数据
                    self.index["releases"][tag] = data

                    # 构建映射
                    commits = data.get("commits", [])
                    prs = data.get("prs", [])

                    self.index["release_to_commits"][tag] = commits
                    self.index["release_to_prs"][tag] = prs

                    for commit in commits:
                        self.index["commit_to_releases"][commit].add(tag)

                    for pr_num in prs:
                        self.index["pr_to_releases"][pr_num].add(tag)

        print(f"  加载了 {len(self.index['releases'])} 个releases")

        # 2. 加载PRs
        print("加载PRs...")
        if prs_file.exists():
            with open(prs_file, "r") as f:
                for line in f:
                    data = json.loads(line)
                    pr_num = data["number"]

                    # 存储原始数据
                    self.index["prs"][pr_num] = data

                    # 构建映射
                    commits = data.get("commits", [])
                    issues = data.get("related_issues", [])
                    merge_commit = data.get("merge_commit_sha")

                    self.index["pr_to_commits"][pr_num] = commits
                    self.index["pr_to_issues"][pr_num] = issues

                    for commit in commits:
                        self.index["commit_to_prs"][commit].add(pr_num)

                    for issue_num in issues:
                        self.index["issue_to_prs"][issue_num].add(pr_num)

                    if merge_commit:
                        self.index["merge_commit_to_pr"][merge_commit] = pr_num
                        self.index["pr_merge_commits"][pr_num] = merge_commit
                        # 将merge_commit也加入commit_to_prs映射，这样它就不会被识别为孤立commit
                        self.index["commit_to_prs"][merge_commit].add(pr_num)

                        # 标记哪些commits是通过这个PR merge的
                        for commit in commits:
                            if commit != merge_commit:  # 排除merge commit本身
                                self.index["commit_to_merge_pr"][commit] = pr_num

        print(f"  加载了 {len(self.index['prs'])} 个PRs")

        # 3. 加载issues
        print("加载issues...")
        if issues_file.exists():
            with open(issues_file, "r") as f:
                for line in f:
                    data = json.loads(line)
                    issue_num = data["number"]
                    self.index["issues"][issue_num] = data

        print(f"  加载了 {len(self.index['issues'])} 个issues")

        # 转换set为list以便序列化
        for key in ["commit_to_releases", "commit_to_prs", "issue_to_prs", "pr_to_releases"]:
            self.index[key] = {k: list(v) for k, v in self.index[key].items()}

        # 转换defaultdict为普通dict
        for key in ["release_to_commits", "release_to_prs", "pr_to_commits", "pr_to_issues"]:
            self.index[key] = dict(self.index[key])

        # 保存索引
        self.save_index()

        # 统计信息
        self._print_build_statistics()

        return self.index

    def save_index(self):
        """保存索引到文件"""
        print(f"\n保存索引到 {self.index_file}...")
        with open(self.index_file, "wb") as f:
            pickle.dump(self.index, f, protocol=pickle.HIGHEST_PROTOCOL)

        file_size = self.index_file.stat().st_size / (1024 * 1024)
        print(f"索引文件大小: {file_size:.2f} MB")

    def load_index(self) -> Optional[Dict]:
        """加载索引"""
        if not self.index_file.exists():
            print(f"索引文件不存在: {self.index_file}")
            return None

        with open(self.index_file, "rb") as f:
            self.index = pickle.load(f)
            return self.index

    def _print_build_statistics(self):
        """打印构建统计信息"""
        all_commits = set()
        for commits in self.index["release_to_commits"].values():
            all_commits.update(commits)
        for commits in self.index["pr_to_commits"].values():
            all_commits.update(commits)

        print("\n统计信息:")
        print(f"  总releases: {len(self.index['releases'])}")
        print(f"  总PRs: {len(self.index['prs'])}")
        print(f"  总issues: {len(self.index['issues'])}")
        print(f"  唯一commits: {len(all_commits)}")
        print(f"  有merge commit的PRs: {len(self.index['pr_merge_commits'])}")

    # ============== Commit查询功能 ==============

    def query_commit_releases(self, commit_sha: str) -> List[str]:
        """根据commit查询所在的releases"""
        if not self._ensure_index_loaded():
            return []

        commit_sha = self._find_full_sha(commit_sha)
        if not commit_sha:
            return []
        return self.index.get("commit_to_releases", {}).get(commit_sha, [])

    def query_commit_prs(self, commit_sha: str) -> List[int]:
        """根据commit查询所在的PRs"""
        if not self._ensure_index_loaded():
            return []

        commit_sha = self._find_full_sha(commit_sha)
        if not commit_sha:
            return []
        return list(self.index.get("commit_to_prs", {}).get(commit_sha, []))

    def query_commit_merge_pr(self, commit_sha: str) -> Optional[int]:
        """根据commit查询是否是某个PR的merge commit"""
        if not self._ensure_index_loaded():
            return None

        commit_sha = self._find_full_sha(commit_sha)
        if not commit_sha:
            return None
        # 如果这个commit是某个PR的merge commit，返回PR编号
        return self.index.get("merge_commit_to_pr", {}).get(commit_sha)

    def query_commit_info(self, commit_sha: str) -> Optional[Dict]:
        """查询commit的完整信息"""
        if not self._ensure_index_loaded():
            return None

        commit_sha = self._find_full_sha(commit_sha)
        if not commit_sha:
            return None

        info = {
            "sha": commit_sha,
            "releases": self.query_commit_releases(commit_sha),
            "prs": self.query_commit_prs(commit_sha),
            "is_merge_commit": commit_sha in self.index.get("merge_commit_to_pr", {}),
            "merge_pr": self.query_commit_merge_pr(commit_sha),
            "merged_via_pr": self.index.get("commit_to_merge_pr", {}).get(commit_sha),
        }
        return info

    # ============== Release查询功能 ==============

    def query_release_commits(self, release_tag: str) -> List[str]:
        """根据release查询包含的commits"""
        if not self.index:
            self.load_index()
        return self.index.get("release_to_commits", {}).get(release_tag, [])

    def query_release_prs(self, release_tag: str) -> List[int]:
        """根据release查询包含的PRs"""
        if not self.index:
            self.load_index()
        return self.index.get("release_to_prs", {}).get(release_tag, [])

    def query_release_issues(self, release_tag: str) -> List[int]:
        """根据release查询相关的issues"""
        if not self.index:
            self.load_index()

        prs = self.query_release_prs(release_tag)
        issues = set()
        for pr_num in prs:
            pr_issues = self.index.get("pr_to_issues", {}).get(pr_num, [])
            issues.update(pr_issues)
        return list(issues)

    def query_release_info(self, release_tag: str) -> Optional[Dict]:
        """查询release的完整信息"""
        if not self.index:
            self.load_index()

        if release_tag not in self.index.get("releases", {}):
            return None

        release = self.index["releases"][release_tag]
        info = {
            "tag": release_tag,
            "name": release.get("name"),
            "date": release.get("date"),
            "author": release.get("author"),
            "commits": self.query_release_commits(release_tag),
            "prs": self.query_release_prs(release_tag),
            "issues": self.query_release_issues(release_tag),
            "pr_details": [
                self.index["prs"].get(pr_num)
                for pr_num in self.query_release_prs(release_tag)
                if pr_num in self.index.get("prs", {})
            ],
        }
        return info

    def query_releases_between_dates(
        self, start_date: Union[str, datetime], end_date: Union[str, datetime]
    ) -> List[str]:
        """查询指定日期范围内的releases"""
        if not self.index:
            self.load_index()

        releases = []
        for tag, release in self.index.get("releases", {}).items():
            release_date = release.get("date")
            if release_date:
                try:
                    date_obj = datetime.fromisoformat(release_date.replace("Z", "+00:00"))
                    start_obj = datetime.fromisoformat(start_date) if isinstance(start_date, str) else start_date
                    end_obj = datetime.fromisoformat(end_date) if isinstance(end_date, str) else end_date

                    if start_obj <= date_obj <= end_obj:
                        releases.append(tag)
                except:  # noqa: E722
                    pass
        return releases

    # ============== PR查询功能 ==============

    def query_pr_commits(self, pr_number: int) -> List[str]:
        """根据PR查询包含的commits"""
        if not self.index:
            self.load_index()
        return self.index.get("pr_to_commits", {}).get(pr_number, [])

    def query_pr_releases(self, pr_number: int) -> List[str]:
        """根据PR查询所在的releases"""
        if not self.index:
            self.load_index()
        return self.index.get("pr_to_releases", {}).get(pr_number, [])

    def query_pr_issues(self, pr_number: int) -> List[int]:
        """根据PR查询解决的issues"""
        if not self.index:
            self.load_index()
        return self.index.get("pr_to_issues", {}).get(pr_number, [])

    def query_pr_merge_commit(self, pr_number: int) -> Optional[str]:
        """根据PR查询merge commit"""
        if not self.index:
            self.load_index()
        return self.index.get("pr_merge_commits", {}).get(pr_number)

    def query_pr_info(self, pr_number: int) -> Optional[Dict]:
        """查询PR的完整信息"""
        if not self.index:
            self.load_index()

        if pr_number not in self.index.get("prs", {}):
            return None

        pr = self.index["prs"][pr_number]
        info = {
            "number": pr_number,
            "title": pr.get("title"),
            "state": pr.get("state"),
            "merged": pr.get("merged", False),
            "created_at": pr.get("created_at"),
            "closed_at": pr.get("closed_at"),
            "commits": self.query_pr_commits(pr_number),
            "merge_commit": self.query_pr_merge_commit(pr_number),
            "releases": self.query_pr_releases(pr_number),
            "issues": self.query_pr_issues(pr_number),
        }
        return info

    def query_prs_by_state(self, state: str = "open") -> List[int]:
        """查询指定状态的PRs"""
        if not self.index:
            self.load_index()

        prs = []
        for pr_num, pr in self.index.get("prs", {}).items():
            if pr.get("state") == state:
                prs.append(pr_num)
        return prs

    def query_merged_prs(self) -> List[int]:
        """查询所有已合并的PRs"""
        if not self.index:
            self.load_index()

        prs = []
        for pr_num, pr in self.index.get("prs", {}).items():
            if pr.get("merged", False):
                prs.append(pr_num)
        return prs

    def query_pr_by_merge_commit(self, merge_commit_sha: str) -> Optional[Dict]:
        """根据merge commit找PR"""
        if not self.index:
            self.load_index()

        pr_num = self.index.get("merge_commit_to_pr", {}).get(merge_commit_sha)
        if pr_num:
            return self.query_pr_info(pr_num)
        return None

    # ============== Issue查询功能 ==============

    def query_issue_prs(self, issue_number: int) -> List[int]:
        """根据issue查询解决它的PRs"""
        if not self.index:
            self.load_index()

        prs = []
        for pr_num, issues in self.index.get("pr_to_issues", {}).items():
            if issue_number in issues:
                prs.append(pr_num)
        return prs

    def query_issue_releases(self, issue_number: int) -> List[str]:
        """根据issue查询包含修复的releases"""
        if not self.index:
            self.load_index()

        prs = self.query_issue_prs(issue_number)
        releases = set()
        for pr_num in prs:
            pr_releases = self.index.get("pr_to_releases", {}).get(pr_num, [])
            releases.update(pr_releases)
        return list(releases)

    def query_issue_info(self, issue_number: int) -> Optional[Dict]:
        """查询issue的完整信息"""
        if not self.index:
            self.load_index()

        if issue_number not in self.index.get("issues", {}):
            return None

        issue = self.index["issues"][issue_number]
        info = {
            "number": issue_number,
            "title": issue.get("title"),
            "state": issue.get("state"),
            "created_at": issue.get("created_at"),
            "closed_at": issue.get("closed_at"),
            "prs": self.query_issue_prs(issue_number),
            "releases": self.query_issue_releases(issue_number),
        }
        return info

    def get_prs_for_issue(self, issue_number: int) -> List[int]:
        """获取与指定issue相关的PR编号列表"""
        if not self.index:
            self.load_index()

        if not self.index:
            return []

        prs = self.index.get("issue_to_prs", {}).get(issue_number, [])
        # Ensure deterministic ordering
        return sorted(set(prs))

    # ============== 复合查询功能 ==============

    def query_release_comparison(self, release1: str, release2: str) -> Optional[Dict]:
        """比较两个release之间的差异"""
        if not self.index:
            self.load_index()

        if release1 not in self.index.get("releases", {}) or release2 not in self.index.get("releases", {}):
            return None

        commits1 = set(self.query_release_commits(release1))
        commits2 = set(self.query_release_commits(release2))
        prs1 = set(self.query_release_prs(release1))
        prs2 = set(self.query_release_prs(release2))
        issues1 = set(self.query_release_issues(release1))
        issues2 = set(self.query_release_issues(release2))

        return {
            "release1": release1,
            "release2": release2,
            "unique_commits_in_1": list(commits1 - commits2),
            "unique_commits_in_2": list(commits2 - commits1),
            "common_commits": list(commits1 & commits2),
            "unique_prs_in_1": list(prs1 - prs2),
            "unique_prs_in_2": list(prs2 - prs1),
            "common_prs": list(prs1 & prs2),
            "unique_issues_in_1": list(issues1 - issues2),
            "unique_issues_in_2": list(issues2 - issues1),
            "common_issues": list(issues1 & issues2),
        }

    def query_orphan_commits(self) -> List[str]:
        """查询孤立的commits（不属于任何PR）"""
        if not self.index:
            self.load_index()

        all_commits = set()
        for commits in self.index.get("release_to_commits", {}).values():
            all_commits.update(commits)

        orphans = []
        for commit in all_commits:
            if commit not in self.index.get("commit_to_prs", {}):
                orphans.append(commit)
        return orphans

    def query_multi_release_prs(self) -> List[Dict]:
        """查询被多个release包含的PRs"""
        if not self.index:
            self.load_index()

        multi_release_prs = []
        for pr_num, releases in self.index.get("pr_to_releases", {}).items():
            if len(releases) > 1:
                multi_release_prs.append({"pr": pr_num, "releases": releases, "count": len(releases)})
        return multi_release_prs

    def query_statistics(self) -> Dict:
        """查询整体统计信息"""
        if not self.index:
            self.load_index()

        # 收集release中的所有commits（主分支commits）
        commits_in_releases = set()
        for commits in self.index.get("release_to_commits", {}).values():
            commits_in_releases.update(commits)

        all_issues = set()
        for issues in self.index.get("pr_to_issues", {}).values():
            all_issues.update(issues)

        orphan_commits = self.query_orphan_commits()
        multi_release_prs = self.query_multi_release_prs()

        # 找出包含最多PR的release
        max_prs_release = None
        max_prs_count = 0
        for tag, prs in self.index.get("release_to_prs", {}).items():
            if len(prs) > max_prs_count:
                max_prs_count = len(prs)
                max_prs_release = tag

        # 找出包含最多commits的release
        max_commits_release = None
        max_commits_count = 0
        for tag, commits in self.index.get("release_to_commits", {}).items():
            if len(commits) > max_commits_count:
                max_commits_count = len(commits)
                max_commits_release = tag

        return {
            "total_releases": len(self.index.get("releases", {})),
            "total_prs": len(self.index.get("prs", {})),
            "total_issues": len(self.index.get("issues", {})),
            "commits_in_releases": len(commits_in_releases),  # 更清晰的命名
            "unique_issues_referenced": len(all_issues),
            "orphan_commits": len(orphan_commits),
            "multi_release_prs": len(multi_release_prs),
            "max_prs_release": {"tag": max_prs_release, "count": max_prs_count},
            "max_commits_release": {"tag": max_commits_release, "count": max_commits_count},
            "merged_prs": len(self.query_merged_prs()),
            "open_prs": len(self.query_prs_by_state("open")),
            "closed_prs": len(self.query_prs_by_state("closed")),
        }

    # ============== 辅助功能 ==============

    def _find_full_sha(self, partial_sha: str) -> Optional[str]:
        """根据部分SHA找到完整的commit SHA"""
        if not partial_sha:
            return None

        # 先在commit_to_releases中查找
        for commit_sha in self.index.get("commit_to_releases", {}):
            if commit_sha.startswith(partial_sha):
                return commit_sha

        # 再在commit_to_prs中查找
        for commit_sha in self.index.get("commit_to_prs", {}):
            if commit_sha.startswith(partial_sha):
                return commit_sha

        # 在merge_commit_to_pr中查找
        for commit_sha in self.index.get("merge_commit_to_pr", {}):
            if commit_sha.startswith(partial_sha):
                return commit_sha

        return None

    def export_query_result(self, result: Any, output_file: str, format: str = "json"):
        """导出查询结果到文件"""
        output_path = Path(output_file)

        if format == "json":
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
        elif format == "txt":
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(str(result))
        else:
            raise ValueError(f"Unsupported format: {format}")

        return output_path

    # ============== 示例和测试功能 ==============

    def run_examples(self):
        """运行示例查询"""
        if not self.index:
            self.load_index()
            if not self.index:
                print("索引未加载，请先构建索引")
                return

        print("=== 索引查询示例 ===\n")

        # 1. Release查询示例
        print("1. Release查询示例:")
        releases = list(self.index.get("releases", {}).keys())[:1]
        if releases:
            release = releases[0]
            info = self.query_release_info(release)
            if info:
                print(f"   Release {info['tag']}:")
                print(f"   - 发布日期: {info['date']}")
                print(f"   - 包含commits: {len(info['commits'])}个")
                print(f"   - 包含PRs: {len(info['prs'])}个")
                print(f"   - 相关issues: {len(info['issues'])}个")

        # 2. PR查询示例
        print("\n2. PR查询示例:")
        prs = list(self.index.get("prs", {}).keys())[:1]
        if prs:
            pr = prs[0]
            info = self.query_pr_info(pr)
            if info:
                print(f"   PR #{info['number']}:")
                print(f"   - 标题: {info['title'][:50]}...")
                print(f"   - 状态: {info['state']}, 已合并: {info['merged']}")
                print(f"   - 包含commits: {len(info['commits'])}个")
                print(f"   - 所在releases: {info['releases']}")

        # 3. 统计信息
        print("\n3. 统计信息:")
        stats = self.query_statistics()
        print(f"   - 总releases数: {stats['total_releases']}")
        print(f"   - 总PRs数: {stats['total_prs']}")
        print(f"   - 总issues数: {stats['total_issues']}")
        print(f"   - 主分支commits数: {stats['commits_in_releases']}")
        print(f"   - 直接提交数（非PR）: {stats['orphan_commits']}")
        print(f"   - 跨版本PRs数: {stats['multi_release_prs']}")

        print("\n=== 查询完成 ===")


def main():
    """主函数，提供命令行接口"""
    import sys

    if len(sys.argv) < 3:
        print("使用方法:")
        print("  python index_tool.py <data_dir> <command> [options]")
        print("\n命令:")
        print("  build           - 构建索引")
        print("  query           - 运行示例查询")
        print("  stats           - 显示统计信息")
        print("  release <tag>   - 查询指定release")
        print("  pr <number>     - 查询指定PR")
        print("  commit <sha>    - 查询指定commit")
        print("  issue <number>  - 查询指定issue")
        print("  compare <r1> <r2> - 比较两个release")
        print("\n示例:")
        print("  python index_tool.py ./data build")
        print("  python index_tool.py ./data query")
        print("  python index_tool.py ./data release 2.2.0")
        print("  python index_tool.py ./data pr 1234")
        print("  python index_tool.py ./data compare 2.1.0 2.2.0")
        sys.exit(1)

    data_dir = sys.argv[1]
    command = sys.argv[2]

    # 创建工具实例
    tool = IndexTool(data_dir)

    if command == "build":
        print("构建索引...")
        tool.build_index()
        print("\n构建完成！")

    elif command == "query":
        tool.run_examples()

    elif command == "stats":
        if not tool.load_index():
            print("索引文件不存在，请先运行 build 命令")
            sys.exit(1)

        stats = tool.query_statistics()
        print("\n=== 统计信息 ===")
        for key, value in stats.items():
            if isinstance(value, dict):
                print(f"{key}: {value}")
            else:
                print(f"{key}: {value}")

    elif command == "release" and len(sys.argv) > 3:
        if not tool.load_index():
            print("索引文件不存在，请先运行 build 命令")
            sys.exit(1)

        tag = sys.argv[3]
        info = tool.query_release_info(tag)
        if info:
            print(json.dumps(info, indent=2, ensure_ascii=False))
        else:
            print(f"Release '{tag}' not found")

    elif command == "pr" and len(sys.argv) > 3:
        if not tool.load_index():
            print("索引文件不存在，请先运行 build 命令")
            sys.exit(1)

        pr_num = int(sys.argv[3])
        info = tool.query_pr_info(pr_num)
        if info:
            print(json.dumps(info, indent=2, ensure_ascii=False))
        else:
            print(f"PR #{pr_num} not found")

    elif command == "commit" and len(sys.argv) > 3:
        if not tool.load_index():
            print("索引文件不存在，请先运行 build 命令")
            sys.exit(1)

        sha = sys.argv[3]
        info = tool.query_commit_info(sha)
        if info:
            print(json.dumps(info, indent=2, ensure_ascii=False))
        else:
            print(f"Commit '{sha}' not found")

    elif command == "issue" and len(sys.argv) > 3:
        if not tool.load_index():
            print("索引文件不存在，请先运行 build 命令")
            sys.exit(1)

        issue_num = int(sys.argv[3])
        info = tool.query_issue_info(issue_num)
        if info:
            print(json.dumps(info, indent=2, ensure_ascii=False))
        else:
            print(f"Issue #{issue_num} not found")

    elif command == "compare" and len(sys.argv) > 4:
        if not tool.load_index():
            print("索引文件不存在，请先运行 build 命令")
            sys.exit(1)

        release1 = sys.argv[3]
        release2 = sys.argv[4]
        comparison = tool.query_release_comparison(release1, release2)
        if comparison:
            print(json.dumps(comparison, indent=2, ensure_ascii=False))
        else:
            print(f"One or both releases not found: {release1}, {release2}")

    else:
        print(f"未知命令: {command}")
        print("运行 'python index_tool.py' 查看帮助")
        sys.exit(1)


if __name__ == "__main__":
    main()
