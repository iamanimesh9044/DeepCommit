#!/usr/bin/env python3
"""
Render environment setup prompts from template.

This script renders the prompts/env_setup_prompt.tmpl template for
configuring Docker images for any target (base, final, or specific commit).

Usage:
    # For base target
    python render_env_prompt.py \
        --repo-path DATA/github_data/repos/urllib3_urllib3 \
        --target base \
        --target-sha abc1234 \
        --config config/urllib3_urllib3_2.0.6_2.3.0.yaml \
        --dockerfile-dir DATA/harness_workspace/xxx/dockerfiles/base \
        --test-results-dir DATA/harness_workspace/xxx/test_results/base

    # For final target
    python render_env_prompt.py \
        --repo-path DATA/github_data/repos/urllib3_urllib3 \
        --target final \
        --target-sha def5678 \
        --config config/urllib3_urllib3_2.0.6_2.3.0.yaml \
        --dockerfile-dir DATA/harness_workspace/xxx/dockerfiles/final \
        --test-results-dir DATA/harness_workspace/xxx/test_results/final

    # For specific commit
    python render_env_prompt.py \
        --repo-path DATA/github_data/repos/urllib3_urllib3 \
        --target abc1234 \
        --target-sha abc1234... \
        --config config/urllib3_urllib3_2.0.6_2.3.0.yaml \
        --dockerfile-dir DATA/harness_workspace/xxx/dockerfiles/abc1234 \
        --test-results-dir DATA/harness_workspace/xxx/test_results/abc1234

Output:
    Rendered prompt file at {dockerfile_dir}/env_setup_prompt.md
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

import yaml


def get_relative_path(path: Path) -> str:
    """
    Return an absolute path string suitable for prompt interpolation.

    Previously returned a path relative to Path.cwd() when possible. That
    coupled prompt correctness to the caller's CWD and silently broke when
    the harness workspace was not a subtree of CWD (e.g., when DATA is a
    symlink to an unrelated mount). Using .absolute() keeps symlinks in the
    path readable while removing the CWD dependency entirely.
    """
    return str(path.absolute())


def compute_base_sha(repo_path: Path, base_tag: str, main_branch: str, end_tag: str = None) -> str:
    """
    Compute the base SHA by finding the branch-out point of the base_tag.

    Uses LocalGitRepo.find_branch_out_point to find where the tag branches off
    from the main development branch.

    Args:
        repo_path: Path to the git repository
        base_tag: The starting version tag (e.g., "1.5.2")
        main_branch: The main branch name (e.g., "main")
        end_tag: The ending version tag, used as reference for branch-out point
    """
    from deepcommit.utils.git.local_git_repo import LocalGitRepo

    repo = LocalGitRepo(str(repo_path), main_branch=main_branch)
    # Use end_tag as reference if provided, otherwise use main_branch
    reference = end_tag if end_tag else main_branch
    base_sha = repo.find_branch_out_point(base_tag, reference)
    print(f"   Found branch-out point for {base_tag} (ref: {reference}): {base_sha[:10]}")
    return base_sha


def compute_final_sha(repo_path: Path, end_tag: str, main_branch: str) -> str:
    """
    Compute the final SHA by finding the branch-out point of the end_tag.

    Uses LocalGitRepo.find_branch_out_point to find where the tag branches off
    from the main development branch.
    """
    from deepcommit.utils.git.local_git_repo import LocalGitRepo

    repo = LocalGitRepo(str(repo_path), main_branch=main_branch)
    final_sha = repo.find_branch_out_point(end_tag, main_branch)
    print(f"   Found branch-out point for {end_tag} (ref: {main_branch}): {final_sha[:10]}")
    return final_sha


def resolve_commit_sha(repo_path: Path, ref: str) -> str:
    """Resolve a git reference (tag, branch, or sha) to full commit SHA."""
    result = subprocess.run(
        f"git rev-parse {ref}^{{commit}}",
        shell=True,
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"Could not resolve '{ref}' to a commit: {result.stderr}")
    return result.stdout.strip()


def render_env_setup_prompt(
    repo_path: Path,
    target_name: str,
    target_sha: str,
    target_description: str,
    start_version: str,
    end_version: str,
    repo_src_dirs: list[str],
    test_dirs: list[str],
    exclude_patterns: list[str],
    dockerfile_dir: Path,
    test_results_dir: Path,
    template_path: Path,
    config_name: str = "",
) -> int:
    """
    Render environment setup prompt for any target.

    Args:
        repo_path: Path to the source repository
        target_name: Target name ('base', 'final', or short SHA)
        target_sha: Full commit SHA for the target
        target_description: Human-readable description of the target
        start_version: Start version of the release range
        end_version: End version of the release range
        repo_src_dirs: List of source directories
        test_dirs: List of test directory patterns
        exclude_patterns: List of patterns to exclude from analysis
        dockerfile_dir: Directory to save Dockerfile
        test_results_dir: Directory to save test results
        template_path: Path to the prompt template file
        config_name: Config name for image naming (e.g., navidrome_navidrome_v0.57.0_v0.58.0)

    Returns:
        0 on success, 1 on error
    """
    if not template_path.exists():
        print(f"Error: Template not found: {template_path}")
        return 1

    template = template_path.read_text()
    dockerfile_dir.mkdir(parents=True, exist_ok=True)

    # Fill in template variables
    prompt = template
    replacements = {
        # Target info
        "{{target_name}}": target_name,
        "{{target_sha}}": target_sha,
        "{{target_sha[:7]}}": target_sha[:7],
        "{{target_description}}": target_description,
        # Legacy compatibility (for base_env_setup_prompt.tmpl)
        "{{base_sha}}": target_sha,
        "{{base_sha[:7]}}": target_sha[:7],
        # Repository info
        "{{repo_path}}": get_relative_path(repo_path),
        "{{start_version}}": start_version,
        "{{end_version}}": end_version,
        "{{repo_src_dirs}}": json.dumps(repo_src_dirs),
        "{{test_dirs}}": json.dumps(test_dirs),
        "{{test_dirs_cli}}": " ".join(test_dirs),
        "{{exclude_patterns}}": json.dumps(exclude_patterns),
        # Output directories
        "{{dockerfile_dir}}": get_relative_path(dockerfile_dir),
        "{{test_results_dir}}": get_relative_path(test_results_dir),
        # Image naming
        "{{temp_image_name}}": f"{config_name}/{target_name}-temp:latest".lower() if config_name else "",
    }

    for placeholder, value in replacements.items():
        prompt = prompt.replace(placeholder, value)

    # Save prompt
    output_file = dockerfile_dir / "env_setup_prompt.md"
    output_file.write_text(prompt)

    print(f"Rendered prompt saved to: {output_file}")
    return 0


def parse_versions_from_config_path(config_path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Parse start_version and end_version from config filename."""
    filename = config_path.stem
    match = re.match(r"^(.+?)_(.+?)_([a-zA-Z-]*[\d.]+[a-zA-Z\d.-]*)_([a-zA-Z-]*[\d.]+[a-zA-Z\d.-]*)$", filename)
    if match:
        return match.group(3), match.group(4)
    return None, None


