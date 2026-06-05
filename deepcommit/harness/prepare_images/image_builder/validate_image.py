#!/usr/bin/env python3
"""
Agent-based Docker Image Validation

Uses Claude agent to validate that a Docker image is correctly configured for testing.
The agent performs runtime checks, test collection verification, and provides
actionable recommendations.

Checks performed:
1. Image exists
2. Runtime environment is working
3. Test framework can collect tests
4. Test files are properly discovered

Usage:
    # Validate with auto-inferred image name
    python validate_image.py --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2

    # Validate with explicit image name
    python validate_image.py --image urllib3_urllib3/base:latest

    # Specify language and framework
    python validate_image.py --work-dir ... --language java --test-framework maven

    # Save validation report
    python validate_image.py --work-dir ... --output validation.json

    # Use different model
    python validate_image.py --work-dir ... --model sonnet
"""

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class ValidationResult:
    """Result of image validation"""

    status: str  # PASS, FAIL, WARNING
    image_name: str
    language: str
    test_framework: str
    checks: List[Dict[str, Any]]
    summary: Dict[str, Any]
    recommendations: List[str]
    duration: float
    raw_output: str = ""


def load_prompt_template() -> str:
    """Load the validation prompt template."""
    prompt_path = Path(__file__).parent / "prompts" / "validate_image.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt template not found: {prompt_path}")
    return prompt_path.read_text()


def build_prompt(
    image_name: str,
    language: str,
    test_framework: str,
    test_dir: str,
    test_file_pattern: str = "test_*.py",
    test_output_dir: str = "/tmp/validate_test_output",
    repo_path: str = "/testbed",
) -> str:
    """Build the validation prompt with parameters."""
    template = load_prompt_template()

    # Replace placeholders
    prompt = template.format(
        image_name=image_name,
        language=language,
        test_framework=test_framework,
        test_dir=test_dir,
        test_file_pattern=test_file_pattern,
        test_output_dir=test_output_dir,
        repo_path=repo_path,
    )

    return prompt


def rebuild_image(
    image_name: str,
    dockerfile: str,
    context_path: str,
    verbose: bool = True,
) -> tuple[bool, str]:
    """
    Rebuild Docker image.

    Args:
        image_name: Target image name
        dockerfile: Path to Dockerfile
        context_path: Build context path (usually repo path)
        verbose: Print progress

    Returns:
        Tuple of (success, error_message)
    """
    if verbose:
        print(f"Rebuilding image: {image_name}")
        print(f"  Dockerfile: {dockerfile}")
        print(f"  Context: {context_path}")

    cmd = [
        "docker",
        "build",
        "-t",
        image_name,
        "-f",
        dockerfile,
        context_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800,  # 30 minutes timeout for build
        )

        if result.returncode != 0:
            error_msg = result.stderr[-1000:] if result.stderr else "Unknown error"
            return False, f"Docker build failed: {error_msg}"

        if verbose:
            print("  ✅ Image rebuilt successfully")
        return True, ""

    except subprocess.TimeoutExpired:
        return False, "Docker build timed out after 30 minutes"
    except Exception as e:
        return False, f"Docker build error: {e}"


def check_image_exists(image_name: str) -> tuple[bool, Dict[str, Any]]:
    """
    Check if Docker image exists.

    Returns:
        Tuple of (exists, details)
    """
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image_name],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            info = json.loads(result.stdout)
            if info:
                image_info = info[0]
                return True, {
                    "id": image_info.get("Id", "")[:12],
                    "created": image_info.get("Created", ""),
                    "size": image_info.get("Size", 0),
                }
        return False, {"error": "Image not found"}

    except subprocess.TimeoutExpired:
        return False, {"error": "Timeout checking image"}
    except Exception as e:
        return False, {"error": str(e)}


def run_agent(
    prompt: str,
    model: str = "haiku",
    timeout_seconds: int = 300,
) -> tuple[bool, str]:
    """
    Run Claude agent with the given prompt.

    Returns:
        Tuple of (success, output)
    """
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )

        if proc.returncode != 0:
            return False, f"Agent failed with exit code {proc.returncode}: {proc.stderr[:500]}"

        # Parse JSON output from claude
        try:
            result = json.loads(proc.stdout)
            output = result.get("result", proc.stdout)
        except json.JSONDecodeError:
            output = proc.stdout

        return True, output

    except subprocess.TimeoutExpired:
        return False, f"Agent timed out after {timeout_seconds}s"
    except FileNotFoundError:
        return False, "Claude CLI not found. Is it installed and in PATH?"
    except Exception as e:
        return False, f"Agent error: {e}"


