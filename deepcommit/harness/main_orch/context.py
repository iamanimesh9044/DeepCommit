"""Shared workspace context kernel for the m3 verb suite.

Responsibilities:
  * workspace resolution (fail loud on a malformed workspace),
  * atomic ``.tmp`` + ``os.replace`` CSV/JSON IO — the atomic write is **new**
    here (legacy ``csv_ops`` writes with a plain ``open(path, "w")``),
  * the unifying backup-on-write convention ``*.bak_round{NNN}_pre``,
  * mtime / freshness helpers,
  * canonical (``prerequisite_id``/``dependant_id``) + legacy
    (``source_id``/``target_id``) CSV vocabulary readers,
  * the JSON-to-stdout-or-``--output`` emit contract + exit-code constants,
    mirroring ``frozen_check_gate.py`` (the suite template).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from deepcommit.harness.main_orch.invariants import parse_commits

# Exit-code contract (mirrors frozen_check_gate.py): 0 ok/pass · 1 input invalid · 2 violation/fail
OK = 0
INPUT_INVALID = 1
VIOLATION = 2

DEFAULT_REQUIRED = ("milestones.csv", "dependencies.csv")

# Gate classification — verbs-side mirror of diagnosis.HARD/SOFT_QUALITY_GATES, kept
# here so the read-only verbs never import the heavy build module. SOT is the M3 skill
# (deepcommit/harness/main_orch/prompts/main.md "Built is a strict per-MS verdict");
# diagnosis.py holds the M2 enforcement copy. HARD gates block `built`; SOFT gates are
# non-blocking advisories (TIMING_DAG pollution vs a real env defect — the agent / M3
# triages them). The dashboard re-derives the split from each gate's recorded `passed`,
# so it works on OLD data too (where the flat failed_gates held every failing gate).
HARD_QUALITY_GATES = (
    "H_patched_completeness",
    "H_patched_clean_signal",
    "H_none_error_fail_to_pass_present",
    "H_error_within_budget",
    "H_total_not_below_base",
)
SOFT_QUALITY_GATES = (
    "H_f2f_not_above_base",
    "H_pass_to_pass_majority",
    "H_s2s_not_above_base",
    "H_anomaly_aggregate",
)
# The H_error ratio hard bar (mirror of diagnosis default). A recorded H_error ratio in
# (0, this] would not block under the current bar, so the dashboard shows such a failure
# (only possible in OLD data graded at the stricter 0 line) as soft, not hard.
H_ERROR_HARD_THRESHOLD = 0.10


class InputError(Exception):
    """Raised when a verb's inputs are invalid (maps to exit code 1)."""


# --------------------------------------------------------------------------- #
# Workspace resolution
# --------------------------------------------------------------------------- #
def resolve_workspace(path: str | os.PathLike[str], *, require: Sequence[str] = DEFAULT_REQUIRED) -> Path:
    """Resolve and validate a harness workspace directory."""
    if path is None:
        raise InputError("workspace is required")
    ws = Path(path).resolve()
    if not ws.is_dir():
        raise InputError(f"workspace is not a directory: {ws}")
    for name in require:
        if not (ws / name).exists():
            raise InputError(f"workspace missing required file: {name} ({ws / name})")
    return ws


# --------------------------------------------------------------------------- #
# Atomic IO
# --------------------------------------------------------------------------- #
def _atomic_replace(path: Path, data: str, *, newline: str | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline=newline) as f:
        f.write(data)
    os.replace(tmp, path)


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    _atomic_replace(Path(path), text)


def read_json(path: str | os.PathLike[str]) -> Any:
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as exc:
        raise InputError(f"JSON file not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"invalid JSON in {p}: {exc}") from exc


def safe_read_json(path: str | os.PathLike[str]) -> tuple[Any | None, str | None]:
    try:
        return read_json(path), None
    except InputError as exc:
        return None, str(exc)


def write_json_atomic(path: str | os.PathLike[str], obj: Any) -> None:
    _atomic_replace(Path(path), json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False, default=str) + "\n")


