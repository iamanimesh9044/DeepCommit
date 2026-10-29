#!/usr/bin/env python3
"""
MCP Server for DeepCommit Repository Analysis Tools
"""

import inspect
import json
import os
import sys
from typing import Any, Dict, List, Optional

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Import MCP FastMCP SDK
from mcp.server.fastmcp import FastMCP

from deepcommit.multirepo.resolver import WorkspaceResolver
from deepcommit.tools.commit_analysis import calculate_commits_src_loc as _calculate_commits_src_loc_impl
from deepcommit.tools.conflict_analysis.milestone_dependency_analyzer import (
    MilestoneDependencyAnalyzer,
)
from deepcommit.tools.conflict_analysis.milestone_internal_conflict_analyzer import (
    analyze_all_milestones,
)
from deepcommit.tools.dag_analysis import dag_analyzer
from deepcommit.tools.milestone_analysis import (
    analyze_commit_coverage as _analyze_commit_coverage_impl,
)
from deepcommit.tools.milestone_analysis import (
    analyze_milestone_size_balance as _analyze_milestone_size_balance_impl,
)
from deepcommit.tools.milestone_analysis import (
    validate_milestone_partitioning as validate_milestone_partitioning_impl,
)

# Import analysis tools
from deepcommit.tools.repo_entity.routed import RoutedEntityTools

AGENTBENCH_REPO_DIR = os.getenv("DEEPCOMMIT_REPO_DIR", os.getenv("AGENTBENCH_REPO_DIR", "repo"))
AGENTBENCH_PREPROC_DIR = os.getenv("DEEPCOMMIT_PREPROC_DIR", os.getenv("AGENTBENCH_PREPROC_DIR", "preprocessed"))
_COMMIT_DAG_PATH = os.getenv("DEEPCOMMIT_COMMIT_DAG_PATH", os.getenv("AGENTBENCH_COMMIT_DAG_PATH"))
if not _COMMIT_DAG_PATH:
    raise ValueError("DEEPCOMMIT_COMMIT_DAG_PATH environment variable must be set")
AGENTBENCH_COMMIT_DAG_PATH: str = _COMMIT_DAG_PATH


# Parse REPO_SRC_DIRS from environment variable (supports both string and JSON array)
_repo_src_dirs_env = os.getenv("REPO_SRC_DIRS", '["src"]')
try:
    # Try parsing as JSON array first
    REPO_SRC_DIRS = json.loads(_repo_src_dirs_env)
    if not isinstance(REPO_SRC_DIRS, list):
        # If it's not a list, wrap it in a list
        REPO_SRC_DIRS = [REPO_SRC_DIRS]
except json.JSONDecodeError:
    # Fallback: treat as a plain string (backward compatibility)
    REPO_SRC_DIRS = [_repo_src_dirs_env]

# Commit -> repo routing. Single repo (no DEEPCOMMIT_WORKSPACE_DIR): passthrough to AGENTBENCH_REPO_DIR / REPO_SRC_DIRS.
# Multi repo: every git call runs in the commit's own checkout with that repo's (unprefixed) src dirs, and returned
# file paths are re-prefixed `<repo>/` to match the merged data.
RESOLVER = WorkspaceResolver.from_env(fallback_repo_path=AGENTBENCH_REPO_DIR, fallback_src_dirs=REPO_SRC_DIRS)
ENTITY_TOOLS = RoutedEntityTools(RESOLVER, AGENTBENCH_PREPROC_DIR)


def _repo_arg_only_if_multi(fn):
    """Expose the optional `repo` tool argument only in multi-repo mode (single-repo tool schema is unchanged)."""
    if not RESOLVER.multi:
        sig = inspect.signature(fn)
        fn.__signature__ = sig.replace(parameters=[p for n, p in sig.parameters.items() if n != "repo"])
    return fn


# Parse REPO_TEST_DIRS and REPO_EXCLUDE from environment variables
_repo_test_dirs_env = os.getenv("REPO_TEST_DIRS", "")
_repo_exclude_env = os.getenv("REPO_EXCLUDE", "")

# Combine test dirs and exclude patterns into a single exclude list
REPO_EXCLUDE_PATTERNS: List[str] = []

# Add test directories to exclude patterns
if _repo_test_dirs_env:
    try:
        test_dirs = json.loads(_repo_test_dirs_env)
        if isinstance(test_dirs, list):
            REPO_EXCLUDE_PATTERNS.extend(test_dirs)
        else:
            REPO_EXCLUDE_PATTERNS.append(test_dirs)
    except json.JSONDecodeError:
        # Treat as a plain string
        REPO_EXCLUDE_PATTERNS.append(_repo_test_dirs_env)

# Add exclude patterns
if _repo_exclude_env:
    try:
        exclude_patterns = json.loads(_repo_exclude_env)
        if isinstance(exclude_patterns, list):
            REPO_EXCLUDE_PATTERNS.extend(exclude_patterns)
        else:
            REPO_EXCLUDE_PATTERNS.append(exclude_patterns)
    except json.JSONDecodeError:
        # Treat as a plain string
        REPO_EXCLUDE_PATTERNS.append(_repo_exclude_env)

# Initialize the FastMCP server
mcp = FastMCP("agentbench-analysis-tools")

# Get enabled tools from environment variable or use all tools as default
# Environment variable format: comma-separated tool names
# Example: ENABLED_MCP_TOOLS="get_commit_context,search_entities,analyze_commit_coverage_tool"
env_tools = os.getenv("ENABLED_MCP_TOOLS")

