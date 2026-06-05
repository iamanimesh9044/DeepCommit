#!/usr/bin/env python3
"""
Build Docker image for any commit using Claude agent.

This script:
1. Generates or reads an environment setup prompt
2. Runs Claude agent to execute the prompt (create Dockerfile, build image, run tests)
3. Validates the results

Supports three target types:
- base: Build image for start_version (beginning of version range)
- final: Build image for end_version (end of version range)
- <commit_sha>: Build image for any specific commit

Usage:
    # Build base image
    # Output structure: DATA/harness_workspace/{config_name}/base/dockerfiles/, base/test_results/
    python build_image.py \
        --config config/urllib3_urllib3_2.0.6_2.3.0.yaml \
        --target base \
        --repo-path DATA/github_data/repos/urllib3_urllib3

    # Build final image
    # Output structure: DATA/harness_workspace/{config_name}/final/dockerfiles/, final/test_results/
    python build_image.py \
        --config config/urllib3_urllib3_2.0.6_2.3.0.yaml \
        --target final \
        --repo-path DATA/github_data/repos/urllib3_urllib3

    # Build image for specific commit (must specify --output-dir)
    # Output structure: {output-dir}/dockerfiles/{commit}/, {output-dir}/test_results/{commit}/
    python build_image.py \
        --config config/urllib3_urllib3_2.0.6_2.3.0.yaml \
        --target abc1234 \
        --repo-path DATA/github_data/repos/urllib3_urllib3 \
        --output-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/commits

    # Dry run (only generate prompt)
    python build_image.py \
        --config config/urllib3_urllib3_2.0.6_2.3.0.yaml \
        --target base \
        --repo-path DATA/github_data/repos/urllib3_urllib3 \
        --dry-run
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import yaml

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Special target names
TARGET_BASE = "base"
TARGET_FINAL = "final"


@dataclass
class AgentResult:
    """Result from Claude agent execution."""

    success: bool
    output: str
    session_id: str
    duration_ms: int = 0
    exit_code: int = 0
    error: Optional[str] = None


@dataclass
class TargetInfo:
    """Information about the build target."""

    name: str  # 'base', 'final', or commit sha
    sha: str  # Actual commit SHA
    version: str  # Version string for display
    description: str  # Human-readable description


def parse_versions_from_config_path(config_path: Path) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse start_version and end_version from config filename.

    Expected format: {owner}_{repo}_{start_version}_{end_version}.yaml
    Examples:
        - urllib3_urllib3_2.0.6_2.3.0.yaml -> (2.0.6, 2.3.0)
        - navidrome_navidrome_v0.57.0_v0.58.0.yaml -> (v0.57.0, v0.58.0)
        - apache_dubbo_dubbo-3.3.3_dubbo-3.3.6.yaml -> (dubbo-3.3.3, dubbo-3.3.6)

    Returns:
        Tuple of (start_version, end_version) or (None, None) if parsing fails
    """
    filename = config_path.stem  # Remove .yaml extension

    # Pattern: {owner}_{repo}_{version1}_{version2}
    match = re.match(r"^(.+?)_(.+?)_([a-zA-Z-]*[\d.]+[a-zA-Z\d.-]*)_([a-zA-Z-]*[\d.]+[a-zA-Z\d.-]*)$", filename)

    if match:
        start_version = match.group(3)
        end_version = match.group(4)
        return start_version, end_version

    return None, None


def get_default_output_dir(config_path: Path) -> Path:
    """
    Get default output directory for base/final targets.

    Args:
        config_path: Path to config file (e.g., config/urllib3_urllib3_2.0.6_2.3.0.yaml)

    Returns:
        Path like DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/
    """
    config_name = config_path.stem  # e.g., urllib3_urllib3_2.0.6_2.3.0
    return Path("DATA/harness_workspace") / config_name


