"""verify-dag — verify the resulting CSV state of a dag_update event.

Runs per event at execution time against that event's pre-edit ``--before``
snapshot (a round can hold several sequential dag_update events; later edits
would destroy the intermediate before/after states). Checks full-graph
acyclicity, per-edge reachability, candidate DAG-closure, and commit
conservation (no *undeclared* loss/duplication — declared drops come from the
plan's ``drop_commit`` / ``drop_milestone`` ops). Redundant edges are reported
as warnings, not violations.

With ``--strict-ops`` (needs the ``--before`` dir + ``--plan`` + ``--seq``) it
additionally enforces *ops-equivalence*: the event's declared ``final_ops`` (+
``m1_result.applied_refinements``) applied to the before-state must reproduce the
actual after-CSVs exactly — same edge set, per-milestone commit ownership, and
milestone-id set. This catches a wrong/missing edge, a ``reassign_commit`` to the
wrong milestone, or a ``split``/``merge`` that produced the wrong id — divergences
the structural + conservation checks alone let through.

``--cumulative`` is the whole-round form: it folds EVERY ``dag_update`` in the
``--plan`` (optionally up to ``--through-seq``) onto the round-start state loaded
from the plan's ``baseline_run`` checkpoint (a ``_testbed_runs/run_NNN/`` archive),
then compares to the current CSVs — so one canonical baseline per round replaces a
per-event snapshot. A ``revert`` event resets the fold to its ``restore_from``
checkpoint. A missing/partial checkpoint archive fails closed (exit 1, never a
silent pass).

Exit: 0 clean (warnings allowed) · 2 hard violation · 1 input invalid.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Sequence

import yaml

from deepcommit.harness.main_orch import apply_ops
from deepcommit.harness.main_orch import context as ctx
from deepcommit.harness.main_orch import invariants as inv


def _resolve_before_milestones(before: str | Path) -> Path:
    p = Path(before)
    return (p / "milestones.csv") if p.is_dir() else p


def _read_plan(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise ctx.InputError(f"--plan not found: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ctx.InputError(f"invalid YAML in {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ctx.InputError(f"--plan must be a YAML mapping: {p}")
    return data


def declared_drops_from_plan(
    plan_data: dict[str, Any],
    before_commits: dict[str, list[str]],
    seq: int | None = None,
) -> list[str]:
    """Collect commit SHAs declared dropped by the plan's dag_update events.

    ``drop_commit`` contributes its ``commit_sha``; ``drop_milestone`` (which
    records only ``milestone_id``) has its commits expanded from ``before_commits``.
    With ``seq`` given, only that event is consulted.
    """
    drops: list[str] = []
    for event in plan_data.get("events") or []:
        if not isinstance(event, dict):
            continue
        if seq is not None and event.get("seq") != seq:
            continue
        final_ops = event.get("final_ops") or {}
        if not isinstance(final_ops, dict):
            continue
        for op in final_ops.get("drop_commit") or []:
            sha = op.get("commit_sha") if isinstance(op, dict) else None
            if sha:
                drops.append(str(sha))
        for op in final_ops.get("drop_milestone") or []:
            mid = op.get("milestone_id") if isinstance(op, dict) else None
            if mid and mid in before_commits:
                drops.extend(before_commits[mid])
    return drops


def _event_ops(plan_data: dict[str, Any], seq: int | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(final_ops, applied_refinements)`` of the plan event with ``seq``.

    ``--strict-ops`` needs a single event; ``seq`` is required so the right one is
    picked even when a round holds several dag_update events.
    """
    for event in plan_data.get("events") or []:
        if isinstance(event, dict) and event.get("seq") == seq:
            final_ops = event.get("final_ops")
            m1 = event.get("m1_result")
            refinements = m1.get("applied_refinements") if isinstance(m1, dict) else None
            return (
                final_ops if isinstance(final_ops, dict) else {},
                refinements if isinstance(refinements, dict) else {},
            )
    raise ctx.InputError(f"--strict-ops: no event with seq={seq} in plan")


