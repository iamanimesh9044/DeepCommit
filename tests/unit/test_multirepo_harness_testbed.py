"""Workspace testbed builder: real tiny git repos, 2 repos, M1 touches both, M2 touches only repo A."""

import csv
import json
import subprocess
from pathlib import Path

import pytest

from deepcommit.harness.prepare_repo import multirepo_testbed as mt


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, files: dict, msg: str) -> str:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _mkrepo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _commit(path, {"src/lib.py": "V = 0\n", "tests/test_lib.py": "def test_a(): pass\n", "README.md": "r\n"}, "base")
    _git(path, "tag", "v1")
    return path


@pytest.fixture
def ws(tmp_path):
    a = _mkrepo(tmp_path / "ckA")
    b = _mkrepo(tmp_path / "ckB")
    a1 = _commit(a, {"src/lib.py": "V = 1\n", "tests/test_lib.py": "def test_a(): pass\ndef test_n(): pass\n"}, "a1")
    b1 = _commit(b, {"src/lib.py": "V = 10\n", "tests/test_lib.py": "def test_b(): pass\n"}, "b1")
    a2 = _commit(a, {"src/lib.py": "V = 2\n", "tests/test_lib.py": "def test_a(): pass\ndef test_m(): pass\n"}, "a2")
    wsdir = tmp_path / "workspace"
    wsdir.mkdir()
    spec = lambda p: {  # noqa: E731
        "checkout_path": str(p),
        "start": "v1",
        "main_branch": "main",
        "repo_src_dirs": ["src/"],
        "test_dirs": ["tests/**"],
        "exclude": [],
    }
    (wsdir / "repos.json").write_text(json.dumps({"alpha": spec(a), "beta": spec(b)}))
    (wsdir / "commit_repos.json").write_text(json.dumps({a1[:7]: "alpha", b1[:7]: "beta", a2[:7]: "alpha"}))
    mcsv = tmp_path / "milestones.csv"
    with open(mcsv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "title", "category", "commits"])
        w.writerow(["M1", "cross", "feature", f"{a1[:7]};{b1[:7]}"])
        w.writerow(["M2", "alpha only", "feature", a2[:7]])
    dcsv = tmp_path / "dependencies.csv"
    dcsv.write_text("prerequisite_id,dependant_id\nM1,M2\n")
    return {"wsdir": wsdir, "mcsv": mcsv, "dcsv": dcsv, "out": tmp_path / "out", "a1": a1, "b1": b1, "a2": a2}


def test_partition_rows():
    rows = [{"id": "M1", "commits": "aaaaaaa;bbbbbbb", "integration_test_commit": ""}]
    out = mt.partition_milestone_rows(rows, ["x", "y"], {"aaaaaaa": "x", "bbbbbbb": "y"})
    assert out["x"]["M1"]["commits"] == "aaaaaaa" and out["y"]["M1"]["commits"] == "bbbbbbb"
    with pytest.raises(KeyError):
        mt.partition_milestone_rows(rows, ["x"], {"aaaaaaa": "x"})


