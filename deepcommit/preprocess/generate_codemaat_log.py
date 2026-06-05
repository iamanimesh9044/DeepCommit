#!/usr/bin/env python3
"""
Code Maat 日志生成器

从指定的Git仓库生成Code Maat分析所需的日志文件。
支持：
1. 按tag范围过滤提交
2. 只分析指定分支（如main）
3. 只关注特定目录下的文件（如src/）
4. 生成Code Maat git2格式的日志
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

import pygit2

# Handle both module import and direct script execution
try:
    from .code_maat import CodeMaatAnalyzer
except ImportError:
    # When running as a script, add parent directory to path

    sys.path.insert(0, str(Path(__file__).parent))
    from code_maat import CodeMaatAnalyzer

from deepcommit.harness.utils.src_filter import SrcFileFilter


class CodeMaatLogGenerator:
    """Code Maat 日志生成器"""

    def __init__(
        self,
        repo_path: str,
        main_branch: str = "main",
        repo_src_dirs: Optional[List[str]] = None,
        test_dirs: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
    ):
        """
        初始化日志生成器

        Args:
            repo_path: Git仓库路径
            main_branch: 主分支名称，默认为"main"
            repo_src_dirs: 源码目录列表，只包含这些目录下的文件（如["src/", "lib/"]）
            test_dirs: 测试目录/文件模式列表，这些文件会被排除
            exclude: 额外排除模式列表
        """
        self.repo = pygit2.Repository(repo_path)
        self.main_branch = main_branch
        self.repo_src_dirs = repo_src_dirs or []
        self._exclude_patterns = SrcFileFilter.build_exclude_patterns(test_dirs=test_dirs, exclude=exclude)

    def get_commits_in_range(self, start_tag: Optional[str], end_tag: Optional[str]) -> List[pygit2.Commit]:
        """
        获取指定tag范围内的提交

        Args:
            start_tag: 起始tag（不包含）
            end_tag: 结束tag（包含）

        Returns:
            提交列表（从新到旧）
        """
        # 获取分支引用
        try:
            branch_ref = self.repo.branches[self.main_branch]
            end_commit = branch_ref.peel(pygit2.Commit)
        except KeyError:
            # 尝试远程分支
            try:
                branch_ref = self.repo.branches[f"origin/{self.main_branch}"]
                end_commit = branch_ref.peel(pygit2.Commit)
            except KeyError:
                raise ValueError(f"分支 '{self.main_branch}' 不存在")  # noqa: B904

        # 如果指定了end_tag，使用end_tag作为结束点（支持 tag 名称或 commit SHA）
        if end_tag:
            try:
                # 先尝试作为 tag
                end_ref = self.repo.references[f"refs/tags/{end_tag}"]
                end_commit = end_ref.peel(pygit2.Commit)
            except KeyError:
                # 不是 tag，尝试作为 commit SHA
                try:
                    end_commit = self.repo.revparse_single(end_tag).peel(pygit2.Commit)
                except KeyError:
                    raise ValueError(f"'{end_tag}' 既不是有效的 tag 也不是有效的 commit SHA")  # noqa: B904

        # 获取start_tag对应的commit（如果指定，支持 tag 名称或 commit SHA）
        start_commit_oid = None
        if start_tag:
            try:
                # 先尝试作为 tag
                start_ref = self.repo.references[f"refs/tags/{start_tag}"]
                start_commit = start_ref.peel(pygit2.Commit)
                start_commit_oid = start_commit.id
            except KeyError:
                # 不是 tag，尝试作为 commit SHA
                try:
                    start_commit = self.repo.revparse_single(start_tag).peel(pygit2.Commit)
                    start_commit_oid = start_commit.id
                except KeyError:
                    raise ValueError(f"'{start_tag}' 既不是有效的 tag 也不是有效的 commit SHA")  # noqa: B904

        # 遍历提交历史
        commits = []
        for commit in self.repo.walk(end_commit.id, pygit2.GIT_SORT_TIME):
            # 如果遇到start_tag对应的commit，停止（不包含start_tag）
            if start_commit_oid and commit.id == start_commit_oid:
                break
            commits.append(commit)

        return commits

    def _filter_diff(self, diff: pygit2.Diff) -> List[pygit2.DiffDelta]:
        """
        根据源码目录和排除模式过滤diff

        Args:
            diff: Git diff对象

        Returns:
            过滤后的delta列表
        """
        filtered_deltas = []
        for delta in diff.deltas:
            # 检查新文件路径或旧文件路径
            new_path = delta.new_file.path
            old_path = delta.old_file.path
            file_path = new_path if new_path else old_path

            # 1. 检查是否匹配排除模式（test_dirs + exclude）
            if self._exclude_patterns and SrcFileFilter.should_exclude_file(file_path, self._exclude_patterns):
                continue

            # 2. 检查是否在源码目录下 (canonical S-norm membership)
            if self.repo_src_dirs:
                in_src_dir = SrcFileFilter.in_src_dirs(new_path, self.repo_src_dirs) or SrcFileFilter.in_src_dirs(
                    old_path, self.repo_src_dirs
                )
                if not in_src_dir:
                    continue

            filtered_deltas.append(delta)

        return filtered_deltas

    def generate_log(
        self,
        output_path: str,
        start_tag: Optional[str] = None,
        end_tag: Optional[str] = None,
        overwrite: bool = False,
    ) -> None:
        """
        生成Code Maat格式的日志文件

        Code Maat git2格式：
        [hash] author date message
        added   deleted filename
        added   deleted filename
        (空行)

        Args:
            output_path: 输出文件路径
            start_tag: 起始tag（不包含）
            end_tag: 结束tag（包含）
            overwrite: 是否覆盖已存在的文件
        """
        output_file = Path(output_path)

        # 检查文件是否存在
        if output_file.exists() and not overwrite:
            raise FileExistsError(f"文件 '{output_path}' 已存在。使用 --overwrite 覆盖。")

        # 获取提交列表
        print(f"正在获取 {self.main_branch} 分支的提交历史...")
        if start_tag and end_tag:
            print(f"Tag范围: {start_tag} .. {end_tag}")
        elif end_tag:
            print(f"直到Tag: {end_tag}")

        commits = self.get_commits_in_range(start_tag, end_tag)
        print(f"找到 {len(commits)} 个提交")

        if self.repo_src_dirs:
            print(f"源码目录: {', '.join(self.repo_src_dirs)}")

        # 生成日志
        print(f"正在生成日志文件: {output_path}")
        with open(output_path, "w", encoding="utf-8") as f:
            processed = 0
            skipped = 0

            for commit in commits:
                # 获取父提交
                if len(commit.parents) == 0:
                    # 初始提交，跳过
                    skipped += 1
                    continue
                elif len(commit.parents) > 1:
                    # 合并提交，使用第一个父提交
                    parent = commit.parents[0]
                else:
                    parent = commit.parents[0]

                # 计算diff
                diff = self.repo.diff(parent.tree, commit.tree)

                # 应用路径过滤器
                deltas = self._filter_diff(diff)

                if not deltas:
                    # 没有匹配的文件变更，跳过
                    skipped += 1
                    continue

                # 写入提交信息 (Code Maat git2格式: --hash--date--author)
                short_hash = str(commit.id)[:8]
                author = commit.author.name
                # 格式化日期为 YYYY-MM-DD
                date = commit.commit_time
                import datetime

                date_str = datetime.datetime.fromtimestamp(date).strftime("%Y-%m-%d")

                f.write(f"--{short_hash}--{date_str}--{author}\n")

                # 写入文件变更统计
                # 使用diff的patch来获取统计信息
                patches = [p for p in diff]

                for delta in deltas:
                    # 找到对应的patch
                    patch = None
                    for p in patches:
                        if p.delta.new_file.path == delta.new_file.path:
                            patch = p
                            break

                    if patch is None:
                        # 如果找不到patch（二进制文件等），使用0
                        added = 0
                        deleted = 0
                    else:
                        # 计算添加和删除的行数
                        added = patch.line_stats[1]  # insertions
                        deleted = patch.line_stats[2]  # deletions

                    # 使用新文件路径（对于删除的文件，使用旧路径）
                    file_path = delta.new_file.path if delta.status != pygit2.GIT_DELTA_DELETED else delta.old_file.path

                    f.write(f"{added}\t{deleted}\t{file_path}\n")

                # 空行分隔提交
                f.write("\n")
                processed += 1

                if processed % 100 == 0:
                    print(f"已处理 {processed} 个提交...")

        print(f"✓ 完成！处理了 {processed} 个提交，跳过 {skipped} 个提交")
        print(f"日志文件: {output_path}")

    def generate_analyses(
        self,
        log_file: Path,
        output_dir: Optional[Path] = None,
        verbose: bool = False,
    ) -> dict:
        """
        运行Code Maat分析

        Args:
            log_file: Code Maat日志文件路径
            output_dir: 输出目录（默认：log_file所在目录/analysis/）
            verbose: 是否显示详细输出

        Returns:
            包含分析结果路径的字典
        """
        log_file = Path(log_file)

        if output_dir is None:
            output_dir = log_file.parent / "analysis"

        print("\n正在运行Code Maat分析...")
        print(f"输出目录: {output_dir}")

        analyzer = CodeMaatAnalyzer(
            log_file=log_file,
            output_dir=output_dir,
            verbose=verbose,
        )

        results = analyzer.run_all_analyses(generate_json=True)

        print("\n✓ Code Maat分析完成！")
        print(f"  - Hotspots: {results.get('hotspots')}")
        print(f"  - Coupling: {results.get('coupling')}")
        print(f"  - Temporal Churn: {results.get('temporal_churn')}")
        print(f"  - Entity Churn: {results.get('entity_churn')}")
        print(f"  - JSON Summary: {results.get('json_summary')}")

        return results


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="生成Code Maat分析所需的git日志文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 生成整个仓库的日志
  %(prog)s --owner urllib3 --repo urllib3 --local-repo DATA/github_data/repos/urllib3_urllib3

  # 生成指定tag范围的日志
  %(prog)s --owner urllib3 --repo urllib3 --local-repo DATA/github_data/repos/urllib3_urllib3 \\
           --start-tag 2.0.6 --end-tag 2.3.0

  # 只分析src/目录，覆盖已存在的文件
  %(prog)s --owner urllib3 --repo urllib3 --local-repo DATA/github_data/repos/urllib3_urllib3 \\
           --start-tag 2.0.6 --end-tag 2.3.0 --repo-src-dirs src/ --overwrite

  # 分析多个源码目录（src/ 和 lib/）
  %(prog)s --owner myrepo --repo myproject --local-repo /path/to/repo \\
           --start-tag v1.0 --end-tag v2.0 --repo-src-dirs src/ lib/ --overwrite

  # 使用 commit SHA 代替 tag（适用于 diverged 分支场景）
  # 先计算 merge-base: git merge-base origin/main 5.1
  %(prog)s --owner django --repo django --local-repo /path/to/django \\
           --start-tag abc1234 --end-tag def5678 --repo-src-dirs django/
        """,
    )

    parser.add_argument("--owner", required=True, help="仓库所有者")
    parser.add_argument("--repo", required=True, help="仓库名称")
    parser.add_argument(
        "--local-repo", required=True, help="本地Git仓库路径（例如：DATA/github_data/repos/urllib3_urllib3）"
    )
    parser.add_argument("--start-tag", help="起始tag或commit SHA（不包含此提交）")
    parser.add_argument("--end-tag", help="结束tag或commit SHA（包含此提交）")
    parser.add_argument("--main-branch", default="main", help="主分支名称（默认：main）")
    parser.add_argument(
        "--repo-src-dirs",
        nargs="+",
        help="源码目录列表，只保留修改了这些目录下文件的commits（例如: --repo-src-dirs src/ lib/）",
    )
    parser.add_argument(
        "--output",
        help="输出文件路径（默认：/tmp/{owner}_{repo}_codemaat.log）",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已存在的输出文件",
    )
    parser.add_argument(
        "--test-dirs",
        nargs="*",
        help="测试目录/文件模式列表，这些文件会被排除（例如: --test-dirs 'tests/**' '**/*_test.go'）",
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        help="额外排除模式列表（例如: --exclude '**/vendor/**'）",
    )

    args = parser.parse_args()

    # 确定输出路径
    if args.output:
        output_path = args.output
    else:
        output_path = f"/tmp/{args.owner}_{args.repo}_codemaat.log"

    # 创建生成器
    try:
        generator = CodeMaatLogGenerator(
            repo_path=args.local_repo,
            main_branch=args.main_branch,
            repo_src_dirs=args.repo_src_dirs,
            test_dirs=args.test_dirs,
            exclude=args.exclude,
        )

        # 生成日志
        generator.generate_log(
            output_path=output_path,
            start_tag=args.start_tag,
            end_tag=args.end_tag,
            overwrite=args.overwrite,
        )

    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
