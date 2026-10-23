"""End-to-end multi-repo evaluation: one persistent workspace, a milestone queue, submit by tag, test in Docker.

  setup     build the workspace (all repos at their base state, go.work, queue, specs, submit.sh)
  evaluate  score one submitted milestone (tag agent-impl-<id> in every repo)
  oracle    apply the gold commits milestone by milestone, submit, evaluate (upper bound)
  nop       submit the untouched base for every milestone, evaluate (lower bound)

Score per graded milestone (as in SWE-Milestone): recall = fixed required / required,
precision = (fixed + 1) / (fixed + broken P2P + 1), score = harmonic mean, resolved = all required pass and none broken.
A go.work file makes the local protocol checkout override the version pinned in livekit's go.mod.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from deepcommit.multirepo.run_tests import GIT, git, parse_go, parse_rust, run_docker

GO_PREFIX = {"livekit": "github.com/livekit/livekit-server/", "protocol": "github.com/livekit/protocol/"}
QUEUE = "# Task queue\n\n{items}\n"


def load(a):
    cfg = yaml.safe_load(open(a.config))
    repos = {cfg["contract"]["id"]: {**cfg["contract"], "lang": "go"}, **{d["id"]: d for d in cfg["dependents"]}}
    man = json.loads((Path(a.task) / "manifest.json").read_text())
    return cfg, repos, man



def base_of(man, repos, rid):
    """Base state of a repo = parent of its earliest gold commit across all milestones."""
    shas = [c for m in man["milestones"] for c in m["repos"].get(rid, {}).get("commits", [])]
    if not shas:
        return None
    path = repos[rid]["local"]
    first = min(shas, key=lambda s: int(git(path, "show", "-s", "--format=%ct", s)))
    return git(path, "rev-parse", first + "^")


def setup(a):
    cfg, repos, man = load(a)
    ws = Path(a.workspace)
    if ws.exists():
        for rid, R in repos.items():
            git(R["local"], "worktree", "remove", "--force", str(ws / rid))
        shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True)
    for rid, R in repos.items():
        b = base_of(man, repos, rid)
        if not b:
            b = git(R["local"], "rev-parse", "origin/HEAD")
        for t in git(R["local"], "tag", "-l", "agent-impl-*").split():
            git(R["local"], "tag", "-d", t)
        git(R["local"], "branch", "-D", f"e2e-{ws.name}-{rid}")
        git(R["local"], "worktree", "add", "-b", f"e2e-{ws.name}-{rid}", str(ws / rid), b)
        if R.get("pin", {}).get("kind") == "submodule":
            subprocess.run([*GIT, "-C", str(ws / rid), "submodule", "update", "--init", R["pin"]["path"]], capture_output=True)
    (ws / "go.work").write_text("go 1.26.0\n\nuse (\n\t./livekit\n\t./protocol\n)\n")
    items = "\n".join(f"- {m['id']}: {m['title']} (srs/{m['id']}/SRS.md)" for m in man["milestones"] if (Path(a.task) / "srs" / m["id"]).exists())
    (ws / "TASK_QUEUE.md").write_text(QUEUE.format(items=items))
    shutil.copytree(Path(a.task) / "srs", ws / "srs")
    (ws / "submit.sh").write_text('#!/bin/sh\n# usage: ./submit.sh X004  (commits and tags every repo)\nfor r in protocol livekit rust-sdks; do\n  git -C $r add -A && git -C $r commit -q --allow-empty -m "Implement $1" && git -C $r tag agent-impl-$1\ndone\n')
    (ws / "submit.sh").chmod(0o755)
    print("workspace ready:", ws, {r: git(ws / r, "rev-parse", "--short", "HEAD") for r in repos})


def submit(ws, repos, mid):
    for rid in repos:
        git(ws / rid, "add", "-A")
        git(ws / rid, "commit", "-q", "--allow-empty", "-m", f"Implement {mid}")
        git(ws / rid, "tag", "-f", f"agent-impl-{mid}")


def go_pkgs(ids, rid):
    out = set()
    for t in ids:
        i = t.find("/Test")
        pkg = t[:i] if i > 0 else t.rsplit("/", 1)[0]
        out.add("./" + pkg[len(GO_PREFIX[rid]):] if pkg.startswith(GO_PREFIX[rid]) and pkg != GO_PREFIX[rid].rstrip("/") else ".")
    return sorted(out)


def evaluate_one(a, mid, tag=None):
    cfg, repos, man = load(a)
    ws = Path(a.workspace)
    m = next(x for x in man["milestones"] if x["id"] == mid)
    snap = ws / ".eval" / mid
    shutil.rmtree(snap, ignore_errors=True); snap.mkdir(parents=True)
    for rid, R in repos.items():
        git(R["local"], "worktree", "add", "--detach", str(snap / rid), tag or f"agent-impl-{mid}")
        if R.get("pin", {}).get("kind") == "submodule":
            subprocess.run([*GIT, "-C", str(snap / rid), "submodule", "update", "--init", R["pin"]["path"]], capture_output=True)
    (snap / "go.work").write_text((ws / "go.work").read_text())
    result = {"id": mid, "repos": {}}
    req_all = fixed_all = broken_all = 0
    for rid in repos:
        if not (Path(a.task) / getattr(a, "tests_dir", "tests") / f"{mid}_{rid}.json").exists():
            continue
        t = json.loads((Path(a.task) / getattr(a, "tests_dir", "tests") / f"{mid}_{rid}.json").read_text())
        required, keep = t["fail_to_pass"] + t["none_to_pass"], t["pass_to_pass"]
        if repos[rid]["lang"] == "go":
            pk = go_pkgs(required + keep, rid)
            out = run_docker("golang:1.26", snap, f"cd /src/{rid} && go test -json -count=1 -timeout 20m {' '.join(pk)}", ["lk-gomod:/go/pkg/mod", "lk-gobuild:/root/.cache/go-build"])
            st = parse_go(out)
        else:
            pf = " ".join(f"-p {c}" for c in repos[rid]["test_crates"])
            out = run_docker("rust:1.97.1", snap, f"apt-get update -qq >/dev/null && apt-get install -y -qq protobuf-compiler libssl-dev pkg-config clang lld binutils >/dev/null 2>&1; cd /src/{rid} && CARGO_TARGET_DIR=/target cargo test {pf} --no-fail-fast 2>&1",
                             ["lk-cargo:/usr/local/cargo/registry", "lk-target:/target"], timeout=3600)
            import re
            st = {re.sub(r"-[0-9a-f]{16}\b", "", k): v for k, v in parse_rust(out, repos[rid]["test_crates"]).items()}
        (snap / f"{rid}.log").write_text(out)
        fixed = [x for x in required if st.get(x) == "passed"]
        broken = [x for x in keep if st.get(x) != "passed"]
        result["repos"][rid] = {"required": len(required), "fixed": len(fixed), "p2p": len(keep), "broken": len(broken), "broken_examples": broken[:5]}
        req_all += len(required); fixed_all += len(fixed); broken_all += len(broken)
    recall = fixed_all / req_all if req_all else 0.0
    prec = (fixed_all + 1) / (fixed_all + broken_all + 1)
    result |= {"recall": round(recall, 3), "precision": round(prec, 3), "score": round(2 * recall * prec / (recall + prec), 3) if recall + prec else 0.0,
               "resolved": bool(req_all) and fixed_all == req_all and broken_all == 0}
    out_dir = ws / "results"; out_dir.mkdir(exist_ok=True)
    (out_dir / f"{mid}.json").write_text(json.dumps(result, indent=1))
    for rid in repos:
        git(repos[rid]["local"], "worktree", "remove", "--force", str(snap / rid))
    return result


def graded(man):
    return [m["id"] for m in man["milestones"] if m["graded"]]


def apply_gold(ws, repos, m):
    for rid, info in m["repos"].items():
        path = repos[rid]["local"]
        cs = sorted(info["commits"], key=lambda s: int(git(path, "show", "-s", "--format=%ct", s)))
        r = subprocess.run([*GIT, "-C", str(ws / rid), "cherry-pick", "-x", "--allow-empty", "-X", "theirs", *cs], capture_output=True, text=True)
        if r.returncode:
            subprocess.run([*GIT, "-C", str(ws / rid), "cherry-pick", "--abort"], capture_output=True)
            raise RuntimeError(f"{m['id']}/{rid}: cherry-pick failed: {(r.stdout + r.stderr)[-300:]}")


def run_mode(a, mode):
    """oracle = evaluate each milestone's gold tag; nop = evaluate its start tag (context applied, nothing implemented)."""
    cfg, repos, man = load(a)
    ws = Path(a.workspace)
    order = json.loads((ws / "trunk.json").read_text())["order"]
    rows = []
    for mid in order:
        m = next(x for x in man["milestones"] if x["id"] == mid)
        if not m["graded"]:
            print(mid, "non-graded: skipped", flush=True)
            continue
        r = evaluate_one(a, mid, tag=f"agent-impl-{mid}" if mode == "oracle" else f"ctx-{mid}")
        rows.append(r)
        print(mid, {k: r[k] for k in ("recall", "precision", "score", "resolved")}, {k: (v["fixed"], v["required"], v["broken"]) for k, v in r["repos"].items()}, flush=True)
    if rows:
        print(f"{mode}: mean score {sum(r['score'] for r in rows)/len(rows):.3f}, resolved {sum(r['resolved'] for r in rows)}/{len(rows)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["setup", "evaluate", "oracle", "nop"])
    ap.add_argument("--config", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--id")
    ap.add_argument("--tests-dir", dest="tests_dir", default="tests")
    a = ap.parse_args()
    if a.cmd == "setup":
        setup(a)
    elif a.cmd == "evaluate":
        print(json.dumps(evaluate_one(a, a.id), indent=1))
    else:
        run_mode(a, a.cmd)


if __name__ == "__main__":
    main()
