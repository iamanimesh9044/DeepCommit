from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from deepcommit.harness._orchestrator.ensure_base_image import expected_base_image_name

from . import db
from .utils import (
    clone_repo_local,
    copy_tree,
    find_preprocessed_path,
    find_repo_path,
    get_next_trial_name,
    get_project_root,
    get_python_interpreter,
    get_result_base_root,
    get_trial_space_root,
    git_rev_parse,
    parse_repo_spec,
    remove_path,
    select_trial,
)


def _print_info(message: str) -> None:
    print(f"[INFO] {message}")


def _resolve_base_build_fields(config_path: Path, data_root: Path) -> tuple[str | None, str | None]:
    """Resolve Phase-3 base-build fields from the WORKSPACE, never from config.

    The harness no longer writes base_image_name / build_command back into the
    hand-authored config (that writeback was removed). base_image_name is derived
    by convention; build_command is read from the base env build artifact at
    ``<data_root>/harness_workspace/<repo_range>/base/dockerfiles/build_command.txt``.
    Both are legitimately absent on a first run (base image not built yet); the
    BASE_IMAGE_NAME / BUILD_COMMAND env consumers degrade gracefully via
    ``os.environ.get``.
    """
    base_image_name = expected_base_image_name(config_path)
    bc_file = data_root / "harness_workspace" / config_path.stem / "base" / "dockerfiles" / "build_command.txt"
    build_command: str | None = None
    if bc_file.exists():
        build_command = bc_file.read_text().strip() or None
    return base_image_name, build_command


def _validate_trial_name_parent_dir(trial_name: str, result_base: Path) -> None:
    if "/" in trial_name:
        parent_path = Path(trial_name).parent
        full_parent_path = result_base / parent_path
        if not full_parent_path.exists():
            raise FileNotFoundError(
                f"Parent directory '{parent_path}' does not exist under {result_base}. "
                f"Please create it first or use a valid parent directory."
            )
        if not full_parent_path.is_dir():
            raise NotADirectoryError(f"Parent path '{full_parent_path}' exists but is not a directory.")


def run_trial(
    *,
    prompt_file: str,
    repo_id: str,
    trial_name: str,
    agents: str,
    max_retries: int,
    data_root: Path,
    result_root_override: Path | None,
    refresh_views: bool,
    allow_rerun: bool = False,
) -> None:
    project_root = get_project_root()
    data_root = data_root.expanduser().resolve()
    if not data_root.exists():
        raise FileNotFoundError(f"DATA root not found: {data_root}")

    prompt_path = project_root / "deepcommit" / "pipeline" / "prompts" / prompt_file
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

    result_base = result_root_override or get_result_base_root(data_root)
    result_base.mkdir(parents=True, exist_ok=True)

    _validate_trial_name_parent_dir(trial_name, result_base)

    if not allow_rerun:
        trial_name = _ensure_unique_trial_name(trial_name, result_base)

    workspace_root = get_trial_space_root(data_root) / trial_name
    workspace_root.mkdir(parents=True, exist_ok=True)

    deepcommit_src = project_root / "deepcommit"
    deepcommit_dst = workspace_root / "deepcommit"

    _print_info(f"Project root:   {project_root}")
    _print_info(f"DATA root:      {data_root}")
    _print_info(f"Workspace root: {workspace_root}")

    result_root = (result_base / trial_name).expanduser().resolve()
    result_root.mkdir(parents=True, exist_ok=True)
    _print_info(f"Result root:    {result_root}")
    _print_info(f"Trial name:     {trial_name}")
    _print_info(f"Repo ID:        {repo_id}")
    _print_info(f"Refresh views:  {int(refresh_views)}")

    _print_info(f"Copying deepcommit -> {deepcommit_dst}")
    copy_tree(
        deepcommit_src,
        deepcommit_dst,
        ignore_patterns=("__pycache__", "*.pyc", ".venv", ".mypy_cache", ".pytest_cache"),
    )

    # Create repo symlink to specific repository
    github_data_root = data_root / "github_data"
    if not github_data_root.exists():
        raise FileNotFoundError(f"GitHub data directory not found: {github_data_root}")

    repo_id_parsed: str
    time_span: str | None
    if ":" in repo_id:
        repo_id_parsed, time_span = parse_repo_spec(repo_id)
    else:
        repo_id_parsed, time_span = repo_id.replace("/", "_"), None

    repo_src = find_repo_path(repo_id_parsed, data_root)
    if not repo_src:
        expected = github_data_root / "repos" / repo_id_parsed
        raise FileNotFoundError(f"Repository not found: {expected} (or {expected}.git)")

    repo_link = workspace_root / "repo"
    remove_path(repo_link)
    repo_link.symlink_to(repo_src)
    _print_info(f"Linked repo -> {repo_src}")

    # Create preprocessed symlink to specific preprocessed data
    if time_span:
        preprocessed_src = find_preprocessed_path(repo_id_parsed, time_span, data_root)
    else:
        preprocessed_src = github_data_root / "preprocessed" / repo_id_parsed
    if not preprocessed_src.exists():
        raise FileNotFoundError(f"Preprocessed data not found: {preprocessed_src}")

    preprocessed_link = workspace_root / "preprocessed"
    remove_path(preprocessed_link)
    preprocessed_link.symlink_to(preprocessed_src)
    _print_info(f"Linked preprocessed -> {preprocessed_src}")

    result_link = workspace_root / "result"
    remove_path(result_link)
    result_link.symlink_to(result_root)
    _print_info(f"Linked result   -> {result_root}")

    python_env = os.environ.copy()
    python_bin_env = python_env.get("PYTHON")
    if python_bin_env:
        python_bin = Path(python_bin_env)
    else:
        python_bin = get_python_interpreter(project_root)

    code_commit_sha = git_rev_parse(project_root)
    _print_info(f"Code commit:    {code_commit_sha}")

    cmd = [
        str(python_bin),
        "-m",
        "deepcommit.pipeline.main",
        "--trial-name",
        trial_name,
        "--workspace-root",
        str(workspace_root),
        "--prompt-file",
        prompt_file,
        "--repo-id",
        repo_id,
        "--agents",
        agents,
        "--code-commit-sha",
        code_commit_sha,
        "--max-retries",
        str(max_retries),
    ]

    run_env = python_env.copy()
    existing_path = run_env.get("PYTHONPATH")
    run_env["PYTHONPATH"] = f"{workspace_root}:{existing_path}" if existing_path else str(workspace_root)

    _print_info("Launching pipeline in workspace...")
    _print_info("       " + " ".join(cmd))

    subprocess.run(cmd, cwd=workspace_root, env=run_env, check=True)

    if refresh_views:
        db_dir = result_root.parent
        db_path = db_dir / "trials.sqlite"
        try:
            _print_info(f"Refreshing SQLite views -> {db_path}")
            db.refresh_views(db_path=db_path)
        except FileNotFoundError:
            _print_info(f"Skip view refresh (DB not found: {db_path})")


