#!/usr/bin/env python3
"""
Generate complete SRS document for a milestone using Claude Code.

This script orchestrates the entire SRS generation pipeline:
1. Container management (create/verify containers for env comparison)
2. Render prompt templates (srs_gen_v1, acceptance_test_align, env_spec)
3. Execute Claude Code agents sequentially:
   - Agent 1: Generate initial SRS
   - Agent 2: Refine acceptance criteria
   - Agent 3: Generate environment dependencies
4. Assemble final SRS.md document

Example:
    # Minimal usage (trial-name auto-increments: v1, v2, v3, ...):
    python harness/gen_srs/generate_srs.py \\
        --repo-name urllib3_urllib3_2.0.6_2.3.0 \\
        --milestone-version test_multi_stage_V2 \\
        --milestone-id M001

    # With explicit trial name:
    python harness/gen_srs/generate_srs.py \\
        --repo-name urllib3_urllib3_2.0.6_2.3.0 \\
        --milestone-version test_multi_stage_V2 \\
        --milestone-id M001 \\
        --trial-name v1 \\
        --force
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

from jinja2 import Template


def get_next_trial_name(srs_dir: Path, milestone_id: str) -> str:
    """Find the next available trial version number for a specific milestone.

    Logic:
    - If srs_dir doesn't exist, return "v1"
    - Find the latest trial version (highest number)
    - If the milestone already exists in the latest trial, increment to next version
    - If the milestone doesn't exist in the latest trial, use the latest trial

    Args:
        srs_dir: Path to the srs directory (e.g., workspace/repo/version/srs/)
        milestone_id: The milestone ID to check (e.g., "M001")

    Returns:
        Trial name (e.g., "v1" if milestone doesn't exist yet, "v2" if it does)
    """
    if not srs_dir.exists():
        return "v1"

    # Find all directories matching pattern v{number}
    max_version = 0
    pattern = re.compile(r"^v(\d+)$")

    for item in srs_dir.iterdir():
        if item.is_dir():
            match = pattern.match(item.name)
            if match:
                version_num = int(match.group(1))
                max_version = max(max_version, version_num)

    if max_version == 0:
        return "v1"

    # Check if milestone exists in the latest trial
    latest_trial = f"v{max_version}"
    milestone_path = srs_dir / latest_trial / milestone_id

    if milestone_path.exists():
        # Milestone exists in latest trial, increment version
        return f"v{max_version + 1}"
    else:
        # Milestone doesn't exist in latest trial, use it
        return latest_trial


# Reuse existing SRSPromptPreparer
from prepare_prompt import SRSPromptPreparer  # noqa: E402


class SRSGenerator:
    """Orchestrate complete SRS generation using Claude Code."""

    def __init__(
        self,
        repo_name: str,
        milestone_version: str,
        milestone_id: str,
        base_image: str,
        milestone_image: str,
        trial_name: str,
        git_repo: str,
        force: bool = False,
        workspace_root: Optional[str] = None,
        include_new_tests: bool = True,
        suffix: Optional[str] = None,
        model: str = "claude-opus-4-6",
    ):
        """Initialize SRS generator.

        Args:
            repo_name: Repository identifier (e.g., urllib3_urllib3_2.0.6_2.3.0)
            milestone_id: Milestone ID (e.g., M001)
            base_image: Docker image name for base environment
            milestone_image: Docker image name for milestone environment
            trial_name: Trial version (e.g., v1)
            git_repo: Path to git repository for patch access
            force: Force recreate containers if they exist
            workspace_root: Override workspace root (default: srs_workspace/)
            include_new_tests: If True, treat new_tests from stable_classification as fail_to_pass
            suffix: Suffix for data files (e.g., "_filtered" -> "milestones_filtered.csv")
            model: Claude model to use (default: claude-opus-4-6)
        """
        self.repo_name = repo_name
        self.milestone_version = milestone_version
        self.milestone_id = milestone_id
        self.base_image = base_image
        self.milestone_image = milestone_image
        self.trial_name = trial_name
        self.git_repo = Path(git_repo)
        self.force = force
        self.include_new_tests = include_new_tests
        self.suffix = suffix or ""
        self.model = model

        # Setup paths
        if workspace_root:
            self.workspace_root = Path(workspace_root).resolve()  # Convert to absolute path
        else:
            self.workspace_root = Path(__file__).parent.parent.parent / "DATA" / "harness_workspace"

        # Project root (for relative path conversion)
        self.project_root = Path(__file__).parent.parent.parent.resolve()  # Convert to absolute path

        self.work_dir = self.workspace_root / repo_name / milestone_version
        self.env_data_dir = self.work_dir / "dockerfiles"
        self.test_data_dir = self.work_dir / "test_results"
        self.output_dir = self.work_dir / "srs" / trial_name / milestone_id
        # Create logs directory in output_dir instead of /tmp
        self.temp_dir = self.output_dir / "logs"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # Template paths
        self.template_dir = Path(__file__).parent
        self.srs_gen_tmpl = self.template_dir / "srs_gen_v1.tmpl"
        self.acceptance_tmpl = self.template_dir / "acceptance_test_align.tmpl"
        self.env_spec_tmpl = self.template_dir / "env_spec.tmpl"

        # Container names (auto-generated from image names, replace / with -)
        self.base_container = f"{base_image.replace('/', '-')}-container"
        self.milestone_container = f"{milestone_image.replace('/', '-')}-container"

        # Setup logger
        self.logger = self._setup_logger()

        # Validate paths
        self._validate()

    def _setup_logger(self) -> logging.Logger:
        """Setup logger for pipeline output."""
        logger = logging.getLogger(f"srs_gen.{self.milestone_id}")
        logger.setLevel(logging.INFO)

        # Console handler
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        formatter = logging.Formatter("[%(name)s] %(levelname)s: %(message)s")
        console.setFormatter(formatter)
        logger.addHandler(console)

        return logger

    def _validate(self):
        """Validate required paths and files."""
        if not self.work_dir.exists():
            raise FileNotFoundError(f"Work directory not found: {self.work_dir}")

        if not self.git_repo.exists():
            raise FileNotFoundError(f"Git repository not found: {self.git_repo}")

        for tmpl in [self.srs_gen_tmpl, self.acceptance_tmpl, self.env_spec_tmpl]:
            if not tmpl.exists():
                raise FileNotFoundError(f"Template not found: {tmpl}")

    def _run_docker_cmd(self, cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run docker command with logging."""
        self.logger.debug(f"Docker: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            raise RuntimeError(f"Docker command failed: {result.stderr}")
        return result

    def _container_exists(self, container_name: str) -> bool:
        """Check if container exists."""
        result = self._run_docker_cmd(
            ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=^{container_name}$"], check=False
        )
        return container_name in result.stdout

    def _container_running(self, container_name: str) -> bool:
        """Check if container is running."""
        result = self._run_docker_cmd(
            ["docker", "ps", "--format", "{{.Names}}", "--filter", f"name=^{container_name}$"], check=False
        )
        return container_name in result.stdout

    def _image_exists(self, image_name: str) -> bool:
        """Check if Docker image exists."""
        result = self._run_docker_cmd(["docker", "images", "-q", image_name], check=False)
        return bool(result.stdout.strip())

    def _build_image(self, image_name: str, dockerfile_path: Path):
        """Build Docker image from Dockerfile."""
        self.logger.info(f"Building image {image_name} from {dockerfile_path}...")
        # Build context should be the git repo (testbed), not the dockerfile directory
        # This allows COPY . /testbed/ to work correctly
        self._run_docker_cmd(["docker", "build", "-t", image_name, "-f", str(dockerfile_path), str(self.git_repo)])
        self.logger.info(f"✓ Image {image_name} built successfully")

    def setup_containers(self):
        """Setup and verify containers for env comparison."""
        self.logger.info("=" * 70)
        self.logger.info("Phase 1: Container Setup")
        self.logger.info("=" * 70)

        # 1. Setup base container (create if not exists, start if not running)
        # Use check=False to handle race condition when multiple processes try to create the same container
        if not self._container_exists(self.base_container):
            # Check if base image exists
            if not self._image_exists(self.base_image):
                base_dockerfile = self.env_data_dir / "base" / "Dockerfile"
                if not base_dockerfile.exists():
                    raise FileNotFoundError(f"Base Dockerfile not found: {base_dockerfile}")
                self._build_image(self.base_image, base_dockerfile)

            # Create base container
            self.logger.info(f"Creating base container '{self.base_container}'...")
            self._run_docker_cmd(["docker", "create", "--name", self.base_container, self.base_image, "/bin/bash"])
            self.logger.info(f"✓ Base container '{self.base_container}' created")

        # Start base container if not running
        if not self._container_running(self.base_container):
            self.logger.info(f"Starting base container '{self.base_container}'...")
            self._run_docker_cmd(["docker", "start", self.base_container])
            self.logger.info(f"✓ Base container '{self.base_container}' started")
        else:
            self.logger.info(f"✓ Base container '{self.base_container}' is running")

        # 2. Setup milestone container
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
        self._run_docker_cmd(
            [
                "docker",
                "run",
                "-d",
                "--init",
                "--name",
                self.milestone_container,
                self.milestone_image,
                "tail",
                "-f",
                "/dev/null",
            ]
        )
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

    def render_prompts(self) -> dict[str, str]:
        """Render all prompt templates and return their content."""
        self.logger.info("\n" + "=" * 70)
        self.logger.info("Phase 2: Render Prompt Templates")
        self.logger.info("=" * 70)

        prompts = {}

        # 1. Render srs_gen_v1 prompt (reuse SRSPromptPreparer)
        self.logger.info(f"Rendering SRS generation prompt for {self.milestone_id}...")

        # Construct patches_dir from repo_name: DATA/github_data/preprocessed/{repo_name}/patches
        data_root = Path(__file__).parent.parent.parent / "DATA"
        patches_dir_path = data_root / "github_data" / "preprocessed" / self.repo_name / "patches"
        patches_dir = str(patches_dir_path) if patches_dir_path.exists() else None

        preparer = SRSPromptPreparer(
            work_dir=str(self.work_dir),
            milestone_id=self.milestone_id,
            filter_src_only=False,
            patches_dir=patches_dir,
            output_dir=str(self.output_dir),
            project_root=str(self.project_root),
            git_repo=str(self.git_repo),
            milestone_container=self.milestone_container,
            include_new_tests=self.include_new_tests,
            suffix=self.suffix,
        )
        prompts["srs_gen"] = preparer.generate_output()
        srs_gen_path = self.output_dir / "prompt_srs_gen.md"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        srs_gen_path.write_text(prompts["srs_gen"], encoding="utf-8")
        self.logger.info(f"✓ Saved to: {srs_gen_path}")

        # 2. Render acceptance_test_align prompt
        self.logger.info("Rendering acceptance test alignment prompt...")
        acceptance_template = Template(self.acceptance_tmpl.read_text(encoding="utf-8"))

        # Use relative paths from project root (with fallback to absolute if not relative)
        try:
            srs_path_rel = (self.output_dir / "SRS.md").relative_to(self.project_root)
        except ValueError:
            srs_path_rel = self.output_dir / "SRS.md"

        # Use classification file instead of f2p_tests metadata
        classification_file = (
            self.test_data_dir / self.milestone_id / "benchmark" / f"{self.milestone_id}_classification.json"
        )
        try:
            test_metadata_rel = classification_file.relative_to(self.project_root)
        except ValueError:
            test_metadata_rel = classification_file

        # Get f2p data from preparer for acceptance template
        try:
            f2p_tests_list_file_rel = preparer.f2p_tests_list_file.relative_to(self.project_root)
        except ValueError:
            f2p_tests_list_file_rel = preparer.f2p_tests_list_file

        # Prepare f2p_tests_preview based on show_all_tests
        if preparer.show_all_tests:
            f2p_tests_preview = preparer.f2p_tests
        else:
            f2p_tests_preview = []

        prompts["acceptance"] = acceptance_template.render(
            srs_path=str(srs_path_rel),
            start_commit=f"milestone-{self.milestone_id}-start",
            end_commit=f"milestone-{self.milestone_id}-end",
            test_metadata=str(test_metadata_rel),
            milestone_container=self.milestone_container,
            f2p_tests_list_file=str(f2p_tests_list_file_rel),
            f2p_tests_preview=f2p_tests_preview,
            f2p_test_count=len(preparer.f2p_tests),
            show_all_tests=preparer.show_all_tests,
        )
        acceptance_path = self.output_dir / "prompt_acceptance_align.md"
        acceptance_path.write_text(prompts["acceptance"], encoding="utf-8")
        self.logger.info(f"✓ Saved to: {acceptance_path}")

        # 3. Render env_spec prompt
        self.logger.info("Rendering environment specification prompt...")
        base_dockerfile = self.env_data_dir / "base" / "Dockerfile"
        milestone_dockerfile = self.env_data_dir / self.milestone_id / "Dockerfile"

        env_spec_template = Template(self.env_spec_tmpl.read_text(encoding="utf-8"))
        prompts["env_spec"] = env_spec_template.render(
            base_dockerfile=str(base_dockerfile),
            milestone_dockerfile=str(milestone_dockerfile),
            base_container=self.base_container,
            milestone_container=self.milestone_container,
            git_repo=str(self.git_repo),
            output_file=str(self.output_dir / "env_dependencies.md"),
        )
        env_spec_path = self.output_dir / "prompt_env_spec.md"
        env_spec_path.write_text(prompts["env_spec"], encoding="utf-8")
        self.logger.info(f"✓ Saved to: {env_spec_path}")

        return prompts

    def _call_claude(
        self, prompt: str, workspace: Path, session_prefix: str, timeout_ms: int = 600_000
    ) -> tuple[bool, str]:
        """Call Claude Code CLI non-interactively.

        Args:
            prompt: The prompt text to send to Claude
            workspace: Working directory for Claude
            session_prefix: Prefix for session ID (for logging)
            timeout_ms: Timeout in milliseconds

        Returns:
            Tuple of (success: bool, output: str)
        """
        session_id = str(uuid.uuid4())
        self.logger.info(f"  Session ID: {session_id[:8]}...")

        cmd = [
            "claude",
            "--model",
            self.model,
            "--effort",
            "max",
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
            "--session-id",
            session_id,
        ]

        try:
            # Write prompt to temp file for debugging
            prompt_file = self.temp_dir / f"{session_prefix}_prompt.txt"
            prompt_file.write_text(prompt, encoding="utf-8")

            # Run Claude
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(workspace),
                text=True,
            )

            try:
                stdout, stderr = proc.communicate(input=prompt, timeout=timeout_ms / 1000.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                self.logger.error(f"Claude execution timed out after {timeout_ms}ms")
                return False, ""

            # Save outputs for debugging
            (self.temp_dir / f"{session_prefix}_stdout.txt").write_text(stdout or "", encoding="utf-8")
            (self.temp_dir / f"{session_prefix}_stderr.txt").write_text(stderr or "", encoding="utf-8")

            if proc.returncode != 0:
                self.logger.error(f"Claude exited with code {proc.returncode}")
                self.logger.error(f"stderr: {stderr[:500]}")
                return False, ""

            # Parse JSON output
            try:
                result = json.loads(stdout)
                output_text = result.get("content", "")
                return True, output_text
            except json.JSONDecodeError:
                self.logger.warning("Failed to parse JSON output, using raw stdout")
                return True, stdout

        except Exception as e:
            self.logger.error(f"Failed to execute Claude: {e}")
            return False, ""

    def generate_srs(self, prompts: dict[str, str]):
        """Generate SRS using Claude Code in 3 stages."""
        self.logger.info("\n" + "=" * 70)
        self.logger.info("Phase 3: Generate SRS with Claude Code")
        self.logger.info("=" * 70)

        srs_path = self.output_dir / "SRS.md"

        # Stage 1: Generate initial SRS
        self.logger.info("\nStage 1: Generating initial SRS...")
        # Use project_root as workspace so relative paths in prompt work correctly
        success, output = self._call_claude(prompts["srs_gen"], self.project_root, "stage1_srs_gen")

        if not success:
            raise RuntimeError("Failed to generate initial SRS")

        # Check if SRS.md was created
        if not srs_path.exists():
            self.logger.error("SRS.md not found after Stage 1")
            raise RuntimeError("Claude did not create SRS.md file")

        self.logger.info(f"✓ Initial SRS generated: {srs_path}")
        self.logger.info(f"  Size: {srs_path.stat().st_size} bytes")

        # Stage 2: Refine acceptance criteria
        self.logger.info("\nStage 2: Refining acceptance criteria...")
        success, output = self._call_claude(prompts["acceptance"], self.project_root, "stage2_acceptance")

        if not success:
            self.logger.warning("Failed to refine acceptance criteria (continuing anyway)")
        else:
            self.logger.info("✓ Acceptance criteria refined")

        # Stage 3: Generate environment dependencies
        self.logger.info("\nStage 3: Generating environment dependencies...")
        success, output = self._call_claude(prompts["env_spec"], self.project_root, "stage3_env_spec")

        if not success:
            self.logger.warning("Failed to generate environment spec (continuing anyway)")
        else:
            # Check both temp_dir and output_dir for env_dependencies.md
            env_deps_file = self.temp_dir / "env_dependencies.md"
            if not env_deps_file.exists():
                env_deps_file = self.output_dir / "env_dependencies.md"

            if env_deps_file.exists():
                # Append to SRS.md
                env_content = env_deps_file.read_text(encoding="utf-8")
                with open(srs_path, "a", encoding="utf-8") as f:
                    f.write("\n\n---\n\n")
                    f.write(env_content)
                self.logger.info("✓ Environment dependencies appended to SRS")
            else:
                self.logger.warning("Environment dependencies file not found")

    def cleanup(self):
        """Cleanup temporary resources."""
        self.logger.info("\n" + "=" * 70)
        self.logger.info("Phase 4: Cleanup")
        self.logger.info("=" * 70)

        # Remove milestone container (base container is kept for reuse)
        if self._container_exists(self.milestone_container):
            self.logger.info(f"Removing milestone container '{self.milestone_container}'...")
            self._run_docker_cmd(["docker", "rm", "-f", self.milestone_container], check=False)
            self.logger.info("✓ Milestone container removed")

        self.logger.info(f"✓ Base container '{self.base_container}' kept for reuse")

        # Log files saved in output directory
        self.logger.info(f"Log files saved in: {self.temp_dir}")

    def run(self):
        """Execute complete SRS generation pipeline."""
        try:
            self.logger.info("\n" + "=" * 70)
            self.logger.info(f"SRS Generation Pipeline: {self.milestone_id}")
            self.logger.info(f"Repository: {self.repo_name}")
            self.logger.info(f"Trial: {self.trial_name}")
            self.logger.info("=" * 70)

            # Phase 1: Setup containers
            self.setup_containers()

            # Phase 2: Render prompts
            prompts = self.render_prompts()

            # Phase 3: Generate SRS
            self.generate_srs(prompts)

            # Phase 4: Cleanup
            self.cleanup()

            # Success
            self.logger.info("\n" + "=" * 70)
            self.logger.info("✓ SRS GENERATION COMPLETED SUCCESSFULLY")
            self.logger.info("=" * 70)
            self.logger.info(f"Output: {self.output_dir / 'SRS.md'}")
            self.logger.info(f"Prompts: {self.output_dir}")
            self.logger.info(f"Logs: {self.temp_dir}")

        except Exception as e:
            self.logger.error(f"\n✗ Pipeline failed: {e}")
            self.cleanup()
            sys.exit(1)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate complete SRS document using Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Minimal usage (trial-name auto-increments: v1, v2, v3, ...):
  %(prog)s \\
    --repo-name urllib3_urllib3_2.0.6_2.3.0 \\
    --milestone-version test_multi_stage_V2 \\
    --milestone-id M001

  # With explicit trial name and force flag:
  %(prog)s \\
    --repo-name urllib3_urllib3_2.0.6_2.3.0 \\
    --milestone-version test_multi_stage_V2 \\
    --milestone-id M001 \\
    --trial-name v2 \\
    --force

  # Batch run multiple milestones:
  for mid in M001 M002 M003; do
    %(prog)s --repo-name urllib3_urllib3_2.0.6_2.3.0 \\
      --milestone-version test_multi_stage_V2 --milestone-id $mid --force
  done
        """,
    )

    parser.add_argument("--repo-name", required=True, help="Repository name (e.g., urllib3_urllib3_2.0.6_2.3.0)")
    parser.add_argument("--milestone-version", required=True, help="Milestone version (e.g., test_multi_stage_V2)")
    parser.add_argument("--milestone-id", required=True, help="Milestone ID (e.g., M001)")
    parser.add_argument(
        "--base-image",
        help="Docker image name for base environment (auto-generated if not specified)",
    )
    parser.add_argument(
        "--milestone-image",
        help="Docker image name for milestone environment (auto-generated if not specified)",
    )
    parser.add_argument(
        "--trial-name",
        help="Trial version (e.g., v1, v2). If not specified, auto-increments from existing trials",
    )
    parser.add_argument(
        "--git-repo",
        help="Path to git repository (testbed). Auto-inferred from workspace if not specified",
    )
    parser.add_argument("--force", action="store_true", help="Force recreate containers if they exist")
    parser.add_argument("--workspace-root", help="Override workspace root directory")
    parser.add_argument(
        "--no-include-new-tests",
        action="store_true",
        help="Do NOT include new_tests (none_to_pass) as fail_to_pass tests (default: include them)",
    )
    parser.add_argument(
        "--suffix",
        help="Suffix for data files (e.g., '_filtered' -> 'milestones_filtered.csv')",
    )
    parser.add_argument(
        "--model",
        default="claude-opus-4-6",
        help="Claude model to use (default: claude-opus-4-6)",
    )

    args = parser.parse_args()

    # Auto-generate base_image and milestone_image if not provided
    # Docker image names must be lowercase
    # Format for base: {repo_name_lowercase}/base (shared across milestone versions)
    # Format for milestone: {repo_name_lowercase}/{milestone_version_lowercase}/{milestone_id_lowercase}
    if not args.base_image:
        args.base_image = f"{args.repo_name.lower()}/base"
        print(f"Auto-generated base-image: {args.base_image}")

    if not args.milestone_image:
        args.milestone_image = f"{args.repo_name.lower()}/{args.milestone_version.lower()}/{args.milestone_id.lower()}"
        print(f"Auto-generated milestone-image: {args.milestone_image}")

    # Auto-infer git_repo if not provided
    # Default to DATA/harness_workspace/{repo_name}/{milestone_version}/testbed
    if not args.git_repo:
        data_root = Path(__file__).parent.parent.parent / "DATA" / "harness_workspace"
        args.git_repo = str(data_root / args.repo_name / args.milestone_version / "testbed")
        print(f"Auto-inferred git-repo: {args.git_repo}")

    # Auto-increment trial_name if not provided
    if not args.trial_name:
        if args.workspace_root:
            workspace_root = Path(args.workspace_root).resolve()
        else:
            workspace_root = Path(__file__).parent.parent.parent / "DATA" / "harness_workspace"
        srs_dir = workspace_root / args.repo_name / args.milestone_version / "srs"
        args.trial_name = get_next_trial_name(srs_dir, args.milestone_id)
        print(f"Auto-selected trial-name: {args.trial_name}")

    # Create generator and run
    generator = SRSGenerator(
        repo_name=args.repo_name,
        milestone_version=args.milestone_version,
        milestone_id=args.milestone_id,
        base_image=args.base_image,
        milestone_image=args.milestone_image,
        trial_name=args.trial_name,
        git_repo=args.git_repo,
        force=args.force,
        workspace_root=args.workspace_root,
        include_new_tests=not args.no_include_new_tests,
        suffix=args.suffix,
        model=args.model,
    )

    generator.run()


if __name__ == "__main__":
    main()
