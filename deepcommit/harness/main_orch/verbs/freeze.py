"""freeze — the two frozen-prefix state-writers (``freeze-apply`` + ``unfreeze``).

Both are the *one retained "script writes"* case of the m3 suite: ``_frozen_prefix.json``
is the freeze-state deliverable with hard invariants, and it is reversible — so the
write side (FREEZE) and its undo (UNFREEZE) live together in this single module.

Two CLIs are exposed, one per verb (``cli.py`` dispatches to each ``*_main``):

  m3 freeze-apply  → ``freeze_apply_main`` / ``run_freeze_apply``
      Atomically write the candidate prefix into ``_frozen_prefix.json`` (FREEZE).
      Precondition (input-invalid on failure): a passing ``frozen_check.json``
      (``overall_passed && is_dag_closed``) whose ``candidate_prefix`` equals
      ``--candidate``, and ``--decision FREEZE``. Invariants (write refused, exit 2,
      zero partial write): candidate is DAG-closed in the current graph; candidate ⊆
      scope; and the resulting frozen set is top-down. Also pins each frozen MID's
      recipe checkpoint into ``frozen_checkpoints`` (run_id global + per-MID attempt)
      so a later drift of a frozen Dockerfile is detectable against the canonical
      ``_build_runs/{MID}/attempt_N`` archive.

  m3 unfreeze      → ``unfreeze_main`` / ``run_unfreeze``
      Remove milestones from the frozen prefix and append an UNFREEZE entry. Freeze
      membership is reversible by design (reference_freeze_ceremony.md § Freeze decision rules); this is the supported path.
      Invariant (write refused on violation): the remaining frozen set is still
      DAG-closed — you cannot leave a "frozen depends on un-frozen" broken window.

In both, only ``history`` is append-only. Backup/atomic-write is
``write_frozen_prefix_atomic`` → ``_frozen_prefix.json.bak_round{NNN}_pre``.

Exit (both): 0 applied · 2 invariant violation · 1 input invalid.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from deepcommit.harness.main_orch import context as ctx
from deepcommit.harness.main_orch import invariants as inv


# --------------------------------------------------------------------------- #
# freeze-apply                                                                 #
# --------------------------------------------------------------------------- #
def _fc_rel(ws: Path, fc_path: Path) -> str:
    try:
        return str(fc_path.resolve().relative_to(ws))
    except ValueError:
        return str(fc_path)


def _latest_testbed_run(ws: Path) -> str | None:
    """The workspace-global M1 sync checkpoint at freeze (max _testbed_runs/run_NNN)."""
    d = ws / "_testbed_runs"
    if not d.is_dir():
        return None
    runs = sorted(p.name for p in d.iterdir() if p.is_dir() and p.name.startswith("run_"))
    return runs[-1] if runs else None


def run_freeze_apply(
    workspace: str | Path,
    *,
    round_n: int | None,
    candidate: Sequence[str],
    frozen_check: str | Path | None,
    decision: str | None,
    reasoning: str | None,
    scope: Sequence[str] | None = None,
    skill3_recommendation: str | None = None,
    fork_decision: str | None = None,
    fork_confidence: float | None = None,
) -> tuple[dict[str, Any], int]:
    ws = ctx.resolve_workspace(workspace)
    if round_n is None:
        raise ctx.InputError("--round is required")
    if decision != "FREEZE":
        raise ctx.InputError("--decision must be FREEZE for freeze-apply")
    if not candidate:
        raise ctx.InputError("--candidate is required")
    if not reasoning:
        raise ctx.InputError("--reasoning is required")
    if not frozen_check:
        raise ctx.InputError("--frozen-check is required")

    fc_path = Path(frozen_check)
    if not fc_path.is_absolute():
        fc_path = ws / fc_path
    fc, err = ctx.safe_read_json(fc_path)
    if not isinstance(fc, dict):
        raise ctx.InputError(f"frozen_check not readable: {err or fc_path}")
    if not (fc.get("overall_passed") is True and fc.get("is_dag_closed") is True):
        raise ctx.InputError("frozen_check is not passing (need overall_passed && is_dag_closed)")
    if set(candidate) != set(fc.get("candidate_prefix") or []):
        raise ctx.InputError("--candidate does not equal frozen_check.candidate_prefix")

    edges = ctx.load_edges(ws / "dependencies.csv")
    fp = ctx.read_frozen_prefix(ws)
    old_frozen = list(fp.get("frozen_milestones") or [])
    old_set = set(old_frozen)
    added = [c for c in candidate if c not in old_set]
    new_frozen = old_frozen + added

    scope_resolved = (
        list(scope) if scope else (fc.get("scope") or {}).get("scope_milestones") or fp.get("scope_milestones")
    )

    violations: list[Any] = []
    cand_closure = inv.is_dag_closed(list(candidate), edges)
    if not cand_closure.is_closed:
        violations.append({"candidate_not_dag_closed": cand_closure.missing_ancestors})
    if scope_resolved is not None:
        not_in_scope = [c for c in candidate if c not in set(scope_resolved)]
        if not_in_scope:
            violations.append({"candidate_not_in_scope": not_in_scope})
    new_closure = inv.is_dag_closed(new_frozen, edges)
    if not new_closure.is_closed:
        violations.append({"frozen_set_not_top_down": new_closure.missing_ancestors})

    if violations:
        return {
            "verb": "freeze-apply",
            "workspace": str(ws),
            "applied": False,
            "violations": violations,
            "passed": False,
        }, ctx.VIOLATION

    entry: dict[str, Any] = {
        "round": round_n,
        "round_label": f"round_{round_n:03d}",
        "added_milestones": added,
        "removed_milestones": [],
        "decision": "FREEZE",
        "reasoning": reasoning,
        "frozen_check_output": _fc_rel(ws, fc_path),
    }
    if skill3_recommendation:
        entry["skill_3_recommendation"] = skill3_recommendation
    if fork_decision:
        entry["fork_decision"] = fork_decision
    if fork_confidence is not None:
        entry["fork_confidence"] = fork_confidence

    data = dict(fp)
    data.setdefault("schema_version", 1)
    data["frozen_milestones"] = new_frozen
    data["frozen_round"] = round_n
    data["frozen_round_label"] = f"round_{round_n:03d}"
    data["frozen_check_output"] = _fc_rel(ws, fc_path)
    if scope_resolved is not None:
        data["scope_milestones"] = list(scope_resolved)
    baseline_source = (fc.get("phase2_baseline") or {}).get("source") or fp.get("phase2_baseline_source")
    if baseline_source:
        data["phase2_baseline_source"] = baseline_source
    # Pin each frozen MID's recipe checkpoint (run_id global + per-MID attempt) so a
    # later drift of a frozen Dockerfile is detectable against the canonical
    # _build_runs/{MID}/attempt_N archive — reference, not a copy.
    data["frozen_checkpoints"] = {
        "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "testbed_run": _latest_testbed_run(ws),
        "milestones": {mid: ((fc.get("milestones") or {}).get(mid) or {}).get("latest_attempt") for mid in candidate},
    }
    data["history"] = list(fp.get("history") or []) + [entry]

    backup = ctx.write_frozen_prefix_atomic(ws, data, round_n)
    return {
        "verb": "freeze-apply",
        "workspace": str(ws),
        "applied": True,
        "frozen_milestones": new_frozen,
        "added_milestones": added,
        "backup": str(backup) if backup else None,
        "passed": True,
    }, ctx.OK


def build_freeze_apply_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="m3 freeze-apply", description="Atomically FREEZE a candidate prefix.")
    parser.add_argument("--workspace", default=None, help="Harness workspace directory")
    parser.add_argument("--round", type=int, default=None, dest="round_n", help="Round number")
    parser.add_argument("--candidate", default=None, help="Comma/space-separated candidate prefix ids")
    parser.add_argument("--frozen-check", default=None, help="frozen_check.json path (overall_passed && is_dag_closed)")
    parser.add_argument("--decision", default=None, help="Must be FREEZE")
    parser.add_argument("--reasoning", default=None, help="Why this freeze (recorded in history)")
    parser.add_argument("--scope", default=None, help="Override scope ids for the candidate ⊆ scope check")
    parser.add_argument("--skill3-recommendation", default=None, help="Optional Skill 3 recommendation for history")
    parser.add_argument("--fork-decision", default=None, help="Optional fork decision for history")
    parser.add_argument("--fork-confidence", type=float, default=None, help="Optional fork confidence for history")
    parser.add_argument("--output", type=Path, default=None, help="Output JSON path; defaults to stdout")
    return parser


def freeze_apply_main(argv: Sequence[str] | None = None) -> int:
    args = build_freeze_apply_parser().parse_args(argv)
    try:
        report, code = run_freeze_apply(
            args.workspace,
            round_n=args.round_n,
            candidate=ctx.parse_id_list(args.candidate),
            frozen_check=args.frozen_check,
            decision=args.decision,
            reasoning=args.reasoning,
            scope=ctx.parse_id_list(args.scope) or None,
            skill3_recommendation=args.skill3_recommendation,
            fork_decision=args.fork_decision,
            fork_confidence=args.fork_confidence,
        )
    except ctx.InputError as exc:
        ctx.emit({"verb": "freeze-apply", "errors": [str(exc)], "applied": False, "passed": False}, args.output)
        return ctx.INPUT_INVALID
    ctx.emit(report, args.output)
    return code


# --------------------------------------------------------------------------- #
# unfreeze                                                                     #
# --------------------------------------------------------------------------- #
def run_unfreeze(
    workspace: str | Path,
    *,
    round_n: int | None,
    remove: Sequence[str],
    reasoning: str | None,
) -> tuple[dict[str, Any], int]:
    ws = ctx.resolve_workspace(workspace)
    if round_n is None:
        raise ctx.InputError("--round is required")
    if not remove:
        raise ctx.InputError("--remove is required")
    if not reasoning:
        raise ctx.InputError("--reasoning is required")

    fp = ctx.read_frozen_prefix(ws)
    old_frozen = list(fp.get("frozen_milestones") or [])
    if not old_frozen:
        raise ctx.InputError("nothing is frozen (no _frozen_prefix.json / empty frozen_milestones)")
    old_set = set(old_frozen)
    not_frozen = [m for m in remove if m not in old_set]
    if not_frozen:
        raise ctx.InputError(f"not currently frozen: {not_frozen}")

    remove_set = set(remove)
    new_frozen = [m for m in old_frozen if m not in remove_set]

    edges = ctx.load_edges(ws / "dependencies.csv")
    closure = inv.is_dag_closed(new_frozen, edges)
    if not closure.is_closed:
        return {
            "verb": "unfreeze",
            "workspace": str(ws),
            "applied": False,
            "violations": [{"frozen_set_not_top_down_after_removal": closure.missing_ancestors}],
            "passed": False,
        }, ctx.VIOLATION

    entry = {
        "round": round_n,
        "round_label": f"round_{round_n:03d}",
        "added_milestones": [],
        "removed_milestones": list(remove),
        "decision": "UNFREEZE",
        "reasoning": reasoning,
    }
    data = dict(fp)
    data.setdefault("schema_version", 1)
    data["frozen_milestones"] = new_frozen
    data["frozen_round"] = round_n
    data["frozen_round_label"] = f"round_{round_n:03d}"
    data["history"] = list(fp.get("history") or []) + [entry]

    backup = ctx.write_frozen_prefix_atomic(ws, data, round_n)
    return {
        "verb": "unfreeze",
        "workspace": str(ws),
        "applied": True,
        "frozen_milestones": new_frozen,
        "removed_milestones": list(remove),
        "backup": str(backup) if backup else None,
        "passed": True,
    }, ctx.OK


def build_unfreeze_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="m3 unfreeze", description="Remove milestones from the frozen prefix.")
    parser.add_argument("--workspace", default=None, help="Harness workspace directory")
    parser.add_argument("--round", type=int, default=None, dest="round_n", help="Round number")
    parser.add_argument("--remove", default=None, help="Comma/space-separated milestone ids to unfreeze")
    parser.add_argument("--reasoning", default=None, help="Why this unfreeze (recorded in history)")
    parser.add_argument("--output", type=Path, default=None, help="Output JSON path; defaults to stdout")
    return parser


def unfreeze_main(argv: Sequence[str] | None = None) -> int:
    args = build_unfreeze_parser().parse_args(argv)
    try:
        report, code = run_unfreeze(
            args.workspace,
            round_n=args.round_n,
            remove=ctx.parse_id_list(args.remove),
            reasoning=args.reasoning,
        )
    except ctx.InputError as exc:
        ctx.emit({"verb": "unfreeze", "errors": [str(exc)], "applied": False, "passed": False}, args.output)
        return ctx.INPUT_INVALID
    ctx.emit(report, args.output)
    return code
