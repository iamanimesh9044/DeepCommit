"""
回滚检测器 - 基于代码变更模式自动识别回滚

不依赖 commit message，纯粹通过代码变更特征识别：
1. Numstat反向检测: 比较 added/deleted 行数是否互换
2. 文件哈希验证: 检查文件内容是否回到原始状态

使用 pygit2 直接访问 git 对象，无需 subprocess 调用。
"""

import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pygit2


@dataclass
class RevertPair:
    """回滚对"""

    commit_a: str  # SHA of original commit
    commit_b: str  # SHA of revert commit
    score: float  # Similarity score (0.0-1.0)
    method: str  # Detection method: 'numstat' or 'hash'
    detail: str = ""  # Additional details


@dataclass
class FileStats:
    """文件变更统计"""

    added: int  # Lines added
    deleted: int  # Lines deleted


class RevertDetector:
    """回滚检测器"""

    def __init__(self, repo_path: str):
        """
        初始化回滚检测器

        Args:
            repo_path: Git仓库路径
        """
        self.repo = pygit2.Repository(repo_path)
        self._cache_numstat: Dict[str, Dict[str, FileStats]] = {}
        self._cache_file_blob: Dict[Tuple[str, str], Optional[bytes]] = {}

    def get_numstat(self, commit_sha: str) -> Dict[str, FileStats]:
        """
        获取commit的numstat统计

        Args:
            commit_sha: Commit SHA

        Returns:
            {filepath: FileStats(added, deleted)}
        """
        if commit_sha in self._cache_numstat:
            return self._cache_numstat[commit_sha]

        try:
            commit = self.repo.revparse_single(commit_sha)
            if not isinstance(commit, pygit2.Commit):
                commit = commit.peel(pygit2.Commit)
        except (KeyError, ValueError):
            return {}

        # 获取父commit
        if len(commit.parents) == 0:
            # 第一个commit，与空树比较
            parent_tree = None
        else:
            parent_tree = commit.parents[0].tree

        # 计算diff
        current_tree = commit.tree
        diff = self.repo.diff(parent_tree, current_tree)  # type: ignore[arg-type]

        stats = {}
        for patch in diff:
            delta = patch.delta
            # 跳过二进制文件
            if delta.is_binary:
                continue

            filepath = delta.new_file.path or delta.old_file.path

            # 统计行数
            added = 0
            deleted = 0
            for hunk in patch.hunks:
                for line in hunk.lines:
                    if line.origin == "+":
                        added += 1
                    elif line.origin == "-":
                        deleted += 1

            stats[filepath] = FileStats(added=added, deleted=deleted)

        self._cache_numstat[commit_sha] = stats
        return stats

    def is_reverse_pair(self, commit_a: str, commit_b: str, threshold: float = 0.9) -> Tuple[bool, float]:
        """
        检测两个commit是否互为反向（Numstat方法）

        原理: 回滚commit的代码变更是原commit的镜像
        - commit_a: file.py +10/-5
        - commit_b: file.py +5/-10 (完美镜像)

        Args:
            commit_a: 第一个commit SHA
            commit_b: 第二个commit SHA
            threshold: 相似度阈值 (0.0-1.0)

        Returns:
            (是否反向, 相似度分数)
        """
        stats_a = self.get_numstat(commit_a)
        stats_b = self.get_numstat(commit_b)

        # 必须修改相同的文件
        files_a = set(stats_a.keys())
        files_b = set(stats_b.keys())

        if files_a != files_b or len(files_a) == 0:
            return False, 0.0

        # 计算反向匹配数
        reverse_count = 0.0
        for filepath in files_a:
            stat_a = stats_a[filepath]
            stat_b = stats_b[filepath]

            # 完美反向: add_a == del_b AND del_a == add_b
            if stat_a.added == stat_b.deleted and stat_a.deleted == stat_b.added:
                reverse_count += 1.0
            # 近似反向（允许±2行误差）
            elif abs(stat_a.added - stat_b.deleted) <= 2 and abs(stat_a.deleted - stat_b.added) <= 2:
                reverse_count += 0.5

        score = reverse_count / len(files_a)
        return score >= threshold, score

    def get_file_blob(self, commit_sha: str, filepath: str) -> Optional[bytes]:
        """
        获取文件在特定commit的内容

        Args:
            commit_sha: Commit SHA
            filepath: 文件路径

        Returns:
            文件内容（bytes）或 None（文件不存在）
        """
        cache_key = (commit_sha, filepath)
        if cache_key in self._cache_file_blob:
            return self._cache_file_blob[cache_key]

        try:
            commit = self.repo.revparse_single(commit_sha)
            if not isinstance(commit, pygit2.Commit):
                commit = commit.peel(pygit2.Commit)

            # 在树中查找文件
            entry = commit.tree[filepath]
            blob = self.repo[entry.id]

            if isinstance(blob, pygit2.Blob):
                content = blob.data
            else:
                content = None

        except (KeyError, ValueError):
            content = None

        self._cache_file_blob[cache_key] = content
        return content

    def get_file_hash(self, commit_sha: str, filepath: str) -> Optional[str]:
        """
        获取文件在特定commit的MD5哈希

        Args:
            commit_sha: Commit SHA
            filepath: 文件路径

        Returns:
            MD5 hex string 或 None（文件不存在）
        """
        content = self.get_file_blob(commit_sha, filepath)
        if content is None:
            return None

        return hashlib.md5(content).hexdigest()

    def verify_by_hash(self, commit_a: str, commit_b: str) -> Tuple[bool, str]:
        """
        验证两个commit是否互为回滚（文件哈希方法）

        检查: commit_b后的文件 == commit_a前的文件
        即: 回滚到原始状态

        Args:
            commit_a: 原始commit SHA
            commit_b: 疑似回滚的commit SHA

        Returns:
            (是否验证通过, 详情说明)
        """
        try:
            commit_a_obj = self.repo.revparse_single(commit_a)
            if not isinstance(commit_a_obj, pygit2.Commit):
                commit_a_obj = commit_a_obj.peel(pygit2.Commit)

            commit_b_obj = self.repo.revparse_single(commit_b)
            if not isinstance(commit_b_obj, pygit2.Commit):
                commit_b_obj = commit_b_obj.peel(pygit2.Commit)
        except (KeyError, ValueError):
            return False, "无法获取commit对象"

        # 获取commit_a修改的文件列表
        if len(commit_a_obj.parents) == 0:
            parent_tree_a = None
        else:
            parent_tree_a = commit_a_obj.parents[0].tree

        diff_a = self.repo.diff(parent_tree_a, commit_a_obj.tree)  # type: ignore[arg-type]
        files = set()
        for patch in diff_a:
            delta = patch.delta
            if not delta.is_binary:
                filepath = delta.new_file.path or delta.old_file.path
                files.add(filepath)

        if not files:
            return False, "无有效文件"

        # 检查每个文件
        match_count = 0
        total_count = len(files)

        for filepath in files:
            # 获取 commit_a 前的文件哈希
            if len(commit_a_obj.parents) > 0:
                parent_sha_a = str(commit_a_obj.parents[0].id)
                hash_before_a = self.get_file_hash(parent_sha_a, filepath)
            else:
                hash_before_a = None  # 新增文件

            # 获取 commit_b 后的文件哈希
            hash_after_b = self.get_file_hash(commit_b, filepath)

            # 检查是否回到原始状态
            if hash_before_a == hash_after_b:
                match_count += 1

        ratio = match_count / total_count
        detail = f"{match_count}/{total_count} 文件恢复原状"

        return ratio >= 0.9, detail

    def scan_commits(
        self,
        commits: List[str],
        method: str = "numstat",
        threshold: float = 0.9,
        verbose: bool = False,
    ) -> List[RevertPair]:
        """
        扫描commits列表，找出所有回滚对

        Args:
            commits: Commit SHA 列表
            method: 检测方法 ('numstat', 'hash', 'combined')
            threshold: Numstat方法的相似度阈值
            verbose: 是否打印进度

        Returns:
            回滚对列表
        """
        if method == "numstat":
            return self._scan_numstat(commits, threshold, verbose)
        elif method == "hash":
            return self._scan_hash(commits, verbose)
        elif method == "combined":
            return self._scan_combined(commits, threshold, verbose)
        else:
            raise ValueError(f"未知的检测方法: {method}")

    def _scan_numstat(self, commits: List[str], threshold: float, verbose: bool) -> List[RevertPair]:
        """使用Numstat方法扫描"""
        revert_pairs = []
        total_pairs = len(commits) * (len(commits) - 1) // 2

        if verbose:
            print(f"扫描 {len(commits)} 个 commits (Numstat方法)，检查 {total_pairs} 对组合...")

        checked = 0
        for i, commit_a in enumerate(commits):
            for commit_b in commits[i + 1 :]:
                is_revert, score = self.is_reverse_pair(commit_a, commit_b, threshold)
                if is_revert:
                    pair = RevertPair(
                        commit_a=commit_a,
                        commit_b=commit_b,
                        score=score,
                        method="numstat",
                    )
                    revert_pairs.append(pair)

                checked += 1
                if verbose and checked % 100 == 0:
                    print(f"  已检查: {checked}/{total_pairs}")

        return revert_pairs

    def _scan_hash(self, commits: List[str], verbose: bool) -> List[RevertPair]:
        """使用文件哈希方法扫描"""
        revert_pairs = []
        total_pairs = len(commits) * (len(commits) - 1) // 2

        if verbose:
            print(f"扫描 {len(commits)} 个 commits (Hash方法)...")

        checked = 0
        for i, commit_a in enumerate(commits):
            for commit_b in commits[i + 1 :]:
                is_verified, detail = self.verify_by_hash(commit_a, commit_b)
                if is_verified:
                    pair = RevertPair(
                        commit_a=commit_a,
                        commit_b=commit_b,
                        score=1.0,
                        method="hash",
                        detail=detail,
                    )
                    revert_pairs.append(pair)

                checked += 1
                if verbose and checked % 20 == 0:
                    print(f"  已检查: {checked}/{total_pairs}")

        return revert_pairs

    def _scan_combined(self, commits: List[str], threshold: float, verbose: bool) -> List[RevertPair]:
        """组合方法: 快速筛选 + 精确验证"""
        if verbose:
            print("阶段1: Numstat快速筛选...")

        # 阶段1: Numstat快速筛选（宽松阈值）
        candidates = []
        for i, commit_a in enumerate(commits):
            for commit_b in commits[i + 1 :]:
                is_revert, score = self.is_reverse_pair(commit_a, commit_b, threshold=0.7)
                if score >= 0.7:
                    candidates.append((commit_a, commit_b, score))

        if verbose:
            print(f"  发现 {len(candidates)} 个候选")

        if not candidates:
            return []

        # 阶段2: 文件哈希验证
        if verbose:
            print("阶段2: 文件哈希验证...")

        confirmed = []
        for commit_a, commit_b, score in candidates:
            is_verified, detail = self.verify_by_hash(commit_a, commit_b)
            if is_verified:
                pair = RevertPair(
                    commit_a=commit_a,
                    commit_b=commit_b,
                    score=score,
                    method="combined",
                    detail=detail,
                )
                confirmed.append(pair)

                if verbose:
                    short_a = commit_a[:7]
                    short_b = commit_b[:7]
                    print(f"  ✓ {short_a} ↔ {short_b} ({detail})")

        return confirmed

    def get_commit_info(self, commit_sha: str) -> Tuple[str, str]:
        """
        获取commit的简短信息

        Args:
            commit_sha: Commit SHA

        Returns:
            (short_sha, subject)
        """
        try:
            commit = self.repo.revparse_single(commit_sha)
            if not isinstance(commit, pygit2.Commit):
                commit = commit.peel(pygit2.Commit)

            short_sha = str(commit.id)[:7]
            subject = commit.message.split("\n")[0][:60]

            return short_sha, subject
        except (KeyError, ValueError):
            return commit_sha[:7], "Unknown"


