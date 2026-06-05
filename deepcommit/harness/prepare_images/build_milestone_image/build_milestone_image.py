#!/usr/bin/env python3
"""
Build milestone Docker images using a CLI coding agent.

Orchestrates the full build pipeline for milestone images:
  1. Generate environment setup prompt from template
  2. Run a coding agent to create Dockerfile + test_config.json
  3. Validate outputs and test results
  4. Retry on failure with error context

If a Dockerfile already exists for a milestone, the agent automatically
enters refine mode: it reads the existing Dockerfile and build_status.json
hard-gate evidence to fix issues instead of building from scratch.

If Dockerfile + test_config.json already exist, the CLI compares their content
hashes with build_status.json. A matching built=true status can be skipped; a
matching built=false status enters refine mode; a missing/stale status triggers
a fresh pipeline rebuild + validation before any agent work.

Usage:
    # Build all milestones
    python -m deepcommit.harness.prepare_images.build_milestone_image.build_milestone_image \\
        --all --work-dir DATA/harness_workspace/repo_range/scenario

    # Regenerate Dockerfile from scratch (backs up existing Dockerfile)
    python -m deepcommit.harness.prepare_images.build_milestone_image.build_milestone_image \\
        --all --regen-dockerfile --work-dir DATA/harness_workspace/repo_range/scenario

    # Dry run (generate prompts only)
    python -m deepcommit.harness.prepare_images.build_milestone_image.build_milestone_image \\
        --all --dry-run --work-dir DATA/harness_workspace/repo_range/scenario
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..collect_milestone_patched_tests import ensure_patched_tests
from ..compute_base_baseline import ensure_base_baseline
from .attempt_archive import (
    MilestoneAttemptArchive,
    next_aligned_attempt_number,
    validation_attempt_is_complete,
)
from .attempt_archive import (
    agent_input_hashes as archive_agent_input_hashes,
)
from .attempt_archive import (
    attempt_number as archive_attempt_number,
)
from .diagnosis import (
    DEFAULT_VALIDATION_TIMEOUT_SECONDS,
    build_status_image_matches_current,
    build_status_matches_current_inputs,
    check_build_outputs,
    collect_patched_tests,
    current_build_inputs,
    evaluate_build_status,
    load_module2_waivers,
    load_validation_thresholds,
    resolve_validation_retries,
    resolve_validation_timeout_seconds,
    run_tests,
    summarize_build_status_failure,
    write_build_status,
)
from .dockerfile_lint import format_violations, lint_dockerfile
from .render_prompt import build_refine_prompt, build_retry_prompt, render_env_setup_prompt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# =============================================================================
# Data classes
# =============================================================================


@dataclass
class AgentResult:
    """Result from a single CLI agent execution."""

    success: bool
    output: str
    session_id: str
    duration_ms: int = 0
    exit_code: int = 0
    error: Optional[str] = None
    provider_session_id: Optional[str] = None
    resume_attempts: int = 0
    quota_exhausted: bool = False  # True if 429 / session-limit hit (no real LLM call billed)


@dataclass
class BuildResult:
    """Result from building a single milestone image."""

    milestone_id: str
    status: str  # success | skipped | validated | module2_sufficient | agent_failed | validation_failed | dry_run
    skipped_build: bool = False
    attempt: int = 0
    duration_seconds: float = 0
    prompt_file: Optional[Path] = None
    error: Optional[str] = None
    test_summary: Optional[Dict[str, Any]] = None
    agent_sessions: List[str] = field(default_factory=list)


VALIDATION_ONLY_AGENT = "validation_only"


# =============================================================================
# Path inference & Docker utilities
# =============================================================================


def _latest_attempt_dir(work_dir: Path, milestone_id: str) -> Optional[Path]:
    """Return the most recent test_results/{mid}/refinement/attempt_N/, or None.

    Used to point retry prompts at the previous attempt's artifacts so the
    agent can read start_summary.json / end_summary.json itself. As a side
    effect, refresh per-attempt summaries if they were written in an older
    format that lacks `collection_errors` / `error_groups` —
    this guarantees what the retry preamble points at is always current.
    """
    results_dir = work_dir / "test_results" / milestone_id
    attempt_dirs = sorted(
        (path for path in (results_dir / "refinement").glob("attempt_*") if validation_attempt_is_complete(path)),
        key=_attempt_number,
    )
    if not attempt_dirs:
        return None
    latest = attempt_dirs[-1]
    # Lazy import to avoid circular deps at module load
    from deepcommit.harness.utils.pytest_report_utils import ensure_pytest_summary_current

    for side in ("start", "end"):
        ensure_pytest_summary_current(
            latest / f"{side}_summary.json",
            latest / f"{side}.json",
        )
    return latest


def _attempt_number(path: Path) -> int:
    return archive_attempt_number(path)


def _next_validation_attempt_number(work_dir: Path, milestone_id: str) -> int:
    return next_aligned_attempt_number(work_dir, milestone_id)


def _find_resumable_session(
    work_dir: Path,
    milestone_id: str,
    exclude_attempt: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Scan the latest archived attempt for a stuck/transient-error signature
    that warrants resuming the same provider_session_id.

    Heuristic: latest attempt's meta.json marks agent_success=False AND its
    wrapper.log contains ``api_error_status":429`` (Claude session-window limit)
    OR an explicit ``terminal_reason`` indicating quota/network exhaustion.

    ``exclude_attempt`` skips an attempt number that's already reserved/empty
    (e.g. the one we just created for the about-to-run agent invocation).

    Returns ``{"provider_session_id": ..., "attempt": N, "reason": ...}`` if a
    resumable session was found, else ``None``.
    """
    build_runs_dir = work_dir / "_build_runs" / milestone_id
    if not build_runs_dir.is_dir():
        return None

    def _attempt_num(p: Path) -> int:
        return int(p.name.split("_", 1)[1])

    attempts = sorted(
        [
            p
            for p in build_runs_dir.iterdir()
            if p.is_dir()
            and p.name.startswith("attempt_")
            and p.name.split("_", 1)[1].isdigit()
            and (exclude_attempt is None or _attempt_num(p) != exclude_attempt)
        ],
        key=_attempt_num,
    )
    if not attempts:
        return None
    last = attempts[-1]
    meta_p = last / "meta.json"
    if not meta_p.exists():
        return None
    try:
        meta = json.loads(meta_p.read_text())
    except Exception:
        return None
    # Only resume from a failed attempt; successful attempts don't need it.
    if meta.get("agent_success") is True:
        return None
    psid = meta.get("provider_session_id")
    if not psid:
        return None
    wrapper_log = last / "agent" / "wrapper.log"
    if not wrapper_log.exists():
        return None
    try:
        log_text = wrapper_log.read_text(errors="replace")
    except Exception:
        return None
    # Recognized transient-error signatures: session quota (429), upstream rate
    # limit, or network-class CLI exit.
    if 'api_error_status":429' in log_text or "session limit" in log_text.lower():
        reason = "stuck_on_429_session_limit"
    elif 'api_error_status":503' in log_text or "service_unavailable" in log_text.lower():
        reason = "stuck_on_503_service_unavailable"
    else:
        return None
    # Verify the underlying Claude CLI session file actually exists on disk —
    # without it, `claude --resume` cannot rehydrate conversation history.
    # `~/.claude/projects/<project-slug>/<psid>.jsonl` is the canonical store.
    claude_proj_dir = Path.home() / ".claude" / "projects"
    session_present = False
    if claude_proj_dir.is_dir():
        for proj in claude_proj_dir.iterdir():
            if (proj / f"{psid}.jsonl").exists():
                session_present = True
                break
    if not session_present:
        # Detection succeeded (we know it was stuck), but the rehydration source
        # is missing. Tell caller via reason so they can log + fall back.
        reason = f"{reason}_but_session_file_missing"
    return {
        "provider_session_id": psid,
        "attempt": meta.get("attempt", int(last.name.split("_", 1)[1])),
        "reason": reason,
        "source_attempt_dir": str(last),
        "session_file_present": session_present,
    }


def infer_paths_from_work_dir(work_dir: Path) -> Dict[str, Any]:
    """Infer all standard paths from work_dir.

    Expected format: .../harness_workspace/{repo_id}/{test_scenario}
    """
    parts = work_dir.resolve().parts

    try:
        idx = parts.index("harness_workspace")
        repo_id = parts[idx + 1]
        test_scenario = parts[idx + 2]
    except (ValueError, IndexError):
        raise ValueError(  # noqa: B904
            f"Cannot infer repo_id from work_dir path: {work_dir}\n"
            f"Expected format: .../harness_workspace/{{repo_id}}/{{test_scenario}}"
        )

    return {
        "repo_id": repo_id,
        "test_scenario": test_scenario,
        "metadata_path": work_dir / "metadata.json",
        "base_image": f"{repo_id}/base:latest".lower(),
        "dockerfiles_dir": work_dir / "dockerfiles",
        "test_results_dir": work_dir / "test_results",
        "testbed_dir": work_dir / "testbed",
        "build_runs_dir": work_dir / "_build_runs",
    }


def get_milestone_image_name(repo_id: str, test_scenario: str, milestone_id: str) -> str:
    """Get Docker image name for a milestone."""
    return f"{repo_id}/{test_scenario}/{milestone_id}:latest".lower()


def check_image_exists(image_name: str) -> bool:
    """Check if a Docker image exists locally."""
    try:
        result = subprocess.run(
            ["docker", "images", "-q", image_name],
            capture_output=True,
            text=True,
            check=True,
        )
        return bool(result.stdout.strip())
    except subprocess.CalledProcessError:
        return False


def dockerfile_newer_than_image(dockerfile_path: Path, image_name: str) -> bool:
    """True if Dockerfile mtime > image CreatedAt (i.e., image is stale).

    Returns True conservatively if either timestamp can't be determined,
    so the caller errs on the side of rebuilding.
    """
    if not dockerfile_path.exists():
        return False

    try:
        dockerfile_mtime = dockerfile_path.stat().st_mtime
    except OSError:
        return True

    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.Created}}", image_name],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return True

    created_str = result.stdout.strip()
    if not created_str:
        return True

    # docker inspect returns RFC3339, e.g. "2026-04-16T02:47:09.123456789Z".
    # Python 3.11+ fromisoformat handles 'Z'; older versions need replacement.
    from datetime import datetime

    try:
        iso = created_str.replace("Z", "+00:00")
        # Truncate fractional seconds to microseconds (fromisoformat limit on 3.10)
        if "." in iso:
            date_part, tz_part = iso.rsplit("+", 1) if "+" in iso else (iso, "")
            if "." in date_part:
                base, frac = date_part.split(".")
                frac = frac[:6]  # microseconds max
                date_part = f"{base}.{frac}"
            iso = f"{date_part}+{tz_part}" if tz_part else date_part
        image_created = datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return True

    return dockerfile_mtime > image_created


def backup_dockerfile(dockerfile_path: Path) -> Optional[Path]:
    """Rename Dockerfile to Dockerfile.bak (numeric suffix on collision).

    Returns the backup path, or None if the source didn't exist.
    """
    if not dockerfile_path.exists():
        return None

    parent = dockerfile_path.parent
    candidate = parent / "Dockerfile.bak"
    i = 1
    while candidate.exists():
        candidate = parent / f"Dockerfile.bak.{i}"
        i += 1

    dockerfile_path.rename(candidate)
    return candidate


def rebuild_docker_image(dockerfile_path: Path, image_name: str, context_dir: Path) -> bool:
    """Run `docker build` for an existing Dockerfile (no agent involvement)."""
    logger.info(f"Rebuilding image {image_name} from {dockerfile_path}...")
    cmd = [
        "docker",
        "build",
        "-f",
        str(dockerfile_path),
        "-t",
        image_name,
        str(context_dir),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"docker build failed: {result.stderr[-500:] if result.stderr else result.stdout[-500:]}")
        return False
    logger.info(f"Successfully rebuilt image {image_name}")
    return True


# =============================================================================
# Agent runners
# =============================================================================


DEFAULT_AGENT = "codex"
DEFAULT_CODEX_MODEL = "gpt-5.5"
DEFAULT_CODEX_REASONING_EFFORT = "xhigh"
DEFAULT_CLAUDE_MODEL = "claude-opus-4-7"
DEFAULT_AGENT_TIMEOUT_SECONDS = 7200
DEFAULT_AGENT_RESUME_ATTEMPTS = 3


CODEX_SESSION_ID_RE = re.compile(
    r"\bsession id:\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b"
)


def default_model_for_agent(agent: str) -> str:
    if agent == "claude":
        return DEFAULT_CLAUDE_MODEL
    return DEFAULT_CODEX_MODEL


