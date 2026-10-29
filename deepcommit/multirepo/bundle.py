"""Bundle single-repo milestones into cross-repo milestones using hard cross-repo edges.

The Phase 2 LLM stages tend to group commits inside one repo even when HARD cross-repo edges tie them to another repo
(observed on the qdrant pilot: 38 seeds, 32 draft milestones, none spanning repos). This deterministic pass runs on the
stage-2 milestone table and merges the milestones that belong together.

Rule: two milestones are merged only when each of them sends most of its cross-repo evidence to the other
("mutual share"). A release-sync commit that consumes 14 unrelated server features has a diluted share toward each of
them and is left alone, so one batched commit cannot glue the whole workspace into a single blob. Merges are capped by
commit count and by number of repos. Leftover cross-repo links are reported so stage 3 can record them as XREPO
dependencies instead of merges.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

SUM_COLS = (
    "additions",
    "deletions",
    "src_additions",
    "src_deletions",
    "src_loc",
    "loc",
)
UNION_COLS = ("touched_test_files", "touched_src_files")


@dataclass
class BundleParams:
    min_mutual_share: float = 0.4
    max_commits: int = 40
    max_repos: int = 3
    min_edge_confidence: float = 0.7  # only edges at or above this count as evidence


@dataclass
class BundleReport:
    before: int = 0
    after: int = 0
    merges: List[Dict[str, Any]] = field(default_factory=list)
    leftover_links: List[Dict[str, Any]] = field(default_factory=list)  # milestone pairs still linked, not merged
    multi_repo_milestones: List[str] = field(default_factory=list)


def _commits(row: Dict[str, str]) -> List[str]:
    return [c.strip() for c in (row.get("commits") or "").split(";") if c.strip()]


def _key(sha: str) -> str:
    return sha[:7]


def _link_weights(
    owner: Dict[str, str],
    hard_edges: Sequence[Dict[str, Any]],
    min_conf: float,
    commit_repo: Dict[str, str],
) -> Dict[Tuple[str, str], float]:
    """Undirected milestone-pair weight from cross-repo hard edges."""
    w: Dict[Tuple[str, str], float] = defaultdict(float)
    for e in hard_edges:
        if not e.get("xrepo") or float(e.get("confidence", 0) or 0) < min_conf:
            continue
        if commit_repo.get(_key(e["source"])) == commit_repo.get(_key(e["target"])):
            continue
        a, b = owner.get(_key(e["source"])), owner.get(_key(e["target"]))
        if a is None or b is None or a == b:
            continue
        w[tuple(sorted((a, b)))] += float(e.get("confidence", 1.0))
    return dict(w)


def bundle_milestones(
    rows: List[Dict[str, str]],
    hard_edges: Sequence[Dict[str, Any]],
    commit_repo: Dict[str, str],
    params: Optional[BundleParams] = None,
) -> Tuple[List[Dict[str, str]], BundleReport]:
    p = params or BundleParams()
    rep = BundleReport(before=len(rows))
    by_id: Dict[str, Dict[str, str]] = {r["id"]: dict(r) for r in rows}
    members: Dict[str, List[str]] = {r["id"]: [r["id"]] for r in rows}  # merged id -> original ids
    owner: Dict[str, str] = {_key(c): r["id"] for r in rows for c in _commits(r)}

    def repos_of(mid: str) -> Set[str]:
        return {commit_repo.get(_key(c), "") for c in _commits(by_id[mid])} - {""}

    weights = _link_weights(owner, hard_edges, p.min_edge_confidence, commit_repo)
    total: Dict[str, float] = defaultdict(float)
    for (a, b), w in weights.items():
        total[a] += w
        total[b] += w

    scored = []
    for (a, b), w in weights.items():
        score = min(w / total[a], w / total[b])
        if score >= p.min_mutual_share:
            scored.append((score, w, a, b))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2], t[3]))

    merged_into: Dict[str, str] = {}

    def find(x: str) -> str:
        while x in merged_into:
            x = merged_into[x]
        return x

    for score, w, a, b in scored:
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        commits = _commits(by_id[ra]) + _commits(by_id[rb])
        if len(commits) > p.max_commits:
            continue
        if len(repos_of(ra) | repos_of(rb)) > p.max_repos:
            continue
        keep, drop = sorted((ra, rb))
        by_id[keep] = _merge_rows(by_id[keep], by_id[drop], commit_repo)
        members[keep] += members.pop(drop)
        del by_id[drop]
        merged_into[drop] = keep
        rep.merges.append({"into": keep, "from": drop, "mutual_share": round(score, 3), "weight": round(w, 2)})

    out = [by_id[r["id"]] for r in rows if r["id"] in by_id]
    for r in out:
        repos = sorted(repos_of(r["id"]))
        r["repos"] = ";".join(repos)
        if len(repos) > 1:
            rep.multi_repo_milestones.append(r["id"])
    # links that survive between different final milestones
    final_owner = {_key(c): r["id"] for r in out for c in _commits(r)}
    left = _link_weights(final_owner, hard_edges, p.min_edge_confidence, commit_repo)
    rep.leftover_links = [{"a": a, "b": b, "weight": round(w, 2)} for (a, b), w in sorted(left.items(), key=lambda kv: -kv[1])]
    rep.after = len(out)
    return out, rep


def _merge_rows(a: Dict[str, str], b: Dict[str, str], commit_repo: Dict[str, str]) -> Dict[str, str]:
    out = dict(a)
    commits = list(dict.fromkeys(_commits(a) + _commits(b)))
    out["commits"] = ";".join(commits)
    repo_of = lambda r: sorted({commit_repo.get(_key(c), "") for c in _commits(r)} - {""})  # noqa: E731
    ra, rb = repo_of(a), repo_of(b)
    out["title"] = f"{a.get('title', '')} + {b.get('title', '')} (cross-repo)"
    out["mini_srs"] = (
        f"[{'/'.join(ra)}] {a.get('mini_srs', '')}\n[{'/'.join(rb)}] {b.get('mini_srs', '')}".strip()
    )
    for col in SUM_COLS:
        if col in a or col in b:
            out[col] = str(_num(a.get(col)) + _num(b.get(col)))
    for col in UNION_COLS:
        if col in a or col in b:
            out[col] = ";".join(dict.fromkeys([x for x in (a.get(col, "") + ";" + b.get(col, "")).split(";") if x]))
    for col, fn in (("start_time", min), ("end_time", max)):
        vals = [v for v in (a.get(col), b.get(col)) if v]
        if vals:
            out[col] = fn(vals)
    conf = [_num(v) for v in (a.get("confidence_score"), b.get("confidence_score")) if v not in (None, "")]
    if conf:
        out["confidence_score"] = str(min(conf))
    out["category"] = a.get("category", "") or b.get("category", "")
    return out


def _num(v: Optional[str]) -> float:
    try:
        f = float(v) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0
    return int(f) if f == int(f) else f
