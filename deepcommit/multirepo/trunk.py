"""Build the gold trajectory for a multi-repo task and derive its test sets.

build:  apply milestones in history order. If the workspace does not compile (or a pick conflicts),
        pull in the earlier, unselected commits that touch the failing files (context commits) and retry.
        Tag every milestone: agent-impl-<id>. A base tag agent-impl-BASE marks the starting state.
tests:  run each milestone's packages at its start (previous tag) and end tag, classify with finalize_tests.clean,
        write tests/<id>_<repo>.json  (fail_to_pass / none_to_pass / pass_to_pass).
"""
import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

from deepcommit.multirepo import finalize_tests as FT
from deepcommit.multirepo.e2e import GIT, git, load, parse_go, parse_rust, run_docker, submit

ERR_FILE = re.compile(r"^\s*(?:#.*\n)?\s*([\w./@-]+\.go):\d+:\d+:", re.M)
UNDEF = re.compile(r"(?:undefined: |unknown field |has no field or method |no field or method )(\w+)")
GO_REPOS = ("protocol", "livekit")
MANIFEST_FILES = re.compile(r"(^|/)(go\.mod|go\.sum)$")


def ctime(path, sha):
    return int(git(path, "show", "-s", "--format=%ct", sha))


def milestone_order(man, repos):
    def end(m):
        return max(ctime(repos[r]["local"], c) for r, i in m["repos"].items() for c in i["commits"])
    return sorted(man["milestones"], key=end)


def compile_check(ws, rid):
    out = run_docker("golang:1.26", ws, f"cd /src/{rid} && go test -count=1 -run '^$' ./... 2>&1 | head -300",
                     ["lk-gomod:/go/pkg/mod", "lk-gobuild:/root/.cache/go-build"])
    bad = "[build failed]" in out or "cannot use" in out or "undefined:" in out or re.search(r"\.go:\d+:\d+:", out)
    return not bad, out


def context_for(repos, rid, base, upper, files, excluded, applied):
    path = repos[rid]["local"]
    files = [f for f in files if not MANIFEST_FILES.search(f)]
    if not files:
        return []
    out = git(path, "log", "--reverse", "--no-merges", "--format=%H", f"{base}..{upper}", "--", *files)
    return [c for c in out.split() if c not in excluded and c not in applied]


def symbol_commits(repos, rid, base, upper, idents, excluded, applied):
    path = repos[rid]["local"]
    found = []
    for i in list(idents)[:6]:
        out = git(path, "log", "--reverse", "--no-merges", "--format=%H", f"-S{i}", f"{base}..{upper}")
        found += [c for c in out.split() if c not in excluded and c not in applied and c not in found]
    return found[:6]


def pick(ws, rid, shas, path):
    shas = sorted(set(shas), key=lambda s: ctime(path, s))
    r = subprocess.run([*GIT, "-C", str(ws / rid), "cherry-pick", "-x", "--allow-empty", "-X", "theirs", *shas], capture_output=True, text=True)
    if r.returncode:
        un = git(ws / rid, "diff", "--name-only", "--diff-filter=U").split()
        subprocess.run([*GIT, "-C", str(ws / rid), "cherry-pick", "--abort"], capture_output=True)
        return False, un
    return True, []


def ff(ws, rid, sha):
    r = subprocess.run([*GIT, "-C", str(ws / rid), "merge", "--ff-only", "-q", sha], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"{rid}: fast-forward to {sha[:8]} failed: {r.stderr[-300:]}")


def build(a):
    """Fast-forward each repo along its real history. Per milestone and repo:
    ctx-<id>        = state just before the milestone's first selected commit (context commits applied)
    agent-impl-<id> = state after its last selected commit (gold)."""
    cfg, repos, man = load(a)
    ws = Path(a.workspace)
    order = milestone_order(man, repos)
    for r in repos:
        git(ws / r, "tag", "-f", "agent-impl-BASE")
    prev = {r: git(ws / r, "rev-parse", "HEAD") for r in repos}
    trunk = {"order": [m["id"] for m in order], "context": {}, "preapplied": {}}
    for m in order:
        mid = m["id"]; trunk["context"][mid] = {}
        for r in repos:
            path = repos[r]["local"]
            info = m["repos"].get(r)
            if info:
                sel = set(info["commits"])
                last = max(info["commits"], key=lambda c: ctime(path, c))
                if git(path, "merge-base", "--is-ancestor", last, prev[r]) == "" and subprocess.run([*GIT, "-C", path, "merge-base", "--is-ancestor", last, prev[r]]).returncode == 0:
                    trunk["preapplied"].setdefault(mid, []).append(f"{r}: {last[:8]} already in trunk")
                    git(ws / r, "tag", "-f", f"ctx-{mid}"); git(ws / r, "tag", "-f", f"agent-impl-{mid}")
                    continue
                slice_ = git(path, "rev-list", "--reverse", f"{prev[r]}..{last}").split()
                idx = min(i for i, c in enumerate(slice_) if c in sel) if any(c in sel for c in slice_) else len(slice_)
                if idx:
                    ff(ws, r, slice_[idx - 1])
                git(ws / r, "tag", "-f", f"ctx-{mid}")
                ff(ws, r, last)
                trunk["context"][mid][r] = {"before": [git(path, "show", "-s", "--format=%h %s", c)[:110] for c in slice_[:idx]],
                                            "interleaved": [git(path, "show", "-s", "--format=%h %s", c)[:110] for c in slice_[idx:] if c not in sel]}
                prev[r] = last
            else:
                git(ws / r, "tag", "-f", f"ctx-{mid}")
            git(ws / r, "tag", "-f", f"agent-impl-{mid}")
        good = True
        for r in GO_REPOS:
            ok, out = compile_check(ws, r)
            if not ok:
                good = False
                print(f"{mid}: WARNING compile check failed in {r}: {out[-400:]}", flush=True)
        n = {r: (len(v["before"]), len(v["interleaved"])) for r, v in trunk["context"][mid].items()}
        print(f"{mid}: ok compile={good} (context before, interleaved) {n} preapplied={trunk['preapplied'].get(mid, [])}", flush=True)
        (ws / "trunk.json").write_text(json.dumps(trunk, indent=1))


