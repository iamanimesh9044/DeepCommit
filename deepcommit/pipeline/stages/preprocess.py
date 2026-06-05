from __future__ import annotations

import logging
import os

from ..context import TrialContext
from ..types import StageResult


def preprocess_data(ctx: TrialContext, logger: logging.Logger) -> StageResult:
    """Placeholder for preprocessing data.

    repo/ symlink points to specific repository in DATA/github_data/repos/
    preprocessed/ symlink points to preprocessed data in DATA/github_data/preprocessed/
    Results written to ctx.proc_path if needed.
    """
    print()
    logger.info("[1/8] Preprocess data -> %s", ctx.proc_path)

    # Placeholder: no actual processing, touch a marker file
    try:
        marker = os.path.join(ctx.proc_path, "_PREPROCESS_PLACEHOLDER.txt")
        with open(marker, "w", encoding="utf-8") as f:
            f.write("preprocess placeholder\n")
        return StageResult(status="ok", artifacts={"proc_path": ctx.proc_path})
    except Exception as e:
        logger.exception("Preprocess failed: %s", e)
        return StageResult(status="error", error=str(e))
