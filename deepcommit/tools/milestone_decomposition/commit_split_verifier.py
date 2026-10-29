#!/usr/bin/env python3
"""
Commit Split Equivalence Verifier

验证拆分后的 commit 序列是否与原始 commit 等价。

核心思路：
从同一个起点（parent commit）出发，验证：
1. 原 commit 的终点状态（tree hash）== 拆分后最后一个 commit 的终点状态
2. 拆分后的 commits 的累积变更 == 原 commit 的变更
3. 文件覆盖完整性：拆分后修改的文件集合 == 原 commit 修改的文件集合

使用场景：
- 将一个大 commit 拆分成多个小 commit 后，验证拆分结果是否保持等价性
- 确保拆分过程没有引入额外变更或遗漏变更

Example:
    from deepcommit.tools.milestone_decomposition.commit_split_verifier import verify_commit_split_equivalence

    result = verify_commit_split_equivalence(
        repo_path="/path/to/repo",
        original_commit="abc123",
        split_commits=["def456", "789ghi", "jkl012"]
    )

    if result["is_equivalent"]:
        print("✅ 拆分等价验证通过！")
    else:
        print("❌ 拆分验证失败：")
        for issue in result["issues"]:
            print(f"  - {issue}")
"""

import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

import pygit2

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from deepcommit.utils.git.local_git_repo import LocalGitRepo