def _ensure_unique_trial_name(trial_name: str, result_base: Path) -> str:
    candidate_dir = result_base / trial_name
    if candidate_dir.exists():
        new_name = get_next_trial_name(trial_name, result_base)
        _print_info(f"Trial name '{trial_name}' exists, using '{new_name}'")
        return new_name

    trial_path = Path(trial_name)
    parent_dir = result_base / trial_path.parent if trial_path.parent != Path(".") else result_base
    base_name = trial_path.name
    pattern = f"{base_name}_"

    if parent_dir.exists():
        if any(p.name.startswith(pattern) for p in parent_dir.iterdir() if p.is_dir()):
            new_name = get_next_trial_name(trial_name, result_base)
            _print_info(f"Trial name '{trial_name}' exists, using '{new_name}'")
            return new_name

    return trial_name


def _generate_rerun_trial_name(base_trial_name: str, from_stage: str) -> str:
    """Generate new trial name for rerun operation.

    Args:
        base_trial_name: Source trial name
        from_stage: Stage to rerun from (e.g., 'stage2_milestone_consolidation')

    Returns:
        New trial name like 'base_trial_rerun_stage2'

    Example:
        >>> _generate_rerun_trial_name("test_trial_v1", "stage2_milestone_consolidation")
        'test_trial_v1_rerun_stage2'
    """
    # Extract stage number/identifier (e.g., "stage2" from "stage2_milestone_consolidation")
    stage_prefix = from_stage.split("_")[0] if "_" in from_stage else from_stage
    return f"{base_trial_name}_rerun_{stage_prefix}"


def _validate_source_checkpoint(source_workspace: Path, required_stages: list[str], rerun_stage: str) -> None:
    """Validate that source workspace has all required stages completed.

    Args:
        source_workspace: Path to source workspace
        required_stages: List of stage IDs that must be completed
        rerun_stage: The stage to rerun from

    Raises:
        FileNotFoundError: If checkpoint file not found
        ValueError: If required stages are not completed
    """
    checkpoint_file = source_workspace / "result" / "logs" / "checkpoint.json"

    if not checkpoint_file.exists():
        raise FileNotFoundError(
            f"Source checkpoint not found: {checkpoint_file}\n"
            f"Cannot rerun from {rerun_stage} - source trial appears incomplete."
        )

    with open(checkpoint_file, "r") as f:
        checkpoint_data = json.load(f)

    phase2 = checkpoint_data.get("phases", {}).get("phase2_multi_stage", {})
    completed_stages = phase2.get("stages", {})

    missing_stages = []
    for stage_id in required_stages:
        status = completed_stages.get(stage_id, {}).get("status")
        if status != "completed":
            missing_stages.append(f"{stage_id} (status: {status or 'not found'})")

    if missing_stages:
        raise ValueError(
            f"Cannot rerun from {rerun_stage} - prerequisite stages not completed:\n"
            + "\n".join(f"  - {stage}" for stage in missing_stages)
        )


def _copy_stage_outputs(source_workspace: Path, dest_workspace: Path, stages_to_copy: list[str]) -> None:
    """Copy stage output directories from source to destination workspace.

    Args:
        source_workspace: Path to source workspace
        dest_workspace: Path to destination workspace
        stages_to_copy: List of stage IDs whose outputs should be copied
    """
    for stage_id in stages_to_copy:
        source_output = source_workspace / stage_id / "output"
        dest_output = dest_workspace / stage_id / "output"

        if not source_output.exists():
            _print_info(f"Warning: Source output not found for {stage_id}, skipping")
            continue

        _print_info(f"Copying {stage_id}/output...")
        copy_tree(source_output, dest_output)


def _copy_stage_results(source_result: Path, dest_result: Path, stages_to_copy: list[str]) -> None:
    """Copy stage result directories from source to destination result.

    Args:
        source_result: Path to source result directory
        dest_result: Path to destination result directory
        stages_to_copy: List of stage IDs whose results should be copied
    """
    for stage_id in stages_to_copy:
        source_stage_result = source_result / stage_id
        dest_stage_result = dest_result / stage_id

        if not source_stage_result.exists():
            _print_info(f"Warning: Source result not found for {stage_id}, skipping")
            continue

        _print_info(f"Copying result/{stage_id}/...")
        copy_tree(source_stage_result, dest_stage_result)


def _create_initial_checkpoint_with_completed_stages(checkpoint_path: Path, completed_stages: list[str]) -> None:
    """Create initial checkpoint file with specified stages marked as completed.

    Args:
        checkpoint_path: Path to checkpoint file to create
        completed_stages: List of stage IDs to mark as completed
    """
    checkpoint_data = {
        "phases": {
            "phase2_multi_stage": {"stages": {stage_id: {"status": "completed"} for stage_id in completed_stages}}
        }
    }

    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with open(checkpoint_path, "w") as f:
        json.dump(checkpoint_data, f, indent=2)


