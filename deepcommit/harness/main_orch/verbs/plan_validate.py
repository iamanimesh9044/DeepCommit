"""plan-validate — validate a round's plan.yaml against the full plan_schema.md.

Checks: top-level round/started_at/summary/events; events[i].seq == i+1 (the
cross-file anchor); per-event required fields; final_ops keys are known op types
(each appears at most once — guaranteed by the YAML mapping); env_refine mode
validity; subagent_log filenames follow {type}_seq{NN}_* (NN numeric or preN);
and rejected_suggestions.yaml entry schema.

It does NOT re-run verify-dag cross-round (intermediate CSV states are gone by
round close). Instead, if a dag_update event recorded a verify-dag result it must
be passing; absence is a warning by default (the recording convention is Phase-2
wiring) or a violation under --require-verify-dag.

Exit: 0 valid · 2 violation · 1 input invalid.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any, Sequence

import yaml

from deepcommit.harness.main_orch import context as ctx
from deepcommit.harness.main_orch import invariants as inv

TOP_REQUIRED = ("round", "started_at", "summary", "events")
EVENT_REQUIRED = {
    "dag_update": ("final_ops", "m1_result"),
    "env_refine": ("milestones", "mode", "result"),
    "freeze_check": (
        "candidate_prefix",
        "script_gates_passed",
        "skill3_risk",
        "fork_decision",
        "decision_summary",
        "newly_frozen",
    ),
}
OP_TYPES = {
    "add_edge",
    "drop_edge",
    "reverse_edge",
    "reassign_commit",
    "drop_commit",
    "split_milestone",
    "merge_milestone",
    "drop_milestone",
}
ENV_MODES = {"agent", "validate_only", "regen_dockerfile", "surgical"}

# {type}_seq{NN}_{slug}.md where NN is numeric or the Step-1 "pre{N}" form (seqpre1)
_LOG_CONFORMING = re.compile(r"^(m1|skill1|skill2|skill3)_seq(pre)?\d+_.+\.md$")
_LOG_TYPED = re.compile(r"^(m1|skill1|skill2|skill3)_.+\.md$")

REJECTED_REQUIRED = ("claim", "reason", "verified_at", "decided_by")


def _read_plan(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ctx.InputError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ctx.InputError(f"plan.yaml is not a YAML mapping: {path}")
    return data


def _check_events(plan: dict, *, require_verify_dag: bool) -> tuple[list[str], list[str]]:
    violations: list[str] = []
    warnings: list[str] = []
    events = plan.get("events")
    if events is None:
        return violations, warnings  # "missing events" already flagged at top level
    if not isinstance(events, list):
        return ["events must be a list"], warnings

    violations += inv.seq_violations([e.get("seq") if isinstance(e, dict) else None for e in events])

    for i, event in enumerate(events):
        if not isinstance(event, dict):
            violations.append(f"events[{i}] is not a mapping")
            continue
        etype = event.get("event")
        if etype not in EVENT_REQUIRED:
            violations.append(f"events[{i}] unknown event type: {etype!r}")
            continue
        # A revert is a first-class dag_update that restores a checkpoint instead of
        # applying ops, so it requires `revert.restore_from` (not `final_ops`).
        is_revert = etype == "dag_update" and isinstance(event.get("revert"), dict)
        required = ("m1_result",) if is_revert else EVENT_REQUIRED[etype]
        for field in required:
            if field not in event:
                violations.append(f"events[{i}] ({etype}) missing required field: {field}")

        if etype == "dag_update" and is_revert:
            revert = event["revert"]
            if not revert.get("restore_from"):
                violations.append(f"events[{i}] (revert) missing required field: revert.restore_from")
            if event.get("final_ops"):
                violations.append(
                    f"events[{i}] (revert) must not carry final_ops "
                    "(a revert is a pure restore; restore-then-reapply uses a later event)"
                )
            reverts_seqs = revert.get("reverts_seqs")
            if reverts_seqs is not None and not isinstance(reverts_seqs, list):
                violations.append(f"events[{i}] (revert) reverts_seqs must be a list")
        elif etype == "dag_update":
            final_ops = event.get("final_ops")
            if isinstance(final_ops, dict):
                for op_type in final_ops:
                    if op_type not in OP_TYPES:
                        violations.append(f"events[{i}] final_ops has unknown op type: {op_type!r}")
            recorded = event.get("verify_dag")
            if recorded is not None:
                passed = recorded.get("passed") if isinstance(recorded, dict) else recorded
                if passed is not True:
                    violations.append(f"events[{i}] (dag_update) recorded a failing verify-dag result")
            elif require_verify_dag:
                violations.append(f"events[{i}] (dag_update) has no recorded verify-dag result")
            else:
                warnings.append(f"events[{i}] (dag_update) has no recorded verify-dag result (Phase-2 convention)")

        if etype == "env_refine":
            mode = event.get("mode")
            if mode is not None and mode not in ENV_MODES:
                violations.append(f"events[{i}] (env_refine) unknown mode: {mode!r}")
            if mode == "surgical" and "surgical_edits" not in event:
                warnings.append(f"events[{i}] (env_refine mode=surgical) has no surgical_edits")
            if mode == "agent" and "hints" not in event:
                warnings.append(f"events[{i}] (env_refine mode=agent) has no hints")

    return violations, warnings


def _check_subagent_logs(round_dir: Path) -> tuple[list[str], list[str]]:
    violations: list[str] = []
    warnings: list[str] = []
    log_dir = round_dir / "subagent_log"
    if not log_dir.is_dir():
        return violations, warnings
    for path in sorted(log_dir.iterdir()):
        if not path.is_file():
            continue
        if _LOG_CONFORMING.match(path.name):
            continue
        if _LOG_TYPED.match(path.name):
            warnings.append(f"subagent_log/{path.name} lacks a seq anchor ({{type}}_seq{{NN}}_*)")
        else:
            violations.append(f"subagent_log/{path.name} is not a valid {{type}}_seq{{NN}}_* log file")
    return violations, warnings


def _check_rejected_suggestions(round_dir: Path) -> list[str]:
    path = round_dir / "rejected_suggestions.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"rejected_suggestions.yaml invalid YAML: {exc}"]
    if data is None:
        return []
    if not isinstance(data, dict) or "rejected" not in data:
        return ["rejected_suggestions.yaml must be a mapping with a 'rejected' list"]
    rejected = data.get("rejected")
    if not isinstance(rejected, list):
        return ["rejected_suggestions.yaml 'rejected' must be a list"]
    violations: list[str] = []
    for i, entry in enumerate(rejected):
        if not isinstance(entry, dict):
            violations.append(f"rejected[{i}] is not a mapping")
            continue
        for field in REJECTED_REQUIRED:
            if field not in entry:
                violations.append(f"rejected[{i}] missing required field: {field}")
    return violations


def run_plan_validate(
    workspace: str | Path, round_n: int | None, *, require_verify_dag: bool = False
) -> tuple[dict[str, Any], int]:
    ws = ctx.resolve_workspace(workspace)
    if round_n is None:
        raise ctx.InputError("--round is required")
    round_dir = ws / "_orch_runs" / f"round_{round_n:03d}"
    plan_path = round_dir / "plan.yaml"
    if not plan_path.exists():
        raise ctx.InputError(f"plan.yaml not found for round {round_n}: {plan_path}")
    plan = _read_plan(plan_path)

    violations: list[str] = []
    warnings: list[str] = []

    for field in TOP_REQUIRED:
        if field not in plan:
            violations.append(f"missing top-level field: {field}")

    # Optional cumulative-verification anchor: the round-start checkpoint run id.
    if "baseline_run" in plan and not isinstance(plan["baseline_run"], str):
        violations.append("baseline_run must be a string (a checkpoint run id)")

    ev_violations, ev_warnings = _check_events(plan, require_verify_dag=require_verify_dag)
    violations += ev_violations
    warnings += ev_warnings

    log_violations, log_warnings = _check_subagent_logs(round_dir)
    violations += log_violations
    warnings += log_warnings

    violations += _check_rejected_suggestions(round_dir)

    report = {
        "verb": "plan-validate",
        "workspace": str(ws),
        "round": round_n,
        "plan": str(plan_path),
        "violations": violations,
        "warnings": warnings,
        "passed": not violations,
    }
    return report, (ctx.VIOLATION if violations else ctx.OK)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="m3 plan-validate", description="Validate a round's plan.yaml against the full schema."
    )
    parser.add_argument("--workspace", default=None, help="Harness workspace directory")
    parser.add_argument("--round", type=int, default=None, dest="round_n", help="Round number (NNN)")
    parser.add_argument(
        "--require-verify-dag",
        action="store_true",
        help="Require each dag_update event to record a passing verify-dag result (Phase-2)",
    )
    parser.add_argument("--output", type=Path, default=None, help="Output JSON path; defaults to stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        report, code = run_plan_validate(args.workspace, args.round_n, require_verify_dag=args.require_verify_dag)
    except ctx.InputError as exc:
        ctx.emit({"verb": "plan-validate", "errors": [str(exc)], "passed": False}, args.output)
        return ctx.INPUT_INVALID
    ctx.emit(report, args.output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
