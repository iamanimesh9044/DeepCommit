from __future__ import annotations

import logging
import os
import sys
from io import StringIO
from pathlib import Path
from typing import Any, Dict, Optional, Union

from pipeline.utils.pipeline_logger import PipelineLoggerAdapter

from ...tools import (
    MilestoneDependencyAnalyzer,
    analyze_all_milestones,
    analyze_commit_coverage,
    analyze_dag_parallelism_tool,
    analyze_milestone_size_balance,
    validate_dag,
    validate_milestone_partitioning,
)
from ..context import TrialContext
from ..types import StageResult


def run_comprehensive_analysis(
    milestones_csv: Union[str, Path],
    dependencies_csv: Union[str, Path],
    repo_path: Optional[Union[str, Path]],
    preprocessed_path: Optional[Union[str, Path]] = None,
    commit_dag_path: Optional[Union[str, Path]] = None,
    output_dir: Optional[Union[str, Path]] = None,
    trial_name: str = "unknown_trial",
    repo_id: str = "unknown_repo",
    skip_if_exists: bool = False,
    compact_metrics: bool = False,
    dry_run: bool = False,
    logger: Optional[logging.Logger | PipelineLoggerAdapter] = None,
    split_metadata_dir: Optional[Union[str, Path]] = None,
    repo_src_dirs: Optional[list[str]] = None,
    exclude_patterns: Optional[list[str]] = None,
) -> Dict[str, Any]:
    """Run comprehensive analysis on milestones and DAG.

    This is the core analysis function that can be used both by pipeline and standalone scripts.

    Args:
        milestones_csv: Path to milestones.csv
        dependencies_csv: Path to dependencies.csv
        repo_path: Path to git repository (for LoC analysis)
        preprocessed_path: Path to preprocessed data (for commit coverage analysis)
        commit_dag_path: Path to commit DAG file (optional, will auto-detect from preprocessed_path if not provided)
        output_dir: Output directory for analysis results (default: same dir as milestones_csv / final_result)
        trial_name: Name of the trial (for metadata)
        repo_id: Repository ID (for metadata)
        skip_if_exists: If True, skip analysis if output_dir already has results
        compact_metrics: If True, log compact single-line metrics for each analysis
        dry_run: If True, only output to console without saving files
        logger: Logger instance (if None, will print to stdout)
        split_metadata_dir: Path to split commit metadata directory (optional, for split commit DAG edge support)
        repo_src_dirs: Source directories for LOC calculation (e.g., ["src", "lib"])
        exclude_patterns: Exclude patterns for filtering (e.g., ["**/test/**"])

    Returns:
        Dict containing all analysis results
    """
    import json

    # Helper function to resolve commit_dag path
    def _resolve_commit_dag_path() -> Optional[Path]:
        """Resolve commit_dag path from explicit parameter or auto-detect from preprocessed_path.

        Returns:
            Path to commit_dag file or None if not found
        """
        # Use explicit commit_dag_path if provided
        if commit_dag_path:
            dag_path = Path(commit_dag_path)
            if dag_path.exists():
                return dag_path
            else:
                logger.warning(f"Provided commit_dag_path does not exist: {dag_path}")
                return None

        # Auto-detect from preprocessed_path
        if not preprocessed_path or not Path(preprocessed_path).exists():
            return None

        preproc_path = Path(preprocessed_path)

        # Try enhanced format first (required for conflict analysis)
        candidates = [
            preproc_path / "dag" / "commit_dag_v3_ctx_3_pctx_3_enhanced.json",
            preproc_path / "dag" / "commit_dag_v3_ctx_3_pctx_3.json",
            preproc_path / "dag" / "commit_dag.json",
        ]

        for candidate in candidates:
            if candidate.exists():
                return candidate

        return None

    # Setup logger
    if logger is None:
        logger = logging.getLogger(__name__)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)

    # Convert to Path objects
    milestones_csv = Path(milestones_csv).resolve()
    dependencies_csv = Path(dependencies_csv).resolve()
    repo_path = Path(repo_path).resolve() if repo_path else ""
    preprocessed_path = Path(preprocessed_path).resolve() if preprocessed_path else ""

    # Determine output directory
    if output_dir is None:
        output_dir = milestones_csv.parent / "analysis"
    else:
        output_dir = Path(output_dir).resolve()

    # Determine whether to save results
    save_results = not dry_run

    # Check if should skip (only when not dry_run)
    if not dry_run and skip_if_exists and output_dir.exists():
        existing_files = list(output_dir.glob("*.json"))
        if existing_files:
            logger.info(f"Analysis results already exist in {output_dir}, skipping...")
            return {
                "skipped": True,
                "reason": "Results already exist",
                "output_dir": str(output_dir),
                "existing_files": [f.name for f in existing_files],
            }

    # Create output directory only when saving
    if save_results:
        output_dir.mkdir(parents=True, exist_ok=True)

    if not compact_metrics:
        logger.info("Running comprehensive analysis...")
        logger.info(f"  Milestones: {milestones_csv}")
        logger.info(f"  Dependencies: {dependencies_csv}")
        if dry_run:
            logger.info("  Mode: dry-run (no files will be saved)")
        else:
            logger.info(f"  Output: {output_dir}")

    # Check if required files exist
    if not milestones_csv.exists():
        error_msg = f"milestones.csv not found at {milestones_csv}"
        logger.error(error_msg)
        return {"error": error_msg, "status": "error"}

    if not dependencies_csv.exists():
        error_msg = f"dependencies.csv not found at {dependencies_csv}"
        logger.error(error_msg)
        return {"error": error_msg, "status": "error"}

    # Prepare analysis results container
    all_results = {
        "trial_name": trial_name,
        "repo_id": repo_id,
        "milestones_csv": str(milestones_csv),
        "dependencies_csv": str(dependencies_csv),
        "output_dir": str(output_dir),
        "analyses": {},
    }

    # Track analysis success/failure
    analysis_status = []

    # ====== 1. Commit Coverage Analysis ======
    if preprocessed_path and preprocessed_path.exists():
        main_commits_file = preprocessed_path / "main_commits.jsonl"
        logger.info("Running commit coverage analysis...")
        try:
            coverage_result = analyze_commit_coverage(
                csv_file=str(milestones_csv),
                main_commits_file=str(main_commits_file) if main_commits_file.exists() else "",
                repo_path=str(repo_path) if repo_path else "",
                split_metadata_dir=str(split_metadata_dir) if split_metadata_dir else "",
                save_results=save_results,
                output_dir=str(output_dir) if save_results else "",
            )
            all_results["analyses"]["commit_coverage"] = coverage_result

            # Log results with effective coverage (considering sub-commits)
            coverage = coverage_result.get("coverage", {})
            raw_cov = coverage.get("raw_coverage") or coverage_result.get("main_coverage", 0) or 0
            effective_cov = coverage.get("effective_coverage") or raw_cov

            # Check for decomposed commits
            summary = coverage_result.get("summary", {})
            decomposed_count = summary.get("decomposed_commits", 0)
            sub_count = summary.get("total_sub_commits", 0)

            if decomposed_count > 0:
                decomposed = coverage.get("decomposed", {})  # noqa: F841
                sub_cov = coverage.get("sub_commits", {})
                logger.info(
                    "[CommitCoverage] raw=%.1f%% effective=%.1f%% | decomposed=%d (→%d sub-commits, %.1f%% covered)",
                    raw_cov,
                    effective_cov,
                    decomposed_count,
                    sub_count,
                    sub_cov.get("percentage", 0),
                )
                # Log decomposed commit details
                for detail in coverage_result.get("decomposed_details", []):
                    status_icon = (
                        "✓"
                        if detail["status"] == "fully_covered"
                        else "△"
                        if detail["status"] == "partially_covered"
                        else "✗"
                    )
                    logger.info(
                        "  %s [%s] %s → %d/%d sub-commits covered",
                        status_icon,
                        detail["original_sha"],
                        detail["branch"],
                        detail["sub_commits_covered"],
                        detail["sub_commits_total"],
                    )
            else:
                logger.info("[CommitCoverage] main=%.1f%%", raw_cov)

            analysis_status.append(("commit_coverage", "ok"))
        except Exception as e:
            logger.error("✗ Commit coverage analysis failed: %s", str(e))
            all_results["analyses"]["commit_coverage"] = {"error": str(e)}
            analysis_status.append(("commit_coverage", "error"))
    else:
        logger.warning("Skipping commit coverage analysis (no preprocessed_path provided)")
        all_results["analyses"]["commit_coverage"] = {"skipped": "No preprocessed_path provided"}
        analysis_status.append(("commit_coverage", "skipped"))

    # ====== 2. Milestone Size Balance Analysis ======
    logger.info("Running milestone size balance analysis...")
    try:
        balance_result = analyze_milestone_size_balance(
            csv_file=str(milestones_csv),
            repo_path=str(repo_path) if repo_path else "",
            save_results=save_results,
            output_dir=str(output_dir) if save_results else "",
            repo_src_dir=repo_src_dirs,
            exclude_patterns=exclude_patterns,
        )
        all_results["analyses"]["size_balance"] = balance_result

        # Log results - only src_loc based statistics
        loc_stats = balance_result.get("loc_stats", {})
        mean = loc_stats.get("loc_mean", 0)
        std = loc_stats.get("loc_std", 0)
        cv = loc_stats.get("loc_cv") or 0
        logger.info("[SizeBalance] src_loc: mean=%.0f std=%.0f cv=%.2f", mean, std, cv)
        analysis_status.append(("size_balance", "ok"))
    except Exception as e:
        logger.error("✗ Size balance analysis failed: %s", str(e))
        all_results["analyses"]["size_balance"] = {"error": str(e)}
        analysis_status.append(("size_balance", "error"))

    # ====== 3. DAG Validation (Cycle Detection) ======
    logger.info("Running DAG validation (cycle detection)...")
    try:
        dag_validation = validate_dag(
            csv_path=str(dependencies_csv),
            save_results=save_results,
            output_dir=str(output_dir) if save_results else "",
        )
        all_results["analyses"]["dag_validation"] = dag_validation

        # Log results (compact or detailed based on mode)
        node_count = dag_validation.get("node_count", 0)
        edge_count = dag_validation.get("edge_count", 0)
        cycle_count = dag_validation.get("cycle_count", 0)
        is_valid = dag_validation.get("is_valid_dag", False)

        if is_valid:
            logger.info("[DAGValidation] valid=yes nodes=%d edges=%d", node_count, edge_count)
            analysis_status.append(("dag_validation", "ok"))
        else:
            logger.info("[DAGValidation] valid=no cycles=%d", cycle_count)
            analysis_status.append(("dag_validation", "warning"))
    except Exception as e:
        logger.error("✗ DAG validation failed: %s", str(e))
        all_results["analyses"]["dag_validation"] = {"error": str(e)}
        analysis_status.append(("dag_validation", "error"))

    # ====== 4. DAG Parallelism Analysis ======
    if all_results["analyses"]["dag_validation"].get("is_valid_dag", False):
        logger.info("Running DAG parallelism analysis...")
        try:
            parallelism_result = analyze_dag_parallelism_tool(
                csv_path=str(dependencies_csv),
                save_results=save_results,
                output_dir=str(output_dir) if save_results else "",
            )
            all_results["analyses"]["dag_parallelism"] = parallelism_result

            # Log results (compact or detailed based on mode)
            critical_path = parallelism_result.get("critical_path_length", 0)
            max_width = parallelism_result.get("maximum_width", 0)
            avg_parallel = parallelism_result.get("average_parallelism", 0)
            logger.info(
                "[DAGParallelism] critical_path=%d max_width=%d avg=%.1f", critical_path, max_width, avg_parallel
            )
            analysis_status.append(("dag_parallelism", "ok"))
        except Exception as e:
            logger.error("✗ DAG parallelism analysis failed: %s", str(e))
            all_results["analyses"]["dag_parallelism"] = {"error": str(e)}
            analysis_status.append(("dag_parallelism", "error"))
    else:
        logger.warning("Skipping parallelism analysis due to invalid DAG")
        all_results["analyses"]["dag_parallelism"] = {"skipped": "DAG contains cycles"}
        analysis_status.append(("dag_parallelism", "skipped"))

    # ====== 5. Milestone Partitioning Validation ======
    if repo_path and repo_path.exists():
        logger.info("Running milestone partitioning validation...")

        # Resolve commit_dag path
        resolved_commit_dag_path = _resolve_commit_dag_path()

        if resolved_commit_dag_path:
            try:
                # Suppress stderr to avoid BrokenPipeError from analyzer's print() calls
                old_stderr = sys.stderr
                try:
                    sys.stderr = StringIO()
                    # Use validate_milestone_partitioning tool
                    partitioning_result = validate_milestone_partitioning(
                        milestone_csv_path=str(milestones_csv),
                        commit_dag_path=str(resolved_commit_dag_path),
                        split_metadata_dir=str(split_metadata_dir) if split_metadata_dir else None,
                        repo_path=str(repo_path) if repo_path else None,
                    )
                finally:
                    sys.stderr = old_stderr

                all_results["analyses"]["milestone_partitioning"] = partitioning_result

                # Save to file (skip if dry_run)
                if save_results:
                    partitioning_file = output_dir / "milestone_partitioning_validation.json"
                    with open(partitioning_file, "w", encoding="utf-8") as f:
                        json.dump(partitioning_result, f, indent=2, ensure_ascii=False)

                # Log results
                is_valid = partitioning_result["is_valid_partitioning"]
                cycles_count = partitioning_result["cycles_count"]
                inferred_count = partitioning_result["inferred_dependencies_count"]

                logger.info(
                    "[PartitioningValidation] valid=%s cycles=%d inferred_deps=%d",
                    "yes" if is_valid else "no",
                    cycles_count,
                    inferred_count,
                )

                if not is_valid:
                    logger.warning(
                        "⚠ Milestone partitioning has %d cycle(s). Milestones may need re-partitioning.", cycles_count
                    )
                    analysis_status.append(("milestone_partitioning", "warning"))
                else:
                    analysis_status.append(("milestone_partitioning", "ok"))

            except Exception as e:
                logger.error("✗ Milestone partitioning validation failed: %s", str(e))
                all_results["analyses"]["milestone_partitioning"] = {"error": str(e)}
                analysis_status.append(("milestone_partitioning", "error"))
        else:
            logger.warning("Skipping partitioning validation (commit_dag.json not found)")
            all_results["analyses"]["milestone_partitioning"] = {"skipped": "commit_dag.json not found"}
            analysis_status.append(("milestone_partitioning", "skipped"))
    else:
        logger.warning("Skipping partitioning validation (no repo_path provided)")
        all_results["analyses"]["milestone_partitioning"] = {"skipped": "No repo_path provided"}
        analysis_status.append(("milestone_partitioning", "skipped"))

    # ====== 6. Milestone Conflict Analysis ======
    if repo_path and repo_path.exists():
        logger.info("Running milestone conflict analysis (DAG-based)...")

        # Resolve commit_dag path
        resolved_commit_dag_path = _resolve_commit_dag_path()

        if resolved_commit_dag_path:
            try:
                # Suppress stderr to avoid BrokenPipeError from analyzer's print() calls
                old_stderr = sys.stderr
                try:
                    sys.stderr = StringIO()
                    # Call DAG-based analysis function
                    milestone_conflict_result = analyze_all_milestones(
                        repo_path=str(repo_path),
                        milestone_csv_path=str(milestones_csv),
                        commit_dag_path=str(resolved_commit_dag_path),
                        dependencies_csv_path=str(dependencies_csv),  # Add dependencies for frontier analysis
                        main_branch="main",
                        milestone_ids=None,  # Analyze all milestones
                        split_metadata_dir=str(split_metadata_dir) if split_metadata_dir else None,
                    )
                finally:
                    sys.stderr = old_stderr

                all_results["analyses"]["milestone_conflicts"] = milestone_conflict_result

                # Save milestone conflict results to file (skip if dry_run)
                if save_results:
                    milestone_conflict_file = output_dir / "milestone_conflicts_analysis.json"
                    with open(milestone_conflict_file, "w", encoding="utf-8") as f:
                        json.dump(milestone_conflict_result, f, indent=2, ensure_ascii=False)

                # Log results (compact or detailed based on mode)
                summary = milestone_conflict_result.get("summary", {})
                incomplete_chains = summary.get("total_incomplete_chains", 0)
                affected_milestones = summary.get("milestones_with_incomplete_chains", 0)
                logger.info(
                    "[MilestoneConflicts] incomplete_chains=%d affected_milestones=%d",
                    incomplete_chains,
                    affected_milestones,
                )
                analysis_status.append(("milestone_conflicts", "ok"))
            except Exception as e:
                logger.error("✗ Milestone conflict analysis failed: %s", str(e))
                all_results["analyses"]["milestone_conflicts"] = {"error": str(e)}
                analysis_status.append(("milestone_conflicts", "error"))
        else:
            logger.warning("Skipping milestone conflict analysis (commit_dag.json not found)")
            all_results["analyses"]["milestone_conflicts"] = {"skipped": "commit_dag.json not found"}
            analysis_status.append(("milestone_conflicts", "skipped"))
    else:
        logger.warning("Skipping milestone conflict analysis (no repo_path provided)")
        all_results["analyses"]["milestone_conflicts"] = {"skipped": "No repo_path provided"}
        analysis_status.append(("milestone_conflicts", "skipped"))

    # ====== 7. DAG Conflict Analysis ======
    if repo_path and repo_path.exists():
        logger.info("Running DAG conflict analysis...")

        # Resolve commit_dag path
        resolved_commit_dag_path = _resolve_commit_dag_path()

        if not resolved_commit_dag_path:
            logger.warning("Skipping DAG conflict analysis (commit_dag.json not found)")
            all_results["analyses"]["dag_conflicts"] = {"skipped": "commit_dag.json not found"}
            analysis_status.append(("dag_conflicts", "skipped"))
        else:
            try:
                # Suppress stderr to avoid BrokenPipeError from analyzer's print() calls
                old_stderr = sys.stderr
                try:
                    sys.stderr = StringIO()
                    # Use MilestoneDependencyAnalyzer for DAG-based dependency analysis
                    analyzer = MilestoneDependencyAnalyzer(
                        commit_dag_path=str(resolved_commit_dag_path),
                        milestone_csv_path=str(milestones_csv),
                        dependencies_csv_path=str(dependencies_csv),
                        detect_redundant=True,
                        split_metadata_dir=str(split_metadata_dir) if split_metadata_dir else None,
                        repo_path=str(repo_path) if repo_path else None,
                    )

                    dag_conflict_result = analyzer.enrich_dependencies()

                    # Save detailed analysis results to output_dir (skip if dry_run)
                    if save_results:
                        analyzer.export_detailed_analysis_json(
                            str(output_dir / "dag_dependency_analysis.json"), dag_conflict_result
                        )
                finally:
                    sys.stderr = old_stderr

                all_results["analyses"]["dag_conflicts"] = dag_conflict_result

                # Log results (compact or detailed based on mode)
                summary = dag_conflict_result.get("summary", {})
                missing = summary.get("missing_dependencies_count", 0)
                extra = summary.get("extra_dependencies_count", 0)
                cycles = summary.get("all_cycles_count", 0)
                is_valid_dag = summary.get("is_valid_dag", True)
                redundant = len(dag_conflict_result.get("redundant_dependencies", []))

                # Determine log level and status based on severity
                # Error if cycles (invalid DAG), warning if missing/redundant, ok otherwise
                if cycles > 0:
                    logger.error(
                        "[DAGConflicts] valid=%s cycles=%d, missing=%d redundant=%d, extra=%d",
                        "yes" if is_valid_dag else "no",
                        cycles,
                        missing,
                        redundant,
                        extra,
                    )
                    analysis_status.append(("dag_conflicts", "error"))
                elif missing > 0 or redundant > 0:
                    logger.warning(
                        "[DAGConflicts] valid=%s cycles=%d, missing=%d redundant=%d, extra=%d",
                        "yes" if is_valid_dag else "no",
                        cycles,
                        missing,
                        redundant,
                        extra,
                    )
                    analysis_status.append(("dag_conflicts", "warning"))
                else:
                    logger.info(
                        "[DAGConflicts] valid=%s cycles=%d, missing=%d redundant=%d, extra=%d",
                        "yes" if is_valid_dag else "no",
                        cycles,
                        missing,
                        redundant,
                        extra,
                    )
                    analysis_status.append(("dag_conflicts", "ok"))

            except Exception as e:
                logger.error("✗ DAG conflict analysis failed: %s", str(e))
                all_results["analyses"]["dag_conflicts"] = {"error": str(e)}
                analysis_status.append(("dag_conflicts", "error"))
    else:
        logger.warning("Skipping DAG conflict analysis (no repo_path provided)")
        all_results["analyses"]["dag_conflicts"] = {"skipped": "No repo_path provided"}
        analysis_status.append(("dag_conflicts", "skipped"))

    # ====== Summary ======
    all_results["analysis_status"] = analysis_status
    all_results["status"] = "ok" if not any(s == "error" for _, s in analysis_status) else "error"

    # Compact one-line summary
    ok_count = sum(1 for _, s in analysis_status if s == "ok")
    error_count = sum(1 for _, s in analysis_status if s == "error")
    skip_count = sum(1 for _, s in analysis_status if s == "skipped")
    warn_count = sum(1 for _, s in analysis_status if s == "warning")
    logger.info(
        "[AnalysisSummary] total=%d ok=%d error=%d skipped=%d warning=%d",
        len(analysis_status),
        ok_count,
        error_count,
        skip_count,
        warn_count,
    )

    return all_results