def _reset_postprocess_checkpoint(checkpoint_path: Path) -> None:
    """Reset Phase 3, 4, 5 checkpoint status to allow re-execution.

    Args:
        checkpoint_path: Path to checkpoint.json file
    """
    with open(checkpoint_path, "r") as f:
        checkpoint_data = json.load(f)

    phases = checkpoint_data.get("phases", {})

    # Remove Phase 3, 4, 5 status
    postprocess_phases = ["phase3_enrichment", "phase4_analysis", "phase5_upload"]
    for phase_id in postprocess_phases:
        if phase_id in phases:
            del phases[phase_id]

    # Write back
    with open(checkpoint_path, "w") as f:
        json.dump(checkpoint_data, f, indent=2)


def _clean_postprocess_outputs(result_root: Path, logger) -> None:
    """Clean Phase 3, 4, 5 output files to allow fresh re-execution.

    Args:
        result_root: Path to result directory
        logger: Logger function for output
    """
    # Phase 3: Milestone Enrichment outputs
    enrichment_files = [
        "milestones.csv",  # May be updated by enrichment
        "milestones_enriched.csv",
        "enrichment_report.json",
    ]

    # Phase 4: Analysis outputs
    analysis_files = [
        "analysis_report.json",
        "analysis_summary.json",
        "coverage_analysis.json",
        "dag_analysis.json",
        "conflicts_analysis.json",
    ]

    # Phase 5: Upload outputs
    upload_files = [
        "upload_report.json",
        "archive_manifest.json",
    ]

    all_files_to_clean = enrichment_files + analysis_files + upload_files

    cleaned_count = 0
    for filename in all_files_to_clean:
        file_path = result_root / filename
        if file_path.exists():
            remove_path(file_path)
            logger(f"  Removed: {filename}")
            cleaned_count += 1

    if cleaned_count > 0:
        logger(f"  ✓ Cleaned {cleaned_count} postprocess output file(s)")
    else:
        logger("  No postprocess output files to clean")


def _link_workspace_trial_data(ws, ws_dir: Path, data_root: Path, workspace_root: Path, *, reuse: bool) -> None:
    """Lay out a multi-repo trial: repo_data/<name> clones, proc_data -> merged, trial-local workspace_meta."""
    repo_dest_root = workspace_root / "repo_data"
    repo_dest_root.mkdir(parents=True, exist_ok=True)
    repos = json.loads((ws_dir / "repos.json").read_text(encoding="utf-8"))
    for spec in ws.repos:
        src = find_repo_path(spec.repo_id, data_root)
        if not src:
            raise FileNotFoundError(f"Repository not found: {data_root / 'github_data' / 'repos' / spec.repo_id}")
        dest = repo_dest_root / spec.name
        if dest.exists() and reuse:
            _print_info(f"Reusing existing repo_data/{spec.name} (rerun mode)")
        else:
            clone_repo_local(src, dest)
            _print_info(f"Cloned repo_data/{spec.name} <- {src}")
        repos[spec.name]["checkout_path"] = str(dest)

    meta = workspace_root / "workspace_meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "repos.json").write_text(json.dumps(repos, indent=2), encoding="utf-8")
    (meta / "commit_repos.json").write_text((ws_dir / "commit_repos.json").read_text(encoding="utf-8"))
    (meta / "workspace.json").write_text((ws_dir / "workspace.json").read_text(encoding="utf-8"))

    merged = ws_dir / "merged"
    if not merged.exists():
        raise FileNotFoundError(f"Merged data not found: {merged}")
    link = workspace_root / "proc_data"
    remove_path(link)
    link.symlink_to(merged)
    _print_info(f"Linked proc_data -> {merged}")


