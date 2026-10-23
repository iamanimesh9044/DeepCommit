"""Group cross-repo links into milestones (one contract commit + the dependents that adapt to it).

Reads nodes.csv / edges.csv from link_prs.py. Each dependent commit joins exactly one milestone
(the contract commit it shares the most identifiers with), like SWE-Milestone's "one commit, one milestone".

Writes milestones.csv and dependencies.csv in the same columns DeepCommit uses, with repo-prefixed commit ids.
"""
import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

NOISE = re.compile(r"^(release|version packages|chore|bump|deps?[:(]|fix\(deps\)|update .*digest|merge )|release (v|packages)|^v?\d+\.\d+", re.I)
MANIFEST = re.compile(r"(^|/)(go\.mod|go\.sum|Cargo\.toml|Cargo\.lock|package\.json|pnpm-lock\.yaml)$|^\.changeset/")
HUB_FILES = 40       # dependent commits touching more hand-written files than this are flagged oversize


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--links", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-ident", type=int, default=2, help="min shared identifiers for a Weak edge to count")
    a = ap.parse_args()
    L = Path(a.links)
    nodes = {r["id"]: r for r in csv.DictReader(open(L / "nodes.csv"))}
    edges = list(csv.DictReader(open(L / "edges.csv")))

    def usable(n):
        return not NOISE.search(n["subject"]) and int(n["hand_added_lines"]) > 0

    cand = defaultdict(list)       # dependent id -> [(score, contract id, edge)]
    for e in edges:
        src, dst = nodes[e["source_id"]], nodes[e["target_id"]]
        if not (usable(src) and usable(dst)):
            continue
        ids = [i for i in e["identifiers"].split(";") if i]
        explicit = "explicit" in e["evidence"]
        if e["strength"] != "Strong" and len(ids) < a.min_ident and not explicit:
            continue
        cand[dst["id"]].append((len(ids) + (10 if explicit else 0), src["id"], e))

    members = defaultdict(list)    # contract id -> [dependent ids]
    for dep, lst in cand.items():
        lst.sort(key=lambda t: -t[0])
        members[lst[0][1]].append((dep, lst[0][0]))

    rows, mid = [], {}
    for k, (cid, deps) in enumerate(sorted(members.items(), key=lambda kv: nodes[kv[0]]["date"]), 1):
        repos = sorted({nodes[d]["repo"] for d, _ in deps})
        if len(repos) < 1:
            continue
        m = f"X{k:03d}"
        mid[cid] = m
        commits = [cid] + [d for d, _ in sorted(deps, key=lambda t: nodes[t[0]]["date"])]
        big = [d for d, _ in deps if int(nodes[d]["hand_files"]) > HUB_FILES]
        rows.append({
            "id": m, "title": nodes[cid]["subject"], "contract_commit": cid, "repos": "+".join([nodes[cid]["repo"]] + repos),
            "n_dependent_repos": len(repos), "commits": ";".join(commits),
            "titles": " || ".join(nodes[c]["subject"][:60] for c in commits[1:]),
            "graded_candidate": "", "src_loc": sum(int(nodes[c]["hand_added_lines"]) for c in commits),
            "oversize": ";".join(big), "start_time": min(nodes[c]["date"] for c in commits), "end_time": max(nodes[c]["date"] for c in commits),
        })
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    with open(out / "milestones.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

    # milestone -> milestone edges: a later milestone edits files an earlier one already changed
    files = {m: {f"{nodes[c]['repo']}:{f}" for c in r["commits"].split(";") for f in nodes[c]["files"].split(";") if f and not MANIFEST.search(f)}
             for m, r in ((x["id"], x) for x in rows)}
    order = sorted(rows, key=lambda r: (r["start_time"], r["id"]))
    deps_rows = []
    for i, later in enumerate(order):
        for earlier in order[:i]:
            shared = sorted(files[earlier["id"]] & files[later["id"]])
            if shared:
                deps_rows.append([earlier["id"], later["id"], "TEXT", "Strong" if len(shared) >= 2 else "Weak",
                                  f"both edit {', '.join(shared[:3])}", "0.7"])
    with open(out / "dependencies.csv", "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["source_id", "target_id", "type", "strength", "rationale", "confidence_score"]); w.writerows(deps_rows)
    print(f"milestones={len(rows)} cross-language={sum(r['n_dependent_repos']>1 for r in rows)} deps={len(deps_rows)} -> {out}")


if __name__ == "__main__":
    main()
