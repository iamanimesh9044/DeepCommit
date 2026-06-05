"""Preprocessing commands for DeepCommit."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .utils import (
    expand_tag,
    find_preprocessed_path,
    find_repo_path,
    parse_repo_spec,
    resolve_tags_for_codemaat,
    select_preprocessed,
)


def _parse_time(time_str: str) -> datetime:
    """Parse ISO 8601 time string."""
    return datetime.fromisoformat(time_str.replace("Z", "+00:00"))


def generate_commit_dag(
    repo_spec: str,
    data_root: Path,
    output_dir: Optional[Path] = None,
    overwrite: bool = False,
    generate_image: bool = False,
    context_lines: int = 3,
    patch_context_lines: int = 3,
    repo_src_dirs: Optional[List[str]] = None,
    follow_depth: int = 2,
    verify_with_patch: bool = True,
    reduce_transitive: bool = True,
    use_pygit2: bool = False,
    exclude_test_files: bool = False,
    exclude_ci_files: bool = False,
    exclude_config_files: bool = False,
    exclude_doc_files: bool = False,
    exclude_patterns: Optional[List[str]] = None,
    test_dirs: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    verbose: bool = False,
) -> int:
    """Generate commit DAG for a repository.

    Args:
        repo_spec: Repository specification in format 'owner_repo:start_end' (e.g., "urllib3_urllib3:2.0.6_2.3.0")
                  or a substring pattern for fuzzy matching (e.g., "urllib3", "2.0.6")
        data_root: DATA root directory
        output_dir: Output directory (default: DATA/github_data/preprocessed/<repo_id>_<time_span>/dag/)
        overwrite: Overwrite existing output files
        generate_image: Generate PNG image from DOT file
        context_lines: Number of context lines for git blame
        patch_context_lines: Number of context lines for git apply
        repo_src_dirs: List of source directories to filter analysis (e.g., ["src/", "lib/"])
        follow_depth: Depth of copy detection (1-3)
        verify_with_patch: Verify dependencies using git apply
        reduce_transitive: Remove transitive redundant edges
        use_pygit2: Use pygit2 library for git operations (default: False)
        exclude_test_files: Exclude test files from dependency analysis
        exclude_ci_files: Exclude CI/CD workflow files from dependency analysis
        exclude_config_files: Exclude configuration and build files from dependency analysis
        exclude_doc_files: Exclude documentation files from dependency analysis
        exclude_patterns: Additional glob patterns to exclude
        verbose: Enable verbose output

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Try to resolve as a pattern first (supports fuzzy matching)
    # If repo_spec contains ':', try exact format parsing first
    if ":" in repo_spec:
        try:
            repo_id, time_span = parse_repo_spec(repo_spec)
            preprocessed_dir = find_preprocessed_path(repo_id, time_span, data_root)
            if not preprocessed_dir.exists():
                # Exact path doesn't exist, fall back to fuzzy search
                print("Exact path not found, trying fuzzy search...", file=sys.stderr)
                preprocessed_dir, repo_spec = select_preprocessed(repo_spec, data_root)
                repo_id, time_span = parse_repo_spec(repo_spec)
        except (ValueError, FileNotFoundError):
            # Format error or not found, try fuzzy search
            try:
                preprocessed_dir, repo_spec = select_preprocessed(repo_spec, data_root)
                repo_id, time_span = parse_repo_spec(repo_spec)
            except (FileNotFoundError, RuntimeError) as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
    else:
        # No colon, definitely a pattern for fuzzy search
        try:
            preprocessed_dir, repo_spec = select_preprocessed(repo_spec, data_root)
            repo_id, time_span = parse_repo_spec(repo_spec)
        except (FileNotFoundError, RuntimeError) as e:
            print(f"Error: {e}", file=sys.stderr)
            print(f"Pattern '{repo_spec}' did not match any preprocessed repositories.", file=sys.stderr)
            return 1
    if not preprocessed_dir.exists():
        print(f"Error: Preprocessed directory not found: {preprocessed_dir}", file=sys.stderr)
        return 1

    commit_file = preprocessed_dir / "main_commits.jsonl"
    if not commit_file.exists():
        print(f"Error: main_commits.jsonl not found: {commit_file}", file=sys.stderr)
        return 1

    # Get repository path (repo_id is already in the correct format)
    repo_path = data_root / "github_data" / "repos" / repo_id
    if not repo_path.exists():
        print(f"Error: Repository not found: {repo_path}", file=sys.stderr)
        return 1

    # Determine output directory
    if output_dir is None:
        output_dir = preprocessed_dir / "dag"
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check if output already exists
    json_pattern = f"commit_dag_v3_ctx_{context_lines}_pctx_{patch_context_lines}.json"
    json_output = output_dir / json_pattern
    dot_output = output_dir / f"commit_dag_v3_ctx_{context_lines}_pctx_{patch_context_lines}.dot"

    skip_dag_generation = json_output.exists() and not overwrite

    if skip_dag_generation:
        print(f"Skipping DAG generation: Output already exists: {json_output}")
        if generate_image:
            png_output = dot_output.with_suffix(".png")
            if png_output.exists():
                print(f"PNG image also exists: {png_output}")
                print("Use --overwrite to regenerate")
                return 0
            else:
                print("But will generate PNG image as requested...")
        else:
            print("Use --overwrite to regenerate")
            return 0

    # Track DAG generation time (excluding image generation)
    dag_duration = 0

    # Only generate DAG if needed
    if not skip_dag_generation:
        # Print configuration
        print("=" * 80)
        print("Commit DAG v3 Generation")
        print("=" * 80)
        print(f"Repository: {repo_id}")
        print(f"Time span: {time_span}")
        print(f"Repo spec: {repo_spec}")
        print(f"Commit file: {commit_file}")
        print(f"Output directory: {output_dir}")
        print(f"Context lines: {context_lines}")
        print(f"Patch context lines: {patch_context_lines}")
        print(f"Follow depth: {follow_depth}")
        print(f"Verify with patch: {verify_with_patch}")
        print(f"Reduce transitive: {reduce_transitive}")
        print(f"Use pygit2: {use_pygit2}")
        if repo_src_dirs:
            print(f"Source directory filter: {', '.join(repo_src_dirs)}")
        else:
            print("Source directory filter: None (will use default)")
        # Print exclude options
        exclude_opts = []
        if exclude_test_files:
            exclude_opts.append("test files")
        if exclude_ci_files:
            exclude_opts.append("CI/CD files")
        if exclude_config_files:
            exclude_opts.append("config files")
        if exclude_doc_files:
            exclude_opts.append("doc files")
        if exclude_opts:
            print(f"Exclude: {', '.join(exclude_opts)}")
        if exclude_patterns:
            print(f"Exclude patterns: {', '.join(exclude_patterns)}")
        print("=" * 80)
        print()

    # Build command (only if not skipping)
    if not skip_dag_generation:
        commit_dag_script = Path(__file__).parent.parent / "preprocess" / "commit_dag" / "commit_dag_v3.py"
        if not commit_dag_script.exists():
            print(f"Error: commit_dag_v3.py not found: {commit_dag_script}", file=sys.stderr)
            return 1

        cmd = [
            sys.executable,
            str(commit_dag_script),
            "--repo",
            str(repo_path),
            "--commit-file",
            str(commit_file),
            "--out",
            str(json_output),
            "--context-lines",
            str(context_lines),
            "--patch-context-lines",
            str(patch_context_lines),
            "--follow-depth",
            str(follow_depth),
        ]

        if verify_with_patch:
            cmd.append("--verify-with-patch")
        else:
            cmd.append("--no-verify-with-patch")

        if reduce_transitive:
            cmd.append("--reduce-transitive")

        if use_pygit2:
            cmd.append("--use-pygit2")

        if repo_src_dirs:
            cmd.append("--repo-src-dirs")
            cmd.extend(repo_src_dirs)

        # Add exclude options
        if exclude_test_files:
            cmd.append("--exclude-test-files")
        if exclude_ci_files:
            cmd.append("--exclude-ci-files")
        if exclude_config_files:
            cmd.append("--exclude-config-files")
        if exclude_doc_files:
            cmd.append("--exclude-doc-files")
        # Merge all exclude patterns into a single list
        all_exclude = []
        if exclude_patterns:
            all_exclude.extend(exclude_patterns)
        if test_dirs:
            all_exclude.extend(test_dirs)
        if exclude:
            all_exclude.extend(exclude)
        if all_exclude:
            cmd.append("--exclude-patterns")
            cmd.extend(all_exclude)

        # Add DOT file path
        cmd.extend(["--dot", str(dot_output)])

        # Execute command
        start_time = time.time()
        print(f"Executing: {' '.join(cmd)}\n")

        try:
            result = subprocess.run(cmd, check=True)  # noqa: F841
            dag_duration = time.time() - start_time

            print()
            print("=" * 80)
            print("✓ DAG generation completed!")
            print("=" * 80)
            print(f"JSON output: {json_output}")
            print(f"DOT output: {dot_output}")
            print(f"Duration: {dag_duration:.1f}s ({_format_duration(dag_duration)})")
            print("=" * 80)

        except subprocess.CalledProcessError as e:
            print()
            print("=" * 80)
            print(f"✗ DAG generation failed (exit code: {e.returncode})")
            print("=" * 80)
            return e.returncode

    # Generate PNG image if requested
    if generate_image:
        if not dot_output.exists():
            print(f"\nWarning: DOT file not found: {dot_output}", file=sys.stderr)
            return 0

        if not shutil.which("dot"):
            print("\nWarning: graphviz 'dot' command not found, skipping PNG generation", file=sys.stderr)
            print("Install: sudo apt-get install graphviz  or  brew install graphviz")
            return 0

        png_output = dot_output.with_suffix(".png")
        print(f"\nGenerating PNG image: {png_output}")

        png_start = time.time()
        try:
            subprocess.run(["dot", "-Tpng", str(dot_output), "-o", str(png_output)], check=True)
            png_duration = time.time() - png_start
            print(f"✓ PNG generated successfully ({png_duration:.1f}s)")
        except subprocess.CalledProcessError as e:
            print(f"✗ PNG generation failed (exit code: {e.returncode})", file=sys.stderr)
            return e.returncode

    if dag_duration > 0:
        print(f"\nDAG generation duration: {dag_duration:.1f}s ({_format_duration(dag_duration)})")

    return 0


