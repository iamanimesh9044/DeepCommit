from __future__ import annotations

import logging
import os

from ...analysis.claude_agent_tool import run_claude_cli_agent
from ..context import TrialContext
from ..types import StageResult


def _has_success(result_dir: str) -> bool:
    if not os.path.isdir(result_dir):
        return False
    if os.path.exists(os.path.join(result_dir, "SUCCESS")):
        return True
    if os.path.exists(os.path.join(result_dir, "milestones.csv")) and os.path.exists(
        os.path.join(result_dir, "dependencies.csv")
    ):
        return True
    return False


def check_and_recover(ctx: TrialContext, res_agent: StageResult, logger: logging.Logger) -> StageResult:
    """Check outputs and recover by resuming Claude session if needed.

    If agent status is error and artifacts are missing, attempts to resume the
    Claude session using session_id from parsed_stdout, up to ctx.max_retries times.
    """

    print()
    logger.info("[5/8] Check outputs and recover if needed")

    # If agent succeeded and artifacts exist, no recovery needed
    if res_agent.status == "ok" and _has_success(ctx.result_path):
        return StageResult(status="ok", artifacts={"retries": 0, "recovered": False})

    # If agent status is not error, but artifacts missing, log warning but don't recover
    if res_agent.status != "error":
        logger.warning("Agent status is %s but artifacts incomplete", res_agent.status)
        if _has_success(ctx.result_path):
            return StageResult(status="ok", artifacts={"retries": 0, "recovered": False})

    # Extract session_id from agent result
    session_id = res_agent.artifacts.get("agent_cli_session_id", "")
    if not session_id:
        logger.error("No session_id found in agent output, cannot resume")
        return StageResult(status="error", error="Cannot recover: no session_id available")

    logger.info("Found session_id: %s, attempting recovery", session_id)

    # Get validation error from agent artifacts to pass as recovery prompt
    validation_error = res_agent.artifacts.get("validation_error", "")  # noqa: F841
    recovery_prompt = """Continue"""

    # Note: All environment variables (including DEEPCOMMIT_REPO_DIR, DEEPCOMMIT_PREPROC_DIR,
    # PYTHON_BIN, etc.) are now configured in trial.mcp.json and passed to MCP server automatically.
    # No need for env_overrides here.
    env_overrides = {}

    model = res_agent.artifacts.get("model", "claude-opus-4-6")
    mcp_config_path = res_agent.artifacts.get("mcp_config_path")

    # Retry loop
    for retry_num in range(1, ctx.max_retries + 1):
        logger.warning("Recovery attempt %d/%d: resuming session %s", retry_num, ctx.max_retries, session_id)

        try:
            res = run_claude_cli_agent(
                prompt_text=recovery_prompt,
                workspace_root=ctx.workspace_root,
                model=model,
                mcp_config=str(mcp_config_path) if mcp_config_path else None,
                timeout_ms=600_000,
                extra_args=["--effort", "max"],
                env_overrides=env_overrides,
                log_dir=ctx.logs_path,
                add_default_cli_flags=True,
                output_format="json",
                dangerously_skip_permissions=True,
                resume_session_id=session_id,
            )

            logger.info("Recovery attempt %d completed with status: %s", retry_num, res.status)

            # Check if artifacts are now present
            if _has_success(ctx.result_path):
                logger.info("Recovery successful after %d attempt(s)", retry_num)
                return StageResult(
                    status="ok",
                    artifacts={
                        "retries": retry_num,
                        "recovered": True,
                        "session_id": session_id,
                    },
                )
            else:
                logger.warning("Recovery attempt %d did not produce valid artifacts", retry_num)

        except Exception as e:
            logger.exception("Recovery attempt %d failed with exception: %s", retry_num, e)
            continue

    # Max retries exceeded
    logger.error("Max retries (%d) exceeded; recovery failed", ctx.max_retries)
    return StageResult(
        status="error",
        error=f"Max retries ({ctx.max_retries}) exceeded; no valid artifacts generated",
        artifacts={"retries": ctx.max_retries, "recovered": False, "session_id": session_id},
    )