if env_tools:
    # Parse comma-separated tool names from environment
    ENABLED_TOOLS = [tool.strip() for tool in env_tools.split(",") if tool.strip()]
    print(f"[MCP Server] Enabled tools from environment: {ENABLED_TOOLS}", file=sys.stderr)
else:
    # Default: enable all available tools
    ENABLED_TOOLS = [
        "get_commit_context",
        "search_entities",
        # DAG analysis tools
        "validate_dag",
        "analyze_dag_parallelism",
        # Milestone analysis tools
        "analyze_commit_coverage",
        "analyze_milestone_size_balance",
        # Conflict analysis tools
        "analyze_milestone_conflicts",
        "analyze_dag_conflicts",
        "validate_milestone_partitioning",
        # Dependency check tools
        "check_dependency",
        # Commit analysis tools
        "calculate_commits_src_loc",
    ]
    print(f"[MCP Server] No ENABLED_MCP_TOOLS specified, using all {len(ENABLED_TOOLS)} tools", file=sys.stderr)

# Tool definitions based on Notion specifications
TOOL_DEFINITIONS = {
    "get_issue_context": {
        "title": "Issue Context Retriever",
        "description": "Return core issue metadata with optional expansions: conversation (comments), events timeline, and closing PRs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer", "description": "Issue number."},
                "expand": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["conversation", "events", "closing_prs"]},
                    "default": [],
                },
                "include_body": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to include the issue body (markdown).",
                },
                "body_limit_chars": {
                    "type": "integer",
                    "default": 8192,
                    "minimum": 0,
                    "description": "Max characters of issue body before truncation.",
                },
                "comments_mode": {
                    "type": "string",
                    "enum": ["summary", "full"],
                    "default": "summary",
                    "description": "When expanding conversation, choose summary highlights or full bodies (paginated).",
                },
                "comments_limit_chars": {
                    "type": "integer",
                    "default": 200,
                    "minimum": 0,
                    "description": "Max characters for comment excerpts or per-item body before truncation.",
                },
                "events_since": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Only include events at or after this time.",
                },
                "limit": {
                    "type": "integer",
                    "default": 200,
                    "minimum": 1,
                    "description": "Page size for list-like expansions (conversation, events, closing_prs).",
                },
                "cursor": {"type": "string", "description": "Pagination cursor for list-like expansions."},
            },
            "required": ["issue_number"],
        },
    },
    "get_pr_context": {
        "title": "Pull Request Context Retriever",
        "description": "Return core PR metadata with optional expansions: commits, files, timeline, comments, checks, and linked issues.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pr_number": {"type": "integer", "description": "Pull Request number."},
                "expand": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["pr_commits", "files", "timeline", "comments", "checks", "linked_issues"],
                    },
                    "default": [],
                },
                "include_body": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to include the PR body (markdown).",
                },
                "body_limit_chars": {
                    "type": "integer",
                    "default": 8192,
                    "minimum": 0,
                    "description": "Max characters of PR body before truncation.",
                },
                "comments_mode": {
                    "type": "string",
                    "enum": ["summary", "full"],
                    "default": "summary",
                    "description": "When expanding comments, choose summary highlights or full bodies (paginated).",
                },
                "comments_limit_chars": {
                    "type": "integer",
                    "default": 200,
                    "minimum": 0,
                    "description": "Max characters for comment excerpts.",
                },
                "limit": {
                    "type": "integer",
                    "default": 200,
                    "minimum": 1,
                    "description": "Page size for list expansions.",
                },
                "cursor": {"type": "string", "description": "Pagination cursor."},
            },
            "required": ["pr_number"],
        },
    },
    "get_commit_context": {
        "title": "Commit Context Retriever",
        "description": "Return core metadata for a commit SHA with optional expansions: files, stats, PR references, diff snippet, and issue references.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sha": {"type": "string", "description": "Commit SHA (full or uniquely resolvable)."},
                "expand": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["files", "stats", "pr_refs", "diff", "issue_refs", "releases"],
                    },
                    "default": [],
                    "description": "Which sub-resources to expand.",
                },
                "diff_limit_lines": {
                    "type": "integer",
                    "default": 200,
                    "minimum": 0,
                    "description": "Max lines for diff snippet before truncation.",
                },
                "files_limit": {
                    "type": "integer",
                    "default": 100,
                    "minimum": 1,
                    "description": "Max files to return when expanding files.",
                },
                "cursor": {"type": "string", "description": "Pagination cursor for file list."},
            },
            "required": ["sha"],
        },
    },
    "get_release_context": {
        "title": "Release Context (Index-Only, Previous Tag Baseline)",
        "description": "Return release metadata (with optional notes) and index-level change lists (commit SHAs, file paths, PR/Issue numbers) for a tag, using the previous tag as the fixed baseline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Target release tag to inspect."},
                "include_notes": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to include release notes (markdown).",
                },
                "notes_limit_chars": {
                    "type": "integer",
                    "default": 8192,
                    "minimum": 0,
                    "description": "Max characters for notes before truncation.",
                },
                "include_merges": {
                    "type": "boolean",
                    "default": True,
                    "description": "Only affects the commits list: include merge commits in the window summary.",
                },
                "expand": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["commits", "files", "prs", "issues"]},
                    "default": [],
                    "description": "Which index lists to expand.",
                },
                "limit": {
                    "type": "integer",
                    "default": 100,
                    "minimum": 1,
                    "description": "Max items per expanded list page.",
                },
                "cursor": {"type": "string", "description": "Pagination cursor for expanded lists."},
            },
            "required": ["tag"],
        },
    },
    "get_commits_diff": {
        "title": "Commits Window Diff",
        "description": "Compute a window diff between two refs (base_ref -> head_ref) with optional summary, file list, patch snippets, and commit list.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "base_ref": {"type": "string", "description": "Baseline ref/sha/tag (rev-parse resolvable)."},
                "head_ref": {"type": "string", "description": "Target ref/sha/tag (rev-parse resolvable)."},
                "expand": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["summary", "files", "patch", "commits"]},
                    "default": [],
                },
                "paths_glob": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional path filters (gitignore/pathspec). Affects files/patch outputs and summary.",
                },
                "filter_commits_by_paths": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, also filter the commit list by paths_glob.",
                },
                "detect_renames": {"type": "boolean", "default": True},
                "rename_similarity_threshold": {"type": "integer", "minimum": 0, "maximum": 100, "default": 50},
                "include_merges": {
                    "type": "boolean",
                    "default": True,
                    "description": "Affects commits list only; tree diff is unaffected.",
                },
                "commits_order": {"type": "string", "enum": ["time", "topo", "first_parent"], "default": "time"},
                "first_parent_only": {"type": "boolean", "default": False},
                "patch_limit_lines": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 3000,
                    "description": "Maximum lines across returned patches before truncation.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 200,
                    "description": "Page size for list expansions (files/commits).",
                },
                "cursor": {"type": "string", "description": "Pagination cursor for list expansions."},
            },
            "required": ["base_ref", "head_ref"],
        },
    },
    "search_entities": {
        "title": "Unified Repo Entity Search",
        "description": "Search commits, PRs, releases, and issues with keyword/structured filters and optional per-type expansions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["commit", "pr", "release", "issue"]},
                    "default": ["commit", "pr", "release", "issue"],
                    "description": "Entity types to search.",
                },
                "search_mode": {
                    "type": "string",
                    "enum": ["keyword", "struct", "mixed"],
                    "default": "mixed",
                    "description": "Search mode: keyword (text), struct (filters), or mixed.",
                },
                "query": {"type": "string", "description": "Keyword or light DSL query."},
                "filters": {
                    "type": "object",
                    "description": "Structured filters (e.g., date ranges, authors, labels).",
                },
                "expand": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Per-type expansions (e.g., commit.files, pr.stats).",
                },
                "notes_limit_chars": {"type": "integer", "default": 512, "minimum": 0},
                "conversation_limit_chars": {"type": "integer", "default": 200, "minimum": 0},
                "snippet_limit_chars": {"type": "integer", "default": 160, "minimum": 0},
                "order_by": {
                    "type": "string",
                    "enum": ["relevance", "updated_at", "created_at"],
                    "default": "relevance",
                },
                "limit": {"type": "integer", "default": 50, "minimum": 1, "description": "Page size."},
                "cursor": {"type": "string", "description": "Pagination cursor."},
            },
            "required": [],
        },
    },
}