def resolve_target_sha(
    repo_path: Path,
    target: str,
    start_version: str,
    end_version: str,
    main_branch: str,
) -> TargetInfo:
    """
    Resolve target to actual commit SHA.

    Args:
        repo_path: Path to git repository
        target: 'base', 'final', or a commit SHA
        start_version: Start version string
        end_version: End version string
        main_branch: Main branch name

    Returns:
        TargetInfo with resolved SHA and metadata
    """
    from .render_env_prompt import compute_base_sha, compute_final_sha

    if target == TARGET_BASE:
        # Compute base SHA from start_version using branch-out point
        sha = compute_base_sha(repo_path, start_version, main_branch, end_version)
        return TargetInfo(
            name=TARGET_BASE,
            sha=sha,
            version=start_version,
            description=f"Base image for {start_version} (start of version range)",
        )

    elif target == TARGET_FINAL:
        # Compute final SHA from end_version using branch-out point
        sha = compute_final_sha(repo_path, end_version, main_branch)
        return TargetInfo(
            name=TARGET_FINAL,
            sha=sha,
            version=end_version,
            description=f"Final image for {end_version} (end of version range)",
        )

    else:
        # Treat target as commit SHA - validate it exists
        result = subprocess.run(
            f"git rev-parse {target}^{{commit}}",
            shell=True,
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(f"Could not resolve '{target}' to a valid commit: {result.stderr}")

        sha = result.stdout.strip()
        short_sha = sha[:7]
        return TargetInfo(
            name=short_sha,
            sha=sha,
            version=short_sha,
            description=f"Image for commit {short_sha}",
        )


class ClaudeAgentRunner:
    """Run Claude CLI agent for image building."""

    def __init__(
        self,
        workspace: Path,
        log_dir: Optional[Path] = None,
        timeout_ms: int = 1_800_000,  # 30 minutes default
        model: str = "claude-opus-4-6",
    ):
        self.workspace = Path(workspace)
        self.log_dir = Path(log_dir) if log_dir else None
        self.timeout_ms = timeout_ms
        self.model = model

        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    def run(self, prompt: str, task_name: str = "build_image") -> AgentResult:
        """Run Claude CLI with the given prompt."""
        start_time = time.time()
        session_id = str(uuid.uuid4())
        logger.info(f"Starting Claude session: {session_id[:8]}...")

        cmd = [
            "claude",
            "-p",
            prompt,
            "--model",
            self.model,
            "--effort",
            "max",
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.workspace),
                text=True,
            )

            try:
                stdout, stderr = proc.communicate(timeout=self.timeout_ms / 1000.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                duration_ms = int((time.time() - start_time) * 1000)

                self._save_logs(
                    task_name, cmd, session_id, -1, duration_ms, stdout or "", stderr or "", prompt, "timeout"
                )

                logger.error(f"Claude execution timed out after {self.timeout_ms}ms")
                return AgentResult(
                    success=False,
                    output="",
                    session_id=session_id,
                    duration_ms=duration_ms,
                    exit_code=-1,
                    error=f"Timeout after {self.timeout_ms}ms",
                )

            duration_ms = int((time.time() - start_time) * 1000)
            status = "ok" if proc.returncode == 0 else "error"
            self._save_logs(
                task_name, cmd, session_id, proc.returncode, duration_ms, stdout or "", stderr or "", prompt, status
            )

            if proc.returncode != 0:
                logger.error(f"Claude exited with code {proc.returncode}")
                return AgentResult(
                    success=False,
                    output="",
                    session_id=session_id,
                    duration_ms=duration_ms,
                    exit_code=proc.returncode,
                    error=f"Exit code {proc.returncode}: {stderr[:200] if stderr else 'unknown error'}",
                )

            # Parse JSON output
            try:
                result = json.loads(stdout)
                output_text = result.get("result", "")
                return AgentResult(
                    success=True, output=output_text, session_id=session_id, duration_ms=duration_ms, exit_code=0
                )
            except json.JSONDecodeError:
                return AgentResult(
                    success=True, output=stdout, session_id=session_id, duration_ms=duration_ms, exit_code=0
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
            )
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"Failed to execute Claude: {e}")
            return AgentResult(
                success=False, output="", session_id=session_id, duration_ms=duration_ms, exit_code=1, error=str(e)
            )

    def _save_logs(self, task_name, cmd, session_id, exit_code, duration_ms, stdout, stderr, prompt, status):
        """Save structured agent log file."""
        if not self.log_dir:
            return

        log_content = f"""$ {" ".join(cmd[:5])}... (prompt truncated)
SESSION_ID={session_id}
STATUS={status}
EXIT={exit_code}
DURATION={duration_ms}ms

==== PROMPT ====
{prompt[:2000]}{"...(truncated)" if len(prompt) > 2000 else ""}

==== STDOUT ====
{stdout}

==== STDERR ====
{stderr}
"""
        log_file = self.log_dir / f"{task_name}.log"
        log_file.write_text(log_content, encoding="utf-8")
        logger.debug(f"Saved agent log to: {log_file}")


