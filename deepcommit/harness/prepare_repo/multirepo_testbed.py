"""Workspace (multi-repo) testbed builder.

A workspace milestone's commits are partitioned by repo (``commit_repos.json``). One ``BranchCreator`` per repo builds
``testbed/<repo>/`` with that repo's ``milestone-{MID}-start`` / ``-end`` tags; a repo untouched by a milestone gets
``start == end`` (an empty-commit milestone). ``testbed/manifest.json`` records the per-repo base SHAs.

This module is a thin wrapper: all cherry-pick / snapshot logic stays in ``BranchCreator``. It is only used when
``build_milestone_testbed.py --workspace-dir`` is given, so single-repo behaviour is untouched.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from deepcommit.harness.prepare_repo.build_milestone_testbed import (
    BranchCreator,
    MilestoneData,
    _archive_testbed_run,
)

MANIFEST_NAME = "manifest.json"


def load_workspace_files(workspace_dir: Path) -> tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    repos = json.loads((workspace_dir / "repos.json").read_text(encoding="utf-8"))
    commit_repos = json.loads((workspace_dir / "commit_repos.json").read_text(encoding="utf-8"))
    return repos, commit_repos


def _read_rows(csv_path: Path) -> List[dict]:
    with open(csv_path, "r", newline="") as f:
        return list(csv.DictReader(f))


def partition_milestone_rows(
    rows: List[dict], repo_names: List[str], commit_repos: Dict[str, str]
) -> Dict[str, Dict[str, dict]]:
    """``{repo: {milestone_id: row-with-that-repo's-commits-only}}``; every repo gets every milestone."""
    out: Dict[str, Dict[str, dict]] = {r: {} for r in repo_names}
    for row in rows:
        shas = [c for c in (row.get("commits") or "").split(";") if c]
        by_repo: Dict[str, List[str]] = {r: [] for r in repo_names}
        for sha in shas:
            key = sha[:7].lower()
            if key not in commit_repos:
                raise KeyError(f"milestone {row['id']}: commit {sha} not in commit_repos.json")
            by_repo[commit_repos[key]].append(sha)
        itc = [c for c in (row.get("integration_test_commit") or "").split(";") if c.strip()]
        for repo in repo_names:
            sub = dict(row)
            sub["commits"] = ";".join(by_repo[repo])
            sub["integration_test_commit"] = ";".join(c for c in itc if commit_repos.get(c[:7].lower()) == repo)
            out[repo][row["id"]] = sub
    return out


def make_creator(
    name: str,
    info: Dict[str, Any],
    work_dir: Path,
    *,
    force_rebuild: bool,
    continue_on_error: bool,
    preserve_commit_order: bool,
    rename_threshold: int,
    verbose: bool,
) -> BranchCreator:
    return BranchCreator(
        repo_path=Path(info["checkout_path"]),
        work_dir=work_dir,
        base_tag=info.get("start") or "HEAD",
        verbose=verbose,
        rename_threshold=rename_threshold,
        force_rebuild=force_rebuild,
        src_dirs=info.get("repo_src_dirs") or None,
        main_branch=info.get("main_branch") or "main",
        test_dirs=info.get("test_dirs") or None,
        exclude_patterns=info.get("exclude") or None,
        preserve_commit_order=preserve_commit_order,
        continue_on_error=continue_on_error,
    )


def _prepare_milestones(creator: BranchCreator, rows: Dict[str, dict], preserve_commit_order: bool):
    milestones: Dict[str, MilestoneData] = {}
    for mid, row in rows.items():
        m = MilestoneData(row)
        if m.commits and not preserve_commit_order:
            m.commits = creator.sort_commits_by_topo_order(m.commits)
            m.last_commit = m.commits[-1]
        milestones[mid] = m
    return milestones


def _align_untouched(creator: BranchCreator, milestones: Dict[str, MilestoneData], results: List[Dict]) -> None:
    """A repo untouched by a milestone must have start == end (the start-state amend leaves them one commit apart)."""
    by_id = {r["milestone_id"]: r for r in results}
    for mid, m in milestones.items():
        if m.commits or mid not in by_id:
            continue
        r = by_id[mid]
        creator.run_git(f"tag -f {r['tag_name_end']} {r['tag_name_start']}", quiet=True)
        r["commit_sha_end"] = r["commit_sha_start"]


def changed_files_split(creator: BranchCreator, tag_start: str, tag_end: str) -> Dict[str, List[str]]:
    """Files changed start..end, split with this repo's own ``SrcFileFilter``."""
    res = creator.run_git(f"diff --name-only {tag_start} {tag_end}", capture=True, check=False)
    files = [f for f in res.stdout.splitlines() if f]
    return {
        "src_files": [f for f in files if creator.src_filter.is_src_file(f)],
        "test_files": [f for f in files if creator.src_filter.is_test_file(f)],
        "other_files": [
            f for f in files if not creator.src_filter.is_src_file(f) and not creator.src_filter.is_test_file(f)
        ],
    }


def build_workspace_testbed(
    *,
    workspace_dir: Path,
    milestone_csv: Path,
    dependencies_csv: Path,
    output_dir: Path,
    work_dir_name: str = "testbed",
    force_rebuild: bool = False,
    continue_on_error: bool = False,
    preserve_commit_order: bool = False,
    rename_threshold: int = 20,
    verbose: bool = False,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build every repo's testbed, patches, manifest, metadata and build_report. Returns ``{"failed": [...], ...}``."""
    workspace_dir = Path(workspace_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    repos, commit_repos = load_workspace_files(workspace_dir)
    names = list(repos)
    testbed = output_dir / work_dir_name
    rows = _read_rows(milestone_csv)
    parts = partition_milestone_rows(rows, names, commit_repos)

    creators: Dict[str, BranchCreator] = {}
    per_repo_ms: Dict[str, Dict[str, MilestoneData]] = {}
    per_repo_results: Dict[str, List[Dict]] = {}
    topo_order: List[str] = []
    dependencies: Dict[str, List[str]] = {}
    for name in names:
        c = make_creator(
            name,
            repos[name],
            testbed / name,
            force_rebuild=force_rebuild,
            continue_on_error=continue_on_error,
            preserve_commit_order=preserve_commit_order,
            rename_threshold=rename_threshold,
            verbose=verbose,
        )
        c.setup_work_repository()
        c.actual_base_commit = c._resolve_base_commit()
        ms = _prepare_milestones(c, parts[name], preserve_commit_order)
        dependencies = c.parse_dependencies(dependencies_csv)
        results, topo_order = c.create_all_branches(ms, dependencies)
        _align_untouched(c, ms, results)
        creators[name], per_repo_ms[name], per_repo_results[name] = c, ms, results

    failed = sorted({m for c in creators.values() for m in c._failed_milestones})
    report_path = write_workspace_build_report(
        output_dir=output_dir,
        creators=creators,
        topo_order=topo_order,
        per_repo_ms=per_repo_ms,
        dependencies=dependencies,
        continue_on_error=continue_on_error,
    )
    out: Dict[str, Any] = {"failed": failed, "report": report_path, "topo_order": topo_order}
    if failed:
        return out

    manifest = build_manifest(creators, per_repo_ms, per_repo_results, topo_order, workspace_id)
    (testbed / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    for name, c in creators.items():
        c.generate_milestone_patches(milestones=per_repo_ms[name], output_dir=output_dir, repo_name=name)
    metadata = generate_workspace_metadata(
        creators, per_repo_ms, per_repo_results, dependencies, topo_order, milestone_csv, dependencies_csv,
        output_dir, workspace_dir, workspace_id,
    )  # fmt: skip
    for n in metadata["repos"]:
        metadata["repos"][n]["role"] = repos[n].get("role", "library")
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    out.update(manifest=manifest, metadata=metadata)
    return out


def build_manifest(
    creators: Dict[str, BranchCreator],
    per_repo_ms: Dict[str, Dict[str, MilestoneData]],
    per_repo_results: Dict[str, List[Dict]],
    topo_order: List[str],
    workspace_id: Optional[str],
) -> Dict[str, Any]:
    """Base SHA per repo per milestone (``base_sha`` = start tag SHA, the tree the agent starts from)."""
    res_by = {n: {r["milestone_id"]: r for r in rs} for n, rs in per_repo_results.items()}
    milestones: Dict[str, Any] = {}
    for mid in topo_order:
        entry: Dict[str, Any] = {}
        for name in creators:
            r = res_by[name][mid]
            entry[name] = {
                "tag_start": r["tag_name_start"],
                "tag_end": r["tag_name_end"],
                "base_sha": r["commit_sha_start"],
                "end_sha": r["commit_sha_end"],
                "commits": list(per_repo_ms[name][mid].commits),
                "changed": r["commit_sha_start"] != r["commit_sha_end"],
            }
        milestones[mid] = entry
    return {
        "workspace_id": workspace_id,
        "repos": {
            n: {"path": n, "base_commit": c.actual_base_commit, "base_tag": c.base_tag} for n, c in creators.items()
        },
        "milestones": milestones,
    }


def generate_workspace_metadata(
    creators, per_repo_ms, per_repo_results, dependencies, topo_order, milestone_csv, dependencies_csv, output_dir,
    workspace_dir, workspace_id,
) -> Dict[str, Any]:  # fmt: skip
    """Per-repo ``BranchCreator.generate_metadata`` merged into one metadata.json (``repos`` + per-repo milestone data)."""
    per_repo_meta = {
        n: c.generate_metadata(
            milestones=per_repo_ms[n],
            dependencies=dependencies,
            topo_order=topo_order,
            results=per_repo_results[n],
            milestone_csv_path=milestone_csv,
            dependencies_csv_path=dependencies_csv,
            output_dir=output_dir,
        )
        for n, c in creators.items()
    }
    first = next(iter(per_repo_meta.values()))
    merged: List[Dict[str, Any]] = []
    for mid in topo_order:
        repos_entry: Dict[str, Any] = {}
        title = category = ""
        parents: List[str] = []
        for n, meta in per_repo_meta.items():
            m = next(x for x in meta["milestones"] if x["id"] == mid)
            title, category, parents = m["title"], m["category"], m["parent_milestones"]
            split = changed_files_split(creators[n], m["tag_name_start"], m["tag_name_end"])
            repos_entry[n] = {
                "commits": m["commits"],
                "commits_count": m["commits_count"],
                "base_commit": m["base_commit"],
                "tag_name_start": m["tag_name_start"],
                "commit_sha_start": m["commit_sha_start"],
                "tag_name_end": m["tag_name_end"],
                "commit_sha_end": m["commit_sha_end"],
                **split,
            }
        merged.append(
            {
                "id": mid,
                "title": title,
                "category": category,
                "commits_count": sum(r["commits_count"] for r in repos_entry.values()),
                "commits": ";".join(r["commits"] for r in repos_entry.values() if r["commits"]),
                "tag_name_start": f"milestone-{mid}-start",
                "tag_name_end": f"milestone-{mid}-end",
                "parent_milestones": parents,
                "repos": repos_entry,
                "repos_changed": [n for n, r in repos_entry.items() if r["commit_sha_start"] != r["commit_sha_end"]],
            }
        )
    return {
        "workspace_id": workspace_id,
        "workspace_dir": str(Path(workspace_dir).absolute()),
        "milestone_csv_source": str(Path(milestone_csv).absolute()),
        "dependencies_csv_source": str(Path(dependencies_csv).absolute()),
        "generated_time": datetime.now().isoformat(),
        "total_milestones": len(topo_order),
        "total_commits": sum(m["commits_count"] for m in merged),
        "rename_threshold": first["rename_threshold"],
        "generator_version": "branch_creator-1.2-workspace",
        "topological_order": first["topological_order"],
        "repos": {
            n: {
                "repo_name": meta["repo_name"],
                "repo_path": meta["repo_path"],
                "base_tag": meta["base_tag"],
                "main_branch": meta["main_branch"],
                "repo_src_dirs": meta["repo_src_dirs"],
                "test_dirs": meta["test_dirs"],
                "exclude_patterns": meta["exclude_patterns"],
            }
            for n, meta in per_repo_meta.items()
        },
        "milestones": merged,
    }


def write_workspace_build_report(
    *,
    output_dir: Path,
    creators: Dict[str, BranchCreator],
    topo_order: List[str],
    per_repo_ms: Dict[str, Dict[str, MilestoneData]],
    dependencies: Dict[str, List[str]],
    continue_on_error: bool,
) -> Path:
    """build_report.json with per-repo conflict diagnostics; overall status = worst across repos."""
    milestones: Dict[str, Any] = {}
    counts: Dict[str, int] = {}
    for mid in topo_order:
        per_repo: Dict[str, Any] = {}
        for name, c in creators.items():
            rec = dict(
                c._milestone_statuses.get(mid) or {"status": "not_attempted", "parents": dependencies.get(mid, [])}
            )
            rec["num_commits_csv"] = len(per_repo_ms[name][mid].commits)
            per_repo[name] = rec
        statuses = {r["status"] for r in per_repo.values()}
        for s in ("conflict", "skipped_due_to_parent", "not_attempted", "success", "success_cached"):
            if s in statuses:
                overall = s
                break
        else:
            overall = sorted(statuses)[0]
        first = next(iter(per_repo_ms.values()))[mid]
        milestones[mid] = {
            "status": overall,
            "title": first.title,
            "category": first.category,
            "failing_repos": [n for n, r in per_repo.items() if r["status"] == "conflict"],
            "repos": per_repo,
        }
        counts[overall] = counts.get(overall, 0) + 1
    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mode": {"continue_on_error": continue_on_error, "workspace": True},
        "repos": list(creators),
        "total_milestones": len(topo_order),
        "topological_order": topo_order,
        "summary": counts,
        "failed_milestones": sorted({m for c in creators.values() for m in c._failed_milestones}),
        "milestones": milestones,
    }
    path = output_dir / "build_report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    _archive_testbed_run(output_dir=output_dir, report_path=path)
    return path
