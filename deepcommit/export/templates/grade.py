"""Grade one step of a cross-repo CSF task from the per-repo logs that test.sh wrote to /logs/verifier.

test.sh runs, for every repo with a non-empty list, ``<repo>.<f2p|p2p>.log`` and ``<repo>.<f2p|p2p>.rc``. This script parses
those logs with the parser named in tests/suites.json, namespaces every test id as ``<repo>::<id>`` and merges them into
one result set.

Writes /logs/verifier/reward.json (numbers only, as Harbor requires):
  reward      1 if every fail-to-pass and every pass-to-pass test of EVERY repo passed (and at least one F2P ran), else 0
  f2p_total, f2p_passed, p2p_total, p2p_passed
  f2p_total__<repo>, f2p_passed__<repo>, p2p_total__<repo>, p2p_passed__<repo>
and /logs/verifier/ctrf.json with one entry per test.

  python3 grade.py                grade the logs
  python3 grade.py --error WHY    record a verifier error (reward 0, no logs)
  python3 grade.py repos          print every repo of suites.json (suites and contract-only repos: patches apply to all)
  python3 grade.py suites         print "repo<TAB>workdir<TAB>whole_suite<TAB>run_cmd" per suite (contract-only repos have none)
  python3 grade.py services       print "name<TAB>start_cmd<TAB>health_cmd" per service the suites need (e.g. a database)
  python3 grade.py excludes REPO  print the patterns git apply must exclude (the repo's test dirs)

CSF_LOG_DIR / CSF_TESTS_DIR override /logs/verifier and /tests (used by the exporter's own tests).
"""

import json
import os
import re
import sys
from pathlib import Path

LOG = Path(os.environ.get("CSF_LOG_DIR", "/logs/verifier"))
TESTS = Path(os.environ.get("CSF_TESTS_DIR", "/tests"))
GROUPS = ("f2p", "p2p")

_PYTEST = re.compile(r"^(PASSED|FAILED|ERROR)\s+(\S+)")
_CARGO = re.compile(r"^test (\S+) \.\.\. (ok|FAILED)")
_CARGO_RUN = re.compile(r"^\s*Running (?:unittests )?(\S+)")
_GO = re.compile(r"^\s*--- (PASS|FAIL): (\S+)")


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
    return [
        (m.group(2), "passed" if m.group(1) == "PASS" else "failed") for m in map(_GO.match, text.splitlines()) if m
    ]


PARSERS = {"pytest": parse_pytest, "cargo": parse_cargo, "go": parse_go, "exit-code": lambda text: []}


def load_suites():
    return json.loads((TESTS / "suites.json").read_text(encoding="utf-8"))["suites"]


def contract_only_repos():
    return sorted(json.loads((TESTS / "suites.json").read_text(encoding="utf-8")).get("contract_only", {}))


def grade_group(repo, group, suite):
    """Tests of one repo/group, or None if test.sh did not run it (empty list)."""
    rc_path = LOG / f"{repo}.{group}.rc"
    if not rc_path.exists():
        return None
    rc = int(rc_path.read_text().strip() or 1)
    log_path = LOG / f"{repo}.{group}.log"
    text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    tests = [(f"{repo}::{tid}", status) for tid, status in PARSERS[suite.get("parser", "exit-code")](text)]
    if rc != 0 and not any(s == "failed" for _, s in tests):
        tests.append((f"{repo}::{group}:exit-code-{rc}", "failed"))
    if rc == 0 and not tests:
        tests.append((f"{repo}::{group}", "passed" if suite.get("parser", "exit-code") == "exit-code" else "failed"))
    return tests


def load_ids():
    """Curated test ids of this step (tests/ids.json: {"f2p": [...], "p2p": [...]}), or None for file-level grading."""
    path = TESTS / "ids.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write(rewards, tests, notes):
    LOG.mkdir(parents=True, exist_ok=True)
    (LOG / "reward.json").write_text(json.dumps(rewards, sort_keys=True), encoding="utf-8")
    counts = {}
    for t in tests:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    ctrf = {
        "results": {
            "tool": {"name": "csf-grade"},
            "summary": {
                "tests": len(tests),
                "passed": counts.get("passed", 0),
                "failed": counts.get("failed", 0),
                "skipped": 0,
                "pending": 0,
                "other": 0,
            },
            "tests": tests,
        }
    }
    (LOG / "ctrf.json").write_text(json.dumps(ctrf, indent=2), encoding="utf-8")
    print(f"csf-grade: {json.dumps(rewards, sort_keys=True)}")
    for n in notes:
        print(f"csf-grade: {n}")


def grade():
    rewards = {}
    tests = []
    notes = []
    totals = {g: [0, 0] for g in GROUPS}
    ids = load_ids()
    seen = {}  # repo -> {group: {id: status}}
    for suite in load_suites():
        repo = suite["repo"]
        for group in GROUPS:
            seen.setdefault(repo, {})[group] = dict(grade_group(repo, group, suite) or [])
    for suite in load_suites():
        repo = suite["repo"]
        for group in GROUPS:
            got = seen[repo][group]
            if ids is None:
                res = list(got.items())
            else:
                # curated: exactly the listed ids of this repo, from the run of any group (a test is one test)
                pool = {**seen[repo]["p2p"], **seen[repo]["f2p"]}
                res = [(i, pool.get(i, "failed")) for i in ids.get(group, []) if i.startswith(f"{repo}::")]
            passed = sum(1 for _, s in res if s == "passed")
            rewards[f"{group}_total__{repo}"] = len(res)
            rewards[f"{group}_passed__{repo}"] = passed
            totals[group][0] += len(res)
            totals[group][1] += passed
            tests.extend({"name": name, "status": status, "suite": group, "repo": repo} for name, status in res)
            if passed != len(res):
                notes.append(f"{repo} {group}: {len(res) - passed} failed")
    ok = totals["f2p"][0] > 0 and totals["f2p"][0] == totals["f2p"][1] and totals["p2p"][0] == totals["p2p"][1]
    if totals["f2p"][0] == 0:
        notes.append("no F2P test ran")
    rewards.update(
        reward=1 if ok else 0,
        f2p_total=totals["f2p"][0],
        f2p_passed=totals["f2p"][1],
        p2p_total=totals["p2p"][0],
        p2p_passed=totals["p2p"][1],
    )
    write(rewards, tests, notes)


def main(argv):
    if argv[:1] == ["--error"]:
        rewards = {"reward": 0, "f2p_total": 0, "f2p_passed": 0, "p2p_total": 0, "p2p_passed": 0, "verifier_error": 1}
        write(rewards, [], [argv[1] if len(argv) > 1 else "verifier error"])
    elif argv[:1] == ["repos"]:
        print("\n".join([s["repo"] for s in load_suites()] + contract_only_repos()))
    elif argv[:1] == ["suites"]:
        for s in load_suites():
            print(f"{s['repo']}\t{s.get('workdir', '.')}\t{int(bool(s.get('whole_suite')))}\t{s['run_cmd']}")
    elif argv[:1] == ["services"]:
        for sv in json.loads((TESTS / "suites.json").read_text(encoding="utf-8")).get("services", []):
            print(f"{sv['name']}\t{sv['start']}\t{sv['health']}")
    elif argv[:1] == ["excludes"]:
        for s in load_suites():
            if s["repo"] == argv[1]:
                print("\n".join(s.get("test_excludes", [])))
    else:
        grade()


if __name__ == "__main__":
    main(sys.argv[1:])
