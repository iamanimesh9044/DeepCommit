#!/usr/bin/env python3
"""
Commit DAG Loader

用于加载和管理 commit DAG 数据的工具类。

功能：
- 加载 enhanced format commit_dag JSON 文件（由 commit_dag_v3.py 生成）
- 构建正向和反向依赖关系映射
- 提供高效的依赖查询接口
- 支持路径查找和节点遍历
- 提供拓扑排序、层级信息、节点度数等元数据访问
- 支持 split commit 的在线合并（使用 git blame 精确映射边）

Enhanced Format 特性：
- 节点包含完整的度数和祖先/后代计数信息
- 边包含涉及的文件列表 (files_ref)
- 提供预计算的拓扑排序结果
- 包含 DAG 元数据（层数、节点数、边数等）
"""

import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Import commit_utils for normalization
from ...tools.utils.commit_utils import normalize_commit_sha


class CommitDAGLoader:
    """
    加载和管理 commit DAG 数据 (Enhanced Format)

    此加载器专门用于加载由 commit_dag_v3.py 生成的 enhanced format JSON 文件。
    Enhanced format 包含以下字段：
    - meta: DAG 元数据（层数、节点数、边数、约束信息）
    - nodes: 节点详情（SHA、主题、层级、度数、祖先/后代计数）
    - edges_dag: 边列表（源、目标、涉及的文件）
    - layers: 按层级分组的节点
    - topo_order: 拓扑排序结果
    """

    def __init__(self, dag_json_path: str):
        """
        初始化 DAG 加载器

        Args:
            dag_json_path: commit_dag enhanced format JSON 文件路径
                          (通常为 *_enhanced.json，由 commit_dag_v3.py 生成)

        Raises:
            ValueError: 如果文件格式不是 enhanced format
        """
        self.dag_json_path = Path(dag_json_path)
        self.dag_data = self._load_dag()

        # 构建快速查询的数据结构
        self.nodes = self.dag_data.get("nodes", {})
        self.edges_map = self._build_edges_map()
        self.reverse_edges_map = self._build_reverse_edges_map()

        # 保持与 enhanced format 一致的字段名
        self.layers = self.dag_data.get("layers", {})  # {layer: [shas]}
        # 构建 sha -> layer 的索引（用于快速查询单个节点的层级）
        self._layer_index = self._build_layer_index()

        # 传递闭包（懒加载）
        self._transitive_closure = None
        self._reverse_transitive_closure = None

    def _load_dag(self) -> Dict:
        """
        加载 DAG JSON 文件 (Enhanced format from commit_dag_v3.py)

        Expected format:
        - nodes: {short_sha: {sha, subject, layer, in_degree, out_degree, ancestors_count, descendants_count}}
        - edges_dag: [{source, target, files_ref}]
        - layers: {layer_str: [short_shas]}
        - meta: {...}
        - topo_order: [short_shas]
        """
        with open(self.dag_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 验证是否为 enhanced format
        if "meta" not in data or "edges_dag" not in data:
            raise ValueError(
                f"Invalid DAG format. Expected enhanced format from commit_dag_v3.py "
                f"with 'meta' and 'edges_dag' fields. "
                f"Found keys: {list(data.keys())}"
            )

        # 直接使用 enhanced 格式，只做字段映射
        return self._prepare_enhanced_data(data)

    def _prepare_enhanced_data(self, enhanced_data: Dict) -> Dict:
        """
        准备 enhanced 格式数据，保持字段名称一致

        直接使用 enhanced 格式，不重命名字段
        """
        return {
            "nodes": enhanced_data.get("nodes", {}),  # 直接使用
            "edges_dag": enhanced_data.get("edges_dag", []),  # 保持原字段名
            "layers": enhanced_data.get("layers", {}),  # 保持原字段名和结构
            "topo_order": enhanced_data.get("topo_order", []),
            "meta": enhanced_data.get("meta", {}),
        }

    def _build_layer_index(self) -> Dict[str, int]:
        """
        构建 sha -> layer 的索引（用于快速查询单个节点的层级）

        从 layers {layer: [shas]} 转换为 {sha: layer}

        Returns:
            {short_sha: layer_int}
        """
        layer_index = {}
        for layer_str, short_shas in self.layers.items():
            layer = int(layer_str)
            for short_sha in short_shas:
                layer_index[short_sha] = layer
        return layer_index

    def _build_edges_map(self) -> Dict[str, List[Dict]]:
        """
        构建依赖关系映射（正向）

        Returns:
            {target: [{"source": source_sha, "files_ref": [...]}, ...]}
            即：每个 commit 依赖哪些 commits
        """
        edges_map = {}
        for edge in self.dag_data.get("edges_dag", []):
            source = edge["source"]
            target = edge["target"]
            files_ref = edge.get("files_ref", [])

            if target not in edges_map:
                edges_map[target] = []

            edges_map[target].append({"source": source, "files_ref": files_ref})

        return edges_map

    def _build_reverse_edges_map(self) -> Dict[str, List[str]]:
        """
        构建反向依赖关系映射

        Returns:
            {source: [target1, target2, ...]}
            即：每个 commit 被哪些 commits 依赖
        """
        reverse_map = {}
        for edge in self.dag_data.get("edges_dag", []):
            source = edge["source"]
            target = edge["target"]

            if source not in reverse_map:
                reverse_map[source] = []

            reverse_map[source].append(target)

        return reverse_map

    def get_dependencies(self, commit_sha: str) -> List[Dict]:
        """
        获取 commit 的直接依赖

        Args:
            commit_sha: Commit SHA (支持短或长格式)

        Returns:
            依赖列表，每个元素包含 {"src": sha, "kind": [types]}
        """
        # 标准化 SHA（尝试匹配长或短格式）
        for sha in self.edges_map.keys():
            if self._sha_match(sha, commit_sha):
                return self.edges_map.get(sha, [])
        return []

    def get_all_nodes_on_paths(self, from_sha: str, to_sha: str) -> Set[str]:
        """
        获取从 from_sha 到 to_sha 的所有可达路径上的所有节点

        使用 DFS 遍历所有路径，收集路径上的所有节点

        Args:
            from_sha: 源 commit
            to_sha: 目标 commit

        Returns:
            所有路径上的节点集合（包括 from_sha 和 to_sha）
        """
        # 标准化 SHA
        from_sha = self._normalize_sha(from_sha)
        to_sha = self._normalize_sha(to_sha)

        if not from_sha or not to_sha:
            return set()

        all_nodes = set()
        visited_in_path = set()

        def dfs(current: str, path: List[str]) -> bool:
            """
            DFS 搜索所有路径

            Returns:
                True 如果找到至少一条路径
            """
            if current in visited_in_path:
                return False

            path.append(current)
            visited_in_path.add(current)

            if self._sha_match(current, to_sha):
                # 找到一条路径，记录所有节点
                all_nodes.update(path)
                visited_in_path.remove(current)
                path.pop()
                return True

            found_path = False

            # 查找所有被 current 依赖的 commits（反向遍历）
            dependents = self.reverse_edges_map.get(current, [])
            for dependent in dependents:
                if dfs(dependent, path):
                    found_path = True

            visited_in_path.remove(current)
            path.pop()
            return found_path

        dfs(from_sha, [])
        return all_nodes

    def has_dependency_path(self, from_sha: str, to_sha: str) -> bool:
        """
        检查是否存在从 from_sha 到 to_sha 的依赖路径

        即：to_sha 是否直接或间接依赖于 from_sha

        Args:
            from_sha: 源 commit
            to_sha: 目标 commit

        Returns:
            True 如果存在依赖路径
        """
        # 标准化 SHA
        from_sha = self._normalize_sha(from_sha)
        to_sha = self._normalize_sha(to_sha)

        if not from_sha or not to_sha:
            return False

        if self._sha_match(from_sha, to_sha):
            return True

        # BFS 搜索依赖路径
        visited = set()
        to_visit = [from_sha]

        while to_visit:
            current = to_visit.pop(0)

            if current in visited:
                continue

            if self._sha_match(current, to_sha):
                return True

            visited.add(current)

            # 查找所有被 current 依赖的 commits
            for dependent in self.reverse_edges_map.get(current, []):
                if dependent not in visited:
                    to_visit.append(dependent)

        return False

    def has_direct_dependency(self, from_sha: str, to_sha: str) -> bool:
        """
        检查 to_sha 是否**直接依赖**于 from_sha

        注意：只检查直接边，不检查传递依赖

        Args:
            from_sha: 源 commit
            to_sha: 目标 commit

        Returns:
            True 如果 to_sha 直接依赖于 from_sha（在 DAG 中存在直接边）
        """
        # 获取 to_sha 的直接依赖
        deps = self.get_dependencies(to_sha)

        # 检查 from_sha 是否在直接依赖中
        for dep in deps:
            if self._sha_match(dep["source"], from_sha):
                return True

        return False

    def get_dependency_chain(self, from_sha: str, to_sha: str) -> Optional[List[str]]:
        """
        获取从 from_sha 到 to_sha 的依赖链（如果存在）

        Args:
            from_sha: 源 commit
            to_sha: 目标 commit

        Returns:
            依赖链 [from_sha, ..., to_sha] 或 None
        """
        # BFS 搜索，记录路径
        visited = {}
        to_visit = [(from_sha, [from_sha])]

        while to_visit:
            current, path = to_visit.pop(0)

            if current in visited:
                continue

            # 标准化 SHA 匹配
            if self._sha_match(current, to_sha):
                return path

            visited[current] = True

            # 查找所有依赖于 current 的 commits
            for dst_sha, deps in self.edges_map.items():
                for dep in deps:
                    if self._sha_match(dep["source"], current):
                        to_visit.append((dst_sha, path + [dst_sha]))

        return None

    def get_commit_info(self, commit_sha: str) -> Optional[Dict]:
        """获取 commit 的详细信息"""
        for sha, info in self.nodes.items():
            if self._sha_match(sha, commit_sha):
                return info
        return None

    def get_layer(self, commit_sha: str) -> Optional[int]:
        """获取 commit 在 DAG 中的层级 (layer)"""
        for sha, layer in self._layer_index.items():
            if self._sha_match(sha, commit_sha):
                return layer
        return None

    def get_meta(self) -> Dict:
        """
        获取 DAG 的元数据信息

        Returns:
            包含 layers_count, nodes_count, edges_count, constraints, dag_info 等信息
        """
        return self.dag_data.get("meta", {})

    def get_topo_order(self) -> List[str]:
        """
        获取拓扑排序结果（已经是完整SHA）

        Returns:
            按拓扑顺序排列的 commit SHA 列表
        """
        return self.dag_data.get("topo_order", [])

    def get_commit_layer(self, commit_sha: str) -> Optional[int]:
        """
        获取 commit 所在的 layer

        Args:
            commit_sha: Commit SHA (支持短或长格式)

        Returns:
            Layer 编号，如果不存在则返回 None
        """
        return self.get_layer(commit_sha)

    def get_commit_degrees(self, commit_sha: str) -> Optional[Dict[str, int]]:
        """
        获取 commit 的入度和出度信息

        Args:
            commit_sha: Commit SHA (支持短或长格式)

        Returns:
            包含 in_degree 和 out_degree 的字典，如果不存在则返回 None
        """
        info = self.get_commit_info(commit_sha)
        if info:
            return {
                "in_degree": info.get("in_degree", 0),
                "out_degree": info.get("out_degree", 0),
            }
        return None

    def get_commit_ancestry(self, commit_sha: str) -> Optional[Dict[str, int]]:
        """
        获取 commit 的祖先和后代数量

        Args:
            commit_sha: Commit SHA (支持短或长格式)

        Returns:
            包含 ancestors_count 和 descendants_count 的字典，如果不存在则返回 None
        """
        info = self.get_commit_info(commit_sha)
        if info:
            return {
                "ancestors_count": info.get("ancestors_count", 0),
                "descendants_count": info.get("descendants_count", 0),
            }
        return None

    def get_files_for_dependency(self, from_sha: str, to_sha: str) -> List[str]:
        """
        获取某个依赖关系涉及的文件列表

        Args:
            from_sha: 源 commit (被依赖的 commit)
            to_sha: 目标 commit (依赖于 from_sha 的 commit)

        Returns:
            文件路径列表，如果依赖不存在则返回空列表
        """
        deps = self.get_dependencies(to_sha)
        for dep in deps:
            if self._sha_match(dep["source"], from_sha):
                return dep.get("files_ref", [])
        return []

    def _normalize_sha(self, sha: str) -> Optional[str]:
        """
        标准化 SHA，返回在 DAG 中存在的完整 SHA

        Args:
            sha: 短或长 SHA

        Returns:
            完整 SHA 或 None
        """
        # 在 nodes 中查找
        for node_sha in self.nodes.keys():
            if self._sha_match(node_sha, sha):
                return node_sha

        # 在 edges 中查找
        for edge_sha in self.edges_map.keys():
            if self._sha_match(edge_sha, sha):
                return edge_sha

        for edge_sha in self.reverse_edges_map.keys():
            if self._sha_match(edge_sha, sha):
                return edge_sha

        return None

    def _sha_match(self, sha1: str, sha2: str) -> bool:
        """检查两个 SHA 是否匹配（支持短格式）"""
        if not sha1 or not sha2:
            return False
        short_len = min(len(sha1), len(sha2), 7)
        return sha1[:short_len] == sha2[:short_len]

    def get_forward_dependency_graph(self, normalize: bool = True) -> Dict[str, Set[str]]:
        """
        获取正向依赖图（前向边）

        返回一个映射：每个 commit 被哪些 commits 依赖
        即：如果 dst 依赖 src（边 src -> dst），则 graph[src] 包含 dst

        这对于计算传递闭包很有用：从某个 commit 出发可以到达哪些 commits

        Args:
            normalize: 是否对 commit SHA 进行标准化（默认 True）

        Returns:
            {commit_sha: {dependent_commits}}
            即：graph[src] = {dst1, dst2, ...} 表示 dst1, dst2 等依赖 src

        Example:
            >>> loader = CommitDAGLoader("commit_dag.json")
            >>> graph = loader.get_forward_dependency_graph()
            >>> # graph["abc123"] = {"def456", "ghi789"}
            >>> # 表示 def456 和 ghi789 都依赖 abc123
        """
        forward_graph = defaultdict(set)

        if normalize:
            # 使用 edges_map: {target: [{"source": source, "files_ref": [...]}, ...]}
            for target_commit, dependencies in self.edges_map.items():
                target_normalized = normalize_commit_sha(target_commit)
                for dep in dependencies:
                    source_normalized = normalize_commit_sha(dep["source"])
                    # target 依赖 source，所以添加 source -> target
                    forward_graph[source_normalized].add(target_normalized)
        else:
            # 直接使用 reverse_edges_map
            for src_commit, dst_list in self.reverse_edges_map.items():
                forward_graph[src_commit] = set(dst_list)

        return dict(forward_graph)

    def get_transitive_closure(self) -> Dict[str, Set[str]]:
        """
        获取传递闭包（懒加载，计算一次后缓存）

        传递闭包包含所有直接和间接的依赖关系。
        例如：如果 A→B 且 B→C，则闭包中 A→{B, C}

        这个方法在第一次调用时计算传递闭包并缓存结果，
        后续调用直接返回缓存的结果，性能为 O(1)。

        Returns:
            传递闭包字典 {commit_sha: {reachable_commits}}

        Example:
            >>> closure = dag.get_transitive_closure()
            >>> if 'commit_b' in closure.get('commit_a', set()):
            >>>     print("commit_a can reach commit_b", file=sys.stderr)
        """
        if self._transitive_closure is None:
            print("  🔧 Computing transitive closure (one-time operation)...", file=sys.stderr)

            # 构建依赖图格式：{node: {neighbors}}
            # reverse_edges_map[src] = [dst1, dst2, ...] 表示 src → dst
            dependencies = defaultdict(set)
            for src, dst_list in self.reverse_edges_map.items():
                dependencies[src] = set(dst_list)

            # 使用 graph_algorithms 计算传递闭包
            from ...tools.utils.graph_algorithms import compute_transitive_closure

            self._transitive_closure = compute_transitive_closure(dependencies, set(self.nodes.keys()))

            print(f"     ✓ Transitive closure computed for {len(self.nodes)} nodes", file=sys.stderr)

        return self._transitive_closure

    def get_reverse_transitive_closure(self) -> Dict[str, Set[str]]:
        """
        获取反向传递闭包（懒加载，从正向闭包转换）

        反向传递闭包包含所有能到达某个节点的节点（依赖的逆向）。
        例如：如果 A→B 且 B→C，则反向闭包中 C→{A, B}（所有能到达 C 的节点）

        优化：直接从正向传递闭包转换而来，避免重复计算 BFS。
        时间复杂度：O(闭包边数) ≈ O(V²)

        Returns:
            反向传递闭包字典 {commit_sha: {nodes_that_can_reach_it}}

        Example:
            >>> reverse_closure = dag.get_reverse_transitive_closure()
            >>> dependencies = reverse_closure.get('commit_c', set())
            >>> # dependencies 包含所有 commit_c 依赖的节点
        """
        if self._reverse_transitive_closure is None:
            print("  🔧 Building reverse transitive closure from forward closure...", file=sys.stderr)

            # 先确保正向传递闭包已计算
            closure = self.get_transitive_closure()

            # 从正向闭包构建反向闭包
            # 如果 closure[A] 包含 B，则 reverse_closure[B] 应包含 A
            reverse_closure = defaultdict(set)
            for src, reachable_nodes in closure.items():
                for dst in reachable_nodes:
                    reverse_closure[dst].add(src)

            self._reverse_transitive_closure = dict(reverse_closure)

            print(f"     ✓ Reverse transitive closure built for {len(self.nodes)} nodes", file=sys.stderr)

        return self._reverse_transitive_closure

    def has_dependency_path_fast(self, from_sha: str, to_sha: str) -> bool:
        """
        使用传递闭包快速检查是否存在依赖路径（O(1) 查询）

        这个方法比 has_dependency_path() 快得多，特别是在需要多次查询时。
        第一次调用时会计算传递闭包（O(V × (V + E))），之后每次查询都是 O(1)。

        适用场景：
        - 需要对同一个 DAG 进行大量路径查询（如分析多个 milestones）
        - DAG 规模适中（节点数 < 100k）

        Args:
            from_sha: 源 commit
            to_sha: 目标 commit

        Returns:
            True 如果存在从 from_sha 到 to_sha 的路径

        Example:
            >>> if dag.has_dependency_path_fast("abc1234", "def5678"):
            >>>     print("Path exists!", file=sys.stderr)
        """
        # 获取传递闭包（第一次调用会计算并缓存）
        closure = self.get_transitive_closure()

        # 标准化 SHA
        from_sha = self._normalize_sha(from_sha)
        to_sha = self._normalize_sha(to_sha)

        if not from_sha or not to_sha:
            return False

        # 检查自环
        if self._sha_match(from_sha, to_sha):
            return True

        # O(1) 查表
        return to_sha in closure.get(from_sha, set())

    def get_intermediate_nodes_fast(self, from_sha: str, to_sha: str) -> Set[str]:
        """
        使用传递闭包快速获取在 from_sha → to_sha 路径上的所有节点

        节点 N 在路径上当且仅当：
        1. from_sha 能到达 N（或 N == from_sha）
        2. N 能到达 to_sha（或 N == to_sha）

        这个方法比 get_all_nodes_on_paths() 快得多，避免了指数级路径枚举。
        时间复杂度：O(V) vs O(指数级)

        Args:
            from_sha: 源 commit
            to_sha: 目标 commit

        Returns:
            路径上的所有节点集合（包括 from_sha 和 to_sha）

        Example:
            >>> # A → B → C → D
            >>> nodes = dag.get_intermediate_nodes_fast("A", "D")
            >>> print(nodes, file=sys.stderr)  # {'A', 'B', 'C', 'D'}
        """
        # 获取传递闭包
        closure = self.get_transitive_closure()

        # 标准化 SHA
        from_sha = self._normalize_sha(from_sha)
        to_sha = self._normalize_sha(to_sha)

        if not from_sha or not to_sha:
            return set()

        # 如果不存在路径，返回空集
        if not self.has_dependency_path_fast(from_sha, to_sha):
            return set()

        nodes_on_path = set()

        # 遍历所有节点，检查是否在路径上
        for node in self.nodes.keys():
            # 条件1：from_sha 能到达 node（或 node == from_sha）
            from_can_reach_node = node == from_sha or node in closure.get(from_sha, set())

            # 条件2：node 能到达 to_sha（或 node == to_sha）
            node_can_reach_to = node == to_sha or to_sha in closure.get(node, set())

            # 两个条件都满足，则节点在路径上
            if from_can_reach_node and node_can_reach_to:
                nodes_on_path.add(node)

        return nodes_on_path

    # =========================================================================
    # Split Commit Support (with Git Blame-based Edge Remapping)
    # =========================================================================

    def apply_split_metadata(
        self,
        split_metadata_dir: str,
        repo_path: Optional[str] = None,
        use_blame: bool = True,
        context_lines: int = 5,
        follow_moves: bool = True,
        ignore_whitespace: bool = True,
        follow_depth: int = 2,
    ) -> int:
        """
        在线合并 split commit 的内部边到 DAG（不持久化）

        当原始 commit O 被 split 成 sub-commits A, B, C 时：
        1. 添加 A, B, C 到 nodes
        2. 添加内部边（如 A->B->C）
        3. 重新映射入边：X->O 变成 X->最上游的相关 split commit
        4. 重新映射出边：O->Y 变成 精确匹配的 split commit->Y（使用 git blame）
        5. 删除原始 commit O

        边重映射策略（与 commit_dag_v3 保持一致）：
        - 使用 git blame 分析 Y 的老端行，确定它们依赖 O 的哪些 hunk
        - 根据 hunk 对应关系，精确映射到正确的 split commit
        - 这避免了简单的"连接到最下游"策略导致的误报

        Args:
            split_metadata_dir: 包含 split-xxx 目录的父目录
                               例如: trial_space/xxx/stage4_decomp/output/commit_splits/
            repo_path: Git 仓库路径，用于执行 blame 操作（必须提供）
            use_blame: 是否使用 git blame 进行精确映射（必须为 True）
            context_lines: blame 时的上下文行数（与 commit_dag_v3 一致）
            follow_moves: 是否追踪文件移动/重命名（-M 标志）
            ignore_whitespace: 是否忽略空白变化（-w 标志）
            follow_depth: 复制检测深度（1-3，默认 2）

        Returns:
            处理的 split commit 数量
        """
        from pathlib import Path

        split_dir = Path(split_metadata_dir)
        if not split_dir.exists():
            return 0

        metadata_files = sorted(split_dir.glob("*/METADATA.json"))
        if not metadata_files:
            return 0

        if not repo_path or not use_blame:
            raise ValueError(
                "Blame-based split mapping requires repo_path and use_blame=True (fallback file mapping is disabled)."
            )

        # 初始化 Git 操作辅助器（如果提供了 repo_path）
        git_helper = _GitBlameHelper(
            repo_path=Path(repo_path),
            context_lines=context_lines,
            follow_moves=follow_moves,
            ignore_whitespace=ignore_whitespace,
            follow_depth=follow_depth,
        )

        processed_count = 0

        metadata_entries = []
        # 扫描所有包含 METADATA.json 的子目录
        # 支持两种命名格式：
        # 1. split-7d90f90 (带 split- 前缀)
        # 2. 7d90f90 (直接使用 commit SHA)
        for metadata_file in metadata_files:
            with open(metadata_file, "r", encoding="utf-8") as f:
                metadata = json.load(f)

            self._prepare_split_metadata(metadata, git_helper, metadata_file)
            metadata_entries.append((metadata, metadata_file))

        cache_dir = split_dir / ".deps_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        for metadata, _metadata_file in self._order_split_metadata_by_topo(metadata_entries):
            cache_data = self._load_split_cache(cache_dir, metadata)
            if cache_data:
                self._apply_cached_split(metadata, cache_data, git_helper)
            else:
                cache_payload = self._apply_single_split(metadata, git_helper)
                self._write_split_cache(cache_dir, cache_payload)
            processed_count += 1

        # 清空传递闭包缓存（下次查询时重算）
        self._transitive_closure = None
        self._reverse_transitive_closure = None

        return processed_count

    def _prepare_split_metadata(
        self,
        metadata: Dict,
        git_helper: "_GitBlameHelper",
        metadata_file: Optional[Path] = None,
    ) -> None:
        """Validate split metadata commits exist in repo and resolve usable refs.

        Supports two METADATA.json formats:
        1. New format: {"original_commit": {...}, "split_result": {"src_commits": [...]}}
        2. Legacy format: {"split_info": {"original_commit_sha": ...}, "split_result": {"commits": [...]}}
        """
        # Support both new and legacy formats
        original_commit = metadata.get("original_commit", {})
        split_info = metadata.get("split_info", {})

        # Handle string format for original_commit (e.g., "00c83af")
        if isinstance(original_commit, str):
            original_commit = {
                "hash": original_commit,
                "sha": original_commit,
                "short_hash": original_commit[:7],
            }
            metadata["original_commit"] = original_commit

        # If original_commit is empty, try to build it from split_info (legacy format)
        if not original_commit and split_info:
            original_commit = {
                "hash": split_info.get("original_commit_sha"),
                "sha": split_info.get("original_commit_sha"),
                "short_hash": split_info.get("original_commit_sha", "")[:7],
            }
            metadata["original_commit"] = original_commit

        split_result = metadata.get("split_result", {})
        # Support multiple formats:
        # 1. split_result.src_commits (preferred)
        # 2. split_result.commits (legacy)
        # 3. top-level "commits" (newest format)
        # 4. top-level "split_commits" (another format)
        split_commits = (
            split_result.get("src_commits", [])
            or split_result.get("commits", [])
            or metadata.get("commits", [])  # Support top-level commits
            or metadata.get("split_commits", [])  # Support top-level split_commits
        )
        # Normalize to src_commits for downstream code
        if not split_result:
            split_result = {}
            metadata["split_result"] = split_result
        split_result["src_commits"] = split_commits

        original_candidates = [
            original_commit.get("hash"),
            original_commit.get("sha"),
            original_commit.get("full_hash"),
            original_commit.get("short_hash"),
            original_commit.get("short_sha"),
        ]
        original_commit["resolved_hash"] = self._resolve_commit_ref(
            git_helper,
            original_candidates,
            "original commit",
            metadata_file,
        )
        original_commit["short_hash"] = self._normalize_short_hash(
            original_commit.get("short_hash") or original_commit.get("short_sha"),
            original_commit["resolved_hash"],
            "original commit",
            metadata_file,
        )
        if not original_commit.get("hash"):
            original_commit["hash"] = original_commit["resolved_hash"]

        for sc in split_commits:
            patch_hint = None
            patch_file = sc.get("patch_file")
            if patch_file:
                patch_hint = Path(patch_file).name.split(".")[0]

            split_candidates = [
                sc.get("hash"),
                sc.get("sha"),
                sc.get("full_hash"),
                sc.get("short_hash"),
                sc.get("short_sha"),
                patch_hint,
            ]
            short_hint = (
                sc.get("short_hash") or sc.get("short_sha") or sc.get("hash") or sc.get("sha") or patch_hint or ""
            )[:7]
            sc["resolved_hash"] = self._resolve_commit_ref(
                git_helper,
                split_candidates,
                f"split commit {short_hint}",
                metadata_file,
            )
            sc["short_hash"] = self._normalize_short_hash(
                sc.get("short_hash") or sc.get("short_sha"),
                sc["resolved_hash"],
                f"split commit {short_hint}",
                metadata_file,
            )
            if not sc.get("hash"):
                sc["hash"] = sc["resolved_hash"]
            if not sc.get("files"):
                sc_files = git_helper.get_changed_files(sc["resolved_hash"])
                if not sc_files:
                    # Fallback 1: try to parse files from patch_file
                    patch_file = sc.get("patch_file")
                    if patch_file and metadata_file:
                        patch_path = metadata_file.parent / patch_file
                        if patch_path.exists():
                            sc_files = self._parse_files_from_patch(patch_path)
                if not sc_files:
                    # Fallback 2: try to derive files from hunk_mapping in METADATA.json
                    hunk_mapping = metadata.get("hunk_mapping", {})
                    if hunk_mapping:
                        sc_short = sc.get("short_hash", "")[:7]
                        sc_files = [
                            file_path
                            for file_path, mappings in hunk_mapping.items()
                            if any(str(m.get("commit", ""))[:7] == sc_short for m in mappings)
                        ]
                if not sc_files:
                    location = f" (metadata: {metadata_file})" if metadata_file else ""
                    raise RuntimeError(f"Split commit {short_hint} has no changed files{location}.")
                sc["files"] = sc_files

    def _resolve_commit_ref(
        self,
        git_helper: "_GitBlameHelper",
        candidates: List[Optional[str]],
        label: str,
        metadata_file: Optional[Path] = None,
    ) -> str:
        """Resolve a commit ref that exists in the repo or raise."""
        cleaned: List[str] = []
        seen = set()
        for candidate in candidates:
            if not candidate:
                continue
            cand = str(candidate).strip()
            if not cand or cand in seen:
                continue
            seen.add(cand)
            cleaned.append(cand)

        for candidate in cleaned:
            resolved = git_helper.resolve_commit_ref(candidate)
            if resolved:
                return resolved

        location = f" (metadata: {metadata_file})" if metadata_file else ""
        raise RuntimeError(f"Split metadata references {label} not found in repo{location}: {cleaned}")

    def _normalize_short_hash(
        self,
        short_value: Optional[str],
        resolved_hash: str,
        label: str,
        metadata_file: Optional[Path] = None,
    ) -> str:
        """Normalize short hash to 7 chars and validate against resolved hash."""
        if short_value:
            short_value = str(short_value).strip().lower()

        if short_value and len(short_value) >= 7:
            if resolved_hash and not resolved_hash.startswith(short_value):
                location = f" (metadata: {metadata_file})" if metadata_file else ""
                raise RuntimeError(
                    f"Split metadata {label} short hash does not match resolved hash{location}: "
                    f"{short_value} vs {resolved_hash}"
                )
            return short_value[:7]

        if resolved_hash:
            return resolved_hash[:7]

        location = f" (metadata: {metadata_file})" if metadata_file else ""
        raise RuntimeError(f"Split metadata {label} missing resolvable hash{location}.")

    def _parse_files_from_patch(self, patch_path: Path) -> List[str]:
        """Parse file paths from a git patch file.

        Looks for lines like:
            diff --git a/path/to/file.go b/path/to/file.go
            --- a/path/to/file.go
            +++ b/path/to/file.go
        """
        import re

        files = set()
        try:
            with open(patch_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    # Match: diff --git a/path/to/file b/path/to/file
                    if line.startswith("diff --git "):
                        match = re.match(r"diff --git a/(.+?) b/(.+)", line.strip())
                        if match:
                            files.add(match.group(2))
        except Exception:
            pass
        return list(files)

    def _order_split_metadata_by_topo(
        self,
        metadata_entries: List[Tuple[Dict, Path]],
    ) -> List[Tuple[Dict, Path]]:
        """Order split metadata by original commit's topo order (short SHA)."""
        topo_order = [sha[:7] for sha in self.get_topo_order()]
        topo_index = {sha: idx for idx, sha in enumerate(topo_order)}

        def _key(entry: Tuple[Dict, Path]) -> Tuple[int, str]:
            metadata, _path = entry
            original_sha = metadata["original_commit"]["short_hash"][:7]
            return (topo_index.get(original_sha, len(topo_index)), original_sha)

        return sorted(metadata_entries, key=_key)

    def _split_cache_path(self, cache_dir: Path, original_hash: str) -> Path:
        """Get cache path for an original commit."""
        return cache_dir / f"original_{original_hash}.json"

    def _load_split_cache(self, cache_dir: Path, metadata: Dict) -> Optional[Dict]:
        """Load cached dependencies for a split commit if matching."""
        original_hash = metadata["original_commit"].get("resolved_hash")
        if not original_hash:
            return None

        cache_path = self._split_cache_path(cache_dir, original_hash)
        if not cache_path.exists():
            return None

        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
        except Exception:
            return None

        if not self._split_cache_matches(metadata, cache_data):
            return None

        return cache_data

    def _split_cache_matches(self, metadata: Dict, cache_data: Dict) -> bool:
        """Check if cache matches current split metadata."""
        original_hash = metadata["original_commit"].get("resolved_hash")
        if not original_hash:
            return False

        cache_original = cache_data.get("original_commit", {})
        if cache_original.get("hash") != original_hash:
            return False

        split_commits = metadata["split_result"].get("src_commits", [])
        meta_split_hashes = sorted(sc.get("resolved_hash") for sc in split_commits if sc.get("resolved_hash"))

        cache_split_hashes = sorted(sc.get("hash") for sc in cache_data.get("split_commits", []) if sc.get("hash"))

        return meta_split_hashes == cache_split_hashes and len(meta_split_hashes) == len(split_commits)

    def _write_split_cache(self, cache_dir: Path, cache_payload: Dict) -> None:
        """Write split dependencies cache to disk."""
        original_hash = cache_payload.get("original_commit", {}).get("hash")
        if not original_hash:
            return

        cache_path = self._split_cache_path(cache_dir, original_hash)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_payload, f, indent=2)

    def _build_split_cache_payload(
        self,
        metadata: Dict,
        internal_edges: List[List[str]],
        inbound_map: Dict[str, Set[str]],
        outbound_map: Dict[str, Set[str]],
    ) -> Dict:
        """Build cache payload for a split commit."""
        original_commit = metadata["original_commit"]
        split_commits = metadata["split_result"]["src_commits"]

        inbound = {k[:7]: sorted(v) for k, v in inbound_map.items()}
        outbound = {k[:7]: sorted(v) for k, v in outbound_map.items()}

        return {
            "original_commit": {
                "hash": original_commit.get("resolved_hash", original_commit.get("hash")),
                "short_hash": original_commit.get("short_hash", "")[:7],
            },
            "split_commits": [
                {
                    "hash": sc.get("resolved_hash", sc.get("hash", sc.get("full_hash"))),
                    "short_hash": sc.get("short_hash", "")[:7],
                }
                for sc in split_commits
            ],
            "edges": {
                "internal": internal_edges,
                "inbound": inbound,
                "outbound": outbound,
            },
        }

    def _add_split_nodes(self, original_sha: str, split_commits: List[Dict]) -> None:
        """Add split commit nodes to DAG with original layer metadata."""
        original_node = self.nodes.get(original_sha, {})
        original_layer = original_node.get("layer", 0)

        for sc in split_commits:
            sc_sha = sc["short_hash"][:7]
            self.nodes[sc_sha] = {
                "sha": sc.get("resolved_hash", sc.get("hash", sc.get("full_hash", sc_sha))),
                "subject": f"[Split from {original_sha}]",
                "layer": original_layer,
                "in_degree": 0,
                "out_degree": 0,
                "ancestors_count": 0,
                "descendants_count": 0,
            }

    def _apply_cached_split(
        self,
        metadata: Dict,
        cache_data: Dict,
        git_helper: "_GitBlameHelper",
    ) -> None:
        """Apply cached split dependencies, falling back to blame when needed."""
        original_commit = metadata["original_commit"]
        original_sha = original_commit["short_hash"][:7]
        original_full_sha = original_commit.get("resolved_hash", original_commit.get("hash", original_sha))

        split_commits = metadata["split_result"]["src_commits"]
        cached_edges = cache_data.get("edges", {})
        cached_internal = cached_edges.get("internal", [])
        cached_inbound = cached_edges.get("inbound", {})
        cached_outbound = cached_edges.get("outbound", {})

        self._add_split_nodes(original_sha, split_commits)

        for src_sha, dst_sha in cached_internal:
            self._add_edge(src_sha, dst_sha, [])

        original_in_edges = self.edges_map.get(original_sha, [])
        for edge_info in original_in_edges:
            source_sha = edge_info["source"][:7]
            files_ref = edge_info.get("files_ref", [])

            target_splits = cached_inbound.get(source_sha)
            if not target_splits:
                target_splits = self._get_splits_by_blame_for_inbound(
                    git_helper,
                    source_sha,
                    split_commits,
                    [],
                )

            for target_split_sha in target_splits:
                self._add_edge(source_sha, target_split_sha, files_ref)

        original_out_targets = self.reverse_edges_map.get(original_sha, [])
        hunk_mapping = None
        for target_sha in original_out_targets:
            edge_files = self._get_files_for_edge(original_sha, target_sha)
            target_sha = target_sha[:7]

            source_splits = cached_outbound.get(target_sha)
            if not source_splits:
                if hunk_mapping is None:
                    hunk_mapping = self._build_hunk_mapping(git_helper, original_full_sha, split_commits)
                source_splits = self._get_splits_by_blame(
                    git_helper,
                    original_sha,
                    target_sha,
                    edge_files,
                    hunk_mapping,
                    split_commits,
                    [],
                )

            for source_split_sha in source_splits:
                self._add_edge(source_split_sha, target_sha, edge_files)

        self._remove_commit(original_sha)

    def _apply_single_split(self, metadata: Dict, git_helper: Optional["_GitBlameHelper"] = None) -> Dict:
        """
        应用单个 split commit 的变更到 DAG

        算法（统一使用 blame 构建所有边）：
        1. 获取原始 commit 和 split commits 信息
        2. 构建 hunk 范围映射（用于 split 映射）
        3. 添加 split commits 到 nodes
        4. 使用 blame 计算 sub-commits 之间的依赖（替代直接使用 dependency_edges）
        5. 使用 blame 重新映射入边 (X -> original) => (X -> sub-commits)
        6. 使用 blame 重新映射出边 (original -> Y) => (sub-commits -> Y)
        7. 移除原 commit 的节点和边

        注意：不再直接使用 METADATA.json 中的 dependency_edges，
        而是统一通过 blame 来确定所有依赖关系（包括 sub-commits 之间的依赖）。
        """
        original_commit = metadata["original_commit"]
        original_sha = original_commit["short_hash"][:7]
        original_full_sha = original_commit.get("resolved_hash", original_commit.get("hash", original_sha))

        split_result = metadata["split_result"]
        split_commits = split_result["src_commits"]
        # 注意：不再使用 dependency_edges，改为通过 blame 计算

        # 1. 构建 hunk 范围 -> split_commit 映射（用于精确的 blame 匹配）
        hunk_mapping = None
        if git_helper:
            hunk_mapping = self._build_hunk_mapping(git_helper, original_full_sha, split_commits)
        else:
            raise RuntimeError("Blame-based split mapping requires git_helper; fallback mapping is disabled.")

        # 2. 添加 split commits 到 nodes
        self._add_split_nodes(original_sha, split_commits)

        # 3. 使用 blame 计算 sub-commits 之间的依赖
        # 对于每对 (A, B)，检查 B 是否依赖 A（B 的老端行是否 blame 到 A）
        internal_edges = self._compute_inter_split_dependencies(git_helper, split_commits)

        inbound_map: Dict[str, Set[str]] = {}
        outbound_map: Dict[str, Set[str]] = {}

        # 4. 重新映射入边 (X -> original) => (X -> split commits that depend on X)
        # 使用 git blame 精确确定哪些 split commits 依赖 X
        original_in_edges = self.edges_map.get(original_sha, [])
        for edge_info in original_in_edges:
            source_sha = edge_info["source"]
            files_ref = edge_info.get("files_ref", [])

            # 精确模式：使用 git blame 分析哪些 split commits 依赖 source
            target_splits = self._get_splits_by_blame_for_inbound(
                git_helper,
                source_sha,
                split_commits,
                [],  # 不再传入 internal_edges，因为已经通过 blame 计算了
            )

            inbound_map.setdefault(source_sha[:7], set()).update(target_splits)
            for target_split_sha in target_splits:
                self._add_edge(source_sha, target_split_sha, files_ref)

        # 5. 重新映射出边 (original -> Y) => (split commits -> Y)
        # 使用 git blame 精确确定应该从哪些 split commits 出发
        original_out_targets = self.reverse_edges_map.get(original_sha, [])
        for target_sha in original_out_targets:
            edge_files = self._get_files_for_edge(original_sha, target_sha)

            if not hunk_mapping:
                # Fallback: without hunk mapping, connect all split commits to target
                # This is less precise but avoids errors
                source_splits = {sc["resolved_hash"][:7] for sc in split_commits}
            else:
                # 精确模式：使用 git blame 分析 target 依赖 original 的哪些行
                source_splits = self._get_splits_by_blame(
                    git_helper,
                    original_sha,
                    target_sha,
                    edge_files,
                    hunk_mapping,
                    split_commits,
                    [],  # 不再传入 internal_edges
                )

            outbound_map.setdefault(target_sha[:7], set()).update(source_splits)
            for source_split_sha in source_splits:
                self._add_edge(source_split_sha, target_sha, edge_files)

        # 6. 移除原 commit 的节点和边
        self._remove_commit(original_sha)

        return self._build_split_cache_payload(
            metadata,
            internal_edges,
            inbound_map,
            outbound_map,
        )

    def _compute_inter_split_dependencies(
        self,
        git_helper: "_GitBlameHelper",
        split_commits: List[Dict],
    ) -> List[List[str]]:
        """
        使用 blame 计算 sub-commits 之间的依赖关系

        对于每对 (A, B)，检查 B 是否依赖 A：
        - 获取 B 修改的老端行
        - 在 B^ 上 blame 这些行
        - 如果 blame 到 A，则 A → B

        这替代了直接使用 METADATA.json 中的 dependency_edges。
        """
        internal_edges: List[List[str]] = []
        # 构建 sha -> full_sha 映射
        sha_to_full = {}
        for sc in split_commits:
            sha7 = sc["short_hash"][:7]
            sha_to_full[sha7] = sc.get("resolved_hash", sc.get("hash", sc.get("full_hash", sc["short_hash"])))

        # 对于每个 sub-commit B
        for sc_b in split_commits:
            b_sha7 = sc_b["short_hash"][:7]
            b_full_sha = sha_to_full[b_sha7]
            b_files = sc_b.get("files", [])

            # 检查 B 是否依赖其他 sub-commits
            for sc_a in split_commits:
                a_sha7 = sc_a["short_hash"][:7]
                if a_sha7 == b_sha7:
                    continue

                # 检查 B 的老端行是否 blame 到 A
                has_dependency = False

                for file_path in b_files:
                    if has_dependency:
                        break

                    try:
                        old_ranges = git_helper.get_old_line_ranges(b_full_sha, file_path)
                        if not old_ranges:
                            continue

                        for start, end in old_ranges:
                            if has_dependency:
                                break

                            try:
                                blamed_lines = git_helper.blame_file_range(
                                    commit=f"{b_full_sha}^",
                                    path=file_path,
                                    start=start,
                                    end=end,
                                )

                                for _line_num, blamed_sha in blamed_lines:
                                    if blamed_sha.startswith(a_sha7) or a_sha7.startswith(blamed_sha[:7]):
                                        has_dependency = True
                                        break
                            except Exception:
                                continue
                    except Exception:
                        continue

                # 如果 B 依赖 A，添加边 A → B
                if has_dependency:
                    self._add_edge(a_sha7, b_sha7, [])
                    internal_edges.append([a_sha7, b_sha7])

        return internal_edges

    def _build_hunk_mapping(
        self,
        git_helper: "_GitBlameHelper",
        original_sha: str,
        split_commits: List[Dict],
    ) -> Dict[str, List[Tuple[Tuple[int, int], str]]]:
        """
        构建原始 commit 的 hunk 范围到 split commit 的映射

        对于原始 commit 的每个文件，获取其 diff hunks，然后判断每个 hunk
        属于哪个 split commit（按 hunk 顺序分配给 split commits）

        Returns:
            {file_path: [((start, end), split_sha), ...]}
        """
        hunk_mapping: Dict[str, List[Tuple[Tuple[int, int], str]]] = {}

        # 获取原始 commit 的 diff 信息
        original_hunks = git_helper.get_diff_hunks(original_sha)

        sha_to_ref = {
            sc["short_hash"][:7]: sc.get("resolved_hash", sc.get("hash", sc.get("full_hash", sc["short_hash"])))
            for sc in split_commits
        }

        for file_path, hunks in original_hunks.items():
            # 找出哪些 split commits 修改了这个文件
            splits_for_file = []
            for sc in split_commits:
                if file_path in sc.get("files", []):
                    splits_for_file.append(sc["short_hash"][:7])

            if not splits_for_file:
                continue

            if len(splits_for_file) == 1:
                # 只有一个 split commit 修改这个文件
                hunk_mapping[file_path] = [(hunk, splits_for_file[0]) for hunk in hunks]
            else:
                # 多个 split commits 修改同一个文件
                # 需要更精确地确定每个 hunk 属于哪个 split
                # 方法：获取每个 split commit 的 diff，看哪些 hunks 匹配
                split_hunks = {}
                for sc_sha in splits_for_file:
                    sc_ref = sha_to_ref.get(sc_sha, sc_sha)
                    sc_diff = git_helper.get_diff_hunks_for_commit(sc_ref, file_path)
                    split_hunks[sc_sha] = sc_diff

                # 将原始 hunks 分配给 split commits
                file_mapping = []
                total_hunks = len(hunks)
                mid_point = total_hunks // 2

                for idx, hunk in enumerate(hunks):
                    # 简化策略：按 hunk 顺序分配（前半给第一个，后半给后面的）
                    # 更精确的方法是比较 hunk 内容，但这需要更多计算
                    if len(splits_for_file) == 2:
                        assigned_split = splits_for_file[0] if idx < mid_point else splits_for_file[1]
                    else:
                        # 按比例分配
                        split_idx = min(idx * len(splits_for_file) // total_hunks, len(splits_for_file) - 1)
                        assigned_split = splits_for_file[split_idx]

                    file_mapping.append((hunk, assigned_split))

                hunk_mapping[file_path] = file_mapping

        return hunk_mapping

    def _get_splits_by_blame(
        self,
        git_helper: "_GitBlameHelper",
        original_sha: str,
        target_sha: str,
        edge_files: List[str],
        hunk_mapping: Dict[str, List[Tuple[Tuple[int, int], str]]],
        split_commits: List[Dict],
        internal_edges: List[List[str]],
    ) -> Set[str]:
        """
        使用 git blame 精确确定 target 依赖 original 的哪些 split commits

        与 commit_dag_v3 的逻辑保持一致：
        1. 获取 target 修改的老端行范围
        2. 在 target 的 parent 上对这些行做 blame
        3. 找出 blamed 到 original 的行
        4. 确定这些行落在 original 的哪些 hunk 中
        5. 根据 hunk_mapping 映射到对应的 split commit
        """
        # Early return if no split commits
        if not split_commits:
            return set()

        matched_splits: Set[str] = set()

        for file_path in edge_files:
            if file_path not in hunk_mapping:
                continue

            # 获取 target 修改这个文件的老端行范围
            old_ranges = git_helper.get_old_line_ranges(target_sha, file_path)
            if not old_ranges:
                continue

            # 对每个老端行范围做 blame
            for start, end in old_ranges:
                blamed_lines = git_helper.blame_file_range(
                    commit=f"{target_sha}^",  # target 的 parent
                    path=file_path,
                    start=start,
                    end=end,
                )

                # 找出 blamed 到 original 的行号
                for line_num, blamed_sha in blamed_lines:
                    if not self._sha_match(blamed_sha, original_sha):
                        continue

                    # 确定这个行号落在 original 的哪个 hunk 中
                    for (hunk_start, hunk_end), split_sha in hunk_mapping[file_path]:
                        if hunk_start <= line_num <= hunk_end:
                            matched_splits.add(split_sha)
                            break

        if not matched_splits:
            # 没有匹配到，回退到最下游策略
            return {split_commits[-1]["short_hash"][:7]}

        # 不需要过滤最下游，因为 blame 已经给出了精确的依赖
        # 但仍然需要考虑内部边的传递性：如果 A 和 C 都匹配，
        # 而且 A -> B -> C，那么只需要连接到 C（最下游的匹配）
        return self._filter_downstream_only(matched_splits, internal_edges)

    def _get_splits_by_blame_for_inbound(
        self,
        git_helper: "_GitBlameHelper",
        source_sha: str,
        split_commits: List[Dict],
        internal_edges: List[List[str]],
    ) -> Set[str]:
        """
        使用 git blame 精确确定哪些 split commits 依赖 source_sha

        逻辑（与出边对称）：
        1. 对每个 split commit S
        2. 获取 S 修改的老端行范围（S 的 diff 中被删除/修改的行）
        3. 在 S^ 上对这些行做 blame
        4. 如果 blame 到 source_sha → S 依赖 source_sha

        这样可以精确确定哪些 split commits 实际引用了 source 的代码。
        """
        # Early return if no split commits
        if not split_commits:
            return set()

        matched_splits: Set[str] = set()

        for sc in split_commits:
            sc_sha = sc["short_hash"][:7]
            sc_full_sha = sc.get("resolved_hash", sc.get("hash", sc.get("full_hash", sc_sha)))

            # 获取 split commit 修改的所有文件
            for file_path in sc.get("files", []):
                # 获取 split commit 修改这个文件的老端行范围
                old_ranges = git_helper.get_old_line_ranges(sc_full_sha, file_path)
                if not old_ranges:
                    continue

                # 对每个老端行范围做 blame
                for start, end in old_ranges:
                    try:
                        blamed_lines = git_helper.blame_file_range(
                            commit=f"{sc_full_sha}^",  # split commit 的 parent
                            path=file_path,
                            start=start,
                            end=end,
                        )

                        # 检查是否有行 blamed 到 source_sha
                        for _line_num, blamed_sha in blamed_lines:
                            if self._sha_match(blamed_sha, source_sha):
                                matched_splits.add(sc_sha)
                                break
                    except Exception:
                        # blame 失败（可能文件不存在等），跳过
                        continue

                if sc_sha in matched_splits:
                    # 已经找到匹配，不需要继续检查这个 split 的其他文件
                    break

        if not matched_splits:
            # 没有匹配到，回退到第一个 split（最上游）
            return {split_commits[0]["short_hash"][:7]}

        # 过滤最上游：如果 A 和 C 都匹配，而且 A -> B -> C，
        # 那么只需要连接到 A（source 只需要连到最上游的依赖者）
        return self._filter_upstream_only(matched_splits, internal_edges)

    def _get_upstream_splits_for_files(
        self,
        files_ref: List[str],
        file_to_split: Dict[str, str],
        split_commits: List[Dict],
        internal_edges: List[List[str]],
    ) -> Set[str]:
        """
        根据文件列表确定应该连接到哪些 split commits（作为入边的目标）

        关键优化：考虑内部边的传递性，只连接到"最上游"的 split commits
        例如：如果内部边是 A -> B -> C，且文件分布在 A 和 C：
        - 入边只连接到 A（因为从 X->A 可以通过 A->B->C 传递）
        """
        # Early return if no split commits
        if not split_commits:
            return set()

        if not files_ref:
            # 没有文件信息，连接到第一个 split commit（最上游）
            return {split_commits[0]["short_hash"][:7]}

        matched_splits = self._match_files_to_splits(files_ref, file_to_split)

        if not matched_splits:
            return {split_commits[0]["short_hash"][:7]}

        # 只保留最上游的 commits（不能被其他 matched splits 到达）
        return self._filter_upstream_only(matched_splits, internal_edges)

    def _get_downstream_splits_for_files(
        self,
        files_ref: List[str],
        file_to_split: Dict[str, str],
        split_commits: List[Dict],
        internal_edges: List[List[str]],
    ) -> Set[str]:
        """
        根据文件列表确定应该从哪些 split commits 出发（作为出边的源）

        关键优化：考虑内部边的传递性，只从"最下游"的 split commits 出发
        例如：如果内部边是 A -> B -> C，且文件分布在 A 和 C：
        - 出边只从 C 出发（因为 A 的内容可以通过 A->B->C 传递到 C）
        """
        # Early return if no split commits
        if not split_commits:
            return set()

        if not files_ref:
            # 没有文件信息，从最后一个 split commit 出发
            return {split_commits[-1]["short_hash"][:7]}

        matched_splits = self._match_files_to_splits(files_ref, file_to_split)

        if not matched_splits:
            return {split_commits[-1]["short_hash"][:7]}

        # 只保留最下游的 commits（不能到达其他 matched splits）
        return self._filter_downstream_only(matched_splits, internal_edges)

    def _match_files_to_splits(self, files_ref: List[str], file_to_split: Dict[str, str]) -> Set[str]:
        """根据文件列表找出对应的 split commits"""
        matched = set()
        for f in files_ref:
            if f in file_to_split:
                matched.add(file_to_split[f])
            else:
                basename = f.split("/")[-1]
                if basename in file_to_split:
                    matched.add(file_to_split[basename])
        return matched

    def _filter_upstream_only(self, matched_splits: Set[str], internal_edges: List[List[str]]) -> Set[str]:
        """过滤出最上游的 splits（不能被其他 matched splits 到达）"""
        internal_graph = self._build_internal_graph(internal_edges)

        result = set()
        for split_sha in matched_splits:
            can_be_reached = False
            for other_sha in matched_splits:
                if other_sha != split_sha and self._can_reach(other_sha, split_sha, internal_graph):
                    can_be_reached = True
                    break
            if not can_be_reached:
                result.add(split_sha)

        return result if result else matched_splits

    def _filter_downstream_only(self, matched_splits: Set[str], internal_edges: List[List[str]]) -> Set[str]:
        """过滤出最下游的 splits（不能到达其他 matched splits）"""
        internal_graph = self._build_internal_graph(internal_edges)

        result = set()
        for split_sha in matched_splits:
            can_reach_other = False
            for other_sha in matched_splits:
                if other_sha != split_sha and self._can_reach(split_sha, other_sha, internal_graph):
                    can_reach_other = True
                    break
            if not can_reach_other:
                result.add(split_sha)

        return result if result else matched_splits

    def _build_internal_graph(self, internal_edges: List[List[str]]) -> Dict[str, List[str]]:
        """构建内部边的邻接表"""
        graph = {}
        for edge in internal_edges:
            src = edge[0][:7]
            dst = edge[1][:7]
            if src not in graph:
                graph[src] = []
            graph[src].append(dst)
        return graph

    def _can_reach(self, start: str, end: str, graph: Dict[str, List[str]]) -> bool:
        """检查在内部图中 start 是否能到达 end"""
        if start == end:
            return True
        visited = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            for neighbor in graph.get(current, []):
                if neighbor == end:
                    return True
                stack.append(neighbor)
        return False

    def _add_edge(self, source_sha: str, target_sha: str, files_ref: List[str]) -> None:
        """添加一条边到 DAG"""
        source_sha = source_sha[:7]
        target_sha = target_sha[:7]

        # 添加到 edges_map
        if target_sha not in self.edges_map:
            self.edges_map[target_sha] = []
        if not any(e["source"][:7] == source_sha for e in self.edges_map[target_sha]):
            self.edges_map[target_sha].append({"source": source_sha, "files_ref": files_ref})

        # 添加到 reverse_edges_map
        if source_sha not in self.reverse_edges_map:
            self.reverse_edges_map[source_sha] = []
        if target_sha not in self.reverse_edges_map[source_sha]:
            self.reverse_edges_map[source_sha].append(target_sha)

    def _get_files_for_edge(self, source_sha: str, target_sha: str) -> List[str]:
        """获取某条边的 files_ref"""
        edges = self.edges_map.get(target_sha, [])
        for edge in edges:
            if edge["source"][:7] == source_sha[:7]:
                return edge.get("files_ref", [])
        return []

    def _remove_commit(self, sha: str) -> None:
        """从 DAG 中移除一个 commit 及其所有边"""
        sha = sha[:7]

        # 从 nodes 移除
        if sha in self.nodes:
            del self.nodes[sha]

        # 从 edges_map 移除（作为 target）
        if sha in self.edges_map:
            del self.edges_map[sha]

        # 从 reverse_edges_map 移除（作为 source）
        if sha in self.reverse_edges_map:
            del self.reverse_edges_map[sha]

        # 清理其他 edges_map 中对此 commit 的引用（作为 source）
        for target in list(self.edges_map.keys()):
            self.edges_map[target] = [e for e in self.edges_map[target] if e["source"][:7] != sha]
            if not self.edges_map[target]:
                del self.edges_map[target]

        # 清理其他 reverse_edges_map 中对此 commit 的引用（作为 target）
        for source in list(self.reverse_edges_map.keys()):
            self.reverse_edges_map[source] = [t for t in self.reverse_edges_map[source] if t[:7] != sha]
            if not self.reverse_edges_map[source]:
                del self.reverse_edges_map[source]


# =============================================================================
# Git Blame Helper (与 commit_dag_v3 保持一致的 blame 逻辑)
# =============================================================================


class _GitBlameHelper:
    """
    Git Blame 辅助类

    提供与 commit_dag_v3.py 一致的 git blame 功能，用于精确分析
    commit 之间的行级别依赖关系。
    """

    def __init__(
        self,
        repo_path: Path,
        context_lines: int = 5,
        follow_moves: bool = True,
        ignore_whitespace: bool = True,
        follow_depth: int = 2,
    ):
        """
        初始化 Git Blame Helper

        Args:
            repo_path: Git 仓库路径
            follow_moves: 是否追踪文件移动/重命名（-M 标志）
            ignore_whitespace: 是否忽略空白变化（-w 标志）
            follow_depth: 复制检测深度（1-3）
                         1: same commit modified files
                         2: same commit all files (default, good balance)
                         3: cross-commit history (most accurate but slowest)
        """
        self.repo_path = repo_path
        self.context_lines = context_lines
        self.follow_moves = follow_moves
        self.ignore_whitespace = ignore_whitespace
        self.follow_depth = follow_depth

    def commit_exists(self, sha: str) -> bool:
        """Check if a commit exists in the repository."""
        return self.resolve_commit_ref(sha) is not None

    def resolve_commit_ref(self, ref: str) -> Optional[str]:
        """Resolve a commit reference to a full hash if it exists."""
        if not ref:
            return None
        cmd = ["git", "-C", str(self.repo_path), "rev-parse", "--verify", f"{ref}^{{commit}}"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            return None
        resolved = proc.stdout.strip().splitlines()
        if not resolved:
            return None
        return resolved[0].strip().lower()

    def get_changed_files(self, commit: str) -> List[str]:
        """Return the list of files changed by a commit."""
        return self._get_changed_files(commit)

    def _run_git(self, args: List[str], check: bool = False) -> str:
        """执行 git 命令并返回输出"""
        cmd = ["git", "-C", str(self.repo_path)] + args
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def blame_file_range(
        self,
        commit: str,
        path: str,
        start: int,
        end: int,
    ) -> List[Tuple[int, str]]:
        """
        对文件的指定行范围进行 blame

        与 commit_dag_v3.py 的 blame_file_range 保持一致的参数和逻辑。

        Args:
            commit: 要 blame 的 commit（通常是目标 commit 的 parent）
            path: 文件路径
            start: 起始行号（1-indexed）
            end: 结束行号（1-indexed）

        Returns:
            [(line_num, blamed_sha), ...] 每行对应的 commit SHA
        """
        if start <= 0 or end < start:
            return []

        args = ["blame"]
        if self.follow_moves:
            args.append("-M")
            for _ in range(self.follow_depth):
                args.append("-C")
        if self.ignore_whitespace:
            args.append("-w")
        args.extend([f"-L{start},{end}", commit, "--", path])

        out = self._run_git(args, check=False)
        results: List[Tuple[int, str]] = []

        current_line = start
        for line in out.splitlines():
            if not line:
                continue
            tok = line.split()[0]
            if tok.startswith("^"):
                tok = tok[1:]
            results.append((current_line, tok))
            current_line += 1

        return results

    def get_old_line_ranges(
        self,
        commit: str,
        file_path: str,
    ) -> List[Tuple[int, int]]:
        """
        获取 commit 在指定文件上的老端行范围

        解析 git diff 输出（带 context_lines），提取所有 hunk 的老端行范围。

        Args:
            commit: Commit SHA
            file_path: 文件路径

        Returns:
            [(start, end), ...] 老端行范围列表
        """
        diff_out = self._run_git(["show", commit, "--", file_path, f"-U{self.context_lines}", "--no-color"])

        old_ranges = []
        for line in diff_out.splitlines():
            if line.startswith("@@"):
                # 解析 @@ -old_start,old_count +new_start,new_count @@
                parts = line.split()
                if len(parts) >= 2:
                    old_part = parts[1]  # -old_start,old_count
                    if old_part.startswith("-"):
                        old_part = old_part[1:]
                        if "," in old_part:
                            start_str, count_str = old_part.split(",")
                            start, count = int(start_str), int(count_str)
                        else:
                            start, count = int(old_part), 1

                        if count > 0:
                            old_ranges.append((start, start + count - 1))

        return old_ranges

    def get_diff_hunks(self, commit: str) -> Dict[str, List[Tuple[int, int]]]:
        """
        获取 commit 的所有 diff hunk（新端行范围，带 context_lines）

        Returns:
            {file_path: [(start, end), ...]}
        """
        diff_out = self._run_git(["show", commit, f"-U{self.context_lines}", "--no-color", "--name-only"])

        # 先获取修改的文件列表
        files = []
        in_diff = False
        for line in diff_out.splitlines():
            if line.startswith("diff --git"):
                in_diff = True
            elif in_diff and not line.startswith(("---", "+++", "@@", "+", "-", " ", "\\")):
                if line.strip():
                    files.append(line.strip())

        result: Dict[str, List[Tuple[int, int]]] = {}

        # 获取每个文件的完整 diff
        for file_path in self._get_changed_files(commit):
            hunks = self._get_file_hunks(commit, file_path)
            if hunks:
                result[file_path] = hunks

        return result

    def get_diff_hunks_for_commit(
        self,
        commit: str,
        file_path: str,
    ) -> List[Tuple[int, int]]:
        """
        获取指定 commit 在指定文件上的 hunk 范围（新端）

        Returns:
            [(start, end), ...]
        """
        return self._get_file_hunks(commit, file_path)

    def _get_changed_files(self, commit: str) -> List[str]:
        """获取 commit 修改的文件列表"""
        out = self._run_git(["show", "--name-only", "--format=", commit])
        return [f.strip() for f in out.splitlines() if f.strip()]

    def _get_file_hunks(
        self,
        commit: str,
        file_path: str,
    ) -> List[Tuple[int, int]]:
        """
        获取 commit 在指定文件上的新端行范围

        Returns:
            [(start, end), ...] 新端行范围列表
        """
        diff_out = self._run_git(["show", commit, "--", file_path, f"-U{self.context_lines}", "--no-color"])

        new_ranges = []
        for line in diff_out.splitlines():
            if line.startswith("@@"):
                parts = line.split()
                if len(parts) >= 3:
                    new_part = parts[2]  # +new_start,new_count
                    if new_part.startswith("+"):
                        new_part = new_part[1:]
                        if "," in new_part:
                            start_str, count_str = new_part.split(",")
                            start, count = int(start_str), int(count_str)
                        else:
                            start, count = int(new_part), 1

                        if count > 0:
                            new_ranges.append((start, start + count - 1))

        return new_ranges
