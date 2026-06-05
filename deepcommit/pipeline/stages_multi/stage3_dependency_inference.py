"""
Stage 3: Dependency Inference

Input: milestone.csv (from Stage 2), shared inputs
Output: dependencies.csv
"""

from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from ..base_stage import BaseStage
from ..models import DependencyEdgeModel

if TYPE_CHECKING:
    pass


def _get_commit_time(commit: dict) -> Optional[str]:
    """Extract time from commit dict, supporting both old (top-level) and new (nested) formats."""
    # Old format: top-level "time" field
    if "time" in commit:
        return commit["time"]
    # New format: nested in author or committer dict
    author = commit.get("author")
    if isinstance(author, dict) and "time" in author:
        return author["time"]
    committer = commit.get("committer")
    if isinstance(committer, dict) and "time" in committer:
        return committer["time"]
    return None


class Stage3DependencyInference(BaseStage):
    """Stage 3: Dependency Inference - infers milestone dependencies."""

    def _get_dependencies_from_dag(self) -> Tuple[List[List[str]], List[List[str]]]:
        """Get inferred and optional dependencies from commit DAG.

        Returns:
            (inferred_list, optional_list) tuple
        """
        self.logger.info("Computing dependencies from commit DAG...")

        from ...tools.conflict_analysis.milestone_dependency_analyzer import (
            MilestoneDependencyAnalyzer,
        )

        # Get paths from ctx
        commit_dag_path = self.ctx.get_input("commit_dag")
        milestones_path = self.ctx.get_input("milestone_csv")

        # Create analyzer
        analyzer = MilestoneDependencyAnalyzer(
            commit_dag_path=commit_dag_path,
            milestone_csv_path=milestones_path,
        )

        # Get inferred and optional dependencies (single call to avoid duplicate computation)
        inferred_list, optional_list = analyzer.get_inferred_and_optional_dependencies(filter_transitive=True)

        self.logger.info(f"  ✓ Inferred: {len(inferred_list)} edges, Optional: {len(optional_list)} edges")

        return inferred_list, optional_list

    def _save_inferred_dependencies(self, inferred_list: List[List[str]]) -> None:
        """Save inferred_dependencies.json.

        Args:
            inferred_list: List of inferred dependency edges
        """
        self.logger.info("Saving inferred_dependencies.json...")

        # Save to input directory
        inferred_deps_path = self.ctx.get_input("inferred_dependencies")
        self.logger.save_json(
            data=inferred_list,
            full_path=Path(inferred_deps_path),
            archive=False,  # This is an intermediate preprocessing file
        )

        self.logger.info(f"  ✓ Saved inferred_dependencies.json with {len(inferred_list)} edges")

    @staticmethod
    def _calculate_ranking_score(
        prerequisite_id: str,
        dependant_id: str,
        milestones: Dict,
        milestone_commits: Dict[str, List[str]],
        commits: Dict,
        commit_symbols: Dict[str, List[str]],
        sha_prefix_map: Dict[str, str],
    ) -> Tuple[float, Dict]:
        """Calculate ranking score for a candidate edge.

        Simplified version without semantic scoring, focusing on core signals.

        Returns:
            (composite_score, signals_dict)
        """
        source_commits = milestone_commits.get(prerequisite_id, [])
        target_commits = milestone_commits.get(dependant_id, [])

        if not source_commits or not target_commits:
            return 0.0, {}

        signals = {}

        # 1. File Overlap (30%)
        source_files = set(milestones[prerequisite_id].get("touched_src_files", "").split(";"))
        target_files = set(milestones[dependant_id].get("touched_src_files", "").split(";"))
        source_files.discard("")
        target_files.discard("")

        file_overlap = len(source_files & target_files)
        file_overlap_ratio = file_overlap / max(len(target_files), 1)
        signals["file_overlap"] = file_overlap
        signals["file_overlap_ratio"] = file_overlap_ratio

        # 2. Symbol Overlap (20%)
        source_symbols = set()
        target_symbols = set()

        for short_sha in source_commits:
            full_sha = sha_prefix_map.get(short_sha)
            if full_sha and full_sha in commit_symbols:
                source_symbols.update(commit_symbols[full_sha])

        for short_sha in target_commits:
            full_sha = sha_prefix_map.get(short_sha)
            if full_sha and full_sha in commit_symbols:
                target_symbols.update(commit_symbols[full_sha])

        symbol_overlap = len(source_symbols & target_symbols)
        symbol_overlap_ratio = symbol_overlap / max(len(target_symbols), 1)
        signals["symbol_overlap"] = symbol_overlap
        signals["symbol_overlap_ratio"] = symbol_overlap_ratio

        # 3. Temporal Order (40%)
        source_times = []
        target_times = []

        for short_sha in source_commits:
            full_sha = sha_prefix_map.get(short_sha)
            if full_sha and full_sha in commits:
                t = _get_commit_time(commits[full_sha])
                if t:
                    source_times.append(t)

        for short_sha in target_commits:
            full_sha = sha_prefix_map.get(short_sha)
            if full_sha and full_sha in commits:
                t = _get_commit_time(commits[full_sha])
                if t:
                    target_times.append(t)

        temporal_score = 0
        if source_times and target_times:
            source_times_sorted = sorted([datetime.fromisoformat(t.replace("Z", "+00:00")) for t in source_times])
            target_times_sorted = sorted([datetime.fromisoformat(t.replace("Z", "+00:00")) for t in target_times])

            # Use median source time
            source_median = source_times_sorted[len(source_times_sorted) // 2]

            # Calculate proportion of target commits after source median
            target_after = sum(1 for t in target_times_sorted if t > source_median)
            proportion_after = target_after / len(target_times_sorted)

            # Score based on proportion
            if proportion_after >= 0.8:
                temporal_score = 80 + (proportion_after - 0.8) * 100
            elif proportion_after >= 0.6:
                temporal_score = 40 + (proportion_after - 0.6) * 200
            elif proportion_after >= 0.4:
                temporal_score = 0 + (proportion_after - 0.4) * 200
            else:
                temporal_score = -50

            temporal_score = max(-100, min(100, temporal_score))

        signals["temporal_score"] = temporal_score

        # 4. Author & Keyword Overlap (10% total)
        source_authors = set()
        target_authors = set()
        source_keywords = set()
        target_keywords = set()

        for short_sha in source_commits:
            full_sha = sha_prefix_map.get(short_sha)
            if full_sha and full_sha in commits:
                source_authors.add(commits[full_sha].get("author", ""))
                msg = commits[full_sha].get("message", "").lower()
                words = [w.strip("(),:.!?#") for w in msg.split()]
                source_keywords.update(w for w in words if len(w) > 3)

        for short_sha in target_commits:
            full_sha = sha_prefix_map.get(short_sha)
            if full_sha and full_sha in commits:
                target_authors.add(commits[full_sha].get("author", ""))
                msg = commits[full_sha].get("message", "").lower()
                words = [w.strip("(),:.!?#") for w in msg.split()]
                target_keywords.update(w for w in words if len(w) > 3)

        author_overlap = len(source_authors & target_authors)
        keyword_overlap = len(source_keywords & target_keywords)

        signals["author_overlap"] = author_overlap
        signals["keyword_overlap"] = keyword_overlap

        # Calculate composite score (weights: file=30%, temporal=40%, symbol=20%, author=7%, keyword=3%)
        score = 0.0
        score += (file_overlap_ratio * 100) * 0.30  # File
        score += ((temporal_score + 100) / 2) * 0.40  # Temporal (normalize -100~100 to 0~100)
        score += (symbol_overlap_ratio * 100) * 0.20  # Symbol
        score += min(author_overlap * 10, 100) * 0.07  # Author
        score += min(keyword_overlap * 5, 100) * 0.03  # Keyword

        return score, signals

    def _save_pre_selected_candidates(self, optional_list: List[List[str]]) -> None:
        """Generate and save pre_selected_candidates.json with ranked optional dependencies.

        Args:
            optional_list: List of optional dependency edges
        """
        self.logger.info("Generating pre_selected_candidates.json...")

        # Get paths from ctx
        milestones_path = self.ctx.get_input("milestone_csv")
        main_commits_path = self.ctx.get_input("main_commits")

        # Load symbols if available
        commit_symbols = {}
        try:
            symbol_path = self.ctx.get_input("symbol")
            symbols_file = Path(symbol_path)

            if symbols_file.exists():
                with open(symbols_file, "r") as f:
                    for line in f:
                        data = json.loads(line)
                        commit_sha = data["commit"]
                        all_symbols = []
                        for file_info in data["files"]:
                            all_symbols.extend(file_info.get("symbols", []))
                        commit_symbols[commit_sha] = all_symbols
                self.logger.info(f"  Loaded {len(commit_symbols)} commits with symbols")
            else:
                self.logger.info("  Symbol file not found, ranking without symbol overlap")
        except Exception as e:
            self.logger.warning(f"  Could not load symbols: {e}")

        # Load milestone data
        milestones = {}
        milestone_commits = defaultdict(list)

        with open(milestones_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                mid = row["id"]
                milestones[mid] = row
                commits = [c.strip() for c in row["commits"].split(";") if c.strip()]
                milestone_commits[mid] = commits

        # Load commit data
        commits = {}
        with open(main_commits_path) as f:
            for line in f:
                if line.strip():
                    commit = json.loads(line)
                    commits[commit["sha"]] = commit

        # Build SHA prefix map (7-char -> full)
        sha_prefix_map = {}
        for full_sha in commits.keys():
            prefix7 = full_sha[:7]
            sha_prefix_map[prefix7] = full_sha

        # Rank optional dependencies
        self.logger.info(f"  Ranking {len(optional_list)} optional dependencies...")
        ranked_edges = []

        for i, (source, target) in enumerate(optional_list):
            if (i + 1) % 50 == 0:
                self.logger.info(f"    Processed {i + 1}/{len(optional_list)} edges...")

            score, signals = self._calculate_ranking_score(
                source, target, milestones, milestone_commits, commits, commit_symbols, sha_prefix_map
            )

            # Get commit SHAs
            source_commits_7d = milestone_commits.get(source, [])
            target_commits_7d = milestone_commits.get(target, [])

            source_commits_full = [sha_prefix_map.get(s, s) for s in source_commits_7d]
            target_commits_full = [sha_prefix_map.get(t, t) for t in target_commits_7d]

            ranked_edges.append(
                {
                    "source_milestone": source,
                    "target_milestone": target,
                    "score": score,
                    "source_commits": source_commits_full,
                    "target_commits": target_commits_full,
                    "signals": signals,
                }
            )

        # Sort by score (descending)
        ranked_edges.sort(key=lambda x: x["score"], reverse=True)

        # Add rank
        for i, edge in enumerate(ranked_edges, 1):
            edge["rank"] = i

        # Save to input directory
        pre_selected_path = self.ctx.get_input("pre_selected_candidates")

        result = {
            "metadata": {
                "algorithm": "Simplified ranking (no semantic scoring)",
                "weights": {
                    "file": 30.0,
                    "temporal": 40.0,
                    "symbol": 20.0,
                    "author": 7.0,
                    "keyword": 3.0,
                },
                "input_edges": len(optional_list),
                "ranked_edges": len(ranked_edges),
                "source": "optional_dependencies (filtered, transitive removed)",
            },
            "candidates": ranked_edges,
        }

        self.logger.save_json(
            data=result,
            full_path=Path(pre_selected_path),
            archive=False,  # This is an intermediate preprocessing file
        )

        self.logger.info(f"  ✓ Generated pre_selected_candidates.json with {len(ranked_edges)} candidates")

    def _setup_input_data(self) -> None:
        """Setup input data by generating new variables or linking from preprocessed.

        Note: Links from previous stage are now handled automatically by main_multi_stage.py.
        This function handles generating new input variables:
        - inferred_dependencies: Generate from commit DAG
        - pre_selected_candidates: Generate ranked optional dependencies
        """
        self.logger.info("Setting up input data...")

        # Calculate new input variables
        new_input_vars = self.ctx.get_new_input_vars()

        if not new_input_vars:
            self.logger.info("  No new input variables to generate")
            return

        self.logger.info(f"  Detected {len(new_input_vars)} new input variable(s): {new_input_vars}")

        # Check which variables need to be generated
        need_inferred = "inferred_dependencies" in new_input_vars
        need_candidates = "pre_selected_candidates" in new_input_vars

        # Optimize: compute dependencies once if needed by either variable
        if need_inferred or need_candidates:
            inferred_list, optional_list = self._get_dependencies_from_dag()

            # Save inferred dependencies if needed
            if need_inferred:
                self._save_inferred_dependencies(inferred_list)

            # Generate and save pre-selected candidates if needed
            if need_candidates:
                self._save_pre_selected_candidates(optional_list)

        self.logger.info("  (Links from previous stage handled by pipeline controller)")

    def _validate_outputs(self) -> Optional[str]:
        """Validate Stage 3 output file: dependencies.csv.

        Note: milestones.csv is an INPUT (from Stage 2), not an output of Stage 3.

        Returns:
            Error message if validation fails, None if successful
        """
        self.logger.info("Validating output files...")

        deps_csv_path = Path(self.ctx.get_output("dependencies_csv"))
        error_messages: List[str] = []

        # Load milestone IDs from input for referential integrity check
        milestone_csv_path = Path(self.ctx.get_input("milestone_csv"))
        milestone_ids = set()

        try:
            with open(milestone_csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("id"):
                        milestone_ids.add(row["id"].strip())
        except Exception as e:
            return f"Failed to read milestone IDs from input: {e}"

        # Validate dependencies.csv
        if not os.path.isfile(deps_csv_path):
            return f"Missing dependencies.csv at {deps_csv_path}"

        try:
            with open(deps_csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                dep_count = 0
                for idx, row in enumerate(reader, start=2):
                    payload = {k: (row.get(k) or "").strip() for k in DependencyEdgeModel.model_fields.keys()}
                    try:
                        eobj = DependencyEdgeModel.model_validate(payload)
                    except Exception as e:
                        error_messages.append(f"dependencies.csv line {idx} invalid: {e}")
                        continue

                    # Check referential integrity
                    if milestone_ids and (
                        eobj.prerequisite_id not in milestone_ids or eobj.dependant_id not in milestone_ids
                    ):
                        error_messages.append(
                            f"dependencies.csv line {idx}: references unknown milestone ID: {eobj.prerequisite_id}->{eobj.dependant_id}"
                        )
                        continue

                    dep_count += 1

                if dep_count == 0:
                    error_messages.append("dependencies.csv contains no valid data rows")
        except Exception as e:
            return f"Failed to read dependencies.csv: {e}"

        if error_messages:
            return "; ".join(error_messages)

        self.logger.info(f"  ✓ Validated {dep_count} dependency edges")

        # === DAG Validity Validation (cycle detection + redundancy check) ===
        self.logger.info("  Validating DAG structure (cycle detection + redundancy check)...")

        try:
            from ...tools.conflict_analysis.milestone_dependency_analyzer import (
                MilestoneDependencyAnalyzer,
            )

            # Create analyzer
            analyzer = MilestoneDependencyAnalyzer(
                commit_dag_path=self.ctx.get_input("commit_dag"),
                milestone_csv_path=str(milestone_csv_path),
                dependencies_csv_path=str(deps_csv_path),
                detect_redundant=True,
            )

            # Run enrichment analysis
            dag_result = analyzer.enrich_dependencies()

            # Check is_valid_dag
            summary = dag_result.get("summary", {})
            is_valid_dag = summary.get("is_valid_dag", True)
            cycles_count = summary.get("all_cycles_count", 0)
            missing_count = summary.get("missing_dependencies_count", 0)
            redundant_count = len(dag_result.get("redundant_dependencies", []))

            if not is_valid_dag or cycles_count > 0:
                # High-level summary with key metrics (self-explanatory)
                error_msg = (
                    f"Dependencies validation failed: "
                    f"valid={'yes' if is_valid_dag else 'no'}, "
                    f"cycles={cycles_count}, "
                    f"missing={missing_count} (inferred from commit DAG but not declared), "
                    f"redundant={redundant_count} (derivable via transitivity). "
                    f"DAG contains {cycles_count} cycle(s) that must be broken."
                )
                return error_msg

            # Log warnings for missing/redundant dependencies (non-blocking)
            if missing_count > 0:
                self.logger.warning(f"  ⚠ Found {missing_count} missing dependencies (inferred but not declared)")
            if redundant_count > 0:
                self.logger.warning(f"  ⚠ Found {redundant_count} redundant dependencies (transitive)")

            self.logger.info(
                f"  ✓ DAG valid: no cycles in {dep_count} dependencies "
                f"(missing={missing_count}, redundant={redundant_count})"
            )

        except Exception as e:
            # Tool failure is treated as validation failure
            error_msg = f"DAG validation tool failed: {e}"
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

        # Archive dependencies.csv and copy to global result directory
        output_path = Path(self.ctx.get_output("dependencies_csv"))

        if output_path.exists():
            self.logger.archive_file(output_path, global_filename="dependencies.csv")
            saved_files.append(output_path.name)
        else:
            self.logger.warning(f"Expected output not found: {output_path}")

        return saved_files