# Register tools using FastMCP decorators


if "get_issue_context" in ENABLED_TOOLS:

    @mcp.tool()
    @_repo_arg_only_if_multi
    def get_issue_context(
        issue_number: int,
        repo: Optional[str] = None,
        expand: Optional[List[str]] = None,
        include_body: bool = True,
        body_limit_chars: int = 8192,
        comments_mode: str = "summary",
        comments_limit_chars: int = 200,
        events_since: Optional[str] = None,
        limit: int = 200,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Return core issue metadata with optional expansions: conversation (comments), events timeline, and closing PRs.

        Args:
            issue_number: Issue number
            repo: Repo name (multi-repo workspaces only; issue numbers are per repo)
            expand: Optional expansions ["conversation", "events", "closing_prs"]
            include_body: Whether to include issue body
            body_limit_chars: Max characters for body before truncation
            comments_mode: "summary" or "full" for conversation expansion
            comments_limit_chars: Max characters for comment excerpts
            events_since: Only include events after this time (ISO8601)
            limit: Page size for list expansions
            cursor: Pagination cursor
        """
        return ENTITY_TOOLS.get_issue_context(
            issue_number=issue_number,
            repo=repo,
            expand=expand,
            include_body=include_body,
            body_limit_chars=body_limit_chars,
            comments_mode=comments_mode,
            comments_limit_chars=comments_limit_chars,
            events_since=events_since,
            limit=limit,
            cursor=cursor,
        )


if "get_pr_context" in ENABLED_TOOLS:

    @mcp.tool()
    @_repo_arg_only_if_multi
    def get_pr_context(
        pr_number: int,
        repo: Optional[str] = None,
        expand: Optional[List[str]] = None,
        include_body: bool = True,
        body_limit_chars: int = 8192,
        comments_mode: str = "summary",
        comments_limit_chars: int = 200,
        limit: int = 200,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Return core PR metadata with optional expansions: commits, files, timeline, comments, checks, and linked issues.

        Args:
            pr_number: Pull Request number
            repo: Repo name (multi-repo workspaces only; PR numbers are per repo)
            expand: Optional expansions ["pr_commits", "files", "timeline", "comments", "checks", "linked_issues"]
            include_body: Whether to include PR body
            body_limit_chars: Max characters for body before truncation
            comments_mode: "summary" or "full" for comments
            comments_limit_chars: Max characters for comment excerpts
            limit: Page size for list expansions
            cursor: Pagination cursor
        """
        return ENTITY_TOOLS.get_pr_context(
            pr_number=pr_number,
            repo=repo,
            expand=expand,
            include_body=include_body,
            body_limit_chars=body_limit_chars,
            comments_mode=comments_mode,
            comments_limit_chars=comments_limit_chars,
            limit=limit,
            cursor=cursor,
        )


if "get_commit_context" in ENABLED_TOOLS:

    @mcp.tool()
    def get_commit_context(
        sha: str,
        expand: Optional[List[str]] = None,
        diff_limit_lines: int = 200,
        files_limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Return core metadata for a commit SHA with optional expansions: files, stats, PR references, diff snippet, and issue references.

        Args:
            sha: Commit SHA (full or uniquely resolvable)
            expand: Which sub-resources to expand ["files", "stats", "pr_refs", "diff", "issue_refs", "releases"]
            diff_limit_lines: Max lines for diff snippet before truncation
            files_limit: Max files to return when expanding files
            cursor: Pagination cursor for file list
        """
        params = {
            "sha": sha,
            "expand": expand,
            "diff_limit_lines": diff_limit_lines,
            "files_limit": files_limit,
            "cursor": cursor,
        }

        # Remove 'include_renames' from params since it's not in the original function signature
        params.pop("include_renames", None)

        return ENTITY_TOOLS.get_commit_context(**params)


if "get_release_context" in ENABLED_TOOLS:

    @mcp.tool()
    @_repo_arg_only_if_multi
    def get_release_context(
        tag: str,
        repo: Optional[str] = None,
        include_notes: bool = True,
        notes_limit_chars: int = 8192,
        include_merges: bool = True,
        expand: Optional[List[str]] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Return release metadata (with optional notes) and index-level change lists for a tag, using the previous tag as baseline.

        Args:
            tag: Target release tag to inspect (multi-repo: may be '<repo>:<tag>')
            repo: Repo name (multi-repo workspaces only; inferred when the tag exists in exactly one repo)
            include_notes: Whether to include release notes
            notes_limit_chars: Max characters for notes before truncation
            include_merges: Include merge commits in commits list
            expand: Which index lists to expand ["commits", "files", "prs", "issues"]
            limit: Max items per expanded list page
            cursor: Pagination cursor
        """
        return ENTITY_TOOLS.get_release_context(
            tag=tag,
            repo=repo,
            include_notes=include_notes,
            notes_limit_chars=notes_limit_chars,
            include_merges=include_merges,
            expand=expand,
            limit=limit,
            cursor=cursor,
        )


if "get_commits_diff" in ENABLED_TOOLS:

    @mcp.tool()
    @_repo_arg_only_if_multi
    def get_commits_diff(
        base_ref: str,
        head_ref: str,
        repo: Optional[str] = None,
        expand: Optional[List[str]] = None,
        paths_glob: Optional[List[str]] = None,
        filter_commits_by_paths: bool = False,
        detect_renames: bool = True,
        rename_similarity_threshold: int = 50,
        include_merges: bool = True,
        commits_order: str = "time",
        first_parent_only: bool = False,
        patch_limit_lines: int = 3000,
        limit: int = 200,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Compute a window diff between two refs (base_ref -> head_ref) with optional summary, file list, patch snippets, and commit list.

        Args:
            base_ref: Baseline ref/sha/tag (rev-parse resolvable)
            head_ref: Target ref/sha/tag (rev-parse resolvable)
            repo: Repo name (multi-repo workspaces only; inferred from commit shas or a '<repo>:<ref>' prefix)
            expand: Optional expansions ["summary", "files", "patch", "commits"]
            paths_glob: Optional path filters (affects files/patch outputs)
            filter_commits_by_paths: Also filter commit list by paths_glob
            detect_renames: Whether to detect renames
            rename_similarity_threshold: Rename similarity threshold (0-100)
            include_merges: Include merge commits in commits list
            commits_order: Commit list ordering ("time", "topo", "first_parent")
            first_parent_only: Only follow first-parent chain
            patch_limit_lines: Max lines for patch snippets
            limit: Page size for list expansions
            cursor: Pagination cursor
        """
        return ENTITY_TOOLS.get_commits_diff(
            base_ref=base_ref,
            head_ref=head_ref,
            repo=repo,
            expand=expand,
            paths_glob=paths_glob,
            filter_commits_by_paths=filter_commits_by_paths,
            detect_renames=detect_renames,
            rename_similarity_threshold=rename_similarity_threshold,
            include_merges=include_merges,
            commits_order=commits_order,
            first_parent_only=first_parent_only,
            patch_limit_lines=patch_limit_lines,
            limit=limit,
            cursor=cursor,
        )


if "search_entities" in ENABLED_TOOLS:

    @mcp.tool()
    def search_entities(
        types: Optional[List[str]] = None,
        search_mode: str = "mixed",
        query: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        expand: Optional[List[str]] = None,
        notes_limit_chars: int = 512,
        conversation_limit_chars: int = 200,
        snippet_limit_chars: int = 160,
        order_by: str = "relevance",
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Search commits, PRs, releases, and issues with keyword/structured filters and optional per-type expansions.

        Args:
            types: Entity types to search ["commit", "pr", "release", "issue"]
            search_mode: Search mode ("keyword", "struct", "mixed")
            query: Keyword or light DSL query
            filters: Structured filters
            expand: Per-type expansions
            notes_limit_chars: Max chars for release notes excerpts
            conversation_limit_chars: Max chars for issue conversation highlights
            snippet_limit_chars: Max chars for text highlights
            order_by: Result ordering ("relevance", "updated_at", "created_at")
            limit: Page size
            cursor: Pagination cursor
        """
        return ENTITY_TOOLS.search_entities(
            types=types,
            search_mode=search_mode,
            query=query,
            filters=filters,
            expand=expand,
            notes_limit_chars=notes_limit_chars,
            conversation_limit_chars=conversation_limit_chars,
            snippet_limit_chars=snippet_limit_chars,
            order_by=order_by,
            limit=limit,
            cursor=cursor,
        )


# DAG Analysis Tools
if "validate_dag" in ENABLED_TOOLS:

    @mcp.tool()
    def validate_dag(csv_path: str) -> Dict[str, Any]:
        """
        Validate if a graph is a proper DAG (Directed Acyclic Graph) by checking for cycles.

        Args:
            csv_path: Path to CSV file with columns 'prerequisite_id' and 'dependant_id' representing edges

        Returns:
            Dictionary containing:
            - is_valid_dag: Boolean indicating if the graph is a valid DAG
            - has_cycle: Boolean indicating presence of cycles
            - node_count: Total number of nodes
            - edge_count: Total number of edges
            - cycles: List of detected cycles (if any)
            - cycle_count: Number of cycles found (if any)
            - topological_order: Topological sort of nodes (if acyclic)

        Example:
            result = validate_dag("/path/to/dependencies.csv")
            if result["is_valid_dag"]:
                print(f"Valid DAG with {result['node_count']} nodes")
            else:
                print(f"Found {result['cycle_count']} cycles")
        """
        return dag_analyzer.validate_dag(csv_path, save_results=False)


if "analyze_dag_parallelism" in ENABLED_TOOLS:

    @mcp.tool()
    def analyze_dag_parallelism(csv_path: str) -> Dict[str, Any]:
        """
        Analyze the parallelism characteristics and performance metrics of a DAG.

        Args:
            csv_path: Path to CSV file with columns 'prerequisite_id' and 'dependant_id' representing edges

        Returns:
            Dictionary containing:
            - critical_path_length: Length of the longest path (critical path)
            - critical_path_nodes: List of nodes on the critical path
            - maximum_width: Maximum number of nodes that can execute in parallel
            - total_nodes: Total number of nodes in the DAG
            - total_edges: Total number of edges in the DAG
            - levels: List of node sets that can execute in parallel
            - level_widths: Number of nodes at each parallel level
            - level_widths_stats: Statistical distribution of level widths
              - mean: Average width across all levels
              - median: Median width (more robust than mean)
              - std_dev: Standard deviation (measures variability)
              - min: Minimum level width
              - max: Maximum level width
              - variance_coefficient: Coefficient of variation (std_dev/mean, measures relative variability)

        Example:
            metrics = analyze_dag_parallelism("/path/to/dependencies.csv")
            print(f"Critical path: {' -> '.join(metrics['critical_path_nodes'])}")
            print(f"Maximum parallelism: {metrics['maximum_width']} nodes")
            print(f"Level width median: {metrics['level_widths_stats']['median']} nodes")
        """
        return dag_analyzer.analyze_dag_parallelism_tool(csv_path, save_results=False)


# Milestone Analysis Tools
if "analyze_commit_coverage" in ENABLED_TOOLS:

    @mcp.tool()
    def analyze_commit_coverage(
        csv_file: str,
    ) -> Dict[str, Any]:
        """
        Analyze milestone commit coverage rates against main branch commits.

        This tool analyzes how well milestones cover the commits in the repository,
        providing insights into coverage rates, distribution across categories, and
        validates commit integrity (invalid commits and overlapping commits).

        IMPORTANT: Valid milestones require empty invalid_commits and overlapping_commits.

        Args:
            csv_file: Path to the milestone CSV file contains columns:
                     - id: Milestone identifier
                     - title: Milestone title
                     - commits: Semicolon-separated list of commit SHAs

        Returns:
            Dictionary containing:
            - main_coverage: Percentage of main commits covered by milestones
            - total_main_commits: Total number of main branch commits
            - invalid_commits: Dict mapping milestone IDs to list of commits not in main branch
            - overlapping_commits: Dict mapping commit SHAs to list of milestone IDs they appear in
        """
        main_commits_file = f"{AGENTBENCH_PREPROC_DIR}/main_commits.jsonl"
        return _analyze_commit_coverage_impl(csv_file, main_commits_file, save_results=False)


if "analyze_milestone_size_balance" in ENABLED_TOOLS:

    @mcp.tool()
    def analyze_milestone_size_balance(
        csv_file: str,
        # save_results: bool = False,
    ) -> Dict[str, Any]:
        """
        Analyze milestone size balance and distribution, including LoC-weighted metrics.

        This tool evaluates how evenly work is distributed across milestones,
        providing both simple and LoC-weighted statistics to assess balance.

        The tool automatically filters files using:
        - REPO_SRC_DIRS environment variable for source directory paths
        - REPO_TEST_DIRS and REPO_EXCLUDE environment variables for exclusion patterns

        Args:
            csv_file: Path to the milestone CSV file with columns:
                     - id: Milestone identifier
                     - title: Milestone title
                     - category: Milestone category
                     - core_pr_commits: Semicolon-separated list of commit SHAs
                     - commit_count: Number of commits in the milestone
            save_results: Whether to save results to JSON file (default: False)
            repo_src_dir: Optional path prefix to filter files for LoC calculation
                         Example: "src/" to only count lines changed in src directory
                         If None, all files are counted (default behavior)

        Returns:
            Dictionary containing:
            - milestones: List of milestone data
            - total_count: Total number of milestones
            - size_stats: Basic size statistics
                - mean: Average milestone size (commits)
                - median: Median milestone size
                - std_dev: Standard deviation
                - min/max: Minimum and maximum sizes
                - cv: Coefficient of variation
            - loc_stats: LoC statistics (if repo_path provided)
                - loc_mean/median/std: LoC statistics
                - loc_cv: LoC coefficient of variation (workload balance indicator)
                - loc_size_correlation: Correlation between LoC and commit count
                - path_prefix: Path prefix used for filtering (if repo_src_dir specified)
                - exclude_patterns: Exclude patterns used for filtering
            - output_file: Path to saved file (if save_results=True)
            - output_message: Message about saved file (if save_results=True)

        Example:
            # Count all files
            result = analyze_milestone_size_balance_tool(
                "/data/milestones.csv"
            )

            # Count only src/ directory
            result = analyze_milestone_size_balance_tool(
                "/data/milestones.csv",
                repo_src_dir="src/"
            )
            print(f"Average size: {result['size_stats']['mean']:.1f} commits")
            print(f"Standard deviation: {result['size_stats']['std_dev']:.1f}")
            if 'loc_stats' in result:
                print(f"LoC CV: {result['loc_stats']['loc_cv']:.3f}")
        """
        return _analyze_milestone_size_balance_impl(
            csv_file=csv_file,
            repo_path=AGENTBENCH_REPO_DIR,
            # save_results=save_results,
            repo_src_dir=REPO_SRC_DIRS,
            exclude_patterns=REPO_EXCLUDE_PATTERNS if REPO_EXCLUDE_PATTERNS else None,
            resolver=RESOLVER,
        )


# Conflict Analysis Tools
if "analyze_milestone_conflicts" in ENABLED_TOOLS:

    @mcp.tool()
    def analyze_milestone_conflicts(
        milestone_csv_path: str,
        milestone_ids: Optional[List[str]] = None,
        main_branch: str = "main",
        dependencies_csv_path: Optional[str] = None,
        split_metadata_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Analyze conflicts within milestones using DAG-based dependency completeness checking.

        This tool performs a comprehensive conflict analysis for milestones by checking
        whether milestone commits form complete dependency chains in the commit DAG.
        Uses static DAG analysis instead of actual cherry-pick testing for better performance.

        Args:
            milestone_csv_path: Path to the milestone CSV file with columns:
                              - id: Milestone identifier
                              - title: Milestone title
                              - core_pr_commits: Semicolon-separated commit SHAs
                              - main_commits: Semicolon-separated main branch commits
            milestone_ids: List of specific milestone IDs to analyze (None for all)
            main_branch: Name of the main branch (default: "main")
            dependencies_csv_path: Optional path to dependencies CSV file with columns:
                                 - prerequisite_id: prerequisite milestone ID (upstream; must complete first)
                                 - dependant_id: dependant milestone ID (downstream; depends on source)
                                 If provided, enables frontier prerequisites analysis (required vs declared)
            split_metadata_dir: Optional path to split commit metadata directory
                              (e.g., stage4_decomp/output/commit_splits)

        Returns:
            Dictionary containing:
            - metadata: Analysis metadata (repo path, analysis time, commit_dag path, version)
            - milestones: Detailed conflict analysis for each milestone
                - milestone_id: Milestone identifier
                - title: Milestone title
                - total_commits: Total number of commits in milestone
                - valid_commits: Number of commits found in DAG
                - invalid_commits: List of commits not found in DAG
                - incomplete_chains: List of incomplete dependency chains
                - missing_commits: List of unique commits missing from dependency paths (extracted from incomplete_chains)
                - conflict_count: Number of incomplete chains found
                - frontier_prerequisites: Analysis of required/provided frontier commits
                    - required_frontier: All prerequisite commits needed (based on commit DAG)
                    - provided_frontier: Commits provided by prerequisite milestones (if dependencies_csv_path provided)
                    - missing_frontier: Required commits not provided by dependencies
            - summary: Overall summary statistics
                - total_milestones_analyzed: Number of milestones analyzed
                - milestones_with_incomplete_chains: Milestones with dependency issues
                - total_incomplete_chains: Total incomplete dependency chains found

        Example:
            result = analyze_milestone_conflicts_tool(
                "/data/milestones.csv",
                ["milestone_001", "milestone_002"]
            )
            print(f"Total conflicts: {result['summary']['total_incomplete_chains']}")
            print(f"Milestones with issues: {result['summary']['milestones_with_incomplete_chains']}")
        """
        # Call DAG-based analysis function
        # Note: All print statements are redirected to stderr to avoid polluting MCP stdio communication
        analysis_results = analyze_all_milestones(
            repo_path=AGENTBENCH_REPO_DIR,
            milestone_csv_path=milestone_csv_path,
            commit_dag_path=AGENTBENCH_COMMIT_DAG_PATH,
            main_branch=main_branch,
            milestone_ids=milestone_ids,
            dependencies_csv_path=dependencies_csv_path,
            split_metadata_dir=split_metadata_dir,
            resolver=RESOLVER,
        )

        return analysis_results


if "analyze_dag_conflicts" in ENABLED_TOOLS:

    @mcp.tool()
    def analyze_dag_conflicts(
        milestone_csv_path: str,
        dependencies_csv_path: str,
        detect_redundant: bool = True,
        split_metadata_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Analyze DAG dependency correctness using commit-level dependency inference and cycle detection.

        This tool validates the declared DAG dependencies by:
        1. Inferring dependencies from commit DAG (based on commit-level dependencies)
        2. Comparing inferred vs existing dependencies to find missing/extra dependencies
        3. Detecting cycles in existing, inferred, and merged dependency graphs
        4. Optionally detecting redundant transitive dependencies

        Should be run AFTER ensuring milestones have no internal conflicts.

        Args:
            milestone_csv_path: Path to milestone CSV file with columns:
                              - id: Milestone identifier
                              - title: Milestone title
                              - category: Milestone category
                              - core_pr_commits: Semicolon-separated commit SHAs (or commits)
                              - main_commits: Semicolon-separated main branch commits
            dependencies_csv_path: Path to dependencies CSV with columns:
                                 - prerequisite_id: Source milestone ID
                                 - dependant_id: Target milestone ID (dependency)
            detect_redundant: Whether to detect redundant transitive dependencies (default: True)
            split_metadata_dir: Optional path to split commit metadata directory
                              (e.g., stage4_decomp/output/commit_splits)

        Returns:
            Dictionary containing:
            - is_valid_dag: Boolean - whether the dependency graph has no cycles
            - redundant_dependencies: List of redundant transitive dependencies [[source, target], ...]
                                     Empty list if no redundant edges found or graph contains cycles
            - cycles: Cycle detection results
                - enriched_cycles: List of cycles in merged dependencies (inferred + existing)
                - existing_cycles: List of cycles in existing dependencies
                - inferred_cycles: List of cycles in inferred dependencies (from commit DAG)
        """

        analyzer = MilestoneDependencyAnalyzer(
            commit_dag_path=AGENTBENCH_COMMIT_DAG_PATH,
            milestone_csv_path=milestone_csv_path,
            dependencies_csv_path=dependencies_csv_path,
            detect_redundant=detect_redundant,
            split_metadata_dir=split_metadata_dir,
            repo_path=AGENTBENCH_REPO_DIR,
            resolver=RESOLVER,
        )

        # Run the enrichment analysis (includes cycle detection)
        results = analyzer.enrich_dependencies()

        # Return essential conflict detection fields (cycles + redundant edges)
        return {
            "is_valid_dag": results["summary"]["is_valid_dag"],
            "redundant_dependencies": results.get("redundant_dependencies", []),
            "cycles": {
                "enriched_cycles": results["cycles"]["enriched_cycles"],
                "existing_cycles": results["cycles"]["existing_cycles"],
                "inferred_cycles": results["cycles"]["inferred_cycles"],
            },
        }


if "validate_milestone_partitioning" in ENABLED_TOOLS:

    @mcp.tool()
    def validate_milestone_partitioning(
        milestone_csv_path: str,
        split_metadata_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Validate milestone partitioning by inferring dependencies from commit DAG and detecting cycles.

        This tool helps determine if milestone partitioning is reasonable by:
        1. Inferring dependencies ONLY from commit DAG (no existing dependencies needed)
        2. Detecting cycles in the inferred dependency graph
        3. If cycles exist, it indicates milestone partitioning issues (commits that depend on each other are split into different milestones)

        Use this tool to:
        - Validate initial milestone definitions before creating dependencies.csv
        - Check if milestones are logically separated (no bidirectional dependencies)
        - Identify which milestones should be merged

        Args:
            milestone_csv_path: Path to milestone CSV file with columns:
                              - id: Milestone identifier
                              - title: Milestone title
                              - category: Milestone category
                              - commits: Semicolon-separated commit SHAs
            split_metadata_dir: Optional path to split commit metadata directory
                              (e.g., stage4_decomp/output/commit_splits)

        Returns:
            Dictionary containing:
            - is_valid_partitioning: Boolean - whether milestones can form a DAG (no cycles)
            - inferred_dependencies: List of [source, target] dependency pairs
            - inferred_dependencies_count: Number of inferred dependencies
            - cycles: List of cycles detected in inferred dependencies
            - cycles_count: Number of cycles found
            - cycle_edge_details: For each cycle, statistics about edges (commit dependency counts)
            - problematic_milestones: Set of milestone IDs involved in cycles
            - recommendation: Suggestions for fixing partitioning issues

        Example:
            result = validate_milestone_partitioning_tool(
                milestone_csv_path="/data/milestones.csv"
            )
            if not result['is_valid_partitioning']:
                print(f"Found {result['cycles_count']} cycles")
                print(f"Problematic milestones: {result['problematic_milestones']}")
                print(result['recommendation'])
        """
        return validate_milestone_partitioning_impl(
            milestone_csv_path=milestone_csv_path,
            commit_dag_path=AGENTBENCH_COMMIT_DAG_PATH,
            split_metadata_dir=split_metadata_dir,
            repo_path=AGENTBENCH_REPO_DIR,
            resolver=RESOLVER,
        )


# Dependency Check Tools
if "check_dependency" in ENABLED_TOOLS:

    @mcp.tool()
    def check_dependency(
        milestone_csv_path: str,
        milestone_a_id: Optional[str] = None,
        milestone_b_id: Optional[str] = None,
        commits_a: Optional[List[str]] = None,
        commits_b: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Check dependency relationships between two milestones or two groups of commits.

        This tool supports two modes of operation:
        1. Milestone mode: Check dependency between two milestones
        2. Commits mode: Check dependency between two groups of commits

        Args:
            milestone_csv_path: Path to the milestone CSV file with columns:
                              - id: Milestone identifier
                              - title: Milestone title
                              - category: Milestone category
                              - commits: Semicolon-separated list of commit SHAs
            milestone_a_id: ID of first milestone (for milestone mode)
            milestone_b_id: ID of second milestone (for milestone mode)
            commits_a: List of commits in first group (for commits mode)
            commits_b: List of commits in second group (for commits mode)

        Returns:
            Dictionary containing:
            - mode: "milestone" or "commits" depending on input
            - dependency_type: "none", "unidirectional", or "bidirectional"
            - direction: Visual representation of dependency direction
            - description: Human-readable description of the relationship
            - a_depends_on_b: Boolean indicating if A depends on B
            - b_depends_on_a: Boolean indicating if B depends on A
            - details: Detailed dependency information
                - commits_a_count: Number of commits in group A
                - commits_b_count: Number of commits in group B
                - a_to_b_dependencies: List of A→B dependency edges
                - b_to_a_dependencies: List of B→A dependency edges
                - a_to_b_count: Count of A→B dependencies
                - b_to_a_count: Count of B→A dependencies
            - milestone_a: Milestone A metadata (milestone mode only)
            - milestone_b: Milestone B metadata (milestone mode only)

        Example (Milestone mode):
            result = check_dependency(
                milestone_csv_path="/data/milestones.csv",
                milestone_a_id="milestone_001",
                milestone_b_id="milestone_002"
            )
            print(f"Dependency: {result['dependency_type']}")
            print(f"Direction: {result['direction']}")

        Example (Commits mode):
            result = check_dependency(
                milestone_csv_path="/data/milestones.csv",
                commits_a=["abc123", "def456"],
                commits_b=["789ghi", "012jkl"]
            )
            print(f"A depends on B: {result['a_depends_on_b']}")
            print(f"B depends on A: {result['b_depends_on_a']}")
        """
        # Validate input: must provide either milestone IDs or commit lists
        has_milestone_params = milestone_a_id is not None and milestone_b_id is not None
        has_commits_params = commits_a is not None and commits_b is not None

        if not has_milestone_params and not has_commits_params:
            return {
                "error": "Must provide either (milestone_a_id, milestone_b_id) or (commits_a, commits_b)",
                "mode": "invalid",
            }

        if has_milestone_params and has_commits_params:
            return {
                "error": "Cannot provide both milestone IDs and commit lists. Choose one mode.",
                "mode": "invalid",
            }

        # Create analyzer instance
        analyzer = MilestoneDependencyAnalyzer(
            commit_dag_path=AGENTBENCH_COMMIT_DAG_PATH,
            milestone_csv_path=milestone_csv_path,
        )

        # Execute appropriate mode
        if has_milestone_params:
            # Milestone mode
            # Type assertion: we know these are not None due to has_milestone_params check
            assert milestone_a_id is not None and milestone_b_id is not None
            result = analyzer.check_milestone_dependency(milestone_a_id, milestone_b_id)
            result["mode"] = "milestone"
            return result

        else:
            # Commits mode
            # Type assertion: we know these are not None due to has_commits_params check
            assert commits_a is not None and commits_b is not None
            a_depends_on_b, b_depends_on_a, details = analyzer.check_dependency_between_commits(commits_a, commits_b)

            # Determine dependency type
            if a_depends_on_b and b_depends_on_a:
                dependency_type = "bidirectional"
                direction = "A ↔ B"
                description = "双向依赖：两组 commits 互相依赖"
            elif a_depends_on_b:
                dependency_type = "unidirectional"
                direction = "B → A"
                description = "单向依赖：A 依赖 B"
            elif b_depends_on_a:
                dependency_type = "unidirectional"
                direction = "A → B"
                description = "单向依赖：B 依赖 A"
            else:
                dependency_type = "none"
                direction = "A ∥ B"
                description = "无依赖：两组 commits 可以并行"

            return {
                "mode": "commits",
                "dependency_type": dependency_type,
                "direction": direction,
                "description": description,
                "a_depends_on_b": a_depends_on_b,
                "b_depends_on_a": b_depends_on_a,
                "details": details,
            }


# Commit Analysis Tools
if "calculate_commits_src_loc" in ENABLED_TOOLS:

    @mcp.tool()
    def calculate_commits_src_loc(
        commits: List[str],
    ) -> Dict[str, Any]:
        """
        Calculate source lines of code (src_loc) for given commits.

        This tool analyzes commits and calculates the lines of code changed,
        filtered by:
        - REPO_SRC_DIRS environment variable for source directory paths
        - REPO_TEST_DIRS and REPO_EXCLUDE environment variables for exclusion patterns

        Args:
            commits: List of commit SHAs to analyze (can be full or 7-char SHAs)

        Returns:
            Dictionary containing:
            - total_src_loc: Total source code lines changed (additions + deletions)
            - total_additions: Total lines added
            - total_deletions: Total lines deleted
            - valid_commits: Number of valid commits analyzed
            - invalid_commits: Number of commits that don't exist
            - commits: Per-commit details with loc breakdown
            - path_prefix: The path prefixes used for filtering (from REPO_SRC_DIRS)
            - exclude_patterns: The exclude patterns used for filtering

        Example:
            # Calculate src_loc for commits (using REPO_SRC_DIRS as filter)
            result = calculate_commits_src_loc_tool(
                commits=["1812eac", "7b3f948", "0eebf42"]
            )
            print(f"Total src_loc: {result['total_src_loc']}")
            print(f"Path prefixes: {result['path_prefix']}")
            print(f"Exclude patterns: {result['exclude_patterns']}")
        """
        # Always use REPO_SRC_DIRS for filtering
        path_prefix = REPO_SRC_DIRS if REPO_SRC_DIRS else None
        exclude_patterns = REPO_EXCLUDE_PATTERNS if REPO_EXCLUDE_PATTERNS else None

        return _calculate_commits_src_loc_impl(
            commits=commits,
            repo_path=AGENTBENCH_REPO_DIR,
            path_prefix=path_prefix,
            exclude_patterns=exclude_patterns,
            resolver=RESOLVER,
        )


if __name__ == "__main__":
    # Run the FastMCP server
    mcp.run()
