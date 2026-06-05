#!/usr/bin/env python3
"""
Build Docker images for milestones.

This script builds Docker images for one or more milestones from their
Dockerfiles in the dockerfiles/ directory. By default, validates each image
after building and saves validation reports to dockerfiles/{MILESTONE_ID}/validation.json

The repo_id and test_scenario are automatically inferred from the work_dir path.
Expected format: .../harness_workspace/{repo_id}/{test_scenario}

Usage:
    # Build all milestone images (with validation by default)
    python build_images.py --all --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2

    # Build specific milestones
    python build_images.py --milestones M001 M002 M003 --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2

    # Build without validation
    python build_images.py --all --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2 --no-validate

    # List available milestones
    python build_images.py --list --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2
"""

import argparse
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

from deepcommit.harness.prepare_images.show_env_patches import export_tag_patches

# Import validation functions
from deepcommit.harness.prepare_images.validate_docker_images import save_report, validate_single_image


def find_milestones(work_dir: Path) -> List[str]:
    """
    Find all available milestone directories.

    Args:
        work_dir: Working directory

    Returns:
        List of milestone IDs (e.g., ["M001", "M002", ...])
    """
    # Support dockerfiles/, configs/, and env_data/ directory names
    dockerfiles_dir = work_dir / "dockerfiles"
    configs_dir = work_dir / "configs"
    env_data_dir = work_dir / "env_data"

    if dockerfiles_dir.exists():
        milestone_dir = dockerfiles_dir
    elif env_data_dir.exists():
        milestone_dir = env_data_dir
    elif configs_dir.exists():
        milestone_dir = configs_dir
    else:
        return []

    milestones = []
    # Support both M* (e.g., M001) and milestone_* naming patterns
    for milestone_path in sorted(milestone_dir.iterdir()):
        if milestone_path.is_dir() and (milestone_path / "Dockerfile").exists():
            milestones.append(milestone_path.name)

    return milestones


def list_milestones(work_dir: Path, repo_id: str, test_scenario: str) -> None:
    """List all available milestones with their Dockerfiles."""
    milestones = find_milestones(work_dir)

    print("📋 Available milestones:")
    print()

    if not milestones:
        print("   No milestones found with Dockerfiles")
        return

    # Determine milestone directory
    if (work_dir / "dockerfiles").exists():
        milestone_dir = work_dir / "dockerfiles"
    elif (work_dir / "env_data").exists():
        milestone_dir = work_dir / "env_data"
    else:
        milestone_dir = work_dir / "configs"

    for milestone_id in milestones:
        dockerfile_path = milestone_dir / milestone_id / "Dockerfile"
        # Docker image names must be lowercase
        image_name = f"{repo_id}/{test_scenario}/{milestone_id}:latest".lower()

        print(f"   • {milestone_id}")
        print(f"     Dockerfile: {dockerfile_path.relative_to(work_dir)}")
        print(f"     Image name: {image_name}")

        # Check if image exists
        try:
            result = subprocess.run(
                ["docker", "images", "-q", image_name],
                capture_output=True,
                text=True,
                check=True,
            )
            if result.stdout.strip():
                print("     Status: ✅ Built")
            else:
                print("     Status: ⚪ Not built")
        except subprocess.CalledProcessError:
            print("     Status: ⚪ Not built")

        print()


def image_exists(image_name: str) -> bool:
    """
    Check if a Docker image exists locally.

    Args:
        image_name: Docker image name (e.g., "repo/scenario/milestone:tag")

    Returns:
        True if image exists, False otherwise
    """
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


