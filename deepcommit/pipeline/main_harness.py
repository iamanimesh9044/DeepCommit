"""
Harness Pipeline Main Controller

Simplified pipeline with flat directory structure:
- No multi-stage complexity
- No input/output subdirectories
- All outputs in workspace root
- Single stage execution model
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List

import yaml

from .harness_context import HarnessContext
from .types import StageResult
from .utils.io import ensure_dir, setup_pipeline_logger


def _module_name_to_class_name(module_name: str) -> str:
    """Convert module name to expected class name.

    Examples:
        stage1_harness_main -> Stage1HarnessMain

    Args:
        module_name: Module name (without .py extension)

    Returns:
        Expected class name in PascalCase
    """
    parts = module_name.split("_")
    return "".join(part.capitalize() for part in parts)


def load_stage_class(stage_impl: str):
    """Dynamically load stage implementation class.

    Args:
        stage_impl: Stage implementation filename (e.g., "stage1_harness_main.py")

    Returns:
        The stage class
    """
    module_name = stage_impl.replace(".py", "")

    # Import from deepcommit.pipeline.stages_harness
    try:
        module = __import__(f"deepcommit.pipeline.stages_harness.{module_name}", fromlist=[module_name])
    except ImportError as e:
        raise ImportError(
            f"Failed to import stage module '{module_name}' from deepcommit.pipeline.stages_harness. Error: {e}"
        ) from e

    # Get the stage class from the module
    class_name = _module_name_to_class_name(module_name)

    if not hasattr(module, class_name):
        raise AttributeError(
            f"Module '{module_name}' does not have expected class '{class_name}'. Available attributes: {dir(module)}"
        )

    return getattr(module, class_name)


def load_pipeline_config(config_file: str, workspace_root: Path) -> List[Dict[str, str]]:
    """Load harness pipeline configuration.

    Args:
        config_file: Pipeline config filename (e.g., "harness_pipeline.yaml")
        workspace_root: Workspace root directory

    Returns:
        List of stage configurations
    """
    config_path = workspace_root / "deepcommit" / "pipeline" / "configs_harness" / config_file
    if not config_path.exists():
        raise FileNotFoundError(f"Pipeline config not found: {config_path}")

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    stages: List[Dict[str, str]] = config.get("stages", [])
    if not stages:
        raise ValueError(f"No stages defined in {config_file}")

    return stages


def parse_args(argv: List[str]) -> argparse.Namespace:
    """Parse command-line arguments for harness pipeline."""
    p = argparse.ArgumentParser(description="Deepcommit harness pipeline (simplified)")
    p.add_argument("--trial-name", type=str, required=True)
    p.add_argument("--workspace-root", type=str, required=True)
    p.add_argument("--repo-id", type=str, required=True)
    p.add_argument(
        "--config-file",
        type=str,
        default="harness_pipeline.yaml",
        help="Pipeline config file (default: harness_pipeline.yaml)",
    )
    p.add_argument("--agents", type=str, default="claude", help="Comma-separated agent list")
    p.add_argument("--code-commit-sha", type=str, required=True)
    p.add_argument("--max-retries", type=int, default=2, help="Maximum retries per stage")
    p.add_argument(
        "--workspace-dir",
        type=str,
        default=None,
        help="Multi-repo workspace dir (repos.json + commit_repos.json). Defaults to $DEEPCOMMIT_WORKSPACE_DIR.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Only test setup (prompt rendering, directory structure) without executing agent",
    )

    return p.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    """Harness pipeline main controller."""
    ns = parse_args(sys.argv[1:] if argv is None else argv)

    # Load repo configuration from environment variables
    repo_src_dirs = None
    repo_src_dirs_env = os.environ.get("REPO_SRC_DIRS")
    if repo_src_dirs_env:
        try:
            import json

            repo_src_dirs = json.loads(repo_src_dirs_env)
        except Exception:
            pass

    def _parse_list_env(name: str) -> List[str] | None:
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

    test_dirs = _parse_list_env("REPO_TEST_DIRS")
    exclude = _parse_list_env("REPO_EXCLUDE")
    base_image_name = os.environ.get("BASE_IMAGE_NAME")
    build_command = os.environ.get("BUILD_COMMAND")

    # Create harness context (simplified, no multi-stage complexity)
    ctx = HarnessContext(
        trial_name=ns.trial_name,
        repo_id=ns.repo_id,
        config_file=ns.config_file,
        agents=ns.agents,
        code_commit_sha=ns.code_commit_sha,
        max_retries=ns.max_retries,
        workspace_root=ns.workspace_root,
        repo_src_dirs=repo_src_dirs,
        test_dirs=test_dirs,
        exclude=exclude,
        base_image_name=base_image_name,
        build_command=build_command,
        workspace_dir=ns.workspace_dir or os.environ.get("DEEPCOMMIT_WORKSPACE_DIR") or None,
    )

    # Setup logs directory
    ensure_dir(ctx.logs_path)

    # Setup pipeline logger
    logger = setup_pipeline_logger(
        log_dir=ctx.logs_path,
        workspace_root=ctx.workspace_root,
        is_multi_stage=False,  # Simplified pipeline
        current_stage_id="harness",
    )
    logger.info("=" * 70)
    logger.info("Harness Pipeline Execution (Simplified)")
    logger.info("=" * 70)

    # Log Python environment
    logger.info("Python Environment:")
    logger.info(f"  Python executable: {sys.executable}")
    logger.info(f"  Python version: {sys.version.split()[0]}")
    logger.info(f"  Virtual env: {os.environ.get('VIRTUAL_ENV', 'None')}")
    logger.info(f"  Working directory: {Path.cwd()}")
    logger.info("")

    logger.info(ctx.get_info_string())

    # Phase 0: Sanity Check
    logger.info("\n[Phase 0] Sanity Check")
    # Note: sanity_check expects TrialContext-like interface
    # For now, we skip sanity check or need to adapt it
    logger.info("  (Skipped for harness pipeline)")

    # Load pipeline configuration
    logger.info("\n[Phase 1] Load Pipeline Configuration")
    try:
        stages = load_pipeline_config(ns.config_file, Path(ns.workspace_root))
    except (FileNotFoundError, ValueError, yaml.YAMLError) as e:
        logger.error(f"Failed to load pipeline config '{ns.config_file}': {e}")
        return 1

    logger.info(f"Loaded pipeline config: {ns.config_file}")
    logger.info(f"Total stages: {len(stages)}")

    # Execute stages
    dry_run = ns.dry_run
    if dry_run:
        logger.info("\n[Phase 2] Dry Run - Testing Setup Only")
    else:
        logger.info("\n[Phase 2] Execute Stages")

    for idx, stage_info in enumerate(stages, 1):
        stage_id = stage_info["stage_id"]
        stage_impl = stage_info["stage_impl"]
        stage_config = stage_info.get("stage_config", f"{stage_id}.yaml")

        logger.info(f"\n{'=' * 70}")
        logger.info(f"Stage {idx}/{len(stages)}: {stage_id}")
        logger.info(f"Implementation: {stage_impl}")
        logger.info(f"Stage config: {stage_config}")
        logger.info(f"{'=' * 70}")

        # Update current_stage_id so render_prompt() loads correct config
        # Use stage_config filename without .yaml extension as the config key
        ctx.current_stage_id = stage_config.replace(".yaml", "")
        logger.info(f"Set current_stage_id: {ctx.current_stage_id}")

        # Load and instantiate stage class
        try:
            stage_class = load_stage_class(stage_impl)
            logger.info(f"Loaded stage class: {stage_class.__name__}")
            stage_instance = stage_class(ctx=ctx, logger=logger)
            logger.info("✓ Stage instance created")
        except Exception as e:
            logger.error(f"✗ Failed to load/instantiate stage: {e}", exc_info=True)
            error_msg = f"Stage initialization failed: {e}"
            ctx.set_stage_results(f"Stage {idx}: {stage_id}", StageResult(status="error", error=error_msg))
            return 1

        if dry_run:
            # Dry run mode: only test setup without executing agent
            logger.info("DRY RUN: Testing setup only...")

            # Test _setup_input_data
            try:
                stage_instance._setup_input_data()
                logger.info("✓ _setup_input_data() completed")
            except Exception as e:
                logger.error(f"✗ _setup_input_data() failed: {e}", exc_info=True)
                return 1

            # Test _render_prompt
            try:
                stage_instance._render_prompt()
                logger.info("✓ _render_prompt() completed")
                logger.info(f"  Rendered prompt: {ctx.get_prompt_rendered_path()}")
            except Exception as e:
                logger.error(f"✗ _render_prompt() failed: {e}", exc_info=True)
                return 1

            # Log workspace structure
            logger.info("\nWorkspace structure:")
            workspace = Path(ctx.workspace_root)
            for item in sorted(workspace.iterdir()):
                if item.is_dir():
                    logger.info(f"  {item.name}/")
                else:
                    logger.info(f"  {item.name}")

            logger.info(f"\n✓ Stage {stage_id} dry run completed")
            ctx.set_stage_results(f"Stage {idx}: {stage_id}", StageResult(status="ok", notes="dry run"))

            # In dry run mode, only test stage 1
            logger.info("\nDRY RUN: Stopping after first stage")
            break
        else:
            # Normal execution
            logger.info("Executing stage...")
            try:
                stage_result = stage_instance.run_stage()
            except Exception as e:
                error_msg = str(e)
                logger.error(f"✗ Stage {stage_id} failed with exception: {error_msg}", exc_info=True)
                ctx.set_stage_results(f"Stage {idx}: {stage_id}", StageResult(status="error", error=error_msg))
                return 1

            # Process stage result
            ctx.set_stage_results(f"Stage {idx}: {stage_id}", stage_result)

            if stage_result.status == "error":
                logger.error(f"✗ Stage {stage_id} failed: {stage_result.error}")
                return 1

            logger.info(f"✓ Stage {stage_id} completed successfully")
            if stage_result.notes:
                logger.info(f"  Notes: {stage_result.notes}")

    # Final summary
    logger.info("\n" + "=" * 70)
    logger.info("Harness Pipeline Completed")
    logger.info("=" * 70)
    logger.info(f"Trial: {ctx.trial_name}")
    logger.info(f"Total stages: {len(stages)}")
    logger.info(f"Workspace: {ctx.workspace_root}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
