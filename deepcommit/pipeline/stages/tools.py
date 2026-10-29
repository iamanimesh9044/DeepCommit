from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ..context import TrialContext
from ..types import StageResult
from ..utils.template import render_template_recursive

if TYPE_CHECKING:
    from ..utils.pipeline_logger import PipelineLoggerAdapter


def configure_tools(ctx: TrialContext, logger: "PipelineLoggerAdapter") -> StageResult:
    """Configure MCP tools for this stage.

    Loads MCP server configurations and generates trial.mcp.json with appropriate
    tool filtering based on stage configuration.

    For multi-stage pipelines:
    - Gets required tools from current stage config (ctx.get_mcp_tools())
    - Generates trial.mcp.json in stage workspace (e.g., workspace_root/stage1_seed/trial.mcp.json)

    For single-stage mode:
    - Uses ctx.requested_tools() (from command-line args)
    - Generates trial.mcp.json at workspace root
    """

    print()
    logger.info("[3/8] Configure MCP tools")

    tools_config_dir = Path(ctx.tools_config_path)
    if not tools_config_dir.is_dir():
        error_msg = f"Tools config directory missing: {tools_config_dir}"
        logger.error(error_msg)
        return StageResult(status="error", error=error_msg, artifacts={"tools_config_dir": str(tools_config_dir)})

    # Get MCP tools configuration from stage config
    # Format: {"git_analysis": ["get_commit_context", "search_entities"], ...}
    if ctx.is_multi_stage:
        mcp_tools_config = ctx.get_mcp_tools()
        logger.info(f"Stage '{ctx.current_stage_id}' MCP configuration:")
        for server_name, tool_list in mcp_tools_config.items():
            logger.info(f"  - {server_name}: {len(tool_list)} tool(s) - {', '.join(tool_list)}")
    else:
        # Single-stage mode: use command-line args (backward compatibility)
        requested_tools = ctx.requested_tools()
        mcp_tools_config = {"git_analysis": requested_tools} if requested_tools else {}

    # Load and configure each MCP server
    mcp_servers: dict[str, object] = {}
    for server_name, tool_list in mcp_tools_config.items():
        # Load the server config file (e.g., git_analysis.json)
        config_file = tools_config_dir / f"{server_name}.json"
        if not config_file.exists():
            error_msg = f"MCP server config not found: {config_file}"
            logger.error(error_msg)
            return StageResult(
                status="error",
                error=error_msg,
                artifacts={
                    "tools_config_dir": str(tools_config_dir),
                    "missing_config": server_name,
                },
            )

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                server_config = json.load(f)

            # Add ENABLED_MCP_TOOLS to template variables for this server
            server_template_vars = {
                "REPO_PATH": ctx.get_repo(),
                "PREPROCESSED_PATH": ctx.get_preprocess(),
                "COMMIT_DAG_PATH": ctx.get_input("commit_dag"),
                "REPO_SRC_DIRS": json.dumps(ctx.get_repo_src_dirs()),  # Serialize list to JSON
                "REPO_TEST_DIRS": json.dumps(ctx.test_dirs) if ctx.test_dirs else "",  # Test directories
                "REPO_EXCLUDE": json.dumps(ctx.exclude) if ctx.exclude else "",  # Additional exclude patterns
                "PYTHON_BIN": sys.executable,
                "ENABLED_MCP_TOOLS": ",".join(tool_list) if tool_list else "",
            }

            # Render template variables
            server_config = render_template_recursive(server_config, server_template_vars)
            # Multi-repo: tell the MCP server where repos.json / commit_repos.json live (absent for single repo)
            workspace_dir = getattr(ctx, "workspace_dir", None)
            if workspace_dir:
                server_config.setdefault("env", {})["DEEPCOMMIT_WORKSPACE_DIR"] = str(workspace_dir)
            mcp_servers[server_name] = server_config

        except Exception as exc:
            error_msg = f"Failed to load MCP server config {config_file}: {exc}"
            logger.error(error_msg)
            return StageResult(
                status="error",
                error=error_msg,
                artifacts={
                    "tools_config_dir": str(tools_config_dir),
                    "failed_config": server_name,
                },
            )

    # Save MCP config to working directory
    try:
        # trial.mcp.json is a temporary config file for agent execution
        mcp_path = logger.save_json(
            data={"mcpServers": mcp_servers},
            full_path=Path(ctx.get_mcp_config_path()),
            archive=False,  # Don't archive - it's a temporary config
        )
        logger.info("MCP config generated with %d server(s)", len(mcp_servers))
    except Exception as exc:
        error_msg = f"Failed to write MCP config: {exc}"
        logger.error(error_msg)
        return StageResult(
            status="error",
            error=error_msg,
            artifacts={
                "tools_config_dir": str(tools_config_dir),
            },
        )

    return StageResult(
        status="ok",
        artifacts={
            "tools_config_dir": str(tools_config_dir),
            "mcp_config_path": str(mcp_path),
            "mcp_servers": list(mcp_servers.keys()),
            "tools_config": mcp_tools_config,
        },
    )
