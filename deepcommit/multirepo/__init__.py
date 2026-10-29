"""Multi-repo support: a workspace of 1..5 interdependent repos processed as one milestone pool."""

from .merge import CommitKeyCollision, MergeResult, MissingPreprocessed, merge_workspace, workspace_dir
from .resolver import ENV_WORKSPACE_DIR, UnknownCommit, WorkspaceResolver
from .workspace import (
    MAX_REPOS,
    RepoSpec,
    Workspace,
    WorkspaceError,
    XRepoConfig,
    load_workspace,
    parse_workspace,
    prefix_path,
    single_repo_workspace,
    split_path,
)

__all__ = [
    "CommitKeyCollision",
    "ENV_WORKSPACE_DIR",
    "MAX_REPOS",
    "MergeResult",
    "MissingPreprocessed",
    "RepoSpec",
    "UnknownCommit",
    "Workspace",
    "WorkspaceError",
    "WorkspaceResolver",
    "XRepoConfig",
    "load_workspace",
    "merge_workspace",
    "parse_workspace",
    "prefix_path",
    "single_repo_workspace",
    "split_path",
    "workspace_dir",
]
