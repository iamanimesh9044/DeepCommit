from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

from .checkpoint import CheckpointManager
from .context import TrialContext
from .sanity import sanity_check
from .types import StageResult
from .utils.io import ensure_dir, setup_pipeline_logger

# Stage classes cache
STAGE_CLASSES: Dict[str, Any] = {}


def _restore_global_files_from_stages(result_root: Path, workspace_root: Path, logger) -> None:
    """Restore global result files from stage directories if missing.

    This is useful for rerun scenarios where stages were skipped but their
    outputs are needed for post-processing phases.

    IMPORTANT:
    1. First checks workspace stage output directories (fresh data from agent)
    2. Falls back to result stage directories (archived data)
    3. Searches in REVERSE order (stage4 before stage3, etc.)

    Args:
        result_root: Path to result directory
        workspace_root: Path to workspace root directory
        logger: Pipeline logger instance
    """
    files_to_restore = ["milestones.csv", "dependencies.csv"]

    for filename in files_to_restore:
        global_file = result_root / filename
        if global_file.exists():
            continue  # Already exists, skip

        found = False

        # First: Search in workspace stage output directories (fresh data)
        # These have the most recent data from agent execution
        for stage_dir in sorted(workspace_root.glob("stage*"), reverse=True):
            if not stage_dir.is_dir():
                continue
            output_dir = stage_dir / "output"
            if not output_dir.exists():
                continue

            source_file = output_dir / filename
            if source_file.exists():
                logger.info(f"Restoring {filename} from {stage_dir.name}/output/...")
                shutil.copy2(source_file, global_file)
                logger.info(f"  ✓ Restored {filename}")
                found = True
                break

        if found:
            continue

        # Fallback: Search in result stage directories (archived data)
        for stage_dir in sorted(result_root.glob("stage*"), reverse=True):
            if not stage_dir.is_dir():
                continue

            source_file = stage_dir / filename
            if source_file.exists():
                logger.info(f"Restoring {filename} from result/{stage_dir.name}/...")
                shutil.copy2(source_file, global_file)
                logger.info(f"  ✓ Restored {filename}")
                break


def _workspace_resolver(ctx: TrialContext):
    """Resolver for a multi-repo trial, None for a classic single-repo trial."""
    if not ctx.is_multi_repo:
        return None
    from deepcommit.multirepo.resolver import WorkspaceResolver

    return WorkspaceResolver.from_dir(ctx.get_workspace_dir())


def _module_name_to_class_name(module_name: str) -> str:
    """Convert module name to expected class name.

    Examples:
        stage1_seed_discovery -> Stage1SeedDiscovery
        stage2_milestone_consolidation -> Stage2MilestoneConsolidation

    Args:
        module_name: Module name (without .py extension)

    Returns:
        Expected class name in PascalCase
    """
    # Split by underscore and capitalize each part
    parts = module_name.split("_")
    return "".join(part.capitalize() for part in parts)


def load_stage_class(stage_impl: str):
    """Dynamically load stage implementation class.

    Args:
        stage_impl: Stage implementation filename (e.g., "stage1_seed_discovery.py")

    Returns:
        The stage class (subclass of BaseStage)
    """
    if stage_impl in STAGE_CLASSES:
        return STAGE_CLASSES[stage_impl]

    # stage_impl = "stage1_seed_discovery.py"
    module_name = stage_impl.replace(".py", "")

    # Import from deepcommit.pipeline.stages_multi
    try:
        module = __import__(f"deepcommit.pipeline.stages_multi.{module_name}", fromlist=[module_name])
    except ImportError as e:
        raise ImportError(
            f"Failed to import stage module '{module_name}' from deepcommit.pipeline.stages_multi. Error: {e}"
        ) from e

    # Get the stage class from the module
    class_name = _module_name_to_class_name(module_name)

    if not hasattr(module, class_name):
        raise AttributeError(
            f"Module '{module_name}' does not have expected class '{class_name}'. Available attributes: {dir(module)}"
        )

    stage_class = getattr(module, class_name)
    STAGE_CLASSES[stage_impl] = stage_class
    return stage_class


