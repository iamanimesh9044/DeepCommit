"""Shared synthetic-workspace builders for m3 verb-suite tests.

Mirrors the real on-disk shapes confirmed from
DATA/harness_workspace/django_django_4.2_5.2/v1_002_refactor_1:
  * dockerfiles/{MID}/build_status.json  (built, failed_gates, the 9 H gates,
    raw_metrics.unbound_issues_count / errors.lint_warnings_count, artifact_paths.latest_attempt)
  * dockerfiles/{MID}/module2_waivers.json  (bound_waivers: id/reason_code/anchor/attached_gates)
  * test_results/{MID}/refinement/{latest_attempt}/classification.json
  * _build_runs/{MID}/attempt_N/
  * _frozen_prefix.json  (frozen_milestones / scope_milestones / history)
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

H_GATES = [
    "H_patched_completeness",
    "H_patched_clean_signal",
    "H_pass_to_pass_majority",
    "H_total_not_below_base",
    "H_f2f_not_above_base",
    "H_error_within_budget",
    "H_s2s_not_above_base",
    "H_anomaly_aggregate",
    "H_none_error_fail_to_pass_present",
]


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def base_workspace(tmp_path: Path, mids=("M1", "M2"), edges=()) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    lines = ["id,commits"]
    for i, mid in enumerate(mids):
        lines.append(f"{mid},c{i}")
    (ws / "milestones.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    dep_lines = ["prerequisite_id,dependant_id"]
    for u, v in edges:
        dep_lines.append(f"{u},{v}")
    (ws / "dependencies.csv").write_text("\n".join(dep_lines) + "\n", encoding="utf-8")
    return ws


def write_build_status(
    ws: Path,
    mid: str,
    *,
    built: bool = True,
    failed_gates=(),
    gate_overrides=None,
    unbound: int = 0,
    lint: int = 0,
    latest_attempt: str = "attempt_1",
    tests_ran: bool = True,
    soft_warnings=(),
    inputs=None,
) -> Path:
    """Write a realistic build_status.json. ``gate_overrides`` maps gate -> (passed, value)."""
    overrides = gate_overrides or {}
    quality = {}
    for gate in H_GATES:
        passed, value = overrides.get(gate, (gate not in failed_gates, 1.0))
        quality[gate] = {"passed": passed, "value": value, "threshold": 1.0, "evidence": {}}
    payload = {
        "schema_version": 1,
        "milestone_id": mid,
        "built": built,
        "failed_gates": list(failed_gates),
        "build_run_integrity": {
            "B_tests_ran_successfully": {"passed": tests_ran, "evidence": {}},
        },
        "test_collection_and_result_quality": quality,
        "raw_metrics": {
            "unbound_issues_count": unbound,
            "errors": {"lint_warnings_count": lint},
            "soft_warnings": list(soft_warnings),
        },
        "artifact_paths": {"latest_attempt": latest_attempt},
    }
    if inputs is not None:
        payload["inputs"] = dict(inputs)
    path = ws / "dockerfiles" / mid / "build_status.json"
    _write_json(path, payload)
    return path


def write_recipe(ws: Path, mid: str, *, dockerfile: str = "FROM scratch\n", test_config=None) -> Path:
    """Write the recipe deliverables preflight C2 requires: Dockerfile + test_config.json."""
    base = ws / "dockerfiles" / mid
    base.mkdir(parents=True, exist_ok=True)
    (base / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    _write_json(base / "test_config.json", test_config or {"framework": "pytest"})
    return base


def write_frozen_check_report(
    ws: Path, milestones: dict, *, round_label: str = "round_009", name: str = "frozen_check.json", slim: bool = False
) -> Path:
    """Write a frozen_check report. ``milestones`` maps mid -> {gate: real_value}.

    ``slim=False`` → legacy per-MS shape ``{gate: {real_value: v}}``.
    ``slim=True``  → production slim shape ``{latest_attempt, real_value: {gate: v}, real_passed: {...}}``.
    """
    if slim:
        ms = {
            mid: {"latest_attempt": "attempt_1", "real_value": dict(gates), "real_passed": {g: True for g in gates}}
            for mid, gates in milestones.items()
        }
    else:
        ms = {mid: {gate: {"real_value": val} for gate, val in gates.items()} for mid, gates in milestones.items()}
    path = ws / "_orch_runs" / round_label / name
    _write_json(path, {"overall_passed": True, "is_dag_closed": True, "milestones": ms})
    return path


def write_m1_run(ws: Path, run: str = "run_001", *, failed=()) -> Path:
    """A passing _testbed_runs/run_NNN with build_report + archived CSVs matching current (passes preflight C5)."""
    d = ws / "_testbed_runs" / run
    d.mkdir(parents=True, exist_ok=True)
    _write_json(d / "build_report.json", {"failed_milestones": list(failed), "milestones": {}, "summary": {}})
    for csv in ("milestones.csv", "dependencies.csv"):
        if (ws / csv).exists():
            shutil.copy2(ws / csv, d / csv)
    return d


def write_fresh_recipe_and_status(ws: Path, mid: str, *, built: bool = True, **bs_kwargs) -> Path:
    """Recipe files + a build_status whose `inputs` hashes MATCH them + classification —
    i.e. a milestone that passes preflight C1 (freshness) / C2 (structural) / C3 (pointer)."""
    base = ws / "dockerfiles" / mid
    base.mkdir(parents=True, exist_ok=True)
    files = {
        "Dockerfile": f"FROM scratch\n# {mid}\n",
        "test_config.json": "{}\n",
        "module2_waivers.json": '{"bound_waivers": []}\n',
        "module2_signals.json": "{}\n",
    }
    for name, content in files.items():
        (base / name).write_text(content, encoding="utf-8")
    inputs = {
        "dockerfile_sha256": hashlib.sha256(files["Dockerfile"].encode()).hexdigest(),
        "test_config_sha256": hashlib.sha256(files["test_config.json"].encode()).hexdigest(),
        "module2_waivers_sha256": hashlib.sha256(files["module2_waivers.json"].encode()).hexdigest(),
        "module2_signals_sha256": hashlib.sha256(files["module2_signals.json"].encode()).hexdigest(),
    }
    latest = bs_kwargs.pop("latest_attempt", "attempt_1")
    write_build_status(ws, mid, built=built, inputs=inputs, latest_attempt=latest, **bs_kwargs)
    write_classification(ws, mid, latest_attempt=latest)
    return base


def write_frozen_check_full(
    ws: Path,
    candidate,
    scope=None,
    *,
    passed: bool = True,
    closed: bool = True,
    round_label: str = "round_003",
    name: str = "frozen_check.json",
    baseline_source: str = "test_scope",
    milestones=None,
    generated_at: str | None = None,
) -> Path:
    """Write a full frozen_check report (candidate_prefix / scope / gates verdict)."""
    payload: dict[str, Any] = {
        "candidate_prefix": list(candidate),
        "scope": {"scope_milestones": list(scope)} if scope is not None else None,
        "is_dag_closed": closed,
        "overall_passed": passed,
        "phase2_baseline": {"source": baseline_source},
        "milestones": dict(milestones) if milestones else {},
    }
    if generated_at is not None:
        payload["generated_at"] = generated_at
    path = ws / "_orch_runs" / round_label / name
    _write_json(path, payload)
    return path


def write_tracker(ws: Path, mid: str, *, effective: int, collected: int) -> Path:
    path = ws / "milestone_patched_tests" / f"{mid}.json"
    _write_json(path, {"summary": {"effective_tests": effective, "patched_collected": collected}})
    return path


def write_build_report(ws: Path, run: str = "run_001", *, failed=()) -> Path:
    path = ws / "_testbed_runs" / run / "build_report.json"
    _write_json(path, {"failed_milestones": list(failed), "milestones": {}, "summary": {}})
    return path


def write_waivers(ws: Path, mid: str, bound_waivers=()) -> Path:
    path = ws / "dockerfiles" / mid / "module2_waivers.json"
    _write_json(path, {"schema_version": 3, "milestone_id": mid, "bound_waivers": list(bound_waivers)})
    return path


def valid_bound_waiver(wid: int = 1) -> dict:
    return {
        "id": wid,
        "reason_code": "feature_introduced_later",
        "anchor": "AttributeError: ...",
        "symbol": "ShowFacets",
        "path": "django/contrib/admin/options.py",
        "attached_gates": ["H_error_within_budget"],
        "rationale": "verified via sandbox cherry-pick",
    }


def minimal_bound_waiver() -> dict:
    """A real-corpus shape: only the universal fields (no id / attached_gates)."""
    return {
        "reason_code": "feature_introduced_later",
        "anchor": "AttributeError: ...",
        "symbol": "ShowFacets",
        "path": "django/contrib/admin/options.py",
        "rationale": "verified via sandbox cherry-pick",
    }


def write_classification(
    ws: Path, mid: str, latest_attempt: str = "attempt_1", *, present: bool = True, summary=None
) -> Path:
    path = ws / "test_results" / mid / "refinement" / latest_attempt / "classification.json"
    if not present:
        return path
    _write_json(
        path,
        {
            "summary": summary
            or {
                "fail_to_pass": 1,
                "none_to_pass": 0,
                "error_to_pass": 0,
                "pass_to_fail": 0,
                "total_before": 10,
                "total_after": 10,
            }
        },
    )
    return path


def make_attempts(ws: Path, mid: str, n: int) -> None:
    for i in range(1, n + 1):
        (ws / "_build_runs" / mid / f"attempt_{i}" / "after_validation").mkdir(parents=True, exist_ok=True)


def write_build_run_archive(ws: Path, mid: str, attempt: int, *, built: bool = True) -> Path:
    """Write _build_runs/{mid}/attempt_N/after_validation/build_status.json (the
    last-known-good archive guard points restores at)."""
    path = ws / "_build_runs" / mid / f"attempt_{attempt}" / "after_validation" / "build_status.json"
    _write_json(path, {"milestone_id": mid, "built": built, "failed_gates": [] if built else ["H_error_within_budget"]})
    return path


def write_plan(ws: Path, round_n: int, plan) -> Path:
    import yaml as _yaml  # type: ignore[import-untyped]

    path = ws / "_orch_runs" / f"round_{round_n:03d}" / "plan.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = plan if isinstance(plan, str) else _yaml.safe_dump(plan, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return path


def write_subagent_log(ws: Path, round_n: int, name: str, text: str = "log\n") -> Path:
    path = ws / "_orch_runs" / f"round_{round_n:03d}" / "subagent_log" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_rejected(ws: Path, round_n: int, obj) -> Path:
    import yaml as _yaml  # type: ignore[import-untyped]

    path = ws / "_orch_runs" / f"round_{round_n:03d}" / "rejected_suggestions.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = obj if isinstance(obj, str) else _yaml.safe_dump(obj, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return path


def write_frozen_prefix(ws: Path, frozen=(), scope=None, round_n: int = 1, history=None, checkpoints=None) -> Path:
    path = ws / "_frozen_prefix.json"
    data = {
        "schema_version": 1,
        "frozen_milestones": list(frozen),
        "frozen_round": round_n,
        "frozen_round_label": f"round_{round_n:03d}",
        "scope_milestones": list(scope) if scope is not None else list(frozen),
        "phase2_baseline_source": "test",
        "history": history if history is not None else [],
    }
    if checkpoints is not None:
        data["frozen_checkpoints"] = checkpoints
    _write_json(path, data)
    return path
