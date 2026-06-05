"""apply_ops — canonical simulator for a dag_update event's ``final_ops``.

Applies an event's declared ``final_ops`` (+ M1's ``applied_refinements`` edge
ops) to the event's pre-edit state and returns the resulting edge set + commit
ownership. ``ops_equivalence`` then compares that *simulated* after-state to the
*actual* recorded CSVs, so verify-dag can reject a ledger whose declared ops
diverge from what the CSVs really became — a missing/wrong edge, a reassign to
the wrong milestone, a split that produced the wrong new id: defects the plain
structural + conservation checks let through.

Scope: edge ops (add/drop/reverse) and the commit/milestone-id effects of
reassign/drop_commit/split/merge/drop_milestone are simulated exactly. A split's
free-text ``edge_rewire`` notes are NOT parsed — when the host keeps its id
(``new_ids[0] == milestone_id``, the common case) its edges are unchanged and
real rewiring is carried by explicit add_edge/drop_edge ops; when the host is
renamed, its edges are remapped to ``new_ids[0]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from deepcommit.harness.main_orch.invariants import Edge, match_sha


@dataclass
class AppliedState:
    """The simulated after-state: an edge set + ``milestone_id -> [sha]`` map."""

    edges: set[Edge]
    commits: dict[str, list[str]]


@dataclass
class EquivalenceResult:
    edges_only_in_declared: list[Edge]
    edges_only_in_actual: list[Edge]
    ownership_mismatches: dict[str, dict[str, list[str]]]
    milestones_only_in_declared: list[str]
    milestones_only_in_actual: list[str]

    @property
    def ok(self) -> bool:
        return not (
            self.edges_only_in_declared
            or self.edges_only_in_actual
            or self.ownership_mismatches
            or self.milestones_only_in_declared
            or self.milestones_only_in_actual
        )


def _edge(op: Mapping[str, Any]) -> Edge:
    return (str(op.get("prerequisite_id")), str(op.get("dependant_id")))


def _remap(node: str, old: str, new: str) -> str:
    return new if node == old else node


def _find_owner(
    commits: Mapping[str, Sequence[str]], sha: str, prefer: str | None = None
) -> tuple[str | None, str | None]:
    """Return ``(milestone_id, full_sha)`` of the milestone owning ``sha``.

    Looks in ``prefer`` (the op's declared ``from``) first, else searches all
    milestones. ``match_sha`` makes a short op SHA resolve to the full stored one.
    """
    order = [prefer] if prefer in commits else []
    order += [m for m in commits if m != prefer]
    for mid in order:
        hit = match_sha(sha, commits[mid])
        if hit:
            return mid, hit[0]
    return None, None


def _apply_edge_ops(edges: set[Edge], ops: Mapping[str, Any]) -> None:
    for op in ops.get("drop_edge") or []:
        edges.discard(_edge(op))
    for op in ops.get("reverse_edge") or []:
        prereq, dependant = _edge(op)
        edges.discard((prereq, dependant))
        edges.add((dependant, prereq))
    for op in ops.get("add_edge") or []:
        edges.add(_edge(op))


def apply_ops(
    before_edges: Iterable[Edge],
    before_commits: Mapping[str, Sequence[str]],
    final_ops: Mapping[str, Any] | None,
    applied_refinements: Mapping[str, Any] | None = None,
) -> AppliedState:
    edges: set[Edge] = {(str(p), str(d)) for p, d in before_edges}
    commits: dict[str, list[str]] = {m: list(s) for m, s in before_commits.items()}
    ops = final_ops or {}

    # 1. split_milestone — moves commits; renames the host only if new_ids[0] != id
    for op in ops.get("split_milestone") or []:
        mid = op["milestone_id"]
        new_a, new_b = op["new_ids"][0], op["new_ids"][1]
        src = commits.get(mid, [])
        if op.get("extracted_commits") is not None:
            extracted = [hit[0] for sha in op["extracted_commits"] if (hit := match_sha(str(sha), src))]
            remaining = [c for c in src if c not in extracted]
            b_commits = extracted
        else:
            n = int(op["at_index"])
            remaining, b_commits = src[:n], src[n:]
        commits.pop(mid, None)
        commits[new_a] = remaining
        commits[new_b] = b_commits
        if new_a != mid:  # host renamed → carry its edges to the new id
            edges = {(_remap(p, mid, new_a), _remap(d, mid, new_a)) for p, d in edges}

    # 2. merge_milestone — combine commits into new_id, remap touching edges
    for op in ops.get("merge_milestone") or []:
        ids = list(op["merge_ids"])
        new_id = op["new_id"]
        merged: list[str] = []
        for mid in ids:
            merged.extend(commits.pop(mid, []))
        commits[new_id] = commits.get(new_id, []) + merged
        remap = {mid: new_id for mid in ids}
        edges = {
            (remap.get(p, p), remap.get(d, d))
            for p, d in edges
            if remap.get(p, p) != remap.get(d, d)  # drop self-loops the merge creates
        }

    # 3. drop_milestone — remove the node and every edge touching it
    for op in ops.get("drop_milestone") or []:
        mid = op["milestone_id"]
        commits.pop(mid, None)
        edges = {(p, d) for p, d in edges if mid not in (p, d)}

    # 4. reassign_commit — move one sha from its owner to `to`
    for op in ops.get("reassign_commit") or []:
        owner, full = _find_owner(commits, str(op["commit_sha"]), op.get("from"))
        if full is not None:
            commits[owner] = [c for c in commits[owner] if c != full]
            commits.setdefault(op["to"], []).append(full)

    # 5. drop_commit — remove one sha from its owner
    for op in ops.get("drop_commit") or []:
        owner, full = _find_owner(commits, str(op["commit_sha"]), op.get("from"))
        if full is not None:
            commits[owner] = [c for c in commits[owner] if c != full]

    # 6. edge ops — M3 plan first, then M1's authorized refinements
    _apply_edge_ops(edges, ops)
    if applied_refinements:
        _apply_edge_ops(edges, applied_refinements)

    return AppliedState(edges=edges, commits=commits)


def apply_plan(
    baseline_edges: Iterable[Edge],
    baseline_commits: Mapping[str, Sequence[str]],
    events: Iterable[Mapping[str, Any]],
    restore_loader: Callable[[str], tuple[Iterable[Edge], Mapping[str, Sequence[str]]]],
    through_seq: int | None = None,
) -> AppliedState:
    """Fold a round's ``dag_update`` events onto a baseline (round-start) state.

    Events are processed in order. Non-``dag_update`` events are skipped (only DAG
    edits change the CSVs). A ``revert`` event resets the running state to its
    declared ``restore_from`` checkpoint — loaded via ``restore_loader`` (a run-id
    -> ``(edges, commits)`` resolver) — and applies no ops, so the final state is
    determined by the LAST revert's restore point plus the events after it. With
    ``through_seq`` set, only events up to and including that seq are folded.
    """
    state = AppliedState(
        {(str(p), str(d)) for p, d in baseline_edges},
        {m: list(s) for m, s in baseline_commits.items()},
    )
    for event in events or []:
        if not isinstance(event, dict) or event.get("event") != "dag_update":
            continue
        seq = event.get("seq")
        if through_seq is not None and isinstance(seq, int) and seq > through_seq:
            continue
        revert = event.get("revert")
        if isinstance(revert, dict) and revert.get("restore_from"):
            edges, commits = restore_loader(str(revert["restore_from"]))
            state = AppliedState(
                {(str(p), str(d)) for p, d in edges},
                {m: list(s) for m, s in commits.items()},
            )
            continue
        m1 = event.get("m1_result") or {}
        refinements = m1.get("applied_refinements") if isinstance(m1, dict) else None
        state = apply_ops(state.edges, state.commits, event.get("final_ops") or {}, refinements)
    return state


def _commit_diff(a: set[str], b: set[str]) -> tuple[list[str], list[str]]:
    """Prefix-tolerant set difference (so a short/long sha pair isn't a false diff)."""
    a_only = sorted(s for s in a if not match_sha(s, b))
    b_only = sorted(s for s in b if not match_sha(s, a))
    return a_only, b_only


def ops_equivalence(
    simulated: AppliedState,
    actual_edges: Iterable[Edge],
    actual_commits: Mapping[str, Sequence[str]],
) -> EquivalenceResult:
    """Compare the simulated after-state to the actual recorded CSVs."""
    sim_edges = set(simulated.edges)
    act_edges = {(str(p), str(d)) for p, d in actual_edges}

    sim_ms, act_ms = set(simulated.commits), set(actual_commits)
    ownership: dict[str, dict[str, list[str]]] = {}
    for mid in sim_ms & act_ms:
        declared_only, actual_only = _commit_diff(set(simulated.commits[mid]), set(actual_commits[mid]))
        if declared_only or actual_only:
            ownership[mid] = {"declared_only": declared_only, "actual_only": actual_only}

    return EquivalenceResult(
        edges_only_in_declared=sorted(sim_edges - act_edges),
        edges_only_in_actual=sorted(act_edges - sim_edges),
        ownership_mismatches=ownership,
        milestones_only_in_declared=sorted(sim_ms - act_ms),
        milestones_only_in_actual=sorted(act_ms - sim_ms),
    )
