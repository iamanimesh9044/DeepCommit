from __future__ import annotations

import argparse
import os
import sys
from typing import List

from .context import TrialContext
from .sanity import sanity_check
from .stages.agent import run_agent
from .stages.analysis import analyze_outputs
from .stages.milestone_enrichment import enrich_milestones
from .stages.preprocess import preprocess_data
from .stages.prompts import load_prompts
from .stages.recovery import check_and_recover
from .stages.tools import configure_tools
from .stages.upload import upload_results
from .utils.io import ensure_dir, setup_logger


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deepcommit pipeline main (template)")
    p.add_argument("--trial-name", type=str, required=True)
    p.add_argument("--workspace-root", type=str, required=True)
    p.add_argument("--repo-id", type=str, required=True)
    p.add_argument(
        "--prompt-file",
        type=str,
        required=True,
        help="Prompt template path relative to <workspace>/deepcommit/prompt/ (supports subdirectories, e.g., 'mcp/dev.tmpl')",
    )
    p.add_argument(
        "--agents", type=str, default="claude", help="input a list of agents, for example: `claude,openhands,codex`"
    )
    p.add_argument(
        "--tool-list",
        type=str,
        default="all",
        help="input a list of mcp tools used by agents, for example: `git_analysis, ...`",
    )
    p.add_argument("--code-commit-sha", type=str, required=True)
    p.add_argument("--max-retries", type=int, default=2)
    return p.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    ns = parse_args(sys.argv[1:] if argv is None else argv)

    # Setup context
    code_sha = ns.code_commit_sha or ns.code_version or "unknown"
    ctx = TrialContext(
        trial_name=ns.trial_name,
        repo_id=str(ns.repo_id) if ns.repo_id else "",
        prompt_file=ns.prompt_file,
        agents=ns.agents,
        tool_list=ns.tool_list,
        code_commit_sha=code_sha,
        max_retries=ns.max_retries,
        workspace_root=ns.workspace_root,
    )

    # Ensure result/logs/proc created
    result_abs = os.path.realpath(ctx.result_path)
    ensure_dir(result_abs)
    ensure_dir(ctx.logs_path)
    ensure_dir(ctx.proc_path)

    # Setup logger
    logger = setup_logger(ctx.logs_path)
    print()
    logger.info(ctx.get_info_string())

    # 0) Sanity check
    res_sanity = sanity_check(ctx, logger)
    ctx.set_stage_results("Stage 0 Sanity", res_sanity)
    if res_sanity.status == "error":
        return 2

    # 1) Preprocess
    res_pre = preprocess_data(ctx, logger)
    ctx.set_stage_results("Stage 1 Preprocess", res_pre)
    if res_pre.status == "error":
        return 1

    # 2) Prompts
    res_prompt = load_prompts(ctx, logger)
    ctx.set_stage_results("Stage 2 Prompts", res_prompt)
    if res_prompt.status == "error":
        return 1

    # 3) Tools
    res_tools = configure_tools(ctx, logger)
    ctx.set_stage_results("Stage 3 Tools", res_tools)
    if res_tools.status == "error":
        return 1

    # 4) Agent run (all paths from ctx)
    res_agent = run_agent(ctx, logger)
    ctx.set_stage_results("Stage 4 Agent Run", res_agent)
    # if res_agent.status == "error":
    #     return 1

    # 5) Recovery
    res_recover = check_and_recover(ctx, res_agent, logger)
    ctx.set_stage_results("Stage 5 Recovery", res_recover)
    if res_recover.status == "error":
        return 1

    # 6) Milestone enrichment
    res_enrichment = enrich_milestones(ctx, logger)
    ctx.set_stage_results("Stage 6 Milestone Enrichment", res_enrichment)
    if res_enrichment.status == "error":
        return 1

    # 7) Analysis
    res_analysis = analyze_outputs(ctx, logger)
    ctx.set_stage_results("Stage 7 Analysis", res_analysis)
    if res_analysis.status == "error":
        return 1

    # 8) Upload
    res_upload = upload_results(ctx, res_analysis, res_agent, logger)
    ctx.set_stage_results("Stage 8 Upload", res_upload)
    if res_upload.status == "error":
        return 1

    print()
    logger.info("Trial completed: %s", ctx.trial_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
