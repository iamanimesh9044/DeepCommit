#!/usr/bin/env python3
"""
Cleanup Docker containers after SRS generation.

This script automates Phase 3 of the SRS generation workflow:
1. Remove milestone container (temporary)
2. Keep base container (reusable across milestones)

The base container is kept running to speed up future SRS generations
for other milestones in the same repository.
"""

import argparse
import logging
import subprocess
import sys


class ContainerCleanup:
    """Cleanup Docker containers after SRS generation."""

    def __init__(
        self,
        milestone_id: str,
        milestone_image: str,
        keep_milestone: bool = False,
    ):
        self.milestone_id = milestone_id
        self.milestone_image = milestone_image
        self.keep_milestone = keep_milestone

        # Container name
        self.milestone_container = f"{milestone_image.replace('/', '-')}-container"

        # Setup logger
        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        """Setup logger."""
        logger = logging.getLogger(f"cleanup.{self.milestone_id}")
        logger.setLevel(logging.INFO)

        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        formatter = logging.Formatter("[%(name)s] %(levelname)s: %(message)s")
        console.setFormatter(formatter)
        logger.addHandler(console)

        return logger

    def _run_docker_cmd(self, cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run docker command."""
        self.logger.debug(f"Docker: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            raise RuntimeError(f"Docker command failed: {result.stderr}")
        return result

    def _container_exists(self, container_name: str) -> bool:
        """Check if container exists."""
        result = self._run_docker_cmd(
            ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=^{container_name}$"],
            check=False,
        )
        return container_name in result.stdout

    def cleanup(self):
        """Cleanup containers."""
        self.logger.info("=" * 70)
        self.logger.info("Container Cleanup for SRS Generation")
        self.logger.info("=" * 70)
        self.logger.info(f"Milestone: {self.milestone_id}")
        self.logger.info(f"Milestone container: {self.milestone_container}")
        self.logger.info("=" * 70)

        # Remove milestone container
        if self._container_exists(self.milestone_container):
            if self.keep_milestone:
                self.logger.info(f"Keeping milestone container '{self.milestone_container}' (--keep-milestone)")
                self.logger.info(f"  To remove later: docker rm -f {self.milestone_container}")
            else:
                self.logger.info(f"Removing milestone container '{self.milestone_container}'...")
                self._run_docker_cmd(["docker", "rm", "-f", self.milestone_container], check=False)
                self.logger.info("✓ Milestone container removed")
        else:
            self.logger.info(f"Milestone container '{self.milestone_container}' not found (already removed?)")

        # Summary
        self.logger.info("\n" + "=" * 70)
        self.logger.info("✓ CLEANUP COMPLETED")
        self.logger.info("=" * 70)
        self.logger.info("Base container kept for reuse across milestones")
        self.logger.info(
            "To cleanup all containers: docker ps -a | grep container | awk '{print $1}' | xargs docker rm -f"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Cleanup Docker containers after SRS generation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--milestone-id", required=True, help="Milestone ID (e.g., M001)")
    parser.add_argument(
        "--milestone-image",
        help="Milestone Docker image name (auto-generated if --repo-name provided)",
    )
    parser.add_argument(
        "--repo-name",
        help="Repository name for auto-deriving milestone-image (e.g., urllib3_urllib3_2.0.6_2.3.0)",
    )
    parser.add_argument(
        "--milestone-version",
        default="test_multi_stage_v2",
        help="Milestone version for auto-deriving milestone-image (default: test_multi_stage_v2)",
    )
    parser.add_argument(
        "--keep-milestone",
        action="store_true",
        help="Keep milestone container instead of removing it",
    )

    args = parser.parse_args()

    # Auto-derive milestone-image if not provided
    if not args.milestone_image:
        if not args.repo_name:
            parser.error("Either --milestone-image or --repo-name must be specified")

        args.milestone_image = f"{args.repo_name}/{args.milestone_version.lower()}/{args.milestone_id.lower()}"
        print(f"Auto-derived milestone-image: {args.milestone_image}")

    try:
        cleanup = ContainerCleanup(
            milestone_id=args.milestone_id,
            milestone_image=args.milestone_image,
            keep_milestone=args.keep_milestone,
        )
        cleanup.cleanup()
        sys.exit(0)
    except Exception as e:
        print(f"\n✗ Cleanup failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
