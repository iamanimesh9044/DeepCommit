"""Write the README.md of a measured and curated CSF multi-repo task.

  python -m deepcommit.export.readme TASK_DIR --report export_report.json --folded measure_folded.json [--gates gates.json]

Reads task.toml, every step's tests/suite.json and solution/gold.patch, and the exporter's report (milestone of each
original step). ``--gates`` (a JSON {step: {"oracle": r, "nop": r}}) fills the generated Gates section.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import tomllib


def _patch_lines(path: Path) -> dict:
    """Changed lines per repo of a gold.patch rooted at /app (paths <repo>/...)."""
    out, repo = {}, None
    for ln in path.read_text(errors="replace").splitlines():
        if ln.startswith("diff --git a/"):
            repo = ln[len("diff --git a/"):].split("/", 1)[0]
            out.setdefault(repo, 0)
        elif repo and ln[:1] in "+-" and ln[:3] not in ("+++", "---"):
            out[repo] += 1
    return out


def build(task: Path, report: dict, folded: dict, gates: dict | None = None) -> str:
    cfg = tomllib.loads((task / "task.toml").read_text())
    md = cfg["metadata"]
    mid_of = {s["name"]: s["mid"] for s in report["steps"] if s.get("name")}
    rows, gate_rows, built = [], [], set()
    for st in cfg["steps"]:
        name = st["name"]
        sd = task / "steps" / name
        suite = json.loads((sd / "tests/suite.json").read_text())
        checks = suite.get("checks", [])
        ids = {b: [c["id"] for c in checks if c["bucket"] == b] for b in ("f2p", "p2p")}
        lines = _patch_lines(sd / "solution/gold.patch")
        ms = [mid_of.get(n, "?") for g in folded["steps"] if g["step"] == name for n in g.get("members", [])]
        built.update(ms)
        f2p_repos = sorted({i.split("::", 1)[0] for i in ids["f2p"]})
        rows.append(
            f"| {name} | {', '.join(ms) or '-'} | {', '.join(f'{r} ({n} lines)' for r, n in lines.items())} "
            f"| {len(ids['f2p'])} ({', '.join(f2p_repos)}) | {len(ids['p2p'])} |"
        )
        gate_rows.append(
            f"| {name} | {gates[name].get('oracle', '?')} | {gates[name].get('nop', '?')} |"
            if gates and name in gates
            else f"| {name} | not run | not run |"
        )
    background = [s for s in report["steps"] if s["status"] == "background"]
    notbuilt = [s for s in report["steps"] if s["status"] in ("blocked", "skipped")]
    srcs = md.get("source_repos", [])
    first = json.loads((task / "steps" / cfg["steps"][0]["name"] / "tests/suite.json").read_text())
    suites = {"suites": first["suites"]}
    services = first.get("services", [])
    contract = first.get("contract_only", {})
    limits_file = task / "LIMITS.md"
    out = [
        f"# {cfg['task']['name'].split('/')[-1]}",
        "",
        "Cross-repo milestone task derived with DeepCommit from the "
        f"{', '.join(r['name'] for r in srcs)} workspace. One agent keeps one environment through the steps below "
        "with no reset between them. After each step Harbor copies the agent's `/app` into a fresh grading stack, which "
        + ("deploys the services with harbor-svc and " if services else "")
        + "runs hidden tests across all repositories. Episode score is the mean over steps.",
        "",
        "## Status",
        "",
        "| Step | Milestones | Gold patch | F2P (repos) | P2P |",
        "| --- | --- | --- | --- | --- |",
        *rows,
        "",
        f"DAG: `{md.get('csf_milestone_dag', '')}`",
        "",
        "A step is the smallest run of consecutive milestones that has at least one hidden test going from failing to passing "
        "across its gold patch. Milestones that cannot be graded alone (for example a change that leaves the checkout "
        "unimportable until generated stubs land in a later milestone) are folded into the next step.",
        "",
        f"Milestones in steps: {len(built)}. Milestones whose commits are background in the base state "
        f"(no hidden tests of their own): {len(background)}. Not built: "
        + (", ".join(f"{s['mid']} ({s['status']})" for s in notbuilt) or "none")
        + ".",
        "",
        "## Repositories",
        "",
        "| Repo | Role | Upstream | Base commit |",
        "| --- | --- | --- | --- |",
        *[
            f"| {r['name']} | {md['repo_roles'].get(r['name'], '')}"
            f"{' (contract only)' if r['name'] in md.get('contract_only_repos', []) else ''} | {r['url']} | `{r['commit'][:12]}` |"
            for r in srcs
        ],
        "",
        *[
            f"`/app/{n}` is a contract-only repository, sparse: only {', '.join('`' + p + '`' for p in c.get('contract_paths', []))} "
            "are present. It has no tests and nothing to build; contract edits are graded with the rest of `/app`."
            for n, c in contract.items()
        ],
        "",
        "## Services",
        "",
        *(
            ["| Service | Kind | Health (from main) |", "| --- | --- | --- |"]
            + [f"| `{sv['name']}` | {sv.get('kind', 'app')} | `{sv['health']}` |" for sv in services]
            + ["", "App services are deployed by Harbor from `/app/<svc>/csf-service.toml` into the runtime images of "
               "`environment/runtimes/` (when the agent's stack starts, on `harbor-svc deploy`, and by `test.sh` in each "
               "grading stack). The agent manages them with `harbor-svc`.", ""]
            if services
            else ["No services: the repositories are graded by their test suites alone.", ""]
        ),
        "## How grading works",
        "",
        "1. The task's only artifact is `/app` (build outputs excluded). After each step Harbor copies it out of the agent's "
        "stack into a fresh grading stack built from `environment/` and uploads `steps/<step>/tests/` to `/tests`.",
        "2. `test.sh` resets each repo's test dirs to `/opt/csf/base/<repo>.git` and copies `checks/<repo>/` in, "
        + ("deploys every app service with `harbor-svc deploy` and waits for it (one `deploy` check each), " if services else "")
        + "then runs the suites ("
        + ", ".join(f"{s['parser']} for {s['repo']}" for s in suites["suites"])
        + ").",
        "3. `grade.py` grades exactly the checks in `suite.json` (`deploy`; `f2p`: failed before the gold patch, must pass; "
        "`p2p`: must keep passing). `reward` is 1 only if every check passed.",
        "4. The f2p/p2p ids were measured, not guessed: the gold patches were applied step by step in the task's own stack "
        "and every test was run twice per state (`deepcommit/export/templates/measure.py`, `curate.py`); a test that "
        "failed in any run is excluded. `ledger.json` records every check across steps (never read at grading).",
        "",
        "## Running it",
        "",
        "Needs Harbor with service control (`docs/service-control.md`; multi-step tasks, schema 1.4) and Docker. The "
        "starting code is in `environment/app/`; the build downloads toolchains and packages from their registries, so it "
        "needs network. The trial itself needs none.",
        "",
        "```bash",
        "harbor run -p <this folder> -a oracle        # gold solution: every step must score 1",
        "harbor run -p <this folder> -a nop           # no change: step 1 must score 0",
        "harbor run -p <this folder> -a claude-code -m <model>",
        "```",
        "",
        "## Gates",
        "",
        "GENERATED from the gates run (`readme --gates`).",
        "",
        "| Step | Oracle | NOP |",
        "| --- | --- | --- |",
        *gate_rows,
        "",
        "## Known limits",
        "",
        "- **Cross-repo coupling is weak.** The upstream clients lag the contract: server proto changes reach the clients in "
        "release-sync commits, so within one milestone the contract part and the client part rarely depend on each other. Steps "
        "whose gold touches several repositories are listed above; their hidden tests are each repository's own tests, so no test "
        "checks that two repositories agree with each other.",
        "- Instructions are generated from the gold patch and the hidden tests (`deepcommit/export/gen_instructions.py`), not "
        "from a full Phase 4 SRS.",
        *(
            [f"- {ln.lstrip('- ').strip()}" for ln in limits_file.read_text().splitlines() if ln.strip()]
            if limits_file.exists()
            else []
        ),
        "",
    ]
    return "\n".join(out)


def rename_of(folded: dict, old_name: str) -> str:
    for r in folded["steps"]:
        if old_name in r.get("members", []):
            return r["step"]
    return old_name


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--report", required=True)
    ap.add_argument("--folded", required=True)
    ap.add_argument("--gates", default="")
    a = ap.parse_args(argv)
    task = Path(a.task)
    gates = json.loads(Path(a.gates).read_text()) if a.gates else None
    text = build(task, json.loads(Path(a.report).read_text()), json.loads(Path(a.folded).read_text()), gates)
    (task / "README.md").write_text(text)
    print(task / "README.md")


if __name__ == "__main__":
    main()