class CommitSplitVerifier:
    """Commit 拆分等价性验证器"""

    def __init__(self, repo_path: str):
        """
        初始化验证器

        Args:
            repo_path: Git 仓库路径
        """
        self.repo = LocalGitRepo(repo_path)
        self.pygit_repo = pygit2.Repository(repo_path)

    def verify_split_equivalence(self, original_commit: str, split_commits: List[str]) -> Dict:
        """
        验证 commit 拆分的等价性

        Args:
            original_commit: 原始 commit SHA
            split_commits: 拆分后的 commits 列表（按顺序）

        Returns:
            验证结果字典：
            {
                "is_equivalent": bool,           # 总体是否等价
                "validations": {
                    "tree_hash_match": bool,      # tree hash 是否匹配
                    "parent_match": bool,         # parent 是否一致
                    "file_coverage": {            # 文件覆盖检查
                        "complete": bool,
                        "original_files": Set[str],
                        "split_files": Set[str],
                        "missing_files": Set[str],  # 原有但拆分后缺失
                        "extra_files": Set[str]     # 拆分后新增的文件
                    },
                    "stats_comparison": {         # 变更统计对比
                        "original_additions": int,
                        "original_deletions": int,
                        "split_additions": int,
                        "split_deletions": int,
                        "stats_match": bool
                    }
                },
                "issues": List[str]               # 发现的问题列表
            }
        """
        issues = []

        # 获取 commit 对象
        try:
            orig_commit = self._get_commit(original_commit)
        except Exception as e:
            return {
                "is_equivalent": False,
                "validations": {},
                "issues": [f"无法获取原始 commit {original_commit}: {e}"],
            }

        if not split_commits:
            return {"is_equivalent": False, "validations": {}, "issues": ["拆分 commits 列表为空"]}

        try:
            split_commit_objs = [self._get_commit(sha) for sha in split_commits]
        except Exception as e:
            return {"is_equivalent": False, "validations": {}, "issues": [f"无法获取拆分 commits: {e}"]}

        # 1. 验证 parent 一致性
        parent_match, parent_issue = self._check_parent_match(orig_commit, split_commit_objs[0])
        if parent_issue:
            issues.append(parent_issue)

        # 2. 验证 tree hash 一致性（核心验证）
        tree_match, tree_issue = self._check_tree_match(orig_commit, split_commit_objs[-1])
        if tree_issue:
            issues.append(tree_issue)

        # 3. 验证文件覆盖完整性
        file_coverage = self._check_file_coverage(orig_commit, split_commit_objs)
        if not file_coverage["complete"]:
            if file_coverage["missing_files"]:
                issues.append(f"拆分后缺失文件：{', '.join(sorted(file_coverage['missing_files']))}")
            if file_coverage["extra_files"]:
                issues.append(f"拆分后额外文件：{', '.join(sorted(file_coverage['extra_files']))}")

        # 4. 验证变更统计
        stats = self._check_stats(orig_commit, split_commit_objs)
        if not stats["stats_match"]:
            issues.append(
                f"变更统计不匹配 - 原始: +{stats['original_additions']}/-{stats['original_deletions']}, "
                f"拆分: +{stats['split_additions']}/-{stats['split_deletions']}"
            )

        # 判断总体是否等价
        is_equivalent = parent_match and tree_match and file_coverage["complete"] and stats["stats_match"]

        return {
            "is_equivalent": is_equivalent,
            "validations": {
                "tree_hash_match": tree_match,
                "parent_match": parent_match,
                "file_coverage": file_coverage,
                "stats_comparison": stats,
            },
            "issues": issues,
        }

    def _get_commit(self, sha: str) -> pygit2.Commit:
        """获取 commit 对象"""
        try:
            obj = self.pygit_repo.revparse_single(sha)
            commit = obj.peel(pygit2.Commit)
            if not isinstance(commit, pygit2.Commit):
                raise ValueError(f"{sha} 不是有效的 commit")
            return commit
        except Exception as e:
            raise ValueError(f"无法获取 commit {sha}: {e}") from e

    def _check_parent_match(
        self, orig_commit: pygit2.Commit, first_split_commit: pygit2.Commit
    ) -> tuple[bool, Optional[str]]:
        """
        检查 parent 是否一致

        Returns:
            (是否匹配, 问题描述)
        """
        orig_parent = orig_commit.parents[0] if orig_commit.parents else None
        split_parent = first_split_commit.parents[0] if first_split_commit.parents else None

        if orig_parent is None and split_parent is None:
            return True, None

        if orig_parent is None or split_parent is None:
            return False, "原始 commit 和拆分后第一个 commit 的 parent 数量不一致"

        if orig_parent.id != split_parent.id:
            return False, (f"Parent 不一致 - 原始: {orig_parent.id}, 拆分: {split_parent.id}")

        return True, None

    def _check_tree_match(
        self, orig_commit: pygit2.Commit, last_split_commit: pygit2.Commit
    ) -> tuple[bool, Optional[str]]:
        """
        检查 tree hash 是否一致（核心验证）

        Tree hash 代表文件系统的完整状态，如果一致则说明最终状态完全相同

        Returns:
            (是否匹配, 问题描述)
        """
        orig_tree = orig_commit.tree_id
        split_tree = last_split_commit.tree_id

        if orig_tree != split_tree:
            return False, (f"Tree hash 不一致 - 原始: {orig_tree}, 拆分后最后一个 commit: {split_tree}")

        return True, None

    def _check_file_coverage(self, orig_commit: pygit2.Commit, split_commits: List[pygit2.Commit]) -> Dict:
        """
        检查文件覆盖完整性

        验证拆分后修改的文件集合是否与原 commit 一致

        Returns:
            {
                "complete": bool,
                "original_files": Set[str],
                "split_files": Set[str],
                "missing_files": Set[str],
                "extra_files": Set[str]
            }
        """
        # 获取原始 commit 修改的文件
        orig_files = self._get_changed_files(orig_commit)

        # 获取拆分后所有 commits 修改的文件（合并）
        split_files = set()
        for commit in split_commits:
            split_files.update(self._get_changed_files(commit))

        missing_files = orig_files - split_files
        extra_files = split_files - orig_files

        return {
            "complete": len(missing_files) == 0 and len(extra_files) == 0,
            "original_files": orig_files,
            "split_files": split_files,
            "missing_files": missing_files,
            "extra_files": extra_files,
        }

    def _get_changed_files(self, commit: pygit2.Commit) -> Set[str]:
        """获取 commit 修改的文件集合"""
        files = set()

        if commit.parents:
            parent = commit.parents[0]
            diff = self.pygit_repo.diff(parent, commit)
        else:
            # 初始 commit，与空树比较
            diff = self.pygit_repo.diff(None, commit)

        for patch in diff:
            delta = patch.delta
            # 包含新文件和旧文件路径（处理重命名）
            files.add(delta.new_file.path)
            if delta.old_file.path != delta.new_file.path:
                files.add(delta.old_file.path)

        return files

    def _check_stats(self, orig_commit: pygit2.Commit, split_commits: List[pygit2.Commit]) -> Dict:
        """
        检查变更统计是否一致

        注意：由于中间可能有重构，这里允许一定的差异

        Returns:
            {
                "original_additions": int,
                "original_deletions": int,
                "split_additions": int,
                "split_deletions": int,
                "stats_match": bool
            }
        """
        orig_stats = self._get_commit_stats(orig_commit)

        # 累加所有拆分 commits 的统计
        total_additions = 0
        total_deletions = 0
        for commit in split_commits:
            stats = self._get_commit_stats(commit)
            total_additions += stats["additions"]
            total_deletions += stats["deletions"]

        # 允许 10% 的误差（考虑到可能的重构）
        tolerance = 0.1
        additions_match = abs(orig_stats["additions"] - total_additions) <= max(
            1, int(orig_stats["additions"] * tolerance)
        )
        deletions_match = abs(orig_stats["deletions"] - total_deletions) <= max(
            1, int(orig_stats["deletions"] * tolerance)
        )

        return {
            "original_additions": orig_stats["additions"],
            "original_deletions": orig_stats["deletions"],
            "split_additions": total_additions,
            "split_deletions": total_deletions,
            "stats_match": additions_match and deletions_match,
        }

    def _get_commit_stats(self, commit: pygit2.Commit) -> Dict[str, int]:
        """获取 commit 的变更统计"""
        if commit.parents:
            parent = commit.parents[0]
            diff = self.pygit_repo.diff(parent, commit)
        else:
            diff = self.pygit_repo.diff(None, commit)

        additions = 0
        deletions = 0

        for patch in diff:
            line_stats = patch.line_stats
            # line_stats 返回 (context_lines, additions, deletions)
            additions += line_stats[1]
            deletions += line_stats[2]

        return {"additions": additions, "deletions": deletions}


