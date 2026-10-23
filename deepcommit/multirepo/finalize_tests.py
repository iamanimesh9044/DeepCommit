"""Turn raw start/end test results into per-milestone F2P / N2P / P2P sets.

Cleaning applied (same spirit as DeepCommit's filter list):
  - Rust binary hashes are stripped from test ids (`livekit_api-2bd9...` -> `livekit_api`).
  - A test that only appears on one side AND with an ancestor test that passes on both sides is an unstable
    subtest name (map-order or random names), so it is dropped.
"""
import argparse
import json
import re
from pathlib import Path

HASH = re.compile(r"-[0-9a-f]{16}\b")
INDEXED = re.compile(r"/\d+/[^/]+$")   # map-order subtests such as Test/9/name, whose names change between runs


def norm(d):
    return {HASH.sub("", k): v for k, v in d.items()}


def clean(start, end):
    both_pass = {t for t in start if start[t] == "passed" and end.get(t) == "passed"}
    b = {"fail_to_pass": [], "none_to_pass": [], "pass_to_pass": [], "dropped_unstable": [], "fail_to_fail": []}
    for t in sorted(set(start) | set(end)):
        s, e = start.get(t), end.get(t)
        parts = t.split("/")
        anc = ["/".join(parts[:i]) for i in range(1, len(parts))]
        one_sided = (s is None) != (e is None)
        if INDEXED.search(t):
            b["dropped_unstable"].append(t)
        elif one_sided and any(x in both_pass for x in anc):
            b["dropped_unstable"].append(t)
        elif s == "passed" and e == "passed": b["pass_to_pass"].append(t)
        elif s == "failed" and e == "passed": b["fail_to_pass"].append(t)
        elif s is None and e == "passed": b["none_to_pass"].append(t)
        elif s == "failed" and e == "failed": b["fail_to_fail"].append(t)
    return b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    summary = {}
    for mdir in sorted(Path(a.tests).iterdir()):
        for rdir in sorted(p for p in mdir.iterdir() if p.is_dir()):
            s, e = norm(json.loads((rdir / "start.json").read_text())), norm(json.loads((rdir / "end.json").read_text()))
            c = clean(s, e)
            (out / f"{mdir.name}_{rdir.name}.json").write_text(json.dumps(c, indent=1))
            summary[f"{mdir.name}/{rdir.name}"] = {k: len(v) for k, v in c.items()} | {"start_total": len(s), "end_total": len(e)}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    for k, v in summary.items():
        print(f"{k:16} F2P={v['fail_to_pass']:<3} N2P={v['none_to_pass']:<4} P2P={v['pass_to_pass']:<4} F2F={v['fail_to_fail']:<2} dropped={v['dropped_unstable']:<3} (start {v['start_total']}, end {v['end_total']})")


if __name__ == "__main__":
    main()
