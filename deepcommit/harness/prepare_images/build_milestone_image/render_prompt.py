#!/usr/bin/env python3
"""Render milestone environment setup prompts.

Milestone prompts are rendered from a single Jinja2 entrypoint under
``build_milestone_image/prompts``. The renderer only knows about milestone
prompt assets in this package; base-image prompt rendering remains separate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

PROMPTS_DIR = Path(__file__).parent / "prompts"
DEFAULT_TEMPLATE = PROMPTS_DIR / "base.md.j2"
SUPPORTED_LANGUAGES = ("python", "java", "rust", "go", "javascript")
DEFAULT_FRAMEWORK_BY_LANGUAGE = {
    "python": "pytest",
    "javascript": "jest",
    "java": "maven",
    "rust": "cargo",
    "go": "go_test",
}


def _extract_repo_family(repo_id: str) -> Optional[str]:
    """Strip version suffix from repo_id to get the owner_repo family."""
    parts = repo_id.split("_")
    repo_parts = []
    for part in parts:
        if re.match(r"^v?\d+(\.|$)", part):
            break
        repo_parts.append(part)
    return "_".join(repo_parts) if repo_parts else None


def _infer_repo_context(work_dir: Optional[Path], repo_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Infer ``repo_id`` and ``test_scenario`` from a harness work_dir."""
    inferred_repo_id = repo_id
    test_scenario = None

    if work_dir is not None:
        parts = Path(work_dir).resolve().parts
        try:
            idx = parts.index("harness_workspace")
            inferred_repo_id = inferred_repo_id or parts[idx + 1]
            test_scenario = parts[idx + 2]
        except (ValueError, IndexError):
            pass

    return inferred_repo_id, test_scenario


def _repo_pack_candidates(repo_id: Optional[str], test_scenario: Optional[str]) -> list[str]:
    """Return repo-pack lookup keys in descending priority."""
    if not repo_id:
        return []

    candidates = []
    if test_scenario:
        candidates.append(f"{repo_id}__{test_scenario}")
    candidates.append(repo_id)

    family = _extract_repo_family(repo_id)
    if family and family not in candidates:
        candidates.append(family)
    return candidates


def get_relative_path(path: Path) -> str:
    """Return an absolute path string suitable for prompt interpolation."""
    return str(Path(path).absolute())


