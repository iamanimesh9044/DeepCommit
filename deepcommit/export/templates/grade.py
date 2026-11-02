"""Grade one step of a cross-repo CSF task. GENERATED (DeepCommit export): the same in every step.

Reads /tests/suite.json (this step's name, its suites, services, the files to run and every check with its bucket) and
the logs test.sh wrote to /logs/verifier (<repo>.<f2p|p2p>.log/.rc, deploy.<service>.rc).

Buckets: ``deploy`` (a service the suites need came up healthy), ``f2p`` (failed before this step's gold patch, must
pass), ``p2p`` (must keep passing). Test ids are ``<repo>::<id>``. A suite.json without ``checks`` (not curated yet) is
graded by files: every test the f2p and p2p runs report must pass.

Writes /logs/verifier/reward.json (numbers only, as Harbor requires):
  reward      1 only if every check passed (and at least one f2p check exists), else 0
  <bucket>_total, <bucket>_passed, and per repo <bucket>_total__<repo>, <bucket>_passed__<repo>
and /logs/verifier/checks.json (every check with its bucket and status) and ctrf.json.

  python3 grade.py                grade the logs
  python3 grade.py --error WHY    record a verifier error (reward 0)
  python3 grade.py step           print the step name
  python3 grade.py repos          print every repo (suites and contract-only repos)
  python3 grade.py suites         print "repo workdir whole_suite run_cmd f2p_files p2p_files", separated by \\x1f
  python3 grade.py services       print "name<TAB>kind<TAB>health_cmd" per service (kind: app = harbor-svc deploy)
  python3 grade.py excludes REPO  print the repo's test dirs (test.sh resets them to the base tree)

CSF_LOG_DIR / CSF_TESTS_DIR / CSF_APP_DIR override /logs/verifier, /tests and /app.
"""

import json
import os
import re
import sys
from pathlib import Path

LOG = Path(os.environ.get("CSF_LOG_DIR", "/logs/verifier"))
TESTS = Path(os.environ.get("CSF_TESTS_DIR", "/tests"))
APP = Path(os.environ.get("CSF_APP_DIR", "/app"))
BUCKETS = ("deploy", "f2p", "p2p")

_PYTEST = re.compile(r"^(PASSED|FAILED|ERROR)\s+(\S+)")
_CARGO = re.compile(r"^test (\S+) \.\.\. (ok|FAILED)")
_CARGO_RUN = re.compile(r"^\s*Running (?:unittests )?(\S+)")
_GO = re.compile(r"^\s*--- (PASS|FAIL): (\S+)")
_GO_PKG = re.compile(r"^(?:ok|FAIL)\s+(\S+)\s")
_VITEST_PKG = re.compile(r"^== (\S+)\s*$")
_VITEST = re.compile(r"^\s*(\u2713|\u00d7|\u2717)\s+(.+?)(?:\s+\d+(?:\.\d+)?m?s)?\s*$")


def parse_pytest(text):
    return [
        (m.group(2), "passed" if m.group(1) == "PASSED" else "failed")
        for m in map(_PYTEST.match, text.splitlines())
        if m
    ]


def parse_cargo(text):
    """Cargo test ids are ``<test binary>::<test name>``: the same name can exist in several binaries."""
    out, binary = [], ""
    for line in text.splitlines():
        r = _CARGO_RUN.match(line)
        if r:
            binary = r.group(1).split("/")[-1].removesuffix(".rs")
            continue
        m = _CARGO.match(line)
        if m:
            out.append(
                (f"{binary}::{m.group(1)}" if binary else m.group(1), "passed" if m.group(2) == "ok" else "failed")
            )
    return out


def parse_go(text):
    """``go test -v`` ids are ``<package>::<test>``: a package's tests are followed by its ``ok``/``FAIL <package>`` line."""
    out, pending = [], []
    for line in text.splitlines():
        m = _GO.match(line)
        if m:
            pending.append((m.group(2), "passed" if m.group(1) == "PASS" else "failed"))
            continue
        p = _GO_PKG.match(line)
        if p:
            out.extend((f"{p.group(1)}::{name}", status) for name, status in pending)
            pending = []
    out.extend((f"?::{name}", status) for name, status in pending)
    return out