def get_module2_acceptance_mode(work_dir: Path, repo_id: str, milestone_id: Optional[str] = None) -> str:
    config = load_validation_thresholds(work_dir, repo_id)
    mode = str(config.get("module2_acceptance_mode", "hard_gate")).strip()
    if mode not in {"hard_gate", "agent_sufficient"}:
        label = f"[{milestone_id}] " if milestone_id else ""
        logger.warning(f"{label}Unknown module2_acceptance_mode={mode!r}; using hard_gate")
        return "hard_gate"
    return mode


def agent_sufficient_requested(docker_dir: Path, milestone_id: str) -> bool:
    _bound, _unbound, agent_sufficient, errors = load_module2_waivers(docker_dir, milestone_id)
    if errors:
        logger.warning(f"[{milestone_id}] Ignoring agent_sufficient due to module2_waivers errors: {errors}")
        return False
    return bool(agent_sufficient.get("value"))


def pristine_state_sha(repo_path: Path, ref: str) -> Optional[str]:
    """Resolve ``ref`` to its commit SHA in the testbed (un-patched by Module 2).

    Returns None on any git failure; callers should treat None as "skip the
    numstat check" rather than failing the build.
    """
    if not ref or not repo_path.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


class ClaudeAgentRunner:
    """Run Claude CLI agent for milestone image building."""

    def __init__(
        self,
        workspace: Path,
        log_dir: Optional[Path] = None,
        timeout_ms: int = DEFAULT_AGENT_TIMEOUT_SECONDS * 1000,
        model: str = DEFAULT_CLAUDE_MODEL,
        max_resume_attempts: int = DEFAULT_AGENT_RESUME_ATTEMPTS,
    ):
        self.workspace = Path(workspace)
        self.log_dir = Path(log_dir) if log_dir else None
        self.timeout_ms = timeout_ms
        self.model = model
        self.max_resume_attempts = max(0, max_resume_attempts)

        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        prompt: str,
        task_name: str = "build_milestone",
        log_path: Optional[Path] = None,
        resume_provider_session_id: Optional[str] = None,
    ) -> AgentResult:
        """Run Claude CLI with the given prompt.

        If ``resume_provider_session_id`` is provided, the first invocation uses
        ``claude --resume <id>`` (instead of a fresh ``--session-id``) so that
        conversation history from a prior stuck/limited session is preserved.
        Subsequent in-attempt resume retries (the normal failure-recovery loop)
        continue to use the same provider_session_id.
        """
        start_time = time.time()
        session_id = str(uuid.uuid4())[:8]
        resume_from_prior = resume_provider_session_id is not None
        provider_session_id = resume_provider_session_id or str(uuid.uuid4())
        resume_attempts_used = 0
        stdout_parts: List[str] = []
        stderr_parts: List[str] = []
        if resume_from_prior:
            logger.info(
                f"[{task_name}] Resuming Claude session: {session_id} "
                f"from prior provider_session_id={provider_session_id}"
            )
        else:
            logger.info(
                f"[{task_name}] Starting Claude session: {session_id} (provider_session_id={provider_session_id})..."
            )
        resume_prompt = (
            "The previous Claude process for this milestone exited before handing control "
            "back to the pipeline. The session is preserved — your full original prompt is "
            "still in context. Resume from the current filesystem state; do not restart from "
            "scratch, and inspect existing artifacts before launching duplicate expensive checks.\n\n"
            "Resume could be triggered from any phase (Phase 0-5). To re-orient: "
            "(1) `ls` the dockerfiles/<milestone_id>/ directory and read build_status.json if "
            "present to see which phase you were in; "
            "(2) re-read your original prompt's CRITICAL WORKFLOW section (Phase 0-5) to locate "
            "where you left off; "
            "(3) continue ALL remaining work — including Phase 4.5 four exits (bound_waivers / "
            "unbound_issues / commit_signals / agent_sufficient) if any hard gate would "
            "otherwise fail. Stop only when the milestone is genuinely ready for pipeline validation."
        )

        try:
            for invocation_index in range(self.max_resume_attempts + 1):
                # First invocation uses --resume when resuming a prior session
                # OR --session-id for a fresh start; later invocations always
                # use --resume to recover from in-attempt failures.
                is_resume = invocation_index > 0 or resume_from_prior
                if is_resume and invocation_index > 0:
                    resume_attempts_used += 1
                    current_prompt = resume_prompt
                    cmd = self._resume_cmd(provider_session_id)
                    logger.warning(
                        f"[{task_name}] Resuming Claude provider session {provider_session_id} "
                        f"({resume_attempts_used}/{self.max_resume_attempts})..."
                    )
                elif resume_from_prior:
                    # First invocation but we're resuming from a prior stuck session
                    current_prompt = resume_prompt
                    cmd = self._resume_cmd(provider_session_id)
                else:
                    current_prompt = prompt
                    cmd = self._start_cmd(provider_session_id)

                exit_code, stdout, stderr, timed_out = self._run_claude_process(cmd, current_prompt)
                stdout_parts.append(
                    f"\n==== CLAUDE INVOCATION {invocation_index + 1} "
                    f"({'resume' if is_resume else 'start'}) STDOUT ====\n{stdout or ''}"
                )
                stderr_parts.append(
                    f"\n==== CLAUDE INVOCATION {invocation_index + 1} "
                    f"({'resume' if is_resume else 'start'}) STDERR ====\n{stderr or ''}"
                )
                duration_ms = int((time.time() - start_time) * 1000)
                output_text = self._parse_output(stdout)

                # Detect 429/session-limit and bail out fast (don't waste 3 resume retries
                # all returning the same 429). Claude CLI exits 0 but wraps the error in
                # the result JSON: {"is_error":true,"api_error_status":429,...}.
                quota_429 = 'api_error_status":429' in (stdout or "") or "session limit" in (stdout or "").lower()
                if quota_429:
                    logger.warning(
                        f"[{task_name}] 429 / session-limit detected on invocation {invocation_index + 1}; "
                        f"skipping remaining resume retries."
                    )
                    self._save_logs(
                        task_name,
                        session_id,
                        1,
                        duration_ms,
                        "".join(stdout_parts),
                        "".join(stderr_parts),
                        prompt,
                        "quota_429",
                        provider_session_id,
                        resume_attempts_used,
                        log_path=log_path,
                    )
                    return AgentResult(
                        success=False,
                        output=output_text,
                        session_id=session_id,
                        duration_ms=duration_ms,
                        exit_code=0,
                        error="quota_exhausted_429",
                        provider_session_id=provider_session_id,
                        resume_attempts=resume_attempts_used,
                        quota_exhausted=True,
                    )

                if exit_code == 0:
                    self._save_logs(
                        task_name,
                        session_id,
                        0,
                        duration_ms,
                        "".join(stdout_parts),
                        "".join(stderr_parts),
                        prompt,
                        "ok",
                        provider_session_id,
                        resume_attempts_used,
                        log_path=log_path,
                    )
                    return AgentResult(
                        success=True,
                        output=output_text,
                        session_id=session_id,
                        duration_ms=duration_ms,
                        exit_code=0,
                        provider_session_id=provider_session_id,
                        resume_attempts=resume_attempts_used,
                    )

                if timed_out:
                    logger.error(f"[{task_name}] Claude session {session_id} timed out after {self.timeout_ms}ms")
                    error = f"Timeout after {self.timeout_ms}ms"
                else:
                    logger.error(
                        f"[{task_name}] Claude session {session_id} "
                        f"(provider_session_id={provider_session_id}) exited with code {exit_code}"
                    )
                    error = f"Exit code {exit_code}: {self._error_detail(stdout, stderr)}"

                if resume_attempts_used >= self.max_resume_attempts:
                    self._save_logs(
                        task_name,
                        session_id,
                        exit_code,
                        duration_ms,
                        "".join(stdout_parts),
                        "".join(stderr_parts),
                        prompt,
                        "error",
                        provider_session_id,
                        resume_attempts_used,
                        log_path=log_path,
                    )
                    return AgentResult(
                        success=False,
                        output=output_text,
                        session_id=session_id,
                        duration_ms=duration_ms,
                        exit_code=exit_code,
                        error=error,
                        provider_session_id=provider_session_id,
                        resume_attempts=resume_attempts_used,
                    )

            duration_ms = int((time.time() - start_time) * 1000)
            self._save_logs(
                task_name,
                session_id,
                1,
                duration_ms,
                "".join(stdout_parts),
                "".join(stderr_parts),
                prompt,
                "error",
                provider_session_id,
                resume_attempts_used,
                log_path=log_path,
            )
            return AgentResult(
                success=False,
                output="",
                session_id=session_id,
                duration_ms=duration_ms,
                exit_code=1,
                error="Claude failed and could not be resumed",
                provider_session_id=provider_session_id,
                resume_attempts=resume_attempts_used,
            )

        except FileNotFoundError:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error("Claude CLI not found. Is it installed and in PATH?")
            return AgentResult(
                success=False,
                output="",
                session_id=session_id,
                duration_ms=duration_ms,
                exit_code=127,
                error="Claude CLI not found",
                provider_session_id=provider_session_id,
                resume_attempts=resume_attempts_used,
            )
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"Failed to execute Claude: {e}")
            return AgentResult(
                success=False,
                output="",
                session_id=session_id,
                duration_ms=duration_ms,
                exit_code=1,
                error=str(e),
                provider_session_id=provider_session_id,
                resume_attempts=resume_attempts_used,
            )

    def _start_cmd(self, provider_session_id: str) -> List[str]:
        return [
            "claude",
            "-p",
            "--session-id",
            provider_session_id,
            "--model",
            self.model,
            "--effort",
            "max",
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
        ]

    def _resume_cmd(self, provider_session_id: str) -> List[str]:
        return [
            "claude",
            "-p",
            "--resume",
            provider_session_id,
            "--model",
            self.model,
            "--effort",
            "max",
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
        ]

    def _run_claude_process(self, cmd: List[str], prompt: str) -> tuple[int, str, str, bool]:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.workspace),
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(input=prompt, timeout=self.timeout_ms / 1000.0)
            return proc.returncode, stdout or "", stderr or "", False
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return -1, stdout or "", stderr or "", True

    @staticmethod
    def _parse_output(stdout: str) -> str:
        try:
            result = json.loads(stdout)
            return result.get("result", "")
        except json.JSONDecodeError:
            return stdout

    @staticmethod
    def _error_detail(stdout: str, stderr: str) -> str:
        if stdout:
            try:
                parsed = json.loads(stdout)
                if parsed.get("is_error") and parsed.get("result"):
                    return str(parsed["result"])[:500]
            except json.JSONDecodeError:
                pass
        return stderr[:500] if stderr else "unknown error"

    def _save_logs(
        self,
        task_name,
        session_id,
        exit_code,
        duration_ms,
        stdout,
        stderr,
        prompt,
        status,
        provider_session_id=None,
        resume_attempts=0,
        *,
        log_path: Optional[Path] = None,
    ):
        """Save structured agent log file."""
        if log_path is not None:
            log_file = log_path
        elif self.log_dir:
            log_file = self.log_dir / f"{task_name}_{session_id}.log"
        else:
            return

        log_content = (
            f"AGENT=claude\nMODEL={self.model}\n"
            f"SESSION_ID={session_id}\nPROVIDER_SESSION_ID={provider_session_id or ''}\n"
            f"RESUME_ATTEMPTS={resume_attempts}\n"
            f"STATUS={status}\nEXIT={exit_code}\nDURATION={duration_ms}ms\n\n"
            f"==== PROMPT ====\n{prompt[:5000]}{'...(truncated)' if len(prompt) > 5000 else ''}\n\n"
            f"==== STDOUT ====\n{stdout[:10000]}{'...(truncated)' if len(stdout) > 10000 else ''}\n\n"
            f"==== STDERR ====\n{stderr}\n"
        )
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(log_content, encoding="utf-8")
        logger.debug(f"Saved agent log to: {log_file}")


