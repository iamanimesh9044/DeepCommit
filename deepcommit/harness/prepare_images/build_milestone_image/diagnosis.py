#!/usr/bin/env python3
"""
Milestone Image Diagnosis
==========================

Milestone build validation utilities.

The authoritative Module 2 acceptance path is the Spec v0.9 hard-gate
validation section below. Debug diagnosis helpers remain in this module for
manual inspection only; they must not decide Module 2 acceptance.

Build-time utilities (called by build_milestone_image retry loop):
  - check_build_outputs: Dockerfile + test_config.json exist and are valid
  - run_tests: execute test suite via run_milestone_tests.py
  - collect_patched_tests: refresh commit-related test artifacts
  - evaluate_build_status / write_build_status: authoritative hard gates

Post-hoc diagnosis CLI (manual debug only; not Module 2 acceptance):
  Layer 1: Metric anomaly detection from post-loop benchmark test_summary.json
  Layer 2: Root cause classification -- extract error symbols from logs,
           trace via git log -S, map to milestones, check dependency graph
  Cross-milestone: test count comparison to detect incomplete collection

Usage:
    # Diagnose all milestones
    python -m deepcommit.harness.prepare_images.build_milestone_image.diagnosis \\
        --work-dir DATA/harness_workspace/django_django_4.2_5.2/v1_001_fix_v2_5

    # Diagnose specific milestones
    python -m deepcommit.harness.prepare_images.build_milestone_image.diagnosis \\
        --work-dir DATA/harness_workspace/django_django_4.2_5.2/v1_001_fix_v2_5 \\
        --milestones M1_sub-04 M1_sub-06

    # Output JSON report
    python -m deepcommit.harness.prepare_images.build_milestone_image.diagnosis \\
        --work-dir DATA/harness_workspace/django_django_4.2_5.2/v1_001_fix_v2_5 \\
        --json
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from deepcommit.harness.test_runner.core.classifier import CLASSIFICATION_CATEGORIES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class ErrorSymbol:
    """A symbol extracted from an error traceback."""

    symbol: str
    error_type: str  # "NameError", "ImportError", "AttributeError", "ModuleNotFoundError"
    file_path: str  # file where the error occurred
    message: str  # full error message line
    defined_in_commit: Optional[str] = None
    defined_in_milestone: Optional[str] = None


@dataclass
class MissingEdge:
    """A dependency edge missing from the DAG.

    Field names match ``dependencies.csv`` convention: ``source_*`` is the
    prerequisite (where the symbol is defined — must come first), ``target_*``
    is the dependant (where the error surfaced — comes later). A CSV row for
    this missing edge is literally ``prerequisite_id=source_milestone,
    dependant_id=target_milestone``.

    Captured at both granularities:
      - milestone level (coarse): for human-readable reports / dependencies.csv
      - commit level (fine): the precise constraint, lets downstream constraint
        solving stay sound (commit-level cycles are rarer than milestone-level
        ones because intra-milestone ordering can be respected).

    source_commit = commit in source_milestone that defined the symbol
    target_commit = earliest commit in target_milestone that references the symbol
    """

    source_milestone: str
    target_milestone: str
    source_commit: str
    target_commit: str
    symbol: str
    error_type: str

    def to_dict(self) -> dict:
        return {
            "source_milestone": self.source_milestone,
            "target_milestone": self.target_milestone,
            "source_commit": self.source_commit,
            "target_commit": self.target_commit,
            "symbol": self.symbol,
            "error_type": self.error_type,
        }


@dataclass
class DiagnosisResult:
    """Diagnosis result for a single milestone."""

    milestone_id: str
    is_healthy: bool
    severity: str  # "CRITICAL", "HIGH", "MEDIUM", "OK"
    issues: List[str]  # human-readable descriptions
    root_cause: str  # "dag_missing_edge", "env_issue", "healthy"
    missing_edges: List[MissingEdge] = field(default_factory=list)
    error_symbols: List[ErrorSymbol] = field(default_factory=list)
    uncollected_patched_tests: List[str] = field(default_factory=list)
    metrics: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "milestone_id": self.milestone_id,
            "is_healthy": self.is_healthy,
            "severity": self.severity,
            "issues": self.issues,
            "root_cause": self.root_cause,
            "missing_edges": [e.to_dict() for e in self.missing_edges],
            "error_symbols": [
                {
                    "symbol": s.symbol,
                    "error_type": s.error_type,
                    "file_path": s.file_path,
                    "message": s.message,
                    "defined_in_commit": s.defined_in_commit,
                    "defined_in_milestone": s.defined_in_milestone,
                }
                for s in self.error_symbols
            ],
        }
        if self.uncollected_patched_tests:
            d["uncollected_patched_tests"] = self.uncollected_patched_tests
        d["metrics"] = self.metrics
        return d


# =============================================================================
# Spec v0.9 hard-gate validation
# =============================================================================


DEFAULT_VALIDATION_TIMEOUT_SECONDS = 3600
DEFAULT_VALIDATION_RETRIES = 3


DEFAULT_VALIDATION_THRESHOLDS: Dict[str, Any] = {
    "H_patched_completeness": 1.00,
    "H_patched_clean_signal": 0.30,
    "H_pass_to_pass_majority": 0.75,
    "H_none_error_fail_to_pass_present": 1,
    "H_total_not_below_base": 0.20,
    # H_f2f HARD gate is PATCHED-scoped (the milestone's OWN tests' fail->fail rate) —
    # whole-suite f2f is dominated by TIMING_DAG pollution (tests ahead of the milestone's
    # src that fail at both START and END, ~410:1 vs patched) so it is a soft warning only.
    "H_f2f_not_above_base": 0.15,
    # H_error HARD gate is a RATIO (adjusted end errors / end collection): only a
    # catastrophic env collapse blocks built; mid-level error (ahead-of-src / env-gated
    # tests) is a soft warning, so the agent fixes real env breakage without being blocked
    # by pollution it cannot fix. NOTE: 'error' is pytest-specific today (issue #15) —
    # frameworks without an error outcome report ratio 0.
    "H_error_within_budget": 0.10,
    "H_s2s_not_above_base": 0.05,
    "H_anomaly_aggregate": 0.02,
    # Soft-warning thresholds — surfaced in raw_metrics.soft_warnings, do NOT block built.
    # They flag "approaching/over a stricter bar" for the agent + M3 without rejecting.
    "H_pass_to_pass_majority_warn": 0.85,
    "H_anomaly_aggregate_warn": 0.01,
    # Strict 0: any residual error ratio > 0 raises a (non-blocking) soft warning,
    # so a milestone that still errors is never silently passed. HARD bar stays 0.10
    # (only a catastrophic env collapse blocks built).
    "H_error_within_budget_warn": 0.0,
    "H_f2f_whole_warn": 0.05,
    "H_total_not_below_base_warn": 0.10,
    "module2_acceptance_mode": "hard_gate",
    "module2_validation_timeout_seconds": DEFAULT_VALIDATION_TIMEOUT_SECONDS,
    "module2_validation_retries": DEFAULT_VALIDATION_RETRIES,
    # B_lint_block_clean sub-rules backed by git diff --numstat against pristine SHAs.
    # See module2_three_exits_plan.md §4.4.
    # NOTE: large_src_append + src_function_replaced (below) are the legacy single-threshold
    # rules; the new architecture (see module2_lint_class_threshold_plan.md) is per-class
    # via `per_class_thresholds`. Both keys are read by diagnosis.py during the transition;
    # `per_class_thresholds` takes precedence when present.
    "large_src_append": {
        "total_added_max": 500,
        "per_file_added_max": 200,
        "files_touched_max": 5,
        "total_added_min_for_files_check": 30,
    },
    "src_function_replaced": {
        "per_file_min_paired": 50,
    },
    # Per-class numstat thresholds (Phase B). Only classes that need real constraints
    # appear here — `test_infrastructure` and `non_src_non_test` are absent on purpose
    # (evaluator skips them, freely modifiable per HOW-layer). All `*_src` use the same
    # bumped threshold (20) initially; calibrate after 5-milestone replay if needed.
    "per_class_thresholds": {
        "python_src": {"total_added_max": 20, "per_file_added_max": 20, "function_replaced_max": 0},
        "java_src": {"total_added_max": 20, "per_file_added_max": 20, "function_replaced_max": 0},
        "go_src": {"total_added_max": 20, "per_file_added_max": 20, "function_replaced_max": 0},
        "js_src": {"total_added_max": 20, "per_file_added_max": 20, "function_replaced_max": 0},
        "rust_src": {"total_added_max": 20, "per_file_added_max": 20, "function_replaced_max": 0},
        "rust_inline_test": {"total_added_max": 0, "per_file_added_max": 0, "function_replaced_max": 0},
        "test_content": {"total_added_max": 0, "per_file_added_max": 0, "function_replaced_max": 0},
    },
}

BUILD_RUN_GATES = [
    "B_lint_block_clean",
    "B_base_image_consistent",
    "B_image_exists",
    "B_state_tags_consistent",
    "B_agent_completed",
    "B_tests_ran_successfully",
]

QUALITY_GATES = [
    "H_patched_completeness",
    "H_patched_clean_signal",
    "H_pass_to_pass_majority",
    "H_none_error_fail_to_pass_present",
    "H_total_not_below_base",
    "H_f2f_not_above_base",
    "H_error_within_budget",
    "H_s2s_not_above_base",
    "H_anomaly_aggregate",
]

# `built` is decided by HARD gates only. SOFT gates are real, fixable signals but
# do NOT block — they surface in raw_metrics.soft_warnings so the agent / M3 can
# triage TIMING_DAG pollution (whole-suite tests ahead of the milestone's own src)
# from a genuine env defect. This replaces the old "built == ALL gates pass", which
# was over-strict: most H gates are whole-suite and the pollution failed milestones
# whose own body was fine. See deepcommit/harness/main_orch/prompts/main.md
# "Built is a strict per-MS verdict".
HARD_QUALITY_GATES = [
    "H_patched_completeness",
    "H_patched_clean_signal",
    "H_none_error_fail_to_pass_present",
    "H_error_within_budget",
    "H_total_not_below_base",
]
SOFT_QUALITY_GATES = [
    "H_f2f_not_above_base",
    "H_pass_to_pass_majority",
    "H_s2s_not_above_base",
    "H_anomaly_aggregate",
]
# A partition of QUALITY_GATES — guard against drift if a gate is added/renamed.
assert set(HARD_QUALITY_GATES) | set(SOFT_QUALITY_GATES) == set(QUALITY_GATES)
assert not (set(HARD_QUALITY_GATES) & set(SOFT_QUALITY_GATES))

TRANSITION_KEYS = list(CLASSIFICATION_CATEGORIES)

ANOMALY_TRANSITION_KEYS = [
    "pass_to_fail",
    "pass_to_skipped",
    # skipped_to_pass is NOT an anomaly: it is a src-driven positive signal (START
    # skips via @skipUnlessDBFeature because the milestone's feature is absent, END
    # passes once the milestone's src adds the feature) — the structural twin of
    # error_to_pass. Credited in H_none_error_fail_to_pass_present + the clean
    # numerator, so it must not also be penalised here.
    "fail_to_skipped",
    "skipped_to_fail",
    "none_to_fail",
    "none_to_skipped",
    "pass_to_none",
    "fail_to_none",
    "skipped_to_none",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not path.exists():
        return None, f"missing: {path}"
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {path}: {e}"
    if not isinstance(data, dict):
        return None, f"not a JSON object: {path}"
    return data, None


def _refinement_results_dir(work_dir: Path, milestone_id: str) -> Path:
    return work_dir / "test_results" / milestone_id / "refinement"


def _refinement_attempt_dir(work_dir: Path, milestone_id: str, attempt_number: int) -> Path:
    return _refinement_results_dir(work_dir, milestone_id) / f"attempt_{attempt_number}"


def _latest_attempt_path(work_dir: Path, milestone_id: str) -> Optional[Path]:
    attempt_dirs = sorted(
        _refinement_results_dir(work_dir, milestone_id).glob("attempt_*"),
        key=lambda path: _attempt_number(path),
    )
    return attempt_dirs[-1] if attempt_dirs else None


def _attempt_number(path: Path) -> int:
    try:
        return int(path.name.split("_", 1)[1])
    except (IndexError, ValueError):
        return -1


def _gate(passed: bool, **fields: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"passed": bool(passed)}
    result.update(fields)
    return result


def _unevaluated_gate(**fields: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"passed": False, "evaluated": False}
    result.update(fields)
    return result


def _collect_failed_gates(build_run_integrity: Dict[str, Any], quality: Dict[str, Any]) -> List[str]:
    """The blocking gates → ``built = not failed_gates``.

    All B_* build-run gates block; among the H gates ONLY the HARD subset blocks.
    A HARD gate that did not run (``evaluated`` is ``False``) is skipped, never a
    blocker. SOFT quality gates are intentionally absent — they warn, they do not
    block.
    """
    failed = [g for g in BUILD_RUN_GATES if not build_run_integrity[g]["passed"]]
    failed += [g for g in HARD_QUALITY_GATES if quality[g].get("evaluated") is not False and not quality[g]["passed"]]
    return failed


def _collect_soft_gate_warnings(quality: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every evaluated SOFT quality gate that failed, as a non-blocking warning.

    Guarantees coverage for SOFT gates with no bespoke soft-warning of their own
    (``H_s2s_not_above_base``) and for ``H_f2f_not_above_base`` when only the
    patched-scoped hard bar is crossed (its bespoke warning keys on the whole-suite
    value). Callers gate this on ``classification_error is None`` — these gates set
    ``passed=False`` on a classification error without an ``evaluated=False`` flag.
    """
    return [
        {
            "gate": g,
            "value": quality[g].get("value"),
            "soft_threshold": quality[g].get("threshold"),
            "hint": "soft gate over its bar — non-blocking; triage (TIMING_DAG pollution vs real defect)",
        }
        for g in SOFT_QUALITY_GATES
        if quality[g].get("evaluated") is not False and not quality[g]["passed"]
    ]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_yaml_validation(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        return {}
    validation = data.get("validation", data)
    return validation if isinstance(validation, dict) else {}


def load_validation_thresholds(work_dir: Path, repo_id: Optional[str] = None) -> Dict[str, Any]:
    """Load Spec v0.9 validation thresholds.

    Precedence: hardcoded defaults < config/validation_thresholds.yaml <
    config/{repo_id}.yaml::validation.
    """
    thresholds = dict(DEFAULT_VALIDATION_THRESHOLDS)
    config_dir = Path("config")
    thresholds.update(_load_yaml_validation(config_dir / "validation_thresholds.yaml"))
    if repo_id:
        thresholds.update(_load_yaml_validation(config_dir / f"{repo_id}.yaml"))
    # A workspace-local config directory is useful for one-off scenario tuning.
    workspace_config = work_dir / "config"
    thresholds.update(_load_yaml_validation(workspace_config / "validation_thresholds.yaml"))
    if repo_id:
        thresholds.update(_load_yaml_validation(workspace_config / f"{repo_id}.yaml"))
    return thresholds


def resolve_validation_timeout_seconds(
    work_dir: Path,
    repo_id: Optional[str] = None,
    override: Optional[int] = None,
) -> int:
    """Resolve the outer validation subprocess timeout.

    This timeout guards the full pipeline invocation. The test runner's
    per-test timeout stays controlled by test_config/run_milestone_tests.
    """
    if override is not None:
        value = override
    else:
        thresholds = load_validation_thresholds(work_dir, repo_id)
        value = thresholds.get(
            "module2_validation_timeout_seconds",
            DEFAULT_VALIDATION_TIMEOUT_SECONDS,
        )
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid module2_validation_timeout_seconds=%r; using default %ss",
            value,
            DEFAULT_VALIDATION_TIMEOUT_SECONDS,
        )
        return DEFAULT_VALIDATION_TIMEOUT_SECONDS
    if seconds <= 0:
        logger.warning(
            "Invalid non-positive module2_validation_timeout_seconds=%r; using default %ss",
            value,
            DEFAULT_VALIDATION_TIMEOUT_SECONDS,
        )
        return DEFAULT_VALIDATION_TIMEOUT_SECONDS
    return seconds


def resolve_validation_retries(
    work_dir: Path,
    repo_id: Optional[str] = None,
    override: Optional[int] = None,
) -> int:
    """Resolve timeout-only validation reruns for the same attempt number."""
    if override is not None:
        value = override
    else:
        thresholds = load_validation_thresholds(work_dir, repo_id)
        value = thresholds.get("module2_validation_retries", DEFAULT_VALIDATION_RETRIES)
    try:
        retries = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid module2_validation_retries=%r; using default %s",
            value,
            DEFAULT_VALIDATION_RETRIES,
        )
        return DEFAULT_VALIDATION_RETRIES
    if retries < 0:
        logger.warning(
            "Invalid negative module2_validation_retries=%r; using default %s",
            value,
            DEFAULT_VALIDATION_RETRIES,
        )
        return DEFAULT_VALIDATION_RETRIES
    return retries


BOUND_WAIVER_REASON_CODES = {
    # M3-action paths (waiver_m3_actionable_plan §2)
    "test_out_of_scope",
    "feature_introduced_later",
    # patched data quality (retained, M3 action = fix upstream patched data)
    "nodeid_absent_in_states",
    "deleted_earlier",
}

DEPRECATED_REASON_CODES = {
    "symbol_introduced_later": ("use 'feature_introduced_later' with attached_gates=['H_patched_completeness']"),
    "error_signature_dag_caused": (
        "use 'test_out_of_scope' (Path A) or 'feature_introduced_later' (Path B); "
        "if neither applies, document in unbound_issues (which does NOT offset gates) "
        "and accept the gate failure rather than waive it"
    ),
    "dag_gap_acknowledged": (
        "removed — boilerplate Path C rationales were undermining M3-actionability; "
        "if you cannot find Path A or Path B candidates, document in unbound_issues "
        "(does NOT offset gates) and accept the gate failure honestly"
    ),
}

LEGAL_ATTACHED_GATES = {"H_patched_completeness", "H_error_within_budget"}

# Symbol identifier patterns for feature_introduced_later (Path B)
import re as _re  # noqa: E402

_CLEAN_IDENT_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEF_DECL_RE = _re.compile(r"^(async )?def [A-Za-z_][A-Za-z0-9_]*$")
_CLASS_DECL_RE = _re.compile(r"^class [A-Za-z_][A-Za-z0-9_]*$")


def _is_valid_feature_symbol(symbol: str) -> bool:
    """Return True iff symbol matches clean identifier or def/class declaration."""
    if not symbol:
        return False
    return bool(_CLEAN_IDENT_RE.match(symbol) or _DEF_DECL_RE.match(symbol) or _CLASS_DECL_RE.match(symbol))


def _validate_attached_gates(value: Any) -> Tuple[List[str], Optional[str]]:
    """Validate attached_gates field (§4.2).

    Returns (normalized_list, error_message). If error_message is non-None, the
    list is empty (do not use). Rules:
    - must be a list
    - non-empty
    - each element ∈ LEGAL_ATTACHED_GATES
    - no duplicates
    """
    if not isinstance(value, list):
        return [], "attached_gates must be a list"
    if not value:
        return [], "attached_gates must be non-empty"
    seen: Set[str] = set()
    for item in value:
        if not isinstance(item, str):
            return [], f"attached_gates items must be strings (got {type(item).__name__})"
        if item not in LEGAL_ATTACHED_GATES:
            return [], (f"attached_gates contains invalid gate {item!r}; legal values: {sorted(LEGAL_ATTACHED_GATES)}")
        if item in seen:
            return [], f"attached_gates contains duplicate gate {item!r}"
        seen.add(item)
    return list(value), None


def _has_legal_path_prefix(path: str, prefixes: List[str]) -> bool:
    """True iff path starts with any of prefixes (with trailing-slash normalization)."""
    if not path or not prefixes:
        return False
    norm = path.replace("\\", "/").lstrip("./")
    for p in prefixes:
        pn = (p or "").replace("\\", "/").rstrip("/").lstrip("./")
        if not pn:
            continue
        if norm == pn or norm.startswith(pn + "/"):
            return True
    return False


_EMPTY_AGENT_SUFFICIENT = {"value": False, "rationale": ""}


