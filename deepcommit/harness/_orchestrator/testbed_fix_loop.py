"""Testbed build with LLM cherry-pick fix subloop (Module 1 wrapper).

The underlying ``build_milestone_testbed.py`` is a "dumb" cherry-pick
tool: on failure it exits 1 with no DAG-repair intelligence. This module
supplies the agent-driven policy that lives above it — invoke the
testbed script, and on failure run a sub-agent LLM that reads the stderr
and the current CSVs, emits a CSV ops list, the orchestrator applies
those ops, and we retry with ``--incremental`` up to ``MAX_ATTEMPTS``
times.

The fix ops produced across all attempts are captured in
``{iter_dir}/stage1_fix_log.json`` so Main Agent (and humans) can see
exactly what stage1 had to do to succeed.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from deepcommit.harness.prepare_images.build_milestone_image.build_milestone_image import (
    ClaudeAgentRunner,
)

from .csv_ops import apply_csv_ops

logger = logging.getLogger(__name__)


MAX_ATTEMPTS = 5
STDERR_TAIL_CHARS = 8000  # how much stderr to inline into the prompt


@dataclass
class Stage1Result:
    status: str  # "success" | "failed_at_stage1"
    attempts: int = 0
    fix_log: List[Dict[str, Any]] = field(default_factory=list)
    last_error: Optional[str] = None


# =============================================================================
# Top-level entry point
# =============================================================================


def run_stage1_with_fix_loop(
    iter_dir: Path,
    config_path: Path,
    repo_path: Path,
    repo_range: str,
    *,
    testbed_agent_model: str = "claude-opus-4-6",
    fix_agent_timeout_seconds: int = 600,
    testbed_script_timeout_seconds: int = 3600,
) -> Stage1Result:
    """Build the testbed for ``iter_dir``, with LLM cherry-pick fixes on failure.

    After success, ``iter_dir`` will contain:
      - ``testbed/`` (git repo with milestone tags)
      - ``metadata.json``
      - ``stage1_fix_log.json`` (possibly empty — one entry per failed attempt)

    On exhausted retries, returns ``Stage1Result(status="failed_at_stage1")``
    and leaves whatever partial state exists in place.
    """
    fix_log: List[Dict[str, Any]] = []
    last_error: Optional[str] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        incremental = attempt > 1
        logger.info(f"[stage1] {iter_dir.name} attempt {attempt}/{MAX_ATTEMPTS} (incremental={incremental})")

        rc, stderr_tail, stderr_log_path = _run_testbed_subprocess(
            iter_dir=iter_dir,
            config_path=config_path,
            repo_path=repo_path,
            incremental=incremental,
            timeout_seconds=testbed_script_timeout_seconds,
        )

        if rc == 0:
            logger.info(f"[stage1] {iter_dir.name} succeeded on attempt {attempt}")
            _write_fix_log(iter_dir, fix_log)
            return Stage1Result(
                status="success",
                attempts=attempt,
                fix_log=fix_log,
            )

        last_error = stderr_tail
        logger.warning(f"[stage1] {iter_dir.name} attempt {attempt} failed (rc={rc}); invoking fix sub-agent")

        if attempt == MAX_ATTEMPTS:
            # Out of retries — record the last failure and bail.
            fix_log.append(
                {
                    "attempt": attempt,
                    "testbed_rc": rc,
                    "stderr_tail": stderr_tail,
                    "fix_outcome": "not_attempted_max_reached",
                }
            )
            break

        # Invoke fix sub-agent.
        proposal = _invoke_fix_agent(
            iter_dir=iter_dir,
            repo_path=repo_path,
            attempt=attempt,
            stderr_tail=stderr_tail,
            stderr_log_path=stderr_log_path,
            prior_fix_log=fix_log,
            model=testbed_agent_model,
            timeout_seconds=fix_agent_timeout_seconds,
        )

        attempt_record: Dict[str, Any] = {
            "attempt": attempt,
            "testbed_rc": rc,
            "stderr_tail": stderr_tail,
        }

        if proposal is None:
            attempt_record["fix_outcome"] = "agent_failed"
            fix_log.append(attempt_record)
            logger.error(f"[stage1] {iter_dir.name} fix agent failed; aborting")
            break

        proposed_ops = proposal.get("ops", []) or []
        attempt_record["proposed_ops"] = proposed_ops
        attempt_record["notes"] = proposal.get("notes", "")

        if not proposed_ops:
            attempt_record["fix_outcome"] = "agent_gave_up"
            fix_log.append(attempt_record)
            logger.error(f"[stage1] {iter_dir.name} fix agent proposed no ops (notes: {proposal.get('notes', '')})")
            break

        applied, rejected = apply_csv_ops(iter_dir, proposed_ops)
        attempt_record["applied_ops"] = applied
        attempt_record["rejected_ops"] = rejected
        attempt_record["fix_outcome"] = "ops_applied" if applied else "ops_all_rejected"
        fix_log.append(attempt_record)

        if not applied:
            logger.error(f"[stage1] {iter_dir.name} all {len(proposed_ops)} proposed ops rejected; aborting")
            break

    _write_fix_log(iter_dir, fix_log)
    return Stage1Result(
        status="failed_at_stage1",
        attempts=MAX_ATTEMPTS,
        fix_log=fix_log,
        last_error=last_error,
    )


# =============================================================================
# Testbed subprocess
# =============================================================================


def _run_testbed_subprocess(
    iter_dir: Path,
    config_path: Path,
    repo_path: Path,
    incremental: bool,
    timeout_seconds: int,
) -> tuple[int, str, Path]:
    """Run build_milestone_testbed.py once. Returns (rc, stderr_tail, log_path)."""
    log_dir = iter_dir / "stage1_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"attempt_{int(time.time())}.log"

    cmd = [
        sys.executable,
        "-m",
        "deepcommit.harness.prepare_repo.build_milestone_testbed",
        "--milestone-csv",
        str(iter_dir / "milestones.csv"),
        "--dependencies-csv",
        str(iter_dir / "dependencies.csv"),
        "--config",
        str(config_path),
        "--repo-path",
        str(repo_path),
        "--output-dir",
        str(iter_dir),
        "--verbose",
    ]
    if incremental:
        cmd.append("--incremental")

    logger.info(f"[stage1] exec: {' '.join(cmd)}")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        rc = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        rc = -1
        stdout = e.stdout.decode() if e.stdout else ""
        stderr = (e.stderr.decode() if e.stderr else "") + "\n[TIMEOUT]"

    log_path.write_text(
        f"==== CMD ====\n{' '.join(cmd)}\n\n"
        f"==== RC ====\n{rc}\n\n"
        f"==== STDOUT ====\n{stdout}\n\n"
        f"==== STDERR ====\n{stderr}\n"
    )

    tail = (stderr or stdout)[-STDERR_TAIL_CHARS:]
    return rc, tail, log_path


# =============================================================================
# Fix sub-agent
# =============================================================================


def _render_fix_prompt(
    iter_dir: Path,
    repo_path: Path,
    attempt: int,
    stderr_tail: str,
    stderr_log_path: Path,
    prior_attempt_count: int,
) -> str:
    template_path = Path(__file__).parent / "prompts" / "cherry_pick_fix.tmpl"
    template = template_path.read_text()
    return (
        template.replace("{{iter_dir}}", str(iter_dir))
        .replace("{{attempt}}", str(attempt))
        .replace("{{max_attempts}}", str(MAX_ATTEMPTS))
        .replace("{{prior_attempt_count}}", str(prior_attempt_count))
        .replace("{{stderr_tail}}", stderr_tail)
        .replace("{{stderr_log_path}}", str(stderr_log_path))
        .replace("{{testbed_path}}", str(iter_dir / "testbed"))
        .replace("{{repo_path}}", str(repo_path))
    )


def _invoke_fix_agent(
    iter_dir: Path,
    repo_path: Path,
    attempt: int,
    stderr_tail: str,
    stderr_log_path: Path,
    prior_fix_log: List[Dict[str, Any]],
    model: str,
    timeout_seconds: int,
) -> Optional[Dict[str, Any]]:
    """Run the cherry-pick fix LLM. Returns parsed proposal dict or None."""
    prompt = _render_fix_prompt(
        iter_dir=iter_dir,
        repo_path=repo_path,
        attempt=attempt,
        stderr_tail=stderr_tail,
        stderr_log_path=stderr_log_path,
        prior_attempt_count=len(prior_fix_log),
    )

    # Archive the rendered prompt for debugging.
    prompt_archive = iter_dir / f"stage1_fix_prompt_attempt{attempt}.md"
    prompt_archive.write_text(prompt)

    # Make sure stale proposal from a prior attempt doesn't leak.
    proposal_path = iter_dir / "stage1_fix_proposal.json"
    if proposal_path.exists():
        proposal_path.unlink()

    agent_logs_dir = iter_dir / "_agent_logs"
    runner = ClaudeAgentRunner(
        workspace=iter_dir,
        log_dir=agent_logs_dir,
        timeout_ms=timeout_seconds * 1000,
        model=model,
    )
    result = runner.run(prompt=prompt, task_name=f"stage1_fix_attempt{attempt}")

    if not result.success:
        logger.error(f"[stage1] fix agent subprocess failed: {result.error}")
        return None

    if not proposal_path.exists():
        logger.error(f"[stage1] fix agent did not produce {proposal_path}")
        return None

    try:
        proposal = json.loads(proposal_path.read_text())
    except json.JSONDecodeError as e:
        logger.error(f"[stage1] fix agent produced invalid JSON: {e}")
        return None

    if not isinstance(proposal, dict) or "ops" not in proposal:
        logger.error(f"[stage1] fix agent proposal missing 'ops' field: {proposal}")
        return None

    return proposal


# =============================================================================
# Fix log persistence
# =============================================================================


def _write_fix_log(iter_dir: Path, fix_log: List[Dict[str, Any]]) -> None:
    path = iter_dir / "stage1_fix_log.json"
    path.write_text(json.dumps({"attempts": fix_log}, indent=2, ensure_ascii=False))
    logger.info(f"[stage1] wrote {path} ({len(fix_log)} attempt record(s))")
