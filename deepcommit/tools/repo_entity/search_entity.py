"""
Search Entity Tool

Repository entity search tool for DeepCommit.
Provides unified search functionality across commits, PRs, releases, and issues.
"""

import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from pygit2.enums import SortMode

# Import utilities from the repo module
from ...tools.repo_entity.repo_analysis import RepositoryAnalyzer


def _check_date_filter(date_str: str, filter_after: Optional[str], filter_before: Optional[str]) -> bool:
    """Check if a date string falls within the filter range."""
    if not date_str:
        return False

    if filter_after and date_str < filter_after:
        return False

    if filter_before and date_str > filter_before:
        return False

    return True


def search_entities(
    repo_path: str,
    data_dir: str,
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
    Unified Repo Entity Search

    Search commits, PRs, releases, and issues with keyword/structured filters
    and optional per-type expansions.

    Args:
        repo_path: Path to the repository directory
        data_dir: Path to the data directory containing analysis files
        types: Entity types to search ["commit", "pr", "release", "issue"]
        search_mode: Search mode ("keyword", "struct", "mixed")
        query: Keyword or light DSL query
        filters: Optional structured filters
        expand: Per-type expansions
        notes_limit_chars: Max chars for release notes excerpts
        conversation_limit_chars: Max chars for issue conversation highlights
        snippet_limit_chars: Max chars for text highlights
        order_by: Result ordering ("relevance", "updated_at", "created_at")
        limit: Page size
        cursor: Pagination cursor

    Returns:
        Search results
    """
    # Import here to avoid circular dependency
    from .repo_entity_retriever import _extract_snippet, _get_commit_stats

    analyzer = RepositoryAnalyzer(repo_path, data_dir)
    if not analyzer:
        return {
            "hits": [],
            "total_estimate": 0,
            "pagination": {"next_cursor": None},
            "diagnostics": {
                "search_mode": search_mode,
                "filters_applied": list(filters.keys()) if filters else [],
                "data_available": False,
                "analyzer_available": False,
                "warnings": ["Repository context unavailable; returning empty search results"],
            },
        }
    types = types or ["commit", "pr", "release", "issue"]
    expand = expand or []
    filters = filters or {}

    hits = []

    # Search commits if requested
    if "commit" in types:
        try:
            # Search through commit history
            head_commit = analyzer.git_repo.main_head
            walker = analyzer.git_repo.repo.walk(head_commit.id, SortMode.TIME)

            commit_count = 0
            max_commits_to_search = 1000  # Limit search to avoid performance issues

            for commit in walker:
                if commit_count >= max_commits_to_search:
                    break
                commit_count += 1

                # Apply filters first
                commit_date = datetime.fromtimestamp(commit.commit_time).isoformat()

                if filters:
                    # Check authored_at filters (also support committed_after/before as aliases)
                    filter_after = filters.get("authored_at_after") or filters.get("committed_after")
                    filter_before = filters.get("authored_at_before") or filters.get("committed_before")

                    if filter_after or filter_before:
                        if not _check_date_filter(commit_date, filter_after, filter_before):
                            continue

                    # Check author filter
                    if "author" in filters:
                        if commit.author.name != filters["author"] and commit.author.email != filters["author"]:
                            continue

                    # Check is_merge filter
                    if "is_merge" in filters:
                        is_merge = len(commit.parents) > 1
                        if is_merge != filters["is_merge"]:
                            continue

                # Apply text search if query is provided
                if query:
                    message_match = query.lower() in commit.message.lower()
                    author_match = query.lower() in commit.author.name.lower()
                    if not (message_match or author_match):
                        continue

                # If we reach here, the commit passes all filters
                hit = {
                    "type": "commit",
                    "score": 1.0 if not query else (1.0 if query and query.lower() in commit.message.lower() else 0.7),
                    "sha": str(commit.id),
                    "author": {"name": commit.author.name, "email": commit.author.email},
                    "authored_at": commit_date,
                    "message_excerpt": commit.message.strip()[:snippet_limit_chars],
                    "snippet": (
                        _extract_snippet(commit.message, query, snippet_limit_chars)
                        if query
                        else commit.message.strip()[:snippet_limit_chars]
                    ),
                }

                if "commit.files" in expand:
                    try:
                        parent = commit.parents[0] if commit.parents else None
                        diff = analyzer.git_repo.repo.diff(parent, commit)
                        hit["files"] = [p.delta.new_file.path for p in diff][:10]
                    except:  # noqa: E722
                        hit["files"] = []

                if "commit.stats" in expand:
                    hit["stats"] = _get_commit_stats(commit)

                hits.append(hit)
                if len(hits) >= limit:
                    break
        except Exception as e:
            # Log the actual error for debugging
            print(f"Error searching commits: {e}", file=sys.stderr)
            pass

    # Search PRs if requested
    if "pr" in types and analyzer.data_extractor:
        try:
            for pr in analyzer.data_extractor.iter_pull_requests():
                # Apply filters first
                if filters:
                    # Check merged_at filters
                    if "merged_at_after" in filters or "merged_at_before" in filters:
                        if not pr.merged_at:
                            continue  # Skip non-merged PRs when filtering by merge date
                        if not _check_date_filter(
                            pr.merged_at, filters.get("merged_at_after"), filters.get("merged_at_before")
                        ):
                            continue

                    # Check created_at filters
                    if "created_at_after" in filters or "created_at_before" in filters:
                        if not _check_date_filter(
                            pr.created_at, filters.get("created_at_after"), filters.get("created_at_before")
                        ):
                            continue

                    # Check closed_at filters
                    if "closed_at_after" in filters or "closed_at_before" in filters:
                        if not pr.closed_at:
                            continue  # Skip open PRs when filtering by close date
                        if not _check_date_filter(
                            pr.closed_at, filters.get("closed_at_after"), filters.get("closed_at_before")
                        ):
                            continue

                    # Check state filter
                    if "state" in filters:
                        if pr.state != filters["state"]:
                            continue

                    # Check merged filter
                    if "merged" in filters:
                        if pr.merged != filters["merged"]:
                            continue

                    # Check author filter
                    if "author" in filters:
                        if pr.user != filters["author"]:
                            continue

                # Apply text search if query is provided
                if query:
                    # Try to match PR number if query is numeric
                    number_match = False
                    if query.isdigit():
                        number_match = str(pr.number) == query

                    # Check title and body
                    title_body_match = query.lower() in pr.title.lower() or (
                        pr.body and query.lower() in pr.body.lower()
                    )

                    if not (number_match or title_body_match):
                        continue

                # If we reach here, the PR passes all filters
                hit = {
                    "type": "pr",
                    "score": 1.0 if not query else (1.0 if query.lower() in pr.title.lower() else 0.8),
                    "number": pr.number,
                    "title": pr.title,
                    "state": pr.state,
                    "author": pr.user,
                    "created_at": pr.created_at,
                    "merged_at": pr.merged_at if hasattr(pr, "merged_at") else None,
                    "closed_at": pr.closed_at if hasattr(pr, "closed_at") else None,
                    "snippet": (
                        _extract_snippet(pr.title + "\n" + (pr.body or ""), query, snippet_limit_chars)
                        if query
                        else pr.title[:snippet_limit_chars]
                    ),
                }

                if "pr.stats" in expand:
                    hit["stats"] = {
                        "additions": pr.additions,
                        "deletions": pr.deletions,
                        "changed_files": pr.changed_files,
                    }

                hits.append(hit)
                if len(hits) >= limit:
                    break
        except Exception as e:
            # Log the actual error for debugging
            print(f"Error searching PRs: {e}", file=sys.stderr)
            pass

    # Search issues if requested
    if "issue" in types and analyzer.data_extractor:
        try:
            for issue in analyzer.data_extractor.iter_issues():
                # Apply filters first
                if filters:
                    # Check created_at filters
                    if "created_at_after" in filters or "created_at_before" in filters:
                        if not _check_date_filter(
                            issue.created_at, filters.get("created_at_after"), filters.get("created_at_before")
                        ):
                            continue

                    # Check closed_at filters
                    if "closed_at_after" in filters or "closed_at_before" in filters:
                        if not issue.closed_at:
                            continue  # Skip open issues when filtering by close date
                        if not _check_date_filter(
                            issue.closed_at, filters.get("closed_at_after"), filters.get("closed_at_before")
                        ):
                            continue

                    # Check state filter
                    if "state" in filters:
                        if issue.state != filters["state"]:
                            continue

                    # Check author filter
                    if "author" in filters:
                        if issue.user != filters["author"]:
                            continue

                    # Check label filter
                    if "label" in filters:
                        if not issue.labels or filters["label"] not in issue.labels:
                            continue

                # Apply text search if query is provided
                if query:
                    # Try to match issue number if query is numeric
                    number_match = False
                    if query.isdigit():
                        number_match = str(issue.number) == query

                    # Check title and body
                    title_body_match = query.lower() in issue.title.lower() or (
                        issue.body and query.lower() in issue.body.lower()
                    )

                    if not (number_match or title_body_match):
                        continue

                # If we reach here, the issue passes all filters
                hit = {
                    "type": "issue",
                    "score": 1.0 if not query else (1.0 if query.lower() in issue.title.lower() else 0.8),
                    "number": issue.number,
                    "title": issue.title,
                    "state": issue.state,
                    "author": issue.user,
                    "created_at": issue.created_at,
                    "closed_at": issue.closed_at if hasattr(issue, "closed_at") else None,
                    "snippet": (
                        _extract_snippet(issue.title + "\n" + (issue.body or ""), query, snippet_limit_chars)
                        if query
                        else issue.title[:snippet_limit_chars]
                    ),
                }

                if "issue.labels" in expand:
                    hit["labels"] = issue.labels

                hits.append(hit)
                if len(hits) >= limit:
                    break
        except Exception as e:
            # Log the actual error for debugging
            print(f"Error searching issues: {e}", file=sys.stderr)
            pass

    # Search releases if requested
    if "release" in types and analyzer.data_extractor:
        try:
            for release in analyzer.data_extractor.iter_releases():
                # Apply filters first
                if filters:
                    # Check published_at filters
                    if "published_at_after" in filters or "published_at_before" in filters:
                        if not _check_date_filter(
                            release.published_at, filters.get("published_at_after"), filters.get("published_at_before")
                        ):
                            continue

                    # Check prerelease filter
                    if "prerelease" in filters:
                        if release.prerelease != filters["prerelease"]:
                            continue

                    # Check author filter
                    if "author" in filters:
                        if release.author != filters["author"]:
                            continue

                # Apply text search if query is provided
                searchable_text = f"{release.tag} {release.name} {release.release_notes}"
                if query:
                    if query.lower() not in searchable_text.lower():
                        continue

                # If we reach here, the release passes all filters
                hit = {
                    "type": "release",
                    "score": 1.0 if not query else (1.0 if query.lower() in release.tag.lower() else 0.8),
                    "tag": release.tag,
                    "name": release.name,
                    "published_at": release.published_at,
                    "author": release.author,
                    "snippet": (
                        _extract_snippet(searchable_text, query, snippet_limit_chars)
                        if query
                        else release.name[:snippet_limit_chars]
                    ),
                }

                if "release.notes" in expand:
                    hit["notes_excerpt"] = release.release_notes[:notes_limit_chars]

                hits.append(hit)
                if len(hits) >= limit:
                    break
        except Exception as e:
            # Log the actual error for debugging
            print(f"Error searching releases: {e}", file=sys.stderr)
            pass

    # Sort by score if relevance ordering
    if order_by == "relevance":
        hits.sort(key=lambda x: x.get("score", 0), reverse=True)
    elif order_by == "created_at":
        hits.sort(key=lambda x: x.get("created_at", x.get("authored_at", x.get("published_at", ""))), reverse=True)
    elif order_by == "updated_at":
        hits.sort(key=lambda x: x.get("updated_at", x.get("authored_at", x.get("published_at", ""))), reverse=True)

    result = {
        "hits": hits,
        "total_estimate": len(hits),
        "pagination": {"next_cursor": None},
        "diagnostics": {
            "search_mode": search_mode,
            "filters_applied": list(filters.keys()) if filters else [],
            "data_available": analyzer.index_tool is not None,
            "analyzer_available": True,
        },
    }

    return result
