"""Turn an exported CSF multi-repo task into a graded one: measure, curate, fold, write instructions and README.

  deepcommit workspace finalize-task TASK_DIR [--src DIR] [--image TAG] [--runs 2] [--skip-build] [--no-instructions]

The exporter writes gold patches and hidden test files; this step finds out, in the task's own stack (main plus its
services, deployed as Harbor deploys them), which tests
really fail before and pass after each gold patch (``templates/measure.py``), keeps those as the F2P/P2P ids
(``curate``), folds steps that cannot be graded alone into their successor (``fold``), rewrites the instructions
(``gen_instructions``), the milestone cards and ledger.json (``svc_layout``) and the README (``readme``). The gates (oracle, NOP) stay a separate run:
``harbor run -p TASK_DIR -a oracle`` / ``-a nop`` with Harbor 0.25 or newer.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from . import curate as curate_mod
from . import fold as fold_mod
from . import gen_instructions, readme, svc_layout

TEMPLATES = Path(__file__).parent / "templates"


def _run(cmd: list, **kw) -> None:
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def app_services_overlay(task: Path, tag: str) -> tuple[dict, list[tuple[str, Path]]]:
    """Compose services for the task's app services, as Harbor writes them from their manifests (its app-service
    template: read-only root, tmpfs /tmp and /build, non-root, no capabilities, code volume read-only, appnet only), and
    the runtime images to build first: [(image tag, build context)]."""
    import tomllib

    cfg = tomllib.loads((task / "task.toml").read_text())
    sc = cfg.get("environment", {}).get("service_control", {}) or {}
    catalog = sc.get("runtimes", {}) or {}
    services, builds = {}, {}
    for manifest in sorted((task / "environment" / "app").glob("*/csf-service.toml")):
        name = manifest.parent.name
        m = tomllib.loads(manifest.read_text())
        rt = m["runtime"]
        image = f"{tag}-rt-{rt}".lower()
        builds[image] = task / "environment" / catalog[rt]
        env = {"CSF_SERVICE": name, "CSF_COMMAND": m["command"]}
        if m.get("build"):
            env["CSF_BUILD"] = m["build"]
        if m.get("port"):
            env["CSF_PORT"] = str(m["port"])
        if m.get("health"):
            env["CSF_HEALTH"] = m["health"]
        env.update({k: str(v) for k, v in (m.get("env") or {}).items()})
        svc = {
            "image": image, "pull_policy": "never", "read_only": True,
            "tmpfs": ["/tmp:exec,mode=1777", "/build:exec,mode=1777"], "user": "10001:10001",
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "environment": env,
            "volumes": ["app-src:/app:ro"], "networks": ["appnet"],
            "healthcheck": {"test": ["CMD", "csf-health"], "interval": "2s", "timeout": "5s", "retries": 60},
        }  # fmt: skip
        if sc.get("app_cpus"):
            svc["cpus"] = float(sc["app_cpus"])
        if sc.get("app_memory_mb"):
            svc["mem_limit"] = f"{int(sc['app_memory_mb'])}m"
        if m.get("depends_on"):
            svc["depends_on"] = {d: {"condition": "service_healthy"} for d in m["depends_on"]}
        services[name] = svc
    return services, sorted(builds.items())


def measure(task: Path, image: str, out: Path, runs: int = 2, skip_build: bool = False) -> dict:
    """Start the task's stack (environment/docker-compose.yaml + its app services) and run measure.py in main over the
    task folder (mounted read-only)."""
    import yaml

    project = re.sub(r"[^a-z0-9-]", "-", f"csf-measure-{task.name}".lower())
    services, builds = app_services_overlay(task, project)
    if not skip_build:
        for tag, context in builds:
            _run(["docker", "build", "-q", "-t", tag, context])
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(TEMPLATES / "measure.py", Path(tmp) / "measure.py")
        overlay = {
            "services": {
                "main": {"image": image, "volumes": [f"{task.resolve()}:/task:ro", f"{Path(tmp).resolve()}:/out"]},
                **services,
            }
        }
        (Path(tmp) / "overlay.yaml").write_text(yaml.safe_dump(overlay, sort_keys=False))
        compose = [
            "docker", "compose", "-p", project, "--project-directory", task / "environment",
            "-f", task / "environment" / "docker-compose.yaml", "-f", Path(tmp) / "overlay.yaml",
        ]  # fmt: skip
        try:
            _run([*compose, "up", "-d", "--wait", *([] if skip_build else ["--build"])])
            _run([*compose, "exec", "-T", "main", "python3", "/out/measure.py", "--task", "/task",
                  "--out", "/out/measure.json", "--runs", str(runs)])  # fmt: skip
            data = json.loads((Path(tmp) / "measure.json").read_text())
        finally:
            subprocess.run([str(c) for c in [*compose, "down", "-v", "--remove-orphans"]], capture_output=True)
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
    report = src / "export_report.json"
    mid_of = (
        {s["name"]: s["mid"] for s in json.loads(report.read_text())["steps"] if s.get("name")}
        if report.exists()
        else {}
    )
    milestones = {r["step"]: [mid_of.get(n, n) for n in r.get("members", [r["step"]])] for r in merged["steps"]}
    svc_layout.rewrite_cards(task, svc_layout.step_cards(task, milestones))
    svc_layout.write_ledger(task)
    if instructions:
        gen_instructions.main([str(task), "--model", model])
    if report.exists():
        readme.main([str(task), "--report", str(report), "--folded", str(src / "measure_folded.json")])
    write_base_compose(task, image)
    return final


def write_base_compose(task: Path, image: str) -> None:
    """``compose.base.yaml``: build the task's base state and open a shell in it, without Harbor (for people reading the task)."""
    (task / "compose.base.yaml").write_text(
        "# The base state every trial starts from: repositories at their pinned commits (vendored in environment/vendor,\n"
        "# background commits applied), dependencies installed. Not used by Harbor.\n"
        "#   docker compose -f compose.base.yaml run --rm base\n"
        "services:\n"
        "  base:\n"
        "    build:\n"
        "      context: environment\n"
        f"    image: {image}\n"
        "    working_dir: /app\n"
        "    command: bash\n"
        "    stdin_open: true\n"
        "    tty: true\n",
        encoding="utf-8",
    )
