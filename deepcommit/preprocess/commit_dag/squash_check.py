from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Reuse utilities from commit_dag.py
try:
    # When executed as a module
    from . import commit_dag as cd  # type: ignore
except Exception:
    # Fallback when running as a script
    import sys

    sys.path.append(str(Path(__file__).parent))
    import commit_dag as cd  # type: ignore


def _topo_cycle_check(nodes: List[str], edges: List[Tuple[str, str]]) -> bool:
    """Return True if a cycle exists within 'nodes' under directed 'edges'."""
    from collections import defaultdict, deque

    idx = {n: 0 for n in nodes}
    adj = defaultdict(list)
    for s, d in edges:
        if s in idx and d in idx:
            adj[s].append(d)
            idx[d] += 1
    q = deque([n for n, deg in idx.items() if deg == 0])
    seen = 0
    while q:
        u = q.popleft()
        seen += 1
        for v in adj[u]:
            idx[v] -= 1
            if idx[v] == 0:
                q.append(v)
    return seen != len(nodes)


def _validate_levels_dag(dag_json: Path, kinds: List[str]) -> Dict:
    data = json.loads(dag_json.read_text())
    levels: Dict[str, int] = data["levels"]
    edges_raw = data["edges"]
    nodes = set(data["nodes"].keys())

    # group by level
    lvl2nodes: Dict[int, List[str]] = {}
    for n, lvl in levels.items():
        lvl2nodes.setdefault(lvl, []).append(n)

    # filter edges by kinds
    # Support both old format (kind as string) and new format (kind as list)
    filtered_edges = []
    for e in edges_raw:
        edge_kind = e.get("kind")
        if isinstance(edge_kind, list):
            # New format: check if any kind in the list matches
            if any(k in kinds for k in edge_kind):
                # Use first matching kind for compatibility
                matched_kind = next(k for k in edge_kind if k in kinds)
                filtered_edges.append((e["src"], e["dst"], matched_kind))
        else:
            # Old format: direct check
            if edge_kind in kinds:
                filtered_edges.append((e["src"], e["dst"], edge_kind))
    E = filtered_edges

    per_level = {}
    for lvl in sorted(lvl2nodes.keys()):
        S = set(lvl2nodes[lvl])
        intra = [(s, d) for (s, d, k) in E if s in S and d in S]

        # predecessor closure for this level under allowed kinds
        from collections import defaultdict, deque

        incoming = defaultdict(set)
        for s, d, _k in E:
            incoming[d].add(s)
        preds = set()
        q = deque(list(S))
        while q:
            u = q.popleft()
            for p in incoming.get(u, ()):  # predecessors
                if p not in preds:
                    preds.add(p)
                    q.append(p)

        group = sorted(S | preds)
        # cycle check within the induced subgraph
        sub_edges = [(s, d) for (s, d, k) in E if s in group and d in group]
        has_cycle = _topo_cycle_check(group, sub_edges)

        per_level[lvl] = {
            "level_nodes": sorted(S),
            "intra_edges": [(s, d) for (s, d) in intra],
            "intra_edges_count": len(intra),
            "required_prior_nodes": sorted(preds),
            "squash_group_nodes": group,
            "group_edges_count": len(sub_edges),
            "group_has_cycle": has_cycle,
            "ok_level_squash": len(intra) == 0,
            "ok_group_squash": not has_cycle,
        }

    return {
        "edge_kinds": kinds,
        "levels": sorted(lvl2nodes.keys()),
        "per_level": per_level,
        "summary": {
            "total_nodes": len(nodes),
        },
    }


