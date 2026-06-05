"""Pipeline logger adapter with file output capabilities.

Uses LoggerAdapter pattern (recommended by Python docs) to extend standard
logging.Logger with file I/O operations.

Reference:
https://docs.python.org/3/howto/logging-cookbook.html#using-loggeradapter-to-impart-contextual-information
"""

from __future__ import annotations

import json
import logging
import shutil
from logging import LoggerAdapter
from pathlib import Path
from typing import Any, Optional


class ColoredFormatter(logging.Formatter):
    """Formatter that adds ANSI color codes to log messages."""

    # ANSI color codes
    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[0m",  # Default (no color)
        "WARNING": "\033[31m",  # Red (same as ERROR for visibility)
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    def format(self, record):
        # Get the original formatted message
        log_message = super().format(record)

        # Add color based on log level
        color = self.COLORS.get(record.levelname, self.RESET)
        return f"{color}{log_message}{self.RESET}"


class PipelineLoggerAdapter(LoggerAdapter):
    """Pipeline logger with file output capabilities.

    Extends standard logging.Logger with file I/O methods while maintaining
    full compatibility with Python's logging ecosystem.

    Design:
    - Wraps standard logging.Logger (composition over inheritance)
    - Adds file output methods (save_json, save_text, copy_file)
    - Automatic archiving to output directory
    - Unified logging for all operations

    Responsibilities:
    1. Standard logging (info, warning, error, debug) - delegated to wrapped logger
    2. File output operations with automatic archiving
    3. Contextual information in log messages (stage_id)

    Usage:
        >>> logger = setup_pipeline_logger(...)
        >>> logger.info("Starting stage")  # Standard logging
        >>> logger.save_json("config.json", data)  # File output + logging
    """

    def __init__(
        self,
        logger: logging.Logger,
        workspace_root: Path,
        is_multi_stage: bool = False,
        current_stage_id: Optional[str] = None,
        result_dir: str = "result",
        output_subdir: str = "output",
    ):
        """Initialize pipeline logger adapter.

        Args:
            logger: Standard logging.Logger instance to wrap
            workspace_root: Root directory of the trial workspace
            is_multi_stage: Whether in multi-stage mode
            current_stage_id: Current stage ID (required if is_multi_stage=True)
            result_dir: Result directory name (default: "result")
            output_subdir: Output subdirectory name within stage workspace (default: "output")

        Raises:
            ValueError: If is_multi_stage=True but current_stage_id is None
        """
        # Extra context for log messages
        extra = {
            "stage_id": current_stage_id or "single",
        }
        super().__init__(logger, extra)

        # File output configuration
        self.workspace_root = workspace_root
        self.is_multi_stage = is_multi_stage
        self.current_stage_id = current_stage_id
        self.result_dir = result_dir
        self.output_subdir = output_subdir

        if is_multi_stage and not current_stage_id:
            raise ValueError("current_stage_id is required in multi-stage mode")

    def set_stage_id(self, stage_id: str) -> None:
        """Update the current stage ID for logging and file operations.

        This allows reusing the same logger adapter across multiple stages
        without creating new instances.

        Args:
            stage_id: New stage ID to set

        Example:
            >>> logger = setup_pipeline_logger(...)
            >>> logger.set_stage_id("stage1_seed")
            >>> logger.info("Starting stage 1")  # Logs: "[stage1_seed] Starting stage 1"
            >>> logger.set_stage_id("stage2_milestone")
            >>> logger.info("Starting stage 2")  # Logs: "[stage2_milestone] Starting stage 2"
        """
        self.current_stage_id = stage_id
        # Update extra dict (need to cast to dict for type checker)
        if isinstance(self.extra, dict):
            self.extra["stage_id"] = stage_id

    def process(self, msg, kwargs):
        """Add contextual information to log records.

        Prepends stage_id to all log messages for better traceability.

        Args:
            msg: Log message
            kwargs: Keyword arguments

        Returns:
            Tuple of (processed_msg, kwargs)
        """
        # Add stage_id prefix to message
        stage_id = self.extra.get("stage_id", "unknown") if self.extra else "unknown"
        stage_prefix = f"[{stage_id}] "
        return f"{stage_prefix}{msg}", kwargs

    def get_working_dir(self) -> Path:
        """Get the working directory for current stage.

        Returns:
            Path to working directory
            - Single-stage: workspace_root/
            - Multi-stage: workspace_root/stage_id/
        """
        if self.is_multi_stage:
            if not self.current_stage_id:
                raise ValueError("current_stage_id is required in multi-stage mode")
            return self.workspace_root / self.current_stage_id
        else:
            return self.workspace_root

    def get_archive_dir(self) -> Path:
        """Get the archive directory for file outputs.

        Returns:
            Path to archive directory
            - Single-stage: workspace_root/result/
            - Multi-stage: workspace_root/result/stage_id/
        """
        if self.is_multi_stage:
            if not self.current_stage_id:
                raise ValueError("current_stage_id is required in multi-stage mode")
            return self.workspace_root / self.result_dir / self.current_stage_id
        else:
            return self.workspace_root / self.result_dir

    def save_json(
        self,
        data: Any,
        *,
        filename: Optional[str] = None,
        full_path: Optional[Path] = None,
        archive: bool = True,
        indent: int = 2,
    ) -> Path:
        """Save JSON data to file with automatic logging and archiving.

        Args:
            data: Data to save (will be JSON serialized)
            filename: Filename only (saved to stage working dir) - mutually exclusive with full_path
            full_path: Complete file path (from ctx) - mutually exclusive with filename
            archive: Whether to also save a copy to archive directory (default: True)
            indent: JSON indentation level (default: 2)

        Returns:
            Path to the saved file

        Raises:
            ValueError: If both or neither filename/full_path are provided
            Exception: If file write fails (also logs error)

        Example:
            >>> # Mode 1: Simple filename (saves to stage working dir)
            >>> logger.save_json(data={"key": "value"}, filename="config.json")
            PosixPath('/workspace/stage1_seed/config.json')

            >>> # Mode 2: Full path from ctx
            >>> logger.save_json(data={...}, full_path=ctx.get_mcp_config_path())
            PosixPath('/workspace/stage1_seed/trial.mcp.json')
        """
        if full_path is None and filename is None:
            raise ValueError("Either filename or full_path must be provided")
        if full_path is not None and filename is not None:
            raise ValueError("Cannot specify both filename and full_path")

        if full_path:
            work_path = Path(full_path)
        else:
            work_dir = self.get_working_dir()
            work_path = work_dir / filename  # type: ignore

        # Ensure directory exists
        work_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Save to working directory
            with open(work_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=indent, ensure_ascii=False)

            self.info(f"Saved JSON: {work_path}")

            # Archive if requested
            if archive:
                archive_dir = self.get_archive_dir()
                archive_path = archive_dir / work_path.name
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(work_path, archive_path)
                self.debug(f"Archived to: {archive_path}")

            return work_path

        except Exception as e:
            self.error(f"Failed to save JSON to {work_path}: {e}")
            raise

    def save_text(
        self,
        content: str,
        *,
        filename: Optional[str] = None,
        full_path: Optional[Path] = None,
        archive: bool = False,
    ) -> Path:
        """Save text content to file with automatic logging and archiving.

        Args:
            content: Text content to save
            filename: Filename only (saved to stage working dir) - mutually exclusive with full_path
            full_path: Complete file path (from ctx) - mutually exclusive with filename
            archive: Whether to also save a copy to archive directory (default: True)

        Returns:
            Path to the saved file

        Raises:
            ValueError: If both or neither filename/full_path are provided
            Exception: If file write fails (also logs error)

        Example:
            >>> # Mode 1: Simple filename
            >>> logger.save_text(content="Hello", filename="output.txt")
            PosixPath('/workspace/stage1_seed/output.txt')

            >>> # Mode 2: Full path from ctx
            >>> logger.save_text(content="...", full_path=ctx.get_prompt_rendered_path())
            PosixPath('/workspace/stage1_seed_prompt_rendered.md')
        """
        if full_path is None and filename is None:
            raise ValueError("Either filename or full_path must be provided")
        if full_path is not None and filename is not None:
            raise ValueError("Cannot specify both filename and full_path")

        if full_path:
            work_path = Path(full_path)
        else:
            work_dir = self.get_working_dir()
            work_path = work_dir / filename  # type: ignore

        # Ensure directory exists
        work_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Save to working directory
            with open(work_path, "w", encoding="utf-8") as f:
                f.write(content)

            self.info(f"Saved text: {work_path}")

            # Archive if requested
            if archive:
                archive_dir = self.get_archive_dir()
                archive_path = archive_dir / work_path.name
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(work_path, archive_path)
                self.debug(f"Archived to: {archive_path}")

            return work_path

        except Exception as e:
            self.error(f"Failed to save text to {work_path}: {e}")
            raise

    def copy_file(
        self,
        src: Path,
        *,
        filename: Optional[str] = None,
        dest_full_path: Optional[Path] = None,
        archive: bool = True,
    ) -> Path:
        """Copy file to destination with automatic logging and archiving.

        Args:
            src: Source file path
            filename: Destination filename only (copied to stage working dir) - mutually exclusive with dest_full_path
            dest_full_path: Complete destination path (from ctx) - mutually exclusive with filename
            archive: Whether to also copy to archive directory (default: True)

        Returns:
            Path to the copied file

        Raises:
            ValueError: If both or neither filename/dest_full_path are provided
            Exception: If file copy fails (also logs error)

        Example:
            >>> # Mode 1: Simple filename (copies to stage working dir)
            >>> logger.copy_file(src=Path("/tmp/data.csv"), filename="data.csv")
            PosixPath('/workspace/stage1_seed/data.csv')

            >>> # Mode 2: Full destination path from ctx
            >>> logger.copy_file(src=Path("/tmp/data.csv"), dest_full_path=ctx.get_agent_output_path("data.csv"))
            PosixPath('/workspace/result/data.csv')
        """
        if dest_full_path is None and filename is None:
            # Default: use source filename in working dir
            filename = src.name
        if dest_full_path is not None and filename is not None:
            raise ValueError("Cannot specify both filename and dest_full_path")

        if dest_full_path:
            work_path = Path(dest_full_path)
        else:
            work_dir = self.get_working_dir()
            work_path = work_dir / filename  # type: ignore

        # Ensure directory exists
        work_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Copy to working directory
            shutil.copyfile(src, work_path)
            self.info(f"Copied file: {src} -> {work_path}")

            # Archive if requested
            if archive:
                archive_dir = self.get_archive_dir()
                archive_path = archive_dir / work_path.name
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(work_path, archive_path)
                self.debug(f"Archived to: {archive_path}")

            return work_path

        except Exception as e:
            self.error(f"Failed to copy file from {src} to {work_path}: {e}")
            raise

    def archive_file(self, src: Path, global_filename: str | None = None, skip_stage_archive: bool = False) -> Path:
        """Archive a file to result/stage_id/ directory.

        Simple archiving: copy file from source to archive directory,
        keeping the same filename.

        Args:
            src: Source file path (e.g., output/milestone_seeds.jsonl)
            global_filename: If provided, also copy to global result_path with this filename.
                           Can include subdirectories (e.g., "logs/file.md")
            skip_stage_archive: If True, skip archiving to stage directory (only copy to global)

        Returns:
            Path to archived file (e.g., result/stage1_seed/milestone_seeds.jsonl)
            If skip_stage_archive=True and global_filename is provided, returns the global path instead

        Example:
            >>> # Archive to result/stage_id/ only
            >>> logger.archive_file(Path("result/milestone_seeds.jsonl"))
            PosixPath('result/stage1_seed/milestone_seeds.jsonl')

            >>> # Archive and also copy to global result directory
            >>> logger.archive_file(
            ...     Path("result/milestone.csv"),
            ...     global_filename="milestones.csv"
            ... )

            >>> # Skip stage archive, only copy to global directory
            >>> logger.archive_file(
            ...     Path("stage1_seed/logs/conversation.md"),
            ...     global_filename="logs/stage1_seed_conversation.md",
            ...     skip_stage_archive=True
            ... )
        """
        archive_dir = self.get_archive_dir()
        archive_path = archive_dir / src.name

        try:
            # Optionally skip stage archiving
            if not skip_stage_archive:
                # Ensure archive directory exists
                archive_dir.mkdir(parents=True, exist_ok=True)

                # Copy file to archive
                shutil.copyfile(src, archive_path)
                self.info(f"Archived: {src.name} -> {archive_path}")

            # Optionally copy to global result_path if global_filename is provided
            if global_filename is not None:
                result_dir = Path(self.workspace_root) / self.result_dir
                global_path = result_dir / global_filename

                # Ensure parent directory exists (for global_filename with subdirs)
                global_path.parent.mkdir(parents=True, exist_ok=True)

                shutil.copy2(src, global_path)
                self.info(f"  ✓ Copied {global_filename} to global result directory")

                # Return global path if skipping stage archive
                if skip_stage_archive:
                    return global_path

            return archive_path

        except Exception as e:
            self.error(f"Failed to archive {src} to {archive_path}: {e}")
            raise


def setup_pipeline_logger(
    log_dir: str,
    workspace_root: str,
    level: int = logging.INFO,
    is_multi_stage: bool = False,
    current_stage_id: Optional[str] = None,
) -> PipelineLoggerAdapter:
    """Setup pipeline logger with file output capabilities.

    Creates a PipelineLoggerAdapter that wraps the standard logger with
    file I/O operations.

    Args:
        log_dir: Directory for log files
        workspace_root: Root directory of the trial workspace
        level: Logging level (default: logging.INFO)
        is_multi_stage: Whether in multi-stage mode (default: False)
        current_stage_id: Current stage ID (required if is_multi_stage=True)

    Returns:
        Configured PipelineLoggerAdapter instance

    Example:
        >>> logger = setup_pipeline_logger(
        ...     log_dir="/workspace/logs",
        ...     workspace_root="/workspace",
        ...     is_multi_stage=True,
        ...     current_stage_id="stage1_seed",
        ... )
        >>> logger.info("Starting...")
        >>> logger.save_json("config.json", {"key": "value"})
    """
    # Create log directory
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_path = str(Path(log_dir) / "pipeline.log")

    # Get or create base logger
    base_logger = logging.getLogger("deepcommit.pipeline")
    base_logger.setLevel(level)

    # Avoid duplicate handlers if re-initialized
    if not base_logger.handlers:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(level)
        ch = logging.StreamHandler()
        ch.setLevel(level)

        # File handler: no colors (plain text)
        fmt_file = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        fh.setFormatter(fmt_file)

        # Console handler: with colors
        fmt_console = ColoredFormatter("%(asctime)s [%(levelname)s] %(message)s")
        ch.setFormatter(fmt_console)

        base_logger.addHandler(fh)
        base_logger.addHandler(ch)

    # Wrap in adapter
    adapter = PipelineLoggerAdapter(
        logger=base_logger,
        workspace_root=Path(workspace_root),
        is_multi_stage=is_multi_stage,
        current_stage_id=current_stage_id,
    )

    return adapter