def verify_commit_split_equivalence(
    repo_path: str, original_commit: str, split_commits: List[str], resolver=None
) -> Dict:
    """
    便捷函数：验证 commit 拆分的等价性

    Args:
        repo_path: Git 仓库路径（multi-repo resolver 下被忽略）
        original_commit: 原始 commit SHA
        split_commits: 拆分后的 commits 列表（按顺序）
        resolver: 可选的多仓 WorkspaceResolver；multi 时在原始 commit 所属仓库里验证

    Returns:
        验证结果字典（详见 CommitSplitVerifier.verify_split_equivalence）

    Example:
        result = verify_commit_split_equivalence(
            "/path/to/repo",
            "abc123",
            ["def456", "789ghi"]
        )

        if result["is_equivalent"]:
            print("✅ 验证通过")
    """
    if resolver is not None and resolver.multi:
        repo_path = resolver.repo_path_for(original_commit)
    verifier = CommitSplitVerifier(repo_path)
    return verifier.verify_split_equivalence(original_commit, split_commits)


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="验证 commit 拆分的等价性")
    parser.add_argument("--repo-path", required=True, help="Git 仓库路径")
    parser.add_argument("--original", required=True, help="原始 commit SHA")
    parser.add_argument("--split", required=True, nargs="+", help="拆分后的 commit SHAs（按顺序）")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    args = parser.parse_args()

    result = verify_commit_split_equivalence(args.repo_path, args.original, args.split)

    if args.json:
        # JSON 输出（需要处理 set 类型）
        def set_to_list(obj):
            if isinstance(obj, set):
                return sorted(list(obj))
            raise TypeError

        print(json.dumps(result, indent=2, default=set_to_list))
    else:
        # 人类可读输出
        print("\n" + "=" * 60)
        print("Commit 拆分等价性验证结果")
        print("=" * 60)
        print(f"\n原始 commit: {args.original}")
        print(f"拆分后 commits: {', '.join(args.split)}")
        print(f"\n验证结果: {'✅ 等价' if result['is_equivalent'] else '❌ 不等价'}")

        print("\n详细检查：")
        validations = result["validations"]
        print(f"  Tree Hash 匹配: {'✅' if validations.get('tree_hash_match') else '❌'}")
        print(f"  Parent 一致: {'✅' if validations.get('parent_match') else '❌'}")

        if "file_coverage" in validations:
            fc = validations["file_coverage"]
            print(f"  文件覆盖完整: {'✅' if fc['complete'] else '❌'}")
            if not fc["complete"]:
                if fc["missing_files"]:
                    print(f"    缺失文件: {', '.join(sorted(fc['missing_files']))}")
                if fc["extra_files"]:
                    print(f"    额外文件: {', '.join(sorted(fc['extra_files']))}")

        if "stats_comparison" in validations:
            sc = validations["stats_comparison"]
            print(f"  变更统计匹配: {'✅' if sc['stats_match'] else '❌'}")
            print(f"    原始: +{sc['original_additions']}/-{sc['original_deletions']}")
            print(f"    拆分: +{sc['split_additions']}/-{sc['split_deletions']}")

        if result["issues"]:
            print("\n发现的问题：")
            for issue in result["issues"]:
                print(f"  - {issue}")

        print("\n" + "=" * 60)
