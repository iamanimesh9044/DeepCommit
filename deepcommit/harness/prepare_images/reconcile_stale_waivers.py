#!/usr/bin/env python3
"""Reconcile stale waivers / commit-signals after a DAG change (善后 cleanup).

After a ``dag_update`` changes commit reachability, some ``bound_waivers``
become **moot**: the error they excused no longer occurs (the cited feature
became reachable, or the milestone's commit set changed). The validation
pipeline flags these in
``build_status.json::raw_metrics.invalid_bound_waivers`` with the error
``"feature_introduced_later: anchor not in error_groups[...]"`` (the anchored
error is absent from both START and END error groups).

Moot waivers do **not** affect the hard gate (gates read ``value_real``, which
already excludes unverified/invalid waivers). But they are *noise*: the next
M2 agent re-reads and re-audits prior waivers (retry_context.md.j2 instructs
it to), and Skill 1/3 audits trip over them. Per operator decision, waivers
are shared aids (not private records) and **moot entries are physically
deleted** so the next agent starts from a clean, truthful set.

Targets ONLY the live authoritative recipe files:
    dockerfiles/{MID}/module2_waivers.json
    dockerfiles/{MID}/module2_signals.json
NEVER touches ``_build_runs/{MID}/attempt_N/`` archives (immutable history —
they remain the full provenance record of what each attempt produced).

Default (safe) removals:
  - ``bound_waivers[]`` whose build_status invalid reason is
    "anchor not in error_groups" (excused error gone -> moot).
  - cascade: strip the deleted ids from
    ``commit_moves[].resolves.bound_waiver_ids``; drop any ``commit_move``
    that, after stripping, resolves nothing (orphaned proposal).

Reported but NOT removed (actionable, not moot):
  - invalid waivers for other reasons (schema errors; "IS reachable" while the
    error still persists) — M3 must fix or escalate these, not delete them.

ORDERING: run AFTER a fresh validation (``--validate-current-only``) so the
build_status error_groups are current. Canonical 善后 sequence:
    dag_update / surgical edit  ->  validate_only  ->  reconcile  ->  re-validate
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

MOOT_REASON_SUBSTR = "anchor not in error_groups"
_ID_RE = re.compile(r"id=(\d+)")


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _moot_waiver_ids(build_status: dict) -> set[int]:
    """Integer ids of bound_waivers whose excused error is gone (moot)."""
    moot: set[int] = set()
    invalid = build_status.get("raw_metrics", {}).get("invalid_bound_waivers", []) or []
    for entry in invalid:
        err = str(entry.get("error", ""))
        if MOOT_REASON_SUBSTR not in err:
            continue
        m = _ID_RE.search(str(entry.get("id", "")))
        if m:
            moot.add(int(m.group(1)))
    return moot


def _other_invalid(build_status: dict) -> list[dict]:
    """Invalid waivers that are NOT moot (actionable — reported, not deleted)."""
    out = []
    invalid = build_status.get("raw_metrics", {}).get("invalid_bound_waivers", []) or []
    for entry in invalid:
        if MOOT_REASON_SUBSTR not in str(entry.get("error", "")):
            out.append(entry)
    return out


def reconcile_milestone(docker_dir: Path, *, dry_run: bool) -> dict:
    """Remove moot bound_waivers (+ cascade signals) for one milestone.

    Returns a provenance dict: {removed_waivers, orphaned_moves, other_invalid, changed}.
    """
    result = {
        "milestone": docker_dir.name,
        "removed_waivers": [],
        "orphaned_moves": [],
        "other_invalid": [],
        "changed": False,
        "skipped": None,
    }

    bs = _read_json(docker_dir / "build_status.json")
    if bs is None:
        result["skipped"] = "no build_status.json"
        return result

    moot_ids = _moot_waiver_ids(bs)
    result["other_invalid"] = [{"id": e.get("id"), "error": str(e.get("error"))[:80]} for e in _other_invalid(bs)]
    if not moot_ids:
        return result  # nothing moot to clean

    waivers_path = docker_dir / "module2_waivers.json"
    waivers = _read_json(waivers_path)
    if waivers is None:
        result["skipped"] = "no module2_waivers.json"
        return result

    bound = waivers.get("bound_waivers", []) or []
    kept, removed = [], []
    for w in bound:
        if w.get("id") in moot_ids:
            removed.append({"id": w.get("id"), "anchor": str(w.get("anchor"))[:90]})
        else:
            kept.append(w)
    if not removed:
        return result  # build_status flagged ids not present in current waivers file
    waivers["bound_waivers"] = kept
    result["removed_waivers"] = removed
    result["changed"] = True

    # cascade into module2_signals.json: strip deleted waiver ids from each
    # commit_move's resolves.bound_waiver_ids; drop moves that resolve nothing.
    signals_path = docker_dir / "module2_signals.json"
    signals = _read_json(signals_path)
    signals_changed = False
    if signals is not None:
        moves = signals.get("commit_moves", []) or []
        new_moves = []
        for mv in moves:
            resolves = mv.get("resolves", {}) or {}
            bwids = [i for i in (resolves.get("bound_waiver_ids", []) or []) if i not in moot_ids]
            uiids = resolves.get("unbound_issue_ids", []) or []
            if not bwids and not uiids:
                result["orphaned_moves"].append({"id": mv.get("id"), "resolving_commit": mv.get("resolving_commit")})
                signals_changed = True
                continue
            if bwids != (resolves.get("bound_waiver_ids", []) or []):
                resolves["bound_waiver_ids"] = bwids
                mv["resolves"] = resolves
                signals_changed = True
            new_moves.append(mv)
        if signals_changed:
            signals["commit_moves"] = new_moves

    if not dry_run:
        _atomic_write_json(waivers_path, waivers)
        if signals_changed and signals is not None:
            _atomic_write_json(signals_path, signals)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("milestones", nargs="*", help="milestone ids (default: --all)")
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--all", action="store_true", help="process every dockerfiles/*/")
    parser.add_argument("--dry-run", action="store_true", help="report only, do not write")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    dockerfiles_dir = args.work_dir / "dockerfiles"
    if args.all or not args.milestones:
        mids = sorted(p.name for p in dockerfiles_dir.iterdir() if p.is_dir() and (p / "build_status.json").exists())
    else:
        mids = args.milestones

    total_w = total_m = changed = 0
    for mid in mids:
        r = reconcile_milestone(dockerfiles_dir / mid, dry_run=args.dry_run)
        if r["skipped"]:
            continue
        if r["removed_waivers"] or r["orphaned_moves"]:
            changed += 1
            total_w += len(r["removed_waivers"])
            total_m += len(r["orphaned_moves"])
            tag = "[dry-run] would remove" if args.dry_run else "removed"
            logger.info(
                f"{mid}: {tag} {len(r['removed_waivers'])} moot waiver(s), "
                f"{len(r['orphaned_moves'])} orphaned commit_move(s)"
            )
            for w in r["removed_waivers"]:
                logger.info(f"    - waiver id={w['id']}  anchor={w['anchor']!r}")
            for mv in r["orphaned_moves"]:
                logger.info(f"    - commit_move id={mv['id']} (resolved only-deleted waivers)")
        if r["other_invalid"]:
            logger.info(
                f"{mid}: {len(r['other_invalid'])} OTHER invalid waiver(s) (NOT moot — review, not auto-deleted):"
            )
            for e in r["other_invalid"]:
                logger.info(f"    ! {e['id']}: {e['error']}")

    verb = "Would clean" if args.dry_run else "Cleaned"
    logger.info(f"\n{verb} {total_w} moot waiver(s) + {total_m} orphaned move(s) across {changed} milestone(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
