#!/usr/bin/env python3
"""
Module 3: Main Agent — top-level Phase 3 orchestrator.

Inputs (what you give the Main Agent):

  * source git repo
  * repo config YAML
  * Phase 2 initial CSVs (milestones.csv + dependencies.csv)
  * Phase 2 trial name (used as the initial iter's name)

What the Main Agent does:

  * bootstrap an initial iter dir (``<phase2_trial>/``) under the harness workspace
  * ensure a base Docker image exists (invoke Base Env Build if missing)
  * loop up to ``--max-iterations`` times:
       1. Stage1 (module 1) — build testbed for current iter, with a LLM
          cherry-pick fix subloop (up to 5 attempts, edits CSVs via ops)
       2. Module 2 — build milestone Docker images + write build_status.json
       3. LLM Main Agent review — read build_status.json artifacts, emit decision_plan.json
          with branch_from + csv_ops (+ halt)
       5. Materialize next iter from the chosen parent + ops
  * track every iter in ``<phase2_trial>_iteration_tree.json`` with parents,
    health, and the exact ops applied
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from deepcommit.harness.prepare_images.build_milestone_image.build_milestone_image import (
    ClaudeAgentRunner,
    build_milestones,
    discover_milestones,
    infer_paths_from_work_dir,
)
from deepcommit.harness.prepare_images.compute_base_baseline import (
    ensure_base_baseline,
)

from .ensure_base_image import ensure_base_image
from .iteration_manager import (
    IterationTree,
    bootstrap_initial_iter,
    compute_csv_sha256,
    materialize_iteration,
)
from .testbed_fix_loop import run_stage1_with_fix_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [main_agent] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


DEFAULT_WORKSPACE_ROOT = Path("DATA/harness_workspace")


# =============================================================================
# Per-iteration processing
# =============================================================================


def _process_iteration(
    iter_dir: Path,
    iter_name: str,
    tree: IterationTree,
    config_path: Path,
    repo_path: Path,
    repo_range: str,
    *,
    model: str,
    stage1_fix_agent_timeout: int,
    testbed_subprocess_timeout: int,
    module2_parallel: int,
    module2_max_retries: int,
    module2_timeout: int,
    language: str,
    test_framework: str,
    llm_review_timeout: int,
    iter_number: int,
    max_iterations: int,
) -> Dict[str, Any]:
    """Run one full iter: stage1 → module2 build status → LLM review.

    Returns the decision_plan dict (or a fallback plan if LLM failed). The
    iteration tree is updated in place as each sub-step completes.
    """
    # ------------------------------------------------------------ #
    # Stage 1: Module 1 with cherry-pick fix subloop
    # ------------------------------------------------------------ #
    stage1 = run_stage1_with_fix_loop(
        iter_dir=iter_dir,
        config_path=config_path,
        repo_path=repo_path,
        repo_range=repo_range,
        testbed_agent_model=model,
        fix_agent_timeout_seconds=stage1_fix_agent_timeout,
        testbed_script_timeout_seconds=testbed_subprocess_timeout,
    )
    tree.update_iter(
        iter_name,
        stage1={
            "status": stage1.status,
            "attempts": stage1.attempts,
            "fix_log_entries": len(stage1.fix_log),
        },
        csv_sha256=compute_csv_sha256(iter_dir),
    )

    if stage1.status != "success":
        logger.error(
            f"[iter {iter_name}] Stage1 failed after {stage1.attempts} attempts; "
            f"skipping module 2, going straight to LLM review"
        )
        tree.update_iter(iter_name, status="failed_at_stage1")
        # Still give LLM a chance to produce a plan (e.g. halt or branch).
        plan = _llm_review(
            iter_dir=iter_dir,
            iter_name=iter_name,
            tree=tree,
            repo_range=repo_range,
            parent_name=(tree.get_iter(iter_name) or {}).get("parent"),
            stage1_status=stage1.status,
            stage1_attempts=stage1.attempts,
            build_results=None,
            build_statuses=None,
            iter_number=iter_number,
            max_iterations=max_iterations,
            model=model,
            timeout_seconds=llm_review_timeout,
        )
        return plan

    # ------------------------------------------------------------ #
    # Module 2: build / refine milestone Docker images
    # ------------------------------------------------------------ #
    try:
        paths = infer_paths_from_work_dir(iter_dir)
    except ValueError as e:
        logger.error(f"[iter {iter_name}] could not infer paths: {e}")
        tree.update_iter(iter_name, status="failed_at_module2_setup")
        return {"halt": True, "halt_reason": str(e), "next_iteration": {}}

    metadata_path = iter_dir / "metadata.json"
    if not metadata_path.exists():
        logger.error(f"[iter {iter_name}] metadata.json missing after stage1 success; something is wrong upstream")
        tree.update_iter(iter_name, status="failed_at_module2_setup")
        return {"halt": True, "halt_reason": "metadata.json missing", "next_iteration": {}}

    with open(metadata_path) as f:
        metadata = json.load(f)

    milestone_ids = discover_milestones(metadata)
    logger.info(f"[iter {iter_name}] Module 2: building {len(milestone_ids)} milestones (parallel={module2_parallel})")

    baseline_path = ensure_base_baseline(
        work_dir=iter_dir,
        base_image=paths["base_image"],
        language=language,
        test_framework=test_framework,
        test_dir=metadata.get("test_dir", "test/"),
    )
    logger.info(f"[iter {iter_name}] Base baseline ready: {baseline_path}")

    build_results = build_milestones(
        milestone_ids=milestone_ids,
        metadata=metadata,
        work_dir=iter_dir,
        paths=paths,
        base_image=paths["base_image"],
        parallel=module2_parallel,
        model=model,
        timeout=module2_timeout,
        max_retries=module2_max_retries,
        language=language,
        test_framework=test_framework,
    )

    build_statuses = _load_build_statuses(iter_dir, milestone_ids)
    module2_summary = {
        mid: {
            "runner_status": build_results[mid].status,
            "built": bool(build_statuses.get(mid, {}).get("built")),
            "failed_gates": build_statuses.get(mid, {}).get("failed_gates", []),
        }
        for mid in milestone_ids
    }
    tree.update_iter(iter_name, module2={"build_results": module2_summary})

    built = [mid for mid, status in build_statuses.items() if status.get("built")]
    health = {"healthy": len(built), "total": len(build_statuses), "basis": "build_status.built"}
    tree.update_iter(iter_name, health=health)
    logger.info(f"[iter {iter_name}] Build status: {health['healthy']}/{health['total']} built")

    # ------------------------------------------------------------ #
    # LLM review
    # ------------------------------------------------------------ #
    parent_name = (tree.get_iter(iter_name) or {}).get("parent")
    plan = _llm_review(
        iter_dir=iter_dir,
        iter_name=iter_name,
        tree=tree,
        repo_range=repo_range,
        parent_name=parent_name,
        stage1_status=stage1.status,
        stage1_attempts=stage1.attempts,
        build_results=build_results,
        build_statuses=build_statuses,
        iter_number=iter_number,
        max_iterations=max_iterations,
        model=model,
        timeout_seconds=llm_review_timeout,
    )

    status = "done" if health["healthy"] == health["total"] else "done_with_issues"
    tree.update_iter(iter_name, status=status, decision_plan=plan)
    tree.update_best()
    return plan


# =============================================================================
# LLM review helpers
# =============================================================================


def _load_build_statuses(iter_dir: Path, milestone_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load per-milestone build_status.json files as Module 2 authority."""
    statuses: Dict[str, Dict[str, Any]] = {}
    for mid in milestone_ids:
        path = iter_dir / "dockerfiles" / mid / "build_status.json"
        try:
            status = json.loads(path.read_text())
        except FileNotFoundError:
            status = {
                "schema_version": 1,
                "milestone_id": mid,
                "built": False,
                "failed_gates": ["build_status_missing"],
                "artifact_paths": {"latest_attempt": None},
                "artifact_error": str(path),
            }
        except (json.JSONDecodeError, OSError) as e:
            status = {
                "schema_version": 1,
                "milestone_id": mid,
                "built": False,
                "failed_gates": ["build_status_unreadable"],
                "artifact_paths": {"latest_attempt": None},
                "artifact_error": str(e),
            }
        statuses[mid] = (
            status
            if isinstance(status, dict)
            else {
                "schema_version": 1,
                "milestone_id": mid,
                "built": False,
                "failed_gates": ["build_status_invalid"],
                "artifact_paths": {"latest_attempt": None},
                "artifact_error": "build_status.json is not an object",
            }
        )
    return statuses


