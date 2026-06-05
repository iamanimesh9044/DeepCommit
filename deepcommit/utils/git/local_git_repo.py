# pip install pygit2
import os
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import pygit2

from deepcommit.harness.utils.src_filter import SrcFileFilter


class LocalGitRepo:
    """
    基于pygit2的本地Git仓库工具类，直接从本地仓库读取信息，提供以下功能：
    1) is_tag_on_main_first_parent(tag)  判断某 tag 是否在 main 的 first-parent 链上
    2) previous_tag_on_main(tag, tag_filter)  找到该 tag 在主线上的"上一个 tag"
    3) commits_between_on_main(start_tag_exclusive, end_tag_inclusive)  得到 (start, end] 的主线提交
    4) get_commits_between_tags(start_tag, end_tag)  获取两个tag之间的所有commits（包含start_tag的变更）
    5) changed_files_between_tags(start, end)  查看两个 tag 间的改动文件
    6) get_all_tags()  获取所有tags及其日期
    7) get_tag_info(tag)  获取tag的详细信息

    注意：需要提供本地git仓库路径，所有操作都是离线的，不需要网络访问。
    """

    def __init__(self, repo_path: str, main_branch: Optional[str] = None):
        self.repo = pygit2.Repository(repo_path)
        self.main_refname = self._resolve_main_branch(main_branch)
        self.main_head = self.repo.revparse_single(self.main_refname).peel(pygit2.Commit)

        self._main_chain_set: Optional[Set[pygit2.Oid]] = (
            None  # main 的 first-parent 上所有提交 oid（做 membership 判断）
        )
        self._commit2tags: Optional[Dict[pygit2.Oid, List[str]]] = None  # 提交 oid -> [tag names]

        self._build_main_chain_set()
        self._build_commit2tags()

    # ---------- 基础工具 ----------

    def _resolve_main_branch(self, name: Optional[str]) -> str:
        # 允许传自定义分支名；否则 main/master/HEAD 兜底
        candidates = []
        if name:
            candidates += [f"refs/heads/{name}", f"refs/remotes/origin/{name}"]
        candidates += [
            "refs/heads/main",
            "refs/remotes/origin/main",
            "refs/heads/master",
            "refs/remotes/origin/master",
        ]
        for ref in candidates:
            try:
                self.repo.revparse_single(ref)
                return ref
            except KeyError:
                continue
        return "HEAD"

    def _peel_to_commit(self, rev: str) -> pygit2.Commit:
        """把任意 rev（tag 名 / refs/tags/xxx / commit sha）剥到 commit。"""
        if not rev.startswith("refs/"):
            # 优先按 tag 解析；若不是 tag 名也能处理（commit sha / 其它 rev）
            try:
                obj = self.repo.revparse_single(f"refs/tags/{rev}")
            except KeyError:
                obj = self.repo.revparse_single(rev)
        else:
            obj = self.repo.revparse_single(rev)

        c = obj.peel(pygit2.Commit)
        if not isinstance(c, pygit2.Commit):
            raise ValueError(f"{rev} 无法剥到提交对象")
        return c

    def _build_main_chain_set(self) -> None:
        """缓存 main 的 first-parent 链上的所有提交 oid。使用git subprocess替代pygit2循环以提高性能。"""
        if self._main_chain_set is not None:
            return

        import subprocess

        repo_path = self.repo.workdir or self.repo.path

        # 使用git log --first-parent 获取所有主线commits，比pygit2循环快很多
        cmd = ["git", "-C", repo_path, "log", "--first-parent", "--format=%H", str(self.main_head.id)]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)

        s = set()
        for line in result.stdout.strip().split("\n"):
            sha = line.strip()
            if sha:
                # 将SHA字符串转换为pygit2.Oid以与现有代码兼容
                s.add(pygit2.Oid(hex=sha))

        self._main_chain_set = s

    def _build_commit2tags(self) -> None:
        """建立 提交 -> [tag 名] 的映射；轻量/附注 tag 都会 peel 到 commit。"""
        if self._commit2tags is not None:
            return
        m: Dict[pygit2.Oid, List[str]] = {}
        for refname in self.repo.references:
            if not refname.startswith("refs/tags/"):
                continue
            try:
                obj = self.repo.revparse_single(refname)
                c = obj.peel(pygit2.Commit)
                if not isinstance(c, pygit2.Commit):
                    continue
            except KeyError:
                continue
            tag_name = refname.split("/", 2)[-1]
            m.setdefault(c.id, []).append(tag_name)
        self._commit2tags = m

    # ---------- 对外方法 ----------

    def is_tag_on_main_first_parent(self, tag: str) -> bool:
        """严格意义上的"在主线"：tag 指向的提交 ∈ main 的 first-parent 链。"""
        c = self._peel_to_commit(tag)
        if self._main_chain_set is None:
            return False
        return c.id in self._main_chain_set

    def previous_tag_on_main(
        self,
        tag: str,
        tag_filter: Optional[Callable[[str], bool]] = None,
    ) -> Optional[str]:
        """
        返回主线上“紧邻在 tag 之前”的一个 tag（沿 first-parent 向后找，第一个挂着 tag 的提交）。
        - 要求：tag 本身在主线；否则抛错（你也可以改成自动找最近的主线祖先再回溯）。
        - tag_filter：可选的过滤器（比如只要 2.x 的语义化版本 tag）。
        """
        start = self._peel_to_commit(tag)
        if self._main_chain_set is None or start.id not in self._main_chain_set:
            raise ValueError(f"{tag} 不在 {self.main_refname} 的 first-parent 主线上")

        cur = start.parents[0] if start.parents else None
        while cur is not None:
            candidates = self._commit2tags.get(cur.id, [])
            if tag_filter:
                candidates = [t for t in candidates if tag_filter(t)]
            if candidates:
                # 若一个提交挂多个 tag，可在此自定义排序选择；这里返回字典序最小的一个
                return sorted(candidates)[0]
            cur = cur.parents[0] if cur.parents else None
        return None  # 可能在一开始就没有更早的 tag（例如仓库很新）

    def commits_between_on_main(
        self,
        start_tag_exclusive: str,
        end_tag_inclusive: str,
    ) -> List[pygit2.Oid]:
        """沿 main 的 first-parent 链，返回 (start, end] 的提交（新→旧）。"""
        start_c = self._peel_to_commit(start_tag_exclusive)
        end_c = self._peel_to_commit(end_tag_inclusive)
        if (
            self._main_chain_set is None
            or start_c.id not in self._main_chain_set
            or end_c.id not in self._main_chain_set
        ):
            raise ValueError("start 或 end 不在主线")

        res: List[pygit2.Oid] = []
        c = end_c
        while True:
            if c.id == start_c.id:
                break
            res.append(c.id)
            if not c.parents:
                raise ValueError("在 main 主线未找到从 start 到 end 的 first-parent 路径")
            c = c.parents[0]
        return res

    def changed_files_between_tags(self, start_tag: str, end_tag: str) -> List[str]:
        """
        计算两个 tag（对应提交）之间的 diff，返回改动的文件路径列表。
        注意：这是普通 diff（不限 first-parent），用于实际改动查看。
        """
        a = self._peel_to_commit(start_tag)
        b = self._peel_to_commit(end_tag)
        try:
            diff = self.repo.diff(a, b)
            files = []
            for p in diff:
                # 可能是新增/删除/重命名，统一取 "新的" 路径（删除时 new_file.path 也会有值）
                files.append(p.delta.new_file.path)
            return sorted(set(files))
        except KeyError as e:
            print(f"Warning: Could not compute diff between {start_tag} and {end_tag}: {e}")
            return []

    def get_commits_between_tags(self, start_tag: Optional[str], end_tag: str, include_start: bool = True) -> List[str]:
        """获取两个tag之间的所有commits

        Args:
            start_tag: 起始tag（如果为None，则获取到end_tag的所有commits）
            end_tag: 结束tag
            include_start: 是否包含start_tag本身的变更（默认True，会自动找start_tag的前一个tag）

        Returns:
            commit SHA列表（字符串格式）
        """
        import subprocess

        repo_path = self.repo.workdir or self.repo.path

        if start_tag is None:
            # 如果没有起始tag，获取到end_tag的所有commits
            cmd = ["git", "-C", repo_path, "log", "--format=%H", end_tag]
        else:
            # 使用 git log start_tag..end_tag 语法（不包含start_tag的commit）
            # 这比pygit2 walker快很多，特别是对于大型仓库
            cmd = ["git", "-C", repo_path, "log", "--format=%H", f"{start_tag}..{end_tag}"]

        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        commits = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]

        return commits

    def get_all_tags(self) -> List[Tuple[str, str, str]]:
        """获取所有tags及其信息

        Returns:
            [(tag_name, commit_sha, date), ...] 按日期排序
        """
        tags = []

        # 遍历所有引用
        for refname in self.repo.references:
            if not refname.startswith("refs/tags/"):
                continue

            tag_name = refname.split("/", 2)[-1]

            try:
                # 获取tag对应的commit
                obj = self.repo.revparse_single(refname)
                commit = obj.peel(pygit2.Commit)

                if not isinstance(commit, pygit2.Commit):
                    continue

                # 获取commit日期
                timestamp = commit.commit_time
                # 转换为ISO格式字符串
                from datetime import datetime, timezone

                date = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()

                tags.append((tag_name, str(commit.id), date))

            except Exception as e:
                print(f"Warning: Could not process tag {tag_name}: {e}")
                continue

        # 按日期排序
        tags.sort(key=lambda x: x[2])
        return tags

    def get_tag_info(self, tag: str) -> Dict[str, Any]:
        """获取tag的详细信息

        Returns:
            包含tag信息的字典
        """
        try:
            # 获取tag对象
            if not tag.startswith("refs/"):
                ref = f"refs/tags/{tag}"
            else:
                ref = tag

            obj = self.repo.revparse_single(ref)

            # 判断是否是annotated tag (type 4 in pygit2)
            if obj.type == 4:  # GIT_OBJ_TAG = 4
                # Annotated tag
                tag_obj = obj.peel(pygit2.Tag)
                commit = tag_obj.peel(pygit2.Commit)

                return {
                    "name": tag,
                    "type": "annotated",
                    "commit_sha": str(commit.id),
                    "tagger": tag_obj.tagger.name if tag_obj.tagger else None,
                    "tagger_email": tag_obj.tagger.email if tag_obj.tagger else None,
                    "message": tag_obj.message,
                    "commit_time": commit.commit_time,
                    "on_mainline": self.is_tag_on_main_first_parent(tag),
                }
            else:
                # Lightweight tag
                commit = obj.peel(pygit2.Commit)

                return {
                    "name": tag,
                    "type": "lightweight",
                    "commit_sha": str(commit.id),
                    "tagger": None,
                    "tagger_email": None,
                    "message": None,
                    "commit_time": commit.commit_time,
                    "on_mainline": self.is_tag_on_main_first_parent(tag),
                }

        except Exception as e:
            raise ValueError(f"Could not get info for tag {tag}: {e}") from e

    def get_commits_between_tags_on_mainline(self, start_tag: Optional[str], end_tag: str) -> List[str]:
        """仅获取主线（first-parent）上两个tag之间的commits

        这是commits_between_on_main的包装，返回字符串格式的SHA列表
        """
        if start_tag is None:
            # 获取到end_tag的所有主线commits
            end_commit = self._peel_to_commit(end_tag)
            commits = []
            c = end_commit
            while c:
                commits.append(str(c.id))
                if not c.parents:
                    break
                c = c.parents[0]  # 只走first-parent
            return commits
        else:
            # 使用现有方法
            commit_ids = self.commits_between_on_main(start_tag, end_tag)
            return [str(cid) for cid in commit_ids]

    def _find_fork_point(self, tag: str) -> pygit2.Commit:
        """找到 tag 从 main 分叉出去的点（fork point）

        使用 git merge-base 找到 tag 和 main 的共同祖先。

        策略：
        1. 如果 tag commit 已经在 main 的 first-parent 链上，直接返回
        2. 否则，使用 git merge-base 找到 tag 与 main 的分叉点

        Args:
            tag: tag 名称

        Returns:
            分叉点的 commit 对象
        """
        import subprocess

        tag_commit = self._peel_to_commit(tag)

        # 检查是否已经在first-parent链上
        if tag_commit.id in self._main_chain_set:
            return tag_commit

        # 使用git merge-base找到tag与main的分叉点，这比遍历所有commits快很多
        repo_path = self.repo.workdir or self.repo.path

        # 获取main分支的名称
        main_ref = str(self.main_head.id)

        try:
            cmd = ["git", "-C", repo_path, "merge-base", main_ref, tag]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            merge_base_sha = result.stdout.strip()

            if merge_base_sha:
                # 返回merge-base对应的commit对象
                return self.repo.revparse_single(merge_base_sha).peel(pygit2.Commit)
        except subprocess.CalledProcessError:
            pass

        raise ValueError(f"Cannot find fork point for tag {tag} on main branch")

    def get_commits_between_release_tags_on_main(self, start_tag: str, end_tag: str) -> List[str]:
        r"""获取两个 release tag 之间 main 分支上的 commits（适用于 tags 在 release 分支的情况）

        适用场景：tags 在 release 分支上，不在 main 的 first-parent 链上

        方法：
        1. 找到 start_tag 的 fork point（从 main 分叉出去的点 A）
        2. 找到 end_tag 的 fork point（从 main 分叉出去的点 B）
        3. 使用 git log 获取 A 到 B 之间 main 上的 commits（沿 first-parent）

        Args:
            start_tag: 起始 tag（不包括对应点本身）
            end_tag: 结束 tag

        Returns:
            commit SHA列表（从新到旧）

        Example:
            main:    ... --- A (fork点1) --- M1 --- M2 --- B (fork点2) --- ...
                              \                                 /
            release:           C1 --- C2 (v3.3.3)              C3 --- C4 (v3.3.6)

            返回: [B, M2, M1] (不包括A)
        """
        import subprocess

        # 找到两个tag的fork point
        start_point = self._find_fork_point(start_tag)
        end_point = self._find_fork_point(end_tag)

        print(f"    start_tag {start_tag} -> merge-back: {str(start_point.id)[:8]}")
        print(f"    end_tag {end_tag} -> merge-back: {str(end_point.id)[:8]}")

        # 如果两个点相同，返回空
        if start_point.id == end_point.id:
            return []

        # 使用git log获取两点之间的commits，比pygit2循环快很多
        repo_path = self.repo.workdir or self.repo.path
        start_sha = str(start_point.id)
        end_sha = str(end_point.id)

        cmd = ["git", "-C", repo_path, "log", "--first-parent", "--format=%H", f"{start_sha}..{end_sha}"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)

        commits = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
        return commits

    def get_commit_info(self, commit_sha: str) -> Dict[str, Any]:
        """获取commit的详细信息

        Args:
            commit_sha: commit的SHA值

        Returns:
            包含commit信息的字典
        """
        try:
            commit = self.repo.revparse_single(commit_sha).peel(pygit2.Commit)

            return {
                "sha": str(commit.id),
                "message": commit.message,
                "author": {"name": commit.author.name, "email": commit.author.email, "time": commit.author.time},
                "committer": {
                    "name": commit.committer.name,
                    "email": commit.committer.email,
                    "time": commit.committer.time,
                },
                "parent_shas": [str(p.id) for p in commit.parents],
                "tree_sha": str(commit.tree_id),
            }
        except Exception as e:
            raise ValueError(f"Could not get info for commit {commit_sha}: {e}") from e

    def get_commit_files(self, commit_sha: str) -> List[Dict[str, Any]]:
        """获取commit修改的文件列表

        Args:
            commit_sha: commit的SHA值

        Returns:
            文件改动列表，每个元素包含文件路径和改动类型
        """
        try:
            commit = self.repo.revparse_single(commit_sha).peel(pygit2.Commit)

            # 获取parent用于diff
            if commit.parents:
                parent = commit.parents[0]
                diff = self.repo.diff(parent, commit)
            else:
                # 初始commit，与空树比较
                diff = self.repo.diff(None, commit)

            files = []
            for patch in diff:
                delta = patch.delta
                files.append(
                    {
                        "path": delta.new_file.path,
                        "old_path": delta.old_file.path if delta.old_file.path != delta.new_file.path else None,
                        "status": self._get_file_status(delta.status),
                        "additions": patch.line_stats[1],  # added lines
                        "deletions": patch.line_stats[2],  # deleted lines
                    }
                )

            return files
        except Exception as e:
            raise ValueError(f"Could not get files for commit {commit_sha}: {e}") from e

    def get_commit_loc(
        self,
        commit_sha: str,
        path_prefix: Optional[Union[str, List[str]]] = None,
        exclude_patterns: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """获取commit修改的代码行数（LoC）

        Args:
            commit_sha: commit的SHA值（支持完整SHA或前7位）
            path_prefix: 可选的路径前缀过滤器，只统计该路径下的文件变更
                        可以是单个字符串或字符串列表
                        例如: "src/" 只统计 src 目录下的文件
                              ["src/", "lib/"] 统计 src 或 lib 目录下的文件
            exclude_patterns: 可选的排除模式列表，用于排除匹配的文件
                            支持glob模式，例如: ["**/tests/**", "**/*.md"]

        Returns:
            包含代码行数统计的字典:
            - additions: 增加的行数
            - deletions: 删除的行数
            - total: 总修改行数（增加+删除）
            - exists: commit是否存在（用于区分失败和0修改）
        """
        try:
            # 确保获取完整的commit对象
            commit = self.repo.revparse_single(commit_sha).peel(pygit2.Commit)

            # 获取parent用于diff
            if commit.parents:
                parent = commit.parents[0]
                diff = self.repo.diff(parent, commit)
            else:
                # 初始commit，与空树比较
                diff = self.repo.diff(None, commit)

            additions = 0
            deletions = 0

            # Normalize path_prefix to list for uniform handling
            path_prefixes = None
            if path_prefix is not None:
                if isinstance(path_prefix, str):
                    path_prefixes = [path_prefix]
                else:
                    path_prefixes = path_prefix

            for patch in diff:
                file_path = patch.delta.new_file.path

                # 如果指定了排除模式，先检查是否需要排除
                if exclude_patterns:
                    if SrcFileFilter.should_exclude_file(file_path, exclude_patterns):
                        continue

                # 如果指定了路径前缀，只统计匹配的文件
                if path_prefixes is not None:
                    # Check if file matches any of the prefixes
                    if not any(file_path.startswith(prefix) for prefix in path_prefixes):
                        continue

                line_stats = patch.line_stats
                # line_stats返回 (context_lines, additions, deletions)
                additions += line_stats[1]
                deletions += line_stats[2]

            return {
                "additions": additions,
                "deletions": deletions,
                "total": additions + deletions,
                "exists": True,  # commit存在
            }
        except Exception:
            # 如果获取失败，返回特殊标记
            # 静默处理，不打印每个失败的commit
            return {"additions": 0, "deletions": 0, "total": 0, "exists": False}  # commit不存在或获取失败

    def _get_file_status(self, status_flag) -> str:
        """将pygit2的状态标志转换为字符串"""
        from pygit2 import GIT_DELTA_ADDED, GIT_DELTA_DELETED, GIT_DELTA_MODIFIED, GIT_DELTA_RENAMED

        status_map = {
            GIT_DELTA_ADDED: "added",
            GIT_DELTA_DELETED: "deleted",
            GIT_DELTA_MODIFIED: "modified",
            GIT_DELTA_RENAMED: "renamed",
        }
        return status_map.get(status_flag, "unknown")

    def get_all_main_commits(self) -> List[str]:
        """获取主分支上的所有commits（沿first-parent链）

        Returns:
            主分支commit SHA列表（从新到旧）
        """
        commits = []
        c = self.main_head
        while c:
            commits.append(str(c.id))
            if not c.parents:
                break
            c = c.parents[0]  # 只走first-parent
        return commits

    # ---------- Branch-out 分支模型支持 ----------

    def _get_first_parent_chain(self, ref: str, limit: int = 10000) -> List[pygit2.Oid]:
        """获取 ref 的 first-parent 链（返回 Oid 列表）

        Args:
            ref: 起始点（tag 名、commit sha 等）
            limit: 最大遍历深度

        Returns:
            first-parent 链上的 commit Oid 列表（从新到旧）
        """
        chain = []
        c = self._peel_to_commit(ref)
        count = 0
        while c and count < limit:
            chain.append(c.id)
            if not c.parents:
                break
            c = c.parents[0]
            count += 1
        return chain

    def find_branch_out_point(self, tag: str, reference: Optional[str] = None) -> str:
        """找到 tag 从 reference 的 first-parent 链上 branch out 的点

        适用于 branch-out 分支模型（如 element-web）：
        - Tags 打在从主干 branch out 的 release 分支上
        - 需要找到 tag 从主干分叉出去的那个 commit

        算法：找到 tag 的 first-parent 链上，第一个也在 reference first-parent 链上的 commit

        Args:
            tag: 要查找的 tag
            reference: 参考点（默认为 main_refname）

        Returns:
            branch-out point 的 commit SHA

        Example:
            主干:        ... ─── A ─── B ─── C ─── D ─── ...
                                │
            release:            └─ rc.0 ─ dep_upgrade ─ v1.0.0 (tag)

            find_branch_out_point("v1.0.0", "D") 返回 "A"
        """
        if reference is None:
            reference = self.main_refname
        # 获取 tag 的 first-parent 链
        tag_chain = self._get_first_parent_chain(tag)

        # 获取 reference 的 first-parent 链 set
        ref_chain_set = set(self._get_first_parent_chain(reference))

        # 找到 tag 链上第一个也在 reference 链上的 commit
        for oid in tag_chain:
            if oid in ref_chain_set:
                return str(oid)

        raise ValueError(f"Cannot find branch-out point for {tag} relative to {reference}")

    def get_commits_between_branch_out_points(self, start_tag: str, end_tag: str) -> List[str]:
        """获取两个 tags 的 branch-out points 之间的 commits（适用于 branch-out 分支模型）

        适用于：tags 从主干 branch out 的项目（如 element-web）

        算法：
        1. 找到 start_tag 的 branch-out point（相对于 end_tag 的 first-parent 链）
        2. 找到 end_tag 的 branch-out point（相对于 main_branch 的 first-parent 链）
        3. 返回两个 branch-out points 之间的 first-parent commits

        Args:
            start_tag: 起始 tag（不包括）
            end_tag: 结束 tag（包括其 branch-out point）

        Returns:
            commit SHA 列表（从新到旧）

        Example:
            主干:    ... ─ A (start_bo) ─ B ─ C ─ D (end_bo) ─ ...
                          │                       │
            releases:     └─ v1.0.0               └─ v2.0.0

            返回: [D, C, B]（不包括 A）
        """
        # 找到两个 tag 相对于 main 的 branch-out point
        start_bo = self.find_branch_out_point(start_tag, self.main_refname)
        end_bo = self.find_branch_out_point(end_tag, self.main_refname)

        print(f"    start_tag {start_tag} -> branch-out: {start_bo[:8]}")
        print(f"    end_tag {end_tag} -> branch-out: {end_bo[:8]}")

        # 如果两个点相同，返回空
        if start_bo == end_bo:
            print("    Warning: start and end branch-out points are the same")
            return []

        # 获取 start_bo..end_bo 之间的 first-parent commits
        start_oid = self._peel_to_commit(start_bo).id
        end_commit = self._peel_to_commit(end_bo)

        commits = []
        c = end_commit
        while c.id != start_oid:
            commits.append(str(c.id))
            if not c.parents:
                raise ValueError(
                    f"Reached root without finding start branch-out point. "
                    f"start_tag={start_tag}, end_tag={end_tag}, "
                    f"start_bo={start_bo}, end_bo={end_bo}"
                )
            c = c.parents[0]  # first-parent only

        return commits


# ---------- 用法示例 ----------
if __name__ == "__main__":
    repo_path = os.getenv("DEEPCOMMIT_REPO_DIR", os.getenv("AGENTBENCH_REPO_DIR", "/path/to/bare/repo.git"))
    repo = LocalGitRepo(
        repo_path,
        main_branch=os.getenv("DEEPCOMMIT_MAIN_BRANCH", os.getenv("AGENTBENCH_MAIN_BRANCH", "main")),
    )

    # 1) 判断 tag 是否在 main 主线
    print("Testing if 2.1.0 is on mainline...")
    on_mainline = repo.is_tag_on_main_first_parent("2.1.0")
    print(f"2.1.0 on mainline: {on_mainline}")

    # 2) 找到 2.1.0 在主线上的"上一个 tag"
    prev_of_210 = repo.previous_tag_on_main("2.1.0")
    print(f"Previous tag before 2.1.0: {prev_of_210}")

    # 3) 获取两个tag之间的所有commits（包含2.1.0的变更）
    if prev_of_210:
        commits = repo.get_commits_between_tags("2.1.0", "2.3.0", include_start=True)
        print(f"Commits between 2.1.0 and 2.3.0 (including 2.1.0 changes): {len(commits)}")

    # 4) 获取主线上的commits
    if prev_of_210:
        mainline_commits = repo.commits_between_on_main(prev_of_210, "2.3.0")
        print(f"Mainline commits from {prev_of_210} to 2.3.0: {len(mainline_commits)}")
    else:
        print("No previous tag found, skipping mainline commits check")

    # 5) 获取所有tags
    all_tags = repo.get_all_tags()
    print(f"Total tags in repo: {len(all_tags)}")
    if len(all_tags) > 5:
        print("Last 5 tags:")
        for tag_name, sha, date in all_tags[-5:]:
            print(f"  {tag_name}: {sha[:8]} ({date})")

    # 6) 获取tag详细信息
    tag_info = repo.get_tag_info("2.1.0")  # Method is commented out
    print(f"Tag 2.1.0 info: type={tag_info['type']}, on_mainline={tag_info['on_mainline']}")
