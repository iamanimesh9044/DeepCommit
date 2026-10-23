"""Run each cross-repo milestone's tests at its start and end state, per repo, and classify them.

For a milestone and a repo: start = parent of the milestone's earliest commit in that repo;
end = start + the milestone's commits cherry-picked. Tests run in Docker (go 1.26 / rust 1.97.1).
Classification uses the SWE-Milestone buckets: fail_to_pass, none_to_pass, pass_to_pass, ...
"""
import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

GIT = ["git", "-c", "core.commitGraph=false", "-c", "user.name=mr", "-c", "user.email=mr@example.com"]
GEN = re.compile(r"\.(pb|twirp)\.go$|(^|/)generated(/|$)|\.serde\.rs$")


def git(path, *a, check=False):
    r = subprocess.run([*GIT, "-C", str(path), *a], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(a)}: {r.stderr[:300]}")
    return r.stdout.strip()


def worktree(repo, name, work, sha, picks):
    d = Path(work) / name
    if d.exists():
        git(repo, "worktree", "remove", "--force", str(d))
    git(repo, "worktree", "add", "--detach", str(d), sha, check=True)
    for p in picks:
        r = subprocess.run([*GIT, "-C", str(d), "cherry-pick", "-x", "--allow-empty", p], capture_output=True, text=True)
        if r.returncode:
            subprocess.run([*GIT, "-C", str(d), "cherry-pick", "--abort"], capture_output=True)
            raise RuntimeError(f"cherry-pick {p} failed: {r.stdout[-200:]}{r.stderr[-200:]}")
    return d


def go_pkgs(repo, shas):
    dirs = set()
    for s in shas:
        for f in git(repo, "show", "--name-only", "--format=", s).splitlines():
            if f.endswith(".go"):
                dirs.add("./" + str(Path(f).parent) if str(Path(f).parent) != "." else ".")
    return sorted(dirs)


def rust_crates(repo, shas, tree):
    crates = set()
    for s in shas:
        for f in git(repo, "show", "--name-only", "--format=", s).splitlines():
            if not f.endswith(".rs") or GEN.search(f):
                continue
            p = Path(f)
            for parent in p.parents:
                if (Path(tree) / parent / "Cargo.toml").exists() and str(parent) != "." and "generate" not in parent.parts:
                    crates.add(parent.name)
                    break
    return sorted(crates)


def run_docker(image, tree, cmd, volumes, timeout=2400):
    args = ["docker", "run", "--rm", "-v", f"{tree}:/src", "-w", "/src"]
    for v in volumes:
        args += ["-v", v]
    r = subprocess.run([*args, image, "sh", "-c", cmd], capture_output=True, text=True, timeout=timeout)
    return r.stdout + "\n" + r.stderr


def parse_go(out):
    res = {}
    for ln in out.splitlines():
        if not ln.startswith("{"):
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if e.get("Test") and e.get("Action") in ("pass", "fail", "skip"):
            res[f"{e['Package']}/{e['Test']}"] = {"pass": "passed", "fail": "failed", "skip": "skipped"}[e["Action"]]
    return res


def parse_rust(out, crates):
    res, cur = {}, ""
    for ln in out.splitlines():
        m = re.match(r"\s+Running .*\((.*?)\)|\s+Doc-tests (\S+)", ln)
        if m:
            cur = m.group(1) or m.group(2)
        m = re.match(r"test (\S+) \.\.\. (ok|FAILED|ignored)", ln)
        if m:
            res[f"{cur}/{m.group(1)}"] = {"ok": "passed", "FAILED": "failed", "ignored": "skipped"}[m.group(2)]
    return res


def classify(start, end):
    b = {k: [] for k in ("pass_to_pass", "fail_to_pass", "none_to_pass", "fail_to_fail", "pass_to_fail", "pass_to_none", "other")}
    for t in sorted(set(start) | set(end)):
        s, e = start.get(t), end.get(t)
        if s is None and e == "passed": b["none_to_pass"].append(t)
        elif s == "passed" and e == "passed": b["pass_to_pass"].append(t)
        elif s == "failed" and e == "passed": b["fail_to_pass"].append(t)
        elif s == "failed" and e == "failed": b["fail_to_fail"].append(t)
        elif s == "passed" and e == "failed": b["pass_to_fail"].append(t)
        elif s == "passed" and e is None: b["pass_to_none"].append(t)
        else: b["other"].append(t)
    return b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--milestones", required=True)
    ap.add_argument("--ids", nargs="+", required=True)
    ap.add_argument("--only-repos", nargs="*", default=None)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    repos = {cfg["contract"]["id"]: {**cfg["contract"], "lang": "go"}, **{d["id"]: d for d in cfg["dependents"]}}
    rows = {r["id"]: r for r in csv.DictReader(open(a.milestones))}
    for mid in a.ids:
        by = {}
        for c in rows[mid]["commits"].split(";"):
            rid, sha = c.split("@")
            by.setdefault(rid, []).append(sha)
        for rid, shorts in by.items():
            if a.only_repos and rid not in a.only_repos:
                continue
            R = repos[rid]; path = R["local"]
            full = [git(path, "rev-parse", s) for s in shorts]
            full.sort(key=lambda s: int(git(path, "show", "-s", "--format=%ct", s)))
            start = git(path, "rev-parse", full[0] + "^")
            res = {}
            od = Path(a.out) / mid / rid; od.mkdir(parents=True, exist_ok=True)
            for state, picks in (("start", []), ("end", full)):
                try:
                    tree = worktree(path, f"{mid}-{rid}-{state}", a.work, start, picks)
                    if R["lang"] == "go":
                        pk = go_pkgs(path, full)
                        if not pk:
                            res[state] = {}; continue
                        out = run_docker("golang:1.26", tree, f"go test -json -count=1 -timeout 20m {' '.join(pk)}",
                                         ["lk-gomod:/go/pkg/mod", "lk-gobuild:/root/.cache/go-build"])
                        res[state] = parse_go(out)
                    else:
                        if R.get("pin", {}).get("kind") == "submodule":
                            subprocess.run([*GIT, "-C", str(tree), "submodule", "update", "--init", "--recursive", R["pin"]["path"]], capture_output=True)
                        cr = rust_crates(path, full, tree)
                        if not cr:
                            res[state] = {}; continue
                        pf = " ".join(f"-p {c}" for c in cr)
                        out = run_docker("rust:1.97.1", tree,
                                         f"apt-get update -qq >/dev/null && apt-get install -y -qq protobuf-compiler libssl-dev pkg-config clang lld binutils >/dev/null 2>&1; CARGO_TARGET_DIR=/target cargo test {pf} --no-fail-fast 2>&1",
                                         ["lk-cargo:/usr/local/cargo/registry", "lk-target:/target"], timeout=3600)
                        res[state] = parse_rust(out, cr)
                    (od / f"{state}.log").write_text(out[-20000:])
                except Exception as ex:
                    res[state] = {}
                    (od / f"{state}.error").write_text(str(ex))
                (od / f"{state}.json").write_text(json.dumps(res[state], indent=0))
            cl = classify(res.get("start", {}), res.get("end", {}))
            (od / "classification.json").write_text(json.dumps(cl, indent=0))
            print(mid, rid, {k: len(v) for k, v in cl.items() if v}, flush=True)


if __name__ == "__main__":
    main()
