"""Unified post-processing command for Phase 3 (Enrichment) & Phase 4 (Analysis).

This module consolidates the functionality from main_multi_stage.py Phase 3 and Phase 4
into a standalone CLI command, eliminating the redundant stage.py module.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from deepcommit.pipeline.stages.analysis import fix_dependencies, run_comprehensive_analysis
from deepcommit.pipeline.stages.milestone_enrichment import run_milestone_enrichment

from .utils import (
    find_preprocessed_path,
    find_repo_path,
    parse_repo_spec,
)


def setup_logger(verbose: bool = False, log_file: Path | None = None) -> logging.Logger:
    """Setup logger with optional file output."""
    logger = logging.getLogger("deepcommit.postprocess")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    # Clear existing handlers to avoid duplicates
    logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler()
    console_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%H:%M:%S")
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File handler if log_file is provided
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        file_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

    return logger


def run_postprocess(
    result_dir: Path,
    repo_spec: str,
    *,
    data_root: Path,
    run_enrich: bool = True,
    run_analyze: bool = True,
    repo_src_dirs: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    test_patterns: list[str] | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Run post-processing (Phase 3 + Phase 4).

    This function replicates the logic from main_multi_stage.py Phase 3 and Phase 4,
    providing a unified interface for standalone execution.

    Args:
        result_dir: Path to the trial result directory containing milestones.csv and dependencies.csv
        repo_spec: Repository specification in format 'owner_repo:start_end'
        data_root: Root directory for DATA (contains github_data/, result/, etc.)
        run_enrich: If True, run Phase 3 (Milestone Enrichment)
        run_analyze: If True, run Phase 4 (Analysis & Evaluation)
        repo_src_dirs: Source directories to filter (e.g., ["src", "lib"])
        exclude_patterns: Glob patterns to exclude (e.g., ["**/test/**"])
        overwrite: If True, overwrite existing analysis results
        dry_run: If True, only output to console without saving files
        verbose: If True, enable verbose logging

    Returns:
        Dict containing results from both phases:
        {
            "enrich": {...} or None,
            "analyze": {...} or None,
            "status": "ok" | "error",
            "error": str (if status is "error")
        }
    """
    repo_id, time_span = parse_repo_spec(repo_spec)
    result_dir = result_dir.expanduser().resolve()

    # Validate result directory
    if not result_dir.exists():
        return {
            "status": "error",
            "error": f"Result directory not found: {result_dir}",
            "enrich": None,
            "analyze": None,
        }

    # Setup paths
    milestones_csv = result_dir / "milestones.csv"
    dependencies_csv = result_dir / "dependencies.csv"
    analysis_output_dir = result_dir / "analysis"

    # Find repository path
    # Priority: trial's repo_data (has split commits) > original repo
    repo_path = _detect_trial_repo_path(result_dir, data_root)
    if repo_path is None:
        repo_path = find_repo_path(repo_id, data_root)
    if repo_path is None:
        return {
            "status": "error",
            "error": f"Could not find repository for '{repo_id}'. Ensure DATA/github_data/repos contains it.",
            "enrich": None,
            "analyze": None,
        }

    # Find preprocessed path
    preprocessed_path = find_preprocessed_path(repo_id, time_span, data_root)
    if not preprocessed_path.exists():
        return {
            "status": "error",
            "error": f"Preprocessed data not found: {preprocessed_path}",
            "enrich": None,
            "analyze": None,
        }

    # Resolve commit_dag_path (same logic as main_multi_stage.py)
    commit_dag_path = _resolve_commit_dag_path(preprocessed_path)

    # Auto-detect split metadata directory
    split_metadata_dir = _detect_split_metadata_dir(result_dir)

    # Setup logger
    log_file = analysis_output_dir / "postprocess.log"
    logger = setup_logger(verbose, log_file=log_file)

    logger.info("=" * 70)
    logger.info("Running post-processing for: %s", result_dir.name)
    logger.info("  Repository: %s", repo_path)
    logger.info("  Preprocessed: %s", preprocessed_path)
    if split_metadata_dir:
        logger.info("  Split metadata: %s", split_metadata_dir)
    if repo_src_dirs:
        logger.info("  Source dirs: %s", repo_src_dirs)
    if exclude_patterns:
        logger.info("  Exclude patterns: %s", exclude_patterns)
    logger.info("=" * 70)

    results: dict[str, Any] = {
        "enrich": None,
        "analyze": None,
        "status": "ok",
    }

    # Phase 3: Milestone Enrichment
    if run_enrich:
        logger.info("\nMilestone Enrichment")
        if not milestones_csv.exists():
            logger.warning("  Skipping: milestones.csv not found at %s", milestones_csv)
        else:
            try:
                enrich_result = run_milestone_enrichment(
                    milestones_csv=milestones_csv,
                    repo_path=repo_path,
                    logger=logger,
                    repo_src_dirs=repo_src_dirs,
                    exclude_patterns=exclude_patterns,
                    test_patterns=test_patterns,
                )
                results["enrich"] = enrich_result
                if enrich_result.get("status") == "ok":
                    logger.info("  Enriched %d milestones", enrich_result.get("milestones_enriched", 0))
                else:
                    logger.warning("  Enrichment failed: %s", enrich_result.get("error", "Unknown error"))
            except Exception as e:
                logger.error("  Enrichment error: %s", e, exc_info=True)
                results["enrich"] = {"status": "error", "error": str(e)}

    # Phase 4: Analysis & Evaluation
    if run_analyze:
        logger.info("\nAnalysis & Evaluation")
        if not milestones_csv.exists():
            logger.warning("  Skipping: milestones.csv not found")
        elif not dependencies_csv.exists():
            logger.warning("  Skipping: dependencies.csv not found")
        else:
            try:
                # Combine exclude_patterns and test_patterns for consistent filtering
                # (analysis needs to exclude test files just like enrichment does)
                combined_exclude = list(exclude_patterns or [])
                if test_patterns:
                    combined_exclude.extend(test_patterns)

                analyze_result = run_comprehensive_analysis(
                    milestones_csv=milestones_csv,
                    dependencies_csv=dependencies_csv,
                    repo_path=repo_path,
                    preprocessed_path=preprocessed_path,
                    commit_dag_path=commit_dag_path,
                    output_dir=analysis_output_dir,
                    trial_name=result_dir.name,
                    repo_id=repo_id,
                    skip_if_exists=not overwrite and not dry_run,
                    compact_metrics=True,
                    dry_run=dry_run,
                    logger=logger,
                    split_metadata_dir=split_metadata_dir,
                    repo_src_dirs=repo_src_dirs,
                    exclude_patterns=combined_exclude if combined_exclude else None,
                )
                results["analyze"] = analyze_result

                if analyze_result.get("skipped"):
                    logger.info("  Skipped: results already exist (use --overwrite to re-run)")
                elif analyze_result.get("status") == "ok":
                    logger.info("  Analysis completed successfully")
                    # Log analysis summary
                    statuses = analyze_result.get("analysis_status", [])
                    success_count = sum(1 for _, status in statuses if status == "ok")
                    error_count = sum(1 for _, status in statuses if status == "error")
                    skip_count = sum(1 for _, status in statuses if status == "skipped")
                    logger.info(
                        "  Summary: %d succeeded, %d failed, %d skipped", success_count, error_count, skip_count
                    )
                else:
                    logger.warning("  Analysis failed: %s", analyze_result.get("error", "Unknown error"))
            except Exception as e:
                logger.error("  Analysis error: %s", e, exc_info=True)
                results["analyze"] = {"status": "error", "error": str(e)}

    logger.info("\n" + "=" * 70)
    logger.info("Post-processing complete")
    logger.info("=" * 70)

    return results


