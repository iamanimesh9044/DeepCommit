"""Workspace-aware wrappers around the repo_entity tools (what the MCP server calls).

Single repo (``resolver.multi`` false): every call is forwarded unchanged to the original function with the
resolver's only checkout path, so output is identical to the pre-workspace behaviour.

Multi repo: each call is routed to one repo checkout (by commit sha, an explicit ``repo`` argument, or a
``<repo>:<ref>`` prefix on tags/refs), file paths in the result are re-prefixed ``<repo>/<path>`` to match the merged
data, and records gain a ``repo`` field. Issue / PR numbers are only unique per repo, so they need ``repo``.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pygit2

from deepcommit.multirepo.resolver import ENV_WORKSPACE_DIR, UnknownCommit, WorkspaceResolver

from . import repo_entity_retriever as analysis
from .search_entity import search_entities as _search_entities_impl

_PATH_KEYS = ("path", "old_path", "rename_from")


def prefix_record_paths(result: Dict[str, Any], repo: str) -> Dict[str, Any]:
    """Prefix file paths in ``files`` / ``patches`` entries with ``<repo>/`` and tag the result with ``repo``."""
    for key in ("files", "patches"):
        for entry in result.get(key) or []:
            if isinstance(entry, dict):
                for k in _PATH_KEYS:
                    if entry.get(k):
                        entry[k] = f"{repo}/{entry[k]}"
    for entry in result.get("commits") or []:
        if isinstance(entry, dict):
            entry["repo"] = repo
    result["repo"] = repo
    return result


class RoutedEntityTools:
    def __init__(self, resolver: WorkspaceResolver, data_dir: str, workspace_dir: Optional[str] = None):
        self.resolver = resolver
        self.data_dir = data_dir
        self._preprocessed: Dict[str, str] = {}
        wdir = workspace_dir or os.getenv(ENV_WORKSPACE_DIR)
        if resolver.multi and wdir and (Path(wdir) / "repos.json").exists():
            repos = json.loads((Path(wdir) / "repos.json").read_text(encoding="utf-8"))
            self._preprocessed = {n: r["preprocessed_dir"] for n, r in repos.items() if r.get("preprocessed_dir")}

    # ---- routing helpers
    def _data_dir(self, name: str) -> str:
        d = self._preprocessed.get(name)
        return d if d and Path(d).exists() else self.data_dir

    def _repo_path(self, name: str) -> str:
        return self.resolver.repos[name].checkout_path

    def _split_ref(self, ref: str) -> Tuple[Optional[str], str]:
        head, sep, rest = ref.partition(":")
        if sep and head in self.resolver.repos:
            return head, rest
        return None, ref

    def _require(self, repo: Optional[str], what: str) -> str:
        if repo is None:
            raise ValueError(f"{what} needs `repo` in a multi-repo workspace (one of: {sorted(self.resolver.repos)})")
        if repo not in self.resolver.repos:
            raise ValueError(f"unknown repo {repo!r} (one of: {sorted(self.resolver.repos)})")
        return repo

    # ---- tools
    def get_issue_context(self, issue_number: int, repo: Optional[str] = None, **kw) -> Dict[str, Any]:
        if not self.resolver.multi:
            return analysis.get_issue_context(
                repo_path=self.resolver.default_repo_path(), data_dir=self.data_dir, issue_number=issue_number, **kw
            )
        name = self._require(repo, "issue_number")
        res = analysis.get_issue_context(
            repo_path=self._repo_path(name), data_dir=self._data_dir(name), issue_number=issue_number, **kw
        )
        return prefix_record_paths(res, name)

    def get_pr_context(self, pr_number: int, repo: Optional[str] = None, **kw) -> Dict[str, Any]:
        if not self.resolver.multi:
            return analysis.get_pr_context(
                repo_path=self.resolver.default_repo_path(), data_dir=self.data_dir, pr_number=pr_number, **kw
            )
        name = self._require(repo, "pr_number")
        res = analysis.get_pr_context(
            repo_path=self._repo_path(name), data_dir=self._data_dir(name), pr_number=pr_number, **kw
        )
        return prefix_record_paths(res, name)

    def get_commit_context(self, sha: str, **kw) -> Dict[str, Any]:
        if not self.resolver.multi:
            return analysis.get_commit_context(
                repo_path=self.resolver.default_repo_path(), data_dir=self.data_dir, sha=sha, **kw
            )
        name = self.resolver.repo_for(sha)
        res = analysis.get_commit_context(repo_path=self._repo_path(name), data_dir=self._data_dir(name), sha=sha, **kw)
        return prefix_record_paths(res, name)

    def get_release_context(self, tag: str, repo: Optional[str] = None, **kw) -> Dict[str, Any]:
        if not self.resolver.multi:
            return analysis.get_release_context(
                repo_path=self.resolver.default_repo_path(), data_dir=self.data_dir, tag=tag, **kw
            )
        prefixed, tag = self._split_ref(tag)
        name = repo or prefixed
        if name is None:
            holders = [
                n
                for n in self.resolver.repos
                if pygit2.Repository(self._repo_path(n)).references.get(f"refs/tags/{tag}") is not None
            ]
            if len(holders) != 1:
                raise ValueError(f"tag {tag!r} found in repos {holders}; pass `repo` or use '<repo>:<tag>'")
            name = holders[0]
        name = self._require(name, "tag")
        res = analysis.get_release_context(
            repo_path=self._repo_path(name), data_dir=self._data_dir(name), tag=tag, **kw
        )
        return prefix_record_paths(res, name)

    def get_commits_diff(
        self, base_ref: str, head_ref: str, repo: Optional[str] = None, paths_glob: Optional[List[str]] = None, **kw
    ) -> Dict[str, Any]:
        if not self.resolver.multi:
            return analysis.get_commits_diff(
                repo_path=self.resolver.default_repo_path(),
                data_dir=self.data_dir,
                base_ref=base_ref,
                head_ref=head_ref,
                paths_glob=paths_glob,
                **kw,
            )
        base_repo, base_ref = self._split_ref(base_ref)
        head_repo, head_ref = self._split_ref(head_ref)
        name = repo or base_repo or head_repo
        if name is None:
            for ref in (base_ref, head_ref):
                try:
                    name = self.resolver.repo_for(ref)
                    break
                except UnknownCommit:
                    continue
        name = self._require(name, "base_ref/head_ref")
        if paths_glob:
            paths_glob = [p[len(name) + 1 :] if p.startswith(name + "/") else p for p in paths_glob]
        res = analysis.get_commits_diff(
            repo_path=self._repo_path(name),
            data_dir=self._data_dir(name),
            base_ref=base_ref,
            head_ref=head_ref,
            paths_glob=paths_glob,
            **kw,
        )
        return prefix_record_paths(res, name)

    def search_entities(self, types: Optional[List[str]] = None, **kw) -> Dict[str, Any]:
        if not self.resolver.multi:
            return _search_entities_impl(
                repo_path=self.resolver.default_repo_path(), data_dir=self.data_dir, types=types, **kw
            )
        types = types or ["commit", "pr", "release", "issue"]
        limit = kw.get("limit", 50)
        names = list(self.resolver.repos)
        hits: List[Dict[str, Any]] = []
        diag: Dict[str, Any] = {}
        if "commit" in types:
            for name in names:
                res = _search_entities_impl(
                    repo_path=self._repo_path(name), data_dir=self._data_dir(name), types=["commit"], **kw
                )
                for h in res["hits"]:
                    h["repo"] = name
                    if h.get("files"):
                        h["files"] = [f"{name}/{f}" for f in h["files"]]
                hits.extend(res["hits"])
                diag = res["diagnostics"]
        other = [t for t in types if t != "commit"]
        if other:
            # PRs / issues / releases live in the merged data dir (each row tagged with its repo): query it once.
            res = _search_entities_impl(repo_path=self._repo_path(names[0]), data_dir=self.data_dir, types=other, **kw)
            hits.extend(res["hits"])
            diag = res["diagnostics"]
        order_by = kw.get("order_by", "relevance")
        if order_by == "relevance":
            hits.sort(key=lambda x: x.get("score", 0), reverse=True)
        elif order_by == "created_at":
            hits.sort(key=lambda x: x.get("created_at", x.get("authored_at", x.get("published_at", ""))), reverse=True)
        elif order_by == "updated_at":
            hits.sort(key=lambda x: x.get("updated_at", x.get("authored_at", x.get("published_at", ""))), reverse=True)
        hits = hits[:limit]
        return {
            "hits": hits,
            "total_estimate": len(hits),
            "pagination": {"next_cursor": None},
            "diagnostics": diag,
        }
