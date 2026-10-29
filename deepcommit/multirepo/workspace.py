"""Workspace manifest: the multi-repo input to DeepCommit.

A *workspace* is 1..5 repositories that are developed together (for example a protocol repo plus a
server and an SDK). Every member keeps the single-repo contract used by Phase 1 (``repo_src_dirs`` /
``test_dirs`` / ``exclude`` / ``main_branch`` and a ``start``/``end`` tag range), so each member can be
preprocessed by the unchanged single-repo pipeline. The workspace adds the glue needed for the merge step.

Manifest (``config/<workspace_id>.workspace.yaml``)::

    workspace_id: livekit_stack_v1
    repos:
      - name: protocol            # short, filesystem-safe; becomes the directory name under repo_data/
        owner: livekit
        repo: protocol
        role: contract            # contract | server | sdk | library
        start: v1.50.0            # tag, or use since/until timestamps
        end: v1.53.0
        repo_src_dirs: [protobufs/]
        test_dirs: ["**/*_test.go"]
        exclude: []
        contract_paths: ["**/*.proto"]
      - name: server
        ...
    xrepo:
      pin_files: [go.mod, Cargo.toml, package.json, pyproject.toml, .gitmodules]
      link_confidence_threshold: 0.7
      time_window_days: 3
      generated_globs: ["**/*.pb.go", "**/*_pb2*.py"]

Path convention used everywhere downstream: inside a workspace every repo lives at ``repo_data/<name>/`` and
every path in merged data is ``<name>/<path-in-repo>``. ``prefix_path`` / ``split_path`` convert between the two.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

MAX_REPOS = 5
VALID_ROLES = ("contract", "server", "sdk", "library")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

DEFAULT_PIN_FILES = (
    "go.mod",
    "Cargo.toml",
    "package.json",
    "pyproject.toml",
    "Package.swift",
    ".gitmodules",
)
DEFAULT_GENERATED_GLOBS = (
    "**/*.pb.go",
    "**/*_pb2.py",
    "**/*_pb2_grpc.py",
    "**/*_pb2.pyi",
    "**/*.pb.rs",
    "**/*.twirp.go",
    "**/*_grpc.pb.go",
    "**/generated/**",
    "**/gen/**",
)


class WorkspaceError(ValueError):
    """Raised for an invalid workspace manifest."""


@dataclass
class RepoSpec:
    name: str
    owner: str
    repo: str
    role: str = "library"
    start: str = ""
    end: str = ""
    since: str = ""
    until: str = ""
    main_branch: str = "main"
    repo_src_dirs: List[str] = field(default_factory=list)
    test_dirs: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    contract_paths: List[str] = field(default_factory=list)
    contract_only: bool = False  # exported as a sparse repo holding only the contract_paths files (no cherry-picks)
    base_image_name: Optional[str] = None
    build_command: Optional[str] = None

    @property
    def repo_id(self) -> str:
        """Same normalization as ``parse_repo_spec``: ``owner/repo`` -> ``owner_repo``."""
        return f"{self.owner}_{self.repo}"

    @property
    def time_span(self) -> str:
        return f"{self.start}_{self.end}"

    @property
    def preprocessed_dirname(self) -> str:
        """Directory name used by the single-repo Phase 1 (``<repo_id>_<start>_<end>``)."""
        return f"{self.repo_id}_{self.time_span}"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    def prefixed_src_dirs(self) -> List[str]:
        return [prefix_path(self.name, d) for d in self.repo_src_dirs]

    def prefixed_globs(self, globs: List[str]) -> List[str]:
        """Prefix root-anchored gitwildmatch globs with ``<name>/``; leave ``**/``-anchored ones alone."""
        return [g if g.startswith("**/") or g.startswith("/") else f"{self.name}/{g}" for g in globs]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "owner": self.owner,
            "repo": self.repo,
            "role": self.role,
            "start": self.start,
            "end": self.end,
            "since": self.since,
            "until": self.until,
            "main_branch": self.main_branch,
            "repo_src_dirs": list(self.repo_src_dirs),
            "test_dirs": list(self.test_dirs),
            "exclude": list(self.exclude),
            "contract_paths": list(self.contract_paths),
            "contract_only": self.contract_only,
            "base_image_name": self.base_image_name,
            "build_command": self.build_command,
        }


@dataclass
class XRepoConfig:
    pin_files: List[str] = field(default_factory=lambda: list(DEFAULT_PIN_FILES))
    link_confidence_threshold: float = 0.7
    time_window_days: float = 3.0
    pin_lag_days: float = 90.0
    generated_globs: List[str] = field(default_factory=lambda: list(DEFAULT_GENERATED_GLOBS))
    # soft edges are recorded but never enforced by path completeness; disable to skip the heuristic entirely
    emit_soft_edges: bool = True


@dataclass
class Workspace:
    workspace_id: str
    repos: List[RepoSpec]
    xrepo: XRepoConfig = field(default_factory=XRepoConfig)

    @property
    def is_multi(self) -> bool:
        return len(self.repos) > 1

    def get(self, name: str) -> RepoSpec:
        for r in self.repos:
            if r.name == name:
                return r
        raise KeyError(f"repo {name!r} not in workspace {self.workspace_id!r}")

    @property
    def names(self) -> List[str]:
        return [r.name for r in self.repos]

    def prefixed_src_dirs(self) -> List[str]:
        return [d for r in self.repos for d in r.prefixed_src_dirs()]

    def prefixed_test_dirs(self) -> List[str]:
        return [g for r in self.repos for g in r.prefixed_globs(r.test_dirs)]

    def prefixed_exclude(self) -> List[str]:
        return [g for r in self.repos for g in r.prefixed_globs(r.exclude)]

    def split_path(self, path: str) -> Tuple[str, str]:
        return split_path(path, self.names)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "repos": [r.to_dict() for r in self.repos],
            "xrepo": {
                "pin_files": list(self.xrepo.pin_files),
                "link_confidence_threshold": self.xrepo.link_confidence_threshold,
                "time_window_days": self.xrepo.time_window_days,
                "pin_lag_days": self.xrepo.pin_lag_days,
                "generated_globs": list(self.xrepo.generated_globs),
                "emit_soft_edges": self.xrepo.emit_soft_edges,
            },
        }


def prefix_path(name: str, path: str) -> str:
    path = path.lstrip("/")
    return f"{name}/{path}" if path else f"{name}/"


def split_path(path: str, names: List[str]) -> Tuple[str, str]:
    """Split ``<name>/<rest>`` into ``(name, rest)``. Longest matching repo name wins."""
    for n in sorted(names, key=len, reverse=True):
        if path == n:
            return n, ""
        if path.startswith(n + "/"):
            return n, path[len(n) + 1 :]
    raise KeyError(f"path {path!r} is not under any workspace repo ({', '.join(names)})")


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def parse_workspace(data: Dict[str, Any]) -> Workspace:
    if not isinstance(data, dict):
        raise WorkspaceError("workspace manifest must be a mapping")
    wid = str(data.get("workspace_id", "")).strip()
    if not wid or not _NAME_RE.match(wid):
        raise WorkspaceError("workspace_id is required and must be filesystem-safe")
    raw_repos = data.get("repos")
    if not isinstance(raw_repos, list) or not raw_repos:
        raise WorkspaceError("workspace needs a non-empty 'repos' list")
    if len(raw_repos) > MAX_REPOS:
        raise WorkspaceError(f"a workspace supports at most {MAX_REPOS} repos, got {len(raw_repos)}")

    repos: List[RepoSpec] = []
    seen_names: set = set()
    seen_ids: set = set()
    for i, r in enumerate(raw_repos):
        if not isinstance(r, dict):
            raise WorkspaceError(f"repos[{i}] must be a mapping")
        owner, repo = r.get("owner"), r.get("repo")
        if not owner or not repo:
            raise WorkspaceError(f"repos[{i}] needs 'owner' and 'repo'")
        name = str(r.get("name") or repo)
        if not _NAME_RE.match(name):
            raise WorkspaceError(f"repos[{i}].name {name!r} must be filesystem-safe")
        if name in seen_names:
            raise WorkspaceError(f"duplicate repo name {name!r}")
        # a name that is a path prefix of another would make split_path ambiguous only if '/' were allowed
        seen_names.add(name)
        role = str(r.get("role", "library"))
        if role not in VALID_ROLES:
            raise WorkspaceError(f"repos[{i}].role {role!r} must be one of {VALID_ROLES}")
        start, end = str(r.get("start", "") or ""), str(r.get("end", "") or "")
        since, until = str(r.get("since", "") or ""), str(r.get("until", "") or "")
        if not ((start and end) or (since and until)):
            raise WorkspaceError(f"repo {name!r} needs start+end tags or since+until timestamps")
        spec = RepoSpec(
            name=name,
            owner=str(owner),
            repo=str(repo),
            role=role,
            start=start,
            end=end,
            since=since,
            until=until,
            main_branch=str(r.get("main_branch", "main")),
            repo_src_dirs=_as_list(r.get("repo_src_dirs")),
            test_dirs=_as_list(r.get("test_dirs")),
            exclude=_as_list(r.get("exclude")),
            contract_paths=_as_list(r.get("contract_paths")),
            contract_only=bool(r.get("contract_only", False)),
            base_image_name=r.get("base_image_name"),
            build_command=r.get("build_command"),
        )
        if not spec.repo_src_dirs:
            raise WorkspaceError(f"repo {name!r} needs repo_src_dirs (literal path prefixes, not globs)")
        if any(any(c in d for c in "*?[") for d in spec.repo_src_dirs):
            raise WorkspaceError(f"repo {name!r}: repo_src_dirs are literal prefixes, globs are not allowed")
        if spec.role == "contract" and not spec.contract_paths:
            raise WorkspaceError(f"contract repo {name!r} needs contract_paths")
        if spec.contract_only and (spec.role != "contract" or not spec.contract_paths):
            raise WorkspaceError(f"repo {name!r}: contract_only needs role: contract and non-empty contract_paths")
        if spec.repo_id in seen_ids and (start or since):
            # same repo twice is only valid with different ranges; keep it simple and forbid
            raise WorkspaceError(f"repo {spec.full_name} listed twice")
        seen_ids.add(spec.repo_id)
        repos.append(spec)

    x = data.get("xrepo") or {}
    cfg = XRepoConfig()
    if "pin_files" in x:
        cfg.pin_files = _as_list(x["pin_files"])
    if "generated_globs" in x:
        cfg.generated_globs = _as_list(x["generated_globs"])
    for key in ("link_confidence_threshold", "time_window_days", "pin_lag_days"):
        if key in x:
            setattr(cfg, key, float(x[key]))
    if "emit_soft_edges" in x:
        cfg.emit_soft_edges = bool(x["emit_soft_edges"])
    if not 0.0 <= cfg.link_confidence_threshold <= 1.0:
        raise WorkspaceError("xrepo.link_confidence_threshold must be within [0, 1]")
    return Workspace(workspace_id=wid, repos=repos, xrepo=cfg)


def load_workspace(path: Path | str) -> Workspace:
    with open(path, encoding="utf-8") as f:
        return parse_workspace(yaml.safe_load(f))


def single_repo_workspace(
    owner: str,
    repo: str,
    start: str,
    end: str,
    repo_src_dirs: List[str],
    test_dirs: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    main_branch: str = "main",
) -> Workspace:
    """Wrap a classic single-repo run as an N=1 workspace (no behaviour change)."""
    spec = RepoSpec(
        name=repo,
        owner=owner,
        repo=repo,
        start=start,
        end=end,
        main_branch=main_branch,
        repo_src_dirs=list(repo_src_dirs),
        test_dirs=list(test_dirs or []),
        exclude=list(exclude or []),
    )
    return Workspace(workspace_id=f"{owner}_{repo}_{start}_{end}", repos=[spec])
