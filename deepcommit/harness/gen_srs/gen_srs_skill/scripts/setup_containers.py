#!/usr/bin/env python3
"""
Setup Docker containers for SRS generation environment comparison.

This script automates Phase 1.1 of the SRS generation workflow:
1. Verify/build base Docker image
2. Create and start base container
3. Verify/build milestone Docker image
4. Create milestone container (ready for env comparison)

The containers are used for comparing environment dependencies between
the base state and milestone state.
"""

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path


class ContainerSetup:
    """Manage Docker container setup for SRS generation."""

    def __init__(
        self,
        repo_name: str,
        milestone_version: str,
        milestone_id: str,
        base_image: str,
        milestone_image: str,
        git_repo: str,
        force: bool = False,
    ):
        self.repo_name = repo_name
        self.milestone_version = milestone_version
        self.milestone_id = milestone_id
        self.base_image = base_image
        self.milestone_image = milestone_image
        self.git_repo = Path(git_repo)
        self.force = force

        # Setup paths
        self.project_root = Path(__file__).parent.parent.parent.parent
        self.workspace_root = self.project_root / "srs_workspace"
        self.work_dir = self.workspace_root / repo_name / milestone_version
        self.env_data_dir = self.work_dir / "env_data"

        # Container names
        self.base_container = f"{base_image.replace('/', '-')}-container"
        self.milestone_container = f"{milestone_image.replace('/', '-')}-container"

        # Setup logger
        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        """Setup logger."""
        logger = logging.getLogger(f"container_setup.{self.milestone_id}")
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

    def _container_running(self, container_name: str) -> bool:
        """Check if container is running."""
        result = self._run_docker_cmd(
            ["docker", "ps", "--format", "{{.Names}}", "--filter", f"name=^{container_name}$"],
            check=False,
        )
        return container_name in result.stdout

    def _image_exists(self, image_name: str) -> bool:
        """Check if Docker image exists."""
        result = self._run_docker_cmd(["docker", "images", "-q", image_name], check=False)
        return bool(result.stdout.strip())

    def _build_docker_network_args(self) -> list[str]:
        """Build Docker network arguments with proxy and DNS hosts."""
        args = ["--network=host"]

        # Add proxy environment variables if set
        for env_var in ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"]:
            if env_var in os.environ:
                args.extend(["-e", f"{env_var}={os.environ[env_var]}"])

        # Add DNS host mappings for common domains (gVisor workaround)
        dns_hosts = {
            "pypi.org": "151.101.0.223",
            "files.pythonhosted.org": "151.101.0.223",
            "github.com": "140.82.121.3",
            "google.com": "142.250.185.46",
        }
        for host, ip in dns_hosts.items():
            args.extend(["--add-host", f"{host}:{ip}"])

        return args

    def _build_image(self, image_name: str, dockerfile_path: Path):
        """Build Docker image from Dockerfile."""
        self.logger.info(f"Building image {image_name} from {dockerfile_path}...")
        self._run_docker_cmd(["docker", "build", "-t", image_name, "-f", str(dockerfile_path), str(self.git_repo)])
        self.logger.info(f"✓ Image {image_name} built successfully")

    def setup(self):
        """Setup containers."""
        self.logger.info("=" * 70)
        self.logger.info("Container Setup for SRS Generation")
        self.logger.info("=" * 70)
        self.logger.info(f"Repository: {self.repo_name}")
        self.logger.info(f"Milestone: {self.milestone_id}")
        self.logger.info(f"Base image: {self.base_image}")
        self.logger.info(f"Milestone image: {self.milestone_image}")
        self.logger.info("=" * 70)

        # Verify git repo exists
        if not self.git_repo.exists():
            raise FileNotFoundError(f"Git repository not found: {self.git_repo}")

        # 1. Setup base container
        self.logger.info("\n[1/2] Setting up base container...")

        if not self._container_exists(self.base_container):
            # Check/build base image
            if not self._image_exists(self.base_image):
                base_dockerfile = self.env_data_dir / "Base" / "Dockerfile"
                if not base_dockerfile.exists():
                    raise FileNotFoundError(f"Base Dockerfile not found: {base_dockerfile}")
                self._build_image(self.base_image, base_dockerfile)

            # Create and start base container with long-running process
            # Use --init to properly reap zombie child processes
            self.logger.info(f"Creating base container '{self.base_container}'...")
            network_args = self._build_docker_network_args()
            run_cmd = (
                ["docker", "run", "-d", "--init"]
                + network_args
                + ["--name", self.base_container, self.base_image, "tail", "-f", "/dev/null"]
            )
            self._run_docker_cmd(run_cmd)
            self.logger.info(f"✓ Base container '{self.base_container}' created and started")
        elif not self._container_running(self.base_container):
            # Start base container if it exists but is not running
            self.logger.info(f"Starting base container '{self.base_container}'...")
            self._run_docker_cmd(["docker", "start", self.base_container])
            self.logger.info(f"✓ Base container '{self.base_container}' started")
        else:
            self.logger.info(f"✓ Base container '{self.base_container}' is already running")

        # 2. Setup milestone container
        self.logger.info("\n[2/2] Setting up milestone container...")

        if self._container_exists(self.milestone_container):
            if self.force:
                self.logger.info(f"Force flag set - removing existing container '{self.milestone_container}'")
                self._run_docker_cmd(["docker", "rm", "-f", self.milestone_container])
            else:
                raise RuntimeError(
                    f"Container '{self.milestone_container}' already exists. Use --force to recreate it."
                )

        # Check/build milestone image
        milestone_dockerfile = self.env_data_dir / self.milestone_id / "Dockerfile"
        if not self._image_exists(self.milestone_image):
            if not milestone_dockerfile.exists():
                raise FileNotFoundError(f"Dockerfile not found: {milestone_dockerfile}")
            self._build_image(self.milestone_image, milestone_dockerfile)
        else:
            self.logger.info(f"✓ Image '{self.milestone_image}' exists")

        # Create milestone container with a long-running process
        # Use --init to properly reap zombie child processes
        self.logger.info(f"Creating container '{self.milestone_container}'...")
        network_args = self._build_docker_network_args()
        run_cmd = (
            ["docker", "run", "-d", "--init"]
            + network_args
            + ["--name", self.milestone_container, self.milestone_image, "tail", "-f", "/dev/null"]
        )
        self._run_docker_cmd(run_cmd)
        self.logger.info(f"✓ Container '{self.milestone_container}' created and started")

        # Checkout to milestone-end
        self.logger.info(f"Checking out to milestone-{self.milestone_id}-end...")
        checkout_result = self._run_docker_cmd(
            ["docker", "exec", self.milestone_container, "git", "checkout", f"milestone-{self.milestone_id}-end"],
            check=False,
        )
        if checkout_result.returncode == 0:
            self.logger.info(f"✓ Milestone container checked out to milestone-{self.milestone_id}-end")
        else:
            self.logger.warning(f"⚠ Failed to checkout to milestone-{self.milestone_id}-end: {checkout_result.stderr}")

        # Summary
        self.logger.info("\n" + "=" * 70)
        self.logger.info("✓ CONTAINER SETUP COMPLETED")
        self.logger.info("=" * 70)
        self.logger.info(f"Base container: {self.base_container} (running)")
        self.logger.info(
            f"Milestone container: {self.milestone_container} (running, at milestone-{self.milestone_id}-end)"
        )
        self.logger.info("\nNext step: Run prepare_prompts.py to generate prompts")