def _build_programmatic_summary(
    build_results: Optional[Dict[str, Any]],
    build_statuses: Optional[Dict[str, Dict[str, Any]]],
) -> Dict[str, Any]:
    """Compact structured input for the LLM prompt."""
    if build_statuses is None:
        return {"note": "stage1 failed; no module2 build_status artifacts this iter"}

    def _compact_status(status: Dict[str, Any]) -> Dict[str, Any]:
        failed_gates = status.get("failed_gates", [])
        compact = {
            "built": bool(status.get("built")),
            "failed_gates": failed_gates,
            "latest_attempt": (status.get("artifact_paths") or {}).get("latest_attempt"),
        }
        if "artifact_error" in status:
            compact["artifact_error"] = status["artifact_error"]
        return compact

    return {
        "build_results": {mid: r.status for mid, r in (build_results or {}).items()},
        "build_statuses": {mid: _compact_status(status) for mid, status in build_statuses.items()},
    }


def _render_main_loop_prompt(
    *,
    iter_dir: Path,
    iter_name: str,
    parent_name: Optional[str],
    tree: IterationTree,
    repo_range: str,
    phase2_trial: str,
    iter_number: int,
    max_iterations: int,
    stage1_status: str,
    stage1_attempts: int,
    programmatic_summary: Dict[str, Any],
) -> str:
    template_path = Path(__file__).parent / "prompts" / "main_loop.tmpl"
    template = template_path.read_text()

    best = tree.state.get("best")
    best_info = tree.get_iter(best) if best else None
    best_health = "unknown"
    if best_info and best_info.get("health"):
        h = best_info["health"]
        best_health = f"{h.get('healthy', '?')}/{h.get('total', '?')}"

    return (
        template.replace("{{iter_dir}}", str(iter_dir))
        .replace("{{iter_name}}", iter_name)
        .replace("{{parent_name}}", parent_name or "null")
        .replace("{{range_dir}}", str(iter_dir.parent))
        .replace("{{tree_path}}", str(tree.path))
        .replace("{{repo_range}}", repo_range)
        .replace("{{phase2_trial}}", phase2_trial)
        .replace("{{iter_number}}", str(iter_number))
        .replace("{{max_iterations}}", str(max_iterations))
        .replace("{{stage1_status}}", stage1_status)
        .replace("{{stage1_attempts}}", str(stage1_attempts))
        .replace("{{best_iter}}", best or "none")
        .replace("{{best_health}}", best_health)
        .replace(
            "{{programmatic_summary}}",
            json.dumps(programmatic_summary, indent=2, ensure_ascii=False),
        )
        .replace(
            "{{history_summary}}",
            json.dumps(tree.history_summary(), indent=2, ensure_ascii=False),
        )
    )


