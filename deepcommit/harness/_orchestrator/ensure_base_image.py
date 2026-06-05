"""Ensure the base image exists for Main Agent (reuse or build-on-miss).

This is NOT a builder — the actual base image is constructed by
``image_builder/build_image.py --target base``. This module is the
precondition gate Main Agent runs before any iteration: check whether
``{repo_id}/base:latest`` already exists, reuse it if so, otherwise
subprocess the real builder, and re-verify the image actually landed
(the builder's rc alone isn't a reliable signal).

The expected base image name is derived the same way the other Phase 3
modules derive it: ``{repo_id}/base:latest`` (lowercased), where
``repo_id`` is the stem of the config YAML path (matching how
``infer_paths_from_work_dir`` handles it downstream).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def expected_base_image_name(config_path: Path) -> str:
    """Derive the base image tag the other modules will look up."""
    return f"{config_path.stem}/base:latest".lower()


def image_exists(image_name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "images", "-q", image_name],
            capture_output=True,
            text=True,
            check=True,
        )
        return bool(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def ensure_base_image(
    config_path: Path,
    repo_path: Path,
    *,
    timeout_seconds: int = 3600,
    model: str = "claude-opus-4-6",
) -> bool:
    """Make sure ``{repo_id}/base:latest`` exists. Build it if it doesn't.

    Returns True on success (image present at the end), False on any
    build failure. On success the base image is usable by Stage1 +
    Module 2 downstream without further configuration.
    """
    image_name = expected_base_image_name(config_path)

    if image_exists(image_name):
        logger.info(f"[ensure_base_image] Base image {image_name} already exists; reusing")
        return True

    logger.info(f"[ensure_base_image] Base image {image_name} missing; invoking build_image.py")

    cmd = [
        sys.executable,
        "-m",
        "deepcommit.harness.prepare_images.image_builder.build_image",
        "--config",
        str(config_path),
        "--target",
        "base",
        "--repo-path",
        str(repo_path),
        "--model",
        model,
    ]
    logger.info(f"[ensure_base_image] exec: {' '.join(cmd)}")

    try:
        rc = subprocess.run(cmd, timeout=timeout_seconds).returncode
    except subprocess.TimeoutExpired:
        logger.error(f"[ensure_base_image] build_image.py timed out after {timeout_seconds}s")
        return False

    if rc != 0:
        logger.error(f"[ensure_base_image] build_image.py exited with rc={rc}")
        return False

    # Verify the image really got built (the script's rc doesn't always reflect it).
    if not image_exists(image_name):
        logger.error(f"[ensure_base_image] build_image.py reported success but {image_name} is still missing")
        return False

    logger.info(f"[ensure_base_image] Base image {image_name} built successfully")
    return True
