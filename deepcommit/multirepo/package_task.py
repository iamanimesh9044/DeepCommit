"""Package a multi-repo task bundle: manifest, milestones, dependencies, cleaned test sets."""
import argparse
import csv
import json
import shutil
import subprocess
from pathlib import Path

import yaml


def git(path, *a):
    return subprocess.run(["git", "-c", "core.commitGraph=false", "-C", str(path), *a], capture_output=True, text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--ms", required=True, help="dir with milestones.csv / dependencies.csv")
    ap.add_argument("--final", required=True, help="dir with finalize_tests output")
    ap.add_argument("--ids", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    repos = {cfg["contract"]["id"]: {**cfg["contract"], "lang": "go"}, **{d["id"]: d for d in cfg["dependents"]}}
    out = Path(a.out); (out / "tests").mkdir(parents=True, exist_ok=True)
    rows = {r["id"]: r for r in csv.DictReader(open(Path(a.ms) / "milestones.csv"))}
    manifest = {"name": cfg["name"], "window": cfg["window"], "repos": {k: {"repo": v["repo"], "lang": v["lang"]} for k, v in repos.items()}, "milestones": []}
    kept = []
    for mid in a.ids:
        r = rows[mid]; by = {}
        for c in r["commits"].split(";"):
            rid, sha = c.split("@"); by.setdefault(rid, []).append(sha)
        entry = {"id": mid, "title": r["title"], "repos": {}, "graded": False, "tests": {}}
        for rid, shorts in by.items():
            path = repos[rid]["local"]
            full = sorted((git(path, "rev-parse", s) for s in shorts), key=lambda s: int(git(path, "show", "-s", "--format=%ct", s)))
            entry["repos"][rid] = {"start": git(path, "rev-parse", full[0] + "^"), "commits": full, "subjects": [git(path, "show", "-s", "--format=%s", s) for s in full]}
            f = Path(a.final) / f"{mid}_{rid}.json"
            if f.exists():
                t = json.loads(f.read_text())
                (out / "tests" / f"{mid}_{rid}.json").write_text(json.dumps({k: t[k] for k in ("fail_to_pass", "none_to_pass", "pass_to_pass")}, indent=1))
                entry["tests"][rid] = {k: len(t[k]) for k in ("fail_to_pass", "none_to_pass", "pass_to_pass")}
                if t["fail_to_pass"] or t["none_to_pass"]:
                    entry["graded"] = True
        manifest["milestones"].append(entry)
        kept.append(r)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    with open(out / "milestones.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(kept[0])); w.writeheader(); w.writerows(kept)
    with open(Path(a.ms) / "dependencies.csv") as fh, open(out / "dependencies.csv", "w", newline="") as oh:
        rd = csv.reader(fh); w = csv.writer(oh); w.writerow(next(rd))
        w.writerows(r for r in rd if r[0] in a.ids and r[1] in a.ids)
    for m in manifest["milestones"]:
        print(m["id"], "graded" if m["graded"] else "NON-GRADED", m["tests"])


if __name__ == "__main__":
    main()