def _random_validate(repo: Path, dag_json: Path, level: Optional[int], tries: int, randomize: bool) -> Dict:
    """Validate squash feasibility by cherry-picking commits in random order.

    Proper approach:
    1. Find all commits from levels 0 to level-1 (prior commits)
    2. Find the merge-base of all prior commits as the starting point
    3. Cumulatively cherry-pick all prior commits (in deterministic order)
    4. Cherry-pick target level commits (in random order if randomize=True)
    5. Check if all attempts succeed and produce the same tree
    """
    import random
    import shutil
    import tempfile
    from typing import Any

    data = json.loads(dag_json.read_text())
    levels_map: Dict[str, int] = data["levels"]

    # Determine target level
    target_level = level if level is not None else max(levels_map.values())

    # Collect target level commits
    target_commits = [n for n, l in levels_map.items() if l == target_level]  # noqa: E741
    if not target_commits:
        return {
            "mode": "validate",
            "level": target_level,
            "error": "no nodes for given level",
        }

    # Collect all prior level commits (levels 0 to target_level - 1)
    prior_commits = [n for n, l in levels_map.items() if l < target_level]  # noqa: E741

    # Calculate proper base commit
    if prior_commits:
        # Base is the merge-base of all prior level commits
        base = cd.run_git(repo, ["merge-base", "--octopus", *prior_commits], check=False).strip()
        base_strategy = "merge-base of prior levels"
    else:
        # For level 0, use merge-base of target commits themselves
        base = cd.run_git(repo, ["merge-base", "--octopus", *target_commits], check=False).strip()
        base_strategy = "merge-base of target level (no prior levels)"

    if not base:
        return {"mode": "validate", "level": target_level, "error": "merge-base failed"}

    base_level = levels_map.get(base, "external")

    tmpdir = Path(tempfile.mkdtemp(prefix="rnd_squash_"))
    attempts: List[Dict[str, Any]] = []
    try:
        code = subprocess.run(
            ["git", "clone", str(repo), str(tmpdir / "repo"), "--no-tags", "--quiet"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if code.returncode != 0:
            return {"mode": "validate", "level": target_level, "error": f"clone failed: {code.stderr.strip()}"}
        rdir = tmpdir / "repo"

        base_order = sorted(target_commits)  # deterministic base order
        N = max(1, tries)

        for t in range(N):
            order = base_order[:]
            if randomize:
                random.shuffle(order)

            # Step 1: Checkout base
            subprocess.run(
                ["git", "-C", str(rdir), "checkout", "-B", "squash-test", base],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            attempt: Dict[str, Any] = {
                "try": t + 1,
                "order": order,
                "success": False,
                "failed_at": None,
                "conflict_files": [],
                "cumulative_commit": None,
                "new_commit": None,
                "new_tree": None,
            }

            # Step 2: Cumulatively cherry-pick all prior level commits
            cumulative_ok = True
            if prior_commits:
                # Sort by (level, sha) to ensure topological order
                prior_with_level = [(levels_map[sha], sha) for sha in prior_commits]
                prior_order = [sha for lvl, sha in sorted(prior_with_level)]
                for sha in prior_order:
                    p = subprocess.run(
                        ["git", "-C", str(rdir), "cherry-pick", "-n", sha],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    if p.returncode != 0:
                        # Conflict in prior commits - this shouldn't happen if DAG is correct
                        p2 = subprocess.run(
                            ["git", "-C", str(rdir), "diff", "--name-only", "--diff-filter=U"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                        files = [x for x in p2.stdout.splitlines() if x.strip()]
                        subprocess.run(
                            ["git", "-C", str(rdir), "cherry-pick", "--abort"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                        attempt["failed_at"] = f"prior:{sha}"
                        attempt["conflict_files"] = files
                        cumulative_ok = False
                        break

                if cumulative_ok:
                    # Commit the cumulative prior levels
                    p3 = subprocess.run(
                        ["git", "-C", str(rdir), "commit", "-m", f"Cumulative prior levels (0 to {target_level - 1})"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    if p3.returncode == 0:
                        h = subprocess.run(
                            ["git", "-C", str(rdir), "rev-parse", "HEAD"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                        attempt["cumulative_commit"] = h.stdout.strip()
                    else:
                        attempt["failed_at"] = "<cumulative commit stage>"
                        cumulative_ok = False

            if not cumulative_ok:
                attempts.append(attempt)
                continue

            # Step 3: Cherry-pick target level commits in specified order
            ok = True
            for sha in order:
                p = subprocess.run(
                    ["git", "-C", str(rdir), "cherry-pick", "-n", sha],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if p.returncode != 0:
                    # conflicts
                    p2 = subprocess.run(
                        ["git", "-C", str(rdir), "diff", "--name-only", "--diff-filter=U"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    files = [x for x in p2.stdout.splitlines() if x.strip()]
                    subprocess.run(
                        ["git", "-C", str(rdir), "cherry-pick", "--abort"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    attempt["failed_at"] = sha
                    attempt["conflict_files"] = files
                    ok = False
                    break

            if ok:
                p3 = subprocess.run(
                    ["git", "-C", str(rdir), "commit", "-m", f"random-squash level {target_level}"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if p3.returncode == 0:
                    h = subprocess.run(
                        ["git", "-C", str(rdir), "rev-parse", "HEAD"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    t_sha = subprocess.run(
                        ["git", "-C", str(rdir), "rev-parse", "HEAD^{tree}"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    attempt["success"] = True
                    attempt["new_commit"] = h.stdout.strip()
                    attempt["new_tree"] = t_sha.stdout.strip()
                else:
                    attempt["failed_at"] = "<commit stage>"
                    attempt["conflict_files"] = []

            attempts.append(attempt)

        # Analyze results
        successful_attempts = [a for a in attempts if a.get("success")]
        unique_trees = set(a["new_tree"] for a in successful_attempts if a.get("new_tree"))

        return {
            "mode": "validate",
            "level": target_level,
            "base": base,
            "base_level": base_level,
            "base_strategy": base_strategy,
            "num_prior_commits": len(prior_commits),
            "num_target_commits": len(target_commits),
            "randomize": randomize,
            "tries": tries,
            "attempts": attempts,
            "any_success": len(successful_attempts) > 0,
            "all_success": len(successful_attempts) == len(attempts),
            "unique_trees": list(unique_trees),
            "num_unique_trees": len(unique_trees),
            "deterministic_tree": len(unique_trees) == 1 if unique_trees else False,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _format_dag_summary(res: Dict, verbose: bool = False) -> str:
    """Format DAG analysis result in a human-readable way."""
    lines = []
    lines.append("=" * 80)
    lines.append("DAG Squash Analysis Summary")
    lines.append("=" * 80)
    lines.append(f"Edge kinds considered: {', '.join(res['edge_kinds'])}")
    lines.append(f"Total levels: {len(res['levels'])}")
    lines.append(f"Total nodes: {res['summary']['total_nodes']}")
    lines.append("")

    # Count squashable levels
    ok_level_count = sum(1 for info in res["per_level"].values() if info["ok_level_squash"])
    ok_group_count = sum(1 for info in res["per_level"].values() if info["ok_group_squash"])

    lines.append(f"Levels with no intra-level edges (can squash freely): {ok_level_count}/{len(res['levels'])}")
    lines.append(f"Levels with acyclic dependency groups: {ok_group_count}/{len(res['levels'])}")
    lines.append("")

    # Show problematic levels (if any)
    problematic_levels = [
        (lvl, info)
        for lvl, info in res["per_level"].items()
        if not info["ok_level_squash"] or not info["ok_group_squash"]
    ]

    if problematic_levels:
        lines.append("⚠ Problematic levels (with intra-level dependencies or cycles):")
        for lvl, info in sorted(problematic_levels, key=lambda x: int(str(x[0]))):
            lines.append(f"  Level {lvl}:")
            lines.append(f"    Nodes: {len(info['level_nodes'])}")
            lines.append(f"    Intra-level edges: {info['intra_edges_count']}")
            lines.append(f"    OK for level squash: {info['ok_level_squash']}")
            lines.append(f"    OK for group squash: {info['ok_group_squash']}")
    else:
        lines.append("✓ All levels are squashable!")

    lines.append("")
    lines.append("Level-by-level statistics:")
    lines.append(f"{'Level':<8} {'Nodes':<8} {'Intra-edges':<15} {'Prior-nodes':<15} {'Level-OK':<12} {'Group-OK':<12}")
    lines.append("-" * 80)

    for lvl in sorted(res["levels"]):
        # Handle both string and int keys
        lvl_key = str(lvl) if str(lvl) in res["per_level"] else lvl
        info = res["per_level"][lvl_key]
        level_ok = "✓" if info["ok_level_squash"] else "✗"
        group_ok = "✓" if info["ok_group_squash"] else "✗"
        lines.append(
            f"{lvl:<8} {len(info['level_nodes']):<8} {info['intra_edges_count']:<15} "
            f"{len(info['required_prior_nodes']):<15} {level_ok:<12} {group_ok:<12}"
        )

        if verbose and (not info["ok_level_squash"] or info["intra_edges_count"] > 0):
            lines.append(f"  Nodes: {', '.join([n[:7] for n in info['level_nodes']])}")
            if info["intra_edges"]:
                lines.append(f"  Intra-edges: {info['intra_edges'][:5]}{'...' if len(info['intra_edges']) > 5 else ''}")

    lines.append("=" * 80)
    return "\n".join(lines)


def _format_validate_summary(res: Dict, verbose: bool = False) -> str:
    """Format validate result in a human-readable way."""
    lines = []
    lines.append("=" * 80)
    lines.append("Validate Mode - Cherry-pick Test Results")
    lines.append("=" * 80)

    lines.append(f"Mode: {res.get('mode', 'N/A')}")
    lines.append(f"Target level: {res.get('level', 'N/A')}")
    lines.append(f"Base commit: {res.get('base', 'N/A')[:16] if res.get('base') else 'N/A'}...")
    lines.append(f"Base strategy: {res.get('base_strategy', 'N/A')}")
    lines.append("")

    lines.append(f"Prior commits (lower levels): {res.get('num_prior_commits', 0)}")
    lines.append(f"Target commits (this level): {res.get('num_target_commits', 0)}")
    lines.append(f"Randomize order: {res.get('randomize', False)}")
    lines.append(f"Number of tries: {res.get('tries', 0)}")
    lines.append("")

    # Overall results
    all_success = res.get("all_success", False)
    any_success = res.get("any_success", False)
    num_unique_trees = res.get("num_unique_trees", 0)
    deterministic = res.get("deterministic_tree", False)

    if all_success:
        lines.append("✓ Result: ALL ATTEMPTS SUCCEEDED")
    elif any_success:
        lines.append("⚠ Result: SOME ATTEMPTS SUCCEEDED")
    else:
        lines.append("✗ Result: ALL ATTEMPTS FAILED")

    lines.append(f"  Unique trees produced: {num_unique_trees}")
    lines.append(f"  Deterministic result: {'✓' if deterministic else '✗'}")
    lines.append("")

    # Attempt details
    attempts = res.get("attempts", [])
    if attempts:
        lines.append(f"Attempt details ({len(attempts)} total):")
        lines.append(f"{'Try':<6} {'Success':<10} {'Tree':<20} {'Failed At':<15}")
        lines.append("-" * 80)

        for att in attempts[: 10 if not verbose else None]:
            success_mark = "✓" if att.get("success") else "✗"
            tree = att.get("new_tree", "N/A")[:16] if att.get("new_tree") else "N/A"
            failed_at = att.get("failed_at", "N/A")
            if failed_at and len(str(failed_at)) > 12:
                failed_at = str(failed_at)[:7] + "..."

            lines.append(f"{att.get('try', 'N/A'):<6} {success_mark:<10} {tree:<20} {failed_at or '-':<15}")

            if verbose and not att.get("success") and att.get("conflict_files"):
                lines.append(f"       Conflict files: {', '.join(att['conflict_files'][:3])}")
                if len(att["conflict_files"]) > 3:
                    lines.append(f"       ... and {len(att['conflict_files']) - 3} more files")

        if len(attempts) > 10 and not verbose:
            lines.append(f"... and {len(attempts) - 10} more attempts (use --verbose to see all)")

    lines.append("=" * 80)

    if verbose and res.get("unique_trees"):
        lines.append("")
        lines.append("Unique tree SHAs:")
        for tree in res["unique_trees"]:
            lines.append(f"  {tree}")
        lines.append("=" * 80)

    return "\n".join(lines)


def _validate_all_levels(repo: Path, dag_json: Path, tries: int, randomize: bool) -> Dict:
    """Validate all levels by running cherry-pick tests on each level."""
    data = json.loads(dag_json.read_text())
    levels_map: Dict[str, int] = data["levels"]

    # Group commits by level
    lvl2commits: Dict[int, List[str]] = {}
    for commit, lvl in levels_map.items():
        lvl2commits.setdefault(lvl, []).append(commit)

    all_levels = sorted(lvl2commits.keys())
    results_by_level: Dict[int, Dict] = {}

    print(f"[INFO] Validating {len(all_levels)} levels...")

    for i, lvl in enumerate(all_levels):
        print(f"[INFO] Validating level {lvl} ({i + 1}/{len(all_levels)})...")
        result = _random_validate(repo, dag_json, level=lvl, tries=tries, randomize=randomize)
        results_by_level[lvl] = result

    # Compute summary statistics
    total_levels = len(all_levels)
    successful_levels = sum(1 for r in results_by_level.values() if r.get("all_success"))
    failed_levels = sum(1 for r in results_by_level.values() if not r.get("any_success"))
    partial_levels = total_levels - successful_levels - failed_levels

    return {
        "mode": "validate-all",
        "total_levels": total_levels,
        "successful_levels": successful_levels,
        "failed_levels": failed_levels,
        "partial_success_levels": partial_levels,
        "tries_per_level": tries,
        "randomize": randomize,
        "results_by_level": results_by_level,
    }


def _format_validate_all_summary(res: Dict, verbose: bool = False) -> str:
    """Format validate-all result in a human-readable way."""
    lines = []
    lines.append("=" * 80)
    lines.append("Validate All Levels - Batch Cherry-pick Test Results")
    lines.append("=" * 80)

    total = res.get("total_levels", 0)
    successful = res.get("successful_levels", 0)
    failed = res.get("failed_levels", 0)
    partial = res.get("partial_success_levels", 0)

    lines.append(f"Total levels tested: {total}")
    lines.append(f"Tries per level: {res.get('tries_per_level', 0)}")
    lines.append(f"Randomize order: {res.get('randomize', False)}")
    lines.append("")

    # Overall result
    if failed == 0:
        lines.append("✓ Result: ALL LEVELS PASSED")
    elif successful == 0:
        lines.append("✗ Result: ALL LEVELS FAILED")
    else:
        lines.append("⚠ Result: MIXED RESULTS")

    lines.append(f"  Successful levels: {successful}/{total}")
    lines.append(f"  Failed levels: {failed}/{total}")
    if partial > 0:
        lines.append(f"  Partial success levels: {partial}/{total}")
    lines.append("")

    # Level-by-level summary
    lines.append("Level-by-level results:")
    lines.append(f"{'Level':<8} {'Commits':<10} {'Success':<10} {'Deterministic':<15} {'Unique Trees':<15}")
    lines.append("-" * 80)

    results = res.get("results_by_level", {})
    for lvl in sorted(results.keys(), key=int):
        result = results[lvl]
        num_commits = result.get("num_target_commits", 0)
        all_success = result.get("all_success", False)
        deterministic = result.get("deterministic_tree", False)
        num_trees = result.get("num_unique_trees", 0)

        success_mark = "✓" if all_success else ("⚠" if result.get("any_success") else "✗")
        det_mark = "✓" if deterministic else "✗"

        lines.append(f"{lvl:<8} {num_commits:<10} {success_mark:<10} {det_mark:<15} {num_trees:<15}")

        if verbose and not all_success:
            # Show failed attempts
            attempts = result.get("attempts", [])
            failed_attempts = [a for a in attempts if not a.get("success")]
            if failed_attempts:
                lines.append(f"  Failed attempts: {len(failed_attempts)}/{len(attempts)}")
                for att in failed_attempts[:2]:
                    failed_at = att.get("failed_at", "N/A")
                    if failed_at and len(str(failed_at)) > 10:
                        failed_at = str(failed_at)[:7] + "..."
                    lines.append(f"    Try {att.get('try')}: failed at {failed_at}")
                    if att.get("conflict_files"):
                        files = ", ".join(att["conflict_files"][:2])
                        lines.append(f"      Conflicts: {files}")

    lines.append("=" * 80)

    # Summary by result type
    if verbose or not verbose:
        lines.append("")
        lines.append("Summary by result type:")

        if successful > 0:
            lines.append(f"  ✓ Fully successful levels ({successful}):")
            for lvl in sorted(results.keys(), key=int):
                if results[lvl].get("all_success"):
                    lines.append(f"    Level {lvl}: {results[lvl].get('num_target_commits')} commits")

        if failed > 0:
            lines.append(f"  ✗ Failed levels ({failed}):")
            for lvl in sorted(results.keys(), key=int):
                if not results[lvl].get("any_success"):
                    lines.append(f"    Level {lvl}: {results[lvl].get('num_target_commits')} commits")

        if partial > 0:
            lines.append(f"  ⚠ Partial success levels ({partial}):")
            for lvl in sorted(results.keys(), key=int):
                result = results[lvl]
                if result.get("any_success") and not result.get("all_success"):
                    attempts = result.get("attempts", [])
                    succeeded = sum(1 for a in attempts if a.get("success"))
                    lines.append(f"    Level {lvl}: {succeeded}/{len(attempts)} attempts succeeded")

        lines.append("=" * 80)

    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="逐层校验 DAG 的 squash 可行性；或做随机顺序验证。")
    p.add_argument("--dag", required=True, help="commit_dag.py 生成的 JSON 文件")
    p.add_argument("--repo", default=None, help="Git 仓库路径（用于随机验证）")
    p.add_argument("--level", type=int, default=None, help="仅分析/验证指定层（validate 模式下可用）")
    p.add_argument("--edge-kinds", default="line,file", help="作为依赖的边类型，逗号分隔（默认: line,file）")
    p.add_argument(
        "--mode",
        choices=["dag", "validate", "validate-all"],
        default="dag",
        help=(
            "dag: 仅基于 JSON 逐层校验；"
            "validate: 真实在共同基线上按顺序 cherry-pick 验证并提交；"
            "validate-all: 对所有层批量执行 validate 检查"
        ),
    )
    p.add_argument("--randomize", action="store_true", help="validate 模式下：同层提交顺序随机打乱")
    p.add_argument("--tries", type=int, default=3, help="validate 模式下：随机尝试次数（默认 3）")
    p.add_argument("--out", default=None, help="输出 JSON 路径（不指定则打印）")
    p.add_argument("--verbose", "-v", action="store_true", help="显示详细的 commit SHA 列表")
    p.add_argument("--summary-only", action="store_true", help="仅显示汇总信息，不显示每层详情")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    dag = Path(args.dag)
    kinds = [k.strip() for k in args.edge_kinds.split(",") if k.strip()]

    if args.mode == "dag":
        res = _validate_levels_dag(dag, kinds)
    else:
        if not args.repo:
            raise SystemExit("--repo is required in 'validate'/'validate-all' mode")
        if args.mode == "validate":
            # validate mode: actually try to cherry-pick same-level commits (random or fixed order)
            res = _random_validate(
                repo=Path(args.repo), dag_json=dag, level=args.level, tries=args.tries, randomize=args.randomize
            )
        else:  # validate-all
            # validate-all mode: validate all levels
            res = _validate_all_levels(repo=Path(args.repo), dag_json=dag, tries=args.tries, randomize=args.randomize)

    # Select formatter based on mode
    if args.mode == "dag":
        formatter = _format_dag_summary
    elif args.mode == "validate":
        formatter = _format_validate_summary
    else:  # validate-all
        formatter = _format_validate_all_summary

    # Output handling
    if args.out:
        # Always write full JSON to file
        text = json.dumps(res, indent=2)
        Path(args.out).write_text(text)
        print(f"Wrote full report to {args.out}")

        # Also print formatted summary to stdout
        if not args.summary_only:
            print("\n" + formatter(res, verbose=args.verbose))
        else:
            # Extract just the summary part
            full_output = formatter(res, verbose=False)
            if args.mode == "dag":
                summary_part = full_output.split("Level-by-level statistics:")[0]
            elif args.mode == "validate":
                summary_part = full_output.split("Attempt details")[0]
            else:  # validate-all
                summary_part = full_output.split("Level-by-level results:")[0]
            print("\n" + summary_part)
    else:
        # No output file specified - show formatted output by default
        if args.summary_only:
            # Show only summary statistics
            full_output = formatter(res, verbose=False)
            if args.mode == "dag":
                summary_part = full_output.split("Level-by-level statistics:")[0]
            elif args.mode == "validate":
                summary_part = full_output.split("Attempt details")[0]
            else:  # validate-all
                summary_part = full_output.split("Level-by-level results:")[0]
            print(summary_part)
        else:
            # Show full formatted output
            print(formatter(res, verbose=args.verbose))

        # Hint about saving to file
        if args.verbose:
            print("\n[Use --out <file> to save full JSON report]")


if __name__ == "__main__":
    main()
