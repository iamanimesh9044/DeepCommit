"""preflight — pre-operation go/no-go gate (deliverables clean / fresh / consistent).

Run before ANY operation (incl. a dag_update). Validates the real next-stage
deliverables — Dockerfiles + build_status, test_results, milestones/dependencies
CSVs, testbed/, and the frozen recipe pins — for cleanliness, content-hash
freshness, and cross-file consistency, then returns one verdict. It JUDGES only:
it never runs Docker and prints no remedy command (the caller decides the fix).

**Fail-closed**: when a check cannot obtain its evidence (no recorded input
hashes, no M1 run, a missing pin archive, …) it emits a finding rather than
silently passing — an unverifiable workspace is not a safe workspace.

Findings: ``{milestone_id, check, severity, detail[, restore_source]}``.
  severity ``violation`` → verdict ``blocked``       (unsafe to operate; fix required)
  severity ``stale``     → verdict ``needs_rebuild``  (outdated / unverifiable; refresh required)
  severity ``warning``   → surfaced only (no verdict impact)

Checklist: C1 recipe freshness (hash) · C2 structural closure (ALL milestones.csv
ids) · C3 pointer integrity · C3′ rebuild_crashed (+restore_source) · C4 testbed
clean · C5 M1 currency · C6 frozen-recipe drift · C7 waiver schema.

Exit: 0 safe · 2 blocked / needs_rebuild · 1 input invalid.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path
from typing import Any, Sequence

from deepcommit.harness.main_orch import context as ctx

# build_status.inputs hash key -> the recipe file it pins
_RECIPE_INPUTS = {
    "dockerfile_sha256": "Dockerfile",
    "test_config_sha256": "test_config.json",
    "module2_waivers_sha256": "module2_waivers.json",
    "module2_signals_sha256": "module2_signals.json",
}
# Universal bound_waiver fields (the rest are <100% in the real corpus — not gated).
_WAIVER_REQUIRED = ("reason_code", "anchor", "rationale")


def _finding(mid: str | None, check: str, severity: str, detail: Any, **extra: Any) -> dict[str, Any]:
    return {"milestone_id": mid, "check": check, "severity": severity, "detail": detail, **extra}


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _attempt_num(name: str) -> int:
    try:
        return int(name.rsplit("_", 1)[-1])
    except ValueError:
        return -1


# --- C4: host testbed clean --------------------------------------------------
def host_testbed_clean(workspace: str | Path) -> bool | None:
    """True if ``testbed/`` git status is clean, False if dirty, None if inconclusive."""
    testbed = Path(workspace) / "testbed"
    if not testbed.is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(testbed), "status", "--porcelain"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() == ""


# --- C1: recipe freshness (content hash; fail-closed when unrecorded) --------
def _recipe_freshness(ws: Path, mid: str, build_status: dict) -> list[dict[str, Any]]:
    inputs = build_status.get("inputs")
    if not isinstance(inputs, dict) or not any(inputs.get(k) for k in _RECIPE_INPUTS):
        return [
            _finding(mid, "recipe_freshness", "stale", "freshness unverifiable: build_status records no input hashes")
        ]
    rdir = ctx.dockerfiles_dir(ws) / mid
    drifted = [
        fname for key, fname in _RECIPE_INPUTS.items() if inputs.get(key) and _sha256(rdir / fname) != inputs[key]
    ]
    if drifted:
        return [
            _finding(mid, "recipe_freshness", "stale", f"build_status predates current recipe (changed: {drifted})")
        ]
    return []


# --- C3 / C3′: pointer integrity + rebuild_crashed ---------------------------
def _last_known_good(ws: Path, mid: str) -> str | None:
    d = ws / "_build_runs" / mid
    if not d.is_dir():
        return None
    attempts = sorted((p for p in d.glob("attempt_*") if p.is_dir()), key=lambda p: _attempt_num(p.name), reverse=True)
    for attempt in attempts:
        bs = attempt / "after_validation" / "build_status.json"
        data, _ = ctx.safe_read_json(bs)
        if isinstance(data, dict) and data.get("built") is True:
            return str(bs)
    return None


def _integrity(ws: Path, mid: str, build_status: dict) -> list[dict[str, Any]]:
    latest = ctx.latest_attempt_of(build_status)
    integ = (build_status.get("build_run_integrity") or {}).get("B_tests_ran_successfully") or {}
    b_tests_failed = ("B_tests_ran_successfully" in (build_status.get("failed_gates") or [])) or (
        integ.get("passed") is False
    )
    classification_missing = (latest is None) or (not ctx.classification_path(ws, mid, latest).exists())
    # C3′ rebuild_crashed: the rebuild died before tests ran → this built=false is spurious.
    if build_status.get("built") is False and b_tests_failed and classification_missing:
        return [
            _finding(
                mid,
                "rebuild_crashed",
                "violation",
                "rebuild died before tests ran (built=false, B_tests_ran_successfully failed, "
                "classification.json missing) — this built=false is spurious; restore the last known-good",
                restore_source=_last_known_good(ws, mid),
            )
        ]
    # C3 pointer integrity: tests ran but the classification.json they produced is gone
    # (covers both a missing latest_attempt and a missing artifact for the recorded attempt).
    if integ.get("passed") is True and classification_missing:
        return [
            _finding(
                mid,
                "classification_pointer",
                "violation",
                "B_tests_ran_successfully passed but classification.json is missing "
                "(no latest_attempt recorded, or the artifact is gone)",
            )
        ]
    return []


# --- C7: bound_waiver schema -------------------------------------------------
def _waiver_schema(ws: Path, mid: str) -> list[dict[str, Any]]:
    data, _ = ctx.safe_read_json(ctx.dockerfiles_dir(ws) / mid / "module2_waivers.json")
    if not isinstance(data, dict):
        return []
    bw = data.get("bound_waivers")
    if bw is None:
        return []
    if not isinstance(bw, list):
        return [_finding(mid, "waiver_schema", "warning", "bound_waivers is not a list")]
    issues: list[Any] = []
    for i, w in enumerate(bw):
        if not isinstance(w, dict):
            issues.append(f"bound_waivers[{i}] is not an object")
            continue
        missing = [f for f in _WAIVER_REQUIRED if not (isinstance(w.get(f), str) and w[f].strip())]
        if missing:
            issues.append({"waiver_index": i, "missing_or_invalid": missing})
    return [_finding(mid, "waiver_schema", "warning", issues)] if issues else []


# --- C2: structural closure (ALL milestones.csv ids ↔ filesystem) ------------
def _structural(ws: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    ddir = ctx.dockerfiles_dir(ws)
    ms_ids = set(ctx.load_milestone_commits(ws / "milestones.csv"))
    for mid in sorted(ms_ids):  # every CSV milestone, not just the operating scope
        for fname in ("Dockerfile", "build_status.json", "test_config.json"):
            if not (ddir / mid / fname).exists():
                findings.append(_finding(mid, "structural", "violation", f"missing dockerfiles/{mid}/{fname}"))
    dangling: set[str] = set()
    for prereq, dep in ctx.load_edges(ws / "dependencies.csv"):
        dangling.update(e for e in (prereq, dep) if e not in ms_ids)
    for endpoint in sorted(dangling):
        findings.append(
            _finding(
                endpoint, "structural", "violation", f"dependencies.csv endpoint {endpoint!r} not in milestones.csv"
            )
        )
    return findings


# --- C5: M1 currency (fail-closed when evidence is missing) ------------------
def _m1_currency(ws: Path) -> list[dict[str, Any]]:
    runs = ws / "_testbed_runs"
    run_dirs = [p for p in runs.iterdir() if p.is_dir() and p.name.startswith("run_")] if runs.is_dir() else []
    if not run_dirs:
        return [_finding(None, "m1_currency", "stale", "no _testbed_runs evidence — M1 currency unverifiable")]
    latest = max(run_dirs, key=lambda p: p.name)
    data, _ = ctx.safe_read_json(latest / "build_report.json")
    if not isinstance(data, dict) or data.get("failed_milestones") is None:
        return [
            _finding(
                None, "m1_currency", "stale", f"{latest.name} build_report missing/unreadable — currency unverifiable"
            )
        ]
    if data["failed_milestones"]:
        return [
            _finding(
                None, "m1_currency", "stale", f"{latest.name} reports failed milestones: {data['failed_milestones']}"
            )
        ]
    for csv in ("milestones.csv", "dependencies.csv"):
        arch, cur = latest / csv, ws / csv
        if not arch.exists() or not cur.exists():
            return [_finding(None, "m1_currency", "stale", f"{latest.name}/{csv} not archived — currency unverifiable")]
        if _sha256(arch) != _sha256(cur):
            return [
                _finding(None, "m1_currency", "stale", f"{latest.name}/{csv} differs from current — M1 lags the DAG")
            ]
    return []


# --- C6: frozen-recipe drift (fail-closed when the pin can't be verified) ----
def _frozen_drift(ws: Path) -> list[dict[str, Any]]:
    pins = (ctx.read_frozen_prefix(ws).get("frozen_checkpoints") or {}).get("milestones") or {}
    findings: list[dict[str, Any]] = []
    for mid, attempt in pins.items():
        if not attempt:
            findings.append(
                _finding(mid, "frozen_recipe_drift", "stale", "frozen pin has no recorded attempt — drift unverifiable")
            )
            continue
        cur = ctx.dockerfiles_dir(ws) / mid / "Dockerfile"
        if not cur.exists():
            continue  # a missing current Dockerfile is C2's job
        pinned = ws / "_build_runs" / mid / str(attempt) / "after_agent" / "Dockerfile"
        if not pinned.exists():
            findings.append(
                _finding(
                    mid,
                    "frozen_recipe_drift",
                    "stale",
                    f"pinned recipe archive missing ({attempt}) — drift unverifiable",
                )
            )
        elif _sha256(cur) != _sha256(pinned):
            findings.append(
                _finding(mid, "frozen_recipe_drift", "violation", f"frozen Dockerfile changed since pinned {attempt}")
            )
    return findings


def run_preflight(workspace: str | Path, *, scope: Sequence[str] | None = None) -> tuple[dict[str, Any], int]:
    ws = ctx.resolve_workspace(workspace)
    mids, source = ctx.resolve_scope(ws, scope)

    findings: list[dict[str, Any]] = []
    findings += _structural(ws)  # C2 (all CSV ids)
    findings += _m1_currency(ws)  # C5
    findings += _frozen_drift(ws)  # C6
    for mid in mids:
        build_status, err = ctx.read_build_status(ws, mid)
        if build_status is None:
            findings.append(_finding(mid, "build_status_present", "warning", err or "missing build_status.json"))
            continue
        findings += _recipe_freshness(ws, mid, build_status)  # C1
        findings += _integrity(ws, mid, build_status)  # C3 / C3′
        findings += _waiver_schema(ws, mid)  # C7

    host_clean = host_testbed_clean(ws)  # C4
    if host_clean is False:
        findings.append(
            _finding(
                None,
                "host_testbed",
                "violation",
                "host testbed/ has uncommitted changes; COPY . /testbed/ would drag residue into the image",
            )
        )
    elif host_clean is None:
        findings.append(
            _finding(
                None, "host_testbed", "warning", "testbed cleanliness unverifiable (no testbed/ dir or not a git repo)"
            )
        )

    has_violation = any(f["severity"] == "violation" for f in findings)
    has_stale = any(f["severity"] == "stale" for f in findings)
    if has_violation:
        verdict, code = "blocked", ctx.VIOLATION
    elif has_stale:
        verdict, code = "needs_rebuild", ctx.VIOLATION
    else:
        verdict, code = "safe", ctx.OK

    report = {
        "verb": "preflight",
        "workspace": str(ws),
        "scope_source": source,
        "host_clean": host_clean,
        "verdict": verdict,
        "summary": {
            "n_findings": len(findings),
            "n_violations": sum(1 for f in findings if f["severity"] == "violation"),
            "n_stale": sum(1 for f in findings if f["severity"] == "stale"),
            "n_warnings": sum(1 for f in findings if f["severity"] == "warning"),
        },
        "findings": findings,
        "passed": code == ctx.OK,
    }
    return report, code


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="m3 preflight", description="Pre-operation go/no-go: deliverables clean / fresh / self-consistent."
    )
    parser.add_argument("--workspace", default=None, help="Harness workspace directory")
    parser.add_argument(
        "--scope", default=None, help="Comma/space-separated milestone ids (default: frozen scope or dockerfiles/*)"
    )
    parser.add_argument("--output", type=Path, default=None, help="Output JSON path; defaults to stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        report, code = run_preflight(args.workspace, scope=ctx.parse_id_list(args.scope) or None)
    except ctx.InputError as exc:
        ctx.emit({"verb": "preflight", "errors": [str(exc)], "passed": False}, args.output)
        return ctx.INPUT_INVALID
    ctx.emit(report, args.output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
