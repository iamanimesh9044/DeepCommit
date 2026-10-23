"""Link commits in dependent repos to commits in a shared contract repo.

Evidence per (contract commit C, dependent commit D):
  explicit  D's message cites C's PR number ("protocol#1841", ".../protocol/pull/1841")
  ident     D's hand-written added lines use identifiers that C added in the contract
  pin       D bumps the contract pin (go.mod, submodule) over a range that contains C

Output (in --out): nodes.csv, edges.csv (explicit/ident), edges_pin_only.csv.
"""
import argparse
import collections
import csv
import re
import subprocess
from pathlib import Path

import yaml

TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{7,}")
PROTO_DEF = re.compile(r"^\s*(?:message|enum|rpc|service)\s+(\w+)")
PROTO_FIELD = re.compile(r"^\s*(?:optional\s+|repeated\s+)?[\w.]+\s+(\w+)\s*=\s*\d+")
PR_NUM = re.compile(r"\(#(\d+)\)\s*$")
MIN_ID = 10


def git(path, *args):
    r = subprocess.run(["git", "-c", "core.commitGraph=false", "-C", str(path), *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def norm(s):
    return s.replace("_", "").lower()


def scan(path, since, until, gen, paths=None):
    """Return commits (newest first) with per-commit added tokens and pin lines from one `git log -p`."""
    fmt = "--format=@@C%x1f%H%x1f%cs%x1f%s%x1f%b%x1e"
    cmd = ["log", "--no-merges", "-p", "-U0", f"--since={since}", f"--until={until}", fmt, "origin/HEAD"]
    if paths:
        cmd += ["--", *paths]
    out = git(path, *cmd)
    commits, cur, f = [], None, None
    for ln in out.splitlines():
        if ln.startswith("@@C\x1f"):
            _, h, d, s, b = (ln.split("\x1f") + [""])[:5]
            cur = {"sha": h, "date": d, "subject": s, "body": b, "files": set(), "hand_files": set(),
                   "tokens": set(), "pin_new": [], "pin_old": [], "proto_ids": set(), "hand_add": 0}
            commits.append(cur)
            f = None
        elif cur is None:
            continue
        elif ln.startswith("diff --git "):
            f = ln.split(" b/", 1)[-1]
            cur["files"].add(f)
            if not any(g.search(f) for g in gen):
                cur["hand_files"].add(f)
        elif ln.startswith("+++") or ln.startswith("---"):
            continue
        elif ln.startswith("+") or ln.startswith("-"):
            sign, txt = ln[0], ln[1:]
            cur.setdefault("lines", []).append((f, sign, txt))
            if f and f in cur["hand_files"] and sign == "+":
                cur["hand_add"] += 1
                cur["tokens"].update(norm(t) for t in TOKEN.findall(txt))
        else:
            cur["body"] += "\n" + ln if not ln.startswith("@@") else ""
    return commits


def proto_ids(commit):
    ids = set()
    for f, sign, txt in commit.get("lines", []):
        if sign == "+" and f and f.endswith(".proto"):
            for rx in (PROTO_DEF, PROTO_FIELD):
                m = rx.match(txt)
                if m:
                    ids.add(norm(m.group(1)))
    return {i for i in ids if len(i) >= MIN_ID}


def pins(commit, pin):
    """Return (old_ref, new_ref) strings or None if this commit does not touch the pin."""
    old = new = None
    for f, sign, txt in commit.get("lines", []):
        if pin["kind"] == "gomod" and f == pin["path"] and pin["module"] in txt:
            m = re.search(re.escape(pin["module"]) + r"\s+(v[\w.\-+]+)", txt)
            if m:
                ref = m.group(1)
                ref = ref.rsplit("-", 1)[-1] if re.search(r"-\d{14}-", ref) else ref
                if sign == "+": new = ref
                else: old = ref
        elif pin["kind"] == "submodule" and f == pin["path"] and "Subproject commit" in txt:
            sha = txt.split()[-1]
            if sign == "+": new = sha
            else: old = sha
    return (old, new) if new else None


def rev_range(contract, old, new):
    def sha(r):
        return git(contract, "rev-parse", "--verify", "-q", f"{r}^{{commit}}").strip()
    n = sha(new)
    if not n:
        return set()
    o = sha(old) if old else ""
    out = git(contract, "rev-list", f"{o}..{n}" if o else n, "--max-count=500")
    return set(out.split())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    since, until = cfg["window"]["since"], cfg["window"]["until"]
    gen = [re.compile(g) for g in cfg["generated"]]
    c = cfg["contract"]
    cc = scan(c["local"], since, until, gen)
    for x in cc:
        x["proto_ids"] = proto_ids(x)
        m = PR_NUM.search(x["subject"])
        x["pr"] = m.group(1) if m else ""
    # identifiers added by several contract commits are too generic to link on
    freq = collections.Counter(i for x in cc for i in x["proto_ids"])
    by_pr = {x["pr"]: x for x in cc if x["pr"]}
    nodes, edges, pin_only = [], [], []
    for x in cc:
        nodes.append([f"{c['id']}@{x['sha'][:8]}", c["id"], x["date"], x["subject"][:100], x["pr"], len(x["hand_files"]), x["hand_add"], len(x["proto_ids"]), ";".join(sorted(x["hand_files"]))])
    for d in cfg["dependents"]:
        dc = scan(d["local"], since, until, gen)
        for y in dc:
            nid = f"{d['id']}@{y['sha'][:8]}"
            nodes.append([nid, d["id"], y["date"], y["subject"][:100], (PR_NUM.search(y["subject"]) or [None, ""])[1], len(y["hand_files"]), y["hand_add"], 0, ";".join(sorted(y["hand_files"]))])
            msg = y["subject"] + "\n" + y["body"]
            cited = set(re.findall(r"protocol(?:#|/pull/)(\d+)", msg))
            p = pins(y, d["pin"])
            window = rev_range(c["local"], *p) if p else set()
            for x in cc:
                if y["date"] < x["date"]:
                    continue
                ev = []
                if x["pr"] and x["pr"] in cited:
                    ev.append("explicit")
                hits = sorted(i for i in x["proto_ids"] if freq[i] == 1 and i in y["tokens"])
                if hits:
                    ev.append("ident")
                inpin = x["sha"] in window
                if inpin:
                    ev.append("pin")
                if not ev:
                    continue
                row = [f"{c['id']}@{x['sha'][:8]}", nid, "FUNC", "", "+".join(ev), ";".join(hits[:6])]
                if "explicit" in ev or len(hits) >= 2:
                    row[3] = "Strong"
                elif hits:
                    row[3] = "Weak"
                (edges if hits or "explicit" in ev else pin_only).append(row)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    for name, hdr, rows in (("nodes.csv", ["id", "repo", "date", "subject", "pr", "hand_files", "hand_added_lines", "new_proto_ids", "files"], nodes),
                            ("edges.csv", ["source_id", "target_id", "type", "strength", "evidence", "identifiers"], edges),
                            ("edges_pin_only.csv", ["source_id", "target_id", "type", "strength", "evidence", "identifiers"], pin_only)):
        with open(out / name, "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(hdr); w.writerows(rows)
    print(f"nodes={len(nodes)} edges={len(edges)} (strong={sum(r[3]=='Strong' for r in edges)}) pin_only={len(pin_only)} -> {out}")


if __name__ == "__main__":
    main()
