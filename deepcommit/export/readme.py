"""Write the README.md of a measured and curated CSF multi-repo task.

  python -m deepcommit.export.readme TASK_DIR --report export_report.json --folded measure_folded.json [--gates gates.json]

Reads task.toml, every step's tests/ids.json, gold patches, and the exporter's report (milestone of each original step).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import tomllib


def _patch_lines(path: Path) -> int:
    return sum(
        1 for ln in path.read_text(errors="replace").splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---")
    )


def build(task: Path, report: dict, folded: dict, gates: dict | None = None) -> str:
    cfg = tomllib.loads((task / "task.toml").read_text())
    md = cfg["metadata"]
    mid_of = {s["name"]: s["mid"] for s in report["steps"] if s.get("name")}
    rows, built = [], set()
    for st in cfg["steps"]:
        name = st["name"]
        sd = task / "steps" / name
        ids = json.loads((sd / "tests/ids.json").read_text())
        repos = sorted(p.stem for p in (sd / "solution/gold").glob("*.patch"))
        lines = {r: _patch_lines(sd / "solution/gold" / f"{r}.patch") for r in repos}
        ms = [mid_of.get(n, "?") for g in folded["steps"] if g["step"] == name for n in g.get("members", [])]
        built.update(ms)
        f2p_repos = sorted({i.split("::", 1)[0] for i in ids["f2p"]})
        g = ""
        if gates and name in gates:
            g = f"oracle {gates[name].get('oracle', '?')} / nop {gates[name].get('nop', '?')}"
        rows.append(
            f"| {name} | {', '.join(ms) or '-'} | {', '.join(f'{r} ({n} lines)' for r, n in lines.items())} "
            f"| {len(ids['f2p'])} ({', '.join(f2p_repos)}) | {len(ids['p2p'])} | {g} |"
        )
    background = [s for s in report["steps"] if s["status"] == "background"]
    notbuilt = [s for s in report["steps"] if s["status"] in ("blocked", "skipped")]
    srcs = md.get("source_repos", [])
    out = [
        f"# {cfg['task']['name'].split('/')[-1]}",
        "",
        "Cross-repo milestone task derived with DeepCommit from the `qdrant_clients_v1` workspace "
        f"({', '.join(r['name'] for r in srcs)}, v1.18.0 to v1.20.0). One agent keeps one environment through the steps below "
        "with no reset between them. After each step Harbor collects the agent's work, a clean verifier container applies it "
        "to the base state and runs hidden tests across all repositories. Episode score is the mean over steps.",
        "",
        "## Status",
        "",
        "| Step | Milestones | Gold patch | F2P (repos) | P2P | Gates |",
        "| --- | --- | --- | --- | --- | --- |",
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
        "`/app/qdrant` is the contract repository, sparse: only `lib/api/src/grpc/proto/**/*.proto` (the wire contract the "
        "clients consume). It has no tests and nothing to build; contract edits are collected and applied like any other repo.",
        "",
        "## How grading works",
        "",
        "1. `csf-collect` writes `/logs/artifacts/model.<repo>.patch` for every repo (the agent's changes against "
        "`/opt/csf/base/<repo>.git`, which the agent cannot alter).",
        "2. The verifier builds a fresh container from `environment/`, applies each patch (changes under test dirs are dropped), "
        "copies `tests/hidden/<repo>/` in, starts a real Qdrant server (`tests/suites.json` `services`), runs the suites "
        "(pytest for qdrant-client, `cargo test --offline` for rust-client).",
        "3. `tests/grade.py` grades exactly the ids in `steps/<step>/tests/ids.json` (`f2p`: must pass, they failed before the "
        "gold patch; `p2p`: must keep passing). `reward` is 1 only if every F2P and P2P id passed in every repo.",
        "4. The ids were measured, not guessed: the gold patches were applied step by step in the task's own image and every "
        "test was run twice per state (`deepcommit/export/templates/measure.py`, `curate.py`); a test that failed in any run "
        "is excluded. pytest retries a failing test twice (congruence tests compare local mode with the server on random data).",
        "",
        "## Running it",
        "",
        "Needs Harbor 0.25 or newer (multi-step tasks, schema 1.4) and Docker. The image build clones the three repositories "
        "from GitHub and downloads toolchains, crates and the Qdrant server binary, so the build needs network; the trial itself "
        "needs none.",
        "",
        "```bash",
        "harbor run -p <this folder> -a oracle        # gold solution: every step must score 1",
        "harbor run -p <this folder> -a nop           # no change: step 1 must score 0",
        "harbor run -p <this folder> -a claude-code -m <model>",
        "```",
        "",
        "## Known limits",
        "",
        "- **Cross-repo coupling is weak.** The upstream clients lag the contract: server proto changes reach the clients in "
        "release-sync commits, so within one milestone the contract part and the client part rarely depend on each other. Steps "
        "whose gold touches several repositories are listed above; their hidden tests are each repository's own tests, so no test "
        "checks that two repositories agree with each other.",
        "- Instructions are generated from the gold patch and the hidden tests (`deepcommit/export/gen_instructions.py`), not "
        "from a full Phase 4 SRS.",
        "- `-X theirs` was used when replaying 11 background commits and about 25 test-only conflicts were auto-resolved during "
        "export (see `_src/csf-qdrant-clients-v1-001/export_report.json`); the measured tests all pass on the resulting states.",
        "- Milestones M35, M17_sub-02 and M17_sub-03 are not built (cherry-pick conflict on `poetry.lock`).",
        "- The Python tests that need FastEmbed models are skipped (no model download at run time); embedding-model tests are not part of any step.",
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