def load_pipeline_config(config_file: str) -> List[Dict[str, str]]:
    """Load multi-stage pipeline configuration.

    Args:
        config_file: Pipeline config filename (e.g., "multi_stage_pipeline.yaml")

    Returns:
        List of stage configurations
    """
    config_path = Path(__file__).parent / "configs" / config_file
    if not config_path.exists():
        raise FileNotFoundError(f"Pipeline config not found: {config_path}")

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    stages: List[Dict[str, str]] = config.get("stages", [])
    if not stages:
        raise ValueError(f"No stages defined in {config_file}")

    return stages


def link_previous_stage_outputs(ctx: TrialContext, prev_stage_id: Optional[str], logger) -> Set[str]:
    """Link output files from previous stages to current stage input.

    Automatically links files with matching variable names between:
    - Any completed stage's output config (searched in reverse order)
    - Current stage input config

    This allows stage4 to link milestone_csv from stage2 even if stage3 doesn't output it.

    Args:
        ctx: Trial context with current_stage_id set
        prev_stage_id: Previous stage ID (used to determine which stages to search)
        logger: Logger instance

    Returns:
        Set of variable names that were linked
    """
    if not prev_stage_id:
        logger.info("  No previous stage, skipping auto-link")
        return set()

    # ✅ Use ctx to get configs instead of loading files
    current_config = ctx._get_stage_config()
    current_input_vars = current_config.get_input_vars()

    if not current_input_vars:
        logger.info("  No input variables to link")
        return set()

    # Get all executed stages (in order), excluding current stage
    # This is important for rerun scenarios where current stage was added to
    # executed_stages before linking (by set_current_stage)
    executed_stages = [s for s in ctx.executed_stages if s != ctx.current_stage_id]
    if not executed_stages:
        logger.info("  No executed stages to link from")
        return set()

    # Track which variables still need to be linked
    unlinked_vars = current_input_vars.copy()
    linked_vars = set()
    current_input_dir = ctx.get_stage_input_dir()

    # Search executed stages in reverse order (most recent first)
    for stage_id in reversed(executed_stages):
        if not unlinked_vars:
            break  # All variables linked

        try:
            stage_config = ctx._get_stage_config(stage_id)
            stage_output_vars = stage_config.get_output_vars()
        except Exception:
            continue

        # Find matching variables that haven't been linked yet
        matching_vars = unlinked_vars & stage_output_vars

        if not matching_vars:
            continue

        logger.info(f"  Found {len(matching_vars)} matching variable(s): {', '.join(sorted(matching_vars))}")

        # Link files from this stage
        stage_output_dir = ctx.get_stage_output_dir(stage_id)

        for var_name in matching_vars:
            src_filename = stage_config.get_output_filename(var_name)
            dst_filename = current_config.get_input_filename(var_name)

            if not src_filename or not dst_filename:
                continue

            src = stage_output_dir / src_filename
            dst = current_input_dir / dst_filename

            if not src.exists():
                logger.warning(f"  ⚠ Source file not found: {src} (skipping {var_name})")
                continue

            if dst.exists():
                logger.info(f"  ○ Already exists: {dst_filename} (skipping)")
                linked_vars.add(var_name)
                unlinked_vars.discard(var_name)
                continue

            # Create symlink
            dst.symlink_to(src)
            logger.info(f"  ✓ Linked: {var_name} ({src_filename}) from {stage_id}")
            linked_vars.add(var_name)
            unlinked_vars.discard(var_name)

    # Log any unlinked variables
    if unlinked_vars:
        logger.warning(f"  ⚠ Could not find sources for: {', '.join(sorted(unlinked_vars))}")

    return linked_vars