def parse_agent_output(output: str) -> Dict[str, Any]:
    """
    Parse the agent's JSON output.

    Looks for JSON block in the output, handles various formats.
    """
    # Try to find JSON block in markdown code fence
    json_match = re.search(r"```json\s*(.*?)\s*```", output, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find raw JSON object
    json_match = re.search(r'\{[\s\S]*"status"[\s\S]*\}', output)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    # Try parsing entire output as JSON
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        pass

    # Return a default structure if parsing fails
    return {
        "status": "WARNING",
        "checks": [],
        "summary": {"parse_error": True},
        "recommendations": ["Agent output could not be parsed"],
        "raw_output": output[:2000],
    }


def validate_image(
    image_name: str,
    language: str = "python",
    test_framework: str = "pytest",
    test_dir: str = "test/",
    test_file_pattern: str = "test_*.py",
    test_output_dir: Optional[str] = None,
    repo_path: Optional[str] = None,
    dockerfile: Optional[str] = None,
    force_rebuild: bool = False,
    skip_agent: bool = False,
    model: str = "haiku",
    timeout: int = 300,
    verbose: bool = True,
) -> ValidationResult:
    """
    Validate a Docker image using Claude agent.

    Args:
        image_name: Docker image name
        language: Programming language
        test_framework: Test framework
        test_dir: Test directory
        test_file_pattern: Test file pattern (e.g., test_*.py)
        test_output_dir: Directory to save test execution results
        repo_path: Path to repository (for reference during validation)
        dockerfile: Path to Dockerfile (required if force_rebuild=True)
        force_rebuild: Force rebuild Docker image before running tests
        model: Claude model to use
        timeout: Agent timeout in seconds
        verbose: Print progress

    Returns:
        ValidationResult with all check results
    """
    start_time = time.time()
    checks = []

    if verbose:
        print(f"\n{'=' * 70}")
        print(f"Validating Image: {image_name}")
        print(f"Language: {language}, Framework: {test_framework}")
        print(f"Model: {model}")
        if force_rebuild:
            print("Force Rebuild: enabled")
        print(f"{'=' * 70}\n")

    # Step 0: Force rebuild if requested
    if force_rebuild:
        if not dockerfile:
            return ValidationResult(
                status="FAIL",
                image_name=image_name,
                language=language,
                test_framework=test_framework,
                checks=[],
                summary={"error": "--force-rebuild requires --dockerfile"},
                recommendations=["Provide --dockerfile path when using --force-rebuild"],
                duration=time.time() - start_time,
            )
        if not repo_path:
            return ValidationResult(
                status="FAIL",
                image_name=image_name,
                language=language,
                test_framework=test_framework,
                checks=[],
                summary={"error": "--force-rebuild requires --repo-path"},
                recommendations=["Provide --repo-path when using --force-rebuild"],
                duration=time.time() - start_time,
            )

        if verbose:
            print("0. Force rebuilding image...")

        rebuild_success, rebuild_error = rebuild_image(
            image_name=image_name,
            dockerfile=dockerfile,
            context_path=repo_path,
            verbose=verbose,
        )

        if not rebuild_success:
            return ValidationResult(
                status="FAIL",
                image_name=image_name,
                language=language,
                test_framework=test_framework,
                checks=[
                    {
                        "name": "rebuild",
                        "status": "FAIL",
                        "message": rebuild_error,
                        "details": {},
                    }
                ],
                summary={"error": "Image rebuild failed"},
                recommendations=["Fix Dockerfile and try again"],
                duration=time.time() - start_time,
            )

        checks.append(
            {
                "name": "rebuild",
                "status": "PASS",
                "message": "Image rebuilt successfully",
                "details": {},
            }
        )

    # Check 1: Image exists (fast check before calling agent)
    if verbose:
        print("1. Checking image exists...")

    image_exists, image_details = check_image_exists(image_name)
    checks.append(
        {
            "name": "image_exists",
            "status": "PASS" if image_exists else "FAIL",
            "message": (
                f"Image found: {image_details.get('id', '')}"
                if image_exists
                else image_details.get("error", "Image not found")
            ),
            "details": image_details,
        }
    )

    if verbose:
        status_icon = "✅" if image_exists else "❌"
        print(f"   {status_icon} {checks[-1]['message']}")

    if not image_exists:
        return ValidationResult(
            status="FAIL",
            image_name=image_name,
            language=language,
            test_framework=test_framework,
            checks=checks,
            summary={"error": "Image does not exist"},
            recommendations=["Build the image first using build_image.py"],
            duration=time.time() - start_time,
        )

    # Skip agent validation if requested
    if skip_agent:
        if verbose:
            print("\n2. Skipping agent validation (--skip-agent)")

        return ValidationResult(
            status="PASS",
            image_name=image_name,
            language=language,
            test_framework=test_framework,
            checks=checks,
            summary={"agent_skipped": True},
            recommendations=[],
            duration=time.time() - start_time,
        )

    # Build prompt for remaining checks
    if verbose:
        print("\n2. Running agent for remaining checks...")

    prompt = build_prompt(
        image_name=image_name,
        language=language,
        test_framework=test_framework,
        test_dir=test_dir,
        test_file_pattern=test_file_pattern,
        test_output_dir=test_output_dir or "/tmp/validate_test_output",
        repo_path=repo_path or "/testbed",
    )

    if verbose:
        print("Running validation agent...")

    # Run agent
    success, output = run_agent(prompt, model=model, timeout_seconds=timeout)

    duration = time.time() - start_time

    if not success:
        if verbose:
            print(f"\n❌ Agent failed: {output}")
        checks.append({"name": "agent", "status": "FAIL", "message": output})
        return ValidationResult(
            status="FAIL",
            image_name=image_name,
            language=language,
            test_framework=test_framework,
            checks=checks,
            summary={"error": output},
            recommendations=[],
            duration=duration,
            raw_output=output,
        )

    # Parse agent output
    result = parse_agent_output(output)

    # Merge checks: image_exists (from code) + agent checks
    agent_checks = result.get("checks", [])
    # Filter out image_exists from agent checks (we already did it)
    agent_checks = [c for c in agent_checks if c.get("name") != "image_exists"]
    all_checks = checks + agent_checks

    # Build ValidationResult
    validation_result = ValidationResult(
        status=result.get("status", "WARNING"),
        image_name=image_name,
        language=language,
        test_framework=test_framework,
        checks=all_checks,
        summary=result.get("summary", {}),
        recommendations=result.get("recommendations", []),
        duration=duration,
        raw_output=output if result.get("summary", {}).get("parse_error") else "",
    )

    if verbose:
        print(f"\n{'=' * 70}")
        status_icon = {"PASS": "", "FAIL": "", "WARNING": ""}.get(validation_result.status, "")
        print(f"{status_icon} Validation {validation_result.status}")
        print(f"Duration: {duration:.2f}s")

        # Print check results
        for check in validation_result.checks:
            check_status = check.get("status", "UNKNOWN")
            check_icon = {"PASS": "", "FAIL": "", "WARNING": "", "SKIPPED": ""}.get(check_status, "")
            print(f"  {check_icon} {check.get('name', 'unknown')}: {check.get('message', '')}")

        # Print summary
        if validation_result.summary:
            summary = validation_result.summary
            if "test_count" in summary:
                print(f"\n  Tests collected: {summary['test_count']}")

        # Print recommendations
        if validation_result.recommendations:
            print("\n  Recommendations:")
            for rec in validation_result.recommendations[:5]:
                print(f"    - {rec}")

        print(f"{'=' * 70}\n")

    return validation_result


def save_result(result: ValidationResult, output_path: Path):
    """Save validation result to JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    result_dict = {
        "status": result.status,
        "image_name": result.image_name,
        "language": result.language,
        "test_framework": result.test_framework,
        "checks": result.checks,
        "summary": result.summary,
        "recommendations": result.recommendations,
        "duration": result.duration,
        "timestamp": datetime.now().isoformat(),
    }

    with open(output_path, "w") as f:
        json.dump(result_dict, f, indent=2)

    print(f"Result saved to: {output_path}")


def infer_image_name(work_dir: Path) -> str:
    """
    Infer Docker image name from work_dir path.

    Format: {config_name}/{target}:latest
    Example: urllib3_urllib3_2.0.6_2.3.0/base:latest
    """
    path_parts = work_dir.resolve().parts
    if len(path_parts) < 2:
        raise ValueError(f"Cannot infer image name from: {work_dir}")

    target = path_parts[-1]
    config_name = path_parts[-2]

    return f"{config_name}/{target}:latest".lower()


def load_metadata(work_dir: Path) -> Dict[str, Any]:
    """Load metadata.json from work_dir if it exists."""
    metadata_path = work_dir / "metadata.json"
    if metadata_path.exists():
        with open(metadata_path) as f:
            return json.load(f)
    return {}


def load_config(config_path: Path) -> Dict[str, Any]:
    """Load YAML config file."""
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def main():
    parser = argparse.ArgumentParser(
        description="Agent-based Docker image validation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Validate with auto-inferred settings
  python validate_image.py --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/base

  # Validate with explicit image
  python validate_image.py --image myimage:latest --language python --test-framework pytest

  # Use different model
  python validate_image.py --work-dir ... --model sonnet

Supported language/framework combinations:
  python/pytest, python/unittest
  java/maven, java/gradle
  javascript/jest, javascript/mocha
  go/go_test
  rust/cargo
        """,
    )

    # Image specification
    image_group = parser.add_mutually_exclusive_group(required=True)
    image_group.add_argument("--work-dir", type=Path, help="Working directory (auto-generates image name)")
    image_group.add_argument("--image", type=str, help="Explicit Docker image name")

    # Config file
    parser.add_argument(
        "--config", type=Path, help="YAML config file to load settings (language, test_framework, test_dirs)"
    )

    # Language and framework
    parser.add_argument("--language", type=str, default="python", help="Programming language (default: python)")
    parser.add_argument("--test-framework", type=str, default="pytest", help="Test framework (default: pytest)")
    parser.add_argument(
        "--test-dirs",
        "--test-dir",
        type=str,
        default="test/",
        help="Test directory/directories (space-separated, default: test/)",
    )
    parser.add_argument(
        "--test-file-pattern", type=str, default="test_*.py", help="Test file pattern (default: test_*.py)"
    )
    parser.add_argument("--repo-path", type=Path, help="Path to repository (for reference during validation)")
    parser.add_argument("--dockerfile", type=Path, help="Path to Dockerfile (required if --force-rebuild is used)")
    parser.add_argument("--force-rebuild", action="store_true", help="Force rebuild Docker image before running tests")

    # Agent options
    parser.add_argument(
        "--model",
        type=str,
        default="haiku",
        choices=["haiku", "sonnet", "opus"],
        help="Claude model to use (default: haiku)",
    )
    parser.add_argument("--timeout", type=int, default=300, help="Agent timeout in seconds (default: 300)")
    parser.add_argument(
        "--skip-agent",
        action="store_true",
        help="Skip model-based validation, only check if image exists",
    )

    # Test execution options
    parser.add_argument(
        "--test-output-dir", type=Path, help="Directory to save test execution results (for Step 3: Test Execution)"
    )

    # Output options
    parser.add_argument("--output", type=Path, help="Save validation result to JSON file")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")

    args = parser.parse_args()

    # Determine image name
    if args.work_dir:
        work_dir = args.work_dir.resolve()
        if not work_dir.exists():
            print(f"Work directory not found: {work_dir}")
            sys.exit(1)

        try:
            image_name = infer_image_name(work_dir)
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)

        # Try to load settings from metadata.json
        metadata = load_metadata(work_dir)
        if metadata:
            if "language" in metadata and args.language == "python":
                args.language = metadata["language"]
            if "test_framework" in metadata and args.test_framework == "pytest":
                args.test_framework = metadata["test_framework"]
            if "test_dirs" in metadata and args.test_dirs == "test/":
                args.test_dirs = " ".join(metadata["test_dirs"])

        if not args.quiet:
            print("Inferred from work_dir:")
            print(f"  image_name: {image_name}")
            print(f"  language: {args.language}")
            print(f"  test_framework: {args.test_framework}")
    else:
        image_name = args.image

    # Load settings from config file if provided
    if args.config:
        if not args.config.exists():
            print(f"Error: Config file not found: {args.config}")
            sys.exit(1)

        config = load_config(args.config)
        if config:
            # Use config values as defaults, CLI args override
            if "language" in config:
                args.language = config["language"]
            if "test_framework" in config:
                args.test_framework = config["test_framework"]
            if "test_dirs" in config:
                args.test_dirs = " ".join(config["test_dirs"])
            if "test_file_pattern" in config:
                args.test_file_pattern = config["test_file_pattern"]

            if not args.quiet:
                print(f"Loaded from config {args.config}:")
                print(f"  language: {args.language}")
                print(f"  test_framework: {args.test_framework}")
                print(f"  test_dir: {args.test_dirs}")
                print(f"  test_file_pattern: {args.test_file_pattern}")

    # Determine test output directory
    test_output_dir = None
    if args.test_output_dir:
        test_output_dir = str(args.test_output_dir.resolve())
        args.test_output_dir.mkdir(parents=True, exist_ok=True)

    # Determine repo path
    repo_path = None
    if args.repo_path:
        repo_path = str(args.repo_path.resolve())

    # Determine dockerfile path
    dockerfile = None
    if args.dockerfile:
        dockerfile = str(args.dockerfile.resolve())

    # Run validation
    result = validate_image(
        image_name=image_name,
        language=args.language,
        test_framework=args.test_framework,
        test_dir=args.test_dirs,
        test_file_pattern=args.test_file_pattern,
        test_output_dir=test_output_dir,
        repo_path=repo_path,
        dockerfile=dockerfile,
        force_rebuild=args.force_rebuild,
        skip_agent=args.skip_agent,
        model=args.model,
        timeout=args.timeout,
        verbose=not args.quiet,
    )

    # Save result if output specified
    if args.output:
        save_result(result, args.output)

    # Exit with appropriate code
    if result.status == "FAIL":
        sys.exit(1)
    elif result.status == "WARNING":
        sys.exit(0)  # Warning is still success
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
