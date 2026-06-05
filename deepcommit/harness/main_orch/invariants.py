"""Shared invariant library for the m3 verb suite.

These primitives are **lifted** (re-implemented), not imported, from the
deprecated ``deepcommit/harness/_orchestrator/csv_ops.py`` — that module speaks
the old ``source_id``/``target_id`` vocabulary, has no split/merge, and writes
non-atomically. Here we keep the proven logic (cycle detection via
``_would_create_cycle``, ambiguity-safe ``_match_sha``) and generalize it to
full-graph acyclicity, reachability, DAG-closure, redundant-edge detection,
``seq`` anchoring, and commit conservation.

Edge convention (canonical): an edge is ``(prerequisite_id, dependant_id)`` —
"prerequisite must complete before dependant", i.e. a directed edge pointing
prerequisite -> dependant. ``reaches(edges, src, dst)`` therefore answers "is
``dst`` a transitive *dependant* of ``src``".
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

Edge = tuple[str, str]


# --------------------------------------------------------------------------- #
# Adjacency + reachability
# --------------------------------------------------------------------------- #
def build_successors(edges: Iterable[Edge]) -> dict[str, set[str]]:
    """Map each prerequisite to the set of its direct dependants."""
    succ: dict[str, set[str]] = defaultdict(set)
    for prerequisite, dependant in edges:
        succ[prerequisite].add(dependant)
    return succ


def build_parents(edges: Iterable[Edge]) -> dict[str, set[str]]:
    """Map each dependant to the set of its direct prerequisites."""
    parents: dict[str, set[str]] = defaultdict(set)
    for prerequisite, dependant in edges:
        parents[dependant].add(prerequisite)
    return parents


def reaches(edges: Iterable[Edge], src: str, dst: str) -> bool:
    """True iff ``dst`` is reachable from ``src`` following prerequisite->dependant edges.

    A node does not reach itself unless an actual (cyclic) path returns to it.
    """
    succ = build_successors(edges)
    seen: set[str] = set()
    stack = list(succ.get(src, ()))
    while stack:
        node = stack.pop()
        if node == dst:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(succ.get(node, ()))
    return False


def ancestors_of(node: str, edges: Iterable[Edge]) -> set[str]:
    """All transitive prerequisites of ``node``."""
    parents = build_parents(edges)
    seen: set[str] = set()
    stack = list(parents.get(node, ()))
    while stack:
        parent = stack.pop()
        if parent in seen:
            continue
        seen.add(parent)
        stack.extend(parents.get(parent, ()))
    return seen


# --------------------------------------------------------------------------- #
# Cycle detection
# --------------------------------------------------------------------------- #
def would_create_cycle(edges: Iterable[Edge], prerequisite: str, dependant: str) -> bool:
    """True iff adding ``prerequisite -> dependant`` would close a cycle.

    Lifted from ``csv_ops._would_create_cycle``: the new edge cycles iff the
    dependant can already reach the prerequisite. A self-edge is always a cycle.
    """
    if prerequisite == dependant:
        return True
    return reaches(edges, dependant, prerequisite)


def detect_cycle(edges: Iterable[Edge]) -> list[str] | None:
    """Return one cycle as a node path ``[n0, n1, ..., n0]``, or ``None`` if acyclic."""
    edge_list = list(edges)
    succ = build_successors(edge_list)
    nodes = {n for e in edge_list for n in e}
    color: dict[str, int] = {n: 0 for n in nodes}  # 0=white 1=gray 2=black
    path: list[str] = []

    def visit(start: str) -> list[str] | None:
        color[start] = 1
        path.append(start)
        for nxt in sorted(succ.get(start, ())):
            state = color.get(nxt, 0)
            if state == 1:  # back-edge into the active path → cycle
                return path[path.index(nxt) :] + [nxt]
            if state == 0:
                found = visit(nxt)
                if found is not None:
                    return found
        path.pop()
        color[start] = 2
        return None

    for node in sorted(nodes):
        if color[node] == 0:
            found = visit(node)
            if found is not None:
                return found
    return None


def is_acyclic(edges: Iterable[Edge]) -> bool:
    return detect_cycle(edges) is None


# --------------------------------------------------------------------------- #
# DAG closure
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ClosureResult:
    is_closed: bool
    missing_ancestors: dict[str, list[str]]


def is_dag_closed(candidate: Sequence[str], edges: Iterable[Edge]) -> ClosureResult:
    """A candidate set is DAG-closed iff it contains every transitive prerequisite
    of every member."""
    cand = set(candidate)
    parents = build_parents(edges)
    missing: dict[str, list[str]] = {}
    for mid in candidate:
        anc: set[str] = set()
        stack = list(parents.get(mid, ()))
        while stack:
            parent = stack.pop()
            if parent in anc:
                continue
            anc.add(parent)
            stack.extend(parents.get(parent, ()))
        gap = sorted(anc - cand)
        if gap:
            missing[mid] = gap
    return ClosureResult(is_closed=not missing, missing_ancestors=missing)


# --------------------------------------------------------------------------- #
# Redundant edge
# --------------------------------------------------------------------------- #
def is_redundant_edge(edges: Iterable[Edge], prerequisite: str, dependant: str) -> bool:
    """True iff ``dependant`` is reachable from ``prerequisite`` *without* the direct
    edge — i.e. a transitive path already implies it (adding ``A->C`` when
    ``A->B->C`` exists). A redundant edge does not corrupt the DAG.
    """
    others = [e for e in edges if e != (prerequisite, dependant)]
    return reaches(others, prerequisite, dependant)


# --------------------------------------------------------------------------- #
# seq anchoring
# --------------------------------------------------------------------------- #
def seq_violations(seqs: Sequence[int | None]) -> list[str]:
    """Check the cross-file ``events[i].seq == i + 1`` anchor invariant."""
    violations: list[str] = []
    for i, seq in enumerate(seqs):
        expected = i + 1
        if seq is None:
            violations.append(f"events[{i}] missing seq (expected {expected})")
        elif seq != expected:
            violations.append(f"events[{i}].seq == {seq}, expected {expected}")
    return violations


# --------------------------------------------------------------------------- #
# SHA matching (lifted from csv_ops._match_sha) + commit parsing
# --------------------------------------------------------------------------- #
def parse_commits(value: str | None) -> list[str]:
    """Split a ``;``-separated commits cell into stripped, non-empty SHAs."""
    if not value:
        return []
    return [c.strip() for c in str(value).split(";") if c.strip()]


def match_sha(needle: str, haystack: Iterable[str]) -> list[str]:
    """Match ``needle`` against ``haystack`` as a prefix in either direction.

    SHAs may be stored at full length while a short prefix is passed (or vice
    versa). Both directions are accepted; ambiguous matches are returned in full
    so the caller can reject them rather than corrupt silently.
    """
    needle = (needle or "").strip()
    if not needle:
        return []
    return [c for c in haystack if c == needle or c.startswith(needle) or needle.startswith(c)]


# --------------------------------------------------------------------------- #
# Commit conservation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ConservationResult:
    undeclared_lost: list[str]
    duplicated: list[str]


def commit_conservation(
    before: Mapping[str, Sequence[str]],
    after: Mapping[str, Sequence[str]],
    declared_drops: Iterable[str],
) -> ConservationResult:
    """Verify no *undeclared* commit loss or duplication across a DAG edit.

    - ``undeclared_lost``: a commit present in ``before`` that is neither present
      in ``after`` nor covered by a declared ``drop_commit`` / ``drop_milestone``.
    - ``duplicated``: a commit owned by more than one milestone in ``after``
      (a split / reassign bug).

    Presence and declared-drop coverage use ``match_sha`` so a commit stored at a
    different prefix length across snapshots still counts as the same commit.
    """
    after_owner: dict[str, list[str]] = defaultdict(list)
    for mid, shas in after.items():
        for sha in shas:
            after_owner[sha].append(mid)
    after_all = list(after_owner.keys())

    declared = list(declared_drops or [])

    seen: set[str] = set()
    before_unique: list[str] = []
    for shas in before.values():
        for sha in shas:
            if sha not in seen:
                seen.add(sha)
                before_unique.append(sha)

    undeclared_lost: list[str] = []
    for sha in before_unique:
        if match_sha(sha, after_all):
            continue
        if match_sha(sha, declared):
            continue
        undeclared_lost.append(sha)

    duplicated = sorted(sha for sha, mids in after_owner.items() if len(mids) > 1)
    return ConservationResult(undeclared_lost=undeclared_lost, duplicated=duplicated)