def read_csv(path: str | os.PathLike[str]) -> tuple[list[dict[str, str]], list[str]]:
    p = Path(path)
    if not p.exists():
        raise InputError(f"CSV file not found: {p}")
    with p.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = [dict(row) for row in reader]
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def write_csv_atomic(
    path: str | os.PathLike[str], rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]
) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(fieldnames), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_replace(Path(path), buf.getvalue(), newline="")


# --------------------------------------------------------------------------- #
# Backup-on-write
# --------------------------------------------------------------------------- #
def backup_file(path: str | os.PathLike[str], round_n: int) -> Path:
    """Copy ``path`` to ``{path}.bak_round{NNN}_pre`` (the unifying convention)."""
    p = Path(path)
    backup = p.with_name(f"{p.name}.bak_round{int(round_n):03d}_pre")
    shutil.copy2(p, backup)
    return backup


# --------------------------------------------------------------------------- #
# mtime / freshness
# --------------------------------------------------------------------------- #
def mtime(path: str | os.PathLike[str]) -> float | None:
    p = Path(path)
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def newer_than(path: str | os.PathLike[str], *others: str | os.PathLike[str]) -> bool:
    """True iff ``path`` exists and is not older than any existing ``other``.

    Equal mtimes count as fresh (same write batch). A non-existent ``other`` does
    not constrain freshness.
    """
    pm = mtime(path)
    if pm is None:
        return False
    for other in others:
        om = mtime(other)
        if om is not None and om > pm:
            return False
    return True


# --------------------------------------------------------------------------- #
# CSV vocabulary readers
# --------------------------------------------------------------------------- #
def load_edges(dependencies_csv: str | os.PathLike[str]) -> list[tuple[str, str]]:
    """Load ``(prerequisite_id, dependant_id)`` edges, accepting the legacy
    ``source_id``/``target_id`` header (same direction)."""
    p = Path(dependencies_csv)
    if not p.exists():
        raise InputError(f"dependencies.csv not found: {p}")
    edges: list[tuple[str, str]] = []
    with p.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        header = set(reader.fieldnames or [])
        if {"prerequisite_id", "dependant_id"}.issubset(header):
            prereq_col, dep_col = "prerequisite_id", "dependant_id"
        elif {"source_id", "target_id"}.issubset(header):
            prereq_col, dep_col = "source_id", "target_id"
        else:
            raise InputError(
                f"dependencies.csv missing required columns "
                f"(prerequisite_id/dependant_id or legacy source_id/target_id): {p}"
            )
        for row in reader:
            prereq = (row.get(prereq_col) or "").strip()
            dep = (row.get(dep_col) or "").strip()
            if prereq and dep:
                edges.append((prereq, dep))
    return edges


def load_milestone_commits(milestones_csv: str | os.PathLike[str]) -> dict[str, list[str]]:
    """Map each milestone id to its parsed ``commits`` list."""
    rows, fieldnames = read_csv(milestones_csv)
    if "id" not in fieldnames:
        raise InputError(f"milestones.csv missing required id column: {milestones_csv}")
    result: dict[str, list[str]] = {}
    for row in rows:
        mid = (row.get("id") or "").strip()
        if mid:
            result[mid] = parse_commits(row.get("commits", ""))
    return result


# --------------------------------------------------------------------------- #
# Cross-round state
# --------------------------------------------------------------------------- #
def frozen_prefix_path(workspace: str | os.PathLike[str]) -> Path:
    return Path(workspace) / "_frozen_prefix.json"


def read_frozen_prefix(workspace: str | os.PathLike[str]) -> dict[str, Any]:
    """Read ``_frozen_prefix.json``; return ``{}`` if absent or malformed."""
    p = frozen_prefix_path(workspace)
    if not p.exists():
        return {}
    data, _ = safe_read_json(p)
    return data if isinstance(data, dict) else {}