def snapshot(a, repos, tag, name):
    ws = Path(a.workspace)
    snap = ws / ".eval" / name
    shutil.rmtree(snap, ignore_errors=True); snap.mkdir(parents=True)
    for rid, R in repos.items():
        git(R["local"], "worktree", "add", "--detach", str(snap / rid), tag)
    (snap / "go.work").write_text((ws / "go.work").read_text())
    return snap


def release(repos, snap):
    for rid, R in repos.items():
        git(R["local"], "worktree", "remove", "--force", str(snap / rid))


def run_pkgs(snap, repos, rid, pkgs):
    if repos[rid]["lang"] == "go":
        out = run_docker("golang:1.26", snap, f"cd /src/{rid} && go test -json -count=1 -timeout 20m {' '.join(pkgs)}", ["lk-gomod:/go/pkg/mod", "lk-gobuild:/root/.cache/go-build"])
        return parse_go(out)
    pf = " ".join(f"-p {c}" for c in repos[rid]["test_crates"])
    out = run_docker("rust:1.97.1", snap, f"apt-get update -qq >/dev/null && apt-get install -y -qq protobuf-compiler libssl-dev pkg-config clang lld binutils >/dev/null 2>&1; cd /src/{rid} && CARGO_TARGET_DIR=/target cargo test {pf} --no-fail-fast 2>&1",
                     ["lk-cargo:/usr/local/cargo/registry", "lk-target:/target"], timeout=3600)
    return {re.sub(r"-[0-9a-f]{16}\b", "", k): v for k, v in parse_rust(out, repos[rid]["test_crates"]).items()}


def derive(a):
    cfg, repos, man = load(a)
    ws = Path(a.workspace)
    order = json.loads((ws / "trunk.json").read_text())["order"]
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    prev = "agent-impl-BASE"
    summary = {}
    for mid in order:
        m = next(x for x in man["milestones"] if x["id"] == mid)
        if not m["graded"] and not a.all:
            prev = f"agent-impl-{mid}"; continue
        start = snapshot(a, repos, f"ctx-{mid}", f"{mid}-start"); end = snapshot(a, repos, f"agent-impl-{mid}", f"{mid}-end")
        for rid, R in repos.items():
            changed = git(ws / rid, "diff", "--name-only", f"ctx-{mid}", f"agent-impl-{mid}").split()
            if R["lang"] == "go":
                pk = sorted({"./" + str(Path(f).parent) if str(Path(f).parent) != "." else "." for f in changed if f.endswith(".go")})
                if not pk:
                    continue
            else:
                if not any(f.endswith(".rs") for f in changed):
                    continue
                pk = []
            s, e = run_pkgs(start, repos, rid, pk), run_pkgs(end, repos, rid, pk)
            c = FT.clean(FT.norm(s), FT.norm(e))
            sel_files = [f for sh in m["repos"].get(rid, {}).get("commits", []) for f in git(repos[rid]["local"], "show", "--name-only", "--format=", sh).split()]
            if R["lang"] == "go":
                sel_pk = {("github.com/livekit/livekit-server/" if rid == "livekit" else "github.com/livekit/protocol/") + str(Path(f).parent) for f in sel_files if f.endswith(".go")}
                keep_req = lambda t: any(t.startswith(p + "/") for p in sel_pk)
                for k in ("fail_to_pass", "none_to_pass"):
                    c["dropped_unstable"] += [t for t in c[k] if not keep_req(t)]
                    c[k] = [t for t in c[k] if keep_req(t)]
            (out / f"{mid}_{rid}.json").write_text(json.dumps({k: c[k] for k in ("fail_to_pass", "none_to_pass", "pass_to_pass")}, indent=1))
            summary[f"{mid}/{rid}"] = {k: len(v) for k, v in c.items()}
            print(mid, rid, summary[f"{mid}/{rid}"], flush=True)
        release(repos, start); release(repos, end)
        prev = f"agent-impl-{mid}"
    (out / "summary.json").write_text(json.dumps(summary, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "tests"])
    ap.add_argument("--config", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--out")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    build(a) if a.cmd == "build" else derive(a)


if __name__ == "__main__":
    main()