class CodexAgentRunner:
    """Run Codex CLI agent for milestone image building."""

    def __init__(
        self,
        workspace: Path,
        log_dir: Optional[Path] = None,
        timeout_ms: int = DEFAULT_AGENT_TIMEOUT_SECONDS * 1000,
        model: str = DEFAULT_CODEX_MODEL,
        reasoning_effort: str = DEFAULT_CODEX_REASONING_EFFORT,
        max_resume_attempts: int = DEFAULT_AGENT_RESUME_ATTEMPTS,
    ):
        self.workspace = Path(workspace)
        self.log_dir = Path(log_dir) if log_dir else None
        self.timeout_ms = timeout_ms
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_resume_attempts = max(0, max_resume_attempts)

        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        prompt: str,
        task_name: str = "build_milestone",
        log_path: Optional[Path] = None,
    ) -> AgentResult:
        """Run Codex CLI non-interactively with the given prompt."""
        start_time = time.time()
        session_id = str(uuid.uuid4())[:8]
        provider_session_id: Optional[str] = None
        resume_attempts_used = 0
        stdout_parts: List[str] = []
        stderr_parts: List[str] = []
        logger.info(
            f"[{task_name}] Starting Codex session: {session_id} "
            f"(model={self.model}, effort={self.reasoning_effort})..."
        )

        last_message_path = (self.log_dir or Path("/tmp")) / f"{task_name}_{session_id}.last.txt"
        resume_prompt = (
            "The previous Codex process for this milestone exited before handing control "
            "back to the pipeline. The session is preserved — your full original prompt is "
            "still in context. Resume from the current filesystem state; do not restart from "
            "scratch, and inspect existing artifacts before launching duplicate expensive checks.\n\n"
            "Resume could be triggered from any phase (Phase 0-5). To re-orient: "
            "(1) `ls` the dockerfiles/<milestone_id>/ directory and read build_status.json if "
            "present to see which phase you were in; "
            "(2) re-read your original prompt's CRITICAL WORKFLOW section (Phase 0-5) to locate "
            "where you left off; "
            "(3) continue ALL remaining work — including Phase 4.5 four exits (bound_waivers / "
            "unbound_issues / commit_signals / agent_sufficient) if any hard gate would "
            "otherwise fail. Stop only when the milestone is genuinely ready for pipeline validation."
        )

        try:
            for invocation_index in range(self.max_resume_attempts + 1):
                is_resume = invocation_index > 0
                if is_resume:
                    if not provider_session_id:
                        logger.error(
                            f"[{task_name}] Cannot resume Codex session {session_id}: provider session id was not found"
                        )
                        break
                    resume_attempts_used += 1
                    current_prompt = resume_prompt
                    cmd = self._resume_cmd(provider_session_id, last_message_path)
                    logger.warning(
                        f"[{task_name}] Resuming Codex provider session {provider_session_id} "
                        f"({resume_attempts_used}/{self.max_resume_attempts})..."
                    )
                else:
                    current_prompt = prompt
                    cmd = self._start_cmd(last_message_path)

                exit_code, stdout, stderr, timed_out = self._run_codex_process(cmd, current_prompt)
                stdout_parts.append(
                    f"\n==== CODEX INVOCATION {invocation_index + 1} "
                    f"({'resume' if is_resume else 'start'}) STDOUT ====\n{stdout or ''}"
                )
                stderr_parts.append(
                    f"\n==== CODEX INVOCATION {invocation_index + 1} "
                    f"({'resume' if is_resume else 'start'}) STDERR ====\n{stderr or ''}"
                )
                provider_session_id = self._extract_provider_session_id(stdout, stderr) or provider_session_id
                output_text = self._read_last_message(last_message_path) or (stdout or "")
                duration_ms = int((time.time() - start_time) * 1000)

                if exit_code == 0:
                    self._save_logs(
                        task_name,
                        session_id,
                        0,
                        duration_ms,
                        "".join(stdout_parts),
                        "".join(stderr_parts),
                        prompt,
                        "ok",
                        output_text,
                        provider_session_id,
                        resume_attempts_used,
                        log_path=log_path,
                    )
                    return AgentResult(
                        success=True,
                        output=output_text,
                        session_id=session_id,
                        duration_ms=duration_ms,
                        exit_code=0,
                        provider_session_id=provider_session_id,
                        resume_attempts=resume_attempts_used,
                    )

                if timed_out:
                    logger.error(f"[{task_name}] Codex session {session_id} timed out after {self.timeout_ms}ms")
                    error = f"Timeout after {self.timeout_ms}ms"
                else:
                    logger.error(
                        f"[{task_name}] Codex session {session_id} "
                        f"(provider_session_id={provider_session_id}) exited with code {exit_code}"
                    )
                    detail = (stderr or stdout or output_text or "unknown error")[:500]
                    error = f"Exit code {exit_code}: {detail}"

                if resume_attempts_used >= self.max_resume_attempts:
                    self._save_logs(
                        task_name,
                        session_id,
                        exit_code,
                        duration_ms,
                        "".join(stdout_parts),
                        "".join(stderr_parts),
                        prompt,
                        "error",
                        output_text,
                        provider_session_id,
                        resume_attempts_used,
                        log_path=log_path,
                    )
                    return AgentResult(
                        success=False,
                        output=output_text,
                        session_id=session_id,
                        duration_ms=duration_ms,
                        exit_code=exit_code,
                        error=error,
                        provider_session_id=provider_session_id,
                        resume_attempts=resume_attempts_used,
                    )

            duration_ms = int((time.time() - start_time) * 1000)
            output_text = self._read_last_message(last_message_path)
            self._save_logs(
                task_name,
                session_id,
                1,
                duration_ms,
                "".join(stdout_parts),
                "".join(stderr_parts),
                prompt,
                "error",
                output_text,
                provider_session_id,
                resume_attempts_used,
                log_path=log_path,
            )
            return AgentResult(
                success=False,
                output=output_text,
                session_id=session_id,
                duration_ms=duration_ms,
                exit_code=1,
                error="Codex failed and could not be resumed",
                provider_session_id=provider_session_id,
                resume_attempts=resume_attempts_used,
            )

        except FileNotFoundError:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error("Codex CLI not found. Is it installed and in PATH?")
            return AgentResult(
                success=False,
                output="",
                session_id=session_id,
                duration_ms=duration_ms,
                exit_code=127,
                error="Codex CLI not found",
                provider_session_id=provider_session_id,
                resume_attempts=resume_attempts_used,
            )
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"Failed to execute Codex: {e}")
            return AgentResult(
                success=False,
                output="",
                session_id=session_id,
                duration_ms=duration_ms,
                exit_code=1,
                error=str(e),
                provider_session_id=provider_session_id,
                resume_attempts=resume_attempts_used,
            )
        finally:
            try:
                last_message_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _read_last_message(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _start_cmd(self, last_message_path: Path) -> List[str]:
        return [
            "codex",
            "exec",
            "--model",
            self.model,
            "-c",
            f'model_reasoning_effort="{self.reasoning_effort}"',
            "--sandbox",
            "danger-full-access",
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            str(self.workspace),
            "--skip-git-repo-check",
            "--output-last-message",
            str(last_message_path),
            "-",
        ]

    def _resume_cmd(self, provider_session_id: str, last_message_path: Path) -> List[str]:
        return [
            "codex",
            "exec",
            "resume",
            "--model",
            self.model,
            "-c",
            f'model_reasoning_effort="{self.reasoning_effort}"',
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--output-last-message",
            str(last_message_path),
            provider_session_id,
            "-",
        ]

    def _run_codex_process(self, cmd: List[str], prompt: str) -> tuple[int, str, str, bool]:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.workspace),
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(prompt, timeout=self.timeout_ms / 1000.0)
            return proc.returncode, stdout or "", stderr or "", False
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return -1, stdout or "", stderr or "", True

    @staticmethod
    def _extract_provider_session_id(stdout: str, stderr: str) -> Optional[str]:
        match = CODEX_SESSION_ID_RE.search((stdout or "") + "\n" + (stderr or ""))
        return match.group(1) if match else None

    def _save_logs(
        self,
        task_name,
        session_id,
        exit_code,
        duration_ms,
        stdout,
        stderr,
        prompt,
        status,
        output_text,
        provider_session_id=None,
        resume_attempts=0,
        *,
        log_path: Optional[Path] = None,
    ):
        """Save structured agent log file."""
        if log_path is not None:
            log_file = log_path
        elif self.log_dir:
            log_file = self.log_dir / f"{task_name}_{session_id}.log"
        else:
            return

        log_content = (
            f"AGENT=codex\nMODEL={self.model}\nREASONING_EFFORT={self.reasoning_effort}\n"
            f"SESSION_ID={session_id}\nPROVIDER_SESSION_ID={provider_session_id or ''}\n"
            f"RESUME_ATTEMPTS={resume_attempts}\n"
            f"STATUS={status}\nEXIT={exit_code}\nDURATION={duration_ms}ms\n\n"
            f"==== PROMPT ====\n{prompt[:5000]}{'...(truncated)' if len(prompt) > 5000 else ''}\n\n"
            f"==== LAST_MESSAGE ====\n{output_text[:10000]}{'...(truncated)' if len(output_text) > 10000 else ''}\n\n"
            f"==== STDOUT ====\n{stdout[:10000]}{'...(truncated)' if len(stdout) > 10000 else ''}\n\n"
            f"==== STDERR ====\n{stderr}\n"
        )
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(log_content, encoding="utf-8")
        logger.debug(f"Saved agent log to: {log_file}")


def make_agent_runner(
    agent: str,
    *,
    workspace: Path,
    log_dir: Path,
    timeout_ms: int,
    model: str,
    reasoning_effort: str,
    agent_resume_attempts: int,
):
    if agent == "claude":
        return ClaudeAgentRunner(
            workspace=workspace,
            log_dir=log_dir,
            timeout_ms=timeout_ms,
            model=model,
            max_resume_attempts=agent_resume_attempts,
        )
    if agent == "codex":
        return CodexAgentRunner(
            workspace=workspace,
            log_dir=log_dir,
            timeout_ms=timeout_ms,
            model=model,
            reasoning_effort=reasoning_effort,
            max_resume_attempts=agent_resume_attempts,
        )
    raise ValueError(f"Unsupported agent provider: {agent}")


def _validation_only_agent_history_entry(
    *,
    validation_attempt: int,
    validation_run_kind: str,
    note: str,
) -> Dict[str, Any]:
    return {
        "attempt": 0,
        "reserved_attempt": f"attempt_{validation_attempt}",
        "validation_ran": False,
        "agent_run": False,
        "validation_only": True,
        "validation_run_kind": validation_run_kind,
        "agent": VALIDATION_ONLY_AGENT,
        "model": VALIDATION_ONLY_AGENT,
        "reasoning_effort": None,
        "session_id": None,
        "provider_session_id": None,
        "resume_attempts": None,
        "success": None,
        "duration_ms": None,
        "exit_code": None,
        "error": None,
        "note": note,
    }


def get_agent_workspace_root() -> Path:
    """Return a workspace where both project code and DATA paths are reachable."""
    return Path(__file__).resolve().parents[4]


def _runner_diagnostic_timed_out(work_dir: Path, milestone_id: str) -> bool:
    diag_path = work_dir / "test_results" / milestone_id / "refinement" / "runner_diagnostic.json"
    try:
        diag = json.loads(diag_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(diag, dict) and diag.get("timed_out"))


