"""Export a DeepCommit workspace trial as a CSF (Harbor multi-step) task of family ``cross-repo-change``.

Input: a workspace manifest, a trial's Phase 2 output (``milestones.csv`` with a ``repos`` column, ``dependencies.csv``)
and the merged workspace data (``repos.json`` / ``commit_repos.json``). Output: ``<out_dir>/<task-id>/`` (the Harbor task)
and ``<out_dir>/../_src/<task-id>/`` (authoring sources + ``render.py`` that regenerates ``steps/``).

The milestones are linearised in topological order. Per repo, the state after step k is the repo's start tag plus the
commits of steps 1..k cherry-picked in order (real git, in temp clones; the DATA checkouts are only read). Step k's gold
patch per repo is ``git diff state_{k-1} state_k`` restricted to non-test files; the test files it changes become the
hidden tests. A cherry-pick conflict is never hidden: the step is reported ``blocked`` and so is everything depending on it.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shlex
import shutil
import stat
import string
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from deepcommit.harness.utils.src_filter import SrcFileFilter
from deepcommit.multirepo.merge import preprocessed_path, repo_checkout_path, workspace_dir
from deepcommit.multirepo.workspace import RepoSpec, Workspace, load_workspace

TEMPLATES = Path(__file__).parent / "templates"
RENAME_THRESHOLD = 20
CONTRACT_GOLD_FLAG = 2000  # a step whose contract gold has more lines than this is flagged
GIT_ENV = {
    "GIT_COMMITTER_NAME": "csf-export",
    "GIT_COMMITTER_EMAIL": "csf-export@localhost",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    "GIT_TERMINAL_PROMPT": "0",
}

# language -> (framework, run_cmd, parser); {tests} is replaced by the space-separated test list of the group
SUITE_DEFAULTS = {
    "rust": ("cargo test", "cargo test --no-fail-fast --offline", "cargo"),
    "python": ("pytest", "python -m pytest -rA -p no:cacheprovider -q {tests}", "pytest"),
    "go": ("go test", "go test -v -count=1 ./...", "go"),
    "javascript": ("npm test", "npm test --silent", "exit-code"),
    "unknown": ("none", "echo 'no test runner configured for this repo' >&2; false", "exit-code"),
}
LANGUAGE_MARKERS = (
    ("Cargo.toml", "rust"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("setup.cfg", "python"),
    ("go.mod", "go"),
    ("package.json", "javascript"),
)


class ExportError(RuntimeError):
    """The workspace/trial cannot be exported (missing input, bad manifest)."""


@dataclass
class ExportReport:
    task_id: str
    task_dir: Path
    src_dir: Path
    steps: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    background: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    splits: List[Dict[str, Any]] = field(default_factory=list)
    check: Optional[Any] = None  # csf_check.CheckReport when the checker is available

    @property
    def built(self) -> List[Dict[str, Any]]:
        return [s for s in self.steps if s["status"] == "built"]

    @property
    def ok(self) -> bool:
        return bool(self.built) and (self.check is None or self.check.ok)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "steps": self.steps,
            "conflicts": self.conflicts,
            "background": self.background,
            "splits": self.splits,
            "notes": self.notes,
            "check": None
            if self.check is None
            else {"ok": self.check.ok, "errors": [str(e) for e in self.check.errors]},
        }

    def format(self) -> str:
        lines = [f"task {self.task_id}: {len(self.built)}/{len(self.steps)} milestones built -> {self.task_dir}"]
        for s in self.steps:
            lines.append(f"  {s['mid']:<8} {s.get('name') or '-':<44} {s['status']}: {s['detail']}")
        for repo, b in self.background.items():
            if "mode" in b:
                lines.append(
                    f"  contract-only {repo}: {b['history_commits']} commits in range, "
                    f"{b['contract_background_commits']} background commits changed contract paths"
                )
                continue
            lines.append(
                f"  background {repo}: {b['total']} commits, {b['applied_to_base']} applied to base, "
                f"{b['attached']} attached to steps, {b['unresolved']} unresolved, {b['unused']} unused, "
                f"{b['merges_skipped']} merges skipped"
            )
        lines += [f"  note: {n}" for n in self.notes]
        if self.check is not None:
            lines.append(self.check.format())
        return "\n".join(lines)


# ------------------------------------------------------------------ git helpers


def _git(cwd: Path, *args: str, input: Optional[bytes] = None, check: bool = True, env: Optional[dict] = None):
    full_env = {**os.environ, **GIT_ENV, **(env or {})}
    cmd = ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-c", "core.quotepath=off", *args]
    res = subprocess.run(cmd, cwd=cwd, input=input, capture_output=True, env=full_env)
    if check and res.returncode != 0:
        raise ExportError(f"git {' '.join(args)} failed in {cwd}: {res.stderr.decode(errors='replace').strip()}")
    return res


def _git_text(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.decode("utf-8", errors="replace").strip()


class _Sandbox:
    """A temp clone of one repo at its base commit; steps are cherry-picked onto it in order."""

    def __init__(self, spec: RepoSpec, checkout: Path, base: str, tmp: Path):
        self.spec = spec
        self.dir = tmp / spec.name
        _git(tmp, "clone", "-q", "--no-checkout", str(checkout), str(self.dir))
        _git(self.dir, "checkout", "-q", "--detach", base)
        self.filter = SrcFileFilter(
            src_dirs=spec.repo_src_dirs, test_dirs=spec.test_dirs, exclude_patterns=spec.exclude or None
        )
        self.base = base
        self.resolved_tests: List[str] = []
        self.split_rank: Dict[str, Tuple[str, int]] = {}  # split sub-commit -> (original commit, position in its chain)
        self.split_chain: Dict[str, List[str]] = {}  # original commit -> its sub-commits (+ test commit), in order
        self.split_union: Dict[str, bool] = {}  # original commit -> the chain's final tree equals the original's tree
        self.head = base
        self._order: Optional[Dict[str, int]] = None

    def resolve(self, sha: str) -> str:
        return _git_text(self.dir, "rev-parse", f"{sha}^{{commit}}")

    def add_splits(self, source: Path, splits: Dict[str, Dict[str, Any]]) -> None:
        """Fetch the split branches (from the trial's repo clone, read-only) and index their sub-commits."""
        for info in splits.values():
            _git(self.dir, "fetch", "-q", str(source), f"+refs/heads/{info['branch']}:refs/split/{info['branch']}")
            orig = self.resolve(info["original"])
            chain = [self.resolve(x) for x in info["commits"]]
            self.split_chain[orig] = chain
            last = _git_text(self.dir, "rev-parse", f"{chain[-1]}^{{tree}}")
            self.split_union[orig] = last == _git_text(self.dir, "rev-parse", f"{orig}^{{tree}}")
            for i, c in enumerate(chain, 1):
                self.split_rank[c] = (orig, i)

    def expanded_range(self) -> List[str]:
        """The range with every split original replaced by its chain of sub-commits."""
        return [x for c in self.range_commits() for x in self.split_chain.get(c, [c])]

    def order(self, shas: List[str]) -> List[str]:
        """Ancestors first: position in the topological history of the end ref (commit time for strays)."""
        if self._order is None:
            ref = self.spec.end if self.spec.end else self.spec.main_branch
            out = _git_text(self.dir, "rev-list", "--topo-order", "--reverse", ref)
            self._order = {sha: i for i, sha in enumerate(out.splitlines())}
        full = [self.resolve(s) for s in shas]
        stamp = {s: int(_git_text(self.dir, "show", "-s", "--format=%ct", s)) for s in full}

        def key(s: str):
            orig, number = self.split_rank.get(s, (s, 0))
            return (self._order.get(orig, len(self._order)), number, stamp[s], s)

        return sorted(dict.fromkeys(full), key=key)

    def range_commits(self) -> List[str]:
        """Every commit of the repo's range (``start..end``), oldest first in topological order."""
        end = self.spec.end or f"origin/{self.spec.main_branch}"
        out = _git_text(self.dir, "rev-list", "--reverse", "--topo-order", f"{self.base}..{end}")
        return out.splitlines()

    def is_merge(self, sha: str) -> bool:
        return len(_git_text(self.dir, "rev-list", "--parents", "-n", "1", sha).split()) > 2

    def files_of(self, sha: str) -> List[str]:
        """Files a commit changes (against its first parent), deletions included."""
        out = _git_text(self.dir, "show", "--format=", "--name-only", "--no-renames", "--first-parent", sha)
        return sorted(out.splitlines())

    def pick(self, sha: str, theirs: bool = False, resolve_tests: bool = False) -> Optional[List[str]]:
        """Cherry-pick one commit; ``None`` on success, else the conflicting files (the pick is aborted)."""
        args = [
            "cherry-pick", "-x", "--allow-empty", "--keep-redundant-commits", "--strategy=ort",
            "-X", f"rename-threshold={RENAME_THRESHOLD}", "-X", "patience",
        ]  # fmt: skip
        if theirs:
            args += ["-X", "theirs"]
        if self.is_merge(sha):
            args += ["-m", "1"]
        res = _git(self.dir, *args, sha, check=False)
        if res.returncode == 0:
            self.head = _git_text(self.dir, "rev-parse", "HEAD")
            return None
        files = _git_text(self.dir, "diff", "--name-only", "--diff-filter=U").splitlines()
        if resolve_tests and files and all(self.filter.is_test_file(f) for f in files):
            # conflicts only in test files do not matter: the hidden tests are the end state's blobs. Take the commit's side.
            for f in files:
                if _git(self.dir, "cat-file", "-e", f"{sha}:{f}", check=False).returncode == 0:
                    _git(self.dir, "checkout", sha, "--", f)
                else:
                    _git(self.dir, "rm", "-q", "-f", "--", f)
            _git(self.dir, "-c", "core.editor=true", "cherry-pick", "--continue")
            self.head = _git_text(self.dir, "rev-parse", "HEAD")
            self.resolved_tests = files
            return None
        _git(self.dir, "cherry-pick", "--abort", check=False)
        return sorted(files) or [res.stderr.decode(errors="replace").strip().splitlines()[0]]

    def reset(self, sha: str) -> None:
        _git(self.dir, "reset", "-q", "--hard", sha)
        self.head = sha

    def changed(self, a: str, b: str) -> List[Tuple[str, str]]:
        out = _git(self.dir, "diff", "--name-status", "--no-renames", "-z", a, b).stdout.decode("utf-8", "replace")
        tok = out.split("\0")
        return sorted((tok[i][0], tok[i + 1]) for i in range(0, len(tok) - 1, 2))

    def patch(self, a: str, b: str, paths: List[str]) -> bytes:
        if not paths:
            return b""
        return _git(
            self.dir, "diff", "--binary", "--full-index", "--no-color", "--no-renames", a, b, "--", *paths,
            env={"GIT_LITERAL_PATHSPECS": "1"},
        ).stdout  # fmt: skip

    def blob(self, rev: str, path: str) -> bytes:
        return _git(self.dir, "cat-file", "blob", f"{rev}:{path}").stdout


class _ContractRepo:
    """A contract-only repo: no cherry-picks. The state after step k is the real tree, restricted to the contract
    paths, at the newest (history order) commit of steps 1..k; read straight from the checkout (read-only git)."""

    def __init__(self, spec: RepoSpec, checkout: Path, base: str):
        self.spec = spec
        self.dir = checkout
        self.base = base
        self.pathspecs = [f":(glob){p}" for p in spec.contract_paths]
        end = spec.end or f"origin/{spec.main_branch}"
        out = _git_text(checkout, "rev-list", "--reverse", "--topo-order", f"{base}..{end}")
        self.history = out.splitlines()
        self.pos = {c: i for i, c in enumerate(self.history)}

    def resolve(self, sha: str) -> str:
        return _git_text(self.dir, "rev-parse", f"{sha}^{{commit}}")

    def files_of(self, sha: str) -> List[str]:
        """Contract files a commit changes (against its first parent)."""
        out = _git_text(
            self.dir, "show", "--format=", "--name-only", "--no-renames", "--first-parent", sha, "--", *self.pathspecs
        )
        return sorted(out.splitlines())

    def changed(self, a: str, b: str) -> List[str]:
        out = _git(self.dir, "diff", "--name-only", "--no-renames", "-z", a, b, "--", *self.pathspecs).stdout
        return sorted(f for f in out.decode("utf-8", "replace").split("\0") if f)

    def patch(self, a: str, b: str, paths: List[str], prefix: bool = False) -> bytes:
        if not paths:
            return b""
        pre = [f"--src-prefix=a/{self.spec.name}/", f"--dst-prefix=b/{self.spec.name}/"] if prefix else []
        return _git(
            self.dir, "diff", "--binary", "--full-index", "--no-color", "--no-renames", *pre, a, b, "--", *paths,
            env={"GIT_LITERAL_PATHSPECS": "1"},
        ).stdout  # fmt: skip


# ------------------------------------------------------------------ inputs


def _slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "x"


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _first(paths: List[Path]) -> Optional[Path]:
    return next((p for p in paths if p.is_file()), None)


def _load_inputs(ws: Workspace, data_root: Path, trial_name: str, explicit: bool = False):
    wid = ws.workspace_id
    res = data_root / "result" / f"{wid}_{trial_name}"
    trial = data_root / "trial_space" / f"{wid}_{trial_name}"
    mpath = _first(
        [
            res / "stage4_decomp" / "milestones.csv",
            res / "milestones.csv",
            trial / "stage4_decomp" / "output" / "milestones.csv",
            res / "stage2_mstone" / "milestones.csv",
            trial / "stage2_mstone" / "output" / "milestones.csv",
        ]
    )
    if mpath is None and not explicit:
        raise ExportError(f"no milestones.csv for trial {wid}_{trial_name} under {res} or {trial}")
    dpath = _first(
        [
            res / "stage4_decomp" / "dependencies.csv",
            res / "dependencies.csv",
            trial / "stage4_decomp" / "output" / "dependencies.csv",
            res / "stage3_depend" / "dependencies.csv",
            trial / "stage3_depend" / "output" / "dependencies.csv",
            res / "stage2_mstone" / "dependency_proposals.csv",
            trial / "stage2_mstone" / "output" / "dependency_proposals.csv",
        ]
    )
    wdir = workspace_dir(data_root, wid)
    commit_repos = json.loads((wdir / "commit_repos.json").read_text(encoding="utf-8"))
    return res, trial, mpath, dpath, commit_repos


def _load_splits(splits_dir: Path, trial: Path, ws: Workspace) -> Dict[str, Dict[str, Any]]:
    """``{split-branch: {original, commits, repo, branch}}`` from the stage-4 ``commit_splits/split-*/METADATA.json``.

    ``commits`` is the replacement chain: the sub-commits in order, then the integration-test commit (test files only).
    The repo is the trial clone (``repo_data/<repo>``) that has the ``split-<sha>`` branch."""
    out: Dict[str, Dict[str, Any]] = {}
    for meta in sorted(splits_dir.glob("split-*/METADATA.json")):
        m = json.loads(meta.read_text(encoding="utf-8"))
        sr = m["split_result"]
        chain = [c["sha"] for c in sr["src_commits"]]
        itc = (sr.get("integration_test_commit") or {}).get("sha")
        branch = meta.parent.name
        repo = None
        for r in ws.repos:
            probe = _git(
                trial / "repo_data" / r.name, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False
            )
            if probe.returncode == 0:
                repo = r.name
                break
        if repo is None:
            raise ExportError(f"{meta}: no repo clone under {trial / 'repo_data'} has the branch {branch}")
        out[branch] = {
            "original": m["original_commit"]["sha"],
            "commits": chain + ([itc] if itc else []),
            "repo": repo,
            "branch": branch,
        }
    return out


def _linearise(ids: List[str], deps: Dict[str, List[str]]) -> List[str]:
    """Stable topological order (Kahn, smallest id first among the ready ones)."""
    indeg = {m: len([p for p in deps.get(m, []) if p in ids]) for m in ids}
    children: Dict[str, List[str]] = defaultdict(list)
    for m in ids:
        for p in deps.get(m, []):
            if p in indeg:
                children[p].append(m)
    ready = sorted(m for m, d in indeg.items() if d == 0)
    out: List[str] = []
    while ready:
        cur = ready.pop(0)
        out.append(cur)
        for c in children[cur]:
            indeg[c] -= 1
            if indeg[c] == 0:
                ready.append(c)
        ready.sort()
    if len(out) != len(ids):
        raise ExportError(f"dependency cycle among milestones {sorted(set(ids) - set(out))}")
    return out


def _find_srs(ws: Workspace, data_root: Path, trial: Path, res: Path, mid: str) -> Optional[str]:
    cands: List[Path] = []
    for spec in ws.repos:
        cands += sorted((preprocessed_path(data_root, spec) / "srs").glob(f"*/{mid}/SRS.md"))
    cands += sorted((workspace_dir(data_root, ws.workspace_id) / "srs").glob(f"*/{mid}/SRS.md"))
    cands += [trial / "test_results" / mid / "srs" / "SRS.md", res / "test_results" / mid / "srs" / "SRS.md"]
    found = _first(cands)
    return found.read_text(encoding="utf-8").strip() if found else None


def _phase3_f2p(trial: Path, res: Path, repo_names: List[str], mid: str) -> Optional[Dict[str, List[str]]]:
    """F2P test ids per repo from Phase 3 (``test_results/<MID>/benchmark``), or None when not there yet."""
    for base in (trial, res):
        bench = base / "test_results" / mid / "benchmark"
        cls = bench / f"{mid}_classification.json"
        if not cls.is_file():
            continue
        data = json.loads(cls.read_text(encoding="utf-8"))
        ids = list(data.get("fail_to_pass", [])) + list(data.get("env_patch_skip_to_pass", []))
        out: Dict[str, List[str]] = {r: [] for r in repo_names}
        for tid in ids:
            head, _, rest = tid.partition("::")
            if head in out and rest:
                out[head].append(rest)
        return {r: sorted(set(v)) for r, v in out.items()}
    return None


def _detect_language(repo_dir: Path, base: str) -> str:
    files = set(_git_text(repo_dir, "ls-tree", "--name-only", base).splitlines())
    return next((lang for marker, lang in LANGUAGE_MARKERS if marker in files), "unknown")


def _gitwild_to_apply_globs(patterns: List[str]) -> List[str]:
    """``git apply --exclude`` globs (``*`` crosses ``/``) for gitwildmatch test dir patterns."""
    out: List[str] = []
    for p in patterns:
        g = p.replace("**", "*")
        out += [g] + ([g[2:]] if g.startswith("*/") else [])
    return sorted(set(out))


def _regen_script(repo_dir: Path, base: str) -> Optional[str]:
    """The repo's own script that regenerates its generated stubs from the contract, if it has one."""
    names = _git_text(repo_dir, "ls-tree", "-r", "--name-only", base).splitlines()
    scripts = [
        n for n in names if re.search(r"(^|/)(generate|gen|regen|sync)[-_a-z]*(grpc|proto)[-_a-z]*\.(sh|py)$", n)
    ]
    return sorted(scripts, key=lambda n: (n.count("/"), n))[0] if scripts else None


def _license(repo_dir: Path, base: str) -> Optional[str]:
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        res = _git(repo_dir, "cat-file", "blob", f"{base}:{name}", check=False)
        if res.returncode == 0:
            text = res.stdout.decode("utf-8", "replace")
            for needle, spdx in (("Apache License", "Apache-2.0"), ("MIT License", "MIT"), ("BSD", "BSD")):
                if needle in text:
                    return spdx
    return None


# ------------------------------------------------------------------ rendering


def _t(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _env_name(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", repo).upper()


def _dag_string(edges: Dict[str, List[str]]) -> str:
    """``m1 -> m2; m2 + m3 -> m4; m5`` (steps without any edge are listed on their own)."""
    linked = {p for ps in edges.values() for p in ps} | {t for t, ps in edges.items() if ps}
    parts = [f"{' + '.join(ps)} -> {t}" for t, ps in edges.items() if ps]
    return "; ".join(parts + [t for t in edges if t not in linked])


def _write(path: Path, data: str | bytes, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


BASE_PATCH_BLOCK = """# Background commits (history that no step owns) are part of the base state. The patch is bind-mounted for this one
# step, so it never lands in an image layer the agent could read.
RUN --mount=type=bind,source=base.patch,target=/tmp/csf/base.patch \\
    cd /app && git apply --whitespace=nowarn /tmp/csf/base.patch
"""


def _sparse_block(contract_only: Dict[str, List[str]]) -> str:
    out = []
    for name, paths in sorted(contract_only.items()):
        keep = " ".join(shlex.quote(f":(glob){p}") for p in paths)
        out.append(
            f"# {name} is contract-only: keep just the files matching its contract paths.\n"
            f"RUN cd /app/{name} \\\n"
            f" && git ls-files -z -- {keep} | sort -z > /tmp/keep.txt \\\n"
            " && git ls-files -z | sort -z > /tmp/all.txt \\\n"
            " && comm -z -23 /tmp/all.txt /tmp/keep.txt | xargs -0 -r rm -f \\\n"
            " && find . -type d -empty -not -path './.git/*' -delete \\\n"
            " && rm -f /tmp/keep.txt /tmp/all.txt\n"
        )
    return "\n".join(out)


def _render_dockerfile(
    task_id: str,
    repos: List[Dict[str, Any]],
    languages: List[str],
    base_patch: bool,
    contract_only: Optional[Dict[str, List[str]]] = None,
) -> str:
    clone = "\n".join(
        f"RUN git clone -q {r['url']} /app/{r['name']} \\\n && git -C /app/{r['name']} checkout -q --detach {r['commit']}"
        for r in repos
    )
    names = " ".join(r["name"] for r in repos)
    return string.Template((TEMPLATES / "Dockerfile.tmpl").read_text(encoding="utf-8")).substitute(
        task_id=task_id,
        repo_count=len(repos),
        clone_block=clone,
        repo_names=names,
        languages=", ".join(languages),
        base_patch_block=(_sparse_block(contract_only or {}) + "\n" if contract_only else "")
        + (BASE_PATCH_BLOCK if base_patch else ""),
    )


def _context_md(ws: Workspace, suites: Dict[str, Dict[str, Any]]) -> str:
    rows = [
        f"| `/app/{r.name}` | {r.full_name} | {r.role} | "
        + ("none: contract files only" if r.contract_only else suites[r.name]["framework"])
        + " |"
        for r in ws.repos
    ]
    contract = [r for r in ws.repos if r.role == "contract"]
    lines = [
        "## Environment",
        "",
        "Every repository below is a separate git checkout at the state this milestone starts from. Your changes from "
        "earlier milestones are still in place.",
        "",
        "| Path | Upstream | Role | Tests |",
        "| --- | --- | --- | --- |",
        *rows,
        "",
        "- There is no network access: do not try to fetch dependencies or the upstream repositories.",
        "- Your work is graded by hidden tests that run in every repository. A change made in only one repository "
        "is not enough when the milestone spans several.",
        "- Do not add or edit tests under the repositories' test directories; hidden tests replace them.",
    ]
    if contract:
        lines.append(
            f"- `{contract[0].name}` is the contract repository: the other repositories consume what it defines."
        )
    for r in ws.repos:
        if r.contract_only:
            lines.append(
                f"- `/app/{r.name}` is a sparse checkout: it holds only the contract files, there is nothing to build or "
                "test there. Edit the contract files in place."
            )
    return "\n".join(lines) + "\n"


def _step_md(
    ws: Workspace,
    row: Dict[str, str],
    repos: List[str],
    srs: Optional[str],
    contract_files: Optional[Dict[str, List[str]]] = None,
    regen: Optional[Dict[str, str]] = None,
) -> str:
    scope = ["## Repositories that must change", ""]
    for name in repos:
        spec = ws.get(name)
        scope.append(f"- `/app/{name}` ({spec.role}{', contract files only' if spec.contract_only else ''})")
    for name, files in (contract_files or {}).items():
        scope += ["", f"Contract files in scope in `/app/{name}`:", ""]
        scope += [f"- `{f}`" for f in files]
        clients = [r for r in repos if not ws.get(r).contract_only]
        scope += [
            "",
            "The clients' generated stubs must be regenerated from the contract, not edited by hand"
            + (
                ": " + ", ".join(f"`{regen[c]}` in `/app/{c}`" for c in clients if c in regen)
                if any(c in regen for c in clients)
                else ""
            )
            + ".",
        ]
    contract = [ws.get(r) for r in repos if ws.get(r).contract_paths]
    if contract:
        scope += ["", "Contract boundary: " + ", ".join(f"`{p}`" for c in contract for p in c.contract_paths) + "."]
    body = srs if srs else f"# {row['title']}\n\n{row.get('mini_srs', '').strip()}"
    return body.rstrip("\n") + "\n\n" + "\n".join(scope) + "\n"


def _render_toml(meta: Dict[str, Any]) -> str:
    ws_repos = meta["source_repos"]
    contract = next((r for r in ws_repos if r["name"] == meta["contract_repo"]), ws_repos[0])
    t = [
        'schema_version = "1.4"',
        'multi_step_reward_strategy = "mean"',
        "",
        "[task]",
        f"name = {_t('csf/' + meta['slug'])}",
        'version = "0.1.0"',
        f"description = {_t(meta['description'])}",
        f"keywords = {_t(['csf', meta['family'], *meta['languages'], 'multi-repo', 'multi-step'])}",
        "",
        "[[task.authors]]",
        'name = "Deccan AI"',
        'email = "csf@deccan.ai"',
        "",
        "[metadata]",
        f"family = {_t(meta['family'])}",
        f"source_repo = {_t(contract['url'])}",
        f"source_commit = {_t(contract['commit'])}",
        f"milestones_planned = {meta['planned']}",
        f"milestones_built = {meta['built']}",
        f"languages = {_t(meta['languages'])}",
        f"repos = {_t(meta['repos'])}",
        "repo_roles = { " + ", ".join(f"{_t(k)} = {_t(v)}" for k, v in meta["repo_roles"].items()) + " }",
        f"contract_only_repos = {_t(meta['contract_only_repos'])}",
        "source_repos = [",
        *[f"  {{ name = {_t(r['name'])}, url = {_t(r['url'])}, commit = {_t(r['commit'])} }}," for r in ws_repos],
        "]",
        'csf_task_source = "derived"',
        f"csf_milestone_dag = {_t(meta['dag'])}",
    ]
    if meta["license"]:
        t.append(f"source_license = {_t(meta['license'])}")
    t += [
        "",
        "[environment]",
        "build_timeout_sec = 3600",
        "cpus = 4",
        "memory_mb = 8192",
        "storage_mb = 20480",
        'workdir = "/app"',
        "",
        "[agent]",
        "timeout_sec = 7200",
        "",
        "[verifier]",
        'environment_mode = "separate"',
        "timeout_sec = 1800",
        "",
        "# Runs in the agent's container after each step: writes /logs/artifacts/model.<repo>.patch for every repo.",
        "[[verifier.collect]]",
        'command = "csf-collect"',
        "timeout_sec = 300",
    ]
    for name in meta["step_names"]:
        t += ["", "[[steps]]", f"name = {_t(name)}"]
    return "\n".join(t) + "\n"


def _render_readme(task_id: str, ws: Workspace, report: ExportReport, bases: Dict[str, str], dag: str) -> str:
    rows = [
        "| # | Step | Milestone | Repos | Gold files | Hidden test files | Status |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, s in enumerate(report.steps, 1):
        gold = ", ".join(f"{r} {n}" for r, n in s.get("src_files", {}).items()) or "-"
        hidden = ", ".join(f"{r} {n}" for r, n in s.get("test_files", {}).items()) or "-"
        rows.append(
            f"| {i} | {s.get('name') or '-'} | {s['mid']} | {', '.join(s['repos'])} | {gold} | {hidden} | "
            f"{s['status']}: {s['detail']} |"
        )
    repo_rows = [
        f"| {r.name} | {r.role}{' (contract-only, sparse)' if r.contract_only else ''} | {r.full_name} | "
        f"`{r.start}` (`{bases[r.name][:12]}`) |"
        for r in ws.repos
    ]
    contract_only = [r for r in ws.repos if r.contract_only]
    sparse = (
        [
            "## Contract-only repos",
            "",
            *[
                f"`/app/{r.name}` is a SPARSE repo holding only the files matching its `contract_paths` "
                f"({', '.join(f'`{p}`' for p in r.contract_paths)}). Its state after step k is the real tree, restricted to "
                "those paths, at the newest commit (history order) among the commits of steps 1..k, so older commits' "
                "contract changes land in the gold of the first step whose prefix includes them (`contract_background` in "
                "`_src/export_report.json`). Nothing is cherry-picked, tests of that repo are never exported, and it has "
                "no entry in `tests/suites.json` (only a `contract_only` record). `csf-collect` still collects it, so "
                "contract edits by the agent are applied in the verifier before the clients' suites run."
                for r in contract_only
            ],
            "",
        ]
        if contract_only
        else []
    )
    return "\n".join(
        [
            f"# {task_id}",
            "",
            f"Cross-repo milestone task derived from the `{ws.workspace_id}` workspace with DeepCommit. One agent keeps one "
            "environment through the steps below with no reset between them; after each step Harbor collects the "
            "agent's work and a clean verifier runs hidden tests across all repos with a test suite. Episode score is the mean over steps.",
            "",
            "## Status",
            "",
            *rows,
            "",
            "Built steps are `pending: Phase 3 test curation` unless noted: F2P/P2P lists are the hidden test FILES the "
            "milestone's commits changed, not curated test ids. Gates (oracle, NOP, per-step negative control) have not been run.",
            "",
            f"DAG: `{dag or 'none'}`",
            "",
            "## Repositories",
            "",
            "| Repo | Role | Upstream | Base |",
            "| --- | --- | --- | --- |",
            *repo_rows,
            "",
            *sparse,
            "## How grading works",
            "",
            "1. `csf-collect` (agent container, after each step) diffs every `/app/<repo>` against `/opt/csf/base/<repo>.git` "
            "and writes `/logs/artifacts/model.<repo>.patch`.",
            "2. `tests/test.sh` (clean verifier container) applies each patch to the base tree, drops changes under the "
            "repo's test dirs, copies `tests/hidden/<repo>/` over, runs each repo's suites from `tests/suites.json`, "
            "then `tests/grade.py` merges the results (ids are `<repo>::<id>`).",
            "3. `reward.json`: `reward` is 1 only if every F2P and every P2P test of every repo passed; it also has "
            "`f2p_total`, `f2p_passed`, `p2p_total`, `p2p_passed` and the same four per repo (`f2p_total__<repo>`, ...).",
            "",
            "## How the states were cut",
            "",
            "Per repo the base is the start tag. State after step k = base + the commits of steps 1..k cherry-picked in "
            "topological order (`-x --allow-empty`, ort, patience). Gold of step k per repo = diff of the two states "
            "restricted to non-test files (`steps/<step>/solution/gold/<repo>.patch`); test files the milestone changed are "
            "hidden (`steps/<step>/tests/hidden/<repo>/`, cumulative over steps 1..k). Cherry-pick conflicts are listed "
            "in `_src/export_report.json` and block the step and its dependents.",
            "",
            "## Regenerating `steps/`",
            "",
            "```",
            f"python3 ../_src/{task_id}/render.py",
            "```",
            "",
            "## Known limits",
            "",
            "- `environment/Dockerfile` is a skeleton: toolchains, dependency prefetch (no network at run time) and "
            "the build are TODO; it was not built.",
            "- `tests/test.sh` and the Dockerfile were not executed (no Docker in the export environment).",
            "- `environment/base.patch` (when present) carries the background commits applied to the base; commits that "
            "conflicted there are retried before the first step touching the same files (`attached_background` in "
            "`_src/export_report.json`).",
            "",
        ]
    )


# ------------------------------------------------------------------ orchestration


def _next_task_id(out_dir: Path, workspace_id: str) -> str:
    slug = _slug(workspace_id, 60)
    n = 1
    while (out_dir / f"csf-{slug}-{n:03d}").exists():
        n += 1
    return f"csf-{slug}-{n:03d}"


def _suites(ws: Workspace, raw_manifest: dict, sandboxes: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    overrides = {r.get("name") or r.get("repo"): r.get("csf_suite") or {} for r in raw_manifest.get("repos", [])}
    out: Dict[str, Dict[str, Any]] = {}
    for spec in (r for r in ws.repos if not r.contract_only):
        lang = sandboxes[spec.name]
        framework, run_cmd, parser = SUITE_DEFAULTS[lang]
        suite = {
            "repo": spec.name,
            "workdir": ".",
            "language": lang,
            "framework": framework,
            "run_cmd": run_cmd,
            "parser": parser,
        }
        suite.update(overrides.get(spec.name, {}))
        suite["test_excludes"] = _gitwild_to_apply_globs(spec.test_dirs)
        out[spec.name] = suite
    return out


def export_csf_task(
    workspace_manifest: Path | str,
    trial_name: str,
    data_root: Path | str,
    out_dir: Path | str,
    task_id: Optional[str] = None,
    family: str = "cross-repo-change",
    milestones: Optional[List[str]] = None,
    source_license: Optional[str] = None,
    min_repos: int = 1,
    milestones_csv: Optional[Path | str] = None,
    dependencies_csv: Optional[Path | str] = None,
) -> ExportReport:
    """Export a workspace trial as ``<out_dir>/<task_id>/`` (+ ``<out_dir>/../_src/<task_id>/``).

    ``out_dir`` is the family folder (``tasks/<family>``). ``milestones`` restricts/limits the exported milestone ids; ``min_repos=2`` keeps only
    steps whose gold spans at least two repos (what the CSF checker requires of a cross-repo task).
    """
    data_root, out_dir = Path(data_root), Path(out_dir)
    ws = load_workspace(workspace_manifest)
    raw_manifest = yaml.safe_load(Path(workspace_manifest).read_text(encoding="utf-8"))
    res, trial, mpath, dpath, commit_repos = _load_inputs(ws, data_root, trial_name, bool(milestones_csv))
    mpath = Path(milestones_csv) if milestones_csv else mpath
    dpath = Path(dependencies_csv) if dependencies_csv else dpath
    task_id = task_id or _next_task_id(out_dir, ws.workspace_id)
    task_dir, src_dir = out_dir / task_id, out_dir.parent / "_src" / task_id
    report = ExportReport(task_id=task_id, task_dir=task_dir, src_dir=src_dir)

    rows = {r["id"]: r for r in _read_csv(mpath)}
    if milestones:
        missing = [m for m in milestones if m not in rows]
        if missing:
            raise ExportError(f"milestones not in {mpath}: {missing}")
        rows = {m: rows[m] for m in milestones}
    deps: Dict[str, List[str]] = defaultdict(list)
    if dpath is not None:
        for d in _read_csv(dpath):
            deps[d["dependant_id"]].append(d["prerequisite_id"])
        report.notes.append(f"dependencies from {dpath}")
    else:
        report.notes.append("no dependencies.csv found: milestones ordered by id")
    report.notes.append(f"milestones from {mpath}")
    order = _linearise(list(rows), deps)

    names = [r.name for r in ws.repos]
    bases: Dict[str, str] = {}
    for spec in ws.repos:
        if not spec.start:
            raise ExportError(f"repo {spec.name}: export needs a start tag (since/until is not supported)")
        bases[spec.name] = _git_text(repo_checkout_path(data_root, spec), "rev-parse", f"{spec.start}^{{commit}}")

    splits_dir = mpath.parent / "commit_splits"
    splits = _load_splits(splits_dir, trial, ws) if splits_dir.is_dir() else {}
    split_repo = {c[:7].lower(): i["repo"] for i in splits.values() for c in i["commits"]}
    # per-milestone, per-repo commits (the milestone's `repos` column is propagated; commit_repos.json decides)
    plan: Dict[str, Dict[str, List[str]]] = {}
    for mid, row in rows.items():
        by_repo: Dict[str, List[str]] = defaultdict(list)
        itc = [c for c in (row.get("integration_test_commit") or "").split(";") if c.strip()]
        for sha in [c for c in (row.get("commits") or "").split(";") if c] + itc:
            key = sha[:7].lower()
            if key in commit_repos:
                by_repo[commit_repos[key]].append(sha)
            elif key in split_repo:
                by_repo[split_repo[key]].append(sha)
            else:
                raise ExportError(f"milestone {mid}: commit {sha} is in neither commit_repos.json nor commit_splits")
        declared = sorted(x for x in (row.get("repos") or "").split(";") if x)
        if declared and declared != sorted(by_repo):
            report.notes.append(f"{mid}: repos column {declared} differs from commits' repos {sorted(by_repo)}")
        plan[mid] = {r: by_repo[r] for r in names if r in by_repo}

    tmp_root = Path(tempfile.mkdtemp(prefix="csf-export-"))
    try:
        return _export(
            ws, raw_manifest, report, rows, order, deps, plan, bases, data_root, res, trial, tmp_root,
            family, source_license, min_repos, splits,
        )  # fmt: skip
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _export(
    ws,
    raw_manifest,
    report,
    rows,
    order,
    deps,
    plan,
    bases,
    data_root,
    res,
    trial,
    tmp,
    family,
    source_license,
    min_repos,
    splits,
):
    task_dir, src_dir, task_id = report.task_dir, report.src_dir, report.task_id
    names = [r.name for r in ws.repos]
    boxes: Dict[str, _Sandbox] = {}

    def box(name: str) -> _Sandbox:
        if name not in boxes:
            sb = _Sandbox(ws.get(name), repo_checkout_path(data_root, ws.get(name)), bases[name], tmp)
            mine = {b: i for b, i in splits.items() if i["repo"] == name}
            if mine:
                sb.add_splits(trial / "repo_data" / name, mine)
            boxes[name] = sb
        return boxes[name]

    contract_only = [n for n in names if ws.get(n).contract_only]
    clients = [n for n in names if n not in contract_only]
    crepo = {n: _ContractRepo(ws.get(n), repo_checkout_path(data_root, ws.get(n)), bases[n]) for n in contract_only}
    languages = {n: _detect_language(box(n).dir, bases[n]) for n in clients}
    suites = _suites(ws, raw_manifest, languages)
    regen = {n: _regen_script(box(n).dir, bases[n]) for n in clients}
    regen = {n: path for n, path in regen.items() if path}
    licenses = sorted(
        {lic for lic in (_license(repo_checkout_path(data_root, ws.get(n)), bases[n]) for n in names) if lic}
    )

    for sub in (task_dir, src_dir):
        shutil.rmtree(sub, ignore_errors=True)
    built: List[Dict[str, Any]] = []  # per built step: name, mid, env, gold repos
    dead: Dict[str, str] = {}  # milestone -> "blocked" | "skipped", for its dependants
    earlier_f2p: Dict[str, List[str]] = {n: [] for n in names}

    # ---- which milestones become steps: they must have hidden tests (or Phase 3 F2P) and span min_repos repos
    phase3 = {mid: _phase3_f2p(trial, res, names, mid) for mid in order}
    exported: Dict[str, bool] = {}
    owned: Dict[str, set] = {n: set() for n in names}
    for mid in order:
        has_tests = phase3[mid] is not None and any(phase3[mid].values())
        for repo, shas in plan[mid].items():
            if repo in crepo:
                plan[mid][repo] = [crepo[repo].resolve(x) for x in shas]
                continue  # a contract-only repo has no hidden tests
            sb = box(repo)
            full = [sb.resolve(x) for x in shas]
            has_tests = has_tests or any(sb.filter.is_test_file(f) for c in full for f in sb.files_of(c))
            plan[mid][repo] = full
        exported[mid] = has_tests and len(plan[mid]) >= min_repos
        if exported[mid]:
            for repo, full in plan[mid].items():
                owned[repo].update(full)

    # ---- background first: every range commit no exported step owns goes onto the base, in history order;
    # the ones that conflict are retried right before the first step commit touching the same files
    deferred: Dict[str, List[Dict[str, Any]]] = {n: [] for n in names}
    stats: Dict[str, Dict[str, Any]] = {}
    for repo in clients:
        sb = box(repo)
        commits = sb.expanded_range()
        stats[repo] = {"total": len(commits), "applied_to_base": 0, "merges_skipped": 0, "owned_by_steps": 0}
        for sha in commits:
            if sha in owned[repo]:
                stats[repo]["owned_by_steps"] += 1
            elif sb.is_merge(sha):
                stats[repo]["merges_skipped"] += 1  # its content arrives through the commits it merges
            elif sb.pick(sha) is None:
                stats[repo]["applied_to_base"] += 1
            else:
                deferred[repo].append({"commit": sha, "files": sb.files_of(sha), "tried": False})
    state = {n: box(n).head for n in clients}
    base_heads = dict(state)
    attached: Dict[str, List[Dict[str, Any]]] = {n: [] for n in clients}
    # contract-only repos: the base is the real contract tree just before the first exported-step commit that touches
    # the contract; background contract changes older than that are part of the base, newer ones attach to steps
    cbase: Dict[str, str] = {}
    cpos: Dict[str, int] = {}
    for repo, cr in crepo.items():
        firsts = sorted(cr.pos[c] for c in owned[repo] if c in cr.pos and cr.files_of(c))
        cpos[repo] = (firsts[0] if firsts else 0) - 1
        cbase[repo] = cr.history[cpos[repo]] if cpos[repo] >= 0 else bases[repo]
    cstate = dict(cbase)  # the commit whose contract tree is the state
    cbackground = {n: 0 for n in contract_only}

    history = {n: {c: i for i, c in enumerate(box(n).expanded_range())} for n in clients}

    def retry_background(repo: str, sha: str, after: bool) -> None:
        """Retry the deferred background commits that touch the same files as step commit ``sha`` (and, transitively,
        the deferred ones they overlap): the ones older than it right before it, the newer ones (which build on it)
        right after it. Retried in history order."""
        sb = box(repo)
        idx = history[repo]
        pos = idx.get(sha, -1)
        pool = [d for d in deferred[repo] if (idx[d["commit"]] > pos) == after]
        files = set(sb.files_of(sha))
        picked: List[Dict[str, Any]] = []
        grew = True
        while grew:
            grew = False
            for d in pool:
                if d not in picked and files & set(d["files"]):
                    picked.append(d)
                    files |= set(d["files"])
                    grew = True
        for d in sorted(picked, key=lambda d: idx[d["commit"]]):
            how = "clean"
            if sb.pick(d["commit"]) is not None:
                how = "theirs"
                if sb.pick(d["commit"], theirs=True) is not None:
                    d["tried"] = True
                    continue
            deferred[repo].remove(d)
            attached[repo].append({"commit": d["commit"], "resolution": how, "files": d["files"]})

    for mid in order:
        row = rows[mid]
        touched = [r for r in plan[mid] if r not in crepo]
        entry: Dict[str, Any] = {"mid": mid, "title": row["title"], "repos": list(plan[mid]), "name": None}
        report.steps.append(entry)
        if not exported[mid]:
            entry.update(status="background", detail="no hidden tests or too few repos: its commits are background")
            continue
        bad_parent = next((p for p in deps.get(mid, []) if p in dead), None)
        if bad_parent:
            dead[mid] = "skipped"
            entry.update(status="skipped", detail=f"depends on {bad_parent}, which is {dead[bad_parent]}")
            continue
        entry["split_commits"] = [
            {
                "repo": repo,
                "original": orig,
                "sub_commits": [c for c in plan[mid][repo] if c in chain],
                "union_ok": box(repo).split_union[orig],
            }
            for repo in touched
            for orig, chain in box(repo).split_chain.items()
            if any(c in chain for c in plan[mid][repo])
        ]
        before = {n: box(n).head for n in touched}
        saved = {n: (list(deferred[n]), list(attached[n])) for n in touched}
        mark = {n: len(attached[n]) for n in touched}
        conflict = None
        test_conflicts: List[Dict[str, Any]] = []
        for repo in touched:
            sb = box(repo)
            for sha in sb.order(plan[mid][repo]):
                retry_background(repo, sha, after=False)
                sb.resolved_tests = []
                files = sb.pick(sha, resolve_tests=True)
                if files is None and sb.resolved_tests:
                    test_conflicts.append({"repo": repo, "commit": sha, "files": sb.resolved_tests})
                if files is None:
                    retry_background(repo, sha, after=True)
                else:
                    conflict = {"step": mid, "milestone": mid, "repo": repo, "commit": sha, "files": files}
                    break
            if conflict:
                break
        if conflict:
            for n, sha in before.items():
                box(n).reset(sha)
                deferred[n], attached[n] = saved[n]
            report.conflicts.append(conflict)
            dead[mid] = "blocked"
            entry.update(
                status="blocked",
                detail=f"cherry-pick conflict in {conflict['repo']} at {conflict['commit'][:10]}: "
                + ", ".join(conflict["files"][:4]),
            )
            continue
        entry["test_file_conflicts_resolved"] = test_conflicts
        entry["attached_background"] = [
            {"repo": n, **a} for n in touched for a in attached[n][mark[n] :]
        ]  # fmt: skip

        gold: Dict[str, bytes] = {}
        hidden: Dict[str, Dict[str, bytes]] = {}
        deleted: Dict[str, List[str]] = {}
        for repo in touched:
            sb = box(repo)
            changes = sb.changed(state[repo], sb.head)
            tests = [p for s, p in changes if sb.filter.is_test_file(p)]
            patch = sb.patch(state[repo], sb.head, [p for _, p in changes if not sb.filter.is_test_file(p)])
            if patch:
                gold[repo] = patch
            hidden[repo] = {p: sb.blob(sb.head, p) for s, p in changes if s != "D" and p in tests}
            deleted[repo] = [p for s, p in changes if s == "D" and p in tests]
            state[repo] = sb.head
        entry["src_files"] = {r: p.count(b"\ndiff --git ") + 1 for r, p in gold.items()}
        entry["test_files"] = {r: len(h) for r, h in hidden.items() if h}
        f2p_now = {r: phase3[mid][r] if phase3[mid] is not None else sorted(hidden.get(r, {})) for r in clients}
        # contract-only repos: the state moves to the newest commit among steps 1..k; the diff is this step's gold
        pending: Dict[str, Any] = {}
        contract_bg: List[Dict[str, Any]] = []
        for repo, cr in crepo.items():
            new_pos = max([cpos[repo], *(cr.pos[c] for c in plan[mid].get(repo, []) if c in cr.pos)])
            if new_pos == cpos[repo]:
                continue
            new_state = cr.history[new_pos]
            patch = cr.patch(cstate[repo], new_state, cr.changed(cstate[repo], new_state))
            if patch:
                gold[repo] = patch
            pending[repo] = (new_pos, new_state, patch)
            entry.setdefault("contract_gold_lines", {})[repo] = patch.count(b"\n")
            for c in cr.history[cpos[repo] + 1 : new_pos + 1]:
                if c not in owned[repo] and cr.files_of(c):
                    contract_bg.append({"repo": repo, "commit": c, "files": cr.files_of(c)})
        if not gold or not any(f2p_now.values()):
            entry.update(status="background", detail="no net source change or no hidden tests: state kept, no step")
            continue
        for repo, (new_pos, new_state, _) in pending.items():
            cpos[repo], cstate[repo] = new_pos, new_state
            cbackground[repo] += sum(1 for x in contract_bg if x["repo"] == repo)
        entry["contract_background"] = contract_bg
        entry["contract_background_count"] = len(contract_bg)
        entry["src_files"] = {r: p.count(b"\ndiff --git ") + 1 for r, p in gold.items()}
        contract_files = {
            r: sorted(m.decode() for m in re.findall(rb"^diff --git a/(\S+) b/", gold[r], re.M))
            for r in pending
            if gold.get(r)
        }
        name = f"m{len(built) + 1}-{_slug(row['title'])}"
        env: Dict[str, str] = {}
        for repo in clients:
            env[f"F2P_{_env_name(repo)}"] = " ".join(f2p_now[repo])
            env[f"P2P_EARLIER_{_env_name(repo)}"] = " ".join(t for t in earlier_f2p[repo] if t not in f2p_now[repo])
        for repo in clients:
            gone = set(deleted.get(repo, []))
            earlier_f2p[repo] = list(dict.fromkeys(t for t in earlier_f2p[repo] + f2p_now[repo] if t not in gone))
        detail = "F2P from Phase 3" if phase3[mid] is not None else "pending: Phase 3 test curation"
        big = {r: n for r, n in entry.get("contract_gold_lines", {}).items() if n > CONTRACT_GOLD_FLAG}
        if big:
            entry["contract_gold_large"] = big
            detail += "; FLAG contract gold above %d lines: %s" % (CONTRACT_GOLD_FLAG, big)
        if entry["attached_background"]:
            detail += f"; {len(entry['attached_background'])} background commit(s) attached to the gold"
        if any(deleted.values()):
            report.notes.append(f"{mid}: test files deleted by the milestone (not hidden): {deleted}")
        entry.update(name=name, status="built", detail=detail)
        built.append(
            {
                "name": name, "mid": mid, "env": env, "gold": gold, "hidden": hidden, "row": row,
                "contract_files": contract_files,
            }
        )  # fmt: skip

    for repo in clients:
        for orig, chain in box(repo).split_chain.items():
            report.splits.append(
                {"repo": repo, "original": orig, "chain": chain, "union_ok": box(repo).split_union[orig]}
            )
    for sp in report.splits:
        if not sp["union_ok"]:
            report.notes.append(f"split {sp['original'][:10]} in {sp['repo']}: UNION MISMATCH (final tree differs)")
    for repo in clients:
        left = deferred[repo]
        stats[repo].update(
            attached=sum(1 for st in report.steps for a in st.get("attached_background", []) if a["repo"] == repo),
            unresolved=sum(1 for d in left if d["tried"]),
            unused=sum(1 for d in left if not d["tried"]),
            deferred_commits=[d["commit"] for d in left],
        )
    report.background = {r: dict(sorted(v.items())) for r, v in stats.items()}
    for repo, cr in crepo.items():
        report.background[repo] = {
            "mode": "contract-only",
            "history_commits": len(cr.history),
            "contract_background_commits": cbackground[repo],
            "contract_base_commit": cbase[repo],
            "contract_base_background_commits": sum(
                1
                for c in crepo[repo].history[: crepo[repo].pos.get(cbase[repo], -1) + 1]
                if c not in owned[repo] and crepo[repo].files_of(c)
            ),
        }

    # the environment base = start tag + the clean background commits, shipped as one patch rooted at /app
    base_patch = b"".join(
        _git(
            box(r).dir, "diff", "--binary", "--full-index", "--no-color", "--no-renames",
            f"--src-prefix=a/{r}/", f"--dst-prefix=b/{r}/", bases[r], base_heads[r],
        ).stdout
        for r in clients
    ) + b"".join(
        cr.patch(bases[r], cbase[r], cr.changed(bases[r], cbase[r]), prefix=True) for r, cr in crepo.items()
    )  # fmt: skip

    # ---- authoring sources
    step_labels = {b["mid"]: f"m{i}" for i, b in enumerate(built, 1)}
    edges = {
        step_labels[b["mid"]]: sorted(step_labels[p] for p in deps.get(b["mid"], []) if p in step_labels) for b in built
    }
    dag = _dag_string(edges)
    _write(src_dir / "instructions" / "_context.md", _context_md(ws, suites))
    for b in built:
        name = b["name"]
        repos = [r for r in names if r in b["gold"]]
        _write(
            src_dir / "instructions" / f"{name}.md",
            _step_md(ws, b["row"], repos, _find_srs(ws, data_root, trial, res, b["mid"]), b["contract_files"], regen),
        )
        for repo, patch in b["gold"].items():
            _write(src_dir / "gold" / name / f"{repo}.patch", patch)
        for repo, files in b["hidden"].items():
            for path, data in files.items():
                _write(src_dir / "hidden" / name / repo / path, data)
    steps_json = {
        "task_id": task_id,
        "family": family,
        "task_dir_rel": os.path.relpath(task_dir, src_dir),
        "steps": [
            {"name": b["name"], "milestone": b["mid"], "gold_repos": sorted(b["gold"]), "env": dict(sorted(b["env"].items()))}
            for b in built
        ],
    }  # fmt: skip
    _write(src_dir / "steps.json", json.dumps(steps_json, indent=2, sort_keys=True) + "\n")
    with open(src_dir / "milestones.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["step", "id", "title", "category", "repos", "commits", "depends_on"])
        for b in built:
            r = b["row"]
            w.writerow([b["name"], b["mid"], r["title"], r.get("category", ""), ";".join(plan[b["mid"]]), r["commits"],
                        ";".join(deps.get(b["mid"], []))])  # fmt: skip
    _write(src_dir / "render.py", (TEMPLATES / "render.py").read_text(encoding="utf-8"), executable=True)

    # ---- the task
    repo_meta = [
        {"name": r.name, "url": f"https://github.com/{r.full_name}", "commit": bases[r.name]} for r in ws.repos
    ]
    contract = next((r.name for r in ws.repos if r.role == "contract"), names[0])
    all_langs = sorted({lang for lang in languages.values() if lang != "unknown"})
    meta = {
        "slug": task_id[4:] if task_id.startswith("csf-") else task_id,
        "description": f"Cross-repo milestones over {', '.join(r.full_name for r in ws.repos)} "
        f"({ws.repos[0].start}..{ws.repos[0].end}): {len(built)} of {len(report.steps)} planned milestones built.",
        "family": family,
        "planned": len(report.steps),
        "built": len(built),
        "languages": all_langs,
        "repos": names,
        "repo_roles": {r.name: r.role for r in ws.repos},
        "contract_only_repos": contract_only,
        "source_repos": repo_meta,
        "contract_repo": contract,
        "dag": dag,
        "license": source_license or ",".join(licenses),
        "step_names": [b["name"] for b in built],
    }
    _write(task_dir / "task.toml", _render_toml(meta))
    _write(
        task_dir / "environment" / "Dockerfile",
        _render_dockerfile(
            task_id, repo_meta, all_langs, bool(base_patch), {n: ws.get(n).contract_paths for n in contract_only}
        ),
    )
    if base_patch:
        _write(task_dir / "environment" / "base.patch", base_patch)
    _write(
        task_dir / "environment" / "csf-collect",
        (TEMPLATES / "csf-collect").read_text(encoding="utf-8").replace("@REPOS@", " ".join(names)),
        True,
    )
    _write(task_dir / "tests" / "test.sh", (TEMPLATES / "test.sh").read_text(encoding="utf-8"), True)
    _write(task_dir / "tests" / "grade.py", (TEMPLATES / "grade.py").read_text(encoding="utf-8"), True)
    _write(
        task_dir / "tests" / "p2p_core.txt",
        "# Core regression tests run as P2P at every step: '<repo> <test file or id>' per line.\n",
    )
    _write(
        task_dir / "tests" / "suites.json",
        json.dumps(
            {
                "suites": [suites[n] for n in clients],
                "contract_only": {
                    n: {"mode": "contract-only", "contract_paths": ws.get(n).contract_paths} for n in contract_only
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    _write(task_dir / "README.md", _render_readme(task_id, ws, report, bases, dag))
    subprocess.run(
        [sys.executable, str(src_dir / "render.py"), "--task", str(task_dir)], check=True, capture_output=True
    )
    _write(src_dir / "export_report.json", json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")

    if not built:
        report.notes.append("no milestone could be built")
    try:
        from . import csf_check
    except ImportError:
        report.notes.append("csf_check is not available: the task was not validated")
    else:
        report.check = csf_check.check_task(task_dir)
        _write(src_dir / "export_report.json", json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    return report
