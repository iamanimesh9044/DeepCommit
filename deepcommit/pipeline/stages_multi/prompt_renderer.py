"""
Multi-stage pipeline prompt renderer.

Renders prompt templates with paths resolved from stage and shared configs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import yaml

from ..context import TrialContext
from ..utils.template import render_template_string


def _load_yaml_config(config_path: Path) -> dict:
    """Load YAML configuration file."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _extract_paths_from_config(config: dict, base_dir: str, section: Optional[str] = None) -> Dict[str, str]:
    """Extract paths from config section (input or output).

    Args:
        config: Configuration dict
        base_dir: Base directory to prepend to paths
        section: Section name ("input" or "output"), None for root level

    Returns:
        Dict mapping variable names to full paths
    """
    paths = {}

    if section:
        # New format: input/output with directory and children
        section_config = config.get(section, {})
        if not section_config:
            return paths

        directory = section_config.get("directory", "")
        children = section_config.get("children")

        # Handle case where children is None or not a dict
        if not children or not isinstance(children, dict):
            return paths

        for var_name, filename in children.items():
            if filename:  # Skip None/empty values
                full_path = f"{base_dir}/{directory}/{filename}" if directory else f"{base_dir}/{filename}"
                paths[var_name] = full_path
    else:
        # Old format or root level: direct key-value pairs
        for key, value in config.items():
            if key.startswith("_") or not isinstance(value, str):
                continue
            paths[key] = f"{base_dir}/{value}"

    return paths


def _parse_repo_tags(repo_id: str) -> tuple[str, str]:
    """Parse repo_id to extract start and end tags.

    Args:
        repo_id: Repository ID in format "owner_repo:start_tag_end_tag"

    Returns:
        Tuple of (start_tag, end_tag)
    """
    if ":" in repo_id:
        _, tags_part = repo_id.split(":", 1)
        parts = tags_part.split("_")
        if len(parts) >= 2 and parts[0] and parts[1]:
            return parts[0], parts[1]

    raise ValueError(f"Failed to parse repo_id. Expected format 'owner_repo:startTag_endTag', got {repo_id!r}")


def build_path_mapping(ctx: TrialContext, stage_config_file: str) -> Dict[str, str]:
    """Build path mapping from stage and shared configs.

    Args:
        ctx: Trial context with current_stage_id set
        stage_config_file: Stage configuration filename

    Returns:
        Dict mapping template variables to actual paths
    """
    if not ctx.current_stage_id:
        raise ValueError("current_stage_id must be set in context")

    stage_id = ctx.current_stage_id

    # Load stage config
    stage_config_path = Path(__file__).parent.parent / "configs" / stage_config_file
    stage_config = _load_yaml_config(stage_config_path)

    # Load shared config
    shared_config_path = Path(__file__).parent.parent / "configs" / "shared.yaml"
    shared_config = _load_yaml_config(shared_config_path)

    # Build path mapping
    # Note: Stage config variables will override shared config variables if names conflict
    path_mapping = {}

    # 1. First, extract paths from shared config (lower priority)
    # Input paths (from proc_data)
    shared_input_section = shared_config.get("input", {})
    shared_input_dir = shared_input_section.get("directory", "proc_data")
    shared_input_children = shared_input_section.get("children", {})

    for var_name, filename in shared_input_children.items():
        if filename:
            path_mapping[var_name] = f"{shared_input_dir}/{filename}"

    # Output paths (from result)
    shared_output_section = shared_config.get("output", {})
    shared_output_dir = shared_output_section.get("directory", "result")
    shared_output_children = shared_output_section.get("children", {})

    for var_name, filename in shared_output_children.items():
        if filename:
            path_mapping[var_name] = f"{shared_output_dir}/{filename}"

    # 2. Then, extract paths from stage config (higher priority - will override shared)
    # For stage config, we need to handle the directory field specially
    # Input section
    stage_input_section = stage_config.get("input", {})
    if stage_input_section and isinstance(stage_input_section, dict):
        stage_input_dir = stage_input_section.get("directory", "input")
        stage_input_children = stage_input_section.get("children")
        if stage_input_children and isinstance(stage_input_children, dict):
            for var_name, filename in stage_input_children.items():
                if filename:
                    # This will override shared config if same var_name exists
                    path_mapping[var_name] = f"{stage_id}/{stage_input_dir}/{filename}"

    # Output section
    stage_output_section = stage_config.get("output", {})
    if stage_output_section and isinstance(stage_output_section, dict):
        stage_output_dir = stage_output_section.get("directory", "output")
        stage_output_children = stage_output_section.get("children")
        if stage_output_children and isinstance(stage_output_children, dict):
            for var_name, filename in stage_output_children.items():
                if filename:
                    # This will override shared config if same var_name exists
                    path_mapping[var_name] = f"{stage_id}/{stage_output_dir}/{filename}"

    # 3. Add common variables
    try:
        start_tag, end_tag = _parse_repo_tags(ctx.repo_id)
        path_mapping["start_tag"] = start_tag
        path_mapping["end_tag"] = end_tag
        path_mapping["version_range"] = f"{start_tag} to {end_tag}"
    except ValueError:
        # If parsing fails, use placeholders
        path_mapping["start_tag"] = "START_TAG"
        path_mapping["end_tag"] = "END_TAG"
        path_mapping["version_range"] = "START_TAG to END_TAG"

    # Extract repo name from repo_id (e.g., "urllib3_urllib3" -> "urllib3/urllib3")
    repo_name = ctx.repo_id.split(":")[0] if ":" in ctx.repo_id else ctx.repo_id
    repo_name_formatted = repo_name.replace("_", "/", 1)  # Replace first underscore only
    path_mapping["repo_name"] = repo_name_formatted

    # Add direct paths
    path_mapping["repo_path"] = "repo_data"
    path_mapping["result_path"] = f"{stage_id}/output"

    # Add common aliases for backward compatibility
    if "main_commits" in path_mapping:
        path_mapping["main_commits_with_dag"] = path_mapping["main_commits"]
        path_mapping["main_commits_refined"] = path_mapping["main_commits"]

    # Add patch_dir alias
    if "patches" in path_mapping:
        path_mapping["patch_dir"] = path_mapping["patches"]

    # Note: subagent_prompt_file will be added in load_and_render_prompt()
    # after rendering the subagent prompt template

    return path_mapping


