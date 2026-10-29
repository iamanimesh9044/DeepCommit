"""Turn measured test outcomes into curated F2P / P2P id lists for an exported CSF task (the task's Phase 3).

Input: the JSON written by ``templates/measure.py`` (per step: ``pre`` = the step's own test files before the gold
patch, ``post`` = full suites after it; ``post`` may be a list of runs when measured more than once).

Rules, per step k (state k = base + gold of steps 1..k):
  F2P   a test of the step's own test files that did not pass before the gold patch and passes in EVERY post run
  P2P   a test of any earlier step's test files (or a core test) that passes in state k-1 and in state k, every run
A step with no F2P test cannot be graded: it is reported as ``unusable`` so the caller folds it into its successor.

Writes steps/<step>/tests/ids.json and rewrites the F2P_/P2P_EARLIER_ lists of steps/<step>/tests/step.env to the
files of those ids (test.sh runs files; grade.py grades ids).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set


@dataclass
class StepCuration:
    step: str
    f2p: List[str] = field(default_factory=list)
    p2p: List[str] = field(default_factory=list)
    own_files: Dict[str, List[str]] = field(default_factory=dict)
    note: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.f2p)


def _runs(post) -> List[Dict[str, str]]:
    return post if isinstance(post, list) else [post]


def _passing(runs: List[Dict[str, str]]) -> Set[str]:
    """Ids that passed in every run."""
    if not runs:
        return set()
    sets = [{k for k, v in r.items() if v == "passed"} for r in runs]
    return set.intersection(*sets)


def _file_of(tid: str) -> Optional[str]:
    """Test file of a pytest id ``repo::path.py::name``; None for ids that carry no file (cargo)."""
    parts = tid.split("::", 2)
    return parts[1] if len(parts) >= 3 and parts[1].endswith(".py") else None


def _own_files(step_dir: Path) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for line in (step_dir / "tests/step.env").read_text().splitlines():
        m = re.match(r"F2P_([A-Z_]+)='(.*)'", line)
        if m and m.group(2).strip():
            out[m.group(1).lower().replace("_", "-")] = m.group(2).split()
    return out


def curate(measure: dict, task_dir: Path, max_p2p: int = 400) -> List[StepCuration]:
    results: List[StepCuration] = []
    earlier_files: Dict[str, Set[str]] = {}  # repo -> test files of steps already seen
    earlier_ids: Set[str] = set()  # ids that belonged to earlier steps' tests (cargo ids carry no file)
    prev_pass: Set[str] = set()
    for rec in measure["steps"]:
        step = rec["step"]
        sd = task_dir / "steps" / step
        pre, runs = rec["pre"], _runs(rec["post"])
        cur = StepCuration(step=step, own_files=_own_files(sd))
        post_pass = _passing(runs)
        if not post_pass:
            cur.note = "not measured"
            results.append(cur)
            continue
        own = {(repo, f) for repo, fs in cur.own_files.items() for f in fs}
        for tid in sorted(post_pass):
            repo = tid.split("::", 1)[0]
            f = _file_of(tid)
            if f:
                is_own = (repo, f) in own
            else:  # cargo id ``<test binary>::<name>``: own when the binary is one of the step's tests/<binary>.rs files
                # (binary, module path..., name): own when a path component is the stem of one of the step's test files
                parts = set(tid.split("::")[1:])
                is_own = any(Path(of).stem in parts for of in cur.own_files.get(repo, ()) if Path(of).stem != "mod")
            if is_own and pre.get(tid) != "passed":
                cur.f2p.append(tid)
        for tid in sorted(post_pass & prev_pass):
            repo = tid.split("::", 1)[0]
            f = _file_of(tid)
            if tid in earlier_ids or (f and f in earlier_files.get(repo, ())):
                cur.p2p.append(tid)
        kept: Dict[str, int] = {}
        capped = []
        for tid in cur.p2p:  # cap per repo, so a big suite cannot push another repo's tests out of the list
            repo = tid.split("::", 1)[0]
            kept[repo] = kept.get(repo, 0) + 1
            if kept[repo] <= max_p2p:
                capped.append(tid)
        if len(capped) < len(cur.p2p):
            cur.note = f"p2p capped at {max_p2p} per repo"
        cur.p2p = capped
        results.append(cur)
        for repo, fs in cur.own_files.items():
            earlier_files.setdefault(repo, set()).update(fs)
        earlier_ids.update(cur.f2p)
        prev_pass = post_pass
    return results


def write(task_dir: Path, curated: List[StepCuration]) -> None:
    for cur in curated:
        sd = task_dir / "steps" / cur.step / "tests"
        if not cur.usable:
            continue
        (sd / "ids.json").write_text(json.dumps({"f2p": cur.f2p, "p2p": cur.p2p}, indent=1) + "\n")
        env = (sd / "step.env").read_text().splitlines()
        keep = [ln for ln in env if not re.match(r"(F2P_|P2P_EARLIER_)", ln)]

        def files(ids: List[str]) -> Dict[str, List[str]]:
            out: Dict[str, List[str]] = {}
            for tid in ids:
                repo = tid.split("::", 1)[0]
                f = _file_of(tid) or "__suite__"
                if f not in out.setdefault(repo, []):
                    out[repo].append(f)
            return out

        for prefix, ids in (("F2P", cur.f2p), ("P2P_EARLIER", cur.p2p)):
            by_repo = files(ids)
            for repo in sorted(set(cur.own_files) | set(by_repo)):
                fs = [f for f in by_repo.get(repo, []) if f != "__suite__"]
                keep.append(f"{prefix}_{repo.upper().replace('-', '_')}='{' '.join(fs)}'")
        (sd / "step.env").write_text("\n".join(keep) + "\n")