def _llm_review(
    *,
    iter_dir: Path,
    iter_name: str,
    tree: IterationTree,
    repo_range: str,
    parent_name: Optional[str],
    stage1_status: str,
    stage1_attempts: int,
    build_results: Optional[Dict[str, Any]],
    build_statuses: Optional[Dict[str, Dict[str, Any]]],
    iter_number: int,
    max_iterations: int,
    model: str,
    timeout_seconds: int,
) -> Dict[str, Any]:
    """Invoke the Main Agent LLM and parse its decision_plan.json.

    Returns a plan dict. On any LLM-side failure, returns a conservative
    halt plan so the outer loop can stop gracefully.
    """
    programmatic_summary = _build_programmatic_summary(build_results, build_statuses)
    prompt = _render_main_loop_prompt(
        iter_dir=iter_dir,
        iter_name=iter_name,
        parent_name=parent_name,
        tree=tree,
        repo_range=repo_range,
        phase2_trial=tree.state["phase2_trial"],
        iter_number=iter_number,
        max_iterations=max_iterations,
        stage1_status=stage1_status,
        stage1_attempts=stage1_attempts,
        programmatic_summary=programmatic_summary,
    )

    prompt_archive = iter_dir / "main_loop_prompt_rendered.md"
    prompt_archive.write_text(prompt)

    plan_path = iter_dir / "decision_plan.json"
    if plan_path.exists():
        plan_path.unlink()

    runner = ClaudeAgentRunner(
        workspace=iter_dir,
        log_dir=iter_dir / "_agent_logs",
        timeout_ms=timeout_seconds * 1000,
        model=model,
    )
    result = runner.run(prompt=prompt, task_name="main_agent_review")

    if not result.success:
        logger.error(f"[iter {iter_name}] LLM review failed: {result.error}")
        return _fallback_halt_plan(iter_name, f"LLM review failed: {result.error}")

    if not plan_path.exists():
        logger.error(f"[iter {iter_name}] LLM did not produce {plan_path}")
        return _fallback_halt_plan(iter_name, "LLM did not produce decision_plan.json")

    try:
        plan = json.loads(plan_path.read_text())
    except json.JSONDecodeError as e:
        logger.error(f"[iter {iter_name}] decision_plan.json is not valid JSON: {e}")
        return _fallback_halt_plan(iter_name, f"invalid JSON: {e}")

    if not isinstance(plan, dict):
        return _fallback_halt_plan(iter_name, "decision_plan is not an object")

    plan.setdefault("next_iteration", {})
    plan["next_iteration"].setdefault("branch_from", iter_name)
    plan["next_iteration"].setdefault("csv_ops", [])
    plan.setdefault("halt", False)
    return plan


