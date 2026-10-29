"""Commit -> repo resolution for tools that must run git inside the right checkout.

``WorkspaceResolver`` is built from the files the merge step writes (``repos.json`` + ``commit_repos.json``).
For a single-repo run use ``WorkspaceResolver.single(repo_path, src_dirs)``; every method then behaves exactly like
the old scalar ``repo_path`` / ``REPO_SRC_DIRS`` pair, so tools can call the resolver unconditionally.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ENV_WORKSPACE_DIR = "DEEPCOMMIT_WORKSPACE_DIR"


class UnknownCommit(KeyError):
    pass


@dataclass
class RepoInfo:
    name: str
    checkout_path: str
    src_dirs: List[str]  # repo-relative literal prefixes
    test_dirs: List[str]
    exclude: List[str]
    role: str = "library"


class WorkspaceResolver:
    def __init__(self, repos: Dict[str, RepoInfo], commit_repo: Dict[str, str], multi: bool):
        self.repos = repos
        self.commit_repo = commit_repo
        self.multi = multi

    # ---- constructors
    @classmethod
    def single(cls, repo_path: str, src_dirs: Optional[List[str]] = None) -> "WorkspaceResolver":
        info = RepoInfo(name="", checkout_path=str(repo_path), src_dirs=list(src_dirs or []), test_dirs=[], exclude=[])
        return cls({"": info}, {}, multi=False)

    @classmethod
    def from_dir(cls, workspace_dir: Path | str) -> "WorkspaceResolver":
        d = Path(workspace_dir)
        repos_json = json.loads((d / "repos.json").read_text(encoding="utf-8"))
        commit_repo = json.loads((d / "commit_repos.json").read_text(encoding="utf-8"))
        repos = {
            name: RepoInfo(
                name=name,
                checkout_path=r["checkout_path"],
                src_dirs=r.get("repo_src_dirs", []),
                test_dirs=r.get("test_dirs", []),
                exclude=r.get("exclude", []),
                role=r.get("role", "library"),
            )
            for name, r in repos_json.items()
        }
        return cls(repos, commit_repo, multi=len(repos) > 1)

    @classmethod
    def from_env(cls, fallback_repo_path: str = "repo", fallback_src_dirs: Optional[List[str]] = None) -> "WorkspaceResolver":
        d = os.getenv(ENV_WORKSPACE_DIR)
        if d and (Path(d) / "repos.json").exists():
            return cls.from_dir(d)
        return cls.single(fallback_repo_path, fallback_src_dirs)

    # ---- queries
    def repo_for(self, sha: str) -> str:
        if not self.multi:
            return ""
        key = sha[:7].lower()
        try:
            return self.commit_repo[key]
        except KeyError as exc:
            raise UnknownCommit(f"commit {sha!r} is not in any workspace repo") from exc

    def info_for(self, sha: str) -> RepoInfo:
        return self.repos[self.repo_for(sha)]

    def repo_path_for(self, sha: str) -> str:
        return self.info_for(sha).checkout_path

    def default_repo_path(self) -> str:
        return next(iter(self.repos.values())).checkout_path

    def prefixed_src_dirs(self) -> List[str]:
        """Src dirs as seen from the merged tree (``<repo>/<dir>``); plain dirs for a single repo."""
        if not self.multi:
            return next(iter(self.repos.values())).src_dirs
        return [f"{n}/{d.lstrip('/')}" for n, r in self.repos.items() for d in r.src_dirs]

    def strip_prefix(self, path: str) -> Tuple[str, str]:
        """``<repo>/<rel>`` -> ``(repo, rel)``; ``("", path)`` for single repo."""
        if not self.multi:
            return "", path
        for n in sorted(self.repos, key=len, reverse=True):
            if path.startswith(n + "/"):
                return n, path[len(n) + 1 :]
        raise UnknownCommit(f"path {path!r} is not under a workspace repo")

    def group_commits_by_repo(self, shas: List[str]) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for s in shas:
            out.setdefault(self.repo_for(s), []).append(s)
        return out