def render_prompt_template(template_content: str, path_mapping: Dict[str, str]) -> str:
    """Render prompt template with path mapping.

    Uses the shared template rendering utility from utils.template.

    Args:
        template_content: Raw template content with {{variable}} placeholders
        path_mapping: Dict mapping variable names to actual paths

    Returns:
        Rendered prompt string
    """
    return render_template_string(template_content, path_mapping)


def load_and_render_prompt(ctx: TrialContext, stage_config_file: str, prompt_file: str) -> tuple[str, Dict[str, str]]:
    """Load and render prompt template for a stage.

    Args:
        ctx: Trial context with current_stage_id set
        stage_config_file: Stage configuration filename
        prompt_file: Prompt template filename (relative to prompts/)

    Returns:
        Tuple of (rendered_prompt, path_mapping)
    """
    # Build path mapping
    path_mapping = build_path_mapping(ctx, stage_config_file)

    # Check if there's a subagent prompt that needs rendering
    stage_config_path = Path(__file__).parent.parent / "configs" / stage_config_file
    stage_config = _load_yaml_config(stage_config_path)
    subagent_prompt_template = stage_config.get("subagent_prompt_file")

    if subagent_prompt_template:
        # Render subagent prompt first
        subagent_template_path = Path(__file__).parent.parent / "prompts" / subagent_prompt_template
        if subagent_template_path.exists():
            with open(subagent_template_path, "r", encoding="utf-8") as f:
                subagent_template_content = f.read()

            # Render subagent prompt with current path mapping
            subagent_rendered = render_prompt_template(subagent_template_content, path_mapping)

            # Save rendered subagent prompt (note: this function doesn't have access to logger)
            # TODO: Refactor to accept logger parameter and use logger.save_text()
            subagent_rendered_path = Path(ctx.get_subagent_prompt_rendered_path())
            with open(subagent_rendered_path, "w", encoding="utf-8") as f:
                f.write(subagent_rendered)

            # Update path_mapping to point to rendered file (relative to workspace root)
            path_mapping["subagent_prompt_file"] = f"{ctx.current_stage_id}_subagent_prompt_rendered.md"

    # Check if there's a deep analysis subagent prompt (for stage4 complex milestone analysis)
    deep_analysis_prompt_template = stage_config.get("deep_analysis_subagent_prompt_file")
    if deep_analysis_prompt_template:
        deep_analysis_template_path = Path(__file__).parent.parent / "prompts" / deep_analysis_prompt_template
        if deep_analysis_template_path.exists():
            with open(deep_analysis_template_path, "r", encoding="utf-8") as f:
                deep_analysis_template_content = f.read()

            # Render deep analysis subagent prompt
            deep_analysis_rendered = render_prompt_template(deep_analysis_template_content, path_mapping)

            # Save rendered prompt
            deep_analysis_rendered_path = (
                Path(ctx.workspace_root) / f"{ctx.current_stage_id}_deep_analysis_subagent_rendered.md"
            )
            with open(deep_analysis_rendered_path, "w", encoding="utf-8") as f:
                f.write(deep_analysis_rendered)

            # Update path_mapping
            path_mapping["deep_analysis_subagent_prompt"] = f"{ctx.current_stage_id}_deep_analysis_subagent_rendered.md"

    # Load main template
    template_path = Path(__file__).parent.parent / "prompts" / prompt_file
    if not template_path.exists():
        raise FileNotFoundError(f"Prompt template not found: {template_path}")

    with open(template_path, "r", encoding="utf-8") as f:
        template_content = f.read()

    # Render main template
    rendered = render_prompt_template(template_content, path_mapping)

    return rendered, path_mapping
