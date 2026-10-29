#!/usr/bin/env python3
"""Regenerate the task's steps/ folder from the authoring sources next to this script.

Sources (all under this folder):
  steps.json                     ordered steps: name, F2P/P2P env, repos with a gold patch
  instructions/_context.md       shared environment context appended to every step
  instructions/<step>.md         the milestone spec the agent sees
  gold/<step>/<repo>.patch       the milestone's source change on top of the previous milestone's end state
  hidden/<step>/<repo>/<path>    test files the milestone changed, at the milestone's end state

Output (the task root, ``task_dir_rel`` of steps.json): steps/<step>/{instruction.md, solution/solve.sh,
solution/gold/<repo>.patch, tests/step.env, tests/hidden/<repo>/...}. Step k's hidden tree is cumulative: every test file
steps 1..k changed, so earlier steps' tests can run as pass-to-pass.

  python3 render.py [--task DIR]
"""

import argparse
import json
import shutil
from pathlib import Path

SOLVE = """#!/bin/bash
# Reference solution: apply this milestone's gold patch to every repo it touches.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
"""


def copy_tree(src, dst):
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        target = dst / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target)


def render(src, task):
    meta = json.loads((src / "steps.json").read_text(encoding="utf-8"))
    context = (src / "instructions" / "_context.md").read_text(encoding="utf-8")
    shutil.rmtree(task / "steps", ignore_errors=True)
    for i, step in enumerate(meta["steps"]):
        name = step["name"]
        out = task / "steps" / name
        (out / "solution" / "gold").mkdir(parents=True)
        (out / "tests").mkdir(parents=True)
        spec = (src / "instructions" / f"{name}.md").read_text(encoding="utf-8")
        (out / "instruction.md").write_text(spec.rstrip("\n") + "\n\n---\n\n" + context, encoding="utf-8")
        solve = out / "solution" / "solve.sh"
        applies = [
            f'git -C /app/{repo} apply --whitespace=nowarn "$here/gold/{repo}.patch"' for repo in step["gold_repos"]
        ]
        solve.write_text(SOLVE + "\n".join(applies) + "\n", encoding="utf-8")
        solve.chmod(0o755)
        for repo in step["gold_repos"]:
            shutil.copyfile(src / "gold" / name / f"{repo}.patch", out / "solution" / "gold" / f"{repo}.patch")
        lines = [f"STEP={name}"] + [f"{k}='{v}'" for k, v in sorted(step["env"].items())]
        (out / "tests" / "step.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
        for earlier in meta["steps"][: i + 1]:
            hidden = src / "hidden" / earlier["name"]
            if hidden.is_dir():
                copy_tree(hidden, out / "tests" / "hidden")


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
