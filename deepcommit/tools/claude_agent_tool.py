from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from pipeline.utils.pipeline_logger import PipelineLoggerAdapter


@dataclass
class ClaudeRunResult:
    status: str
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    log_path: Optional[str] = None
    notes: Optional[str] = None


def _write_log(log_dir: Path, content: str, prefix: str = "claude") -> str:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = log_dir / f"{prefix}_{ts}.log"
    path.write_text(content, encoding="utf-8")
    return str(path)


def _find_session_number_by_id(
    session_id: str, logger: Optional[logging.Logger | PipelineLoggerAdapter] = None, max_retries: int = 3
) -> Optional[int]:
    """Find the session number by session ID from claude-extract --list.

    Args:
        session_id: The UUID session ID to search for (can be full or partial)
        logger: Optional logger for warnings/errors
        max_retries: Number of retries if session not found (handles race conditions)

    Returns:
        The session number (1-indexed) if found, None otherwise
    """
    for attempt in range(max_retries):
        try:
            # List all sessions
            result = subprocess.run(
                ["claude-extract", "--list"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                if logger:
                    logger.warning(f"Failed to list sessions (exit code {result.returncode})")
                return None

            # Parse the output to find the session ID
            # Format: "   📄 Session: 5db33370..."
            # We need to match this and extract the number before it
            lines = result.stdout.split("\n")
            session_number = None

            for i, line in enumerate(lines):
                # Look for the session ID line
                if "📄 Session:" in line or "Session:" in line:
                    # Extract the session ID from this line
                    match = re.search(r"Session:\s*([a-f0-9-]+)", line)
                    if match:
                        found_id = match.group(1)
                        # Check if it matches our target session ID (support partial match)
                        if found_id.startswith(session_id[:8]) or session_id.startswith(found_id[:8]):
                            # Look backwards to find the session number
                            for j in range(i - 1, max(0, i - 10), -1):
                                # Look for lines like "1. 📁" or just "1."
                                num_match = re.match(r"^(\d+)\.\s", lines[j])
                                if num_match:
                                    session_number = int(num_match.group(1))
                                    break
                            if session_number:
                                break

            if session_number:
                if logger:
                    logger.debug(f"Found session ID {session_id[:8]}... at position {session_number}")
                return session_number

            # Session not found, retry after a short delay
            if attempt < max_retries - 1:
                if logger:
                    logger.debug(f"Session {session_id[:8]}... not found, retrying ({attempt + 1}/{max_retries})...")
                time.sleep(0.5)

        except FileNotFoundError:
            if logger:
                logger.warning("claude-extract command not found")
            return None
        except Exception as e:
            if logger:
                logger.warning(f"Failed to find session number: {e}")
            return None

    if logger:
        logger.warning(f"Session {session_id[:8]}... not found after {max_retries} attempts")
    return None


def _extract_trace(log_dir: Path, session_id: str, logger: Optional[logging.Logger | PipelineLoggerAdapter] = None):
    """Extract Claude conversation to markdown file.

    Args:
        log_dir: Directory to save the extracted conversation
        session_id: Session ID (UUID) - REQUIRED for precise tracking
        logger: Optional logger for warnings/errors
    """
    # Best-effort: persist Claude session extract (markdown) alongside logs
    try:
        extract_args = [
            "claude-extract",
            "--detailed",
            "--format",
            "markdown",
            "--output",
            str(log_dir),
        ]

        # Try to find and extract the specific session
        session_number = _find_session_number_by_id(session_id, logger)
        if session_number:
            extract_args.extend(["--extract", str(session_number)])
            if logger:
                logger.debug(f"Extracting session {session_number} with ID {session_id[:8]}...")
        else:
            # Fallback to recent if we can't find the session
            if logger:
                logger.warning(f"Could not find session ID {session_id[:8]}..., falling back to --recent 1")
            extract_args.extend(["--recent", "1"])

        result = subprocess.run(
            extract_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 and logger:
            logger.warning(f"Failed to extract conversation (exit code {result.returncode}): {result.stderr[:200]}")
    except FileNotFoundError as e:
        if logger:
            logger.warning(f"claude-extract command not found: {e}")
    except subprocess.TimeoutExpired:
        if logger:
            logger.warning("claude-extract command timed out")
    except Exception as e:
        if logger:
            logger.warning(f"Failed to extract conversation: {e}")


def _run_stdin(cmd: List[str], prompt_text: str, cwd: Path, timeout_ms: int, env: Dict[str, str]) -> ClaudeRunResult:
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            text=True,
            env=env,
        )
        try:
            stdout, stderr = proc.communicate(input=prompt_text, timeout=timeout_ms / 1000.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            dt = int((time.time() - t0) * 1000)
            return ClaudeRunResult(
                status="timeout", stdout=stdout or "", stderr=stderr or "", exit_code=124, duration_ms=dt
            )
        dt = int((time.time() - t0) * 1000)
        status = "ok" if proc.returncode == 0 else "error"
        return ClaudeRunResult(
            status=status, stdout=stdout or "", stderr=stderr or "", exit_code=proc.returncode or 0, duration_ms=dt
        )
    except FileNotFoundError as e:
        dt = int((time.time() - t0) * 1000)
        return ClaudeRunResult(
            status="error", stdout="", stderr=f"Command not found: {e}", exit_code=127, duration_ms=dt
        )
    except (OSError, PermissionError) as e:
        dt = int((time.time() - t0) * 1000)
        return ClaudeRunResult(status="error", stdout="", stderr=f"System error: {e}", exit_code=1, duration_ms=dt)
    except MemoryError as e:
        dt = int((time.time() - t0) * 1000)
        return ClaudeRunResult(status="error", stdout="", stderr=f"Out of memory: {e}", exit_code=137, duration_ms=dt)
    except Exception as e:
        dt = int((time.time() - t0) * 1000)
        return ClaudeRunResult(status="error", stdout="", stderr=f"Unexpected error: {e}", exit_code=1, duration_ms=dt)


def run_claude_cli_agent(
    prompt_text: str,
    workspace_root: str,
    *,
    session_id: str,
    resume: bool = False,
    model: Optional[str] = None,
    mcp_config: Optional[str] = None,
    timeout_ms: int = 600_000,
    extra_args: Optional[List[str]] = None,
    env_overrides: Optional[Dict[str, str]] = None,
    log_dir: Optional[str] = None,
    add_default_cli_flags: bool = True,
    think_level: Optional[str] = None,  # None = don't pass -p flag
    output_format: str = "json",
    dangerously_skip_permissions: bool = True,
    logger: Optional[logging.Logger | PipelineLoggerAdapter] = None,
) -> ClaudeRunResult:
    """Run the local Claude CLI Agent non-interactively and return outputs.

    Args:
        session_id: Session ID (UUID) - REQUIRED for precise tracking
        resume: If True, resume existing session; if False, create new session with session_id

    This function attempts a stdin-based execution first (or as specified), with a TTY fallback.
    """
    workspace = Path(workspace_root)
    if log_dir is None:
        log_dir = str(workspace / "result" / "logs")
    logs_path = Path(log_dir)

    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    if shutil.which(claude_bin) is None:
        notes = f"claude CLI not found in PATH (CLAUDE_BIN={claude_bin})"
        log_path = _write_log(logs_path, notes, prefix="claude_error")
        return ClaudeRunResult(
            status="error", stdout="", stderr=notes, exit_code=127, duration_ms=0, log_path=log_path, notes=notes
        )

    # Build base command according to provided guidance
    cmd: List[str] = [claude_bin]
    if add_default_cli_flags:
        if think_level:
            cmd.append("-p")
            cmd.append(think_level)
        if model:
            cmd.extend(["--model", model])
        if output_format:
            cmd.extend(["--output-format", output_format])
        if dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if mcp_config:
            cmd.extend(["--mcp-config", mcp_config])

        # Use --resume or --session-id based on resume flag (mutually exclusive)
        if resume:
            # Resume existing session
            cmd.extend(["--resume", session_id])
        else:
            # Create new session with specific ID
            cmd.extend(["--session-id", session_id])
    if extra_args:
        cmd.extend(extra_args)

    env = os.environ.copy()
    if env_overrides:
        env.update({k: str(v) for k, v in env_overrides.items()})

    # Log the session ID for debugging
    if logger:
        logger.debug(f"Running Claude CLI with session ID: {session_id[:8]}...")

    # Only stdin mode supported (non-interactive)
    res = _run_stdin(cmd, prompt_text, workspace, timeout_ms, env)
    content = f"$ {' '.join(cmd)}\nSESSION_ID={session_id}\nEXIT={res.exit_code} DUR={res.duration_ms}ms\nSTDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}\n"
    log_path = _write_log(logs_path, content)
    res.log_path = log_path

    # Wait briefly for session to be indexed before extraction
    # This prevents race condition where session isn't visible immediately after CLI exits
    time.sleep(1.0)

    # extract entire claude session trace using the specific session ID
    _extract_trace(logs_path, session_id=session_id, logger=logger)
    return res