def parse_args(argv: List[str]) -> argparse.Namespace:
    """Parse command-line arguments for multi-stage pipeline."""
    p = argparse.ArgumentParser(description="Deepcommit multi-stage pipeline")
    p.add_argument("--trial-name", type=str, required=True)
    p.add_argument("--workspace-root", type=str, required=True)
    p.add_argument("--repo-id", type=str, required=True)
    p.add_argument(
        "--config-file",
        type=str,
        default="multi_stage_pipeline.yaml",
        help="Pipeline config file (default: multi_stage_pipeline.yaml)",
    )
    p.add_argument("--agents", type=str, default="claude", help="Comma-separated agent list")
    p.add_argument(
        "--tool-list", type=str, default="all", help="MCP tools list (currently unused, reserved for future)"
    )
    p.add_argument("--code-commit-sha", type=str, required=True)
    p.add_argument("--max-retries", type=int, default=2, help="Maximum retries per stage")

    return p.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    """Multi-stage pipeline main controller."""
    ns = parse_args(sys.argv[1:] if argv is None else argv)

    # Load repo_src_dirs from environment variable (set by CLI)
    repo_src_dirs = None
    repo_src_dirs_env = os.environ.get("REPO_SRC_DIRS")
    if repo_src_dirs_env:
        try:
            import json

            repo_src_dirs = json.loads(repo_src_dirs_env)
        except Exception:
            pass  # Fall back to default ["src"]

    def _parse_list_env(name: str) -> Optional[List[str]]:
        """Parse environment variable as JSON list or comma-separated string."""
        raw = os.environ.get(name)
        if not raw:
            return None
        try:
            import json

            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(p) for p in parsed if str(p).strip()]
            return [str(parsed)]
        except Exception:
            return [p.strip() for p in raw.split(",") if p.strip()]

    # Load test_dirs and exclude patterns from environment variables (set by CLI)
    test_dirs = _parse_list_env("REPO_TEST_DIRS")
    exclude = _parse_list_env("REPO_EXCLUDE")

    # Load build environment config (for Stage 4 commit split verification)
    base_image_name = os.environ.get("BASE_IMAGE_NAME")
    build_command = os.environ.get("BUILD_COMMAND")

    # Create global context (multi-stage mode)
    ctx = TrialContext(
        trial_name=ns.trial_name,
        repo_id=ns.repo_id,
        prompt_file=ns.config_file,  # Store config file name here
        agents=ns.agents,
        tool_list=ns.tool_list,
        code_commit_sha=ns.code_commit_sha,
        max_retries=ns.max_retries,
        workspace_root=ns.workspace_root,
        is_multi_stage=True,  # Enable multi-stage mode
        repo_src_dirs=repo_src_dirs,
        test_dirs=test_dirs,
        exclude=exclude,
        base_image_name=base_image_name,
        build_command=build_command,
        workspace_dir=os.environ.get("DEEPCOMMIT_WORKSPACE_DIR") or None,
        workspace_id=ns.repo_id if os.environ.get("DEEPCOMMIT_WORKSPACE_DIR") else None,
    )

    # Setup global logs directory (result_path already created by CLI)
    ensure_dir(ctx.logs_path)

    # Setup global pipeline logger with file I/O capabilities
    # All log messages go to logs/pipeline.log
    # File outputs (save_json/save_text) are stage-specific
    logger = setup_pipeline_logger(
        log_dir=ctx.logs_path,
        workspace_root=ctx.workspace_root,
        is_multi_stage=True,
        current_stage_id="global",  # Initial stage for global operations
    )
    logger.info("=" * 70)
    logger.info("Multi-Stage Pipeline Execution")
    logger.info("=" * 70)

    # Log Python environment information
    logger.info("Python Environment:")
    logger.info(f"  Python executable: {sys.executable}")
    logger.info(f"  Python version: {sys.version.split()[0]}")
    logger.info(f"  Virtual env: {os.environ.get('VIRTUAL_ENV', 'None')}")
    logger.info(f"  Working directory: {Path.cwd()}")
    logger.info(f"  sys.path[0]: {sys.path[0] if sys.path else 'N/A'}")
    logger.info("")

    logger.info(ctx.get_info_string())

    # Initialize checkpoint manager
    checkpoint = CheckpointManager(Path(ctx.logs_path) / "checkpoint.json")
    logger.info(f"Checkpoint: {checkpoint.checkpoint_file}")

    # Phase 0: Global Sanity Check (always run, never skip)
    logger.info("\n[Phase 0] Global Sanity Check")
    res_sanity = sanity_check(ctx, logger)
    ctx.set_stage_results("Phase 0: Sanity Check", res_sanity)
    if res_sanity.status == "error":
        logger.error("Sanity check failed")
        checkpoint.mark_phase_failed("phase0_sanity", res_sanity.error or "Unknown error")
        return 2
    checkpoint.mark_phase_completed("phase0_sanity")
    logger.info("✓ Sanity check passed")

    # Phase 1: Global Preprocessing (reserved for future)
    logger.info("\n[Phase 1] Global Preprocessing")
    logger.info("  (Currently skipped, reserved for future implementation)")

    # Phase 2: Multi-Stage Agent Execution
    logger.info("\n[Phase 2] Multi-Stage Agent Execution")

    # Load pipeline configuration
    try:
        stages = load_pipeline_config(ns.config_file)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as e:
        logger.error(f"Failed to load pipeline config '{ns.config_file}': {e}")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error loading pipeline config: {e}", exc_info=True)
        return 1

    logger.info(f"Loaded pipeline config: {ns.config_file}")
    logger.info(f"Total stages: {len(stages)}")

    for idx, stage_info in enumerate(stages, 1):
        stage_id = stage_info["stage_id"]
        stage_impl = stage_info["stage_impl"]

        # Reset logger to global for loop control messages
        logger.set_stage_id("global")

        logger.info(f"\n{'=' * 70}")
        logger.info(f"Stage {idx}/{len(stages)}: {stage_id}")
        logger.info(f"Implementation: {stage_impl}")
        logger.info(f"{'=' * 70}")

        # Check if already completed
        if checkpoint.is_stage_completed(stage_id):
            logger.info(f"✓ {stage_id} already completed, skipping")
            ctx.set_current_stage(stage_id)  # Still set for context continuity
            continue

        # Set current stage in context and logger
        ctx.set_current_stage(stage_id)
        logger.set_stage_id(stage_id)

        # Auto-link matching outputs from previous stage
        prev_stage_id = ctx.get_prev_stage()
        if prev_stage_id:
            logger.info(f"Linking outputs from {prev_stage_id}...")
            try:
                link_previous_stage_outputs(ctx=ctx, prev_stage_id=prev_stage_id, logger=logger)
            except Exception as e:
                logger.warning(f"  Failed to link previous outputs: {e}")

        # Load and instantiate stage class
        try:
            stage_class = load_stage_class(stage_impl)
            stage_instance = stage_class(ctx=ctx, logger=logger)
        except Exception as e:
            logger.error(f"✗ Failed to load/instantiate stage: {e}", exc_info=True)
            error_msg = f"Stage initialization failed: {e}"
            ctx.set_stage_results(f"Stage {idx}: {stage_id}", StageResult(status="error", error=error_msg))
            checkpoint.mark_stage_failed(stage_id, error_msg)
            return 1

        # If previous run failed, try validating first
        if checkpoint.get_stage_status(stage_id) == "failed":
            logger.info("Previous run failed, attempting validation before re-execution...")
            try:
                validation_error = stage_instance.validate()
                if validation_error is None:
                    logger.info("✓ Validation passed, marking as completed")
                    checkpoint.mark_stage_completed(stage_id)
                    ctx.set_stage_results(
                        f"Stage {idx}: {stage_id}", StageResult(status="ok", notes="Validated from previous run")
                    )
                    continue
                else:
                    logger.info(f"Validation failed: {validation_error}, re-executing...")
            except Exception as e:
                logger.warning(f"Validation check failed: {e}, proceeding with execution...")

        # Execute stage
        logger.info("Executing stage...")
        try:
            stage_result = stage_instance.run_stage()
        except Exception as e:
            error_msg = str(e)
            logger.error(f"✗ Stage {stage_id} failed with exception: {error_msg}", exc_info=True)
            ctx.set_stage_results(f"Stage {idx}: {stage_id}", StageResult(status="error", error=error_msg))
            checkpoint.mark_stage_failed(stage_id, error_msg)
            return 1

        # Process stage result
        ctx.set_stage_results(f"Stage {idx}: {stage_id}", stage_result)

        if stage_result.status == "error":
            logger.error(f"✗ Stage {stage_id} failed: {stage_result.error}")
            checkpoint.mark_stage_failed(stage_id, stage_result.error or "Unknown error")
            return 1

        # Mark as completed
        checkpoint.mark_stage_completed(stage_id)
        logger.info(f"✓ Stage {stage_id} completed successfully")
        if stage_result.notes:
            logger.info(f"  Notes: {stage_result.notes}")

    # Mark Phase 2 as completed
    checkpoint.mark_phase_completed("phase2_multi_stage")

    # Reset logger to global for global post-processing phases
    logger.set_stage_id("global")

    # Restore global files from stage results if missing (for rerun scenarios)
    logger.info("\nRestoring global files from stage results if needed...")
    _restore_global_files_from_stages(Path(ctx.to_abs_path(ctx.result_path)), Path(ctx.workspace_root), logger)

    # Phase 3: Global Post-Processing - Milestone Enrichment (non-blocking)
    logger.info("\n[Phase 3] Global Post-Processing: Milestone Enrichment")
    if checkpoint.is_phase_completed("phase3_enrichment"):
        logger.info("✓ Phase 3 already completed, skipping")
    else:
        try:
            from .stages.milestone_enrichment import run_milestone_enrichment

            # Use result directory directly instead of ctx.get_output()
            # because Phase 3 runs after stages complete, but ctx.current_stage_id
            # is still set to the last stage, causing get_output() to return
            # the stage output path instead of result path
            result_path = Path(ctx.to_abs_path(ctx.result_path))
            milestones_csv = result_path / "milestones.csv"
            if milestones_csv.exists():
                repo_src_dirs_value = ctx.get_repo_src_dirs()
                exclude_patterns_value = ctx.get_repo_exclude_patterns()
                logger.info(f"Running enrichment with repo_src_dirs: {repo_src_dirs_value}")
                enrich_result = run_milestone_enrichment(
                    milestones_csv=milestones_csv,
                    repo_path=Path(ctx.get_repo()),
                    logger=logger,
                    repo_src_dirs=repo_src_dirs_value,
                    exclude_patterns=exclude_patterns_value,
                    resolver=_workspace_resolver(ctx),
                )
                if enrich_result.get("status") == "ok":
                    checkpoint.mark_phase_completed("phase3_enrichment")
                    logger.info("  ✓ Milestone enrichment completed")
                else:
                    error = enrich_result.get("error", "Unknown error")
                    checkpoint.mark_phase_failed("phase3_enrichment", str(error))
                    logger.warning(f"  ⚠ Enrichment failed: {error}")
            else:
                error = f"milestones.csv not found at {milestones_csv}"
                checkpoint.mark_phase_failed("phase3_enrichment", error)
                logger.warning(f"  ⚠ {error}, skipping enrichment")
        except Exception as e:
            checkpoint.mark_phase_failed("phase3_enrichment", str(e))
            logger.warning(f"  ⚠ Enrichment error: {e}", exc_info=True)

    # Phase 4: Analysis & Evaluation (non-blocking)
    logger.info("\n[Phase 4] Analysis & Evaluation")
    analysis_results = {}
    if checkpoint.is_phase_completed("phase4_analysis"):
        logger.info("✓ Phase 4 already completed, skipping")
    else:
        try:
            from .stages.analysis import run_comprehensive_analysis

            milestones_csv = Path(ctx.get_output("milestone_csv"))
            dependencies_csv = Path(ctx.get_output("dependencies_csv"))
            analysis_output_dir = Path(ctx.get_output("analysis_dir"))

            # Determine split_metadata_dir from stage4_decomp output
            split_metadata_dir = None
            stage4_output = ctx.get_stage_output_dir("stage4_decomp")
            if stage4_output.exists():
                commit_splits_dir = stage4_output / "commit_splits"
                if commit_splits_dir.exists():
                    split_metadata_dir = commit_splits_dir
                    logger.info(f"  Found split commit metadata: {split_metadata_dir}")

            if milestones_csv.exists() and dependencies_csv.exists():
                analysis_results = run_comprehensive_analysis(
                    milestones_csv=milestones_csv,
                    dependencies_csv=dependencies_csv,
                    repo_path=ctx.get_repo(),
                    preprocessed_path=ctx.get_preprocess(),
                    commit_dag_path=ctx.get_input("commit_dag"),
                    output_dir=analysis_output_dir,
                    trial_name=ctx.trial_name,
                    repo_id=ctx.repo_id,
                    skip_if_exists=False,
                    compact_metrics=True,
                    logger=logger,
                    split_metadata_dir=split_metadata_dir,
                    workspace_dir=ctx.get_workspace_dir() if ctx.is_multi_repo else None,
                )
                if analysis_results.get("status") == "ok":
                    checkpoint.mark_phase_completed("phase4_analysis")
                    logger.info("  ✓ Analysis completed")
                else:
                    error = analysis_results.get("error", "Unknown error")
                    checkpoint.mark_phase_failed("phase4_analysis", str(error))
                    logger.warning(f"  ⚠ Analysis failed: {error}")
            else:
                error = "Required CSV files not found"
                checkpoint.mark_phase_failed("phase4_analysis", error)
                logger.warning(f"  ⚠ {error}, skipping analysis")
        except Exception as e:
            checkpoint.mark_phase_failed("phase4_analysis", str(e))
            logger.warning(f"  ⚠ Analysis error: {e}", exc_info=True)

    # Phase 5: Upload & Archive (non-blocking)
    logger.info("\n[Phase 5] Upload & Archive")
    if checkpoint.is_phase_completed("phase5_upload"):
        logger.info("✓ Phase 5 already completed, skipping")
    else:
        try:
            from .stages.upload import upload_results

            # Wrap analysis results in StageResult format
            # Note: analysis_results may be empty if Phase 4 was skipped
            analysis_stage_result = StageResult(
                status=analysis_results.get("status", "ok"),
                artifacts={
                    "metrics": analysis_results.get("analyses", {}),
                    "results": analysis_results,
                },
            )

            # Get last stage result as proxy for agent_run
            last_stage_result = None
            for stage_name in reversed(list(ctx.stage_results.keys())):
                last_stage_result = ctx.stage_results[stage_name]
                break

            upload_result = upload_results(
                ctx=ctx,
                analysis=analysis_stage_result,
                agent_run=last_stage_result or StageResult(status="ok"),
                logger=logger,
            )

            if upload_result.status == "ok":
                checkpoint.mark_phase_completed("phase5_upload")
                logger.info("  ✓ Upload completed")
            else:
                checkpoint.mark_phase_failed("phase5_upload", upload_result.error or "Unknown error")
                logger.warning(f"  ⚠ Upload failed: {upload_result.error}")
        except Exception as e:
            checkpoint.mark_phase_failed("phase5_upload", str(e))
            logger.warning(f"  ⚠ Upload error: {e}", exc_info=True)

    # Phase 6: Fix dependencies (add missing, remove redundant)
    dag_conflicts = analysis_results.get("analyses", {}).get("dag_conflicts", {})
    missing_count = dag_conflicts.get("summary", {}).get("missing_dependencies_count", 0)
    redundant_count = len(dag_conflicts.get("redundant_dependencies", []))

    if missing_count > 0 or redundant_count > 0:
        issues = []
        if missing_count > 0:
            issues.append(f"{missing_count} missing")
        if redundant_count > 0:
            issues.append(f"{redundant_count} redundant")
        logger.info(f"\n[Phase 6] Fix Dependencies ({', '.join(issues)})")

        try:
            from .stages.analysis import auto_fill_missing_dependencies, remove_redundant_dependencies

            # Update result/dependencies.csv directly
            result_path = Path(ctx.result_path)
            dependencies_csv = result_path / "dependencies.csv"
            commit_dag_path = ctx.get_input("commit_dag")
            milestones_csv = result_path / "milestones.csv"

            # Determine split_metadata_dir from stage4_decomp output
            split_metadata_dir = None
            stage4_output = ctx.get_stage_output_dir("stage4_decomp")
            if stage4_output.exists():
                commit_splits_dir = stage4_output / "commit_splits"
                if commit_splits_dir.exists():
                    split_metadata_dir = commit_splits_dir

            if dependencies_csv.exists() and milestones_csv.exists():
                # Add missing dependencies
                if missing_count > 0:
                    filled = auto_fill_missing_dependencies(
                        dependencies_csv=dependencies_csv,
                        milestones_csv=milestones_csv,
                        commit_dag_path=commit_dag_path,
                        logger=logger,
                        split_metadata_dir=split_metadata_dir,
                        repo_path=None if ctx.is_multi_repo else ctx.get_repo(),
                    )
                    if filled > 0:
                        logger.info(f"  ✓ Added {filled} missing dependencies")

                # Remove redundant dependencies
                if redundant_count > 0:
                    removed = remove_redundant_dependencies(
                        dependencies_csv=dependencies_csv,
                        milestones_csv=milestones_csv,
                        commit_dag_path=commit_dag_path,
                        logger=logger,
                        split_metadata_dir=split_metadata_dir,
                        repo_path=None if ctx.is_multi_repo else ctx.get_repo(),
                    )
                    if removed > 0:
                        logger.info(f"  ✓ Removed {removed} redundant dependencies")
        except Exception as e:
            logger.warning(f"  ⚠ Fix dependencies failed: {e}")

    # Final summary
    logger.info("\n" + "=" * 70)
    logger.info("Multi-Stage Pipeline Completed Successfully")
    logger.info("=" * 70)
    logger.info(f"Trial: {ctx.trial_name}")
    logger.info(f"Total stages: {len(stages)}")
    logger.info(f"Result path: {ctx.result_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
