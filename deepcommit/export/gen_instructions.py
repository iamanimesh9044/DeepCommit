"""Write the agent-facing instruction.md of every step of an exported CSF task with the ``claude`` CLI.

The exporter's first instruction is the milestone summary: it names commits and implementation details and can omit what
the hidden tests need. This pass reads, per step, the gold patch, the hidden F2P tests and the milestone summary, and
writes a behaviour-level specification: what must be true afterwards, every public name/signature/message field the
tests rely on, nothing about how the reference solution is laid out.

  python -m deepcommit.export.gen_instructions TASK_DIR [--jobs 4] [--model sonnet] [--only m3,m4]

The environment section (shared context) is appended unchanged below a ``---`` rule, as the exporter writes it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import tomllib

PROMPT = """You are writing the task statement for one step of a multi-step software-engineering benchmark. An AI coding agent
will receive ONLY your text plus a shell in the repositories; hidden tests then grade its work.

Read these files (they are the ground truth; the agent never sees them):
- Milestone summary written by the pipeline (may name commits, may be incomplete): {summary_file}
- Reference solution (diff per repository): {gold_files}
- Hidden tests that must go from failing to passing (ids): {f2p_file}
- Hidden test sources: {test_files}
- Titles of the earlier steps (already done in the agent's checkout): {earlier}

Write the task statement in Markdown, starting with a `# <title>` line. Rules:
1. Describe the required behaviour, as a feature request or change ticket: what must be true afterwards, observable from
   the public API, the wire contract, the CLI, or the files named below.
2. The agent must be able to pass the hidden tests from your text alone. For every public name, signature, argument,
   default, return value, error type, message field, enum value or file the tests import, call or read, state it exactly.
   If a test checks a number or a string, state it.
3. Do NOT mention commit hashes, PR numbers, the reference solution's internal helper names, or how the reference solution
   is organised. Do not paste code from the diff beyond signatures and contract definitions.
4. Name each repository that must change and why (`/app/<repo>`); when a change in the contract repository must be
   mirrored in a client, say what must stay consistent between them.
5. Generated code (protobuf stubs, schemas) must be regenerated from its source with the repository's own tooling, not edited
   by hand; say which tool when the diff shows it.
6. No sections about environment, tests-to-run or grading; those are appended separately. 200-600 words, tight prose and
   short lists. No preamble, no closing remarks. Output ONLY the Markdown."""


def _step_info(task: Path, step: dict):
    sd = task / "steps" / step["name"]
    ids = sd / "tests/ids.json"
    return sd, ids


def build_prompt(task: Path, scratch: Path, names: list[str], i: int) -> str:
    name = names[i]
    sd = task / "steps" / name
    summary = scratch / f"{name}.summary.md"
    orig = scratch / f"{name}.orig.md"
    old = orig.read_text() if orig.exists() else (sd / "instruction.md").read_text()
    summary.write_text(old.split("\n---\n")[0])
    f2p = scratch / f"{name}.f2p.json"
    ids = sd / "tests/ids.json"
    f2p.write_text(json.dumps(json.loads(ids.read_text())["f2p"], indent=1) if ids.exists() else "[]")
    gold = " ".join(str(p) for p in sorted((sd / "solution/gold").glob("*.patch")))
    hidden = sd / "tests/hidden"
    tests = (
        " ".join(str(p) for p in sorted(hidden.rglob("*")) if p.is_file() and p.suffix in (".py", ".rs"))
        if hidden.is_dir()
        else "none"
    )
    return PROMPT.format(
        summary_file=summary,
        gold_files=gold,
        f2p_file=f2p,
        test_files=tests[:6000],
        earlier=", ".join(names[:i]) or "none",
    )


def run_one(task: Path, scratch: Path, names: list[str], i: int, model: str) -> tuple[str, bool, str]:
    name = names[i]
    prompt = build_prompt(task, scratch, names, i)
    p = subprocess.run(
        [
            "claude",
            "-p",
            prompt,
            "--model",
            model,
            "--allowedTools",
            "Read,Grep,Glob",
            "--add-dir",
            str(task),
            "--add-dir",
            str(scratch),
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        cwd=str(scratch),
    )
    text = p.stdout.strip()
    if p.returncode or not text.startswith("#"):
        return name, False, (p.stderr or text)[:500]
    sd = task / "steps" / name
    old = (sd / "instruction.md").read_text()
    context = old.split("\n---\n", 1)[1] if "\n---\n" in old else ""
    if not (scratch / f"{name}.orig.md").exists():
        (scratch / f"{name}.orig.md").write_text(old)
    (sd / "instruction.md").write_text(text.rstrip("\n") + "\n\n---\n" + context)
    return name, True, ""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--only", default="")
    ap.add_argument("--scratch", default="")
    a = ap.parse_args(argv)
    task = Path(a.task).resolve()
    names = [s["name"] for s in tomllib.loads((task / "task.toml").read_text())["steps"]]
    only = {s for s in a.only.split(",") if s}
    scratch = Path(a.scratch or (task.parent.parent / "_src" / task.name / "instructions_work")).resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    todo = [i for i, n in enumerate(names) if not only or n.split("-")[0] in only]
    with ThreadPoolExecutor(a.jobs) as ex:
        for name, ok, err in ex.map(lambda i: run_one(task, scratch, names, i, a.model), todo):
            print(("ok  " if ok else "FAIL"), name, err, flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