def main():
    parser = argparse.ArgumentParser(
        description="Setup Docker containers for SRS generation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--repo-name", required=True, help="Repository name (e.g., urllib3_urllib3_2.0.6_2.3.0)")
    parser.add_argument("--milestone-version", required=True, help="Milestone version (e.g., test_multi_stage_V2)")
    parser.add_argument("--milestone-id", required=True, help="Milestone ID (e.g., M001)")
    parser.add_argument(
        "--base-image",
        help="Base Docker image name (auto-generated if not specified)",
    )
    parser.add_argument(
        "--milestone-image",
        help="Milestone Docker image name (auto-generated if not specified)",
    )
    parser.add_argument(
        "--git-repo",
        help="Path to git repository (auto-inferred if not specified)",
    )
    parser.add_argument("--force", action="store_true", help="Force recreate containers if they exist")

    args = parser.parse_args()

    # Auto-derive images
    if not args.base_image:
        args.base_image = f"{args.repo_name}/{args.milestone_version.lower()}/base"
        print(f"Auto-derived base-image: {args.base_image}")

    if not args.milestone_image:
        args.milestone_image = f"{args.repo_name}/{args.milestone_version.lower()}/{args.milestone_id.lower()}"
        print(f"Auto-derived milestone-image: {args.milestone_image}")

    # Auto-infer git_repo
    if not args.git_repo:
        project_root = Path(__file__).parent.parent.parent.parent
        data_root = project_root / "DATA" / "harness_workspace"
        args.git_repo = str(data_root / args.repo_name / args.milestone_version / "testbed")
        print(f"Auto-inferred git-repo: {args.git_repo}")

    try:
        setup = ContainerSetup(
            repo_name=args.repo_name,
            milestone_version=args.milestone_version,
            milestone_id=args.milestone_id,
            base_image=args.base_image,
            milestone_image=args.milestone_image,
            git_repo=args.git_repo,
            force=args.force,
        )
        setup.setup()
        sys.exit(0)
    except Exception as e:
        print(f"\n✗ Setup failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