def analyze_outputs(ctx: TrialContext, logger: logging.Logger) -> StageResult:
    """Comprehensive analysis of milestones and DAG outputs (Pipeline wrapper).

    This function wraps the core analysis logic for use in the pipeline.

    Performs the following analyses:
    1. Commit coverage rate analysis
    2. Milestone size balance analysis
    3. DAG cycle validation
    4. DAG parallelism analysis
    5. Milestone conflict detection
    6. DAG conflict detection
    """

    print()
    logger.info("[7/8] Analyze outputs in %s", ctx.result_path)

    # Prepare paths
    result_path = Path(ctx.result_path).resolve()  # noqa: F841
    milestones_csv = Path(ctx.get_agent_output_path("milestones.csv"))
    dependencies_csv = Path(ctx.get_agent_output_path("dependencies.csv"))
    final_result_path = Path(ctx.get_agent_output_path("analysis_dir"))

    # Check if required files exist
    if not milestones_csv.exists():
        logger.error("milestones.csv not found at %s", milestones_csv)
        return StageResult(
            status="error", error=f"milestones.csv not found at {milestones_csv}", artifacts={"files_in_result": 0}
        )

    if not dependencies_csv.exists():
        logger.error("dependencies.csv not found at %s", dependencies_csv)
        return StageResult(
            status="error", error=f"dependencies.csv not found at {dependencies_csv}", artifacts={"files_in_result": 0}
        )

    logger.info("Found milestones.csv and dependencies.csv")

    # Determine split_metadata_dir from stage4_decomp output
    split_metadata_dir = None
    stage4_output = ctx.get_stage_output_dir("stage4_decomp")
    if stage4_output.exists():
        commit_splits_dir = stage4_output / "commit_splits"
        if commit_splits_dir.exists():
            split_metadata_dir = commit_splits_dir
            logger.info(f"Found split commit metadata: {split_metadata_dir}")

    # Call the core analysis function
    all_results = run_comprehensive_analysis(
        milestones_csv=milestones_csv,
        dependencies_csv=dependencies_csv,
        repo_path=ctx.get_repo(),
        preprocessed_path=ctx.get_preprocess(),
        output_dir=final_result_path,
        trial_name=ctx.trial_name,
        repo_id=ctx.repo_id,
        skip_if_exists=False,  # Pipeline always runs analysis
        logger=logger,
        split_metadata_dir=split_metadata_dir,
    )

    # Handle skip case
    if all_results.get("skipped"):
        logger.info("Analysis was skipped")
        return StageResult(
            status="ok",
            artifacts={"metrics": {"skipped": True}, "results": all_results},
            notes="Analysis skipped - results already exist",
        )

    # Handle error case
    if all_results.get("error"):
        return StageResult(status="error", error=all_results["error"], artifacts={"files_in_result": 0})

    # Count output files
    files_count = 0
    for _root, _dirs, files in os.walk(final_result_path):
        files_count += len(files)

    # Extract status from results
    analysis_status = all_results.get("analysis_status", [])
    all_ok = all(status == "ok" for _, status in analysis_status)
    has_error = any(status == "error" for _, status in analysis_status)

    # Prepare metrics
    metrics = {
        "files_in_result": files_count,
        "analysis_status": analysis_status,
        "all_analyses_successful": all_ok,
    }

    # Prepare notes
    notes_lines = []
    for analysis_name, status in analysis_status:
        if status == "ok":
            notes_lines.append(f"✓ {analysis_name}: success")
        elif status == "error":
            notes_lines.append(f"✗ {analysis_name}: failed")
        elif status == "skipped":
            notes_lines.append(f"⊘ {analysis_name}: skipped")

    # Return result
    if has_error:
        return StageResult(
            status="error",
            artifacts={"metrics": metrics, "results": all_results},
            notes="\n".join(notes_lines),
            error="Some analyses failed",
        )
    else:
        return StageResult(
            status="ok", artifacts={"metrics": metrics, "results": all_results}, notes="\n".join(notes_lines)
        )


