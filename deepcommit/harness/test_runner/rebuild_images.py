#!/usr/bin/env python3
"""
Rebuild Docker images for milestones without running tests.

Usage:
    # Rebuild specific milestones
    python -m harness.test_runner.rebuild_images M001 M002 \
        --work-dir DATA/harness_workspace/repo_id/test_scenario

    # Rebuild all milestones
    python -m harness.test_runner.rebuild_images --all \
        --work-dir DATA/harness_workspace/repo_id/test_scenario

    # Verbose output (show docker build logs)
    python -m harness.test_runner.rebuild_images M001 --work-dir ... -v
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from .core.docker import build_docker_image, check_image_exists
from .run_milestone_tests import discover_milestones, infer_repo_info

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def rebuild_image(
    milestone_id: str,
    work_dir: Path,
    repo_id: str,
    test_scenario: str,
    verbose: bool = False,
) -> bool:
    dockerfile_dir = work_dir / "dockerfiles" / milestone_id
    dockerfile_path = dockerfile_dir / "Dockerfile"
    build_context = work_dir / "testbed"

    image_name = f"{repo_id}/{test_scenario}/{milestone_id}:latest".lower()

    if not dockerfile_path.exists():
        logger.error(f"[{milestone_id}] Dockerfile not found: {dockerfile_path}")
        return False

    if not build_context.exists():
        logger.error(f"[{milestone_id}] Build context not found: {build_context}")
        return False

    exists = check_image_exists(image_name)
    logger.info(f"[{milestone_id}] {'Rebuilding' if exists else 'Building'} image: {image_name}")

    return build_docker_image(str(dockerfile_path), image_name, build_context, verbose)


def main():
    parser = argparse.ArgumentParser(
        description="Rebuild Docker images for milestones (without running tests)",
    )
    parser.add_argument("milestones", nargs="*", help="Milestone ID(s) to rebuild")
    parser.add_argument("--all", action="store_true", help="Rebuild all milestones")
    parser.add_argument("--work-dir", type=Path, required=True, help="Working directory")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show docker build output")

    args = parser.parse_args()

    work_dir = args.work_dir.resolve()
    if not work_dir.exists():
        print(f"Error: Work directory not found: {work_dir}", file=sys.stderr)
        return 1

    repo_id, test_scenario = infer_repo_info(work_dir)
    if not repo_id:
        print("Error: Cannot infer repo_id from work_dir path", file=sys.stderr)
        return 1

    if args.all:
        milestones = discover_milestones(work_dir)
        if not milestones:
            print(f"Error: No milestones found in {work_dir / 'dockerfiles'}", file=sys.stderr)
            return 1
    elif args.milestones:
        milestones = args.milestones
    else:
        print("Error: Specify milestone ID(s) or use --all", file=sys.stderr)
        return 1

    print(f"Repository: {repo_id}")
    print(f"Test scenario: {test_scenario}")
    print(f"Milestones to rebuild: {', '.join(milestones)}")
    print()

    success = 0
    failed = 0
    start_time = time.time()

    for i, milestone_id in enumerate(milestones):
        print(f"[{i + 1}/{len(milestones)}] {milestone_id}...")
        if rebuild_image(milestone_id, work_dir, repo_id, test_scenario, args.verbose):
            print("  -> OK")
            success += 1
        else:
            print("  -> FAILED")
            failed += 1

    elapsed = time.time() - start_time
    print()
    print(f"Done: {success} succeeded, {failed} failed ({elapsed:.1f}s)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
