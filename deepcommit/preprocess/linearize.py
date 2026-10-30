"""Turn a merge-commit history into a linear one: one commit per first-parent step, trees unchanged.

Bitbucket (and GitLab "merge commit" projects) land every pull request as a merge commit. The Phase 1 pipeline expects the
mainline to be a chain of PR-sized commits, as in squash-merge repositories: a merge commit has no patch of its own and
its branch commits are not on the mainline. Linearizing replaces each first-parent step by a commit with the same tree,
author, committer, dates and message, whose single parent is the previous linear commit. The tree at every point of the
mainline is therefore identical to the original, and the diff of a step is exactly what the pull request changed.

Only run on a clone made for DeepCommit: the branch is rewritten in place. The original tip is kept at
``refs/deepcommit/orig/<branch>`` and tags that point at a mainline commit are moved to its linear twin.

  python -m deepcommit.preprocess.linearize CLONE --branch master
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def _git(repo: Path, *args: str, env: dict | None = None, input: str | None = None) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **(env or {})},
        input=input,
    )
    return r.stdout.rstrip("\n")


def linearize(repo: str | Path, branch: str = "master") -> int:
    """Rewrite ``branch`` of the clone at ``repo`` as a linear history. Returns the number of commits; 0 if already done."""
    repo = Path(repo)
    keep = f"refs/deepcommit/orig/{branch}"
    if subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", keep], capture_output=True).returncode == 0:
        return 0
    tip = _git(repo, "rev-parse", branch)
    chain = _git(repo, "rev-list", "--first-parent", "--reverse", tip).splitlines()
    twin: dict[str, str] = {}
    prev = None
    fmt = "%T%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B"
    for sha in chain:
        tree, an, ae, ad, cn, ce, cd, msg = _git(repo, "show", "-s", f"--format={fmt}", sha).split("\x00", 7)
        env = {
            "GIT_AUTHOR_NAME": an,
            "GIT_AUTHOR_EMAIL": ae,
            "GIT_AUTHOR_DATE": ad,
            "GIT_COMMITTER_NAME": cn,
            "GIT_COMMITTER_EMAIL": ce,
            "GIT_COMMITTER_DATE": cd,
        }
        args = ["commit-tree", tree] + (["-p", prev] if prev else [])
        prev = _git(repo, *args, env=env, input=msg.rstrip("\n") + "\n")
        twin[sha] = prev
    _git(repo, "update-ref", keep, tip)
    if _git(repo, "branch", "--show-current") == branch:
        _git(repo, "reset", "--hard", "-q", prev)
    else:
        _git(repo, "update-ref", f"refs/heads/{branch}", prev)
    for tag in _git(repo, "tag").splitlines():
        old = _git(repo, "rev-parse", f"{tag}^{{commit}}")
        if old in twin:
            _git(repo, "tag", "-f", tag, twin[old])
    return len(chain)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("clone")
    ap.add_argument("--branch", default="master")
    a = ap.parse_args(argv)
    n = linearize(a.clone, a.branch)
    print(f"linearized {n} first-parent commits of {a.branch}" if n else f"{a.branch} already linearized")


if __name__ == "__main__":
    main()
