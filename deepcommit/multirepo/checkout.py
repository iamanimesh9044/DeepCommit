"""``workspace checkout``: make the local clones of a workspace from its manifest, so the setup is one reproducible step.

For every member repository: clone ``clone_url`` to ``DATA/github_data/repos/<owner>_<repo>`` (skipped if it exists), make the
``main_branch`` a local branch, and create the ``start`` / ``end`` tags from ``start_date`` / ``end_date`` when a tag is
missing: the tag goes on the last first-parent commit of the main branch before ``start_date`` and on or before ``end_date``.
Repositories without release tags (most Bitbucket and in-house projects) can then be described by dates.

The tags exist only in the clone. Nothing is pushed and the source repository is never modified.
"""

from __future__ import annotations

import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import List

from .merge import repo_checkout_path
from .workspace import RepoSpec, Workspace


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def _has_tag(repo: Path, tag: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "-q", f"refs/tags/{tag}", check=False).returncode == 0


def _tag_at(repo: Path, tag: str, branch: str, before: str) -> str:
    sha = _git(repo, "rev-list", "-1", "--first-parent", f"--before={before}T00:00:00", branch).stdout.strip()
    if not sha:
        raise ValueError(f"{repo.name}: no commit of {branch} before {before} for tag {tag}")
    _git(repo, "tag", tag, sha)
    return sha[:12]


def checkout_repo(spec: RepoSpec, data_root: Path) -> List[str]:
    path = repo_checkout_path(data_root, spec)
    log: List[str] = []
    if not path.exists():
        if not spec.clone_url:
            raise ValueError(f"{spec.name}: {path} does not exist and the manifest has no clone_url")
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", spec.clone_url, str(path)], check=True)
        log.append(f"cloned {spec.clone_url}")
    if _git(path, "rev-parse", "--verify", "-q", f"refs/heads/{spec.main_branch}", check=False).returncode != 0:
        _git(path, "branch", spec.main_branch, f"origin/{spec.main_branch}")
        log.append(f"created local branch {spec.main_branch}")
    if _git(path, "branch", "--show-current").stdout.strip() != spec.main_branch:
        _git(path, "checkout", "-q", spec.main_branch)
    if spec.start and spec.start_date and not _has_tag(path, spec.start):
        log.append(f"tag {spec.start} -> {_tag_at(path, spec.start, spec.main_branch, spec.start_date)} (before {spec.start_date})")
    if spec.end and spec.end_date and not _has_tag(path, spec.end):
        after = (date.fromisoformat(spec.end_date) + timedelta(days=1)).isoformat()
        log.append(f"tag {spec.end} -> {_tag_at(path, spec.end, spec.main_branch, after)} (through {spec.end_date})")
    for t in (spec.start, spec.end):
        if t and not _has_tag(path, t):
            raise ValueError(f"{spec.name}: tag {t} is missing and no start_date/end_date is given to create it")
    return log


def checkout_workspace(ws: Workspace, data_root: Path) -> None:
    for spec in ws.repos:
        for line in checkout_repo(spec, data_root) or ["already set up"]:
            print(f"[workspace checkout] {spec.name}: {line}")
