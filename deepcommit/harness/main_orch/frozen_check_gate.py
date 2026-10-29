"""Frozen-prefix hard gate aggregation for Module 3.

This script computes the objective part of the M3 Frozen Check: aggregate hard
gate values for a candidate DAG-closed prefix, with the M2 signal gates derived
from raw artifacts instead of waiver-adjusted build_status verdicts.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from deepcommit.harness.test_runner.core.classifier import CLASSIFICATION_CATEGORIES
from deepcommit.harness.utils.xrepo_gates import compute_xrepo_gates

DEFAULT_GATE_CONFIG_PATH = Path(__file__).with_name("gate_config.yaml")
TRANSITION_KEYS = tuple(CLASSIFICATION_CATEGORIES)
RAW_M2_SIGNAL_THRESHOLDS: dict[str, float] = {
    "H_patched_completeness": 1.00,
    "H_none_error_fail_to_pass_present": 1.00,
    "H_anomaly_aggregate": 0.02,
}
UPPER_BOUND_GATES = frozenset(
    {
        "milestone_count_delta",
        "loc_cv",
    }
)
ANOMALY_TRANSITION_KEYS = (
    "pass_to_fail",
    "pass_to_skipped",
    # skipped_to_pass is NOT an anomaly: it is a src-driven positive signal (START
    # skips via @skipUnlessDBFeature because the milestone's feature is absent, END
    # passes once the milestone's src adds the feature) — the structural twin of
    # error_to_pass. It is credited in H_none_error_fail_to_pass_present + the clean
    # numerator, so it must not also be penalised here.
    "fail_to_skipped",
    "skipped_to_fail",
    "none_to_fail",
    "none_to_skipped",
    "pass_to_none",
    "fail_to_none",
    "skipped_to_none",
)


class FrozenCheckInputError(ValueError):
    """Raised when the requested frozen check cannot be evaluated."""


@dataclass(frozen=True)
class Phase2Baseline:
    total_commits: int
    total_src_loc: int
    total_milestones: int
    source: str = "input"

    def as_report(self) -> dict[str, Any]:
        return {
            "total_commits": self.total_commits,
            "total_src_loc": self.total_src_loc,
            "total_milestones": self.total_milestones,
            "source": self.source,
        }


@dataclass(frozen=True)
class GateConfig:
    thresholds: dict[str, float]
    source_path: str

    def as_report(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "thresholds": dict(self.thresholds),
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return default
    return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return float(text)
        except ValueError:
            return default
    return default


def _read_yaml(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError as exc:
        raise FrozenCheckInputError(f"YAML file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise FrozenCheckInputError(f"Invalid YAML in {path}: {exc}") from exc


def _read_gate_config_yaml(path: Path) -> dict[str, Any]:
    data = _read_yaml(path)
    if not isinstance(data, dict):
        raise FrozenCheckInputError(f"gate config must be a YAML object: {path}")
    return data


def _float_config_map(raw: Any, *, section: str) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        raise FrozenCheckInputError(f"gate config section {section!r} must be a mapping")
    return {str(key): _as_float(value) for key, value in raw.items()}


def load_gate_config(path: Path | None = None) -> GateConfig:
    default_data = _read_gate_config_yaml(DEFAULT_GATE_CONFIG_PATH)
    override_data = _read_gate_config_yaml(path) if path else {}

    thresholds = _float_config_map(default_data.get("thresholds", {}), section="thresholds")

    raw_thresholds = override_data.get("thresholds", override_data)
    if isinstance(raw_thresholds, Mapping):
        for key, value in raw_thresholds.items():
            key = str(key)
            if key in thresholds:
                thresholds[key] = _as_float(value, thresholds[key])

    source_path = str(path) if path else str(DEFAULT_GATE_CONFIG_PATH)
    return GateConfig(
        thresholds=thresholds,
        source_path=source_path,
    )


DEFAULT_GATE_CONFIG = load_gate_config()
DEFAULT_THRESHOLDS = DEFAULT_GATE_CONFIG.thresholds


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as exc:
        raise FrozenCheckInputError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FrozenCheckInputError(f"Invalid JSON in {path}: {exc}") from exc


def _safe_read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = _read_json(path)
    except FrozenCheckInputError as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, f"JSON file must contain an object: {path}"
    return data, None


def _write_report(report: Mapping[str, Any], output: Path | None) -> None:
    text = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False)
    if output is None:
        print(text)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text + "\n", encoding="utf-8")


def _parse_candidate_prefix(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, Sequence):
        items = [str(part).strip() for part in value]
    else:
        raise FrozenCheckInputError("candidate_prefix must be a list or comma-separated string")

    candidate = [item for item in items if item]
    if not candidate:
        raise FrozenCheckInputError("candidate_prefix must not be empty")
    duplicates = sorted({item for item in candidate if candidate.count(item) > 1})
    if duplicates:
        raise FrozenCheckInputError(f"candidate_prefix contains duplicate milestone ids: {duplicates}")
    return candidate


def _split_commits(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(";") if part.strip()]


def load_milestones(milestones_csv: Path) -> dict[str, dict[str, Any]]:
    if not milestones_csv.exists():
        raise FrozenCheckInputError(f"milestones.csv not found: {milestones_csv}")

    rows: dict[str, dict[str, Any]] = {}
    with milestones_csv.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "id" not in reader.fieldnames:
            raise FrozenCheckInputError(f"milestones.csv missing required id column: {milestones_csv}")
        for row in reader:
            mid = (row.get("id") or "").strip()
            if not mid:
                continue
            rows[mid] = dict(row)
    if not rows:
        raise FrozenCheckInputError(f"milestones.csv has no milestone rows: {milestones_csv}")
    return rows


def load_dependency_edges(dependencies_csv: Path) -> list[tuple[str, str]]:
    """Load (prerequisite, dependant) edges from dependencies.csv.

    Supports two column conventions:
    - NEW: prerequisite_id, dependant_id  (canonical)
    - LEGACY: source_id, target_id        (old header — same direction,
                                          source=prerequisite, target=dependant)
    """
    if not dependencies_csv.exists():
        raise FrozenCheckInputError(f"dependencies.csv not found: {dependencies_csv}")

    edges: list[tuple[str, str]] = []
    with dependencies_csv.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        header = set(reader.fieldnames or [])
        if {"prerequisite_id", "dependant_id"}.issubset(header):
            prereq_col, dep_col = "prerequisite_id", "dependant_id"
        elif {"source_id", "target_id"}.issubset(header):
            prereq_col, dep_col = "source_id", "target_id"
        else:
            raise FrozenCheckInputError(
                f"dependencies.csv missing required columns "
                f"(prerequisite_id/dependant_id or legacy source_id/target_id): {dependencies_csv}"
            )
        for row in reader:
            prereq = (row.get(prereq_col) or "").strip()
            dep = (row.get(dep_col) or "").strip()
            if prereq and dep:
                edges.append((prereq, dep))
    return edges


def infer_phase2_baseline(
    milestones: Mapping[str, Mapping[str, Any]],
    *,
    scope: Sequence[str] | None = None,
) -> Phase2Baseline:
    """Infer phase-2 baseline from milestones.csv rows.

    If `scope` is provided, baseline is restricted to those milestone ids.
    Caller must ensure `scope` is DAG-closed (validated separately in
    `run_frozen_check`). Otherwise baseline covers the entire csv (production
    full-DAG mode).
    """
    if scope:
        in_scope_ids = list(scope)
        source = f"scope_milestones({len(in_scope_ids)}_milestones)"
    else:
        in_scope_ids = list(milestones)
        source = "workspace_milestones_csv_full"

    commits: set[str] = set()
    src_loc = 0
    for mid in in_scope_ids:
        row = milestones[mid]
        commits.update(_split_commits(str(row.get("commits", ""))))
        src_loc += _as_int(row.get("src_loc", row.get("loc", 0)))
    return Phase2Baseline(
        total_commits=len(commits),
        total_src_loc=src_loc,
        total_milestones=len(in_scope_ids),
        source=source,
    )


def parse_phase2_baseline(raw: Mapping[str, Any] | None, *, source: str = "input") -> Phase2Baseline | None:
    if raw is None:
        return None
    missing = [key for key in ("total_commits", "total_src_loc", "total_milestones") if key not in raw]
    if missing:
        raise FrozenCheckInputError(f"phase2_baseline missing required keys: {missing}")

    baseline = Phase2Baseline(
        total_commits=_as_int(raw.get("total_commits")),
        total_src_loc=_as_int(raw.get("total_src_loc")),
        total_milestones=_as_int(raw.get("total_milestones")),
        source=source,
    )
    if baseline.total_commits <= 0:
        raise FrozenCheckInputError("phase2_baseline.total_commits must be > 0")
    if baseline.total_src_loc <= 0:
        raise FrozenCheckInputError("phase2_baseline.total_src_loc must be > 0")
    if baseline.total_milestones <= 0:
        raise FrozenCheckInputError("phase2_baseline.total_milestones must be > 0")
    return baseline


def check_dag_closed(candidate_prefix: Sequence[str], edges: Iterable[tuple[str, str]]) -> dict[str, Any]:
    candidate = set(candidate_prefix)
    parents_by_child: dict[str, set[str]] = defaultdict(set)
    for source, target in edges:
        parents_by_child[target].add(source)

    missing_by_milestone: dict[str, list[str]] = {}
    for mid in candidate_prefix:
        ancestors: set[str] = set()
        stack = list(parents_by_child.get(mid, set()))
        while stack:
            parent = stack.pop()
            if parent in ancestors:
                continue
            ancestors.add(parent)
            stack.extend(parents_by_child.get(parent, set()))
        missing = sorted(ancestors - candidate)
        if missing:
            missing_by_milestone[mid] = missing

    return {
        "is_dag_closed": not missing_by_milestone,
        "missing_ancestors_by_milestone": missing_by_milestone,
    }


def _classification_summary(classification: Mapping[str, Any] | None) -> dict[str, int]:
    raw = classification.get("summary", {}) if classification else {}
    if not isinstance(raw, Mapping):
        raw = {}
    keys = set(TRANSITION_KEYS + ("total_before", "total_after"))
    return {key: _as_int(raw.get(key, 0)) for key in keys}


def _quality_gates(build_status: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not build_status:
        return {}
    quality = build_status.get("test_collection_and_result_quality", {})
    return quality if isinstance(quality, Mapping) else {}


# Transitional read-compat: H_none_error_fail_to_pass_present was renamed from the
# former H_fail_to_pass_present. build_status.json artifacts written before the rename
# still carry the old key; read either so the rename does not blank the with-waiver
# value for milestones that have not been re-validated yet.
_GATE_NAME_READ_ALIASES = {
    "H_none_error_fail_to_pass_present": ("H_fail_to_pass_present",),
}


def _build_status_gate(
    build_status: Mapping[str, Any] | None,
    gate_name: str,
) -> tuple[bool | None, float | int | None, float | int | None]:
    quality = _quality_gates(build_status)
    gate = quality.get(gate_name)
    if not isinstance(gate, Mapping):
        for alias in _GATE_NAME_READ_ALIASES.get(gate_name, ()):
            alias_gate = quality.get(alias)
            if isinstance(alias_gate, Mapping):
                gate = alias_gate
                break
    if not isinstance(gate, Mapping):
        return None, None, None
    passed = gate.get("passed")
    passed_bool = passed if isinstance(passed, bool) else None
    value = gate.get("value")
    threshold = gate.get("threshold")
    return (
        passed_bool,
        value if isinstance(value, (int, float)) else None,
        threshold if isinstance(threshold, (int, float)) else None,
    )


def _latest_attempt_from_build_status(build_status: Mapping[str, Any] | None) -> str | None:
    if not build_status:
        return None
    artifact_paths = build_status.get("artifact_paths", {})
    if not isinstance(artifact_paths, Mapping):
        return None
    latest_attempt = artifact_paths.get("latest_attempt")
    return str(latest_attempt) if latest_attempt else None


def _patched_counts_from_tracker(
    workspace: Path,
    milestone_id: str,
    build_status: Mapping[str, Any] | None,
) -> tuple[int | None, int | None, str | None, str]:
    tracker_path = workspace / "milestone_patched_tests" / f"{milestone_id}.json"
    tracker, tracker_error = _safe_read_json(tracker_path)
    if tracker_error is None and tracker is not None:
        summary = tracker.get("summary", {})
        if isinstance(summary, Mapping):
            return (
                _as_int(summary.get("effective_tests")),
                _as_int(summary.get("patched_collected")),
                None,
                str(tracker_path),
            )

    gate = _quality_gates(build_status).get("H_patched_completeness")
    if isinstance(gate, Mapping):
        evidence = gate.get("evidence", {})
        if isinstance(evidence, Mapping):
            return (
                _as_int(evidence.get("effective")),
                _as_int(evidence.get("collected")),
                tracker_error,
                "build_status.evidence_fallback",
            )

    return None, None, tracker_error, str(tracker_path)


def _waiver_summary(workspace: Path, milestone_id: str) -> dict[str, Any]:
    path = workspace / "dockerfiles" / milestone_id / "module2_waivers.json"
    waivers, error = _safe_read_json(path)
    if error is not None:
        return {
            "path": str(path),
            "present": False,
            "bound_waivers_count": 0,
            "legacy_waived_count": 0,
            "error": None if not path.exists() else error,
        }

    bound = waivers.get("bound_waivers", []) if waivers else []
    legacy = waivers.get("waived", []) if waivers else []
    return {
        "path": str(path),
        "present": True,
        "bound_waivers_count": len(bound) if isinstance(bound, list) else 0,
        "legacy_waived_count": len(legacy) if isinstance(legacy, list) else 0,
        "error": None,
    }


def analyze_milestone_raw_signals(
    workspace: Path,
    milestone_id: str,
    m2_gate_thresholds: Mapping[str, float],
    anomaly_transition_keys: Sequence[str],
) -> dict[str, Any]:
    build_status_path = workspace / "dockerfiles" / milestone_id / "build_status.json"
    build_status, build_status_error = _safe_read_json(build_status_path)
    latest_attempt = _latest_attempt_from_build_status(build_status)

    classification_path: Path | None = None
    classification: dict[str, Any] | None = None
    classification_error: str | None = None
    if latest_attempt:
        classification_path = (
            workspace / "test_results" / milestone_id / "refinement" / latest_attempt / "classification.json"
        )
        classification, classification_error = _safe_read_json(classification_path)
    else:
        classification_error = "build_status.artifact_paths.latest_attempt is missing"

    class_summary = _classification_summary(classification)

    effective, collected, patched_error, patched_source = _patched_counts_from_tracker(
        workspace,
        milestone_id,
        build_status,
    )
    patched_threshold = _as_float(m2_gate_thresholds.get("H_patched_completeness"), 1.0)
    if effective is None or collected is None:
        patched_value = None
        patched_passed = False
    elif effective == 0:
        patched_value = 1.0
        patched_passed = patched_error is None
    else:
        patched_value = collected / effective
        patched_passed = patched_error is None and patched_value >= patched_threshold

    f2p_count = _as_int(class_summary.get("fail_to_pass", 0))
    n2p_count = _as_int(class_summary.get("none_to_pass", 0))
    e2p_count = _as_int(class_summary.get("error_to_pass", 0))
    s2p_count = _as_int(class_summary.get("skipped_to_pass", 0))
    f2p_n2p_total = f2p_count + n2p_count + e2p_count + s2p_count
    f2p_threshold = _as_float(m2_gate_thresholds.get("H_none_error_fail_to_pass_present"), 1.0)
    f2p_passed = classification_error is None and f2p_n2p_total >= f2p_threshold

    total_before = _as_int(class_summary.get("total_before", 0))
    total_after = _as_int(class_summary.get("total_after", 0))
    anomaly_count = sum(_as_int(class_summary.get(key, 0)) for key in anomaly_transition_keys)
    anomaly_total = max(total_before, total_after)
    anomaly_threshold = _as_float(m2_gate_thresholds.get("H_anomaly_aggregate"), 0.02)
    anomaly_value = (anomaly_count / anomaly_total) if anomaly_total else None
    anomaly_passed = classification_error is None and anomaly_value is not None and anomaly_value <= anomaly_threshold

    patched_with_waiver = _build_status_gate(build_status, "H_patched_completeness")
    f2p_with_waiver = _build_status_gate(build_status, "H_none_error_fail_to_pass_present")
    anomaly_with_waiver = _build_status_gate(build_status, "H_anomaly_aggregate")

    return {
        "build_status_path": str(build_status_path),
        "latest_attempt": latest_attempt,
        "classification_path": str(classification_path) if classification_path else None,
        "errors": [err for err in (build_status_error, classification_error, patched_error) if err],
        "waivers": _waiver_summary(workspace, milestone_id),
        "H_patched_completeness": {
            "real_value": _round(patched_value),
            "real_passed": patched_passed,
            "raw_effective": effective,
            "raw_collected": collected,
            "raw_threshold": patched_threshold,
            "patched_counts_source": patched_source,
            "value_with_waiver": _round(_as_float(patched_with_waiver[1]))
            if patched_with_waiver[1] is not None
            else None,
            "passed_with_waiver": patched_with_waiver[0],
            "threshold_with_waiver": patched_with_waiver[2],
        },
        "H_none_error_fail_to_pass_present": {
            "real_value": f2p_n2p_total,
            "real_passed": f2p_passed,
            "fail_to_pass": f2p_count,
            "none_to_pass": n2p_count,
            "error_to_pass": e2p_count,
            "skipped_to_pass": s2p_count,
            "raw_threshold": f2p_threshold,
            "value_with_waiver": f2p_with_waiver[1],
            "passed_with_waiver": f2p_with_waiver[0],
            "threshold_with_waiver": f2p_with_waiver[2],
        },
        "H_anomaly_aggregate": {
            "real_value": _round(anomaly_value),
            "real_passed": anomaly_passed,
            "anomaly_count": anomaly_count,
            "anomaly_total": anomaly_total,
            "raw_threshold": anomaly_threshold,
            "denominator": "max(total_before,total_after)",
            "value_with_waiver": _round(_as_float(anomaly_with_waiver[1]))
            if anomaly_with_waiver[1] is not None
            else None,
            "passed_with_waiver": anomaly_with_waiver[0],
            "threshold_with_waiver": anomaly_with_waiver[2],
        },
    }


_SIGNAL_GATES = ("H_patched_completeness", "H_none_error_fail_to_pass_present", "H_anomaly_aggregate")


def _slim_milestone_report(full: Mapping[str, Any]) -> dict[str, Any]:
    """Project the rich per-MS analysis to the freeze index + recomputed values.

    The full dict (paths, evidence, waivers, with-waiver sub-values) duplicates
    ``dockerfiles/{MID}/build_status.json`` and is what dashboard surfaces; the
    freeze report keeps only ``latest_attempt`` (the pin index) and the recomputed
    ``real_value`` / ``real_passed`` per signal gate (the freeze-authoritative
    numbers dashboard's consistency check reconciles against).
    """
    return {
        "latest_attempt": full.get("latest_attempt"),
        "real_value": {g: full[g]["real_value"] for g in _SIGNAL_GATES if isinstance(full.get(g), dict)},
        "real_passed": {g: full[g]["real_passed"] for g in _SIGNAL_GATES if isinstance(full.get(g), dict)},
    }


def _quantity_gate(
    name: str,
    value: float,
    threshold: float,
    evidence: Mapping[str, Any],
    upper_bound_gates: frozenset[str],
) -> dict[str, Any]:
    passed = value <= threshold if name in upper_bound_gates else value >= threshold
    return {
        "value": _round(value),
        "threshold": threshold,
        "passed": passed,
        "direction": "<=" if name in upper_bound_gates else ">=",
        "evidence": dict(evidence),
    }


def compute_quantity_gates(
    milestones: Mapping[str, Mapping[str, Any]],
    candidate_prefix: Sequence[str],
    baseline: Phase2Baseline,
    thresholds: Mapping[str, float],
    upper_bound_gates: frozenset[str],
    *,
    total_milestones_in_csv: int | None = None,
) -> dict[str, Any]:
    candidate_commits: set[str] = set()
    src_locs: list[int] = []
    for mid in candidate_prefix:
        row = milestones[mid]
        candidate_commits.update(_split_commits(str(row.get("commits", ""))))
        src_locs.append(_as_int(row.get("src_loc", row.get("loc", 0))))

    candidate_src_loc = sum(src_locs)
    loc_mean = statistics.mean(src_locs) if src_locs else 0.0
    loc_cv = (statistics.pstdev(src_locs) / loc_mean) if loc_mean else 0.0

    return {
        "commit_retention_rate": _quantity_gate(
            "commit_retention_rate",
            _ratio(len(candidate_commits), baseline.total_commits),
            thresholds["commit_retention_rate"],
            {
                "candidate_commits": len(candidate_commits),
                "baseline_total_commits": baseline.total_commits,
            },
            upper_bound_gates,
        ),
        "loc_retention_rate": _quantity_gate(
            "loc_retention_rate",
            _ratio(candidate_src_loc, baseline.total_src_loc),
            thresholds["loc_retention_rate"],
            {
                "candidate_src_loc": candidate_src_loc,
                "baseline_total_src_loc": baseline.total_src_loc,
            },
            upper_bound_gates,
        ),
        "milestone_count_delta": _quantity_gate(
            "milestone_count_delta",
            (
                abs(len(candidate_prefix) - baseline.total_milestones)
                / (total_milestones_in_csv or baseline.total_milestones)
            ),
            thresholds["milestone_count_delta"],
            {
                "candidate_milestones": len(candidate_prefix),
                "baseline_scope_milestones": baseline.total_milestones,
                "csv_total_milestones": total_milestones_in_csv or baseline.total_milestones,
                "note": "delta = |candidate - scope| / total_csv (trial-wide denominator for stable anti-cheat across debug/production)",
            },
            upper_bound_gates,
        ),
        "loc_cv": _quantity_gate(
            "loc_cv",
            loc_cv,
            thresholds["loc_cv"],
            {
                "candidate_src_locs": src_locs,
                "mean_src_loc": _round(loc_mean),
            },
            upper_bound_gates,
        ),
    }


def _aggregate_with_waiver(
    milestone_reports: Mapping[str, Mapping[str, Any]],
    signal_key: str,
    aggregation_mode: str = "pass_count_ratio",
) -> tuple[float | None, dict[str, Any]]:
    """Aggregate waiver-adjusted signal. Same modes as _signal_gate.

    Returns (value, evidence). value is None if data unavailable.
    """
    unavailable = [
        mid for mid, report in milestone_reports.items() if report[signal_key].get("passed_with_waiver") is None
    ]
    if unavailable:
        return None, {
            "available": False,
            "unavailable_milestones": unavailable,
            "passed_milestones": None,
        }

    passed_milestones = [
        mid for mid, report in milestone_reports.items() if report[signal_key].get("passed_with_waiver") is True
    ]
    pass_count_ratio = _ratio(len(passed_milestones), len(milestone_reports))

    # Optional value-based mean (uses each milestone's value_with_waiver numeric value)
    value_mean: float | None = None
    if aggregation_mode == "value_mean":
        numeric_values: list[float] = []
        for _mid, report in milestone_reports.items():
            raw = report[signal_key].get("value_with_waiver")
            if isinstance(raw, (int, float)):
                numeric_values.append(float(raw))
        if numeric_values:
            value_mean = sum(numeric_values) / len(numeric_values)

    if aggregation_mode == "value_mean" and value_mean is not None:
        value = value_mean
    else:
        value = pass_count_ratio

    return value, {
        "available": True,
        "unavailable_milestones": [],
        "passed_milestones": passed_milestones,
        "pass_count_ratio": _round(pass_count_ratio),
        "value_mean": _round(value_mean) if value_mean is not None else None,
    }


def _signal_gate(
    output_name: str,
    signal_key: str,
    milestone_reports: Mapping[str, Mapping[str, Any]],
    threshold: float,
    aggregation_mode: str = "pass_count_ratio",
) -> dict[str, Any]:
    """Aggregate per-milestone signal into a frozen-prefix ratio.

    aggregation_mode:
      - "pass_count_ratio" (default): ratio = passing_milestones / total. Suited
        for boolean-style signals (e.g., F2P presence, anomaly threshold).
      - "value_mean": mean(real_value) across milestones. Suited for continuous
        0-1 signals where partial completeness counts (e.g., H_patched_completeness
        — milestone at 0.95 contributes 0.95 instead of being a hard 0).
    """
    real_passed_milestones = [
        mid for mid, report in milestone_reports.items() if report[signal_key].get("real_passed") is True
    ]
    real_failed_milestones = [mid for mid in milestone_reports if mid not in real_passed_milestones]
    # Count-based ratio (always computed for evidence)
    pass_count_ratio = _ratio(len(real_passed_milestones), len(milestone_reports))

    # Optional value-based mean
    value_mean: float | None = None
    if aggregation_mode == "value_mean":
        numeric_values: list[float] = []
        for _mid, report in milestone_reports.items():
            raw = report[signal_key].get("real_value")
            if isinstance(raw, (int, float)):
                numeric_values.append(float(raw))
        if numeric_values:
            value_mean = sum(numeric_values) / len(numeric_values)

    # Pick which one to compare against threshold
    if aggregation_mode == "value_mean" and value_mean is not None:
        value_real = value_mean
    else:
        value_real = pass_count_ratio

    value_with_waiver, with_waiver_evidence = _aggregate_with_waiver(
        milestone_reports, signal_key, aggregation_mode=aggregation_mode
    )

    gate = {
        "value_with_waiver": _round(value_with_waiver),
        "value_real": _round(value_real),
        "threshold": threshold,
        "passed": value_real >= threshold,
        "direction": ">=",
        "aggregation_mode": aggregation_mode,
        "evidence": {
            "milestones_total": len(milestone_reports),
            "real_passed_count": len(real_passed_milestones),
            "real_failed_count": len(real_failed_milestones),
            "real_passed_milestones": real_passed_milestones,
            "real_failed_milestones": real_failed_milestones,
            "pass_count_ratio": _round(pass_count_ratio),
            "value_mean": _round(value_mean) if value_mean is not None else None,
            "with_waiver": with_waiver_evidence,
        },
    }
    if real_failed_milestones:
        gate["note"] = (
            f"Raw evaluation failed for {len(real_failed_milestones)}/"
            f"{len(milestone_reports)} milestone(s) after ignoring waivers."
        )
    elif output_name and value_with_waiver is not None and value_with_waiver != value_real:
        gate["note"] = "Waiver-adjusted and raw pass ratios differ."
    return gate


def compute_signal_gates(
    milestone_reports: Mapping[str, Mapping[str, Any]],
    thresholds: Mapping[str, float],
) -> dict[str, Any]:
    return {
        "H_patched_completeness_ratio": _signal_gate(
            "H_patched_completeness_ratio",
            "H_patched_completeness",
            milestone_reports,
            thresholds["H_patched_completeness_ratio"],
            aggregation_mode="value_mean",  # continuous 0-1; partial completeness counts
        ),
        "H_none_error_fail_to_pass_present_ratio": _signal_gate(
            "H_none_error_fail_to_pass_present_ratio",
            "H_none_error_fail_to_pass_present",
            milestone_reports,
            thresholds["H_none_error_fail_to_pass_present_ratio"],
            # default pass_count_ratio: boolean (each milestone has F2P/N2P or not)
        ),
        "H_anomaly_within_budget_ratio": _signal_gate(
            "H_anomaly_within_budget_ratio",
            "H_anomaly_aggregate",
            milestone_reports,
            thresholds["H_anomaly_within_budget_ratio"],
            # default pass_count_ratio: each milestone's anomaly_ratio either ≤ threshold or not
        ),
    }


def resolve_graded_milestones(
    candidate: Sequence[str],
    selected_milestones: Sequence[str] | None,
    non_graded_milestones: Sequence[str] | None,
) -> dict[str, Any]:
    """Compute the graded subset of ``candidate`` after applying exclusion lists.

    - ``selected_milestones`` (whitelist): if given, only candidate ids also in
      this list stay graded. Use sparingly — to skip branches/standalone
      milestones that are simply too poor or too hard to repair.
    - ``non_graded_milestones`` (blacklist): always removed from grading. Use for
      milestones that still build and run but lack a correct evaluation
      environment or collected evaluation tests (so their signals would mislead),
      NOT for milestones whose quality is merely low.

    Exclusions only change which milestones the *signal-gate ratios* aggregate
    over. Every milestone stays in the DAG (it may be a prerequisite), the
    quantity gates still cover the full candidate, and the full-scope ratios are
    reported alongside for audit. Apply exclusions only AFTER a full-scope
    freeze — never to lift a pre-freeze ratio (that is gaming).
    """
    ng = {str(m).strip() for m in (non_graded_milestones or []) if str(m).strip()}
    sel = {str(m).strip() for m in selected_milestones if str(m).strip()} if selected_milestones is not None else None
    graded: list[str] = []
    for mid in candidate:
        if sel is not None and mid not in sel:
            continue
        if mid in ng:
            continue
        graded.append(mid)
    graded_set = set(graded)
    return {
        "graded": graded,
        "excluded": [m for m in candidate if m not in graded_set],
        "non_graded_applied": sorted(m for m in candidate if m in ng),
        "deselected": (sorted(m for m in candidate if m not in sel) if sel is not None else []),
        "exclusion_used": sel is not None or bool(ng),
    }


def run_frozen_check(
    *,
    workspace: Path,
    candidate_prefix: Sequence[str],
    phase2_baseline: Phase2Baseline | None,
    scope_milestones: Sequence[str] | None = None,
    selected_milestones: Sequence[str] | None = None,
    non_graded_milestones: Sequence[str] | None = None,
    gate_config: GateConfig | None = None,
    thresholds: Mapping[str, float] | None = None,
    m2_gate_thresholds: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    gate_config = gate_config or DEFAULT_GATE_CONFIG
    thresholds = dict(thresholds or gate_config.thresholds)
    m2_gate_thresholds = dict(m2_gate_thresholds or RAW_M2_SIGNAL_THRESHOLDS)
    candidate = _parse_candidate_prefix(candidate_prefix)

    milestones = load_milestones(workspace / "milestones.csv")
    missing_milestones = [mid for mid in candidate if mid not in milestones]
    if missing_milestones:
        raise FrozenCheckInputError(
            f"candidate_prefix has milestone ids missing from milestones.csv: {missing_milestones}"
        )

    edges = load_dependency_edges(workspace / "dependencies.csv")

    # Validate scope (if provided): scope must be DAG-closed, and candidate ⊆ scope.
    scope_report: dict[str, Any] | None = None
    scope_list: list[str] | None = None
    if scope_milestones:
        scope_list = [str(mid).strip() for mid in scope_milestones if str(mid).strip()]
        if not scope_list:
            raise FrozenCheckInputError("scope_milestones must not be empty if provided")
        duplicates = sorted({mid for mid in scope_list if scope_list.count(mid) > 1})
        if duplicates:
            raise FrozenCheckInputError(f"scope_milestones contains duplicate ids: {duplicates}")
        missing_from_csv = [mid for mid in scope_list if mid not in milestones]
        if missing_from_csv:
            raise FrozenCheckInputError(
                f"scope_milestones has milestone ids missing from milestones.csv: {missing_from_csv}"
            )
        scope_closure = check_dag_closed(scope_list, edges)
        scope_report = {
            "scope_milestones": scope_list,
            "is_dag_closed": scope_closure["is_dag_closed"],
            "missing_ancestors_by_milestone": scope_closure["missing_ancestors_by_milestone"],
        }
        if not scope_closure["is_dag_closed"]:
            # WARN, do not block — caller may want to see partial gate signals
            # even when scope is incomplete. Baseline over a non-closed subgraph
            # is interpretive, but the per-milestone signal aggregates remain valid.
            scope_report["warning"] = (
                "scope_milestones is not DAG-closed; baseline/ratio numbers are over "
                "a non-closed subgraph and should be interpreted as partial signal. "
                f"Missing ancestors: {scope_closure['missing_ancestors_by_milestone']}"
            )
        not_in_scope = [mid for mid in candidate if mid not in set(scope_list)]
        if not_in_scope:
            raise FrozenCheckInputError(f"candidate_prefix has milestone ids not in scope_milestones: {not_in_scope}")

    baseline = phase2_baseline or infer_phase2_baseline(
        milestones,
        scope=scope_list,
    )
    if baseline.total_commits <= 0 or baseline.total_src_loc <= 0 or baseline.total_milestones <= 0:
        raise FrozenCheckInputError("phase2_baseline values must all be > 0")

    closure = check_dag_closed(candidate, edges)

    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": _utc_now_iso(),
        "workspace": str(workspace),
        "candidate_prefix": candidate,
        "scope": scope_report,
        "is_dag_closed": closure["is_dag_closed"],
        "dag_closure": closure,
        "phase2_baseline": baseline.as_report(),
        "gate_config": gate_config.as_report(),
        "thresholds": dict(thresholds),
        "m2_gate_thresholds": dict(m2_gate_thresholds),
        "gates": {},
        "overall_passed": False,
        "failing_gates": [],
        "milestones": {},
        "errors": [],
    }

    if not closure["is_dag_closed"]:
        # WARN, do not block. Compute gates over the available milestones —
        # gate values become "partial" but they're still the most useful diagnostic
        # signal M3 has when scope is incomplete.
        report.setdefault("warnings", []).append(
            "candidate_prefix is not DAG-closed; gates computed over partial subgraph. "
            f"Missing ancestors: {closure.get('missing_ancestors_by_milestone')}"
        )

    milestone_reports = {
        mid: analyze_milestone_raw_signals(
            workspace,
            mid,
            m2_gate_thresholds,
            ANOMALY_TRANSITION_KEYS,
        )
        for mid in candidate
    }
    # Grading subset: exclusion lists (selected whitelist / non_graded blacklist)
    # narrow which milestones the *signal-gate ratios* aggregate over. Quantity
    # gates always cover the full candidate (exclusions are an evaluation choice,
    # not a structural one). The full-scope ratios are reported alongside so an
    # exclusion that inflates a ratio is always visible to an auditor.
    grade = resolve_graded_milestones(candidate, selected_milestones, non_graded_milestones)
    graded_set = set(grade["graded"])
    graded_reports = {mid: r for mid, r in milestone_reports.items() if mid in graded_set}
    if grade["exclusion_used"] and not graded_reports:
        raise FrozenCheckInputError("exclusion lists removed every candidate milestone from grading")

    gates = {}
    gates.update(
        compute_quantity_gates(
            milestones,
            candidate,
            baseline,
            thresholds,
            UPPER_BOUND_GATES,
            total_milestones_in_csv=len(milestones),
        )
    )
    gates.update(
        compute_signal_gates(
            graded_reports if grade["exclusion_used"] else milestone_reports,
            thresholds,
        )
    )

    # Workspace (multi-repo) milestones only: extra X_* gates; adds nothing when no milestone has a `repos` column value.
    gates.update(compute_xrepo_gates(workspace, milestones, candidate))

    failing_gates = [name for name, gate in gates.items() if not gate.get("passed")]
    report["gates"] = gates
    report["overall_passed"] = not failing_gates
    report["failing_gates"] = failing_gates
    report["milestones"] = {mid: _slim_milestone_report(r) for mid, r in milestone_reports.items()}
    report["grading"] = {
        "exclusion_used": grade["exclusion_used"],
        "candidate_count": len(candidate),
        "graded_count": len(grade["graded"]),
        "graded_milestones": grade["graded"],
        "excluded_from_grading": grade["excluded"],
        "non_graded": grade["non_graded_applied"],
        "deselected_by_whitelist": grade["deselected"],
    }
    if grade["exclusion_used"]:
        # Anti-cheat transparency: same signal gates over the FULL candidate, no
        # exclusions, so the audit can see exactly what each exclusion bought.
        report["signal_gates_full_scope"] = compute_signal_gates(milestone_reports, thresholds)
        report["grading"]["note"] = (
            "Signal-gate ratios in `gates` are over the GRADED subset; "
            "`signal_gates_full_scope` is the same gates over the full candidate "
            "(no exclusions) for audit. Exclusions are valid only as POST-freeze "
            "analysis on the already-frozen full scope — applying them to lift a "
            "pre-freeze ratio is gaming."
        )
    return report


def _input_data_from_args(args: argparse.Namespace) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if args.input:
        input_data = _read_json(args.input)
        if not isinstance(input_data, dict):
            raise FrozenCheckInputError(f"--input must contain a JSON object: {args.input}")
        data.update(input_data)

    if args.workspace:
        data["workspace"] = str(args.workspace)
    if args.candidate_prefix:
        data["candidate_prefix"] = args.candidate_prefix
    if args.scope_milestones:
        data["scope_milestones"] = args.scope_milestones

    baseline_data: dict[str, Any] | None = None
    if data.get("phase2_baseline") is not None:
        raw = data["phase2_baseline"]
        if not isinstance(raw, dict):
            raise FrozenCheckInputError("phase2_baseline must be a JSON object")
        baseline_data = dict(raw)
    if args.phase2_baseline:
        raw_baseline = _read_json(args.phase2_baseline)
        if not isinstance(raw_baseline, dict):
            raise FrozenCheckInputError(f"--phase2-baseline must contain a JSON object: {args.phase2_baseline}")
        baseline_data = dict(raw_baseline)

    cli_baseline = {
        "total_commits": args.phase2_total_commits,
        "total_src_loc": args.phase2_total_src_loc,
        "total_milestones": args.phase2_total_milestones,
    }
    provided_cli_keys = {key: value for key, value in cli_baseline.items() if value is not None}
    if provided_cli_keys:
        baseline_data = {**(baseline_data or {}), **provided_cli_keys}

    if baseline_data is not None:
        data["phase2_baseline"] = baseline_data

    return data


def _read_id_list_file(path: Path) -> list[str]:
    """Read a milestone-id list file (one id per line, ``#`` comments allowed)."""
    ids: list[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ids.append(line)
    return ids


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compute M3 frozen-prefix hard gate aggregates from raw M2 artifacts.",
    )
    parser.add_argument(
        "--input", type=Path, default=None, help="JSON input containing workspace/candidate_prefix/phase2_baseline"
    )
    parser.add_argument("--workspace", type=Path, default=None, help="Harness workspace directory")
    parser.add_argument("--candidate-prefix", default=None, help="Comma-separated DAG-closed milestone ids")
    parser.add_argument(
        "--scope-milestones",
        default=None,
        help="Comma-separated milestone ids declaring the M3 working set (fixture subgraph). "
        "Must be DAG-closed; candidate-prefix must be a subset. Baseline is computed from these. "
        "Omit to default to entire milestones.csv (production full-DAG mode).",
    )
    parser.add_argument(
        "--phase2-baseline",
        type=Path,
        default=None,
        help="JSON file with total_commits,total_src_loc,total_milestones (explicit override of scope-based baseline)",
    )
    parser.add_argument("--phase2-total-commits", type=int, default=None, help="Phase 2 baseline total commits")
    parser.add_argument("--phase2-total-src-loc", type=int, default=None, help="Phase 2 baseline total source LoC")
    parser.add_argument("--phase2-total-milestones", type=int, default=None, help="Phase 2 baseline total milestones")
    parser.add_argument(
        "--thresholds-config", type=Path, default=None, help="YAML file overriding aggregate and/or M2 gate thresholds"
    )
    parser.add_argument(
        "--selected-milestones-file",
        type=Path,
        default=None,
        help="Whitelist file (one milestone id per line): grade ONLY these candidate ids. "
        "Use sparingly for too-poor/too-hard branches. POST-freeze analysis only — "
        "applying it to lift a pre-freeze ratio is gaming.",
    )
    parser.add_argument(
        "--non-graded-milestones-file",
        type=Path,
        default=None,
        help="Blacklist file (one milestone id per line): still built/run but excluded "
        "from the signal-gate ratios (missing eval environment / no collected eval "
        "tests). POST-freeze analysis only.",
    )
    parser.add_argument("--output", type=Path, default=None, help="Output JSON path; defaults to stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        data = _input_data_from_args(args)
        if "workspace" not in data:
            raise FrozenCheckInputError("workspace is required via --workspace or --input")
        if "candidate_prefix" not in data:
            raise FrozenCheckInputError("candidate_prefix is required via --candidate-prefix or --input")

        gate_config = load_gate_config(args.thresholds_config)
        baseline = parse_phase2_baseline(data.get("phase2_baseline"), source="input")
        scope_raw = data.get("scope_milestones")
        if isinstance(scope_raw, str):
            scope_list = [s.strip() for s in scope_raw.split(",") if s.strip()]
        elif isinstance(scope_raw, Sequence):
            scope_list = [str(s).strip() for s in scope_raw if str(s).strip()]
        else:
            scope_list = None
        selected_milestones = (
            _read_id_list_file(args.selected_milestones_file)
            if args.selected_milestones_file
            else data.get("selected_milestones")
        )
        non_graded_milestones = (
            _read_id_list_file(args.non_graded_milestones_file)
            if args.non_graded_milestones_file
            else data.get("non_graded_milestones")
        )
        report = run_frozen_check(
            workspace=Path(str(data["workspace"])),
            candidate_prefix=_parse_candidate_prefix(data["candidate_prefix"]),
            phase2_baseline=baseline,
            scope_milestones=scope_list,
            selected_milestones=selected_milestones,
            non_graded_milestones=non_graded_milestones,
            gate_config=gate_config,
        )
        _write_report(report, args.output)
    except FrozenCheckInputError as exc:
        report = {
            "schema_version": 1,
            "generated_at": _utc_now_iso(),
            "is_dag_closed": None,
            "gates": {},
            "overall_passed": False,
            "failing_gates": [],
            "errors": [str(exc)],
        }
        _write_report(report, args.output)
        return 1

    if not report.get("is_dag_closed"):
        return 1
    return 0 if report.get("overall_passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
