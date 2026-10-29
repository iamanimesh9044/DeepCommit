"""Cross-repo milestone gates (pure logic).

Workspace milestones must (1) change at least two repos and (2) not be solvable by one repo's patch alone
("single-repo shortcut"). Both checks are pure functions over already-collected evidence so they can be unit-tested
without Docker; ``compute_xrepo_gates`` reads that evidence from a harness workspace for ``frozen_check_gate``.

Evidence layout (workspace root, i.e. the dir holding ``milestones.csv``):
  milestone_patches/{MID}/{repo}.patch            gold patch per repo (non-empty => repo changed)
  xrepo_gate/{MID}.json                           {"per_repo_results": {repo: {test_id: outcome}}, "f2p_ids": [...]}
      per_repo_results[repo] = results of running the milestone tests on the start state with ONLY that repo's gold
      patch applied (ids namespaced ``{repo}::{native id}``). ``f2p_ids`` defaults to the milestone's f2p list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PASS_OUTCOMES = frozenset({"passed", "pass", "ok"})


def _passed_ids(results: Any) -> set[str]:
    if isinstance(results, Mapping):
        return {k for k, v in results.items() if str(v).lower() in PASS_OUTCOMES}
    return {str(x) for x in results}  # an iterable of passed ids


def single_repo_shortcut_check(f2p_ids: Iterable[str], per_repo_results: Mapping[str, Any]) -> dict[str, Any]:
    """Each repo's patch alone must NOT make all F2P tests pass.

    ``per_repo_results[repo]`` is ``{test_id: outcome}`` (or an iterable of passing ids) measured with only that repo's
    gold patch applied. ``passed`` is True when no single repo's patch turns every F2P test green.
    """
    f2p = sorted({str(i) for i in f2p_ids})
    if not f2p:
        return {"passed": False, "reason": "no_f2p_ids", "shortcut_repos": [], "per_repo": {}}
    per_repo: dict[str, Any] = {}
    shortcut: list[str] = []
    for repo, results in per_repo_results.items():
        ok = _passed_ids(results)
        green = [t for t in f2p if t in ok]
        per_repo[repo] = {
            "f2p_passed": len(green),
            "f2p_total": len(f2p),
            "still_failing": [t for t in f2p if t not in ok],
        }
        if len(green) == len(f2p):
            shortcut.append(repo)
    return {
        "passed": not shortcut,
        "reason": "single_repo_shortcut" if shortcut else "ok",
        "shortcut_repos": sorted(shortcut),
        "per_repo": per_repo,
    }


def cross_repo_validity(milestone_repos: Sequence[str], patched_repos: Iterable[str]) -> dict[str, Any]:
    """A cross-repo milestone must declare >=2 repos and its gold patch must be non-empty in >=2 of them."""
    declared = sorted({r for r in milestone_repos if r})
    patched = sorted({r for r in patched_repos if r})
    unknown = [r for r in patched if r not in declared]
    reasons = []
    if len(declared) < 2:
        reasons.append("fewer_than_2_declared_repos")
    if len(patched) < 2:
        reasons.append("fewer_than_2_repos_patched")
    if unknown:
        reasons.append("patched_repo_not_declared")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "declared_repos": declared,
        "patched_repos": patched,
        "undeclared_patched_repos": unknown,
    }


def _split_repos(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [str(r).strip() for r in raw if str(r).strip()]
    return [r.strip() for r in str(raw or "").replace(",", ";").split(";") if r.strip()]


def patched_repos_from_dir(workspace: Path, milestone_id: str) -> list[str]:
    d = workspace / "milestone_patches" / milestone_id
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.patch") if p.stat().st_size > 0)


def is_workspace_milestone(row: Mapping[str, Any]) -> bool:
    """Cross-repo milestone: the ``repos`` column names >=2 repos (absent/1 repo => no X_* gates, so N=1 is untouched)."""
    return len(_split_repos(row.get("repos"))) >= 2


def compute_xrepo_gates(
    workspace: Path, milestones: Mapping[str, Mapping[str, Any]], candidate: Sequence[str]
) -> dict[str, Any]:
    """Gate dicts (frozen_check shape) for workspace milestones in ``candidate``; ``{}`` when there are none.

    Milestones declaring fewer than 2 repos are skipped, so N=1 runs add nothing.
    """
    ws_mids = [m for m in candidate if is_workspace_milestone(milestones[m])]
    if not ws_mids:
        return {}
    validity: dict[str, Any] = {}
    shortcut: dict[str, Any] = {}
    for mid in ws_mids:
        validity[mid] = cross_repo_validity(
            _split_repos(milestones[mid].get("repos")), patched_repos_from_dir(workspace, mid)
        )
        ev_path = workspace / "xrepo_gate" / f"{mid}.json"
        if not ev_path.exists():
            shortcut[mid] = {"passed": False, "reason": "missing_evidence", "path": str(ev_path)}
            continue
        ev = json.loads(ev_path.read_text(encoding="utf-8"))
        shortcut[mid] = single_repo_shortcut_check(ev.get("f2p_ids", []), ev.get("per_repo_results", {}))
    n = len(ws_mids)
    out: dict[str, Any] = {}
    for name, per in (("X_cross_repo_validity", validity), ("X_single_repo_shortcut", shortcut)):
        failing = sorted(m for m, r in per.items() if not r["passed"])
        out[name] = {
            "value": round((n - len(failing)) / n, 4),
            "threshold": 1.0,
            "passed": not failing,
            "direction": ">=",
            "evidence": {"workspace_milestones": ws_mids, "failing_milestones": failing, "per_milestone": per},
        }
    return out