def _fallback_halt_plan(iter_name: str, reason: str) -> Dict[str, Any]:
    return {
        "iteration": iter_name,
        "halt": True,
        "halt_reason": reason,
        "next_iteration": {"branch_from": iter_name, "csv_ops": []},
        "summary": f"Main agent halted: {reason}",
    }


# =============================================================================
# Outer loop
# =============================================================================


@dataclass
class RunReport:
    iterations_processed: List[str] = field(default_factory=list)
    final_best: Optional[str] = None
    exit_reason: str = ""
    tree_path: Optional[str] = None


def run_main_agent(
    *,
    repo_path: Path,
    config_path: Path,
    repo_range: str,
    phase2_trial: str,
    initial_milestones_csv: Path,
    initial_dependencies_csv: Path,
    workspace_root: Path = DEFAULT_WORKSPACE_ROOT,
    max_iterations: int = 3,
    model: str = "claude-opus-4-6",
    module2_parallel: int = 1,
    module2_max_retries: int = 3,
    module2_timeout: int = 1800,
    stage1_fix_agent_timeout: int = 600,
    testbed_subprocess_timeout: int = 3600,
    llm_review_timeout: int = 600,
    language: str = "python",
    test_framework: str = "pytest",
    skip_base_image: bool = False,
) -> RunReport:
    """Top-level Main Agent entry point. Returns a RunReport summary."""
    report = RunReport()

    # Step 0: base image.
    if not skip_base_image:
        ok = ensure_base_image(
            config_path=config_path,
            repo_path=repo_path,
            timeout_seconds=testbed_subprocess_timeout,
            model=model,
        )
        if not ok:
            report.exit_reason = "base image build failed"
            return report

    # Bootstrap initial iter.
    initial_iter_dir, tree = bootstrap_initial_iter(
        workspace_root=workspace_root,
        repo_range=repo_range,
        phase2_trial=phase2_trial,
        initial_milestones_csv=initial_milestones_csv,
        initial_dependencies_csv=initial_dependencies_csv,
    )
    report.tree_path = str(tree.path)

    current_iter_dir = initial_iter_dir
    current_iter_name = phase2_trial

    for iter_num in range(1, max_iterations + 1):
        logger.info("=" * 72)
        logger.info(f"Iteration {iter_num}/{max_iterations} — processing {current_iter_name}")
        logger.info("=" * 72)

        t0 = time.time()
        plan = _process_iteration(
            iter_dir=current_iter_dir,
            iter_name=current_iter_name,
            tree=tree,
            config_path=config_path,
            repo_path=repo_path,
            repo_range=repo_range,
            model=model,
            stage1_fix_agent_timeout=stage1_fix_agent_timeout,
            testbed_subprocess_timeout=testbed_subprocess_timeout,
            module2_parallel=module2_parallel,
            module2_max_retries=module2_max_retries,
            module2_timeout=module2_timeout,
            language=language,
            test_framework=test_framework,
            llm_review_timeout=llm_review_timeout,
            iter_number=iter_num,
            max_iterations=max_iterations,
        )
        report.iterations_processed.append(current_iter_name)
        logger.info(f"[iter {current_iter_name}] processed in {time.time() - t0:.1f}s")

        if plan.get("halt"):
            report.exit_reason = f"halt: {plan.get('halt_reason', 'no reason given')}"
            logger.info(f"Halting loop: {report.exit_reason}")
            break

        if iter_num == max_iterations:
            report.exit_reason = "max_iterations reached"
            break

        # Materialize next iter from plan.
        next_plan = plan.get("next_iteration", {}) or {}
        branch_from = next_plan.get("branch_from") or current_iter_name
        csv_ops = next_plan.get("csv_ops") or []

        parent_dir = current_iter_dir.parent / branch_from
        if not parent_dir.exists():
            report.exit_reason = f"branch_from {branch_from!r} doesn't exist"
            logger.error(report.exit_reason)
            break

        next_name = tree.next_iter_name()
        next_dir = current_iter_dir.parent / next_name
        logger.info(f"Materializing {next_name} from {branch_from} with {len(csv_ops)} op(s)")
        try:
            applied, rejected = materialize_iteration(parent_dir, next_dir, csv_ops)
        except Exception as e:
            report.exit_reason = f"materialize_iteration failed: {e}"
            logger.error(report.exit_reason)
            break

        tree.register_iter(
            next_name,
            parent=branch_from,
            applied_changes=applied,
            rejected_changes=rejected,
        )
        tree.update_iter(next_name, csv_sha256=compute_csv_sha256(next_dir))

        current_iter_dir = next_dir
        current_iter_name = next_name

    report.final_best = tree.state.get("best")
    return report