def _equiv_report(eq: apply_ops.EquivalenceResult) -> dict[str, Any]:
    return {
        "passed": eq.ok,
        "edges_only_in_declared": [list(e) for e in eq.edges_only_in_declared],
        "edges_only_in_actual": [list(e) for e in eq.edges_only_in_actual],
        "ownership_mismatches": eq.ownership_mismatches,
        "milestones_only_in_declared": eq.milestones_only_in_declared,
        "milestones_only_in_actual": eq.milestones_only_in_actual,
    }


def _run_archive_loader(ws: Path):
    """Return a loader resolving a canonical checkpoint (run id) -> (edges, commits).

    Reads the run's archived input CSVs at ``_testbed_runs/{run_id}/``; raises if
    either CSV is absent so a missing/partial checkpoint fails closed (never a
    silent pass).
    """

    def load(run_id: str) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
        d = ws / "_testbed_runs" / run_id
        mpath, dpath = d / "milestones.csv", d / "dependencies.csv"
        if not mpath.exists() or not dpath.exists():
            raise ctx.InputError(f"checkpoint archive incomplete (need both CSVs): {d}")
        return ctx.load_edges(dpath), ctx.load_milestone_commits(mpath)

    return load


def run_verify_dag(
    workspace: str | Path,
    *,
    before: str | Path | None = None,
    plan: str | Path | None = None,
    seq: int | None = None,
    declared_drops: Sequence[str] = (),
    candidate: Sequence[str] | None = None,
    strict_ops: bool = False,
    cumulative: bool = False,
    baseline_run: str | None = None,
    through_seq: int | None = None,
) -> tuple[dict[str, Any], int]:
    ws = ctx.resolve_workspace(workspace)
    edges = ctx.load_edges(ws / "dependencies.csv")
    after_commits = ctx.load_milestone_commits(ws / "milestones.csv")

    # --- acyclicity + per-edge reachability (acyclic ⟺ no edge closes a cycle) ---
    cycle = inv.detect_cycle(edges)
    unreachable: list[list[str]] = []
    for prereq, dep in edges:
        others = [e for e in edges if e != (prereq, dep)]
        if inv.reaches(others, dep, prereq):
            unreachable.append([prereq, dep])

    # --- redundant edges (soft) ---
    redundant = [[u, v] for (u, v) in edges if inv.is_redundant_edge(edges, u, v)]

    # --- candidate DAG-closure ---
    unclosed: dict[str, list[str]] = {}
    if candidate:
        closure = inv.is_dag_closed(list(candidate), edges)
        if not closure.is_closed:
            unclosed = closure.missing_ancestors

    # --- commit conservation ---
    commit_diff: dict[str, Any] | None = None
    if before is not None:
        before_mpath = _resolve_before_milestones(before)
        if not Path(before_mpath).exists():
            raise ctx.InputError(f"--before milestones.csv not found: {before_mpath}")
        before_commits = ctx.load_milestone_commits(before_mpath)
        drops = list(declared_drops)
        if plan is not None:
            drops += declared_drops_from_plan(_read_plan(plan), before_commits, seq)
        conservation = inv.commit_conservation(before_commits, after_commits, drops)
        commit_diff = {
            "undeclared_lost": conservation.undeclared_lost,
            "dup": conservation.duplicated,
            "declared_drops": sorted(set(drops)),
        }

    # --- ops-equivalence: declared ops == actual CSV diff (cumulative or per-event) ---
    ops_equiv: dict[str, Any] | None = None
    if cumulative:
        if plan is None:
            raise ctx.InputError("--cumulative requires --plan")
        plan_data = _read_plan(plan)
        run_ref = baseline_run or plan_data.get("baseline_run")
        if not run_ref:
            raise ctx.InputError("--cumulative requires a baseline_run (plan top-level field or --baseline-run)")
        loader = _run_archive_loader(ws)
        base_edges, base_commits = loader(str(run_ref))
        sim = apply_ops.apply_plan(base_edges, base_commits, plan_data.get("events") or [], loader, through_seq)
        ops_equiv = _equiv_report(apply_ops.ops_equivalence(sim, edges, after_commits))
    elif strict_ops:
        if before is None or plan is None or seq is None:
            raise ctx.InputError("--strict-ops requires --before, --plan, and --seq")
        before_dir = Path(before)
        if not before_dir.is_dir():
            raise ctx.InputError(f"--strict-ops needs --before to be a snapshot dir with both CSVs: {before_dir}")
        sim = apply_ops.apply_ops(
            ctx.load_edges(before_dir / "dependencies.csv"),
            ctx.load_milestone_commits(before_dir / "milestones.csv"),
            *_event_ops(_read_plan(plan), seq),
        )
        ops_equiv = _equiv_report(apply_ops.ops_equivalence(sim, edges, after_commits))

    violations: list[str] = []
    if cycle:
        violations.append("cycle")
    if unreachable:
        violations.append("unreachable_edge")
    if unclosed:
        violations.append("not_dag_closed")
    if commit_diff and (commit_diff["undeclared_lost"] or commit_diff["dup"]):
        violations.append("commit_conservation")
    if ops_equiv is not None and not ops_equiv["passed"]:
        violations.append("ops_equivalence")

    report = {
        "verb": "verify-dag",
        "workspace": str(ws),
        "cycles": cycle or [],
        "unreachable_violations": unreachable,
        "unclosed": unclosed,
        "commit_diff": commit_diff,
        "ops_equivalence": ops_equiv,
        "warnings": {"redundant_edges": redundant},
        "violations": violations,
        "passed": not violations,
    }
    if ops_equiv is None:
        del report["ops_equivalence"]
    return report, (ctx.VIOLATION if violations else ctx.OK)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="m3 verify-dag", description="Verify a dag_update event's resulting CSV state."
    )
    parser.add_argument("--workspace", default=None, help="Harness workspace directory")
    parser.add_argument(
        "--before", default=None, help="Pre-edit baseline CSV dir (or milestones.csv) for commit conservation"
    )
    parser.add_argument("--plan", default=None, help="Round plan.yaml — declared drop_commit/drop_milestone ops")
    parser.add_argument("--seq", type=int, default=None, help="Only consult the plan event with this seq")
    parser.add_argument("--declared-drops", default=None, help="Comma/space-separated SHAs declared dropped")
    parser.add_argument("--candidate", default=None, help="Comma/space-separated ids to DAG-closure check")
    parser.add_argument(
        "--strict-ops",
        action="store_true",
        help="Per-event: enforce declared final_ops == actual before->after CSV diff (needs --before dir, --plan, --seq)",
    )
    parser.add_argument(
        "--cumulative",
        action="store_true",
        help="Whole-round: fold ALL dag_update events from baseline_run and compare to the current CSVs",
    )
    parser.add_argument(
        "--baseline-run", default=None, help="Round-start checkpoint run id (overrides plan baseline_run)"
    )
    parser.add_argument(
        "--through-seq", type=int, default=None, help="With --cumulative, fold only events up to this seq"
    )
    parser.add_argument("--output", type=Path, default=None, help="Output JSON path; defaults to stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        report, code = run_verify_dag(
            args.workspace,
            before=args.before,
            plan=args.plan,
            seq=args.seq,
            declared_drops=ctx.parse_id_list(args.declared_drops),
            candidate=ctx.parse_id_list(args.candidate) or None,
            strict_ops=args.strict_ops,
            cumulative=args.cumulative,
            baseline_run=args.baseline_run,
            through_seq=args.through_seq,
        )
    except ctx.InputError as exc:
        ctx.emit({"verb": "verify-dag", "errors": [str(exc)], "passed": False}, args.output)
        return ctx.INPUT_INVALID
    ctx.emit(report, args.output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
