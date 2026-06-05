"""dashboard — the Step-1 broad survey (read-only, self-validating).

One shot over every scope milestone's *latest* state: frozen?, built, the
H-gates partitioned into ``hard_failed`` (block ``built``) / ``soft_failed``
(whole-suite, non-blocking) with their numeric ``gate_values``, and unbound
count. When a ``frozen_check`` is available it also folds in the metadata
number-reconciliation that used to be the separate ``audit-artifacts`` verb:
per row, a ``consistency`` flag appears ONLY when build_status is *fresh* yet its
gate values diverge from the recomputed ``frozen_check.real_value`` / tracker —
so reading the survey, you can trust it. Surfaces problems; never rewrites
artifacts. Exit 0 (exit 1 only on a malformed workspace).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from deepcommit.harness.main_orch import context as ctx

_TOL = 1e-6


def _resolve_frozen_check(ws: Path, frozen_check: str | Path | None) -> Path | None:
    if frozen_check:
        p = Path(frozen_check)
        return p if p.is_absolute() else ws / p
    rel = ctx.read_frozen_prefix(ws).get("frozen_check_output")
    if rel:
        candidate = ws / str(rel)
        if candidate.exists():
            return candidate
    return None


def _tracker_value(ws: Path, mid: str) -> float | None:
    data, _ = ctx.safe_read_json(ws / "milestone_patched_tests" / f"{mid}.json")
    if not isinstance(data, dict):
        return None
    summary = data.get("summary") or {}
    collected = summary.get("patched_collected")
    effective = summary.get("effective_tests")
    if not isinstance(collected, (int, float)):
        return None
    if isinstance(effective, (int, float)) and effective > 0:
        return collected / effective
    return 1.0


_RECONCILED_GATE = "H_patched_completeness"


def _field_divergences(ws: Path, mid: str, build_status: dict, fc_data: dict) -> list[dict[str, Any]]:
    """3-way reconciliation on ``H_patched_completeness`` only.

    Only this gate is apples-to-apples across all three sources: build_status
    ``value``, frozen_check recomputed ``real_value``, and the patched-tests
    tracker ratio all measure patched completeness. The other H-gates' build_status
    ``value`` (whole-suite counts/ratios) and frozen_check ``real_value``
    (classification-summary counts) are different computations, so comparing them
    would surface false divergences. Tolerates both the slim frozen_check shape
    (per-MS ``{real_value: {gate: v}}``) and the legacy ``{gate: {real_value: v}}``.
    """
    fc_ms = (fc_data.get("milestones") or {}).get(mid)
    if not isinstance(fc_ms, dict):
        return []
    real_map = fc_ms.get("real_value")
    if isinstance(real_map, dict):
        real = real_map.get(_RECONCILED_GATE)
    else:
        real = (
            (fc_ms.get(_RECONCILED_GATE) or {}).get("real_value")
            if isinstance(fc_ms.get(_RECONCILED_GATE), dict)
            else None
        )
    values: dict[str, float] = {}
    if isinstance(real, (int, float)) and not isinstance(real, bool):
        values["frozen_check"] = float(real)
    gate_detail = (build_status.get("test_collection_and_result_quality") or {}).get(_RECONCILED_GATE)
    if isinstance(gate_detail, dict) and isinstance(gate_detail.get("value"), (int, float)):
        values["build_status"] = float(gate_detail["value"])
    tracker_val = _tracker_value(ws, mid)
    if tracker_val is not None:
        values["tracker"] = float(tracker_val)
    if len(values) >= 2 and (max(values.values()) - min(values.values())) > _TOL:
        return [{"gate": _RECONCILED_GATE, "values": values}]
    return []


def _survey_mid(
    workspace: Path, mid: str, frozen_set: set[str], fc_data: dict | None, fc_path: Path | None
) -> dict[str, Any]:
    build_status, err = ctx.read_build_status(workspace, mid)
    row: dict[str, Any] = {
        "milestone_id": mid,
        "frozen": mid in frozen_set,
        "build_status_present": build_status is not None,
    }
    if build_status is None:
        row.update(
            built=None,
            latest_attempt=None,
            unbound_count=None,
            gate_values={},
            hard_failed=None,
            soft_failed=None,
            note=err,
        )
        return row

    quality = build_status.get("test_collection_and_result_quality") or {}
    gate_values: dict[str, Any] = {}
    hard_failed: list[str] = []
    soft_failed: list[str] = []
    for gate, detail in quality.items():
        if not isinstance(detail, dict):
            continue
        value = detail.get("value")
        gate_values[gate] = value
        # Classify only genuine failures (recorded passed is False and the gate ran),
        # derived from `passed` + HARD/SOFT membership so OLD data re-partitions correctly.
        if detail.get("evaluated") is False or detail.get("passed") is not False:
            continue
        if gate in ctx.SOFT_QUALITY_GATES:
            soft_failed.append(gate)
        elif gate in ctx.HARD_QUALITY_GATES:
            sub_bar_ratio = (
                gate == "H_error_within_budget"
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0 < value <= ctx.H_ERROR_HARD_THRESHOLD
            )
            (soft_failed if sub_bar_ratio else hard_failed).append(gate)

    raw = build_status.get("raw_metrics") or {}
    # B_* build-run failures always block — take them from the recorded failed_gates.
    for g in build_status.get("failed_gates") or []:
        if isinstance(g, str) and g.startswith("B_") and g not in hard_failed:
            hard_failed.append(g)
    # Fold any recorded soft_warnings into the soft view so a built=true MS that still warns is visible.
    for w in raw.get("soft_warnings") or []:
        g = w.get("gate") if isinstance(w, dict) else None
        if isinstance(g, str) and g not in soft_failed and g not in hard_failed:
            soft_failed.append(g)

    row.update(
        built=build_status.get("built"),
        latest_attempt=ctx.latest_attempt_of(build_status),
        unbound_count=raw.get("unbound_issues_count"),
        gate_values=gate_values,
        hard_failed=hard_failed,
        soft_failed=soft_failed,
    )

    # Absorbed consistency: flag ONLY a genuine divergence (build_status fresh vs
    # frozen_check yet disagreeing). Expected lag (build_status predates frozen_check)
    # is not a problem and is not flagged.
    if fc_data is not None and isinstance((fc_data.get("milestones") or {}).get(mid), dict):
        divergences = _field_divergences(workspace, mid, build_status, fc_data)
        if divergences and (fc_path is None or ctx.newer_than(ctx.build_status_path(workspace, mid), fc_path)):
            row["consistency"] = {"status": "divergent", "fields": divergences}
    return row


def run_dashboard(
    workspace: str | Path,
    *,
    scope: Sequence[str] | None = None,
    frozen_check: str | Path | None = None,
) -> tuple[dict[str, Any], int]:
    ws = ctx.resolve_workspace(workspace)
    mids, source = ctx.resolve_scope(ws, scope)
    frozen_set = set(ctx.read_frozen_prefix(ws).get("frozen_milestones") or [])
    fc_path = _resolve_frozen_check(ws, frozen_check)
    fc_data, _ = ctx.safe_read_json(fc_path) if fc_path else (None, None)
    if not isinstance(fc_data, dict):
        fc_data = None

    rows = [_survey_mid(ws, mid, frozen_set, fc_data, fc_path) for mid in mids]
    summary = {
        "n": len(rows),
        "n_built": sum(1 for r in rows if r["built"] is True),
        "n_frozen": sum(1 for r in rows if r["frozen"]),
        "n_missing_build_status": sum(1 for r in rows if not r["build_status_present"]),
        "n_hard_failed": sum(1 for r in rows if r.get("hard_failed")),
        "n_soft_failed": sum(1 for r in rows if r.get("soft_failed")),
        "n_consistency_divergent": sum(1 for r in rows if r.get("consistency")),
    }
    report = {
        "verb": "dashboard",
        "workspace": str(ws),
        "scope_source": source,
        "frozen_check": str(fc_path) if fc_path else None,
        "summary": summary,
        "rows": rows,
    }
    return report, ctx.OK


def _render_table(report: dict[str, Any]) -> str:
    header = f"{'milestone':<28} {'F':<1} {'built':<5} {'unb':>3} {'!':<1} hard_failed (+soft)"
    lines = [header, "-" * len(header)]
    for r in report["rows"]:
        built = {True: "yes", False: "no", None: "?"}[r["built"]]
        hard = ",".join(r["hard_failed"]) if r.get("hard_failed") else ""
        soft = r.get("soft_failed") or []
        gate_cell = f"{hard}{f' (+{len(soft)} soft)' if soft else ''}".strip()
        lines.append(
            f"{r['milestone_id']:<28} "
            f"{'F' if r['frozen'] else '-':<1} "
            f"{built:<5} "
            f"{(r['unbound_count'] if r['unbound_count'] is not None else '-'):>3} "
            f"{'!' if r.get('consistency') else '-':<1} {gate_cell}"
        )
    s = report["summary"]
    lines.append("-" * len(header))
    lines.append(
        f"{s['n']} ms · built {s['n_built']} · frozen {s['n_frozen']} · "
        f"hard {s['n_hard_failed']} · soft {s['n_soft_failed']} · divergent {s['n_consistency_divergent']}"
    )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="m3 dashboard", description="Survey each milestone's latest state.")
    parser.add_argument("--workspace", default=None, help="Harness workspace directory")
    parser.add_argument(
        "--scope", default=None, help="Comma/space-separated milestone ids (default: frozen scope or dockerfiles/*)"
    )
    parser.add_argument(
        "--frozen-check",
        default=None,
        help="frozen_check report for consistency (default: _frozen_prefix.frozen_check_output)",
    )
    parser.add_argument("--output", type=Path, default=None, help="Output JSON path; defaults to stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        report, code = run_dashboard(
            args.workspace, scope=ctx.parse_id_list(args.scope) or None, frozen_check=args.frozen_check
        )
    except ctx.InputError as exc:
        ctx.emit({"verb": "dashboard", "errors": [str(exc)]}, args.output)
        return ctx.INPUT_INVALID
    print(_render_table(report), file=sys.stderr)
    ctx.emit(report, args.output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