def run_multi_stage_trial(
    *,
    config_file: str,
    repo_id: str,
    trial_name: str,
    agents: str,
    max_retries: int,
    data_root: Path,
    result_root_override: Path | None,
    refresh_views: bool,
    allow_rerun: bool = False,
    rerun_from_stage: str | None = None,
    rerun_postproc: bool = False,
    workspace_manifest: Path | None = None,
) -> None:
    """Run a multi-stage pipeline trial.

    Args:
        config_file: Pipeline configuration file (e.g., multi_stage_pipeline.yaml)
        repo_id: Repository specification (owner_repo:start_end)
        trial_name: Base trial name (or source trial name if rerun_from_stage is set)
        agents: Comma-separated agent list
        max_retries: Maximum recovery retries per stage
        data_root: DATA root directory
        result_root_override: Optional result root override
        refresh_views: Whether to refresh SQLite views after completion
        allow_rerun: Allow reusing existing trial name (skip auto-increment)
        rerun_from_stage: Stage to rerun from (e.g., 'stage2_milestone_consolidation').
                         Creates new workspace with completed stages copied from source.
        rerun_postproc: Rerun Phase 3, 4, 5 (postprocessing) after Phase 2 completion.
                       Requires Phase 2 to be fully completed. Uses same trial name.
        workspace_manifest: Multi-repo workspace manifest. When set, repo_id is ignored and the trial is keyed by
                       workspace_id (merge_workspace must have been run already).
    """
    project_root = get_project_root()
    data_root = data_root.expanduser().resolve()
    if not data_root.exists():
        raise FileNotFoundError(f"DATA root not found: {data_root}")

    ws = None
    ws_dir = None
    if workspace_manifest is not None:
        from deepcommit.multirepo import load_workspace, workspace_dir

        ws = load_workspace(workspace_manifest)
        ws_dir = workspace_dir(data_root, ws.workspace_id)
        if not (ws_dir / "repos.json").exists():
            raise FileNotFoundError(f"Workspace not merged: {ws_dir / 'repos.json'} (run `workspace merge` first)")
        repo_id = ws.workspace_id

    # Load pipeline config to get stage info
    config_path = project_root / "deepcommit" / "pipeline" / "configs" / config_file
    if not config_path.exists():
        raise FileNotFoundError(f"Pipeline config file not found: {config_path}")

    # Parse pipeline config to get stage list
    import yaml

    with open(config_path, "r") as f:
        pipeline_config = yaml.safe_load(f)
    stages = pipeline_config.get("stages", [])
    if not stages:
        raise ValueError(f"No stages defined in {config_file}")

    result_base = result_root_override or get_result_base_root(data_root)
    result_base.mkdir(parents=True, exist_ok=True)

    # Auto-prefix trial_name with repo_range if not already present
    # e.g., repo_id="django_django:4.2_5.2", trial_name="v1_002" -> "django_django_4.2_5.2_v1_002"
    if ws is not None:
        repo_id_parsed, time_span = ws.workspace_id, ""
        repo_range = ws.workspace_id
    else:
        repo_id_parsed, time_span = parse_repo_spec(repo_id)
        repo_range = f"{repo_id_parsed}_{time_span}"
    if not trial_name.startswith(repo_range):
        trial_name = f"{repo_range}_{trial_name}"

    _validate_trial_name_parent_dir(trial_name, result_base)

    # Handle rerun postprocess mode
    if rerun_postproc:
        # 1. Verify trial exists
        trial_space_root = get_trial_space_root(data_root)
        workspace_root = trial_space_root / trial_name
        if not workspace_root.exists():
            raise FileNotFoundError(
                f"Trial '{trial_name}' not found at {workspace_root}\nCannot rerun postprocess - trial does not exist"
            )

        result_root = result_base / trial_name
        checkpoint_file = result_root / "logs" / "checkpoint.json"

        # 2. Verify Phase 2 completed
        if not checkpoint_file.exists():
            raise FileNotFoundError(
                f"Checkpoint not found: {checkpoint_file}\nCannot rerun postprocess - trial appears incomplete"
            )

        with open(checkpoint_file, "r") as f:
            checkpoint_data = json.load(f)

        phase2_status = checkpoint_data.get("phases", {}).get("phase2_multi_stage", {}).get("status")
        if phase2_status != "completed":
            raise ValueError(
                f"Phase 2 not completed (status: {phase2_status or 'not found'})\n"
                f"Cannot rerun postprocess - Phase 2 must be fully completed"
            )

        _print_info("Rerun postprocess mode enabled")
        _print_info("✓ Phase 2 verified as completed")

        # 3. Clean postprocess output files
        _print_info("Cleaning Phase 3, 4, 5 output files...")
        _clean_postprocess_outputs(result_root, _print_info)

        # 4. Reset postprocess checkpoint
        _print_info("Resetting Phase 3, 4, 5 checkpoint status...")
        _reset_postprocess_checkpoint(checkpoint_file)
        _print_info("✓ Checkpoint reset complete")

        # 5. Force allow_rerun to use same trial name
        allow_rerun = True

    # Handle rerun from specific stage
    source_trial_name = None
    source_workspace = None
    stages_to_copy: list[str] = []

    if rerun_from_stage:
        # Validate rerun_from_stage exists in pipeline config
        stage_ids = [s["stage_id"] for s in stages]
        if rerun_from_stage not in stage_ids:
            raise ValueError(
                f"Stage '{rerun_from_stage}' not found in pipeline config.\nAvailable stages: {', '.join(stage_ids)}"
            )

        # Find stage index
        rerun_stage_idx = stage_ids.index(rerun_from_stage)
        if rerun_stage_idx == 0:
            raise ValueError(f"Cannot rerun from first stage '{rerun_from_stage}'. Use normal run instead.")

        # Get stages that need to be completed before rerun stage
        stages_to_copy = stage_ids[:rerun_stage_idx]

        # Resolve source workspace
        source_trial_name = trial_name
        source_workspace = get_trial_space_root(data_root) / source_trial_name

        if not source_workspace.exists():
            raise FileNotFoundError(
                f"Source workspace not found: {source_workspace}\nCannot rerun from {rerun_from_stage}"
            )

        # Validate source checkpoint
        _print_info(f"Rerun mode: from {rerun_from_stage}")
        _print_info(f"Source trial: {source_trial_name}")
        _print_info("Validating source checkpoint...")
        _validate_source_checkpoint(source_workspace, stages_to_copy, rerun_from_stage)
        _print_info("✓ Source checkpoint validated")

        # Generate new trial name for rerun
        trial_name = _generate_rerun_trial_name(source_trial_name, rerun_from_stage)
        _print_info(f"New trial name: {trial_name}")

        # Ensure new trial name is unique
        if (result_base / trial_name).exists():
            trial_name = _ensure_unique_trial_name(trial_name, result_base)
    elif not allow_rerun:
        trial_name = _ensure_unique_trial_name(trial_name, result_base)

    workspace_root = get_trial_space_root(data_root) / trial_name
    workspace_root.mkdir(parents=True, exist_ok=True)

    deepcommit_src = project_root / "deepcommit"
    deepcommit_dst = workspace_root / "deepcommit"

    _print_info(f"Project root:   {project_root}")
    _print_info(f"DATA root:      {data_root}")
    _print_info(f"Workspace root: {workspace_root}")

    result_root = (result_base / trial_name).expanduser().resolve()
    result_root.mkdir(parents=True, exist_ok=True)
    _print_info(f"Result root:    {result_root}")
    _print_info(f"Trial name:     {trial_name}")
    _print_info(f"Repo ID:        {repo_id}")
    _print_info("Pipeline mode:  Multi-stage")
    _print_info(f"Refresh views:  {int(refresh_views)}")

    _print_info(f"Copying deepcommit -> {deepcommit_dst}")
    copy_tree(
        deepcommit_src,
        deepcommit_dst,
        ignore_patterns=("__pycache__", "*.pyc", ".venv", ".mypy_cache", ".pytest_cache"),
    )

    # Create repo symlink to specific repository
    github_data_root = data_root / "github_data"
    if not github_data_root.exists():
        raise FileNotFoundError(f"GitHub data directory not found: {github_data_root}")

    if ws is not None:
        _link_workspace_trial_data(ws, ws_dir, data_root, workspace_root, reuse=allow_rerun or rerun_postproc)
    else:
        repo_id_parsed, time_span = parse_repo_spec(repo_id)

        repo_src = find_repo_path(repo_id_parsed, data_root)
        if not repo_src:
            expected = github_data_root / "repos" / repo_id_parsed
            raise FileNotFoundError(f"Repository not found: {expected} (or {expected}.git)")

        repo_dest = workspace_root / "repo_data"  # Changed from "repo" to "repo_data"
        # Skip clone if repo_data already exists (rerun modes: --allow-rerun, --rerun-postproc)
        # This preserves split branches created by stage4
        if repo_dest.exists() and (allow_rerun or rerun_postproc):
            _print_info("Reusing existing repo_data (rerun mode)")
        else:
            clone_repo_local(repo_src, repo_dest)
            _print_info(f"Cloned repo_data <- {repo_src}")

        # Create preprocessed symlink to specific preprocessed data
        preprocessed_src = find_preprocessed_path(repo_id_parsed, time_span, data_root)
        if not preprocessed_src.exists():
            raise FileNotFoundError(f"Preprocessed data not found: {preprocessed_src}")

        preprocessed_link = workspace_root / "proc_data"  # Changed from "preprocessed" to "proc_data"
        remove_path(preprocessed_link)
        preprocessed_link.symlink_to(preprocessed_src)
        _print_info(f"Linked proc_data -> {preprocessed_src}")

    result_link = workspace_root / "result"
    remove_path(result_link)
    result_link.symlink_to(result_root)
    _print_info(f"Linked result   -> {result_root}")

    # Create stage directories
    for stage_info in stages:
        stage_id = stage_info["stage_id"]
        stage_dir = workspace_root / stage_id
        stage_dir.mkdir(parents=True, exist_ok=True)
        (stage_dir / "input").mkdir(exist_ok=True)
        (stage_dir / "output").mkdir(exist_ok=True)
        (stage_dir / "logs").mkdir(exist_ok=True)
        _print_info(f"Created stage workspace: {stage_id}/")

    # Handle rerun: copy completed stage outputs and create initial checkpoint
    if rerun_from_stage and source_workspace and stages_to_copy:
        _print_info("Copying completed stages from source...")

        # Copy workspace outputs
        _copy_stage_outputs(source_workspace, workspace_root, stages_to_copy)
        _print_info(f"✓ Copied {len(stages_to_copy)} stage output(s)")

        # Copy result directories
        source_result = result_base / source_trial_name
        _copy_stage_results(source_result, result_root, stages_to_copy)
        _print_info(f"✓ Copied {len(stages_to_copy)} stage result(s)")

        # Create initial checkpoint with completed stages
        checkpoint_file = result_root / "logs" / "checkpoint.json"
        _create_initial_checkpoint_with_completed_stages(checkpoint_file, stages_to_copy)
        _print_info(f"✓ Created checkpoint with {len(stages_to_copy)} completed stage(s)")

    python_env = os.environ.copy()
    python_bin_env = python_env.get("PYTHON")
    if python_bin_env:
        python_bin = Path(python_bin_env)
    else:
        python_bin = get_python_interpreter(project_root)

    code_commit_sha = git_rev_parse(project_root)
    _print_info(f"Code commit:    {code_commit_sha}")

    # Load repo filters from config and pass via environment variables
    try:
        # Load config from project root
        config_path = project_root / "config" / f"{repo_id_parsed}_{time_span}.yaml"
        if ws is not None:
            config_path = Path("/nonexistent")  # workspace members carry their filters in repos.json

        repo_src_dirs = None
        test_dirs = None
        exclude = None
        exclude_test_files = False
        exclude_ci_files = False
        exclude_config_files = False
        exclude_doc_files = False
        base_image_name = None
        build_command = None
        if config_path.exists():
            import yaml

            with open(config_path, "r") as f:
                project_config = yaml.safe_load(f)
            repo_src_dirs = project_config.get("repo_src_dirs")
            test_dirs = project_config.get("test_dirs")
            exclude_test_files = bool(project_config.get("exclude_test_files", False))
            exclude_ci_files = bool(project_config.get("exclude_ci_files", False))
            exclude_config_files = bool(project_config.get("exclude_config_files", False))
            exclude_doc_files = bool(project_config.get("exclude_doc_files", False))
            exclude = project_config.get("exclude") or project_config.get("exclude_patterns")
            # Phase-3 base-build fields come from the WORKSPACE, not config
            # (build_image writeback removed): derive base image, read build_command.txt.
            base_image_name, build_command = _resolve_base_build_fields(config_path, data_root)

            if test_dirs is not None and not isinstance(test_dirs, list):
                test_dirs = [str(test_dirs)]
            if isinstance(test_dirs, list):
                test_dirs = [str(p) for p in test_dirs if str(p).strip()]

            if exclude is not None and not isinstance(exclude, list):
                exclude = [str(exclude)]
            if isinstance(exclude, list):
                exclude = [str(p) for p in exclude if str(p).strip()]
    except Exception as e:
        _print_info(f"Warning: Failed to load repo filters: {e}")
        repo_src_dirs = None
        test_dirs = None
        exclude = None
        exclude_test_files = False
        exclude_ci_files = False
        exclude_config_files = False
        exclude_doc_files = False
        base_image_name = None
        build_command = None

    cmd = [
        str(python_bin),
        "-m",
        "deepcommit.pipeline.main_multi_stage",
        "--trial-name",
        trial_name,
        "--workspace-root",
        str(workspace_root),
        "--config-file",
        config_file,
        "--repo-id",
        repo_id,
        "--agents",
        agents,
        "--code-commit-sha",
        code_commit_sha,
        "--max-retries",
        str(max_retries),
    ]

    run_env = python_env.copy()

    if ws is not None:
        # prefixed lists; DEEPCOMMIT_WORKSPACE_DIR is the trial-local dir (checkouts rewritten to repo_data/<name>)
        trial_ws_dir = workspace_root / "workspace_meta"
        run_env["DEEPCOMMIT_WORKSPACE_DIR"] = str(trial_ws_dir)
        repo_src_dirs = ws.prefixed_src_dirs()
        test_dirs = ws.prefixed_test_dirs() or None
        exclude = ws.prefixed_exclude() or None
        _print_info(f"Setting DEEPCOMMIT_WORKSPACE_DIR: {trial_ws_dir}")

    # Pass repo_src_dirs via environment variable
    if repo_src_dirs:
        run_env["REPO_SRC_DIRS"] = json.dumps(repo_src_dirs)
        _print_info(f"Setting REPO_SRC_DIRS: {repo_src_dirs}")

    if test_dirs:
        run_env["REPO_TEST_DIRS"] = json.dumps(test_dirs)
        _print_info(f"Setting REPO_TEST_DIRS: {test_dirs}")

    if exclude:
        run_env["REPO_EXCLUDE"] = json.dumps(exclude)
        _print_info(f"Setting REPO_EXCLUDE: {exclude}")

    # Build environment config for Stage 4 commit split verification
    if base_image_name:
        run_env["BASE_IMAGE_NAME"] = base_image_name
        _print_info(f"Setting BASE_IMAGE_NAME: {base_image_name}")
    if build_command:
        run_env["BUILD_COMMAND"] = build_command
        _print_info(f"Setting BUILD_COMMAND: {build_command}")

    # Pass exclude settings via environment variables (for post-processing)
    if exclude_test_files:
        run_env["REPO_EXCLUDE_TEST_FILES"] = "1"
    if exclude_ci_files:
        run_env["REPO_EXCLUDE_CI_FILES"] = "1"
    if exclude_config_files:
        run_env["REPO_EXCLUDE_CONFIG_FILES"] = "1"
    if exclude_doc_files:
        run_env["REPO_EXCLUDE_DOC_FILES"] = "1"

    existing_path = run_env.get("PYTHONPATH")

    # Activate virtual environment by setting VIRTUAL_ENV and PATH
    venv_path = project_root / ".venv"
    pythonpath_parts = [str(workspace_root)]

    if venv_path.exists():
        run_env["VIRTUAL_ENV"] = str(venv_path)
        run_env["PATH"] = f"{venv_path / 'bin'}:{run_env.get('PATH', '')}"
        # Remove PYTHONHOME if set (can interfere with venv)
        run_env.pop("PYTHONHOME", None)
        # Add venv site-packages to PYTHONPATH
        venv_site_packages = venv_path / "lib" / "python3.11" / "site-packages"
        if venv_site_packages.exists():
            pythonpath_parts.append(str(venv_site_packages))

    if existing_path:
        pythonpath_parts.append(existing_path)

    run_env["PYTHONPATH"] = ":".join(pythonpath_parts)

    _print_info("Launching multi-stage pipeline in workspace...")
    _print_info("       " + " ".join(cmd))
    _print_info(f"PYTHONPATH: {run_env.get('PYTHONPATH', 'not set')}")
    _print_info(f"VIRTUAL_ENV: {run_env.get('VIRTUAL_ENV', 'not set')}")

    subprocess.run(cmd, cwd=workspace_root, env=run_env, check=True)

    if refresh_views:
        db_dir = result_root.parent
        db_path = db_dir / "trials.sqlite"
        try:
            _print_info(f"Refreshing SQLite views -> {db_path}")
            db.refresh_views(db_path=db_path)
        except FileNotFoundError:
            _print_info(f"Skip view refresh (DB not found: {db_path})")