def load_module2_waivers(
    docker_dir: Path,
    milestone_id: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any], List[str]]:
    """Load module2_waivers.json (schema v3 — M3-actionable reason codes).

    See ``module2_waiver_m3_actionable_plan.md`` for the
    full design. Hard cut to v3 — older versions and deprecated reason_codes are
    rejected with explicit migration hints.

    Returns ``(bound_waivers, unbound_issues, agent_sufficient, validation_errors)``:

    - ``bound_waivers`` — list of dicts; each entry has ``id`` (int >= 1, unique
      within list), ``reason_code`` ∈ ``BOUND_WAIVER_REASON_CODES``, ``anchor``,
      ``attached_gates`` (non-empty List[str] subset of LEGAL_ATTACHED_GATES),
      ``rationale``, plus per-reason required fields (test_path / symbol+path /
      rationale length).
    - ``unbound_issues`` — list of dicts with ``id`` (unique), ``kind`` (free-form
      string label), ``summary``, optional ``evidence``.
    - ``agent_sufficient`` — dict with ``value`` (bool) and ``rationale`` (str).
    - ``validation_errors`` — strings describing schema problems. When non-empty,
      the entire waiver file is treated as invalid for gate offsetting.
    """
    path = docker_dir / "module2_waivers.json"
    if not path.exists():
        return [], [], dict(_EMPTY_AGENT_SUFFICIENT), []

    data, error = _safe_read_json(path)
    if error:
        return [], [], dict(_EMPTY_AGENT_SUFFICIENT), [error]
    if not isinstance(data, dict):
        return [], [], dict(_EMPTY_AGENT_SUFFICIENT), ["module2_waivers must be a JSON object"]
    if data.get("milestone_id") not in (None, milestone_id):
        return (
            [],
            [],
            dict(_EMPTY_AGENT_SUFFICIENT),
            [f"module2_waivers milestone_id mismatch: {data.get('milestone_id')}"],
        )

    schema_version = data.get("schema_version")
    if schema_version != 3:
        return (
            [],
            [],
            dict(_EMPTY_AGENT_SUFFICIENT),
            [
                f"module2_waivers schema_version must be 3 (got {schema_version!r}); "
                "see module2_waiver_m3_actionable_plan.md "
                "§2 for the new reason_code table and required fields "
                "(id, attached_gates, per-path required fields)"
            ],
        )

    errors: List[str] = []

    # --- bound_waivers[] ---
    raw_bound = data.get("bound_waivers", []) or []
    bound: List[Dict[str, Any]] = []
    seen_ids: Set[int] = set()
    if not isinstance(raw_bound, list):
        errors.append("module2_waivers.bound_waivers must be a list")
        raw_bound = []
    for idx, entry in enumerate(raw_bound):
        if not isinstance(entry, dict):
            errors.append(f"bound_waivers[{idx}] must be an object")
            continue
        entry_errors: List[str] = []

        # id
        raw_id = entry.get("id")
        wid: Optional[int] = None
        if not isinstance(raw_id, int) or isinstance(raw_id, bool):
            entry_errors.append("id must be an integer")
        elif raw_id < 1:
            entry_errors.append(f"id must be >= 1 (got {raw_id})")
        elif raw_id in seen_ids:
            entry_errors.append(f"id {raw_id} duplicates an earlier waiver")
        else:
            seen_ids.add(raw_id)
            wid = raw_id

        # reason_code
        reason_code = str(entry.get("reason_code", "")).strip()
        if reason_code in DEPRECATED_REASON_CODES:
            entry_errors.append(f"reason_code {reason_code!r} deprecated — {DEPRECATED_REASON_CODES[reason_code]}")
        elif reason_code not in BOUND_WAIVER_REASON_CODES:
            entry_errors.append(f"reason_code must be one of {sorted(BOUND_WAIVER_REASON_CODES)} (got {reason_code!r})")

        # anchor + rationale
        anchor = str(entry.get("anchor", "")).strip()
        if not anchor:
            entry_errors.append("anchor must be a non-empty string")
        rationale = str(entry.get("rationale", "")).strip()
        if not rationale:
            entry_errors.append("rationale must be a non-empty string")

        # attached_gates
        gates, gates_err = _validate_attached_gates(entry.get("attached_gates"))
        if gates_err:
            entry_errors.append(gates_err)

        # optional fields (presence required per reason_code)
        symbol = str(entry.get("symbol", "")).strip() or None
        path_field = str(entry.get("path", "")).strip() or None
        test_path_field = str(entry.get("test_path", "")).strip() or None

        # per-reason_code field presence (light check; deep semantics in verify)
        if reason_code == "test_out_of_scope":
            if not test_path_field:
                entry_errors.append("test_out_of_scope: test_path is required")
        elif reason_code == "feature_introduced_later":
            if not symbol:
                entry_errors.append("feature_introduced_later: symbol is required")
            if not path_field:
                entry_errors.append("feature_introduced_later: path is required")
        # nodeid_absent_in_states / deleted_earlier: anchor only, already checked

        waiver = {
            "id": wid,
            "reason_code": reason_code,
            "anchor": anchor,
            "rationale": rationale,
            "attached_gates": gates,
            "symbol": symbol,
            "path": path_field,
            "test_path": test_path_field,
        }
        if entry_errors:
            for msg in entry_errors:
                errors.append(f"bound_waivers[{idx}]: {msg}")
            waiver["schema_errors"] = entry_errors
        bound.append(waiver)

    # --- unbound_issues[] ---
    raw_unbound = data.get("unbound_issues", []) or []
    unbound: List[Dict[str, Any]] = []
    seen_u_ids: Set[int] = set()
    if not isinstance(raw_unbound, list):
        errors.append("module2_waivers.unbound_issues must be a list")
        raw_unbound = []
    for idx, entry in enumerate(raw_unbound):
        if not isinstance(entry, dict):
            errors.append(f"unbound_issues[{idx}] must be an object")
            continue
        entry_errors: List[str] = []
        raw_id = entry.get("id")
        uid: Optional[int] = None
        if not isinstance(raw_id, int) or isinstance(raw_id, bool):
            entry_errors.append("id must be an integer")
        elif raw_id < 1:
            entry_errors.append(f"id must be >= 1 (got {raw_id})")
        elif raw_id in seen_u_ids:
            entry_errors.append(f"id {raw_id} duplicates an earlier issue")
        else:
            seen_u_ids.add(raw_id)
            uid = raw_id
        kind = str(entry.get("kind", "")).strip()
        summary = str(entry.get("summary", "")).strip()
        if not kind:
            entry_errors.append("kind must be a non-empty string")
        if not summary:
            entry_errors.append("summary must be a non-empty string")
        evidence = entry.get("evidence")
        if evidence is not None and not isinstance(evidence, dict):
            entry_errors.append("evidence must be an object when present")
            evidence = None

        issue = {
            "id": uid,
            "kind": kind,
            "summary": summary,
            "evidence": evidence,
        }
        if entry_errors:
            for msg in entry_errors:
                errors.append(f"unbound_issues[{idx}]: {msg}")
            issue["schema_errors"] = entry_errors
        unbound.append(issue)

    # --- agent_sufficient ---
    raw_sufficient = data.get("agent_sufficient", _EMPTY_AGENT_SUFFICIENT)
    agent_sufficient = dict(_EMPTY_AGENT_SUFFICIENT)
    if not isinstance(raw_sufficient, dict):
        errors.append("module2_waivers.agent_sufficient must be an object")
    else:
        value = bool(raw_sufficient.get("value", False))
        rationale = str(raw_sufficient.get("rationale", "")).strip()
        if value and not rationale:
            errors.append("agent_sufficient.rationale must be non-empty when value is true")
        if value and not bound and not unbound:
            errors.append("agent_sufficient.value=true requires at least one bound_waivers or unbound_issues entry")
        agent_sufficient = {"value": value, "rationale": rationale}

    return bound, unbound, agent_sufficient, errors


# ---------------------------------------------------------------------------
# module2_signals.json (Phase C — commit_signal schema validation)
# See module2_commit_signal_plan.md v0.5 §4.3 for full
# field-by-field spec. commit_signal is a *conditional upgrade* over waivers:
# emitted only when bound_waivers/unbound_issues already exist, proposes a DAG
# adjustment that resolves them, lives in a separate file from waivers, and is
# **non-destructive** (production Dockerfile + waivers stay unchanged).


_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIFF_FILE_HEADER_FROM_RE = re.compile(r"^--- (?:a/|/dev/null)", re.MULTILINE)
_DIFF_FILE_HEADER_TO_RE = re.compile(r"^\+\+\+ (?:b/|/dev/null)", re.MULTILINE)
_DIFF_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.MULTILINE)


def _is_valid_unified_diff(diff_text: str) -> Tuple[bool, Optional[str]]:
    """Lightweight regex sanity check for unified diff format.

    Empty string is valid (signals no companion shim required). Otherwise the
    diff must contain at least one ``--- ``/``+++ `` file-header pair and at
    least one ``@@ ... @@`` hunk header. This catches obvious malformations
    without invoking ``git apply`` (which would couple validation to the
    testbed's current state).
    """
    if not diff_text or not diff_text.strip():
        return True, None
    if not _DIFF_FILE_HEADER_FROM_RE.search(diff_text):
        return False, "missing '--- ' file header"
    if not _DIFF_FILE_HEADER_TO_RE.search(diff_text):
        return False, "missing '+++ ' file header"
    if not _DIFF_HUNK_HEADER_RE.search(diff_text):
        return False, "missing '@@ ... @@' hunk header"
    return True, None


def load_module2_signals(
    docker_dir: Path,
    milestone_id: str,
    *,
    bound_waivers_count: int = 0,
    unbound_issues_count: int = 0,
) -> Tuple[List[Dict[str, Any]], str, List[int], List[str]]:
    """Load module2_signals.json (schema v2 — id-based refs).

    See ``module2_waiver_m3_actionable_plan.md`` §3.
    Hard cut to v2 — older versions rejected.

    Returns ``(commit_moves, dockerfile_diff, remaining_unbound_issue_ids,
    validation_errors)``:

    - ``commit_moves`` — list of validated entries with ``id`` for stable
      identification. Per-entry schema errors live in ``_schema_errors`` so
      ``verify_commit_signals`` can reject them downstream.
    - ``dockerfile_diff`` — single composite shim diff for the whole proposal,
      applied AFTER all commit_moves[] are applied. Format-validated by
      ``verify_commit_signals``. May be empty string.
    - ``remaining_unbound_issue_ids`` — list of int ids (references into
      ``module2_waivers.json :: unbound_issues[*].id``). May be empty (=
      proposal completely resolves all blockers).
    - ``validation_errors`` — top-level schema errors.

    Conditional precondition: ``module2_signals.json`` may only exist when
    ``module2_waivers.json`` has at least one ``bound_waivers`` or
    ``unbound_issues`` entry.
    """
    path = docker_dir / "module2_signals.json"
    if not path.exists():
        return [], "", [], []

    data, error = _safe_read_json(path)
    if error:
        return [], "", [], [error]
    if not isinstance(data, dict):
        return [], "", [], ["module2_signals must be a JSON object"]

    if data.get("milestone_id") not in (None, milestone_id):
        return [], "", [], [f"module2_signals milestone_id mismatch: {data.get('milestone_id')!r}"]

    schema_version = data.get("schema_version")
    if schema_version != 2:
        return (
            [],
            "",
            [],
            [
                f"module2_signals schema_version must be 2 (got {schema_version!r}); "
                "see module2_waiver_m3_actionable_plan.md §3 "
                "for id-based refs (bound_waiver_ids / unbound_issue_ids / "
                "remaining_unbound_issue_ids) and commit_moves[].id"
            ],
        )

    if bound_waivers_count == 0 and unbound_issues_count == 0:
        return (
            [],
            "",
            [],
            [
                "module2_signals requires at least one bound_waivers or unbound_issues entry "
                "in module2_waivers.json (commit_signals is a conditional upgrade over waivers, "
                "not a standalone exit)"
            ],
        )

    errors: List[str] = []

    # --- commit_moves[] ---
    raw_moves = data.get("commit_moves", [])
    if not isinstance(raw_moves, list):
        errors.append("module2_signals.commit_moves must be a list")
        raw_moves = []
    elif not raw_moves:
        errors.append("module2_signals.commit_moves must be non-empty")

    valid_applied_at = {"predecessor", "internal"}
    valid_conflict = {"clean", "resolved_theirs", "unresolvable"}
    valid_status = {"pending", "verified", "attempted_failed"}

    commit_moves: List[Dict[str, Any]] = []
    seen_move_ids: Set[int] = set()
    for idx, entry in enumerate(raw_moves):
        if not isinstance(entry, dict):
            errors.append(f"commit_moves[{idx}] must be an object")
            continue
        entry_errors: List[str] = []

        # id (unique within commit_moves[])
        raw_id = entry.get("id")
        mid: Optional[int] = None
        if not isinstance(raw_id, int) or isinstance(raw_id, bool):
            entry_errors.append("id must be an integer")
        elif raw_id < 1:
            entry_errors.append(f"id must be >= 1 (got {raw_id})")
        elif raw_id in seen_move_ids:
            entry_errors.append(f"id {raw_id} duplicates an earlier commit_move")
        else:
            seen_move_ids.add(raw_id)
            mid = raw_id

        # status (optional; default 'verified' for backward compat)
        status = str(entry.get("status", "verified")).strip()
        if status not in valid_status:
            entry_errors.append(f"status must be one of {sorted(valid_status)} (got {status!r})")

        # resolving_commit (required for all statuses)
        resolving_commit = str(entry.get("resolving_commit", "")).strip()
        if not _COMMIT_SHA_RE.match(resolving_commit):
            entry_errors.append("resolving_commit must be a 40-hex SHA")

        # Per-status field requirements
        applied_at = str(entry.get("applied_at", "")).strip()
        conflict = str(entry.get("conflict_during_pick", "")).strip()
        verified_via_anchor = str(entry.get("verified_via_anchor", "")).strip()
        intended_symbol = str(entry.get("intended_symbol", "")).strip()
        intended_path = str(entry.get("intended_path", "")).strip()
        attempt_log = str(entry.get("attempt_log", "")).strip()

        if status == "verified":
            # Full validation: existing semantics
            if applied_at not in valid_applied_at:
                entry_errors.append(f"applied_at must be one of {sorted(valid_applied_at)}")
            if conflict not in valid_conflict:
                entry_errors.append(f"conflict_during_pick must be one of {sorted(valid_conflict)}")
            if not verified_via_anchor:
                entry_errors.append("verified_via_anchor must be a non-empty nodeid string")
        elif status == "pending":
            # TODO entries — written before sandbox cherry-pick
            if not intended_symbol:
                entry_errors.append("pending: intended_symbol required")
            if not intended_path:
                entry_errors.append("pending: intended_path required")
        elif status == "attempted_failed":
            # Audit log entries — written after failed cherry-pick.
            # attempt_log must cite real sandbox execution evidence; shim-as-failed
            # patterns ("not run", "absorbed by sitecustomize", "shim handles it")
            # are not valid evidence and the entry is rejected. See
            # workflow.md.j2 Exit 3 "`attempted_failed` requires actual cherry-pick evidence".
            if not attempt_log:
                entry_errors.append(
                    "attempted_failed: attempt_log required (specific reason — what conflicted, what was tried)"
                )
            else:
                lowered = attempt_log.lower()
                fake_evidence_phrases = (
                    "not run this attempt",
                    "cherry-pick not run",
                    "not run; ",
                    "absorbed by /tmp/sitecustomize",
                    "absorbed by sitecustomize",
                    "shim handles it",
                    "shim handles the",
                    "shim absorbs",
                )
                if any(p in lowered for p in fake_evidence_phrases):
                    entry_errors.append(
                        "attempted_failed: attempt_log shows cherry-pick was not actually run "
                        "(phrases like 'not run', 'absorbed by sitecustomize', 'shim handles' "
                        "are not valid evidence). Either run the sandbox cherry-pick and record "
                        "real stderr/conflict output, or escalate to unbound_issue (Exit 2)."
                    )

        # resolves (required only for verified; optional for pending/attempted_failed)
        resolves_raw = entry.get("resolves", {})
        resolves_bw: List[int] = []
        resolves_ui: List[int] = []
        if status == "verified":
            if not isinstance(resolves_raw, dict):
                entry_errors.append("resolves must be an object with bound_waiver_ids / unbound_issue_ids lists")
            else:
                for key, target in (("bound_waiver_ids", resolves_bw), ("unbound_issue_ids", resolves_ui)):
                    raw_ids = resolves_raw.get(key, [])
                    if not isinstance(raw_ids, list):
                        entry_errors.append(f"resolves.{key} must be a list of integers")
                        continue
                    for j, val in enumerate(raw_ids):
                        if isinstance(val, bool) or not isinstance(val, int):
                            entry_errors.append(f"resolves.{key}[{j}] must be an integer id")
                            continue
                        if val < 1:
                            entry_errors.append(f"resolves.{key}[{j}] must be >= 1 (got {val})")
                            continue
                        target.append(val)
        elif isinstance(resolves_raw, dict):
            # pending/attempted_failed may have resolves but it's informational only
            for key, target in (("bound_waiver_ids", resolves_bw), ("unbound_issue_ids", resolves_ui)):
                raw_ids = resolves_raw.get(key, []) or []
                if isinstance(raw_ids, list):
                    for val in raw_ids:
                        if isinstance(val, int) and not isinstance(val, bool) and val >= 1:
                            target.append(val)

        move = {
            "id": mid,
            "status": status,
            "resolving_commit": resolving_commit,
            "applied_at": applied_at,
            "conflict_during_pick": conflict,
            "resolves": {"bound_waiver_ids": resolves_bw, "unbound_issue_ids": resolves_ui},
            "verified_via_anchor": verified_via_anchor,
            "intended_symbol": intended_symbol or None,
            "intended_path": intended_path or None,
            "attempt_log": attempt_log or None,
        }
        if entry_errors:
            move["_schema_errors"] = entry_errors
            for msg in entry_errors:
                errors.append(f"commit_moves[{idx}]: {msg}")
        commit_moves.append(move)

    # --- dockerfile_diff (single top-level composite shim) ---
    dockerfile_diff = data.get("dockerfile_diff", "")
    if not isinstance(dockerfile_diff, str):
        errors.append("module2_signals.dockerfile_diff must be a string (may be empty)")
        dockerfile_diff = ""

    # --- remaining_unbound_issue_ids[] (list of int ids) ---
    raw_remaining = data.get("remaining_unbound_issue_ids", [])
    remaining_ids: List[int] = []
    if not isinstance(raw_remaining, list):
        errors.append("module2_signals.remaining_unbound_issue_ids must be a list of integers")
    else:
        for j, val in enumerate(raw_remaining):
            if isinstance(val, bool) or not isinstance(val, int):
                errors.append(f"remaining_unbound_issue_ids[{j}] must be an integer id")
                continue
            if val < 1:
                errors.append(f"remaining_unbound_issue_ids[{j}] must be >= 1 (got {val})")
                continue
            remaining_ids.append(val)

    return commit_moves, dockerfile_diff, remaining_ids, errors