def auto_fill_missing_dependencies(
    dependencies_csv: Union[str, Path],
    milestones_csv: Union[str, Path],
    commit_dag_path: Union[str, Path],
    logger: Optional[logging.Logger] = None,
    split_metadata_dir: Optional[Union[str, Path]] = None,
    repo_path: Optional[Union[str, Path]] = None,
) -> int:
    """Auto-fill missing dependencies detected by DAG conflict analysis.

    Uses MilestoneDependencyAnalyzer to identify inferred dependencies
    that are missing from dependencies.csv and adds them automatically.

    Args:
        dependencies_csv: Path to dependencies.csv to update
        milestones_csv: Path to milestones.csv
        commit_dag_path: Path to commit DAG JSON
        logger: Optional logger
        split_metadata_dir: Path to split commit metadata directory (optional)
        repo_path: Path to git repository (for precise blame-based edge mapping)

    Returns:
        Number of dependencies added
    """
    import csv
    from io import StringIO

    dependencies_csv = Path(dependencies_csv)
    milestones_csv = Path(milestones_csv)

    if not dependencies_csv.exists() or not milestones_csv.exists():
        return 0

    # Suppress stderr during analyzer execution
    old_stderr = sys.stderr
    try:
        sys.stderr = StringIO()

        analyzer = MilestoneDependencyAnalyzer(
            commit_dag_path=str(commit_dag_path),
            milestone_csv_path=str(milestones_csv),
            dependencies_csv_path=str(dependencies_csv),
            detect_redundant=False,
            split_metadata_dir=str(split_metadata_dir) if split_metadata_dir else None,
            repo_path=str(repo_path) if repo_path else None,
        )
        result = analyzer.enrich_dependencies()
    finally:
        sys.stderr = old_stderr

    missing_deps = result.get("missing_dependencies", [])
    if not missing_deps:
        return 0

    # Read existing dependencies
    existing_deps = set()
    with open(dependencies_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
        for row in rows:
            existing_deps.add((row["prerequisite_id"], row["dependant_id"]))

    # Add missing dependencies
    added = 0
    for prerequisite_id, dependant_id in missing_deps:
        if (prerequisite_id, dependant_id) not in existing_deps:
            rows.append(
                {
                    "prerequisite_id": prerequisite_id,
                    "dependant_id": dependant_id,
                    "type": "FUNC",
                    "strength": "Weak",
                    "rationale": "Auto-inferred from commit-level dependencies",
                    "confidence_score": "0.70",
                }
            )
            added += 1

    if added > 0:
        # Write back
        with open(dependencies_csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        if logger:
            logger.info(f"  Auto-filled {added} missing dependencies")

    return added


def remove_redundant_dependencies(
    dependencies_csv: Union[str, Path],
    milestones_csv: Union[str, Path],
    commit_dag_path: Union[str, Path],
    logger: Optional[logging.Logger] = None,
    split_metadata_dir: Optional[Union[str, Path]] = None,
    repo_path: Optional[Union[str, Path]] = None,
) -> int:
    """Remove redundant dependencies that can be reached via transitive paths.

    Uses MilestoneDependencyAnalyzer to identify dependencies that are redundant
    (i.e., can be reached through other paths) and removes them from dependencies.csv.

    Args:
        dependencies_csv: Path to dependencies.csv to update
        milestones_csv: Path to milestones.csv
        commit_dag_path: Path to commit DAG JSON
        logger: Optional logger
        split_metadata_dir: Path to split commit metadata directory (optional)
        repo_path: Path to git repository (for precise blame-based edge mapping)

    Returns:
        Number of dependencies removed
    """
    import csv
    from io import StringIO

    dependencies_csv = Path(dependencies_csv)
    milestones_csv = Path(milestones_csv)

    if not dependencies_csv.exists() or not milestones_csv.exists():
        return 0

    # Suppress stderr during analyzer execution
    old_stderr = sys.stderr
    try:
        sys.stderr = StringIO()

        analyzer = MilestoneDependencyAnalyzer(
            commit_dag_path=str(commit_dag_path),
            milestone_csv_path=str(milestones_csv),
            dependencies_csv_path=str(dependencies_csv),
            detect_redundant=True,
            split_metadata_dir=str(split_metadata_dir) if split_metadata_dir else None,
            repo_path=str(repo_path) if repo_path else None,
        )
        result = analyzer.enrich_dependencies()
    finally:
        sys.stderr = old_stderr

    redundant_deps = result.get("redundant_dependencies", [])
    if not redundant_deps:
        return 0

    # Convert to set for fast lookup (redundant_deps is [[src, tgt], ...])
    redundant_set = {(dep[0], dep[1]) for dep in redundant_deps}

    # Read existing dependencies and filter out redundant ones
    with open(dependencies_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    # Filter rows
    original_count = len(rows)
    filtered_rows = [row for row in rows if (row["prerequisite_id"], row["dependant_id"]) not in redundant_set]
    removed = original_count - len(filtered_rows)

    if removed > 0:
        # Write back
        with open(dependencies_csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(filtered_rows)

        if logger:
            logger.info(f"  Removed {removed} redundant dependencies")

    return removed


def fix_dependencies(
    dependencies_csv: Union[str, Path],
    milestones_csv: Union[str, Path],
    commit_dag_path: Union[str, Path],
    *,
    add_missing: bool = True,
    remove_redundant: bool = True,
    dry_run: bool = False,
    logger: Optional[logging.Logger] = None,
    split_metadata_dir: Optional[Union[str, Path]] = None,
    repo_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Fix dependencies by adding missing ones and/or removing redundant ones.

    This is a unified function that combines auto_fill_missing_dependencies
    and remove_redundant_dependencies with dry-run support.

    Args:
        dependencies_csv: Path to dependencies.csv
        milestones_csv: Path to milestones.csv
        commit_dag_path: Path to commit DAG JSON
        add_missing: If True, add inferred dependencies that are missing
        remove_redundant: If True, remove dependencies reachable via transitive paths
        dry_run: If True, only report what would be done without modifying files
        logger: Optional logger
        split_metadata_dir: Path to split commit metadata directory (optional)
        repo_path: Path to git repository (for precise blame-based edge mapping)

    Returns:
        Dict with results:
        {
            "status": "ok" | "error",
            "missing_to_add": [(source, target), ...],
            "redundant_to_remove": [(source, target, reason), ...],
            "added": int,  # actual count (0 if dry_run)
            "removed": int,  # actual count (0 if dry_run)
        }
    """
    import csv
    from io import StringIO

    dependencies_csv = Path(dependencies_csv)
    milestones_csv = Path(milestones_csv)

    result = {
        "status": "ok",
        "missing_to_add": [],
        "redundant_to_remove": [],
        "added": 0,
        "removed": 0,
        "dry_run": dry_run,
    }

    if not dependencies_csv.exists():
        result["status"] = "error"
        result["error"] = f"dependencies.csv not found: {dependencies_csv}"
        return result

    if not milestones_csv.exists():
        result["status"] = "error"
        result["error"] = f"milestones.csv not found: {milestones_csv}"
        return result

    if not commit_dag_path or not Path(commit_dag_path).exists():
        result["status"] = "error"
        result["error"] = f"commit_dag not found: {commit_dag_path}"
        return result

    # Run analyzer to detect issues
    old_stderr = sys.stderr
    try:
        sys.stderr = StringIO()

        analyzer = MilestoneDependencyAnalyzer(
            commit_dag_path=str(commit_dag_path),
            milestone_csv_path=str(milestones_csv),
            dependencies_csv_path=str(dependencies_csv),
            detect_redundant=remove_redundant,
            split_metadata_dir=str(split_metadata_dir) if split_metadata_dir else None,
            repo_path=str(repo_path) if repo_path else None,
        )
        analysis = analyzer.enrich_dependencies()
    finally:
        sys.stderr = old_stderr

    # Collect issues
    if add_missing:
        result["missing_to_add"] = analysis.get("missing_dependencies", [])
    if remove_redundant:
        result["redundant_to_remove"] = analysis.get("redundant_dependencies", [])

    # Log findings
    if logger:
        if result["missing_to_add"]:
            logger.info(f"  Found {len(result['missing_to_add'])} missing dependencies to add")
            for dep in result["missing_to_add"]:
                src, tgt = dep[0], dep[1]
                logger.info(f"    + {src} → {tgt}")
        if result["redundant_to_remove"]:
            logger.info(f"  Found {len(result['redundant_to_remove'])} redundant dependencies to remove")
            for dep in result["redundant_to_remove"]:
                src, tgt = dep[0], dep[1]
                logger.info(f"    - {src} → {tgt}")

    if dry_run:
        if logger:
            logger.info("  Dry-run mode: no changes made")
        return result

    # Read existing dependencies
    with open(dependencies_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    existing_deps = {(row["prerequisite_id"], row["dependant_id"]) for row in rows}
    modified = False

    # Add missing dependencies
    if add_missing and result["missing_to_add"]:
        for prerequisite_id, dependant_id in result["missing_to_add"]:
            if (prerequisite_id, dependant_id) not in existing_deps:
                rows.append(
                    {
                        "prerequisite_id": prerequisite_id,
                        "dependant_id": dependant_id,
                        "type": "FUNC",
                        "strength": "Weak",
                        "rationale": "Auto-inferred from commit-level dependencies",
                        "confidence_score": "0.70",
                    }
                )
                result["added"] += 1
                modified = True

    # Remove redundant dependencies
    if remove_redundant and result["redundant_to_remove"]:
        redundant_set = {(dep[0], dep[1]) for dep in result["redundant_to_remove"]}
        original_count = len(rows)
        rows = [row for row in rows if (row["prerequisite_id"], row["dependant_id"]) not in redundant_set]
        result["removed"] = original_count - len(rows)
        if result["removed"] > 0:
            modified = True

    # Write back if modified
    if modified:
        with open(dependencies_csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        if logger:
            logger.info(f"  ✓ Updated dependencies.csv: +{result['added']} added, -{result['removed']} removed")

    return result