def _format_duration(seconds: float) -> str:
    """Format duration in seconds to HH:MM:SS."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def generate_codemaat_log(
    repo_spec: str,
    data_root: Path,
    repo_src_dirs: Optional[List[str]] = None,
    main_branch: str = "main",
    output_dir: Optional[Path] = None,
    overwrite: bool = False,
    tag_prefix: Optional[str] = None,
    test_dirs: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    verbose: bool = False,
) -> int:
    """Generate Code Maat log for a repository.

    Args:
        repo_spec: Repository specification in format 'owner_repo:start_end' (e.g., "urllib3_urllib3:2.0.6_2.3.0")
        data_root: DATA root directory
        repo_src_dirs: List of source directories to filter commits (e.g., ["src/", "lib/"])
        main_branch: Main branch name (default: "main")
        output_dir: Output directory (default: DATA/github_data/preprocessed/{repo_id}_{time_span}/codemaat/)
        overwrite: Overwrite existing output files
        tag_prefix: Prefix for simplified tag names (e.g., "tutanota-release-")
        test_dirs: List of test directory/file patterns to exclude
        exclude: List of additional patterns to exclude
        verbose: Enable verbose output

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Parse repo_spec to extract owner, repo, start_tag, end_tag
    try:
        repo_id, time_span = parse_repo_spec(repo_spec)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Expected format: 'owner_repo:start_end' (e.g., 'urllib3_urllib3:2.0.6_2.3.0')", file=sys.stderr)
        return 1

    # Parse repo_id to get owner and repo
    parts = repo_id.split("_", 1)
    if len(parts) != 2:
        print(f"Error: Invalid repo_id format: {repo_id}", file=sys.stderr)
        print("Expected format: 'owner_repo' (e.g., 'urllib3_urllib3')", file=sys.stderr)
        return 1

    owner, repo = parts

    # Parse time_span to get start_tag and end_tag
    tag_parts = time_span.split("_", 1)
    if len(tag_parts) != 2:
        print(f"Error: Invalid time_span format: {time_span}", file=sys.stderr)
        print("Expected format: 'start_end' (e.g., '2.0.6_2.3.0')", file=sys.stderr)
        return 1

    start_tag, end_tag = tag_parts

    # Auto-resolve local repository path
    local_repo = find_repo_path(repo_id, data_root)
    if not local_repo or not local_repo.exists():
        print(f"Error: Local repository not found for {repo_id}", file=sys.stderr)
        print(f"Expected path: {local_repo}", file=sys.stderr)
        return 1

    # Expand tags using prefix if provided
    if tag_prefix:
        start_tag = expand_tag(start_tag, tag_prefix, local_repo)
        end_tag = expand_tag(end_tag, tag_prefix, local_repo)
        if verbose:
            print(f"Expanded tags: {tag_parts[0]} -> {start_tag}, {tag_parts[1]} -> {end_tag}")

    # Determine output directory
    if output_dir is None:
        # Default: DATA/github_data/preprocessed/{repo_id}_{time_span}/codemaat/
        output_dir = find_preprocessed_path(repo_id, time_span, data_root) / "codemaat"
    else:
        output_dir = Path(output_dir).expanduser().resolve()

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Determine output file path
    # Generate filename based on filters
    if repo_src_dirs:
        # Sanitize directory names for filename
        dir_names = "_".join(d.rstrip("/").replace("/", "_") for d in repo_src_dirs)
        output_file = output_dir / f"codemaat_{dir_names}.log"
    else:
        output_file = output_dir / "codemaat_all.log"

    # Check if output file exists
    if output_file.exists() and not overwrite:
        print(f"Error: Output file already exists: {output_file}", file=sys.stderr)
        print("Use --overwrite to overwrite existing files", file=sys.stderr)
        return 1

    # Print configuration
    print("=" * 80)
    print("Code Maat Log Generation")
    print("=" * 80)
    print(f"Repository: {owner}/{repo}")
    print(f"Tag range: {start_tag} -> {end_tag}")
    print(f"Repo ID: {repo_id}")
    print(f"Time span: {time_span}")
    print(f"Local repo: {local_repo}")
    print(f"Main branch: {main_branch}")
    if repo_src_dirs:
        print(f"Source directory filter: {', '.join(repo_src_dirs)}")
    else:
        print("Source directory filter: Disabled (include all files)")
    print(f"Output directory: {output_dir}")
    print(f"Output file: {output_file.name}")
    print(f"Overwrite: {'Yes' if overwrite else 'No'}")
    print("=" * 80)
    print()

    # Check for diverged branches and resolve to merge-base if needed
    resolved_start, resolved_end, is_diverged = resolve_tags_for_codemaat(local_repo, start_tag, end_tag, main_branch)
    if is_diverged:
        print()  # Extra newline after diverged message

    # Build command to run generate_codemaat_log.py
    codemaat_script = Path(__file__).parent.parent / "preprocess" / "generate_codemaat_log.py"
    if not codemaat_script.exists():
        print(f"Error: generate_codemaat_log.py not found: {codemaat_script}", file=sys.stderr)
        return 1

    cmd = [
        sys.executable,
        str(codemaat_script),
        "--owner",
        owner,
        "--repo",
        repo,
        "--local-repo",
        str(local_repo),
        "--start-tag",
        resolved_start,  # Use resolved tag (may be merge-base SHA)
        "--end-tag",
        resolved_end,  # Use resolved tag (may be merge-base SHA)
        "--main-branch",
        main_branch,
        "--output",
        str(output_file),
    ]

    if repo_src_dirs:
        cmd.append("--repo-src-dirs")
        cmd.extend(repo_src_dirs)

    # Pass test_dirs and exclude patterns
    if test_dirs:
        cmd.append("--test-dirs")
        cmd.extend(test_dirs)
    if exclude:
        cmd.append("--exclude")
        cmd.extend(exclude)

    if overwrite:
        cmd.append("--overwrite")

    # Execute command
    start_time = time.time()
    if verbose:
        print(f"Executing: {' '.join(cmd)}\n")

    try:
        result = subprocess.run(cmd, check=True)  # noqa: F841
        duration = time.time() - start_time

        print()
        print("=" * 80)
        print("✓ Code Maat log generation completed!")
        print("=" * 80)
        print(f"Output file: {output_file}")
        print(f"Duration: {duration:.1f}s ({_format_duration(duration)})")
        print("=" * 80)

        # Run Code Maat analyses
        from ..preprocess.generate_codemaat_log import CodeMaatLogGenerator

        generator = CodeMaatLogGenerator(
            repo_path=str(local_repo),
            main_branch=main_branch,
            repo_src_dirs=repo_src_dirs,
            test_dirs=test_dirs,
            exclude=exclude,
        )

        analysis_dir = output_dir / "analysis"
        analysis_results = generator.generate_analyses(  # noqa: F841
            log_file=output_file,
            output_dir=analysis_dir,
            verbose=verbose,
        )

        return 0

    except subprocess.CalledProcessError as e:
        print()
        print("=" * 80)
        print(f"✗ Code Maat log generation failed (exit code: {e.returncode})")
        print("=" * 80)
        return e.returncode


