"""Turn measured test outcomes into curated F2P / P2P id lists for an exported CSF task (the task's Phase 3).

Input: the JSON written by ``templates/measure.py`` (per step: ``pre`` = the step's own test files before the gold
patch, ``post`` = full suites after it; ``post`` may be a list of runs when measured more than once).

Rules, per step k (state k = base + gold of steps 1..k):
  F2P   a test of the step's own test files that did not pass before the gold patch and passes in EVERY post run
  P2P   a test of any earlier step's test files (or a core test) that passes in state k-1 and in state k, every run
A step with no F2P test cannot be graded: it is reported as ``unusable`` so the caller folds it into its successor.

Writes the ``checks`` of steps/<step>/tests/suite.json and its ``files`` (the files of those ids: test.sh runs files,
grade.py grades ids).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from .svc_layout import set_checks


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


def _is_vitest(tid: str) -> bool:
    """Vitest id ``repo::<package dir>::<file> > <suite> > <test>``."""
    parts = tid.split("::", 2)
    return len(parts) == 3 and " > " in parts[2] and parts[2].split(" > ", 1)[0].endswith((".ts", ".tsx", ".js", ".mjs"))


def _vitest_file(tid: str) -> str:
    parts = tid.split("::", 2)
    pkg = parts[1].rstrip('/')
    f = parts[2].split(' > ', 1)[0]
    return f if pkg in ('', '.') else f"{pkg}/{f}"


def _go_pkg_dir(tid: str) -> Optional[str]:
    """Go id ``repo::<import path>::<Test>``: repo-relative package dir ('.' for the module root), None for other ids."""
    parts = tid.split("::", 2)
    if len(parts) != 3 or "/" not in parts[1] or not re.match(r"^[\w.\-]+\.[\w.\-]+/", parts[1]):
        return None
    segs = parts[1].split("/")[3:]  # host/owner/module -> remainder
    return "/".join(segs) or "."


def _own_files(step_dir: Path) -> Dict[str, List[str]]:
    """The step's own test files per repo: ``own_files`` of tests/suite.json (written by render.py, kept by curation)."""
    suite = json.loads((step_dir / "tests/suite.json").read_text())
    own = suite.get("own_files")
    if own is None:  # a suite.json written before own_files existed: its f2p files
        own = {r: f.get("f2p", []) for r, f in suite.get("files", {}).items()}
    return {r: list(fs) for r, fs in own.items() if fs}


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
            elif _is_vitest(tid):
                is_own = (repo, _vitest_file(tid)) in own
            elif _go_pkg_dir(tid) is not None:
                is_own = any(str(Path(of).parent) == _go_pkg_dir(tid) for of in cur.own_files.get(repo, ()))
            else:  # cargo id ``<test binary>::<name>``: own when the binary is one of the step's tests/<binary>.rs files
                # (binary, module path..., name): own when a path component is the stem of one of the step's test files
                parts = set(tid.split("::")[1:])
                is_own = any(Path(of).stem in parts for of in cur.own_files.get(repo, ()) if Path(of).stem != "mod")
            if is_own and pre.get(tid) != "passed" and tid not in prev_pass:
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
    """Write every usable step's checks into tests/suite.json (deploy check per service, f2p, p2p) and the files
    test.sh runs for them; ``own_files`` is left as it is, so the task can be curated again."""
    for cur in curated:
        if not cur.usable:
            continue
        set_checks(task_dir / "steps" / cur.step, cur.f2p, cur.p2p, {"curation_note": cur.note} if cur.note else None)
