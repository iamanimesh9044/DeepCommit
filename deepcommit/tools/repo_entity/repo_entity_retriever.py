"""
Analysis Tools (分析类)

Repository entity retrieval and search tools for DeepCommit.
These tools provide access to repository metadata including issues, PRs, commits, and releases.
"""

import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pygit2
from pygit2.enums import SortMode

# Import utilities from the repo module
from ...tools.repo_entity.repo_analysis import RepositoryAnalyzer

# from ..utils import resolve_repo_path, resolve_data_dir


# def _create_analyzer(repo_id: str) -> Optional[RepositoryAnalyzer]:
#     """Create a RepositoryAnalyzer for the provided repo identifier if possible."""
#     repo_path = resolve_repo_path(repo_id)
#     if not repo_path:
#         return None

#     data_dir = resolve_data_dir(repo_id)

#     try:
#         return RepositoryAnalyzer(repo_path, data_dir)
#     except Exception:
#         return None


def get_issue_context(
    # repo_id: str,
    repo_path: str,
    data_dir: str,
    issue_number: int,
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
    Issue Context Retriever

    Return core issue metadata with optional expansions: conversation (comments),
    events timeline, and closing PRs.

    Args:
        repo_path: Path to the repository directory
        data_dir: Path to the data directory containing analysis files
        issue_number: Issue number
        expand: Optional expansions ["conversation", "events", "closing_prs"]
        include_body: Whether to include issue body
        body_limit_chars: Max characters for body before truncation
        comments_mode: "summary" or "full" for conversation expansion
        comments_limit_chars: Max characters for comment excerpts
        events_since: Only include events after this time (ISO8601)
        limit: Page size for list expansions
        cursor: Pagination cursor

    Returns:
        Issue context data
    """
    analyzer = RepositoryAnalyzer(repo_path, data_dir)
    expand = expand or []

    # Try to get issue from DataExtractor
    issue_data = None
    if analyzer and analyzer.data_extractor:
        issue_data = analyzer.data_extractor.get_issue_by_number(issue_number)

    if issue_data:
        # Build result from actual issue data
        body_text = issue_data.body or ""
        body_truncated = False
        if include_body and len(body_text) > body_limit_chars:
            body_text = body_text[:body_limit_chars] + "..."
            body_truncated = True

        result = {
            "number": issue_data.number,
            "title": issue_data.title,
            "author": {"name": issue_data.user, "email": None},
            "state": issue_data.state,
            "labels": issue_data.labels if issue_data.labels else [],
            "created_at": issue_data.created_at,
            "updated_at": issue_data.updated_at,
            "closed_at": issue_data.closed_at,
            "assignees": issue_data.assignees if issue_data.assignees else [],
            "milestone": issue_data.milestone,
            "include_body": include_body,
            "body": body_text if include_body else "",
            "body_truncated": body_truncated,
            "body_char_count": len(issue_data.body or ""),
            "comments_count": issue_data.comments_count,
            "diagnostics": {
                "source": "data_extractor",
                "comments_mode": comments_mode,
                "data_available": True,
                "analyzer_available": analyzer is not None,
            },
        }

        # Add state_reason if available
        if hasattr(issue_data, "state_reason") and issue_data.state_reason:
            result["state_reason"] = issue_data.state_reason

        # Add closed_by if available
        if hasattr(issue_data, "closed_by") and issue_data.closed_by:
            result["closed_by"] = issue_data.closed_by

        # Add expanded data based on expand parameter
        if "conversation" in expand:
            conversation_data = (
                issue_data.conversations if hasattr(issue_data, "conversations") and issue_data.conversations else {}
            )
            comments = conversation_data.get("comments", [])

            if comments_mode == "summary":
                # Provide highlights/summaries
                highlights = []
                for comment in comments[:limit]:
                    body = comment.get("body", "")
                    excerpt = body[:comments_limit_chars]
                    if len(body) > comments_limit_chars:
                        excerpt += "..."
                    highlights.append(
                        {
                            "user": comment.get("user", "unknown"),
                            "created_at": comment.get("created_at", ""),
                            "body": excerpt,
                        }
                    )

                result["conversation"] = {
                    "mode": comments_mode,
                    "count": len(comments),
                    "comments": highlights,
                    "items": None,
                    "truncated": len(comments) > limit,
                }
            else:
                # Full comments
                result["conversation"] = {
                    "mode": comments_mode,
                    "count": len(comments),
                    "highlights": None,
                    "items": comments[:limit],
                    "truncated": len(comments) > limit,
                }

        if "events" in expand:
            events = issue_data.events if hasattr(issue_data, "events") and issue_data.events else []
            if events_since:
                # Filter events by date
                events = [e for e in events if e.get("created_at", "") >= events_since]
            result["events"] = events[:limit]

        if "closing_prs" in expand:
            # Find PRs that may have closed this issue
            closing_prs = []
            if analyzer and analyzer.index_tool:
                try:
                    # Search for PRs that reference this issue
                    pr_refs = analyzer.index_tool.get_prs_for_issue(issue_number)
                    for pr_num in pr_refs[:limit]:
                        if analyzer.data_extractor:
                            pr = analyzer.data_extractor.get_pull_request_by_number(pr_num)
                            if pr and pr.merged:
                                closing_prs.append({"number": pr.number, "title": pr.title, "merged_at": pr.merged_at})
                except:  # noqa: E722
                    pass
            result["closing_prs"] = closing_prs
    else:
        # Fallback to placeholder if no data available
        result = {
            "number": issue_number,
            "title": f"Issue #{issue_number}",
            "author": {"name": "unknown", "email": None},
            "state": "unknown",
            "labels": [],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "closed_at": None,
            "assignees": [],
            "milestone": None,
            "include_body": include_body,
            "body": "" if not include_body else "TODO: Issue body content",
            "body_truncated": False,
            "body_char_count": 0,
            "comments_count": 0,
            "diagnostics": {
                "source": "local_placeholder",
                "comments_mode": comments_mode,
                "data_available": False,
                "analyzer_available": analyzer is not None,
            },
        }

        # Add expanded data based on expand parameter
        if "conversation" in expand:
            result["conversation"] = {
                "mode": comments_mode,
                "count": 0,
                "comments": [] if comments_mode == "summary" else None,
                "items": [] if comments_mode == "full" else None,
                "truncated": False,
            }
        if "events" in expand:
            result["events"] = []
        if "closing_prs" in expand:
            result["closing_prs"] = []

    result["pagination"] = {}

    return result


def get_pr_context(
    repo_path: str,
    data_dir: str,
    pr_number: int,
    expand: Optional[List[str]] = None,
    include_body: bool = True,
    body_limit_chars: int = 8192,
    comments_mode: str = "summary",
    comments_limit_chars: int = 200,
    limit: int = 200,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Pull Request Context Retriever

    Return core PR metadata with optional expansions: commits, files, timeline,
    comments, checks, and linked issues.

    Args:
        repo_path: Path to the repository directory
        data_dir: Path to the data directory containing analysis files
        pr_number: Pull Request number
        expand: Optional expansions ["pr_commits", "files", "timeline", "comments", "checks", "linked_issues"]
        include_body: Whether to include PR body
        body_limit_chars: Max characters for body before truncation
        comments_mode: "summary" or "full" for comments
        comments_limit_chars: Max characters for comment excerpts
        limit: Page size for list expansions
        cursor: Pagination cursor

    Returns:
        PR context data
    """
    analyzer = RepositoryAnalyzer(repo_path, data_dir)
    expand = expand or []
    warnings = []

    # Try to get PR data from DataExtractor first
    pr_data = None
    if analyzer and analyzer.data_extractor:
        try:
            pr_data = analyzer.data_extractor.get_pull_request_by_number(pr_number)
        except Exception as e:
            warnings.append(f"Could not get PR from data extractor: {e}")

    # Try to get PR data from index
    pr_index_info = None
    if analyzer and analyzer.index_tool and analyzer.index_tool.index:
        try:
            pr_index_info = analyzer.index_tool.query_pr_info(pr_number)
        except Exception as e:
            warnings.append(f"Could not get PR from index: {e}")

    # Build result based on available data
    if pr_data:
        # Use DataExtractor data (most complete)
        result = {
            "number": pr_number,
            "title": pr_data.title,
            "author": {
                "name": pr_data.user,  # PR data has 'user' field, not 'author'
                "email": None,  # Not available in current data
            },
            "state": pr_data.state,
            "created_at": pr_data.created_at,
            "updated_at": pr_data.updated_at or pr_data.created_at,
            "merged_at": pr_data.merged_at,
            "closed_at": pr_data.closed_at,
            "base_branch": "main",  # Not available in current PR data
            "head_ref": f"pr-{pr_number}",  # Not available in current PR data
            "merge_commit_sha": pr_data.merge_commit_sha,
            "merge_strategy": "merge",  # Default, as not stored in current data
            "labels": pr_data.labels,
            "additions": pr_data.additions,
            "deletions": pr_data.deletions,
            "files_changed": pr_data.changed_files,
            "comments_count": pr_data.comments_count,
            "is_merged": pr_data.merged,
            "url": pr_data.url,
            "diagnostics": {
                "source": "data_extractor",
                "data_available": True,
                "index_available": bool(analyzer and analyzer.index_tool),
                "analyzer_available": analyzer is not None,
                "warnings": warnings,
            },
        }

        # Include body if requested
        if include_body and hasattr(pr_data, "body"):
            body = pr_data.body or ""
            if len(body) > body_limit_chars:
                result["body"] = body[:body_limit_chars]
                result["body_truncated"] = True
            else:
                result["body"] = body
                result["body_truncated"] = False

    elif pr_index_info:
        # Use index data (less complete but available)
        result = {
            "number": pr_number,
            "title": pr_index_info.get("title", f"PR #{pr_number}"),
            "author": {"name": "unknown", "email": None},
            "state": pr_index_info.get("state", "unknown"),
            "created_at": pr_index_info.get("created_at", datetime.now().isoformat()),
            "updated_at": pr_index_info.get("created_at", datetime.now().isoformat()),
            "merged_at": pr_index_info.get("closed_at") if pr_index_info.get("merged") else None,
            "closed_at": pr_index_info.get("closed_at"),
            "base_branch": "main",
            "head_ref": f"pr-{pr_number}",
            "merge_commit_sha": pr_index_info.get("merge_commit"),
            "merge_strategy": "merge",
            "labels": [],
            "additions": 0,
            "deletions": 0,
            "files_changed": 0,
            "comments_count": 0,
            "is_merged": pr_index_info.get("merged", False),
            "url": pr_index_info.get("url"),
            "diagnostics": {
                "source": "index",
                "data_available": False,
                "index_available": True,
                "analyzer_available": analyzer is not None,
                "warnings": warnings,
            },
        }
    else:
        # No data available, return placeholder
        result = {
            "number": pr_number,
            "title": f"PR #{pr_number}",
            "author": {"name": "unknown", "email": None},
            "state": "unknown",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "merged_at": None,
            "closed_at": None,
            "base_branch": "main",
            "head_ref": f"pr-{pr_number}",
            "merge_commit_sha": None,
            "merge_strategy": "unknown",
            "labels": [],
            "additions": 0,
            "deletions": 0,
            "files_changed": 0,
            "comments_count": 0,
            "is_merged": False,
            "diagnostics": {
                "source": "placeholder",
                "data_available": False,
                "index_available": False,
                "analyzer_available": analyzer is not None,
                "warnings": warnings + ["No PR data found in index or data extractor"],
            },
        }

    # Add expanded data
    repo_object = analyzer.git_repo.repo if analyzer else None

    if "pr_commits" in expand:
        commits = []
        if repo_object and pr_index_info and pr_index_info.get("commits") and pr_index_info["commits"]:
            # Get commits from index
            for commit_sha in pr_index_info["commits"][:limit]:
                try:
                    commit = repo_object.revparse_single(commit_sha)
                    if isinstance(commit, pygit2.Commit):
                        commits.append(
                            {
                                "sha": str(commit.id),
                                "message": commit.message.strip(),
                                "author": {"name": commit.author.name, "email": commit.author.email},
                                "authored_at": datetime.fromtimestamp(commit.author.time).isoformat(),
                            }
                        )
                except Exception:
                    commits.append(
                        {
                            "sha": commit_sha,
                            "message": "Commit data unavailable",
                            "author": {"name": "unknown", "email": None},
                            "authored_at": None,
                        }
                    )
        elif repo_object and pr_data and hasattr(pr_data, "commits") and pr_data.commits:
            # Get commits from data extractor
            for commit_sha in pr_data.commits[:limit]:
                try:
                    commit = repo_object.revparse_single(commit_sha)
                    if isinstance(commit, pygit2.Commit):
                        commits.append(
                            {
                                "sha": str(commit.id),
                                "message": commit.message.strip(),
                                "author": {"name": commit.author.name, "email": commit.author.email},
                                "authored_at": datetime.fromtimestamp(commit.author.time).isoformat(),
                            }
                        )
                except Exception:
                    commits.append(
                        {
                            "sha": commit_sha,
                            "message": "Commit data unavailable",
                            "author": {"name": "unknown", "email": None},
                            "authored_at": None,
                        }
                    )
        elif pr_data and hasattr(pr_data, "commits") and pr_data.commits:
            for commit_sha in pr_data.commits[:limit]:
                commits.append(
                    {
                        "sha": commit_sha,
                        "message": "Commit data unavailable",
                        "author": {"name": "unknown", "email": None},
                        "authored_at": None,
                    }
                )

        result["pr_commits"] = commits

    if "files" in expand:
        files = []
        if repo_object and pr_index_info and pr_index_info.get("commits") and pr_index_info["commits"]:
            # Try to get files by analyzing commits
            file_set = set()
            for commit_sha in pr_index_info["commits"]:
                try:
                    commit = repo_object.revparse_single(commit_sha)
                    if isinstance(commit, pygit2.Commit):
                        commit_files = _get_commit_files(commit, 100)
                        for f in commit_files:
                            file_set.add(f["path"])
                        if len(file_set) >= limit:
                            break
                except Exception:
                    pass
            files = [{"path": path, "status": "modified"} for path in list(file_set)[:limit]]

        result["files"] = files

    if "timeline" in expand:
        # Timeline would require more complex data aggregation
        result["timeline"] = []

    if "comments" in expand:
        comments_data = {
            "mode": comments_mode,
            "count": result.get("comments_count", 0),
            "highlights": [] if comments_mode == "summary" else None,
            "items": [] if comments_mode == "full" else None,
        }

        # If we have comment data from data extractor
        if pr_data and hasattr(pr_data, "comments_data"):
            if comments_mode == "summary" and hasattr(pr_data, "comment_highlights"):
                comments_data["highlights"] = pr_data.conversations.get("comments", [])[:limit]
            elif comments_mode == "full" and hasattr(pr_data, "comments_full"):
                comments_data["items"] = pr_data.conversations.get("comments", [])[:limit]

        result["comments"] = comments_data

    if "checks" in expand:
        # Check runs data would come from CI/CD integration
        result["checks"] = []

    if "linked_issues" in expand:
        issues = []
        if pr_index_info and pr_index_info.get("issues"):
            # Get linked issues from index
            for issue_num in pr_index_info["issues"][:limit]:
                if analyzer and analyzer.index_tool and analyzer.index_tool.index:
                    issue_data = analyzer.index_tool.index.get("issues", {}).get(issue_num, {})
                    issues.append(
                        {
                            "number": issue_num,
                            "title": issue_data.get("title", f"Issue #{issue_num}"),
                            "state": issue_data.get("state", "unknown"),
                        }
                    )
                else:
                    issues.append({"number": issue_num, "title": f"Issue #{issue_num}", "state": "unknown"})
        elif pr_data and hasattr(pr_data, "related_issues"):
            if pr_data.related_issues:
                for issue_num in pr_data.related_issues[:limit]:
                    issues.append({"number": issue_num, "title": f"Issue #{issue_num}", "state": "unknown"})

        result["linked_issues"] = issues

    # Add pagination
    result["pagination"] = {
        "limit": limit,
        "cursor": cursor,
        "has_next_page": False,  # Would need proper implementation
    }

    return result


def get_commit_context(
    repo_path: str,
    data_dir: str,
    sha: str,
    expand: Optional[List[str]] = None,
    diff_limit_lines: int = 3000,
    files_limit: int = 100,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Commit Context Retriever

    Return core metadata for a commit SHA with optional expansions: files, stats,
    PR references, diff snippet, and issue references.

    Args:
        repo_path: Path to the repository directory
        data_dir: Path to the data directory containing analysis files
        sha: Commit SHA (full or uniquely resolvable)
        expand: Optional expansions ["files", "stats", "pr_refs", "diff", "issue_refs", "releases"]
        diff_limit_lines: Max lines for diff snippet
        files_limit: Max files to return
        cursor: Pagination cursor (currently unused)

    Returns:
        Commit context data
    """
    # analyzer = _create_analyzer(repo_id)
    analyzer = RepositoryAnalyzer(repo_path, data_dir)

    if not analyzer:
        short_sha = sha[:8] if sha else ""
        return {
            "sha": sha,
            "short_sha": short_sha,
            "author": {"name": "unknown", "email": None},
            "committer": {"name": "unknown", "email": None},
            "authored_at": None,
            "committed_at": None,
            "message": "",
            "message_subject": "",
            "parent_count": 0,
            "parents": [],
            "is_merge": False,
            "touches_tests": False,
            "diagnostics": {
                "source": "placeholder",
                "index_available": False,
                "data_extractor_available": False,
                "analyzer_available": False,
                "index_commit_found": False,
                "warnings": ["Repository context unavailable; returning placeholder commit summary"],
            },
            "tool_py_version": sys.version,
            "pagination": {},
        }
    expand = expand or []

    try:
        # Get commit from git repository
        commit = analyzer.git_repo.repo.revparse_single(sha)
        if not isinstance(commit, pygit2.Commit):
            commit = commit.peel(pygit2.Commit)

        commit_sha = str(commit.id)

        # Get index-based information if available
        index_info = None
        if analyzer.index_tool:
            index_info = analyzer.index_tool.query_commit_info(commit_sha)

        result = {
            "sha": commit_sha,
            "short_sha": commit_sha[:8],
            "author": {"name": commit.author.name, "email": commit.author.email},
            "committer": {"name": commit.committer.name, "email": commit.committer.email},
            "authored_at": datetime.fromtimestamp(commit.author.time).isoformat(),
            "committed_at": datetime.fromtimestamp(commit.commit_time).isoformat(),
            "message": commit.message.strip(),
            "message_subject": commit.message.strip().split("\n")[0],
            "parent_count": len(commit.parents),
            "parents": [str(parent.id) for parent in commit.parents],
            "is_merge": len(commit.parents) > 1,
            "touches_tests": _touches_tests(commit),
            "tool_py_version": sys.version,
            "diagnostics": {
                "source": "enhanced_analysis",
                "index_available": analyzer.index_tool is not None,
                "data_extractor_available": analyzer.data_extractor is not None,
                "analyzer_available": True,
                "index_commit_found": index_info is not None,
            },
        }

        # Add index-based information
        if index_info:
            result["releases"] = index_info.get("releases", [])
            result["associated_refs"] = index_info.get("prs", [])
            result["is_merge_commit"] = index_info.get("is_merge_commit", False)
            result["merge_pr"] = index_info.get("merge_pr")
            result["merged_via_pr"] = index_info.get("merged_via_pr")

        # Add expanded data
        if "files" in expand:
            result["files"] = _get_commit_files(commit, files_limit)

        if "stats" in expand:
            result["stats"] = _get_commit_stats(commit)

        if "pr_refs" in expand:
            pr_refs = []
            # Get PR references from index if available
            if index_info and index_info.get("prs"):
                for pr_num in index_info["prs"]:
                    pr_info = {"number": pr_num, "source": "index"}
                    # Try to get PR details from data_extractor
                    if analyzer.data_extractor:
                        try:
                            pr_data = analyzer.data_extractor.get_pull_request_by_number(pr_num)
                            if pr_data:
                                pr_info.update(
                                    {
                                        "title": pr_data.title,
                                        "state": pr_data.state,
                                        "merged": pr_data.merged,
                                        "url": pr_data.url,
                                    }
                                )
                        except Exception:
                            pass
                    pr_refs.append(pr_info)

            # Also extract from commit message
            message_refs = _extract_pr_refs(commit.message)
            for ref in message_refs:
                if not any(pr["number"] == ref["number"] for pr in pr_refs):
                    pr_refs.append(ref)

            result["pr_refs"] = pr_refs

        if "releases" in expand and index_info and index_info.get("releases"):
            release_details = []
            for release_tag in index_info["releases"]:
                release_info = {"tag": release_tag, "source": "index"}
                # Try to get release details from data_extractor
                if analyzer.data_extractor:
                    try:
                        release_data = analyzer.data_extractor.get_release_by_tag(release_tag)
                        if release_data:
                            release_info.update(
                                {
                                    "name": release_data.name,
                                    "date": release_data.date,
                                    "prerelease": release_data.prerelease,
                                    "url": release_data.url,
                                }
                            )
                    except Exception:
                        pass
                release_details.append(release_info)
            result["release_details"] = release_details

        if "diff" in expand:
            diff_data = _get_commit_diff(commit, diff_limit_lines)
            result["diff"] = {
                "snippet": diff_data["snippet"],
                "truncated": diff_data["truncated"],
                "hunk_count": diff_data["hunk_count"],
                "total_lines": diff_data.get("total_lines", 0),
            }
            if "error" in diff_data:
                result["diff"]["error"] = diff_data["error"]

        if "issue_refs" in expand:
            result["issue_refs"] = _extract_issue_refs(commit.message)

        result["pagination"] = {}

        return result

    except Exception as e:
        raise ValueError(f"Could not retrieve commit {sha}: {e}") from e


def _is_prerelease(tag: str) -> bool:
    """
    Detect if a tag represents a prerelease version

    Args:
        tag: Tag name to check

    Returns:
        True if tag is a prerelease
    """
    tag_lower = tag.lower()
    prerelease_patterns = [
        r".*alpha.*",
        r".*beta.*",
        r".*rc.*",
        r".*pre.*",
        r".*dev.*",
        r".*nightly.*",
        r".*snapshot.*",
        r".*preview.*",
        r".*test.*",
    ]

    for pattern in prerelease_patterns:
        if re.match(pattern, tag_lower):
            return True
    return False


# def _filter_merge_commits(commits: List[str], git_repo: LocalGitRepo) -> List[str]:
#     """
#     Filter out merge commits from a list of commit SHAs

#     Args:
#         commits: List of commit SHAs
#         git_repo: Git repository instance

#     Returns:
#         List of commit SHAs without merge commits
#     """
#     filtered_commits = []
#     for commit_sha in commits:
#         try:
#             obj = git_repo.repo.revparse_single(commit_sha)
#             commit = obj.peel(pygit2.Commit)
#             # A merge commit has more than one parent
#             if len(commit.parents) <= 1:
#                 filtered_commits.append(commit_sha)
#         except Exception:
#             # If we can't parse the commit, include it to be safe
#             filtered_commits.append(commit_sha)
#     return filtered_commits


def _paginate_list(items: List[Any], limit: int, cursor: Optional[str] = None) -> Tuple[List[Any], Dict[str, Any]]:
    """
    Apply pagination to a list of items

    Args:
        items: List of items to paginate
        limit: Maximum number of items per page
        cursor: Pagination cursor (item index as string)

    Returns:
        Tuple of (paginated_items, pagination_info)
    """
    start_idx = 0
    if cursor:
        try:
            start_idx = int(cursor)
        except (ValueError, TypeError):
            start_idx = 0

    end_idx = start_idx + limit
    paginated_items = items[start_idx:end_idx]

    pagination_info = {
        "total_count": len(items),
        "has_next_page": end_idx < len(items),
        "has_previous_page": start_idx > 0,
        "start_cursor": str(start_idx) if paginated_items else None,
        "end_cursor": str(end_idx - 1) if paginated_items else None,
        "next_cursor": str(end_idx) if end_idx < len(items) else None,
        "previous_cursor": str(max(0, start_idx - limit)) if start_idx > 0 else None,
    }

    return paginated_items, pagination_info


def get_release_context(
    repo_path: str,
    data_dir: str,
    tag: str,
    include_notes: bool = True,
    notes_limit_chars: int = 8192,
    include_merges: bool = True,
    expand: Optional[List[str]] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Release Context (Index-Only, Previous Tag Baseline)

    Return release metadata (with optional notes) and index-level change lists
    (commit SHAs, file paths, PR/Issue numbers) for a tag, using the previous tag
    as the fixed baseline.

    Args:
        repo_path: Path to the repository directory
        data_dir: Path to the data directory containing analysis files
        tag: Target release tag to inspect
        include_notes: Whether to include release notes
        notes_limit_chars: Max characters for notes before truncation
        include_merges: Include merge commits in commits list
        expand: Which index lists to expand ["commits", "files", "prs", "issues"]
        limit: Max items per expanded list page
        cursor: Pagination cursor

    Returns:
        Release context data
    """
    expand = expand or []
    warnings = []

    # analyzer = _create_analyzer(repo_id)
    analyzer = RepositoryAnalyzer(repo_path, data_dir)
    if not analyzer:
        result = {
            "tag": tag,
            "author": {"name": "unknown"},
            "created_at": None,
            "published_at": None,
            "is_prerelease": _is_prerelease(tag),
            "is_draft": False,
            "include_notes": include_notes,
            "notes": "",
            "notes_truncated": False,
            "notes_char_count": 0,
            "compare_window": {"base_tag": None, "head_tag": tag},
            "diagnostics": {
                "source": "placeholder",
                "diff_base": "previous_tag",
                "resolved_base_tag": None,
                "data_available": False,
                "extractor_available": False,
                "analyzer_available": False,
                "warnings": ["Repository context unavailable; returning placeholder release summary"],
            },
            "pagination": {},
        }

        if expand:
            if "commits" in expand:
                result["commit_count"] = 0
                result["commits"] = []
            if "prs" in expand:
                result["prs"] = []
            if "issues" in expand:
                result["issues"] = []
            if "files" in expand:
                result["files"] = []

        return result

    try:
        # Try to get release info from DataExtractor first
        release_info = None
        if analyzer.data_extractor:
            try:
                release_info = analyzer.data_extractor.get_release_by_tag(tag)
            except Exception as e:
                warnings.append(f"Could not get release from data extractor: {e}")

        # Find previous tag
        prev_tag = None
        if release_info and release_info.prev_tag:
            prev_tag = release_info.prev_tag
        elif analyzer.git_repo.is_tag_on_main_first_parent(tag):
            prev_tag = analyzer.git_repo.previous_tag_on_main(tag)
        else:
            warnings.append(f"Tag {tag} is not on main branch first-parent chain")

        # Use DataExtractor info if available, fallback to git info
        if release_info:
            author_name = release_info.author
            created_at = release_info.created_at
            published_at = release_info.published_at
            is_prerelease = release_info.prerelease
            is_draft = release_info.draft
            notes = release_info.release_notes if include_notes else ""
            url = release_info.url
            source = "data_extractor"

            result = {
                "tag": tag,
                "author": {
                    "name": author_name,
                    # "email": tag_info.get("tagger_email")
                },
                "created_at": created_at,
                "published_at": published_at,
                "is_prerelease": is_prerelease,
                "is_draft": is_draft,
                "include_notes": include_notes,
                "notes": notes,
                "notes_truncated": False,
                "notes_char_count": len(notes),
                "url": url,
                "compare_window": {"base_tag": prev_tag, "head_tag": tag},
                "diagnostics": {
                    "source": source,
                    "diff_base": "previous_tag",
                    "resolved_base_tag": prev_tag,
                    "data_available": analyzer.index_tool is not None,
                    "extractor_available": analyzer.data_extractor is not None,
                    "analyzer_available": True,
                    "warnings": warnings,
                    # "on_mainline": tag_info.get("on_mainline", False)
                },
            }
        else:
            # No release info from data extractor, use default values
            result = {
                "tag": tag,
                "author": {
                    "name": "unknown",
                },
                "created_at": datetime.now().isoformat(),
                "published_at": datetime.now().isoformat(),
                "is_prerelease": _is_prerelease(tag),
                "is_draft": False,
                "include_notes": include_notes,
                "notes": "",
                "notes_truncated": False,
                "notes_char_count": 0,
                "compare_window": {"base_tag": prev_tag, "head_tag": tag},
                "diagnostics": {
                    "source": "placeholder",
                    "diff_base": "previous_tag",
                    "resolved_base_tag": prev_tag,
                    "data_available": analyzer.index_tool is not None,
                    "extractor_available": analyzer.data_extractor is not None,
                    "analyzer_available": True,
                    "warnings": warnings + ["No release data found in data extractor"],
                },
            }

        # Truncate notes if needed
        if include_notes and result["notes"]:
            if len(result["notes"]) > notes_limit_chars:
                result["notes"] = result["notes"][:notes_limit_chars]
                result["notes_truncated"] = True

        # Add expanded data
        if expand:
            if "commits" in expand:
                commits_with_details: List[Dict[str, Any]] = []
                if release_info and getattr(release_info, "commits", None):
                    for commit_sha in release_info.commits:
                        try:
                            commit = analyzer.git_repo.repo.revparse_single(commit_sha)
                            if isinstance(commit, pygit2.Commit):
                                commits_with_details.append({"sha": str(commit.id), "message": commit.message.strip()})
                            else:
                                commit = commit.peel(pygit2.Commit)
                                commits_with_details.append({"sha": str(commit.id), "message": commit.message.strip()})
                        except Exception as e:
                            commits_with_details.append({"sha": commit_sha, "message": f"Error retrieving commit: {e}"})
                elif analyzer.index_tool:
                    try:
                        commit_shas = analyzer.index_tool.query_release_commits(tag)
                        for commit_sha in commit_shas:
                            commits_with_details.append({"sha": commit_sha, "message": "Commit data unavailable"})
                    except Exception as e:
                        warnings.append(f"Could not query commits from index: {e}")

                result["commit_count"] = len(commits_with_details)
                result["commits"] = commits_with_details[:limit]

            if "prs" in expand:
                prs = []
                if analyzer.index_tool and analyzer.index_tool.index:
                    try:
                        prs_list = analyzer.index_tool.query_release_prs(tag)
                        prs = [
                            {
                                "number": pr_num,
                                "title": analyzer.index_tool.index.get("prs", {}).get(pr_num, {}).get("title", ""),
                            }
                            for pr_num in prs_list
                        ]
                    except Exception as e:
                        warnings.append(f"Could not query PRs from index: {e}")
                result["prs"] = prs
                # Apply pagination
                # paginated_prs, pagination = _paginate_list(prs, limit, cursor)
                # result["prs"] = paginated_prs
                # if "pagination" not in result:
                #     result["pagination"] = pagination

            if "issues" in expand:
                issues = []
                if analyzer.index_tool and analyzer.index_tool.index:
                    try:
                        issues_list = analyzer.index_tool.query_release_issues(tag)
                        issues = [
                            {
                                "number": issue_num,
                                "title": analyzer.index_tool.index.get("issues", {})
                                .get(issue_num, {})
                                .get("title", ""),
                            }
                            for issue_num in issues_list
                        ]
                    except Exception as e:
                        warnings.append(f"Could not query issues from index: {e}")

                # Apply pagination
                paginated_issues, pagination = _paginate_list(issues, limit, cursor)
                result["issues"] = paginated_issues
                if "pagination" not in result:
                    result["pagination"] = pagination

            if "files" in expand:
                if prev_tag:
                    files = analyzer.git_repo.changed_files_between_tags(prev_tag, tag)
                    # Apply pagination
                    paginated_files, pagination = _paginate_list(files, limit, cursor)
                    result["files"] = [{"path": path} for path in paginated_files]
                    if "pagination" not in result:
                        result["pagination"] = pagination
                else:
                    result["files"] = []

        # Set empty pagination if not set
        if "pagination" not in result:
            result["pagination"] = {}

        # Update diagnostics with final warnings
        result["diagnostics"]["warnings"] = warnings

        return result

    except Exception as e:
        raise ValueError(f"Could not retrieve release context for {tag}: {e}") from e


def get_commits_diff(
    repo_path: str,
    data_dir: str,
    base_ref: str,
    head_ref: str,
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
    Commits Window Diff

    Compute a window diff between two refs (base_ref -> head_ref) with optional
    summary, file list, patch snippets, and commit list.

    Args:
        repo_path: Path to the repository directory
        data_dir: Path to the data directory containing analysis files
        base_ref: Baseline ref/sha/tag (rev-parse resolvable)
        head_ref: Target ref/sha/tag (rev-parse resolvable)
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

    Returns:
        Commits diff data
    """
    # analyzer = _create_analyzer(repo_id)
    analyzer = RepositoryAnalyzer(repo_path, data_dir)
    if not analyzer:
        return {
            "compare_window": {"base_ref": base_ref, "head_ref": head_ref, "base_sha": None, "head_sha": None},
            "diagnostics": {
                "source": "placeholder",
                "paths_filter_applied": bool(paths_glob),
                "detect_renames": detect_renames,
                "rename_threshold": rename_similarity_threshold,
                "data_available": False,
                "analyzer_available": False,
                "warnings": ["Repository context unavailable; returning placeholder diff summary"],
            },
            "pagination": {},
        }
    expand = expand or []

    try:
        # Resolve refs to commits
        base_commit = analyzer.git_repo._peel_to_commit(base_ref)
        head_commit = analyzer.git_repo._peel_to_commit(head_ref)

        result = {
            "compare_window": {
                "base_ref": base_ref,
                "head_ref": head_ref,
                "base_sha": str(base_commit.id),
                "head_sha": str(head_commit.id),
            },
            "diagnostics": {
                "source": "git_direct",
                "paths_filter_applied": bool(paths_glob),
                "detect_renames": detect_renames,
                "rename_threshold": rename_similarity_threshold,
                "data_available": analyzer.index_tool is not None,
                "analyzer_available": True,
            },
        }

        # Add expanded data
        if "summary" in expand:
            result["summary"] = _get_diff_summary(base_commit, head_commit, analyzer.git_repo.repo)

        if "files" in expand:
            result["files"] = _get_diff_files(
                base_commit, head_commit, analyzer.git_repo.repo, paths_glob, detect_renames, limit
            )

        if "patch" in expand:
            result["patches"] = _get_diff_patches(
                base_commit, head_commit, analyzer.git_repo.repo, paths_glob, patch_limit_lines, limit
            )

        if "commits" in expand:
            result["commits"] = _get_commits_in_range(
                base_commit,
                head_commit,
                analyzer.git_repo.repo,
                include_merges,
                first_parent_only,
                commits_order,
                limit,
            )

        result["pagination"] = {}

        return result

    except Exception as e:
        raise ValueError(f"Could not compute diff between {base_ref} and {head_ref}: {e}") from e


# Helper functions


def _diff_to_parent(commit: pygit2.Commit) -> pygit2.Diff:
    """Create a diff between a commit's tree and its first parent's tree.

    Safely handles initial commits where no parent exists.
    """
    parent_tree: Optional[pygit2.Tree] = None
    if commit.parents:
        try:
            parent_tree = commit.parents[0].tree
        except KeyError:
            parent_tree = None

    # Use swap=True so the patch is generated as parent -> commit rather than commit -> parent.
    # This keeps plus/minus markers and the `index` header hashes consistent with git.
    return (
        commit.tree.diff_to_tree(parent_tree, swap=True)
        if parent_tree is not None
        else commit.tree.diff_to_tree(swap=True)
    )


def _touches_tests(commit: pygit2.Commit) -> bool:
    """Check if commit touches test files"""
    try:
        # Try to get parent tree
        parent_tree = None
        if commit.parents:
            try:
                parent_tree = commit.parents[0].tree
            except KeyError:
                # Parent object not found, fallback to no parent
                print(f"Warning: Parent commit not found for {commit.id}, treating as initial commit", file=sys.stderr)
                parent_tree = None  # noqa: F841

        # Create diff
        try:
            diff = _diff_to_parent(commit)
        except Exception as e:
            print(f"Warning: Could not create diff for commit {commit.id}: {e}", file=sys.stderr)
            return False

        for patch in diff:
            # Check both old and new paths (for renames)
            paths_to_check = []
            if patch.delta.new_file.path:
                paths_to_check.append(patch.delta.new_file.path.lower())
            if patch.delta.old_file.path:
                paths_to_check.append(patch.delta.old_file.path.lower())

            for path in paths_to_check:
                # Check various test patterns
                if (
                    "test" in path
                    or path.endswith(".test")
                    or "/tests/" in path
                    or path.startswith("tests/")
                    or path.endswith("_test.py")
                    or path.endswith("_test.go")
                    or path.endswith("_test.js")
                    or path.endswith(".spec.js")
                    or path.endswith(".spec.ts")
                    or "spec/" in path
                    or "/spec/" in path
                ):
                    return True
        return False
    except Exception as e:
        print(f"Error in _touches_tests: {e}", file=sys.stderr)
        return False


def _get_commit_files_fallback(commit: pygit2.Commit, parent_tree, limit: int) -> List[Dict[str, Any]]:
    """Fallback method to get commit files when normal diff fails"""
    files = []
    try:
        # If no parent, list all files as added
        if parent_tree is None:
            for entry in commit.tree:
                # Check if entry is a blob (file) by checking filemode
                if entry.filemode & 0o100000:  # Regular file
                    files.append({"path": entry.name, "status": "added", "additions": 0, "deletions": 0})
                    if len(files) >= limit:
                        break
        else:
            # Try to manually compare trees
            files.append({"path": "unknown", "status": "unknown", "additions": 0, "deletions": 0})
    except Exception as e:
        print(f"Fallback method also failed: {e}", file=sys.stderr)
    return files


def _get_commit_files(commit: pygit2.Commit, limit: int) -> List[Dict[str, Any]]:
    """Get files changed in commit"""
    files = []
    try:
        # Try to get parent tree
        parent_tree = None
        if commit.parents:
            try:
                parent_tree = commit.parents[0].tree
            except KeyError:
                # Parent object not found, fallback to no parent
                print(f"Warning: Parent commit not found for {commit.id}, treating as initial commit", file=sys.stderr)
                parent_tree = None

        # Create diff
        try:
            diff = _diff_to_parent(commit)
        except Exception as e:
            print(f"Warning: Could not create diff for commit {commit.id}: {e}", file=sys.stderr)
            # Try alternative: list all files in commit as "added" if no parent
            if not commit.parents or parent_tree is None:
                try:
                    # Walk the tree and list all files as added
                    for entry in commit.tree:
                        # Check if entry is a blob (file) by checking filemode
                        if entry.filemode & 0o100000:  # Regular file
                            files.append(
                                {
                                    "path": entry.name,
                                    "status": "added",
                                    "additions": 0,  # Can't determine without diff
                                    "deletions": 0,
                                }
                            )
                            if len(files) >= limit:
                                break
                except Exception as tree_error:
                    print(f"Error walking tree: {tree_error}", file=sys.stderr)
            return files

        # Safely iterate through patches
        try:
            patches = list(diff)
        except KeyError as e:
            print(f"Warning: Cannot read all diff patches for {commit.id}: {e}", file=sys.stderr)
            # Try to get file changes through alternative method
            return _get_commit_files_fallback(commit, parent_tree, limit)

        for patch in patches:
            delta = patch.delta
            # Map status to readable format
            status_map = {
                pygit2.GIT_DELTA_ADDED: "added",
                pygit2.GIT_DELTA_MODIFIED: "modified",
                pygit2.GIT_DELTA_DELETED: "deleted",
                pygit2.GIT_DELTA_RENAMED: "renamed",
                pygit2.GIT_DELTA_COPIED: "copied",
                pygit2.GIT_DELTA_UNMODIFIED: "unmodified",
                pygit2.GIT_DELTA_TYPECHANGE: "typechange",
                pygit2.GIT_DELTA_UNREADABLE: "unreadable",
                pygit2.GIT_DELTA_CONFLICTED: "conflicted",
            }

            # Get line stats safely
            try:
                additions = patch.line_stats[1]
                deletions = patch.line_stats[2]
            except:  # noqa: E722
                additions = 0
                deletions = 0

            file_info = {
                "path": delta.new_file.path if delta.new_file.path else delta.old_file.path,
                "status": status_map.get(delta.status, "unknown"),
                "additions": additions,
                "deletions": deletions,
            }

            # Add old path for renames/moves
            if delta.status in [pygit2.GIT_DELTA_RENAMED, pygit2.GIT_DELTA_COPIED]:
                file_info["old_path"] = delta.old_file.path

            files.append(file_info)
            if len(files) >= limit:
                break
    except Exception as e:
        # Log the error for debugging
        print(f"Error in _get_commit_files: {e}", file=sys.stderr)
    return files


def _get_commit_stats(commit: pygit2.Commit) -> Dict[str, int]:
    """Get commit statistics"""
    try:
        # Try to get parent tree
        parent_tree = None
        if commit.parents:
            try:
                parent_tree = commit.parents[0].tree
            except KeyError:
                print(f"Warning: Parent commit not found for {commit.id}, treating as initial commit", file=sys.stderr)
                parent_tree = None  # noqa: F841

        # Create diff with correct order
        try:
            diff = _diff_to_parent(commit)
        except Exception as e:
            print(f"Warning: Could not create diff for commit {commit.id}: {e}", file=sys.stderr)
            return {"additions": 0, "deletions": 0, "files_changed": 0, "renames": 0}

        stats = diff.stats

        # Count renames by checking delta statuses
        renames = 0
        for patch in diff:
            if patch.delta.status == pygit2.GIT_DELTA_RENAMED:
                renames += 1

        return {
            "additions": stats.insertions,
            "deletions": stats.deletions,
            "files_changed": stats.files_changed,
            "renames": renames,
        }
    except Exception as e:
        print(f"Error in _get_commit_stats: {e}", file=sys.stderr)
        return {"additions": 0, "deletions": 0, "files_changed": 0, "renames": 0}


def _get_commit_pr_refs(commit: pygit2.Commit, analyzer: RepositoryAnalyzer) -> List[Dict[str, Any]]:
    """Extract PR references from commit"""
    refs = []
    # TODO: Implement PR reference extraction
    return refs


def _get_commit_diff(commit: pygit2.Commit, limit_lines: int) -> Dict[str, Any]:
    """Get commit diff snippet"""
    try:
        # Try to get parent tree
        parent_tree = None
        if commit.parents:
            try:
                parent_tree = commit.parents[0].tree
            except KeyError:
                print(f"Warning: Parent commit not found for {commit.id}, treating as initial commit", file=sys.stderr)
                parent_tree = None  # noqa: F841

        # Create diff with correct order
        try:
            diff = _diff_to_parent(commit)
        except Exception as e:
            print(f"Warning: Could not create diff for commit {commit.id}: {e}", file=sys.stderr)
            return {"snippet": "", "truncated": False, "hunk_count": 0, "total_lines": 0, "error": str(e)}

        # Generate patch text
        patch_text = diff.patch or ""

        if not patch_text:
            return {"snippet": "", "truncated": False, "hunk_count": 0, "total_lines": 0}

        lines = patch_text.split("\n")
        original_line_count = len(lines)

        # Truncate if needed
        truncated = False
        if len(lines) > limit_lines:
            lines = lines[:limit_lines]
            patch_text = "\n".join(lines)
            truncated = True

        # Count hunks (lines starting with @@)
        hunk_count = len([line for line in lines if line.startswith("@@")])

        return {
            "snippet": patch_text,
            "truncated": truncated,
            "hunk_count": hunk_count,
            "total_lines": original_line_count,
        }
    except Exception as e:
        print(f"Error in _get_commit_diff: {e}", file=sys.stderr)
        return {"snippet": "", "truncated": False, "hunk_count": 0, "total_lines": 0, "error": str(e)}


def _extract_pr_refs(message: str) -> List[Dict[str, Any]]:
    """Extract PR references from commit message"""
    refs = []
    # Look for patterns like #123, merge #456, pull request #789
    patterns = [
        r"#(\d+)",
        r"(?:merge|merges)\s+#(\d+)",
        r"(?:pull\s+request|pr)\s+#(\d+)",
    ]

    for pattern in patterns:
        matches = re.finditer(pattern, message, re.IGNORECASE)
        for match in matches:
            pr_num = int(match.group(1))
            source = (
                "merge" if any(keyword in match.group(0).lower() for keyword in ["merge", "pr", "pull"]) else "message"
            )
            refs.append({"number": pr_num, "source": source, "platform": "github"})  # TODO: Detect platform

    return refs


def _extract_issue_refs(message: str) -> List[Dict[str, Any]]:
    """Extract issue references from commit message"""
    refs = []
    # Look for patterns like #123, fixes #456, closes #789
    patterns = [
        r"#(\d+)",
        r"(?:fix|fixes|close|closes|resolve|resolves)\s+#(\d+)",
    ]

    for pattern in patterns:
        matches = re.finditer(pattern, message, re.IGNORECASE)
        for match in matches:
            issue_num = int(match.group(1))
            source = (
                "metadata"
                if any(keyword in match.group(0).lower() for keyword in ["fix", "close", "resolve"])
                else "message"
            )
            refs.append({"number": issue_num, "source": source, "platform": "github"})  # TODO: Detect platform

    return refs


def _get_diff_summary(base_commit, head_commit, repo) -> Dict[str, int]:
    """Get diff summary statistics"""
    try:
        diff = repo.diff(base_commit, head_commit)
        stats = diff.stats

        # Count commits in range (base_commit..head_commit)
        commits_count = 0
        renames_count = 0

        try:
            # Use git walker to count commits from head_commit back to base_commit
            walker = repo.walk(head_commit.id, SortMode.TOPOLOGICAL)
            walker.hide(base_commit.id)  # Exclude base_commit and its ancestors

            for _commit in walker:
                commits_count += 1

        except Exception as e:
            print(
                f"Warning: Could not count commits between {base_commit.id} and {head_commit.id}: {e}", file=sys.stderr
            )
            commits_count = 0

        # Count renames in the diff
        try:
            for patch in diff:
                if patch.delta.status == pygit2.GIT_DELTA_RENAMED:
                    renames_count += 1
        except Exception as e:
            print(f"Warning: Could not count renames in diff: {e}", file=sys.stderr)
            renames_count = 0

        return {
            "commits_count": commits_count,
            "files_changed": stats.files_changed,
            "additions": stats.insertions,
            "deletions": stats.deletions,
            "renames": renames_count,
        }
    except Exception as e:
        print(f"Error in _get_diff_summary: {e}", file=sys.stderr)
        return {"commits_count": 0, "files_changed": 0, "additions": 0, "deletions": 0, "renames": 0}


def _get_diff_files(base_commit, head_commit, repo, paths_glob, detect_renames, limit) -> List[Dict[str, Any]]:
    """Get files changed in diff"""
    files = []
    try:
        diff = repo.diff(base_commit, head_commit)
        for patch in diff:
            delta = patch.delta
            file_info = {
                "path": delta.new_file.path,
                "change_type": _delta_status_to_change_type(delta.status),
                "additions": patch.line_stats[1],
                "deletions": patch.line_stats[2],
            }
            if delta.old_file.path != delta.new_file.path:
                file_info["rename_from"] = delta.old_file.path
            files.append(file_info)
            if len(files) >= limit:
                break
    except:  # noqa: E722
        pass
    return files


def _get_diff_patches(base_commit, head_commit, repo, paths_glob, limit_lines, limit) -> List[Dict[str, Any]]:
    """Get patch snippets for diff"""
    patches = []
    try:
        # Create diff with proper flags for patch generation
        diff_flags = pygit2.GIT_DIFF_NORMAL

        # Create the diff
        diff = repo.diff(base_commit, head_commit, flags=diff_flags)

        # Try to detect renames if the method is available
        try:
            diff.find_similar()
        except AttributeError:
            # find_similar not available in this version of pygit2
            pass

        # Generate patch string for the entire diff first
        full_patch = diff.patch

        if not full_patch:
            print(
                f"Warning: No patch content generated for diff between {base_commit.id} and {head_commit.id}",
                file=sys.stderr,
            )
            # Try alternative method - iterate patches individually
            for i, patch in enumerate(diff):
                if i >= limit:
                    break

                delta = patch.delta
                path = delta.new_file.path if delta.new_file.path else delta.old_file.path

                # Try to get individual patch data
                try:
                    # Get patch data through delta
                    patch_data = {
                        "path": path,
                        "patch": "Binary file changed" if delta.is_binary else "",
                        "truncated": False,
                        "hunk_count": 0,
                        "additions": patch.line_stats[1] if hasattr(patch, "line_stats") else 0,
                        "deletions": patch.line_stats[2] if hasattr(patch, "line_stats") else 0,
                        "status": _delta_status_to_change_type(delta.status),
                        "is_binary": delta.is_binary,
                    }

                    # Try to get the patch text
                    try:
                        if hasattr(patch, "data"):
                            patch_text = patch.data
                        elif hasattr(patch, "text"):
                            patch_text = patch.text
                        else:
                            # Generate patch manually for this file
                            single_diff = repo.diff(base_commit, head_commit, pathspec=[path])
                            patch_text = single_diff.patch

                        if patch_text:
                            lines = (
                                patch_text.split("\n") if isinstance(patch_text, str) else str(patch_text).split("\n")
                            )

                            if len(lines) > limit_lines:
                                patch_text = "\n".join(lines[:limit_lines])
                                patch_data["truncated"] = True

                            patch_data["patch"] = patch_text
                            patch_data["hunk_count"] = sum(1 for line in lines if line.startswith("@@"))
                    except Exception as patch_err:
                        print(f"Could not get patch text for {path}: {patch_err}", file=sys.stderr)

                    patches.append(patch_data)

                except Exception as e:
                    print(f"Error processing patch for {path}: {e}", file=sys.stderr)
                    continue

        else:
            # Parse the full patch into individual file patches
            current_file_patch = []
            current_file = None

            for line in full_patch.split("\n"):
                if line.startswith("diff --git"):
                    # Start of a new file patch
                    if current_file_patch and current_file:
                        # Save the previous patch
                        patch_text = "\n".join(current_file_patch)
                        lines = current_file_patch

                        if len(lines) > limit_lines:
                            patch_text = "\n".join(lines[:limit_lines])
                            truncated = True
                        else:
                            truncated = False

                        patches.append(
                            {
                                "path": current_file,
                                "patch": patch_text,
                                "truncated": truncated,
                                "hunk_count": sum(1 for line in lines if line.startswith("@@")),
                                "additions": sum(1 for line in lines if line.startswith("+")),
                                "deletions": sum(1 for line in lines if line.startswith("-")),
                                "status": "modified",  # Will be refined based on the header
                            }
                        )

                        if len(patches) >= limit:
                            break

                    # Extract file path from the diff line
                    parts = line.split()
                    if len(parts) >= 4:
                        current_file = parts[2][2:] if parts[2].startswith("a/") else parts[2]
                    current_file_patch = [line]
                else:
                    if current_file_patch is not None:
                        current_file_patch.append(line)

            # Don't forget the last patch
            if current_file_patch and current_file and len(patches) < limit:
                patch_text = "\n".join(current_file_patch)
                lines = current_file_patch

                if len(lines) > limit_lines:
                    patch_text = "\n".join(lines[:limit_lines])
                    truncated = True
                else:
                    truncated = False

                patches.append(
                    {
                        "path": current_file,
                        "patch": patch_text,
                        "truncated": truncated,
                        "hunk_count": sum(1 for line in lines if line.startswith("@@")),
                        "additions": sum(1 for line in lines if line.startswith("+")),
                        "deletions": sum(1 for line in lines if line.startswith("-")),
                        "status": "modified",
                    }
                )

    except Exception as e:
        print(f"Error in _get_diff_patches: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()

    return patches


def _get_commits_in_range(
    base_commit, head_commit, repo, include_merges, first_parent_only, order, limit
) -> List[Dict[str, Any]]:
    """Get commits in range"""
    commits = []
    try:
        walker = repo.walk(head_commit.id)
        walker.hide(base_commit.id)

        for commit in walker:
            if not include_merges and len(commit.parents) > 1:
                continue

            commit_info = {
                "sha": str(commit.id),
                "author": {"name": commit.author.name, "email": commit.author.email},
                "committed_at": datetime.fromtimestamp(commit.commit_time).isoformat(),
                "message_excerpt": commit.message.strip().split("\n")[0][:100],
                "is_merge": len(commit.parents) > 1,
            }
            commits.append(commit_info)

            if len(commits) >= limit:
                break
    except:  # noqa: E722
        pass
    return commits


def _delta_status_to_change_type(status):
    """Convert pygit2 delta status to change type"""
    mapping = {
        pygit2.GIT_DELTA_ADDED: "added",
        pygit2.GIT_DELTA_MODIFIED: "modified",
        pygit2.GIT_DELTA_DELETED: "deleted",
        pygit2.GIT_DELTA_RENAMED: "renamed",
        pygit2.GIT_DELTA_COPIED: "copied",
    }
    return mapping.get(status, "modified")


def _extract_snippet(text: str, query: str, limit: int) -> str:
    """Extract text snippet around query match"""
    if not query or query.lower() not in text.lower():
        return text[:limit]

    # Find the match position
    pos = text.lower().find(query.lower())
    if pos == -1:
        return text[:limit]

    # Extract context around the match
    start = max(0, pos - limit // 3)
    end = min(len(text), pos + len(query) + limit // 3)

    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."

    return snippet[:limit]