def attach_session(pattern: str, *, data_root: Path) -> None:
    trial_space = get_trial_space_root(data_root)
    trial_dir = select_trial(pattern, trial_space, required=["result"])

    session_id = None
    meta_file = trial_dir / "result" / "meta.json"
    artifacts_file = trial_dir / "result" / "agent_artifacts.json"

    # Try meta.json first
    if meta_file.exists():
        with meta_file.open("r", encoding="utf-8") as handle:
            meta = json.load(handle)
        session_id = meta.get("agent_cli_session_id")

    # Fallback to agent_artifacts.json
    if not session_id and artifacts_file.exists():
        with artifacts_file.open("r", encoding="utf-8") as handle:
            artifacts = json.load(handle)
        session_id = artifacts.get("agent_cli_session_id")

    if not session_id:
        raise RuntimeError(f"Field 'agent_cli_session_id' not found in {meta_file} or {artifacts_file}")

    cmd = ["claude", "-r", session_id, "--mcp-config", "trial.mcp.json"]
    subprocess.run(cmd, cwd=trial_dir, check=True)


def find_conversation_logs(pattern: str, *, data_root: Path) -> tuple[str, list[Path]]:
    trial_root = get_result_base_root(data_root)
    trial_dir = select_trial(pattern, trial_root, required=["logs"])
    logs_dir = trial_dir / "logs"

    conversation_files = sorted(logs_dir.glob("claude-conversation*"))
    if not conversation_files:
        raise FileNotFoundError(f"No claude-conversation files found in {logs_dir}")

    return trial_dir.name, conversation_files