# =============================================================================
# CLI
# =============================================================================


def _resolve_phase2_csvs(
    explicit_milestones: Optional[Path],
    explicit_deps: Optional[Path],
    phase2_trial: str,
) -> tuple[Path, Path]:
    """If explicit paths aren't given, look under DATA/result/<phase2_trial>/."""
    if explicit_milestones and explicit_deps:
        return explicit_milestones, explicit_deps

    default_dir = Path("DATA/result") / phase2_trial
    candidates_m = [
        explicit_milestones,
        default_dir / "milestones.csv",
    ]
    candidates_d = [
        explicit_deps,
        default_dir / "dependencies.csv",
    ]
    m = next((p for p in candidates_m if p and p.exists()), None)
    d = next((p for p in candidates_d if p and p.exists()), None)
    if not m:
        raise FileNotFoundError(
            f"Could not find milestones.csv; pass --milestones-csv or place it at "
            f"DATA/result/{phase2_trial}/milestones.csv"
        )
    if not d:
        raise FileNotFoundError(
            f"Could not find dependencies.csv; pass --dependencies-csv or place it at "
            f"DATA/result/{phase2_trial}/dependencies.csv"
        )
    return m, d


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Module 3: Main Agent — Phase 3 top-level orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Core inputs.
    parser.add_argument(
        "--repo-path", type=Path, required=True, help="Source git repository, e.g. DATA/github_data/repos/django_django"
    )
    parser.add_argument(
        "--config", type=Path, required=True, help="Repo config YAML, e.g. config/django_django_4.2_5.2.yaml"
    )
    parser.add_argument(
        "--phase2-trial",
        type=str,
        required=True,
        help="Phase 2 trial name; also used as the initial iter's directory name (e.g. v1_001_fix)",
    )
    parser.add_argument(
        "--repo-range", type=str, required=True, help="Harness workspace subdir, e.g. django_django_4.2_5.2"
    )

    # Phase 2 CSVs (optional; resolved from phase2_trial if omitted).
    parser.add_argument(
        "--milestones-csv",
        type=Path,
        default=None,
        help="Path to Phase 2 milestones.csv (default: DATA/result/<phase2_trial>/milestones.csv)",
    )
    parser.add_argument(
        "--dependencies-csv",
        type=Path,
        default=None,
        help="Path to Phase 2 dependencies.csv (default: DATA/result/<phase2_trial>/dependencies.csv)",
    )

    # Workspace.
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=DEFAULT_WORKSPACE_ROOT,
        help=f"Harness workspace root (default: {DEFAULT_WORKSPACE_ROOT})",
    )

    # Loop control.
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument(
        "--skip-base-image", action="store_true", help="Don't run Base Env Build even if the base image is missing"
    )

    # Module 2 passthrough.
    parser.add_argument("--parallel", "-p", type=int, default=1)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument(
        "--language", type=str, default="python", choices=["python", "java", "go", "rust", "javascript"]
    )
    parser.add_argument("--test-framework", type=str, default="pytest")
    parser.add_argument("--module2-timeout", type=int, default=1800)

    # Agent control.
    parser.add_argument("--model", type=str, default="claude-opus-4-6")
    parser.add_argument("--stage1-fix-timeout", type=int, default=600)
    parser.add_argument("--testbed-subprocess-timeout", type=int, default=3600)
    parser.add_argument("--llm-review-timeout", type=int, default=600)

    args = parser.parse_args()

    try:
        milestones_csv, deps_csv = _resolve_phase2_csvs(
            args.milestones_csv,
            args.dependencies_csv,
            args.phase2_trial,
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not args.repo_path.exists():
        print(f"error: --repo-path does not exist: {args.repo_path}", file=sys.stderr)
        return 2
    if not args.config.exists():
        print(f"error: --config does not exist: {args.config}", file=sys.stderr)
        return 2

    report = run_main_agent(
        repo_path=args.repo_path,
        config_path=args.config,
        repo_range=args.repo_range,
        phase2_trial=args.phase2_trial,
        initial_milestones_csv=milestones_csv,
        initial_dependencies_csv=deps_csv,
        workspace_root=args.workspace_root,
        max_iterations=args.max_iterations,
        model=args.model,
        module2_parallel=args.parallel,
        module2_max_retries=args.max_retries,
        module2_timeout=args.module2_timeout,
        stage1_fix_agent_timeout=args.stage1_fix_timeout,
        testbed_subprocess_timeout=args.testbed_subprocess_timeout,
        llm_review_timeout=args.llm_review_timeout,
        language=args.language,
        test_framework=args.test_framework,
        skip_base_image=args.skip_base_image,
    )

    logger.info("=" * 72)
    logger.info("Run complete")
    logger.info(f"  iterations processed: {len(report.iterations_processed)}")
    logger.info(f"  final best iter:      {report.final_best or '(none)'}")
    logger.info(f"  exit reason:          {report.exit_reason}")
    logger.info(f"  tree:                 {report.tree_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
