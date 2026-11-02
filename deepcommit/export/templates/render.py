#!/usr/bin/env python3
"""Regenerate the task's steps/ folder from the authoring sources next to this script.

Sources (all under this folder):
  steps.json                     ordered steps: name, F2P/P2P env (test files per repo), repos with a gold patch
  suites.json                    suites (run command, parser, test dirs), contract-only repos, services
  instructions/_context.md       shared environment context appended to every step
  instructions/<step>.md         the milestone spec the agent sees
  gold/<step>/<repo>.patch       the milestone's source change on top of the previous milestone's end state
  hidden/<step>/<repo>/<path>    test files the milestone changed, at the milestone's end state
  runner/test.sh, runner/grade.py

Output (the task root, ``task_dir_rel`` of steps.json), per step:
  instruction.md                 spec + context
  solution/gold.patch            the repos' gold patches in one patch rooted at /app (paths <repo>/...)
  solution/solve.sh              git apply gold.patch, then harbor-svc deploy --all when the task has app services
  tests/test.sh, tests/grade.py  the runner (the same in every step)
  tests/suite.json               step name, suites, services, files to run, own test files (``checks`` come from curation)
  tests/checks/<repo>/<path>     cumulative: every test file steps 1..k changed, so earlier steps' tests run as p2p

  python3 render.py [--task DIR]
"""

import argparse
import json
import re
import shutil
from pathlib import Path

SOLVE = """#!/bin/bash
# Reference solution: apply this step's gold patch (paths <repo>/..., rooted at /app){deploy_note}.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cd /app
git apply --whitespace=nowarn "$here/gold.patch"
"""

_HEADER = re.compile(rb"^(diff --git a/)(\S+)( b/)(\S+)$|^(--- a/|\+\+\+ b/|rename from |rename to |copy from |copy to )(.*)$")


def prefix_patch(repo, patch):
    """Rewrite one repo's git patch so its paths are ``<repo>/...`` (a patch applied from /app)."""
    out = []
    for line in patch.splitlines(keepends=True):
        body = line.rstrip(b"\n")
        m = _HEADER.match(body)
        if m and m.group(1):
            r = repo.encode()
            line = m.group(1) + r + b"/" + m.group(2) + m.group(3) + r + b"/" + m.group(4) + b"\n"
        elif m and m.group(5):
            line = m.group(5) + repo.encode() + b"/" + m.group(6) + b"\n"
        out.append(line)
    return b"".join(out)


def gold_patch(patches):
    """``{repo: patch bytes}`` (repo-relative paths) -> one patch rooted at /app."""
    return b"".join(prefix_patch(repo, p if p.endswith(b"\n") else p + b"\n") for repo, p in patches.items() if p)


def solve_script(app_services):
    note = ", then redeploy every app service so the stack matches /app" if app_services else ""
    text = SOLVE.format(deploy_note=note)
    return text + ("harbor-svc deploy --all\n" if app_services else "")


def env_files(env, repos):
    """F2P_<REPO> / P2P_EARLIER_<REPO> lists of steps.json -> {repo: {"f2p": [...], "p2p": [...]}}."""
    out = {}
    for repo in repos:
        key = re.sub(r"[^A-Za-z0-9]", "_", repo).upper()
        out[repo] = {
            "f2p": env.get(f"F2P_{key}", "").split(),
            "p2p": env.get(f"P2P_EARLIER_{key}", "").split(),
        }
    return out


def copy_tree(src, dst):
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        target = dst / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target)


def render(src, task):
    meta = json.loads((src / "steps.json").read_text(encoding="utf-8"))
    suites = json.loads((src / "suites.json").read_text(encoding="utf-8"))
    context = (src / "instructions" / "_context.md").read_text(encoding="utf-8")
    repos = [s["repo"] for s in suites["suites"]]
    app_services = any(sv.get("kind") == "app" for sv in suites.get("services", []))
    shutil.rmtree(task / "steps", ignore_errors=True)
    for i, step in enumerate(meta["steps"]):
        name = step["name"]
        out = task / "steps" / name
        (out / "solution").mkdir(parents=True)
        (out / "tests").mkdir(parents=True)
        spec = (src / "instructions" / f"{name}.md").read_text(encoding="utf-8")
        (out / "instruction.md").write_text(spec.rstrip("\n") + "\n\n---\n\n" + context, encoding="utf-8")
        patches = {repo: (src / "gold" / name / f"{repo}.patch").read_bytes() for repo in step["gold_repos"]}
        (out / "solution" / "gold.patch").write_bytes(gold_patch(patches))
        solve = out / "solution" / "solve.sh"
        solve.write_text(solve_script(app_services), encoding="utf-8")
        solve.chmod(0o755)
        files = env_files(step["env"], repos)
        suite = {
            "step": name,
            "index": i + 1,
            "suites": suites["suites"],
            "contract_only": suites.get("contract_only", {}),
            "services": suites.get("services", []),
            "files": files,
            "own_files": {r: f["f2p"] for r, f in files.items() if f["f2p"]},
        }
        (out / "tests" / "suite.json").write_text(json.dumps(suite, indent=1) + "\n", encoding="utf-8")
        for runner in ("test.sh", "grade.py"):
            shutil.copyfile(src / "runner" / runner, out / "tests" / runner)
        (out / "tests" / "test.sh").chmod(0o755)
        for earlier in meta["steps"][: i + 1]:
            hidden = src / "hidden" / earlier["name"]
            if hidden.is_dir():
                copy_tree(hidden, out / "tests" / "checks")


def main():
    here = Path(__file__).resolve().parent
    meta = json.loads((here / "steps.json").read_text(encoding="utf-8"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", type=Path, default=(here / meta["task_dir_rel"]).resolve())
    args = ap.parse_args()
    render(here, args.task)
    print(f"rendered {len(meta['steps'])} steps into {args.task / 'steps'}")


if __name__ == "__main__":
    main()