def generate_prompt(
    repo_path: Path,
    config_path: Path,
    target_info: TargetInfo,
    dockerfile_dir: Path,
    test_results_dir: Path,
    start_version: str,
    end_version: str,
    main_branch: str,
    repo_src_dirs: list,
    test_dirs: list,
    exclude_patterns: list,
    template_path: Optional[Path] = None,
) -> Tuple[bool, str, Path]:
    """
    Generate environment setup prompt.

    Returns:
        Tuple of (success, error_message_or_prompt, prompt_file_path)
    """
    from .render_env_prompt import render_env_setup_prompt

    # Use default template if not provided
    if template_path is None:
        template_path = Path(__file__).parent / "prompts" / "env_setup_prompt.tmpl"

    if not template_path.exists():
        # Try fallback to old template name
        fallback = Path(__file__).parent / "prompts" / "base_env_setup_prompt.tmpl"
        if fallback.exists():
            template_path = fallback
        else:
            return False, f"Template not found: {template_path}", Path()

    # Get config_name from config_path for image naming
    config_name = config_path.stem  # e.g., navidrome_navidrome_v0.57.0_v0.58.0

    # Render prompt
    result = render_env_setup_prompt(
        repo_path=repo_path,
        target_name=target_info.name,
        target_sha=target_info.sha,
        target_description=target_info.description,
        start_version=start_version,
        end_version=end_version,
        repo_src_dirs=repo_src_dirs,
        test_dirs=test_dirs,
        exclude_patterns=exclude_patterns,
        dockerfile_dir=dockerfile_dir,
        test_results_dir=test_results_dir,
        template_path=template_path,
        config_name=config_name,
    )

    if result != 0:
        return False, "Failed to render prompt", Path()

    prompt_file = dockerfile_dir / "env_setup_prompt.md"
    if not prompt_file.exists():
        # Try old name for compatibility
        prompt_file = dockerfile_dir / "base_env_setup_prompt.md"

    if not prompt_file.exists():
        return False, f"Prompt file not created in {dockerfile_dir}", Path()

    prompt_content = prompt_file.read_text()
    return True, prompt_content, prompt_file


def run_build_agent(
    workspace: Path,
    prompt: str,
    log_dir: Optional[Path] = None,
    timeout_ms: int = 1_800_000,
    model: str = "claude-opus-4-6",
    task_name: str = "build_image",
) -> AgentResult:
    """Run Claude agent to build image."""
    runner = ClaudeAgentRunner(
        workspace=workspace,
        log_dir=log_dir,
        timeout_ms=timeout_ms,
        model=model,
    )
    return runner.run(prompt, task_name=task_name)


def validate_build_result(dockerfile_dir: Path) -> Tuple[bool, str]:
    """Validate the build result."""
    dockerfile = dockerfile_dir / "Dockerfile"
    if not dockerfile.exists():
        return False, f"Dockerfile not found: {dockerfile}"

    test_config = dockerfile_dir / "test_config.json"
    if not test_config.exists():
        return False, f"test_config.json not found: {test_config}"

    build_command_file = dockerfile_dir / "build_command.txt"
    if not build_command_file.exists():
        return False, f"build_command.txt not found: {build_command_file}"

    return True, "Validation passed (Dockerfile, test_config.json, and build_command.txt exist)"