def parse_vitest(text):
    """``vitest run --reporter=verbose`` lines: `` ✓ <file> > <suite> > <test> 12ms`` (× for a failure).

    A runner that loops over workspace packages prints ``== <package>`` before each run; the id is then
    ``<package>::<file> > <suite> > <test>``, so equal test files of two packages stay apart.
    """
    out, prefix = [], ""
    for line in text.splitlines():
        h = _VITEST_PKG.match(line)
        if h:
            prefix = h.group(1).rstrip("/") + "::"
            continue
        m = _VITEST.match(line)
        if m:
            out.append((prefix + m.group(2), "passed" if m.group(1) == "\u2713" else "failed"))
    return out


PARSERS = {
    "pytest": parse_pytest,
    "cargo": parse_cargo,
    "go": parse_go,
    "vitest": parse_vitest,
    "exit-code": lambda text: [],
}


def _glob_re(pattern):
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def test_dirs(repo, patterns, app=None):
    """Concrete test directories of a repo for its test_excludes patterns (``tests/*``, ``**/tests/**``): every
    directory, in the agent's tree or in the base tree, whose files the pattern covers. Outermost only."""
    import subprocess

    dirs = set()
    root = (app or APP) / repo
    skip = {".git", "target", "node_modules", "__pycache__", ".venv", ".pytest_cache"}
    for cur, subdirs, _ in os.walk(root):
        subdirs[:] = [d for d in subdirs if d not in skip]
        dirs |= {(Path(cur) / d).relative_to(root).as_posix() for d in subdirs}
    base = subprocess.run(["git", f"--git-dir=/opt/csf/base/{repo}.git", "ls-tree", "-r", "-d", "--name-only", "HEAD"],
                          capture_output=True, text=True)
    dirs |= set(base.stdout.split())
    out = set()
    for pattern in patterns:
        stem = pattern
        for suffix in ("/**", "/*"):
            stem = stem.removesuffix(suffix)
        rx = _glob_re(stem)
        out |= {d for d in dirs if rx.match(d)}
    return sorted(d for d in out if not any(d.startswith(o + "/") for o in out))


def suite():
    return json.loads((TESTS / "suite.json").read_text(encoding="utf-8"))


def run_results(repo, group, s):
    """Test results of one repo/group run, or {} if test.sh did not run it. A run that exits non-zero without any
    failing test (a build or collection error) adds the failed id ``<repo>::<group>:exit-code-<rc>``."""
    rc_path = LOG / f"{repo}.{group}.rc"
    if not rc_path.exists():
        return {}
    rc = int(rc_path.read_text().strip() or 1)
    log_path = LOG / f"{repo}.{group}.log"
    text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    out = {f"{repo}::{tid}": status for tid, status in PARSERS[s.get("parser", "exit-code")](text)}
    if rc != 0 and "failed" not in out.values():
        out[f"{repo}::{group}:exit-code-{rc}"] = "failed"
    if rc == 0 and not out and s.get("parser", "exit-code") == "exit-code":
        out[f"{repo}::{group}"] = "passed"
    return out


def deploy_status(service):
    rc = LOG / f"deploy.{service}.rc"
    return "passed" if rc.exists() and rc.read_text().strip() == "0" else "failed"


def write(rewards, checks, notes):
    LOG.mkdir(parents=True, exist_ok=True)
    (LOG / "reward.json").write_text(json.dumps(rewards, sort_keys=True), encoding="utf-8")
    (LOG / "checks.json").write_text(json.dumps(checks, indent=1), encoding="utf-8")
    counts = {}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    ctrf = {
        "results": {
            "tool": {"name": "csf-grade"},
            "summary": {
                "tests": len(checks),
                "passed": counts.get("passed", 0),
                "failed": counts.get("failed", 0),
                "skipped": 0,
                "pending": 0,
                "other": 0,
            },
            "tests": [{"name": c["id"], "status": c["status"], "suite": c["bucket"], "repo": c["repo"]} for c in checks],
        }
    }
    (LOG / "ctrf.json").write_text(json.dumps(ctrf, indent=2), encoding="utf-8")
    print(f"csf-grade: {json.dumps(rewards, sort_keys=True)}")
    for n in notes:
        print(f"csf-grade: {n}")


