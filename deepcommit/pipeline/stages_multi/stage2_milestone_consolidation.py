"""
Stage 2: Milestone Consolidation

Input: milestone_seeds.jsonl (from Stage 1), shared inputs
Output: milestone.csv, dependency_proposals.csv, seed_groups.json
"""

from __future__ import annotations

import csv
import json
import os
from collections import defaultdict, deque
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, cast

from ..base_stage import BaseStage
from ..models import MilestoneModel

if TYPE_CHECKING:
    pass


class Stage2MilestoneConsolidation(BaseStage):
    """Stage 2: Milestone Consolidation - groups seeds into milestones."""

    def _preprocess_subgraphs(self) -> None:
        """Extract subgraphs for each seed (based on extract_seed_subgraph.py).

        Reads milestone_seeds.jsonl and commit_dag.json, generates subgraphs.
        """
        self.logger.info("Preprocessing: Extracting seed subgraphs...")

        # Load milestone seeds
        milestone_seeds_file = Path(self.ctx.get_input("milestone_seeds"))
        if not milestone_seeds_file.exists():
            raise FileNotFoundError(f"milestone_seeds.jsonl not found: {milestone_seeds_file}")

        seeds = []
        with open(milestone_seeds_file, "r", encoding="utf-8") as f:
            for line in f:
                seed = json.loads(line.strip())
                seeds.append(seed)

        self.logger.info(f"  Loaded {len(seeds)} seed commits")

        # Load commit DAG
        dag_file = Path(self.ctx.get_input("commit_dag"))
        if not dag_file.exists():
            raise FileNotFoundError(f"commit_dag.json not found: {dag_file}")

        with open(dag_file, "r", encoding="utf-8") as f:
            dag_data = json.load(f)

        nodes = dag_data.get("nodes", {})
        edges = dag_data.get("edges_dag", [])

        # Build adjacency lists
        adjacency: dict[str, list[str]] = {}  # source -> [targets]
        reverse_adjacency: dict[str, list[str]] = {}  # target -> [sources]

        for edge in edges:
            source = edge["source"]
            target = edge["target"]

            if source not in adjacency:
                adjacency[source] = []
            adjacency[source].append(target)

            if target not in reverse_adjacency:
                reverse_adjacency[target] = []
            reverse_adjacency[target].append(source)

        # Create subgraphs directory
        subgraphs_dir = Path(self.ctx.get_input("subgraphs"))
        subgraphs_dir.mkdir(parents=True, exist_ok=True)

        # Extract subgraph for each seed
        stats = []
        for seed in seeds:
            seed_sha = seed["sha"]

            # Find full node ID (may be longer than 8 chars)
            node_id = None
            for nid in nodes:
                node_sha = nodes[nid].get("sha", "")
                if nid.startswith(seed_sha) or node_sha.startswith(seed_sha):
                    node_id = nid
                    break

            if not node_id:
                self.logger.warning(f"  Seed {seed_sha} not found in DAG, skipping")
                continue

            # BFS for ancestors
            ancestors = set()
            queue = deque([node_id])
            visited = set([node_id])

            while queue:
                current = queue.popleft()
                for source in reverse_adjacency.get(current, []):
                    if source not in visited:
                        visited.add(source)
                        ancestors.add(source)
                        queue.append(source)

            # BFS for descendants
            descendants = set()
            queue = deque([node_id])
            visited = set([node_id])

            while queue:
                current = queue.popleft()
                for target in adjacency.get(current, []):
                    if target not in visited:
                        visited.add(target)
                        descendants.add(target)
                        queue.append(target)

            # All nodes in subgraph
            all_nodes = ancestors | {node_id} | descendants

            # Extract subgraph edges
            subgraph_edges = [e for e in edges if e["source"] in all_nodes and e["target"] in all_nodes]

            # Build subgraph JSON
            subgraph = {
                "meta": {
                    "seed_sha": node_id,
                    "seed_subject": nodes[node_id]["subject"],
                    "total_nodes": len(all_nodes),
                    "total_edges": len(subgraph_edges),
                },
                "nodes": {nid: nodes[nid] for nid in all_nodes},
                "edges_dag": subgraph_edges,
            }

            # Save subgraph
            output_file = subgraphs_dir / f"subgraph_{seed_sha[:8]}.json"
            self.logger.save_json(
                data=subgraph,
                full_path=output_file,
                archive=False,  # These are intermediate preprocessing files
            )

            stats.append(
                {
                    "seed_sha": seed_sha,
                    "nodes": len(all_nodes),
                    "edges": len(subgraph_edges),
                }
            )

        self.logger.info(f"  ✓ Extracted {len(stats)} subgraphs to {subgraphs_dir}")

    def _preprocess_seed_group_analysis(self) -> None:
        """Analyze seed overlap and create groups (based on subgraph_seed_overlap_groups.py)."""
        self.logger.info("Preprocessing: Analyzing seed groups...")

        # Get subgraphs directory
        subgraphs_dir = Path(self.ctx.get_input("subgraphs"))

        if not subgraphs_dir.exists():
            raise FileNotFoundError(f"Subgraphs directory not found: {subgraphs_dir}")

        # Load all subgraphs
        subgraph_files = sorted(subgraphs_dir.glob("subgraph_*.json"))
        if not subgraph_files:
            self.logger.warning("  No subgraph files found")
            return

        self.logger.info(f"  Found {len(subgraph_files)} subgraph files")

        seed_info = {}
        commit_to_seeds = defaultdict(set)

        for file_path in subgraph_files:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            seed_sha = data["meta"]["seed_sha"]
            seed_info[seed_sha] = {
                "subject": data["meta"]["seed_subject"],
                "total_nodes": data["meta"]["total_nodes"],
                "total_edges": data["meta"]["total_edges"],
                "file": file_path.name,
            }

            # Record commits in this seed's subgraph
            for commit in data["nodes"].keys():
                commit_to_seeds[commit].add(seed_sha)

        # Group seeds using Union-Find
        parent = {seed: seed for seed in seed_info.keys()}

        def find(x):
            if parent[x] != x:
                parent[x] = find(parent[x])
            return parent[x]

        def union(x, y):
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        # Merge seeds that share commits
        for _commit, seeds in commit_to_seeds.items():
            if len(seeds) > 1:
                seeds_list = list(seeds)
                for i in range(len(seeds_list) - 1):
                    union(seeds_list[i], seeds_list[i + 1])

        # Collect groups
        groups_dict = defaultdict(set)
        for seed in seed_info.keys():
            groups_dict[find(seed)].add(seed)

        groups = list(groups_dict.values())

        # Build group analysis
        group_analysis = []
        for i, group in enumerate(groups, 1):
            group_info = {
                "overlap_group_id": i,
                "seeds_count": len(group),
                "seeds": [{"sha": seed, "subject": seed_info[seed]["subject"]} for seed in sorted(group)],
            }
            group_analysis.append(group_info)

        # Sort by group size
        group_analysis.sort(key=lambda x: cast(int, x["seeds_count"]), reverse=True)

        # Build report
        total_seeds = len(seed_info)
        isolated_seeds = sum(1 for g in groups if len(g) == 1)

        report = {
            "summary": {
                "total_seeds": total_seeds,
                "total_overlap_groups": len(groups),
                "isolated_seeds": isolated_seeds,
                "connected_overlap_groups": len(groups) - isolated_seeds,
                "largest_overlap_group_size": max(len(g) for g in groups) if groups else 0,
            },
            "subgraph_overlap_groups": group_analysis,
        }

        # Save analysis to input directory
        analysis_file = Path(self.ctx.get_input("seed_group_analysis"))
        self.logger.save_json(
            data=report,
            full_path=analysis_file,
            archive=False,  # This is an intermediate preprocessing file
        )

        self.logger.info(f"  ✓ Seed group analysis saved to {analysis_file}")
        self.logger.info(f"    Total seeds: {total_seeds}, Groups: {len(groups)}, Isolated: {isolated_seeds}")

    def _setup_input_data(self) -> None:
        """Setup input data by preprocessing new variables.

        Note: Links from previous stage are now handled automatically by main_multi_stage.py.
        This function handles preprocessing new input variables:
        - subgraphs: Extract subgraphs from commit DAG
        - seed_group_analysis: Analyze seed overlap and groups
        """
        self.logger.info("Setting up input data...")

        # Calculate new input variables
        new_input_vars = self.ctx.get_new_input_vars()

        if not new_input_vars:
            self.logger.info("  No new input variables to preprocess")
            return

        self.logger.info(f"  Detected {len(new_input_vars)} new input variable(s): {new_input_vars}")

        # Extract subgraphs if needed
        if "subgraphs" in new_input_vars:
            self._preprocess_subgraphs()

        # Analyze seed groups if needed
        if "seed_group_analysis" in new_input_vars:
            self._preprocess_seed_group_analysis()

    def _validate_outputs(self) -> Optional[str]:
        """Validate Stage 2 output file: milestone.csv.

        Returns:
            Error message if validation fails, None if successful
        """
        self.logger.info("Validating output files...")

        milestone_csv_path = Path(self.ctx.get_output("milestone_csv"))
        error_messages: List[str] = []

        # Validate milestone.csv
        if not os.path.isfile(milestone_csv_path):
            return f"Missing milestone.csv at {milestone_csv_path}"

        try:
            with open(milestone_csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                seen_ids = set()
                milestone_count = 0

                for idx, row in enumerate(reader, start=2):  # header is line 1
                    # Build payload strictly from model fields
                    payload = {k: (row.get(k) or "").strip() for k in MilestoneModel.model_fields.keys()}
                    try:
                        m = MilestoneModel.model_validate(payload)
                    except Exception as e:
                        error_messages.append(f"milestone.csv line {idx} invalid: {e}")
                        continue

                    # Check for duplicate IDs
                    if m.id in seen_ids:
                        error_messages.append(f"Duplicate milestone id detected: {m.id}")
                        continue

                    seen_ids.add(m.id)
                    milestone_count += 1

                if milestone_count == 0:
                    error_messages.append("milestone.csv contains no valid data rows")
        except Exception as e:
            return f"Failed to read milestone.csv: {e}"

        if error_messages:
            return "; ".join(error_messages)

        self.logger.info(f"  ✓ Validated {milestone_count} milestones")

        # === Milestone Partitioning Validation (inferred DAG cycle check) ===
        self.logger.info("  Validating milestone partitioning (inferred DAG cycle check)...")

        try:
            from ...tools import validate_milestone_partitioning

            validation_result = validate_milestone_partitioning(
                milestone_csv_path=str(milestone_csv_path), commit_dag_path=str(Path(self.ctx.get_input("commit_dag")))
            )

            if not validation_result["is_valid_partitioning"]:
                # Return recommendation directly (already contains cycles count and problematic milestones)
                error_msg = validation_result["recommendation"]
                return error_msg

            self.logger.info(
                f"  ✓ Partitioning valid: {validation_result['inferred_dependencies_count']} dependencies, no cycles"
            )

        except Exception as e:
            # Tool failure is treated as validation failure (strict mode)
            error_msg = f"Partitioning validation tool failed: {e}"
            self.logger.error(error_msg)
            return error_msg

        return None

    def _archive_outputs(self) -> List[str]:
        """Archive agent output files to result/stage_id/ directory.

        Returns:
            List of archived filenames
        """
        self.logger.info("Archiving output files...")
        saved_files = []

        # Archive Stage 2 outputs
        output_vars = [
            ("dependency_proposals_csv", None),
            ("seed_groups", None),
            ("milestone_csv", "milestones.csv"),  # Copy to global as milestones.csv
        ]

        for var_name, global_filename in output_vars:
            output_path = Path(self.ctx.get_output(var_name))

            if output_path.exists():
                self.logger.archive_file(output_path, global_filename=global_filename)
                saved_files.append(output_path.name)
            else:
                self.logger.warning(f"Expected output not found: {var_name} ({output_path})")

        return saved_files
