from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..context import TrialContext
from ..types import StageResult
from ..utils.io import read_text
from ..utils.template import render_template_string

if TYPE_CHECKING:
    from ..utils.pipeline_logger import PipelineLoggerAdapter


def _parse_tags(repo_id: str) -> tuple[str, str]:
    """Extract start and end tags from the repo identifier."""

    # Expected format: owner_repo:startTag_endTag
    # Example: urllib3_urllib3:2.0.6_2.3.0
    if ":" in repo_id:
        _, tags_part = repo_id.split(":", 1)
        parts = tags_part.split("_")
        if len(parts) >= 2 and parts[0] and parts[1]:
            return parts[0], parts[1]

    raise ValueError(f"Failed to parse repo_id. Expected format 'owner_repo:startTag_endTag', got {repo_id!r}")


# Use the shared template rendering utility from utils.template
# (Kept as alias for backward compatibility)
_render_template = render_template_string


def load_prompts(ctx: TrialContext, logger: "PipelineLoggerAdapter") -> StageResult:
    """Load and render prompt template."""

    print()
    logger.info("[2/8] Load & render prompt template")
    prompt_path = getattr(ctx, "prompt_path", None)  # type: ignore
    raw = read_text(str(prompt_path), default="")

    try:
        start_tag, end_tag = _parse_tags(ctx.repo_id)
    except ValueError as exc:
        logger.error("Unable to parse tags from repo_id '%s': %s", ctx.repo_id, exc)
        raise SystemExit(1) from exc

    # Build template mapping with basic variables
    mapping = {
        "repo_path": getattr(ctx, "repo_path", ""),  # type: ignore
        "release_path": getattr(ctx, "release_path", ""),  # type: ignore
        "result_path": ctx.result_path,
        "code_maat_jar": "/tmp/code-maat-1.0.4-standalone.jar",
        "start_tag": start_tag,
        "end_tag": end_tag,
        "mcp_config_path": getattr(ctx, "mcp_config_path", ""),  # type: ignore
        "repo_src_dir": "src/",
    }

    # Add MCP tool mappings from stage config
    try:
        mcp_tool_mappings = ctx.get_mcp_tool_mappings()
        mapping.update(mcp_tool_mappings)
        logger.debug(f"Added {len(mcp_tool_mappings)} MCP tool mappings to template context")
    except ValueError as e:
        # If agent_platform is not claude_code, this will raise ValueError
        logger.error(f"Failed to get MCP tool mappings: {e}")
        raise SystemExit(1) from e

    # Render template with all mappings
    rendered = _render_template(raw, mapping)

    # Use logger's save_text method for unified file output management
    rendered_path = logger.save_text(
        content=rendered,
        full_path=Path(ctx.get_prompt_rendered_path()),
        archive=True,
    )
    return StageResult(
        status="ok",
        artifacts={
            "prompt_path": str(prompt_path),
            "prompt_template": raw,
            "prompt_rendered_path": rendered_path,
            "prompt_vars": mapping,
        },
    )