def _build_retry_prompt(original_prompt: str, error_message: str) -> str:
    """
    Build a retry prompt that includes the original prompt and error context.

    Args:
        original_prompt: The original prompt sent to the agent
        error_message: The error message from the failed attempt

    Returns:
        A new prompt with error context prepended
    """
    retry_context = f"""
## ⚠️ RETRY ATTEMPT - Previous attempt failed

The previous attempt to build and validate the image failed with the following error:

```
{error_message}
```

Please analyze the error above and fix the issue. The original task follows below.

---

"""
    return retry_context + original_prompt


def main():
    parser = argparse.ArgumentParser(
        description="Build Docker image for any commit using Claude agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build base image
  # Output: DATA/harness_workspace/{config_name}/base/dockerfiles/, base/test_results/
  python build_image.py \\
    --config config/urllib3_urllib3_2.0.6_2.3.0.yaml \\
    --target base \\
    --repo-path DATA/github_data/repos/urllib3_urllib3

  # Build final image
  # Output: DATA/harness_workspace/{config_name}/final/dockerfiles/, final/test_results/
  python build_image.py \\
    --config config/urllib3_urllib3_2.0.6_2.3.0.yaml \\
    --target final \\
    --repo-path DATA/github_data/repos/urllib3_urllib3

  # Build image for specific commit (must specify --output-dir)
  # Output: {output-dir}/dockerfiles/{commit}/, {output-dir}/test_results/{commit}/
  python build_image.py \\
    --config config/urllib3_urllib3_2.0.6_2.3.0.yaml \\
    --target abc1234 \\
    --repo-path DATA/github_data/repos/urllib3_urllib3 \\
    --output-dir DATA/harness_workspace/xxx/commits
        """,
    )

    # Required arguments
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="YAML config file path (e.g., config/urllib3_urllib3_2.0.6_2.3.0.yaml)",
    )
    parser.add_argument(
        "--target",
        type=str,
        required=True,
        help="Target: 'base', 'final', or a commit SHA",
    )

    # Prompt source
    prompt_group = parser.add_mutually_exclusive_group()
    prompt_group.add_argument(
        "--prompt-file",
        type=Path,
        help="Use existing prompt file instead of generating",
    )
    prompt_group.add_argument(
        "--repo-path",
        type=Path,
        help="Repository path (required for prompt generation)",
    )

    # Output directory
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory. Required for commit SHA targets. "
        "For 'base'/'final', defaults to DATA/harness_workspace/{config_name}/ "
        "with structure {target}/dockerfiles/ and {target}/test_results/",
    )

    # Optional version overrides
    parser.add_argument("--start-version", type=str, help="Override start_version from config filename")
    parser.add_argument("--end-version", type=str, help="Override end_version from config filename")
    parser.add_argument("--main-branch", type=str, help="Main branch name (default: from config or 'main')")

    # Optional config overrides
    parser.add_argument("--repo-src-dirs", type=str, nargs="*", help="Source directories (overrides config)")
    parser.add_argument("--test-dirs", type=str, nargs="*", help="Test directories (overrides config)")
    parser.add_argument("--exclude-patterns", type=str, nargs="*", help="Exclude patterns (overrides config)")
    parser.add_argument("--template", type=Path, help="Prompt template path")

    # Language and test framework (for validation)
    parser.add_argument(
        "--language",
        type=str,
        help="Programming language (default: from config or 'python'). Options: python, java, javascript, go, rust",
    )
    parser.add_argument(
        "--test-framework",
        type=str,
        help="Test framework (default: from config or 'pytest'). "
        "Options: pytest, unittest, maven, gradle, jest, mocha, go_test, cargo",
    )

    # Agent execution options
    parser.add_argument("--agent-cwd", type=Path, help="Working directory for agent (default: current directory)")
    parser.add_argument("--log-dir", type=Path, help="Directory to save agent logs")
    parser.add_argument("--model", type=str, default="claude-opus-4-6", help="Claude model (default: claude-opus-4-6)")
    parser.add_argument("--timeout", type=int, default=1800, help="Timeout in seconds (default: 1800)")
    parser.add_argument("--skip-validation", action="store_true", help="Skip validation after agent completes")
    parser.add_argument("--max-retries", type=int, default=3, help="Maximum retry attempts on failure (default: 3)")
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Force rebuild even if final image already exists",
    )

    args = parser.parse_args()

    # Validate config file
    if not args.config.exists():
        logger.error(f"Config file not found: {args.config}")
        sys.exit(1)

    # Load config
    logger.info(f"Loading config from: {args.config}")
    with open(args.config) as f:
        config = yaml.safe_load(f) or {}

    # Parse versions from config filename
    inferred_start, inferred_end = parse_versions_from_config_path(args.config)
    start_version = args.start_version or inferred_start
    end_version = args.end_version or inferred_end

    if not start_version or not end_version:
        logger.error("Could not determine versions from config filename. Use --start-version and --end-version.")
        sys.exit(1)

    logger.info(f"Version range: {start_version} -> {end_version}")

    # Determine output directory
    target = args.target.lower()
    is_special_target = target in (TARGET_BASE, TARGET_FINAL)

    if args.output_dir:
        output_dir = args.output_dir.resolve()
        # For user-specified output_dir, use original structure: dockerfiles/{target}, test_results/{target}
        dockerfile_dir = output_dir / "dockerfiles" / target
        test_results_dir = output_dir / "test_results" / target
    elif is_special_target:
        output_dir = get_default_output_dir(args.config).resolve()
        logger.info(f"Using default output directory: {output_dir}")
        # For base/final targets, use structure: {target}/dockerfiles, {target}/test_results
        dockerfile_dir = output_dir / target / "dockerfiles"
        test_results_dir = output_dir / target / "test_results"
    else:
        logger.error(f"--output-dir is required for commit SHA target '{args.target}'")
        sys.exit(1)

    # Set up log directory
    log_dir = args.log_dir or output_dir / "_agent_logs"

    dockerfile_dir.mkdir(parents=True, exist_ok=True)
    test_results_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Get config values
    main_branch = args.main_branch or config.get("main_branch") or "main"
    repo_src_dirs = args.repo_src_dirs if args.repo_src_dirs is not None else config.get("repo_src_dirs", [])
    test_dirs = args.test_dirs if args.test_dirs is not None else config.get("test_dirs", ["test"])
    exclude_patterns = args.exclude_patterns if args.exclude_patterns is not None else config.get("exclude", [])
    language = args.language or config.get("language") or "python"
    test_framework = args.test_framework or config.get("test_framework") or "pytest"

    # Get or generate prompt
    if args.prompt_file:
        if not args.prompt_file.exists():
            logger.error(f"Prompt file not found: {args.prompt_file}")
            sys.exit(1)
        prompt = args.prompt_file.read_text()
        prompt_file = args.prompt_file
        logger.info(f"Using existing prompt: {prompt_file}")

        # Still need to resolve target for logging
        if args.repo_path:
            try:
                target_info = resolve_target_sha(args.repo_path, args.target, start_version, end_version, main_branch)
            except ValueError as e:
                logger.warning(f"Could not resolve target SHA: {e}")
                target_info = TargetInfo(name=target, sha="unknown", version=target, description=f"Target: {target}")
        else:
            target_info = TargetInfo(name=target, sha="unknown", version=target, description=f"Target: {target}")
    else:
        # Generate prompt - need repo_path
        if not args.repo_path:
            logger.error("Either --prompt-file or --repo-path must be provided")
            sys.exit(1)

        if not args.repo_path.exists():
            logger.error(f"Repository path not found: {args.repo_path}")
            sys.exit(1)

        # Resolve target to SHA
        logger.info(f"Resolving target '{args.target}'...")
        try:
            target_info = resolve_target_sha(args.repo_path, args.target, start_version, end_version, main_branch)
        except ValueError as e:
            logger.error(f"Failed to resolve target: {e}")
            sys.exit(1)

        logger.info(f"Target: {target_info.description}")
        logger.info(f"SHA: {target_info.sha[:10]}")

        # Generate prompt
        logger.info("Generating prompt...")
        success, result, prompt_file = generate_prompt(
            repo_path=args.repo_path,
            config_path=args.config,
            target_info=target_info,
            dockerfile_dir=dockerfile_dir,
            test_results_dir=test_results_dir,
            start_version=start_version,
            end_version=end_version,
            main_branch=main_branch,
            repo_src_dirs=repo_src_dirs,
            test_dirs=test_dirs,
            exclude_patterns=exclude_patterns,
            template_path=args.template,
        )

        if not success:
            logger.error(f"Failed to generate prompt: {result}")
            sys.exit(1)

        prompt = result
        logger.info(f"Prompt generated: {prompt_file}")

    # Generate image names
    config_name = args.config.stem
    temp_image_name = f"{config_name}/{target_info.name}-temp:latest".lower()
    final_image_name = f"{config_name}/{target_info.name}:latest".lower()

    logger.info("Image names:")
    logger.info(f"  Temp image: {temp_image_name}")
    logger.info(f"  Final image: {final_image_name}")

    # Check if final image already exists
    check_final_exists = subprocess.run(
        f"docker image inspect {final_image_name}",
        shell=True,
        capture_output=True,
        text=True,
    )
    final_image_exists = check_final_exists.returncode == 0

    # Prepare validation command helper
    def run_validation(image_name: str) -> tuple[bool, str]:
        """Run validate_image.py on the given image. Returns (success, error_message)."""
        logger.info("Validating image with validate_image.py...")
        logger.info(f"  Image: {image_name}")
        logger.info(f"  Language: {language}, Test framework: {test_framework}")
        test_dirs_str = " ".join(test_dirs)
        validate_cmd = [
            sys.executable,
            str(Path(__file__).parent / "validate_image.py"),
            "--image",
            image_name,
            "--test-dirs",
            test_dirs_str,
            "--language",
            language,
            "--test-framework",
            test_framework,
        ]

        # Set PYTHONPATH to include the project root
        env = os.environ.copy()
        project_root = Path(__file__).parent.parent.parent.parent
        env["PYTHONPATH"] = str(project_root) + ":" + env.get("PYTHONPATH", "")

        validate_result = subprocess.run(
            validate_cmd,
            capture_output=True,
            text=True,
            env=env,
        )

        if validate_result.returncode != 0:
            error_msg = f"Image validation failed:\n{validate_result.stdout}\n{validate_result.stderr}"
            return False, error_msg
        return True, ""

    # If final image exists and not force-rebuild, try validation first
    skip_agent_build = False
    if final_image_exists and not args.force_rebuild:
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Final image already exists: {final_image_name}")
        logger.info("Attempting validation without rebuilding...")
        logger.info(f"{'=' * 60}")

        valid, error_msg = run_validation(final_image_name)
        if valid:
            logger.info("Validation passed! Skipping agent build.")
            skip_agent_build = True
        else:
            logger.warning(f"Existing image failed validation: {error_msg[:500]}")
            logger.info("Will proceed with agent rebuild...")

    # Run Claude agent with retry logic (if not skipping)
    if not skip_agent_build:
        agent_cwd = args.agent_cwd or Path.cwd()
        max_retries = args.max_retries
        current_prompt = prompt
        last_error = None

        for attempt in range(1, max_retries + 1):
            logger.info(f"\n{'=' * 60}")
            logger.info(f"Attempt {attempt}/{max_retries}")
            logger.info(f"{'=' * 60}")

            # Run Claude agent
            logger.info("Running Claude agent...")
            logger.info(f"  Model: {args.model}")
            logger.info(f"  Timeout: {args.timeout}s")
            logger.info(f"  Agent CWD: {agent_cwd}")

            result = run_build_agent(
                workspace=agent_cwd,
                prompt=current_prompt,
                log_dir=log_dir,
                timeout_ms=args.timeout * 1000,
                model=args.model,
                task_name=f"build_{target_info.name}_attempt{attempt}",
            )

            if not result.success:
                last_error = f"Agent execution failed: {result.error}"
                logger.error(f"Agent failed: {result.error}")
                logger.error(f"Session ID: {result.session_id}")
                logger.error(f"Duration: {result.duration_ms}ms")

                if attempt < max_retries:
                    current_prompt = _build_retry_prompt(prompt, last_error)
                    logger.info("Will retry with error context...")
                    continue
                else:
                    logger.error(f"All {max_retries} attempts failed")
                    sys.exit(1)

            logger.info("Agent completed successfully")
            logger.info(f"  Session ID: {result.session_id}")
            logger.info(f"  Duration: {result.duration_ms}ms")

            # Validate Dockerfile and test_config.json exist
            logger.info("Validating build artifacts...")
            valid, message = validate_build_result(dockerfile_dir)
            if not valid:
                last_error = f"Build artifacts validation failed: {message}"
                logger.error(last_error)

                if attempt < max_retries:
                    current_prompt = _build_retry_prompt(prompt, last_error)
                    logger.info("Will retry with error context...")
                    continue
                else:
                    logger.error(f"All {max_retries} attempts failed")
                    sys.exit(1)

            logger.info(f"Build artifacts validation passed: {message}")

            # Check if temp image exists
            logger.info(f"Checking temp image exists: {temp_image_name}")
            check_result = subprocess.run(
                f"docker image inspect {temp_image_name}",
                shell=True,
                capture_output=True,
                text=True,
            )

            if check_result.returncode != 0:
                last_error = f"Temp image not found: {temp_image_name}. Agent may have failed to build the image."
                logger.error(last_error)

                if attempt < max_retries:
                    current_prompt = _build_retry_prompt(prompt, last_error)
                    logger.info("Will retry with error context...")
                    continue
                else:
                    logger.error(f"All {max_retries} attempts failed")
                    sys.exit(1)

            logger.info(f"Temp image exists: {temp_image_name}")

            # Validate temp image using validate_image.py
            valid, error_msg = run_validation(temp_image_name)
            if not valid:
                last_error = error_msg
                logger.error(last_error)

                if attempt < max_retries:
                    current_prompt = _build_retry_prompt(prompt, last_error)
                    logger.info("Will retry with error context...")
                    continue
                else:
                    logger.error(f"All {max_retries} attempts failed")
                    sys.exit(1)

            logger.info("Temp image validation passed")
            # All validations passed, break out of retry loop
            break

    # Only do temp -> final tagging if we actually built a new image
    if not skip_agent_build:
        # Check if final image already exists, remove if so
        check_final = subprocess.run(
            f"docker image inspect {final_image_name}",
            shell=True,
            capture_output=True,
            text=True,
        )

        if check_final.returncode == 0:
            logger.info(f"Removing existing final image: {final_image_name}")
            subprocess.run(
                f"docker rmi {final_image_name}",
                shell=True,
                capture_output=True,
                text=True,
            )

        # Tag temp image as final
        logger.info("Tagging temp image as final...")
        tag_result = subprocess.run(
            f"docker tag {temp_image_name} {final_image_name}",
            shell=True,
            capture_output=True,
            text=True,
        )

        if tag_result.returncode != 0:
            logger.error(f"Failed to tag image: {tag_result.stderr}")
            sys.exit(1)

        logger.info(f"Tagged: {temp_image_name} -> {final_image_name}")

        # Remove temp image
        logger.info("Removing temp image...")
        rm_result = subprocess.run(
            f"docker rmi {temp_image_name}",
            shell=True,
            capture_output=True,
            text=True,
        )

        if rm_result.returncode != 0:
            logger.warning(f"Failed to remove temp image: {rm_result.stderr}")
        else:
            logger.info(f"Removed temp image: {temp_image_name}")
    else:
        logger.info(f"Using existing validated image: {final_image_name}")

    # NOTE: base_image_name / build_command are intentionally NOT written back into
    # the hand-authored config/<range>.yaml. base_image_name is derivable by
    # convention (expected_base_image_name); build_command is persisted in
    # dockerfiles/build_command.txt. The config YAML is the Phase-1 source-boundary
    # contract only — the harness must never mutate it.

    # Summary
    print("\n" + "=" * 60)
    print("BUILD COMPLETE")
    print("=" * 60)
    print(f"Target: {target_info.name} ({target_info.sha[:10]})")
    print(f"Config: {args.config}")
    print(f"Output: {output_dir}")
    print(f"Dockerfile: {dockerfile_dir}")
    print(f"Test results: {test_results_dir}")
    print(f"Agent logs: {log_dir}")
    print(f"Final image: {final_image_name}")
    print("=" * 60)


if __name__ == "__main__":
    main()