def verify_commit_signals(
    commit_moves: List[Dict[str, Any]],
    dockerfile_diff: str,
    *,
    work_dir: Path,
    end_state: str,
    valid_bound_waiver_ids: Set[int],
    valid_unbound_issue_ids: Set[int],
) -> Dict[str, Any]:
    """Verify commit_signals against pipeline rules (schema v2 with status field).

    Per ``commit_moves[]`` entry with ``status == 'verified'`` (or status missing):
    - SHA existence: ``git rev-parse --verify <sha>^{commit}`` succeeds
    - SHA is future: ``git merge-base --is-ancestor <sha> <end_state>`` exits 1
    - ``resolves.bound_waiver_ids[i]`` / ``resolves.unbound_issue_ids[i]`` must
      be in the corresponding set of valid ids from ``module2_waivers.json``

    Per ``commit_moves[]`` entry with ``status == 'pending'``:
    - schema-only check (intended_symbol + intended_path required, from load step)
    - NOT counted as paired commit_signal for Path B mandate

    Per ``commit_moves[]`` entry with ``status == 'attempted_failed'``:
    - schema-only check (attempt_log required)
    - NOT counted as paired commit_signal

    Globally:
    - dockerfile_diff is a parseable unified diff (regex sanity, empty OK)

    Returns ``{"verified": [...], "pending": [...], "failed": [...],
    "invalid": [...], "count": int, "dockerfile_diff_error": str | None}``.
    """
    repo_path = work_dir / "testbed"
    verified: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []

    for move in commit_moves:
        mid = move.get("id")
        status = move.get("status", "verified")
        signal_label = f"commit_moves[id={mid}]"
        move_errors: List[str] = list(move.get("_schema_errors", []))
        sha = move.get("resolving_commit", "")

        # Schema-only handling for non-verified statuses
        if status != "verified":
            if move_errors:
                invalid.append(
                    {
                        "id": signal_label,
                        "move_id": mid,
                        "status": status,
                        "resolving_commit": sha,
                        "error": "; ".join(move_errors),
                    }
                )
            elif status == "pending":
                pending.append(
                    {
                        "id": signal_label,
                        "move_id": mid,
                        "status": status,
                        "resolving_commit": sha,
                        "intended_symbol": move.get("intended_symbol"),
                        "intended_path": move.get("intended_path"),
                    }
                )
            else:  # attempted_failed
                failed.append(
                    {
                        "id": signal_label,
                        "move_id": mid,
                        "status": status,
                        "resolving_commit": sha,
                        "attempt_log": move.get("attempt_log"),
                    }
                )
            continue

        # status == 'verified': full git verification
        if not move_errors and sha:
            # SHA existence
            try:
                result = subprocess.run(
                    ["git", "-C", str(repo_path), "rev-parse", "--verify", f"{sha}^{{commit}}"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    stderr_tail = (result.stderr or "").strip()[:200]
                    move_errors.append(f"resolving_commit SHA does not exist in repo: {sha} ({stderr_tail})")
            except Exception as e:
                move_errors.append(f"git rev-parse failed for {sha}: {e}")

            # SHA is future (NOT reachable from end_state)
            if not move_errors:
                try:
                    anc = subprocess.run(
                        ["git", "-C", str(repo_path), "merge-base", "--is-ancestor", sha, end_state],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if anc.returncode == 0:
                        move_errors.append(
                            f"resolving_commit IS reachable from end_state ({end_state}); "
                            "candidate must be a future commit"
                        )
                    elif anc.returncode != 1:
                        stderr_tail = (anc.stderr or "").strip()[:200]
                        move_errors.append(
                            f"git merge-base --is-ancestor returned unexpected exit {anc.returncode}: {stderr_tail}"
                        )
                except Exception as e:
                    move_errors.append(f"git merge-base failed for {sha}: {e}")

        # Id lookup: each ref in resolves.* must exist in valid id set.
        resolves = move.get("resolves", {}) or {}
        for key, valid_ids in (
            ("bound_waiver_ids", valid_bound_waiver_ids),
            ("unbound_issue_ids", valid_unbound_issue_ids),
        ):
            for j, val in enumerate(resolves.get(key, []) or []):
                if val not in valid_ids:
                    move_errors.append(
                        f"resolves.{key}[{j}]=id={val} not found in module2_waivers.json "
                        f"({'bound_waivers' if key == 'bound_waiver_ids' else 'unbound_issues'}[*].id)"
                    )

        if move_errors:
            invalid.append(
                {
                    "id": signal_label,
                    "move_id": mid,
                    "status": status,
                    "resolving_commit": sha,
                    "error": "; ".join(move_errors),
                }
            )
        else:
            verified.append(
                {
                    "id": signal_label,
                    "move_id": mid,
                    "status": status,
                    "resolving_commit": sha,
                    "applied_at": move.get("applied_at"),
                    "conflict_during_pick": move.get("conflict_during_pick"),
                    "verified_via_anchor": move.get("verified_via_anchor"),
                    "resolves": resolves,
                }
            )

    # Dockerfile diff format check (independent of per-entry validation).
    dockerfile_diff_ok, dockerfile_diff_err = _is_valid_unified_diff(dockerfile_diff)
    if not dockerfile_diff_ok:
        invalid.append(
            {
                "id": "dockerfile_diff",
                "error": f"dockerfile_diff is not parseable as unified diff: {dockerfile_diff_err}",
            }
        )

    return {
        "verified": verified,
        "pending": pending,
        "failed": failed,
        "invalid": invalid,
        "count": len(verified),
        "pending_count": len(pending),
        "failed_count": len(failed),
        "dockerfile_diff_error": None if dockerfile_diff_ok else dockerfile_diff_err,
    }


def _docker_image_exists(image_name: Optional[str]) -> bool:
    if not image_name:
        return False
    try:
        result = subprocess.run(
            ["docker", "images", "-q", image_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _docker_image_id(image_name: Optional[str]) -> Optional[str]:
    if not image_name:
        return None
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.Id}}", image_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _file_sha256(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def current_build_inputs(docker_dir: Path) -> Dict[str, Optional[str]]:
    """Return content hashes for the files that define a milestone image.

    Includes ``module2_waivers.json`` and ``module2_signals.json`` (both optional)
    so retry-time input-change detection recognizes waiver/signal edits as a
    distinct attempt, not a re-run of the same inputs.
    """
    return {
        "dockerfile_sha256": _file_sha256(docker_dir / "Dockerfile"),
        "test_config_sha256": _file_sha256(docker_dir / "test_config.json"),
        "module2_waivers_sha256": _file_sha256(docker_dir / "module2_waivers.json"),
        "module2_signals_sha256": _file_sha256(docker_dir / "module2_signals.json"),
    }


def build_status_matches_current_inputs(status: Optional[Dict[str, Any]], docker_dir: Path) -> bool:
    """True when build_status.json was produced from the current input files."""
    if not status:
        return False
    recorded = status.get("inputs")
    if not isinstance(recorded, dict):
        return False
    return recorded == current_build_inputs(docker_dir)


def build_status_image_matches_current(status: Optional[Dict[str, Any]]) -> bool:
    """True when build_status.json points at the currently tagged Docker image."""
    if not status:
        return False
    image = status.get("image")
    if not isinstance(image, dict):
        return False
    image_name = image.get("name")
    recorded_id = image.get("id")
    if not image_name or not recorded_id:
        return False
    return _docker_image_id(str(image_name)) == recorded_id


def _docker_state_tags_consistent(
    image_name: Optional[str],
    base_state: str,
    end_state: str,
) -> Tuple[bool, Dict[str, Any]]:
    if not image_name:
        return False, {"reason": "missing image_name"}
    cmd = (
        "cd /testbed && "
        f"base_sha=$(git rev-parse --verify {base_state}^{{commit}}) && "
        f"end_sha=$(git rev-parse --verify {end_state}^{{commit}}) && "
        f"git checkout -q {end_state} && "
        f"git checkout -q {base_state} && "
        "head_sha=$(git rev-parse HEAD) && "
        'test "$head_sha" = "$base_sha" && '
        'printf \'{"base_sha":"%s","end_sha":"%s","default_head":"%s"}\' "$base_sha" "$end_sha" "$head_sha"'
    )
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", image_name, "bash", "-lc", cmd],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as e:
        return False, {"reason": str(e)}
    evidence: Dict[str, Any] = {
        "exit_code": result.returncode,
        "stderr_tail": (result.stderr or "")[-1000:],
    }
    if result.returncode == 0 and result.stdout.strip():
        try:
            evidence.update(json.loads(result.stdout.strip()))
        except json.JSONDecodeError:
            evidence["stdout_tail"] = result.stdout[-1000:]
    return result.returncode == 0, evidence


_NUMSTAT_SEPARATOR = "---DEEPCOMMIT-NUMSTAT-SEP---"


def _docker_src_diff_numstat(
    image_name: Optional[str],
    pristine_base_sha: Optional[str],
    pristine_end_sha: Optional[str],
    base_state: str,
    end_state: str,
    repo_src_dirs: List[str],
) -> Tuple[Dict[str, Dict[str, int]], Dict[str, Any]]:
    """Run ``git diff --numstat`` inside the built image for both states.

    Returns ``(per_file_stats, debug_evidence)``. ``per_file_stats`` maps
    repo-relative path → ``{"added": int, "deleted": int}`` where each value is
    the **maximum** of the two states (so the worst case is always reported).
    Files that appear in only one diff still surface with their stats from that
    side. Binary files (``-``) are recorded with both counters set to 0.

    ``debug_evidence`` carries pristine SHAs, raw exit codes, and stderr tail
    for surfacing in build_status when something goes wrong.
    """
    debug: Dict[str, Any] = {
        "pristine_base_sha": pristine_base_sha,
        "pristine_end_sha": pristine_end_sha,
        "base_state": base_state,
        "end_state": end_state,
    }
    if not image_name:
        debug["skipped_reason"] = "image_name missing"
        return {}, debug
    if not pristine_base_sha and not pristine_end_sha:
        debug["skipped_reason"] = "no pristine SHAs supplied"
        return {}, debug

    if repo_src_dirs:
        # Pass each src dir as a pathspec; let git filter.
        path_args = " ".join(_shell_quote(d.rstrip("/")) for d in repo_src_dirs)
        path_filter = f"-- {path_args}"
    else:
        path_filter = ""

    parts: List[str] = ["cd /testbed"]
    if pristine_base_sha:
        parts.append(
            f"echo '{_NUMSTAT_SEPARATOR}base'; "
            f"git diff --numstat {pristine_base_sha} {base_state} {path_filter} 2>&1 || true"
        )
    if pristine_end_sha:
        parts.append(
            f"echo '{_NUMSTAT_SEPARATOR}end'; "
            f"git diff --numstat {pristine_end_sha} {end_state} {path_filter} 2>&1 || true"
        )
    cmd = " && ".join(parts)

    try:
        result = subprocess.run(
            ["docker", "run", "--rm", image_name, "bash", "-lc", cmd],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as e:
        debug["error"] = str(e)
        return {}, debug

    debug["exit_code"] = result.returncode
    debug["stderr_tail"] = (result.stderr or "")[-500:]

    if result.returncode != 0:
        debug["stdout_tail"] = (result.stdout or "")[-500:]
        return {}, debug

    per_state: Dict[str, Dict[str, Dict[str, int]]] = {"base": {}, "end": {}}
    current_section: Optional[str] = None
    for line in (result.stdout or "").splitlines():
        if line.startswith(_NUMSTAT_SEPARATOR):
            current_section = line[len(_NUMSTAT_SEPARATOR) :].strip()
            continue
        if current_section is None or not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) != 3:
            continue
        added_raw, deleted_raw, path = cols
        added = 0 if added_raw == "-" else _safe_atoi(added_raw)
        deleted = 0 if deleted_raw == "-" else _safe_atoi(deleted_raw)
        if added is None or deleted is None:
            continue
        per_state[current_section][path] = {"added": added, "deleted": deleted}

    # Take per-file MAX of both states so the worst case is reported.
    merged: Dict[str, Dict[str, int]] = {}
    for path in set(per_state["base"]) | set(per_state["end"]):
        b = per_state["base"].get(path, {"added": 0, "deleted": 0})
        e = per_state["end"].get(path, {"added": 0, "deleted": 0})
        merged[path] = {
            "added": max(b["added"], e["added"]),
            "deleted": max(b["deleted"], e["deleted"]),
        }
    return merged, debug


def _safe_atoi(s: str) -> Optional[int]:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _shell_quote(s: str) -> str:
    if not s or any(c in s for c in " \t\n\"'\\$`"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


def evaluate_large_src_append(
    per_file_stats: Dict[str, Dict[str, int]],
    src_filter: Any,
    *,
    large_src_thresholds: Dict[str, int],
    function_replaced_thresholds: Dict[str, int],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Apply large_src_append + src_function_replaced thresholds against numstat
    output filtered through ``src_filter.is_src_file``.

    Returns ``(violations, evidence_dict)``. Violations are emitted with the
    block severity in the same shape as ``dockerfile_lint`` so they merge into
    ``B_lint_block_clean`` cleanly.
    """
    src_only: Dict[str, Dict[str, int]] = {}
    skipped_non_src: List[str] = []
    for path, stats in per_file_stats.items():
        try:
            is_src = bool(src_filter.is_src_file(path)) if src_filter is not None else True
        except Exception:
            is_src = True
        if is_src:
            src_only[path] = stats
        else:
            skipped_non_src.append(path)

    total_added = sum(s["added"] for s in src_only.values())
    total_deleted = sum(s["deleted"] for s in src_only.values())
    files_touched = len([p for p, s in src_only.items() if s["added"] or s["deleted"]])

    total_added_max = int(large_src_thresholds.get("total_added_max", 500))
    per_file_added_max = int(large_src_thresholds.get("per_file_added_max", 200))
    files_touched_max = int(large_src_thresholds.get("files_touched_max", 5))
    total_added_min_for_files_check = int(large_src_thresholds.get("total_added_min_for_files_check", 30))
    per_file_min_paired = int(function_replaced_thresholds.get("per_file_min_paired", 50))

    violations: List[Dict[str, Any]] = []
    if total_added > total_added_max:
        violations.append(
            {
                "rule": "large_src_append",
                "severity": "block",
                "line": 0,
                "command": "",
                "detail": (
                    f"total +{total_added} lines across {files_touched} src files (threshold: {total_added_max})"
                ),
            }
        )
    if files_touched > files_touched_max and total_added > total_added_min_for_files_check:
        violations.append(
            {
                "rule": "large_src_append",
                "severity": "warn",
                "line": 0,
                "command": "",
                "detail": (
                    f"distributed src patches: {files_touched} src files touched "
                    f"(threshold: {files_touched_max}) with +{total_added} total added "
                    f"(minimum for this check: {total_added_min_for_files_check}). "
                    "This can indicate many small source shims split across files; "
                    "review whether these are load-only interface restorations or "
                    "future framework behavior that should be represented as a waiver."
                ),
            }
        )
    for path, stats in sorted(src_only.items()):
        if stats["added"] > per_file_added_max:
            violations.append(
                {
                    "rule": "large_src_append",
                    "severity": "block",
                    "line": 0,
                    "command": path,
                    "detail": (f"{path}: +{stats['added']} lines (per-file threshold: {per_file_added_max})"),
                }
            )
        if stats["added"] >= per_file_min_paired and stats["deleted"] >= per_file_min_paired:
            violations.append(
                {
                    "rule": "src_function_replaced",
                    "severity": "block",
                    "line": 0,
                    "command": path,
                    "detail": (
                        f"{path}: +{stats['added']}/-{stats['deleted']} lines paired "
                        f"(threshold: each ≥ {per_file_min_paired})"
                    ),
                }
            )

    evidence = {
        "per_file_stats": src_only,
        "totals": {
            "added": total_added,
            "deleted": total_deleted,
            "files": files_touched,
        },
        "thresholds_used": {
            "total_added_max": total_added_max,
            "per_file_added_max": per_file_added_max,
            "files_touched_max": files_touched_max,
            "total_added_min_for_files_check": total_added_min_for_files_check,
            "per_file_min_paired": per_file_min_paired,
        },
        "skipped_non_src_files_count": len(skipped_non_src),
    }
    return violations, evidence


# ---------------------------------------------------------------------------
# Phase B — per-class numstat threshold evaluator
# Replaces the legacy single-threshold ``evaluate_large_src_append`` (still kept
# above for transitional safety). See
# module2_lint_class_threshold_plan.md for the full design.


_DIFF_HUNK_NEW_RANGE_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def _parse_diff_hunks(diff_text: str) -> List[Tuple[int, int]]:
    """Parse ``git diff -U0`` output into added-line line ranges in the NEW file.

    Returns list of ``(start_line, end_line)`` inclusive, 1-indexed. Pure
    deletion hunks (new-side length 0) are skipped.
    """
    hunks: List[Tuple[int, int]] = []
    for match in _DIFF_HUNK_NEW_RANGE_RE.finditer(diff_text):
        start = int(match.group(1))
        length = int(match.group(2)) if match.group(2) else 1
        if length <= 0:
            continue
        hunks.append((start, start + length - 1))
    return hunks


def _docker_src_diff_hunks(
    image_name: str,
    pristine_sha: str,
    state_ref: str,
    file_path: str,
) -> List[Tuple[int, int]]:
    """Run ``git diff -U0 <pristine> <state> -- <file>`` inside the image.

    Returns added-line ranges in the new file. Empty list on error / no diff.
    """
    quoted_path = _shell_quote(file_path)
    cmd = f"cd /testbed && git diff -U0 {pristine_sha} {state_ref} -- {quoted_path} 2>&1 || true"
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", image_name, "bash", "-lc", cmd],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return _parse_diff_hunks(result.stdout or "")


def _docker_extract_file(image_name: str, in_image_path: str, dest: Path) -> bool:
    """``docker cp <image>:<path> <dest>`` via an ephemeral container.

    Used to materialize a Rust src file from the image so
    ``find_test_code_ranges`` can run ast-grep over it locally. Returns True
    iff the file was extracted successfully.
    """
    try:
        create = subprocess.run(
            ["docker", "create", image_name, "true"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return False
    if create.returncode != 0:
        return False
    container_id = (create.stdout or "").strip()
    if not container_id:
        return False
    try:
        cp = subprocess.run(
            ["docker", "cp", f"{container_id}:{in_image_path}", str(dest)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cp.returncode == 0
    except Exception:
        return False
    finally:
        try:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception:
            pass


def _classify_rust_hunks(
    image_name: str,
    pristine_sha: str,
    state_ref: str,
    file_path: str,
) -> Tuple[int, int, Dict[str, Any]]:
    """Split a Rust src file's added lines into (rust_src, rust_inline_test).

    Conservative on failure: if hunks can't be obtained or the file can't be
    extracted, everything goes to ``rust_src`` (so the standard rust_src
    threshold still applies — we don't accidentally classify into the
    threshold-0 ``rust_inline_test`` bucket).

    Returns ``(rust_src_added, rust_inline_test_added, debug_info)``.
    """
    debug: Dict[str, Any] = {"file": file_path}
    hunks = _docker_src_diff_hunks(image_name, pristine_sha, state_ref, file_path)
    if not hunks:
        debug["skipped_reason"] = "no hunks parsed from git diff -U0"
        return 0, 0, debug

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".rs", delete=False) as tf:
        tmp_path = Path(tf.name)
    try:
        if not _docker_extract_file(image_name, f"/testbed/{file_path}", tmp_path):
            debug["fallback"] = "docker extract failed; all added lines → rust_src"
            return sum(end - start + 1 for start, end in hunks), 0, debug
        try:
            from deepcommit.harness.prepare_repo.split_test_patches.test_detector import (
                find_test_code_ranges,
            )

            test_ranges = find_test_code_ranges(str(tmp_path))
        except Exception as e:
            debug["fallback"] = f"find_test_code_ranges failed ({e}); all added lines → rust_src"
            return sum(end - start + 1 for start, end in hunks), 0, debug

        debug["test_ranges_count"] = len(test_ranges)
        rust_src_added = 0
        rust_inline_test_added = 0
        for h_start, h_end in hunks:
            for line in range(h_start, h_end + 1):
                in_test = any(t_start <= line <= t_end for t_start, t_end, _ in test_ranges)
                if in_test:
                    rust_inline_test_added += 1
                else:
                    rust_src_added += 1
        debug["hunks"] = [list(h) for h in hunks]
        return rust_src_added, rust_inline_test_added, debug
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def evaluate_per_class_thresholds(
    per_file_stats: Dict[str, Dict[str, int]],
    classifier: Any,
    per_class_thresholds: Dict[str, Dict[str, int]],
    *,
    image_name: Optional[str] = None,
    pristine_base_sha: Optional[str] = None,
    pristine_end_sha: Optional[str] = None,
    base_state: Optional[str] = None,
    end_state: Optional[str] = None,
    function_replaced_min_paired: int = 50,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Per-class numstat threshold evaluation (Phase B).

    For each file in ``per_file_stats``:
    - Call ``classifier.classify(path)`` to get a ``FileClass`` label
    - For ``.rs`` files classified ``rust_src``, additionally split hunks via
      ``_classify_rust_hunks`` to separate the src portion from the
      ``#[cfg(test)]`` inline test portion (handled at hunk granularity, not
      file granularity)
    - Look up ``per_class_thresholds[label]``; if absent, skip (intentional —
      ``test_infrastructure`` and ``non_src_non_test`` are unchecked by design)
    - Compare actual added lines vs ``total_added_max`` (aggregate) and
      ``per_file_added_max`` (per file). Threshold = 0 means "any change blocks."

    ``function_replaced_min_paired`` is the legacy ``src_function_replaced``
    threshold (added ≥ N AND deleted ≥ N on the same file = likely body
    rewrite). Applied uniformly across all src classes whose
    ``function_replaced_max == 0``.

    Returns ``(violations, evidence)`` where evidence carries per-class totals
    and per-file classification so retry context can surface
    ``raw_metrics.per_class_lint_evidence``.
    """
    sha = pristine_end_sha or pristine_base_sha
    state_ref = end_state if pristine_end_sha else base_state

    per_class_added: Dict[str, int] = {label: 0 for label in per_class_thresholds}
    per_class_files: Dict[str, List[str]] = {label: [] for label in per_class_thresholds}
    # Track per-file-class added for per_file_added_max check.
    per_file_class: Dict[str, Tuple[str, int, int]] = {}
    per_file_classification: List[Dict[str, Any]] = []
    rust_hunk_debug: List[Dict[str, Any]] = []

    for path, stats in sorted(per_file_stats.items()):
        try:
            label = classifier.classify(path) if classifier is not None else "non_src_non_test"
        except Exception:
            label = "non_src_non_test"
        added = int(stats.get("added", 0))
        deleted = int(stats.get("deleted", 0))

        entry: Dict[str, Any] = {"path": path, "label": label, "added": added, "deleted": deleted}

        # Rust .rs files under src: hunk-level split into rust_src vs rust_inline_test.
        if label == "rust_src" and image_name and sha and state_ref and path.endswith(".rs"):
            rs_added, rit_added, debug = _classify_rust_hunks(image_name, sha, state_ref, path)
            # Robustness: if hunks-level parsing produced 0 attributions but
            # numstat shows real additions, fall back to attributing all added
            # lines to rust_src (conservative — file's content moves into the
            # threshold-checked bucket rather than vanishing).
            if rs_added + rit_added == 0 and added > 0:
                rs_added = added
                debug["fallback_attribution"] = "hunks empty; numstat added → rust_src"
            entry["rust_src_added"] = rs_added
            entry["rust_inline_test_added"] = rit_added
            rust_hunk_debug.append(debug)
            per_class_added["rust_src"] = per_class_added.get("rust_src", 0) + rs_added
            if rs_added > 0:
                per_class_files.setdefault("rust_src", []).append(path)
                per_file_class[path] = ("rust_src", rs_added, deleted)
            if "rust_inline_test" in per_class_thresholds:
                per_class_added["rust_inline_test"] = per_class_added.get("rust_inline_test", 0) + rit_added
                if rit_added > 0:
                    per_class_files.setdefault("rust_inline_test", []).append(path)
                    # When a file straddles both classes, the per-file check applies
                    # the stricter (rust_inline_test, threshold 0) to that file.
                    per_file_class[path] = ("rust_inline_test", rit_added, deleted)
            per_file_classification.append(entry)
            continue

        if label in per_class_thresholds:
            per_class_added[label] = per_class_added.get(label, 0) + added
            if added > 0:
                per_class_files.setdefault(label, []).append(path)
            per_file_class[path] = (label, added, deleted)

        per_file_classification.append(entry)

    violations: List[Dict[str, Any]] = []
    per_class_summary: Dict[str, Dict[str, Any]] = {}

    for label, thresholds in per_class_thresholds.items():
        total_added = per_class_added.get(label, 0)
        total_max = int(thresholds.get("total_added_max", 0))
        per_file_max = int(thresholds.get("per_file_added_max", 0))
        func_replaced_max = int(thresholds.get("function_replaced_max", 0))
        files = sorted(per_class_files.get(label, []))
        per_class_summary[label] = {
            "total_added": total_added,
            "total_added_max": total_max,
            "per_file_added_max": per_file_max,
            "function_replaced_max": func_replaced_max,
            "files_count": len(files),
            "files": files,
        }
        if total_added > total_max:
            violations.append(
                {
                    "rule": "large_src_append",
                    "severity": "block",
                    "line": 0,
                    "command": "",
                    "file_class": label,
                    "added_lines": total_added,
                    "threshold": total_max,
                    "exceeded_by": total_added - total_max,
                    "detail": (
                        f"file_class={label}: total +{total_added} lines across "
                        f"{len(files)} file(s) (total_added_max={total_max}, "
                        f"exceeded by {total_added - total_max})"
                    ),
                }
            )

    # Per-file added cap (one violation per file when it exceeds its class cap).
    for path, (label, added, _deleted) in sorted(per_file_class.items()):
        thresholds = per_class_thresholds.get(label)
        if thresholds is None:
            continue
        per_file_max = int(thresholds.get("per_file_added_max", 0))
        if added > per_file_max:
            violations.append(
                {
                    "rule": "large_src_append",
                    "severity": "block",
                    "line": 0,
                    "command": path,
                    "file_class": label,
                    "added_lines": added,
                    "threshold": per_file_max,
                    "exceeded_by": added - per_file_max,
                    "detail": (
                        f"{path} (file_class={label}): +{added} lines "
                        f"(per_file_added_max={per_file_max}, "
                        f"exceeded by {added - per_file_max})"
                    ),
                }
            )

    # function_replaced check: when function_replaced_max == 0 for any src class
    # ("no body replacement"), flag files where added ≥ N AND deleted ≥ N (legacy
    # heuristic; AST-based detection is a future enhancement). Iterate per_file_class
    # directly — it only contains paths whose class is in per_class_thresholds, so
    # no redundant classify call is needed (and no inconsistent exception handling
    # with the main classification loop above).
    for path, (actual_label, actual_added, actual_deleted) in sorted(per_file_class.items()):
        thresholds = per_class_thresholds.get(actual_label)
        if thresholds is None:
            continue
        if int(thresholds.get("function_replaced_max", 0)) != 0:
            continue
        if actual_added >= function_replaced_min_paired and actual_deleted >= function_replaced_min_paired:
            violations.append(
                {
                    "rule": "src_function_replaced",
                    "severity": "block",
                    "line": 0,
                    "command": path,
                    "file_class": actual_label,
                    "added_lines": actual_added,
                    "deleted_lines": actual_deleted,
                    "threshold": function_replaced_min_paired,
                    "detail": (
                        f"{path} (file_class={actual_label}): +{actual_added}/-{actual_deleted} "
                        f"paired lines (function_replaced_min_paired={function_replaced_min_paired}); "
                        "this signature pattern is function-body replacement, never a Kind 1 shim"
                    ),
                }
            )

    evidence = {
        "per_class_summary": per_class_summary,
        "per_file_classification": per_file_classification,
        "rust_hunk_debug": rust_hunk_debug,
        "function_replaced_min_paired": function_replaced_min_paired,
    }
    return violations, evidence


def _dockerfile_base_image(dockerfile_path: Path) -> Optional[str]:
    if not dockerfile_path.exists():
        return None
    for line in dockerfile_path.read_text(errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"FROM\s+([^\s]+)", stripped, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _json_exists_and_parseable(path: Path) -> Tuple[bool, Optional[str]]:
    _, error = _safe_read_json(path)
    return error is None, error


def _validation_report_health(latest_attempt_dir: Optional[Path]) -> Dict[str, Any]:
    """Index of per-mode test reports — aggregate summary only.

    build_status.json is a summary + index. Detail (per-test outcomes,
    per-chunk subprocess returncodes, error tracebacks) lives in the
    referenced report files; agent reads them on demand via
    `latest_attempt_dir / <mode_name>`.
    """
    health: Dict[str, Any] = {
        "latest_attempt": latest_attempt_dir.name if latest_attempt_dir else None,
        "modes": [],
    }
    if latest_attempt_dir is None:
        return health

    def _load(path: Path) -> Optional[Dict[str, Any]]:
        payload, error = _safe_read_json(path)
        if error or not isinstance(payload, dict):
            return None
        return payload

    synthetic_total = 0
    seen: Set[Path] = set()
    for state_name in ("start.json", "end.json"):
        merged = _load(latest_attempt_dir / state_name)
        merge_info = merged.get("_merge_info") if merged else None
        source_files = merge_info.get("source_files") if isinstance(merge_info, dict) else []
        if not isinstance(source_files, list):
            continue
        for source in source_files:
            source_path = Path(str(source))
            if not source_path.is_absolute():
                source_path = latest_attempt_dir / source_path
            if source_path in seen:
                continue
            seen.add(source_path)
            payload = _load(source_path)
            if payload is None:
                health["modes"].append({"name": source_path.name, "parseable": False})
                continue
            tests = payload.get("tests") if isinstance(payload.get("tests"), list) else []
            summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
            synthetic = [
                str(t.get("nodeid", ""))
                for t in tests
                if isinstance(t, dict)
                and str(t.get("nodeid", "")).startswith("__")
                and ("fatal" in str(t.get("nodeid", "")) or "timeout" in str(t.get("nodeid", "")))
            ]
            entry: Dict[str, Any] = {
                "name": source_path.name,
                "framework": payload.get("_framework"),
                "returncode": payload.get("_returncode"),
                "summary": {
                    key: summary.get(key)
                    for key in ("total", "collected", "passed", "failed", "skipped", "error")
                    if key in summary
                },
            }
            if synthetic:
                entry["synthetic_errors"] = synthetic
                synthetic_total += len(synthetic)
            health["modes"].append(entry)

    if synthetic_total:
        health["synthetic_error_count"] = synthetic_total
    return health


def _summary_count(summary: Optional[Dict[str, Any]], key: str) -> int:
    if not summary:
        return 0
    raw = summary.get("summary")
    if not isinstance(raw, dict):
        raw = summary
    value = _as_int(raw.get(key, 0))
    if key == "collected" and value == 0:
        value = _as_int(raw.get("total", 0))
    return value


def _classification_summary(classification: Optional[Dict[str, Any]]) -> Dict[str, int]:
    raw = classification.get("summary", {}) if classification else {}
    return {key: _as_int(raw.get(key, 0)) for key in set(TRANSITION_KEYS + ["total_before", "total_after"])}


def _baseline_metrics(work_dir: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    baseline, error = _safe_read_json(work_dir.parent / "base" / "baseline_metrics.json")
    if error:
        return baseline, error
    if baseline and baseline.get("runner_success") is not True:
        return baseline, "baseline runner_success is not true"
    return baseline, None


def _baseline_result_ratio(baseline: Optional[Dict[str, Any]], key: str) -> float:
    exec_results = (baseline or {}).get("exec_results", {})
    total_run = _as_int(exec_results.get("total_run", 0))
    if total_run <= 0:
        return 0.0
    return _as_int(exec_results.get(key, 0)) / total_run


def _test_id_path(test_id: str) -> str:
    return str(test_id).split("::", 1)[0]


def _stringify_error_records(value: Any) -> List[str]:
    records: List[str] = []
    if isinstance(value, dict):
        for inner in value.values():
            records.extend(_stringify_error_records(inner))
    elif isinstance(value, list):
        for item in value:
            records.extend(_stringify_error_records(item))
    elif value is not None:
        records.append(str(value))
    return records


def _summary_error_texts(*summaries: Any) -> str:
    chunks: List[str] = []
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        chunks.extend(_stringify_error_records(summary.get("collection_errors")))
        chunks.extend(_stringify_error_records(summary.get("error_groups")))
        results = summary.get("results")
        if isinstance(results, dict):
            chunks.extend(_stringify_error_records(results.get("collection_errors")))
            chunks.extend(_stringify_error_records(results.get("error_groups")))
            chunks.extend(_stringify_error_records(results.get("error")))
    return "\n".join(chunks)


def _git_show_path_exists(repo_path: Path, ref: str, path: str) -> Optional[bool]:
    if not path or not repo_path.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "cat-file", "-e", f"{ref}:{path}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return None
    return result.returncode == 0


def _git_show_file(repo_path: Path, ref: str, path: str) -> Optional[str]:
    if not path or not repo_path.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "show", f"{ref}:{path}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _nodeid_present_in_source(source: Optional[str], test_id: str) -> Optional[bool]:
    if source is None:
        return None
    parts = [part for part in str(test_id).split("::")[1:] if part]
    if not parts:
        return bool(source)
    leaf = parts[-1].split("[", 1)[0]
    if not leaf:
        return None
    patterns = (
        rf"\bdef\s+{re.escape(leaf)}\b",
        rf"\basync\s+def\s+{re.escape(leaf)}\b",
        rf"\bclass\s+{re.escape(leaf)}\b",
    )
    return any(re.search(pattern, source) for pattern in patterns)


def _summary_error_groups(*summaries: Any) -> List[Dict[str, Any]]:
    """Collect error_groups[] entries (with .reason and .count) from summaries."""
    out: List[Dict[str, Any]] = []
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        for container in (summary, summary.get("results")):
            if not isinstance(container, dict):
                continue
            groups = container.get("error_groups")
            if isinstance(groups, list):
                for entry in groups:
                    if isinstance(entry, dict):
                        out.append(entry)
    return out


def _verify_deleted_earlier_v2(
    waiver: Dict[str, Any],
    missing_patched: Set[str],
    *,
    repo_path: Path,
    base_state: str,
) -> Tuple[Set[str], Dict[str, Any], Optional[str]]:
    """v2 deleted_earlier: anchor is the missing nodeid; rationale already in waiver."""
    anchor_test_id = waiver["anchor"]
    test_path = _test_id_path(anchor_test_id)
    if not test_path:
        return (
            set(),
            {"anchor": anchor_test_id},
            "deleted_earlier: anchor must contain a test path (e.g. tests/foo.py::test_x)",
        )
    if anchor_test_id not in missing_patched:
        return set(), {"anchor": anchor_test_id}, "deleted_earlier: anchor is not in patched_not_in_results"

    path_exists = _git_show_path_exists(repo_path, base_state, test_path)
    if path_exists is None:
        return (
            set(),
            {"anchor": anchor_test_id, "test_path": test_path, "path_exists_at_start": None},
            "deleted_earlier: could not verify START source state (testbed git unavailable?)",
        )
    source = _git_show_file(repo_path, base_state, test_path) if path_exists else None
    node_present = _nodeid_present_in_source(source, anchor_test_id)
    if path_exists is True and node_present is not False:
        return (
            set(),
            {"anchor": anchor_test_id, "path_exists_at_start": True, "node_present_at_start": node_present},
            "deleted_earlier: nodeid still appears present at START — not actually deleted",
        )
    return (
        {anchor_test_id},
        {
            "anchor": anchor_test_id,
            "test_path": test_path,
            "path_exists_at_start": path_exists,
            "node_present_at_start": node_present,
        },
        None,
    )


def _verify_nodeid_absent_in_states_v2(
    waiver: Dict[str, Any],
    missing_patched: Set[str],
    *,
    repo_path: Path,
    base_state: str,
    end_state: str,
) -> Tuple[Set[str], Dict[str, Any], Optional[str]]:
    """v2 nodeid_absent_in_states: anchor is missing nodeid; verify it's absent from
    BOTH start and end source states."""
    anchor_test_id = waiver["anchor"]
    test_path = _test_id_path(anchor_test_id)
    if not test_path:
        return set(), {"anchor": anchor_test_id}, "nodeid_absent_in_states: anchor must contain a test path"
    if anchor_test_id not in missing_patched:
        return set(), {"anchor": anchor_test_id}, "nodeid_absent_in_states: anchor is not in patched_not_in_results"
    state_results: Dict[str, Dict[str, Any]] = {}
    for label, ref in (("start", base_state), ("end", end_state)):
        path_exists = _git_show_path_exists(repo_path, ref, test_path)
        if path_exists is None:
            return (
                set(),
                {"anchor": anchor_test_id, label: {"path_exists": None}},
                f"nodeid_absent_in_states: could not verify {label.upper()} source state",
            )
        source = _git_show_file(repo_path, ref, test_path) if path_exists else None
        node_present = False if not path_exists else _nodeid_present_in_source(source, anchor_test_id)
        state_results[label] = {"ref": ref, "path_exists": path_exists, "node_present": node_present}
        if node_present is None:
            return (
                set(),
                {"anchor": anchor_test_id, label: state_results[label]},
                f"nodeid_absent_in_states: could not verify nodeid at {label.upper()}",
            )
        if node_present:
            return (
                set(),
                {"anchor": anchor_test_id, label: state_results[label]},
                f"nodeid_absent_in_states: nodeid appears present at {label.upper()}",
            )
    return (
        {anchor_test_id},
        {"anchor": anchor_test_id, "test_path": test_path, "states": state_results, "covered": [anchor_test_id]},
        None,
    )


def _earliest_symbol_intro_commit(repo_path: Path, symbol: Optional[str], path: Optional[str]) -> Optional[str]:
    """Find the earliest commit that introduced ``symbol`` in ``path`` via git log -S."""
    if not symbol or not path or not repo_path.exists():
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "log",
                "--all",
                "--format=%H",
                "-S",
                symbol,
                "--",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    commits = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return commits[-1] if commits else None  # last in --format=%H is earliest chronologically


def _commit_is_ancestor(repo_path: Path, commit: str, ref: str) -> Optional[bool]:
    """True iff ``commit`` is an ancestor of ``ref``. None on git failure."""
    if not commit or not ref or not repo_path.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "merge-base", "--is-ancestor", commit, ref],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


# ─────────────────────────────────────────────────────────────────────────────
# v3 verify helpers — M3-actionable bound_waiver verification
# (waiver_m3_actionable_plan §4)
# ─────────────────────────────────────────────────────────────────────────────


def _empty_offset() -> Dict[str, Any]:
    """Zero offset for a rejected waiver (no gate contribution)."""
    return {"h_patched_covered": set(), "h_error_start_count": 0, "h_error_end_count": 0}


def _extract_test_function_name(nodeid: str) -> Optional[str]:
    """Extract pytest test function name from a nodeid like ``tests/x.py::Cls::test_foo``."""
    if not nodeid or "::" not in nodeid:
        return None
    last = nodeid.split("::")[-1]
    if "[" in last:
        last = last.split("[", 1)[0]
    if not last:
        return None
    if not (last.startswith("test_") or last.startswith("Test")):
        return None
    return last


def _anchor_symptom_check(
    anchor: str,
    attached_gates: List[str],
    missing_set: Set[str],
    start_error_groups: List[Dict[str, Any]],
    end_error_groups: List[Dict[str, Any]],
) -> Optional[str]:
    """Universal anchor pool check (§4.4 通用 anchor symptom 校验).

    - ``H_patched_completeness`` ∈ attached_gates → anchor must be in ``missing_set``
    - ``H_error_within_budget`` ∈ attached_gates → anchor must appear in
      ``error_groups[].reason`` on at least one side (start or end)
    """
    if "H_patched_completeness" in attached_gates:
        if anchor not in missing_set:
            return "anchor not in patched_not_in_results (required when H_patched_completeness is in attached_gates)"
    if "H_error_within_budget" in attached_gates:
        s_hit = any(str(g.get("reason", "")) == anchor for g in start_error_groups)
        e_hit = any(str(g.get("reason", "")) == anchor for g in end_error_groups)
        if not (s_hit or e_hit):
            return (
                "anchor not in error_groups[].reason on either side (required "
                "when H_error_within_budget is in attached_gates)"
            )
    return None


def _compute_offset(
    anchor: str,
    attached_gates: List[str],
    missing_set: Set[str],
    start_error_groups: List[Dict[str, Any]],
    end_error_groups: List[Dict[str, Any]],
    *,
    cluster_test_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute per-gate offset for a verified waiver.

    Only contributes to gates listed in ``attached_gates``.

    ``cluster_test_path`` (used by ``feature_introduced_later``): when set and
    ``H_patched_completeness`` ∈ attached_gates, all missing patched tests under
    that test_path are covered (matches legacy ``symbol_introduced_later`` v2
    behavior — same root cause covers the whole file).
    """
    offset = _empty_offset()
    if "H_patched_completeness" in attached_gates:
        if anchor in missing_set:
            offset["h_patched_covered"].add(anchor)
        if cluster_test_path:
            for nodeid in missing_set:
                if _test_id_path(nodeid) == cluster_test_path:
                    offset["h_patched_covered"].add(nodeid)
    if "H_error_within_budget" in attached_gates:
        for g in start_error_groups:
            if str(g.get("reason", "")) == anchor:
                offset["h_error_start_count"] += _as_int(g.get("count", 0))
        for g in end_error_groups:
            if str(g.get("reason", "")) == anchor:
                offset["h_error_end_count"] += _as_int(g.get("count", 0))
    return offset


def _verify_test_out_of_scope(
    waiver: Dict[str, Any],
    missing_set: Set[str],
    start_error_groups: List[Dict[str, Any]],
    end_error_groups: List[Dict[str, Any]],
    *,
    repo_path: Path,
    end_state: str,
    test_dirs: List[str],
) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[str]]:
    """Path A: test_out_of_scope — test introduced in future commit; M3 removes from scope."""
    anchor = waiver["anchor"]
    test_path = waiver.get("test_path") or ""
    attached_gates = waiver["attached_gates"]
    evidence: Dict[str, Any] = {"anchor": anchor, "test_path": test_path}

    if not _has_legal_path_prefix(test_path, test_dirs):
        return (
            _empty_offset(),
            evidence,
            (f"test_out_of_scope: test_path {test_path!r} must be under test_dirs={test_dirs}"),
        )

    err = _anchor_symptom_check(anchor, attached_gates, missing_set, start_error_groups, end_error_groups)
    if err:
        return _empty_offset(), evidence, f"test_out_of_scope: {err}"

    test_fn = _extract_test_function_name(anchor)
    if not test_fn:
        return (
            _empty_offset(),
            evidence,
            (
                "test_out_of_scope: could not extract test function name from anchor "
                "(expected nodeid like tests/x.py::Class::test_foo)"
            ),
        )
    intro = _earliest_symbol_intro_commit(repo_path, test_fn, test_path)
    evidence["test_function"] = test_fn
    evidence["introduction_commit"] = intro
    if intro is None:
        return _empty_offset(), evidence, (f"test_out_of_scope: no commit defining {test_fn!r} in {test_path!r}")
    reachable = _commit_is_ancestor(repo_path, intro, end_state)
    evidence["reachable_from_end_state"] = reachable
    if reachable is True:
        return (
            _empty_offset(),
            evidence,
            (
                f"test_out_of_scope: defining commit {intro[:10]} IS reachable from {end_state} "
                "— test exists in milestone scope, this is not Path A"
            ),
        )
    if reachable is None:
        return _empty_offset(), evidence, "test_out_of_scope: could not check ancestry against end_state"

    offset = _compute_offset(anchor, attached_gates, missing_set, start_error_groups, end_error_groups)
    return offset, evidence, None


def _verify_feature_introduced_later(
    waiver: Dict[str, Any],
    missing_set: Set[str],
    start_error_groups: List[Dict[str, Any]],
    end_error_groups: List[Dict[str, Any]],
    signals_data: Optional[Dict[str, Any]],
    *,
    repo_path: Path,
    end_state: str,
    test_dirs: List[str],
    repo_src_dirs: List[str],
) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[str]]:
    """Path B: feature_introduced_later — future production symbol; M3 cherry-picks paired commit_signal."""
    anchor = waiver["anchor"]
    symbol = waiver.get("symbol") or ""
    path_field = waiver.get("path") or ""
    attached_gates = waiver["attached_gates"]
    waiver_id = waiver["id"]
    evidence: Dict[str, Any] = {"anchor": anchor, "symbol": symbol, "path": path_field}

    if not _is_valid_feature_symbol(symbol):
        return (
            _empty_offset(),
            evidence,
            (
                f"feature_introduced_later: symbol {symbol!r} must be a clean identifier, "
                "'(async )?def NAME', or 'class NAME' — code fragments and test function names rejected"
            ),
        )

    if not _has_legal_path_prefix(path_field, repo_src_dirs):
        return (
            _empty_offset(),
            evidence,
            (
                f"feature_introduced_later: path {path_field!r} must be under "
                f"repo_src_dirs={repo_src_dirs} (production source only)"
            ),
        )
    if _has_legal_path_prefix(path_field, test_dirs):
        return (
            _empty_offset(),
            evidence,
            (
                f"feature_introduced_later: path {path_field!r} must NOT be under "
                f"test_dirs={test_dirs} (path B is for production symbols, not test code)"
            ),
        )

    err = _anchor_symptom_check(anchor, attached_gates, missing_set, start_error_groups, end_error_groups)
    if err:
        return _empty_offset(), evidence, f"feature_introduced_later: {err}"

    intro = _earliest_symbol_intro_commit(repo_path, symbol, path_field)
    evidence["introduction_commit"] = intro
    if intro is None:
        return _empty_offset(), evidence, (f"feature_introduced_later: no commit defining {symbol!r} in {path_field!r}")
    reachable = _commit_is_ancestor(repo_path, intro, end_state)
    evidence["reachable_from_end_state"] = reachable
    if reachable is True:
        return (
            _empty_offset(),
            evidence,
            (
                f"feature_introduced_later: defining commit {intro[:10]} IS reachable from "
                f"{end_state} — symbol already in milestone scope"
            ),
        )
    if reachable is None:
        return _empty_offset(), evidence, "feature_introduced_later: could not check ancestry"

    # MANDATORY paired commit_signal
    paired_move = None
    commit_moves = (signals_data or {}).get("commit_moves", []) or []
    for m in commit_moves:
        if m.get("_schema_errors"):
            continue
        resolves = m.get("resolves") or {}
        if waiver_id in (resolves.get("bound_waiver_ids") or []):
            paired_move = m
            break
    if paired_move is None:
        return (
            _empty_offset(),
            evidence,
            (
                f"feature_introduced_later: MANDATORY paired commit_signal missing — "
                f"add a commit_moves[] entry in module2_signals.json whose "
                f"resolves.bound_waiver_ids includes id={waiver_id}"
            ),
        )
    evidence["paired_commit_signal_id"] = paired_move.get("id")
    evidence["paired_resolving_commit"] = paired_move.get("resolving_commit")

    # Cluster the H_patched offset by test_path (when anchor is a nodeid)
    cluster_path = _test_id_path(anchor) if ("::" in anchor and "H_patched_completeness" in attached_gates) else None
    offset = _compute_offset(
        anchor,
        attached_gates,
        missing_set,
        start_error_groups,
        end_error_groups,
        cluster_test_path=cluster_path,
    )
    return offset, evidence, None


def verify_bound_waivers(
    bound_waivers: List[Dict[str, Any]],
    missing_patched: List[str],
    start_error_groups: List[Dict[str, Any]],
    end_error_groups: List[Dict[str, Any]],
    signals_data: Optional[Dict[str, Any]] = None,
    *,
    work_dir: Path,
    base_state: str,
    end_state: str,
    test_dirs: Optional[List[str]] = None,
    repo_src_dirs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Dispatch v3 bound_waiver verification by reason_code with multi-gate offset.

    See ``module2_waiver_m3_actionable_plan.md`` §4.

    Returns a dict with per-gate aggregations:
    - ``covered_missing`` — sorted nodeids covered for H_patched_completeness
    - ``waived_start_error_count`` / ``waived_end_error_count`` — error count
      offset for H_error_within_budget per side
    - ``verified`` — list of verified waiver dicts with evidence + attached_gates
    - ``invalid`` — list of failed waiver dicts with error reasons
    """
    missing_set = {str(t) for t in missing_patched}
    repo_path = work_dir / "testbed"
    test_dirs = test_dirs or []
    repo_src_dirs = repo_src_dirs or []
    verified: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []
    covered_missing: Set[str] = set()
    waived_start_error = 0
    waived_end_error = 0

    for waiver in bound_waivers:
        wid = waiver.get("id")
        reason_code = waiver["reason_code"]
        waiver_label = f"bound_waivers[id={wid}]"

        # Reject entries with schema errors before running any git commands.
        if waiver.get("schema_errors"):
            invalid.append(
                {
                    "id": waiver_label,
                    "reason_code": reason_code,
                    "error": "schema invalid: " + "; ".join(waiver["schema_errors"]),
                }
            )
            continue

        # Dispatch v3 reason_codes
        if reason_code == "test_out_of_scope":
            offset, evidence, err = _verify_test_out_of_scope(
                waiver,
                missing_set,
                start_error_groups,
                end_error_groups,
                repo_path=repo_path,
                end_state=end_state,
                test_dirs=test_dirs,
            )
        elif reason_code == "feature_introduced_later":
            offset, evidence, err = _verify_feature_introduced_later(
                waiver,
                missing_set,
                start_error_groups,
                end_error_groups,
                signals_data,
                repo_path=repo_path,
                end_state=end_state,
                test_dirs=test_dirs,
                repo_src_dirs=repo_src_dirs,
            )
        elif reason_code == "nodeid_absent_in_states":
            cov, evidence, err = _verify_nodeid_absent_in_states_v2(
                waiver,
                missing_set,
                repo_path=repo_path,
                base_state=base_state,
                end_state=end_state,
            )
            offset = _empty_offset()
            if err is None and "H_patched_completeness" in waiver["attached_gates"]:
                offset["h_patched_covered"] = cov
        elif reason_code == "deleted_earlier":
            cov, evidence, err = _verify_deleted_earlier_v2(
                waiver,
                missing_set,
                repo_path=repo_path,
                base_state=base_state,
            )
            offset = _empty_offset()
            if err is None and "H_patched_completeness" in waiver["attached_gates"]:
                offset["h_patched_covered"] = cov
        else:  # pragma: no cover (load-time validation prevents this)
            invalid.append(
                {
                    "id": waiver_label,
                    "reason_code": reason_code,
                    "error": f"unsupported reason_code: {reason_code}",
                }
            )
            continue

        if err:
            invalid.append(
                {
                    "id": waiver_label,
                    "reason_code": reason_code,
                    "error": err,
                    "evidence": evidence,
                }
            )
            continue

        # Aggregate offset across gates
        covered_missing.update(offset["h_patched_covered"])
        waived_start_error += offset["h_error_start_count"]
        waived_end_error += offset["h_error_end_count"]
        verified.append(
            {
                "id": waiver_label,
                "waiver_id": wid,
                "reason_code": reason_code,
                "attached_gates": list(waiver["attached_gates"]),
                "anchor": waiver["anchor"],
                "rationale": waiver["rationale"],
                "covered": sorted(offset["h_patched_covered"]) if offset["h_patched_covered"] else None,
                "waived_start_count": offset["h_error_start_count"] or None,
                "waived_end_count": offset["h_error_end_count"] or None,
                "evidence": evidence,
            }
        )

    return {
        "covered_missing": sorted(covered_missing),
        "waived_start_error_count": waived_start_error,
        "waived_end_error_count": waived_end_error,
        "verified": verified,
        "invalid": invalid,
    }


def _waiver_audit_gate_summary(
    waiver_audit: Dict[str, Any],
    *,
    gate: str,
    reason_codes: Set[str],
) -> Dict[str, Any]:
    """Small gate-facing waiver summary.

    Full machine-verification evidence stays in ``raw_metrics`` so gate evidence
    remains readable and doesn't duplicate ``module2_waivers.json``.
    """
    verified = [item for item in waiver_audit.get("verified", []) if gate in (item.get("attached_gates") or [])]
    invalid = [
        item
        for item in waiver_audit.get("invalid", [])
        if item.get("reason_code") in reason_codes or item.get("id") == "module2_waivers"
    ]
    return {
        "details_ref": "raw_metrics.verified_bound_waivers / raw_metrics.invalid_bound_waivers",
        "verified_ids": [item.get("id") for item in verified],
        "invalid_ids": [item.get("id") for item in invalid],
        "invalid_errors": [
            {
                "id": item.get("id"),
                "reason_code": item.get("reason_code"),
                "error": item.get("error"),
            }
            for item in invalid
        ],
    }


def evaluate_build_status(
    milestone_id: str,
    work_dir: Path,
    dockerfiles_dir: Path,
    repo_id: str,
    test_scenario: str,
    base_image: str,
    base_state: str,
    end_state: str,
    *,
    image_name: Optional[str] = None,
    latest_attempt_dir: Optional[Path] = None,
    agent_success: Optional[bool] = None,
    agent_required: bool = True,
    agent_history: Optional[List[Dict[str, Any]]] = None,
    tests_subprocess_ok: Optional[bool] = None,
    lint_violations: Optional[List[Any]] = None,
    image_exists: Optional[bool] = None,
    state_tags_ok: Optional[bool] = None,
    state_tags_evidence: Optional[Dict[str, Any]] = None,
    repo_src_dirs: Optional[List[str]] = None,
    test_dir: str = "test",
    test_dirs: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
    test_infra_patterns: Optional[List[str]] = None,
    pristine_base_sha: Optional[str] = None,
    pristine_end_sha: Optional[str] = None,
    thresholds: Optional[Dict[str, Any]] = None,
    allow_latest_attempt_fallback: bool = True,
) -> Dict[str, Any]:
    """Evaluate Spec v0.9 hard gates and return the build_status schema dict.

    ``test_dirs``, ``exclude_patterns``, ``test_infra_patterns``,
    ``pristine_base_sha``, ``pristine_end_sha`` feed the
    ``B_lint_block_clean::large_src_append`` / ``src_function_replaced`` sub-rules
    backed by ``git diff --numstat`` against the pristine testbed SHAs. The
    architecture is per-class numstat thresholds — see
    ``module2_lint_class_threshold_plan.md`` for the file-class table and
    ``module2_commit_signal_plan.md`` v0.5 §2.3 for the design rationale.
    """
    docker_dir = dockerfiles_dir / milestone_id
    dockerfile_path = docker_dir / "Dockerfile"
    test_config_path = docker_dir / "test_config.json"  # noqa: F841
    image_name = image_name or f"{repo_id}/{test_scenario}/{milestone_id}:latest"
    thresholds = thresholds or load_validation_thresholds(work_dir, repo_id)
    if latest_attempt_dir is None and allow_latest_attempt_fallback:
        latest_attempt_dir = _latest_attempt_path(work_dir, milestone_id)

    # Load waivers + signals early so both the early-return path (when tests
    # don't run) and the main path can surface their schema-validation results
    # in raw_metrics. verify_bound_waivers needs test summaries and runs later
    # in the main path; schema-load errors flow into invalid_bound_waivers here.
    bound_waivers, unbound_issues, agent_sufficient_data, waiver_load_errors = load_module2_waivers(
        docker_dir,
        milestone_id,
    )
    # Collect valid id sets for verify_commit_signals (schema v2 — id-based refs).
    valid_bound_waiver_ids = {
        w["id"] for w in bound_waivers if isinstance(w.get("id"), int) and not w.get("schema_errors")
    }
    valid_unbound_issue_ids = {
        u["id"] for u in unbound_issues if isinstance(u.get("id"), int) and not u.get("schema_errors")
    }
    commit_moves, dockerfile_diff, remaining_unbound_issue_ids, signal_load_errors = load_module2_signals(
        docker_dir,
        milestone_id,
        bound_waivers_count=len(bound_waivers),
        unbound_issues_count=len(unbound_issues),
    )
    if signal_load_errors:
        commit_signal_audit = {
            "verified": [],
            "invalid": [{"id": "module2_signals", "error": err} for err in signal_load_errors],
            "count": 0,
            "dockerfile_diff_error": None,
        }
    else:
        commit_signal_audit = verify_commit_signals(
            commit_moves,
            dockerfile_diff,
            work_dir=work_dir,
            end_state=end_state,
            valid_bound_waiver_ids=valid_bound_waiver_ids,
            valid_unbound_issue_ids=valid_unbound_issue_ids,
        )

    # MANDATORY commit_signal detection (waiver_m3_actionable_plan §4.7):
    # narrowed to per-waiver pairing for feature_introduced_later only.
    paired_waiver_ids: Set[int] = set()
    for m in commit_signal_audit.get("verified", []):
        for wid in (m.get("resolves") or {}).get("bound_waiver_ids", []) or []:
            paired_waiver_ids.add(wid)
    unpaired_feature_waivers = [
        w
        for w in bound_waivers
        if w.get("reason_code") == "feature_introduced_later"
        and not w.get("schema_errors")
        and isinstance(w.get("id"), int)
        and w["id"] not in paired_waiver_ids
    ]
    commit_signal_missing_warning: Optional[str] = None
    if unpaired_feature_waivers:
        sample_ids = [w["id"] for w in unpaired_feature_waivers[:5]]
        commit_signal_missing_warning = (
            f"{len(unpaired_feature_waivers)} feature_introduced_later bound_waiver(s) "
            f"have no paired commit_signal. Each MANDATES a commit_moves[] entry whose "
            f"resolves.bound_waiver_ids includes the waiver's id. "
            f"Unpaired waiver ids (up to 5): {sample_ids}. "
            f"See retry_context.md.j2 item 7 + workflow.md.j2 Phase 4.5 Exit 3."
        )

    # candidates_pending_warning: when agent_sufficient.value=true but
    # commit_moves[] still has status='pending' entries, the agent gave up
    # mid-exploration. Surface this so retry_context can push for completion.
    pending_count = commit_signal_audit.get("pending_count", 0)
    failed_count = commit_signal_audit.get("failed_count", 0)  # noqa: F841
    candidates_pending_warning: Optional[str] = None
    if pending_count > 0 and agent_sufficient_data.get("value"):
        pending_entries = commit_signal_audit.get("pending", [])
        sample = [
            {
                "id": e.get("move_id"),
                "symbol": e.get("intended_symbol"),
                "path": e.get("intended_path"),
                "sha": (e.get("resolving_commit") or "")[:12],
            }
            for e in pending_entries[:5]
        ]
        candidates_pending_warning = (
            f"{pending_count} commit_moves[] entries have status='pending' but "
            f"agent_sufficient.value=true. Each pending candidate has a discovered "
            f"future SHA — you MUST attempt sandbox cherry-pick OR explicitly mark "
            f"status='attempted_failed' with attempt_log before declaring sufficient. "
            f"Sample pending (up to 5): {sample}."
        )

    # unbound_issue_quality_warnings: soft check on evidence quality. Does NOT
    # fail any gate; surfaces in raw_metrics for retry_context to push agent
    # away from lumped/boilerplate unbound entries.
    # Per workflow Exit 2: evidence must have root_cause + m3_action, and
    # a single unbound_issue should not cover >50 distinct error_group reasons.
    unbound_issue_quality_warnings: List[Dict[str, Any]] = []
    _LUMP_THRESHOLD = 50
    for u in unbound_issues:
        if u.get("schema_errors"):
            continue
        ev = u.get("evidence")
        if not isinstance(ev, dict):
            ev = {}
        per_issue: List[str] = []
        root_cause = ev.get("root_cause")
        if not isinstance(root_cause, str) or not root_cause.strip():
            per_issue.append("evidence.root_cause missing or empty (required)")
        m3_action = ev.get("m3_action")
        if not isinstance(m3_action, str) or not m3_action.strip():
            per_issue.append("evidence.m3_action missing or empty (required)")
        aeg = ev.get("affected_error_groups")
        if isinstance(aeg, list) and len(aeg) > _LUMP_THRESHOLD:
            per_issue.append(
                f"covers {len(aeg)} error_groups — likely lumping; split into multiple "
                f"unbound_issues with distinct root_causes (threshold {_LUMP_THRESHOLD})"
            )
        if per_issue:
            unbound_issue_quality_warnings.append(
                {
                    "id": u.get("id"),
                    "kind": u.get("kind"),
                    "summary": (u.get("summary") or "")[:120],
                    "warnings": per_issue,
                }
            )

    # Cross-issue boilerplate check: many issues sharing same root_cause string
    valid_unbound_with_evidence = [
        u for u in unbound_issues if not u.get("schema_errors") and isinstance(u.get("evidence"), dict)
    ]
    if len(valid_unbound_with_evidence) >= 3:
        root_causes = [(u.get("evidence", {}) or {}).get("root_cause", "") for u in valid_unbound_with_evidence]
        non_empty = [rc.strip() for rc in root_causes if isinstance(rc, str) and rc.strip()]
        if non_empty:
            unique_rc = set(non_empty)
            if len(unique_rc) < max(2, len(non_empty) // 2):
                unbound_issue_quality_warnings.append(
                    {
                        "id": None,
                        "kind": "global_boilerplate_check",
                        "warnings": [
                            f"Found {len(non_empty)} unbound_issues with only "
                            f"{len(unique_rc)} distinct root_cause strings — boilerplate "
                            f"detected. Each unbound_issue must name its own specific root cause."
                        ],
                    }
                )

    if lint_violations is None:
        try:
            from .dockerfile_lint import lint_dockerfile

            lint_violations = lint_dockerfile(
                dockerfile_path=dockerfile_path,
                src_dirs=repo_src_dirs or [],
                test_dir=test_dir,
                allowed_tags=[base_state, end_state],
            )
        except Exception as e:
            lint_violations = [
                {
                    "rule": "lint_exception",
                    "severity": "block",
                    "line": 0,
                    "command": "",
                    "detail": str(e),
                }
            ]

    # Numstat-based per-class lint check (B_lint_block_clean::large_src_append +
    # src_function_replaced). Architecture: classify each changed file into one of
    # nine FileClass labels, then apply per-class thresholds from
    # ``thresholds["per_class_thresholds"]``. Files classified ``test_infrastructure``
    # or ``non_src_non_test`` are unchecked by design. Rust files get hunk-level
    # refinement to split src vs `#[cfg(test)]` inline test ranges.
    # See module2_lint_class_threshold_plan.md for the full design.
    per_class_lint_evidence: Dict[str, Any] = {"skipped_reason": None}
    src_diff_violations: List[Dict[str, Any]] = []
    if (
        (pristine_base_sha or pristine_end_sha)
        and image_name
        and image_exists is not False  # may be None if not yet checked
    ):
        try:
            from deepcommit.harness.utils.src_filter import SrcFileFilter

            src_filter_inst = SrcFileFilter(
                src_dirs=repo_src_dirs or [],
                test_dirs=test_dirs or [],
                exclude_patterns=exclude_patterns or [],
                test_infra_patterns=test_infra_patterns,
            )
        except Exception as e:
            src_filter_inst = None
            per_class_lint_evidence["src_filter_error"] = str(e)

        per_file_stats, debug = _docker_src_diff_numstat(
            image_name=image_name,
            pristine_base_sha=pristine_base_sha,
            pristine_end_sha=pristine_end_sha,
            base_state=base_state,
            end_state=end_state,
            repo_src_dirs=repo_src_dirs or [],
        )
        per_class_lint_evidence.update(debug)
        if per_file_stats and src_filter_inst is not None:
            per_class_thresholds = dict(thresholds.get("per_class_thresholds") or {})
            function_replaced_thresholds = dict(thresholds.get("src_function_replaced") or {})
            function_replaced_min_paired = int(function_replaced_thresholds.get("per_file_min_paired", 50))
            if per_class_thresholds:
                src_diff_violations, eval_evidence = evaluate_per_class_thresholds(
                    per_file_stats,
                    src_filter_inst,
                    per_class_thresholds,
                    image_name=image_name,
                    pristine_base_sha=pristine_base_sha,
                    pristine_end_sha=pristine_end_sha,
                    base_state=base_state,
                    end_state=end_state,
                    function_replaced_min_paired=function_replaced_min_paired,
                )
            else:
                # Backward compat: no per-class thresholds configured — fall back to legacy
                # single-threshold ``evaluate_large_src_append``. Drop once all repos have
                # migrated to per_class_thresholds (planned cleanup after Phase B ships).
                src_diff_violations, eval_evidence = evaluate_large_src_append(
                    per_file_stats,
                    src_filter_inst,
                    large_src_thresholds=dict(thresholds.get("large_src_append") or {}),
                    function_replaced_thresholds=function_replaced_thresholds,
                )
            per_class_lint_evidence.update(eval_evidence)
            per_class_lint_evidence["violations"] = src_diff_violations
    else:
        if not (pristine_base_sha or pristine_end_sha):
            per_class_lint_evidence["skipped_reason"] = "no pristine SHAs supplied"
        elif image_exists is False:
            per_class_lint_evidence["skipped_reason"] = "image not built"

    lint_violations = list(lint_violations) + list(src_diff_violations)

    lint_blockers = [
        v
        for v in lint_violations
        if getattr(v, "severity", None) == "block" or (isinstance(v, dict) and v.get("severity") == "block")
    ]
    lint_warnings = [
        v
        for v in lint_violations
        if getattr(v, "severity", None) == "warn" or (isinstance(v, dict) and v.get("severity") == "warn")
    ]
    lint_evidence = []
    for violation in lint_violations:
        if hasattr(violation, "__dict__"):
            lint_evidence.append(dict(violation.__dict__))
        else:
            lint_evidence.append(dict(violation))

    actual_base_image = _dockerfile_base_image(dockerfile_path)
    if image_exists is None:
        image_exists = _docker_image_exists(image_name)
    image_id = _docker_image_id(image_name) if image_exists else None
    if state_tags_ok is None:
        if image_exists:
            state_tags_ok, state_tags_evidence = _docker_state_tags_consistent(
                image_name,
                base_state,
                end_state,
            )
        else:
            state_tags_ok, state_tags_evidence = False, {"reason": "image missing"}

    build_run_integrity = {
        "B_lint_block_clean": _gate(
            not lint_blockers,
            evidence={"violations": lint_evidence},
        ),
        "B_base_image_consistent": _gate(
            actual_base_image == base_image,
            evidence={"expected": base_image, "actual": actual_base_image},
        ),
        "B_image_exists": _gate(bool(image_exists), evidence={"image": image_name}),
        "B_state_tags_consistent": _gate(
            bool(state_tags_ok),
            evidence={
                "base_state": base_state,
                "end_state": end_state,
                **(state_tags_evidence or {}),
            },
        ),
        "B_agent_completed": _gate(
            (not agent_required) or bool(agent_success),
            evidence={
                "agent_required": agent_required,
                "agent_success": agent_success,
            },
        ),
    }

    report_errors: Dict[str, str] = {}
    reports_parseable = True
    report_paths = {}
    if latest_attempt_dir:
        for name in ("start.json", "end.json", "classification.json"):
            path = latest_attempt_dir / name
            report_paths[name] = str(path)
            ok, error = _json_exists_and_parseable(path)
            if not ok:
                reports_parseable = False
                report_errors[name] = error or "invalid"
    else:
        reports_parseable = False
        report_errors["latest_attempt"] = "no attempt_N directory"
    report_health = _validation_report_health(latest_attempt_dir)

    tests_ok = bool(tests_subprocess_ok) and reports_parseable
    gate_evidence: Dict[str, Any] = {"report_paths": report_paths}
    if not tests_ok:
        gate_evidence["subprocess_ok"] = tests_subprocess_ok
        gate_evidence["reports_parseable"] = reports_parseable
        if report_errors:
            gate_evidence["errors"] = report_errors
    build_run_integrity["B_tests_ran_successfully"] = _gate(
        tests_ok,
        evidence=gate_evidence,
    )

    if not tests_ok:
        unevaluated_reason = (
            "quality gates require a successful, parseable validation attempt; "
            "current attempt did not complete normally"
        )
        unevaluated_evidence: Dict[str, Any] = {
            "reason": unevaluated_reason,
            "report_paths": report_paths,
        }
        if report_errors:
            unevaluated_evidence["errors"] = report_errors
        test_collection_and_result_quality = {
            gate_name: _unevaluated_gate(
                value=None,
                threshold=thresholds.get(gate_name),
                evidence=unevaluated_evidence,
            )
            for gate_name in QUALITY_GATES
        }
        failed_gates = [gate_name for gate_name in BUILD_RUN_GATES if not build_run_integrity[gate_name]["passed"]]
        return {
            "schema_version": 1,
            "milestone_id": milestone_id,
            "generated_at": _utc_now_iso(),
            "built": False,
            "failed_gates": failed_gates,
            "build_run_integrity": build_run_integrity,
            "test_collection_and_result_quality": test_collection_and_result_quality,
            "raw_metrics": {
                "transitions_full": None,
                "transitions_patched": None,
                "errors": {
                    "collection_errors_count": None,
                    "error_groups_count": None,
                    "lint_warnings_count": len(lint_warnings),
                },
                "validation_report_health": report_health,
                "per_class_lint_evidence": per_class_lint_evidence,
                # waiver + signal schema-validation results — surfaced even when
                # tests didn't run so retry context can see invalid_* errors. The
                # full verify_bound_waivers audit (which needs test summaries)
                # runs only in the main path; here we only have load errors.
                "unbound_issues_count": len(unbound_issues),
                "verified_bound_waivers": [],
                "invalid_bound_waivers": [
                    {"id": "module2_waivers", "reason_code": None, "error": err} for err in waiver_load_errors
                ],
                "agent_sufficient": agent_sufficient_data,
                "remaining_unbound_issue_ids": remaining_unbound_issue_ids,
                "commit_signals_count": commit_signal_audit["count"],
                "candidates_pending_count": commit_signal_audit.get("pending_count", 0),
                "candidates_pending": commit_signal_audit.get("pending", []),
                "candidates_attempted_failed_count": commit_signal_audit.get("failed_count", 0),
                "candidates_attempted_failed": commit_signal_audit.get("failed", []),
                "verified_commit_signals": commit_signal_audit["verified"],
                "invalid_commit_signals": commit_signal_audit["invalid"],
                "commit_signal_missing_warning": commit_signal_missing_warning,
                "candidates_pending_warning": candidates_pending_warning,
                "unbound_issue_quality_warnings": unbound_issue_quality_warnings,
            },
            "artifact_paths": {
                "latest_attempt": latest_attempt_dir.name if latest_attempt_dir else None,
            },
            "inputs": current_build_inputs(docker_dir),
            "image": {
                "name": image_name,
                "id": image_id,
            },
            "agent_history": agent_history or [],
        }

    start_summary, start_summary_error = (
        _safe_read_json(latest_attempt_dir / "start_summary.json")
        if latest_attempt_dir
        else (None, "no latest attempt")
    )
    end_summary, end_summary_error = (
        _safe_read_json(latest_attempt_dir / "end_summary.json") if latest_attempt_dir else (None, "no latest attempt")
    )
    classification, classification_error = (
        _safe_read_json(latest_attempt_dir / "classification.json")
        if latest_attempt_dir
        else (None, "no latest attempt")
    )
    baseline, baseline_error = _baseline_metrics(work_dir)
    patched, patched_error = _safe_read_json(work_dir / "milestone_patched_tests" / f"{milestone_id}.json")
    start_error_groups = _summary_error_groups(start_summary)
    end_error_groups = _summary_error_groups(end_summary)

    class_summary = _classification_summary(classification)
    patched_summary = patched.get("summary", {}) if patched else {}
    patched_collected = _as_int(patched_summary.get("patched_collected", 0))
    patched_effective = _as_int(patched_summary.get("effective_tests", 0))
    missing_patched = patched.get("collected", {}).get("patched_not_in_results", []) if patched else []
    if not isinstance(missing_patched, list):
        missing_patched = []

    # Reject all bound_waivers when load-time validation found schema problems —
    # plan §4.1 requires v2 schema validity as a precondition.
    if waiver_load_errors:
        waiver_audit = {
            "covered_missing": [],
            "waived_start_error_count": 0,
            "waived_end_error_count": 0,
            "verified": [],
            "invalid": [{"id": "module2_waivers", "reason_code": None, "error": err} for err in waiver_load_errors],
        }
    else:
        signals_data_for_verify = (
            {
                "commit_moves": commit_moves,
            }
            if not signal_load_errors
            else None
        )
        waiver_audit = verify_bound_waivers(
            bound_waivers,
            missing_patched,
            start_error_groups,
            end_error_groups,
            signals_data_for_verify,
            work_dir=work_dir,
            base_state=base_state,
            end_state=end_state,
            test_dirs=test_dirs or [],
            repo_src_dirs=repo_src_dirs or [],
        )
    verified_waived = waiver_audit["covered_missing"]
    residual_missing = sorted(set(missing_patched) - set(verified_waived))
    adjusted_patched_collected = min(patched_effective, patched_collected + len(verified_waived))
    completeness_threshold = _as_float(thresholds.get("H_patched_completeness"), 1.0)
    if patched_effective == 0:
        patched_completeness_value = 1.0
        patched_completeness_passed = patched_error is None
    else:
        patched_completeness_value = adjusted_patched_collected / patched_effective
        patched_completeness_passed = patched_error is None and patched_completeness_value >= completeness_threshold

    stable_patched = patched_summary.get("stable_classification", {}) if patched else {}
    patched_p2p = _as_int(stable_patched.get("pass_to_pass", 0))
    patched_f2p = _as_int(stable_patched.get("fail_to_pass", 0))
    # none_to_pass, error_to_pass and skipped_to_pass are positive, pass-ending patched
    # signals (the milestone's commit made a previously-absent / previously-erroring /
    # previously-feature-skipped test pass) and count in the clean numerator alongside
    # p2p/f2p. All three are structurally src-driven: env is constant across START/END
    # (one image), so it can never flip error->pass or skip->pass — only the milestone's
    # own src can. Excluding them would falsely fail feature / collapse-recovered
    # milestones whose patched tests are dominated by none/error/skipped->pass.
    patched_n2p = _as_int(stable_patched.get("none_to_pass", 0))
    patched_e2p = _as_int(stable_patched.get("error_to_pass", 0))
    patched_s2p = _as_int(stable_patched.get("skipped_to_pass", 0))
    clean_threshold = _as_float(thresholds.get("H_patched_clean_signal"), 0.30)
    if patched_collected == 0:
        patched_clean_value = None
        all_effective_waived = patched_effective > 0 and adjusted_patched_collected >= patched_effective
        patched_clean_passed = patched_error is None and (patched_effective == 0 or all_effective_waived)
        patched_clean_evidence_extra = {
            "skipped": True,
            "reason": (
                "patched_collected == 0 and all effective patched tests are covered by verified root waivers"
                if all_effective_waived
                else "patched_collected == 0 without full verified waiver coverage"
            ),
        }
    else:
        patched_clean_value = (patched_p2p + patched_f2p + patched_n2p + patched_e2p + patched_s2p) / patched_collected
        patched_clean_passed = patched_clean_value >= clean_threshold and patched_error is None
        patched_clean_evidence_extra = {}

    total_after = _as_int(class_summary.get("total_after", 0))
    total_before = _as_int(class_summary.get("total_before", 0))
    pass_to_pass = _as_int(class_summary.get("pass_to_pass", 0))
    p2p_threshold = _as_float(thresholds.get("H_pass_to_pass_majority"), 0.50)
    p2p_value = pass_to_pass / total_after if total_after else 0.0

    f2p_present_threshold = _as_int(thresholds.get("H_none_error_fail_to_pass_present"), 1)
    whole_f2p = _as_int(class_summary.get("fail_to_pass", 0))
    whole_n2p = _as_int(class_summary.get("none_to_pass", 0))
    whole_e2p = _as_int(class_summary.get("error_to_pass", 0))
    whole_s2p = _as_int(class_summary.get("skipped_to_pass", 0))
    f2p_present_value = whole_f2p + whole_n2p + whole_e2p + whole_s2p
    f2p_present_passed = classification_error is None and f2p_present_value >= f2p_present_threshold

    base_collected = _as_int((baseline or {}).get("collection", {}).get("total_collected", 0))
    total_threshold = _as_float(thresholds.get("H_total_not_below_base"), 0.20)
    min_total = base_collected * (1 - total_threshold)
    start_total = _summary_count(start_summary, "collected")
    end_total = _summary_count(end_summary, "collected")
    total_not_below_passed = (
        baseline_error is None
        and base_collected > 0
        and start_summary_error is None
        and end_summary_error is None
        and start_total >= min_total
        and end_total >= min_total
    )

    base_fail_ratio = _baseline_result_ratio(baseline, "failed")
    f2f = _as_int(class_summary.get("fail_to_fail", 0))
    # whole-suite f2f (soft warning only — dominated by TIMING_DAG pollution: tests ahead
    # of the milestone's src failing at both START and END).
    f2f_value = (f2f / total_after - base_fail_ratio) if total_after else 1.0
    f2f_whole_soft_threshold = _as_float(thresholds.get("H_f2f_whole_warn"), 0.05)
    # patched-scoped f2f (HARD gate — the milestone's OWN tests' persistent-fail rate,
    # which actually reflects whether its src made them pass).
    patched_f2f = _as_int(stable_patched.get("fail_to_fail", 0))
    patched_f2f_ratio = (patched_f2f / patched_collected) if patched_collected else 0.0
    f2f_threshold = _as_float(thresholds.get("H_f2f_not_above_base"), 0.15)

    start_error = _summary_count(start_summary, "error")
    end_error = _summary_count(end_summary, "error")
    # start_only errors (error->pass / error->fail / error->skip) are the milestone's
    # src delta arriving at the baseline — the symbol/feature was simply absent at START
    # so the test could not run — NOT an environment bug. A real env bug is constant
    # across START/END (same image) and surfaces as error->error, which stays counted.
    # So exempt the resolved start_only errors from the START budget; persistent
    # error->error (at START) and newly-introduced *->error (at END) still count.
    start_only_error = _as_int(class_summary.get("start_only_error", 0))
    waived_start_error = _as_int(waiver_audit.get("waived_start_error_count", 0))
    waived_end_error = _as_int(waiver_audit.get("waived_end_error_count", 0))
    # v3 waivers (test_out_of_scope / feature_introduced_later) reduce the per-side
    # error budget when attached to H_error_within_budget — the
    # same signature may appear in start, end, or both, and the gate evaluates
    # sides independently.
    adjusted_start_error = max(0, start_error - start_only_error - waived_start_error)
    adjusted_end_error = max(0, end_error - waived_end_error)
    # H_error is now a RATIO of persistent END errors over END collection (incl. errors).
    # Relaxed HARD threshold => only a catastrophic env collapse blocks built; a strict
    # SOFT threshold warns on any mid-level error so the agent still chases real env bugs.
    error_hard_threshold = _as_float(thresholds.get("H_error_within_budget"), 0.10)
    error_soft_threshold = _as_float(thresholds.get("H_error_within_budget_warn"), 0.05)
    end_collection = end_total + adjusted_end_error
    error_ratio = (adjusted_end_error / end_collection) if end_collection > 0 else 0.0

    base_skip_ratio = _baseline_result_ratio(baseline, "skipped")
    s2s = _as_int(class_summary.get("skipped_to_skipped", 0))
    s2s_value = (s2s / total_after - base_skip_ratio) if total_after else 1.0
    s2s_threshold = _as_float(thresholds.get("H_s2s_not_above_base"), 0.05)

    anomaly_count = sum(_as_int(class_summary.get(key, 0)) for key in ANOMALY_TRANSITION_KEYS)
    anomaly_total = max(total_before, total_after)
    anomaly_value = anomaly_count / anomaly_total if anomaly_total else 1.0
    anomaly_threshold = _as_float(thresholds.get("H_anomaly_aggregate"), 0.10)
    # reason_codes per gate (which invalid entries to surface for each gate's audit)
    _PATCHED_REASON_CODES = {
        "deleted_earlier",
        "nodeid_absent_in_states",
        "test_out_of_scope",
        "feature_introduced_later",
    }
    _ERROR_REASON_CODES = {
        "test_out_of_scope",
        "feature_introduced_later",
    }
    patched_waiver_summary = _waiver_audit_gate_summary(
        waiver_audit,
        gate="H_patched_completeness",
        reason_codes=_PATCHED_REASON_CODES,
    )
    error_waiver_summary = _waiver_audit_gate_summary(
        waiver_audit,
        gate="H_error_within_budget",
        reason_codes=_ERROR_REASON_CODES,
    )

    test_collection_and_result_quality = {
        "H_patched_completeness": _gate(
            patched_completeness_passed,
            value=round(patched_completeness_value, 6),
            threshold=completeness_threshold,
            evidence={
                "effective": patched_effective,
                "collected": patched_collected,
                "adjusted_collected": adjusted_patched_collected,
                "missing": len(missing_patched),
                "verified_waivers": len(waiver_audit["verified"]),
                "verified_waived_missing": len(verified_waived),
                "residual_missing": len(residual_missing),
                "waiver_errors": waiver_load_errors,
                "waiver_audit": patched_waiver_summary,
                "artifact_error": patched_error,
            },
        ),
        "H_patched_clean_signal": _gate(
            patched_clean_passed,
            value=None if patched_clean_value is None else round(patched_clean_value, 6),
            threshold=clean_threshold,
            evidence={
                "patched_p2p": patched_p2p,
                "patched_f2p": patched_f2p,
                "patched_n2p": patched_n2p,
                "patched_e2p": patched_e2p,
                "patched_s2p": patched_s2p,
                "patched_collected": patched_collected,
                "artifact_error": patched_error,
                **patched_clean_evidence_extra,
            },
        ),
        "H_pass_to_pass_majority": _gate(
            classification_error is None and total_after > 0 and p2p_value >= p2p_threshold,
            value=round(p2p_value, 6),
            threshold=p2p_threshold,
            evidence={
                "pass_to_pass": pass_to_pass,
                "total_after": total_after,
                "artifact_error": classification_error,
            },
        ),
        "H_none_error_fail_to_pass_present": _gate(
            f2p_present_passed,
            value=f2p_present_value,
            threshold=f2p_present_threshold,
            evidence={
                "fail_to_pass": whole_f2p,
                "none_to_pass": whole_n2p,
                "error_to_pass": whole_e2p,
                "skipped_to_pass": whole_s2p,
                "total": f2p_present_value,
                "artifact_error": classification_error,
            },
        ),
        "H_total_not_below_base": _gate(
            total_not_below_passed,
            threshold=total_threshold,
            evidence={
                "start_total": start_total,
                "end_total": end_total,
                "base_collected": base_collected,
                "min_total": min_total,
                "start_ratio": (start_total / base_collected) if base_collected else None,
                "end_ratio": (end_total / base_collected) if base_collected else None,
                "baseline_error": baseline_error,
                "start_summary_error": start_summary_error,
                "end_summary_error": end_summary_error,
            },
        ),
        "H_f2f_not_above_base": _gate(
            baseline_error is None and classification_error is None and patched_f2f_ratio <= f2f_threshold,
            value=round(patched_f2f_ratio, 6),
            threshold=f2f_threshold,
            evidence={
                "patched_f2f": patched_f2f,
                "patched_collected": patched_collected,
                "patched_f2f_ratio": round(patched_f2f_ratio, 6),
                "whole_f2f_count": f2f,
                "whole_f2f_value": round(f2f_value, 6),
                "whole_soft_threshold": f2f_whole_soft_threshold,
                "total_after": total_after,
                "base_fail_ratio": base_fail_ratio,
                "baseline_error": baseline_error,
                "artifact_error": classification_error,
                "scope": "hard=patched, soft=whole",
            },
        ),
        "H_error_within_budget": _gate(
            start_summary_error is None and end_summary_error is None and error_ratio <= error_hard_threshold,
            value=round(error_ratio, 6),
            threshold=error_hard_threshold,
            evidence={
                "error_ratio": round(error_ratio, 6),
                "end_collection": end_collection,
                "soft_threshold": error_soft_threshold,
                "start_error": start_error,
                "end_error": end_error,
                "start_only_error_exempted": start_only_error,
                "adjusted_start_error": adjusted_start_error,
                "adjusted_end_error": adjusted_end_error,
                "waived_start_error_count": waived_start_error,
                "waived_end_error_count": waived_end_error,
                "waiver_audit": error_waiver_summary,
                "start_summary_error": start_summary_error,
                "end_summary_error": end_summary_error,
            },
        ),
        "H_s2s_not_above_base": _gate(
            baseline_error is None and classification_error is None and total_after > 0 and s2s_value <= s2s_threshold,
            value=round(s2s_value, 6),
            threshold=s2s_threshold,
            evidence={
                "s2s_count": s2s,
                "total_after": total_after,
                "base_skip_ratio": base_skip_ratio,
                "baseline_error": baseline_error,
                "artifact_error": classification_error,
            },
        ),
        "H_anomaly_aggregate": _gate(
            classification_error is None and anomaly_total > 0 and anomaly_value <= anomaly_threshold,
            value=round(anomaly_value, 6),
            threshold=anomaly_threshold,
            evidence={
                "anomaly_count": anomaly_count,
                "total": anomaly_total,
                "artifact_error": classification_error,
            },
        ),
    }

    failed_gates = _collect_failed_gates(build_run_integrity, test_collection_and_result_quality)

    # Soft warnings — stricter bars than the hard gates; DIAGNOSTIC only, they do NOT
    # block built. They tell the agent / M3 "this is approaching or over a tighter
    # threshold" so they can adjudicate (e.g. whole>>patched f2f = TIMING_DAG pollution,
    # accept; mid-level error = chase the real env bug). See M3 skill hard-vs-soft section.
    p2p_warn = _as_float(thresholds.get("H_pass_to_pass_majority_warn"), 0.85)
    anomaly_warn = _as_float(thresholds.get("H_anomaly_aggregate_warn"), 0.01)
    total_warn = _as_float(thresholds.get("H_total_not_below_base_warn"), 0.10)
    soft_warnings: List[Dict[str, Any]] = []
    if classification_error is None:
        if total_after > 0 and p2p_value < p2p_warn:
            soft_warnings.append(
                {
                    "gate": "H_pass_to_pass_majority",
                    "value": round(p2p_value, 6),
                    "soft_threshold": p2p_warn,
                    "hint": "stable pass-to-pass below the soft bar",
                }
            )
        if anomaly_total > 0 and anomaly_value > anomaly_warn:
            soft_warnings.append(
                {
                    "gate": "H_anomaly_aggregate",
                    "value": round(anomaly_value, 6),
                    "soft_threshold": anomaly_warn,
                    "hint": "anomaly transitions above the soft bar",
                }
            )
        if error_ratio > error_soft_threshold:
            soft_warnings.append(
                {
                    "gate": "H_error_within_budget",
                    "value": round(error_ratio, 6),
                    "soft_threshold": error_soft_threshold,
                    "hint": "mid-level error — run diagnose_error_causes (ENV_FIXABLE => fix env; TIMING_DAG => DAG/accept)",
                }
            )
        if total_after > 0 and f2f_value > f2f_whole_soft_threshold:
            soft_warnings.append(
                {
                    "gate": "H_f2f_not_above_base",
                    "whole_value": round(f2f_value, 6),
                    "patched_value": round(patched_f2f_ratio, 6),
                    "soft_threshold": f2f_whole_soft_threshold,
                    "hint": "whole-suite f2f high; if whole>>patched it is TIMING_DAG pollution, not a defect",
                }
            )
        if base_collected > 0:
            soft_floor = base_collected * (1 - total_warn)
            for side, tot in (("start", start_total), ("end", end_total)):
                if tot < soft_floor:
                    soft_warnings.append(
                        {
                            "gate": "H_total_not_below_base",
                            "side": side,
                            "value": tot,
                            "soft_floor": soft_floor,
                            "hint": f"{side} collection below the soft floor",
                        }
                    )
        # Ensure every failing SOFT quality gate surfaces at least once — covers
        # H_s2s (no bespoke warning) and patched-scoped H_f2f (its warning above
        # keys on the whole-suite value). Dedup so a gate already warned with richer
        # evidence is not duplicated.
        warned = {w["gate"] for w in soft_warnings}
        for warning in _collect_soft_gate_warnings(test_collection_and_result_quality):
            if warning["gate"] not in warned:
                soft_warnings.append(warning)

    raw_metrics = {
        "transitions_full": {key: _as_int(class_summary.get(key, 0)) for key in TRANSITION_KEYS},
        "transitions_patched": {
            "pass_to_pass": patched_p2p,
            "fail_to_pass": patched_f2p,
            "none_to_pass": patched_n2p,
            "error_to_pass": patched_e2p,
            "skipped_to_pass": patched_s2p,
            "fail_to_fail": _as_int(stable_patched.get("fail_to_fail", 0)),
            "skipped_to_skipped": _as_int(stable_patched.get("skipped_to_skipped", 0)),
        },
        "errors": {
            "collection_errors_count": _summary_count(start_summary, "collection_errors")
            + _summary_count(end_summary, "collection_errors"),
            "error_groups_count": _summary_count(start_summary, "error_groups")
            + _summary_count(end_summary, "error_groups"),
            "lint_warnings_count": len(lint_warnings),
        },
        # Stricter-than-hard-gate warnings (diagnostic, do NOT block built). The agent
        # fixes what it can; M3 reads these to tell real defects from TIMING_DAG pollution.
        "soft_warnings": soft_warnings,
        "validation_report_health": report_health,
        # Single integer index — full unbound_issues live in module2_waivers.json
        # (source of truth); not duplicated here.
        "unbound_issues_count": len(unbound_issues),
        # agent_sufficient surfaced for Module 3 / human auditors per plan §4 +
        # validation Phase 4.5 Exit 4 ("even when retry does not stop,
        # agent_sufficient is recorded in raw_metrics").
        "agent_sufficient": agent_sufficient_data,
        # Pipeline-augmented audit of bound_waivers (machine-verified status with
        # attached_gate / covered fields) — kept because it is not raw waiver data.
        "invalid_bound_waivers": waiver_audit["invalid"],
        "verified_bound_waivers": waiver_audit["verified"],
        # Per-class numstat lint evidence (Phase B). Full per-class / per-file
        # classification + thresholds_used surface here so retry context can show
        # the agent exactly which class crossed which threshold by how much.
        "per_class_lint_evidence": per_class_lint_evidence,
        # commit_signals audit (Phase C, schema v2 — id-based refs). count +
        # invalid mirror the waivers shape — gate pass/fail is unaffected, but
        # the agent can re-verify on retry whether SHA / diff / id-lookup checks
        # passed. remaining_unbound_issue_ids points to unbound_issues the
        # proposal does NOT resolve (visible to Module 3).
        "commit_signals_count": commit_signal_audit["count"],
        # Iterative cherry-pick status (status field on commit_moves[]):
        # 'pending' = discovered SHA, not yet sandboxed; 'attempted_failed' =
        # sandbox tried but cherry-pick unresolvable. Both surface here so retry
        # context can push agent to finish exploration.
        "candidates_pending_count": commit_signal_audit.get("pending_count", 0),
        "candidates_pending": commit_signal_audit.get("pending", []),
        "candidates_attempted_failed_count": commit_signal_audit.get("failed_count", 0),
        "candidates_attempted_failed": commit_signal_audit.get("failed", []),
        "verified_commit_signals": commit_signal_audit["verified"],
        "invalid_commit_signals": commit_signal_audit["invalid"],
        "remaining_unbound_issue_ids": remaining_unbound_issue_ids,
        # MANDATORY commit_signal violation detection. Non-null when agent emitted
        # addressable bound_waivers / unbound_issues but no commit_signal — retry
        # context (item 7) surfaces this to force agent to add signals next round.
        "commit_signal_missing_warning": commit_signal_missing_warning,
        # candidates_pending_warning: non-null when agent declared sufficient but
        # left commit_moves[] entries in status='pending' (gave up exploration).
        "candidates_pending_warning": candidates_pending_warning,
        # unbound_issue_quality_warnings: soft warnings per unbound_issue when
        # evidence is missing required keys (root_cause/m3_action), covers too
        # many error_groups in one entry (>50, likely lumping), or shares
        # boilerplate root_cause across multiple issues. Does NOT fail gates;
        # retry_context surfaces this to push agent away from generic catch-alls.
        "unbound_issue_quality_warnings": unbound_issue_quality_warnings,
    }

    return {
        "schema_version": 1,
        "milestone_id": milestone_id,
        "generated_at": _utc_now_iso(),
        "built": not failed_gates,
        "failed_gates": failed_gates,
        "build_run_integrity": build_run_integrity,
        "test_collection_and_result_quality": test_collection_and_result_quality,
        "raw_metrics": raw_metrics,
        "artifact_paths": {
            "latest_attempt": latest_attempt_dir.name if latest_attempt_dir else None,
        },
        "inputs": current_build_inputs(docker_dir),
        "image": {
            "name": image_name,
            "id": image_id,
        },
        "agent_history": agent_history or [],
    }


def write_build_status(status: Dict[str, Any], dockerfiles_dir: Path, milestone_id: str) -> Path:
    """Write dockerfiles/{mid}/build_status.json, overwriting prior attempts."""
    out_path = dockerfiles_dir / milestone_id / "build_status.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(status, indent=2, sort_keys=False))
    return out_path


def summarize_build_status_failure(status: Dict[str, Any]) -> str:
    """Compact retry message from build_status.json content."""
    failed = status.get("failed_gates", [])
    lines = ["Hard gate validation failed.", f"Failed gates: {', '.join(failed) if failed else '(none listed)'}"]
    for group_name in ("build_run_integrity", "test_collection_and_result_quality"):
        group = status.get(group_name, {})
        for gate_name in failed:
            gate = group.get(gate_name)
            if gate:
                evidence = gate.get("evidence", {})
                lines.append(f"- {gate_name}: {json.dumps(evidence, ensure_ascii=False)[:1200]}")
    return "\n".join(lines)


# =============================================================================
# Error pattern extraction
# =============================================================================

# Patterns for extracting error symbols from Python tracebacks
ERROR_PATTERNS = [
    # NameError: name 'XXX' is not defined
    (r"NameError: name '(\w+)' is not defined", "NameError"),
    # ImportError: cannot import name 'XXX' from 'YYY'
    (r"ImportError: cannot import name '(\w+)' from '([\w.]+)'", "ImportError"),
    # ModuleNotFoundError: No module named 'XXX'
    (r"ModuleNotFoundError: No module named '([\w.]+)'", "ModuleNotFoundError"),
    # AttributeError: 'XXX' object has no attribute 'YYY'
    (r"AttributeError: '(\w+)' object has no attribute '(\w+)'", "AttributeError"),
    # AttributeError: module 'XXX' has no attribute 'YYY'
    (r"AttributeError: module '([\w.]+)' has no attribute '(\w+)'", "AttributeError"),
    # AttributeError: type object 'XXX' has no attribute 'YYY'
    (r"AttributeError: type object '(\w+)' has no attribute '(\w+)'", "AttributeError"),
]

# Go / Java / Rust patterns (for future use)
ERROR_PATTERNS_GO = [
    (r"undefined: (\w+\.?\w+)", "UndefinedError"),
]

ERROR_PATTERNS_JAVA = [
    (r"error: cannot find symbol.*symbol:\s+(?:method|variable|class)\s+(\w+)", "CannotFindSymbol"),
    (r"package (\S+) does not exist", "PackageNotExist"),
]


def extract_error_symbols_from_log(log_content: str) -> List[ErrorSymbol]:
    """Extract error symbols from a test runner log file (plain text)."""
    symbols = []
    seen = set()

    # Also extract file path from the traceback line before the error
    lines = log_content.split("\n")
    for i, line in enumerate(lines):
        for pattern, error_type in ERROR_PATTERNS:
            match = re.search(pattern, line)
            if not match:
                continue

            groups = match.groups()
            if error_type == "NameError":
                symbol = groups[0]
            elif error_type == "ImportError":
                symbol = groups[0]  # the name being imported
            elif error_type == "ModuleNotFoundError":
                symbol = groups[0]
            elif error_type == "AttributeError":
                symbol = groups[-1]  # the attribute name
            else:
                symbol = groups[0]

            # Deduplicate
            key = (error_type, symbol)
            if key in seen:
                continue
            seen.add(key)

            # Try to extract file path from preceding traceback lines
            file_path = ""
            for j in range(max(0, i - 5), i):
                file_match = re.search(r'File "([^"]+)"', lines[j])
                if file_match:
                    file_path = file_match.group(1)

            symbols.append(
                ErrorSymbol(
                    symbol=symbol,
                    error_type=error_type,
                    file_path=file_path,
                    message=line.strip(),
                )
            )

    return symbols


def extract_error_symbols_from_json(test_results: dict) -> List[ErrorSymbol]:
    """Extract error symbols from structured test result JSON (pytest-report format)."""
    symbols = []
    seen = set()

    tests = test_results.get("tests", [])
    # Only look at error tests (setup failures) — these indicate env/import issues
    error_tests = [t for t in tests if t.get("outcome") == "error"]

    for test in error_tests:
        for phase in ("setup", "call"):
            phase_data = test.get(phase, {})
            crash = phase_data.get("crash", {})
            crash_msg = crash.get("message", "")
            longrepr = phase_data.get("longrepr", "")

            # Try crash message first, then longrepr
            for text in (crash_msg, longrepr):
                if not text:
                    continue
                for pattern, error_type in ERROR_PATTERNS:
                    match = re.search(pattern, text)
                    if not match:
                        continue

                    groups = match.groups()
                    if error_type == "NameError":
                        symbol = groups[0]
                    elif error_type == "ImportError":
                        symbol = groups[0]
                    elif error_type == "ModuleNotFoundError":
                        symbol = groups[0]
                    elif error_type == "AttributeError":
                        symbol = groups[-1]
                    else:
                        symbol = groups[0]

                    key = (error_type, symbol)
                    if key in seen:
                        continue
                    seen.add(key)

                    file_path = crash.get("path", "")
                    symbols.append(
                        ErrorSymbol(
                            symbol=symbol,
                            error_type=error_type,
                            file_path=file_path,
                            message=text[:200],
                        )
                    )

    return symbols


# =============================================================================
# Symbol tracing
# =============================================================================


def trace_symbol_to_commits(
    symbol: str,
    testbed_path: Path,
    file_hint: str = "",
) -> List[str]:
    """Use `git log -S` to find commits that touch a symbol.

    Filters out synthetic testbed commits (e.g., "Start/End state for milestone_*").

    Returns list of short commit hashes (most recent first).
    """
    cmd = ["git", "log", "--all", "--oneline", "-S", symbol]
    if file_hint:
        # Strip /testbed/ prefix if present
        clean_path = re.sub(r"^/testbed/", "", file_hint)
        if clean_path and not clean_path.startswith("/"):
            cmd.extend(["--", clean_path])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(testbed_path),
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            commits = []
            for line in result.stdout.strip().split("\n"):
                msg = " ".join(line.split()[1:])
                if re.match(r"(Start|End) state for ", msg):
                    continue
                commits.append(line.split()[0])
            return commits
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass
    return []


def find_first_use_commit(
    symbol: str,
    milestone_commits: List[str],
    testbed_path: Path,
    file_hint: str = "",
) -> Optional[str]:
    """Earliest commit in `milestone_commits` that introduces a use of `symbol`.

    Uses `git log --all --reverse -S symbol` and returns the first match whose
    SHA prefix is in milestone_commits. Returns None if no commit in the
    milestone references the symbol (e.g., the use was inherited from a parent
    milestone).
    """
    if not milestone_commits:
        return None

    cmd = ["git", "log", "--all", "--oneline", "--reverse", "-S", symbol]
    if file_hint:
        clean_path = re.sub(r"^/testbed/", "", file_hint)
        if clean_path and not clean_path.startswith("/"):
            cmd.extend(["--", clean_path])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(testbed_path),
            timeout=30,
        )
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None

    milestone_short = {c[:7] for c in milestone_commits}
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(maxsplit=1)
        sha = parts[0]
        msg = parts[1] if len(parts) > 1 else ""
        if re.match(r"(Start|End) state for ", msg):
            continue
        if sha[:7] in milestone_short:
            return sha
    return None


def map_commit_to_milestone(
    commit_sha: str,
    milestones: Dict[str, List[str]],
) -> Optional[str]:
    """Find which milestone contains a given commit.

    Handles SHA length mismatches by prefix matching (git log may return
    longer SHAs than the 7-char ones stored in milestones.csv).

    Args:
        commit_sha: Commit SHA to look up (any length)
        milestones: {milestone_id: [commit_sha, ...]}

    Returns:
        milestone_id or None
    """
    # Normalize to 7-char prefix for matching
    short = commit_sha[:7]
    for mid, commits in milestones.items():
        for c in commits:
            if c[:7] == short:
                return mid
    return None


# =============================================================================
# Data loading
# =============================================================================


def load_milestones_csv(csv_path: Path) -> Dict[str, List[str]]:
    """Load milestones.csv → {milestone_id: [commit_sha, ...]}."""
    milestones = {}
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            mid = row["id"]
            commits_str = row.get("commits", "")
            commits = [c.strip() for c in commits_str.split(";") if c.strip()]
            milestones[mid] = commits
    return milestones


def load_dependencies_csv(csv_path: Path) -> Dict[str, Set[str]]:
    """Load dependencies.csv → {milestone_id: set_of_dependency_milestone_ids}.

    Convention: source → target means target depends on source.
    We return {target: {source, ...}} (what each milestone depends on).
    """
    deps = {}
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            source = row["prerequisite_id"]
            target = row["dependant_id"]
            deps.setdefault(target, set()).add(source)
    return deps


def get_transitive_deps(
    milestone_id: str,
    deps: Dict[str, Set[str]],
) -> Set[str]:
    """Compute transitive closure of dependencies for a milestone."""
    visited = set()
    stack = [milestone_id]
    while stack:
        mid = stack.pop()
        if mid in visited:
            continue
        visited.add(mid)
        for dep in deps.get(mid, set()):
            stack.append(dep)
    visited.discard(milestone_id)
    return visited


# =============================================================================
# Debug-only metric anomaly detection
# Not used for Spec v0.9 Module 2 acceptance; retained for manual comparison.
# =============================================================================


def diagnose_metrics(summary: dict) -> Tuple[str, List[str]]:
    """Debug helper: analyze post-loop benchmark summary metrics for anomalies.

    Returns:
        (severity, list_of_issues)
    """
    issues = []
    severity = "OK"

    total_before = summary.get("total_before", 0)
    total_after = summary.get("total_after", 0)
    pass_to_pass = summary.get("pass_to_pass", 0)
    fail_to_fail = summary.get("fail_to_fail", 0)

    # CRITICAL: end state collected 0 tests
    if total_after == 0 and total_before > 0:
        issues.append(f"total_after=0 (end state crashed, total_before={total_before})")
        severity = "CRITICAL"

    # CRITICAL: start state collected 0 tests
    if total_before == 0 and total_after > 0:
        issues.append(f"total_before=0 (start state crashed, total_after={total_after})")
        severity = "CRITICAL"

    # HIGH: no stable passing tests
    if pass_to_pass == 0 and total_before > 0 and total_after > 0:
        issues.append("pass_to_pass=0 (no stable passing tests)")
        if severity != "CRITICAL":
            severity = "HIGH"

    # HIGH: test count divergence > 50%
    if total_before > 0 and total_after > 0:
        diff_ratio = abs(total_before - total_after) / max(total_before, total_after)
        if diff_ratio > 0.5:
            issues.append(f"test count divergence {diff_ratio:.0%} (before={total_before}, after={total_after})")
            if severity != "CRITICAL":
                severity = "HIGH"

    # HIGH: vast majority failing in both states (env issue signal)
    if total_after > 0:
        f2f_ratio = fail_to_fail / total_after
        if f2f_ratio > 0.3:
            issues.append(
                f"fail_to_fail={fail_to_fail}/{total_after} ({f2f_ratio:.0%}) — most tests fail in both states"
            )
            if severity not in ("CRITICAL",):
                severity = "HIGH"

    return severity, issues


# =============================================================================
# Build-time validation (used by build_milestone_image retry loop)
# =============================================================================


def check_build_outputs(dockerfiles_dir: Path, milestone_id: str) -> tuple[bool, str]:
    """Validate that required build artifacts exist."""
    milestone_dir = dockerfiles_dir / milestone_id
    dockerfile = milestone_dir / "Dockerfile"
    test_config = milestone_dir / "test_config.json"

    missing = []
    if not dockerfile.exists():
        missing.append("Dockerfile")
    if not test_config.exists():
        missing.append("test_config.json")

    if missing:
        return False, f"Missing build artifacts: {', '.join(missing)}"

    try:
        with open(test_config) as f:
            json.load(f)
    except json.JSONDecodeError as e:
        return False, f"Invalid test_config.json: {e}"

    return True, "Build artifacts validated"


def check_test_metrics(test_summary: Optional[Dict[str, Any]]) -> list[str]:
    """Debug helper: validate summary metrics after a test run.

    Module 2 build acceptance must use evaluate_build_status() instead.
    """
    summary = test_summary.get("summary", {}) if test_summary else {}
    _severity, issues = diagnose_metrics(summary)
    return issues


def _save_runner_diagnostic(
    milestone_id: str,
    work_dir: Path,
    exit_code: int,
    timed_out: bool,
    duration_sec: float,
    timeout_seconds: int,
    stdout: str,
    stderr: str,
    attempt_dir: Optional[Path] = None,
) -> Path:
    """Persist a compact diagnostic when the test runner subprocess fails.

    Covers the "D3" case: run_milestone_tests.py crashed, was killed, or
    timed out before producing any structured test report. Without this,
    the only signal reaching the retry loop is the empty string
    "Test execution failed". With it, we capture exit code, timing, which
    report files made it to disk, and a tail of stdout/stderr so the
    next retry's preamble can embed concrete evidence.

    Written to a stable path per milestone refinement namespace (overwritten
    each attempt) — it intentionally doesn't go into attempt_N/ because that
    directory may not exist yet if the runner crashed very early.
    """
    diag: Dict[str, Any] = {
        "created_at": _utc_now_iso(),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": round(duration_sec, 2),
        "timeout_seconds": timeout_seconds,
        "reports_written": {},
        "stderr_tail": stderr[-2000:] if stderr else "",
        "stdout_tail": stdout[-2000:] if stdout else "",
    }
    if attempt_dir is not None:
        diag["requested_attempt_dir"] = str(attempt_dir)
        diag["attempt_dir"] = str(attempt_dir)
        diag["attempt_number"] = _attempt_number(attempt_dir)

    attempt_dirs = sorted(
        _refinement_results_dir(work_dir, milestone_id).glob("attempt_*"),
        key=lambda path: _attempt_number(path),
    )
    if not attempt_dirs:
        attempt_dirs = sorted(
            (work_dir / "test_results" / milestone_id).glob("attempt_*"),
            key=lambda path: _attempt_number(path),
        )
    if attempt_dirs:
        latest = attempt_dirs[-1]
        diag["latest_attempt_dir"] = str(latest)
    report_dir = attempt_dir if attempt_dir is not None else (attempt_dirs[-1] if attempt_dirs else None)
    if report_dir is not None:
        for fname in ("start.json", "end.json", "classification.json", "start_summary.json", "end_summary.json"):
            diag["reports_written"][fname] = (report_dir / fname).exists()

    out_path = _refinement_results_dir(work_dir, milestone_id) / "runner_diagnostic.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(diag, indent=2))
    return out_path


def _stop_pipeline_containers(milestone_id: str) -> None:
    """Best-effort cleanup for runner containers left after outer timeout."""
    prefix = f"runner_pipeline_{milestone_id}"
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as e:
        logger.warning(f"[{milestone_id}] Could not list docker containers after timeout: {e}")
        return
    names = [name.strip() for name in result.stdout.splitlines() if name.strip().startswith(prefix)]
    for name in names:
        logger.warning(f"[{milestone_id}] Stopping orphaned validation container after timeout: {name}")
        try:
            subprocess.run(["docker", "stop", name], capture_output=True, text=True, timeout=60)
        except Exception as e:
            logger.warning(f"[{milestone_id}] Failed to stop container {name}: {e}")


def _load_refinement_test_summary(
    milestone_id: str,
    attempt_dir: Optional[Path],
    attempt_number: Optional[int],
) -> Optional[Dict[str, Any]]:
    """Build the Module 2 summary from the current refinement attempt.

    ``run_milestone_tests --output-scope refinement`` intentionally avoids
    writing root test_summary.json because root final files belong to the
    post-loop benchmark. Module 2 only needs enough structured data for logs,
    retry context, and hard-gate metrics, so derive it from the attempt's
    classification.json.
    """
    if attempt_dir is None:
        return None
    classification, error = _safe_read_json(attempt_dir / "classification.json")
    if classification is None:
        logger.warning(f"[{milestone_id}] Could not load refinement classification: {error}")
        return None

    summary = classification.get("summary", {})
    if not isinstance(summary, dict):
        summary = {}
    run_result = {
        "attempt": attempt_number if attempt_number is not None else _attempt_number(attempt_dir),
        "status": "success",
        "statistics": summary,
        "classification_file": str(attempt_dir / "classification.json"),
    }
    return {
        "milestone_id": milestone_id,
        "final_status": "success",
        "output_scope": "refinement",
        "total_runs": 1,
        "successful_runs": 1,
        "failed_runs": 0,
        "summary": summary,
        "all_attempts": [run_result],
    }


def run_tests(
    milestone_id: str,
    work_dir: Path,
    repo_id: str,
    test_scenario: str,
    language: str,
    test_framework: str,
    attempt_number: Optional[int] = None,
    force_rebuild_image: bool = False,
    timeout_seconds: Optional[int] = None,
    clean_attempt_dir: bool = False,
    workers_override: Optional[int] = None,
) -> tuple[bool, Optional[Dict[str, Any]], Optional[Path]]:
    """Run validation tests using run_milestone_tests.py.

    On failure also writes
    ``test_results/{mid}/refinement/runner_diagnostic.json`` with exit code +
    stderr tail (the D3 signal) so callers have something to show the agent
    beyond "Test execution failed".

    Returns:
        Tuple of (success, test_summary, attempt_dir)
    """
    import time

    timeout_seconds = resolve_validation_timeout_seconds(work_dir, repo_id, timeout_seconds)
    attempt_offset = max((attempt_number or 1) - 1, 0)
    attempt_dir = _refinement_attempt_dir(work_dir, milestone_id, attempt_number) if attempt_number else None
    if clean_attempt_dir and attempt_dir and attempt_dir.exists():
        logger.info(f"[{milestone_id}] Clearing stale validation attempt dir: {attempt_dir}")
        shutil.rmtree(attempt_dir)

    cmd = [
        sys.executable,
        "-m",
        "deepcommit.harness.test_runner.run_milestone_tests",
        milestone_id,
        "--work-dir",
        str(work_dir),
        "--output-scope",
        "refinement",
        "--max-retries",
        "1",
        "--language",
        language,
        "--test-framework",
        test_framework,
        "--no-cache",
        "--attempt-offset",
        str(attempt_offset),
        "--attempt-source",
        "pipeline",
    ]
    if force_rebuild_image:
        cmd.append("--force-rebuild-image")
    if workers_override is not None:
        cmd.extend(["--workers-override", str(max(1, int(workers_override)))])

    logger.info(f"[{milestone_id}] Running validation tests (timeout={timeout_seconds}s)...")
    logger.debug(f"Command: {' '.join(cmd)}")

    start = time.time()
    stdout, stderr = "", ""
    exit_code = 0
    timed_out = False

    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        stdout, stderr = stdout or "", stderr or ""
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = -1
        logger.error(f"[{milestone_id}] Validation tests timed out after {timeout_seconds}s")
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.warning(f"[{milestone_id}] Failed to terminate validation process group: {e}")
            try:
                stdout, stderr = proc.communicate(timeout=10)
                stdout, stderr = stdout or "", stderr or ""
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception as e:
                    logger.warning(f"[{milestone_id}] Failed to kill validation process group: {e}")
                stdout, stderr = proc.communicate()
                stdout, stderr = stdout or "", stderr or ""
        _stop_pipeline_containers(milestone_id)
    except Exception as e:
        exit_code = -1
        stderr = f"subprocess exception: {e}"
        logger.error(f"[{milestone_id}] Validation tests error: {e}")

    duration = time.time() - start

    test_summary = _load_refinement_test_summary(milestone_id, attempt_dir, attempt_number)

    if exit_code != 0 or timed_out:
        _save_runner_diagnostic(
            milestone_id,
            work_dir,
            exit_code,
            timed_out,
            duration,
            timeout_seconds,
            stdout,
            stderr,
            attempt_dir=attempt_dir,
        )
        logger.warning(f"[{milestone_id}] Validation tests failed (exit={exit_code}, timed_out={timed_out})")
        if attempt_dir is None:
            attempt_dir = _latest_attempt_path(work_dir, milestone_id)
            test_summary = _load_refinement_test_summary(milestone_id, attempt_dir, attempt_number)
        return False, test_summary, attempt_dir

    logger.info(f"[{milestone_id}] Validation tests passed")
    stale_diag = _refinement_results_dir(work_dir, milestone_id) / "runner_diagnostic.json"
    try:
        stale_diag.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"[{milestone_id}] Could not remove stale runner diagnostic: {e}")
    if attempt_dir is None:
        attempt_dir = _latest_attempt_path(work_dir, milestone_id)
        test_summary = _load_refinement_test_summary(milestone_id, attempt_dir, attempt_number)
    return True, test_summary, attempt_dir


def collect_patched_tests(
    milestone_id: str,
    work_dir: Path,
    repo_path: Path,
    layer: str = "both",
) -> tuple[bool, Optional[Dict[str, Any]]]:
    """Run collect_milestone_patched_tests.py to validate commit-related tests.

    Default ``layer="both"`` preserves backward compatibility for callers
    that didn't pre-run Layer 1. In Module 2's retry loop, the caller
    passes ``layer="2"`` because ``ensure_patched_tests`` already ran
    Layer 1 once for the whole scenario at module entry — this call then
    only refreshes the classification intersection for the milestone's
    just-run tests.

    Returns:
        Tuple of (success, patched_tests_result)
    """
    cmd = [
        sys.executable,
        "-m",
        "deepcommit.harness.prepare_images.collect_milestone_patched_tests",
        str(work_dir),
        "--repo",
        str(repo_path),
        "--milestone",
        milestone_id,
        "--layer",
        layer,
    ]

    logger.info(f"[{milestone_id}] Collecting patched tests...")
    logger.debug(f"Command: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )

        patched_tests_path = work_dir / "milestone_patched_tests" / f"{milestone_id}.json"
        patched_tests_result = None
        if patched_tests_path.exists():
            with open(patched_tests_path) as f:
                patched_tests_result = json.load(f)

        if result.returncode != 0:
            logger.warning(f"[{milestone_id}] Collect patched tests failed with code {result.returncode}")
            logger.warning(f"[{milestone_id}] stderr: {result.stderr[:500] if result.stderr else 'N/A'}")
            return False, patched_tests_result

        if patched_tests_result:
            collected = patched_tests_result.get("collected", {})
            patched_not_in_results = collected.get("patched_not_in_results", [])
            if patched_not_in_results:
                logger.warning(
                    f"[{milestone_id}] {len(patched_not_in_results)} patched tests not collected (dependency issues): "
                    f"{patched_not_in_results[:5]}{'...' if len(patched_not_in_results) > 5 else ''}"
                )
                logger.warning(
                    f"[{milestone_id}] These tests reference modules/functions not yet available in this milestone. "
                    f"Full list: milestone_patched_tests/{milestone_id}.json (collected.patched_not_in_results)"
                )

        logger.info(f"[{milestone_id}] Patched tests validation passed")
        return True, patched_tests_result

    except subprocess.TimeoutExpired:
        logger.error(f"[{milestone_id}] Collect patched tests timed out")
        return False, None
    except Exception as e:
        logger.error(f"[{milestone_id}] Collect patched tests error: {e}")
        return False, None


# =============================================================================
# Debug-only cross-milestone diagnosis
# Not used for Spec v0.9 Module 2 hard gates; later stages can glob build_status.
# =============================================================================


# Threshold for low-count rule: flag if count < LOW_THRESHOLD * median
LOW_COUNT_THRESHOLD = 0.6

# Minimum number of milestones with data required before cross-milestone stats
# are meaningful (fewer than this → medians reported as 0, no flagging).
MIN_MILESTONES_FOR_MEDIAN = 3


def compute_cross_milestone_stats(work_dir: Path) -> Dict[str, int]:
    """Debug helper: scan post-loop benchmark test_summary.json files and compute medians.

    Reads each milestone's root test_summary.json and extracts total_before /
    total_after. Missing files, zero counts, or unparseable JSON are skipped.

    Returns dict with keys:
        median_before, median_after  — medians of non-zero counts (0 if <3 data points)
        count_before, count_after    — number of milestones contributing to each median

    Not used by Module 2 acceptance or retry triggers.
    """
    stats = {"median_before": 0, "median_after": 0, "count_before": 0, "count_after": 0}

    test_results_dir = work_dir / "test_results"
    if not test_results_dir.exists():
        return stats

    counts_before: List[int] = []
    counts_after: List[int] = []
    for mid_dir in test_results_dir.iterdir():
        if not mid_dir.is_dir():
            continue
        summary_path = mid_dir / "test_summary.json"
        if not summary_path.exists():
            continue
        try:
            with open(summary_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        summary = data.get("summary", {})
        tb = summary.get("total_before", 0)
        ta = summary.get("total_after", 0)
        if tb > 0:
            counts_before.append(tb)
        if ta > 0:
            counts_after.append(ta)

    stats["count_before"] = len(counts_before)
    stats["count_after"] = len(counts_after)

    if len(counts_before) >= MIN_MILESTONES_FOR_MEDIAN:
        counts_before.sort()
        stats["median_before"] = counts_before[len(counts_before) // 2]
    if len(counts_after) >= MIN_MILESTONES_FOR_MEDIAN:
        counts_after.sort()
        stats["median_after"] = counts_after[len(counts_after) // 2]

    return stats


def _flag_low_count(
    result: "DiagnosisResult",
    median_before: int,
    median_after: int,
) -> None:
    """Apply the low-count rule: flag if test count is < 60% of median.

    Mutates result in-place: appends issues, and upgrades severity/root_cause
    from OK → MEDIUM / env_issue if the milestone is flagged.

    Skipped for milestones already CRITICAL (the low-count signal would be
    redundant with the existing critical finding).
    """
    if result.severity == "CRITICAL":
        return

    tb = result.metrics.get("total_before", 0)
    ta = result.metrics.get("total_after", 0)

    flagged = False
    if tb > 0 and median_before > 0 and tb < median_before * LOW_COUNT_THRESHOLD:
        ratio = tb / median_before
        result.issues.append(
            f"total_before={tb} is {ratio:.0%} of median ({median_before}) — possible incomplete test collection"
        )
        flagged = True

    if ta > 0 and median_after > 0 and ta < median_after * LOW_COUNT_THRESHOLD:
        ratio = ta / median_after
        result.issues.append(
            f"total_after={ta} is {ratio:.0%} of median ({median_after}) — possible incomplete test collection"
        )
        flagged = True

    if flagged:
        if result.is_healthy:
            result.is_healthy = False
            result.root_cause = "env_issue"
        if result.severity == "OK":
            result.severity = "MEDIUM"


def diagnose_cross_milestone(
    results: Dict[str, "DiagnosisResult"],
    cross_stats: Optional[Dict[str, int]] = None,
) -> None:
    """Debug helper: cross-milestone test count comparison.

    Flags milestones whose test count is far below the median (incomplete
    collection) and reports overall spread statistics.

    If cross_stats is provided, Rule 1 (per-milestone low-count flagging) is
    skipped because callers have already applied it via _flag_low_count when
    building each result. Rule 2 (overall spread warning) always runs.

    Mutates results in-place, upgrading severity and appending issues.
    """
    # Collect test counts (skip milestones already CRITICAL — e.g., total_after=0)
    counts_before = {}
    counts_after = {}
    for mid, r in results.items():
        tb = r.metrics.get("total_before", 0)
        ta = r.metrics.get("total_after", 0)
        if tb > 0:
            counts_before[mid] = tb
        if ta > 0:
            counts_after[mid] = ta

    if len(counts_before) < MIN_MILESTONES_FOR_MEDIAN:
        return  # not enough data for meaningful comparison

    # Compute median
    sorted_before = sorted(counts_before.values())
    sorted_after = sorted(counts_after.values())
    median_before = sorted_before[len(sorted_before) // 2]
    median_after = sorted_after[len(sorted_after) // 2] if sorted_after else 0

    # Rule 1: individual milestone far below median (< 60%)
    # Skip if caller already applied _flag_low_count (via cross_stats passed to
    # diagnose_single_milestone) — avoids duplicate issues on each result.
    if cross_stats is None:
        for r in results.values():
            _flag_low_count(r, median_before, median_after)

    # Rule 2: overall spread — flag if max/min ratio > 10x (across non-zero counts)
    if sorted_before and sorted_before[0] > 0:
        spread_ratio = sorted_before[-1] / sorted_before[0]
        if spread_ratio > 10:
            logger.warning(
                f"Cross-milestone spread: max/min = {sorted_before[-1]}/{sorted_before[0]} "
                f"= {spread_ratio:.0f}x (median={median_before}). "
                f"Some milestones may have incomplete test collection."
            )


# =============================================================================
# Layer 2: Root cause classification
# =============================================================================


def classify_root_cause(
    milestone_id: str,
    error_symbols: List[ErrorSymbol],
    milestones: Dict[str, List[str]],
    deps: Dict[str, Set[str]],
    testbed_path: Path,
) -> Tuple[str, List[MissingEdge]]:
    """Classify whether issues are DAG topology problems or environment problems.

    For each error symbol:
      1. git log -S to find commits that touch it
      2. Map each commit → milestone
      3. Check if that milestone is in the dependency chain
      4. If not: emit a MissingEdge with both milestone- and commit-level info.

    Commit-level info: target_commit is the *earliest* commit in the target
    (erroring) milestone that uses the symbol (resolved via
    find_first_use_commit). This is the precise constraint for downstream
    solvers — milestone-level edges can be over-constraining and create
    spurious cycles.

    Returns:
        (root_cause, missing_edges)
        root_cause: "dag_missing_edge" | "env_issue"
    """
    missing_edges: List[MissingEdge] = []
    seen_edges = set()
    transitive_deps = get_transitive_deps(milestone_id, deps)
    target_commits = milestones.get(milestone_id, [])

    for sym in error_symbols:
        # Skip third-party modules (ModuleNotFoundError for non-project modules)
        if sym.error_type == "ModuleNotFoundError":
            module = sym.symbol.split(".")[0]
            # Heuristic: check if the module exists under the testbed source tree
            # If not, it's likely a pip-installable third-party package
            possible_paths = list(testbed_path.glob(f"{module}/"))
            if not possible_paths:
                continue

        # Trace symbol to all candidate commits
        candidates = trace_symbol_to_commits(sym.symbol, testbed_path, sym.file_path)

        # Try each candidate commit to find one in a *different* milestone
        # that is not already a declared dependency
        for commit in candidates:
            origin_mid = map_commit_to_milestone(commit, milestones)
            if not origin_mid:
                continue

            # Skip if symbol is in the same milestone — try older commits
            if origin_mid == milestone_id:
                continue

            sym.defined_in_commit = commit
            sym.defined_in_milestone = origin_mid

            # Check if origin milestone is already a dependency
            if origin_mid in transitive_deps:
                break

            # Resolve target-side commit (earliest use within the erroring
            # milestone). Fall back to the milestone's last commit if not
            # found — keeps the edge expressible at commit level even when
            # first-use detection fails (e.g., usage was inherited from a
            # parent milestone).
            target_commit = find_first_use_commit(
                sym.symbol,
                target_commits,
                testbed_path,
                sym.file_path,
            )
            if target_commit is None and target_commits:
                target_commit = target_commits[-1]

            edge_key = (origin_mid, milestone_id, commit, target_commit)
            if edge_key in seen_edges:
                break
            seen_edges.add(edge_key)

            missing_edges.append(
                MissingEdge(
                    source_milestone=origin_mid,
                    target_milestone=milestone_id,
                    source_commit=commit,
                    target_commit=target_commit or "",
                    symbol=sym.symbol,
                    error_type=sym.error_type,
                )
            )
            break

    if missing_edges:
        return "dag_missing_edge", missing_edges
    return "env_issue", []


# =============================================================================
# Debug-only main diagnosis
# Not used for Spec v0.9 Module 2 acceptance, retry trigger, or authoritative
# artifact writing. Use evaluate_build_status() / write_build_status().
# =============================================================================


def diagnose_single_milestone(
    milestone_id: str,
    work_dir: Path,
    milestones: Dict[str, List[str]],
    deps: Dict[str, Set[str]],
    testbed_path: Path,
    cross_stats: Optional[Dict[str, int]] = None,
) -> DiagnosisResult:
    """Debug helper: run full diagnosis on a single milestone.

    If cross_stats is provided (from compute_cross_milestone_stats), also
    applies the low-count rule — flagging milestones whose test count is
    abnormally low relative to the cross-milestone median. This lets callers
    that diagnose only one milestone still catch the "incomplete collection"
    signal that diagnose_all detects via diagnose_cross_milestone.
    """
    test_results_dir = work_dir / "test_results" / milestone_id

    # Load test summary
    summary_path = test_results_dir / "test_summary.json"
    if not summary_path.exists():
        return DiagnosisResult(
            milestone_id=milestone_id,
            is_healthy=False,
            severity="CRITICAL",
            issues=["test_summary.json not found — tests never ran"],
            root_cause="env_issue",
            metrics={},
        )

    with open(summary_path) as f:
        test_summary = json.load(f)

    summary = test_summary.get("summary", {})

    # Layer 1: metric anomaly detection
    severity, issues = diagnose_metrics(summary)

    # Build provisional result so we can apply the cross-milestone low-count
    # rule uniformly (it may flip a locally-healthy milestone to MEDIUM).
    result = DiagnosisResult(
        milestone_id=milestone_id,
        is_healthy=(severity == "OK"),
        severity=severity,
        issues=issues,
        root_cause="healthy" if severity == "OK" else "env_issue",
        metrics=summary,
    )

    if cross_stats:
        _flag_low_count(
            result,
            cross_stats.get("median_before", 0),
            cross_stats.get("median_after", 0),
        )

    # Early exit if still healthy after both local and cross-milestone checks
    if result.is_healthy:
        return result

    # Layer 2: extract error symbols and classify root cause
    severity = result.severity
    issues = result.issues
    error_symbols = _extract_errors_from_results(milestone_id, test_results_dir)

    root_cause = "env_issue"
    missing_edges = []

    if error_symbols and testbed_path.exists():
        root_cause, missing_edges = classify_root_cause(
            milestone_id,
            error_symbols,
            milestones,
            deps,
            testbed_path,
        )

        if root_cause == "dag_missing_edge":
            for edge in missing_edges:
                src_c = edge.source_commit[:7] if edge.source_commit else "?"
                tgt_c = edge.target_commit[:7] if edge.target_commit else "?"
                issues.append(
                    f"Missing DAG edge: {edge.source_milestone}@{src_c} → "
                    f"{edge.target_milestone}@{tgt_c} (symbol '{edge.symbol}')"
                )
            # Annotate error symbols with tracing results
            for sym in error_symbols:
                if sym.defined_in_milestone:
                    issues.append(
                        f"Symbol '{sym.symbol}' ({sym.error_type}) defined in "
                        f"commit {sym.defined_in_commit} (milestone {sym.defined_in_milestone})"
                    )

    # Check patched tests
    uncollected_patched_tests: List[str] = []
    patched_path = work_dir / "milestone_patched_tests" / f"{milestone_id}.json"
    if patched_path.exists():
        with open(patched_path) as f:
            patched = json.load(f)
        not_in_results = patched.get("collected", {}).get("patched_not_in_results", [])
        if not_in_results:
            uncollected_patched_tests = not_in_results
            issues.append(f"{len(not_in_results)} patched tests not collected")
            if severity == "OK":
                severity = "MEDIUM"

    return DiagnosisResult(
        milestone_id=milestone_id,
        is_healthy=False,
        severity=severity,
        issues=issues,
        root_cause=root_cause,
        missing_edges=missing_edges,
        error_symbols=error_symbols,
        uncollected_patched_tests=uncollected_patched_tests,
        metrics=summary,
    )


def _extract_errors_from_results(
    milestone_id: str,
    test_results_dir: Path,
) -> List[ErrorSymbol]:
    """Extract error symbols from test results, trying multiple sources."""
    error_symbols = []

    # Prefer post-loop benchmark replicates. If the debug CLI is run before
    # post-loop, fall back to Module 2 refinement attempts.
    run_dirs = sorted(test_results_dir.glob("replicate_*"), key=_attempt_number)
    if not run_dirs:
        run_dirs = sorted((test_results_dir / "refinement").glob("attempt_*"), key=_attempt_number)
    if not run_dirs:
        return error_symbols

    latest_run = run_dirs[-1]

    # Strategy 1: Try structured JSON (end.json) — richer error data
    end_json = latest_run / "end.json"
    if end_json.exists():
        try:
            with open(end_json) as f:
                data = json.load(f)
            if data.get("summary", {}).get("total", 0) > 0:
                error_symbols = extract_error_symbols_from_json(data)
                if error_symbols:
                    return error_symbols
        except (json.JSONDecodeError, KeyError):
            pass

    # Strategy 2: Try log file (end_default.log) — for crashed runs
    end_log = latest_run / "end_default.log"
    if end_log.exists():
        log_content = end_log.read_text(errors="replace")
        error_symbols = extract_error_symbols_from_log(log_content)
        if error_symbols:
            return error_symbols

    # Strategy 3: Try start state files too
    start_json = latest_run / "start.json"
    if start_json.exists():
        try:
            with open(start_json) as f:
                data = json.load(f)
            error_symbols = extract_error_symbols_from_json(data)
            if error_symbols:
                return error_symbols
        except (json.JSONDecodeError, KeyError):
            pass

    start_log = latest_run / "start_default.log"
    if start_log.exists():
        log_content = start_log.read_text(errors="replace")
        error_symbols = extract_error_symbols_from_log(log_content)

    return error_symbols


def diagnose_all(
    work_dir: Path,
    milestone_ids: Optional[List[str]] = None,
) -> Dict[str, DiagnosisResult]:
    """Debug CLI helper: run diagnosis on all/specified milestones.

    The emitted debug_diagnosis_*.json files are debug artifacts only;
    build_status.json is authoritative.

    Args:
        work_dir: Path to harness workspace (e.g., .../v1_001_fix_v2_5)
        milestone_ids: Optional list of milestone IDs to diagnose.
                       If None, diagnoses all milestones found in test_results.

    Returns:
        {milestone_id: DiagnosisResult}
    """
    # Load milestone and dependency data
    milestones_csv = work_dir / "milestones.csv"
    deps_csv = work_dir / "dependencies.csv"
    testbed_path = work_dir / "testbed"

    if not milestones_csv.exists():
        logger.error(f"milestones.csv not found: {milestones_csv}")
        return {}

    milestones = load_milestones_csv(milestones_csv)
    deps = load_dependencies_csv(deps_csv) if deps_csv.exists() else {}

    # Determine which milestones to diagnose
    if milestone_ids is None:
        test_results_dir = work_dir / "test_results"
        if not test_results_dir.exists():
            logger.error(f"test_results/ not found: {test_results_dir}")
            return {}
        milestone_ids = [d.name for d in sorted(test_results_dir.iterdir()) if d.is_dir() and d.name in milestones]

    # Compute cross-milestone stats once so each diagnose_single_milestone
    # can apply the low-count rule with the same median as the post-processing.
    cross_stats = compute_cross_milestone_stats(work_dir)

    results = {}
    for mid in milestone_ids:
        logger.info(f"Diagnosing {mid}...")
        results[mid] = diagnose_single_milestone(
            mid,
            work_dir,
            milestones,
            deps,
            testbed_path,
            cross_stats=cross_stats,
        )

    # Cross-milestone post-processing: Rule 1 (low-count) already applied via
    # cross_stats above, so this only emits the Rule 2 spread warning.
    diagnose_cross_milestone(results, cross_stats=cross_stats)

    # Auto-save debug reports under explicit debug-only filenames.
    dockerfiles_dir = work_dir / "dockerfiles"
    dockerfiles_dir.mkdir(parents=True, exist_ok=True)

    # Save per-milestone reports to dockerfiles/{mid}/debug_diagnosis_report.json
    for mid, r in results.items():
        mid_dir = dockerfiles_dir / mid
        mid_dir.mkdir(parents=True, exist_ok=True)
        mid_report_path = mid_dir / "debug_diagnosis_report.json"
        payload = r.to_dict()
        payload["debug_only"] = True
        payload["authoritative_artifact"] = f"dockerfiles/{mid}/build_status.json"
        mid_report_path.write_text(json.dumps(payload, indent=2))
        logger.info(f"Per-milestone report saved to: {mid_report_path}")

    # Save combined report to dockerfiles/debug_diagnosis_summary.json
    dag_issues = {mid: r.to_dict() for mid, r in results.items() if r.root_cause == "dag_missing_edge"}
    env_issues = {mid: r.to_dict() for mid, r in results.items() if r.root_cause == "env_issue"}

    all_before = [r.metrics.get("total_before", 0) for r in results.values() if r.metrics.get("total_before", 0) > 0]
    all_after = [r.metrics.get("total_after", 0) for r in results.values() if r.metrics.get("total_after", 0) > 0]
    all_before_sorted = sorted(all_before) if all_before else [0]
    all_after_sorted = sorted(all_after) if all_after else [0]

    report = {
        "debug_only": True,
        "authoritative_artifact_pattern": "dockerfiles/*/build_status.json",
        "work_dir": str(work_dir),
        "summary": {
            "total": len(results),
            "healthy": sum(1 for r in results.values() if r.is_healthy),
            "dag_issues": len(dag_issues),
            "env_issues": len(env_issues),
            "test_count_stats": {
                "before": {
                    "min": all_before_sorted[0],
                    "max": all_before_sorted[-1],
                    "median": all_before_sorted[len(all_before_sorted) // 2],
                },
                "after": {
                    "min": all_after_sorted[0],
                    "max": all_after_sorted[-1],
                    "median": all_after_sorted[len(all_after_sorted) // 2],
                },
            },
        },
        "dag_issues": dag_issues,
        "env_issues": env_issues,
        "all_milestones": {mid: r.to_dict() for mid, r in results.items()},
    }
    combined_report_path = dockerfiles_dir / "debug_diagnosis_summary.json"
    combined_report_path.write_text(json.dumps(report, indent=2))
    logger.info(f"Combined report saved to: {combined_report_path}")

    return results


# =============================================================================
# Report formatting
# =============================================================================


def print_report(results: Dict[str, DiagnosisResult]) -> None:
    """Print a human-readable diagnosis report."""
    print(f"\n{'=' * 70}")
    print(f"Diagnosis Report ({len(results)} milestones)")
    print(f"{'=' * 70}\n")

    # Sort by severity
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "OK": 3}
    sorted_results = sorted(
        results.values(),
        key=lambda r: (severity_order.get(r.severity, 99), r.milestone_id),
    )

    dag_issues = []
    env_issues = []
    healthy = []

    for r in sorted_results:
        if r.is_healthy:
            healthy.append(r)
            continue

        icon = {"CRITICAL": "!!!", "HIGH": "!! ", "MEDIUM": "!  "}
        severity_str = icon.get(r.severity, "   ")

        print(f"{severity_str} {r.severity:<10s} {r.milestone_id}")
        print(f"    root_cause: {r.root_cause}")
        for issue in r.issues:
            print(f"    - {issue}")

        if r.root_cause == "dag_missing_edge":
            dag_issues.append(r)
            for edge in r.missing_edges:
                src_c = edge.source_commit[:7] if edge.source_commit else "?"
                tgt_c = edge.target_commit[:7] if edge.target_commit else "?"
                print(
                    f"    FIX: add edge {edge.source_milestone} -> {edge.target_milestone}"
                    f" (commit {tgt_c} needs {src_c}, symbol '{edge.symbol}')"
                )
        else:
            env_issues.append(r)

        print()

    # Healthy milestones (compact)
    if healthy:
        healthy_ids = ", ".join(r.milestone_id for r in healthy)
        print(f"OK  {len(healthy)} healthy: {healthy_ids}\n")

    # Summary
    print(f"{'=' * 70}")
    print(f"Summary: {len(healthy)} healthy, {len(dag_issues)} DAG issue(s), {len(env_issues)} env issue(s)")

    if dag_issues:
        print("\nDAG fixes needed:")
        seen_edges = set()
        for r in dag_issues:
            for edge in r.missing_edges:
                key = (edge.source_milestone, edge.target_milestone)
                if key not in seen_edges:
                    seen_edges.add(key)
                    print(f"  {edge.source_milestone} -> {edge.target_milestone}")

    if env_issues:
        print("\nEnv refines needed:")
        for r in env_issues:
            print(f"  {r.milestone_id}: {r.issues[0] if r.issues else 'unknown'}")

    print(f"{'=' * 70}")


# =============================================================================
# CLI
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Diagnose milestone test results — identify DAG vs environment issues",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        required=True,
        help="Harness workspace directory (e.g., DATA/harness_workspace/repo/scenario)",
    )
    parser.add_argument(
        "--milestones",
        nargs="*",
        help="Specific milestone IDs to diagnose (default: all)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output JSON report instead of human-readable text",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Save report to file (default: print to stdout)",
    )

    args = parser.parse_args()

    work_dir = args.work_dir.resolve()
    if not work_dir.exists():
        print(f"Error: Work directory not found: {work_dir}", file=sys.stderr)
        return 1

    results = diagnose_all(work_dir, args.milestones)
    if not results:
        print("No milestones to diagnose.", file=sys.stderr)
        return 1

    if args.json_output:
        report = {
            "work_dir": str(work_dir),
            "total": len(results),
            "healthy": sum(1 for r in results.values() if r.is_healthy),
            "dag_issues": sum(1 for r in results.values() if r.root_cause == "dag_missing_edge"),
            "env_issues": sum(1 for r in results.values() if r.root_cause == "env_issue"),
            "milestones": {mid: r.to_dict() for mid, r in results.items()},
        }
        output = json.dumps(report, indent=2)
        if args.output:
            args.output.write_text(output)
            print(f"Report saved to: {args.output}")
        else:
            print(output)
    else:
        print_report(results)
        if args.output:
            # Also save JSON version
            report = {
                "work_dir": str(work_dir),
                "milestones": {mid: r.to_dict() for mid, r in results.items()},
            }
            args.output.write_text(json.dumps(report, indent=2))
            print(f"\nJSON report saved to: {args.output}")

    # Return exit code based on results
    has_issues = any(not r.is_healthy for r in results.values())
    return 1 if has_issues else 0


if __name__ == "__main__":
    sys.exit(main())
