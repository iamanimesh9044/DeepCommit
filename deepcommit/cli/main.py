"""DeepCommit CLI - Command-line interface for DeepCommit pipeline.

Usage Examples
==============

Post-processing Commands
------------------------

# Run both enrichment and analysis (default)
deepcommit postprocess <trial_name> <repo_id>

# Run only analysis
deepcommit postprocess analyze <trial_name> <repo_id>

# Run only enrichment
deepcommit postprocess enrich <trial_name> <repo_id>

# Run analysis without saving files (dry-run)
deepcommit postprocess analyze <trial_name> <repo_id> --dry-run

# Overwrite existing analysis results
deepcommit postprocess analyze <trial_name> <repo_id> --overwrite

# Fix dependencies (add missing, remove redundant)
deepcommit postprocess fix <trial_name> <repo_id>

# Fix with dry-run (show what would be done)
deepcommit postprocess fix <trial_name> <repo_id> --dry-run

Other Commands
--------------

# Run multi-stage pipeline
deepcommit pipeline-multi run --repo-id <repo_id> --trial-name <trial_name>

# Preprocessing
deepcommit preproc all <repo_id>

# Trial management
deepcommit trial run <trial_name> <repo_id>

# Database utilities
deepcommit db refresh-views

Arguments
---------

<trial_name>    Trial name or substring (supports fuzzy matching)
<repo_id>       Repository specification in format 'owner_repo:start_end'
                Examples:
                  - urllib3_urllib3:2.0.6_2.3.0
                  - BurntSushi_ripgrep:14.1.1_15.0.0
                  - django_django:4.2_5.2

Configuration
-------------

If a config file exists at config/<repo_id>_<time_span>.yaml, it will be
automatically loaded and used for default values (repo_src_dirs, exclude, etc.).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict

import yaml

from . import db as db_tools
from . import preproc, trials
from .utils import (
    get_data_root,
    get_result_base_root,
    infer_repo_id_from_trial_name,
    parse_repo_spec,
    select_preprocessed,
    select_trial,
)


def _add_verbose_argument(parser: argparse.ArgumentParser) -> None:
    """Add -v/--verbose argument to parser."""
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logs (Debug mode)")


def _add_repo_id_argument(parser: argparse.ArgumentParser, optional: bool = False) -> None:
    """Add repo_id argument to parser.

    Args:
        parser: ArgumentParser to add argument to
        optional: If True, make repo_id optional (can be inferred from trial_name)
    """
    if optional:
        parser.add_argument(
            "repo_id",
            nargs="?",
            default=None,
            help="Repository specification (owner_repo:start_end). Optional if trial_name contains repo info.",
        )
    else:
        parser.add_argument(
            "repo_id", help="Repository specification (owner_repo:start_end) or substring for fuzzy matching"
        )


def _add_trial_name_argument(parser: argparse.ArgumentParser) -> None:
    """Add trial_name positional argument to parser."""
    parser.add_argument("trial_name", help="Trial name or substring")


def _add_data_root_argument(parser: argparse.ArgumentParser) -> None:
    """Add --data-root argument to parser."""
    parser.add_argument("--data-root", dest="data_root", help="Override DATA root directory")


def _add_repo_src_dirs_argument(parser: argparse.ArgumentParser) -> None:
    """Add --repo-src-dirs argument to parser."""
    parser.add_argument(
        "--repo-src-dirs",
        nargs="+",
        default=["src"],
        help="Source directories to filter commits (e.g., --repo-src-dirs src/)",
    )


def _add_repo_id_option(parser: argparse.ArgumentParser, required: bool = True) -> None:
    """Add --repo-id option argument to parser."""
    parser.add_argument("--repo-id", required=required, help="Repository identifier owner_repo:start_end")


def _add_trial_name_option(parser: argparse.ArgumentParser, required: bool = True) -> None:
    """Add --trial-name option argument to parser."""
    parser.add_argument("--trial-name", required=required, help="Trial name" if required else "Base trial name")


def _add_agents_argument(parser: argparse.ArgumentParser) -> None:
    """Add --agents argument to parser."""
    parser.add_argument("--agents", default="claudecode", help="Comma-separated agent list")


def _add_max_retries_argument(parser: argparse.ArgumentParser) -> None:
    """Add --max-retries argument to parser."""
    parser.add_argument("--max-retries", type=int, default=2, help="Maximum recovery retries")


def _add_result_root_argument(parser: argparse.ArgumentParser) -> None:
    """Add --result-root argument to parser."""
    parser.add_argument("--result-root", help="Alternate result root directory")


def _add_refresh_views_argument(parser: argparse.ArgumentParser) -> None:
    """Add --refresh-views argument to parser."""
    parser.add_argument(
        "--refresh-views",
        action="store_true",
        help="Refresh trials.sqlite views after run",
    )


def _handle_trial_commands(args: argparse.Namespace) -> int:
    data_root = get_data_root(args.data_root)

    if args.trial_cmd == "run":
        result_override = Path(args.result_root).expanduser().resolve() if args.result_root else None
        try:
            trials.run_trial(
                prompt_file=args.prompt_file,
                repo_id=args.repo_id,
                trial_name=args.trial_name,
                agents=args.agents,
                max_retries=args.max_retries,
                data_root=data_root,
                result_root_override=result_override,
                refresh_views=args.refresh_views,
            )
        except Exception as exc:  # pragma: no cover - CLI side effect
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.trial_cmd == "attach-session":
        try:
            trials.attach_session(args.trial_name, data_root=data_root)
        except Exception as exc:  # pragma: no cover
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.trial_cmd == "find-conversation":
        try:
            trial_name, files = trials.find_conversation_logs(args.trial_name, data_root=data_root)
        except Exception as exc:  # pragma: no cover
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        if not files:
            print("No conversation logs found", file=sys.stderr)
            return 1

        print()
        for file in files:
            print(f"DATA/result/{trial_name}/logs/{file.name}")
        return 0

    print(f"Unknown trial command: {args.trial_cmd}", file=sys.stderr)
    return 1


def _handle_postprocess_commands(args: argparse.Namespace) -> int:
    """Handle postprocess command - run Phase 3 (enrich) and/or Phase 4 (analyze)."""
    # Lazy import to avoid loading dependencies if not needed
    try:
        from . import postprocess
    except ImportError as e:
        print(f"Error: Postprocess module dependencies not available: {e}", file=sys.stderr)
        return 1

    data_root = get_data_root(getattr(args, "data_root", None))

    # Use --result-root if provided, otherwise default to DATA/result/
    result_root = getattr(args, "result_root", None)
    if result_root:
        result_base = Path(result_root).expanduser().resolve()
    else:
        result_base = get_result_base_root(data_root)

    trial_dir = select_trial(args.trial_name, result_base)

    # Auto-infer repo_id from trial_name if not provided
    repo_id_arg = getattr(args, "repo_id", None)
    if not repo_id_arg:
        inferred = infer_repo_id_from_trial_name(trial_dir.name, data_root)
        if inferred:
            repo_id_arg = inferred
            print(f"✓ Auto-inferred repo_id: {repo_id_arg}")
        else:
            print("Error: Could not infer repo_id from trial_name. Please provide repo_id explicitly.", file=sys.stderr)
            return 1

    preprocessed_dir, repo_id = select_preprocessed(repo_id_arg, data_root)
    print(f"Resolved trial: {trial_dir}")
    print(f"Resolved preprocessed: {preprocessed_dir.name}")

    # Load config NOW (after repo_id is known) and apply to args
    # This is necessary because config loading in _parse_postprocess_args() may miss
    # the config if repo_id was auto-inferred from trial_name
    try:
        config = _load_preproc_config(repo_id, required=False)
        if config:
            print(f"✓ Loading config from config/{repo_id.replace(':', '_')}.yaml")
            _apply_postprocess_config(args, config)
    except Exception as e:
        print(f"Warning: Failed to load config: {e}", file=sys.stderr)

    # Determine which phases to run based on subcommand
    postprocess_cmd = getattr(args, "postprocess_cmd", None)

    # Handle fix subcommand separately
    if postprocess_cmd == "fix":
        return postprocess.run_fix_cli(
            trial_dir,
            repo_id,
            data_root=data_root,
            add_missing=not getattr(args, "no_add_missing", False),
            remove_redundant=not getattr(args, "no_remove_redundant", False),
            dry_run=getattr(args, "dry_run", False),
            verbose=getattr(args, "verbose", False),
        )

    # Handle analyze/enrich/all subcommands
    enrich_only = postprocess_cmd == "enrich"
    analyze_only = postprocess_cmd == "analyze"
    # If postprocess_cmd is None or "all", run both phases

    return postprocess.run_postprocess_cli(
        trial_dir,
        repo_id,
        data_root=data_root,
        enrich_only=enrich_only,
        analyze_only=analyze_only,
        repo_src_dirs=getattr(args, "repo_src_dirs", ["src"]),
        exclude_patterns=getattr(args, "exclude", []) or [],
        test_patterns=getattr(args, "test_patterns", None),
        overwrite=getattr(args, "overwrite", False),
        dry_run=getattr(args, "dry_run", False),
        verbose=getattr(args, "verbose", False),
    )


def _handle_pipeline_multi_commands(args: argparse.Namespace) -> int:
    data_root = get_data_root(args.data_root)

    if args.pipeline_multi_cmd == "run":
        result_override = Path(args.result_root).expanduser().resolve() if args.result_root else None
        try:
            trials.run_multi_stage_trial(
                config_file=args.config_file,
                repo_id=args.repo_id,
                trial_name=args.trial_name,
                agents=args.agents,
                max_retries=args.max_retries,
                data_root=data_root,
                result_root_override=result_override,
                refresh_views=args.refresh_views,
                allow_rerun=args.allow_rerun,
                rerun_from_stage=getattr(args, "rerun_from_stage", None),
                rerun_postproc=getattr(args, "rerun_postproc", False),
            )
        except Exception as exc:  # pragma: no cover
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    print(f"Unknown pipeline-multi command: {args.pipeline_multi_cmd}", file=sys.stderr)
    return 1


def _handle_harness_commands(args: argparse.Namespace) -> int:
    """Handle harness pipeline commands."""
    data_root = get_data_root(args.data_root)

    if args.harness_cmd == "run":
        try:
            trials.run_harness_trial(
                config_file=args.config_file,
                repo_id=args.repo_id,
                trial_name=args.trial_name,
                agents=args.agents,
                max_retries=args.max_retries,
                data_root=data_root,
                source_result=getattr(args, "source_result", None),
                allow_rerun=args.allow_rerun,
                dry_run=args.dry_run,
            )
        except Exception as exc:  # pragma: no cover
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    print(f"Unknown harness command: {args.harness_cmd}", file=sys.stderr)
    return 1


def _handle_db_commands(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path).expanduser() if args.db_path else None
    sql_path = Path(args.sql_path).expanduser() if args.sql_path else None
    try:
        db_tools.refresh_views(db_path=db_path, sql_path=sql_path)
    except Exception as exc:  # pragma: no cover
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print("Views installed/updated OK")
    return 0


def _handle_preproc_commands(args: argparse.Namespace) -> int:
    data_root = get_data_root(getattr(args, "data_root", None))

    if args.preproc_cmd == "collect":
        output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
        dag_path = Path(args.dag_path).expanduser().resolve() if args.dag_path else None
        return preproc.collect_github_data(
            repo_spec=args.repo_id,
            data_root=data_root,
            workers=args.workers,
            repo_src_dirs=args.repo_src_dirs if hasattr(args, "repo_src_dirs") else None,
            dag_path=dag_path,
            overwrite=args.overwrite,
            no_index=args.no_index,
            output_dir=output_dir,
            tag_prefix=getattr(args, "tag_prefix", None),
            use_semver_filter=getattr(args, "use_semver_filter", True),
            test_dirs=getattr(args, "test_dirs", None),
            exclude=getattr(args, "exclude", None),
            verbose=args.verbose,
        )

    if args.preproc_cmd == "dag":
        output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
        return preproc.generate_commit_dag(
            repo_spec=args.repo_id,
            data_root=data_root,
            output_dir=output_dir,
            overwrite=args.overwrite,
            generate_image=args.generate_image,
            context_lines=args.context_lines,
            patch_context_lines=args.patch_context_lines,
            repo_src_dirs=args.repo_src_dirs,
            follow_depth=args.follow_depth,
            verify_with_patch=args.verify_with_patch,
            reduce_transitive=args.reduce_transitive,
            use_pygit2=args.use_pygit2,
            test_dirs=getattr(args, "test_dirs", None),
            exclude=getattr(args, "exclude", None),
            verbose=args.verbose,
        )

    if args.preproc_cmd == "codemaat":
        output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
        return preproc.generate_codemaat_log(
            repo_spec=args.repo_id,
            data_root=data_root,
            repo_src_dirs=args.repo_src_dirs if hasattr(args, "repo_src_dirs") else None,
            main_branch=args.main_branch,
            output_dir=output_dir,
            overwrite=args.overwrite,
            tag_prefix=getattr(args, "tag_prefix", None),
            test_dirs=getattr(args, "test_dirs", None),
            exclude=getattr(args, "exclude", None),
            verbose=args.verbose,
        )

    if args.preproc_cmd == "all":
        output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
        return preproc.run_all_preprocessing(
            repo_spec=args.repo_id,
            data_root=data_root,
            repo_src_dirs=args.repo_src_dirs if hasattr(args, "repo_src_dirs") else None,
            output_dir=output_dir,
            overwrite=args.overwrite,
            overwrite_skip_raw=args.overwrite_skip_raw,
            workers=args.workers,
            no_index=args.no_index,
            main_branch=args.main_branch,
            generate_image=args.generate_image,
            context_lines=args.context_lines,
            patch_context_lines=args.patch_context_lines,
            follow_depth=args.follow_depth,
            verify_with_patch=args.verify_with_patch,
            reduce_transitive=args.reduce_transitive,
            use_pygit2=args.use_pygit2,
            test_dirs=getattr(args, "test_dirs", None),
            exclude=getattr(args, "exclude", None),
            tag_prefix=getattr(args, "tag_prefix", None),
            use_semver_filter=getattr(args, "use_semver_filter", True),
            verbose=args.verbose,
        )

    if args.preproc_cmd == "symbol":
        output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
        return preproc.generate_symbols(
            repo_spec=args.repo_id,
            data_root=data_root,
            repo_src_dirs=args.repo_src_dirs if hasattr(args, "repo_src_dirs") else None,
            include_context=not args.no_context,
            context_lines=args.context_lines,
            output_dir=output_dir,
            overwrite=args.overwrite,
            verbose=args.verbose,
            test_dirs=getattr(args, "test_dirs", None),
            exclude=getattr(args, "exclude", None),
        )

    print(f"Unknown preproc command: {args.preproc_cmd}", file=sys.stderr)
    return 1


def _parse_trial_args(argv: list[str]) -> argparse.Namespace:
    """Build parser for trial commands and parse arguments."""
    parser = argparse.ArgumentParser(prog="deepcommit trial", description="Trial management commands")
    subparsers = parser.add_subparsers(dest="trial_cmd", required=True)

    run_parser = subparsers.add_parser("run", help="Launch a pipeline trial")
    run_parser.add_argument("--prompt-file", required=True, help="Prompt template relative to deepcommit/prompt")
    _add_repo_id_option(run_parser)
    _add_trial_name_option(run_parser)
    _add_agents_argument(run_parser)
    _add_max_retries_argument(run_parser)
    _add_result_root_argument(run_parser)
    _add_refresh_views_argument(run_parser)
    _add_data_root_argument(run_parser)

    attach_parser = subparsers.add_parser("attach-session", help="Attach CLAUDE CLI to a trial session")
    _add_trial_name_argument(attach_parser)
    _add_data_root_argument(attach_parser)

    find_parser = subparsers.add_parser("find-conversation", help="List conversation logs for a trial")
    _add_trial_name_argument(find_parser)
    _add_data_root_argument(find_parser)

    return parser.parse_args(argv)


def _parse_postprocess_args(argv: list[str]) -> argparse.Namespace:
    """Build parser for postprocess command and parse arguments.

    Usage:
        deepcommit postprocess analyze <trial_name> <repo_id> [options]
        deepcommit postprocess enrich <trial_name> <repo_id> [options]
        deepcommit postprocess all <trial_name> <repo_id> [options]
        deepcommit postprocess <trial_name> <repo_id> [options]  # same as 'all'
    """
    parser = argparse.ArgumentParser(
        prog="deepcommit postprocess",
        description="Run post-processing: enrichment and/or analysis",
    )

    # Check if first argument is a subcommand
    subcommands = {"analyze", "enrich", "all", "fix"}
    if argv and argv[0] in subcommands:
        # Subcommand mode
        subparsers = parser.add_subparsers(dest="postprocess_cmd", required=True)

        # Common arguments for all subcommands
        def _add_common_args(p: argparse.ArgumentParser) -> None:
            _add_trial_name_argument(p)
            _add_repo_id_argument(p, optional=True)
            _add_repo_src_dirs_argument(p)
            p.add_argument(
                "--exclude",
                nargs="+",
                default=None,  # None means "use config default"
                help="Glob patterns to exclude (e.g., '**/test/**'). If not specified, reads from config.",
            )
            p.add_argument(
                "--overwrite",
                action="store_true",
                help="Overwrite existing analysis results (default: skip if exists)",
            )
            p.add_argument(
                "--dry-run",
                action="store_true",
                help="Only output to console without saving files",
            )
            _add_verbose_argument(p)
            _add_result_root_argument(p)
            _add_data_root_argument(p)

        # Subcommand: analyze (Phase 4 only)
        analyze_parser = subparsers.add_parser("analyze", help="Run analysis only")
        _add_common_args(analyze_parser)

        # Subcommand: enrich (Phase 3 only)
        enrich_parser = subparsers.add_parser("enrich", help="Run enrichment only")
        _add_common_args(enrich_parser)

        # Subcommand: all (both phases)
        all_parser = subparsers.add_parser("all", help="Run both enrichment and analysis")
        _add_common_args(all_parser)

        # Subcommand: fix (add missing / remove redundant dependencies)
        fix_parser = subparsers.add_parser("fix", help="Fix dependencies (add missing, remove redundant)")
        _add_trial_name_argument(fix_parser)
        _add_repo_id_argument(fix_parser, optional=True)
        fix_parser.add_argument(
            "--no-add-missing",
            action="store_true",
            help="Do not add missing dependencies",
        )
        fix_parser.add_argument(
            "--no-remove-redundant",
            action="store_true",
            help="Do not remove redundant dependencies",
        )
        fix_parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only show what would be done without modifying files",
        )
        _add_verbose_argument(fix_parser)
        _add_result_root_argument(fix_parser)
        _add_data_root_argument(fix_parser)

    else:
        # No subcommand - default to running both phases
        parser.set_defaults(postprocess_cmd="all")
        _add_trial_name_argument(parser)
        _add_repo_id_argument(parser, optional=True)
        _add_repo_src_dirs_argument(parser)
        parser.add_argument(
            "--exclude",
            nargs="+",
            default=None,
            help="Glob patterns to exclude (e.g., '**/test/**'). If not specified, reads from config.",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Overwrite existing analysis results (default: skip if exists)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only output to console without saving files",
        )
        _add_verbose_argument(parser)
        _add_result_root_argument(parser)
        _add_data_root_argument(parser)

    # Parse arguments
    args = parser.parse_args(argv)

    # Load config and apply defaults
    if hasattr(args, "repo_id") and args.repo_id:
        try:
            config = _load_preproc_config(args.repo_id, required=False)
            if config:
                repo_id, time_span = parse_repo_spec(args.repo_id)
                print(f"✓ Loading config from config/{repo_id}_{time_span}.yaml")
                _apply_postprocess_config(args, config)
        except Exception as e:
            print(f"Warning: Failed to load config: {e}", file=sys.stderr)

    return args


def _apply_postprocess_config(args: argparse.Namespace, config: Dict[str, Any]) -> None:
    """Apply config values to postprocess arguments if not explicitly specified."""
    # repo_src_dirs: apply if using default value
    if hasattr(args, "repo_src_dirs") and args.repo_src_dirs == ["src"]:
        if "repo_src_dirs" in config:
            args.repo_src_dirs = config["repo_src_dirs"]

    # exclude: apply if None (not specified on command line)
    if hasattr(args, "exclude") and args.exclude is None:
        if "exclude" in config:
            args.exclude = config["exclude"]
        else:
            args.exclude = []  # Default to empty list

    # test_patterns: load from test_dirs in config (for test file detection)
    if not hasattr(args, "test_patterns") or args.test_patterns is None:
        if "test_dirs" in config:
            args.test_patterns = config["test_dirs"]


def _parse_pipeline_multi_args(argv: list[str]) -> argparse.Namespace:
    """Build parser for pipeline-multi commands and parse arguments."""
    parser = argparse.ArgumentParser(prog="deepcommit pipeline-multi", description="Multi-stage pipeline commands")
    subparsers = parser.add_subparsers(dest="pipeline_multi_cmd", required=True)

    run_parser = subparsers.add_parser("run", help="Run a multi-stage pipeline trial")
    run_parser.add_argument(
        "--config-file",
        default="multi_stage_pipeline.yaml",
        help="Pipeline config file (default: multi_stage_pipeline.yaml)",
    )

    # Reuse existing argument helpers
    _add_repo_id_option(run_parser)
    _add_trial_name_option(run_parser)
    _add_agents_argument(run_parser)
    _add_max_retries_argument(run_parser)
    _add_result_root_argument(run_parser)
    _add_refresh_views_argument(run_parser)
    run_parser.add_argument(
        "--allow-rerun", action="store_true", help="Allow reusing existing trial name (skip auto-increment)"
    )
    run_parser.add_argument(
        "--rerun-from-stage",
        type=str,
        help="Rerun pipeline from specific stage (e.g., stage2_milestone_consolidation). Creates new workspace with completed stages copied from source trial.",
    )
    run_parser.add_argument(
        "--rerun-postproc",
        action="store_true",
        help="Rerun Phase 3, 4, 5 (postprocessing) after Phase 2 completion. Requires Phase 2 to be fully completed. Uses same trial name.",
    )
    _add_data_root_argument(run_parser)

    return parser.parse_args(argv)


def _parse_harness_args(argv: list[str]) -> argparse.Namespace:
    """Build parser for harness pipeline commands and parse arguments."""
    parser = argparse.ArgumentParser(prog="deepcommit harness", description="Harness pipeline commands")
    subparsers = parser.add_subparsers(dest="harness_cmd", required=True)

    run_parser = subparsers.add_parser("run", help="Run a harness pipeline trial")
    run_parser.add_argument(
        "--config-file",
        default="harness_pipeline.yaml",
        help="Pipeline config file (default: harness_pipeline.yaml)",
    )

    # Required arguments
    _add_repo_id_option(run_parser)
    _add_trial_name_option(run_parser)

    # Optional arguments
    _add_agents_argument(run_parser)
    _add_max_retries_argument(run_parser)
    _add_data_root_argument(run_parser)

    run_parser.add_argument(
        "--source-result",
        type=str,
        default=None,
        help="Source result directory name to copy milestones.csv and dependencies.csv from. "
        "If not provided, auto-resolved from trial_name (e.g., 'baseline' -> '{repo_range}_baseline')",
    )
    run_parser.add_argument(
        "--allow-rerun",
        action="store_true",
        help="Allow reusing existing trial name (skip auto-increment)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only test setup (prompt rendering, directory structure) without executing agent",
    )

    return parser.parse_args(argv)


def _parse_db_args(argv: list[str]) -> argparse.Namespace:
    """Build parser for db commands and parse arguments."""
    parser = argparse.ArgumentParser(prog="deepcommit db", description="Database utilities")
    subparsers = parser.add_subparsers(dest="db_cmd", required=True)

    refresh_parser = subparsers.add_parser("refresh-views", help="Install or update trials.sqlite views")
    refresh_parser.add_argument("--db-path", help="Path to trials.sqlite", dest="db_path")
    refresh_parser.add_argument("--sql-path", help="Custom SQL file path", dest="sql_path")

    return parser.parse_args(argv)


def _load_preproc_config(repo_spec: str, required: bool = True) -> Dict[str, Any]:
    """
    Load preprocessing configuration from YAML file in config/ directory.

    Args:
        repo_spec: Repository specification in format 'owner_repo:start_end'
        required: If True, raise error when config file doesn't exist (default: True)

    Returns:
        Dictionary containing config values

    Raises:
        FileNotFoundError: If required=True and config file doesn't exist
    """
    try:
        # Parse repo_spec to extract repo_id and time_span using parse_repo_spec utility
        repo_id, time_span = parse_repo_spec(repo_spec)

        # Construct config file path
        config_path = Path("config") / f"{repo_id}_{time_span}.yaml"

        if not config_path.exists():
            if required:
                raise FileNotFoundError(
                    f"Configuration file not found: {config_path}\n"
                    f"Please create a configuration file for {repo_spec} with at least:\n"
                    f"  repo_src_dirs:\n"
                    f"    - src/\n"
                    f"See existing files in config/ directory for examples."
                )
            return {}

        # Load YAML config
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)

        deprecated_topology_keys = []
        for key in ("use_branch_out_model", "use_release_branch_model"):
            if config and key in config:
                deprecated_topology_keys.append(key)
                config.pop(key, None)

        for key in deprecated_topology_keys:
            print(
                f"Warning: {config_path} contains deprecated key '{key}'. "
                "Branch topology is auto-detected from the local git repository.",
                file=sys.stderr,
            )

        return config if config else {}
    except FileNotFoundError:
        raise
    except Exception as e:
        print(f"Warning: Failed to load config for {repo_spec}: {e}", file=sys.stderr)
        if required:
            raise
        return {}


def _apply_config_to_args(args: argparse.Namespace, config: Dict[str, Any], defaults: Dict[str, Any]) -> None:
    """
    Apply config values to parsed arguments if they weren't explicitly specified.

    Args:
        args: Parsed arguments namespace
        config: Configuration dictionary from YAML
        defaults: Default values for each argument
    """
    if not config:
        return

    # Map config keys to argument names
    config_arg_mapping = {
        "repo_src_dirs": "repo_src_dirs",
        "main_branch": "main_branch",
        "workers": "workers",
        "context_lines": "context_lines",
        "patch_context_lines": "patch_context_lines",
        "follow_depth": "follow_depth",
        "generate_image": "generate_image",
        "verify_with_patch": "verify_with_patch",
        "reduce_transitive": "reduce_transitive",
        "use_pygit2": "use_pygit2",
        # Test dirs and exclude patterns
        "test_dirs": "test_dirs",
        "exclude": "exclude",
        # Tag prefix for simplified version numbers
        "tag_prefix": "tag_prefix",
        # Semver filter control
        "use_semver_filter": "use_semver_filter",
    }

    for config_key, arg_name in config_arg_mapping.items():
        if config_key not in config:
            continue

        # Check if argument exists in namespace
        if not hasattr(args, arg_name):
            # Argument doesn't exist in argparse, set it directly from config
            setattr(args, arg_name, config[config_key])
            continue

        # Get current value
        current_value = getattr(args, arg_name)

        # Check if current value is the default (meaning user didn't specify it)
        if arg_name in defaults and current_value == defaults[arg_name]:
            # Apply config value
            setattr(args, arg_name, config[config_key])


def _parse_preproc_args(argv: list[str]) -> argparse.Namespace:
    """Build parser for preproc commands and parse arguments."""
    parser = argparse.ArgumentParser(prog="deepcommit preproc", description="Preprocessing commands")
    subparsers = parser.add_subparsers(dest="preproc_cmd", required=True)

    # Collect subcommand
    collect_parser = subparsers.add_parser("collect", help="Collect GitHub data (releases, PRs, issues)")
    _add_repo_id_argument(collect_parser)
    _add_data_root_argument(collect_parser)
    _add_verbose_argument(collect_parser)
    _add_repo_src_dirs_argument(collect_parser)

    collect_parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Number of concurrent workers for API calls (default: 10)",
    )
    collect_parser.add_argument(
        "--dag-path",
        help="Path to commit DAG JSON file for enriching main_commits.jsonl with commit_dag_stat field",
    )
    collect_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing data",
    )
    collect_parser.add_argument(
        "--no-index",
        action="store_true",
        help="Skip building index cache",
    )
    collect_parser.add_argument(
        "--output-dir",
        help="Override output directory (default: auto-resolved from repo_id)",
    )

    # DAG subcommand
    dag_parser = subparsers.add_parser("dag", help="Generate commit DAG for a repository")
    _add_repo_id_argument(dag_parser)
    _add_data_root_argument(dag_parser)
    _add_verbose_argument(dag_parser)
    _add_repo_src_dirs_argument(dag_parser)

    dag_parser.add_argument(
        "--output-dir",
        help="Output directory (default: DATA/github_data/preprocessed/<repo_id>/dag/)",
    )
    dag_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output files",
    )
    dag_parser.add_argument(
        "--generate-image",
        action="store_true",
        help="Generate PNG image from DOT file (requires graphviz)",
    )
    dag_parser.add_argument(
        "--context-lines",
        type=int,
        default=3,
        help="Number of context lines for git blame (default: 3)",
    )
    dag_parser.add_argument(
        "--patch-context-lines",
        type=int,
        default=3,
        help="Number of context lines for git apply (default: 3)",
    )
    dag_parser.add_argument(
        "--follow-depth",
        type=int,
        default=2,
        choices=[1, 2, 3],
        help="Depth of copy detection (1-3, default: 2)",
    )
    dag_parser.add_argument(
        "--no-verify-with-patch",
        dest="verify_with_patch",
        action="store_false",
        default=True,
        help="Disable git apply verification (faster but less accurate)",
    )
    dag_parser.add_argument(
        "--no-reduce-transitive",
        dest="reduce_transitive",
        action="store_false",
        default=True,
        help="Do not remove transitive redundant edges",
    )
    dag_parser.add_argument(
        "--use-pygit2",
        dest="use_pygit2",
        action="store_true",
        default=False,
        help="Use pygit2 library for git operations (faster, requires installation)",
    )
    dag_parser.add_argument(
        "--test-dirs",
        dest="test_dirs",
        nargs="*",
        default=None,
        help="Test directory/file patterns to exclude (e.g., 'tests/**' '**/*_test.go')",
    )
    dag_parser.add_argument(
        "--exclude",
        dest="exclude",
        nargs="*",
        default=None,
        help="Additional glob patterns to exclude (e.g., '**/testdata/**' '**/fixtures/**')",
    )

    # Code Maat subcommand
    codemaat_parser = subparsers.add_parser("codemaat", help="Generate Code Maat log for repository analysis")
    _add_repo_id_argument(codemaat_parser)
    _add_data_root_argument(codemaat_parser)
    _add_verbose_argument(codemaat_parser)
    _add_repo_src_dirs_argument(codemaat_parser)

    codemaat_parser.add_argument(
        "--main-branch",
        default="main",
        help="Main branch name (default: main)",
    )
    codemaat_parser.add_argument(
        "--output-dir",
        help="Output directory (default: DATA/github_data/preprocessed/{repo_id}_{time_span}/codemaat/)",
    )
    codemaat_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output file",
    )

    # All subcommand (dag -> collect -> codemaat)
    all_parser = subparsers.add_parser(
        "all", help="Run all preprocessing steps: dag -> collect -> codemaat, then copy result files"
    )
    _add_repo_id_argument(all_parser)
    _add_data_root_argument(all_parser)
    _add_verbose_argument(all_parser)
    _add_repo_src_dirs_argument(all_parser)

    all_parser.add_argument(
        "--output-dir",
        help="Base output directory (default: DATA/github_data/preprocessed/<repo_id>)",
    )
    all_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output files",
    )
    all_parser.add_argument(
        "--overwrite-skip-raw",
        action="store_true",
        help="Overwrite existing files but skip re-collecting raw data if it exists and is complete (implies --overwrite)",
    )
    all_parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Number of concurrent workers for API calls in collect (default: 10)",
    )
    all_parser.add_argument(
        "--no-index",
        action="store_true",
        help="Skip building index cache in collect",
    )
    all_parser.add_argument(
        "--main-branch",
        default="main",
        help="Main branch name for codemaat (default: main)",
    )
    all_parser.add_argument(
        "--generate-image",
        action="store_true",
        help="Generate PNG image from DOT file in dag (requires graphviz)",
    )
    all_parser.add_argument(
        "--context-lines",
        type=int,
        default=3,
        help="Number of context lines for git blame in dag (default: 3)",
    )
    all_parser.add_argument(
        "--patch-context-lines",
        type=int,
        default=3,
        help="Number of context lines for git apply in dag (default: 3)",
    )
    all_parser.add_argument(
        "--follow-depth",
        type=int,
        default=2,
        choices=[1, 2, 3],
        help="Depth of copy detection in dag (1-3, default: 2)",
    )
    all_parser.add_argument(
        "--no-verify-with-patch",
        dest="verify_with_patch",
        action="store_false",
        default=True,
        help="Disable git apply verification in dag (faster but less accurate)",
    )
    all_parser.add_argument(
        "--no-reduce-transitive",
        dest="reduce_transitive",
        action="store_false",
        default=True,
        help="Do not remove transitive redundant edges in dag",
    )
    all_parser.add_argument(
        "--use-pygit2",
        dest="use_pygit2",
        action="store_true",
        default=False,
        help="Use pygit2 library for git operations in dag (faster, requires installation)",
    )
    all_parser.add_argument(
        "--test-dirs",
        dest="test_dirs",
        nargs="*",
        default=None,
        help="Test directory/file patterns to exclude (e.g., 'tests/**' '**/*_test.go')",
    )
    all_parser.add_argument(
        "--exclude",
        dest="exclude",
        nargs="*",
        default=None,
        help="Additional glob patterns to exclude (e.g., '**/testdata/**' '**/fixtures/**')",
    )

    # Symbol extraction subcommand
    symbol_parser = subparsers.add_parser("symbol", help="Extract symbols from commit changes")
    _add_repo_id_argument(symbol_parser)
    _add_data_root_argument(symbol_parser)
    _add_verbose_argument(symbol_parser)
    _add_repo_src_dirs_argument(symbol_parser)

    symbol_parser.add_argument(
        "--output-dir",
        help="Output directory (default: DATA/github_data/preprocessed/<repo_id>_<time_span>/symbol/)",
    )
    symbol_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output files",
    )
    symbol_parser.add_argument(
        "--no-context",
        action="store_true",
        help="Only extract symbols from added lines (ignore context)",
    )
    symbol_parser.add_argument(
        "--context-lines",
        type=int,
        default=3,
        help="Number of context lines to include (default: 3)",
    )
    symbol_parser.add_argument(
        "--test-dirs",
        nargs="*",
        default=None,
        help="Test directory/file patterns to exclude (e.g., 'tests/**' '**/*_test.go')",
    )
    symbol_parser.add_argument(
        "--exclude",
        nargs="*",
        default=None,
        help="Additional glob patterns to exclude (e.g., '**/testdata/**')",
    )

    # Parse arguments
    args = parser.parse_args(argv)

    # Define default values for config application
    defaults = {
        "repo_src_dirs": ["src"],
        "main_branch": "main",
        "workers": 10,
        "context_lines": 3,
        "patch_context_lines": 3,
        "follow_depth": 2,
        "generate_image": False,
        "verify_with_patch": True,
        "reduce_transitive": True,
        "use_pygit2": False,
        # Test dirs and exclude defaults
        "test_dirs": None,
        "exclude": None,
        # Tag prefix for simplified version numbers (e.g., "tutanota-release-")
        "tag_prefix": None,
        # Use semver filter for tags (default: True)
        "use_semver_filter": True,
    }

    # Load config if repo_id is available
    if hasattr(args, "repo_id"):
        try:
            # For preproc commands, require config file to exist
            config = _load_preproc_config(args.repo_id, required=True)
            if config:
                try:
                    repo_id, time_span = parse_repo_spec(args.repo_id)
                    print(f"✓ Loading config from config/{repo_id}_{time_span}.yaml")
                except Exception:
                    # Fallback to manual replacement if parse fails
                    print(f"✓ Loading config from config/{args.repo_id.replace(':', '_').replace('/', '_')}.yaml")
                _apply_config_to_args(args, config, defaults)
        except FileNotFoundError as e:
            print(f"\n{'=' * 80}", file=sys.stderr)
            print("ERROR: Configuration file required for preprocessing", file=sys.stderr)
            print(f"{'=' * 80}", file=sys.stderr)
            print(f"{e}", file=sys.stderr)
            print(f"{'=' * 80}\n", file=sys.stderr)
            sys.exit(1)

    return args


def main(argv: list[str] | None = None) -> None:
    # Use sys.argv if argv is None
    if argv is None:
        argv = sys.argv[1:]

    # Handle top-level help (no command or help flag without command)
    if len(argv) == 0 or argv[0] in ("-h", "--help"):
        print("usage: deepcommit [-h] {trial,postprocess,db,preproc,pipeline-multi,harness} ...\n")
        print("DeepCommit CLI utilities\n")
        print("positional arguments:")
        print("  {trial,postprocess,db,preproc,pipeline-multi,harness}")
        print("    trial           Trial management commands")
        print("    postprocess     Run post-processing (enrichment + analysis)")
        print("    db              Database utilities")
        print("    preproc         Preprocessing commands")
        print("    pipeline-multi  Multi-stage pipeline commands")
        print("    harness         Harness pipeline commands\n")
        print("options:")
        print("  -h, --help        show this help message and exit")
        raise SystemExit(0)

    # Top-level parser to determine which command to run
    top_parser = argparse.ArgumentParser(
        prog="deepcommit",
        description="DeepCommit CLI utilities",
        add_help=False,
    )
    top_parser.add_argument(
        "command", choices=["trial", "postprocess", "db", "preproc", "pipeline-multi", "harness"], help="Command to run"
    )

    # Parse just the command (first argument)
    args, remaining = top_parser.parse_known_args(argv)

    # Parse and execute the appropriate command
    if args.command == "trial":
        sub_args = _parse_trial_args(remaining)
        code = _handle_trial_commands(sub_args)
    elif args.command == "postprocess":
        sub_args = _parse_postprocess_args(remaining)
        code = _handle_postprocess_commands(sub_args)
    elif args.command == "pipeline-multi":
        sub_args = _parse_pipeline_multi_args(remaining)
        code = _handle_pipeline_multi_commands(sub_args)
    elif args.command == "db":
        sub_args = _parse_db_args(remaining)
        code = _handle_db_commands(sub_args)
    elif args.command == "preproc":
        sub_args = _parse_preproc_args(remaining)
        code = _handle_preproc_commands(sub_args)
    elif args.command == "harness":
        sub_args = _parse_harness_args(remaining)
        code = _handle_harness_commands(sub_args)
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        code = 1

    raise SystemExit(code)


if __name__ == "__main__":
    main()
