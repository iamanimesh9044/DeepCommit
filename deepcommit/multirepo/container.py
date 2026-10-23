"""Docker flow for the multi-repo task.

  patches   export per-milestone context and gold patches from a built trunk workspace
  build     build the base image and the agent image (repos reduced to one base commit)
  run       start a container and drive the queue: unlock -> (apply gold) -> submit -> evaluate
            mode oracle applies the gold patch for each milestone; nop submits unchanged

Context patches (upstream commits that come before a milestone) are applied at unlock time, so the agent never
has access to history that is later than its current milestone. Gold patches never enter the image.
"""
import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

from deepcommit.multirepo.e2e import git, load, parse_go, parse_rust

DOCKER = Path(__file__).resolve().parents[2] / "docker"
REPOS = ("protocol", "livekit", "rust-sdks")


def sh(*a, check=True, inp=None, timeout=None):
    r = subprocess.run(a, capture_output=True, input=inp, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(map(str, a))[:200]}\n{r.stderr.decode()[-1500:]}{r.stdout.decode()[-500:]}")
    return r


def patches(a):
    ws = Path(a.workspace)
    t = json.loads((ws / "trunk.json").read_text())
    out = Path(a.task) / "harness"; shutil.rmtree(out, ignore_errors=True)
    prev = {r: "agent-impl-BASE" for r in REPOS}
    for m in t["order"]:
        for r in REPOS:
            for kind, x, y in (("ctx", prev[r], f"ctx-{m}"), ("gold", f"ctx-{m}", f"agent-impl-{m}")):
                d = sh("git", "-c", "core.commitGraph=false", "-C", str(ws / r), "diff", "--binary", x, y).stdout
                if d:
                    (out / kind).mkdir(parents=True, exist_ok=True)
                    (out / kind / f"{m}_{r}.patch").write_bytes(d)
            prev[r] = f"agent-impl-{m}"
    print("patches ->", out, sum(f.stat().st_size for f in out.rglob("*.patch")) // 1024, "KB")


def export_deps(ws, ctx, repos, order):
    """Copy the dependency manifests of every milestone state into the build context."""
    tags = ["agent-impl-BASE"] + [t for m in order for t in (f"ctx-{m}", f"agent-impl-{m}")]
    for r in ("protocol", "livekit"):
        for t in tags:
            d = ctx / "deps" / "go" / t / r; d.mkdir(parents=True)
            for f in ("go.mod", "go.sum"):
                (d / f).write_bytes(sh("git", "-c", "core.commitGraph=false", "-C", str(ws / r), "show", f"{t}:{f}").stdout)
    for name, tag in (("rust-base", "agent-impl-BASE"), ("rust-final", f"agent-impl-{order[-1]}")):
        # the last milestone to touch rust-sdks may not be last overall, so use the latest tag where it changed
        pass
    last_rust = [m for m in order if sh("git", "-c", "core.commitGraph=false", "-C", str(ws / "rust-sdks"), "diff", "--name-only", "agent-impl-BASE", f"agent-impl-{m}").stdout.strip()][-1]
    for name, tag in (("rust-base", "agent-impl-BASE"), ("rust-final", f"agent-impl-{last_rust}")):
        d = ctx / "deps" / name; d.mkdir(parents=True)
        p = subprocess.Popen(["git", "-c", "core.commitGraph=false", "-C", str(ws / "rust-sdks"), "archive", tag], stdout=subprocess.PIPE)
        subprocess.run(["tar", "-x", "-C", str(d)], stdin=p.stdout, check=True); p.wait()
        link = git(ws / "rust-sdks", "ls-tree", tag, "livekit-protocol/protocol").split()
        if len(link) >= 3:
            q = d / "livekit-protocol" / "protocol"; q.mkdir(parents=True, exist_ok=True)
            p = subprocess.Popen(["git", "-c", "core.commitGraph=false", "-C", repos["protocol"]["local"], "archive", link[2]], stdout=subprocess.PIPE)
            subprocess.run(["tar", "-x", "-C", str(q)], stdin=p.stdout, check=True); p.wait()


def build(a):
    cfg, repos, man = load(a)
    ws = Path(a.workspace); ctx = Path(a.ctx)
    shutil.rmtree(ctx, ignore_errors=True)
    for r in REPOS:
        d = ctx / "repos" / r; d.mkdir(parents=True)
        p = subprocess.Popen(["git", "-c", "core.commitGraph=false", "-C", str(ws / r), "archive", "agent-impl-BASE"], stdout=subprocess.PIPE)
        subprocess.run(["tar", "-x", "-C", str(d)], stdin=p.stdout, check=True); p.wait()
    link = git(ws / "rust-sdks", "ls-tree", "agent-impl-BASE", "livekit-protocol/protocol").split()
    if len(link) >= 3:  # submodule: export the pinned protocol tree into the superproject
        d = ctx / "repos" / "rust-sdks" / "livekit-protocol" / "protocol"; d.mkdir(parents=True, exist_ok=True)
        p = subprocess.Popen(["git", "-c", "core.commitGraph=false", "-C", repos["protocol"]["local"], "archive", link[2]], stdout=subprocess.PIPE)
        subprocess.run(["tar", "-x", "-C", str(d)], stdin=p.stdout, check=True); p.wait()
    export_deps(ws, ctx, repos, json.loads((ws / "trunk.json").read_text())["order"])
    (ctx / "go.work").write_text((ws / "go.work").read_text())
    (ctx / "submit.sh").write_text((ws / "submit.sh").read_text())
    shutil.copy(Path(a.task) / "prompt.md", ctx / "prompt.md")
    (ctx / "TASK_QUEUE.md").write_text("# Task queue\n\n(No tasks currently available)\n")
    shutil.copy(DOCKER / "agent" / "Dockerfile", ctx / "Dockerfile")
    print(sh("docker", "build", "-q", "-t", "csf-livekit-base", str(DOCKER / "base")).stdout.decode().strip())
    print(sh("docker", "build", "-q", "-t", "csf-livekit-agent", str(ctx)).stdout.decode().strip())


def ex(c, cmd, inp=None, check=True, timeout=None):
    return sh("docker", "exec", "-i", c, "sh", "-c", cmd, check=check, inp=inp, timeout=timeout)


def unlock(c, task, mid, title):
    for r in REPOS:
        p = Path(task) / "harness" / "ctx" / f"{mid}_{r}.patch"
        if p.exists():
            data = p.read_bytes()
            res = ex(c, f"cd /workspace/{r} && git apply --whitespace=nowarn -", inp=data, check=False)
            if res.returncode:  # the agent's tree differs from gold: fall back to a three-way merge
                res = ex(c, f"cd /workspace/{r} && git apply --3way --whitespace=nowarn -", inp=data, check=False)
            if res.returncode:
                raise RuntimeError(f"unlock {mid}/{r}: {res.stderr.decode()[-600:]}")
    sh("docker", "cp", str(Path(task) / "srs" / mid), f"{c}:/workspace/srs/{mid}")
    ex(c, f"grep -q 'No tasks' /workspace/TASK_QUEUE.md && printf '# Task queue\\n\\n' > /workspace/TASK_QUEUE.md; printf -- '- {mid}: {title} (srs/{mid}/SRS.md)\\n' >> /workspace/TASK_QUEUE.md")


def evaluate(c, a, repos, mid):
    task = Path(a.task)
    ex(c, f"rm -rf /eval/{mid} && mkdir -p /eval/{mid} && cp /workspace/go.work /workspace/go.work.sum /eval/{mid}/ && " + " && ".join(
        f"git -C /workspace/{r} worktree add --detach /eval/{mid}/{r} agent-impl-{mid} -f >/dev/null 2>&1" for r in REPOS))
    res = {"id": mid, "repos": {}}; req_all = fixed_all = broken_all = 0
    for rid in REPOS:
        f = task / a.tests_dir / f"{mid}_{rid}.json"
        if not f.exists():
            continue
        t = json.loads(f.read_text()); required, keep = t["fail_to_pass"] + t["none_to_pass"], t["pass_to_pass"]
        if repos[rid]["lang"] == "go":
            prefix = {"livekit": "github.com/livekit/livekit-server/", "protocol": "github.com/livekit/protocol/"}[rid]
            pk = sorted({"./" + (x[:x.find("/Test")] if "/Test" in x else x.rsplit("/", 1)[0])[len(prefix):] for x in required + keep})
            out = ex(c, f"cd /eval/{mid}/{rid} && go test -json -count=1 -timeout 20m {' '.join(pk)} 2>&1", check=False, timeout=3000).stdout.decode()
            st = parse_go(out)
        else:
            cr = repos[rid]["test_crates"]
            out = ex(c, f"cd /eval/{mid}/{rid} && cargo test {' '.join('-p ' + x for x in cr)} --no-fail-fast 2>&1", check=False, timeout=4500).stdout.decode()
            st = {re.sub(r"^.*/debug/deps/", "/target/debug/deps/", re.sub(r"-[0-9a-f]{16}\b", "", k)): v for k, v in parse_rust(out, cr).items()}
        fixed = [x for x in required if st.get(x) == "passed"]; broken = [x for x in keep if st.get(x) != "passed"]
        res["repos"][rid] = {"required": len(required), "fixed": len(fixed), "broken": len(broken), "broken_examples": broken[:3]}
        req_all += len(required); fixed_all += len(fixed); broken_all += len(broken)
    ex(c, f"for r in {' '.join(REPOS)}; do git -C /workspace/$r worktree remove --force /eval/{mid}/$r; done; rm -rf /eval/{mid}", check=False)
    rec = fixed_all / req_all if req_all else 0.0; prec = (fixed_all + 1) / (fixed_all + broken_all + 1)
    res |= {"recall": round(rec, 3), "precision": round(prec, 3), "score": round(2 * rec * prec / (rec + prec), 3) if rec + prec else 0.0,
            "resolved": bool(req_all) and fixed_all == req_all and broken_all == 0}
    return res


def run(a):
    cfg, repos, man = load(a)
    task = Path(a.task)
    order = json.loads((task / "trunk.json").read_text())["order"]
    c = f"csf-agent-{a.mode}"
    sh("docker", "rm", "-f", c, check=False)
    sh("docker", "run", "-d", "--name", c, *(["--network", "none"] if a.offline else []), "-v", "csf-target:/cache/target", "-v", "csf-gobuild:/root/.cache/go-build", "csf-livekit-agent", "sleep", "infinity")
    rows = []
    try:
        for mid in order:
            m = next(x for x in man["milestones"] if x["id"] == mid)
            unlock(c, task, mid, m["title"])
            def apply_gold(commit_only=False):
                for r in REPOS:
                    p = task / "harness" / "gold" / f"{mid}_{r}.patch"
                    if p.exists():
                        ex(c, f"cd /workspace/{r} && git apply --whitespace=nowarn -", inp=p.read_bytes())
                if commit_only:  # keep the gold prefix in the tree without tagging it
                    ex(c, "for r in protocol livekit rust-sdks; do git -C /workspace/$r add -A && git -C /workspace/$r commit -q --allow-empty -m gold-prefix; done")

            if a.mode == "nop" and m["graded"]:
                ex(c, f"cd /workspace && ./submit.sh {mid}")          # submit the milestone with nothing implemented
                r = evaluate(c, a, repos, mid); rows.append(r)
                apply_gold(commit_only=True)                          # then advance the tree so later patches apply
            else:
                apply_gold()
                ex(c, f"cd /workspace && ./submit.sh {mid}")
                if m["graded"]:
                    r = evaluate(c, a, repos, mid); rows.append(r)
            if m["graded"]:
                print(mid, {k: r[k] for k in ("recall", "precision", "score", "resolved")}, {k: (v["fixed"], v["required"], v["broken"]) for k, v in r["repos"].items()}, flush=True)
            else:
                print(mid, "non-graded: unlocked, submitted", flush=True)
    finally:
        if not a.keep:
            sh("docker", "rm", "-f", c, check=False)
    if rows:
        print(f"{a.mode}: mean score {sum(r['score'] for r in rows)/len(rows):.3f}, resolved {sum(r['resolved'] for r in rows)}/{len(rows)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["patches", "build", "run"])
    ap.add_argument("--config", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--workspace")
    ap.add_argument("--ctx")
    ap.add_argument("--mode", choices=["oracle", "nop"], default="oracle")
    ap.add_argument("--tests-dir", dest="tests_dir", default="e2e_tests")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--offline", action="store_true", help="run the container with no network (proves dependencies are baked in)")
    a = ap.parse_args()
    {"patches": patches, "build": build, "run": run}[a.cmd](a)


if __name__ == "__main__":
    main()