def build_image(
    milestone_id: str,
    work_dir: Path,
    repo_id: str,
    test_scenario: str,
    verbose: bool = False,
    dry_run: bool = False,
    validate: bool = True,
    validation_timeout: int = 120,
    skip_existing: bool = True,
    testbed_dir: Path = None,
    allow_test_count_increase: bool = False,
) -> Tuple[bool, str]:
    """
    Build Docker image for a single milestone.

    Args:
        milestone_id: Milestone ID (e.g., "M001")
        work_dir: Working directory
        repo_id: Repository identifier
        test_scenario: Test scenario name
        verbose: Enable verbose output
        dry_run: Show what would be done without actually building
        validate: Run validation after build
        validation_timeout: Timeout for validation
        skip_existing: Skip building if image already exists (default: True)

    Returns:
        Tuple of (success: bool, message: str)
    """
    # Determine milestone directory
    if (work_dir / "dockerfiles").exists():
        milestone_dir = work_dir / "dockerfiles"
    elif (work_dir / "env_data").exists():
        milestone_dir = work_dir / "env_data"
    else:
        milestone_dir = work_dir / "configs"
    dockerfile_path = milestone_dir / milestone_id / "Dockerfile"

    if not dockerfile_path.exists():
        return False, f"Dockerfile not found: {dockerfile_path}"

    # Docker image names must be lowercase
    image_name = f"{repo_id}/{test_scenario}/{milestone_id}:latest".lower()

    # Check if image already exists
    if skip_existing and image_exists(image_name):
        print(f"⏭️  Skipping {milestone_id} (image already exists)")
        if verbose:
            print(f"   Image: {image_name}")
        print()
        return True, "Image already exists (skipped)"

    if dry_run:
        print(f"[DRY RUN] Would build: {image_name}")
        print(f"          Dockerfile: {dockerfile_path.relative_to(work_dir)}")
        print(f"          Context: {work_dir}")
        if validate:
            print("          Would validate after build")
        return True, "Dry run - no actual build"

    print(f"🔨 Building {milestone_id}...")
    if verbose:
        print(f"   Image: {image_name}")
        print(f"   Dockerfile: {dockerfile_path.relative_to(work_dir)}")
        print(f"   Context: {work_dir}")
    print()

    try:
        # Build command
        # Use testbed/ as build context since Dockerfile expects repository root
        if testbed_dir:
            build_context = Path(testbed_dir)
        else:
            build_context = work_dir / "testbed"
        if not build_context.exists():
            return False, f"Build context not found: {build_context}"

        cmd = [
            "docker",
            "build",
            "-t",
            image_name,
            "-f",
            str(dockerfile_path),
            str(build_context),
        ]

        if verbose:
            print(f"   Command: {' '.join(cmd)}")
            print()

        # Run docker build
        result = subprocess.run(  # noqa: F841
            cmd,
            cwd=work_dir,
            capture_output=not verbose,
            text=True,
            check=True,
        )

        print(f"✅ {milestone_id} built successfully")
        print()

        # Validate the image if requested
        if validate:
            print(f"🔍 Validating {milestone_id}...")
            validation_success = validate_image(
                milestone_id,
                image_name,
                milestone_dir,
                validation_timeout,
                allow_test_count_increase=allow_test_count_increase,
            )
            if not validation_success:
                return True, "Build successful, but validation failed"

        # Export tag patches
        print(f"📤 Exporting tag patches for {milestone_id}...")
        metadata_path = work_dir / "metadata.json"
        output_dir = milestone_dir / milestone_id / "tag_patches"
        diff_success = export_tag_patches(milestone_id, str(metadata_path), output_dir, image_name=image_name)
        if not diff_success:
            print(f"⚠️  Warning: Failed to export tag patches for {milestone_id}")

        return True, "Build successful"

    except subprocess.CalledProcessError as e:
        error_msg = f"Build failed for {milestone_id}"
        if e.stderr:
            error_msg += f"\n{e.stderr}"
        print(f"❌ {error_msg}")
        print()
        return False, error_msg


def validate_image(
    milestone_id: str, image_name: str, milestone_dir: Path, timeout: int = 120, allow_test_count_increase: bool = False
) -> bool:
    """
    Validate a built Docker image.

    Args:
        milestone_id: Milestone ID
        image_name: Docker image name
        milestone_dir: Directory containing milestone Dockerfiles
        timeout: Validation timeout
        allow_test_count_increase: Allow END state to have more tests than START state

    Returns:
        True if validation passed, False otherwise
    """
    try:
        # Run validation
        # Use require_exact_test_count based on allow_test_count_increase flag
        require_exact = not allow_test_count_increase
        report = validate_single_image(
            image_name, milestone_id, timeout=timeout, require_exact_test_count=require_exact
        )

        # Save validation report to Dockerfile directory
        validation_file = milestone_dir / milestone_id / "validation.json"
        save_report(report, validation_file)

        if not report.overall_success:
            print(f"⚠️  Validation failed for {milestone_id}")
            return False

        print(f"✅ Validation passed for {milestone_id}")
        print()
        return True

    except Exception as e:
        print(f"❌ Validation error for {milestone_id}: {e}")
        print()
        return False