def collect_github_data(
    repo_spec: str,
    data_root: Path,
    workers: int = 10,
    repo_src_dirs: Optional[List[str]] = None,
    dag_path: Optional[Path] = None,
    overwrite: bool = False,
    no_index: bool = False,
    output_dir: Optional[Path] = None,
    tag_prefix: Optional[str] = None,
    use_semver_filter: bool = True,
    test_dirs: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    verbose: bool = False,
) -> int:
    """Collect GitHub data (releases, PRs, issues) for a repository.

    Args:
        repo_spec: Repository specification in format 'owner_repo:start_end' (e.g., "urllib3_urllib3:2.0.6_2.3.0")
        data_root: DATA root directory
        workers: Number of concurrent workers for API calls (default: 10)
        repo_src_dirs: List of source directories to filter commits (e.g., ["src/", "lib/"])
        dag_path: Path to commit DAG JSON file for enriching main_commits.jsonl
        overwrite: Overwrite existing data
        no_index: Skip building index cache
        output_dir: Override output directory (default: auto-resolved from repo_spec)
        tag_prefix: Prefix for simplified tag names (e.g., "tutanota-release-")
        use_semver_filter: Use semver filtering for tags (default: True, set False for non-semver tags)
        Branch topology is handled automatically when a local repository is available.
        verbose: Enable verbose output

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Parse repo_spec to extract owner, repo, start_tag, end_tag
    try:
        repo_id, time_span = parse_repo_spec(repo_spec)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Expected format: 'owner_repo:start_end' (e.g., 'urllib3_urllib3:2.0.6_2.3.0')", file=sys.stderr)
        return 1

    # Parse repo_id to get owner and repo
    # repo_id format: owner_repo (e.g., urllib3_urllib3)
    parts = repo_id.split("_", 1)
    if len(parts) != 2:
        print(f"Error: Invalid repo_id format: {repo_id}", file=sys.stderr)
        print("Expected format: 'owner_repo' (e.g., 'urllib3_urllib3')", file=sys.stderr)
        return 1

    owner, repo = parts

    # Parse time_span to get start_tag and end_tag
    # time_span format: start_end (e.g., 2.0.6_2.3.0)
    tag_parts = time_span.split("_", 1)
    if len(tag_parts) != 2:
        print(f"Error: Invalid time_span format: {time_span}", file=sys.stderr)
        print("Expected format: 'start_end' (e.g., '2.0.6_2.3.0')", file=sys.stderr)
        return 1

    start_tag, end_tag = tag_parts

    # Auto-resolve paths
    local_repo = find_repo_path(repo_id, data_root)

    # Expand tags using prefix if provided
    if tag_prefix:
        start_tag = expand_tag(start_tag, tag_prefix, local_repo)
        end_tag = expand_tag(end_tag, tag_prefix, local_repo)
        if verbose:
            print(f"Expanded tags: {tag_parts[0]} -> {start_tag}, {tag_parts[1]} -> {end_tag}")

    # Use provided output_dir or auto-resolve
    if output_dir is None:
        output_dir = find_preprocessed_path(repo_id, time_span, data_root)
    else:
        # Ensure output_dir is a Path object
        output_dir = Path(output_dir).expanduser().resolve()

    # Get GitHub token (prefer GITHUB_CRAWLER_TOKENS, fallback to GITHUB_TOKEN)
    token = os.getenv("GITHUB_CRAWLER_TOKENS", "") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        print("Error: GITHUB_CRAWLER_TOKENS (or GITHUB_TOKEN) environment variable not set", file=sys.stderr)
        return 1

    # Print configuration
    print("=" * 80, flush=True)
    print("GitHub Data Collection", flush=True)
    print("=" * 80, flush=True)
    print(f"Repository: {owner}/{repo}")
    print(f"Tag range: {start_tag} -> {end_tag}")
    print(f"Repo ID: {repo_id}")
    print(f"Time span: {time_span}")
    if local_repo:
        print(f"Local repo: {local_repo}")
    else:
        print("Local repo: Not found (will collect without local Git analysis)")
    print(f"Output directory: {output_dir}")
    print(f"Workers: {workers}")
    if repo_src_dirs:
        print(f"Source directory filter: {', '.join(repo_src_dirs)}")
    else:
        print("Source directory filter: Disabled (collect all commits)")
    print(f"Build index: {'No' if no_index else 'Yes'}")
    print(f"Overwrite: {'Yes' if overwrite else 'No'}")
    print("=" * 80)
    print()

    # Build command to run gh_crawler.py
    gh_crawler_script = Path(__file__).parent.parent / "preprocess" / "gh_crawler.py"
    if not gh_crawler_script.exists():
        print(f"Error: gh_crawler.py not found: {gh_crawler_script}", file=sys.stderr)
        return 1

    cmd = [
        sys.executable,
        str(gh_crawler_script),
        "--owner",
        owner,
        "--repo",
        repo,
        "--start-tag",
        start_tag,
        "--end-tag",
        end_tag,
        "--output-dir",
        str(output_dir),
        "--token",
        token,
        "--workers",
        str(workers),
    ]

    if local_repo:
        cmd.extend(["--local-repo", str(local_repo)])

    if repo_src_dirs:
        cmd.append("--repo-src-dirs")
        cmd.extend(repo_src_dirs)

    if overwrite:
        cmd.append("--overwrite")

    if no_index:
        cmd.append("--no-index")

    # Test dirs and exclude patterns
    if test_dirs:
        cmd.append("--test-dirs")
        cmd.extend(test_dirs)
    if exclude:
        cmd.append("--exclude")
        cmd.extend(exclude)

    # Execute command
    start_time = time.time()
    if verbose:
        print(f"Executing: {' '.join(cmd)}\n")

    try:
        result = subprocess.run(cmd, check=True)  # noqa: F841
        duration = time.time() - start_time

        print()
        print("=" * 80)
        print("✓ Data collection completed!")
        print("=" * 80)
        print(f"Output directory: {output_dir}")
        print(f"Duration: {duration:.1f}s ({_format_duration(duration)})")
        print("=" * 80)

        # Post-process collected data (output_dir already points to the correct location)
        from ..preprocess.collect import PostProcessor

        post_processor = PostProcessor(
            output_dir=output_dir,
            repo_path=local_repo,
            verbose=verbose,
            test_dirs=test_dirs,
            exclude=exclude,
        )

        post_processor.process_all(dag_path=dag_path, repo_src_dirs=repo_src_dirs)

        return 0

    except subprocess.CalledProcessError as e:
        print()
        print("=" * 80)
        print(f"✗ Data collection failed (exit code: {e.returncode})")
        print("=" * 80)
        return e.returncode


def _check_raw_data_complete(raw_data_dir: Path) -> bool:
    """Check if raw_data directory contains complete data.

    Args:
        raw_data_dir: Path to raw_data directory

    Returns:
        True if data appears complete, False otherwise
    """
    from ..preprocess.collect.data_models import CollectedData

    return CollectedData.has_jsonl_dump(raw_data_dir)


def generate_symbols(
    repo_spec: str,
    data_root: Path,
    repo_src_dirs: Optional[List[str]] = None,
    include_context: bool = True,
    context_lines: int = 3,
    output_dir: Optional[Path] = None,
    overwrite: bool = False,
    verbose: bool = False,
    test_dirs: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
) -> int:
    """Generate symbol extraction for commits in a repository.

    Args:
        repo_spec: Repository specification in format 'owner_repo:start_end' (e.g., "urllib3_urllib3:2.0.6_2.3.0")
                  or a substring pattern for fuzzy matching
        data_root: DATA root directory
        repo_src_dirs: List of source directories to filter analysis (e.g., ["src/", "lib/"])
        include_context: Include context lines around changes (default: True)
        context_lines: Number of context lines to include (default: 3)
        output_dir: Output directory (default: DATA/github_data/preprocessed/<repo_id>_<time_span>/symbol/)
        overwrite: Overwrite existing output files
        verbose: Enable verbose output
        test_dirs: Test directory/file patterns to exclude (e.g., ['tests/**', '**/*_test.go'])
        exclude: Additional custom glob patterns to exclude

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Try to resolve as a pattern first (supports fuzzy matching)
    if ":" in repo_spec:
        try:
            repo_id, time_span = parse_repo_spec(repo_spec)
            preprocessed_dir = find_preprocessed_path(repo_id, time_span, data_root)
            if not preprocessed_dir.exists():
                # Exact path doesn't exist, fall back to fuzzy search
                print("Exact path not found, trying fuzzy search...", file=sys.stderr)
                preprocessed_dir, repo_spec = select_preprocessed(repo_spec, data_root)
                repo_id, time_span = parse_repo_spec(repo_spec)
        except (ValueError, FileNotFoundError):
            # Format error or not found, try fuzzy search
            try:
                preprocessed_dir, repo_spec = select_preprocessed(repo_spec, data_root)
                repo_id, time_span = parse_repo_spec(repo_spec)
            except (FileNotFoundError, RuntimeError) as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
    else:
        # No colon, definitely a pattern for fuzzy search
        try:
            preprocessed_dir, repo_spec = select_preprocessed(repo_spec, data_root)
            repo_id, time_span = parse_repo_spec(repo_spec)
        except (FileNotFoundError, RuntimeError) as e:
            print(f"Error: {e}", file=sys.stderr)
            print(
                f"Pattern '{repo_spec}' did not match any preprocessed repositories.",
                file=sys.stderr,
            )
            return 1

    if not preprocessed_dir.exists():
        print(f"Error: Preprocessed directory not found: {preprocessed_dir}", file=sys.stderr)
        return 1

    commit_file = preprocessed_dir / "main_commits.jsonl"
    if not commit_file.exists():
        print(f"Error: main_commits.jsonl not found: {commit_file}", file=sys.stderr)
        return 1

    # Get repository path
    repo_path = data_root / "github_data" / "repos" / repo_id
    if not repo_path.exists():
        print(f"Error: Repository not found: {repo_path}", file=sys.stderr)
        return 1

    # Determine output directory
    if output_dir is None:
        output_dir = preprocessed_dir / "symbol"
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check if output already exists
    output_full = output_dir / "main_commits_symbol_full.jsonl"
    output_minimal = output_dir / "main_commits_symbol.jsonl"

    if output_full.exists() and not overwrite:
        print(f"Error: Output files already exist: {output_full}", file=sys.stderr)
        print("Use --overwrite to overwrite existing files", file=sys.stderr)
        return 1

    # Load commits
    commits = []
    with open(commit_file, "r", encoding="utf-8") as f:
        for line in f:
            commit_data = json.loads(line)
            commits.append(commit_data["sha"])

    # Print configuration
    print("=" * 80)
    print("Symbol Extraction")
    print("=" * 80)
    print(f"Repository: {repo_id}")
    print(f"Time span: {time_span}")
    print(f"Repo spec: {repo_spec}")
    print(f"Commit file: {commit_file}")
    print(f"Total commits: {len(commits)}")
    print(f"Output directory: {output_dir}")
    print(f"Include context: {include_context}")
    if include_context:
        print(f"Context lines: {context_lines}")
    if repo_src_dirs:
        print(f"Source directory filter: {', '.join(repo_src_dirs)}")
    else:
        print("Source directory filter: None (will use default 'src')")
    print("=" * 80)
    print()

    # Get config path
    config_path = Path(__file__).parent.parent / "preprocess" / "configs" / "symbol_extraction_config.yml"
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        return 1

    # Extract symbols
    from ..preprocess.symbol_extractor import extract_symbols_batch

    start_time = time.time()
    print(f"Extracting symbols from {len(commits)} commits...")
    print()

    # Full output - pass the complete repo_src_dirs list
    results_full = extract_symbols_batch(
        repo_path=str(repo_path),
        commit_shas=commits,
        config_path=str(config_path),
        include_context=include_context,
        context_lines=context_lines,
        repo_src_dirs=repo_src_dirs,  # Pass full list instead of just first element
        minimal_output=False,
        verbose=verbose,
        test_dirs=test_dirs,
        exclude=exclude,
    )

    # Minimal output
    results_minimal = extract_symbols_batch(
        repo_path=str(repo_path),
        commit_shas=commits,
        config_path=str(config_path),
        include_context=include_context,
        context_lines=context_lines,
        repo_src_dirs=repo_src_dirs,  # Pass full list instead of just first element
        minimal_output=True,
        verbose=False,  # Don't print twice
        test_dirs=test_dirs,
        exclude=exclude,
    )

    duration = time.time() - start_time

    # Save results
    with open(output_full, "w", encoding="utf-8") as f:
        for result in results_full:
            f.write(json.dumps(result) + "\n")

    with open(output_minimal, "w", encoding="utf-8") as f:
        for result in results_minimal:
            f.write(json.dumps(result) + "\n")

    print()
    print("=" * 80)
    print("✓ Symbol extraction completed!")
    print("=" * 80)
    print(f"Full output: {output_full}")
    print(f"Minimal output: {output_minimal}")
    print(f"Duration: {duration:.1f}s ({_format_duration(duration)})")
    print("=" * 80)

    return 0