def _run_validation_with_retries(
    *,
    milestone_id: str,
    work_dir: Path,
    repo_id: str,
    test_scenario: str,
    language: str,
    test_framework: str,
    attempt_number: int,
    force_rebuild_image: bool,
    timeout_seconds: int,
    validation_retries: int,
    initial_workers: int = 8,
) -> tuple[bool, Optional[Dict[str, Any]], Optional[Path], int]:
    """Run validation, retrying the same attempt number only after timeout.

    On each timeout-driven retry, halve the worker count so subsequent attempts
    use lower parallelism. This breaks in-container Python multiprocessing
    deadlocks (futex wait on a Queue lock held by a crashed worker) that
    re-occur deterministically at the original parallelism level.
    Sequence: 8 -> 4 -> 2 -> 1.
    """
    max_runs = max(0, validation_retries) + 1
    last_summary: Optional[Dict[str, Any]] = None
    last_attempt_dir: Optional[Path] = None
    current_workers = max(1, int(initial_workers))
    for run_index in range(1, max_runs + 1):
        # Pass workers_override only on retries (run_index > 1) so the first run
        # uses test_config.json as-authored; retries scale down.
        workers_override = current_workers if run_index > 1 else None
        success, test_summary, validation_attempt_dir = run_tests(
            milestone_id=milestone_id,
            work_dir=work_dir,
            repo_id=repo_id,
            test_scenario=test_scenario,
            language=language,
            test_framework=test_framework,
            attempt_number=attempt_number,
            force_rebuild_image=force_rebuild_image,
            timeout_seconds=timeout_seconds,
            clean_attempt_dir=True,
            workers_override=workers_override,
        )
        last_summary = test_summary
        last_attempt_dir = validation_attempt_dir
        if success:
            return True, test_summary, validation_attempt_dir, run_index
        if run_index >= max_runs or not _runner_diagnostic_timed_out(work_dir, milestone_id):
            return False, test_summary, validation_attempt_dir, run_index
        next_workers = max(1, current_workers // 2)
        logger.warning(
            f"[{milestone_id}] Validation timed out; rerunning attempt_{attempt_number} "
            f"({run_index + 1}/{max_runs}) with --workers-override={next_workers} "
            f"(down from {current_workers})"
        )
        current_workers = next_workers
    return False, last_summary, last_attempt_dir, max_runs


def validate_current_milestone(
    milestone_id: str,
    milestone_data: Dict[str, Any],
    work_dir: Path,
    paths: Dict[str, Any],
    base_image: str,
    language: str = "python",
    test_framework: str = "pytest",
    validation_timeout: Optional[int] = None,
    validation_retries: int = 0,
    rerun_attempt_number: Optional[int] = None,
    allow_rerun_input_mismatch: bool = False,
) -> BuildResult:
    """Validate current milestone artifacts without running an agent.

    This mode is for cases where the agent process failed after writing useful
    artifacts. It reserves the next aligned attempt number, rebuilds the image
    from the current Dockerfile, runs the normal validation pipeline, and writes
    the authoritative build_status.json.
    """
    start_time = time.time()
    repo_id = paths["repo_id"]
    test_scenario = paths["test_scenario"]
    validation_timeout = resolve_validation_timeout_seconds(work_dir, repo_id, validation_timeout)
    image_name = get_milestone_image_name(repo_id, test_scenario, milestone_id)
    dockerfiles_dir = paths["dockerfiles_dir"]
    docker_dir = dockerfiles_dir / milestone_id
    prompt_file = docker_dir / "env_setup_prompt.md"
    validation_attempt = rerun_attempt_number or _next_validation_attempt_number(work_dir, milestone_id)
    validation_run_kind = "validation_rerun" if rerun_attempt_number else "validation_only"

    logger.info(
        f"[{milestone_id}] Validating current artifacts only "
        f"(attempt_{validation_attempt}, image: {image_name}, timeout={validation_timeout}s)..."
    )

    with open(paths["metadata_path"]) as f:
        metadata = json.load(f)

    repo_path = paths["testbed_dir"]
    repo_src_dirs = metadata.get("repo_src_dirs", [])
    test_dir = metadata.get("test_dir", "test")
    test_dirs_meta = metadata.get("test_dirs", []) or []
    exclude_patterns_meta = metadata.get("exclude_patterns", []) or []
    test_infra_patterns_meta = metadata.get("test_infra_patterns", []) or None
    base_state = milestone_data.get("tag_name_start", f"milestone-{milestone_id}-start")
    end_state = milestone_data.get("tag_name_end", f"milestone-{milestone_id}-end")
    pristine_base_sha_val = pristine_state_sha(repo_path, base_state)
    pristine_end_sha_val = pristine_state_sha(repo_path, end_state)

    try:
        prompt_text = prompt_file.read_text(encoding="utf-8")
    except OSError:
        prompt_text = (
            "# Validate Current Artifacts Only\n\n"
            "No agent prompt was sent in this run. The pipeline validated the "
            "current Dockerfile, test_config.json, and module2_waivers.json."
        )

    if rerun_attempt_number:
        archive = MilestoneAttemptArchive.open_existing(
            work_dir=work_dir,
            milestone_id=milestone_id,
            attempt_number=validation_attempt,
        )
        if archive is None:
            return BuildResult(
                milestone_id=milestone_id,
                status="validation_failed",
                attempt=validation_attempt,
                duration_seconds=time.time() - start_time,
                error=f"Archive attempt not found: _build_runs/{milestone_id}/attempt_{validation_attempt}",
            )
        expected_hashes = archive.meta.get("after_agent_input_hashes")
        current_hashes = archive_agent_input_hashes(docker_dir)
        if isinstance(expected_hashes, dict) and expected_hashes != current_hashes:
            if not allow_rerun_input_mismatch:
                return BuildResult(
                    milestone_id=milestone_id,
                    status="validation_failed",
                    attempt=validation_attempt,
                    duration_seconds=time.time() - start_time,
                    error=(
                        f"Current Dockerfile/test_config/module2_waivers do not match "
                        f"_build_runs/{milestone_id}/attempt_{validation_attempt}/after_agent"
                    ),
                )
            logger.warning(
                f"[{milestone_id}] Debug validation rerun will overwrite attempt_{validation_attempt} "
                "with current inputs that differ from the archived after_agent snapshot"
            )
            archive.meta["validation_rerun_input_mismatch_allowed"] = True
            archive.meta["validation_rerun_expected_input_hashes"] = expected_hashes
            archive.meta["validation_rerun_current_input_hashes"] = current_hashes
        archive.meta["validation_rerun_requested_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        archive.meta["validation_rerun"] = True
        archive.write_meta()
    else:
        archive = MilestoneAttemptArchive.start(
            work_dir=work_dir,
            milestone_id=milestone_id,
            attempt_number=validation_attempt,
            repo_id=repo_id,
            test_scenario=test_scenario,
            agent=VALIDATION_ONLY_AGENT,
            model=VALIDATION_ONLY_AGENT,
            reasoning_effort=None,
            agent_retry_attempt=0,
            docker_dir=docker_dir,
            prompt_file=prompt_file,
            prompt_text=prompt_text,
        )
        archive.meta.update(
            {
                "agent_success": True,
                "validation_only": True,
            }
        )
        archive.snapshot_after_agent(docker_dir)

    agent_history = [
        _validation_only_agent_history_entry(
            validation_attempt=validation_attempt,
            validation_run_kind=validation_run_kind,
            note="User-requested validation of current artifacts without an agent run.",
        )
    ]

    def _evaluate_and_write_status(
        *,
        tests_success: Optional[bool],
        lint_violations: Optional[List[Any]] = None,
        latest_attempt_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        status = evaluate_build_status(
            milestone_id=milestone_id,
            work_dir=work_dir,
            dockerfiles_dir=dockerfiles_dir,
            repo_id=repo_id,
            test_scenario=test_scenario,
            base_image=base_image,
            base_state=base_state,
            end_state=end_state,
            image_name=image_name,
            latest_attempt_dir=latest_attempt_dir,
            agent_success=None,
            agent_required=False,
            agent_history=agent_history,
            tests_subprocess_ok=tests_success,
            lint_violations=lint_violations,
            image_exists=check_image_exists(image_name),
            repo_src_dirs=repo_src_dirs,
            test_dir=test_dir,
            test_dirs=test_dirs_meta,
            exclude_patterns=exclude_patterns_meta,
            test_infra_patterns=test_infra_patterns_meta,
            pristine_base_sha=pristine_base_sha_val,
            pristine_end_sha=pristine_end_sha_val,
            allow_latest_attempt_fallback=False,
        )
        path = write_build_status(status, dockerfiles_dir, milestone_id)
        logger.info(
            f"[{milestone_id}] Wrote build_status.json: built={status.get('built')} "
            f"failed_gates={status.get('failed_gates', [])} ({path})"
        )
        return status

    artifacts_valid, message = check_build_outputs(dockerfiles_dir, milestone_id)
    if not artifacts_valid:
        logger.error(f"[{milestone_id}] {message}")
        status = _evaluate_and_write_status(tests_success=False)
        archive.snapshot_after_validation(
            docker_dir=docker_dir,
            test_results_dir=paths["test_results_dir"],
            status=status,
            latest_attempt_dir=None,
            pipeline_status="artifact_invalid",
            validation_ran=False,
            validation_timeout_seconds=validation_timeout,
            validation_run_kind=validation_run_kind,
        )
        archive.finish()
        return BuildResult(
            milestone_id=milestone_id,
            status="validation_failed",
            attempt=validation_attempt,
            duration_seconds=time.time() - start_time,
            prompt_file=prompt_file,
            error=summarize_build_status_failure(status),
        )

    lint_violations = lint_dockerfile(
        dockerfile_path=docker_dir / "Dockerfile",
        src_dirs=repo_src_dirs,
        test_dir=test_dir,
        allowed_tags=[base_state, end_state],
    )
    lint_blockers = [v for v in lint_violations if v.severity == "block"]
    lint_warnings = [v for v in lint_violations if v.severity == "warn"]
    for warning in lint_warnings:
        logger.warning(f"[{milestone_id}] Lint: {warning.rule} (L{warning.line}): {warning.detail}")

    if lint_blockers:
        logger.error(f"[{milestone_id}] Dockerfile lint blocked ({len(lint_blockers)} violation(s))")
        status = _evaluate_and_write_status(
            tests_success=False,
            lint_violations=lint_violations,
        )
        archive.snapshot_after_validation(
            docker_dir=docker_dir,
            test_results_dir=paths["test_results_dir"],
            status=status,
            latest_attempt_dir=None,
            pipeline_status="lint_blocked",
            validation_ran=False,
            validation_timeout_seconds=validation_timeout,
            validation_run_kind=validation_run_kind,
        )
        archive.finish()
        return BuildResult(
            milestone_id=milestone_id,
            status="validation_failed",
            attempt=validation_attempt,
            duration_seconds=time.time() - start_time,
            prompt_file=prompt_file,
            error=summarize_build_status_failure(status),
        )

    agent_history[0]["validation_ran"] = True
    success, test_summary, validation_attempt_dir, validation_runs = _run_validation_with_retries(
        milestone_id=milestone_id,
        work_dir=work_dir,
        repo_id=repo_id,
        test_scenario=test_scenario,
        language=language,
        test_framework=test_framework,
        attempt_number=validation_attempt,
        force_rebuild_image=True,
        timeout_seconds=validation_timeout,
        validation_retries=validation_retries,
    )
    agent_history[0]["validation_subprocess_attempts"] = validation_runs

    if success:
        patched_ok, _ = collect_patched_tests(
            milestone_id=milestone_id,
            work_dir=work_dir,
            repo_path=repo_path,
            layer="2",
        )
        if not patched_ok:
            logger.warning(f"[{milestone_id}] Patched-test artifact refresh failed or remained incomplete")
    else:
        logger.warning(f"[{milestone_id}] Skipping patched-test refresh because validation did not complete")

    status = _evaluate_and_write_status(
        tests_success=success,
        lint_violations=lint_violations,
        latest_attempt_dir=validation_attempt_dir,
    )
    if status.get("built"):
        pipeline_status = "success"
        result_status = "validated"
    elif get_module2_acceptance_mode(
        work_dir, repo_id, milestone_id
    ) == "agent_sufficient" and agent_sufficient_requested(docker_dir, milestone_id):
        pipeline_status = "module2_sufficient"
        result_status = "module2_sufficient"
        logger.info(
            f"[{milestone_id}] Agent marked Module 2 sufficient; validation-only run will end as module2_sufficient"
        )
    else:
        pipeline_status = "validation_failed"
        result_status = "validation_failed"

    archive.snapshot_after_validation(
        docker_dir=docker_dir,
        test_results_dir=paths["test_results_dir"],
        status=status,
        latest_attempt_dir=validation_attempt_dir,
        pipeline_status=pipeline_status,
        validation_ran=True,
        validation_timeout_seconds=validation_timeout,
        validation_run_kind=validation_run_kind,
    )
    archive.finish()

    return BuildResult(
        milestone_id=milestone_id,
        status=result_status,
        attempt=validation_attempt,
        duration_seconds=time.time() - start_time,
        prompt_file=prompt_file,
        error=None if result_status in {"validated", "module2_sufficient"} else summarize_build_status_failure(status),
        test_summary=test_summary,
    )


# =============================================================================
# Single milestone build
# =============================================================================


def build_single_milestone(
    milestone_id: str,
    milestone_data: Dict[str, Any],
    work_dir: Path,
    paths: Dict[str, Any],
    base_image: str,
    agent: str = DEFAULT_AGENT,
    model: Optional[str] = None,
    reasoning_effort: str = DEFAULT_CODEX_REASONING_EFFORT,
    timeout: int = DEFAULT_AGENT_TIMEOUT_SECONDS,
    agent_resume_attempts: int = DEFAULT_AGENT_RESUME_ATTEMPTS,
    max_retries: int = 3,
    language: str = "python",
    test_framework: str = "pytest",
    validation_timeout: Optional[int] = None,
    validation_retries: int = 0,
    dry_run: bool = False,
    skip_validation: bool = False,
    allow_rebuild: bool = False,
    regen_dockerfile: bool = False,
    extra_guidance_text: Optional[str] = None,
    extra_guidance_source: Optional[str] = None,
    resume_stuck_sessions: bool = False,
) -> BuildResult:
    """Build a Docker image for a single milestone.

    Default: if a Dockerfile already exists, enter refine mode — agent edits
    the existing Dockerfile based on build_status.json evidence.

    Flags:
      allow_rebuild: deprecated compatibility flag; current-file rebuild and
                     validation is automatic when build_status.json input
                     hashes are missing or stale.
      regen_dockerfile: back up existing Dockerfile to Dockerfile.bak (numeric
                        suffix on collision) and let the agent regenerate from
                        scratch.

    Pipeline:
      1. Generate prompt from template
      2. Run CLI coding agent (with retry loop for steps 2-5)
      3. Check build outputs (Dockerfile + test_config.json)
      4. Run tests and refresh patched-test artifacts
      5. Evaluate Spec v0.9 hard gates and write build_status.json
    """
    start_time = time.time()
    repo_id = paths["repo_id"]
    test_scenario = paths["test_scenario"]
    validation_timeout = resolve_validation_timeout_seconds(work_dir, repo_id, validation_timeout)
    model = model or default_model_for_agent(agent)
    image_name = get_milestone_image_name(repo_id, test_scenario, milestone_id)
    dockerfiles_dir = paths["dockerfiles_dir"]
    docker_dir = dockerfiles_dir / milestone_id
    dockerfile_path = dockerfiles_dir / milestone_id / "Dockerfile"
    build_status_path = docker_dir / "build_status.json"

    logger.info(f"[{milestone_id}] Starting build... (image: {image_name})")

    test_results_dir = paths["test_results_dir"]
    commit_changed_tests_dir = work_dir / "commit_level" / "patched_tests"

    commits_raw = milestone_data.get("commits", "")
    milestone_commits = (
        [c.strip() for c in commits_raw.split(";") if c.strip()] if isinstance(commits_raw, str) else commits_raw
    )

    with open(paths["metadata_path"]) as f:
        metadata = json.load(f)

    # Use testbed (has milestone tags) instead of original repo
    repo_path = paths["testbed_dir"]
    repo_src_dirs = metadata.get("repo_src_dirs", [])
    test_dir = metadata.get("test_dir", "test")
    test_dirs_meta = metadata.get("test_dirs", []) or []
    exclude_patterns_meta = metadata.get("exclude_patterns", []) or []
    test_infra_patterns_meta = metadata.get("test_infra_patterns", []) or None
    base_state = milestone_data.get("tag_name_start", f"milestone-{milestone_id}-start")
    end_state = milestone_data.get("tag_name_end", f"milestone-{milestone_id}-end")
    pristine_base_sha_val = pristine_state_sha(repo_path, base_state)
    pristine_end_sha_val = pristine_state_sha(repo_path, end_state)

    agent_sessions: List[str] = []
    agent_history: List[Dict[str, Any]] = []

    def _read_build_status() -> Optional[Dict[str, Any]]:
        if not build_status_path.exists():
            return None
        try:
            status = json.loads(build_status_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[{milestone_id}] Ignoring unreadable build_status.json: {e}")
            return None
        return status if isinstance(status, dict) else None

    def _agent_sufficient_requested() -> bool:
        return agent_sufficient_requested(docker_dir, milestone_id)

    def _module2_acceptance_mode() -> str:
        return get_module2_acceptance_mode(work_dir, repo_id, milestone_id)

    def _evaluate_and_write_status(
        *,
        agent_success: Optional[bool],
        tests_success: Optional[bool],
        agent_required: bool = True,
        lint_violations: Optional[List[Any]] = None,
        latest_attempt_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        status = evaluate_build_status(
            milestone_id=milestone_id,
            work_dir=work_dir,
            dockerfiles_dir=dockerfiles_dir,
            repo_id=repo_id,
            test_scenario=test_scenario,
            base_image=base_image,
            base_state=base_state,
            end_state=end_state,
            image_name=image_name,
            latest_attempt_dir=latest_attempt_dir,
            agent_success=agent_success,
            agent_required=agent_required,
            agent_history=agent_history,
            tests_subprocess_ok=tests_success,
            lint_violations=lint_violations,
            image_exists=check_image_exists(image_name),
            repo_src_dirs=repo_src_dirs,
            test_dir=test_dir,
            test_dirs=test_dirs_meta,
            exclude_patterns=exclude_patterns_meta,
            test_infra_patterns=test_infra_patterns_meta,
            pristine_base_sha=pristine_base_sha_val,
            pristine_end_sha=pristine_end_sha_val,
            allow_latest_attempt_fallback=False,
        )
        path = write_build_status(status, dockerfiles_dir, milestone_id)
        logger.info(
            f"[{milestone_id}] Wrote build_status.json: built={status.get('built')} "
            f"failed_gates={status.get('failed_gates', [])} ({path})"
        )
        return status

    def _validate_current_files(reason: str) -> Optional[BuildResult]:
        """Force rebuild/test current Dockerfile + test_config before agent work."""
        logger.info(f"[{milestone_id}] {reason}; rebuilding and validating current files...")
        validation_attempt = _next_validation_attempt_number(work_dir, milestone_id)
        success_refresh, _, validation_attempt_dir, validation_runs = _run_validation_with_retries(
            milestone_id=milestone_id,
            work_dir=work_dir,
            repo_id=repo_id,
            test_scenario=test_scenario,
            language=language,
            test_framework=test_framework,
            attempt_number=validation_attempt,
            force_rebuild_image=True,
            timeout_seconds=validation_timeout,
            validation_retries=validation_retries,
        )
        history_entry = _validation_only_agent_history_entry(
            validation_attempt=validation_attempt,
            validation_run_kind="startup_current_file_validation",
            note="Current-file validation before agent work.",
        )
        history_entry["validation_ran"] = True
        history_entry["validation_subprocess_attempts"] = validation_runs
        agent_history.append(history_entry)
        if success_refresh:
            collect_patched_tests(
                milestone_id=milestone_id,
                work_dir=work_dir,
                repo_path=paths["testbed_dir"],
                layer="2",
            )
        else:
            logger.warning(f"[{milestone_id}] Current-file validation did not complete normally")
        status = _evaluate_and_write_status(
            agent_success=None,
            agent_required=False,
            tests_success=success_refresh,
            lint_violations=None,
            latest_attempt_dir=validation_attempt_dir,
        )
        if status.get("built"):
            logger.info(f"[{milestone_id}] Current files passed hard gates; skipping agent")
            return BuildResult(
                milestone_id=milestone_id,
                status="validated",
                skipped_build=True,
                duration_seconds=time.time() - start_time,
            )
        logger.warning(
            f"[{milestone_id}] Current files failed hard gates — will refine: {status.get('failed_gates', [])}"
        )
        return None

    # --- regen: back up existing Dockerfile so refine mode is skipped ---
    if regen_dockerfile and dockerfile_path.exists() and not dry_run:
        backup_path = backup_dockerfile(dockerfile_path)
        if backup_path:
            logger.info(f"[{milestone_id}] Backed up existing Dockerfile → {backup_path.name}")

    # --- Early exit / re-entry validation for existing current files ---
    image_exists = check_image_exists(image_name)
    if dockerfile_path.exists() and not regen_dockerfile:
        logger.info(f"[{milestone_id}] Existing Dockerfile found, checking build_status.json...")

        if not dry_run:
            if skip_validation:
                if image_exists:
                    return BuildResult(
                        milestone_id=milestone_id,
                        status="skipped",
                        skipped_build=True,
                        duration_seconds=time.time() - start_time,
                    )
            else:
                input_hashes = current_build_inputs(docker_dir)
                if not all(input_hashes.values()):
                    logger.info(
                        f"[{milestone_id}] Existing files are incomplete (inputs={input_hashes}); entering agent mode"
                    )
                else:
                    status = _read_build_status()
                    if status and not status.get("built"):
                        status_current = build_status_matches_current_inputs(status, docker_dir)
                        freshness = "matches current files" if status_current else "is missing/stale for current hashes"
                        logger.info(
                            f"[{milestone_id}] build_status.json built=false ({freshness}), "
                            f"entering refine mode: {status.get('failed_gates', [])}"
                        )
                    elif status and status.get("built"):
                        status_current = build_status_matches_current_inputs(status, docker_dir)
                        image_current = build_status_image_matches_current(status)
                        if status_current and image_current:
                            logger.info(
                                f"[{milestone_id}] build_status.json matches current files and built=true; skipping"
                            )
                            return BuildResult(
                                milestone_id=milestone_id,
                                status="validated",
                                skipped_build=True,
                                duration_seconds=time.time() - start_time,
                            )
                        if not status_current:
                            reason = "build_status.json built=true does not match current Dockerfile/test_config"
                        else:
                            reason = "build_status.json built=true but Docker image id/name is not current"
                        result = _validate_current_files(reason)
                        if result:
                            return result
                    else:
                        logger.info(
                            f"[{milestone_id}] No build_status.json yet; entering agent mode "
                            "without startup full validation"
                        )

    # --- Step 1: Generate prompt ---
    # Milestone prompts now use one Jinja entrypoint and inject language/repo packs.
    template_path = Path(__file__).parent / "prompts" / "base.md.j2"

    if not template_path.exists():
        return BuildResult(
            milestone_id=milestone_id,
            status="agent_failed",
            duration_seconds=time.time() - start_time,
            error=f"Template not found: {template_path}",
        )

    logger.info(f"[{milestone_id}] Generating prompt...")
    render_result = render_env_setup_prompt(
        milestone_id=milestone_id,
        milestone_commits=milestone_commits,
        base_state=base_state,
        end_state=end_state,
        repo_path=repo_path,
        repo_src_dirs=repo_src_dirs,
        test_dir=test_dir,
        base_image=base_image,
        base_dockerfile_path=work_dir.parent / "base" / "dockerfiles" / "Dockerfile",
        commit_changed_tests_dir=commit_changed_tests_dir,
        test_runner_script="deepcommit.harness.test_runner.run_milestone_tests",
        test_results_dir=test_results_dir,
        output_dir=dockerfiles_dir,
        template_path=template_path,
        work_dir=work_dir,
        repo_id=repo_id,
        language=language,
        test_framework=test_framework,
    )

    if render_result != 0:
        return BuildResult(
            milestone_id=milestone_id,
            status="agent_failed",
            duration_seconds=time.time() - start_time,
            error="Failed to render prompt",
        )

    prompt_file = dockerfiles_dir / milestone_id / "env_setup_prompt.md"
    logger.info(f"[{milestone_id}] Prompt generated: {prompt_file}")

    if dry_run:
        return BuildResult(
            milestone_id=milestone_id,
            status="dry_run",
            duration_seconds=time.time() - start_time,
            prompt_file=prompt_file,
        )

    # --- Steps 2-5: Agent retry loop ---
    # Wrapper log is written directly to each attempt's archive (no workspace-root
    # _agent_logs/ duplicate); log_dir=None disables the runner's fallback writer.
    runner = make_agent_runner(
        agent,
        workspace=get_agent_workspace_root(),
        log_dir=None,
        timeout_ms=timeout * 1000,
        model=model,
        reasoning_effort=reasoning_effort,
        agent_resume_attempts=agent_resume_attempts,
    )

    prompt = prompt_file.read_text()

    # Resolve operator directive: per-milestone override > run-wide --extra-guidance.
    per_mid_guidance_file = docker_dir / "extra_guidance.md"
    if per_mid_guidance_file.exists():
        try:
            resolved_guidance_text = per_mid_guidance_file.read_text(encoding="utf-8")
            resolved_guidance_source = str(per_mid_guidance_file)
            logger.info(f"[{milestone_id}] Using per-milestone extra_guidance.md ({len(resolved_guidance_text)} chars)")
        except OSError as exc:
            logger.warning(
                f"[{milestone_id}] Could not read per-milestone extra_guidance.md: {exc}; "
                "falling back to run-wide --extra-guidance if any"
            )
            resolved_guidance_text = extra_guidance_text
            resolved_guidance_source = extra_guidance_source
    else:
        resolved_guidance_text = extra_guidance_text
        resolved_guidance_source = extra_guidance_source

    # Auto-refine: if Dockerfile already exists, point the agent at current
    # hard-gate status. build_status.json is authoritative; debug diagnosis is
    # intentionally not used for refine / skip decisions.
    existing_dockerfile = dockerfile_path
    if existing_dockerfile.exists() and not regen_dockerfile:
        logger.info(f"[{milestone_id}] Existing Dockerfile found, entering refine mode...")
        status = _read_build_status()
        if status:
            diagnosis_text = summarize_build_status_failure(status)
        else:
            diagnosis_text = (
                "No build_status.json exists for this milestone yet. "
                "Refine the existing Dockerfile, run validation, and rely on "
                "the next build_status.json failed_gates for any further retry."
            )

        prompt = build_refine_prompt(
            prompt,
            str(dockerfiles_dir / milestone_id),
            diagnosis_text,
            operator_directive=resolved_guidance_text,
            operator_directive_source=resolved_guidance_source,
        )
        if resolved_guidance_text:
            logger.info(
                f"[{milestone_id}] Refine mode: injected operator directive "
                f"({len(resolved_guidance_text)} chars) from "
                f"{resolved_guidance_source or '<inline>'}"
            )
        logger.info(f"[{milestone_id}] Refine mode: pointing agent to {dockerfiles_dir / milestone_id}")
    elif resolved_guidance_text:
        # Edge case 1: --extra-guidance set but refine mode won't trigger
        # (no Dockerfile to refine, or --regen-dockerfile was passed).
        return BuildResult(
            milestone_id=milestone_id,
            status="agent_failed",
            duration_seconds=time.time() - start_time,
            error=(
                "extra-guidance requires refine mode (existing Dockerfile + no --regen-dockerfile). "
                f"Milestone {milestone_id} has no Dockerfile or --regen-dockerfile is set; "
                "remove the guidance for this milestone or build a baseline Dockerfile first."
            ),
        )

    base_prompt = prompt  # Preserve prompt (with refine context if any) for retries
    last_error = None
    artifacts_valid = False

    # Resume policy: when --resume-stuck-sessions is on, BEFORE every attempt,
    # check whether the most recent archived attempt for this milestone is a
    # stuck session (429 / session limit / 503) with its CLI session file still
    # on disk. If yes, resume that session; otherwise start fresh. This is
    # re-evaluated each attempt so that even attempt 5 after attempt 4 hit 429
    # will resume attempt 4's session.

    for attempt in range(1, max_retries + 1):
        logger.info(f"[{milestone_id}] Agent attempt {attempt}/{max_retries}...")

        # Decide BEFORE reserving an attempt number: is there a resumable prior
        # attempt? If so, REUSE its slot rather than create a new one. The prior
        # attempt's before_agent snapshot stays intact (it captured the true
        # pre-attempt state); only its agent/ subdir (which contains the failed
        # 429 wrapper.log) gets overwritten by this re-attempt.
        resume_psid_for_this_attempt: Optional[str] = None
        reuse_source_attempt_num: Optional[int] = None
        if resume_stuck_sessions and agent == "claude":
            candidate = _find_resumable_session(work_dir, milestone_id)
            if candidate and candidate.get("session_file_present"):
                resume_psid_for_this_attempt = candidate["provider_session_id"]
                reuse_source_attempt_num = candidate["attempt"]
            elif candidate:
                logger.warning(
                    f"[{milestone_id}] prior stuck session detected "
                    f"(psid={candidate['provider_session_id']}) but CLI session file "
                    f"(~/.claude/projects/<slug>/{candidate['provider_session_id']}.jsonl) "
                    f"is missing — cannot resume; will start fresh attempt."
                )

        if reuse_source_attempt_num is not None:
            reserved_attempt_number = reuse_source_attempt_num
            logger.info(
                f"[{milestone_id}] REUSING attempt_{reserved_attempt_number} slot for resume "
                f"(psid={resume_psid_for_this_attempt}). before_agent snapshot preserved; "
                f"overwriting agent/ subdir."
            )
            # Wipe only the agent/ subdir (the 429 wrapper.log). Preserve
            # before_agent, after_agent (if any), meta.json (we'll overwrite that).
            import shutil

            agent_subdir = work_dir / "_build_runs" / milestone_id / f"attempt_{reserved_attempt_number}" / "agent"
            if agent_subdir.exists():
                shutil.rmtree(agent_subdir)
        else:
            reserved_attempt_number = _next_validation_attempt_number(work_dir, milestone_id)

        archive = MilestoneAttemptArchive.start(
            work_dir=work_dir,
            milestone_id=milestone_id,
            attempt_number=reserved_attempt_number,
            repo_id=repo_id,
            test_scenario=test_scenario,
            agent=agent,
            model=model,
            reasoning_effort=reasoning_effort if agent == "codex" else None,
            agent_retry_attempt=attempt,
            docker_dir=docker_dir,
            prompt_file=prompt_file,
            prompt_text=prompt,
        )

        # Step 2: Run agent — write wrapper log directly into this attempt's archive
        wrapper_log_path = archive.root / "agent" / "wrapper.log"
        wrapper_log_path.parent.mkdir(parents=True, exist_ok=True)
        if agent == "claude":
            agent_result = runner.run(
                prompt=prompt,
                task_name=f"{milestone_id}_attempt_{attempt}",
                log_path=wrapper_log_path,
                resume_provider_session_id=resume_psid_for_this_attempt,
            )
        else:
            # Codex runner doesn't yet implement the resume hook; ignore the flag.
            agent_result = runner.run(
                prompt=prompt,
                task_name=f"{milestone_id}_attempt_{attempt}",
                log_path=wrapper_log_path,
            )
        # Fast-fail on quota_exhausted (429 / session-limit). PRESERVE the
        # attempt dir (before_agent + any prior after_agent snapshots are
        # audit-worthy even if THIS run did no LLM work). Exit the milestone
        # so the caller can re-launch with --resume-stuck-sessions when quota
        # recovers; the resume path will REUSE this attempt's slot rather than
        # incrementing the counter (see logic at top of attempt loop).
        if getattr(agent_result, "quota_exhausted", False):
            logger.warning(
                f"[{milestone_id}] attempt_{reserved_attempt_number}: quota exhausted (429); "
                f"preserving attempt dir for audit + exiting milestone. Re-run with "
                f"--resume-stuck-sessions when quota recovers — that will reuse this "
                f"attempt slot rather than create a new one."
            )
            return BuildResult(
                milestone_id=milestone_id,
                status="quota_exhausted",
                attempt=attempt,
                duration_seconds=time.time() - start_time,
                prompt_file=prompt_file,
                error=(
                    "API quota exhausted (429 / session limit). "
                    "Re-run with --resume-stuck-sessions when quota recovers."
                ),
                agent_sessions=agent_sessions,
            )
        agent_sessions.append(agent_result.session_id)
        archive.record_agent_result(agent_result=agent_result)
        archive.snapshot_after_agent(docker_dir)
        history_entry = {
            "attempt": attempt,
            "reserved_attempt": f"attempt_{reserved_attempt_number}",
            "validation_ran": False,
            "agent": agent,
            "model": model,
            "reasoning_effort": reasoning_effort if agent == "codex" else None,
            "session_id": agent_result.session_id,
            "provider_session_id": agent_result.provider_session_id,
            "resume_attempts": agent_result.resume_attempts,
            "success": agent_result.success,
            "duration_ms": agent_result.duration_ms,
            "exit_code": agent_result.exit_code,
            "error": agent_result.error,
        }
        agent_history.append(history_entry)

        if not agent_result.success:
            last_error = f"Agent execution failed: {agent_result.error}"
            logger.error(f"[{milestone_id}] {last_error}")
            status = _evaluate_and_write_status(
                agent_success=False,
                tests_success=False,
                lint_violations=None,
            )
            archive.snapshot_after_validation(
                docker_dir=docker_dir,
                test_results_dir=test_results_dir,
                status=status,
                latest_attempt_dir=None,
                pipeline_status="agent_failed",
                validation_ran=False,
                validation_timeout_seconds=validation_timeout,
            )
            archive.finish()
            last_error = summarize_build_status_failure(status)
            if attempt < max_retries:
                prompt = build_retry_prompt(
                    base_prompt,
                    last_error,
                    latest_attempt_dir=_latest_attempt_dir(work_dir, milestone_id),
                    save_to=dockerfiles_dir / milestone_id / "retry_context.md",
                    work_dir=work_dir,
                    milestone_id=milestone_id,
                    dockerfiles_dir=dockerfiles_dir,
                )
                continue
            break

        # Step 3: Check build outputs
        artifacts_valid, message = check_build_outputs(dockerfiles_dir, milestone_id)
        if not artifacts_valid:
            last_error = message
            logger.error(f"[{milestone_id}] {last_error}")
            status = _evaluate_and_write_status(
                agent_success=True,
                tests_success=False,
                lint_violations=None,
            )
            archive.snapshot_after_validation(
                docker_dir=docker_dir,
                test_results_dir=test_results_dir,
                status=status,
                latest_attempt_dir=None,
                pipeline_status="artifact_invalid",
                validation_ran=False,
                validation_timeout_seconds=validation_timeout,
            )
            archive.finish()
            last_error = summarize_build_status_failure(status)
            if attempt < max_retries:
                prompt = build_retry_prompt(
                    base_prompt,
                    last_error,
                    latest_attempt_dir=_latest_attempt_dir(work_dir, milestone_id),
                    save_to=dockerfiles_dir / milestone_id / "retry_context.md",
                    work_dir=work_dir,
                    milestone_id=milestone_id,
                    dockerfiles_dir=dockerfiles_dir,
                )
                continue
            break

        logger.info(f"[{milestone_id}] Build outputs validated")

        # Step 3.5: Lint Dockerfile for forbidden operations
        dockerfile_path = dockerfiles_dir / milestone_id / "Dockerfile"
        lint_violations = lint_dockerfile(
            dockerfile_path=dockerfile_path,
            src_dirs=repo_src_dirs,
            test_dir=test_dir,
            allowed_tags=[base_state, end_state],
        )
        lint_blockers = [v for v in lint_violations if v.severity == "block"]
        lint_warnings = [v for v in lint_violations if v.severity == "warn"]

        if lint_warnings:
            for w in lint_warnings:
                logger.warning(f"[{milestone_id}] Lint: {w.rule} (L{w.line}): {w.detail}")

        if lint_blockers:
            last_error = format_violations(lint_blockers)
            logger.error(f"[{milestone_id}] Dockerfile lint blocked ({len(lint_blockers)} violation(s))")
            status = _evaluate_and_write_status(
                agent_success=True,
                tests_success=False,
                lint_violations=lint_violations,
            )
            archive.snapshot_after_validation(
                docker_dir=docker_dir,
                test_results_dir=test_results_dir,
                status=status,
                latest_attempt_dir=None,
                pipeline_status="lint_blocked",
                validation_ran=False,
                validation_timeout_seconds=validation_timeout,
            )
            archive.finish()
            last_error = summarize_build_status_failure(status)
            if attempt < max_retries:
                prompt = build_retry_prompt(
                    base_prompt,
                    last_error,
                    latest_attempt_dir=_latest_attempt_dir(work_dir, milestone_id),
                    save_to=dockerfiles_dir / milestone_id / "retry_context.md",
                    work_dir=work_dir,
                    milestone_id=milestone_id,
                    dockerfiles_dir=dockerfiles_dir,
                )
                continue
            break

        # Step 4: Run tests and refresh patched-test artifacts.
        if skip_validation:
            archive.meta.update(
                {
                    "pipeline_status": "success",
                    "validation_ran": False,
                    "validation_attempt": None,
                }
            )
            archive.finish()
            return BuildResult(
                milestone_id=milestone_id,
                status="success",
                attempt=attempt,
                duration_seconds=time.time() - start_time,
                prompt_file=prompt_file,
                agent_sessions=agent_sessions,
            )

        test_summary: Optional[Dict[str, Any]] = None
        success = False

        validation_attempt = reserved_attempt_number
        history_entry["validation_ran"] = True
        success, test_summary, validation_attempt_dir, validation_runs = _run_validation_with_retries(
            milestone_id=milestone_id,
            work_dir=work_dir,
            repo_id=repo_id,
            test_scenario=test_scenario,
            language=language,
            test_framework=test_framework,
            attempt_number=validation_attempt,
            force_rebuild_image=True,
            timeout_seconds=validation_timeout,
            validation_retries=validation_retries,
        )
        history_entry["validation_subprocess_attempts"] = validation_runs

        # ensure_patched_tests already ran Layer 1 for the whole scenario at
        # entry; here we refresh only this milestone's Layer-2 classification
        # intersection against the just-produced test results.
        if success:
            patched_ok, _ = collect_patched_tests(
                milestone_id=milestone_id,
                work_dir=work_dir,
                repo_path=repo_path,
                layer="2",
            )
            if not patched_ok:
                logger.warning(f"[{milestone_id}] Patched-test artifact refresh failed or remained incomplete")
        else:
            logger.warning(f"[{milestone_id}] Skipping patched-test refresh because validation did not complete")

        status = _evaluate_and_write_status(
            agent_success=True,
            tests_success=success,
            lint_violations=lint_violations,
            latest_attempt_dir=validation_attempt_dir,
        )

        if not success:
            archive.snapshot_after_validation(
                docker_dir=docker_dir,
                test_results_dir=test_results_dir,
                status=status,
                latest_attempt_dir=validation_attempt_dir,
                pipeline_status="validation_failed",
                validation_ran=True,
                validation_timeout_seconds=validation_timeout,
            )
            archive.finish()
            # Pull the D3 signal written by run_tests if the subprocess
            # failed without producing a pytest report.
            runner_diag_path = work_dir / "test_results" / milestone_id / "refinement" / "runner_diagnostic.json"
            if runner_diag_path.exists():
                try:
                    rd = json.loads(runner_diag_path.read_text())
                    last_error = (
                        f"Test execution failed (exit={rd.get('exit_code')}, "
                        f"timed_out={rd.get('timed_out')}, duration={rd.get('duration_seconds')}s).\n"
                        f"Reports written: {rd.get('reports_written', {})}\n"
                        f"Runner stderr tail:\n{rd.get('stderr_tail', '')[:1500]}"
                    )
                except (json.JSONDecodeError, OSError):
                    last_error = "Test execution failed"
            else:
                last_error = "Test execution failed"
            logger.warning(f"[{milestone_id}] {last_error[:200]}")
            last_error = last_error + "\n\n" + summarize_build_status_failure(status)
            if attempt < max_retries:
                prompt = build_retry_prompt(
                    base_prompt,
                    last_error,
                    latest_attempt_dir=validation_attempt_dir,
                    save_to=dockerfiles_dir / milestone_id / "retry_context.md",
                    work_dir=work_dir,
                    milestone_id=milestone_id,
                    dockerfiles_dir=dockerfiles_dir,
                )
                continue
            break

        # Step 5: build_status.json is the sole acceptance authority.
        if not status.get("built"):
            last_error = summarize_build_status_failure(status)
            logger.warning(f"[{milestone_id}] Hard gates failed: {status.get('failed_gates', [])}")
            if _module2_acceptance_mode() == "agent_sufficient" and _agent_sufficient_requested():
                logger.info(f"[{milestone_id}] Agent marked Module 2 sufficient; ending retry as module2_sufficient")
                archive.snapshot_after_validation(
                    docker_dir=docker_dir,
                    test_results_dir=test_results_dir,
                    status=status,
                    latest_attempt_dir=validation_attempt_dir,
                    pipeline_status="module2_sufficient",
                    validation_ran=True,
                    validation_timeout_seconds=validation_timeout,
                )
                archive.finish()
                return BuildResult(
                    milestone_id=milestone_id,
                    status="module2_sufficient",
                    attempt=attempt,
                    duration_seconds=time.time() - start_time,
                    prompt_file=prompt_file,
                    test_summary=test_summary,
                    agent_sessions=agent_sessions,
                )
            archive.snapshot_after_validation(
                docker_dir=docker_dir,
                test_results_dir=test_results_dir,
                status=status,
                latest_attempt_dir=validation_attempt_dir,
                pipeline_status="validation_failed",
                validation_ran=True,
                validation_timeout_seconds=validation_timeout,
            )
            archive.finish()
            if attempt < max_retries:
                prompt = build_retry_prompt(
                    base_prompt,
                    last_error,
                    latest_attempt_dir=validation_attempt_dir,
                    save_to=dockerfiles_dir / milestone_id / "retry_context.md",
                    work_dir=work_dir,
                    milestone_id=milestone_id,
                    dockerfiles_dir=dockerfiles_dir,
                )
                continue
            break

        # All hard gates passed.
        archive.snapshot_after_validation(
            docker_dir=docker_dir,
            test_results_dir=test_results_dir,
            status=status,
            latest_attempt_dir=validation_attempt_dir,
            pipeline_status="success",
            validation_ran=True,
            validation_timeout_seconds=validation_timeout,
        )
        archive.finish()
        return BuildResult(
            milestone_id=milestone_id,
            status="success",
            attempt=attempt,
            duration_seconds=time.time() - start_time,
            prompt_file=prompt_file,
            test_summary=test_summary,
            agent_sessions=agent_sessions,
        )

    # All retries exhausted
    return BuildResult(
        milestone_id=milestone_id,
        status="agent_failed" if not artifacts_valid else "validation_failed",
        attempt=max_retries,
        duration_seconds=time.time() - start_time,
        prompt_file=prompt_file,
        error=last_error,
        agent_sessions=agent_sessions,
    )


# =============================================================================
# Multi-milestone orchestration
# =============================================================================


def build_milestones(
    milestone_ids: List[str],
    metadata: Dict[str, Any],
    work_dir: Path,
    paths: Dict[str, Any],
    base_image: str,
    parallel: int = 1,
    **kwargs,
) -> Dict[str, BuildResult]:
    """Build Docker images for multiple milestones (serial or parallel)."""
    milestone_map = {m["id"]: m for m in metadata["milestones"]}

    invalid = [mid for mid in milestone_ids if mid not in milestone_map]
    if invalid:
        raise ValueError(f"Invalid milestone IDs: {', '.join(invalid)}")

    results = {}

    if parallel > 1 and len(milestone_ids) > 1:
        logger.info(f"Building {len(milestone_ids)} milestones in parallel (max {parallel})...")
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(
                    build_single_milestone,
                    milestone_id=mid,
                    milestone_data=milestone_map[mid],
                    work_dir=work_dir,
                    paths=paths,
                    base_image=base_image,
                    **kwargs,
                ): mid
                for mid in milestone_ids
            }
            for future in as_completed(futures):
                mid = futures[future]
                try:
                    results[mid] = future.result()
                except Exception as e:
                    results[mid] = BuildResult(milestone_id=mid, status="agent_failed", error=str(e))
                    logger.error(f"[{mid}] Exception: {e}")
    else:
        for i, mid in enumerate(milestone_ids, 1):
            logger.info(f"[{i}/{len(milestone_ids)}] Building {mid}...")
            results[mid] = build_single_milestone(
                milestone_id=mid,
                milestone_data=milestone_map[mid],
                work_dir=work_dir,
                paths=paths,
                base_image=base_image,
                **kwargs,
            )

    return results


def validate_current_milestones(
    milestone_ids: List[str],
    metadata: Dict[str, Any],
    work_dir: Path,
    paths: Dict[str, Any],
    base_image: str,
    parallel: int = 1,
    language: str = "python",
    test_framework: str = "pytest",
    validation_timeout: Optional[int] = None,
    validation_retries: int = 0,
) -> Dict[str, BuildResult]:
    """Validate current artifacts for multiple milestones without an agent."""
    milestone_map = {m["id"]: m for m in metadata["milestones"]}

    invalid = [mid for mid in milestone_ids if mid not in milestone_map]
    if invalid:
        raise ValueError(f"Invalid milestone IDs: {', '.join(invalid)}")

    results = {}

    if parallel > 1 and len(milestone_ids) > 1:
        logger.info(f"Validating {len(milestone_ids)} milestones in parallel (max {parallel})...")
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(
                    validate_current_milestone,
                    milestone_id=mid,
                    milestone_data=milestone_map[mid],
                    work_dir=work_dir,
                    paths=paths,
                    base_image=base_image,
                    language=language,
                    test_framework=test_framework,
                    validation_timeout=validation_timeout,
                    validation_retries=validation_retries,
                ): mid
                for mid in milestone_ids
            }
            for future in as_completed(futures):
                mid = futures[future]
                try:
                    results[mid] = future.result()
                except Exception as e:
                    results[mid] = BuildResult(milestone_id=mid, status="validation_failed", error=str(e))
                    logger.error(f"[{mid}] Exception: {e}")
    else:
        for i, mid in enumerate(milestone_ids, 1):
            logger.info(f"[{i}/{len(milestone_ids)}] Validating current artifacts for {mid}...")
            results[mid] = validate_current_milestone(
                milestone_id=mid,
                milestone_data=milestone_map[mid],
                work_dir=work_dir,
                paths=paths,
                base_image=base_image,
                language=language,
                test_framework=test_framework,
                validation_timeout=validation_timeout,
                validation_retries=validation_retries,
            )

    return results


def discover_milestones(metadata: Dict[str, Any]) -> List[str]:
    """Discover all milestone IDs from metadata."""
    return [m["id"] for m in metadata.get("milestones", [])]


# =============================================================================
# CLI
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Build milestone Docker images using a CLI coding agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m deepcommit.harness.prepare_images.build_milestone_image.build_milestone_image \\
      --all --work-dir DATA/harness_workspace/repo/scenario

  python -m deepcommit.harness.prepare_images.build_milestone_image.build_milestone_image \\
      M001 M002 --work-dir DATA/harness_workspace/repo/scenario --parallel 3

  # Regenerate Dockerfile from scratch (backs up existing one)
  python -m deepcommit.harness.prepare_images.build_milestone_image.build_milestone_image \\
      --all --regen-dockerfile --work-dir DATA/harness_workspace/repo/scenario

  python -m deepcommit.harness.prepare_images.build_milestone_image.build_milestone_image \\
      --all --dry-run --work-dir DATA/harness_workspace/repo/scenario

  # Validate current Dockerfile/test_config artifacts without running an agent
  python -m deepcommit.harness.prepare_images.build_milestone_image.build_milestone_image \\
      M001 --validate-current-only --work-dir DATA/harness_workspace/repo/scenario
        """,
    )

    # Positional: milestone IDs
    parser.add_argument("milestones", nargs="*", help="Milestone ID(s) to build")

    # Required
    parser.add_argument("--work-dir", type=Path, required=True, help="Harness workspace directory")

    # Milestone selection
    parser.add_argument(
        "--all",
        action="store_true",
        help="Build all milestones from metadata.json",
    )

    # Optional arguments
    parser.add_argument(
        "--base-image",
        type=str,
        default=None,
        help="Base Docker image name (default: inferred from work_dir as {repo_id}/base:latest)",
    )
    parser.add_argument(
        "--agent",
        type=str,
        default=DEFAULT_AGENT,
        choices=["codex", "claude"],
        help=f"Agent provider to use (default: {DEFAULT_AGENT})",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help=(f"Agent model to use (default: {DEFAULT_CODEX_MODEL} for codex, {DEFAULT_CLAUDE_MODEL} for claude)"),
    )
    parser.add_argument(
        "--reasoning-effort",
        type=str,
        default=DEFAULT_CODEX_REASONING_EFFORT,
        choices=["low", "medium", "high", "xhigh"],
        help=f"Codex reasoning effort (default: {DEFAULT_CODEX_REASONING_EFFORT})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_AGENT_TIMEOUT_SECONDS,
        help=f"Agent timeout in seconds (default: {DEFAULT_AGENT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--validation-timeout",
        type=int,
        default=None,
        help=(
            "Outer validation pipeline timeout in seconds "
            f"(default: config module2_validation_timeout_seconds, currently {DEFAULT_VALIDATION_TIMEOUT_SECONDS})"
        ),
    )
    parser.add_argument(
        "--validation-retries",
        type=int,
        default=None,
        help=(
            "Retry the same validation attempt after an outer validation timeout "
            "(default: config module2_validation_retries, currently 3)."
        ),
    )
    parser.add_argument(
        "--agent-resume-attempts",
        type=int,
        default=DEFAULT_AGENT_RESUME_ATTEMPTS,
        help=(
            "Maximum provider-session resume attempts within one agent retry "
            f"(default: {DEFAULT_AGENT_RESUME_ATTEMPTS}; use 0 to disable)"
        ),
    )
    parser.add_argument(
        "--codex-resume-attempts",
        dest="agent_resume_attempts",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum agent retry attempts (default: 3)",
    )
    parser.add_argument(
        "-p",
        "--parallel",
        type=int,
        default=1,
        help="Number of milestones to build in parallel (default: 1)",
    )
    parser.add_argument(
        "--language",
        type=str,
        default="python",
        choices=["python", "java", "go", "rust", "javascript"],
        help="Project language (default: python)",
    )
    parser.add_argument(
        "--test-framework",
        type=str,
        default="pytest",
        help="Test framework (default: pytest). Options: pytest, maven, gradle, go_test, cargo, jest, mocha",
    )

    # Mode control
    parser.add_argument("--dry-run", action="store_true", help="Only generate prompts")
    parser.add_argument("--skip-validation", action="store_true", help="Skip test validation after build")
    parser.add_argument(
        "--allow-rebuild",
        action="store_true",
        help="Deprecated compatibility flag. Current-file rebuild/validation is automatic "
        "when build_status.json does not match Dockerfile + test_config.json.",
    )
    parser.add_argument(
        "--regen-dockerfile",
        action="store_true",
        help="Back up existing Dockerfile to Dockerfile.bak (numeric suffix on collision) "
        "and regenerate from scratch via agent.",
    )
    parser.add_argument(
        "--validate-current-only",
        action="store_true",
        help=(
            "Skip the agent and run the validation pipeline against the current "
            "Dockerfile, test_config.json, and module2_waivers.json."
        ),
    )
    parser.add_argument(
        "--extra-guidance",
        type=Path,
        default=None,
        help=(
            "Path to a markdown file whose contents are injected into the refine "
            "context as an operator directive (rendered above the pipeline "
            "diagnostic). Requires refine mode (existing Dockerfile + no "
            "--regen-dockerfile). Per-milestone override: place "
            "dockerfiles/{mid}/extra_guidance.md to override the run-wide directive."
        ),
    )
    parser.add_argument(
        "--rerun-validation-attempt",
        type=int,
        default=None,
        help=(
            "Skip the agent and rerun validation for an existing archived attempt_N, "
            "writing validation artifacts back to the same _build_runs/{mid}/attempt_N."
        ),
    )
    parser.add_argument(
        "--allow-rerun-input-mismatch",
        action="store_true",
        help=(
            "Debug only: allow --rerun-validation-attempt to validate current "
            "Dockerfile/test_config/module2_waivers even when they differ from "
            "the archived after_agent snapshot."
        ),
    )
    parser.add_argument(
        "--resume-stuck-sessions",
        action="store_true",
        help=(
            "On the FIRST agent attempt of each milestone, if the latest archived "
            "attempt's wrapper.log shows a transient session-quota / rate-limit "
            "failure (429 / session limit / 503) and meta.json has a "
            "provider_session_id, resume that Claude session via "
            "`claude --resume <session-id>` instead of starting fresh. Later "
            "in-batch attempts revert to fresh sessions automatically. Claude only."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.validate_current_only and args.dry_run:
        print("Error: --validate-current-only cannot be combined with --dry-run", file=sys.stderr)
        return 1
    if args.validate_current_only and args.skip_validation:
        print("Error: --validate-current-only cannot be combined with --skip-validation", file=sys.stderr)
        return 1
    if args.validate_current_only and args.regen_dockerfile:
        print("Error: --validate-current-only cannot be combined with --regen-dockerfile", file=sys.stderr)
        return 1
    if args.extra_guidance is not None and args.validate_current_only:
        print(
            "Error: --extra-guidance cannot be combined with --validate-current-only (no agent runs)", file=sys.stderr
        )
        return 1
    if args.extra_guidance is not None and args.regen_dockerfile:
        print(
            "Error: --extra-guidance cannot be combined with --regen-dockerfile "
            "(refine mode required to inject directive)",
            file=sys.stderr,
        )
        return 1
    if args.validation_retries is not None and args.validation_retries < 0:
        print("Error: --validation-retries must be >= 0", file=sys.stderr)
        return 1
    if args.rerun_validation_attempt is not None:
        if args.rerun_validation_attempt <= 0:
            print("Error: --rerun-validation-attempt must be >= 1", file=sys.stderr)
            return 1
        if args.dry_run or args.skip_validation or args.regen_dockerfile:
            print(
                "Error: --rerun-validation-attempt cannot be combined with "
                "--dry-run, --skip-validation, or --regen-dockerfile",
                file=sys.stderr,
            )
            return 1
        args.validate_current_only = True
    elif args.allow_rerun_input_mismatch:
        print("Error: --allow-rerun-input-mismatch requires --rerun-validation-attempt", file=sys.stderr)
        return 1

    # Validate work_dir
    work_dir = args.work_dir.resolve()
    if not work_dir.exists():
        print(f"Error: Work directory not found: {work_dir}", file=sys.stderr)
        return 1

    # Infer paths and load metadata
    try:
        paths = infer_paths_from_work_dir(work_dir)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Non-blocking integrity check: warn (do not raise) if host testbed/ has
    # uncommitted modifications before any agent dispatch. Agent shouldn't
    # write to host testbed/ — if dirty, it's almost certainly residue from a
    # prior agent that touched testbed/ via Bash. Per-MS Dockerfile's
    # `RUN cd /testbed && git reset --hard HEAD` (added after COPY) is the
    # actual cleanliness mechanism (defense in depth); this warning just
    # surfaces the host-side dirty state so the operator can investigate the
    # source. See _orch_runs/round_005/narrative.md §"Prompt issues observed".
    testbed_dir = paths.get("testbed_dir")
    if testbed_dir and testbed_dir.exists() and (testbed_dir / ".git").exists():
        try:
            _status_check = subprocess.run(
                ["git", "-C", str(testbed_dir), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if _status_check.returncode == 0 and _status_check.stdout.strip():
                _dirty_lines = _status_check.stdout.strip().splitlines()
                _sample = "\n  ".join(_dirty_lines[:5])
                logger.warning(
                    f"[harness] testbed/ has {len(_dirty_lines)} uncommitted "
                    f"modifications before agent dispatch (likely residue from "
                    f"a prior agent that touched the host testbed/). The "
                    f"per-MS Dockerfile's `git reset --hard HEAD` after COPY "
                    f"will discard this inside containers, but the host-side "
                    f"dirty state suggests an agent contract violation worth "
                    f"investigating. Sample paths:\n  {_sample}\n  "
                    f"Forensic snapshot: cd {testbed_dir} && "
                    f"git diff --cached > /tmp/testbed_dirty.patch && "
                    f"git reset --hard HEAD"
                )
        except (subprocess.TimeoutExpired, OSError) as _exc:
            logger.debug(f"[harness] testbed/ status check skipped: {_exc}")

    metadata_path = paths["metadata_path"]
    if not metadata_path.exists():
        print(f"Error: Metadata file not found: {metadata_path}", file=sys.stderr)
        return 1

    with open(metadata_path) as f:
        metadata = json.load(f)

    base_image = args.base_image or paths["base_image"]
    agent_model = args.model or default_model_for_agent(args.agent)

    extra_guidance_text: Optional[str] = None
    extra_guidance_source: Optional[str] = None
    if args.extra_guidance is not None:
        guidance_path = args.extra_guidance.resolve()
        if not guidance_path.is_file():
            print(f"Error: --extra-guidance file not found: {guidance_path}", file=sys.stderr)
            return 1
        try:
            extra_guidance_text = guidance_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error: Could not read --extra-guidance file {guidance_path}: {exc}", file=sys.stderr)
            return 1
        if not extra_guidance_text.strip():
            print(f"Error: --extra-guidance file is empty: {guidance_path}", file=sys.stderr)
            return 1
        extra_guidance_source = str(guidance_path)
    validation_timeout = resolve_validation_timeout_seconds(
        work_dir,
        paths["repo_id"],
        args.validation_timeout,
    )
    validation_retries = resolve_validation_retries(
        work_dir,
        paths["repo_id"],
        args.validation_retries,
    )

    # Determine milestones
    if args.all:
        milestone_ids = discover_milestones(metadata)
        if not milestone_ids:
            print("Error: No milestones found in metadata.json", file=sys.stderr)
            return 1
    elif args.milestones:
        milestone_ids = args.milestones
    else:
        print("Error: Specify milestone ID(s) or use --all", file=sys.stderr)
        return 1
    if args.rerun_validation_attempt is not None and (args.all or len(milestone_ids) != 1):
        print("Error: --rerun-validation-attempt requires exactly one milestone ID", file=sys.stderr)
        return 1

    # Print configuration
    print(f"\n{'=' * 60}")
    print("Build Milestone Images")
    print(f"{'=' * 60}")
    print(f"Work directory: {work_dir}")
    print(f"Base image:     {base_image}")
    print(
        f"Milestones:     {len(milestone_ids)} ({', '.join(milestone_ids[:5])}{'...' if len(milestone_ids) > 5 else ''})"
    )
    print(
        f"Agent:          {args.agent}  |  Model: {agent_model}  |  Timeout: {args.timeout}s  |  Retries: {args.max_retries}"
    )
    print(f"Validation:     Timeout: {validation_timeout}s  |  Timeout retries: {validation_retries}")
    if args.agent == "codex":
        print(f"Reasoning:      {args.reasoning_effort}  |  Resume attempts: {args.agent_resume_attempts}")
    else:
        print(f"Resume attempts:{args.agent_resume_attempts}")
    print(f"Language:       {args.language}  |  Framework: {args.test_framework}  |  Parallel: {args.parallel}")
    modes = []
    if args.dry_run:
        modes.append("DRY RUN")
    if args.skip_validation:
        modes.append("SKIP VALIDATION")
    if args.allow_rebuild:
        modes.append("ALLOW REBUILD (deprecated; automatic when inputs changed)")
    if args.regen_dockerfile:
        modes.append("REGEN DOCKERFILE")
    if args.validate_current_only:
        modes.append("VALIDATE CURRENT ARTIFACTS ONLY")
    if extra_guidance_text:
        modes.append(f"EXTRA GUIDANCE ({len(extra_guidance_text)} chars from {extra_guidance_source})")
    if modes:
        print(f"Mode:           {', '.join(modes)}")
    print(f"{'=' * 60}\n")

    if not args.dry_run and not args.skip_validation:
        baseline_path = ensure_base_baseline(
            work_dir=work_dir,
            base_image=base_image,
            language=args.language,
            test_framework=args.test_framework,
            test_dir=metadata.get("test_dir", "test/"),
        )
        logger.info(f"Base baseline ready: {baseline_path}")

    # Ensure scenario-level patched tests are present before per-milestone
    # fan-out. Idempotent; skipped for dry-run since no real build occurs.
    if not args.dry_run:
        ensure_patched_tests(work_dir)

    # Run builds or validation-only checks
    start_time = time.time()
    if args.validate_current_only:
        if args.rerun_validation_attempt is not None:
            mid = milestone_ids[0]
            milestone_map = {m["id"]: m for m in metadata["milestones"]}
            if mid not in milestone_map:
                print(f"Error: Invalid milestone ID: {mid}", file=sys.stderr)
                return 1
            results = {
                mid: validate_current_milestone(
                    milestone_id=mid,
                    milestone_data=milestone_map[mid],
                    work_dir=work_dir,
                    paths=paths,
                    base_image=base_image,
                    language=args.language,
                    test_framework=args.test_framework,
                    validation_timeout=validation_timeout,
                    validation_retries=validation_retries,
                    rerun_attempt_number=args.rerun_validation_attempt,
                    allow_rerun_input_mismatch=args.allow_rerun_input_mismatch,
                )
            }
        else:
            results = validate_current_milestones(
                milestone_ids=milestone_ids,
                metadata=metadata,
                work_dir=work_dir,
                paths=paths,
                base_image=base_image,
                parallel=args.parallel,
                language=args.language,
                test_framework=args.test_framework,
                validation_timeout=validation_timeout,
                validation_retries=validation_retries,
            )
    else:
        results = build_milestones(
            milestone_ids=milestone_ids,
            metadata=metadata,
            work_dir=work_dir,
            paths=paths,
            base_image=base_image,
            parallel=args.parallel,
            agent=args.agent,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            timeout=args.timeout,
            agent_resume_attempts=args.agent_resume_attempts,
            max_retries=args.max_retries,
            language=args.language,
            test_framework=args.test_framework,
            validation_timeout=validation_timeout,
            validation_retries=validation_retries,
            dry_run=args.dry_run,
            skip_validation=args.skip_validation,
            allow_rebuild=args.allow_rebuild,
            regen_dockerfile=args.regen_dockerfile,
            extra_guidance_text=extra_guidance_text,
            extra_guidance_source=extra_guidance_source,
            resume_stuck_sessions=args.resume_stuck_sessions,
        )
    total_time = time.time() - start_time

    # Print summary
    ok_statuses = ("success", "validated", "module2_sufficient", "dry_run", "skipped")
    success_count = sum(1 for r in results.values() if r.status in ok_statuses)
    failed_count = len(results) - success_count

    print(f"\n{'=' * 60}")
    print(
        f"Build Summary  |  Total: {len(results)}  |  Success: {success_count}  |  Failed: {failed_count}  |  Time: {total_time:.1f}s"
    )
    print(f"{'=' * 60}")

    status_counts = {}
    for r in results.values():
        status_counts[r.status] = status_counts.get(r.status, 0) + 1
    for status, count in sorted(status_counts.items()):
        print(f"  {status}: {count}")

    failed = [r for r in results.values() if r.status not in ok_statuses]
    if failed:
        print("\nFailed milestones:")
        for r in failed:
            print(f"  - {r.milestone_id}: {r.error or r.status}")

    print("\nPer-milestone authority: dockerfiles/{MID}/build_status.json")
    print(f"{'=' * 60}")

    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
