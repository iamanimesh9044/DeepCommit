"""Measure the real test outcomes of a CSF multi-repo task inside its own image (Phase 3 for exported tasks).

Runs in a container built from ``environment/`` with the task folder mounted at /task. For every step, in task.toml
order, on the sequential state (base + gold of steps 1..k-1):

  pre   the step's hidden test files are copied in and run on the state BEFORE the step's gold patch
  post  the gold patch is applied and the full suites run

Per-test outcomes land in ``--out`` as JSON: {"steps": [{"step", "pre": {id: status}, "post": {id: status}}]}.
``curate`` (host side, deepcommit.export.curate) turns those into F2P / P2P id lists.

  python3 measure.py --task /task --out /out/measure.json [--only m4,m5] [--runs 2]

Test ids are ``<repo>::<id>`` exactly as tests/grade.py names them, so a measured id is a gradable id.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import tomllib

PYTEST = re.compile(r"^(PASSED|FAILED|ERROR)\s+(\S+)")
CARGO_RUN = re.compile(r"^\s*Running (?:unittests )?(\S+)")
CARGO = re.compile(r"^test (\S+) \.\.\. (ok|FAILED)")


def parse(parser, text):
    out = {}
    if parser == "pytest":
        for line in text.splitlines():
            m = PYTEST.match(line)
            if m:
                out[m.group(2)] = "passed" if m.group(1) == "PASSED" else "failed"
    elif parser == "cargo":
        binary = ""
        for line in text.splitlines():
            r = CARGO_RUN.match(line)
            if r:
                binary = r.group(1).split("/")[-1].removesuffix(".rs")
                continue
            m = CARGO.match(line)
            if m:
                out[f"{binary}::{m.group(1)}" if binary else m.group(1)] = "passed" if m.group(2) == "ok" else "failed"
    return out


def sh(cmd, cwd=None, timeout=None, env=None):
    p = subprocess.run(
        ["bash", "-c", cmd],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    return p.returncode, p.stdout


def run_suites(suites, tests_of, timeout):
    """Run every suite once. ``tests_of(repo)`` gives the files to run, [] to skip the suite, None for the whole suite."""
    res = {}
    for s in suites:
        repo = s["repo"]
        files = tests_of(repo)
        if files == []:
            continue
        cmd = s["run_cmd"].replace("{tests}", " ".join(files) if files else s.get("all_tests", "."))
        t0 = time.time()
        try:
            rc, out = sh(cmd, cwd=f"/app/{repo}/{s.get('workdir', '.')}", timeout=timeout)
        except subprocess.TimeoutExpired as e:
            rc, out = 124, (e.stdout or "") if isinstance(e.stdout, str) else ""
        parsed = parse(s.get("parser", "pytest"), out)
        print(f"  {repo}: rc={rc} {len(parsed)} tests in {time.time() - t0:.0f}s", flush=True)
        for tid, st in parsed.items():
            res[f"{repo}::{tid}"] = st
    return res


def start_server():
    """A real Qdrant for the clients' remote-mode tests, on a throwaway data dir."""
    shutil.rmtree("/tmp/qd", ignore_errors=True)
    os.makedirs("/tmp/qd", exist_ok=True)
    subprocess.Popen(
        ["qdrant", "--disable-telemetry"],
        cwd="/tmp/qd",
        env={**os.environ, "QDRANT__STORAGE__STORAGE_PATH": "/tmp/qd/storage", "QDRANT__LOG_LEVEL": "WARN"},
        stdout=open("/tmp/qd/log", "w"),
        stderr=subprocess.STDOUT,
    )
    for _ in range(60):
        rc, _ = sh("curl -fs localhost:6333/healthz")
        if rc == 0:
            return
        time.sleep(1)
    raise SystemExit("qdrant server did not start")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="/task")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--timeout", type=int, default=3000)
    ap.add_argument("--no-server", action="store_true")
    ap.add_argument("--runs", type=int, default=2, help="post runs per step; a test must pass in all of them")
    a = ap.parse_args()
    task = Path(a.task)
    cfg = tomllib.loads((task / "task.toml").read_text())
    steps = [s["name"] for s in cfg["steps"]]
    suites = json.loads((task / "tests/suites.json").read_text())["suites"]
    only = {s for s in a.only.split(",") if s}
    if not a.no_server:
        start_server()
    out = {"steps": []}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    for name in steps:
        sd = task / "steps" / name
        hidden = sd / "tests/hidden"
        measured = not only or name.split("-")[0] in only or name in only
        print(f"== {name}", flush=True)
        if measured and hidden.is_dir():
            for repo_dir in hidden.iterdir():
                shutil.copytree(repo_dir, f"/app/{repo_dir.name}", dirs_exist_ok=True)
        files = {}
        for line in (sd / "tests/step.env").read_text().splitlines():
            m = re.match(r"(F2P_[A-Z_]+)='(.*)'", line)
            if m:
                files[m.group(1)[4:].lower().replace("_", "-")] = m.group(2).split()
        pre = {}
        if measured:
            print(" pre (step's own test files, before gold)", flush=True)
            pre = run_suites(suites, lambda r, f=files: f.get(r, []), a.timeout)
        for p in sorted((sd / "solution/gold").glob("*.patch")):
            rc, o = sh(f"git -C /app/{p.stem} apply --whitespace=nowarn {p}")
            if rc:
                print(f"gold {p.name} failed to apply: {o}", file=sys.stderr)
                raise SystemExit(2)
        post = []
        if measured:
            print(" post (full suites, after gold)", flush=True)
            post = [run_suites(suites, lambda r: None, a.timeout) for _ in range(a.runs)]
        out["steps"].append({"step": name, "pre": pre, "post": post})
        Path(a.out).write_text(json.dumps(out))


if __name__ == "__main__":
    main()
