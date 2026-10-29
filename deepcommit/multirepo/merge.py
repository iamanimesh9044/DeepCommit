"""Merge per-repo Phase 1 output into one workspace-level ``proc_data``.

Each member repo is preprocessed by the unchanged single-repo pipeline. This step joins the results so Phase 2
(stages 1-4) can read one directory exactly as it reads a single-repo ``proc_data``:

    github_data/workspaces/<workspace_id>/
        workspace.json          manifest as parsed
        repos.json              name -> checkout path, preprocessed dir, prefixed src/test/exclude
        commit_repos.json       {sha7: repo name}   (the commit -> repo resolver)
        merged/
            main_commits.jsonl  + repo, committed_at, merged_via_pr_key; paths prefixed '<repo>/'
            commit_dag.json     per-repo edges + hard cross-repo edges, stats/layers recomputed
            xrepo_edges.json    every cross-repo edge incl. soft ones, with evidence
            pull_requests.jsonl / issues.jsonl / pr_conversations.jsonl / releases.jsonl   (+ repo field)
            codemaat_analysis.json  entities prefixed with '<repo>/'
            patches/<sha>.patch
            symbol/             concatenated symbol files (+ repo field)

Commit keys stay the bare 7-char SHA. Two repos can in principle collide on 7 hex chars (about 0.2% for 1000 commits);
the merge detects that and raises ``CommitKeyCollision`` instead of silently mixing commits.
"""

from __future__ import annotations

import json
import os
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .workspace import RepoSpec, Workspace, prefix_path
from .xrepo import HARD, SOFT, CommitRec, PRRec, XEdge, build_xrepo_edges

SHA_LEN = 7


class CommitKeyCollision(RuntimeError):
    """Two repos in a workspace share a 7-char commit prefix."""


class MissingPreprocessed(FileNotFoundError):
    pass


@dataclass
class MergeResult:
    out_dir: Path
    merged_dir: Path
    commits_per_repo: Dict[str, int] = field(default_factory=dict)
    intra_edges: int = 0
    xrepo_hard: int = 0
    xrepo_soft: int = 0
    xrepo_dropped_cycle: int = 0
    notes: List[str] = field(default_factory=list)