def run_all_preprocessing(
    repo_spec: str,
    data_root: Path,
    repo_src_dirs: Optional[List[str]] = None,
    output_dir: Optional[Path] = None,
    overwrite: bool = False,
    overwrite_skip_raw: bool = False,
    workers: int = 10,
    no_index: bool = False,
    main_branch: str = "main",
    generate_image: bool = False,
    context_lines: int = 3,
    patch_context_lines: int = 3,
    follow_depth: int = 2,
    verify_with_patch: bool = True,
    reduce_transitive: bool = True,
    use_pygit2: bool = False,
    test_dirs: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    tag_prefix: Optional[str] = None,
    use_semver_filter: bool = True,
    verbose: bool = False,
) -> int:
    """
    Run all preprocessing steps: dag -> collect -> codemaat, then copy result files.

    Args:
        repo_spec: Repository specification (owner_repo:start_end)
        data_root: DATA root directory
        repo_src_dirs: Source directories to filter commits
        output_dir: Base output directory
        overwrite: Overwrite existing output files
        overwrite_skip_raw: Overwrite existing files but skip re-collecting raw data if it exists and is complete (implies overwrite=True)
        workers: Number of concurrent workers for API calls
        no_index: Skip building index cache
        main_branch: Main branch name for codemaat
        generate_image: Generate PNG image from DOT file
        context_lines: Number of context lines for git blame
        patch_context_lines: Number of context lines for git apply
        follow_depth: Depth of copy detection (1-3)
        verify_with_patch: Enable git apply verification
        reduce_transitive: Remove transitive redundant edges
        use_pygit2: Use pygit2 library for git operations
        exclude_test_files: Exclude test files from dependency analysis
        exclude_ci_files: Exclude CI/CD workflow files from dependency analysis
        exclude_config_files: Exclude configuration and build files from dependency analysis
        exclude_doc_files: Exclude documentation files from dependency analysis
        exclude_patterns: Additional glob patterns to exclude
        tag_prefix: Prefix for simplified tag names (e.g., "tutanota-release-")
        use_semver_filter: Use semver filtering for tags (default: True, set False for non-semver tags)
        Branch topology is handled automatically when a local repository is available.
        verbose: Enable verbose output

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # If overwrite_skip_raw is enabled, automatically enable overwrite
    if overwrite_skip_raw:
        overwrite = True

    print()
    print("=" * 80)
    print("Running all preprocessing steps: collect (with patches) -> dag -> codemaat -> symbol")
    print("=" * 80)
    print()

    # Parse repo_spec to get output directory (normalize time_span consistently)
    try:
        repo_id_norm, time_span_norm = parse_repo_spec(repo_spec)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        print(
            "Expected format: 'owner_repo:start_end' (e.g., 'urllib3_urllib3:2.0.6_2.3.0')",
            file=sys.stderr,
        )
        return 1

    preprocessed_dir_name = f"{repo_id_norm}_{time_span_norm}"

    # Determine base output directory
    if output_dir:
        base_output_dir = output_dir
    else:
        preprocessed_root = data_root / "github_data" / "preprocessed"
        base_output_dir = preprocessed_root / preprocessed_dir_name

    # Create output directory if it doesn't exist
    base_output_dir.mkdir(parents=True, exist_ok=True)

    # Use base_output_dir as the actual data directory (no subdirectory needed)
    actual_data_dir = base_output_dir

    # Check if we should skip data collection
    skip_collect = False
    if overwrite_skip_raw:
        raw_data_dir = actual_data_dir / "raw_data"
        if _check_raw_data_complete(raw_data_dir):
            print("=" * 80)
            print("✓ Raw data already exists and is complete, skipping collection")
            print(f"  Location: {raw_data_dir}")
            print("=" * 80)
            print()
            skip_collect = True

    # Step 1: Collect GitHub data (without DAG enrichment first)
    if not skip_collect:
        print("=" * 80)
        print("STEP 1/4: Collecting GitHub data")
        print("=" * 80)
        result = collect_github_data(
            repo_spec=repo_spec,
            data_root=data_root,
            workers=workers,
            repo_src_dirs=repo_src_dirs,
            dag_path=None,  # No DAG enrichment yet
            overwrite=overwrite,
            no_index=no_index,
            output_dir=base_output_dir,  # Use provided output dir
            tag_prefix=tag_prefix,
            use_semver_filter=use_semver_filter,
            test_dirs=test_dirs,
            exclude=exclude,
            verbose=verbose,
        )
        if result != 0:
            print("✗ Data collection failed", file=sys.stderr)
            return result

        print()
        print(f"✓ Data collected to: {base_output_dir}")
        print()

    # Step 2: Generate commit DAG
    print("=" * 80)
    print("STEP 2/4: Generating commit DAG")
    print("=" * 80)

    # Prepare DAG generation
    dag_output_dir = actual_data_dir / "dag"
    dag_output_dir.mkdir(parents=True, exist_ok=True)

    commit_file = actual_data_dir / "main_commits.jsonl"
    if not commit_file.exists():
        print(f"✗ main_commits.jsonl not found: {commit_file}", file=sys.stderr)
        return 1

    repo_path = data_root / "github_data" / "repos" / repo_id_norm
    if not repo_path.exists():
        print(f"✗ Repository not found: {repo_path}", file=sys.stderr)
        return 1

    # Build command to call commit_dag_v3.py directly
    import subprocess

    from .utils import get_project_root, get_python_interpreter

    project_root = get_project_root()
    python_exe = get_python_interpreter(project_root)
    dag_script = project_root / "deepcommit" / "preprocess" / "commit_dag" / "commit_dag_v3.py"

    json_output = dag_output_dir / f"commit_dag_v3_ctx_{context_lines}_pctx_{patch_context_lines}.json"
    dot_output = dag_output_dir / f"commit_dag_v3_ctx_{context_lines}_pctx_{patch_context_lines}.dot"

    cmd = [
        str(python_exe),
        str(dag_script),
        "--repo",
        str(repo_path),
        "--commit-file",
        str(commit_file),
        "--out",
        str(json_output),
        "--context-lines",
        str(context_lines),
        "--patch-context-lines",
        str(patch_context_lines),
        "--follow-depth",
        str(follow_depth),
    ]

    if verify_with_patch:
        cmd.append("--verify-with-patch")
    if reduce_transitive:
        cmd.append("--reduce-transitive")
    if use_pygit2:
        cmd.append("--use-pygit2")
    if repo_src_dirs:
        cmd.append("--repo-src-dirs")
        cmd.extend(repo_src_dirs)

    # Pass test_dirs and exclude patterns to DAG generation (merge into single list)
    all_exclude = []
    if test_dirs:
        all_exclude.extend(test_dirs)
    if exclude:
        all_exclude.extend(exclude)
    if all_exclude:
        cmd.append("--exclude-patterns")
        cmd.extend(all_exclude)

    cmd.extend(["--dot", str(dot_output)])

    if verbose:
        print(f"Executing: {' '.join(cmd)}\n")

    result = subprocess.run(cmd)
    if result.returncode != 0:
        print("✗ DAG generation failed", file=sys.stderr)
        return result.returncode

    # Find the DAG file (with actual context_lines and patch_context_lines)
    # Try _enhanced.json first (the new format with edges_dag and edge_types)
    dag_file = dag_output_dir / f"commit_dag_v3_ctx_{context_lines}_pctx_{patch_context_lines}_enhanced.json"
    if not dag_file.exists():
        # Fall back to _with_types_enhanced.json (from older versions)
        dag_file = (
            dag_output_dir / f"commit_dag_v3_ctx_{context_lines}_pctx_{patch_context_lines}_with_types_enhanced.json"
        )
        if not dag_file.exists():
            print(f"✗ DAG file not found in {dag_output_dir}", file=sys.stderr)
            return 1

    print()
    print(f"✓ DAG generated: {dag_file}")
    print()

    # Step 2.5: Enrich main_commits with DAG data
    print("=" * 80)
    print("Enriching main_commits.jsonl with DAG statistics...")
    print("=" * 80)
    from ..preprocess.collect import PostProcessor

    post_processor = PostProcessor(
        output_dir=actual_data_dir,
        repo_path=data_root / "github_data" / "repos" / repo_id_norm,
        verbose=verbose,
        test_dirs=test_dirs,
        exclude=exclude,
    )

    try:
        post_processor.enrich_with_dag(dag_file)
        print("✓ DAG enrichment completed")
    except Exception as e:
        print(f"⚠️  Warning: DAG enrichment failed: {e}", file=sys.stderr)
    print()

    print()

    # Step 3: Generate Code Maat log
    print("=" * 80)
    print("STEP 3/4: Generating Code Maat log")
    print("=" * 80)
    codemaat_output_dir = actual_data_dir / "codemaat"
    result = generate_codemaat_log(
        repo_spec=repo_spec,
        data_root=data_root,
        repo_src_dirs=repo_src_dirs,
        main_branch=main_branch,
        output_dir=codemaat_output_dir,
        overwrite=overwrite,
        tag_prefix=tag_prefix,
        test_dirs=test_dirs,
        exclude=exclude,
        verbose=verbose,
    )
    if result != 0:
        print("✗ Code Maat generation failed", file=sys.stderr)
        return result

    print()

    # Step 4: Copy result files
    print("=" * 80)
    print("Copying result files to data directory")
    print("=" * 80)

    # Copy commit_dag.json and normalize SHA to 7 characters
    dag_src = dag_file
    dag_dst = actual_data_dir / "commit_dag.json"
    if dag_src.exists():
        # Read the DAG data
        with open(dag_src, "r", encoding="utf-8") as f:
            dag_data = json.load(f)

        # Normalize all SHA keys in nodes to 7 characters and remove 'sha' field
        if "nodes" in dag_data:
            new_nodes = {}
            for full_sha, node_data in dag_data["nodes"].items():
                short_sha = full_sha[:7]
                # Remove the 'sha' field from node_data if it exists
                if "sha" in node_data:
                    del node_data["sha"]
                new_nodes[short_sha] = node_data
            dag_data["nodes"] = new_nodes

        # Normalize SHA in edges (enhanced format uses 'edges_dag' with 'source'/'target')
        if "edges_dag" in dag_data:
            new_edges = []
            for edge in dag_data["edges_dag"]:
                new_edge = edge.copy()
                if "source" in edge:
                    new_edge["source"] = edge["source"][:7]
                if "target" in edge:
                    new_edge["target"] = edge["target"][:7]
                new_edges.append(new_edge)
            dag_data["edges_dag"] = new_edges
        # Also handle legacy format with 'edges' and 'src'/'dst' (for backward compatibility)
        elif "edges" in dag_data:
            new_edges = []
            for edge in dag_data["edges"]:
                new_edge = edge.copy()
                if "src" in edge:
                    new_edge["src"] = edge["src"][:7]
                if "dst" in edge:
                    new_edge["dst"] = edge["dst"][:7]
                new_edges.append(new_edge)
            dag_data["edges"] = new_edges

        # Write normalized DAG to destination
        with open(dag_dst, "w", encoding="utf-8") as f:
            json.dump(dag_data, f, indent=2, ensure_ascii=False)

        print(f"✓ Copied and normalized SHA to 7 chars: {dag_dst}")
    else:
        print(f"⚠️  DAG file not found: {dag_src}", file=sys.stderr)

    # Copy codemaat_analysis.json
    codemaat_src = codemaat_output_dir / "analysis" / "codemaat_analysis.json"
    codemaat_dst = actual_data_dir / "codemaat_analysis.json"
    if codemaat_src.exists():
        shutil.copy2(codemaat_src, codemaat_dst)
        print(f"✓ Copied {codemaat_src} -> {codemaat_dst}")
    else:
        print(f"⚠️  Code Maat analysis file not found: {codemaat_src}", file=sys.stderr)

    # Step 4: Generate symbol extraction
    print("=" * 80)
    print("STEP 4/4: Extracting symbols from commits")
    print("=" * 80)
    result = generate_symbols(
        repo_spec=repo_spec,
        data_root=data_root,
        repo_src_dirs=repo_src_dirs,
        include_context=True,
        context_lines=context_lines,
        output_dir=actual_data_dir / "symbol",
        overwrite=overwrite,
        verbose=verbose,
        test_dirs=test_dirs,
        exclude=exclude,
    )
    if result != 0:
        print("⚠️  Symbol extraction failed, but continuing...", file=sys.stderr)
        # Don't fail the entire pipeline for symbol extraction issues

    print()
    print("=" * 80)
    print("✓ All preprocessing steps completed!")
    print("=" * 80)
    print(f"Output directory: {actual_data_dir}")
    print("=" * 80)

    return 0
