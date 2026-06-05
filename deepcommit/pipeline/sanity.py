from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, List, Union

from .context import TrialContext
from .types import StageResult

if TYPE_CHECKING:
    from .utils.pipeline_logger import PipelineLoggerAdapter


def _must_exist(path: Union[Path, str], notes: List[str]) -> bool:
    if not Path(path).exists():
        notes.append(f"Missing required path: {path}")
        return False
    return True


def sanity_check(ctx: TrialContext, logger: "PipelineLoggerAdapter") -> StageResult:
    """Lightweight preflight checks before heavy stages.

    Rules:
    - Create: result/, logs/
    - Must exist: repo/ (symlink), preprocessed/ (symlink), deepcommit/collect/tools/, prompt file
    - repo/ and preprocessed/ are direct symlinks to specific data directories
    """
    print()
    logger.info("[0/8] Sanity check")

    notes: List[str] = []
    ok = True  # Initialize status flag

    # Ensure current working directory matches workspace root
    current_cwd = Path.cwd().resolve()
    expected_cwd = Path(ctx.workspace_root).resolve()
    if current_cwd != expected_cwd:
        notes.append(f"Current working directory {current_cwd} does not match workspace root {expected_cwd}")

    # Must exist: repo link (points to specific repository)
    repo_path = getattr(ctx, "repo_path", None)  # type: ignore
    if repo_path:
        ok = _must_exist(repo_path, notes) and ok
        ok = _must_exist(ctx.to_abs_path(repo_path), notes) and ok  # type: ignore

    # Must exist: preprocessed link (points to specific preprocessed data)
    release_path = getattr(ctx, "release_path", None)  # type: ignore
    if release_path:
        ok = _must_exist(release_path, notes) and ok
        ok = _must_exist(ctx.to_abs_path(release_path), notes) and ok  # type: ignore

    # Must exist: tools dir from copied deepcommit
    ok = _must_exist(ctx.tools_path, notes) and ok
    ok = _must_exist(ctx.to_abs_path(ctx.tools_path), notes) and ok

    # Must exist: prompt file (relative to workspace)
    prompt_path = getattr(ctx, "prompt_path", None)  # type: ignore
    if prompt_path:
        ok = _must_exist(prompt_path, notes) and ok
        ok = _must_exist(ctx.to_abs_path(prompt_path), notes) and ok  # type: ignore

    status = "ok" if ok else "error"
    if status == "error":
        for n in notes:
            logger.error("Sanity: %s", n)
    else:
        for n in notes:
            logger.warning("Sanity: %s", n)

    return StageResult(
        status=status,
        artifacts={
            "raw_path": str(getattr(ctx, "raw_path", "")),  # type: ignore
            "tools_path": str(ctx.tools_path),
            "prompt_path": str(getattr(ctx, "prompt_path", "")),  # type: ignore
            "bare_repo_root": str(getattr(ctx, "repo_path", "")),  # type: ignore
        },
        notes="; ".join(notes),
    )