def _resolve_source_result(
    trial_name: str,
    repo_range: str,
    result_root: Path,
) -> str:
    """Resolve source_result directory from trial_name.

    Logic:
    1. Try exact match: {repo_range}_{trial_name}
    2. If not found and trial_name has numeric suffix (e.g., baseline_002),
       try base name: {repo_range}_{base_name} (e.g., baseline)
    3. If still not found, raise error

    Args:
        trial_name: Trial name (e.g., "baseline", "baseline_002", "v1_003")
        repo_range: Repository range (e.g., "apache_dubbo_dubbo-3.3.3_dubbo-3.3.6")
        result_root: Path to DATA/result/

    Returns:
        Resolved source_result directory name

    Raises:
        FileNotFoundError: If no matching source_result found
    """
    import re

    # Try exact match first
    exact_match = f"{repo_range}_{trial_name}"
    if (result_root / exact_match).exists():
        return exact_match

    # Check if trial_name has numeric suffix (e.g., baseline_002, v1_003)
    # Pattern: ends with _NNN where N is digit
    suffix_pattern = re.compile(r"^(.+)_(\d{3})$")
    match = suffix_pattern.match(trial_name)

    if match:
        base_name = match.group(1)  # e.g., "baseline" from "baseline_002"
        base_match = f"{repo_range}_{base_name}"
        if (result_root / base_match).exists():
            return base_match

    # List available options for error message
    available = sorted([d.name for d in result_root.iterdir() if d.is_dir() and d.name.startswith(repo_range)])

    raise FileNotFoundError(
        f"No source_result found for trial_name='{trial_name}'\n"
        f"Tried: {exact_match}\n"
        f"Available options for {repo_range}:\n  "
        + "\n  ".join(available[:10])
        + (f"\n  ... ({len(available) - 10} more)" if len(available) > 10 else "")
    )


