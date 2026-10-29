"""Turn an exported CSF multi-repo task into a graded one: measure, curate, fold, write instructions and README.

  deepcommit workspace finalize-task TASK_DIR [--src DIR] [--image TAG] [--runs 2] [--skip-build] [--no-instructions]

The exporter writes gold patches and hidden test files; this step finds out, in the task's own image, which tests
really fail before and pass after each gold patch (``templates/measure.py``), keeps those as the F2P/P2P ids
(``curate``), folds steps that cannot be graded alone into their successor (``fold``), rewrites the instructions
(``gen_instructions``) and the README (``readme``). The gates (oracle, NOP) stay a separate run:
``harbor run -p TASK_DIR -a oracle`` / ``-a nop`` with Harbor 0.25 or newer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from . import curate as curate_mod
from . import fold as fold_mod
from . import gen_instructions, readme

TEMPLATES = Path(__file__).parent / "templates"


def _run(cmd: list, **kw) -> None:
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def measure(task: Path, image: str, out: Path, runs: int = 2, skip_build: bool = False) -> dict:
    """Build the task image and run measure.py inside it over the task folder (mounted read-only)."""
    if not skip_build:
        _run(["docker", "build", "-q", "-t", image, task / "environment"])
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(TEMPLATES / "measure.py", Path(tmp) / "measure.py")
        _run(
            [
                "docker", "run", "--rm",
                "-v", f"{task.resolve()}:/task:ro",
                "-v", f"{Path(tmp).resolve()}:/out",
                image, "python3", "/out/measure.py", "--task", "/task", "--out", "/out/measure.json", "--runs", str(runs),
            ]
        )  # fmt: skip
        data = json.loads((Path(tmp) / "measure.json").read_text())
    out.write_text(json.dumps(data))
    return data


def curate_and_fold(task: Path, measured: dict) -> tuple[list, dict]:
    """Fold steps without a gradable F2P test into their successor and write ids.json / step.env for every step."""
    first = curate_mod.curate(measured, task)
    groups = fold_mod.plan(first)
    merged = fold_mod.merge_measure(measured, groups)
    rename = fold_mod.apply(task, groups)
    for r in merged["steps"]:
        r["step"] = rename[r["step"]]
    final = curate_mod.curate(merged, task)
    curate_mod.write(task, final)
    return final, merged


def finalize(
    task: Path,
    src: Optional[Path] = None,
    image: str = "csf-task-base",
    runs: int = 2,
    skip_build: bool = False,
    instructions: bool = True,
    model: str = "sonnet",
) -> list:
    task = task.resolve()
    src = (src or task.parent.parent / "_src" / task.name).resolve()
    src.mkdir(parents=True, exist_ok=True)
    backup = src / "task_pre_finalize"
    if not backup.exists():
        shutil.copytree(task, backup)
    measured = measure(task, image, src / "measure.json", runs=runs, skip_build=skip_build)
    final, merged = curate_and_fold(task, measured)
    (src / "measure_folded.json").write_text(json.dumps(merged))
    if instructions:
        gen_instructions.main([str(task), "--model", model])
    report = src / "export_report.json"
    if report.exists():
        readme.main([str(task), "--report", str(report), "--folded", str(src / "measure_folded.json")])
    return final