def test_build_workspace_testbed(ws):
    res = mt.build_workspace_testbed(
        workspace_dir=ws["wsdir"], milestone_csv=ws["mcsv"], dependencies_csv=ws["dcsv"], output_dir=ws["out"]
    )
    assert res["failed"] == []
    tb = ws["out"] / "testbed"
    for repo in ("alpha", "beta"):
        for m in ("M1", "M2"):
            for t in ("start", "end"):
                _git(tb / repo, "rev-parse", f"milestone-{m}-{t}")
    man = json.loads((tb / "manifest.json").read_text())
    assert man["milestones"]["M1"]["alpha"]["changed"] and man["milestones"]["M1"]["beta"]["changed"]
    # M2 untouches beta -> start == end; alpha changed
    assert not man["milestones"]["M2"]["beta"]["changed"]
    assert man["milestones"]["M2"]["beta"]["base_sha"] == man["milestones"]["M2"]["beta"]["end_sha"]
    assert man["milestones"]["M2"]["alpha"]["changed"]
    # M2 beta base is M1's end in beta
    assert _git(tb / "beta", "rev-parse", f"{man['milestones']['M2']['beta']['base_sha']}^{{tree}}") == _git(
        tb / "beta", "rev-parse", f"{man['milestones']['M1']['beta']['end_sha']}^{{tree}}"
    )
    # end tree of alpha has V = 2
    assert "V = 2" in _git(tb / "alpha", "show", "milestone-M2-end:src/lib.py")
    assert "V = 1" in _git(tb / "alpha", "show", "milestone-M2-start:src/lib.py")
    # gold patches per repo
    assert (ws["out"] / "milestone_patches/M1/alpha.patch").read_text().count("src/lib.py") >= 1
    assert (ws["out"] / "milestone_patches/M1/beta.patch").read_text().strip()
    assert (ws["out"] / "milestone_patches/M2/beta.patch").read_text() == ""
    assert not (ws["out"] / "milestone_patches/M1.patch").exists()
    # metadata
    md = json.loads((ws["out"] / "metadata.json").read_text())
    m1 = next(m for m in md["milestones"] if m["id"] == "M1")
    assert m1["repos_changed"] == ["alpha", "beta"]
    assert m1["repos"]["alpha"]["src_files"] == ["src/lib.py"]
    assert m1["repos"]["alpha"]["test_files"] == []  # start already carries end-state tests
    assert m1["repos"]["beta"]["commits"] == ws["b1"][:7] or m1["repos"]["beta"]["commits"] == ws["b1"]
    m2 = next(m for m in md["milestones"] if m["id"] == "M2")
    assert m2["repos_changed"] == ["alpha"]
    assert set(md["repos"]) == {"alpha", "beta"}
    rep = json.loads((ws["out"] / "build_report.json").read_text())
    assert rep["summary"] == {"success": 2}
    assert rep["milestones"]["M2"]["repos"]["beta"]["num_commits_csv"] == 0


def test_conflict_report_per_repo(ws, tmp_path):
    # make M1's alpha commit conflict: rewrite alpha so a1 cannot apply on base via a divergent M0-style setup
    a = Path(json.loads((ws["wsdir"] / "repos.json").read_text())["alpha"]["checkout_path"])
    # new milestone M3 with a commit that edits a line absent on its base (parent-less, based on v1 w/ other history)
    _git(a, "checkout", "-b", "side", "v1")
    _commit(a, {"src/lib.py": "V = 0\nW = 1\n"}, "side1")
    c2 = _commit(a, {"src/lib.py": "V = 0\nW = 2\n"}, "side2")
    _git(a, "checkout", "main")
    cr = json.loads((ws["wsdir"] / "commit_repos.json").read_text())
    cr[c2[:7]] = "alpha"
    (ws["wsdir"] / "commit_repos.json").write_text(json.dumps(cr))
    with open(ws["mcsv"], "a", newline="") as f:
        csv.writer(f).writerow(["M3", "broken", "feature", c2[:7]])
    res = mt.build_workspace_testbed(
        workspace_dir=ws["wsdir"],
        milestone_csv=ws["mcsv"],
        dependencies_csv=ws["dcsv"],
        output_dir=ws["out"],
        continue_on_error=True,
        preserve_commit_order=True,
    )
    assert res["failed"] == ["M3"]
    rep = json.loads((ws["out"] / "build_report.json").read_text())
    m3 = rep["milestones"]["M3"]
    assert m3["status"] == "conflict" and m3["failing_repos"] == ["alpha"]
    assert m3["repos"]["alpha"]["failing_commit"] == c2[:7] or m3["repos"]["alpha"]["failing_commit"] == c2
    assert m3["repos"]["beta"]["status"] in ("success",)
    assert not (ws["out"] / "testbed" / "manifest.json").exists()


def test_snapshot_paths_rust_unchanged_and_languages():
    from deepcommit.harness.utils import snapshot as sn

    assert sn.get_snapshot_paths(["src/"]) == ["src/", "Cargo.toml", "Cargo.lock"]
    assert sn.get_snapshot_paths(["src/"], {"Cargo.lock"}, {"src/"}) == ["src/", "Cargo.lock"]
    assert sn.root_build_files_for(["rust"]) == ["Cargo.toml", "Cargo.lock"]
    p = sn.get_workspace_snapshot_paths("sdk", ["pkg/"], ["go", "proto"], existing_root_files={"go.mod", "buf.yaml"})
    assert p == ["pkg/", "go.mod", "buf.yaml", "*.proto"]
    assert sn.root_build_files_for(["python", "javascript"]) == ["pyproject.toml", "uv.lock", "package.json"]