def main():
    parser = argparse.ArgumentParser(
        description="Render environment setup prompt from template",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # For base target
  python render_env_prompt.py \\
    --repo-path DATA/github_data/repos/urllib3_urllib3 \\
    --target base \\
    --target-sha abc1234 \\
    --config config/urllib3_urllib3_2.0.6_2.3.0.yaml \\
    --dockerfile-dir DATA/harness_workspace/xxx/dockerfiles/base \\
    --test-results-dir DATA/harness_workspace/xxx/test_results/base

  # For final target
  python render_env_prompt.py \\
    --repo-path DATA/github_data/repos/urllib3_urllib3 \\
    --target final \\
    --target-sha def5678 \\
    --config config/urllib3_urllib3_2.0.6_2.3.0.yaml \\
    --dockerfile-dir DATA/harness_workspace/xxx/dockerfiles/final \\
    --test-results-dir DATA/harness_workspace/xxx/test_results/final

Note: The agent will identify the programming language and test framework
by exploring the repository structure (CI files, build files, etc.)
        """,
    )

    # Required arguments
    parser.add_argument("--repo-path", type=Path, required=True, help="Path to the source repository")
    parser.add_argument("--target", type=str, required=True, help="Target name: 'base', 'final', or commit SHA")
    parser.add_argument("--target-sha", type=str, required=True, help="Full commit SHA for the target")
    parser.add_argument("--config", type=Path, required=True, help="YAML config file path")
    parser.add_argument("--dockerfile-dir", type=Path, required=True, help="Directory to save Dockerfile and prompt")
    parser.add_argument("--test-results-dir", type=Path, required=True, help="Directory to save test results")

    # Optional arguments
    parser.add_argument("--start-version", type=str, help="Start version (default: from config filename)")
    parser.add_argument("--end-version", type=str, help="End version (default: from config filename)")
    parser.add_argument(
        "--target-description", type=str, help="Description of the target (auto-generated if not provided)"
    )
    parser.add_argument("--main-branch", type=str, help="Main branch name (default: from config or 'main')")
    parser.add_argument("--repo-src-dirs", type=str, nargs="*", help="Source directories (overrides config)")
    parser.add_argument("--test-dirs", type=str, nargs="*", help="Test directories (overrides config)")
    parser.add_argument("--exclude-patterns", type=str, nargs="*", help="Exclude patterns (overrides config)")
    parser.add_argument(
        "--template",
        type=Path,
        default=Path(__file__).parent / "prompts" / "env_setup_prompt.tmpl",
        help="Path to prompt template",
    )

    args = parser.parse_args()

    # Validate paths
    if not args.repo_path.exists():
        print(f"Error: Repository path not found: {args.repo_path}")
        return 1

    if not args.config.exists():
        print(f"Error: Config file not found: {args.config}")
        return 1

    # Try fallback template if default doesn't exist
    template_path = args.template
    if not template_path.exists():
        fallback = Path(__file__).parent / "prompts" / "base_env_setup_prompt.tmpl"
        if fallback.exists():
            template_path = fallback
            print(f"   Using fallback template: {template_path}")
        else:
            print(f"Error: Template file not found: {args.template}")
            return 1

    # Load config
    print(f"   Loading config from: {args.config}")
    with open(args.config) as f:
        config = yaml.safe_load(f) or {}

    # Parse versions from config filename
    inferred_start, inferred_end = parse_versions_from_config_path(args.config)
    start_version = args.start_version or inferred_start
    end_version = args.end_version or inferred_end

    if not start_version or not end_version:
        print("Error: Could not determine versions. Use --start-version and --end-version.")
        return 1

    # Get config values
    main_branch = args.main_branch or config.get("main_branch") or "main"  # noqa: F841
    repo_src_dirs = args.repo_src_dirs if args.repo_src_dirs is not None else config.get("repo_src_dirs", [])
    test_dirs = args.test_dirs if args.test_dirs is not None else config.get("test_dirs", ["test"])
    exclude_patterns = args.exclude_patterns if args.exclude_patterns is not None else config.get("exclude", [])

    # Generate target description if not provided
    target_name = args.target
    target_sha = args.target_sha
    if args.target_description:
        target_description = args.target_description
    elif target_name == "base":
        target_description = f"Base image for {start_version} (start of version range)"
    elif target_name == "final":
        target_description = f"Final image for {end_version} (end of version range)"
    else:
        target_description = f"Image for commit {target_sha[:7]}"

    print(f"\n{'=' * 60}")
    print("Rendering Environment Setup Prompt")
    print(f"{'=' * 60}")
    print(f"Target: {target_name} ({target_sha[:10]})")
    print(f"Description: {target_description}")
    print(f"Repository: {args.repo_path}")
    print(f"Version range: {start_version} -> {end_version}")
    print(f"Source dirs: {repo_src_dirs}")
    print(f"Test dirs: {test_dirs}")
    print(f"Exclude patterns: {exclude_patterns}")
    print(f"Dockerfile dir: {args.dockerfile_dir}")
    print(f"Test results dir: {args.test_results_dir}")
    print(f"{'=' * 60}\n")

    result = render_env_setup_prompt(
        repo_path=args.repo_path,
        target_name=target_name,
        target_sha=target_sha,
        target_description=target_description,
        start_version=start_version,
        end_version=end_version,
        repo_src_dirs=repo_src_dirs,
        test_dirs=test_dirs,
        exclude_patterns=exclude_patterns,
        dockerfile_dir=args.dockerfile_dir,
        test_results_dir=args.test_results_dir,
        template_path=template_path,
    )

    if result == 0:
        print(f"\n{'=' * 60}")
        print("Success!")
        print(f"Output: {args.dockerfile_dir / 'env_setup_prompt.md'}")
        print(f"{'=' * 60}\n")

    return result


if __name__ == "__main__":
    sys.exit(main())
