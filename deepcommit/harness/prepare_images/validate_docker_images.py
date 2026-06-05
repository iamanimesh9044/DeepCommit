#!/usr/bin/env python3
"""
Docker Image Validation Script

Validates that milestone Docker images are correctly built and configured.
Checks:
1. Default state is START
2. Repository has no uncommitted changes
3. START state can collect tests successfully (framework-specific collect command)
4. END state can collect tests successfully (framework-specific collect command)
5. START and END states have the same test count (only when a count is available)

Usage:
    python validate_docker_images.py --image IMAGE_NAME --milestone-id M001
    python validate_docker_images.py --batch --metadata METADATA_FILE
"""

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Default per-language/framework validation commands
try:
    from deepcommit.harness.test_runner.core.types import DEFAULT_VALIDATION_CONFIGS
except Exception:  # pragma: no cover
    DEFAULT_VALIDATION_CONFIGS = {}


@dataclass
class ValidationResult:
    """Result of a single validation check"""

    success: bool
    message: str
    details: Optional[Dict[str, Any]] = None
    duration: float = 0.0


@dataclass
class ImageValidationReport:
    """Complete validation report for a Docker image"""

    image_name: str
    milestone_id: str
    validation_time: str
    default_state_check: ValidationResult
    clean_working_tree_check: ValidationResult
    start_state_check: ValidationResult
    end_state_check: ValidationResult
    test_count_match_check: ValidationResult
    overall_success: bool
    total_duration: float