def _resolve_commit_dag_path(preprocessed_path: Path) -> Path | None:
    """Resolve commit_dag path from preprocessed directory.

    Tries multiple candidate paths in order of preference (enhanced format first).

    Args:
        preprocessed_path: Path to preprocessed data directory

    Returns:
        Path to commit_dag file or None if not found
    """
    if not preprocessed_path or not preprocessed_path.exists():
        return None

    # Try enhanced format first (required for conflict analysis)
    candidates = [
        preprocessed_path / "dag" / "commit_dag_v3_ctx_3_pctx_3_enhanced.json",
        preprocessed_path / "dag" / "commit_dag_v3_ctx_3_pctx_3.json",
        preprocessed_path / "dag" / "commit_dag.json",
        preprocessed_path / "commit_dag.json",  # Legacy location
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return None


def _detect_trial_repo_path(result_dir: Path, data_root: Path) -> Path | None:
    """Auto-detect trial-specific repo_data directory.

    When stage4 creates split commits, they exist only in the trial's cloned repo_data.
    This function finds the trial's repo_data if it exists.

    Args:
        result_dir: Path to trial result directory (e.g., DATA/result/<trial_name>)
        data_root: DATA root directory

    Returns:
        Path to trial's repo_data if found, None otherwise
    """
    trial_name = result_dir.name
    trial_space_root = data_root / "trial_space"

    # Check trial_space/<trial_name>/repo_data
    trial_repo = trial_space_root / trial_name / "repo_data"
    if trial_repo.exists() and trial_repo.is_dir():
        # Verify it's a git repo
        if (trial_repo / ".git").exists() or (trial_repo / "HEAD").exists():
            return trial_repo

    return None


def _detect_split_metadata_dir(result_dir: Path) -> Path | None:
    """Auto-detect split commit metadata directory from trial result directory.

    Looks for commit_splits directory in common locations:
    - result_dir/stage4_decomp/output/commit_splits (trial space structure)
    - trial_space/<trial_name>/stage4_decomp/output/commit_splits (parallel trial_space)

    Args:
        result_dir: Path to trial result directory

    Returns:
        Path to commit_splits directory if found, None otherwise
    """
    # Candidate paths to check
    # NOTE: Only detect "commit_splits", NOT "commit_splits_reference"
    # commit_splits_reference is for reference only (suggesting possible splits)
    # but milestones may intentionally keep original commits unsplit
    candidates = [
        result_dir / "stage4_decomp" / "output" / "commit_splits",
        result_dir / "commit_splits",
    ]

    # Also check parallel trial_space directory (same trial name, different root)
    # e.g., result/ -> trial_space/
    result_dir_name = result_dir.name
    data_root = result_dir.parent.parent  # the trial data root holding result/ and trial_space/
    trial_space_candidate = data_root / "trial_space" / result_dir_name / "stage4_decomp" / "output" / "commit_splits"
    candidates.append(trial_space_candidate)

    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            # Check if there are any split-* subdirectories or SHA-named directories with METADATA.json
            split_dirs = list(candidate.glob("split-*"))
            # Also check for SHA-only named directories (7-40 hex chars) with METADATA.json
            for subdir in candidate.iterdir():
                if subdir.is_dir() and not subdir.name.startswith("split-"):
                    # Check if it looks like a SHA and has METADATA.json
                    if len(subdir.name) >= 7 and all(c in "0123456789abcdef" for c in subdir.name.lower()):
                        if (subdir / "METADATA.json").exists():
                            split_dirs.append(subdir)
            if split_dirs:
                return candidate

    return None


def run_postprocess_cli(
    result_dir: Path,
    repo_id: str,
    *,
    data_root: Path,
    enrich_only: bool = False,
    analyze_only: bool = False,
    repo_src_dirs: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    test_patterns: list[str] | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """CLI entry point for postprocess command.

    Args:
        result_dir: Path to trial result directory
        repo_id: Repository specification
        data_root: DATA root directory
        enrich_only: If True, only run enrichment
        analyze_only: If True, only run analysis
        repo_src_dirs: Source directories
        exclude_patterns: Exclude patterns
        test_patterns: Test file patterns
        overwrite: Overwrite existing results
        dry_run: If True, only output to console without saving files
        verbose: Enable verbose logging

    Returns:
        Exit code (0 for success, 1 for error)
    """
    # Determine which phases to run
    run_enrich = not analyze_only
    run_analyze = not enrich_only

    try:
        results = run_postprocess(
            result_dir,
            repo_id,
            data_root=data_root,
            run_enrich=run_enrich,
            run_analyze=run_analyze,
            repo_src_dirs=repo_src_dirs,
            exclude_patterns=exclude_patterns,
            test_patterns=test_patterns,
            overwrite=overwrite,
            dry_run=dry_run,
            verbose=verbose,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Check for top-level errors
    if results.get("status") == "error":
        print(f"Error: {results.get('error', 'Unknown error')}", file=sys.stderr)
        return 1

    # Print summary
    print("\nPost-processing Results:")
    print("-" * 40)

    has_errors = False

    # Enrichment summary
    if run_enrich:
        enrich = results.get("enrich")
        if enrich is None:
            print("  Enrichment: skipped (missing files)")
        elif enrich.get("status") == "ok":
            updated = enrich.get("milestones_enriched", 0)
            print(f"  Enrichment: OK ({updated} milestones updated)")
            missing = enrich.get("missing_commits", [])
            if missing:
                print(f"    Warning: {len(missing)} commits not found in repo")
        else:
            print(f"  Enrichment: FAILED - {enrich.get('error', 'Unknown error')}")
            has_errors = True

    # Analysis summary
    if run_analyze:
        analyze = results.get("analyze")
        if analyze is None:
            print("  Analysis: skipped (missing files)")
        elif analyze.get("skipped"):
            print(f"  Analysis: skipped (results exist at {analyze.get('output_dir', 'N/A')})")
        elif analyze.get("status") == "ok":
            statuses = analyze.get("analysis_status", [])
            success_count = sum(1 for _, status in statuses if status == "ok")
            error_count = sum(1 for _, status in statuses if status == "error")
            print(f"  Analysis: OK ({success_count} analyses succeeded, {error_count} failed)")
            print(f"    Output: {analyze.get('output_dir', 'N/A')}")
            if error_count > 0:
                has_errors = True
        else:
            print(f"  Analysis: FAILED - {analyze.get('error', 'Unknown error')}")
            has_errors = True

    return 1 if has_errors else 0


def run_fix(
    result_dir: Path,
    repo_spec: str,
    *,
    data_root: Path,
    add_missing: bool = True,
    remove_redundant: bool = True,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Fix dependencies by adding missing ones and removing redundant ones.

    Args:
        result_dir: Path to the trial result directory
        repo_spec: Repository specification in format 'owner_repo:start_end'
        data_root: Root directory for DATA
        add_missing: If True, add inferred dependencies that are missing
        remove_redundant: If True, remove dependencies reachable via transitive paths
        dry_run: If True, only report what would be done without modifying files
        verbose: If True, enable verbose logging

    Returns:
        Dict containing fix results
    """
    repo_id, time_span = parse_repo_spec(repo_spec)
    result_dir = result_dir.expanduser().resolve()

    # Validate result directory
    if not result_dir.exists():
        return {
            "status": "error",
            "error": f"Result directory not found: {result_dir}",
        }

    # Setup paths
    milestones_csv = result_dir / "milestones.csv"
    dependencies_csv = result_dir / "dependencies.csv"

    # Find preprocessed path for commit_dag
    preprocessed_path = find_preprocessed_path(repo_id, time_span, data_root)
    if not preprocessed_path.exists():
        return {
            "status": "error",
            "error": f"Preprocessed data not found: {preprocessed_path}",
        }

    commit_dag_path = _resolve_commit_dag_path(preprocessed_path)
    if not commit_dag_path:
        return {
            "status": "error",
            "error": "commit_dag.json not found in preprocessed directory",
        }

    # Auto-detect split metadata directory
    split_metadata_dir = _detect_split_metadata_dir(result_dir)

    # Setup logger
    logger = setup_logger(verbose)

    logger.info("=" * 70)
    logger.info("Fix Dependencies")
    logger.info("  Trial: %s", result_dir.name)
    logger.info("  Dependencies: %s", dependencies_csv)
    logger.info("  Commit DAG: %s", commit_dag_path)
    if split_metadata_dir:
        logger.info("  Split metadata: %s", split_metadata_dir)
    if dry_run:
        logger.info("  Mode: dry-run (no changes will be made)")
    logger.info("=" * 70)

    # Run fix
    fix_result = fix_dependencies(
        dependencies_csv=dependencies_csv,
        milestones_csv=milestones_csv,
        commit_dag_path=commit_dag_path,
        add_missing=add_missing,
        remove_redundant=remove_redundant,
        dry_run=dry_run,
        logger=logger,
        split_metadata_dir=split_metadata_dir,
    )

    logger.info("=" * 70)
    logger.info("Fix completed")
    logger.info("=" * 70)

    return fix_result


def run_fix_cli(
    result_dir: Path,
    repo_id: str,
    *,
    data_root: Path,
    add_missing: bool = True,
    remove_redundant: bool = True,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """CLI entry point for fix command.

    Args:
        result_dir: Path to trial result directory
        repo_id: Repository specification
        data_root: DATA root directory
        add_missing: Add missing dependencies
        remove_redundant: Remove redundant dependencies
        dry_run: Only show what would be done
        verbose: Enable verbose logging

    Returns:
        Exit code (0 for success, 1 for error)
    """
    try:
        result = run_fix(
            result_dir,
            repo_id,
            data_root=data_root,
            add_missing=add_missing,
            remove_redundant=remove_redundant,
            dry_run=dry_run,
            verbose=verbose,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if result.get("status") == "error":
        print(f"Error: {result.get('error', 'Unknown error')}", file=sys.stderr)
        return 1

    # Print summary
    print("\nFix Dependencies Results:")
    print("-" * 40)

    missing_count = len(result.get("missing_to_add", []))
    redundant_count = len(result.get("redundant_to_remove", []))
    added = result.get("added", 0)
    removed = result.get("removed", 0)

    if dry_run:
        print(f"  Would add: {missing_count} missing dependencies")
        print(f"  Would remove: {redundant_count} redundant dependencies")
        print("  (dry-run mode, no changes made)")
    else:
        print(f"  Added: {added} missing dependencies")
        print(f"  Removed: {removed} redundant dependencies")
        if added > 0 or removed > 0:
            print("  ✓ dependencies.csv updated")
        else:
            print("  No changes needed")

    return 0
