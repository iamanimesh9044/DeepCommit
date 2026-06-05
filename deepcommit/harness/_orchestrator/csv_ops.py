"""Structured CSV mutations for milestones.csv / dependencies.csv.

Every CSV change — whether from the Main Agent review step or from the
Stage1 cherry-pick fix subloop — goes through this module. This gives us:

  * a structured audit trail (each op carries a rationale)
  * pre-apply validation (unknown ids, missing SHAs, cycles, duplicates)
  * a single place where file-level invariants are enforced
  * identical semantics regardless of which LLM produced the op

Phase A op set:

  add_edge        {source_id, target_id, rationale}
  remove_edge     {source_id, target_id, rationale}
  move_commit     {commit_sha, from_milestone_id, to_milestone_id, rationale}
  drop_commit     {commit_sha, milestone_id, rationale}
  drop_milestone  {id, rationale}
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


OP_TYPES = {
    "add_edge",
    "remove_edge",
    "move_commit",
    "drop_commit",
    "drop_milestone",
}


# =============================================================================
# Public API
# =============================================================================


def apply_csv_ops(
    iter_dir: Path,
    ops: List[Dict],
) -> Tuple[List[Dict], List[Dict]]:
    """Apply ops in order to ``iter_dir/milestones.csv`` + ``dependencies.csv``.

    Each op is either applied (mutates the in-memory rows) or rejected with
    a reason. After all ops are processed, the final state is flushed back
    to both CSV files in one pass.

    Returns:
        ``(applied, rejected)``. Each rejected op dict gains a
        ``_reject_reason`` key; applied ops are returned unchanged.
    """
    milestones_path = iter_dir / "milestones.csv"
    deps_path = iter_dir / "dependencies.csv"

    if not milestones_path.exists():
        raise FileNotFoundError(f"milestones.csv not found in {iter_dir}")
    if not deps_path.exists():
        raise FileNotFoundError(f"dependencies.csv not found in {iter_dir}")

    milestones_rows, milestones_fields = _load_csv(milestones_path)
    deps_rows, deps_fields = _load_csv(deps_path)

    applied: List[Dict] = []
    rejected: List[Dict] = []

    for idx, op in enumerate(ops):
        if not isinstance(op, dict):
            rejected.append({"_index": idx, "_reject_reason": f"not a dict: {op!r}"})
            continue

        reason = _apply_one(op, milestones_rows, deps_rows)
        entry = dict(op)
        if reason is None:
            applied.append(entry)
        else:
            entry["_reject_reason"] = reason
            rejected.append(entry)
            logger.warning(f"Rejected op #{idx} ({op.get('op', '?')}): {reason}")

    _write_csv(milestones_path, milestones_rows, milestones_fields)
    _write_csv(deps_path, deps_rows, deps_fields)

    logger.info(f"apply_csv_ops({iter_dir.name}): {len(applied)} applied, {len(rejected)} rejected")
    return applied, rejected


# =============================================================================
# Dispatch + per-op handlers (return None on success, else reason string)
# =============================================================================


def _apply_one(
    op: Dict,
    milestones_rows: List[Dict],
    deps_rows: List[Dict],
) -> Optional[str]:
    op_type = op.get("op")
    if op_type not in OP_TYPES:
        return f"unknown op type: {op_type!r}"

    try:
        if op_type == "add_edge":
            return _op_add_edge(op, milestones_rows, deps_rows)
        if op_type == "remove_edge":
            return _op_remove_edge(op, deps_rows)
        if op_type == "move_commit":
            return _op_move_commit(op, milestones_rows)
        if op_type == "drop_commit":
            return _op_drop_commit(op, milestones_rows)
        if op_type == "drop_milestone":
            return _op_drop_milestone(op, milestones_rows, deps_rows)
    except KeyError as e:
        return f"missing required field: {e}"

    return f"unhandled op type: {op_type}"


def _op_add_edge(op, milestones_rows, deps_rows) -> Optional[str]:
    source = op["source_id"]
    target = op["target_id"]

    if source == target:
        return "self-edge not allowed"

    ids = {r.get("id") for r in milestones_rows}
    if source not in ids:
        return f"source_id {source!r} not in milestones.csv"
    if target not in ids:
        return f"target_id {target!r} not in milestones.csv"

    for row in deps_rows:
        if row.get("source_id") == source and row.get("target_id") == target:
            return "edge already exists"

    if _would_create_cycle(deps_rows, source, target):
        return "would create cycle"

    new_row: Dict[str, str] = {
        "source_id": source,
        "target_id": target,
        "type": op.get("type", "FUNC"),
        "strength": op.get("strength", "Strong"),
        "rationale": op.get("rationale", ""),
        "confidence_score": str(op.get("confidence_score", "0.85")),
    }
    # Ensure the new row has every field the existing CSV uses.
    if deps_rows:
        for k in deps_rows[0].keys():
            new_row.setdefault(k, "")
    deps_rows.append(new_row)
    return None


def _op_remove_edge(op, deps_rows) -> Optional[str]:
    source = op["source_id"]
    target = op["target_id"]
    for i, row in enumerate(deps_rows):
        if row.get("source_id") == source and row.get("target_id") == target:
            del deps_rows[i]
            return None
    return f"edge {source} → {target} not found"


def _op_move_commit(op, milestones_rows) -> Optional[str]:
    sha = op["commit_sha"]
    from_id = op["from_milestone_id"]
    to_id = op["to_milestone_id"]

    if from_id == to_id:
        return "from_milestone_id and to_milestone_id are identical"

    from_row = _find_milestone(milestones_rows, from_id)
    to_row = _find_milestone(milestones_rows, to_id)
    if from_row is None:
        return f"from_milestone_id {from_id!r} not found"
    if to_row is None:
        return f"to_milestone_id {to_id!r} not found"

    from_commits = _parse_commits(from_row.get("commits", ""))
    to_commits = _parse_commits(to_row.get("commits", ""))

    matched = _match_sha(sha, from_commits)
    if len(matched) == 0:
        return f"commit {sha!r} not in {from_id}"
    if len(matched) > 1:
        return f"commit {sha!r} is an ambiguous prefix in {from_id}"
    full_sha = matched[0]

    if _match_sha(full_sha, to_commits):
        return f"commit {sha!r} already in {to_id}"

    from_commits.remove(full_sha)
    to_commits.append(full_sha)
    from_row["commits"] = ";".join(from_commits)
    to_row["commits"] = ";".join(to_commits)
    return None


def _op_drop_commit(op, milestones_rows) -> Optional[str]:
    sha = op["commit_sha"]
    mid = op["milestone_id"]

    row = _find_milestone(milestones_rows, mid)
    if row is None:
        return f"milestone_id {mid!r} not found"

    commits = _parse_commits(row.get("commits", ""))
    matched = _match_sha(sha, commits)
    if len(matched) == 0:
        return f"commit {sha!r} not in {mid}"
    if len(matched) > 1:
        return f"commit {sha!r} is an ambiguous prefix in {mid}"
    commits.remove(matched[0])
    row["commits"] = ";".join(commits)
    return None


def _op_drop_milestone(op, milestones_rows, deps_rows) -> Optional[str]:
    mid = op["id"]
    row = _find_milestone(milestones_rows, mid)
    if row is None:
        return f"milestone {mid!r} not found"

    milestones_rows.remove(row)
    # Cascade: drop any edge touching this milestone.
    deps_rows[:] = [r for r in deps_rows if r.get("source_id") != mid and r.get("target_id") != mid]
    return None


# =============================================================================
# Helpers
# =============================================================================


def _find_milestone(rows: List[Dict], mid: str) -> Optional[Dict]:
    for r in rows:
        if r.get("id") == mid:
            return r
    return None


def _parse_commits(s: str) -> List[str]:
    return [c.strip() for c in s.split(";") if c.strip()]


def _match_sha(needle: str, haystack: List[str]) -> List[str]:
    """Find commits in haystack matching ``needle`` as a prefix either way.

    SHAs may be stored at full length while the agent may pass a short
    prefix (or vice versa). We accept either direction but reject ambiguous
    matches so silent corruption is impossible.
    """
    needle = needle.strip()
    return [c for c in haystack if c == needle or c.startswith(needle) or needle.startswith(c)]


def _would_create_cycle(deps_rows: List[Dict], new_source: str, new_target: str) -> bool:
    """Check whether adding source→target closes a cycle.

    An edge source→target creates a cycle iff ``source`` is already
    reachable via existing edges starting from ``target``.
    """
    adj: Dict[str, Set[str]] = {}
    for r in deps_rows:
        adj.setdefault(r.get("source_id", ""), set()).add(r.get("target_id", ""))

    visited: Set[str] = set()
    stack = [new_target]
    while stack:
        node = stack.pop()
        if node == new_source:
            return True
        if node in visited:
            continue
        visited.add(node)
        stack.extend(adj.get(node, set()))
    return False


def _load_csv(path: Path) -> Tuple[List[Dict], List[str]]:
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    return rows, list(fieldnames)


def _write_csv(path: Path, rows: List[Dict], fieldnames: List[str]) -> None:
    # If add_edge introduced a new field that wasn't in the original header,
    # extend the header so the data round-trips without loss.
    extras: Set[str] = set()
    for r in rows:
        for k in r.keys():
            if k not in fieldnames:
                extras.add(k)
    if extras:
        fieldnames = list(fieldnames) + sorted(extras)

    if not fieldnames:
        # Empty CSV — still need a header row. Best effort based on schema.
        fieldnames = ["id"] if path.name == "milestones.csv" else ["source_id", "target_id"]

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