# ------------------------------------------------------------------ io helpers


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def parse_time(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def workspace_dir(data_root: Path, workspace_id: str) -> Path:
    return data_root / "github_data" / "workspaces" / workspace_id


def repo_checkout_path(data_root: Path, spec: RepoSpec) -> Path:
    return data_root / "github_data" / "repos" / spec.repo_id


def preprocessed_path(data_root: Path, spec: RepoSpec) -> Path:
    return data_root / "github_data" / "preprocessed" / spec.preprocessed_dirname


# ------------------------------------------------------------------ DAG math


def compute_dag_stats(
    nodes: Iterable[str], edges: Iterable[Tuple[str, str]]
) -> Tuple[Dict[str, Dict[str, int]], Dict[int, List[str]], List[str]]:
    """Kahn layering + transitive ancestor/descendant counts.

    Returns ``(stats, layers, topo_order)``. Raises ``ValueError`` if the graph has a cycle.
    """
    order_nodes = list(dict.fromkeys(nodes))
    idx = {n: i for i, n in enumerate(order_nodes)}
    succ: Dict[str, Set[str]] = {n: set() for n in order_nodes}
    pred: Dict[str, Set[str]] = {n: set() for n in order_nodes}
    for s, t in edges:
        if s in idx and t in idx and s != t:
            succ[s].add(t)
            pred[t].add(s)
    indeg = {n: len(pred[n]) for n in order_nodes}
    ready = sorted(n for n in order_nodes if indeg[n] == 0)
    topo: List[str] = []
    layer: Dict[str, int] = {}
    while ready:
        n = ready.pop(0)
        topo.append(n)
        layer[n] = 1 + max((layer[p] for p in pred[n]), default=-1)
        for m in sorted(succ[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
        ready.sort()
    if len(topo) != len(order_nodes):
        raise ValueError("cycle detected while computing DAG stats")
    anc: Dict[str, int] = {}
    for n in topo:
        bits = 0
        for p in pred[n]:
            bits |= anc[p] | (1 << idx[p])
        anc[n] = bits
    desc: Dict[str, int] = {}
    for n in reversed(topo):
        bits = 0
        for m in succ[n]:
            bits |= desc[m] | (1 << idx[m])
        desc[n] = bits
    stats = {
        n: {
            "layer": layer[n],
            "in_degree": len(pred[n]),
            "out_degree": len(succ[n]),
            "ancestors_count": bin(anc[n]).count("1"),
            "descendants_count": bin(desc[n]).count("1"),
        }
        for n in order_nodes
    }
    layers: Dict[int, List[str]] = defaultdict(list)
    for n in topo:
        layers[layer[n]].append(n)
    return stats, {k: sorted(v) for k, v in sorted(layers.items())}, topo


def _reachable(start: str, goal: str, succ: Dict[str, Set[str]]) -> bool:
    seen, stack = {start}, [start]
    while stack:
        n = stack.pop()
        if n == goal:
            return True
        for m in succ.get(n, ()):
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return False


# ------------------------------------------------------------------ per-repo loading


@dataclass
class _RepoData:
    spec: RepoSpec
    pre_dir: Path
    commits: List[Dict[str, Any]]
    raw: Dict[str, Dict[str, Any]]
    dag: Dict[str, Any]
    prs: List[Dict[str, Any]]
    issues: List[Dict[str, Any]]
    convs: List[Dict[str, Any]]
    releases: List[Dict[str, Any]]
    codemaat: Dict[str, Any]


def _load_repo(data_root: Path, spec: RepoSpec) -> _RepoData:
    pre = preprocessed_path(data_root, spec)
    if not (pre / "main_commits.jsonl").exists() or not (pre / "commit_dag.json").exists():
        raise MissingPreprocessed(
            f"repo {spec.name!r}: Phase 1 output missing in {pre} (need main_commits.jsonl and commit_dag.json). "
            f"Run preprocessing for {spec.repo_id}:{spec.time_span} first."
        )
    raw_rows = _read_jsonl(pre / "raw_data" / "main_commits.jsonl")
    codemaat: Dict[str, Any] = {}
    cm = pre / "codemaat_analysis.json"
    if cm.exists():
        codemaat = json.loads(cm.read_text(encoding="utf-8"))
    return _RepoData(
        spec=spec,
        pre_dir=pre,
        commits=_read_jsonl(pre / "main_commits.jsonl"),
        raw={r["sha"][:SHA_LEN]: r for r in raw_rows if r.get("sha")},
        dag=json.loads((pre / "commit_dag.json").read_text(encoding="utf-8")),
        prs=_read_jsonl(pre / "pull_requests.jsonl"),
        issues=_read_jsonl(pre / "issues.jsonl"),
        convs=_read_jsonl(pre / "pr_conversations.jsonl"),
        releases=_read_jsonl(pre / "releases.jsonl"),
        codemaat=codemaat,
    )


def _patch_loader(pre_dir: Path, sha: str):
    path = pre_dir / "patches" / f"{sha}.patch"

    def load() -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    return load


def _file_path(entry: Dict[str, Any]) -> str:
    return entry.get("path") or entry.get("filename") or ""


def _merge_codemaat(per_repo: List[_RepoData]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"hotspots": {}, "coupling": {}, "entity_churn": {}, "temporal_churn": {"all": []}}

    def pref(d: _RepoData, e: str) -> str:
        return prefix_path(d.spec.name, e)

    for key in ("top_20", "top_50", "all"):
        hs: List[Dict[str, Any]] = []
        ec: List[Dict[str, Any]] = []
        for d in per_repo:
            for row in (d.codemaat.get("hotspots") or {}).get(key, []):
                hs.append({**row, "entity": pref(d, row["entity"]), "repo": d.spec.name})
            for row in (d.codemaat.get("entity_churn") or {}).get(key, []):
                ec.append({**row, "entity": pref(d, row["entity"]), "repo": d.spec.name})
        limit = {"top_20": 20, "top_50": 50}.get(key)
        hs.sort(key=lambda r: -int(r.get("n-revs", 0) or 0))
        ec.sort(key=lambda r: -(int(r.get("added", 0) or 0) + int(r.get("deleted", 0) or 0)))
        out["hotspots"][key] = hs[:limit] if limit else hs
        out["entity_churn"][key] = ec[:limit] if limit else ec
    cp: List[Dict[str, Any]] = []
    for d in per_repo:
        for row in (d.codemaat.get("coupling") or {}).get("threshold_40", []):
            cp.append({**row, "entity": pref(d, row["entity"]), "coupled": pref(d, row["coupled"]), "repo": d.spec.name})
    out["coupling"]["threshold_40"] = cp
    by_date: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    for d in per_repo:
        for row in (d.codemaat.get("temporal_churn") or {}).get("all", []):
            acc = by_date[row["date"]]
            acc[0] += int(row.get("added", 0) or 0)
            acc[1] += int(row.get("deleted", 0) or 0)
    out["temporal_churn"]["all"] = [{"date": k, "added": str(v[0]), "deleted": str(v[1])} for k, v in sorted(by_date.items())]
    out["_metadata"] = {"merged_from": [d.spec.name for d in per_repo]}
    return out


def _link_or_copy(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.symlink(src.resolve(), dst)
    except OSError:
        shutil.copy2(src, dst)


# ------------------------------------------------------------------ main entry


def merge_workspace(ws: Workspace, data_root: Path | str, out_dir: Optional[Path | str] = None) -> MergeResult:
    data_root = Path(data_root)
    out = Path(out_dir) if out_dir else workspace_dir(data_root, ws.workspace_id)
    merged = out / "merged"
    per_repo = [_load_repo(data_root, spec) for spec in ws.repos]

    # ---- commit -> repo map with collision check
    commit_repo: Dict[str, str] = {}
    for d in per_repo:
        for row in d.commits:
            sha = row["sha"][:SHA_LEN]
            if sha in commit_repo and commit_repo[sha] != d.spec.name:
                raise CommitKeyCollision(
                    f"7-char commit key {sha} exists in both {commit_repo[sha]!r} and {d.spec.name!r}; "
                    "a workspace needs globally unique 7-char SHAs"
                )
            commit_repo[sha] = d.spec.name

    # ---- patches
    (merged / "patches").mkdir(parents=True, exist_ok=True)
    for d in per_repo:
        pdir = d.pre_dir / "patches"
        if pdir.is_dir():
            for p in pdir.glob("*.patch"):
                _link_or_copy(p, merged / "patches" / p.name)

    # ---- commit records for xrepo detection
    recs: Dict[str, List[CommitRec]] = {}
    pr_recs: Dict[str, List[PRRec]] = {}
    rel_maps: Dict[str, Dict[str, str]] = {}
    for d in per_repo:
        lst: List[CommitRec] = []
        for row in d.commits:
            sha = row["sha"][:SHA_LEN]
            raw = d.raw.get(sha, {})
            author = raw.get("author") or {}
            committer = raw.get("committer") or {}
            lst.append(
                CommitRec(
                    sha=sha,
                    repo=d.spec.name,
                    message=row.get("message", ""),
                    full_sha=raw.get("sha", row["sha"]),
                    committed_at=parse_time(author.get("time") or committer.get("time")),
                    author_key=(author.get("email") or author.get("name") or "").lower(),
                    pr_number=row.get("merged_via_pr"),
                    files=[_file_path(f) for f in row.get("files_changed", [])],
                    patch=_patch_loader(d.pre_dir, sha),
                )
            )
        recs[d.spec.name] = lst
        pr_recs[d.spec.name] = [
            PRRec(
                repo=d.spec.name,
                number=int(p.get("number", 0)),
                title=p.get("title", "") or "",
                body=p.get("body", "") or "",
                merge_commit_sha=(p.get("merge_commit_sha") or "")[:SHA_LEN],
                commit_shas=_listish(p.get("pr_commits")),
                merged_at=parse_time(p.get("merged_at")),
            )
            for p in d.prs
            if str(p.get("number", "")).isdigit()
        ]
        rel_maps[d.spec.name] = {r["tag"]: r.get("commit_sha", "") for r in d.releases if r.get("tag")}

    xedges = build_xrepo_edges(ws, recs, pr_recs, rel_maps)

    # ---- merged DAG
    res = MergeResult(out_dir=out, merged_dir=merged, commits_per_repo={d.spec.name: len(d.commits) for d in per_repo})
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    succ: Dict[str, Set[str]] = defaultdict(set)
    for d in per_repo:
        for sha, info in d.dag.get("nodes", {}).items():
            nodes[sha[:SHA_LEN]] = {**{k: v for k, v in info.items()}, "repo": d.spec.name}
        for e in d.dag.get("edges_dag", []):
            s, t = e["source"][:SHA_LEN], e["target"][:SHA_LEN]
            ne = dict(e)
            ne.update(source=s, target=t, repo=d.spec.name)
            if "files_ref" in e:
                ne["files_ref"] = [prefix_path(d.spec.name, p) for p in e["files_ref"]]
            edges.append(ne)
            succ[s].add(t)
    res.intra_edges = len(edges)

    soft: List[XEdge] = []
    kept: List[XEdge] = []
    for e in sorted(xedges, key=lambda x: (x.edge_class != HARD, -x.confidence)):
        if e.source not in nodes or e.target not in nodes:
            continue
        if e.edge_class != HARD:
            soft.append(e)
            continue
        if _reachable(e.target, e.source, succ):
            res.xrepo_dropped_cycle += 1
            e.edge_class = SOFT
            e.evidence.append("downgraded to soft: hard edge would create a cycle")
            soft.append(e)
            continue
        succ[e.source].add(e.target)
        kept.append(e)
        edges.append(
            {
                "source": e.source,
                "target": e.target,
                "files_ref": [],
                "edge_types": sorted(set(e.edge_types)),
                "xrepo": True,
                "confidence": round(e.confidence, 3),
                "evidence": e.evidence[:4],
            }
        )
    res.xrepo_hard, res.xrepo_soft = len(kept), len(soft)

    stats, layers, topo = compute_dag_stats(nodes.keys(), [(e["source"], e["target"]) for e in edges])
    for sha, st in stats.items():
        nodes[sha].update(st)
    analysis_scope = [x for d in per_repo for x in d.spec.prefixed_src_dirs()]
    first_meta = per_repo[0].dag.get("meta", {})
    meta = {
        "layers_count": len(layers),
        "nodes_count": len(nodes),
        "edges_count": len(edges),
        "constraints": {"acyclic": True, "layer_increasing": True, "directed": True},
        "dag_info": {
            "description": (
                "Textual commit dependency DAG merged across workspace repos, plus cross-repo edges "
                "(edge_types xrepo_*). 'source -> target' means target depends on source. "
                "Execute commits following topo_order or layer by layer."
            ),
            "detection_fuzz": (first_meta.get("dag_info") or {}).get("detection_fuzz"),
            "analysis_scope": analysis_scope,
        },
        "workspace": {
            "workspace_id": ws.workspace_id,
            "repos": ws.names,
            "xrepo_hard_edges": len(kept),
            "xrepo_soft_edges": len(soft),
        },
    }
    _write_json(
        merged / "commit_dag.json",
        {
            "meta": meta,
            "nodes": nodes,
            "edges_dag": edges,
            "edges_soft": [e.to_dict() for e in soft],
            "layers": {str(k): v for k, v in layers.items()},
            "topo_order": topo,
        },
    )
    _write_json(
        merged / "xrepo_edges.json",
        {"hard": [e.to_dict() for e in kept], "soft": [e.to_dict() for e in soft]},
    )

    # ---- merged main_commits (with recomputed DAG stat)
    rows: List[Dict[str, Any]] = []
    rec_by_sha = {c.sha: c for cs in recs.values() for c in cs}
    for d in per_repo:
        for row in d.commits:
            sha = row["sha"][:SHA_LEN]
            nr = dict(row)
            nr["repo"] = d.spec.name
            rc = rec_by_sha[sha]
            nr["committed_at"] = rc.committed_at.isoformat() if rc.committed_at else None
            if row.get("merged_via_pr") is not None:
                nr["merged_via_pr_key"] = f"{d.spec.name}#{row['merged_via_pr']}"
            nr["files_changed"] = [
                {**f, **({"path": prefix_path(d.spec.name, _file_path(f))} if "path" in f else {"filename": prefix_path(d.spec.name, _file_path(f))})}
                for f in row.get("files_changed", [])
            ]
            if sha in stats:
                nr["commit_dag_stat"] = stats[sha]
            rows.append(nr)
    rows.sort(key=lambda r: (r.get("committed_at") or "", r["sha"]))
    _write_jsonl(merged / "main_commits.jsonl", rows)

    # ---- the rest, each row tagged with its repo
    for fname, attr in (
        ("pull_requests.jsonl", "prs"),
        ("issues.jsonl", "issues"),
        ("pr_conversations.jsonl", "convs"),
        ("releases.jsonl", "releases"),
    ):
        out_rows = []
        for d in per_repo:
            for r in getattr(d, attr):
                nr = {**r, "repo": d.spec.name}
                num = r.get("number", r.get("pr_number"))
                if num is not None and attr != "releases":
                    nr["pr_key" if attr != "issues" else "issue_key"] = f"{d.spec.name}#{num}"
                out_rows.append(nr)
        _write_jsonl(merged / fname, out_rows)
    sym_rows: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for d in per_repo:
        for f in (d.pre_dir / "symbol").glob("*.jsonl") if (d.pre_dir / "symbol").is_dir() else []:
            sym_rows[f.name].extend({**r, "repo": d.spec.name} for r in _read_jsonl(f))
    for name, rws in sym_rows.items():
        _write_jsonl(merged / "symbol" / name, rws)
    _write_json(merged / "codemaat_analysis.json", _merge_codemaat(per_repo))

    # ---- workspace-level metadata
    _write_json(out / "workspace.json", ws.to_dict())
    _write_json(out / "commit_repos.json", commit_repo)
    _write_json(
        out / "repos.json",
        {
            d.spec.name: {
                **d.spec.to_dict(),
                "repo_id": d.spec.repo_id,
                "checkout_path": str(repo_checkout_path(data_root, d.spec)),
                "preprocessed_dir": str(d.pre_dir),
                "prefixed_src_dirs": d.spec.prefixed_src_dirs(),
                "prefixed_test_dirs": d.spec.prefixed_globs(d.spec.test_dirs),
                "prefixed_exclude": d.spec.prefixed_globs(d.spec.exclude),
            }
            for d in per_repo
        },
    )
    return res


def _listish(v: Any) -> List[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str) and v.startswith("["):
        try:
            import ast

            return [str(x) for x in ast.literal_eval(v)]
        except (ValueError, SyntaxError):
            return []
    return []
