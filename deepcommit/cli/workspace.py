"""Multi-repo workspace commands: validate / preproc / merge / run."""

from __future__ import annotations

from pathlib import Path

from deepcommit.multirepo import MergeResult, Workspace, load_workspace, merge_workspace, workspace_dir
from deepcommit.multirepo.merge import preprocessed_path, repo_checkout_path

from . import preproc, trials


def member_repo_spec(spec) -> str:
    """Repo spec (``owner_repo:start_end``) of a member, as understood by the single-repo preproc commands."""
    span = f"{spec.start}_{spec.end}" if spec.start and spec.end else f"{spec.since}_{spec.until}"
    return f"{spec.repo_id}:{span}"


def validate_workspace(manifest: Path | str, data_root: Path | None = None) -> tuple[Workspace, list[str]]:
    """Parse the manifest (raises WorkspaceError if invalid) and return (workspace, warnings)."""
    ws = load_workspace(manifest)
    warnings: list[str] = []
    if data_root is not None:
        for spec in ws.repos:
            pre = preprocessed_path(data_root, spec)
            if not (pre / "main_commits.jsonl").exists():
                warnings.append(f"{spec.name}: Phase-1 output missing at {pre} (run `workspace preproc`)")
            if not repo_checkout_path(data_root, spec).exists():
                warnings.append(f"{spec.name}: checkout missing at {repo_checkout_path(data_root, spec)}")
    return ws, warnings


def format_workspace(ws: Workspace) -> str:
    lines = [f"workspace_id: {ws.workspace_id}  repos: {len(ws.repos)}"]
    for r in ws.repos:
        lines.append(f"  - {r.name:<16} {r.role:<9} {r.full_name} {r.start}..{r.end} src={r.repo_src_dirs}")
    return "\n".join(lines)


def run_validate(manifest: Path | str, data_root: Path) -> int:
    ws, warnings = validate_workspace(manifest, data_root)
    print(format_workspace(ws))
    for w in warnings:
        print(f"WARNING: {w}")
    print("OK" if not warnings else f"OK with {len(warnings)} warning(s)")
    return 0


def run_merge(manifest: Path | str, data_root: Path) -> MergeResult:
    ws = load_workspace(manifest)
    res = merge_workspace(ws, data_root)
    print(f"merged -> {res.out_dir}")
    print(f"  commits per repo: {res.commits_per_repo}")
    print(
        f"  intra edges: {res.intra_edges}  xrepo hard: {res.xrepo_hard}  soft: {res.xrepo_soft}  "
        f"dropped (cycle): {res.xrepo_dropped_cycle}"
    )
    for n in res.notes:
        print(f"  note: {n}")
    return res


def run_preproc(manifest: Path | str, data_root: Path, overwrite: bool = False, verbose: bool = False) -> int:
    """Run the unchanged single-repo ``preproc all`` for each member, then merge."""
    ws = load_workspace(manifest)
    for spec in ws.repos:
        print(f"[workspace preproc] {spec.name}: {member_repo_spec(spec)}")
        rc = preproc.run_all_preprocessing(
            repo_spec=member_repo_spec(spec),
            data_root=data_root,
            repo_src_dirs=spec.repo_src_dirs,
            overwrite=overwrite,
            main_branch=spec.main_branch,
            test_dirs=spec.test_dirs or None,
            exclude=spec.exclude or None,
            verbose=verbose,
        )
        if rc != 0:
            print(f"Phase 1 failed for {spec.name} (exit {rc})")
            return rc
    run_merge(manifest, data_root)
    return 0


def run_workspace_trial(manifest: Path | str, trial_name: str, data_root: Path, **kwargs) -> None:
    trials.run_multi_stage_trial(
        config_file=kwargs.pop("config_file", "multi_stage_pipeline.yaml"),
        repo_id="",
        trial_name=trial_name,
        data_root=data_root,
        workspace_manifest=Path(manifest),
        **kwargs,
    )


def run_bundle(
    manifest: Path | str,
    trial_name: str,
    data_root: Path,
    dry_run: bool = False,
    min_share: float = 0.4,
    max_commits: int = 40,
    max_repos: int = 3,
) -> dict:
    """Merge single-repo stage-2 milestones into cross-repo milestones using hard cross-repo edges."""
    import csv
    import json
    import shutil

    from ..multirepo.bundle import BundleParams, bundle_milestones

    ws = load_workspace(manifest)
    wdir = workspace_dir(data_root, ws.workspace_id)
    trial_dir = data_root / "trial_space" / f"{ws.workspace_id}_{trial_name}"
    csv_path = trial_dir / "stage2_mstone" / "output" / "milestones.csv"
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    dag = json.loads((wdir / "merged" / "commit_dag.json").read_text(encoding="utf-8"))
    commit_repo = json.loads((wdir / "commit_repos.json").read_text(encoding="utf-8"))
    params = BundleParams(min_mutual_share=min_share, max_commits=max_commits, max_repos=max_repos)
    new_rows, rep = bundle_milestones(rows, dag["edges_dag"], commit_repo, params)
    summary = {
        "before": rep.before,
        "after": rep.after,
        "merges": rep.merges,
        "multi_repo_milestones": rep.multi_repo_milestones,
        "leftover_links": rep.leftover_links,
    }
    (trial_dir / "stage2_mstone" / "output" / "bundle_report.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    if dry_run:
        return summary
    backup = csv_path.with_name("milestones.premerge.csv")
    if not backup.exists():
        shutil.copy2(csv_path, backup)
    if "repos" not in fields:
        fields.append("repos")
    targets = [csv_path]
    result_copy = data_root / "result" / f"{ws.workspace_id}_{trial_name}" / "stage2_mstone" / "milestones.csv"
    if result_copy.exists():
        targets.append(result_copy)
    for t in targets:
        with open(t, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(new_rows)
    return summary


def run_export_task(
    manifest: Path | str,
    trial_name: str,
    data_root: Path,
    out_dir: Path | None = None,
    task_id: str | None = None,
    min_repos: int = 1,
    milestones_csv: str | None = None,
    dependencies_csv: str | None = None,
):
    """Export a workspace trial as a CSF cross-repo task (see ``deepcommit.export``)."""
    from ..export import export_csf_task

    out = out_dir or data_root / "csf_tasks" / "cross-repo-change"
    report = export_csf_task(
        manifest,
        trial_name,
        data_root,
        out,
        task_id=task_id,
        min_repos=min_repos,
        milestones_csv=milestones_csv,
        dependencies_csv=dependencies_csv,
    )
    print(report.format())
    return report


def run_check_task(task_dir: Path | str, allow_missing_steps: bool = False) -> int:
    """Validate a task folder against the CSF format; returns the process exit code."""
    from ..export.csf_check import check_task

    report = check_task(Path(task_dir), allow_missing_steps=allow_missing_steps)
    print(report.format())
    return 0 if report.ok else 1


__all__ = [
    "workspace_dir",
    "run_validate",
    "run_merge",
    "run_preproc",
    "run_workspace_trial",
    "run_bundle",
    "run_export_task",
    "run_check_task",
    "validate_workspace",
]
