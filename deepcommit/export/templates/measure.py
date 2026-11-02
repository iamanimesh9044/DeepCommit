"""Measure the real test outcomes of a CSF multi-repo task inside its own stack (Phase 3 for exported tasks).

Runs in the main container of the task's compose stack (``finalize.measure`` starts it: environment/docker-compose.yaml
plus the app services deployed from their manifests), with the task folder mounted at /task. For every step, in
task.toml order, on the sequential state (base + gold of steps 1..k-1):

  seed  the step's checks go in exactly as test.sh does it (test dirs reset to /opt/csf/base, checks/<repo>/ copied)
  pre   the step's own test files (suite.json ``own_files``) run on the state BEFORE the step's gold patch
  post  the gold patch is applied and the full suites run ``--runs`` times

Per-test outcomes land in ``--out`` as JSON: {"steps": [{"step", "pre": {id: status}, "post": [{id: status}, ...]}]}.
``curate`` (host side, deepcommit.export.curate) turns those into F2P / P2P ids.

  python3 measure.py --task /task --out /out/measure.json [--only m4,m5] [--runs 2]

Test ids are ``<repo>::<id>`` exactly as the step's grade.py names them, so a measured id is a gradable id.
"""

import argparse
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import tomllib


def load_grade(step_tests: Path):
    spec = importlib.util.spec_from_file_location("grade", step_tests / "grade.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sh(cmd, cwd=None, timeout=None):
    p = subprocess.run(
        ["bash", "-c", cmd], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=timeout, env=dict(os.environ),
    )  # fmt: skip
    return p.returncode, p.stdout


def seed(grade, suite, checks: Path):
    for su in suite["suites"]:
        repo = su["repo"]
        for d in grade.test_dirs(repo, su.get("test_excludes", [])):
            sh(f"rm -rf {shlex.quote(f'/app/{repo}/{d}')}")
            sh(f"git --git-dir=/opt/csf/base/{repo}.git --work-tree=/app/{repo} checkout HEAD -- {shlex.quote(d)}"
               " 2>/dev/null || true")
        if (checks / repo).is_dir():
            sh(f"cp -a {shlex.quote(str(checks / repo))}/. /app/{repo}/")


def run_suites(grade, suites, tests_of, timeout):
    """Run every suite once. ``tests_of(repo)`` gives the files to run, [] to skip the suite, None for the whole suite."""
    res = {}
    for s in suites:
        repo = s["repo"]
        files = tests_of(repo)
        if files == []:
            continue
        wd = Path(f"/app/{repo}/{s.get('workdir', '.')}")
        if files:
            # a file the step adds does not exist before its gold patch; listing it would make the runner (pytest exits
            # 4) refuse the whole run, so the step's existing files would go unmeasured
            files = [f for f in files if (wd / f).exists()]
            if not files:
                continue
        tests = " ".join(shlex.quote(f) for f in files) if files else s.get("all_tests", ".")
        cmd = s["run_cmd"].replace("{tests}", tests)
        t0 = time.time()
        try:
            rc, out = sh(cmd, cwd=f"/app/{repo}/{s.get('workdir', '.')}", timeout=timeout)
        except subprocess.TimeoutExpired as e:
            rc, out = 124, e.stdout if isinstance(e.stdout, str) else ""
        parsed = dict(grade.PARSERS[s.get("parser", "pytest")](out)) if s.get("parser", "pytest") in grade.PARSERS else {}
        print(f"  {repo}: rc={rc} {len(parsed)} tests in {time.time() - t0:.0f}s", flush=True)
        for tid, st in parsed.items():
            res[f"{repo}::{tid}"] = st
    return res


def wait_services(suite, timeout=120):
    for sv in suite.get("services", []):
        for _ in range(timeout):
            if sh(sv["health"])[0] == 0:
                break
            time.sleep(1)
        else:
            raise SystemExit(f"service {sv['name']} is not healthy ({sv['health']})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="/task")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--timeout", type=int, default=3000)
    ap.add_argument("--runs", type=int, default=2, help="post runs per step; a test must pass in all of them")
    a = ap.parse_args()
    task = Path(a.task)
    steps = [s["name"] for s in tomllib.loads((task / "task.toml").read_text())["steps"]]
    only = {s for s in a.only.split(",") if s}
    out = {"steps": []}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    for name in steps:
        sd = task / "steps" / name
        suite = json.loads((sd / "tests/suite.json").read_text())
        grade = load_grade(sd / "tests")
        measured = not only or name.split("-")[0] in only or name in only
        print(f"== {name}", flush=True)
        pre, post = {}, []
        if measured:
            wait_services(suite)
            seed(grade, suite, sd / "tests/checks")
            own = suite.get("own_files", {})
            print(" pre (step's own test files, before gold)", flush=True)
            pre = run_suites(grade, suite["suites"], lambda r, o=own: o.get(r, []), a.timeout)
        rc, o = sh(f"cd /app && git apply --whitespace=nowarn {shlex.quote(str(sd / 'solution/gold.patch'))}")
        if rc:
            print(f"gold of {name} failed to apply: {o}", file=sys.stderr)
            raise SystemExit(2)
        if measured:
            print(" post (full suites, after gold)", flush=True)
            post = [run_suites(grade, suite["suites"], lambda r: None, a.timeout) for _ in range(a.runs)]
        out["steps"].append({"step": name, "pre": pre, "post": post})
        Path(a.out).write_text(json.dumps(out))


if __name__ == "__main__":
    main()
