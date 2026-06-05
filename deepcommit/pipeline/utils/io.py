from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .pipeline_logger import setup_pipeline_logger  # noqa: F401  re-exported for pipeline.main_* consumers


def ensure_dir(path: str) -> None:
    # Use os.makedirs to work properly when parent is a symlink to a directory
    os.makedirs(path, exist_ok=True)


def write_json(path: str, data: Any) -> None:
    ensure_dir(str(Path(path).parent))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def read_text(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


def setup_logger(log_dir: str, level: int = logging.INFO) -> logging.Logger:
    ensure_dir(log_dir)
    log_path = str(Path(log_dir) / "pipeline.log")
    logger = logging.getLogger("deepcommit.pipeline")
    logger.setLevel(level)

    # Avoid duplicate handlers if re-initialized
    if not logger.handlers:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(level)
        ch = logging.StreamHandler()
        ch.setLevel(level)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        fh.setFormatter(fmt)
        ch.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(ch)
    return logger