def _make_environment(template_path: Optional[Path] = None) -> Environment:
    """Create a Jinja2 environment for milestone prompt assets."""
    search_paths = []
    if template_path is not None:
        search_paths.append(str(Path(template_path).parent))
    search_paths.append(str(PROMPTS_DIR))

    deduped = list(dict.fromkeys(search_paths))
    return Environment(
        loader=FileSystemLoader(deduped),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def _render_template_file(template_path: Path, context: dict[str, Any]) -> str:
    env = _make_environment(template_path)
    try:
        template = env.get_template(Path(template_path).name)
    except TemplateNotFound:
        template = env.from_string(Path(template_path).read_text())
    return template.render(**context)


def _render_repo_pack(
    repo_id: Optional[str],
    test_scenario: Optional[str],
    template_path: Path,
    context: dict[str, Any],
) -> tuple[str, Optional[str]]:
    """Render the highest-priority repo pack from prompts/repos/."""
    env = _make_environment(template_path)

    for key in _repo_pack_candidates(repo_id, test_scenario):
        repo_pack_name = f"repos/{key}.md.j2"
        try:
            return env.get_template(repo_pack_name).render(**context), repo_pack_name
        except TemplateNotFound:
            pass

    return "", None


def _base_context(
    *,
    milestone_id: str,
    milestone_commits: list[str],
    base_state: str,
    end_state: str,
    repo_path: Path,
    repo_src_dirs: list[str],
    test_dir: str,
    base_image: str,
    base_dockerfile_path: Path,
    commit_changed_tests_dir: Path,
    test_runner_script: str,
    test_results_dir: Path,
    output_dir: Path,
    work_dir: Optional[Path],
    repo_id: Optional[str],
    language: str,
    test_framework: Optional[str],
) -> dict[str, Any]:
    work_dir_value = get_relative_path(work_dir) if work_dir else get_relative_path(output_dir.parent)
    repo_id_value, test_scenario = _infer_repo_context(work_dir, repo_id)
    framework = test_framework or DEFAULT_FRAMEWORK_BY_LANGUAGE.get(language, "pytest")
    milestone_output_dir = output_dir / milestone_id

    if work_dir is None:
        raise ValueError("render_env_setup_prompt requires work_dir to locate the scenario base baseline.")
    base_baseline_file = Path(work_dir).parent / "base" / "baseline_metrics.json"
    if not base_baseline_file.is_file():
        raise FileNotFoundError(
            f"Base baseline missing: {base_baseline_file}. "
            "Run Phase 1 preprocess (compute_base_baseline) before rendering milestone prompts."
        )
    try:
        baseline_data = json.loads(base_baseline_file.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Base baseline is not valid JSON ({base_baseline_file}): {exc}") from exc
    base_collected_total = baseline_data.get("collection", {}).get("total_collected")
    if not isinstance(base_collected_total, int):
        raise ValueError(
            f"Base baseline at {base_baseline_file} is missing an integer `collection.total_collected` field."
        )

    return {
        "m_id": milestone_id,
        "milestone_id": milestone_id,
        "milestone_commits": milestone_commits,
        "milestone_commits_json": json.dumps(milestone_commits),
        "base_state": base_state,
        "end_state": end_state,
        "repo_path": get_relative_path(repo_path),
        "repo_src_dirs": repo_src_dirs,
        "repo_src_dirs_json": json.dumps(repo_src_dirs),
        "test_dir": test_dir,
        "base_image": base_image,
        "base_dockerfile_path": get_relative_path(base_dockerfile_path),
        "commit_changed_tests_dir": get_relative_path(commit_changed_tests_dir),
        "milestone_test_changes_file": get_relative_path(
            commit_changed_tests_dir / f"{milestone_id}_test_changes.json"
        ),
        "test_runner_script": test_runner_script,
        "test_results_dir": get_relative_path(test_results_dir),
        "output_dir": get_relative_path(milestone_output_dir),
        "work_dir": work_dir_value,
        "repo_id": repo_id_value or "",
        "test_scenario": test_scenario or "",
        "language": language,
        "test_framework": framework,
        "build_status_path": f"dockerfiles/{milestone_id}/build_status.json",
        "module2_waivers_path": f"dockerfiles/{milestone_id}/module2_waivers.json",
        "module2_signals_path": f"dockerfiles/{milestone_id}/module2_signals.json",
        "patched_tests_path": f"milestone_patched_tests/{milestone_id}.json",
        "base_baseline_path": get_relative_path(base_baseline_file),
        "base_collected_total": base_collected_total,
    }


def render_env_setup_prompt(
    milestone_id: str,
    milestone_commits: list[str],
    base_state: str,
    end_state: str,
    repo_path: Path,
    repo_src_dirs: list[str],
    test_dir: str,
    base_image: str,
    base_dockerfile_path: Path,
    commit_changed_tests_dir: Path,
    test_runner_script: str,
    test_results_dir: Path,
    output_dir: Path,
    template_path: Optional[Path] = None,
    work_dir: Optional[Path] = None,
    repo_id: Optional[str] = None,
    language: str = "python",
    test_framework: Optional[str] = None,
) -> int:
    """Render an environment setup prompt for a milestone."""
    language = language or "python"
    if language not in SUPPORTED_LANGUAGES:
        print(f"Error: Unsupported language: {language}")
        return 1

    template_path = template_path or DEFAULT_TEMPLATE
    if not template_path.exists():
        print(f"Error: Template not found: {template_path}")
        return 1

    milestone_output_dir = output_dir / milestone_id
    milestone_output_dir.mkdir(parents=True, exist_ok=True)

    context = _base_context(
        milestone_id=milestone_id,
        milestone_commits=milestone_commits,
        base_state=base_state,
        end_state=end_state,
        repo_path=repo_path,
        repo_src_dirs=repo_src_dirs,
        test_dir=test_dir,
        base_image=base_image,
        base_dockerfile_path=base_dockerfile_path,
        commit_changed_tests_dir=commit_changed_tests_dir,
        test_runner_script=test_runner_script,
        test_results_dir=test_results_dir,
        output_dir=output_dir,
        work_dir=work_dir,
        repo_id=repo_id,
        language=language,
        test_framework=test_framework,
    )

    repo_pack_content, repo_pack_name = _render_repo_pack(
        context["repo_id"] or None,
        context["test_scenario"] or None,
        template_path,
        context,
    )
    context["repo_pack_content"] = repo_pack_content
    context["repo_pack_name"] = repo_pack_name or ""

    try:
        prompt = _render_template_file(template_path, context)
    except Exception as e:  # Jinja errors include useful type-specific messages.
        print(f"Error: Failed to render template {template_path}: {e}")
        return 1

    output_file = milestone_output_dir / "env_setup_prompt.md"
    output_file.write_text(prompt)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render milestone environment setup prompts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m deepcommit.harness.prepare_images.build_milestone_image.render_prompt M001 \\
    --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/baseline_v0

  python -m deepcommit.harness.prepare_images.build_milestone_image.render_prompt --all \\
    --work-dir DATA/harness_workspace/java_project/baseline_v0 \\
    --language java --test-framework maven
        """,
    )

    parser.add_argument("milestone_ids", nargs="*", help="Milestone IDs to render")
    parser.add_argument("--all", action="store_true", help="Render prompts for all milestones in metadata.json")
    parser.add_argument(
        "--work-dir",
        type=Path,
        required=True,
        help="Working directory (e.g., DATA/harness_workspace/repo_id/scenario)",
    )
    parser.add_argument("--metadata", type=Path, default=None, help="Path to metadata.json")
    parser.add_argument("--base-image", type=str, default=None, help="Base Docker image name")
    parser.add_argument("--base-dockerfile-path", type=Path, default=None, help="Path to base Dockerfile")
    parser.add_argument(
        "--commit-changed-tests-dir",
        type=Path,
        default=None,
        help="Directory containing per-commit test changes",
    )
    parser.add_argument(
        "--test-runner-script",
        type=str,
        default="deepcommit.harness.test_runner.run_milestone_tests",
        help="Script/module used to run milestone tests",
    )
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory for prompts")
    parser.add_argument("--test-results-dir", type=Path, default=None, help="Directory to save test results")
    parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help="Advanced override for the single Jinja prompt entrypoint",
    )
    parser.add_argument(
        "--language",
        type=str,
        default="python",
        choices=SUPPORTED_LANGUAGES,
        help="Project language (default: python)",
    )
    parser.add_argument(
        "--test-framework",
        type=str,
        default=None,
        help="Test framework (default: language-specific)",
    )

    args = parser.parse_args()

    if not args.all and not args.milestone_ids:
        parser.error("Either provide milestone IDs or use --all")

    work_dir = args.work_dir.resolve()
    if not work_dir.exists():
        print(f"Error: Work directory not found: {work_dir}")
        return 1

    metadata_path = args.metadata if args.metadata else work_dir / "metadata.json"
    if not metadata_path.exists():
        print(f"Error: Metadata file not found: {metadata_path}")
        return 1

    repo_id, _test_scenario = _infer_repo_context(work_dir, None)
    if args.base_image is None:
        if not repo_id:
            print(f"Error: Cannot infer base_image from work_dir: {work_dir}")
            print("Please specify --base-image explicitly")
            return 1
        args.base_image = f"{repo_id}/base:latest".lower()

    template_path = args.template or DEFAULT_TEMPLATE
    if not template_path.exists():
        print(f"Error: Template file not found: {template_path}")
        return 1

    with open(metadata_path) as f:
        metadata = json.load(f)

    milestone_map = {m["id"]: m for m in metadata["milestones"]}
    if args.all:
        milestone_ids = [m["id"] for m in metadata["milestones"]]
    else:
        milestone_ids = args.milestone_ids
        for mid in milestone_ids:
            if mid not in milestone_map:
                print(f"Error: Milestone '{mid}' not found in metadata")
                print(f"Available milestones: {list(milestone_map.keys())}")
                return 1

    repo_path = work_dir / "testbed"
    repo_src_dirs = metadata.get("repo_src_dirs", [])
    test_dir = metadata.get("test_dir", "test")
    output_dir = args.output_dir if args.output_dir else work_dir / "dockerfiles"
    test_results_dir = args.test_results_dir if args.test_results_dir else work_dir / "test_results"
    base_dockerfile_path = (
        args.base_dockerfile_path
        if args.base_dockerfile_path
        else work_dir.parent / "base" / "dockerfiles" / "Dockerfile"
    )
    commit_changed_tests_dir = (
        args.commit_changed_tests_dir if args.commit_changed_tests_dir else work_dir / "commit_level" / "patched_tests"
    )

    print(f"\n{'=' * 60}")
    print("Rendering Milestone Environment Setup Prompts")
    print(f"{'=' * 60}")
    print(f"Work directory: {work_dir}")
    print(f"Metadata: {metadata_path}")
    print(f"Language: {args.language}")
    print(f"Test framework: {args.test_framework or DEFAULT_FRAMEWORK_BY_LANGUAGE.get(args.language, 'pytest')}")
    print(f"Template: {template_path}")
    print(f"Repository: {repo_path}")
    print(f"Base image: {args.base_image}")
    print(f"Output dir: {output_dir}")
    print(f"Milestones: {len(milestone_ids)}")
    print(f"{'=' * 60}\n")

    success_count = 0
    for i, mid in enumerate(milestone_ids, 1):
        milestone = milestone_map[mid]
        base_state = milestone.get("tag_name_start", f"milestone-{mid}-start")
        end_state = milestone.get("tag_name_end", f"milestone-{mid}-end")
        commits_raw = milestone.get("commits", "")
        milestone_commits = (
            [c.strip() for c in commits_raw.split(";") if c.strip()] if isinstance(commits_raw, str) else commits_raw
        )

        print(f"[{i}/{len(milestone_ids)}] {mid}")
        print(f"    Title: {milestone.get('title', 'N/A')}")
        print(f"    Start: {base_state}")
        print(f"    End: {end_state}")
        print(f"    Commits: {len(milestone_commits)}")

        result = render_env_setup_prompt(
            milestone_id=mid,
            milestone_commits=milestone_commits,
            base_state=base_state,
            end_state=end_state,
            repo_path=repo_path,
            repo_src_dirs=repo_src_dirs,
            test_dir=test_dir,
            base_image=args.base_image,
            base_dockerfile_path=base_dockerfile_path,
            commit_changed_tests_dir=commit_changed_tests_dir,
            test_runner_script=args.test_runner_script,
            test_results_dir=test_results_dir,
            output_dir=output_dir,
            template_path=template_path,
            work_dir=work_dir,
            repo_id=repo_id,
            language=args.language,
            test_framework=args.test_framework,
        )

        if result == 0:
            print(f"    -> {output_dir / mid / 'env_setup_prompt.md'}")
            success_count += 1
        else:
            print("    -> FAILED")
        print()

    print(f"{'=' * 60}")
    print(f"Summary: {success_count}/{len(milestone_ids)} prompts rendered")
    print(f"Output directory: {output_dir}")
    print(f"{'=' * 60}\n")
    return 0 if success_count == len(milestone_ids) else 1


def _maybe_read_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _context_attempt_name(latest_attempt_dir: Optional[Path]) -> str:
    return Path(latest_attempt_dir).name if latest_attempt_dir else "attempt_1"


def _attempt_number(path: Path) -> int:
    try:
        return int(path.name.split("_", 1)[1])
    except (IndexError, ValueError):
        return -1


def _latest_attempt_path(results_dir: Path) -> Optional[Path]:
    attempts = [p for p in (results_dir / "refinement").glob("attempt_*") if p.is_dir()]
    if not attempts:
        return None
    return max(attempts, key=_attempt_number)


def _attempt_path_from_status(results_dir: Path, status_attempt: Optional[str]) -> Optional[Path]:
    if not status_attempt:
        return None
    refinement_path = results_dir / "refinement" / status_attempt
    if refinement_path.exists():
        return refinement_path
    return refinement_path


def _build_runtime_context(
    work_dir: Optional[Path],
    milestone_id: Optional[str],
    latest_attempt_dir: Optional[Path],
    dockerfiles_dir: Optional[Path],
) -> dict[str, Any]:
    if not (work_dir and milestone_id):
        attempt_dir = Path(latest_attempt_dir) if latest_attempt_dir else None
        return {
            "has_work_context": False,
            "milestone_id": milestone_id or "",
            "build_status_path": "dockerfiles/<milestone_id>/build_status.json",
            "latest_attempt": attempt_dir.name if attempt_dir else "",
            "latest_attempt_dir": str(attempt_dir) if attempt_dir else "",
        }

    work_dir = Path(work_dir)
    docker_dir = Path(dockerfiles_dir) / milestone_id if dockerfiles_dir else work_dir / "dockerfiles" / milestone_id
    results_dir = work_dir / "test_results" / milestone_id
    build_status_path = docker_dir / "build_status.json"
    build_status = _maybe_read_json(build_status_path)
    status_attempt = build_status.get("artifact_paths", {}).get("latest_attempt") if build_status else None

    if latest_attempt_dir:
        attempt_dir = Path(latest_attempt_dir)
        latest_attempt = attempt_dir.name
    elif status_attempt:
        latest_attempt = status_attempt
        attempt_dir = _attempt_path_from_status(results_dir, status_attempt)
    else:
        attempt_dir = _latest_attempt_path(results_dir) or results_dir / "refinement" / "attempt_1"
        latest_attempt = attempt_dir.name

    return {
        "has_work_context": True,
        "milestone_id": milestone_id,
        "work_dir": get_relative_path(work_dir),
        "docker_dir": get_relative_path(docker_dir),
        "build_status_path": get_relative_path(build_status_path),
        "build_status_exists": build_status is not None,
        "built": build_status.get("built") if build_status else None,
        "failed_gates": build_status.get("failed_gates", []) if build_status else [],
        "latest_attempt": latest_attempt,
        "latest_attempt_dir": get_relative_path(attempt_dir),
        "patched_tests_path": get_relative_path(work_dir / "milestone_patched_tests" / f"{milestone_id}.json"),
        "module2_waivers_path": get_relative_path(docker_dir / "module2_waivers.json"),
        "module2_signals_path": get_relative_path(docker_dir / "module2_signals.json"),
        "runner_diagnostic_path": get_relative_path(results_dir / "refinement" / "runner_diagnostic.json"),
    }


def build_retry_prompt(
    original_prompt: str,
    error_message: str,
    latest_attempt_dir: Optional[Path] = None,
    save_to: Optional[Path] = None,
    work_dir: Optional[Path] = None,
    milestone_id: Optional[str] = None,
    dockerfiles_dir: Optional[Path] = None,
) -> str:
    """Build a retry prompt by prepending previous-attempt context."""
    context = _build_runtime_context(work_dir, milestone_id, latest_attempt_dir, dockerfiles_dir)
    context["error_message"] = error_message[:2000]

    env = _make_environment(DEFAULT_TEMPLATE)
    retry_context = env.get_template("retry_context.md.j2").render(**context)

    if save_to is not None:
        save_to = Path(save_to)
        save_to.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_to.write_text(f"<!-- generated at {ts} UTC -->\n{retry_context}")

    return retry_context + original_prompt


def build_refine_prompt(
    original_prompt: str,
    dockerfiles_dir: str,
    diagnosis_text: str,
    operator_directive: Optional[str] = None,
    operator_directive_source: Optional[str] = None,
) -> str:
    """Build a refine prompt by prepending existing-artifact context.

    ``operator_directive`` is an optional high-level instruction from the
    orchestrator/operator (e.g. Module 3) that becomes the primary objective
    for this refine run. It is rendered above the pipeline diagnostic so the
    agent sees it first. Both ``operator_directive`` and
    ``operator_directive_source`` are surfaced to the agent verbatim — keep
    the source label short and human-readable (a file path is typical).
    """
    docker_dir = Path(dockerfiles_dir)
    build_status_path = docker_dir / "build_status.json"
    build_status = _maybe_read_json(build_status_path)
    work_dir = docker_dir.parent.parent if docker_dir.parent.name == "dockerfiles" else None
    results_dir = work_dir / "test_results" / docker_dir.name if work_dir else None
    status_attempt = build_status.get("artifact_paths", {}).get("latest_attempt") if build_status else None

    latest_attempt_dir = None
    latest_attempt_source = ""
    if results_dir and status_attempt:
        latest_attempt_dir = _attempt_path_from_status(results_dir, status_attempt)
        latest_attempt_source = "build_status.json artifact_paths.latest_attempt"
    elif results_dir:
        latest_attempt_dir = _latest_attempt_path(results_dir)
        if latest_attempt_dir:
            latest_attempt_source = "latest attempt directory fallback"

    directive_text = operator_directive.strip() if operator_directive else ""
    context = {
        "docker_dir": get_relative_path(docker_dir),
        "build_status_path": get_relative_path(build_status_path),
        "build_status_exists": build_status is not None,
        "built": build_status.get("built") if build_status else None,
        "failed_gates": build_status.get("failed_gates", []) if build_status else [],
        "latest_attempt": latest_attempt_dir.name if latest_attempt_dir else "",
        "latest_attempt_dir": get_relative_path(latest_attempt_dir) if latest_attempt_dir else "",
        "latest_attempt_source": latest_attempt_source,
        "diagnosis_text": diagnosis_text.strip(),
        "operator_directive": directive_text,
        "operator_directive_source": (operator_directive_source or "").strip(),
    }
    env = _make_environment(DEFAULT_TEMPLATE)
    refine_context = env.get_template("refine_context.md.j2").render(**context)
    return refine_context + original_prompt


if __name__ == "__main__":
    sys.exit(main())