def build_milestones(
    milestone_ids: List[str],
    work_dir: Path,
    repo_id: str,
    test_scenario: str,
    verbose: bool = False,
    dry_run: bool = False,
    validate: bool = True,
    validation_timeout: int = 120,
    skip_existing: bool = True,
    testbed_dir: Path = None,
    max_workers: int = None,
    allow_test_count_increase: bool = False,
) -> Dict[str, Tuple[bool, str]]:
    """
    Build Docker images for multiple milestones in parallel, then validate sequentially.

    Args:
        milestone_ids: List of milestone IDs to build
        work_dir: Working directory
        repo_id: Repository identifier
        test_scenario: Test scenario name
        verbose: Enable verbose output
        dry_run: Show what would be done
        validate: Run validation after build (sequentially for reliability)
        validation_timeout: Timeout for validation
        skip_existing: Skip building if image already exists
        max_workers: Maximum number of parallel builds (None = number of milestones)

    Returns:
        Dictionary mapping milestone_id to (success, message) tuples
    """
    results = {}
    total = len(milestone_ids)

    # Use all milestones in parallel if max_workers not specified
    if max_workers is None:
        max_workers = total

    print(f"🚀 Building {total} milestone image(s) in parallel (max_workers={max_workers})...")
    print("   Note: Validation will run sequentially after builds complete")
    print()

    start_time = time.time()

    # Thread-safe printing
    print_lock = threading.Lock()

    def build_with_logging(milestone_id: str, index: int) -> Tuple[str, bool, str]:
        """Wrapper to build image with thread-safe logging (without validation)."""
        with print_lock:
            print(f"[{index}/{total}] Starting {milestone_id}...")

        success, message = build_image(
            milestone_id,
            work_dir,
            repo_id,
            test_scenario,
            verbose=False,  # Disable verbose in parallel mode to avoid mixed output
            dry_run=dry_run,
            validate=False,  # Disable validation during parallel build
            validation_timeout=validation_timeout,
            skip_existing=skip_existing,
            testbed_dir=testbed_dir,
            allow_test_count_increase=allow_test_count_increase,
        )

        with print_lock:
            status = "✅" if success else "❌"
            print(f"[{index}/{total}] {status} {milestone_id} - {message}")

        return milestone_id, success, message

    # Build in parallel using ThreadPoolExecutor
    print("=" * 60)
    print("📦 Phase 1: Parallel Building")
    print("=" * 60)
    print()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all build tasks
        future_to_milestone = {
            executor.submit(build_with_logging, milestone_id, i): milestone_id
            for i, milestone_id in enumerate(milestone_ids, 1)
        }

        # Collect results as they complete
        for future in as_completed(future_to_milestone):
            milestone_id, success, message = future.result()
            results[milestone_id] = (success, message)

    print()
    print("=" * 60)
    print("🔍 Phase 2: Sequential Validation")
    print("=" * 60)
    print()

    # Validate sequentially after all builds complete
    if validate and not dry_run:
        milestone_dir = work_dir / "dockerfiles"
        if not milestone_dir.exists():
            milestone_dir = work_dir / "env_data"
        if not milestone_dir.exists():
            milestone_dir = work_dir / "configs"

        for i, milestone_id in enumerate(milestone_ids, 1):
            # Only validate if the build was successful
            if not results[milestone_id][0]:
                print(f"[{i}/{total}] ⏭️  Skipping validation for {milestone_id} (build failed)")
                continue

            print(f"[{i}/{total}] 🔍 Validating {milestone_id}...")

            image_name = f"{repo_id}/{test_scenario}/{milestone_id}:latest".lower()
            validation_success = validate_image(milestone_id, image_name, milestone_dir, validation_timeout)

            if not validation_success:
                # Update result to reflect validation failure
                results[milestone_id] = (True, "Build successful, but validation failed")
                print(f"[{i}/{total}] ⚠️  {milestone_id} validation failed")
            else:
                print(f"[{i}/{total}] ✅ {milestone_id} validation passed")

            # Export tag patches
            print(f"[{i}/{total}] 📤 Exporting tag patches for {milestone_id}...")
            metadata_path = work_dir / "metadata.json"
            output_dir = milestone_dir / milestone_id / "tag_patches"
            diff_success = export_tag_patches(milestone_id, str(metadata_path), output_dir, image_name=image_name)
            if not diff_success:
                print(f"[{i}/{total}] ⚠️  Warning: Failed to export tag patches for {milestone_id}")

            print()

    print()

    elapsed = time.time() - start_time

    # Summary
    print("=" * 60)
    print("📊 Build Summary")
    print("=" * 60)
    print()

    successful = [m for m, (s, _) in results.items() if s]
    skipped = [m for m, (s, msg) in results.items() if s and "skipped" in msg.lower()]
    built = [m for m in successful if m not in skipped]
    failed = [m for m, (s, _) in results.items() if not s]

    print(f"Total: {total}")
    print(f"🔨 Built: {len(built)}")
    print(f"⏭️  Skipped: {len(skipped)}")
    print(f"❌ Failed: {len(failed)}")
    print(f"⏱️  Time: {elapsed:.1f}s")
    print()

    if built:
        print("🔨 Successfully built:")
        for milestone_id in built:
            # Docker image names must be lowercase
            image_name = f"{repo_id}/{test_scenario}/{milestone_id}:latest".lower()
            print(f"   • {milestone_id} → {image_name}")
        print()

    if skipped:
        print("⏭️  Skipped (already exists):")
        for milestone_id in skipped:
            # Docker image names must be lowercase
            image_name = f"{repo_id}/{test_scenario}/{milestone_id}:latest".lower()
            print(f"   • {milestone_id} → {image_name}")
        print()

    if failed:
        print("❌ Failed to build:")
        for milestone_id in failed:
            _, message = results[milestone_id]
            print(f"   • {milestone_id}: {message}")
        print()

    # Show built images
    if successful and not dry_run:
        print("📦 Built images:")
        try:
            subprocess.run(
                ["docker", "images", "--filter", f"reference={repo_id}/{test_scenario}/*"],
                check=False,
            )
        except Exception:
            pass

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Build Docker images for milestones",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build all milestone images (with validation by default)
  %(prog)s --all --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2

  # Build specific milestones
  %(prog)s --milestones M001 M002 M003 --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2

  # Build without validation
  %(prog)s --all --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2 --no-validate

  # List available milestones
  %(prog)s --list --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2

  # Dry run (show what would be built)
  %(prog)s --all --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2 --dry-run