def write_frozen_prefix_atomic(workspace: str | os.PathLike[str], data: Mapping[str, Any], round_n: int) -> Path | None:
    """Back up the existing ``_frozen_prefix.json`` to ``*.bak_round{NNN}_pre`` (if
    present), then atomically write the new state. Returns the backup path (or None)."""
    p = frozen_prefix_path(workspace)
    backup = backup_file(p, round_n) if p.exists() else None
    write_json_atomic(p, data)
    return backup


# --------------------------------------------------------------------------- #
# Per-milestone artifact locators (shared by dashboard / preflight / freeze-apply)
# --------------------------------------------------------------------------- #
def dockerfiles_dir(workspace: str | os.PathLike[str]) -> Path:
    return Path(workspace) / "dockerfiles"


def list_milestone_dirs(workspace: str | os.PathLike[str]) -> list[str]:
    """List milestone ids = sub*directories* of ``dockerfiles/`` (stray files like
    ``diagnosis_summary.json`` are ignored)."""
    d = dockerfiles_dir(workspace)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


def build_status_path(workspace: str | os.PathLike[str], mid: str) -> Path:
    return dockerfiles_dir(workspace) / mid / "build_status.json"


def read_build_status(workspace: str | os.PathLike[str], mid: str) -> tuple[dict[str, Any] | None, str | None]:
    data, err = safe_read_json(build_status_path(workspace, mid))
    if data is not None and not isinstance(data, dict):
        return None, f"build_status.json is not an object: {build_status_path(workspace, mid)}"
    return data, err


def latest_attempt_of(build_status: Mapping[str, Any] | None) -> str | None:
    if not isinstance(build_status, Mapping):
        return None
    artifact_paths = build_status.get("artifact_paths") or {}
    if not isinstance(artifact_paths, Mapping):
        return None
    latest = artifact_paths.get("latest_attempt")
    return str(latest) if latest else None


def classification_path(workspace: str | os.PathLike[str], mid: str, latest_attempt: str) -> Path:
    """Authoritative classification.json path: reconstructed from the workspace +
    ``latest_attempt`` (the absolute ``report_paths`` inside build_status are from
    the machine M2 ran on and must not be used)."""
    return Path(workspace) / "test_results" / mid / "refinement" / str(latest_attempt) / "classification.json"


def attempt_count(workspace: str | os.PathLike[str], mid: str) -> int:
    d = Path(workspace) / "_build_runs" / mid
    if not d.is_dir():
        return 0
    return sum(1 for p in d.glob("attempt_*") if p.is_dir())


def resolve_scope(workspace: str | os.PathLike[str], scope_arg: Sequence[str] | None = None) -> tuple[list[str], str]:
    """Resolve the survey scope: explicit ``--scope`` > ``_frozen_prefix`` scope >
    ``dockerfiles/*`` dirs. Returns ``(mids, source)``."""
    if scope_arg:
        return list(scope_arg), "scope_arg"
    fp = read_frozen_prefix(workspace)
    scope = fp.get("scope_milestones")
    if isinstance(scope, list) and scope:
        return [str(s) for s in scope], "frozen_prefix_scope"
    return list_milestone_dirs(workspace), "dockerfiles"


# --------------------------------------------------------------------------- #
# Output + arg helpers
# --------------------------------------------------------------------------- #
def emit(report: Any, output: str | os.PathLike[str] | None) -> None:
    """Write a JSON report to ``--output`` (atomically) or stdout."""
    text = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False, default=str)
    if output is None:
        print(text)
    else:
        _atomic_replace(Path(output), text + "\n")


def parse_id_list(value: str | None) -> list[str]:
    """Split a comma- and/or whitespace-separated id list into clean tokens."""
    if not value:
        return []
    return [tok for tok in re.split(r"[,\s]+", value.strip()) if tok]