def run_docker_command(image_name: str, command: str, timeout: int = 120) -> tuple[bool, str, str]:
    """
    Run a docker command and return (success, stdout, stderr)

    Args:
        image_name: Docker image name
        command: Shell command to run inside container (executed via `sh -c`)
        timeout: Command timeout in seconds

    Returns:
        Tuple of (success, stdout, stderr)
    """
    try:
        # Use --init to properly reap zombie child processes
        result = subprocess.run(
            ["docker", "run", "--rm", "--init", image_name, "sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        success = result.returncode == 0
        return success, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", f"Command timed out after {timeout}s"
    except Exception as e:
        return False, "", str(e)


def infer_language_framework_and_test_dir(image_name: str, timeout: int = 30) -> Tuple[str, str, str]:
    """
    Best-effort inference of (language, test_framework, test_dir) from /testbed.

    This keeps milestone validation self-contained: build_images only knows the
    image name and milestone id, not the repo's language/framework.
    """
    detect_script = (
        "cd /testbed || exit 1; "
        "lang=unknown; fw=unknown; tdir='.'; "
        "if [ -f pom.xml ]; then lang=java; fw=maven; "
        "elif [ -f build.gradle ] || [ -f build.gradle.kts ]; then lang=java; fw=gradle; "
        "elif [ -f package.json ]; then lang=javascript; "
        "  if grep -Eqs '\"mocha\"|\\bmocha\\b' package.json; then fw=mocha; "
        "  else fw=jest; fi; "
        "elif [ -f go.mod ]; then lang=go; fw=go_test; "
        "elif [ -f Cargo.toml ]; then lang=rust; fw=cargo; "
        "elif [ -f pyproject.toml ] || [ -f setup.py ] || [ -f setup.cfg ] || [ -f requirements.txt ] || [ -f pytest.ini ]; then "
        "  lang=python; "
        "  if command -v pytest >/dev/null 2>&1; then fw=pytest; else fw=unittest; fi; "
        "fi; "
        'if [ "$lang" = python ]; then '
        "  if [ -d test ]; then tdir=test; "
        "  elif [ -d tests ]; then tdir=tests; "
        "  else tdir='.'; fi; "
        "fi; "
        'echo "$lang $fw $tdir"'
    )

    success, stdout, _stderr = run_docker_command(image_name, detect_script, timeout=timeout)
    if not success:
        return "unknown", "unknown", "."

    parts = stdout.strip().split()
    if len(parts) >= 2:
        language = parts[0]
        test_framework = parts[1]
        test_dir = parts[2] if len(parts) >= 3 else "."
        return language, test_framework, test_dir

    return "unknown", "unknown", "."


def get_validation_defaults(language: str, test_framework: str) -> Optional[Dict[str, Any]]:
    """Lookup DEFAULT_VALIDATION_CONFIGS for a language/framework pair."""
    if not DEFAULT_VALIDATION_CONFIGS:
        return None
    return DEFAULT_VALIDATION_CONFIGS.get((language, test_framework))


def validate_default_state(image_name: str, milestone_id: str, timeout: int = 10) -> ValidationResult:
    """
    Validate that the default state points to the same commit as START tag

    Args:
        image_name: Docker image name
        milestone_id: Milestone ID (e.g., M003)
        timeout: Command timeout in seconds

    Returns:
        ValidationResult with default state check details
    """
    start_time = time.time()

    expected_tag = f"milestone-{milestone_id}-start"

    # Get current HEAD commit
    success1, current_commit, stderr1 = run_docker_command(
        image_name, "cd /testbed && git rev-parse HEAD", timeout=timeout
    )

    # Get START tag commit
    success2, tag_commit, stderr2 = run_docker_command(
        image_name, f"cd /testbed && git rev-parse {expected_tag}", timeout=timeout
    )

    duration = time.time() - start_time

    if not success1:
        return ValidationResult(success=False, message=f"Failed to get current commit: {stderr1}", duration=duration)

    if not success2:
        return ValidationResult(
            success=False, message=f"Failed to get {expected_tag} commit: {stderr2}", duration=duration
        )

    current_commit = current_commit.strip()
    tag_commit = tag_commit.strip()

    if current_commit == tag_commit:
        return ValidationResult(
            success=True,
            message=f"Default state is at START (commit: {current_commit[:8]})",
            details={"current_commit": current_commit, "tag_commit": tag_commit, "tag": expected_tag},
            duration=duration,
        )
    else:
        return ValidationResult(
            success=False,
            message=f"Default state mismatch! Current: {current_commit[:8]}, Expected ({expected_tag}): {tag_commit[:8]}",
            details={"current_commit": current_commit, "tag_commit": tag_commit, "tag": expected_tag},
            duration=duration,
        )


def validate_clean_working_tree(image_name: str, timeout: int = 10) -> ValidationResult:
    """
    Validate that the repository has no uncommitted changes

    Args:
        image_name: Docker image name
        timeout: Command timeout in seconds

    Returns:
        ValidationResult with clean working tree check details
    """
    start_time = time.time()

    # Check git status to ensure no uncommitted changes
    success, stdout, stderr = run_docker_command(image_name, "cd /testbed && git status --porcelain", timeout=timeout)

    duration = time.time() - start_time

    if not success:
        return ValidationResult(success=False, message=f"Failed to check git status: {stderr}", duration=duration)

    # If output is empty, working tree is clean
    output = stdout.strip()
    if not output:
        return ValidationResult(
            success=True,
            message="Repository has no uncommitted changes",
            details={"clean": True, "uncommitted_files": []},
            duration=duration,
        )
    else:
        # Parse uncommitted files
        uncommitted_files = [line.strip() for line in output.split("\n") if line.strip()]
        return ValidationResult(
            success=False,
            message=f"Repository has {len(uncommitted_files)} uncommitted change(s)",
            details={"clean": False, "uncommitted_files": uncommitted_files},
            duration=duration,
        )


def validate_test_collection(image_name: str, milestone_id: str, state: str, timeout: int = 120) -> ValidationResult:
    """
    Validate test collection for a given state (START or END)

    Args:
        image_name: Docker image name
        milestone_id: Milestone ID (e.g., M001)
        state: "START" or "END"
        timeout: Command timeout in seconds

    Returns:
        ValidationResult with test collection details
    """
    start_time = time.time()

    language, test_framework, test_dir = infer_language_framework_and_test_dir(image_name)
    defaults = get_validation_defaults(language, test_framework)
    if not defaults or "collect_cmd" not in defaults:
        return ValidationResult(
            success=False,
            message=f"{state} state: No validation config for {language}/{test_framework}",
            details={
                "language": language,
                "test_framework": test_framework,
                "available": (
                    sorted([f"{l}/{f}" for (l, f) in DEFAULT_VALIDATION_CONFIGS.keys()])  # noqa: E741
                    if DEFAULT_VALIDATION_CONFIGS
                    else []
                ),
            },
            duration=time.time() - start_time,
        )

    collect_cmd = defaults["collect_cmd"]
    collect_pattern = defaults.get("collect_pattern")
    try:
        collect_cmd = collect_cmd.format(test_dir=test_dir)
    except Exception:
        pass

    if state == "START":
        prefix = "cd /testbed"
    elif state == "END":
        prefix = f"cd /testbed && git checkout milestone-{milestone_id}-end"
    else:
        return ValidationResult(success=False, message=f"Invalid state: {state}. Must be START or END", duration=0.0)

    command = f"{prefix} && {collect_cmd}"
    success, stdout, stderr = run_docker_command(image_name, command, timeout=timeout)

    duration = time.time() - start_time
    output = stdout + stderr

    if not success:
        lowered = output.lower()
        if "no tests" in lowered or "0 tests" in lowered:
            return ValidationResult(
                success=True,
                message=f"{state} state: No tests found (may be expected)",
                details={
                    "language": language,
                    "test_framework": test_framework,
                    "test_dir": test_dir,
                    "collect_cmd": collect_cmd,
                    "collect_pattern": collect_pattern,
                    "test_count": 0,
                },
                duration=duration,
            )

        return ValidationResult(
            success=False,
            message=f"{state} state: Test collection failed",
            details={
                "language": language,
                "test_framework": test_framework,
                "test_dir": test_dir,
                "collect_cmd": collect_cmd,
                "collect_pattern": collect_pattern,
                "stdout": stdout[-500:] if len(stdout) > 500 else stdout,
                "stderr": stderr[-500:] if len(stderr) > 500 else stderr,
            },
            duration=duration,
        )

    test_count = None
    if collect_pattern:
        match = re.search(collect_pattern, output)
        if match:
            test_count = int(match.group(1))

    if test_count is not None:
        return ValidationResult(
            success=True,
            message=f"{state} state: Successfully collected {test_count} tests using {language}/{test_framework}",
            details={
                "language": language,
                "test_framework": test_framework,
                "test_dir": test_dir,
                "collect_cmd": collect_cmd,
                "collect_pattern": collect_pattern,
                "test_count": test_count,
            },
            duration=duration,
        )

    return ValidationResult(
        success=True,
        message=f"{state} state: Collection succeeded using {language}/{test_framework} (count not parsed)",
        details={
            "language": language,
            "test_framework": test_framework,
            "test_dir": test_dir,
            "collect_cmd": collect_cmd,
            "collect_pattern": collect_pattern,
            "test_count": None,
            "output": output[-500:] if len(output) > 500 else output,
        },
        duration=duration,
    )


def validate_single_image(
    image_name: str, milestone_id: str, timeout: int = 120, require_exact_test_count: bool = True
) -> ImageValidationReport:
    """
    Run complete validation on a Docker image

    Args:
        image_name: Docker image name
        milestone_id: Milestone ID (e.g., M001)
        timeout: Default timeout for docker commands
        require_exact_test_count: If True, requires START and END test counts to be exactly equal.
                                   If False, allows END >= START (useful for milestones that add features).

    Returns:
        ImageValidationReport with all validation results
    """
    start_time = time.time()

    print(f"\n{'=' * 80}")
    print(f"Validating Image: {image_name}")
    print(f"Milestone ID: {milestone_id}")
    print(f"{'=' * 80}\n")

    # Step 1: Validate default state
    print("📍 Checking default state...")
    default_result = validate_default_state(image_name, milestone_id, timeout=10)
    print(f"   {'✅' if default_result.success else '❌'} {default_result.message}")

    # Step 2: Validate clean working tree
    print("\n🔍 Checking for uncommitted changes...")
    clean_tree_result = validate_clean_working_tree(image_name, timeout=10)
    print(f"   {'✅' if clean_tree_result.success else '❌'} {clean_tree_result.message}")
    if not clean_tree_result.success and clean_tree_result.details:
        uncommitted = clean_tree_result.details.get("uncommitted_files", [])
        for file in uncommitted[:5]:  # Show first 5 files
            print(f"      {file}")
        if len(uncommitted) > 5:
            print(f"      ... and {len(uncommitted) - 5} more")

    # Step 3: Validate START state test collection
    print("\n🧪 Validating START state test collection...")
    start_result = validate_test_collection(image_name, milestone_id, "START", timeout=timeout)
    print(f"   {'✅' if start_result.success else '❌'} {start_result.message}")

    # Step 4: Validate END state test collection
    print("\n🧪 Validating END state test collection...")
    end_result = validate_test_collection(image_name, milestone_id, "END", timeout=timeout)
    print(f"   {'✅' if end_result.success else '❌'} {end_result.message}")

    # Step 5: Validate test count match between START and END
    print("\n🔢 Checking test count match...")
    if start_result.success and end_result.success and start_result.details and end_result.details:
        start_count = start_result.details.get("test_count")
        end_count = end_result.details.get("test_count")

        # Most frameworks don't expose a stable "collect count". Only enforce the
        # comparison when both sides produced a numeric count.
        if start_count is None or end_count is None:
            test_count_result = ValidationResult(
                success=True,
                message="Skipped test count check (count not available for this framework)",
                details={"start_test_count": start_count, "end_test_count": end_count},
            )
        else:
            if require_exact_test_count:
                # Strict mode: require exact match
                if start_count == end_count:
                    test_count_result = ValidationResult(
                        success=True,
                        message=f"Test count matches: START={start_count}, END={end_count}",
                        details={"start_count": start_count, "end_count": end_count},
                    )
                else:
                    test_count_result = ValidationResult(
                        success=False,
                        message=f"Test count mismatch! START={start_count}, END={end_count} (diff={end_count - start_count})",
                        details={"start_count": start_count, "end_count": end_count, "diff": end_count - start_count},
                    )
            else:
                # Flexible mode: allow END >= START (for milestones that add features)
                if end_count >= start_count:
                    test_count_result = ValidationResult(
                        success=True,
                        message=f"Test count acceptable: START={start_count}, END={end_count} (diff={end_count - start_count})",
                        details={"start_count": start_count, "end_count": end_count, "diff": end_count - start_count},
                    )
                else:
                    test_count_result = ValidationResult(
                        success=False,
                        message=f"Test count decreased! START={start_count}, END={end_count} (diff={end_count - start_count})",
                        details={"start_count": start_count, "end_count": end_count, "diff": end_count - start_count},
                    )
    else:
        # Cannot compare if either collection failed
        test_count_result = ValidationResult(
            success=False,
            message="Cannot compare test counts: one or both collections failed",
            details={"start_success": start_result.success, "end_success": end_result.success},
        )
    print(f"   {'✅' if test_count_result.success else '❌'} {test_count_result.message}")

    # Overall success
    overall_success = (
        default_result.success
        and clean_tree_result.success
        and start_result.success
        and end_result.success
        and test_count_result.success
    )

    total_duration = time.time() - start_time

    print(f"\n{'=' * 80}")
    print(f"{'✅ VALIDATION PASSED' if overall_success else '❌ VALIDATION FAILED'}")
    print(f"Total duration: {total_duration:.2f}s")
    print(f"{'=' * 80}\n")

    return ImageValidationReport(
        image_name=image_name,
        milestone_id=milestone_id,
        validation_time=datetime.now().isoformat(),
        default_state_check=default_result,
        clean_working_tree_check=clean_tree_result,
        start_state_check=start_result,
        end_state_check=end_result,
        test_count_match_check=test_count_result,
        overall_success=overall_success,
        total_duration=total_duration,
    )


def validate_batch(
    metadata_file: Path,
    output_dir: Optional[Path] = None,
    save_to_dockerfile_dir: bool = False,
    timeout: int = 120,
    require_exact_test_count: bool = True,
) -> List[ImageValidationReport]:
    """
    Validate multiple images based on metadata file

    Args:
        metadata_file: Path to metadata.json file
        output_dir: Optional directory to save validation reports
        save_to_dockerfile_dir: If True, save reports next to Dockerfiles
        timeout: Default timeout for docker commands
        require_exact_test_count: If True, requires START and END test counts to be exactly equal.
                                   If False, allows END >= START (useful for milestones that add features).

    Returns:
        List of ImageValidationReport
    """
    # Load metadata
    with open(metadata_file, "r") as f:
        metadata = json.load(f)

    # Get all milestones
    milestones = metadata.get("milestones", [])

    # Get workspace directory (parent of metadata file)
    workspace_dir = metadata_file.parent

    # Infer repo_id and test_scenario from workspace_dir path (same logic as build_images.py)
    # Expected format: .../harness_workspace/{repo_id}/{test_scenario}
    path_parts = workspace_dir.parts
    test_scenario = path_parts[-1]  # Last part (e.g., test_multi_stage_V2)
    repo_id = path_parts[-2]  # Second to last part (e.g., urllib3_urllib3_2.0.6_2.3.0)

    reports = []

    for milestone in milestones:
        milestone_id = milestone["id"]
        # Construct image name (same logic as build_images.py)
        # Docker image names must be lowercase
        image_name = f"{repo_id}/{test_scenario}/{milestone_id}:latest".lower()

        print(f"\n{'#' * 80}")
        print(f"Processing {milestone_id}: {milestone.get('title', 'N/A')}")
        print(f"{'#' * 80}")

        try:
            report = validate_single_image(
                image_name, milestone_id, timeout=timeout, require_exact_test_count=require_exact_test_count
            )
            reports.append(report)

            # Determine where to save the report
            if save_to_dockerfile_dir:
                # Save to dockerfiles/{MILESTONE_ID}/validation.json
                dockerfile_dir = workspace_dir / "dockerfiles" / milestone_id
                if dockerfile_dir.exists():
                    report_file = dockerfile_dir / "validation.json"
                    save_report(report, report_file)
                else:
                    print(f"⚠️  Dockerfile directory not found: {dockerfile_dir}, skipping report save")
            elif output_dir:
                # Save to specified output directory
                output_dir.mkdir(parents=True, exist_ok=True)
                report_file = output_dir / f"{milestone_id}_validation.json"
                save_report(report, report_file)

        except Exception as e:
            print(f"❌ Error validating {milestone_id}: {e}")
            continue

    return reports


def save_report(report: ImageValidationReport, output_file: Path):
    """Save validation report to JSON file"""
    # Convert dataclass to dict
    report_dict = {
        "image_name": report.image_name,
        "milestone_id": report.milestone_id,
        "validation_time": report.validation_time,
        "default_state_check": asdict(report.default_state_check),
        "clean_working_tree_check": asdict(report.clean_working_tree_check),
        "start_state_check": asdict(report.start_state_check),
        "end_state_check": asdict(report.end_state_check),
        "test_count_match_check": asdict(report.test_count_match_check),
        "overall_success": report.overall_success,
        "total_duration": report.total_duration,
    }

    with open(output_file, "w") as f:
        json.dump(report_dict, f, indent=2)

    print(f"📄 Report saved to: {output_file}")


def print_summary(reports: List[ImageValidationReport]):
    """Print summary of all validation reports"""
    print(f"\n{'=' * 80}")
    print("VALIDATION SUMMARY")
    print(f"{'=' * 80}\n")

    total = len(reports)
    passed = sum(1 for r in reports if r.overall_success)
    failed = total - passed

    print(f"Total images validated: {total}")
    print(f"✅ Passed: {passed}")
    print(f"❌ Failed: {failed}")

    if failed > 0:
        print("\nFailed images:")
        for report in reports:
            if not report.overall_success:
                print(f"  - {report.milestone_id}: {report.image_name}")

    print(f"\n{'=' * 80}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Validate milestone Docker images",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Validate a single image
  python validate_docker_images.py --image urllib3_urllib3/test_multi_stage_advanced/m001 --milestone-id M001

  # Validate all images from metadata
  python validate_docker_images.py --batch --metadata /path/to/metadata.json

  # Validate and save reports to Dockerfile directories
  python validate_docker_images.py --batch --metadata /path/to/metadata.json --save-to-dockerfile-dir

  # Validate with custom timeout and save reports
  python validate_docker_images.py --image IMAGE_NAME --milestone-id M001 --timeout 180 --output ./reports
        """,
    )

    parser.add_argument("--image", type=str, help="Docker image name to validate")

    parser.add_argument("--milestone-id", type=str, help="Milestone ID (e.g., M001)")

    parser.add_argument("--batch", action="store_true", help="Validate multiple images from metadata file")

    parser.add_argument("--metadata", type=Path, help="Path to metadata.json file (for batch mode)")

    parser.add_argument(
        "--timeout", type=int, default=120, help="Timeout for docker commands in seconds (default: 120)"
    )

    parser.add_argument("--output", type=Path, help="Output directory for validation reports")

    parser.add_argument(
        "--save-to-dockerfile-dir",
        action="store_true",
        help="Save validation reports to Dockerfile directories (dockerfiles/{MILESTONE_ID}/validation.json)",
    )

    parser.add_argument(
        "--allow-test-count-increase",
        action="store_true",
        help="Allow END state to have more tests than START state (useful for milestones that add features). By default, requires exact match.",
    )

    args = parser.parse_args()

    # Validate arguments
    if args.batch:
        if not args.metadata:
            parser.error("--metadata is required when using --batch")
        if not args.metadata.exists():
            parser.error(f"Metadata file not found: {args.metadata}")

        # Batch validation
        require_exact = not args.allow_test_count_increase
        reports = validate_batch(
            args.metadata,
            args.output,
            save_to_dockerfile_dir=args.save_to_dockerfile_dir,
            timeout=args.timeout,
            require_exact_test_count=require_exact,
        )
        print_summary(reports)

        # Save combined report
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            combined_file = args.output / "validation_summary.json"
            with open(combined_file, "w") as f:
                json.dump([asdict(r) for r in reports], f, indent=2)
            print(f"📄 Combined report saved to: {combined_file}")

        # Exit with error if any validation failed
        if any(not r.overall_success for r in reports):
            sys.exit(1)

    else:
        if not args.image or not args.milestone_id:
            parser.error("--image and --milestone-id are required for single image validation")

        # Single image validation
        require_exact = not args.allow_test_count_increase
        report = validate_single_image(
            args.image, args.milestone_id, args.timeout, require_exact_test_count=require_exact
        )

        # Save report if output specified
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            report_file = args.output / f"{args.milestone_id}_validation.json"
            save_report(report, report_file)

        # Exit with error if validation failed
        if not report.overall_success:
            sys.exit(1)


if __name__ == "__main__":
    main()