def run_harness_trial(
    *,
    config_file: str,
    repo_id: str,
    trial_name: str,
    agents: str,
    max_retries: int,
    data_root: Path,
    source_result: str | None = None,
    allow_rerun: bool = False,
    dry_run: bool = False,
) -> None:
    """Run a harness pipeline trial with simplified directory structure.

    Args:
        config_file: Pipeline configuration file (e.g., harness_pipeline.yaml)
        repo_id: Repository specification (owner_repo:start_end)
        trial_name: Trial name (will be created under {repo_range}/ directory)
        agents: Comma-separated agent list
        max_retries: Maximum recovery retries per stage
        data_root: DATA root directory
        source_result: Source result directory name. If None, auto-resolved from trial_name:
                       - "baseline" -> "{repo_range}_baseline"
                       - "baseline_002" -> try "{repo_range}_baseline_002", fallback to "{repo_range}_baseline"
        allow_rerun: Allow reusing existing trial name (skip auto-increment)
        dry_run: Only test setup without executing agent
    """
    project_root = get_project_root()
    data_root = data_root.expanduser().resolve()
    if not data_root.exists():
        raise FileNotFoundError(f"DATA root not found: {data_root}")

    # Load pipeline config
    config_path = project_root / "deepcommit" / "pipeline" / "configs_harness" / config_file
    if not config_path.exists():
        raise FileNotFoundError(f"Pipeline config file not found: {config_path}")

    # Parse repo_id to get repo_range for directory structure
    repo_id_parsed, time_span = parse_repo_spec(repo_id)

    # Construct workspace root: DATA/harness_trial_space/{repo_range}/{trial_name}
    harness_trial_space_root = data_root / "harness_trial_space"
    repo_range_dir = harness_trial_space_root / f"{repo_id_parsed}_{time_span}"
    repo_range_dir.mkdir(parents=True, exist_ok=True)

    # Ensure unique trial name if not allowing rerun
    if not allow_rerun:
        trial_name = _ensure_unique_trial_name(trial_name, repo_range_dir)

    workspace_root = repo_range_dir / trial_name
    workspace_root.mkdir(parents=True, exist_ok=True)

    _print_info(f"Project root:   {project_root}")
    _print_info(f"DATA root:      {data_root}")
    _print_info(f"Workspace root: {workspace_root}")
    _print_info(f"Trial name:     {trial_name}")
    _print_info(f"Repo ID:        {repo_id}")
    _print_info("Pipeline mode:  Harness (simplified)")

    # Copy deepcommit source code to workspace
    deepcommit_src = project_root / "deepcommit"
    deepcommit_dst = workspace_root / "deepcommit"
    _print_info(f"Copying deepcommit -> {deepcommit_dst}")
    copy_tree(
        deepcommit_src,
        deepcommit_dst,
        ignore_patterns=("__pycache__", "*.pyc", ".venv", ".mypy_cache", ".pytest_cache"),
    )

    # Create proc_data symlink to preprocessed data
    github_data_root = data_root / "github_data"
    if not github_data_root.exists():
        raise FileNotFoundError(f"GitHub data directory not found: {github_data_root}")

    preprocessed_src = find_preprocessed_path(repo_id_parsed, time_span, data_root)
    if not preprocessed_src.exists():
        raise FileNotFoundError(f"Preprocessed data not found: {preprocessed_src}")

    proc_data_link = workspace_root / "proc_data"
    remove_path(proc_data_link)
    proc_data_link.symlink_to(preprocessed_src)
    _print_info(f"Linked proc_data -> {preprocessed_src}")

    # Create logs directory (flat structure, no input/output subdirs)
    logs_dir = workspace_root / "logs"
    logs_dir.mkdir(exist_ok=True)
    _print_info("Created logs directory")

    # repo_range is used for config file naming and prompt templates
    repo_range = f"{repo_id_parsed}_{time_span}"

    # Copy milestones.csv and dependencies.csv from source result directory
    result_root = data_root / "result"

    # Auto-resolve source_result if not provided
    if source_result is None:
        source_result = _resolve_source_result(trial_name, repo_range, result_root)
        _print_info(f"Auto-resolved source_result: {source_result}")

    source_result_dir = result_root / source_result
    if not source_result_dir.exists():
        raise FileNotFoundError(f"Source result directory not found: {source_result_dir}")

    milestones_src = source_result_dir / "milestones.csv"
    dependencies_src = source_result_dir / "dependencies.csv"

    if not milestones_src.exists():
        raise FileNotFoundError(f"milestones.csv not found in source result: {milestones_src}")
    if not dependencies_src.exists():
        raise FileNotFoundError(f"dependencies.csv not found in source result: {dependencies_src}")

    import shutil

    shutil.copy2(milestones_src, workspace_root / "milestones.csv")
    _print_info(f"Copied milestones.csv from {source_result}")
    shutil.copy2(dependencies_src, workspace_root / "dependencies.csv")
    _print_info(f"Copied dependencies.csv from {source_result}")

    # Copy config file to workspace/config/
    config_dir = workspace_root / "config"
    config_dir.mkdir(exist_ok=True)
    config_yml_src = project_root / "config" / f"{repo_range}.yaml"
    if config_yml_src.exists():
        shutil.copy2(config_yml_src, config_dir / f"{repo_range}.yaml")
        _print_info(f"Copied config/{repo_range}.yaml")
    else:
        _print_info(f"Warning: Config file not found: {config_yml_src}")

    # Link source_repo to the global repository for MCP tools
    # Note: build_milestone_testbed.py auto-infers repo-path from config,
    # but MCP tools (validate_milestone_partitioning, analyze_dag_conflicts)
    # need DEEPCOMMIT_REPO_DIR to be a valid git repo
    source_repo_link = workspace_root / "source_repo"
    repo_path = find_repo_path(repo_id_parsed, data_root)
    if repo_path and repo_path.exists():
        remove_path(source_repo_link)
        source_repo_link.symlink_to(repo_path)
        _print_info(f"Linked source_repo -> {repo_path}")
    else:
        _print_info(f"Warning: Repository not found for {repo_id_parsed}")

    # Copy harness directory for build_milestone_testbed.py
    harness_src = project_root / "harness"
    harness_dst = workspace_root / "harness"
    if harness_src.exists():
        copy_tree(
            harness_src,
            harness_dst,
            ignore_patterns=("__pycache__", "*.pyc", ".venv", ".mypy_cache", ".pytest_cache"),
        )
        _print_info(f"Copied harness -> {harness_dst}")
    else:
        _print_info(f"Warning: harness directory not found: {harness_src}")

    # Get Python interpreter
    python_env = os.environ.copy()
    python_bin_env = python_env.get("PYTHON")
    if python_bin_env:
        python_bin = Path(python_bin_env)
    else:
        python_bin = get_python_interpreter(project_root)

    code_commit_sha = git_rev_parse(project_root)
    _print_info(f"Code commit:    {code_commit_sha}")

    # Load repo filters from config (optional)
    repo_src_dirs = None
    test_dirs = None
    exclude = None
    base_image_name = None
    build_command = None

    try:
        config_yml_path = project_root / "config" / f"{repo_id_parsed}_{time_span}.yaml"
        if config_yml_path.exists():
            import yaml

            with open(config_yml_path, "r") as f:
                project_config = yaml.safe_load(f)
            repo_src_dirs = project_config.get("repo_src_dirs")
            test_dirs = project_config.get("test_dirs")
            exclude = project_config.get("exclude") or project_config.get("exclude_patterns")
            # Phase-3 base-build fields from WORKSPACE, not config (writeback removed).
            base_image_name, build_command = _resolve_base_build_fields(config_yml_path, data_root)

            if test_dirs is not None and not isinstance(test_dirs, list):
                test_dirs = [str(test_dirs)]
            if isinstance(test_dirs, list):
                test_dirs = [str(p) for p in test_dirs if str(p).strip()]

            if exclude is not None and not isinstance(exclude, list):
                exclude = [str(exclude)]
            if isinstance(exclude, list):
                exclude = [str(p) for p in exclude if str(p).strip()]
    except Exception as e:
        _print_info(f"Warning: Failed to load repo filters: {e}")

    # Build command for harness pipeline
    cmd = [
        str(python_bin),
        "-m",
        "deepcommit.pipeline.main_harness",
        "--trial-name",
        trial_name,
        "--workspace-root",
        str(workspace_root),
        "--config-file",
        config_file,
        "--repo-id",
        repo_id,
        "--agents",
        agents,
        "--code-commit-sha",
        code_commit_sha,
        "--max-retries",
        str(max_retries),
    ]

    if dry_run:
        cmd.append("--dry-run")

    run_env = python_env.copy()

    # Pass repo configuration via environment variables
    if repo_src_dirs:
        run_env["REPO_SRC_DIRS"] = json.dumps(repo_src_dirs)
        _print_info(f"Setting REPO_SRC_DIRS: {repo_src_dirs}")

    if test_dirs:
        run_env["REPO_TEST_DIRS"] = json.dumps(test_dirs)
        _print_info(f"Setting REPO_TEST_DIRS: {test_dirs}")

    if exclude:
        run_env["REPO_EXCLUDE"] = json.dumps(exclude)
        _print_info(f"Setting REPO_EXCLUDE: {exclude}")

    if base_image_name:
        run_env["BASE_IMAGE_NAME"] = base_image_name
        _print_info(f"Setting BASE_IMAGE_NAME: {base_image_name}")
    if build_command:
        run_env["BUILD_COMMAND"] = build_command
        _print_info(f"Setting BUILD_COMMAND: {build_command}")

    existing_path = run_env.get("PYTHONPATH")

    # Activate virtual environment
    venv_path = project_root / ".venv"
    pythonpath_parts = [str(workspace_root)]

    if venv_path.exists():
        run_env["VIRTUAL_ENV"] = str(venv_path)
        run_env["PATH"] = f"{venv_path / 'bin'}:{run_env.get('PATH', '')}"
        run_env.pop("PYTHONHOME", None)
        venv_site_packages = venv_path / "lib" / "python3.11" / "site-packages"
        if venv_site_packages.exists():
            pythonpath_parts.append(str(venv_site_packages))

    if existing_path:
        pythonpath_parts.append(existing_path)

    run_env["PYTHONPATH"] = ":".join(pythonpath_parts)

    _print_info("Launching harness pipeline in workspace...")
    _print_info("       " + " ".join(cmd))
    _print_info(f"PYTHONPATH: {run_env.get('PYTHONPATH', 'not set')}")
    _print_info(f"VIRTUAL_ENV: {run_env.get('VIRTUAL_ENV', 'not set')}")

    subprocess.run(cmd, cwd=workspace_root, env=run_env, check=True)
