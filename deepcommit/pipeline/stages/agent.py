from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ...tools.claude_agent_tool import run_claude_cli_agent
from ..context import TrialContext
from ..types import StageResult
from ..utils.io import ensure_dir, read_text

if TYPE_CHECKING:
    from ..utils.pipeline_logger import PipelineLoggerAdapter


def run_agent(
    ctx: TrialContext,
    logger: "PipelineLoggerAdapter",
    session_id: str,
    resume: bool = False,
    override_prompt: Optional[str] = None,
) -> StageResult:
    """Run Claude CLI Agent via MCP-style wrapper and write outputs.

    All required paths are obtained from ctx:
    - Prompt: {stage_id}_prompt_rendered.md in workspace_root (or override_prompt)
    - MCP config: ctx.get_mcp_config_path()

    Args:
        ctx: Trial context
        logger: Logger instance
        session_id: Session ID (UUID) - REQUIRED for precise tracking
        resume: If True, resume existing session; if False, create new session
        override_prompt: If provided, use this instead of rendered prompt file (for fix/retry mode)

    Falls back to minimal outputs if CLI is unavailable or parsing fails.
    """

    print()
    logger.info("[4/8] Run agent(s) and generate outputs -> %s", ctx.get_stage_output_dir())
    ensure_dir(str(ctx.get_stage_output_dir()))

    # Get MCP config path (stage-specific in multi-stage mode)
    mcp_config_path = ctx.get_mcp_config_path()

    # Use override_prompt if provided (fix mode), else read from file
    if override_prompt:
        prompt_text = override_prompt
        rendered_path = None
        logger.info("Using override prompt (fix mode)")
    else:
        # Get prompt path from ctx (rendered prompt is saved by ctx.render_prompt())
        rendered_path = Path(ctx.get_prompt_rendered_path())

        if not rendered_path.is_file():
            error_msg = f"Prompt file not found: {rendered_path}"
            logger.error(error_msg)
            return StageResult(status="error", error=error_msg, artifacts={"prompt_rendered_path": str(rendered_path)})

        prompt_text = read_text(str(rendered_path), default="")

    model = "claude-opus-4-6"
    timeout_ms = 1_800_000  # 30 minutes

    # Use stage-specific logs directory in multi-stage mode
    if ctx.is_multi_stage:
        log_dir = str(ctx.get_stage_logs_dir())
    else:
        log_dir = ctx.logs_path

    try:
        res = run_claude_cli_agent(
            prompt_text=prompt_text,
            workspace_root=ctx.workspace_root,
            session_id=session_id,
            resume=resume,
            model=model,
            mcp_config=mcp_config_path,
            timeout_ms=timeout_ms,
            extra_args=["--effort", "max"],
            env_overrides={},
            log_dir=log_dir,
            add_default_cli_flags=True,
            output_format="json",
            dangerously_skip_permissions=True,
            logger=logger,
        )
        cli_status = res.status
        exit_code = res.exit_code
        stdout = res.stdout or ""
        stderr = res.stderr or ""
        log_path = res.log_path

        # Special handling for timeout
        if cli_status == "timeout":
            logger.warning("Agent execution timed out after %dms. Check log: %s", timeout_ms, log_path)
    except (OSError, IOError, PermissionError) as e:
        cli_status = "error"
        exit_code = 1
        stdout = ""
        stderr = f"File system error: {e}"
        log_path = None
        logger.error("File system error during agent execution: %s", e)
    except Exception as e:
        cli_status = "error"
        exit_code = 1
        stdout = ""
        stderr = f"Unexpected error: {e}"
        log_path = None
        logger.error("Agent execution failed: %s", e, exc_info=True)

    # Short debug of stdout['result'] if JSON, else stdout prefix; and stderr
    parsed_stdout = None
    try:
        parsed_stdout = json.loads(stdout) if stdout and stdout.strip() else None
    except Exception:
        parsed_stdout = None
    cli_result_text = None
    num_turns: Optional[int] = None
    if isinstance(parsed_stdout, dict):
        if "result" in parsed_stdout:
            cli_result_text = parsed_stdout.get("result")
        if isinstance(parsed_stdout.get("num_turns"), int):
            num_turns = parsed_stdout.get("num_turns")

    # Note: session_id is now passed as a parameter, no need to extract from stdout

    if cli_result_text is not None:
        preview = (
            cli_result_text
            if isinstance(cli_result_text, str)
            else json.dumps(cli_result_text, indent=2, ensure_ascii=False)
        )
        top_bottom = "+" + "-" * 78 + "+"
        middle = "|" + "AGENT CLI RESULT".center(78) + "|"
        logger.info("\n\n%s\n%s\n%s\n\n%s\n\n%s\n", top_bottom, middle, top_bottom, preview, top_bottom)
    elif stdout and stdout.strip():
        logger.error("No result in Agent CLI stdout (prefix): %s", stdout.strip().replace("\n", " ")[:200])
    if stderr and stderr.strip():
        logger.error("Agent CLI stderr (prefix): %s", stderr.strip().replace("\n", " ")[:200])

    # Prepare artifacts (CSV validation is now stage-specific)
    mcp_config_path = Path(ctx.get_mcp_config_path())
    artifacts = {
        "prompt_rendered_path": str(Path(rendered_path).resolve()) if rendered_path else "(override_prompt)",
        "mcp_config_path": str(mcp_config_path),
        "model": model,
        "agent_cli_status": cli_status,
        "agent_cli_exit_code": exit_code,
        "agent_cli_log_path": str(Path(log_path).resolve()) if log_path else "",
        "agent_cli_stderr": (stderr or "")[:500],
        "agent_cli_num_turns": num_turns if isinstance(num_turns, int) else None,
        "agent_cli_session_id": session_id,  # Now always provided as parameter
    }

    # Save artifacts to stage archive directory only (not global result/)
    archive_dir = logger.get_archive_dir()
    logger.save_json(
        data=artifacts,
        full_path=archive_dir / "agent_artifacts.json",
        archive=False,  # Already saving to archive location
    )

    # Rename conversation logs with stage prefix
    if ctx.is_multi_stage and log_path:
        stage_logs_dir = Path(log_dir)
        for conv_file in stage_logs_dir.glob("claude-conversation-*.md"):
            # Check if using flat structure (harness) vs nested structure (multi-stage)
            use_flat = getattr(ctx, "use_flat_structure", False)
            if use_flat:
                # Harness: rename in place with stage prefix (no result/ dir)
                new_name = f"{ctx.current_stage_id}_{conv_file.name}"
                new_path = conv_file.parent / new_name
                conv_file.rename(new_path)
                logger.info(f"Renamed conversation log: {conv_file.name} -> {new_name}")
            else:
                # Multi-stage: archive to result/logs/
                logger.archive_file(
                    src=conv_file,
                    global_filename=f"logs/{ctx.current_stage_id}_{conv_file.name}",
                    skip_stage_archive=True,
                )

    return StageResult(
        status="ok",
        artifacts=artifacts,
    )
