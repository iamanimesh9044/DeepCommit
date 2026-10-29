"""Route per-commit git work to the right repo checkout in a multi-repo workspace.

Only used when ``resolver.multi`` is true; single-repo callers keep their original code path.
"""

from typing import Any, Dict, List, Optional, Tuple

from deepcommit.multirepo.resolver import UnknownCommit, WorkspaceResolver
from deepcommit.utils.git.local_git_repo import LocalGitRepo


def is_multi(resolver: Optional[WorkspaceResolver]) -> bool:
    return resolver is not None and resolver.multi


class RoutedGit:
    """One cached ``LocalGitRepo`` per workspace repo, looked up by commit."""

    def __init__(self, resolver: WorkspaceResolver, main_branch: Optional[str] = None):
        self.resolver = resolver
        self.main_branch = main_branch
        self._repos: Dict[str, LocalGitRepo] = {}

    def repo(self, name: str) -> LocalGitRepo:
        if name not in self._repos:
            self._repos[name] = LocalGitRepo(self.resolver.repos[name].checkout_path, self.main_branch)
        return self._repos[name]

    def for_commit(self, sha: str) -> Tuple[str, LocalGitRepo]:
        name = self.resolver.repo_for(sha)
        return name, self.repo(name)

    def src_filters(self, name: str) -> Tuple[Optional[List[str]], Optional[List[str]]]:
        """(path_prefix, exclude_patterns) of a repo, unprefixed, in the shape ``get_commit_loc`` expects."""
        info = self.resolver.repos[name]
        exclude = list(info.test_dirs) + list(info.exclude)
        return (info.src_dirs or None), (exclude or None)

    def commit_loc(self, sha: str) -> Dict[str, Any]:
        """``LocalGitRepo.get_commit_loc`` run in the commit's repo with that repo's src dirs and excludes."""
        try:
            name, repo = self.for_commit(sha)
        except UnknownCommit:
            return {"exists": False, "additions": 0, "deletions": 0, "total": 0}
        prefix, exclude = self.src_filters(name)
        return repo.get_commit_loc(sha, path_prefix=prefix, exclude_patterns=exclude)
