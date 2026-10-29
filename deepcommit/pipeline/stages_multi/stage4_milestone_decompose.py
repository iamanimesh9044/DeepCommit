"""
Stage 4: Milestone Decompose

Balances the milestone DAG by splitting oversized milestones and merging
undersized ones to achieve a more uniform size distribution (CV < 1.0).

Input: milestones.csv, dependencies.csv (auto-linked from Stage 3)
Output: Updated milestones.csv, dependencies.csv (balanced), balancing_report.json
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from ..base_stage import BaseStage
from ..models import DependencyEdgeModel, MilestoneModel

if TYPE_CHECKING:
    pass


class Stage4MilestoneDecompose(BaseStage):
    """Stage 4: Milestone Decompose - balances milestone DAG by splitting/merging."""

    # Instance variable to store initial CV for comparison
    _initial_cv: Optional[float] = None

    @staticmethod
    def _calculate_cv_from_csv(csv_path: Path) -> Optional[float]:
        """Calculate coefficient of variation (CV) of src_loc from milestones.csv.

        Args:
            csv_path: Path to milestones.csv

        Returns:
            CV value, or None if calculation fails
        """
        import statistics

        src_locs = []
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    src_loc_str = row.get("src_loc", "0")
                    try:
                        src_loc = int(src_loc_str) if src_loc_str else 0
                        if src_loc > 0:
                            src_locs.append(src_loc)
                    except ValueError:
                        continue

            if len(src_locs) < 2:
                return None

            mean_val = statistics.mean(src_locs)
            if mean_val == 0:
                return None

            std_val = statistics.stdev(src_locs)
            return std_val / mean_val

        except Exception:
            return None

    def _setup_input_data(self) -> None:
        """Setup input data - verify linked files from Stage 3.

        Input files are auto-linked from previous stages by main_multi_stage.py:
        - milestone_csv: from Stage 2 output (via Stage 3)
        - dependencies_csv: from Stage 3 output
        """
        self.logger.info("Verifying input files linked from previous stages...")

        # Verify milestone_csv exists
        milestone_csv = Path(self.ctx.get_input("milestone_csv"))
        if not milestone_csv.exists():
            raise FileNotFoundError(f"milestones.csv not found: {milestone_csv}")

        # Verify dependencies_csv exists
        dependencies_csv = Path(self.ctx.get_input("dependencies_csv"))
        if not dependencies_csv.exists():
            raise FileNotFoundError(f"dependencies.csv not found: {dependencies_csv}")

        # Log verified inputs
        self.logger.info(f"  ✓ milestones.csv: {milestone_csv}")
        self.logger.info(f"  ✓ dependencies.csv: {dependencies_csv}")

        if self.ctx.is_multi_repo:
            self.logger.info(
                "  Multi-repo workspace: Docker compile verification is skipped "
                "(no single base image/build command covers several repos); src_loc uses prefixed src dirs: "
                f"{self.ctx.get_repo_src_dirs()}"
            )

        # Log milestone statistics and calculate initial CV
        try:
            with open(milestone_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                milestone_count = sum(1 for _ in reader)
            self.logger.info(f"  Total milestones: {milestone_count}")

            # Calculate and store initial CV for later comparison
            # Note: src_loc column may not exist in input CSV (will be calculated by analysis script)
            self._initial_cv = self._calculate_cv_from_csv(milestone_csv)
            if self._initial_cv is not None:
                self.logger.info(f"  Initial src_loc CV: {self._initial_cv:.3f}")
            else:
                self.logger.info("  Initial CV: N/A (src_loc will be calculated by analysis script)")
        except Exception as e:
            self.logger.warning(f"  Could not count milestones: {e}")

    def _validate_outputs(self) -> Optional[str]:
        """Validate Stage 4 output files.

        Validates:
        1. milestones.csv format and integrity
        2. dependencies.csv references (all IDs must exist in milestones.csv)
        3. DAG is acyclic (no cycles)
        4. balancing_report.json exists and has valid structure

        Returns:
            Error message if validation fails, None if successful
        """
        self.logger.info("Validating output files...")
        error_messages: List[str] = []

        # Get output paths
        milestone_csv_path = Path(self.ctx.get_output("milestone_csv"))
        deps_csv_path = Path(self.ctx.get_output("dependencies_csv"))
        report_path = Path(self.ctx.get_output("balancing_report"))

        # Validate milestones.csv
        if not milestone_csv_path.exists():
            return f"Missing milestones.csv at {milestone_csv_path}"

        milestone_ids = set()
        milestone_count = 0
        commit_to_milestones: Dict[str, set] = {}
        # Track integration_test_commit column values for validation
        milestone_integration_test_col: Dict[str, str] = {}

        try:
            with open(milestone_csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader, start=2):
                    payload = {k: (row.get(k) or "").strip() for k in MilestoneModel.model_fields.keys()}
                    try:
                        mobj = MilestoneModel.model_validate(payload)
                        milestone_ids.add(mobj.id)
                        milestone_count += 1

                        commits = [c.strip() for c in (mobj.commits or "").split(";") if c.strip()]
                        for commit_sha in commits:
                            commit_to_milestones.setdefault(commit_sha[:7], set()).add(mobj.id)

                        # Track integration_test_commit column value
                        if mobj.integration_test_commit:
                            milestone_integration_test_col[mobj.id] = mobj.integration_test_commit[:7]

                        # Validate sub-milestone ID format (must use _sub-{01-99})
                        milestone_id = mobj.id
                        if "_sub-" in milestone_id:
                            # This is a sub-milestone, validate format: M{n}_sub-{01-99}
                            if not re.match(r"^M\d+_sub-(?:0[1-9]|[1-9]\d)$", milestone_id):
                                error_messages.append(
                                    f"Invalid sub-milestone ID format: '{milestone_id}'. "
                                    f"Must use format M{{n}}_sub-{{01-99}} (e.g., M3_sub-01, M3_sub-02)"
                                )
                        else:
                            # Check for incorrect formats (M007a, M007.1, etc.)
                            if re.match(r"^M\d+[a-zA-Z]$", milestone_id) or re.match(r"^M\d+\.\d+$", milestone_id):
                                error_messages.append(
                                    f"Invalid milestone ID format: '{milestone_id}'. "
                                    f"Use _sub- format for sub-milestones (e.g., M007_sub-01), not letter or dot suffix"
                                )
                    except Exception as e:
                        error_messages.append(f"milestones.csv line {idx} invalid: {e}")
                        continue

            if milestone_count == 0:
                error_messages.append("milestones.csv contains no valid data rows")

        except Exception as e:
            return f"Failed to read milestones.csv: {e}"

        self.logger.info(f"  ✓ Validated {milestone_count} milestones")

        # Validate split commits are distributed across milestones
        stage_output = self.ctx.get_stage_output_dir()
        commit_splits_dir = None
        if stage_output.exists():
            commit_splits_dir = stage_output / "commit_splits"

        if commit_splits_dir and commit_splits_dir.exists():
            metadata_files = sorted(commit_splits_dir.glob("*/METADATA.json"))
            if metadata_files:
                for metadata_file in metadata_files:
                    try:
                        with open(metadata_file, "r", encoding="utf-8") as f:
                            metadata = json.load(f)
                    except Exception as e:
                        error_messages.append(f"Failed to read split metadata: {metadata_file} ({e})")
                        continue

                    original = metadata.get("original_commit", {})
                    # Handle both string and dict formats for original_commit
                    if isinstance(original, str):
                        original_sha = original[:7]
                    else:
                        original_sha = (
                            original.get("short_hash")
                            or original.get("short_sha")
                            or original.get("hash")
                            or original.get("sha")
                            or ""
                        )[:7]
                    # Support multiple METADATA.json formats
                    split_result = metadata.get("split_result", {})
                    split_commits = (
                        metadata.get("src_commits", [])  # Top-level src_commits (common format)
                        or split_result.get("src_commits", [])
                        or split_result.get("commits", [])
                        or metadata.get("commits", [])
                        or metadata.get("split_commits", [])
                    )
                    split_shas = [
                        (sc.get("short_hash") or sc.get("short_sha") or sc.get("hash") or sc.get("sha") or "")[:7]
                        for sc in split_commits
                        if sc.get("short_hash") or sc.get("short_sha") or sc.get("hash") or sc.get("sha")
                    ]

                    if not original_sha or not split_shas:
                        continue

                    milestone_bucket = set()
                    missing_splits = []
                    for split_sha in split_shas:
                        milestones_for_split = commit_to_milestones.get(split_sha, set())
                        if not milestones_for_split:
                            missing_splits.append(split_sha)
                            continue
                        milestone_bucket.update(milestones_for_split)

                    if not missing_splits and len(milestone_bucket) == 1:
                        only_ms = next(iter(milestone_bucket))
                        error_messages.append(
                            f"Split commits for original {original_sha} are all assigned to one milestone ({only_ms}). "
                            f"Regroup split commits across milestones (recommended), or drop split commits and keep the original (if ultimately failed to regroup)."
                        )

                    # === Validate integration_test_commit placement ===
                    integration_test = split_result.get("integration_test_commit")
                    if integration_test:
                        test_sha = (
                            integration_test.get("short_sha")
                            or integration_test.get("short_hash")
                            or integration_test.get("sha", "")[:7]
                        )
                        if test_sha:
                            test_sha = test_sha[:7]

                            # Rule 1: integration_test_commit should NOT be in any commits column
                            milestones_with_test_in_commits = commit_to_milestones.get(test_sha, set())
                            if milestones_with_test_in_commits:
                                error_messages.append(
                                    f"[IntegrationTestCommit] {test_sha} found in 'commits' column of "
                                    f"{list(milestones_with_test_in_commits)}. "
                                    f"Fix: Remove {test_sha} from commits column; "
                                    f"put it in 'integration_test_commit' column of first sub-milestone only."
                                )

                            # Rule 2: integration_test_commit should be in integration_test_commit column
                            milestones_with_test_col = [
                                mid for mid, sha in milestone_integration_test_col.items() if sha == test_sha
                            ]

                            if not milestones_with_test_col:
                                # Find the first sub-milestone from this split to suggest
                                first_sub = None
                                for mid in sorted(milestone_bucket):
                                    if "." in mid:
                                        first_sub = mid
                                        break
                                hint = f" (suggest: {first_sub})" if first_sub else ""
                                error_messages.append(
                                    f"[IntegrationTestCommit] {test_sha} not in any 'integration_test_commit' column. "
                                    f"Fix: Add {test_sha} to 'integration_test_commit' column of first sub-milestone{hint}."
                                )
                            elif len(milestones_with_test_col) > 1:
                                error_messages.append(
                                    f"[IntegrationTestCommit] {test_sha} in multiple milestones' "
                                    f"'integration_test_commit' column: {milestones_with_test_col}. "
                                    f"Fix: Keep only in first sub-milestone, clear others."
                                )

        # Validate CV improvement (final CV must be <= initial CV)
        final_cv = self._calculate_cv_from_csv(milestone_csv_path)
        if final_cv is not None:
            self.logger.info(f"  Final src_loc CV: {final_cv:.3f}")

            if self._initial_cv is not None:
                if final_cv > self._initial_cv:
                    error_messages.append(
                        f"CV regression: final CV ({final_cv:.3f}) > initial CV ({self._initial_cv:.3f}). "
                        f"Decomposition should not increase CV."
                    )
                else:
                    cv_improvement = self._initial_cv - final_cv
                    self.logger.info(
                        f"  ✓ CV improved: {self._initial_cv:.3f} -> {final_cv:.3f} (delta: -{cv_improvement:.3f})"
                    )
        else:
            self.logger.warning("  Could not calculate final CV for comparison")

        # Validate dependencies.csv
        if not deps_csv_path.exists():
            return f"Missing dependencies.csv at {deps_csv_path}"

        dep_count = 0

        try:
            with open(deps_csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader, start=2):
                    payload = {k: (row.get(k) or "").strip() for k in DependencyEdgeModel.model_fields.keys()}
                    try:
                        eobj = DependencyEdgeModel.model_validate(payload)
                    except Exception as e:
                        error_messages.append(f"dependencies.csv line {idx} invalid: {e}")
                        continue

                    # Check referential integrity
                    if eobj.prerequisite_id not in milestone_ids or eobj.dependant_id not in milestone_ids:
                        error_messages.append(
                            f"dependencies.csv line {idx}: references unknown milestone ID: "
                            f"{eobj.prerequisite_id}->{eobj.dependant_id}"
                        )
                        continue

                    dep_count += 1

            if dep_count == 0:
                error_messages.append("dependencies.csv contains no valid data rows")

        except Exception as e:
            return f"Failed to read dependencies.csv: {e}"

        self.logger.info(f"  ✓ Validated {dep_count} dependency edges")

        # === DAG Validity Validation (cycle detection + redundancy check) ===
        self.logger.info("  Validating DAG structure (cycle detection + redundancy check)...")

        try:
            import sys
            from io import StringIO

            from ...tools.conflict_analysis.milestone_dependency_analyzer import (
                MilestoneDependencyAnalyzer,
            )

            # Suppress stderr during analyzer execution to avoid BrokenPipeError
            # The analyzer uses print(..., file=sys.stderr) extensively for status output
            old_stderr = sys.stderr
            try:
                sys.stderr = StringIO()  # Redirect stderr to a string buffer

                # Determine split_metadata_dir from current stage output
                split_metadata_dir = None
                if commit_splits_dir and commit_splits_dir.exists():
                    split_metadata_dir = str(commit_splits_dir)

                analyzer = MilestoneDependencyAnalyzer(
                    commit_dag_path=self.ctx.get_input("commit_dag"),
                    milestone_csv_path=str(milestone_csv_path),
                    dependencies_csv_path=str(deps_csv_path),
                    detect_redundant=True,
                    split_metadata_dir=split_metadata_dir,
                    repo_path=None if self.ctx.is_multi_repo else self.ctx.get_repo(),
                    resolver=self.ctx.get_resolver(),
                )

                dag_result = analyzer.enrich_dependencies()
            finally:
                sys.stderr = old_stderr  # Restore stderr

            # Check is_valid_dag
            summary = dag_result.get("summary", {})
            is_valid_dag = summary.get("is_valid_dag", True)
            cycles_count = summary.get("all_cycles_count", 0)
            missing_count = summary.get("missing_dependencies_count", 0)
            redundant_count = len(dag_result.get("redundant_dependencies", []))

            if not is_valid_dag or cycles_count > 0:
                # Get cycle details for error message
                cycles_info = dag_result.get("cycles", {})
                existing_cycles = cycles_info.get("existing_cycles", [])

                cycle_details = []
                for cycle in existing_cycles[:3]:  # Show first 3 cycles
                    cycle_path = " -> ".join(cycle)
                    cycle_details.append(cycle_path)

                cycle_preview = "; ".join(cycle_details) if cycle_details else "see enriched_cycles"

                # High-level summary with key metrics (consistent with stage3)
                error_msg = (
                    f"[DAGConflicts] valid={'yes' if is_valid_dag else 'no'} "
                    f"cycles={cycles_count}, missing={missing_count}, redundant={redundant_count}.\n"
                    f"  Cycles: {cycle_preview}\n"
                    f"  Hint: Remove weakest edge (TEXT/Weak > NFR/Weak > FUNC/Weak > Strong) to break cycles.\n"
                    f"  The agent should have used validate_milestone_partitioning and analyze_dag_conflicts tools."
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
            error_msg = f"DAG validation tool failed: {e}"
            self.logger.error(error_msg)
            return error_msg

        # Validate balancing_report.json (optional but expected)
        if report_path.exists():
            try:
                with open(report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)

                # Check required fields (only status is required)
                if "status" not in report:
                    error_messages.append("balancing_report.json missing required field: status")
                else:
                    # Get final_cv from top-level or summary.final_cv
                    final_cv = report.get("final_cv") or report.get("summary", {}).get("final_cv", 0)
                    status = report.get("status", "")
                    self.logger.info(f"  ✓ Balancing report: status={status}, final_cv={final_cv:.3f}")

                    # Warn if CV target not met (but don't fail)
                    if final_cv >= 1.0:
                        self.logger.warning(f"  ⚠ CV target not met: {final_cv:.3f} >= 1.0 (may need manual review)")

            except json.JSONDecodeError as e:
                error_messages.append(f"balancing_report.json is not valid JSON: {e}")
            except Exception as e:
                error_messages.append(f"Failed to validate balancing_report.json: {e}")
        else:
            self.logger.warning(f"  ⚠ balancing_report.json not found (expected at {report_path})")

        if error_messages:
            return "; ".join(error_messages)

        return None

    def _archive_outputs(self) -> List[str]:
        """Archive output files to stage output directory and global result directory.

        Archives:
        - milestones.csv -> global result/milestones.csv (overwrites)
        - dependencies.csv -> global result/dependencies.csv (overwrites)
        - milestone_balancing_report.json -> stage output directory

        Returns:
            List of archived filenames
        """
        self.logger.info("Archiving output files...")
        saved_files = []

        # Archive milestones.csv (overwrite global)
        milestone_path = Path(self.ctx.get_output("milestone_csv"))
        if milestone_path.exists():
            self.logger.archive_file(milestone_path, global_filename="milestones.csv")
            saved_files.append(milestone_path.name)
            self.logger.info("  ✓ Archived milestones.csv (overwrites global)")
        else:
            self.logger.warning(f"  ⚠ Expected output not found: {milestone_path}")

        # Archive dependencies.csv (overwrite global)
        deps_path = Path(self.ctx.get_output("dependencies_csv"))
        if deps_path.exists():
            self.logger.archive_file(deps_path, global_filename="dependencies.csv")
            saved_files.append(deps_path.name)
            self.logger.info("  ✓ Archived dependencies.csv (overwrites global)")
        else:
            self.logger.warning(f"  ⚠ Expected output not found: {deps_path}")

        # Archive balancing report (stage-local only)
        report_path = Path(self.ctx.get_output("balancing_report"))
        if report_path.exists():
            self.logger.archive_file(report_path)
            saved_files.append(report_path.name)
            self.logger.info(f"  ✓ Archived {report_path.name}")
        else:
            self.logger.warning(f"  ⚠ Balancing report not found: {report_path}")

        return saved_files
