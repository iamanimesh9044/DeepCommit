import json
import re
import sys
from pathlib import Path

import tomllib

root = Path(sys.argv[1])  # cross-repo-change
out = root / "index.html"
MAXLINES = 600


def split_patch(text):
    files, cur = [], None
    for ln in text.splitlines():
        if ln.startswith("diff --git"):
            cur = {"path": ln.split(" b/", 1)[-1], "lines": [], "add": 0, "del": 0}
            files.append(cur)
        elif cur is not None:
            if ln.startswith("+") and not ln.startswith("+++"):
                cur["add"] += 1
            elif ln.startswith("-") and not ln.startswith("---"):
                cur["del"] += 1
            cur["lines"].append(ln)
    for f in files:
        if len(f["lines"]) > MAXLINES:
            f["truncated"] = len(f["lines"]) - MAXLINES
            f["lines"] = f["lines"][:MAXLINES]
    return files


tasks = []
for tdir in sorted(p for p in root.iterdir() if (p / "task.toml").exists()):
    cfg = tomllib.loads((tdir / "task.toml").read_text())
    md = cfg["metadata"]
    steps = []
    for st in cfg["steps"]:
        sd = tdir / "steps" / st["name"]
        ids = json.loads((sd / "tests/ids.json").read_text())
        instr = (sd / "instruction.md").read_text()
        body, _, env = instr.partition("\n---\n")
        gold = {
            p.stem: split_patch(p.read_text(errors="replace")) for p in sorted((sd / "solution/gold").glob("*.patch"))
        }
        own = {}
        for line in (sd / "tests/step.env").read_text().splitlines():
            m = re.match(r"F2P_([A-Z_]+)='(.*)'", line)
            if m and m.group(2).strip():
                own[m.group(1).lower().replace("_", "-")] = m.group(2).split()
        tests = []
        for repo, fs in own.items():
            for f in fs:
                p = sd / "tests/hidden" / repo / f
                if p.is_file():
                    t = p.read_text(errors="replace").splitlines()
                    tests.append(
                        {"repo": repo, "path": f, "text": "\n".join(t[:MAXLINES]), "more": max(0, len(t) - MAXLINES)}
                    )
        steps.append(
            {
                "name": st["name"],
                "instruction": body.strip(),
                "env": env.strip(),
                "gold": gold,
                "f2p": ids["f2p"],
                "p2p_n": len(ids["p2p"]),
                "tests": tests,
            }
        )
    tasks.append(
        {
            "name": cfg["task"]["name"],
            "dag": md.get("csf_milestone_dag", ""),
            "repos": md.get("source_repos", []),
            "readme": (tdir / "README.md").read_text() if (tdir / "README.md").exists() else "",
            "steps": steps,
        }
    )

data = json.dumps(tasks).replace("</", "<\\/")
html = (Path(__file__).parent / "viewer.html").read_text().replace("/*DATA*/", data)
out.write_text(html)
print(out, len(html) // 1024, "KB")
