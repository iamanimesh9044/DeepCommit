"""Fold steps that cannot be graded on their own into their successor.

A step is gradable only if some hidden test goes from failing to passing across its gold patch. A milestone that leaves
the checkout unimportable until a later milestone lands (generated stubs, a pending dependency bump) has no such test:
its change and tests belong in the next step. ``plan`` finds those steps from the curated measurement, ``apply`` rewrites
the task folder: gold patches are concatenated per repository (``git apply`` applies them in order), the hidden tree is
the last member's (it is cumulative), steps are renumbered, task.toml and the DAG follow.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Dict, List

from .curate import StepCuration

SOLVE = """#!/bin/bash
# Reference solution: apply this step's gold patch to every repo it touches.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
"""


def plan(curated: List[StepCuration]) -> List[List[str]]:
    """Groups of consecutive step names; every group but possibly a trailing one ends in a gradable step."""
    groups: List[List[str]] = []
    cur: List[str] = []
    for c in curated:
        cur.append(c.step)
        if c.usable:
            groups.append(cur)
            cur = []
    if cur:  # trailing ungradable steps join the last group
        if groups:
            groups[-1].extend(cur)
        else:
            groups.append(cur)
    return groups


def _slug(name: str) -> str:
    return re.sub(r"^m\d+-", "", name)


def merge_measure(measure: dict, groups: List[List[str]]) -> dict:
    by = {r["step"]: r for r in measure["steps"]}
    out = []
    for g in groups:
        pre: Dict[str, str] = {}
        for n in g:
            for k, v in by[n]["pre"].items():
                if pre.get(k) != "failed":  # keep the strictest (a failure at any member's pre-state)
                    pre[k] = v
        out.append({"step": g[0], "pre": pre, "post": by[g[-1]]["post"], "members": g})
    return {"steps": out}


def apply(task: Path, groups: List[List[str]]) -> Dict[str, str]:
    """Rewrite steps/ and task.toml for ``groups``; returns old step name -> new step name."""
    steps = task / "steps"
    stage = task / ".steps_new"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    rename: Dict[str, str] = {}
    for i, g in enumerate(groups, 1):
        new = f"m{i}-{_slug(g[0])}"
        for n in g:
            rename[n] = new
        out = stage / new
        last = steps / g[-1]
        shutil.copytree(last, out)
        shutil.rmtree(out / "solution/gold")
        (out / "solution/gold").mkdir(parents=True)
        repos = sorted({p.stem for n in g for p in (steps / n / "solution/gold").glob("*.patch")})
        for repo in repos:
            parts = [
                (steps / n / "solution/gold" / f"{repo}.patch").read_text()
                for n in g
                if (steps / n / "solution/gold" / f"{repo}.patch").exists()
            ]
            (out / "solution/gold" / f"{repo}.patch").write_text(
                "".join(p if p.endswith("\n") else p + "\n" for p in parts)
            )
        solve = out / "solution/solve.sh"
        solve.write_text(
            SOLVE + "\n".join(f'git -C /app/{r} apply --whitespace=nowarn "$here/gold/{r}.patch"' for r in repos) + "\n"
        )
        solve.chmod(0o755)
        # own test files: union over members
        own: Dict[str, List[str]] = {}
        for n in g:
            for line in (steps / n / "tests/step.env").read_text().splitlines():
                m = re.match(r"F2P_([A-Z_]+)='(.*)'", line)
                if m:
                    for f in m.group(2).split():
                        if f not in own.setdefault(m.group(1), []):
                            own[m.group(1)].append(f)
        env = [f"STEP={new}"] + [f"F2P_{k}='{' '.join(v)}'" for k, v in sorted(own.items())]
        env += [f"P2P_EARLIER_{k}=''" for k in sorted(own)]
        (out / "tests/step.env").write_text("\n".join(env) + "\n")
        if len(g) > 1:  # instruction: the members' specs in order (gen_instructions rewrites it)
            specs = []
            for n in g:
                txt = (steps / n / "instruction.md").read_text()
                specs.append(txt.split("\n---\n")[0].strip())
            ctx = (steps / g[-1] / "instruction.md").read_text().split("\n---\n", 1)[1]
            (out / "instruction.md").write_text("\n\n".join(specs) + "\n\n---\n" + ctx)
    shutil.rmtree(steps)
    stage.rename(steps)
    _rewrite_toml(task, groups, rename)
    return rename


def _rewrite_toml(task: Path, groups: List[List[str]], rename: Dict[str, str]) -> None:
    p = task / "task.toml"
    text = p.read_text()
    head = text[: text.index("[[steps]]")]
    new_names = [rename[g[0]] for g in groups]
    # DAG: old "mK" tokens -> new "mJ"; drop edges inside a group, dedupe
    old_id = {n.split("-")[0]: rename[n].split("-")[0] for n in rename}
    m = re.search(r'csf_milestone_dag = "([^"]*)"', head)
    if m:
        edges, seen = [], set()
        for part in m.group(1).split(";"):
            part = part.strip()
            if not part:
                continue
            pre, _, post = part.partition("->")
            a = sorted({old_id.get(t.strip(), t.strip()) for t in pre.split("+")})
            b = old_id.get(post.strip(), post.strip()) if post else ""
            if b and b in a:
                a = [x for x in a if x != b]
            key = (tuple(a), b)
            if (not a and b) or key in seen:
                continue
            seen.add(key)
            edges.append(" + ".join(a) + (f" -> {b}" if b else ""))
        referenced = {t for e in edges for t in re.findall(r"m\d+", e)}
        edges += [n.split("-")[0] for n in new_names if n.split("-")[0] not in referenced]
        head = head.replace(m.group(0), f'csf_milestone_dag = "{"; ".join(edges)}"')
    head = re.sub(r"milestones_built = \d+", f"milestones_built = {sum(1 for _ in new_names)}", head)
    p.write_text(head + "\n".join(f'[[steps]]\nname = "{n}"\n' for n in new_names))