def grade():
    s = suite()
    pool = {}  # repo -> {id: status}, from the run of any group (a test is one test)
    for su in s["suites"]:
        repo = su["repo"]
        pool[repo] = {**run_results(repo, "p2p", su), **run_results(repo, "f2p", su)}
    if "checks" not in s:  # not curated yet: grade by files, every reported test of every run counts
        listed = [{"id": f"deploy::{sv['name']}", "repo": sv["name"], "bucket": "deploy"}
                  for sv in s.get("services", [])]
        for su in s["suites"]:
            repo = su["repo"]
            for group in ("f2p", "p2p"):
                seen = {c["id"] for c in listed}
                listed += [{"id": i, "repo": repo, "bucket": group} for i in run_results(repo, group, su) if i not in seen]
        s = {**s, "checks": listed}
    checks = []
    for c in s["checks"]:
        if c["bucket"] == "deploy":
            status = deploy_status(c["id"].split("::", 1)[1])
        else:
            status = pool.get(c["repo"], {}).get(c["id"], "failed")
        checks.append({**c, "status": status})
    rewards, notes = {}, []
    for b in BUCKETS:
        mine = [c for c in checks if c["bucket"] == b]
        rewards[f"{b}_total"] = len(mine)
        rewards[f"{b}_passed"] = sum(c["status"] == "passed" for c in mine)
        for repo in sorted({c["repo"] for c in mine}):
            r = [c for c in mine if c["repo"] == repo]
            rewards[f"{b}_total__{repo}"] = len(r)
            rewards[f"{b}_passed__{repo}"] = sum(c["status"] == "passed" for c in r)
            if rewards[f"{b}_passed__{repo}"] != len(r):
                notes.append(f"{repo} {b}: {len(r) - rewards[f'{b}_passed__{repo}']} failed")
    ok = rewards["f2p_total"] > 0 and all(c["status"] == "passed" for c in checks)
    if rewards["f2p_total"] == 0:
        notes.append("no f2p check in suite.json")
    rewards["reward"] = 1 if ok else 0
    write(rewards, checks, notes)


def main(argv):
    if argv[:1] == ["--error"]:
        rewards = {"reward": 0, "verifier_error": 1, **{f"{b}_{k}": 0 for b in BUCKETS for k in ("total", "passed")}}
        write(rewards, [], [argv[1] if len(argv) > 1 else "verifier error"])
        return
    s = suite()
    if argv[:1] == ["step"]:
        print(s["step"])
    elif argv[:1] == ["repos"]:
        print("\n".join([su["repo"] for su in s["suites"]] + sorted(s.get("contract_only", {}))))
    elif argv[:1] == ["suites"]:
        for su in s["suites"]:
            f = s["files"].get(su["repo"], {})
            fields = [su["repo"], su.get("workdir", "."), str(int(bool(su.get("whole_suite")))), su["run_cmd"],
                      " ".join(f.get("f2p", [])), " ".join(f.get("p2p", []))]
            print("\x1f".join(fields))  # not a tab: bash read merges empty tab-separated fields
    elif argv[:1] == ["services"]:
        for sv in s.get("services", []):
            print(f"{sv['name']}\t{sv.get('kind', 'app')}\t{sv['health']}")
    elif argv[:1] == ["excludes"]:
        for su in s["suites"]:
            if su["repo"] == argv[1]:
                print("\n".join(test_dirs(argv[1], su.get("test_excludes", []))))
    else:
        grade()


if __name__ == "__main__":
    main(sys.argv[1:])