def main():
    """CLI 工具"""
    import argparse

    parser = argparse.ArgumentParser(
        description="回滚检测工具 - 基于代码变更模式（使用 pygit2）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 扫描特定commits (Numstat方法，快速)
  python revert_detector.py --repo /path/to/repo --commits abc123 def456 ghi789

  # 使用组合方法（更准确）
  python revert_detector.py --repo /path/to/repo --commits abc123 def456 --method combined

  # 扫描并显示详细信息
  python revert_detector.py --repo /path/to/repo --commits abc123 def456 --verbose
        """,
    )

    parser.add_argument("--repo", required=True, help="Git仓库路径")
    parser.add_argument("--commits", nargs="+", required=True, help="要检查的commit SHA列表")
    parser.add_argument(
        "--method",
        choices=["numstat", "hash", "combined"],
        default="numstat",
        help="检测方法 (默认: numstat)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.9,
        help="Numstat相似度阈值 (0.0-1.0, 默认: 0.9)",
    )
    parser.add_argument("--verbose", action="store_true", help="显示详细信息")

    args = parser.parse_args()

    # 初始化检测器
    detector = RevertDetector(args.repo)

    # 扫描回滚
    print(f"检测方法: {args.method}")
    print(f"扫描 {len(args.commits)} 个 commits\n")

    reverts = detector.scan_commits(args.commits, method=args.method, threshold=args.threshold, verbose=args.verbose)

    if not reverts:
        print("✓ 未发现回滚")
        return 0

    # 显示结果
    print(f"\n⚠️ 发现 {len(reverts)} 对回滚:\n")
    for pair in reverts:
        short_a, desc_a = detector.get_commit_info(pair.commit_a)
        short_b, desc_b = detector.get_commit_info(pair.commit_b)

        print(f"回滚对 (score: {pair.score:.2f}, method: {pair.method}):")
        print(f"  [{short_a}] {desc_a}")
        print(f"  [{short_b}] {desc_b}")
        if pair.detail:
            print(f"  详情: {pair.detail}")
        print()

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