Note:
  repo_id and test_scenario are automatically inferred from work_dir path
  Expected format: .../harness_workspace/{repo_id}/{test_scenario}
        """,
    )

    parser.add_argument(
        "--work-dir",
        type=str,
        required=True,
        help="Working directory containing dockerfiles/ and testbed/ directories (format: .../harness_workspace/{repo_id}/{test_scenario})",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all",
        action="store_true",
        help="Build all milestone images",
    )
    group.add_argument(
        "--milestones",
        nargs="+",
        metavar="ID",
        help="Build specific milestones (e.g., M001 M002 M003)",
    )
    group.add_argument(
        "--list",
        action="store_true",
        help="List available milestones and exit",
    )

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without actually building",
    )

    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip validation after building (validation is enabled by default)",
    )

    parser.add_argument(
        "--allow-test-count-increase",
        action="store_true",
        help="Allow END state to have more tests than START state (useful for milestones that add features). By default, requires exact match.",
    )

    parser.add_argument(
        "--validation-timeout",
        type=int,
        default=120,
        help="Timeout for validation commands in seconds (default: 120)",
    )

    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Force rebuild even if image already exists (by default, existing images are skipped)",
    )

    parser.add_argument(
        "--testbed-dir",
        type=str,
        default=None,
        help="Custom testbed directory path (default: {work_dir}/testbed)",
    )

    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Maximum number of parallel builds (default: build all milestones in parallel)",
    )

    args = parser.parse_args()

    work_dir = Path(args.work_dir).resolve()

    # Check work directory exists
    if not work_dir.exists():
        print(f"❌ Work directory does not exist: {work_dir}", file=sys.stderr)
        return 1

    # Check for dockerfiles/, env_data/, or configs/ directory
    if (
        not (work_dir / "dockerfiles").exists()
        and not (work_dir / "env_data").exists()
        and not (work_dir / "configs").exists()
    ):
        print(f"❌ None of dockerfiles/, env_data/, or configs/ directory exists in {work_dir}", file=sys.stderr)
        return 1

    # Infer repo_id and test_scenario from work_dir path
    # Expected format: .../harness_workspace/{repo_id}/{test_scenario}
    path_parts = work_dir.parts
    if len(path_parts) < 2:
        print(f"❌ Cannot infer repo_id and test_scenario from work_dir: {work_dir}", file=sys.stderr)
        print("   Expected format: .../harness_workspace/{repo_id}/{test_scenario}", file=sys.stderr)
        return 1

    test_scenario = path_parts[-1]  # Last part
    repo_id = path_parts[-2]  # Second to last part

    print("📋 Inferred from work_dir:")
    print(f"   repo_id: {repo_id}")
    print(f"   test_scenario: {test_scenario}")
    print()

    try:
        if args.list:
            list_milestones(work_dir, repo_id, test_scenario)
            return 0

        # Determine which milestones to build
        if args.all:
            milestones = find_milestones(work_dir)
            if not milestones:
                print("❌ No milestones found with Dockerfiles")
                return 1
        else:
            milestones = args.milestones

        # Validate milestone IDs
        available = find_milestones(work_dir)
        invalid = [m for m in milestones if m not in available]
        if invalid:
            print(f"❌ Invalid milestone IDs: {', '.join(invalid)}")
            print(f"   Available: {', '.join(available)}")
            return 1

        # Build images
        results = build_milestones(
            milestones,
            work_dir,
            repo_id,
            test_scenario,
            verbose=args.verbose,
            dry_run=args.dry_run,
            validate=not args.no_validate,
            validation_timeout=args.validation_timeout,
            skip_existing=not args.force_rebuild,
            testbed_dir=Path(args.testbed_dir) if args.testbed_dir else None,
            max_workers=args.max_workers,
            allow_test_count_increase=args.allow_test_count_increase,
        )

        # Return non-zero if any builds failed
        failed = [m for m, (success, _) in results.items() if not success]
        return 1 if failed else 0

    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
